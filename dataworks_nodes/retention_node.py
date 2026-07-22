# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — log_retention：日志/审计表留存（F-36）

  · qa_blobs      qa_session_log.content_blocks_json 置 NULL（>6 月）   写 fuling_operation
  · qa_rows       qa_session_log 整行删除（>18 月；rollup 活性守卫）    写 fuling_operation
  · audit         kb_audit_log 整行删除（>24 月）                        写 fuling_knowledge
  · pipeline_run  pipeline_run 整行删除（>12 月）                        写 fuling_knowledge
  · findings      document_sensitive_finding（>24 月 且 非当前版本）     写 fuling_knowledge

策略/守卫详见 opensearch_pipeline/retention.py 模块 docstring（dry-run 默认、
批量短事务、rollup 活性守卫、当前版本 finding 永不删）。

建议调度：每日一次，错开 stage 节点与 ops_health_monitor（如 03:30 Asia/Shanghai），
资源组 data_process。⚠️ 新建节点走 DataStudio 控制台（node id >2^53 MCP 改不动）。

上线节奏（与 ops 节点同哲学）：
  阶段1（先跑数天）：DRY_RUN 观察每日将影响行数（本文件默认形态，零写）。
  阶段2（用户确认窗口后）：把 DRY_RUN 改 False —— 真删。
退出码：0=ok；2=守卫拦下（rollup 死掉时 qa_rows 被拒——先修 rollup）；3=作业失败。

凭据：本文件【不含明文密钥】。控制台粘贴时从【清理stage3】节点顶部原样复制
RAG_* 赋值贴到「凭据」标记处（仅需 RDS 三件套，RAG_NO_MODEL_RESOLUTION=ack 豁免模型 key；
不碰 OSS/HA3）。
"""
import os
import sys
import subprocess
import zipfile

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖（PyODPS 3.7 pod 无 pymysql/dbutils；纯 RDS 作业，不装 oss2/ha3）
# ═══════════════════════════════════════════════════════════════
# py3.7 serverless 执行器钉真兼容版（批次9 同族清扫，同 stage3_node 分支法）
if sys.version_info >= (3, 8):
    DEPS = ["PyMySQL", "DBUtils", "requests"]
else:
    DEPS = ["PyMySQL==1.1.1", "DBUtils==3.1.2", "requests==2.31.0"]
subprocess.check_call([
    sys.executable, "-m", "pip", "install", *DEPS, "-t", "/tmp/pydeps", "-q"
])
subprocess.call([sys.executable, "-m", "pip", "freeze", "--path", "/tmp/pydeps"])   # δ3（M13）：传递闭包可见化（冻结回填 user-gated）
if "/tmp/pydeps" not in sys.path:
    sys.path.insert(0, "/tmp/pydeps")

# ═══════════════════════════════════════════════════════════════
# 1. 环境（必须在 import pipeline 代码之前；config.py 首次 import 即读取）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = "false"
os.environ["RAG_ENVIRONMENT"] = "production"

# ── 生产安全姿态断言(批次5 P0-07d)——不设这两行,节点在 load_config() 就 ValueError 崩 ──
# DataWorks 代码包从 claude/ontology-p0 打(≠main),production 启动须显式表态。
# 这两个 flag 只被 api/retriever/readiness 读(服务侧),摄取与运维脚本零读取,设 true 无行为影响。
# 2026-07-21 stage3 实地踩过;另一条路 RAG_ALLOW_LEGACY_OPEN_PROD=ack:<当日> 午夜过期,不适合调度任务。
os.environ["RAG_REQUIRE_AUTH"] = "true"
os.environ["RAG_ACL_FAIL_CLOSED"] = "true"
# retention 是纯 RDS 作业，不碰检索后端/OSS。显式声明这两路走 mock：
#   ① 短路 config 的 production 完整性守卫 R5（config.py:501「production 必须有检索后端，
#      否则 EnvironmentMismatchError」）——2026-07-02 首跑即撞它；
#   ② 免配 HA3/OSS 凭据（本节点不需要）。
# RDS 仍真实：simulate_db 不设 → 继承 RAG_SIMULATE=false → 真连生产 RDS；retention.py 的
# `if cfg.simulate or cfg.simulate_db: skip` 也不会误跳（两者均 false）。
os.environ["RAG_SIMULATE_OPENSEARCH"] = "true"
os.environ["RAG_SIMULATE_OSS"] = "true"

# ⚠️ 归档默认关（2026-07-11 重审计 §2「retention node OSS 死锁」）：本节点 OSS 恒 mock
# （上一行）——而 retention.py 的 qa_rows/audit/agent_audit 三作业在 --commit 下**默认
# 删前归档**（_archive_batch 需真 OSS，mock 即 hard-raise → main() 返回 3 → 节点 exit 3，
# 阶段2 首批到期行就翻车）。docs/blindspot_audit_fix_status.md P3 部署注记早已写明
# 「无 OSS 环境显式设 RAG_RETENTION_ARCHIVE=false」，此前节点文件没同步——现在对齐：
# 纯 RDS 形态 = 直删语义（旧 F-36 行为）。要恢复删前归档：本行改 "true" +
# RAG_SIMULATE_OSS 改 "false" + 凭据区补 OSS 三件套（RAG_OSS_ENDPOINT/AK/SK + bucket）。
os.environ.setdefault("RAG_RETENTION_ARCHIVE", "false")

# 阶段开关：阶段1 = True（dry-run 只报数）；
# 阶段2 = False + 打开 RAG_RETENTION_ENABLE（归档语义见上——保持 ARCHIVE=false 即直删）
DRY_RUN = True
if not DRY_RUN:
    os.environ["RAG_RETENTION_ENABLE"] = "true"   # retention.py 的第二道闸

# 留存窗口按需覆盖（不设即用 retention.py 默认 6/18/24/12/24 月）：
# os.environ["RAG_RETENTION_QA_BLOBS_MONTHS"]     = "6"
# os.environ["RAG_RETENTION_QA_MONTHS"]           = "18"
# os.environ["RAG_RETENTION_AUDIT_MONTHS"]        = "24"
# os.environ["RAG_RETENTION_PIPELINE_RUN_MONTHS"] = "12"
# os.environ["RAG_RETENTION_FINDING_MONTHS"]      = "24"

# ── 凭据：粘贴【清理stage3】顶部的 RAG_* 赋值（取消注释并填真值）────────────────
# os.environ["RAG_RDS_HOST"]      = "..."
# os.environ["RAG_RDS_PORT"]      = "..."
# os.environ["RAG_RDS_USER"]      = "..."
# os.environ["RAG_RDS_PASSWORD"]  = "..."
# os.environ["RAG_RDS_DATABASE"]  = "..."
# ─────────────────────────────────────────────────────────────────────────────

# P1-15：纯 RDS 作业不再要求 DashScope key——RAG_NO_MODEL_RESOLUTION=ack 令 config 把
# llm/ocr/vlm/embedding 全解析为惰性哨兵（无供应商端点，意外模型调用立刻失败），
# 生产供应商守卫据此豁免 key 要求（禁 Gemini 检查照跑）。本节点只碰 RDS。
os.environ.setdefault("RAG_NO_MODEL_RESOLUTION", "ack")
_required = ["RAG_RDS_HOST", "RAG_RDS_PASSWORD"]
_missing = [v for v in _required if not os.environ.get(v)]
if _missing:
    raise RuntimeError("缺少生产环境变量（从【清理stage3】顶部复制 RAG_* 赋值）: %s" % _missing)

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
# 3. 运行留存作业（DataWorks 以退出码判成败；2/3 都会标失败引人来看）
# ═══════════════════════════════════════════════════════════════
import opensearch_pipeline  # noqa: E402
print("opensearch_pipeline:", opensearch_pipeline.__file__)
from opensearch_pipeline.retention import main  # noqa: E402

sys.exit(main([] if DRY_RUN else ["--commit"]))
