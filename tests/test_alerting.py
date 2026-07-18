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
    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")
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

    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")
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

    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")
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


# ── 批次7（ultra alerting:73）：HTTP 200 里的 errcode 失败不再静默 ──────────────


def _mk_resp(body: bytes, status: int = 200):
    class _R:
        def __init__(self):
            self.status = status

        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _R()


def _wire(monkeypatch, body: bytes):
    import urllib.request
    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")
    monkeypatch.delenv("RAG_OPS_ALERT_SECRET", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _mk_resp(body))
    from opensearch_pipeline import alerting
    alerting._LAST_SENT.clear()
    return alerting


def test_errcode_failure_in_200_body_returns_false(monkeypatch, caplog):
    """钉钉在 HTTP 200 里用 errcode 报失败（签名不符 310000/机器人限流 130101）——
    此前只看 HTTP 状态：告警通道死透而 send_ops_alert 还返回 True 零日志。"""
    import logging
    alerting = _wire(monkeypatch, b'{"errcode": 310000, "errmsg": "sign not match"}')
    with caplog.at_level(logging.ERROR):
        assert alerting.send_ops_alert("errcode-test", "x", dedup_key="ec1") is False
    assert any("errcode=310000" in r.message and "sign not match" in str(r.message)
               or "310000" in r.getMessage() for r in caplog.records)


def test_errcode_zero_returns_true(monkeypatch):
    alerting = _wire(monkeypatch, b'{"errcode": 0, "errmsg": "ok"}')
    assert alerting.send_ops_alert("ok-test", "x", dedup_key="ec2") is True


def test_unparseable_200_body_stays_true(monkeypatch):
    """body 非 JSON（代理页/空响应）→ 保守按已送达（旧行为，不误报失败）。"""
    alerting = _wire(monkeypatch, b"<html>proxy</html>")
    assert alerting.send_ops_alert("raw-test", "x", dedup_key="ec3") is True


# ── B5（生产级外审 2026-07-17 P2-08）：webhook allowlist（SSRF 防线）──────────


def _assert_rejected(monkeypatch, caplog, url):
    import logging
    import urllib.request
    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", url)
    monkeypatch.delenv("RAG_OPS_ALERT_SECRET", raising=False)
    monkeypatch.delenv("RAG_OPS_ALERT_WEBHOOK_ALLOW", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("SSRF: 不得外呼")))
    from opensearch_pipeline import alerting
    alerting._LAST_SENT.clear()
    with caplog.at_level(logging.ERROR, logger="opensearch_pipeline.alerting"):
        ok = alerting.send_ops_alert("t", "x", dedup_key=f"allow-{url}")
    assert ok is False
    assert any("不在允许域" in r.getMessage() for r in caplog.records)


def test_webhook_file_scheme_rejected(monkeypatch, caplog):
    _assert_rejected(monkeypatch, caplog, "file:///etc/passwd")


def test_webhook_plain_http_rejected(monkeypatch, caplog):
    _assert_rejected(monkeypatch, caplog, "http://oapi.dingtalk.com/robot/send?access_token=x")


def test_webhook_internal_host_rejected(monkeypatch, caplog):
    _assert_rejected(monkeypatch, caplog, "https://100.100.100.200/latest/meta-data/")


def test_webhook_lookalike_host_rejected(monkeypatch, caplog):
    _assert_rejected(monkeypatch, caplog, "https://evildingtalk.com/robot/send")


def test_webhook_official_domain_allowed(monkeypatch):
    import urllib.request

    sent = []

    class _R:
        status = 200

        def read(self):
            return b'{"errcode": 0}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK",
                       "https://oapi.dingtalk.com/robot/send?access_token=x")
    monkeypatch.delenv("RAG_OPS_ALERT_SECRET", raising=False)
    monkeypatch.delenv("RAG_OPS_ALERT_WEBHOOK_ALLOW", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (sent.append(1), _R())[1])
    from opensearch_pipeline import alerting
    alerting._LAST_SENT.clear()
    assert alerting.send_ops_alert("t", "x", dedup_key="allow-official") is True
    assert sent


def test_webhook_extra_allow_env(monkeypatch):
    import urllib.request

    sent = []

    class _R:
        status = 200

        def read(self):
            return b'{"errcode": 0}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://alerts.fuling.internal/hook")
    monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK_ALLOW", "alerts.fuling.internal")
    monkeypatch.delenv("RAG_OPS_ALERT_SECRET", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (sent.append(1), _R())[1])
    from opensearch_pipeline import alerting
    alerting._LAST_SENT.clear()
    assert alerting.send_ops_alert("t", "x", dedup_key="allow-extra") is True
    assert sent
