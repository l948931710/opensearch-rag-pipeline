"""Orchestrator for the end-to-end HA3 RAG eval.

Phases:
  run    : execute layers L0-L5 live (read-only), write results.json + judge_bundle.json
           + a preliminary report (deterministic gates only).
  merge  : load results.json + judge_verdicts.json (authored by the Claude panel),
           compute judge aggregates, write the FINAL report with all gates.

Usage:
  python -m eval_harness.run_eval run   [--goldset PATH] [--layers l0,l1,l2,l3,l4,l5] [--limit N]
  python -m eval_harness.run_eval merge --results PATH --verdicts PATH

Everything that touches HA3/RDS is read-only. Answer generation runs with thinking OFF.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os

from . import envboot
from .ha3live import install_into_retriever

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GOLDSET = os.path.join(HERE, "goldset", "golden_50.json")


def _ts():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_goldset(path):
    cases = json.load(open(path, encoding="utf-8"))
    return cases


def phase_run(args):
    install_into_retriever()  # force public-HTTP client into the production retriever
    from opensearch_pipeline.config import get_config
    cfg = get_config()

    cases = _load_goldset(args.goldset)
    if args.limit:
        cases = cases[: args.limit]
    layers = set(args.layers.split(","))
    outdir = args.outdir or os.path.join(HERE, "reports", f"run_{_ts()}")
    os.makedirs(outdir, exist_ok=True)

    meta = {
        "run_id": os.path.basename(outdir), "timestamp": _ts(),
        "n_cases": len(cases), "goldset": args.goldset, "layers": sorted(layers),
        "llm_model": cfg.llm.model, "embedding_model": cfg.embedding.model,
        **envboot.facts(),
    }
    results = {"meta": meta}
    print(f"== EVAL RUN {meta['run_id']} | table={meta['ha3_table']} | n={len(cases)} ==")
    print(json.dumps({k: meta[k] for k in ("ha3_endpoint", "llm_model", "rag_environment", "simulate")},
                     ensure_ascii=False))

    if "l0" in layers:
        print("\n[L0] index health ...")
        from .layers import l0_index_health
        results["l0"] = l0_index_health.run()
        print(f"   L0 PASS={results['l0'].get('PASS')}")

    if "l1" in layers:
        print("\n[L1] retrieval ranking ...")
        from .layers import l1_retrieval
        results["l1"] = l1_retrieval.run(cases, top_k=10)
        print(f"   ranking={json.dumps(results['l1'].get('ranking'), ensure_ascii=False)}")

    if "l2" in layers and "l1" in results:
        print("\n[L2] score calibration ...")
        from .layers import l2_calibration
        results["l2"] = l2_calibration.run(results["l1"])
        print(f"   thresholds_ok={results['l2'].get('thresholds_ok')}")

    if "l3" in layers:
        print("\n[L3] answer quality (thinking OFF) + judge bundle ...")
        from .layers import l3_answer
        results["l3"] = l3_answer.run(cases, top_k=7)
        json.dump(results["l3"]["judge_bundle"],
                  open(os.path.join(outdir, "judge_bundle.json"), "w"),
                  ensure_ascii=False, indent=1)
        print(f"   deterministic={json.dumps(results['l3']['deterministic'], ensure_ascii=False)}")

    if "l4" in layers:
        print("\n[L4] multimodal (ingestion + serving) ...")
        from .layers import l4_multimodal
        # L4-ingestion DOCX strict 路径默认开(make eval / DataWorks / CI 自动闭环)
        # 显式 export EVAL_L4_DOCX_BINDING_ENABLE=false 可关 — setdefault 不覆盖
        os.environ.setdefault("EVAL_L4_DOCX_BINDING_ENABLE", "true")
        # L4-ingestion 触发:env EVAL_L4_GT_FILES(逗号分隔)+ EVAL_L4_DOCS_DIR
        # 默认指向 eval_samples/ground_truth + documents(repo 外仓)
        _data = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples")
        gt_files_env = os.environ.get("EVAL_L4_GT_FILES")
        if gt_files_env:
            gt_files = [p for p in gt_files_env.split(",") if p.strip()]
        else:
            gt_files = [p for p in [
                os.path.join(_data, "ground_truth", "gt_pdf_analysis.json"),
                os.path.join(_data, "ground_truth", "gt_xlsx_pptx_analysis.json"),
                os.path.join(_data, "ground_truth", "gt_docx_analysis.json"),
            ] if os.path.exists(p)]
        docs_dir = os.environ.get("EVAL_L4_DOCS_DIR") or os.path.join(_data, "documents")
        results["l4"] = l4_multimodal.run(
            cases,
            gt_files=gt_files if gt_files else None,
            docs_dir=docs_dir if os.path.isdir(docs_dir) else None,
        )
        if results["l4"].get("applicable"):
            # serving bundle(可能为空)
            if results["l4"].get("judge_bundle_mm"):
                json.dump(results["l4"]["judge_bundle_mm"],
                          open(os.path.join(outdir, "judge_bundle_mm.json"), "w"),
                          ensure_ascii=False, indent=1)
            # ingestion bundle(新:L4-ingestion Claude image_binding 评审用)
            if results["l4"].get("judge_bundle_binding"):
                json.dump(results["l4"]["judge_bundle_binding"],
                          open(os.path.join(outdir, "judge_bundle_binding.json"), "w"),
                          ensure_ascii=False, indent=1)
        ing_det = (results["l4"].get("ingestion") or {}).get("deterministic", {})
        print(f"   applicable={results['l4'].get('applicable')}  "
              f"ingestion_pdf={ing_det.get('binding_jaccard_pdf')}  "
              f"ingestion_xlsx={ing_det.get('binding_jaccard_xlsx')}  "
              f"dup_p95={ing_det.get('img_dup_factor_p95')}")

    if "l5" in layers:
        print("\n[L5] permission filtering ...")
        from .layers import l5_permission
        results["l5"] = l5_permission.run()
        print(f"   {json.dumps({k: results['l5'].get(k) for k in ('applicable','PASS')}, ensure_ascii=False)}")

    from . import report
    gates = report.write(results, outdir)
    print(f"\n== PRELIMINARY REPORT -> {outdir}/report.md ==")
    for name, g in gates.items():
        mark = "PASS" if g["pass"] is True else ("FAIL" if g["pass"] is False else "N/A")
        print(f"   [{mark}] {name}: {g['value']}")
    print(f"\nNext: judge the bundle (Claude panel) -> {outdir}/judge_verdicts.json, then:\n"
          f"  python -m eval_harness.run_eval merge --results {outdir}/report.json "
          f"--verdicts {outdir}/judge_verdicts.json")
    return outdir


def phase_merge(args):
    results = json.load(open(args.results, encoding="utf-8"))
    verdicts = json.load(open(args.verdicts, encoding="utf-8"))
    # 合并 L3 bundle(judge_bundle.json)+ L4-binding bundle(judge_bundle_binding.json,
    # 2026-06-12 新增)以提供完整 kind_by_qid。任一缺失视为不存在,不阻断 merge。
    outdir = os.path.dirname(args.results)
    bundle: list = []
    for fname in ("judge_bundle.json", "judge_bundle_binding.json"):
        bp = os.path.join(outdir, fname)
        if os.path.exists(bp):
            try:
                bundle.extend(json.load(open(bp, encoding="utf-8")))
            except Exception:
                pass

    from .judge import merge_panel
    panels = verdicts["panels"] if isinstance(verdicts, dict) and "panels" in verdicts else verdicts
    results["judge"] = merge_panel(bundle, panels)

    from . import report
    outdir = os.path.dirname(args.results)
    gates = report.write(results, outdir)
    print(f"== FINAL REPORT -> {outdir}/report.md ==")
    for name, g in gates.items():
        mark = "PASS" if g["pass"] is True else ("FAIL" if g["pass"] is False else "N/A")
        print(f"   [{mark}] {name}: {g['value']}")
    return outdir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["run", "merge"])
    ap.add_argument("--goldset", default=DEFAULT_GOLDSET)
    ap.add_argument("--layers", default="l0,l1,l2,l3,l4,l5")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--outdir", default="")
    ap.add_argument("--results", default="")
    ap.add_argument("--verdicts", default="")
    args = ap.parse_args()
    if args.phase == "run":
        phase_run(args)
    else:
        phase_merge(args)


if __name__ == "__main__":
    main()
