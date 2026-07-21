# -*- coding: utf-8 -*-
"""tests/test_readiness_f11.py — 评审F11（2026-07-21）：readiness 运营库/Stream 探针 +
qa_logger 表结构漂移 ops 告警。

- /api/ready 新增 operation_db（表存在 + qa_session_log 强制列完整）与 dingtalk_stream
  （is_stream_active 真连通）两字段：**默认只报告不判死**（现网行为逐字不变），
  RAG_READY_REQUIRE_OP_SCHEMA / RAG_READY_REQUIRE_STREAM 开启才计入 critical。
- qa_logger 1054/1146 漂移在 logger.critical 之外补 send_ops_alert(critical)，
  进程内至多一次尝试、绝不外溢到答案主路径。
"""
import threading


def _wire_ready_probes(monkeypatch, fetchones, fetchalls):
    """把 /api/ready 的 RDS + operation_db 探针接到序列化假连接上（HA3 → mock skipped）。"""
    import opensearch_pipeline.retriever as rt
    from opensearch_pipeline.config import get_config

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(rt, "_get_ha3_client", lambda: "MOCK_HA3_CLIENT")

    fetchones = list(fetchones)
    fetchalls = list(fetchalls)

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
        def fetchone(self): return fetchones.pop(0)
        def fetchall(self): return fetchalls.pop(0)

    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: _Conn())


def _ncols():
    from opensearch_pipeline.qa_logger import QA_SESSION_MANDATORY_COLS
    return len(QA_SESSION_MANDATORY_COLS)


def test_ready_reports_operation_db_ok_and_stream_disabled(monkeypatch):
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    _wire_ready_probes(
        monkeypatch,
        fetchones=[(1,), (_ncols(),)],
        fetchalls=[[("qa_session_log",), ("user_feedback",)]],
    )
    r = TestClient(app).get("/api/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["operation_db"] == "ok"
    assert body["dingtalk_stream"] == "disabled"   # 默认未开 Stream


def test_ready_column_drift_reported_but_not_critical_by_default(monkeypatch):
    """缺一个强制列 → operation_db='drift'，但默认不翻 503（fail-open 哲学；现网不变）。"""
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    _wire_ready_probes(
        monkeypatch,
        fetchones=[(1,), (_ncols() - 1,)],
        fetchalls=[[("qa_session_log",), ("user_feedback",)]],
    )
    r = TestClient(app).get("/api/ready")
    assert r.status_code == 200
    assert r.json()["operation_db"] == "drift"


def test_ready_column_drift_critical_when_flag_on(monkeypatch):
    """RAG_READY_REQUIRE_OP_SCHEMA=true → drift 计入 critical → 503（随差部署翻的灰度门）。"""
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    monkeypatch.setenv("RAG_READY_REQUIRE_OP_SCHEMA", "true")
    _wire_ready_probes(
        monkeypatch,
        fetchones=[(1,), (_ncols() - 1,)],
        fetchalls=[[("qa_session_log",), ("user_feedback",)]],
    )
    r = TestClient(app).get("/api/ready")
    assert r.status_code == 503
    assert r.json()["operation_db"] == "drift"


def test_ready_missing_table_reported(monkeypatch):
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    monkeypatch.setenv("RAG_READY_REQUIRE_OP_SCHEMA", "true")
    _wire_ready_probes(
        monkeypatch,
        fetchones=[(1,)],
        fetchalls=[[("qa_session_log",)]],   # user_feedback 缺
    )
    r = TestClient(app).get("/api/ready")
    assert r.status_code == 503
    assert r.json()["operation_db"] == "missing"


def test_ready_stream_inactive_reported_and_flag_gated(monkeypatch):
    """Stream 开着但 WSS 死 → 默认只报 inactive；RAG_READY_REQUIRE_STREAM=true → 503
    （Stream 死 + HTTP 未配 = 主通道全聋，此前 readiness 恒绿）。"""
    from fastapi.testclient import TestClient
    from opensearch_pipeline import dingtalk_stream_runner as dsr
    from opensearch_pipeline.api import app
    monkeypatch.setattr(dsr, "stream_mode_enabled", lambda: True)
    monkeypatch.setattr(dsr, "is_stream_active", lambda: False)
    _wire_ready_probes(
        monkeypatch,
        fetchones=[(1,), (_ncols(),)],
        fetchalls=[[("qa_session_log",), ("user_feedback",)]],
    )
    r = TestClient(app).get("/api/ready")
    assert r.status_code == 200
    assert r.json()["dingtalk_stream"] == "inactive"

    monkeypatch.setenv("RAG_READY_REQUIRE_STREAM", "true")
    _wire_ready_probes(
        monkeypatch,
        fetchones=[(1,), (_ncols(),)],
        fetchalls=[[("qa_session_log",), ("user_feedback",)]],
    )
    r = TestClient(app).get("/api/ready")
    assert r.status_code == 503


def test_ready_simulate_includes_new_fields():
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    body = TestClient(app).get("/api/ready").json()   # RAG_SIMULATE=true 默认
    assert body["operation_db"] == "skipped" and body["dingtalk_stream"] == "skipped"


# ── qa_logger 漂移 ops 告警（at-most-once）──────────────────────────────────


def _reset_alert_state(monkeypatch):
    from opensearch_pipeline import qa_logger
    monkeypatch.setattr(qa_logger, "_SCHEMA_DRIFT_ALERTED", False)


def test_schema_drift_fires_ops_alert_once(monkeypatch):
    """1054 漂移：logger.critical 每次照记，send_ops_alert 进程内只发一枚。"""
    from opensearch_pipeline import qa_logger
    _reset_alert_state(monkeypatch)
    sent = []
    monkeypatch.setattr("opensearch_pipeline.alerting.send_ops_alert",
                        lambda *a, **k: sent.append((a, k)))

    class _Conn:
        def cursor(self):
            raise Exception(1054, "Unknown column 'content_blocks_json'")
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: _Conn())
    for i in range(2):
        qa_logger.log_qa_session(session_id=f"s{i}", message_id=f"m{i}", user_id="u1",
                                 query_text="q", answer_text="a",
                                 answer_status="SUCCESS")   # 绝不抛
    assert len(sent) == 1, "漂移告警必须 at-most-once（不刷屏）"
    assert sent[0][1].get("severity") == "critical"


def test_schema_drift_alert_concurrent_first_failures_single_send(monkeypatch):
    """并发首失（Codex r2）：检查即置位——两线程同时进来只发一枚。"""
    from opensearch_pipeline import qa_logger
    _reset_alert_state(monkeypatch)
    sent = []
    monkeypatch.setattr("opensearch_pipeline.alerting.send_ops_alert",
                        lambda *a, **k: sent.append(1))
    threads = [threading.Thread(target=qa_logger._alert_schema_drift_once,
                                args=(1146, "t")) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sent) == 1


def test_schema_drift_alert_failure_never_raises(monkeypatch):
    """告警传输失败（webhook 未配/网络断）绝不外溢——答案主路径不可触碰。"""
    from opensearch_pipeline import qa_logger
    _reset_alert_state(monkeypatch)
    monkeypatch.setattr("opensearch_pipeline.alerting.send_ops_alert",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("webhook down")))
    qa_logger._alert_schema_drift_once(1054, "x")   # 必须不 raise
    # 置位后不再尝试（失败不重试是既定语义：critical 日志每次照记兜底）
    assert qa_logger._SCHEMA_DRIFT_ALERTED is True


def test_non_drift_errors_do_not_alert(monkeypatch):
    """普通写失败（非 1054/1146）只走 logger.error，不发 ops 告警。"""
    from opensearch_pipeline import qa_logger
    _reset_alert_state(monkeypatch)
    sent = []
    monkeypatch.setattr("opensearch_pipeline.alerting.send_ops_alert",
                        lambda *a, **k: sent.append(1))

    class _Conn:
        def cursor(self):
            raise Exception(2013, "Lost connection")
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: _Conn())
    qa_logger.log_qa_session(session_id="s1", message_id="m1", user_id="u1",
                             query_text="q", answer_text="a", answer_status="SUCCESS")
    assert not sent
