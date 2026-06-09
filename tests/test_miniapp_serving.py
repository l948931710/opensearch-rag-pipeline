# -*- coding: utf-8 -*-
"""
test_miniapp_serving.py — 钉钉小程序服务端改动测试

覆盖：
  - auth_token 会话令牌签发/校验/防篡改/过期
  - build_mini_program_blocks 图文块重映射（含全文 caption）
  - /api/auth/dingtalk 免登（模拟模式）
  - /api/ask 部门一律服务端解析、绝不信任请求体 user_dept（越权防护）
  - /api/feedback user_id 取自令牌而非请求体
"""

import os

# 模拟模式 + 固定签名密钥（须在导入 api 之前设置）
os.environ.setdefault("RAG_SIMULATE", "true")
os.environ.setdefault("RAG_SESSION_SIGNING_KEY", "test-signing-key")

import pytest

from opensearch_pipeline import auth_token
from opensearch_pipeline import content_blocks_builder as cb


# ── auth_token ───────────────────────────────────────────────

def test_token_roundtrip():
    t = auth_token.issue_session_token("U1", dept="行政部", name="张三")
    p = auth_token.verify_session_token(t)
    assert p and p["uid"] == "U1" and p["dept"] == "行政部" and p["name"] == "张三"


def test_token_tamper_and_garbage():
    t = auth_token.issue_session_token("U1", dept="行政部")
    flipped = t[:-2] + ("aa" if not t.endswith("aa") else "bb")
    assert auth_token.verify_session_token(flipped) is None
    assert auth_token.verify_session_token("garbage") is None
    assert auth_token.verify_session_token("") is None


def test_token_expired():
    t = auth_token.issue_session_token("U1", ttl=-10)
    assert auth_token.verify_session_token(t) is None


# ── build_mini_program_blocks ────────────────────────────────

def test_blocks_pure_text_returns_empty():
    assert cb.build_mini_program_blocks("纯文字答案，无图片引用", []) == []


def test_blocks_image_remap_full_caption(monkeypatch):
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=3600: "https://oss/" + key)
    long_summary = "登录界面截图说明" * 30  # > 100 字，验证不被截断
    chunks = [{"chunk_type": "image", "source_image": "k/a.png",
               "visual_summary": long_summary, "title": "x"}]
    blocks = cb.build_mini_program_blocks("第一步看这里<<IMG:1>>然后完成", chunks)
    types = [b["type"] for b in blocks]
    assert types == ["text", "image", "text"]
    img = blocks[1]
    assert img["url"] == "https://oss/k/a.png"
    assert img["caption"] == img["alt"] == long_summary  # 全文，未截断到 100
    assert blocks[0]["format"] == "markdown" and blocks[0]["text"] == "第一步看这里"


# ── HTTP endpoints ───────────────────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import opensearch_pipeline.api as api
    return TestClient(api.app)


def test_auth_dingtalk_simulate(client):
    r = client.post("/api/auth/dingtalk", json={"auth_code": "anything"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["user_id"]          # 模拟模式 → SIM_USER
    assert j["token"]            # 已签发令牌
    # 令牌可被服务端校验
    assert auth_token.verify_session_token(j["token"])["uid"] == j["user_id"]


def test_ask_uses_token_dept_not_body(monkeypatch):
    """请求体里伪造 user_dept/user_id 必须被忽略；部门取自令牌。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient

    captured = {}

    def fake_retrieve(question, top_k=7, user_dept=None, **kw):
        captured["dept"] = user_dept
        return [{"doc_id": "d1", "title": "T", "section_title": "S", "score": 9.0, "chunk_text": "c"}]

    def fake_generate(question, chunks, history=None, max_tokens=2048, temperature=0.1, pure_text=None):
        return {"answer": "答案正文",
                "sources": [{"doc_id": "d1", "title": "T", "section": "S", "score": 9.0}],
                "model": "qwen-test", "usage": {}}

    monkeypatch.setattr(api, "retrieve_and_enrich", fake_retrieve)
    monkeypatch.setattr(api, "generate_answer", fake_generate)
    monkeypatch.setattr(api, "build_mini_program_blocks",
                        lambda ans, chunks: [{"type": "text", "format": "plain", "text": ans}])
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)

    tok = auth_token.issue_session_token("U9", dept="行政部", name="李四")
    c = TestClient(api.app)
    r = c.post("/api/ask",
               json={"question": "q", "user_id": "ATTACKER", "user_dept": "财务部"},
               headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    assert captured["dept"] == "行政部"   # 取自令牌，而非请求体的 财务部
    j = r.json()
    assert j["message_id"]
    assert j["blocks"] == [{"type": "text", "format": "plain", "text": "答案正文"}]


def test_ask_no_token_is_anonymous_public_only(monkeypatch):
    """无令牌按匿名处理：请求体 user_id 不得反查部门（防伪造 staffId 跨部门读取）。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient

    captured = {}

    def fake_retrieve(question, top_k=7, user_dept=None, **kw):
        captured["dept"] = user_dept
        return [{"doc_id": "d1", "title": "T", "section_title": "S", "score": 9.0, "chunk_text": "c"}]

    monkeypatch.setattr(api, "retrieve_and_enrich", fake_retrieve)
    monkeypatch.setattr(api, "generate_answer",
                        lambda *a, **k: {"answer": "x", "sources": [], "model": "m", "usage": {}})
    monkeypatch.setattr(api, "build_mini_program_blocks", lambda ans, chunks: [])
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)

    c = TestClient(api.app)
    r = c.post("/api/ask", json={"question": "q", "user_id": "EMP1", "user_dept": "财务部"})
    assert r.status_code == 200, r.text
    assert captured["dept"] is None   # 无令牌 → 匿名（仅 public）；user_id 不授予部门权限


def test_ask_token_dept_marketing_center(monkeypatch):
    """钉钉部门「营销中心」走令牌 → 检索收到的 user_dept 原样为「营销中心」。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient

    captured = {}

    def fake_retrieve(question, top_k=7, user_dept=None, **kw):
        captured["dept"] = user_dept
        return [{"doc_id": "d1", "title": "T", "section_title": "S", "score": 9.0, "chunk_text": "c"}]

    monkeypatch.setattr(api, "retrieve_and_enrich", fake_retrieve)
    monkeypatch.setattr(api, "generate_answer",
                        lambda *a, **k: {"answer": "x", "sources": [], "model": "m", "usage": {}})
    monkeypatch.setattr(api, "build_mini_program_blocks", lambda ans, chunks: [])
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)

    tok = auth_token.issue_session_token("MKT001", dept="营销中心", name="测试用户")
    c = TestClient(api.app)
    r = c.post("/api/ask", json={"question": "q"},
               headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    assert captured["dept"] == "营销中心"


def test_feedback_uses_token_user(monkeypatch):
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient

    captured = {}

    def fake_handle(message_id, user_id, action, reason=None, comment=None, **kw):
        captured.update(message_id=message_id, user_id=user_id, action=action)
        return True

    monkeypatch.setattr(api, "handle_feedback", fake_handle)
    tok = auth_token.issue_session_token("U9", dept="行政部")
    c = TestClient(api.app)
    r = c.post("/api/feedback",
               json={"message_id": "m1", "user_id": "SPOOF", "feedback_type": "upvote"},
               headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    assert captured["user_id"] == "U9"   # 取自令牌，而非请求体 SPOOF
    assert captured["action"] == "upvote"
