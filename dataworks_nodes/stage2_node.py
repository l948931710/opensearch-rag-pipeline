# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 2: Canonical → Safe Chunks
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
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = str(SIMULATE).lower()
os.environ["RAG_ENVIRONMENT"] = "production"

# ── Robustness features (validated GO 2026-06-23; default-OFF in code, enabled here for prod) ──
# Stage-2. setdefault → overridable (set the env var to 'false' to disable).
# Chunk-explosion: gate on; MODE stays 'warn' (default) — telemetry only, does NOT quarantine
# (set RAG_CHUNK_EXPLOSION_MODE=quarantine to escalate). Image-OCR PII: scans asset['ocr_text'],
# medium→redact, high→whole-doc quarantine (shadow showed 0 high-sev currently).
os.environ.setdefault("RAG_CHUNK_EXPLOSION_GATE", "true")
os.environ.setdefault("RAG_IMAGE_OCR_PII", "true")

if not SIMULATE:
    # 生产凭证：由 DataWorks 调度参数注入
    required_vars = [
        "DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD",
        "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
        # ↓ stage2 本身不碰索引，但 config 守卫 R5/D7 要求 production 配置检索后端+显式表名
        #   （同 stage1，2026-06-11）。节点内联粘贴时从 清理stage3 顶部复制 RAG_HA3_* 赋值。
        "RAG_HA3_ENDPOINT", "RAG_HA3_TABLE_NAME",
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

# 安装 PDF 提取依赖
# stage 2 也需要：unified_extractor._pages_needing_ocr 在 page_count<=0 时
# 走 pypdf→PyPDF2→pdfplumber 三阶 recovery (2026-06-15 fix for RD 61D861)。
# 如果 stage 1 已生成 page_count=0 的 canonical 残次品, stage 2 重读时仍会进 OCR fallback。
print("=== 2.5 安装 PDF 提取依赖（pdfplumber + pypdf）===")
import subprocess
subprocess.check_call([
    sys.executable, "-m", "pip", "install",
    "--force-reinstall", "--no-cache-dir", "-q",
    "pdfplumber", "pypdf>=4.0",
])

try:
    import pypdf
    import pdfplumber
    print(f"✅ pypdf={pypdf.__version__}  pdfplumber={pdfplumber.__version__}")
except ImportError as e:
    raise RuntimeError(
        f"❌ PDF 依赖安装后仍无法 import: {e}. sys.path={sys.path}."
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
# 3. 执行 Stage 2（排空式：drain 整批）
# ═══════════════════════════════════════════════════════════════
# 用 run_stage_drained（与 stage-1/3 一致、与已部署节点对齐）：生产排空整个 stage-2 待处理集。
# pre-drain 对账只在 stage-3（stage-1/2 无）。SIMULATE=True 时 drained 退化为单次 run_stage（不变）。
print(f"=== 4. 启动 Stage 2 排空（{'模拟' if SIMULATE else '生产'}）===")
from opensearch_pipeline.dataworks_orchestrator import run_stage_drained
run_stage_drained(stage=2, bizdate=bizdate, simulate=SIMULATE)
print("✅ Stage 2 完成！")
