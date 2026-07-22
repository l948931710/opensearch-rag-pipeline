# -*- coding: utf-8 -*-
"""tests/test_ops_monitor.py — Phase-3 single-entry health-job runner."""
from opensearch_pipeline import ops_monitor


def test_run_all_sequences_selected_jobs(monkeypatch):
    calls = []
    import opensearch_pipeline.reconcile as rec
    import opensearch_pipeline.qa_rollup as qr
    monkeypatch.setattr(rec, "run_parity_check", lambda **k: calls.append("ha3") or {"ok": True})
    monkeypatch.setattr(rec, "run_oss_parity_check", lambda **k: calls.append("oss") or {"ok": True})
    monkeypatch.setattr(rec, "run_raw_parity_check", lambda **k: calls.append("raw") or {"ok": True})
    monkeypatch.setattr(qr, "run_rollup", lambda **k: calls.append("rollup") or {"ok": True, "slo_ok": 1})
    out = ops_monitor.run_all(alert=False)
    # P2-34/P2-15 起作业集含 queue_aging/ingest_funnel（模拟态 no-op skipped）
    assert set(out) == {"reconcile_ha3", "reconcile_oss", "reconcile_raw", "qa_rollup",
                        "queue_aging", "ingest_funnel"}
    assert calls == ["ha3", "oss", "raw", "rollup"]


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


def test_job_exit_fetch_verdict_contract():
    """终局裁决（2026-07-21）与退出码的契约：纯枚举盲区报告 ok=True → 绿灯 0；
    fetch 证实真丢失 / 无法定性 → ok=False → 2；扫描不完整恒 3（先于 ok 判定）。"""
    blind = {"ok": True, "complete": True, "verdict_basis": "fetch",
             "counts": {"rds_active_missing": 113, "query_invisible": 113,
                        "missing_confirmed": 0, "missing_unclassified": 0,
                        "vanished_at_risk": 0}}
    assert ops_monitor._job_exit("reconcile_ha3", blind) == 0
    confirmed = {"ok": False, "complete": True, "verdict_basis": "fetch",
                 "counts": {"missing_confirmed": 1}}
    assert ops_monitor._job_exit("reconcile_ha3", confirmed) == 2
    fetch_down = {"ok": False, "complete": True, "verdict_basis": "query_only",
                  "counts": {"rds_active_missing": 5}}
    assert ops_monitor._job_exit("reconcile_ha3", fetch_down) == 2
    truncated = {"ok": True, "complete": False, "verdict_basis": "fetch", "counts": {}}
    assert ops_monitor._job_exit("reconcile_ha3", truncated) == 3


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
