#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_image_binding_docx.py — DOCX 逐图绑定精度独立可跑(strict 邻近文本匹配)

DOCX GT(gt_docx_analysis.json)D5 实测 0 个 expected_image_refs,主 Jaccard 路径出
None。本脚本默认设 env EVAL_L4_DOCX_BINDING_ENABLE=true 触发 repo 内
fuling_chunk_exp/ fixture 走 strict 邻近文本匹配(production-faithful 路由,
复用 evaluate_doc 的 UnifiedExtractor + node_chunk_documents,SOP-only 入主聚合),
产 micro-accuracy 当 binding_jaccard_docx 写主聚合。

用法:
  python scripts/eval_image_binding_docx.py
    (默认跑;fuling_chunk_exp/ 缺失 / extractor import 失败 → 优雅 exit 0)
  python scripts/eval_image_binding_docx.py --no-fixture
    (禁 fixture 路径;留给 D7+ 补完 expected_image_refs 后只跑主 GT Jaccard)
  python scripts/eval_image_binding_docx.py --out scratch/binding_docx_<ts>.json

DOCX 不走 manifest(与 PDF 不同),GT 文件仅作辅助:gt_docx_analysis.json 存在则一
并喂 ingestion_binding.run 让未来主 Jaccard 路径自动接管;不存在也不阻断,strict
fixture 即可独立出数。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # `python scripts/eval_image_binding_docx.py` 直跑时找到 eval_harness/

DEFAULT_DATA = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples")
DEFAULT_GT_FILE = os.path.join(DEFAULT_DATA, "ground_truth", "gt_docx_analysis.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fixture", action="store_true",
                    help="禁 strict fixture 路径,只跑主 GT Jaccard(D7+ 补 GT 后用)")
    ap.add_argument("--gt-file", action="append",
                    help="可重复;默认 gt_docx_analysis.json(若存在)")
    ap.add_argument("--docs-dir", default=os.path.join(DEFAULT_DATA, "documents"))
    ap.add_argument("--out", default=None, help="JSON 输出路径(默认 scratch/)")
    args = ap.parse_args()

    if not args.no_fixture:
        os.environ["EVAL_L4_DOCX_BINDING_ENABLE"] = "true"
    else:
        # 显式禁:不动 env(若调用方已设也尊重),但若已开启则清掉以避免误触发
        os.environ.pop("EVAL_L4_DOCX_BINDING_ENABLE", None)

    gt_files = [p for p in (args.gt_file or [DEFAULT_GT_FILE]) if os.path.exists(p)]
    if not gt_files and args.no_fixture:
        print(f"⚠️  --no-fixture 且无 GT 文件可读(检查过: {args.gt_file or [DEFAULT_GT_FILE]})")
        print("    GT 待人工补 expected_image_refs;本脚本现退出 0。")
        sys.exit(0)

    from eval_harness.binding import ingestion_binding
    result = ingestion_binding.run(gt_files, args.docs_dir, only_fmt="docx")

    determ = result["deterministic"]
    fmt_agg = determ["per_fmt"].get("docx") or {}
    src = fmt_agg.get("_source", "gt_jaccard")
    print(f"\n══ DOCX 图文绑定精度 ({datetime.now():%Y-%m-%d %H:%M}) 来源={src} ══\n")

    if not fmt_agg or (fmt_agg.get("n_strong_chunks") or 0) == 0:
        print(f"  ⚠️ 无可评 DOCX 样本(per_fmt.docx = {fmt_agg})")
        print("    检查:")
        print("      1) EVAL_L4_DOCX_BINDING_ENABLE=true(--no-fixture 关闭了)")
        print("      2) fuling_chunk_exp/ 目录在")
        print("      3) gt_docx_analysis.json 是否已补 expected_image_refs(D7+)")
    else:
        print(f"  DOCX docs(SOP):          {fmt_agg.get('n_docs')}"
              f" (degraded 非 SOP: {fmt_agg.get('n_degraded_docs', 0)})")
        print(f"  Strong chunks (∋图):     {fmt_agg.get('n_strong_chunks')}")
        mj = fmt_agg.get("mean_jaccard")
        mj_str = f"{mj:.4f}" if mj is not None else "n/a"
        std = fmt_agg.get("std_jaccard")
        std_str = f"  (std={std:.4f})" if std is not None else ""
        label = "Mean Jaccard" if src == "gt_jaccard" else "Micro-accuracy(strict, SOP-only)"
        print(f"  {label}:           {mj_str}{std_str}")
        print(f"  img_dup_factor (全格式): p95={determ.get('img_dup_factor_p95')}"
              f"  max={determ.get('img_dup_factor_max')}")

    docx_docs = [d for d in result["per_doc"] if d.get("fmt") == "docx"]
    if docx_docs:
        print("\n  逐文档:")
        for d in docx_docs[:60]:
            if "error" in d:
                print(f"    ❌ {d['label']}: {d['error']}")
                continue
            mj = d.get("mean_jaccard")
            mj_str = f"{mj:.3f}" if mj is not None else "n/a"
            n_strong = d.get("n_strong_chunks", 0)
            n_step = d.get("n_step_cards", 0)
            sop_mark = "SOP" if d.get("is_sop") else "non-SOP"
            print(f"    [{sop_mark:>7s}] {d['label'][:50]:50s} strong={n_strong:>3d}  "
                  f"acc={mj_str}  step_cards={n_step:>3d}")

    if determ.get("errors"):
        print(f"\n  ❌ errors ({len(determ['errors'])} 个):")
        for e in determ["errors"][:8]:
            print(f"    - {e}")

    out = args.out or os.path.join(
        ROOT, "scratch", f"binding_docx_{datetime.now():%Y%m%d_%H%M}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✓ 详细结果已写入 {out}")


if __name__ == "__main__":
    main()
