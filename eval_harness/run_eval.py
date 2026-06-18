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
import sys

from . import envboot
from .ha3live import install_into_retriever

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GOLDSET = os.path.join(HERE, "goldset", "golden_50.json")


def _ts():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_goldset(path):
    cases = json.load(open(path, encoding="utf-8"))
    return cases


# The retrieval regime the L1/L2/L3 score thresholds were calibrated on. RRF (or an un-accounted
# fusion change) invalidates the absolute thresholds even when gates still "pass" → the guard fails.
CALIBRATION_FUSION = "weighted"

# Production serves with the routed reranker ON (verified read-only 2026-06-18: docs/audits/L6_*_
# 2026-06-15 record RAG_RERANK_ENABLE=true, and live qa_session_log.top_score sits in the 0-1 rerank
# band — NOT the 7.7/5.8 weighted scale). The eval MUST run in the same rerank state or its L1/L2
# numbers don't represent production: L2 switches to the rerank 0.9/0.8 bands when rerank is on
# (l2_calibration.py), so a rerank-OFF run scores against the wrong thresholds. Override ONLY to
# deliberately evaluate/recalibrate a different regime.
CALIBRATION_RERANK = os.environ.get(
    "RAG_EVAL_CALIBRATION_RERANK", "true").strip().lower() in ("1", "true", "yes", "on")


def _cfg_get(cfg, name, default=None):
    """hybrid_fusion / rerank_enable live on cfg or cfg.alibaba_vector depending on version."""
    if hasattr(cfg, name):
        return getattr(cfg, name)
    av = getattr(cfg, "alibaba_vector", None)
    if av is not None and hasattr(av, name):
        return getattr(av, name)
    return default


def _regime(cfg, goldset_path: str) -> dict:
    """Run-condition fingerprint stamped into meta + the baseline, so deltas are never compared across
    different eval-set / code / model / reranker / fusion / threshold conditions."""
    import hashlib
    try:
        sha = hashlib.sha256(open(goldset_path, "rb").read()).hexdigest()[:16]
    except Exception:
        sha = "unknown"
    try:
        from opensearch_pipeline.versions import git_commit
        commit = git_commit()
    except Exception:
        commit = "unknown"
    av = getattr(cfg, "alibaba_vector", None)
    rag = getattr(cfg, "rag", cfg)
    rerank_on = bool(_cfg_get(cfg, "rerank_enable", False))
    thr = (f"sh={getattr(rag,'score_threshold_high',None)},sm={getattr(rag,'score_threshold_medium',None)},"
           f"rh={getattr(rag,'rerank_score_threshold_high',None)},rm={getattr(rag,'rerank_score_threshold_medium',None)}")
    return {
        "eval_set_sha": sha,
        "fusion": _cfg_get(cfg, "hybrid_fusion", None),
        "rerank_enable": rerank_on,
        "llm_model": cfg.llm.model,
        "embedding_model": cfg.embedding.model,
        "reranker_models": (f"{getattr(av,'rerank_text_model',None)}/{getattr(av,'rerank_vl_model',None)}"
                            if rerank_on and av is not None else None),
        "threshold_version": thr,
        "code_commit": commit,
    }


def _regime_guard(regime: dict) -> dict:
    """Fail-closed if the active retrieval regime != the regime the thresholds were calibrated on
    (== production serving regime). Pins BOTH fusion and rerank state: L2 uses different score bands
    for rerank-on vs -off, so a rerank mismatch makes L1/L2 absolute numbers non-representative."""
    active_fusion = regime.get("fusion")
    active_rerank = bool(regime.get("rerank_enable"))
    fusion_ok = active_fusion == CALIBRATION_FUSION
    rerank_ok = active_rerank == CALIBRATION_RERANK
    mismatch = [m for m, ok in (("fusion", fusion_ok), ("rerank", rerank_ok)) if not ok]
    return {"expected_fusion": CALIBRATION_FUSION, "active_fusion": active_fusion,
            "expected_rerank": CALIBRATION_RERANK, "active_rerank": active_rerank,
            "rerank_enable": active_rerank,
            "match": (fusion_ok and rerank_ok), "mismatch": mismatch}


def _strict_enabled(args) -> bool:
    return bool(getattr(args, "strict", False)) or \
        os.environ.get("RAG_EVAL_STRICT", "").lower() in ("1", "true", "yes")


def _strict_failures(gates: dict, results: dict, *, requested_layers=None) -> list:
    """Hard-fail reasons under --strict. A gate's ``pass``:
      True → ok ; False → fail ; None → depends on ``na_reason``:
        - 'not_executed'  (sample / config / verdict shortfall — should have run but didn't) → FAIL.
          Never silently pass an unmeasured HARD gate.
        - 'expected_na'   (genuinely inapplicable by design, e.g. L5 with no gated docs) → ok.
    Plus: L6 NO_GO_DEFECT; EVAL-2 manifest drift; answer-correctness-not-judged (L3 ran but no judge
    merge); and L6 requested but un-runnable. (L6 NO_GO_INCOMPLETE_EVIDENCE already FAILs via its
    verdict gate's pass=False — once L6 is actually in the run.)"""
    fails = []
    for name, g in (gates or {}).items():
        p = g.get("pass")
        if g.get("advisory"):
            continue  # advisory/diagnostic gate (e.g. L6-soft) — visible in the report, never blocks
        if p is False:
            fails.append(name)
        elif p is None and g.get("na_reason") == "not_executed":
            fails.append(f"{name} [not_executed]")
    if (results.get("l6") or {}).get("state") == "NO_GO_DEFECT":
        fails.append("l6:NO_GO_DEFECT")
    _errs = ((results.get("l4") or {}).get("ingestion") or {}).get("deterministic", {}).get("errors") or []
    if [e for e in _errs if isinstance(e, str) and e.startswith("manifest_drift::")]:
        fails.append("l4:manifest_drift")
    # answer-correctness MUST be judged: L3 ran (answers generated) but no judge merge → a strict run
    # cannot certify correctness. This is a FAIL, not "judge not enabled".
    if results.get("l3") and not (results.get("judge") or {}).get("aggregate"):
        fails.append("answer_correctness:not_judged (merge a judge_verdicts.json)")
    # L6 was requested but could not be evaluated at all → not a silent skip.
    if requested_layers and "l6" in requested_layers and (results.get("l6") or {}).get("applicable") is False:
        fails.append("l6:not_executed (requested but applicable=False)")
    return fails


def _enforce_strict(gates: dict, results: dict, strict: bool, *, requested_layers=None):
    """EVAL-1: convert advisory gates into a blocking exit code (the CI gate, EVAL-3, relies on this)."""
    if not strict:
        return
    fails = _strict_failures(gates, results, requested_layers=requested_layers)
    if fails:
        print(f"\n❌ STRICT: hard gate failure(s): {fails} → exit 1")
        sys.exit(1)
    print("\n✅ STRICT: all hard gates passed")


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
    regime = _regime(cfg, args.goldset)
    results["meta"]["regime"] = regime
    results["regime_guard"] = _regime_guard(regime)  # item 3: fusion/calibration consistency gate
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
        # EVAL-2: image-manifest dir for GT preflight; default mirrors the existing CLI heuristic
        manifest_dir = os.environ.get("EVAL_L4_MANIFEST_DIR") or os.path.join(_data, "scratch", "eval_manifest")
        results["l4"] = l4_multimodal.run(
            cases,
            gt_files=gt_files if gt_files else None,
            docs_dir=docs_dir if os.path.isdir(docs_dir) else None,
            manifest_dir=manifest_dir if os.path.isdir(manifest_dir) else None,
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

    if "l6" in layers:
        print("\n[L6] chunk-artifact content quality (read-only) ...")
        from .layers import l6_chunk_quality
        results["l6"] = l6_chunk_quality.run(d7_json_path=os.environ.get("EVAL_L6_D7_JSON"))
        if results["l6"].get("applicable") and results["l6"].get("judge_bundle_chunk"):
            json.dump(results["l6"]["judge_bundle_chunk"],
                      open(os.path.join(outdir, "judge_bundle_chunk.json"), "w"),
                      ensure_ascii=False, indent=1)
        print(f"   verdict={results['l6'].get('state')}  go_no_go={results['l6'].get('go_no_go')}  "
              f"d7={results['l6'].get('d7_source')}")

    if args.baseline and os.path.exists(args.baseline):
        from . import baseline as _bl
        results["baseline_gates"] = _bl.compare(
            json.load(open(args.baseline, encoding="utf-8")), results)
    from . import report
    gates = report.write(results, outdir)
    print(f"\n== PRELIMINARY REPORT -> {outdir}/report.md ==")
    for name, g in gates.items():
        mark = ("PASS" if g["pass"] is True else "FAIL" if g["pass"] is False
                else "N/A!" if g.get("na_reason") == "not_executed" else "N/A")
        print(f"   [{mark}] {name}: {g['value']}")
    print(f"\nNext: judge the bundle (Claude panel) -> {outdir}/judge_verdicts.json, then:\n"
          f"  python -m eval_harness.run_eval merge --results {outdir}/report.json "
          f"--verdicts {outdir}/judge_verdicts.json")
    _enforce_strict(gates, results, _strict_enabled(args), requested_layers=layers)
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

    # L6 chunk-judge merge (separate rubric/buckets) — convention-based files next to results.
    # judge_bundle_chunk.json (from phase_run) + judge_verdicts_chunk.json (from the panel).
    cb = os.path.join(outdir, "judge_bundle_chunk.json")
    cv = os.path.join(outdir, "judge_verdicts_chunk.json")
    if os.path.exists(cb) and os.path.exists(cv):
        from .judge import merge_chunk_panel
        chunk_bundle = json.load(open(cb, encoding="utf-8"))
        cverd = json.load(open(cv, encoding="utf-8"))
        cpanels = cverd["panels"] if isinstance(cverd, dict) and "panels" in cverd else cverd
        results.setdefault("l6", {})["judge_chunk"] = merge_chunk_panel(chunk_bundle, cpanels)

    if args.baseline and os.path.exists(args.baseline):
        from . import baseline as _bl
        results["baseline_gates"] = _bl.compare(
            json.load(open(args.baseline, encoding="utf-8")), results)
    from . import report
    outdir = os.path.dirname(args.results)
    gates = report.write(results, outdir)
    print(f"== FINAL REPORT -> {outdir}/report.md ==")
    for name, g in gates.items():
        mark = ("PASS" if g["pass"] is True else "FAIL" if g["pass"] is False
                else "N/A!" if g.get("na_reason") == "not_executed" else "N/A")
        print(f"   [{mark}] {name}: {g['value']}")
    _enforce_strict(gates, results, _strict_enabled(args),
                    requested_layers=set((results.get("meta") or {}).get("layers") or []))
    return outdir


def phase_baseline_freeze(args):
    """Freeze the current run's report.json into a committed baseline (regime-tagged)."""
    from . import baseline as _bl
    results = json.load(open(args.results, encoding="utf-8"))
    out = args.baseline or os.path.join(HERE, "goldset", "baseline.json")
    base = _bl.freeze(results, out)
    print(f"== BASELINE FROZEN -> {out} ==")
    print(f"   regime: {json.dumps(base['regime'], ensure_ascii=False)}")
    print(f"   metrics: {len(base['metrics'])} (delta={base['delta']})")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["run", "merge", "baseline-freeze"])
    ap.add_argument("--goldset", default=DEFAULT_GOLDSET)
    ap.add_argument("--layers", default="l0,l1,l2,l3,l4,l5,l6")  # L6 now in default (item 4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--outdir", default="")
    ap.add_argument("--results", default="")
    ap.add_argument("--verdicts", default="")
    ap.add_argument("--baseline", default="",
                    help="path to a frozen baseline.json; enables per-layer/subset regression gating")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on any hard gate FAIL / not_executed / L6 NO_GO_DEFECT / "
                         "unjudged correctness (CI gate); also enabled by RAG_EVAL_STRICT=true")
    args = ap.parse_args()
    if args.phase == "run":
        phase_run(args)
    elif args.phase == "merge":
        phase_merge(args)
    else:
        phase_baseline_freeze(args)


if __name__ == "__main__":
    main()
