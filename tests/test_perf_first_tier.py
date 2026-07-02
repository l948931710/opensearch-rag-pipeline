# -*- coding: utf-8 -*-
"""性能第一梯队回归测试（2026-07-01 中期批次）。

覆盖：#2 共享 HTTP Session（_http_post 接缝）、#3 读时 ACL 短 TTL 缓存、
#4 AnyIO 线程令牌 env、#6 看板聚合 TTL 缓存、#7 query embedding LRU。
#5（BackgroundTasks 落库）由既有 miniapp/parity 测试覆盖（TestClient 在断言前
跑完 bg task，log_qa_session 的 monkeypatch 语义不变）。
"""
import asyncio

import pytest

from opensearch_pipeline import retriever


# ─── #7 query embedding LRU ──────────────────────────────────────────────

def _fake_embed_factory(calls):
    def _fake(texts, **kw):
        calls.append(list(texts))
        return [([0.1, 0.2], [1, 2], [0.5, 0.6])]
    return _fake


def test_query_embed_cache_hits_same_query(monkeypatch):
    calls = []
    import opensearch_pipeline.embedding_client as ec
    monkeypatch.setattr(ec, "embed_texts_native", _fake_embed_factory(calls))
    retriever._query_embed_cache_clear()

    r1 = retriever.get_query_embedding("如何请年假？")
    r2 = retriever.get_query_embedding("如何请年假？")
    assert len(calls) == 1, "同 query 第二次必须命中 LRU，不再打 DashScope"
    assert r1 == r2
    # 命中返回的是拷贝：调用方原地改动不得污染缓存
    r2[0].append(9.9)
    r3 = retriever.get_query_embedding("如何请年假？")
    assert r3[0] == [0.1, 0.2]


def test_query_embed_cache_miss_on_new_query(monkeypatch):
    calls = []
    import opensearch_pipeline.embedding_client as ec
    monkeypatch.setattr(ec, "embed_texts_native", _fake_embed_factory(calls))
    retriever._query_embed_cache_clear()

    retriever.get_query_embedding("问题A")
    retriever.get_query_embedding("问题B")
    assert len(calls) == 2


def test_query_embed_cache_disabled_by_env(monkeypatch):
    calls = []
    import opensearch_pipeline.embedding_client as ec
    monkeypatch.setattr(ec, "embed_texts_native", _fake_embed_factory(calls))
    monkeypatch.setenv("RAG_QUERY_EMBED_CACHE_SIZE", "0")
    retriever._query_embed_cache_clear()

    retriever.get_query_embedding("同一个问题")
    retriever.get_query_embedding("同一个问题")
    assert len(calls) == 2, "RAG_QUERY_EMBED_CACHE_SIZE=0 必须关闭缓存"


def test_query_embed_cache_failure_not_cached(monkeypatch):
    import opensearch_pipeline.embedding_client as ec
    state = {"n": 0}

    def _flaky(texts, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("dashscope down")
        return [([0.3], [1], [1.0])]

    monkeypatch.setattr(ec, "embed_texts_native", _flaky)
    retriever._query_embed_cache_clear()

    with pytest.raises(RuntimeError):
        retriever.get_query_embedding("失败后重试的问题")
    dense, _, _ = retriever.get_query_embedding("失败后重试的问题")
    assert dense == [0.3], "失败不得进缓存；恢复后应取到真值"


def test_query_embed_cache_evicts_lru(monkeypatch):
    calls = []
    import opensearch_pipeline.embedding_client as ec
    monkeypatch.setattr(ec, "embed_texts_native", _fake_embed_factory(calls))
    monkeypatch.setenv("RAG_QUERY_EMBED_CACHE_SIZE", "2")
    retriever._query_embed_cache_clear()

    retriever.get_query_embedding("q1")
    retriever.get_query_embedding("q2")
    retriever.get_query_embedding("q3")   # 逐出 q1
    retriever.get_query_embedding("q1")   # 重算
    assert len(calls) == 4


# ─── #3 读时 ACL 短 TTL 缓存 ─────────────────────────────────────────────

class _RowConn:
    """一次性返回固定行的假连接，记录查询次数。"""

    def __init__(self, row, counter):
        self._row = row
        self._counter = counter

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                conn._counter.append(sql)

            def fetchone(self):
                return conn._row

        return _Cur()

    def close(self):
        pass


def test_live_acl_cache_hits_within_ttl(monkeypatch):
    import opensearch_pipeline.dingtalk_identity as di
    queries = []
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _RowConn(("quality",), queries))
    di._live_acl_cache_clear()

    g1 = di._resolve_user_dept_cached("staff001")
    g2 = di._resolve_user_dept_cached("staff001")
    assert len(queries) == 1, "TTL 内第二次读必须命中缓存，不再打 RDS"
    assert g1 == g2 and g1 is not g2, "返回拷贝，防调用方原地改动污染缓存"


def test_live_acl_cache_none_row_cached_but_db_error_not(monkeypatch):
    import opensearch_pipeline.dingtalk_identity as di
    di._live_acl_cache_clear()
    queries = []
    # 无在册行（DB 真值 None）→ 可缓存
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _RowConn(None, queries))
    assert di._resolve_user_dept_cached("staff002") is None
    assert di._resolve_user_dept_cached("staff002") is None
    assert len(queries) == 1, "『DB 确认无在册行』是可缓存的真值"

    # DB 异常 → 不缓存（保持逐请求重试的 fail-open 原语义）
    di._live_acl_cache_clear()
    boom = {"n": 0}

    def _boom(*a, **k):
        boom["n"] += 1
        raise RuntimeError("rds down")

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    assert di._resolve_user_dept_cached("staff003") is None
    assert di._resolve_user_dept_cached("staff003") is None
    assert boom["n"] == 2, "DB 异常不得进缓存"


def test_live_acl_cache_disabled_by_env(monkeypatch):
    import opensearch_pipeline.dingtalk_identity as di
    queries = []
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _RowConn(("hr",), queries))
    monkeypatch.setenv("RAG_LIVE_ACL_TTL_SECONDS", "0")
    di._live_acl_cache_clear()

    di._resolve_user_dept_cached("staff004")
    di._resolve_user_dept_cached("staff004")
    assert len(queries) == 2, "TTL=0 必须逐请求实时复核（原行为）"


# ─── #4 AnyIO 线程令牌 ───────────────────────────────────────────────────

def test_threadpool_tokens_env_honored(monkeypatch):
    monkeypatch.setenv("RAG_THREADPOOL_TOKENS", "77")
    from opensearch_pipeline import api

    async def probe():
        import anyio.to_thread
        async with api._lifespan(api.app):
            return anyio.to_thread.current_default_thread_limiter().total_tokens

    assert asyncio.run(probe()) == 77


# ─── #2 共享 HTTP Session ────────────────────────────────────────────────

def test_http_session_is_shared_singleton():
    from opensearch_pipeline import http_session
    s1 = http_session.get_session()
    s2 = http_session.get_session()
    assert s1 is s2
    # 三个 DashScope 调用方都以 _http_post 为 patch 接缝（防退化回裸 requests.post）
    import opensearch_pipeline.embedding_client as ec
    import opensearch_pipeline.llm_generator as lg
    import opensearch_pipeline.reranker as rr
    for mod in (ec, lg, rr):
        assert getattr(mod, "_http_post") is http_session.http_post
        import inspect
        assert "requests.post(" not in inspect.getsource(mod), \
            f"{mod.__name__} 不应再直接调用 requests.post（绕过连接池）"


# ─── #6 看板聚合 TTL 缓存 ────────────────────────────────────────────────

def test_dashboard_cache_roundtrip(monkeypatch):
    from opensearch_pipeline.routes import kb_console as kc
    kc._dashboard_cache_clear()
    key = ("insights", ("quality",), 30)
    assert kc._dashboard_cache_get(key) is None
    kc._dashboard_cache_put(key, {"x": 1})
    assert kc._dashboard_cache_get(key) == {"x": 1}
    # 不同作用域不同键：永不跨权限串数据
    assert kc._dashboard_cache_get(("insights", ("hr",), 30)) is None
    # TTL=0 关闭（读写都短路）
    monkeypatch.setenv("RAG_KB_DASHBOARD_CACHE_TTL", "0")
    assert kc._dashboard_cache_get(key) is None
    kc._dashboard_cache_put(("k2",), 1)
    monkeypatch.delenv("RAG_KB_DASHBOARD_CACHE_TTL")
    assert kc._dashboard_cache_get(("k2",)) is None
