# -*- coding: utf-8 -*-
"""Majors γ3(M9.3/9.4+M6.iii) agent_health——gate/裁决/exit 映射/告警契约(2026-07-21 codex 共识)。

- flag off(默认)→ skipped/exit 0，零 DB 触碰；
- Redis 探测失败 → 该维 error、_job_exit=3——绝不报 0/假绿；
- 越阈裁决(可用率/backlog/审批 SLA/stale/relay/心跳/取消/恢复八面)+ 告警
  dedup_key=agent-health；
- ops_monitor 第 8 项接线 + 分位/日界纯函数。
真库 UPSERT 日界与 dispatcher 心跳在 test_majors_gamma_m9_db.py。
"""
import pytest

from opensearch_pipeline import agent_health, ops_monitor
from opensearch_pipeline.agent_health import _percentile, _verdict, run_agent_health_check


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("RAG_AGENT_HEALTH_ENABLE", raising=False)


def test_flag_off_skipped_exit_0():
    rep = run_agent_health_check(alert=False)
    assert rep.get("skipped") is True
    assert ops_monitor._job_exit("agent_health", rep) == 0


def test_simulate_skipped(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_HEALTH_ENABLE", "true")
    rep = run_agent_health_check(alert=False)   # 测试环境 RAG_SIMULATE=true
    assert rep.get("skipped") is True and rep.get("reason") == "simulate"


def test_ops_monitor_wiring():
    assert "agent_health" in ops_monitor._JOBS
    out = ops_monitor.run_all(alert=False, only=["agent_health"])
    assert out["agent_health"].get("skipped") is True    # flag off 默认


def test_percentile_nearest_rank():
    assert _percentile([], 0.95) is None
    assert _percentile([100.0], 0.95) == 100
    vals = [float(i) for i in range(1, 101)]
    assert _percentile(vals, 0.5) == 51          # 最近秩(round(q*(n-1))+1 序位)
    assert _percentile(vals, 0.95) == 95


def test_day_predicate_interval_form():
    sql, params = agent_health._day_predicate("ended_at", "2026-07-21")
    assert "ended_at >= %s AND ended_at < %s" in sql and len(params) == 2
    # 北京 00:00 = 太平洋前一日 08:00/09:00（随 DST）——区间起点必是前一日
    assert params[0].startswith("2026-07-20 ")


class TestVerdict:
    def _dims(self, **kw):
        base = {"runs_total": 100, "success_rate": 0.99, "dispatch_backlog": 0,
                "approval_oldest_age_s": 60, "stale_running": 0, "relay_miss_rate": 0.0,
                "worker_heartbeat_age_s": 30, "_worker_hb_expected": True,
                "cancellation_latency_p95_s": 5, "recovery_convergence_p95_s": 30}
        base.update(kw)
        return base

    def test_all_green(self):
        verdict, breaches = _verdict(self._dims())
        assert verdict == "ok" and breaches == []

    def test_availability_breach(self):
        verdict, breaches = _verdict(self._dims(success_rate=0.5))
        assert verdict == "breach" and any("availability" in b for b in breaches)

    def test_backlog_and_sla_breach(self, monkeypatch):
        monkeypatch.setenv("RAG_AGENT_BACKLOG_MAX", "10")
        monkeypatch.setenv("RAG_AGENT_APPROVAL_SLA_HOURS", "1")
        verdict, breaches = _verdict(self._dims(dispatch_backlog=11,
                                                approval_oldest_age_s=7200))
        assert verdict == "breach" and len(breaches) == 2

    def test_hb_missing_row_breach_only_when_expected(self):
        _v, b1 = _verdict(self._dims(worker_heartbeat_age_s=None))
        assert any("worker_heartbeat 行缺失" in x for x in b1)
        _v2, b2 = _verdict(self._dims(worker_heartbeat_age_s=None,
                                      _worker_hb_expected=False))
        assert b2 == []                       # dispatch off → N/A 不裁决

    def test_na_dims_do_not_breach(self):
        verdict, breaches = _verdict(self._dims(
            relay_miss_rate=None, cancellation_latency_p95_s=None,
            recovery_convergence_p95_s=None, dispatch_backlog=None))
        assert verdict == "ok" and breaches == []


def test_relay_probe_failure_maps_exit_3(monkeypatch):
    """Redis 探测失败 → 该维 error → report['error'] → _job_exit=3（绝不报 0）。"""
    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(agent_health, "_op_db", lambda: "db")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
    monkeypatch.setattr(agent_health, "_collect_day_facts",
                        lambda conn, db, day: {"runs_total": 0})
    monkeypatch.setattr(agent_health, "_dim_approvals", lambda conn, db: {})
    monkeypatch.setattr(agent_health, "_dim_stale_running", lambda conn, db: {})
    monkeypatch.setattr(agent_health, "_dim_uncertain", lambda conn, db: {})
    monkeypatch.setattr(agent_health, "_dim_dispatch", lambda conn, db, day: {})
    monkeypatch.setattr(
        agent_health, "_dim_relay",
        lambda conn, db: {"relay_miss_rate": None, "error": "relay_probe_failed: down"})
    monkeypatch.setattr(agent_health, "_upsert", lambda conn, db, day, dims: None)
    rep = agent_health._execute("2026-07-21", alert=False)
    assert rep.get("error") and rep.get("ok") is False
    assert ops_monitor._job_exit("agent_health", rep) == 3


def test_breach_fires_alert_with_dedup_key(monkeypatch):
    class _Conn:
        def close(self):
            pass

    calls = []
    import opensearch_pipeline.alerting as alerting
    monkeypatch.setattr(alerting, "send_ops_alert",
                        lambda title, text, **kw: calls.append((title, kw)) or True)
    monkeypatch.setattr(agent_health, "_op_db", lambda: "db")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
    monkeypatch.setattr(agent_health, "_collect_day_facts",
                        lambda conn, db, day: {"runs_total": 10, "success_rate": 0.5})
    monkeypatch.setattr(agent_health, "_dim_approvals", lambda conn, db: {})
    monkeypatch.setattr(agent_health, "_dim_stale_running",
                        lambda conn, db: {"stale_running": 3})
    monkeypatch.setattr(agent_health, "_dim_uncertain", lambda conn, db: {})
    monkeypatch.setattr(agent_health, "_dim_dispatch", lambda conn, db, day: {})
    monkeypatch.setattr(agent_health, "_dim_relay", lambda conn, db: {"relay_miss_rate": None})
    monkeypatch.setattr(agent_health, "_upsert", lambda conn, db, day, dims: None)
    rep = agent_health._execute("2026-07-21", alert=True)
    assert rep["ok"] is False and len(rep["breaches"]) == 2
    assert ops_monitor._job_exit("agent_health", rep) == 2
    assert calls and calls[0][1]["dedup_key"] == "agent-health"
    # alert=False 不发
    calls.clear()
    agent_health._execute("2026-07-21", alert=False)
    assert calls == []


def test_upsert_receives_verdict_and_report_shape(monkeypatch):
    class _Conn:
        def close(self):
            pass

    seen = {}
    monkeypatch.setattr(agent_health, "_op_db", lambda: "db")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
    monkeypatch.setattr(agent_health, "_collect_day_facts",
                        lambda conn, db, day: {"runs_total": 5, "runs_succeeded": 5,
                                               "success_rate": 1.0})
    monkeypatch.setattr(agent_health, "_dim_approvals",
                        lambda conn, db: {"approval_pending": 0})
    monkeypatch.setattr(agent_health, "_dim_stale_running",
                        lambda conn, db: {"stale_running": 0})
    monkeypatch.setattr(agent_health, "_dim_uncertain",
                        lambda conn, db: {"uncertain_invocations": 0})
    monkeypatch.setattr(agent_health, "_dim_dispatch", lambda conn, db, day: {})
    monkeypatch.setattr(agent_health, "_dim_relay", lambda conn, db: {"relay_miss_rate": None})
    monkeypatch.setattr(agent_health, "_upsert",
                        lambda conn, db, day, dims: seen.update(day=day, dims=dict(dims)))
    rep = agent_health._execute("2026-07-21", alert=False)
    assert rep["ok"] is True and seen["day"] == "2026-07-21"
    assert seen["dims"]["slo_verdict"] == "ok"
    assert not any(k.startswith("_") for k in rep)      # 内部键不外泄
