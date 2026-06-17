# -*- coding: utf-8 -*-
"""tests/test_ops_monitor.py — Phase-3 single-entry health-job runner."""
from opensearch_pipeline import ops_monitor


def test_run_all_sequences_selected_jobs(monkeypatch):
    calls = []
    import opensearch_pipeline.reconcile as rec
    import opensearch_pipeline.qa_rollup as qr
    monkeypatch.setattr(rec, "run_parity_check", lambda **k: calls.append("ha3") or {"ok": True})
    monkeypatch.setattr(rec, "run_oss_parity_check", lambda **k: calls.append("oss") or {"ok": True})
    monkeypatch.setattr(qr, "run_rollup", lambda **k: calls.append("rollup") or {"ok": True, "slo_ok": 1})
    out = ops_monitor.run_all(alert=False)
    assert set(out) == {"reconcile_ha3", "reconcile_oss", "qa_rollup"}
    assert calls == ["ha3", "oss", "rollup"]


def test_run_all_only_subset(monkeypatch):
    import opensearch_pipeline.reconcile as rec
    monkeypatch.setattr(rec, "run_parity_check", lambda **k: {"ok": True})
    out = ops_monitor.run_all(only=["reconcile_ha3"])
    assert set(out) == {"reconcile_ha3"}


def test_job_exit_codes():
    assert ops_monitor._job_exit("reconcile_ha3", {"skipped": "simulate"}) == 0
    assert ops_monitor._job_exit("reconcile_ha3", {"ok": True, "complete": True}) == 0
    assert ops_monitor._job_exit("reconcile_ha3", {"ok": False, "complete": True}) == 2
    assert ops_monitor._job_exit("reconcile_ha3", {"error": "x"}) == 3
    assert ops_monitor._job_exit("reconcile_ha3", {"complete": False}) == 3
    assert ops_monitor._job_exit("qa_rollup", {"ok": True, "slo_ok": 1}) == 0
    assert ops_monitor._job_exit("qa_rollup", {"ok": True, "slo_ok": 0}) == 2


def test_main_worst_exit_code(monkeypatch):
    monkeypatch.setattr(ops_monitor, "run_all", lambda **k: {
        "reconcile_ha3": {"ok": True, "complete": True},          # 0
        "reconcile_oss": {"ok": False, "complete": True},         # 2
        "qa_rollup": {"ok": True, "slo_ok": 1},                   # 0
    })
    assert ops_monitor.main(["--no-alert"]) == 2


def test_main_simulate_all_skipped(monkeypatch):
    # under simulate each real sub-job no-ops → exit 0
    monkeypatch.setattr(ops_monitor, "run_all", lambda **k: {
        "reconcile_ha3": {"skipped": "simulate"},
        "qa_rollup": {"skipped": "simulate"},
    })
    assert ops_monitor.main([]) == 0
