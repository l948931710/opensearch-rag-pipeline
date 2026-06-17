# -*- coding: utf-8 -*-
"""tests/test_alerting.py — Phase-2 OBS-4: DingTalk ops-alert sink.

Invariants: fail-open (never raises), config-gated no-op when webhook unset, signs when secret set,
rate-limited per dedup_key, wired at the orchestrator non-zero exit.
"""
import inspect


def test_send_noop_when_webhook_unset(monkeypatch, caplog):
    import logging
    monkeypatch.delenv("RAG_OPS_ALERT_WEBHOOK", raising=False)
    monkeypatch.delenv("RAG_OPS_ALERT_SECRET", raising=False)
    # ensure no HTTP attempted
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not HTTP")))
    from opensearch_pipeline.alerting import send_ops_alert
    with caplog.at_level(logging.INFO, logger="opensearch_pipeline.alerting"):
        ok = send_ops_alert("title", "text")
    assert ok is False
    assert any("ops-alert no-op" in r.getMessage() for r in caplog.records)


def test_send_fail_open_on_http_error(monkeypatch, caplog):
    import logging
    import urllib.request
    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://example.invalid/robot/send?access_token=x")
    monkeypatch.delenv("RAG_OPS_ALERT_SECRET", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("net down")))
    # reset dedup window so this test isn't masked by previous tests
    from opensearch_pipeline import alerting
    alerting._LAST_SENT.clear()
    with caplog.at_level(logging.WARNING, logger="opensearch_pipeline.alerting"):
        ok = alerting.send_ops_alert("net-test", "text", dedup_key="net-test-key")
    assert ok is False
    assert any("ops-alert send failed" in r.getMessage() for r in caplog.records)


def test_send_dedupes_within_window(monkeypatch):
    import urllib.request

    calls = []

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        calls.append(req.full_url if hasattr(req, "full_url") else str(req))
        return _R()

    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://example.invalid/robot/send?access_token=x")
    monkeypatch.delenv("RAG_OPS_ALERT_SECRET", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    from opensearch_pipeline import alerting
    alerting._LAST_SENT.clear()

    assert alerting.send_ops_alert("dup-test", "x", dedup_key="dup-test-key") is True
    assert alerting.send_ops_alert("dup-test", "x", dedup_key="dup-test-key") is False  # dedup
    assert len(calls) == 1


def test_signs_when_secret_set(monkeypatch):
    import urllib.request
    captured = {}

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url if hasattr(req, "full_url") else str(req)
        return _R()

    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://example.invalid/robot/send?access_token=x")
    monkeypatch.setenv("RAG_OPS_ALERT_SECRET", "shhh")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    from opensearch_pipeline import alerting
    alerting._LAST_SENT.clear()

    assert alerting.send_ops_alert("sig-test", "x", dedup_key="sig-test-key") is True
    assert "timestamp=" in captured["url"] and "sign=" in captured["url"]


def test_orchestrator_main_wires_ops_alert():
    """Non-zero exit must attempt an ops alert (fail-open if webhook unset)."""
    from opensearch_pipeline.dataworks_orchestrator import main
    src = inspect.getsource(main)
    assert "from opensearch_pipeline.alerting import send_ops_alert" in src
    assert 'severity="critical"' in src and "dedup_key=" in src
