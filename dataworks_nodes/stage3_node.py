# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 3: Chunks → OpenSearch Index
上传到 DataWorks 调度节点中直接使用。

⚠️ 生产模式：将 SIMULATE = True 改为 False
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
# 🔧 模式开关（冒烟测试 True / 生产运行 False）
# ═══════════════════════════════════════════════════════════════
SIMULATE = True

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
resource = odps.get_resource('opensearch_pipeline.zip')
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline.zip', 'wb') as writer:
        writer.write(reader.read())

print("=== 2. 解压代码包 ===")
if not os.path.exists('opensearch_pipeline.zip'):
    raise RuntimeError("❌ 未能下载 opensearch_pipeline.zip")

with zipfile.ZipFile('opensearch_pipeline.zip', 'r') as zip_ref:
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
# 3. 执行 Stage 3
# ═══════════════════════════════════════════════════════════════
print(f"=== 4. 启动 Stage 3 ({'模拟' if SIMULATE else '生产'}) ===")
from opensearch_pipeline.dataworks_orchestrator import run_stage
run_stage(stage=3, bizdate=bizdate, simulate=SIMULATE)
print("✅ Stage 3 完成！")
