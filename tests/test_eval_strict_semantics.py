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


def test_report_l4srv_shortfall_taxonomy():
    from eval_harness.report import build_gates
    r = {"l4": {"applicable": True, "aggregate": {"n_answers_with_images": 2, "marker_validity": 1.0,
                                                  "dangling_ref_rate": 0.0, "orphan_rate": 0.0}}}
    g = build_gates(r)
    assert g["<<IMG:N>> marker validity (L4-srv)"]["na_reason"] == "not_executed"   # hard → fail
    assert g["dangling 口惠图但卡片无图 (L4-srv)"]["na_reason"] == "not_executed"      # hard → fail
    assert g["orphan rate (L4-srv, trend 监控)"]["na_reason"] == "expected_na"        # soft → advisory


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
