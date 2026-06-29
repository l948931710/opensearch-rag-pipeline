# -*- coding: utf-8 -*-
"""test_request_context.py — 请求级 correlation id（统一 trace, OBS-trace）。

覆盖：ContextVar set/get/new、日志 Filter 注入 request_id、纯 ASGI 中间件【传播到端点】+ 响应头
回写 + 入站 X-Request-Id 透传。传播到端点是关键（BaseHTTPMiddleware 做不到，故用纯 ASGI）。
"""
import logging

from opensearch_pipeline.request_context import (
    RequestIdLogFilter,
    RequestIdMiddleware,
    get_request_id,
    new_request_id,
    set_request_id,
)


def test_set_get_new_request_id():
    assert len(new_request_id()) == 8
    assert set_request_id("abc12345") == "abc12345"
    assert get_request_id() == "abc12345"
    # 空值 → 生成新的（非 '-'）
    rid = set_request_id("")
    assert rid and rid != "-" and len(rid) == 8


def test_log_filter_injects_request_id():
    set_request_id("logtrace")
    f = RequestIdLogFilter()
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", None, None)
    assert f.filter(rec) is True
    assert rec.request_id == "logtrace"


def test_middleware_generates_and_returns_header_and_propagates():
    """纯 ASGI 中间件：端点内 get_request_id() == 响应头 X-Request-Id（传播成功）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/probe")
    def probe():
        return {"rid": get_request_id()}

    c = TestClient(app)
    r = c.get("/probe")
    hdr = r.headers.get("X-Request-Id")
    assert hdr and hdr != "-" and len(hdr) == 8       # 自动生成并回写响应头
    assert r.json()["rid"] == hdr                      # 端点内可见 == 响应头（传播到端点）


def test_middleware_honors_incoming_request_id():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/probe")
    def probe():
        return {"rid": get_request_id()}

    c = TestClient(app)
    r = c.get("/probe", headers={"X-Request-Id": "client-supplied-id"})
    assert r.headers["X-Request-Id"] == "client-supplied-id"   # 跨服务透传
    assert r.json()["rid"] == "client-supplied-id"


def test_main_app_emits_request_id_header():
    """主 app 已挂中间件：任意响应带 X-Request-Id。"""
    from fastapi.testclient import TestClient
    import opensearch_pipeline.api as api

    r = TestClient(api.app).get("/api/health")
    assert r.headers.get("X-Request-Id")
