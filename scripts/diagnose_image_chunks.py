# -*- coding: utf-8 -*-
"""
diagnose_image_chunks.py — 诊断图片 chunk 缺失原因

检查三个层面:
  1. RDS chunk_meta: 有没有 chunk_type='image' 的记录?
  2. RDS document_version + OSS canonical: canonical JSON 里有没有 assets?
  3. OSS processing/assets/: 有没有上传过图片文件?
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENVIRONMENT", "production")

from opensearch_pipeline.config import get_config, load_config
import opensearch_pipeline.config as _cfg
_cfg._config = load_config()

from opensearch_pipeline.pipeline_nodes import _get_db_conn, _get_oss_bucket


def main():
    print("=" * 60)
    print("🔍 图片 chunk 诊断工具")
    print("=" * 60)

    # ── 1. 检查 RDS chunk_meta 中的 image chunks ──
    print("\n── 1. chunk_meta 中 chunk_type='image' 的记录 ──")
    conn = _get_db_conn(select_db=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT COUNT(*) FROM chunk_meta WHERE chunk_type = 'image'
            """)
            count = cursor.fetchone()[0]
            print(f"   image chunk 总数: {count}")

            if count > 0:
                cursor.execute("""
                    SELECT chunk_id, doc_id, version_no, LEFT(chunk_text, 80), index_status, is_active
                    FROM chunk_meta WHERE chunk_type = 'image'
                    ORDER BY created_at DESC LIMIT 10
                """)
                for row in cursor.fetchall():
                    print(f"   {row[0]} | doc={row[1]} v{row[2]} | {row[4]} | active={row[5]}")
                    print(f"     text: {row[3]}...")
            else:
                print("   ⚠️ 没有任何 image chunk！继续排查...")

            # ── 2. 检查 canonical JSON 中的 assets ──
            print("\n── 2. 检查 canonical JSON 中的 assets 字段 ──")
            cursor.execute("""
                SELECT dv.doc_id, dv.version_no, dv.canonical_json_key, dm.title
                FROM document_version dv
                LEFT JOIN document_meta dm ON dv.doc_id = dm.doc_id
                WHERE dv.status = 'active' AND dv.canonical_json_key IS NOT NULL
                ORDER BY dv.created_at DESC
                LIMIT 20
            """)
            rows = cursor.fetchall()
            print(f"   找到 {len(rows)} 个有 canonical_json_key 的文档版本")

            bucket, is_sim = _get_oss_bucket({})
            if is_sim or not bucket:
                print("   ⚠️ OSS 连接为模拟模式或不可用，跳过 canonical 检查")
            else:
                docs_with_assets = 0
                total_route_to_vector = 0
                for row in rows:
                    doc_id, ver, key, title = row
                    try:
                        data = bucket.get_object(key).read()
                        cj = json.loads(data.decode("utf-8"))
                        assets = cj.get("assets", [])
                        rtv = [a for a in assets if a.get("status") == "ROUTE_TO_VECTOR"]
                        if assets:
                            docs_with_assets += 1
                            total_route_to_vector += len(rtv)
                            statuses = {}
                            for a in assets:
                                s = a.get("status", "UNKNOWN")
                                statuses[s] = statuses.get(s, 0) + 1
                            summary = ", ".join(f"{k}={v}" for k, v in statuses.items())
                            print(f"   📄 {doc_id} v{ver} ({title})")
                            print(f"      assets: {len(assets)} 个 ({summary})")
                        else:
                            pass  # 无 assets 的文档不输出，太多
                    except Exception as e:
                        print(f"   ⚠️ 读取 {key} 失败: {e}")

                print(f"\n   汇总: {docs_with_assets}/{len(rows)} 个文档有 assets, "
                      f"其中 ROUTE_TO_VECTOR={total_route_to_vector}")

                if total_route_to_vector == 0:
                    print("\n   ⚠️ 没有任何 ROUTE_TO_VECTOR 的图片 asset！")
                    print("   可能原因:")
                    print("     1. Funnel 1 将所有图片判定为装饰图（尺寸太小/比例太极端）")
                    print("     2. Funnel 2 将所有图片判定为高文本密度（OCR 文字>120字符）")
                    print("     3. Funnel 3 VLM 将所有图片判定为 LOW_RELEVANCE")
                    print("     4. Stage 1 提取时 _tmp_dir 缺失/PIL 未安装，图片提取跳过")

            # ── 3. 检查 OSS processing/assets/ 目录 ──
            print("\n── 3. 检查 OSS processing/assets/ 目录 ──")
            if not is_sim and bucket:
                try:
                    import oss2
                    count = 0
                    for obj in oss2.ObjectIterator(bucket, prefix="processing/assets/", max_keys=20):
                        print(f"   📦 {obj.key} ({obj.size} bytes)")
                        count += 1
                    if count == 0:
                        print("   ⚠️ processing/assets/ 下没有任何文件")
                    else:
                        print(f"   共找到 {count} 个文件（仅显示前 20 个）")
                except Exception as e:
                    print(f"   ⚠️ 列举 OSS 对象失败: {e}")

    finally:
        conn.close()

    print("\n" + "=" * 60)
    print("诊断完成")


if __name__ == "__main__":
    main()
