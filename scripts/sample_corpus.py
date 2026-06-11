#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sample_corpus.py — 从生产 OSS **只读**采样语料到本地（LOCAL-DEV 零 OSS 形态的进料口）

本地管线以 RAG_SIMULATE_OSS=true 运行（全程不触 OSS）；真实文档预先用本脚本采样到
scratch/sample_corpus/<raw_key>，stage-1 抽取会自动识别该路径（pipeline_nodes 的
sampled-corpus 回退）。生产访问统一走 prod_access（只读会话 + 只读 OSS 句柄）。

用法：
  python scripts/sample_corpus.py --list-only                  # 只看生产 raw/ 分布
  python scripts/sample_corpus.py --per-ext 3                  # 每个扩展名抽 N 份下载
  python scripts/sample_corpus.py --doc-ids DOC_A,DOC_B        # 指定 doc_id 下载
  python scripts/sample_corpus.py --batch scratch/local_e2e_batch.json   # 按批次清单
  加 --register 同时把 document_meta/version 注册进本地 MySQL（须 RAG_ENV=local）

注册行为与 scratch/local_e2e_ingest.py 同模式：doc_id 加 LOCALSMP_ 前缀、version=1、
content_process_status='NOT_STARTED'，幂等跳过已存在行。
"""

import argparse
import collections
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scratch"))

DEST = os.path.join(REPO, "scratch", "sample_corpus")
PREFIX = "LOCALSMP_"


def _prod_rows(limit=5000):
    """生产 active 文档清单（doc_id, raw_key, title, ext）——只读。"""
    from opensearch_pipeline.prod_access import get_prod_readonly_conn
    conn = get_prod_readonly_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT dm.doc_id, dv.raw_key, dv.file_ext,
                       COALESCE(dm.title, dm.original_filename, '') AS title
                FROM document_meta dm
                JOIN document_version dv
                  ON dv.doc_id = dm.doc_id AND dv.version_no = dm.current_version_no
                WHERE dm.status='active' AND dv.status='active'
                  AND LOCATE('/_quarantine/', dv.raw_key) = 0
                LIMIT %s""", (limit,))
            return cur.fetchall()
    finally:
        conn.close()


def _pick(rows, per_ext=None, doc_ids=None):
    if doc_ids:
        want = set(doc_ids)
        return [r for r in rows if r["doc_id"] in want]
    by_ext = collections.defaultdict(list)
    for r in rows:
        by_ext[(r["file_ext"] or "?").lower()].append(r)
    out = []
    for ext, group in sorted(by_ext.items()):
        out.extend(group[:per_ext])
    return out


def download(picked):
    from opensearch_pipeline.prod_access import get_prod_oss_bucket
    bucket = get_prod_oss_bucket()
    ok = fail = 0
    for r in picked:
        raw_key = r["raw_key"]
        dest = os.path.join(DEST, raw_key)
        if os.path.exists(dest):
            print(f"  ⏭  exists: {raw_key}")
            ok += 1
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            bucket.get_object_to_file(raw_key, dest)
            print(f"  📥 {r['doc_id']}: {raw_key} ({os.path.getsize(dest):,}B)")
            ok += 1
        except Exception as e:
            print(f"  ⚠️ {r['doc_id']}: {e}")
            fail += 1
    print(f"[download] ok={ok} fail={fail} → {DEST}/")


def register(picked):
    """注册进本地 MySQL（LOCALSMP_ 前缀），供本地 stage-1→3 零 OSS 跑通。"""
    from local_e2e_ingest import _local_conn
    local = _local_conn()
    n_new = n_skip = 0
    with local.cursor() as cur:
        for r in picked:
            new_id = PREFIX + r["doc_id"]
            cur.execute("SELECT 1 FROM document_meta WHERE doc_id=%s", (new_id,))
            if cur.fetchone():
                n_skip += 1
                continue
            cur.execute(
                "INSERT INTO document_meta (doc_id, title, status, current_version_no)"
                " VALUES (%s, %s, 'active', 1)", (new_id, r["title"]))
            cur.execute(
                "INSERT INTO document_version (doc_id, version_no, raw_key, raw_key_hash,"
                " file_ext, content_process_status, index_status, status)"
                " VALUES (%s, 1, %s, %s, %s, 'NOT_STARTED', 'NOT_INDEXED', 'active')",
                (new_id, r["raw_key"],
                 hashlib.sha256((r["raw_key"] + "#LOCALSMP").encode()).hexdigest(),
                 r["file_ext"]))
            n_new += 1
    local.commit()
    local.close()
    print(f"[register] inserted={n_new} skipped(existing)={n_skip} (prefix={PREFIX})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--per-ext", type=int, default=0)
    ap.add_argument("--doc-ids", type=str, default="")
    ap.add_argument("--batch", type=str, default="")
    ap.add_argument("--register", action="store_true")
    args = ap.parse_args()

    rows = _prod_rows()
    dist = collections.Counter((r["file_ext"] or "?").lower() for r in rows)
    print(f"[corpus] prod active docs: {len(rows)}; by ext: {dict(dist.most_common())}")
    if args.list_only:
        return

    doc_ids = [s.strip() for s in args.doc_ids.split(",") if s.strip()]
    if args.batch:
        doc_ids = sorted(json.load(open(args.batch, encoding="utf-8")).keys())
    if not doc_ids and not args.per_ext:
        print("用 --per-ext N / --doc-ids / --batch 指定采样范围（或 --list-only）")
        return

    picked = _pick(rows, per_ext=args.per_ext or None, doc_ids=doc_ids or None)
    print(f"[sample] picked {len(picked)} docs")
    download(picked)
    if args.register:
        register(picked)


if __name__ == "__main__":
    main()
