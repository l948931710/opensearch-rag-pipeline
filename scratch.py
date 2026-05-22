import os
import pymysql
from dotenv import load_dotenv
from opensearchpy import OpenSearch

load_dotenv(".env")

# 1. 验证 MySQL 数据
print("=== MySQL chunk_meta counts ===")
conn = pymysql.connect(
    host=os.environ.get("RAG_RDS_HOST", "localhost"),
    port=int(os.environ.get("RAG_RDS_PORT", 3306)),
    user=os.environ.get("RAG_RDS_USER", "root"),
    password=os.environ.get("RAG_RDS_PASSWORD", "your_password"),
    database=os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor
)

try:
    with conn.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) as cnt, is_active, version_no, doc_id FROM chunk_meta GROUP BY doc_id, version_no, is_active")
        rows = cursor.fetchall()
        for r in rows:
            print(f"Doc ID: {r['doc_id']}, Version: {r['version_no']}, is_active: {r['is_active']}, Chunks: {r['cnt']}")
            
        print("\n=== MySQL opensearch_bulk_job records ===")
        cursor.execute("SELECT * FROM opensearch_bulk_job")
        jobs = cursor.fetchall()
        for j in jobs:
            print(f"Job ID: {j['job_id']}, Index: {j['index_name']}, Total Chunks: {j['total_chunks']}, Status: {j['status']}, Size: {j['payload_size_bytes']} bytes")
            
        print("\n=== MySQL document_sensitive_finding records ===")
        cursor.execute("SELECT * FROM document_sensitive_finding")
        findings = cursor.fetchall()
        for f in findings:
            print(f"Doc ID: {f['doc_id']}, Version: {f['version_no']}, Type: {f['finding_type']}, Severity: {f['severity']}, Action: {f['action']}, Preview: {f['matched_text_preview']}")
finally:
    conn.close()

# 2. 验证 OpenSearch 索引及向量内容
print("\n=== OpenSearch stats & sample ===")
client = OpenSearch(
    hosts=[{"host": "localhost", "port": 9200}],
    use_ssl=False,
    verify_certs=False,
    ssl_assert_hostname=False,
    ssl_show_warn=False
)

try:
    idx_name = "fuling_knowledge_v1"
    cnt_resp = client.count(index=idx_name)
    print(f"Index '{idx_name}' document count: {cnt_resp.get('count')}")
    
    # 打印一些文档的 ID 和 is_active 状态以确保一致
    search_resp = client.search(
        index=idx_name,
        body={
            "query": {"match_all": {}},
            "size": 20,
            "_source": ["doc_id", "chunk_id", "version", "is_active", "chunk_type"]
        }
    )
    hits = search_resp.get("hits", {}).get("hits", [])
    print(f"Sample documents in OpenSearch (Total retrieved: {len(hits)}):")
    for hit in hits:
        source = hit["_source"]
        print(f"  - OS Doc ID: {hit['_id']}, File Doc ID: {source.get('doc_id')}, Ver: {source.get('version')}, Active: {source.get('is_active')}, Type: {source.get('chunk_type')}")

except Exception as e:
    print(f"Error querying OpenSearch: {e}")
