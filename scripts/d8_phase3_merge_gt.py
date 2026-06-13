#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
d8_phase3_merge_gt.py — 把 workflow 标注产出的 GT 对象合进 gt_pdf_analysis.json。

输入:
  - workflow_output_file: workflow 返回的 JSON,含 gt_objects 列表(每个对象一个 doc 的 GT)
  - existing_gt_file: 现有 gt_pdf_analysis.json

输出:
  - .bak_d8p3 备份 + 原文件就地更新

保留 _meta 不动,只追加新 doc keys。
"""
import argparse
import json
import os
import shutil
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow-out", required=True, help="workflow JSON output file")
    ap.add_argument("--gt-file", default=os.path.expanduser(
        "~/Downloads/opensearch-rag-data/eval_samples/ground_truth/gt_pdf_analysis.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wf_out = json.load(open(args.workflow_out))
    gt = json.load(open(args.gt_file))

    gt_objects = wf_out.get("gt_objects", [])
    if not gt_objects:
        print("[!] workflow output has no gt_objects")
        sys.exit(2)

    added = []
    for obj in gt_objects:
        if not isinstance(obj, dict):
            print(f"[!] skip non-dict entry: {type(obj).__name__}")
            continue
        label = obj.get("doc_label")
        if not label:
            print(f"[!] entry missing doc_label, keys={list(obj.keys())}")
            continue
        if label in gt:
            print(f"[!] {label} already exists in GT — refuse to overwrite")
            continue
        # 拷贝标 GT chunks + _doc_meta
        gt[label] = {
            "_doc_meta": obj.get("_doc_meta", {}),
            "gt_chunks": obj.get("gt_chunks", []),
        }
        n_chunks = len(obj.get("gt_chunks", []))
        n_with_imgs = sum(1 for c in obj.get("gt_chunks", []) if c.get("expected_image_refs"))
        added.append((label, n_chunks, n_with_imgs))
        print(f"  + {label}: {n_chunks} chunks ({n_with_imgs} with images)")

    if not added:
        print("[!] no entries to merge")
        sys.exit(2)

    if args.dry_run:
        print("\n[dry-run] would write to:", args.gt_file)
        return

    bak = args.gt_file + ".bak_d8p3"
    shutil.copy2(args.gt_file, bak)
    print(f"\n  backup: {bak}")

    with open(args.gt_file, "w") as f:
        json.dump(gt, f, ensure_ascii=False, indent=2)
    print(f"  wrote: {args.gt_file}")
    print(f"  total doc keys now: {[k for k in gt.keys() if k != '_meta']}")


if __name__ == "__main__":
    main()
