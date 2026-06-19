# -*- coding: utf-8 -*-
"""tests/test_eval_strict_semantics.py — EVAL items 1-4 trustworthiness fixes.

1 baseline regression (per layer/subset + regime-gated)  ·  2 pass:None taxonomy + judge-missing
3 fusion/calibration regime guard  ·  4 L6 in default layers.
run_eval is imported lazily inside tests (envboot.boot() runs on import) — matches test_observability_ci.
"""
import json


# ── item 1: baseline ──────────────────────────────────────────────────────────

def test_baseline_extract_and_direction():
    from eval_harness import baseline
    r = {"l1": {"ranking": {"recall@5": 0.9, "recall@1": 0.8},
                "by_module": {"rag_retrieval": {"recall@5": 0.92}}, "n_positive_public": 38},
         "l3": {"deterministic": {"positive": {"over_refusal_rate": 0.05, "mean_keyword_coverage": 0.8}}},
         "l4": {"ingestion": {"deterministic": {"binding_jaccard_pdf": 0.70, "img_dup_factor_p95": 1.1}}}}
    m = baseline.extract_metrics(r)
    assert m["l1.ranking.recall@5"] == 0.9
    assert m["l1.by_module.rag_retrieval.recall@5"] == 0.92
    assert m["l1.n_positive_public"] == 38
    assert m["l3.over_refusal_rate"] == 0.05 and m["l4ing.jaccard.pdf"] == 0.70
    assert baseline._direction("l1.ranking.recall@5") == "higher"
    assert baseline._direction("l3.over_refusal_rate") == "lower"
    assert baseline._direction("l4ing.img_dup_p95") == "lower"


def test_baseline_compare_catches_subset_regression_under_aggregate():
    from eval_harness import baseline
    regime = {"fusion": "weighted", "eval_set_sha": "x", "rerank_enable": False, "llm_model": "q",
              "embedding_model": "e", "reranker_models": None, "threshold_version": "t"}
    base = {"regime": regime, "delta": 0.03,
            "metrics": {"l1.ranking.recall@5": 0.90, "l1.by_module.hr.recall@5": 0.95,
                        "l3.over_refusal_rate": 0.05}}
    # aggregate barely OK (0.89, within delta) but a SUBSET cratered (hr 0.95→0.80) + a rate worsened
    cur = {"meta": {"regime": regime},
           "l1": {"ranking": {"recall@5": 0.89}, "by_module": {"hr": {"recall@5": 0.80}}},
           "l3": {"deterministic": {"positive": {"over_refusal_rate": 0.20}}}}
    gate = list(baseline.compare(base, cur).values())[0]
    assert gate["pass"] is False  # local regression surfaces despite OK aggregate
    assert "hr" in gate["value"] or "over_refusal" in gate["value"]


def test_baseline_regime_mismatch_is_na_not_a_pass():
    from eval_harness import baseline
    base = {"regime": {"fusion": "weighted", "eval_set_sha": "x"}, "metrics": {"l1.ranking.recall@5": 0.9}}
    cur = {"meta": {"regime": {"fusion": "rrf", "eval_set_sha": "x"}}, "l1": {"ranking": {"recall@5": 0.5}}}
    g = list(baseline.compare(base, cur).values())[0]
    assert g["pass"] is None and g["na_reason"] == "expected_na"  # can't compare across regimes


_REG = {"fusion": "weighted", "eval_set_sha": "x", "rerank_enable": True, "llm_model": "q",
        "embedding_model": "e", "reranker_models": None, "threshold_version": "t"}


def test_baseline_advisory_delta_visible_but_not_strict_block():
    """Invariant 1: a baseline DELTA on an advisory metric (l4srv.orphan_rate) is REPORTED (visible)
    but must NOT block --strict."""
    from eval_harness import baseline as BL
    from eval_harness.report import build_gates
    from eval_harness.run_eval import _strict_failures
    base = {"regime": _REG, "delta": 0.03,
            "metrics": {"l4srv.orphan_rate": 0.5, "l1.ranking.recall@5": 0.9}}
    # orphan worsened 0.5 → 0.9 (lower-better → regressed); hard metric fine
    cur = {"meta": {"regime": _REG}, "l4": {"aggregate": {"orphan_rate": 0.9}},
           "l1": {"ranking": {"recall@5": 0.92}}}
    bg = BL.compare(base, cur)
    adv = next(v for v in bg.values() if v.get("advisory"))
    assert adv["pass"] is False and adv["advisory"] is True          # visible drift, flagged advisory
    assert "l4srv.orphan_rate" in adv["value"]                       # reported, not hidden
    hard = next(v for k, v in bg.items() if not v.get("advisory"))
    assert hard["pass"] is True                                      # hard side clean
    g = build_gates({"baseline_gates": bg})
    assert not any("advisory" in f or "orphan" in f for f in _strict_failures(g, {}))  # NOT blocking


def test_baseline_hard_metric_delta_still_blocks_strict():
    """Invariant 2: a HARD metric regression keeps fully blocking --strict (the orphan fix must not
    soften hard regressions)."""
    from eval_harness import baseline as BL
    from eval_harness.report import build_gates
    from eval_harness.run_eval import _strict_failures
    base = {"regime": _REG, "delta": 0.03,
            "metrics": {"l1.ranking.recall@5": 0.9, "l4srv.orphan_rate": 0.5}}
    cur = {"meta": {"regime": _REG}, "l1": {"ranking": {"recall@5": 0.5}},  # cratered hard metric
           "l4": {"aggregate": {"orphan_rate": 0.5}}}                       # orphan fine
    bg = BL.compare(base, cur)
    hard = next(v for v in bg.values() if not v.get("advisory"))
    assert hard["pass"] is False and hard.get("advisory") is not True
    g = build_gates({"baseline_gates": bg})
    assert any("hard metrics" in f for f in _strict_failures(g, {}))        # blocks --strict


def test_baseline_advisory_metric_kept_and_reported(tmp_path):
    """Invariant 3: advisory metrics stay in the frozen baseline (trend output) and a delta on them
    is reported in the advisory gate — value never removed/disguised."""
    import json as _j
    from eval_harness import baseline as BL
    run = {"meta": {"regime": {"fusion": "weighted"}, "timestamp": "t"},
           "l4": {"aggregate": {"orphan_rate": 0.74, "marker_validity": 1.0, "dangling_ref_rate": 0.0}}}
    p = str(tmp_path / "b.json")
    base = BL.freeze(run, p)
    assert base["metrics"]["l4srv.orphan_rate"] == 0.74                     # kept in baseline
    assert _j.load(open(p))["metrics"]["l4srv.orphan_rate"] == 0.74         # persisted for trend
    cur = {"meta": {"regime": {"fusion": "weighted"}}, "l4": {"aggregate": {"orphan_rate": 0.9}}}
    adv = next(v for v in BL.compare(base, cur).values() if v.get("advisory"))
    assert "l4srv.orphan_rate" in adv["value"]                             # visibly reported


def test_baseline_only_orphan_advisory_no_silent_reclassification():
    """Invariant 4: the advisory registry is EXACTLY {l4srv.orphan_rate}; every other extractable
    metric (recall/jaccard/dup/marker_validity/dangling/judge/refusal) stays HARD."""
    from eval_harness.baseline import ADVISORY_METRICS, _is_advisory, extract_metrics
    assert ADVISORY_METRICS == frozenset({"l4srv.orphan_rate"})
    sample = {"l1": {"ranking": {"recall@5": 0.9}, "by_source": {"xlsx": {"recall@5": 0.8}}},
              "l3": {"deterministic": {"positive": {"over_refusal_rate": 0.05, "mean_keyword_coverage": 0.8}}},
              "l4": {"ingestion": {"deterministic": {"binding_jaccard_pdf": 0.8, "img_dup_factor_p95": 1.0}},
                     "aggregate": {"marker_validity": 1.0, "dangling_ref_rate": 0.0, "orphan_rate": 0.7}},
              "judge": {"aggregate": {"positives": {"faithfulness": {"mean": 4.5}}}}}
    m = extract_metrics(sample)
    assert {p for p in m if _is_advisory(p)} == {"l4srv.orphan_rate"}       # exactly one advisory
    for hard in ("l4srv.marker_validity", "l4srv.dangling_ref_rate", "l4ing.jaccard.pdf",
                 "l4ing.img_dup_p95", "l1.ranking.recall@5", "l3.over_refusal_rate", "judge.faithfulness"):
        assert hard in m and not _is_advisory(hard)                         # stays HARD


def test_baseline_clean_passes_and_freeze_roundtrip(tmp_path):
    from eval_harness import baseline
    cur = {"meta": {"regime": {"fusion": "weighted"}, "timestamp": "t"},
           "l1": {"ranking": {"recall@5": 0.91}}}
    base = {"regime": {"fusion": "weighted"}, "delta": 0.03, "metrics": {"l1.ranking.recall@5": 0.90}}
    assert list(baseline.compare(base, cur).values())[0]["pass"] is True
    p = str(tmp_path / "baseline.json")
    baseline.freeze(cur, p)
    saved = json.load(open(p))
    assert saved["metrics"]["l1.ranking.recall@5"] == 0.91 and saved["regime"]["fusion"] == "weighted"


# ── item 2: strict pass:None taxonomy + judge-missing ──────────────────────────

def test_strict_not_executed_fails_expected_na_ok():
    from eval_harness.run_eval import _strict_failures
    gates = {"hardX": {"pass": None, "na_reason": "not_executed"},
             "L5": {"pass": None, "na_reason": "expected_na"},
             "okgate": {"pass": True}}
    f = _strict_failures(gates, {})  # no l3 → judge rule inert
    assert any("hardX" in x for x in f) and not any("L5" in x for x in f)
    assert "okgate" not in f


def test_strict_answer_correctness_requires_judge():
    from eval_harness.run_eval import _strict_failures
    f = _strict_failures({}, {"l3": {"deterministic": {}}})           # l3 ran, NOT judged
    assert any("not_judged" in x for x in f)
    f2 = _strict_failures({}, {"l3": {"deterministic": {}}, "judge": {"aggregate": {"positives": {}}}})
    assert not any("not_judged" in x for x in f2)                     # judged → ok


def test_strict_l6_defect_and_requested_unrunnable():
    from eval_harness.run_eval import _strict_failures
    assert any("NO_GO_DEFECT" in x for x in _strict_failures({}, {"l6": {"state": "NO_GO_DEFECT"}}))
    f = _strict_failures({}, {"l6": {"applicable": False}}, requested_layers={"l6"})
    assert any("l6:not_executed" in x for x in f)


def test_strict_manifest_drift_fails():
    from eval_harness.run_eval import _strict_failures
    res = {"l4": {"ingestion": {"deterministic": {"errors": ["manifest_drift::doc_a: extractor 漂移"]}}}}
    assert any("manifest_drift" in x for x in _strict_failures({}, res))


# ── item 3 + report wiring ─────────────────────────────────────────────────────

def test_report_regime_guard_gate():
    from eval_harness.report import build_gates
    bad = build_gates({"regime_guard": {"expected_fusion": "weighted", "active_fusion": "rrf",
                                         "rerank_enable": False, "match": False}})
    assert bad["fusion/calibration regime (guard)"]["pass"] is False
    good = build_gates({"regime_guard": {"expected_fusion": "weighted", "active_fusion": "weighted",
                                         "rerank_enable": False, "match": True}})
    assert good["fusion/calibration regime (guard)"]["pass"] is True


def test_regime_guard_pins_rerank_state(monkeypatch):
    """The guard fails-closed when the active rerank state != the calibration/prod regime — L2 uses
    different score bands for rerank-on vs -off, so a rerank mismatch makes L1/L2 non-representative."""
    import eval_harness.run_eval as rev
    monkeypatch.setattr(rev, "CALIBRATION_RERANK", True)  # production serves rerank ON
    g_off = rev._regime_guard({"fusion": "weighted", "rerank_enable": False})
    assert g_off["match"] is False and "rerank" in g_off["mismatch"]      # rerank OFF vs prod ON → fail
    g_on = rev._regime_guard({"fusion": "weighted", "rerank_enable": True})
    assert g_on["match"] is True and g_on["mismatch"] == []                # matched regime → pass
    g_fus = rev._regime_guard({"fusion": "rrf", "rerank_enable": True})
    assert g_fus["match"] is False and "fusion" in g_fus["mismatch"]       # fusion still caught


def test_regime_guard_rerank_mismatch_blocks_strict(monkeypatch):
    """A rerank-regime mismatch must make the report gate fail (so _strict_failures blocks)."""
    import eval_harness.run_eval as rev
    from eval_harness.report import build_gates
    monkeypatch.setattr(rev, "CALIBRATION_RERANK", True)
    rg = rev._regime_guard({"fusion": "weighted", "rerank_enable": False})
    gate = build_gates({"regime_guard": rg})["fusion/calibration regime (guard)"]
    assert gate["pass"] is False and "MISMATCH" in gate["value"]


def test_report_l4srv_shortfall_taxonomy():
    from eval_harness.report import build_gates
    r = {"l4": {"applicable": True, "aggregate": {"n_answers_with_images": 2, "marker_validity": 1.0,
                                                  "dangling_ref_rate": 0.0, "orphan_rate": 0.0}}}
    g = build_gates(r)
    assert g["<<IMG:N>> marker validity (L4-srv)"]["na_reason"] == "not_executed"   # hard → fail
    assert g["dangling 口惠图但卡片无图 (L4-srv)"]["na_reason"] == "not_executed"      # hard → fail
    assert g["orphan rate (L4-srv, trend 监控)"]["na_reason"] == "expected_na"        # soft → advisory


def test_report_l4srv_orphan_advisory_when_measured_N5():
    """Invariant 2: at N>=5 the orphan-rate gate is a soft/trend metric — a FAIL (>0.30) stays
    VISIBLE but must NOT block --strict (referenced-only render: unreferenced candidate images
    are never shown to users). The measured value stays observable as advisory telemetry."""
    from eval_harness.report import build_gates
    from eval_harness.run_eval import _strict_failures
    r = {"l4": {"applicable": True, "aggregate": {
        "n_answers_with_images": 5, "marker_validity": 1.0, "dangling_ref_rate": 0.0,
        "orphan_rate": 0.7895, "marker_distinctness": 0.8}}}
    g = build_gates(r)
    orphan = g["orphan rate (L4-srv, trend 监控)"]
    assert orphan["pass"] is False and orphan.get("advisory") is True   # measured FAIL, advisory
    assert orphan["value"] == 0.7895                                    # telemetry stays observable
    assert "na_reason" not in orphan                                    # measured, not expected_na
    assert not any("orphan" in f for f in _strict_failures(g, r))       # ... does NOT block --strict
    # invariant 4: no other L4-srv gate flips classification
    assert g["<<IMG:N>> marker validity (L4-srv)"]["pass"] is True
    assert g["<<IMG:N>> marker validity (L4-srv)"].get("advisory") is not True   # stays HARD
    assert g["dangling 口惠图但卡片无图 (L4-srv)"]["pass"] is True
    assert g["dangling 口惠图但卡片无图 (L4-srv)"].get("advisory") is not True     # stays HARD
    assert g["marker distinctness (L4-srv, advisory)"].get("advisory") is True   # stays advisory


def test_report_l4srv_orphan_expected_na_nonblocking_N_lt_5():
    """Invariant 1: N<5 orphan stays expected-N/A (unmeasured) and non-blocking."""
    from eval_harness.report import build_gates
    from eval_harness.run_eval import _strict_failures
    r = {"l4": {"applicable": True, "aggregate": {
        "n_answers_with_images": 4, "marker_validity": 1.0, "dangling_ref_rate": 0.0,
        "orphan_rate": 0.79, "marker_distinctness": 0.8}}}
    g = build_gates(r)
    orphan = g["orphan rate (L4-srv, trend 监控)"]
    assert orphan["pass"] is None and orphan["na_reason"] == "expected_na"
    assert not any("orphan" in f for f in _strict_failures(g, r))


def test_report_l4srv_marker_and_dangling_stay_hard_blockers_N5():
    """Invariant 3: at N>=5, marker-validity (<0.95) and dangling (>0.05) FAILs remain HARD
    --strict blockers (NOT advisory) — the orphan fix must not soften them."""
    from eval_harness.report import build_gates
    from eval_harness.run_eval import _strict_failures
    r = {"l4": {"applicable": True, "aggregate": {
        "n_answers_with_images": 6, "marker_validity": 0.5, "dangling_ref_rate": 0.5,
        "orphan_rate": 0.1, "marker_distinctness": 1.0}}}
    g = build_gates(r)
    mv = g["<<IMG:N>> marker validity (L4-srv)"]
    dn = g["dangling 口惠图但卡片无图 (L4-srv)"]
    assert mv["pass"] is False and mv.get("advisory") is not True
    assert dn["pass"] is False and dn.get("advisory") is not True
    fails = _strict_failures(g, r)
    assert any("marker validity" in f for f in fails)        # hard blocker
    assert any("dangling" in f for f in fails)               # hard blocker
    assert g["orphan rate (L4-srv, trend 监控)"]["pass"] is True   # 0.1<=0.30 → PASS (still advisory)


def test_l6_soft_gates_are_advisory_not_strict_block():
    """L6 soft signals (mid-sentence, routing-family) are DIAGNOSTIC — a failing one stays visible in
    the report but must NOT block --strict; the GO/NO_GO verdict + [L6-hard] gates are the real gate
    (decision 2026-06-18). A failing HARD L6 gate still blocks."""
    from eval_harness.report import build_gates
    from eval_harness.run_eval import _strict_failures
    r = {"l6": {"applicable": True, "state": "GO", "go_no_go": True, "gates": {
        "mid-sentence cut rate (B)": {"target": "<=0.05", "value": 0.35, "pass": False, "hard": False},
        "tokens in [5,2000] (B)": {"target": "0", "value": 0, "pass": True, "hard": True}}}}
    g = build_gates(r)
    soft = g["[L6-soft] mid-sentence cut rate (B)"]
    assert soft["pass"] is False and soft.get("advisory") is True       # visible FAIL, flagged advisory
    assert not any("L6-soft" in f for f in _strict_failures(g, r))      # ... but does NOT block strict
    # a failing HARD L6 gate DOES block
    r2 = {"l6": {"applicable": True, "state": "NO_GO_DEFECT", "go_no_go": False, "gates": {
        "RDS↔HA3 drift (A/D1)": {"target": "0", "value": 5, "pass": False, "hard": True}}}}
    assert any("L6-hard" in f for f in _strict_failures(build_gates(r2), r2))


# ── L2 off-topic-AUC metric (relevance != answerability) ─────────────────────────

def test_l2_auc_helper():
    from eval_harness.layers.l2_calibration import _auc
    assert _auc([0.9, 0.95, 0.8], [0.1, 0.2]) == 1.0     # perfect separation
    assert _auc([0.5], [0.5]) == 0.5                      # tie → no separation
    assert _auc([], [0.1]) is None and _auc([0.1], []) is None


def _pq(qid, kind, score, nt=None):
    return {"qid": qid, "kind": kind, "top1_score": score, "live_scorable": True,
            "publicly_retrievable": True, "gold_rank": 1 if kind == "positive" else None, "neg_type": nt}


def test_l2_offtopic_auc_gate_and_near_miss_not_penalized():
    from eval_harness.layers import l2_calibration as L2
    pos = [_pq(f"p{i}", "positive", 0.9) for i in range(8)]
    # near-miss negatives scoring HIGH (even above positives) must NOT fail L2 — relevance !=
    # answerability; and with no off-topic negatives the AUC gate is N/A.
    r = L2.run({"per_query": pos + [_pq(f"nm{i}", "negative", 0.92, "near_miss_answer_absent") for i in range(4)]},
               high=0.9, med=0.8)
    assert r["thresholds_ok"] is True
    assert r["separation_auc_offtopic"] is None and r["n_offtopic_neg"] == 0
    # off-topic negatives scoring LOW → high AUC → pass
    r2 = L2.run({"per_query": pos + [_pq(f"o{i}", "negative", 0.2, "off_topic") for i in range(6)]},
                high=0.9, med=0.8)
    assert r2["separation_auc_offtopic"] >= 0.85 and r2["thresholds_ok"] is True
    # off-topic negatives scoring HIGH (a REAL leak) → low AUC → fail
    r3 = L2.run({"per_query": pos + [_pq(f"o{i}", "negative", 0.95, "off_topic") for i in range(6)]},
                high=0.9, med=0.8)
    assert r3["separation_auc_offtopic"] < 0.85 and r3["thresholds_ok"] is False


def test_report_offtopic_auc_advisory_when_absent():
    from eval_harness.report import build_gates
    from eval_harness.run_eval import _strict_failures
    g = build_gates({"l2": {"thresholds_ok": True, "n_offtopic_neg": 0, "separation_auc_offtopic": None}})
    gate = g["off-topic discrimination AUC (L2)"]
    assert gate["pass"] is None and gate.get("advisory") is True          # advisory, not a fail
    assert not any("off-topic discrimination" in f for f in _strict_failures(g, {"l2": {}}))


def test_report_baseline_gates_merged():
    from eval_harness.report import build_gates
    g = build_gates({"baseline_gates": {"baseline regression (x)":
                                        {"target": "t", "value": "v", "pass": False}}})
    assert g["baseline regression (x)"]["pass"] is False


# ── item 4: L6 default ──────────────────────────────────────────────────────────

def test_default_layers_include_l6():
    import inspect
    import eval_harness.run_eval as rev
    assert "l0,l1,l2,l3,l4,l5,l6" in inspect.getsource(rev.main)


# ── auto-judge runner (draft): JSON extraction + panel assembly (no live claude) ──

def test_run_judge_extracts_json_array_tolerant():
    from eval_harness.run_judge import _extract_json_array
    fenced = "sure, here:\n```json\n[{\"qid\":\"a\",\"overall\":4}]\n```\n"
    assert _extract_json_array(fenced) == [{"qid": "a", "overall": 4}]
    bare = "[{\"qid\":\"b\",\"overall\":5}]"
    assert _extract_json_array(bare)[0]["qid"] == "b"
    import pytest
    with pytest.raises(ValueError):
        _extract_json_array("no json here")


def test_run_judge_assembles_panels(monkeypatch, tmp_path):
    import json as _json
    from eval_harness import run_judge
    bundle = [{"qid": "q1"}, {"qid": "q2"}]
    _json.dump(bundle, open(tmp_path / "b.json", "w"))
    # mock the claude call: echo a verdict per item
    monkeypatch.setattr(run_judge, "_judge_batch",
                        lambda rub, items, pi, idk: [{idk: it[idk], "overall": 4} for it in items])
    out = str(tmp_path / "v.json")
    run_judge.run(str(tmp_path / "b.json"), out, panels=3, rubric="answer", batch=1)
    saved = _json.load(open(out))
    assert len(saved["panels"]) == 3
    assert {v["qid"] for v in saved["panels"][0]["verdicts"]} == {"q1", "q2"}


# ── ① inter-judge agreement gate (judge.py computed mean_overall_interjudge_stdev but never gated) ──

def test_judge_interrater_agreement_gate_l3():
    from eval_harness.report import build_gates
    assert build_gates({"judge": {"aggregate": {"mean_overall_interjudge_stdev": 0.4}}})[
        "judge inter-rater agreement (L3 panel)"]["pass"] is True
    assert build_gates({"judge": {"aggregate": {"mean_overall_interjudge_stdev": 1.5}}})[
        "judge inter-rater agreement (L3 panel)"]["pass"] is False
    g = build_gates({"judge": {"aggregate": {"mean_overall_interjudge_stdev": None}}})[
        "judge inter-rater agreement (L3 panel)"]
    assert g["pass"] is None and g["na_reason"] == "expected_na"


def test_judge_high_disagreement_blocks_strict():
    from eval_harness.report import build_gates
    from eval_harness.run_eval import _strict_failures
    g = build_gates({"judge": {"aggregate": {"mean_overall_interjudge_stdev": 1.5}}})
    fails = _strict_failures(g, {"l3": {"deterministic": {}},
                                 "judge": {"aggregate": {"positives": {}}}})
    assert any("inter-rater agreement" in x for x in fails)


def test_l6_chunk_judge_interrater_gate():
    from eval_harness.report import build_gates
    g = build_gates({"l6": {"applicable": True, "state": "GO", "go_no_go": True, "gates": {},
                            "judge_chunk": {"mean_overall_interjudge_stdev": 1.5}}})
    assert g["judge inter-rater agreement (L6 chunk panel)"]["pass"] is False


def test_judge_stdev_threshold_env_override(monkeypatch):
    import importlib
    from eval_harness import report
    monkeypatch.setenv("RAG_EVAL_JUDGE_STDEV_MAX", "0.5")
    importlib.reload(report)
    try:
        assert report.build_gates({"judge": {"aggregate": {"mean_overall_interjudge_stdev": 0.8}}})[
            "judge inter-rater agreement (L3 panel)"]["pass"] is False
    finally:
        monkeypatch.delenv("RAG_EVAL_JUDGE_STDEV_MAX", raising=False)
        importlib.reload(report)


# ── ② judge calibration vs human (DRAFT: code ready, labels TODO) ──

def test_judge_calibration_compare_math():
    from eval_harness.judge_calibration import compare
    panel = {"verdicts": [
        {"qid": "q1", "faithfulness": 4, "correctness": 4, "completeness": 4, "relevance": 4, "fabricated": False},
        {"qid": "q2", "faithfulness": 2, "correctness": 2, "completeness": 2, "relevance": 2, "fabricated": True}]}
    panels = [panel, panel, panel]
    human = [{"qid": "q1", "human": {"faithfulness": 4, "correctness": 4, "completeness": 4, "relevance": 4, "fabricated": False}},
             {"qid": "q2", "human": {"faithfulness": 3, "correctness": 3, "completeness": 3, "relevance": 3, "fabricated": True}}]
    c = compare(human, panels)
    assert c["n_items"] == 2
    assert c["per_dim"]["faithfulness"]["mae"] == 0.5   # mean(|4-4|, |3-2|)
    assert c["fabrication"]["f1"] == 1.0                 # q1 TN, q2 TP


def test_judge_calibration_gate_states():
    from eval_harness.judge_calibration import calibration_gate, MIN_N
    g = calibration_gate({"n_items": 1, "per_dim": {}, "fabrication": {}})
    assert g["pass"] is None and g["na_reason"] == "not_executed"   # too few → must not silently pass
    good = {"n_items": MIN_N, "per_dim": {"faithfulness": {"mae": 0.3}, "correctness": {"mae": 0.4}},
            "fabrication": {"f1": 0.9}}
    assert calibration_gate(good)["pass"] is True
    bad = {"n_items": MIN_N, "per_dim": {"faithfulness": {"mae": 1.2}, "correctness": {"mae": 0.4}},
           "fabrication": {"f1": 0.9}}
    assert calibration_gate(bad)["pass"] is False


def test_judge_calibration_gate_wired():
    from eval_harness.report import build_gates
    from eval_harness.judge_calibration import MIN_N
    g = build_gates({"judge_calibration": {"n_items": MIN_N,
                     "per_dim": {"faithfulness": {"mae": 1.5}, "correctness": {"mae": 0.2}},
                     "fabrication": {"f1": 0.9}}})
    assert g["judge calibration vs human (L3)"]["pass"] is False


def test_judge_calibration_template_blank(tmp_path):
    import json as _j
    from eval_harness.judge_calibration import build_template, SCORE_DIMS
    bundle = [{"qid": f"q{i}", "kind": "positive", "question": "?", "answer": "a"} for i in range(10)]
    bundle.append({"qid": "neg1", "kind": "negative", "question": "?", "answer": "no"})
    out = str(tmp_path / "tmpl.json")
    t = build_template(bundle, 6, out, seed=1)
    assert len(t) == 6
    assert all(item["human"][d] is None for item in t for d in SCORE_DIMS)
    assert _j.load(open(out))
