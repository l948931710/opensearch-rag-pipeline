# -*- coding: utf-8 -*-
"""
HA3 旧 chunk 清理脚本
在 Stage 3 之前运行，删除 HA3 中所有旧的（is_active=0）chunk。

凭证来源：
  - 环境变量（DataWorks 节点参数）
  - .env.local 文件（本地测试）
"""
import os
import sys

# ── 本地测试时加载 .env.local ──
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env.local'), override=False)
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'), override=False)
except ImportError:
    pass

import pymysql

# ── HA3 配置 ──
HA3_TABLE_NAME = os.environ.get("RAG_HA3_TABLE_NAME", "fuling_kb_chunks")
HA3_PK_FIELD = "id"

# ── RDS 配置（不再包含硬编码凭证，全部从环境变量读取）──
RDS_HOST = os.environ.get("RAG_RDS_HOST", "")
RDS_PORT = int(os.environ.get("RAG_RDS_PORT", "3306"))
RDS_USER = os.environ.get("RAG_RDS_USER", "")
RDS_PASS = os.environ.get("RAG_RDS_PASSWORD", "")
RDS_DB   = os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge")

if not RDS_HOST or not RDS_USER or not RDS_PASS:
    print("🚨 缺少 RDS 凭证环境变量 (RAG_RDS_HOST / RAG_RDS_USER / RAG_RDS_PASSWORD)")
    print("   请通过 .env.local 或环境变量配置")
    sys.exit(1)

# ── HA3 连接配置 ──
HA3_ENDPOINT    = os.environ.get("RAG_HA3_ENDPOINT", "")
HA3_INSTANCE_ID = os.environ.get("RAG_HA3_INSTANCE_ID", "")
HA3_USERNAME    = os.environ.get("RAG_HA3_ACCESS_USER_NAME", os.environ.get("RAG_HA3_USER", ""))
HA3_PASSWORD    = os.environ.get("RAG_HA3_ACCESS_PASS_WORD", os.environ.get("RAG_HA3_PASSWORD", ""))


def get_old_rds_ids():
    """从 RDS 获取所有旧 chunk 的 rds_id（即 HA3 主键）"""
    conn = pymysql.connect(
        host=RDS_HOST, port=RDS_PORT, user=RDS_USER,
        password=RDS_PASS, database=RDS_DB,
        charset="utf8mb4"
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM chunk_meta WHERE is_active = 0")
            rows = cur.fetchall()
            return [r[0] for r in rows]
    finally:
        conn.close()


def delete_from_ha3(rds_ids):
    """批量从 HA3 删除旧 chunk"""
    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config as HA3Config, PushDocumentsRequest

    config = HA3Config(
        endpoint=HA3_ENDPOINT,
        instance_id=HA3_INSTANCE_ID,
        access_user_name=HA3_USERNAME,
        access_pass_word=HA3_PASSWORD,
    )
    client = Client(config)

    batch_size = 100
    total = len(rds_ids)
    deleted = 0
    errors = 0

    for i in range(0, total, batch_size):
        batch = rds_ids[i:i + batch_size]
        ha3_deletes = [{"cmd": "delete", "fields": {HA3_PK_FIELD: rid}} for rid in batch]

        try:
            request = PushDocumentsRequest(body=ha3_deletes)
            resp = client.push_documents(HA3_TABLE_NAME, HA3_PK_FIELD, request)
            status_code = getattr(resp, "status_code", 200)

            if 200 <= status_code < 300:
                deleted += len(batch)
                print(f"  ✅ Deleted batch {i//batch_size + 1}: {len(batch)} chunks (total: {deleted}/{total})")
            else:
                body_msg = str(getattr(resp, "body", ""))
                # 容忍 not_found 错误（已删除的 chunk）
                if "not_found" in body_msg.lower() or "not found" in body_msg.lower():
                    deleted += len(batch)
                    print(f"  ⚠️ Batch {i//batch_size + 1}: some already deleted (idempotent), continuing...")
                else:
                    errors += len(batch)
                    print(f"  ❌ Batch {i//batch_size + 1} failed: status={status_code}, body={body_msg[:200]}")
        except Exception as e:
            errors += len(batch)
            print(f"  ❌ Batch {i//batch_size + 1} error: {e}")

    return deleted, errors


if __name__ == "__main__":
    print("=== HA3 旧 chunk 清理 ===")

    print("1. 查询旧 chunk rds_ids...")
    rds_ids = get_old_rds_ids()
    print(f"   找到 {len(rds_ids)} 个旧 chunk 需要从 HA3 删除")

    if not rds_ids:
        print("   ✅ 没有需要清理的旧 chunk")
        sys.exit(0)

    if not HA3_ENDPOINT:
        print("   ❌ 缺少 RAG_HA3_ENDPOINT 环境变量")
        sys.exit(1)

    print(f"2. 开始从 HA3 表 '{HA3_TABLE_NAME}' 批量删除...")
    deleted, errors = delete_from_ha3(rds_ids)

    print(f"\n=== 清理完成 ===")
    print(f"   删除成功: {deleted}")
    print(f"   删除失败: {errors}")

    if errors > 0:
        print("   ⚠️ 有失败的删除，建议检查后重试")
        sys.exit(1)
    else:
        print("   ✅ 全部清理完成，可以跑 Stage 3 了")
