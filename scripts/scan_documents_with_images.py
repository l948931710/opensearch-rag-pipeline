# -*- coding: utf-8 -*-
"""
scan_documents_with_images.py — 扫描知识库中包含嵌入图片的文档

两种模式:
  1. 本地扫描: 扫描本地目录下的文档文件
  2. 线上扫描: 连接 RDS + OSS，扫描已入库的文档

用法:
  # 本地目录扫描
  python scripts/scan_documents_with_images.py --local ./fuling_chunk_exp

  # 线上扫描 (连接 RDS 获取文档列表 + OSS 下载检测)
  python scripts/scan_documents_with_images.py --online

  # 仅查询 RDS 中哪些文档是 PDF/DOCX/XLSX 格式（不下载，不检测图片）
  python scripts/scan_documents_with_images.py --online --quick
"""

import os
import sys
import argparse
import zipfile
import tempfile
from pathlib import Path
from typing import List, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def count_images_in_docx(filepath: str) -> int:
    """检测 DOCX 中的嵌入图片数量（仅解析 zip 结构，不需要额外库）"""
    try:
        with zipfile.ZipFile(filepath) as z:
            media_files = [n for n in z.namelist() if n.startswith("word/media/")]
            return len(media_files)
    except Exception:
        return 0


def count_images_in_pdf(filepath: str) -> int:
    """检测 PDF 中的嵌入图片数量"""
    try:
        import fitz
        doc = fitz.open(filepath)
        total = 0
        for page in doc:
            total += len(page.get_images(full=True))
        doc.close()
        return total
    except ImportError:
        print("  ⚠️ PyMuPDF (fitz) 未安装，无法检测 PDF 图片")
        return -1
    except Exception:
        return 0


def count_images_in_xlsx(filepath: str) -> int:
    """检测 XLSX 中的嵌入图片数量"""
    try:
        with zipfile.ZipFile(filepath) as z:
            media_files = [n for n in z.namelist() if n.startswith("xl/media/")]
            return len(media_files)
    except Exception:
        return 0


def detect_images(filepath: str) -> int:
    """根据文件扩展名检测图片数量"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".docx":
        return count_images_in_docx(filepath)
    elif ext == ".pdf":
        return count_images_in_pdf(filepath)
    elif ext == ".xlsx":
        return count_images_in_xlsx(filepath)
    else:
        return 0  # txt, csv 等不含嵌入图片


def scan_local(directory: str) -> List[Dict]:
    """扫描本地目录下的所有文档"""
    results = []
    supported_exts = {".docx", ".pdf", ".xlsx", ".doc", ".xls", ".pptx"}

    for root, _, files in os.walk(directory):
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in supported_exts:
                continue
            filepath = os.path.join(root, fname)
            img_count = detect_images(filepath)
            size_kb = os.path.getsize(filepath) / 1024
            results.append({
                "filename": fname,
                "path": filepath,
                "ext": ext,
                "size_kb": size_kb,
                "image_count": img_count,
            })
    return results


def scan_online_quick():
    """仅查询 RDS，列出所有 PDF/DOCX/XLSX 格式的文档（不下载）"""
    import pymysql
    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    conn = _get_db_conn(select_db=True)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("""
                SELECT 
                    dv.doc_id,
                    dv.version_no,
                    dv.file_ext,
                    dv.raw_key,
                    dv.content_process_status,
                    dv.index_status,
                    dm.title,
                    dm.owner_dept
                FROM document_version dv
                LEFT JOIN document_meta dm ON dv.doc_id = dm.doc_id
                WHERE dv.file_ext IN ('pdf', 'docx', 'xlsx')
                  AND dv.status = 'active'
                ORDER BY dv.file_ext, dm.owner_dept, dm.title
            """)
            rows = cursor.fetchall()
    finally:
        conn.close()

    return rows


def scan_online_full():
    """连接 RDS + OSS，下载并检测每个 PDF/DOCX/XLSX 文档中的图片"""
    import oss2
    import pymysql
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    config = get_config()

    # 获取文档列表
    conn = _get_db_conn(select_db=True)
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("""
                SELECT 
                    dv.doc_id,
                    dv.version_no,
                    dv.file_ext,
                    dv.raw_key,
                    dv.bucket_name,
                    dm.title,
                    dm.owner_dept
                FROM document_version dv
                LEFT JOIN document_meta dm ON dv.doc_id = dm.doc_id
                WHERE dv.file_ext IN ('pdf', 'docx', 'xlsx')
                  AND dv.status = 'active'
                ORDER BY dm.owner_dept, dm.title
            """)
            rows = cursor.fetchall()
    finally:
        conn.close()

    if not rows:
        print("  ⚠️ RDS 中没有 PDF/DOCX/XLSX 格式的活跃文档")
        return []

    # 连接 OSS
    auth = oss2.Auth(config.oss.access_key_id, config.oss.access_key_secret)
    bucket_name = rows[0]["bucket_name"] if rows else config.oss.bucket_name
    bucket = oss2.Bucket(auth, config.oss.endpoint, bucket_name)

    results = []
    for row in rows:
        raw_key = row["raw_key"]
        doc_id = row["doc_id"]
        title = row.get("title", "")
        ext = row["file_ext"]

        # 下载到临时文件
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp_path = tmp.name
                bucket.get_object_to_file(raw_key, tmp_path)

            img_count = detect_images(tmp_path)
            size_kb = os.path.getsize(tmp_path) / 1024

            results.append({
                "doc_id": doc_id,
                "title": title,
                "dept": row.get("owner_dept", ""),
                "ext": ext,
                "raw_key": raw_key,
                "size_kb": size_kb,
                "image_count": img_count,
            })
        except Exception as e:
            print(f"  ⚠️ 下载失败: {raw_key} → {e}")
            results.append({
                "doc_id": doc_id,
                "title": title,
                "dept": row.get("owner_dept", ""),
                "ext": ext,
                "raw_key": raw_key,
                "size_kb": 0,
                "image_count": -1,
                "error": str(e),
            })
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    return results


def main():
    parser = argparse.ArgumentParser(description="扫描知识库中包含嵌入图片的文档")
    parser.add_argument("--local", type=str, help="扫描本地目录")
    parser.add_argument("--online", action="store_true", help="连接线上 RDS + OSS 扫描")
    parser.add_argument("--quick", action="store_true", help="仅查询 RDS 格式统计（不下载）")
    args = parser.parse_args()

    if args.local:
        print(f"\n{'='*70}")
        print(f"  本地文档图片扫描: {args.local}")
        print(f"{'='*70}\n")

        results = scan_local(args.local)

        with_images = [r for r in results if r["image_count"] > 0]
        without_images = [r for r in results if r["image_count"] == 0]

        if with_images:
            print(f"  📸 包含图片的文档 ({len(with_images)} 个):\n")
            for r in sorted(with_images, key=lambda x: -x["image_count"]):
                print(f"    🖼️  {r['filename']}")
                print(f"       {r['ext']} | {r['size_kb']:.0f}KB | {r['image_count']} 张图片")
                print()

        if without_images:
            print(f"  📝 纯文本文档 ({len(without_images)} 个):\n")
            for r in without_images:
                print(f"    📄 {r['filename']} ({r['ext']}, {r['size_kb']:.0f}KB)")

        print(f"\n{'='*70}")
        print(f"  总计: {len(results)} 文档, {len(with_images)} 含图片, {len(without_images)} 纯文本")
        total_images = sum(r["image_count"] for r in with_images)
        print(f"  图片总数: {total_images} 张")
        print(f"{'='*70}")

    elif args.online and args.quick:
        print(f"\n{'='*70}")
        print(f"  线上 RDS 文档格式统计 (快速模式)")
        print(f"{'='*70}\n")

        rows = scan_online_quick()
        if not rows:
            print("  ⚠️ 无结果")
            return

        by_ext = {}
        for r in rows:
            ext = r["file_ext"]
            by_ext.setdefault(ext, []).append(r)

        for ext, docs in sorted(by_ext.items()):
            print(f"  📁 .{ext} ({len(docs)} 个):")
            for d in docs:
                status = d.get("content_process_status", "?")
                idx_status = d.get("index_status", "?")
                dept = d.get("owner_dept", "?")
                title = d.get("title", d["raw_key"].split("/")[-1])
                print(f"    [{dept}] {title} (process={status}, index={idx_status})")
            print()

        print(f"  总计: {len(rows)} 个 PDF/DOCX/XLSX 文档")
        print(f"  ⚠️ 这些文档可能包含嵌入图片，需要用 --online (不带 --quick) 下载检测")

    elif args.online:
        print(f"\n{'='*70}")
        print(f"  线上 OSS 文档图片扫描 (完整模式)")
        print(f"{'='*70}\n")

        results = scan_online_full()
        with_images = [r for r in results if r["image_count"] > 0]
        without_images = [r for r in results if r["image_count"] == 0]
        errors = [r for r in results if r["image_count"] < 0]

        if with_images:
            print(f"  📸 包含图片的文档 ({len(with_images)} 个):\n")
            for r in sorted(with_images, key=lambda x: -x["image_count"]):
                print(f"    🖼️  [{r['dept']}] {r['title'] or r['doc_id']}")
                print(f"       .{r['ext']} | {r['size_kb']:.0f}KB | {r['image_count']} 张图片")
                print(f"       {r['raw_key']}")
                print()

        if without_images:
            print(f"  📝 纯文本文档 ({len(without_images)} 个):\n")
            for r in without_images:
                print(f"    📄 [{r['dept']}] {r['title'] or r['doc_id']} (.{r['ext']})")

        if errors:
            print(f"\n  ⚠️ 扫描失败 ({len(errors)} 个):")
            for r in errors:
                print(f"    ❌ {r['doc_id']}: {r.get('error', 'unknown')}")

        print(f"\n{'='*70}")
        print(f"  总计: {len(results)} 文档, {len(with_images)} 含图片, {len(without_images)} 纯文本")
        if with_images:
            total_images = sum(r["image_count"] for r in with_images)
            print(f"  图片总数: {total_images} 张 (需重跑 pipeline 处理)")
        print(f"{'='*70}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
