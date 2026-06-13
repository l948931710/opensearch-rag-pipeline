#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_image_binding_pdf.py — PDF 逐图绑定精度独立可跑(per-step expected_image_refs Jaccard)

用法:
  python scripts/eval_image_binding_pdf.py \
      [--gt-file ~/Downloads/opensearch-rag-data/eval_samples/ground_truth/gt_pdf_analysis.json] \
      [--docs-dir ~/Downloads/opensearch-rag-data/eval_samples/documents] \
      [--out scratch/binding_pdf_<ts>.json]

输出:
  - stdout 表格(每 GT chunk 一行 + 全局摘要)
  - JSON 文件(完整 per_doc + per_chunk + judge_bundle_binding)

这是 ingestion_binding.run(only_fmt='pdf') 的薄包装,但同时印漂亮表格人工读用。
Day 1 实测 PDF 生产 binding 最强(45.4% step_card 有图),plan 阈值 ≥0.70 可能保守。
首轮 soft 跑两轮看真分布,std≤5pp 升 hard。

也跑 gt_xlsx_pptx_analysis.json 不会有 PDF 数据 — 默认只读 gt_pdf_analysis.json,
若 GT 文件不存在自动跳过(适合 GT 还没标完的中间态)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # `python scripts/eval_image_binding_pdf.py` 直跑时也能找到 eval_harness/

DEFAULT_DATA = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples")
DEFAULT_GT_FILES = [
    os.path.join(DEFAULT_DATA, "ground_truth", "gt_pdf_analysis.json"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt-file", action="append", help="可重复;默认 gt_pdf_analysis.json")
    ap.add_argument("--docs-dir", default=os.path.join(DEFAULT_DATA, "documents"))
    ap.add_argument("--out", default=None, help="JSON 输出路径(默认 scratch/)")
    args = ap.parse_args()

    # 显式 --gt-file 也要过 exist 过滤(用户传错路径时优雅退出而非 FileNotFoundError)
    gt_files = [p for p in (args.gt_file or DEFAULT_GT_FILES) if os.path.exists(p)]
    if not gt_files:
        candidates = args.gt_file or DEFAULT_GT_FILES
        print(f"⚠️  没有可用的 PDF GT 文件(检查过: {candidates})")
        print("    GT 待 D2-3 manifest 生成后人工标注 — 见 plan.md。本脚本现退出 0。")
        sys.exit(0)

    from eval_harness.binding import ingestion_binding
    result = ingestion_binding.run(gt_files, args.docs_dir, only_fmt="pdf")

    determ = result["deterministic"]
    # 打表
    print(f"\n══ PDF 图文绑定精度 ({datetime.now():%Y-%m-%d %H:%M}) ══\n")
    fmt_agg = determ["per_fmt"].get("pdf")
    if not fmt_agg or fmt_agg["n_strong_chunks"] == 0:
        print(f"  ⚠️ 无 strong-GT chunks(per_fmt.pdf = {fmt_agg})")
        print("    可能原因:gt_pdf_analysis.json 还没填 expected_image_refs;")
        print("    跑 eval_harness.scripts.gen_image_manifest 出图清单后人工标。")
    else:
        print(f"  PDF docs:                {fmt_agg['n_docs']}"
              f" (degraded: {fmt_agg['n_degraded_docs']})")
        print(f"  Strong GT chunks (∋图):  {fmt_agg['n_strong_chunks']}")
        print(f"  Mean Jaccard:            {fmt_agg['mean_jaccard']:.4f}"
              + (f"  (std={fmt_agg['std_jaccard']:.4f})" if fmt_agg.get("std_jaccard") else ""))
        print(f"  img_dup_factor (全格式): p95={determ.get('img_dup_factor_p95')}"
              f"  max={determ.get('img_dup_factor_max')}")

    # 逐 doc 详情
    pdf_docs = [d for d in result["per_doc"] if d.get("fmt") == "pdf"]
    print("\n  逐文档:")
    for d in pdf_docs:
        if "error" in d:
            print(f"    ❌ {d['label']}: {d['error']}")
            continue
        mj = f"{d['mean_jaccard']:.3f}" if d.get("mean_jaccard") is not None else "n/a"
        print(f"    {d['label']:30s} strong={d['n_strong_chunks']:>2d} "
              f"jaccard={mj}  step_cards={d['n_step_cards']:>3d}  "
              f"dup={d.get('img_dup_factor', 1.0):.2f}")
        # 弱 chunk warning(只标 page 的)
        weak = sum(1 for pc in d.get("per_chunk", []) if pc.get("weak"))
        if weak:
            print(f"      ⚠️ {weak} 个弱 GT(只标 page,不计入 Jaccard 均值)")

    if determ["errors"]:
        print(f"\n  ❌ errors ({len(determ['errors'])} 个):")
        for e in determ["errors"][:5]:
            print(f"    - {e}")

    # 落盘
    out = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scratch", f"binding_pdf_{datetime.now():%Y%m%d_%H%M}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✓ 详细结果已写入 {out}")


if __name__ == "__main__":
    main()
