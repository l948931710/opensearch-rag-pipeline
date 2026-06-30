# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 3: Chunks → OpenSearch Index
上传到 DataWorks 调度节点中直接使用。

⚠️ 默认生产模式（SIMULATE=False）。冒烟测试：在节点环境变量设 RAG_NODE_SIMULATE=true。
⚠️ Stage 3 额外需要 HA3 / OpenSearch 凭证
"""
import os
import sys
import subprocess
import zipfile
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════════
# 📦 安装 Stage 3 依赖
# ═══════════════════════════════════════════════════════════════
DEPS = [
    "PyMySQL", "DBUtils", "oss2", "requests",
    "alibabacloud_ha3engine_vector",
    "pdfplumber", "pypdf",
]
subprocess.check_call([
    sys.executable, "-m", "pip", "install",
    *DEPS, "-t", "/tmp/pydeps", "-q"
])
if "/tmp/pydeps" not in sys.path:
    sys.path.insert(0, "/tmp/pydeps")

# ═══════════════════════════════════════════════════════════════
# 🔧 模式开关：默认生产（False）。冒烟测试设环境变量 RAG_NODE_SIMULATE=true。
#    旧写法硬编码 True，部署忘改 → 整阶段跑 mock 却 exit 0（DataWorks 绿），语料静默停更。
# ═══════════════════════════════════════════════════════════════
SIMULATE = os.environ.get("RAG_NODE_SIMULATE", "false").strip().lower() in ("true", "1", "yes")

# ═══════════════════════════════════════════════════════════════
# 🔐 环境变量配置（必须在 import pipeline 代码之前设置）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = str(SIMULATE).lower()
os.environ["RAG_ENVIRONMENT"] = "production"

# ── Robustness features (validated GO 2026-06-23; default-OFF in code, enabled here for prod) ──
# Stage-3 node 04b. setdefault → overridable (set the env var to 'false' to disable).
# Parity-verify: re-read HA3 after push + bounded re-push of silent drops (first prod runs healed
# 96+1 cleanly). Drift: PK-present-but-stale-content sub-check (requires parity-verify on; verbatim
# hash = false-positive-proof). SETTLE_SEC stays default 30s.
os.environ.setdefault("RAG_STAGE3_PARITY_VERIFY", "true")
os.environ.setdefault("RAG_STAGE3_PARITY_DRIFT", "true")

if not SIMULATE:
    # 生产凭证：由 DataWorks 调度参数注入
    required_vars = [
        "DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD",
        "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
        "RAG_HA3_ENDPOINT", "RAG_HA3_USER", "RAG_HA3_PASSWORD",
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing required env vars for production: {missing}")

# ═══════════════════════════════════════════════════════════════
# 1. 下载并解压代码包
# ═══════════════════════════════════════════════════════════════
print("=== 1. 下载 Archive 资源 ===")
resource = odps.get_resource('opensearch_pipeline_production.zip')
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline_production.zip', 'wb') as writer:
        writer.write(reader.read())

print("=== 2. 解压代码包 ===")
if not os.path.exists('opensearch_pipeline_production.zip'):
    raise RuntimeError("❌ 未能下载 opensearch_pipeline_production.zip")

with zipfile.ZipFile('opensearch_pipeline_production.zip', 'r') as zip_ref:
    zip_ref.extractall('.')
print("✅ 解压成功")

current_dir = os.path.abspath(".")
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# ═══════════════════════════════════════════════════════════════
# 2. 解析调度参数
# ═══════════════════════════════════════════════════════════════
print("=== 3. 解析调度参数 ===")
# 兜底 bizdate = T-1（与 DataWorks ${bizdate} 语义一致）。原先硬编码 '20260521'：
# 节点漏配调度参数时会永远跑在那个过期日期上且毫无报错。
bizdate = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
if len(sys.argv) > 1:
    arg_val = sys.argv[1]
    bizdate = arg_val.split("=")[-1].strip() if "=" in arg_val else arg_val.strip()
    print(f"💡 bizdate: {bizdate}")
else:
    print(f"⚠️ 未获取到调度参数，按 T-1 兜底: {bizdate}")

# ═══════════════════════════════════════════════════════════════
# 3. 执行 Stage 3（排空式：drain 整批 + pre-drain 三对账）
# ═══════════════════════════════════════════════════════════════
# ⚠️ 用 run_stage_drained（非单批 run_stage）：生产排空整个 stage-3 待处理集，并在 drain【之前】跑
#    三个对账——reconcile_stranded_versions / reconcile_pending_deletes / reconcile_allowed_depts
#    （Phase D 撤销·授权投影自愈，RAG_ALLOWED_DEPTS_ACL 关时 skipped no-op）。这些对账只在
#    run_stage_drained 里；旧的单批 run_stage 既不排空也不跑对账。SIMULATE=True 时 drained 内部
#    退化为单次 run_stage（行为不变）。
# ⚠️ RAG_ALLOWED_DEPTS_ACL 不在此设默认——保持由 DataWorks 环境注入（双端 flip 前为 OFF），
#    避免在 serving 侧之前单边激活 Phase D。
print(f"=== 4. 启动 Stage 3 排空（{'模拟' if SIMULATE else '生产'}）===")
from opensearch_pipeline.dataworks_orchestrator import run_stage_drained
run_stage_drained(stage=3, bizdate=bizdate, simulate=SIMULATE)
print("✅ Stage 3 完成！")
