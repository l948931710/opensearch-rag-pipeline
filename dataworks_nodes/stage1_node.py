# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 1: Raw → Canonical
上传到 DataWorks 调度节点中直接使用。

⚠️ 生产模式：将 SIMULATE = True 改为 False
"""
import os
import sys
import zipfile
from datetime import datetime, timedelta

# ═══════════════════════════════════════════════════════════════
# 🔧 模式开关（冒烟测试 True / 生产运行 False）
# ═══════════════════════════════════════════════════════════════
SIMULATE = True

# ═══════════════════════════════════════════════════════════════
# 🔐 环境变量配置（必须在 import pipeline 代码之前设置）
#    config.py 在首次 import 时会读取这些值
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = str(SIMULATE).lower()
os.environ["RAG_ENVIRONMENT"] = "production"

# ── Robustness features (validated GO 2026-06-23; default-OFF in code, enabled here for prod) ──
# Stage-1 (node_build_canonical). setdefault → still overridable (set the env var to 'false' to disable).
# Cross-doc dedup: idx_canonical_sha256 applied (schema/005); cross-scope default = WARN-and-process
# (skips only behind a fully-covering incumbent). Skip-gate: fail-safe idempotent re-ingest.
os.environ.setdefault("RAG_DEDUP_CROSS_DOC", "true")
os.environ.setdefault("RAG_SKIP_UNCHANGED_REINGEST", "true")

if not SIMULATE:
    # 生产凭证：由 DataWorks 调度参数注入，或在 PyODPS 节点的「资源」配置中设置
    # 必须设置以下环境变量:
    #   DASHSCOPE_API_KEY, RAG_RDS_HOST, RAG_RDS_PORT, RAG_RDS_USER,
    #   RAG_RDS_PASSWORD, RAG_RDS_DATABASE, RAG_OSS_ENDPOINT,
    #   RAG_OSS_ACCESS_KEY_ID, RAG_OSS_ACCESS_KEY_SECRET, RAG_OSS_BUCKET_NAME
    required_vars = [
        "DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD",
        "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
        # ↓ stage1 本身不碰索引，但 config 守卫 R5 要求 production 必须配置检索后端、
        #   D7 要求配了 HA3 endpoint 就必须显式表名（2026-06-11 stage1 实跑被 R5 拦截）。
        #   节点内联粘贴时从 清理stage3 顶部复制 RAG_HA3_* 同名赋值即可。
        "RAG_HA3_ENDPOINT", "RAG_HA3_TABLE_NAME",
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

# 安装抽取依赖（强化版：force-reinstall + no-cache + import 校验）
# 历史 bug: 旧版 stage1_node 漏装依赖, 静默吞 ImportError →
#   - 没 pypdf → RD 61D861 等扫描 PDF: 'pypdf not installed' + page_count=0 + 0 chunks
#   - 没 python-pptx → 成本核算/甘蔗渣培训.pptx: 抽取全空 → SKIPPED_EMPTY 0 chunks (2026-06-15)
# DataWorks runtime = Python 3.11 (2026-06-15 实测), site-packages 在 sys.path 内
print("=== 1.5 安装抽取依赖（pdfplumber + pypdf + python-pptx）===")
import subprocess
subprocess.check_call([
    sys.executable, "-m", "pip", "install",
    "--force-reinstall", "--no-cache-dir", "-q",
    "pdfplumber", "pypdf>=4.0", "python-pptx>=0.6",
])

# 强校验：装完必须能 import, 否则 fail-fast (避免再次重蹈 RD 61D861 / pptx 静默坑)
try:
    import pypdf
    import pdfplumber
    import pptx as _pptx_check  # noqa: F401  (python-pptx)
    print(f"✅ pypdf={pypdf.__version__}  pdfplumber={pdfplumber.__version__}  python-pptx OK")
except ImportError as e:
    raise RuntimeError(
        f"❌ 抽取依赖安装后仍无法 import: {e}. "
        f"sys.path={sys.path}. 检查 DataWorks 资源组 Python env."
    ) from e

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
# 3. 执行 Stage 1
# ═══════════════════════════════════════════════════════════════
print(f"=== 4. 启动 Stage 1 ({'模拟' if SIMULATE else '生产'}) ===")
from opensearch_pipeline.dataworks_orchestrator import run_stage
run_stage(stage=1, bizdate=bizdate, simulate=SIMULATE)
print("✅ Stage 1 完成！")
