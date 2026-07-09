# -*- coding: utf-8 -*-
"""
test_routes_agent.py — console /api/agent/ask 入口（Task 15）

TestClient + dependency_overrides(current_identity)：
- flag-off → 404（隐藏入口，对现有链路零影响）；
- 未登录 → 401；
- 影子链路端到端：假 gateway 提 knowledge_search → adjudicator 执行（mock retrieve）→ 最终答案，
  SSE 帧 session/tool_call/chunk/done/[DONE] 齐全。
真 registry+policy+adjudicator+executor + 假 gateway/store + mock retrieve；不碰真模型/真检索/真库。
"""
import pytest
from fastapi.testclient import TestClient

import opensearch_pipeline.routes.agent as agent_route
from opensearch_pipeline import api
from opensearch_pipeline.agent_runtime import (
    ChatResponse,
    ModelGateway,
    ThreadedRunExecutor,
    ToolCall,
    Usage,
    default_policy_engine,
    make_adjudicator,
)
from opensearch_pipeline.agent_tools import build_default_registry
from opensearch_pipeline.api import Identity, current_identity

_RETRIEVE = "opensearch_pipeline.retriever.retrieve_and_enrich"


class _FakeStore:
    def __init__(self):
        self._n = 0
        self._step = 0
        self.transitions = []
        self.invocations = []

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        return True

    def consume_budget(self, run_id, **k):
        return {}

    def append_step(self, run_id, step):
        self._step += 1
        return self._step

    def record_invocation(self, run_id, step_no, **kw):
        iid = f"inv{len(self.invocations) + 1}"
        self.invocations.append({"id": iid, **kw})
        return iid

    def finish_invocation(self, invocation_id, **kw):
        for inv in self.invocations:
            if inv["id"] == invocation_id:
                inv.update(kw)

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return None

    def get_run(self, run_id):
        return None                      # 默认：run 不存在（approve 归属校验测试用）


class _FakeProvider:
    name = "dashscope"

    def capabilities(self, m):
        return None

    def chat(self, model, req):
        if any(m.get("role") == "tool" for m in req.messages):
            return ChatResponse(text="根据知识库：应遵循 GB/T 包装标准。", tool_calls=[],
                                usage=Usage(tokens_prompt=10, tokens_completion=5), model=model)
        return ChatResponse(text="", tool_calls=[ToolCall(id="c1", name="knowledge_search",
                            arguments={"query": "包装规范"})], usage=Usage(), model=model)


def _identity():
    return Identity(user_id="u1", acl_groups=["production", "marketing"], role="employee")


@pytest.fixture
def wired(monkeypatch):
    registry = build_default_registry()
    store = _FakeStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _FakeProvider()}, routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime", lambda: (registry, gateway, executor, store))
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: [
        {"doc_id": "D1", "chunk_text": "GB/T 包装标准全文…", "doc_title": "包装规范"}])
    yield executor
    executor.shutdown()


def _client(identity):
    api.app.dependency_overrides[current_identity] = lambda: identity
    return TestClient(api.app)


def test_disabled_returns_404(monkeypatch):
    monkeypatch.delenv("RAG_AGENT_ENABLE", raising=False)
    try:
        r = _client(_identity()).post("/api/agent/ask", json={"question": "x"})
        assert r.status_code == 404
    finally:
        api.app.dependency_overrides.clear()


def test_unauthenticated_returns_401(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    try:
        r = _client(None).post("/api/agent/ask", json={"question": "x"})
        assert r.status_code == 401
    finally:
        api.app.dependency_overrides.clear()


def test_shadow_link_sse(monkeypatch, wired):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    try:
        r = _client(_identity()).post("/api/agent/ask", json={"question": "富岭包装规范?"})
        assert r.status_code == 200
        body = r.text
        assert '"type": "session"' in body and '"run_id"' in body
        assert '"type": "tool_call"' in body and "knowledge_search" in body
        assert '"type": "chunk"' in body and "GB/T" in body     # 最终答案
        assert '"type": "done"' in body and "[DONE]" in body
    finally:
        api.app.dependency_overrides.clear()


def test_approve_disabled_returns_404(monkeypatch):
    monkeypatch.delenv("RAG_AGENT_ENABLE", raising=False)
    try:
        r = _client(_identity()).post("/api/agent/approve",
                                      json={"run_id": "x", "outcome": {"kind": "approved"}})
        assert r.status_code == 404
    finally:
        api.app.dependency_overrides.clear()


def test_approve_run_not_found_returns_404(monkeypatch, wired):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    try:
        r = _client(_identity()).post("/api/agent/approve",
                                      json={"run_id": "nope", "outcome": {"kind": "approved"}})
        assert r.status_code == 404                          # get_run→None → 归属校验前即 404
    finally:
        api.app.dependency_overrides.clear()


class _CapProvider:
    """捕获每次 chat 收到的 messages（多轮测试断言历史进上下文）。"""
    name = "dashscope"

    def __init__(self):
        self.seen = []

    def capabilities(self, m):
        return None

    def chat(self, model, req):
        self.seen.append([m.get("content") for m in req.messages])
        if any(m.get("role") == "tool" for m in req.messages):
            return ChatResponse(text="答案文本", tool_calls=[],
                                usage=Usage(tokens_prompt=6, tokens_completion=4), model=model)
        return ChatResponse(text="", tool_calls=[ToolCall(id="c1", name="knowledge_search",
                            arguments={"query": "x"})], usage=Usage(), model=model)


def test_multi_turn_feeds_prior_history(monkeypatch):
    """两轮同 conversation_id：第二轮模型上下文含第一轮 Q&A，且会话累积到 2 轮（多轮连续）。"""
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    from opensearch_pipeline import session_store as ss
    from opensearch_pipeline.agent_runtime import ThreadedRunExecutor, make_adjudicator

    provider = _CapProvider()
    registry = build_default_registry()
    store = _FakeStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": provider}, routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime", lambda: (registry, gateway, executor, store))
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: [
        {"doc_id": "D", "chunk_text": "GB/T 全文…", "doc_title": "规范"}])

    conv = "mt-conv-x"
    tid = f"{conv}:u1"                                        # identity.user_id = u1
    ss.clear_session(tid, owner="u1", trusted=True)
    try:
        c = _client(_identity())
        assert c.post("/api/agent/ask", json={"question": "第一轮问题",
                                              "conversation_id": conv}).status_code == 200
        assert c.post("/api/agent/ask", json={"question": "第二轮追问",
                                              "conversation_id": conv}).status_code == 200
        # 最后一次模型调用（第二轮）应看到第一轮 Q&A + 第二轮问题
        last = provider.seen[-1]
        assert "第一轮问题" in last and "答案文本" in last and "第二轮追问" in last
        # 会话累积 2 轮（经 SessionMemory 公共 API 读回）
        from opensearch_pipeline.agent_runtime.session_memory import default_session_memory
        hist = default_session_memory().get_snapshot(tid, owner="u1").messages
        assert [m["role"] for m in hist].count("user") == 2
        assert [m["role"] for m in hist].count("assistant") == 2
        assert [m["content"] for m in hist][0] == "第一轮问题"
    finally:
        ss.clear_session(tid, owner="u1", trusted=True)
        executor.shutdown()
        api.app.dependency_overrides.clear()
