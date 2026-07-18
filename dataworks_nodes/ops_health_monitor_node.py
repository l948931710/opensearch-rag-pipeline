# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — ops_health_monitor：标准健康巡检
  · reconcile_ha3  RDS active+INDEXED chunk ⇄ HA3 PK（抓静默丢失/消失文档）  只读
  · reconcile_oss  active chunk 图片 oss_key ⇄ OSS 对象（抓坏图）            只读
  · reconcile_raw  raw 文档 ⇄ OSS                                            只读
  · qa_rollup      qa_session_log → qa_daily_metrics + SLO 判定              写 qa_daily_metrics

复用既有【暂停】节点 ops_health_monitor（dev node Id 5203574917819388193；cron 00 30 02 * * ?
= 02:30 Asia/Shanghai；资源组 data_process；依赖 default_workspace_6na2_root）。
⚠️ 该 dev node id > 2^53，DataWorks MCP 改不了 → 本脚本正文须在 DataStudio 控制台手动粘贴。

凭据：本文件【不含明文密钥】。在控制台粘贴时，从【清理stage3】节点顶部原样复制 RAG_* 的
      os.environ 赋值，贴到下方「凭据」标记处（与 stage 节点同源同值）。

阶段化上线（reconciler 先行，零写风险）：
  阶段1（先上）：main(["--only","reconcile_ha3","reconcile_oss"])  — 纯只读，不碰 qa_daily_metrics
  阶段2（验稳后）：main([])                                          — 加 qa_rollup（写库，production 同款凭据即可）
  （reconcile_raw 默认不在阶段1：它会因 1 个已知良性缺口恒返回 2，掩盖真实漂移信号——
    要么先 triage 那个缺口、要么阶段2 全量时再纳入。）
"""
import os
import sys
import subprocess
import zipfile

# ═══════════════════════════════════════════════════════════════
# 0. 安装存储依赖（关键！）
#    PyODPS 默认 pod = Python 3.7，site-packages【不含】pymysql/oss2/ha3 SDK——
#    与 stage 节点一样必须在节点内 pip 安装（装到 /tmp/pydeps 再加进 sys.path）。
#    与 stage3_node 同款集：reconcile_ha3 → retriever._get_ha3_client 需要 ha3 SDK；
#    reconcile_oss → oss2；_get_db_conn → PyMySQL+DBUtils；embedding 走 requests（非 SDK）。
#    pdfplumber/pypdf 监控本身不用，保留以镜像 stage3 已验证集、规避 retriever 传递 import。
# ═══════════════════════════════════════════════════════════════
# py3.7 陷阱（批次9 S5/S6，同 stage3_node）: serverless 执行器实为 Python 3.7，
# pypdf 5.0.0 元数据谎报支持 3.7 实则用 typing.Protocol(3.8+) → 必须钉真兼容版。
# 镜像恢复 3.8+ 后自动走现代分支，本段无需再改。
if sys.version_info >= (3, 8):
    DEPS = [
        "PyMySQL", "DBUtils", "oss2", "requests",
        "alibabacloud_ha3engine_vector",
        "pdfplumber", "pypdf",
    ]
else:
    DEPS = [
        "PyMySQL==1.1.1", "DBUtils==3.1.2", "oss2==2.19.1", "requests==2.31.0",
        "alibabacloud_ha3engine_vector==1.1.19",
        "typing_extensions==4.7.1",
        "pypdf==3.17.4", "pdfplumber==0.9.0", "Pillow==9.5.0",
    ]
subprocess.check_call([
    sys.executable, "-m", "pip", "install", *DEPS, "-t", "/tmp/pydeps", "-q"
])
if "/tmp/pydeps" not in sys.path:
    sys.path.insert(0, "/tmp/pydeps")

# ═══════════════════════════════════════════════════════════════
# 1. 环境（必须在 import pipeline 代码之前；config.py 首次 import 即读取）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = "false"
os.environ["RAG_ENVIRONMENT"] = "production"

# ── 凭据：粘贴【清理stage3】顶部的 RAG_* 赋值（取消注释并填真值，或直接整段拷过来）──────
# os.environ["DASHSCOPE_API_KEY"]         = "..."  # 监控不调 LLM，但 config 守卫 R5 要求 production 必须配
# os.environ["RAG_RDS_HOST"]              = "..."
# os.environ["RAG_RDS_PORT"]              = "..."
# os.environ["RAG_RDS_USER"]              = "..."
# os.environ["RAG_RDS_PASSWORD"]          = "..."
# os.environ["RAG_RDS_DATABASE"]          = "..."
# os.environ["RAG_OSS_ENDPOINT"]          = "..."
# os.environ["RAG_OSS_ACCESS_KEY_ID"]     = "..."
# os.environ["RAG_OSS_ACCESS_KEY_SECRET"] = "..."
# os.environ["RAG_OSS_BUCKET_NAME"]       = "..."
# os.environ["RAG_HA3_ENDPOINT"]          = "..."
# os.environ["RAG_HA3_TABLE_NAME"]        = "..."
# ── 告警 webhook（不配则 OBS-4 告警是【静默 no-op】——退出码仍会置位，但不会推送）──────
# os.environ["RAG_OPS_ALERT_WEBHOOK"]     = "..."
# os.environ["RAG_OPS_ALERT_SECRET"]      = "..."
# ────────────────────────────────────────────────────────────────────────────────────

_required = ["DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD",
             "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
             "RAG_HA3_ENDPOINT", "RAG_HA3_TABLE_NAME"]
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
    """B4（P1-02）制品完整性：算 zip sha256 并留痕（运行结果可溯源到构建）；有
    sidecar 资源 <zip_name>.sha256 时硬比对（不匹配=资源被替换，拒绝执行）；暂缺=
    过渡期放行（旧包无 sidecar 不误伤；打包侧 deploy/build_dataworks_zip.sh 随包生成）。"""
    import hashlib
    h = hashlib.sha256()
    with open(zip_name, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    digest = h.hexdigest()
    print("[B4] %s sha256=%s" % (zip_name, digest))
    try:
        _sc = odps.get_resource(zip_name + '.sha256')  # noqa: F821 — DataWorks 运行时注入
        with _sc.open(mode='r') as r:
            expected = (r.read() or '').strip().split()[0].lower()
    except Exception as e:  # noqa: BLE001 — 过渡期：sidecar 未上传时放行
        print("[B4] ⚠️ sidecar %s.sha256 不存在/不可读（过渡期放行）: %s" % (zip_name, e))
        return digest
    if expected != digest:
        raise RuntimeError("[B4] 制品完整性校验失败: sha256=%s != sidecar=%s"
                           % (digest, expected))
    print("[B4] ✅ 制品完整性校验通过（sidecar 匹配）")
    return digest


_verify_zip_integrity('opensearch_pipeline_production.zip')
with zipfile.ZipFile('opensearch_pipeline_production.zip', 'r') as zf:
    _safe_extractall(zf, '.')
_cur = os.path.abspath('.')
if _cur not in sys.path:
    sys.path.insert(0, _cur)
# 注：存储依赖已在第 0 段 pip 装好（3.7 pod 自带 site-packages 没有它们）。

# ═══════════════════════════════════════════════════════════════
# 3. 运行健康巡检
# ═══════════════════════════════════════════════════════════════
import opensearch_pipeline  # noqa: E402
print("opensearch_pipeline:", opensearch_pipeline.__file__)
from opensearch_pipeline.ops_monitor import main  # noqa: E402

# 阶段1（先上，只读、零写风险）：
sys.exit(main(["--only", "reconcile_ha3", "reconcile_oss"]))
# 阶段2（验稳后换成全量，含 qa_rollup 写 qa_daily_metrics）：
# sys.exit(main([]))
