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
DEPS = [
    "PyMySQL", "DBUtils", "oss2", "requests",
    "alibabacloud_ha3engine_vector",
    "pdfplumber", "pypdf",
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
# ── 告警 webhook（不配则 OBS-4 告警是【静默 no-op】——2026-07-15 实锤:reconcile_ha3 的
#    CRITICAL 每天被 SUPPRESSED,节点日日 Failure 无人收。⚠️ 必填。铸造步骤:
#    钉钉运维群 → 群设置 → 智能群助手 → 添加机器人 → 自定义(Webhook) → 安全设置选「加签」→
#    把 Webhook URL 填 RAG_OPS_ALERT_WEBHOOK、加签密钥(SEC 开头)填 RAG_OPS_ALERT_SECRET ──────
# os.environ["RAG_OPS_ALERT_WEBHOOK"]     = "https://oapi.dingtalk.com/robot/send?access_token=<TODO>"
# os.environ["RAG_OPS_ALERT_SECRET"]      = "SEC<TODO>"
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
with zipfile.ZipFile('opensearch_pipeline_production.zip', 'r') as zf:
    zf.extractall('.')
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

# 阶段1.5（2026-07-16 扫描停摆调查后口径）：三个只读对账 + 队列老化 + 摄取漏斗。
# unregistered_raw = raw/ 新文件未注册检测（摄取无周期调度、全靠手工批——这是"替人看一眼"的
# 那只眼睛;可摄取+超24h 未注册才告警,旧格式/隔离区/自助上传形状不驱动红）。
# ⚠️ unregistered_raw 需要 opensearch_pipeline_production.zip ≥ 2026-07-16(含 CS4c)——
#    旧 zip 下该作业名会被静默忽略(不报错),重打包后自动生效。
sys.exit(main(["--only", "reconcile_ha3", "reconcile_oss", "unregistered_raw",
               "queue_aging", "ingest_funnel"]))
# 阶段2（验稳后换成全量，含 qa_rollup 写 qa_daily_metrics + reconcile_raw）：
# sys.exit(main([]))
