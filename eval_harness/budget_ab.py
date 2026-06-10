# -*- coding: utf-8 -*-
"""budget_ab.py — 检索预算配对 A/B：top_k=7（生产）vs top_k=10。

动机（multi_doc_guard_findings.md 后续项 2）：跨文档"综合/枚举型"问题的 full-coverage
受 top-7 上下文容量硬约束（gold 预期 4~6 份文档装不下 7 条 chunk）。本评测回答：
  1. 把 top_k 提到 10（rerank 池 20 内直接多取）能换多少跨文档覆盖？
  2. 单文档集是否无回归（非劣界 −5pp）？
  3. 上下文要多大预算（拼接后字符分布 p50/p90）→ RAG_MAX_CONTEXT_CHARS 该配多少？
     （k7+stitch≈5.7k ≤ 6000 现值；k10 必然溢出 6000，量化溢出幅度）

只做检索层（multi-doc gold 无 keyword_gt，生成层需评审团另评——若本评测显示覆盖
增益可观再投入）。复用 multi_doc_ab 的 case 加载/打分/配对统计，mode=off cap=0。

用法：RAG_ENV=test python -m eval_harness.budget_ab [--out eval_harness/reports/budget_ab.json]
"""
import argparse
import json
import os

from eval_harness import envboot  # noqa: F401
from eval_harness.multi_doc_ab import (
    load_cases, log, paired_summary, run_retrieval_arm, strip_chunks,
)


def _pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))]


def context_budget_stats(rows):
    chars = [r["context_chars"] for r in rows.values() if not r.get("error")]
    return {
        "n": len(chars),
        "p50": _pct(chars, 50), "p90": _pct(chars, 90), "max": max(chars) if chars else None,
        "over_6000_rate": round(sum(1 for c in chars if c > 6000) / len(chars), 4)
        if chars else None,
        "over_8000_rate": round(sum(1 for c in chars if c > 8000) / len(chars), 4)
        if chars else None,
    }


def strata_summary(rows):
    ok = [r for r in rows.values() if r["coverage_frac"] is not None]
    small = [r for r in ok if r["n_expected"] <= 3]
    big = [r for r in ok if r["n_expected"] >= 4]
    from statistics import mean
    return {
        "full_coverage_2to3": round(mean([1.0 if r["full_coverage"] else 0.0
                                          for r in small]), 4) if small else None,
        "cov_frac_4plus": round(mean([r["coverage_frac"] for r in big]), 4) if big else None,
        "at_least_2_docs_rate": round(mean([1.0 if r["n_matched"] >= 2 else 0.0
                                            for r in ok]), 4) if ok else None,
        "at_least_3_docs_rate": round(mean([1.0 if r["n_matched"] >= 3 else 0.0
                                            for r in ok]), 4) if ok else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="eval_harness/reports/budget_ab.json")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    multi, single, _negs = load_cases(quick=args.quick)
    log(f"cases: multi={len(multi)} single={len(single)} "
        f"(table={os.environ.get('RAG_HA3_TABLE_NAME')}, rerank=on, mode=off, cap=0)")

    if not args.quick:
        log("warm-up pass (uncounted) ...")
        run_retrieval_arm(multi + single, "off", "warm-up", {}, top_k=7)

    report = {"multidoc": {}, "single": {}}
    arms_multi, arms_single = {}, {}
    for k in (7, 10):
        tb = {}
        arms_multi[f"k{k}"] = run_retrieval_arm(multi, "off", f"multi-doc k{k}", tb, top_k=k)
        arms_single[f"k{k}"] = run_retrieval_arm(single, "off", f"single-doc k{k}", tb, top_k=k)

    report["multidoc"]["arms"] = {m: strip_chunks(r) for m, r in arms_multi.items()}
    report["single"]["arms"] = {m: strip_chunks(r) for m, r in arms_single.items()}
    report["multidoc"]["k7_vs_k10"] = paired_summary(arms_multi["k7"], arms_multi["k10"],
                                                     "k7", "k10")
    report["single"]["k7_vs_k10"] = paired_summary(arms_single["k7"], arms_single["k10"],
                                                   "k7", "k10")
    report["multidoc"]["strata"] = {m: strata_summary(r) for m, r in arms_multi.items()}
    report["context_budget"] = {
        f"{seg}_{m}": context_budget_stats(rows)
        for seg, arms in (("multi", arms_multi), ("single", arms_single))
        for m, rows in arms.items()
    }

    for k, v in report["multidoc"].items():
        if k != "arms":
            log(f"multi-doc {k}: {json.dumps(v, ensure_ascii=False)}")
    log(f"single k7→k10: {json.dumps(report['single']['k7_vs_k10'], ensure_ascii=False)}")
    log(f"context budget: {json.dumps(report['context_budget'], ensure_ascii=False)}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    log(f"saved → {args.out}")


if __name__ == "__main__":
    main()
