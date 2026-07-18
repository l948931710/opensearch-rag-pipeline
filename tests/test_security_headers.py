# -*- coding: utf-8 -*-
"""B5（生产级外审 2026-07-17 P2-07）：统一安全响应头。

设计要点回归：frame 控制走「强制 CSP 只含 frame-ancestors」（钉钉 PC 工作台内嵌
console，X-Frame-Options 表达不了 allowlist；frame-ancestors 在 Report-Only 头会被
浏览器忽略 → 必须拆两头）；其余策略 Report-Only 观察；端点已设同名头不覆盖。
"""
from fastapi.testclient import TestClient

from opensearch_pipeline import api


def _get(monkeypatch, **env):
    for k in ("RAG_SECURITY_HEADERS", "RAG_FRAME_ANCESTORS_EXTRA"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(api.app).get("/api/version")


def test_headers_present_with_dingtalk_frame_ancestors(monkeypatch):
    r = _get(monkeypatch)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert "camera=()" in r.headers["permissions-policy"]
    csp = r.headers["content-security-policy"]
    assert csp.startswith("frame-ancestors")
    assert "'self'" in csp
    assert "https://*.dingtalk.com" in csp and "https://*.dingtalkapps.com" in csp
    assert "default-src" not in csp                      # 强制头只含 frame-ancestors
    ro = r.headers["content-security-policy-report-only"]
    assert "default-src 'self'" in ro
    assert "frame-ancestors" not in ro                   # RO 头里会被浏览器忽略，不放


def test_frame_ancestors_extra_env(monkeypatch):
    r = _get(monkeypatch, RAG_FRAME_ANCESTORS_EXTRA="https://portal.example.com")
    assert "https://portal.example.com" in r.headers["content-security-policy"]


def test_kill_switch_disables_all(monkeypatch):
    r = _get(monkeypatch, RAG_SECURITY_HEADERS="false")
    assert "content-security-policy" not in r.headers
    assert "x-content-type-options" not in r.headers
    assert "permissions-policy" not in r.headers


def test_headers_attach_on_error_responses_too(monkeypatch):
    for k in ("RAG_SECURITY_HEADERS", "RAG_FRAME_ANCESTORS_EXTRA"):
        monkeypatch.delenv(k, raising=False)
    r = TestClient(api.app).get("/api/definitely-not-a-route-404")
    assert r.status_code == 404
    assert r.headers.get("x-content-type-options") == "nosniff"
