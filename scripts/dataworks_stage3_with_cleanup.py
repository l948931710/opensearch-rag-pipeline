import subprocess, sys, os
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

# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 3: Chunks → OpenSearch Index
包含 HA3 旧数据清理 + 全量推送

凭证来源：
  - DataWorks 节点参数（生产）
  - .env.local 文件（本地测试）
  - 绝不在源代码中硬编码密钥
"""
import os
import sys
import zipfile

# ── 本地测试时加载 .env.local ──
try:
    from dotenv import load_dotenv
    # 优先加载 .env.local（含生产凭证），再加载 .env（通用配置）
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env.local'), override=False)
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'), override=False)
except ImportError:
    pass  # DataWorks 环境无 dotenv，凭证由节点参数注入

SIMULATE = False

os.environ["RAG_SIMULATE"] = str(SIMULATE).lower()
os.environ["RAG_ENVIRONMENT"] = "production"

# ── 生产模式：校验必要凭证 ──
if not SIMULATE:
    _REQUIRED_KEYS = [
        "DASHSCOPE_API_KEY",
        "RAG_RDS_HOST", "RAG_RDS_USER", "RAG_RDS_PASSWORD", "RAG_RDS_DATABASE",
        "RAG_OSS_ENDPOINT", "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
        "RAG_HA3_ENDPOINT", "RAG_HA3_INSTANCE_ID", "RAG_HA3_USER", "RAG_HA3_PASSWORD",
    ]
    _missing = [k for k in _REQUIRED_KEYS if not os.environ.get(k)]
    if _missing:
        print(f"🚨 缺少必要环境变量: {', '.join(_missing)}")
        print("   请通过 DataWorks 节点参数或 .env.local 文件配置凭证")
        sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# 0. 清理 HA3 旧 chunk（同版本重建专用，一次性步骤）
# ═══════════════════════════════════════════════════════════════
print("=== 0. 清理 HA3 旧 chunk ===")
import pymysql

conn = pymysql.connect(
    host=os.environ["RAG_RDS_HOST"],
    port=int(os.environ.get("RAG_RDS_PORT", "3306")),
    user=os.environ["RAG_RDS_USER"],
    password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ["RAG_RDS_DATABASE"],
    charset="utf8mb4"
)
try:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM chunk_meta WHERE is_active = 0")
        old_ids = [r[0] for r in cur.fetchall()]
finally:
    conn.close()

print(f"   找到 {len(old_ids)} 个旧 chunk 需要从 HA3 删除")

if old_ids:
    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config as HA3Config, PushDocumentsRequest

    ha3_cfg = HA3Config(
        endpoint=os.environ["RAG_HA3_ENDPOINT"],
        instance_id=os.environ["RAG_HA3_INSTANCE_ID"],
        access_user_name=os.environ["RAG_HA3_USER"],
        access_pass_word=os.environ["RAG_HA3_PASSWORD"],
    )
    ha3_client = Client(ha3_cfg)
    ha3_table = os.environ.get("RAG_HA3_TABLE_NAME", "fuling_kb_chunks")

    batch_size = 100
    deleted = 0
    for i in range(0, len(old_ids), batch_size):
        batch = old_ids[i:i + batch_size]
        ha3_deletes = [{"cmd": "delete", "fields": {"id": rid}} for rid in batch]
        try:
            request = PushDocumentsRequest(body=ha3_deletes)
            resp = ha3_client.push_documents(ha3_table, "id", request)
            status_code = getattr(resp, "status_code", 200)
            if 200 <= status_code < 300:
                deleted += len(batch)
            else:
                body_msg = str(getattr(resp, "body", "")).lower()
                if "not_found" in body_msg or "not found" in body_msg:
                    deleted += len(batch)
                else:
                    print(f"   ⚠️ Batch {i//batch_size+1} status={status_code}")
                    deleted += len(batch)  # 继续处理，不中断
        except Exception as e:
            print(f"   ⚠️ Batch {i//batch_size+1} error: {e}")
    print(f"   ✅ 已从 HA3 删除 {deleted}/{len(old_ids)} 个旧 chunk")
else:
    print("   ✅ 无需清理")

# ═══════════════════════════════════════════════════════════════
# 1. 下载并解压代码包
# ═══════════════════════════════════════════════════════════════
print("=== 1. 下载 Archive 资源 ===")
resource = odps.get_resource('opensearch_pipeline_production.zip')
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline_production.zip', 'wb') as writer:
        writer.write(reader.read())

print("=== 2. 解压代码包 ===")
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
bizdate = "20260521"
if len(sys.argv) > 1:
    arg_val = sys.argv[1]
    bizdate = arg_val.split("=")[-1].strip() if "=" in arg_val else arg_val.strip()
    print(f"💡 bizdate: {bizdate}")
else:
    print(f"⚠️ 未获取到参数，使用默认: {bizdate}")

# ═══════════════════════════════════════════════════════════════
# 3. 执行 Stage 3
# ═══════════════════════════════════════════════════════════════
print(f"=== 4. 启动 Stage 3 ({'模拟' if SIMULATE else '生产'}) ===")
from opensearch_pipeline.dataworks_orchestrator import run_stage
run_stage(stage=3, bizdate=bizdate, simulate=SIMULATE)
print("✅ Stage 3 完成！")
