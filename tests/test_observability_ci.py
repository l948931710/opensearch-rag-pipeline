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


def test_ci_workflow_has_frontend_test_build_job():
    """前端门回归：CI 必须有 console-app 的 vitest + build 阻塞 job（此前 135 测试从未在 CI 跑）。"""
    from pathlib import Path
    txt = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "setup-node" in txt, "CI must set up Node for the frontend job"
    assert "working-directory: console-app" in txt, "frontend job must run in console-app"
    assert "npm run test" in txt and "npm run build" in txt, "frontend job must run vitest + build"


# ── EVAL-2: GT-manifest preflight wired into the L4 run-path ──

def test_eval2_preflight_missing_manifest_is_not_drift(tmp_path):
    """Graceful skip when no manifest is authored yet — NOT a drift."""
    from eval_harness.binding.gt_loader import GtDoc
    from eval_harness.binding.ingestion_binding import _preflight_manifest
    doc = GtDoc(label="d", fmt="pdf", doc_sha256="s", extractor_version="x",
                manifest_path=None, degraded=False, gt_chunks=[])
    assert _preflight_manifest(doc, "d", None) is None
    assert _preflight_manifest(doc, "d", str(tmp_path)) is None  # dir exists, file doesn't


def test_eval2_preflight_drift_returns_reason(tmp_path, monkeypatch):
    """A drifted manifest (extractor_version mismatch) returns a non-empty reason string."""
    import json
    from eval_harness.binding.gt_loader import GtDoc
    from eval_harness.binding import ingestion_binding

    mp = tmp_path / "d_images.json"
    json.dump({"_meta": {"extractor_version": "v2", "doc_sha256": "abc"}, "images": []},
              open(mp, "w"))
    doc = GtDoc(label="d", fmt="pdf", doc_sha256="abc", extractor_version="v1",
                manifest_path=str(mp), degraded=False, gt_chunks=[])
    reason = ingestion_binding._preflight_manifest(doc, "d", None)
    assert reason and "extractor_version" in reason


def test_eval2_preflight_ok_returns_none(tmp_path):
    """A clean manifest returns None (no drift)."""
    import json
    from eval_harness.binding.gt_loader import GtDoc
    from eval_harness.binding import ingestion_binding

    mp = tmp_path / "d_images.json"
    json.dump({"_meta": {"extractor_version": "v1", "doc_sha256": "abc"}, "images": []},
              open(mp, "w"))
    doc = GtDoc(label="d", fmt="pdf", doc_sha256="abc", extractor_version="v1",
                manifest_path=str(mp), degraded=False, gt_chunks=[])
    assert ingestion_binding._preflight_manifest(doc, "d", None) is None


def test_eval2_strict_fails_on_manifest_drift_error():
    """_strict_failures must surface manifest_drift errors as a hard gate failure."""
    from eval_harness.run_eval import _strict_failures
    results = {"l4": {"ingestion": {"deterministic": {"errors": [
        "manifest_drift::doc_a: extractor_version 漂移",
        "other_error",
    ]}}}}
    fails = _strict_failures({}, results)
    assert any("manifest_drift" in f for f in fails)
    # no drift → no failure
    assert _strict_failures({}, {"l4": {"ingestion": {"deterministic": {"errors": []}}}}) == []
