# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — org_sync：钉钉组织架构 → RDS 快照（node-ACL 生命线）

  · dept_dim / staff_dim   全量快照（rev 递增；整轮失败保留上轮——半截快照比没有更危险）
  · dept_admin_node_candidate   admin-node auto 派生【只写候选】，待 kb_admin 控制台确认
    （候选/权威分表防静默提权，schema/061；权威表 dept_admin_node_grant 本作业绝不写）
  · 孤儿授权检测   文档授权指向已消失节点 → 只告警不自动删（exit 2 让 DW 红灯引人来看）

⚠️ 这是节点通道的保鲜生命线：组织快照 >48h 未刷新，读侧 node_channel_ok 一律 fail-close
   （node 文档不可见）。本节点必须【每日】跑 --commit；dry-run 不刷新快照，不能长期停留。

建议调度：每日 01:00 Asia/Shanghai（错开 scan_oss 00:02 / ops_health 02:30 / retention 03:30），
资源组同 stage 节点。独立节点，不进 stage 工作流依赖链。新建节点走 DataStudio 控制台。

上线节奏（与 retention 同哲学）：
  阶段1（首次手动跑）：DRY_RUN=True 观察差异报告（部门/员工增减、孤儿授权，零写）。
  阶段2（确认差异合理后立即）：改 DRY_RUN=False → 保存 → 发布每日调度。
退出码：0=ok；2=孤儿授权告警（快照【已提交】，但有文档授权指向消失节点——处置走控制台，
不删授权）；3=同步失败（整轮放弃，保留上轮快照）。

凭据：本文件【不含明文密钥】。控制台粘贴时在「凭据」标记处填：
  · RDS 三件套 + DASHSCOPE key —— 从【清理stage3】节点顶部原样复制；
  · DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET —— 拉组织架构用（企业内部应用凭证，
    与 SAE 服务应用同一对；从 SAE 应用环境变量或本地 .env 复制。RB-06 轮换族成员）。
"""
import os
import sys
import subprocess
import zipfile

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖（纯 RDS + 钉钉开放平台 HTTP 作业；不装 oss2/ha3/pdf 族）
# ═══════════════════════════════════════════════════════════════
# py3.7 serverless 执行器钉真兼容版（同 retention_node 分支法）
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
# production 启动须显式表态(config.py 守卫;2026-08-02 起代码包从 main 打,此前为 op0 包,两侧同款)。
# 这两个 flag 只被 api/retriever/readiness 读(服务侧),摄取与运维脚本零读取,设 true 无行为影响。
os.environ["RAG_REQUIRE_AUTH"] = "true"
os.environ["RAG_ACL_FAIL_CLOSED"] = "true"
# org_sync 是纯 RDS + 钉钉 HTTP 作业，不碰检索后端/OSS。显式声明这两路走 mock：
#   ① 短路 config 的 production 完整性守卫 R5（production 必须有检索后端）；
#   ② 免配 HA3/OSS 凭据。RDS 仍真实（simulate_db 不设 → 继承 false → 真连生产 RDS）。
os.environ["RAG_SIMULATE_OPENSEARCH"] = "true"
os.environ["RAG_SIMULATE_OSS"] = "true"

# 阶段开关：阶段1 = True（dry-run 只报差异，零写）；阶段2 = False（--commit 真写快照）。
# ⚠️ dry-run 不刷新快照——快照 >48h 节点通道 fail-close，阶段1 只允许停留一次手动跑。
DRY_RUN = True

# ── 凭据：取消注释并填真值（RDS/DASHSCOPE 从【清理stage3】复制；DINGTALK 从 SAE env）──
# os.environ["DASHSCOPE_API_KEY"]        = "..."   # 本节点不调 LLM，production 守卫要求配（防 Gemini 误用）
# os.environ["RAG_RDS_HOST"]             = "..."
# os.environ["RAG_RDS_PORT"]             = "..."
# os.environ["RAG_RDS_USER"]             = "..."
# os.environ["RAG_RDS_PASSWORD"]         = "..."
# os.environ["RAG_RDS_DATABASE"]         = "..."
# os.environ["DINGTALK_CLIENT_ID"]       = "..."   # 钉钉企业内部应用 AppKey（拉组织架构）
# os.environ["DINGTALK_CLIENT_SECRET"]   = "..."   # 对应 AppSecret
# ─────────────────────────────────────────────────────────────────────────────

_required = ["DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD",
             "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"]
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
# 3. 运行组织同步（DataWorks 以退出码判成败；2/3 都会标失败引人来看）
# ═══════════════════════════════════════════════════════════════
import opensearch_pipeline  # noqa: E402
print("opensearch_pipeline:", opensearch_pipeline.__file__)
from opensearch_pipeline.org_sync import main  # noqa: E402

sys.exit(main([] if DRY_RUN else ["--commit"]))
