# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 1: Raw → Canonical
上传到 DataWorks 调度节点中直接使用。

⚠️ 生产模式：将 SIMULATE = True 改为 False
"""
import os
import sys
import zipfile

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

if not SIMULATE:
    # 生产凭证：由 DataWorks 调度参数注入，或在 PyODPS 节点的「资源」配置中设置
    # 必须设置以下环境变量:
    #   DASHSCOPE_API_KEY, RAG_RDS_HOST, RAG_RDS_PORT, RAG_RDS_USER,
    #   RAG_RDS_PASSWORD, RAG_RDS_DATABASE, RAG_OSS_ENDPOINT,
    #   RAG_OSS_ACCESS_KEY_ID, RAG_OSS_ACCESS_KEY_SECRET, RAG_OSS_BUCKET_NAME
    required_vars = [
        "DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD",
        "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
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

# 安装 PDF 提取依赖（pypdf 在 Python 3.7 上无法提取中文 PDF）
import subprocess
subprocess.check_call([
    sys.executable, "-m", "pip", "install",
    "pdfplumber", "pypdf", "-q"
])

# ═══════════════════════════════════════════════════════════════
# 2. 解析调度参数
# ═══════════════════════════════════════════════════════════════
print("=== 3. 解析调度参数 ===")
# 兜底 bizdate = T-1（与 DataWorks ${bizdate} 语义一致）。原先硬编码 '20260521'：
# 节点漏配调度参数时会永远跑在那个过期日期上且毫无报错。
from datetime import datetime, timedelta
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
