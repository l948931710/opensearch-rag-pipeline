# -*- coding: utf-8 -*-
"""tests/test_observability_ci.py — Phase-2 OBS-1 (/api/ready) + EVAL-1 (run_eval --strict)."""
import inspect


# ── OBS-1: /api/ready deep readiness probe ──

def test_api_ready_simulate_returns_ok_skipped():
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    r = TestClient(app).get("/api/ready")  # RAG_SIMULATE=true → simulate branch
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["rds"] == "skipped" and body["mode"] == "simulate"


def test_api_ready_503_on_rds_failure(monkeypatch):
    from fastapi.testclient import TestClient
    import opensearch_pipeline.pipeline_nodes as pn
    import opensearch_pipeline.retriever as rt
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.api import app

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)  # force the live-probe path
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: (_ for _ in ()).throw(RuntimeError("rds down")))
    monkeypatch.setattr(rt, "_get_ha3_client", lambda: "MOCK_HA3_CLIENT")  # HA3 skipped → only RDS fails

    r = TestClient(app).get("/api/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded" and "error" in body["rds"]


def test_api_health_still_dumb_liveness():
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ── EVAL-1: run_eval --strict (advisory → blocking) ──

def test_eval_strict_failures_detects_gate_fail():
    from eval_harness.run_eval import _strict_failures
    gates = {"recall@5": {"pass": True, "value": 0.9}, "source_attr": {"pass": False, "value": 0.7}}
    assert _strict_failures(gates, {}) == ["source_attr"]
    assert _strict_failures({"a": {"pass": True}}, {}) == []
    assert _strict_failures({"a": {"pass": None}}, {}) == []  # N/A is not a hard fail


def test_eval_strict_failures_l6_defect_vs_incomplete():
    from eval_harness.run_eval import _strict_failures
    assert "l6:NO_GO_DEFECT" in _strict_failures({}, {"l6": {"state": "NO_GO_DEFECT"}})
    assert _strict_failures({}, {"l6": {"state": "NO_GO_INCOMPLETE_EVIDENCE"}}) == []  # advisory
    assert _strict_failures({}, {"l6": {"state": "GO"}}) == []


def test_eval_strict_enabled_via_arg_or_env(monkeypatch):
    from eval_harness.run_eval import _strict_enabled

    class _A:
        strict = False

    monkeypatch.delenv("RAG_EVAL_STRICT", raising=False)
    assert _strict_enabled(_A()) is False
    monkeypatch.setenv("RAG_EVAL_STRICT", "true")
    assert _strict_enabled(_A()) is True
    a = _A()
    a.strict = True
    monkeypatch.delenv("RAG_EVAL_STRICT", raising=False)
    assert _strict_enabled(a) is True


def test_run_eval_wires_strict():
    from eval_harness import run_eval
    assert '"--strict"' in inspect.getsource(run_eval.main)
    assert "_enforce_strict(gates, results" in inspect.getsource(run_eval.phase_run)
    assert "_enforce_strict(gates, results" in inspect.getsource(run_eval.phase_merge)


# ── EVAL-3: GitHub Actions CI gate ──

def test_ci_workflow_present_and_runs_tests_in_simulate():
    from pathlib import Path
    ci = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
    assert ci.exists(), "EVAL-3: CI workflow must exist"
    txt = ci.read_text(encoding="utf-8")
    assert "pull_request" in txt, "CI must run on PRs"
    assert "pytest tests/" in txt, "CI must run the test suite"
    assert "RAG_SIMULATE" in txt, "CI must run in simulate mode (no cloud creds)"
