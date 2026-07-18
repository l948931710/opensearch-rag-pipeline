# -*- coding: utf-8 -*-
"""queue_monitor（盲区审计 P2-34/P2-15/P2-14）：人工队列老化 + 摄取漏斗 + 心跳。

全部 fail-open：模拟模式 no-op；单探针失败诚实记 error 不拖垮其余；心跳表未建返回 False/None。
"""
import pytest

from opensearch_pipeline import queue_monitor as qm


class _Cur:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = sql

    def fetchone(self):
        for marker, row in self.rows_by_marker.items():
            if marker in self._last:
                if isinstance(row, Exception):
                    raise row
                return row
        return (0, None)


class _Conn:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker

    def cursor(self):
        return _Cur(self.rows_by_marker)

    def close(self):
        pass

    def commit(self):
        pass


@pytest.fixture
def real_mode(monkeypatch):
    """让 simulate 门放行（探针走桩连接，不碰真库）。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)


def _wire(monkeypatch, rows_by_marker):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _Conn(rows_by_marker))


def test_simulate_is_noop():
    assert qm.run_queue_aging_check().get("skipped") == "simulate"
    assert qm.run_ingest_funnel_check().get("skipped") == "simulate"
    assert qm.write_heartbeat() is False
    assert qm.read_heartbeat_age_hours() is None


def test_queue_aging_breaches_and_alerts(real_mode, monkeypatch):
    """最老 PENDING 超 SLA / 积压超阈 → breach + 一条 warning 告警；干净队列不告警。"""
    alerts = []
    monkeypatch.setattr("opensearch_pipeline.alerting.send_ops_alert",
                        lambda *a, **k: alerts.append((a, k)) or True)
    _wire(monkeypatch, {
        "review_task": (3, 12),            # 12 天 > SLA 7
        "user_feedback": (99, 2),          # 99 > backlog 50
    })
    rep = qm.run_queue_aging_check(alert=True)
    assert rep["ok"] is False
    kinds = {(b["queue"], b["kind"]) for b in rep["breaches"]}
    assert kinds == {("review_task", "sla_age"), ("user_feedback_unhandled", "backlog")}
    assert rep["queues"]["review_task"] == {"backlog": 3, "oldest_days": 12}
    assert len(alerts) == 1 and alerts[0][1]["severity"] == "warning"


def test_queue_aging_single_probe_failure_isolated(real_mode, monkeypatch):
    """单队列查询炸 → 该队列记 error，其余照常，无告警级 breach 则 ok。"""
    _wire(monkeypatch, {
        "review_task": RuntimeError("table missing"),
        "user_feedback": (0, None),
    })
    rep = qm.run_queue_aging_check(alert=False)
    assert rep["ok"] is True
    assert "error" in rep["queues"]["review_task"]


def test_ingest_funnel_flags_stuck_and_unindexed(real_mode, monkeypatch):
    """卡 LOADING/PROCESSING 与注册超龄未入索引 → problems + warning 告警。"""
    alerts = []
    monkeypatch.setattr("opensearch_pipeline.alerting.send_ops_alert",
                        lambda *a, **k: alerts.append(k) or True)
    _wire(monkeypatch, {
        "LOADING": (2, 9),                 # stuck_processing
        "index_status IS NULL": (4, 80),   # registered_not_indexed
        "NEEDS_REVIEW": (0, None),
    })
    rep = qm.run_ingest_funnel_check(alert=True)
    assert rep["ok"] is False
    assert set(rep["problems"]) == {"stuck_processing", "registered_not_indexed"}
    assert rep["buckets"]["stuck_processing"] == {"count": 2, "worst_hours": 9}
    assert len(alerts) == 1


def test_heartbeat_fail_open(real_mode, monkeypatch):
    """rag_runtime_contract 未建（schema/018 未 apply）：写 False / 读 None，不外抛。"""
    def _boom(*a, **k):
        raise RuntimeError("(1146, table doesn't exist)")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    assert qm.write_heartbeat() is False
    assert qm.read_heartbeat_age_hours() is None


def test_ops_monitor_includes_new_jobs(monkeypatch):
    """ops_monitor 作业集纳入 queue_aging/ingest_funnel，且每次运行刷心跳（fail-open）。"""
    from opensearch_pipeline import ops_monitor
    beats = []
    monkeypatch.setattr("opensearch_pipeline.queue_monitor.write_heartbeat",
                        lambda: beats.append(1) or True)
    reports = ops_monitor.run_all(alert=False, only=["queue_aging", "ingest_funnel"])
    assert set(reports) == {"queue_aging", "ingest_funnel"}
    # 模拟态下两作业 no-op（skipped），但心跳挂点仍被调用
    assert all(r.get("skipped") == "simulate" for r in reports.values())
    assert beats == [1]
