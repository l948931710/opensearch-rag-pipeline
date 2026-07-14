# -*- coding: utf-8 -*-
"""HttpsRedirectMiddleware（http_hardening.py）单测。

覆盖安全边界：只认显式 X-Forwarded-Proto；头缺失一律放行（直连 IP 的小程序/钉钉回调
不受影响，且杜绝 CLB 未注头时的重定向环）；仅 Host 白名单命中才动作。
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from opensearch_pipeline.http_hardening import HttpsRedirectMiddleware

HOST = "rag.fulingplastics.com.cn"


def _make_client(hosts=(HOST,)) -> TestClient:
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.post("/api/ping")
    def ping_post():
        return {"ok": True}

    app.add_middleware(HttpsRedirectMiddleware, hosts=hosts)
    # base_url 的 host 决定 TestClient 发出的 Host 头；scheme 对 ASGI scope 无影响
    return TestClient(app, base_url=f"http://{HOST}", follow_redirects=False)


def test_http_get_redirects_301_preserving_path_and_query():
    client = _make_client()
    resp = client.get("/api/ping?a=1&b=%E4%B8%AD", headers={"X-Forwarded-Proto": "http"})
    assert resp.status_code == 301
    assert resp.headers["location"] == f"https://{HOST}/api/ping?a=1&b=%E4%B8%AD"


def test_http_post_redirects_308_preserving_method_semantics():
    client = _make_client()
    resp = client.post("/api/ping", headers={"X-Forwarded-Proto": "http"})
    assert resp.status_code == 308
    assert resp.headers["location"] == f"https://{HOST}/api/ping"


def test_missing_header_passes_through_no_hsts():
    """CLB 未勾 X-Forwarded-Proto / 小程序直连 IP：必须完全无动作。"""
    client = _make_client()
    resp = client.get("/api/ping")
    assert resp.status_code == 200
    assert "strict-transport-security" not in resp.headers


def test_https_response_gets_hsts_header():
    client = _make_client()
    resp = client.get("/api/ping", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 200
    assert resp.headers["strict-transport-security"] == "max-age=31536000"


def test_host_not_in_allowlist_never_acts():
    client = TestClient(
        _make_client().app, base_url="http://120.55.69.9", follow_redirects=False
    )
    r1 = client.get("/api/ping", headers={"X-Forwarded-Proto": "http"})
    assert r1.status_code == 200
    r2 = client.get("/api/ping", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in r2.headers


def test_host_matching_ignores_port_and_case():
    client = _make_client()
    resp = client.get(
        "/api/ping",
        headers={"Host": f"{HOST.upper()}:80", "X-Forwarded-Proto": "http"},
    )
    assert resp.status_code == 301
    # Location 沿用来访 Host（去端口、原大小写转发不敏感）
    assert resp.headers["location"].startswith("https://")


def test_multi_proxy_proto_uses_first_segment():
    client = _make_client()
    resp = client.get("/api/ping", headers={"X-Forwarded-Proto": "http, https"})
    assert resp.status_code == 301


def test_garbage_proto_passes_through():
    client = _make_client()
    resp = client.get("/api/ping", headers={"X-Forwarded-Proto": "wss"})
    assert resp.status_code == 200
    assert "strict-transport-security" not in resp.headers


def test_empty_hosts_is_fully_inert():
    client = _make_client(hosts=())
    resp = client.get("/api/ping", headers={"X-Forwarded-Proto": "http"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /docs 文档面收口（api.py::_docs_urls）
# ---------------------------------------------------------------------------

def test_docs_urls_disabled_in_production_and_staging():
    from opensearch_pipeline.api import _docs_urls

    off = {"docs_url": None, "redoc_url": None, "openapi_url": None}
    assert _docs_urls("production", False) == off
    assert _docs_urls("staging", False) == off
    assert _docs_urls("PRODUCTION", False) == off  # 大小写不敏感


def test_docs_urls_override_and_dev_default():
    from opensearch_pipeline.api import _docs_urls

    assert _docs_urls("production", True) == {}   # RAG_API_DOCS_ENABLE 逃生门
    assert _docs_urls("development", False) == {}
    assert _docs_urls("", False) == {}


def test_docs_reachable_in_dev_app_regression():
    """当前测试环境（development/SIM）下 app 的 /docs 与 /openapi.json 必须照常可达。"""
    from opensearch_pipeline import api

    client = TestClient(api.app, follow_redirects=False)
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_docs_404_when_wired_off():
    """按生产参数构造的 app：三个文档路由全 404。"""
    from opensearch_pipeline.api import _docs_urls

    app = FastAPI(**_docs_urls("production", False))
    client = TestClient(app, follow_redirects=False)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
