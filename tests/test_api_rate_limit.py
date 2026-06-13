# -*- coding: utf-8 -*-
"""/api/* 防刷端点集成测试（TestClient，mock 检索/LLM/落库）。

覆盖：匿名 429 + Retry-After、登录用户与匿名互不影响、XFF 区分匿名 IP、
深思匿名 403 / 配额 429、全局熔断 503（健康检查不受影响）、辅助端点 429、
search 不计 LLM 熔断、流式端点同层准入、限流关闭时全放行。
"""

import os

# 模拟模式 + 固定签名密钥（须在导入 api 之前设置）
os.environ.setdefault("RAG_SIMULATE", "true")
os.environ.setdefault("RAG_SESSION_SIGNING_KEY", "test-signing-key")

import pytest
from fastapi.testclient import TestClient

import opensearch_pipeline.api as api
from opensearch_pipeline import rate_limiter as rl
from opensearch_pipeline.auth_token import issue_session_token

FAKE_CHUNKS = [{
    "chunk_text": "U8+ 登录步骤……", "title": "U8+ 操作手册", "section_title": "登录",
    "doc_id": "d1", "chunk_id": "c1", "category_l1": "ERP", "score": 8.5,
}]
FAKE_ANSWER = {"answer": "按文档操作即可。", "sources": [], "model": "qwen-test", "usage": {}}


@pytest.fixture(autouse=True)
def _isolated_limiter():
    """每用例前后清空单例计数；限额快照延迟重读（monkeypatch 还原后自然回默认）。"""
    rl.LIMITER.reset_for_tests()
    yield
    rl.LIMITER.reset_for_tests()


@pytest.fixture
def client(monkeypatch):
    """启用限流的 TestClient 工厂：client(**env) → TestClient。"""

    def _make(**env):
        monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        rl.LIMITER.reset_for_tests()

        def fake_stream(*a, **kw):
            yield 'data: {"type": "chunk", "content": "答"}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(api, "retrieve_and_enrich", lambda *a, **kw: list(FAKE_CHUNKS))
        monkeypatch.setattr(api, "generate_answer", lambda *a, **kw: dict(FAKE_ANSWER))
        monkeypatch.setattr(api, "generate_answer_via_stream", lambda *a, **kw: dict(FAKE_ANSWER))
        monkeypatch.setattr(api, "generate_answer_stream", fake_stream)
        monkeypatch.setattr(api, "search_chunks", lambda *a, **kw: [])
        monkeypatch.setattr(api, "build_mini_program_blocks", lambda ans, chunks: [])
        monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)
        monkeypatch.setattr(api, "_append_to_history", lambda *a, **kw: None)
        return TestClient(api.app)

    return _make


def _bearer(uid="U1", dept="行政部"):
    return {"Authorization": "Bearer " + issue_session_token(uid, dept=dept, name="测试")}


# ── 匿名按 IP 严格限频 ───────────────────────────────────────

def test_anon_per_min_429_with_retry_after(client):
    c = client(RAG_RATE_ANON_PER_MIN=2, RAG_RATE_ANON_PER_DAY=0)
    for _ in range(2):
        assert c.post("/api/ask", json={"question": "U8 怎么登录"}).status_code == 200
    r = c.post("/api/ask", json={"question": "U8 怎么登录"})
    assert r.status_code == 429
    assert "频繁" in r.json()["detail"]
    assert 1 <= int(r.headers["Retry-After"]) <= 61


def test_anon_keyed_by_xff_ip(client):
    # TestClient host 是字面量 "testclient"（非公网 IP）→ XFF 生效，可区分匿名来源
    c = client(RAG_RATE_ANON_PER_MIN=1, RAG_RATE_ANON_PER_DAY=0)
    h_a = {"X-Forwarded-For": "203.0.113.7"}
    h_b = {"X-Forwarded-For": "203.0.113.8"}
    assert c.post("/api/ask", json={"question": "q"}, headers=h_a).status_code == 200
    assert c.post("/api/ask", json={"question": "q"}, headers=h_a).status_code == 429
    assert c.post("/api/ask", json={"question": "q"}, headers=h_b).status_code == 200


def test_anon_daily_quota_message(client):
    c = client(RAG_RATE_ANON_PER_MIN=0, RAG_RATE_ANON_PER_DAY=1)
    assert c.post("/api/ask", json={"question": "q"}).status_code == 200
    r = c.post("/api/ask", json={"question": "q"})
    assert r.status_code == 429 and "登录" in r.json()["detail"]


# ── 登录用户与匿名隔离 ───────────────────────────────────────

def test_user_not_affected_by_anon_exhaustion(client):
    c = client(RAG_RATE_ANON_PER_MIN=1, RAG_RATE_ANON_PER_DAY=0,
               RAG_RATE_USER_PER_MIN=5, RAG_RATE_USER_PER_DAY=0)
    assert c.post("/api/ask", json={"question": "q"}).status_code == 200
    assert c.post("/api/ask", json={"question": "q"}).status_code == 429
    # 同一来源但持有效令牌 → 走用户档
    for _ in range(5):
        assert c.post("/api/ask", json={"question": "q"}, headers=_bearer()).status_code == 200
    assert c.post("/api/ask", json={"question": "q"}, headers=_bearer()).status_code == 429


def test_body_user_id_cannot_rotate_anon_key(client):
    # 请求体 user_id 未鉴权，绝不能当限流 key（否则攻击者逐请求换名绕过）
    c = client(RAG_RATE_ANON_PER_MIN=1, RAG_RATE_ANON_PER_DAY=0)
    assert c.post("/api/ask", json={"question": "q", "user_id": "a"}).status_code == 200
    assert c.post("/api/ask", json={"question": "q", "user_id": "b"}).status_code == 429


# ── 深思日配额 ───────────────────────────────────────────────

def test_thinking_anon_403(client):
    c = client()
    r = c.post("/api/ask", json={"question": "q", "thinking": True})
    assert r.status_code == 403 and "登录" in r.json()["detail"]


def test_thinking_quota_429_then_plain_ok(client):
    c = client(RAG_THINKING_DAILY_QUOTA=1, RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=0)
    h = _bearer()
    assert c.post("/api/ask", json={"question": "q", "thinking": True}, headers=h).status_code == 200
    r = c.post("/api/ask", json={"question": "q", "thinking": True}, headers=h)
    assert r.status_code == 429 and "深度思考" in r.json()["detail"]
    # 关闭深思仍可正常提问（深思拒绝不消耗常规预算）
    assert c.post("/api/ask", json={"question": "q"}, headers=h).status_code == 200


# ── 全局日熔断 ───────────────────────────────────────────────

def test_global_cap_503_health_unaffected(client):
    c = client(RAG_GLOBAL_DAILY_LLM_CAP=2,
               RAG_RATE_ANON_PER_MIN=0, RAG_RATE_ANON_PER_DAY=0,
               RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=0)
    assert c.post("/api/ask", json={"question": "q"}).status_code == 200
    assert c.post("/api/ask", json={"question": "q"}, headers=_bearer()).status_code == 200
    # 触顶：匿名与登录用户一视同仁 503
    r = c.post("/api/ask", json={"question": "q"}, headers=_bearer("U9"))
    assert r.status_code == 503 and "上限" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) >= 1
    # 健康检查与纯检索不受 LLM 熔断影响
    assert c.get("/api/health").status_code == 200
    assert c.post("/api/search", json={"query": "q"}).status_code == 200


def test_stream_endpoint_shares_admission(client):
    c = client(RAG_RATE_ANON_PER_MIN=1, RAG_RATE_ANON_PER_DAY=0)
    assert c.post("/api/ask/stream", json={"question": "q"}).status_code == 200
    r = c.post("/api/ask/stream", json={"question": "q"})
    assert r.status_code == 429  # 限流在 StreamingResponse 之前拒绝，普通 JSON


# ── 辅助端点轻量限频 ─────────────────────────────────────────

def test_aux_endpoints_per_min(client):
    c = client(RAG_RATE_AUX_PER_MIN=2)
    assert c.get("/api/hot-questions").status_code == 200
    assert c.get("/api/hot-questions").status_code == 200
    r = c.get("/api/hot-questions")
    assert r.status_code == 429 and "频繁" in r.json()["detail"]


def test_aux_resign_images_limited(client):
    c = client(RAG_RATE_AUX_PER_MIN=1)
    body = {"oss_keys": ["processing/assets/a/d/v1/x.png"]}
    assert c.post("/api/resign-images", json=body).status_code == 200
    assert c.post("/api/resign-images", json=body).status_code == 429


# ── 总开关 ───────────────────────────────────────────────────

def test_limits_disabled_all_pass(client):
    c = client(RAG_RATE_LIMIT_ENABLE="false", RAG_RATE_ANON_PER_MIN=1)
    for _ in range(6):
        assert c.post("/api/ask", json={"question": "q"}).status_code == 200
