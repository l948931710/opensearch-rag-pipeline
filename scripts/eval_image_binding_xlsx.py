#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_image_binding_xlsx.py — XLSX 逐图绑定精度独立可跑(block_index Jaccard)

用法:
  python scripts/eval_image_binding_xlsx.py [--gt-file ...] [--docs-dir ...] [--out ...]

口径:
  XLSX GT chunk 的 expected_image_refs 用 {"block_index": N};
  UnifiedExtractor 把 image 也注成 block,chunk.extra.image_refs 里有 block_index
  或 anchor_row(parse_ref_dict 自动认这两个别名,生效列在 ref_keys.py)。

Day 1 实测:XLSX 生产 step_card 仅 19 个,样本极少 — plan 首轮 soft ≥0.80,
单题翻车就掉 5pp,std>5pp 不升 hard。`xlsx_inspect` 历史用 count_only schema,
在 GT 升级到全 schema 之前会通过 _meta.skip_in_binding 自动排除主闸(degraded=True)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # 直跑 `python scripts/...` 时找到 eval_harness/

DEFAULT_DATA = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples")
DEFAULT_GT_FILES = [
    os.path.join(DEFAULT_DATA, "ground_truth", "gt_xlsx_pptx_analysis.json"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt-file", action="append")
    ap.add_argument("--docs-dir", default=os.path.join(DEFAULT_DATA, "documents"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    gt_files = [p for p in (args.gt_file or DEFAULT_GT_FILES) if os.path.exists(p)]
    if not gt_files:
        candidates = args.gt_file or DEFAULT_GT_FILES
        print(f"⚠️ 没有 XLSX GT 文件(检查过: {candidates})")
        sys.exit(0)

    from eval_harness.binding import ingestion_binding
    result = ingestion_binding.run(gt_files, args.docs_dir, only_fmt="xlsx")

    determ = result["deterministic"]
    print(f"\n══ XLSX 图文绑定精度 ({datetime.now():%Y-%m-%d %H:%M}) ══\n")
    fmt_agg = determ["per_fmt"].get("xlsx")
    if not fmt_agg or fmt_agg["n_strong_chunks"] == 0:
        print(f"  ⚠️ 无 strong-GT chunks(per_fmt.xlsx = {fmt_agg})")
        print("    可能原因:GT 还没填 expected_image_refs,或全 doc degraded。")
    else:
        print(f"  XLSX docs:               {fmt_agg['n_docs']}"
              f" (degraded: {fmt_agg['n_degraded_docs']})")
        print(f"  Strong GT chunks (∋图):  {fmt_agg['n_strong_chunks']}")
        print(f"  Mean Jaccard:            {fmt_agg['mean_jaccard']:.4f}"
              + (f"  (std={fmt_agg['std_jaccard']:.4f})" if fmt_agg.get("std_jaccard") else ""))

    xlsx_docs = [d for d in result["per_doc"] if d.get("fmt") == "xlsx"]
    print("\n  逐文档:")
    for d in xlsx_docs:
        if "error" in d:
            print(f"    ❌ {d['label']}: {d['error']}")
            continue
        if d.get("degraded"):
            print(f"    ⚠️ {d['label']:30s} degraded(skip 主闸)")
            continue
        mj = f"{d['mean_jaccard']:.3f}" if d.get("mean_jaccard") is not None else "n/a"
        print(f"    {d['label']:30s} strong={d['n_strong_chunks']:>2d} "
              f"jaccard={mj}  step_cards={d['n_step_cards']:>3d}")

    if determ["errors"]:
        print(f"\n  ❌ errors ({len(determ['errors'])} 个):")
        for e in determ["errors"][:5]:
            print(f"    - {e}")

    out = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scratch", f"binding_xlsx_{datetime.now():%Y%m%d_%H%M}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✓ 详细结果已写入 {out}")


if __name__ == "__main__":
    main()
