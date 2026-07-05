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


# ── P3-9：trace 真正接线（filter 安装 / 非 HTTP 入口 / gen_meta 落 rid）──


def test_ensure_request_id_reuses_or_mints():
    """已有 rid → 原样返回；'-'（executor/线程上下文）→ 铸新的并落入上下文。"""
    from opensearch_pipeline import request_context as rc
    import contextvars

    def _fresh():
        assert rc.get_request_id() == "-"
        rid = rc.ensure_request_id()
        assert rid and rid != "-"
        assert rc.get_request_id() == rid          # 已落上下文
        assert rc.ensure_request_id() == rid       # 幂等，不再铸新
    contextvars.Context().run(_fresh)


def test_install_request_id_logging_injects_rid(monkeypatch):
    """安装后 root handler 出口带 [rid=…]；幂等（二次安装不重复插）。"""
    import io
    import logging
    from opensearch_pipeline import request_context as rc

    monkeypatch.setattr(rc, "_logging_installed", False)
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    try:
        rc.install_request_id_logging()
        rc.install_request_id_logging()   # 幂等
        rid = rc.set_request_id("t3st1d00")
        logging.getLogger("opensearch_pipeline.retriever").error("probe-line")
        out = buf.getvalue()
        assert f"[rid={rid}] probe-line" in out
        assert out.count("[rid=") == out.count("probe-line")   # 没有重复注入
    finally:
        root.removeHandler(h)


def test_gen_meta_carries_request_id():
    """成功路径落库行此前无任何 trace——gen_meta 现在带 request_id（未设置时如实 null）。"""
    from opensearch_pipeline import request_context as rc
    from opensearch_pipeline.llm_generator import build_gen_meta
    import contextvars

    def _with_rid():
        rc.set_request_id("abc12345")
        meta = build_gen_meta("system prompt")
        assert meta["request_id"] == "abc12345"
    contextvars.Context().run(_with_rid)

    def _without():
        meta = build_gen_meta("system prompt")
        assert meta["request_id"] is None
    contextvars.Context().run(_without)


def test_process_rag_query_thread_sets_request_id(monkeypatch):
    """_process_rag_query 显式收 rid 并落线程上下文（Thread 不复制 ContextVar）。"""
    import contextvars
    from opensearch_pipeline import dingtalk_bot as bot
    from opensearch_pipeline import request_context as rc

    seen = {}

    def fake_retrieve(question, **kw):
        seen["rid"] = rc.get_request_id()
        return []
    monkeypatch.setattr(bot, "retrieve_and_enrich", fake_retrieve)
    monkeypatch.setattr(bot, "log_qa_session", lambda **kw: None)
    monkeypatch.setattr(bot, "_send_text_reply", lambda *a, **kw: None)

    def _fresh():
        bot._process_rag_query("问题", "https://oapi.dingtalk.com/x", "昵称",
                               "cid1", "U1", "1", request_id="rid-from-entry")
        assert seen["rid"] == "rid-from-entry"
    contextvars.Context().run(_fresh)
