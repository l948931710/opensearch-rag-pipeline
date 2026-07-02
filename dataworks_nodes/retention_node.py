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
RAG_* 赋值贴到「凭据」标记处（仅需 RDS 三件套 + DASHSCOPE key 过 config 守卫；
不碰 OSS/HA3）。
"""
import os
import sys
import subprocess
import zipfile

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖（PyODPS 3.7 pod 无 pymysql/dbutils；纯 RDS 作业，不装 oss2/ha3）
# ═══════════════════════════════════════════════════════════════
DEPS = ["PyMySQL", "DBUtils", "requests"]
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
# retention 是纯 RDS 作业，不碰检索后端/OSS。显式声明这两路走 mock：
#   ① 短路 config 的 production 完整性守卫 R5（config.py:501「production 必须有检索后端，
#      否则 EnvironmentMismatchError」）——2026-07-02 首跑即撞它；
#   ② 免配 HA3/OSS 凭据（本节点不需要）。
# RDS 仍真实：simulate_db 不设 → 继承 RAG_SIMULATE=false → 真连生产 RDS；retention.py 的
# `if cfg.simulate or cfg.simulate_db: skip` 也不会误跳（两者均 false）。
os.environ["RAG_SIMULATE_OPENSEARCH"] = "true"
os.environ["RAG_SIMULATE_OSS"] = "true"

# 阶段开关：阶段1 = True（dry-run 只报数）；阶段2 = False + 打开 RAG_RETENTION_ENABLE
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
# os.environ["DASHSCOPE_API_KEY"] = "..."   # 本节点不调 LLM，但 production 安全守卫要求配 DashScope key（防 Gemini 误用）
# os.environ["RAG_RDS_HOST"]      = "..."
# os.environ["RAG_RDS_PORT"]      = "..."
# os.environ["RAG_RDS_USER"]      = "..."
# os.environ["RAG_RDS_PASSWORD"]  = "..."
# os.environ["RAG_RDS_DATABASE"]  = "..."
# ─────────────────────────────────────────────────────────────────────────────

_required = ["DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD"]
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

# ═══════════════════════════════════════════════════════════════
# 3. 运行留存作业（DataWorks 以退出码判成败；2/3 都会标失败引人来看）
# ═══════════════════════════════════════════════════════════════
import opensearch_pipeline  # noqa: E402
print("opensearch_pipeline:", opensearch_pipeline.__file__)
from opensearch_pipeline.retention import main  # noqa: E402

sys.exit(main([] if DRY_RUN else ["--commit"]))
