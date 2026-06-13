#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_gt_refs.py — 校验 GT 的 expected_image_refs 全部存在于 manifest

用法:
  python -m eval_harness.scripts.validate_gt_refs \
      --gt-file ~/Downloads/opensearch-rag-data/eval_samples/ground_truth/gt_pdf_analysis.json \
      --manifest-dir scratch/eval_manifest

每个 GT doc 找其 manifest_path(或自动按 doc_label 推),逐条 validate_gt_against_manifest,
任一文档失败 exit 非 0,CI 可挡。
"""
from __future__ import annotations

import argparse
import os
import sys

from eval_harness.binding.gt_loader import load_gt, validate_gt_against_manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt-file", required=True, help="ground_truth/gt_*.json 路径")
    ap.add_argument("--manifest-dir", required=True, help="manifest 目录")
    ap.add_argument("--strict", action="store_true",
                    help="degraded 的 doc 也参与校验(默认跳过)")
    args = ap.parse_args()

    docs = load_gt(args.gt_file)
    total_fail = 0
    for label, doc in docs.items():
        if doc.degraded and not args.strict:
            print(f"  ⚠️  {label}: degraded — skipped")
            continue
        manifest_path = (doc.manifest_path
                         or os.path.join(args.manifest_dir, f"{label}_images.json"))
        if not os.path.exists(manifest_path):
            print(f"  ❌ {label}: manifest 不存在 ({manifest_path})")
            total_fail += 1
            continue
        result = validate_gt_against_manifest(doc, manifest_path)
        if result["ok"]:
            n_refs = sum(len(c.expected_image_refs) for c in doc.gt_chunks)
            print(f"  ✅ {label}: {n_refs} refs all valid")
        else:
            total_fail += 1
            print(f"  ❌ {label}:")
            for r in result["reasons"]:
                print(f"      - {r}")
            for m in result["missing_refs"][:5]:
                print(f"      missing: chunk={m['chunk_label']!r} ref={m['ref']}")

    if total_fail:
        print(f"\n❌ {total_fail} 个 doc 校验失败")
        sys.exit(1)
    print(f"\n✅ all {len(docs)} docs validated")


if __name__ == "__main__":
    main()
