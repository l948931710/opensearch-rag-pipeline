# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — doc_update_notify：文档升版可见范围提醒（日调）

  · discover  扫「版本切换已生效但未建事件」的文档 → doc_update_event（schema/065）
  · resolve   按【权威】判定受众（acl_policy.can_read_doc + 姿态见证）→ doc_update_notice
  · send      钉钉工作通知按人日摘要投递（console 通道无需投递，行本身即站内信）

设计稿：docs/doc_update_notify_design_2026-08-04_DRAFT.md（rev3 + §11 Sam 拍板）。

⚠️ 判定姿态**不取本节点 env**：serving 把当前生效的 ACL flag 姿态盖在 RDS 见证行
   （rag_runtime_contract.acl_posture），本作业读见证值判定。见证缺失/超 24h/组织快照
   >48h ⇒ 整轮 HOLD + 退出码 3，绝不按本地 env 猜（那会让通知面与检索面脱钩：
   宽了就是把标题推给线上读不到该文档的人，窄了就是整批语料静默零通知）。

⚠️ 事件表 doc_update_event 是防重放台账，retention 永不清它；notice 行 6 个月窗 +
   进主体擦除清单。

建议调度：每日 07:30 Asia/Shanghai（在 org_sync 00:15 与夜间 stage-3 之后 ⇒ 组织快照与
版本切换都已收敛；上班前送达）。独立节点，不进 stage 工作流依赖链。

上线节奏（与 retention/org_sync 同哲学）：
  阶段1（首次手动跑）：DRY_RUN=True —— 全链只读，打印会建哪些事件/受众规模，零写零投递。
  阶段2（确认合理后）：改 DRY_RUN=False → 保存 → 发布每日调度。
退出码：0=ok；2=守卫拦下（总闸未开 / schema/065 未 apply）；3=运行失败或整轮 HOLD。

凭据：本文件【不含明文密钥】。控制台粘贴时在「凭据」标记处填：
  · RDS 三件套 + DASHSCOPE key —— 从【清理stage3】节点顶部原样复制；
  · DINGTALK_CLIENT_ID / CLIENT_SECRET / RAG_DINGTALK_AGENT_ID —— 工作通知投递用
    （与 org_sync 同一对企业内部应用凭证 + 该应用的 AgentId）。
"""
import os
import sys
import subprocess
import zipfile

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖（纯 RDS + 钉钉开放平台 HTTP 作业；不装 oss2/ha3/pdf 族）
# ═══════════════════════════════════════════════════════════════
# py3.7 serverless 执行器钉真兼容版（同 retention_node/org_sync_node 分支法）
if sys.version_info >= (3, 8):
    DEPS = ["PyMySQL", "DBUtils", "requests"]
else:
    DEPS = ["PyMySQL==1.1.1", "DBUtils==3.1.2", "requests==2.31.0"]
subprocess.check_call([
    sys.executable, "-m", "pip", "install", *DEPS, "-t", "/tmp/pydeps", "-q"
])
subprocess.call([sys.executable, "-m", "pip", "freeze", "--path", "/tmp/pydeps"])   # δ3（M13）：传递闭包可见化
if "/tmp/pydeps" not in sys.path:
    sys.path.insert(0, "/tmp/pydeps")

# ═══════════════════════════════════════════════════════════════
# 1. 环境（必须在 import pipeline 代码之前；config.py 首次 import 即读取）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = "false"
os.environ["RAG_ENVIRONMENT"] = "production"

# ── 生产安全姿态断言(批次5 P0-07d)——不设这两行,节点在 load_config() 就 ValueError 崩 ──
os.environ["RAG_REQUIRE_AUTH"] = "true"
os.environ["RAG_ACL_FAIL_CLOSED"] = "true"
# 本作业纯 RDS + 钉钉 HTTP，不碰检索后端/OSS：① 短路 config 的 production 完整性守卫 R5；
# ② 免配 HA3/OSS 凭据。RDS 仍真实（simulate_db 不设 → 继承 false → 真连生产 RDS）。
os.environ["RAG_SIMULATE_OPENSEARCH"] = "true"
os.environ["RAG_SIMULATE_OSS"] = "true"

# 总闸与子闸（调度 env 可覆盖 ⇒ 用 setdefault；Sam 在 SAE/DW 侧翻闸无需重贴节点）。
# ⚠️ ACL 判定姿态**不在这里**——它来自 serving 盖的见证行，见模块头注。
os.environ.setdefault("RAG_DOC_NOTIFY", "true")
os.environ.setdefault("RAG_DOC_NOTIFY_DINGTALK", "false")   # console 先行 ≥1 周（Sam 拍板）

# 阶段开关：阶段1 = True（dry-run 全链只读）；阶段2 = False（--commit 真写真投递）。
DRY_RUN = True

# ── 凭据：取消注释并填真值（RDS/DASHSCOPE 从【清理stage3】复制；DINGTALK 从 SAE env）──
# os.environ["DASHSCOPE_API_KEY"]        = "..."   # 本节点不调 LLM，production 守卫要求配（防 Gemini 误用）
# os.environ["RAG_RDS_HOST"]             = "..."
# os.environ["RAG_RDS_PORT"]             = "..."
# os.environ["RAG_RDS_USER"]             = "..."
# os.environ["RAG_RDS_PASSWORD"]         = "..."
# os.environ["RAG_RDS_DATABASE"]         = "..."
# os.environ["DINGTALK_CLIENT_ID"]       = "..."   # 钉钉企业内部应用 AppKey（发工作通知）
# os.environ["DINGTALK_CLIENT_SECRET"]   = "..."   # 对应 AppSecret
# os.environ["RAG_DINGTALK_AGENT_ID"]    = "..."   # 该应用的 AgentId（工作通知必需）
# ─────────────────────────────────────────────────────────────────────────────

# AgentId/钉钉凭据只在钉钉子闸打开时才是硬需求：console-only 阶段缺它们不该拦住整轮。
_required = ["DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD"]
if (os.environ.get("RAG_DOC_NOTIFY_DINGTALK", "") or "").strip().lower() in ("1", "true", "yes", "on"):
    _required += ["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "RAG_DINGTALK_AGENT_ID"]
_missing = [v for v in _required if not os.environ.get(v)]
if _missing:
    raise RuntimeError("缺少生产环境变量: %s" % _missing)

# ═══════════════════════════════════════════════════════════════
# 2. 下载并解压代码包（与 stage 节点同款；odps 为 PyODPS 隐式入口对象）
# ═══════════════════════════════════════════════════════════════
print("=== 下载 Archive 资源 opensearch_pipeline_production.zip ===")
resource = odps.get_resource('opensearch_pipeline_production.zip')  # noqa: F821 (PyODPS 运行时注入)
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline_production.zip', 'wb') as writer:
        writer.write(reader.read())
def _safe_extractall(zf, dest):
    """B4（生产级外审 2026-07-17 P1-02/P1-03）安全解压：Zip-Slip 越界 + 加密成员 +
    成员数/单成员/总量/压缩比预算——资源包被替换成恶意 archive 时，不能借解压写任意
    路径或耗尽磁盘/内存。预算≈生产包实测 10×：成员≤4000、单成员≤200MB、总量≤500MB、
    压缩比≤200:1。"""
    dest_root = os.path.abspath(dest)
    infos = zf.infolist()
    if len(infos) > 4000:
        raise RuntimeError("zip 成员数超预算: %d > 4000" % len(infos))
    total = 0
    for info in infos:
        name = info.filename
        target = os.path.abspath(os.path.join(dest_root, name))
        if not (target == dest_root or target.startswith(dest_root + os.sep)):
            raise RuntimeError("zip 成员越界（Zip-Slip）: %r" % name)
        if info.flag_bits & 0x1:
            raise RuntimeError("zip 加密成员（拒绝）: %r" % name)
        if info.file_size > 200 * 1024 * 1024:
            raise RuntimeError("zip 单成员超预算: %r（%d bytes）" % (name, info.file_size))
        if info.compress_size and info.file_size / float(info.compress_size) > 200:
            raise RuntimeError("zip 压缩比异常（疑似 zip-bomb）: %r" % name)
        total += info.file_size
    if total > 500 * 1024 * 1024:
        raise RuntimeError("zip 总解压量超预算: %d bytes" % total)
    zf.extractall(dest_root)


def _verify_zip_integrity(zip_name):
    """B4（P1-02）+δ3（M13，Majors 批次 δ，codex 共识 2026-07-21）制品完整性：算 zip
    sha256 并留痕；有 sidecar 资源 <zip_name>.sha256 时硬比对（不匹配=资源被替换，拒绝
    执行）；暂缺=默认过渡期放行（打包侧 deploy/build_dataworks_zip.sh 随包生成）。
    δ3 增量：①RAG_DW_SIDECAR_STRICT=on（调度 env）→ 缺 sidecar/不可读/无 HMAC key 均
    raise（默认 off=现状放行+显著警告）；②调度 env 配 RAG_DW_ZIP_HMAC_KEY → 对
    <zip_name>.sha256.hmac 硬验（信任边界分离：能替换资源者可连 sidecar 一起换，但读
    不到调度 env 就伪造不了签名；打包侧 --hmac 产第三份资源）。威胁模型边界（如实）：
    能改节点代码本身或能读调度 env 的攻击者不在本防护面。"""
    import hashlib
    import hmac as _hmac
    _strict = (os.environ.get('RAG_DW_SIDECAR_STRICT', '') or '').strip().lower() \
        in ('1', 'true', 'yes', 'on')
    _key = (os.environ.get('RAG_DW_ZIP_HMAC_KEY', '') or '').strip()
    h = hashlib.sha256()
    with open(zip_name, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    digest = h.hexdigest()
    print("[B4] %s sha256=%s" % (zip_name, digest))
    if _strict and not _key:
        raise RuntimeError("[δ3 M13] RAG_DW_SIDECAR_STRICT=on 但调度 env 缺 "
                           "RAG_DW_ZIP_HMAC_KEY——无 key 无从硬验，拒绝执行")
    try:
        _sc = odps.get_resource(zip_name + '.sha256')  # noqa: F821 — DataWorks 运行时注入
        with _sc.open(mode='r') as r:
            expected = (r.read() or '').strip().split()[0].lower()
    except Exception as e:  # noqa: BLE001 — 默认过渡期放行；STRICT 硬拒
        if _strict:
            raise RuntimeError("[δ3 M13] STRICT：sidecar %s.sha256 不存在/不可读"
                               "（重新上传 zip+sidecar 三份资源）: %s" % (zip_name, e))
        print("[B4] ⚠️ sidecar %s.sha256 不存在/不可读（过渡期放行；"
              "RAG_DW_SIDECAR_STRICT=on 将硬拒）: %s" % (zip_name, e))
        return digest
    if expected != digest:
        raise RuntimeError("[B4] 制品完整性校验失败: sha256=%s != sidecar=%s"
                           % (digest, expected))
    if _key:
        try:
            _hc = odps.get_resource(zip_name + '.sha256.hmac')  # noqa: F821 — 同上注入
            with _hc.open(mode='r') as r:
                _sig = (r.read() or '').strip().split()[0].lower()
        except Exception as e:  # noqa: BLE001 — key 已配则 hmac sidecar 必须在场
            raise RuntimeError("[δ3 M13] 调度 env 已配 HMAC key 但 %s.sha256.hmac 缺失/"
                               "不可读（打包侧用 --hmac 重产并三份同批上传）: %s"
                               % (zip_name, e))
        _want = _hmac.new(_key.encode('utf-8'), digest.encode('ascii'),
                          hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(_sig, _want):
            raise RuntimeError("[δ3 M13] HMAC 校验失败：sha256 sidecar 可被连带伪造，"
                               "签名对不上调度 env key，拒绝执行")
        print("[δ3] ✅ HMAC 校验通过（信任边界=调度 env key）")
    print("[B4] ✅ 制品完整性校验通过（sidecar 匹配）")
    return digest


_verify_zip_integrity('opensearch_pipeline_production.zip')
with zipfile.ZipFile('opensearch_pipeline_production.zip', 'r') as zf:
    _safe_extractall(zf, '.')
_cur = os.path.abspath('.')
if _cur not in sys.path:
    sys.path.insert(0, _cur)

# ═══════════════════════════════════════════════════════════════
# 3. 运行升版提醒作业（DataWorks 以退出码判成败；2/3 都会标失败引人来看）
# ═══════════════════════════════════════════════════════════════
import opensearch_pipeline  # noqa: E402
print("opensearch_pipeline:", opensearch_pipeline.__file__)
from opensearch_pipeline.doc_update_notify import main  # noqa: E402

sys.exit(main([] if DRY_RUN else ["--commit"]))
