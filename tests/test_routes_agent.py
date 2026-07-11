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


# ── B6 对账：decided-but-not-resumed 重驱（reaper 循环消费）──────────────────
class _ReconcileExecutor:
    def __init__(self, reject=False):
        self.resumed = []
        self._reject = reject

    def resume(self, run_id, ctx, outcome, loop, tools, on_complete=None, on_failure=None,
               approval_meta=None):
        from opensearch_pipeline.agent_runtime.executor import RunRejected
        if self._reject:
            raise RunRejected("pool full")
        self.resumed.append((run_id, outcome, ctx.user_id))

        class _H:
            def events(self):
                return iter([])
        return _H()


def _candidate(run_id="r1", decision="approved", reason=None):
    return {"request_id": "req1", "run_id": run_id, "tool_name": "u8_writeback",
            "call_id": "c1", "decision": decision, "reason": reason,
            "decided_by": "admin2", "decided_at": "2026-07-09 20:00:00"}


class _ReconcileApprovalStore:
    def __init__(self, candidates):
        self._c = candidates

    def list_decided_unresumed(self, *, grace_s=120, limit=20):
        return list(self._c)


def _reconcile_run_store(status="suspended"):
    store = _SuspendedStore()
    store.run["status"] = status
    return store


def test_reconcile_redrives_approved_as_requester(monkeypatch):
    """决定落库但 run 仍挂着 → 按 approval_decision 重建 Approved 重驱；ctx=发起人身份。"""
    import opensearch_pipeline.dingtalk_identity as di
    from opensearch_pipeline.agent_runtime.approval import Approved
    monkeypatch.setattr(di, "_resolve_user_identity", lambda uid: {"dept": ["production"], "name": uid})
    monkeypatch.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
    ex = _ReconcileExecutor()
    registry = build_default_registry()
    gateway = ModelGateway({"dashscope": _FakeProvider()}, routes={"light": [("dashscope", "m")]})
    n = agent_route._reconcile_decided(registry, gateway, ex,
                                       _reconcile_run_store(),
                                       _ReconcileApprovalStore([_candidate()]))
    assert n == 1 and len(ex.resumed) == 1
    run_id, outcome, as_user = ex.resumed[0]
    assert run_id == "r1" and isinstance(outcome, Approved)
    assert as_user == "u1", "重驱必须以发起人身份续跑（绝不套对账进程身份）"


def test_reconcile_skips_edited_and_feedback_carries_reason(monkeypatch):
    """EDITED 不自动重驱（库存参数已脱敏，掩码值绝不能执行）；rejected_feedback 带回理由。"""
    import opensearch_pipeline.dingtalk_identity as di
    from opensearch_pipeline.agent_runtime.approval import RejectedFeedback
    monkeypatch.setattr(di, "_resolve_user_identity", lambda uid: {"dept": [], "name": uid})
    monkeypatch.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
    ex = _ReconcileExecutor()
    registry = build_default_registry()
    gateway = ModelGateway({"dashscope": _FakeProvider()}, routes={"light": [("dashscope", "m")]})
    n = agent_route._reconcile_decided(
        registry, gateway, ex, _reconcile_run_store(),
        _ReconcileApprovalStore([_candidate(decision="edited"),
                                 _candidate(run_id="r1", decision="rejected_feedback",
                                            reason="金额过大")]))
    assert n == 1 and len(ex.resumed) == 1                      # edited 被跳过
    _, outcome, _ = ex.resumed[0]
    assert isinstance(outcome, RejectedFeedback) and outcome.reason == "金额过大"


def test_reconcile_pool_full_and_stale_candidates_are_safe(monkeypatch):
    """池满（RunRejected）吞掉下轮再来；run 已非 suspended（被人工重试抢先）→ 让位不重驱。"""
    import opensearch_pipeline.dingtalk_identity as di
    monkeypatch.setattr(di, "_resolve_user_identity", lambda uid: {"dept": [], "name": uid})
    monkeypatch.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
    registry = build_default_registry()
    gateway = ModelGateway({"dashscope": _FakeProvider()}, routes={"light": [("dashscope", "m")]})
    # 池满：不抛出、计数 0
    n = agent_route._reconcile_decided(registry, gateway, _ReconcileExecutor(reject=True),
                                       _reconcile_run_store(),
                                       _ReconcileApprovalStore([_candidate()]))
    assert n == 0
    # run 已 running（被人工 /approve 抢先认领）→ 不重驱
    ex = _ReconcileExecutor()
    n = agent_route._reconcile_decided(registry, gateway, ex,
                                       _reconcile_run_store(status="running"),
                                       _ReconcileApprovalStore([_candidate()]))
    assert n == 0 and ex.resumed == []


class _StreamFakeProvider:
    """带 chat_stream 的假 provider：工具轮零增量返回 tool_call；最终轮流式吐字。"""

    name = "dashscope"

    def capabilities(self, m):
        return None

    def chat(self, model, req):
        raise AssertionError("流式模式下不应回落到同步 chat")

    def chat_stream(self, model, req):
        from opensearch_pipeline.agent_runtime.model_gateway import StreamDelta
        if any(m.get("role") == "tool" for m in req.messages):
            yield StreamDelta(text="根据")
            yield StreamDelta(text="检索")
            return ChatResponse(text="根据检索", tool_calls=[],
                                usage=Usage(tokens_prompt=6, tokens_completion=4), model=model)
        return ChatResponse(text="", tool_calls=[ToolCall(id="c1", name="knowledge_search",
                                                          arguments={"query": "x"})],
                            usage=Usage(), model=model)


def test_streaming_sse_typewriter_no_duplicate_final(monkeypatch):
    """真流式端到端：模型增量 → ModelDelta → SSE 多个 chunk 帧（打字机）；
    RunCompleted.streamed=True → 全文**不重发**（否则前端看到答案两遍）；done 帧带 usage。"""
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    registry = build_default_registry()
    store = _FakeStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _StreamFakeProvider()},
                           routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime", lambda: (registry, gateway, executor, store))
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: [
        {"doc_id": "D1", "chunk_text": "GB/T 全文…", "doc_title": "规范"}])
    try:
        r = _client(_identity()).post("/api/agent/ask", json={"question": "包装规范?"})
        assert r.status_code == 200
        body = r.text
        assert '"content": "根据"' in body and '"content": "检索"' in body   # 打字机增量
        assert '"content": "根据检索"' not in body, "streamed=True 时全文不得重发"
        assert '"type": "tool_call"' in body and '"type": "done"' in body
        assert body.count('"type": "chunk"') == 2
    finally:
        executor.shutdown()
        api.app.dependency_overrides.clear()


def test_failed_run_logs_agent_error(monkeypatch):
    """run 失败 → qa_session_log 落 AGENT_ERROR 行（深度审查治理组：此前失败对运维零可见），
    且失败不进会话记忆。"""
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")

    class _BoomProvider:
        name = "dashscope"

        def capabilities(self, m):
            return None

        def chat(self, model, req):
            raise RuntimeError("provider down")

    registry = build_default_registry()
    store = _FakeStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _BoomProvider()},
                           routes={"light": [("dashscope", "m")]}, max_retries=0)
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime", lambda: (registry, gateway, executor, store))
    logged = []
    import opensearch_pipeline.qa_logger as ql
    monkeypatch.setattr(ql, "log_qa_session", lambda **kw: logged.append(kw))
    try:
        r = _client(_identity()).post("/api/agent/ask", json={"question": "会失败的问题"})
        assert r.status_code == 200 and '"type": "error"' in r.text
        assert logged, "失败 run 必须落 qa_session_log"
        assert logged[-1]["answer_status"] == "AGENT_ERROR"
        assert logged[-1]["model_name"] == "agent" and logged[-1]["error_message"]
    finally:
        executor.shutdown()
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


# ─────────────────────────────────────────────────────────────────────────────
# 审批闭环（schema/025 + 职责分离；深度审查 A 组 P1）：
# 自批 403 / 发起人撤回 200 / kb_admin·dept_admin scope 裁决 / 决定持久化 / 审批队列
# ─────────────────────────────────────────────────────────────────────────────
class _SuspendedStore(_FakeStore):
    """带内存 CAS 状态机的假 run store（approve 路径用）。"""

    def __init__(self, run_id="r1", user_id="u1"):
        super().__init__()
        self.run = {"run_id": run_id, "status": "suspended", "user_id": user_id,
                    "channel": "console", "agent_profile": "default", "started_at": None,
                    "ended_at": None, "thread_id": "appr-t1", "conversation_id": None}

    def get_run(self, run_id):
        return dict(self.run) if run_id == self.run["run_id"] else None

    def load_latest_checkpoint(self, run_id):
        return None

    def heartbeat(self, run_id):
        pass

    def transition(self, run_id, frm, to):
        if self.run["status"] != frm:
            return False
        self.run["status"] = to
        self.transitions.append((run_id, frm, to))
        return True


class _FakeApprovalStore:
    def __init__(self, areq=None):
        self.areq = areq
        self.decisions = []

    def get_latest_by_run(self, run_id):
        return dict(self.areq) if self.areq and self.areq["run_id"] == run_id else None

    def decide(self, request_id, *, decision, decided_by, reason=None,
               edited_args=None, idempotency_key=None):
        self.decisions.append({"request_id": request_id, "decision": decision,
                               "decided_by": decided_by})
        self.areq["status"] = decision
        return "accepted"

    def list_pending(self, scopes=None, *, requested_by=None, limit=100):
        items = [dict(self.areq)] if self.areq and self.areq["status"] == "pending" else []
        if scopes is not None:
            items = [a for a in items if a.get("approver_scope") in set(scopes)]
        if requested_by:
            items = [a for a in items if a.get("requested_by") == requested_by]
        return items

    def expire_stale(self):
        return 0


def _areq(run_id="r1", scope="production", requested_by="u1"):
    return {"request_id": "req1", "run_id": run_id, "call_id": "c1", "tool_name": "u8_writeback",
            "tool_version": "1.0", "proposed_args": {"qty": 7}, "args_digest": "d",
            "render_summary": "u8_writeback(qty)", "requested_by": requested_by,
            "requested_dept": scope, "approver_scope": scope, "status": "pending",
            "expires_at": None, "created_at": None, "decided_at": None}


def _kb_ident(role, granted=()):
    from opensearch_pipeline.kb_authz import KbIdentity
    return KbIdentity.build(user_id="whoever", role=role, granted_owner_depts=list(granted))


@pytest.fixture
def approval_wired(monkeypatch):
    """approve 路径打桩：挂起 run + 假审批存储 + 假 gateway（terminate 不触模型）。
    限流打桩为 no-op：本组测授权矩阵，RAG_ENV=local 组合跑时真限流的跨测试计数会误伤。"""
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setattr(agent_route, "_enforce_rate_limit", lambda *a, **k: None)
    registry = build_default_registry()
    store = _SuspendedStore()
    astore = _FakeApprovalStore(_areq())
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _FakeProvider()}, routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime", lambda: (registry, gateway, executor, store))
    monkeypatch.setattr(agent_route, "_get_approval_store", lambda: astore)
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: [
        {"doc_id": "D1", "chunk_text": "GB/T 包装标准全文…", "doc_title": "包装规范"}])
    # 发起人身份现解（resume ctx 重建用）
    import opensearch_pipeline.dingtalk_identity as di
    monkeypatch.setattr(di, "_resolve_user_identity",
                        lambda uid: {"dept": ["production"], "name": uid})
    yield store, astore, executor, monkeypatch
    from opensearch_pipeline import session_store as ss
    ss.clear_session("appr-t1", owner="u1", trusted=True)
    executor.shutdown()


def _post_approve(identity, run_id="r1", kind="approved"):
    return _client(identity).post("/api/agent/approve",
                                  json={"run_id": run_id, "outcome": {"kind": kind}})


def test_approve_self_blocked_403(approval_wired):
    """发起人自批被拒（职责分离）——深度审查 A 组「审批人=发起人」的硬修。"""
    try:
        r = _post_approve(_identity(), kind="approved")       # identity.user_id == run.user_id == u1
        assert r.status_code == 403
        assert "职责分离" in r.text
    finally:
        api.app.dependency_overrides.clear()


def test_approve_self_terminate_allowed(approval_wired):
    """发起人可撤回自己的申请（rejected_terminate）→ run cancelled + 决定持久化。"""
    store, astore, _, _ = approval_wired
    try:
        r = _post_approve(_identity(), kind="rejected_terminate")
        assert r.status_code == 200
        assert store.run["status"] == "cancelled"
        assert astore.decisions and astore.decisions[0]["decision"] == "rejected_terminate"
    finally:
        api.app.dependency_overrides.clear()


def test_approve_other_employee_403(approval_wired):
    """非发起人 + 非管理员（DB 现查 employee）→ 403。"""
    _, _, _, mp = approval_wired
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
    try:
        other = Identity(user_id="u2", acl_groups=["production"], role="kb_admin")  # 令牌 role 是提示，不可信
        r = _post_approve(other, kind="approved")
        assert r.status_code == 403
    finally:
        api.app.dependency_overrides.clear()


def test_approve_dept_admin_scope_mismatch_403(approval_wired):
    """dept_admin 但 approver_scope 不在其 managed 集合 → 403。"""
    _, _, _, mp = approval_wired
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("dept_admin", granted=("hr",)))
    try:
        r = _post_approve(Identity(user_id="u2", acl_groups=["hr"], role="employee"))
        assert r.status_code == 403
    finally:
        api.app.dependency_overrides.clear()


def test_approve_dept_admin_scope_match_executes(approval_wired):
    """dept_admin 且 scope 覆盖 → 放行；决定持久化（decided_by=审批人），run 续跑。"""
    store, astore, _, mp = approval_wired
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity",
               lambda uid: _kb_ident("dept_admin", granted=("production",)))
    try:
        r = _post_approve(Identity(user_id="u2", acl_groups=["production"], role="employee"))
        assert r.status_code == 200
        assert astore.decisions and astore.decisions[0]["decided_by"] == "u2"
        assert ("r1", "suspended", "resuming") in store.transitions
    finally:
        api.app.dependency_overrides.clear()


def test_approve_already_decided_409(approval_wired):
    """请求已被处置（非 pending 且与本次处置不同向）→ 409（迟到决策拒绝）。"""
    _, astore, _, mp = approval_wired
    astore.areq["status"] = "rejected_terminate"
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("kb_admin"))
    try:
        r = _post_approve(Identity(user_id="u2", acl_groups=["production"], role="employee"))
        assert r.status_code == 409
    finally:
        api.app.dependency_overrides.clear()


class _FakeRegistryStore:
    def __init__(self):
        self.status_calls = []
        self.rows = [{"tool_name": "u8_writeback", "version": "1.0", "spec_json": "{}",
                      "risk_level": "HIGH_WRITE", "permission_scope": "u8.writeback",
                      "owner_team": "platform", "status": "active",
                      "registered_by": "platform", "created_at": None}]

    def list_rows(self):
        return [dict(r) for r in self.rows]

    def disabled_names(self):
        return {r["tool_name"] for r in self.rows if r["status"] == "disabled"}

    def set_status(self, name, status):
        n = 0
        for r in self.rows:
            if r["tool_name"] == name:
                r["status"] = status
                n += 1
        return n

    def drift_warnings(self, registry):
        return []

    def sync_specs(self, registry):
        return len(self.rows)


def test_tools_endpoints_require_kb_admin(approval_wired):
    """工具治理端点（kill switch）：employee/dept_admin 403（DB 现查），kb_admin 放行。"""
    _, _, _, mp = approval_wired
    fake = _FakeRegistryStore()
    mp.setattr(agent_route, "_get_registry_store", lambda: fake)
    import opensearch_pipeline.dingtalk_identity as di
    try:
        mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("dept_admin", granted=("production",)))
        assert _client(_identity()).get("/api/agent/tools").status_code == 403
        mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("kb_admin"))
        r = _client(_identity()).get("/api/agent/tools")
        assert r.status_code == 200
        body = r.json()
        assert body["items"][0]["tool_name"] == "u8_writeback"
        assert "spec_json" not in body["items"][0]            # 治理视图不回灌全量 schema
        assert body["disabled"] == [] and body["drift"] == []
    finally:
        api.app.dependency_overrides.clear()


def test_tool_toggle_flips_status_and_404_on_unknown(approval_wired):
    _, _, _, mp = approval_wired
    fake = _FakeRegistryStore()
    mp.setattr(agent_route, "_get_registry_store", lambda: fake)
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("kb_admin"))
    try:
        c = _client(_identity())
        r = c.post("/api/agent/tools/toggle",
                   json={"tool_name": "u8_writeback", "disabled": True, "reason": "写回事故排查"})
        assert r.status_code == 200 and r.json()["status"] == "disabled"
        assert fake.rows[0]["status"] == "disabled"
        r = c.post("/api/agent/tools/toggle", json={"tool_name": "u8_writeback", "disabled": False})
        assert r.status_code == 200 and fake.rows[0]["status"] == "active"
        assert c.post("/api/agent/tools/toggle",
                      json={"tool_name": "nope", "disabled": True}).status_code == 404
    finally:
        api.app.dependency_overrides.clear()


def test_approvals_queue_employee_403(approval_wired):
    _, _, _, mp = approval_wired
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
    try:
        r = _client(_identity()).get("/api/agent/approvals")
        assert r.status_code == 403
    finally:
        api.app.dependency_overrides.clear()


def test_approvals_queue_scoping(approval_wired):
    """kb_admin 见全部；dept_admin 只见 scope 覆盖的；?mine=1 给发起人看自己的 pending。"""
    _, _, _, mp = approval_wired
    import opensearch_pipeline.dingtalk_identity as di
    try:
        mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("kb_admin"))
        assert len(_client(_identity()).get("/api/agent/approvals").json()["items"]) == 1
        mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("dept_admin", granted=("hr",)))
        assert _client(_identity()).get("/api/agent/approvals").json()["items"] == []
        mp.setattr(di, "resolve_kb_identity",
                   lambda uid: _kb_ident("dept_admin", granted=("production",)))
        assert len(_client(_identity()).get("/api/agent/approvals").json()["items"]) == 1
        # 发起人视角（employee 也可）：只看自己的
        mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
        assert len(_client(_identity()).get("/api/agent/approvals?mine=1").json()["items"]) == 1
        other = Identity(user_id="u9", acl_groups=["hr"], role="employee")
        assert _client(other).get("/api/agent/approvals?mine=1").json()["items"] == []
    finally:
        api.app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# P0-C 已决重放不可变性（重评报告 §5C）：重放只消费 DB decision（同向 + digest 一致 +
# decided_by/reason 以库为准）；过期在决策时刻原子拒绝。
# ─────────────────────────────────────────────────────────────────────────────
class _DecidedApprovalStore(_FakeApprovalStore):
    """已决请求 + 权威决定行（get_decision）。decide 可脚本化返回（expired 场景）。"""

    def __init__(self, areq=None, decision_row=None, decide_result="accepted"):
        super().__init__(areq)
        self.decision_row = decision_row
        self.decide_result = decide_result

    def get_decision(self, request_id):
        return dict(self.decision_row) if self.decision_row else None

    def decide(self, request_id, **kw):
        if self.decide_result != "accepted":
            return self.decide_result
        return super().decide(request_id, **kw)


def _wire_decided(mp, store, astore):
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(agent_route, "_get_approval_store", lambda: astore)
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("kb_admin"))


def test_approve_replay_edited_mismatched_args_409(approval_wired):
    """DB 批 qty=7、重放带 qty=999999 → 409（改参重放被拒）——动态探针场景的守护。"""
    store, astore, _, mp = approval_wired
    from opensearch_pipeline.agent_runtime.tool_executor import digest
    areq = _areq(); areq["status"] = "edited"
    astore2 = _DecidedApprovalStore(
        areq, decision_row={"decision": "edited", "final_args_digest": digest({"qty": 7}),
                            "decided_by": "admin9", "reason": None})
    _wire_decided(mp, store, astore2)
    try:
        r = _client(Identity(user_id="u2", acl_groups=["production"], role="employee")).post(
            "/api/agent/approve",
            json={"run_id": "r1", "outcome": {"kind": "edited",
                                              "edited_args": {"qty": 999999}}})
        assert r.status_code == 409
        assert "不一致" in r.text
        assert store.run["status"] == "suspended"          # 未被认领
    finally:
        api.app.dependency_overrides.clear()


def test_approve_replay_edited_matching_args_resumes(approval_wired):
    """重放带与 DB final_args_digest 完全一致的参数 → 放行续跑。"""
    store, astore, _, mp = approval_wired
    from opensearch_pipeline.agent_runtime.tool_executor import digest
    areq = _areq(); areq["status"] = "edited"
    astore2 = _DecidedApprovalStore(
        areq, decision_row={"decision": "edited", "final_args_digest": digest({"qty": 7}),
                            "decided_by": "admin9", "reason": None})
    _wire_decided(mp, store, astore2)
    try:
        r = _client(Identity(user_id="u2", acl_groups=["production"], role="employee")).post(
            "/api/agent/approve",
            json={"run_id": "r1", "outcome": {"kind": "edited", "edited_args": {"qty": 7}}})
        assert r.status_code == 200
        assert ("r1", "suspended", "resuming") in store.transitions
    finally:
        api.app.dependency_overrides.clear()


def test_approve_replay_missing_decision_row_409(approval_wired):
    """已决但决定行缺失（历史/故障）→ fail-closed 409，绝不按 body 语义重放。"""
    store, _astore, _, mp = approval_wired
    areq = _areq(); areq["status"] = "approved"
    astore2 = _DecidedApprovalStore(areq, decision_row=None)
    _wire_decided(mp, store, astore2)
    try:
        r = _post_approve(Identity(user_id="u2", acl_groups=["production"], role="employee"))
        assert r.status_code == 409
        assert "决定行缺失" in r.text
    finally:
        api.app.dependency_overrides.clear()


def test_approve_replay_edited_legacy_without_digest_409(approval_wired):
    """031 前的历史 edited 决定（无 final_args_digest）→ 拒绝自动重放（宁停不猜）。"""
    store, _astore, _, mp = approval_wired
    areq = _areq(); areq["status"] = "edited"
    astore2 = _DecidedApprovalStore(
        areq, decision_row={"decision": "edited", "final_args_digest": None,
                            "decided_by": "admin9", "reason": None})
    _wire_decided(mp, store, astore2)
    try:
        r = _client(Identity(user_id="u2", acl_groups=["production"], role="employee")).post(
            "/api/agent/approve",
            json={"run_id": "r1", "outcome": {"kind": "edited", "edited_args": {"qty": 7}}})
        assert r.status_code == 409
    finally:
        api.app.dependency_overrides.clear()


def test_approve_expired_pending_409(approval_wired):
    """decide 返回 DECIDE_EXPIRED（决策时刻过期原子裁决）→ 409 过期文案。"""
    store, _astore, _, mp = approval_wired
    astore2 = _DecidedApprovalStore(_areq(), decide_result="expired")
    _wire_decided(mp, store, astore2)
    try:
        r = _post_approve(Identity(user_id="u2", acl_groups=["production"], role="employee"))
        assert r.status_code == 409
        assert "过期" in r.text
    finally:
        api.app.dependency_overrides.clear()


def test_scope_covers_backup_steward_csv():
    """P1-11：approver_scope CSV（steward,backup）——backup steward 的 managed 覆盖即可审。"""
    assert agent_route._scope_covers("pmc,rd", {"rd"}) is True
    assert agent_route._scope_covers("pmc,rd", {"pmc"}) is True
    assert agent_route._scope_covers("pmc,rd", {"hr"}) is False
    assert agent_route._scope_covers("pmc", {"pmc"}) is True
    assert agent_route._scope_covers("", {"pmc"}) is False


# ─────────────────────────────────────────────────────────────────────────────
# P0-F run center + P0-E 对账端点
# ─────────────────────────────────────────────────────────────────────────────
class _RunCenterStore(_SuspendedStore):
    def __init__(self):
        super().__init__()
        self.resolved = []
        self.uncertain = [{"invocation_id": "inv_u1", "run_id": "r1", "step_no": 2,
                           "tool_name": "u8_writeback", "status": "uncertain",
                           "policy_decision": "approved", "approval_request_id": "req1",
                           "idempotency_key": "r1:c1", "args_digest": "d",
                           "error_text": "超时", "started_at": None, "ended_at": None}]

    def list_runs_by_user(self, user_id, *, limit=20):
        return [dict(self.run)] if user_id == self.run["user_id"] else []

    def list_steps(self, run_id, *, limit=200):
        return [{"step_no": 1, "kind": "model_call", "payload": {"turn_index": 0},
                 "tokens_prompt": 10, "tokens_completion": 5, "created_at": None}]

    def list_invocations(self, *, status=None, run_id=None, limit=50):
        if status == "uncertain" or run_id == "r1":
            return [dict(r) for r in self.uncertain]
        return []

    def resolve_uncertain_invocation(self, invocation_id, *, to_status, note, resolved_by):
        if any(r["invocation_id"] == invocation_id for r in self.uncertain):
            self.resolved.append((invocation_id, to_status, note, resolved_by))
            self.uncertain = [r for r in self.uncertain if r["invocation_id"] != invocation_id]
            return True
        return False


@pytest.fixture
def runcenter_wired(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setattr(agent_route, "_enforce_rate_limit", lambda *a, **k: None)
    registry = build_default_registry()
    store = _RunCenterStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _FakeProvider()}, routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime", lambda: (registry, gateway, executor, store))
    monkeypatch.setattr(agent_route, "_get_approval_store", lambda: _FakeApprovalStore(_areq()))
    yield store, monkeypatch
    executor.shutdown()


def test_run_center_list_and_detail_owner(runcenter_wired):
    store, mp = runcenter_wired
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
    try:
        c = _client(_identity())                            # u1 = run 归属人
        runs = c.get("/api/agent/runs").json()["items"]
        assert runs and runs[0]["run_id"] == "r1"
        detail = c.get("/api/agent/runs/r1")
        assert detail.status_code == 200
        body = detail.json()
        assert body["run"]["run_id"] == "r1"
        assert body["steps"] and body["steps"][0]["kind"] == "model_call"
        assert body["invocations"] and body["invocations"][0]["status"] == "uncertain"
        assert body["approval"]["request_id"] == "req1"     # 等谁审/有效期指向
    finally:
        api.app.dependency_overrides.clear()


def test_run_center_other_user_404_kb_admin_ok(runcenter_wired):
    """他人 run 一律 404（不可见==不存在）；kb_admin 可见（治理面）。"""
    _store, mp = runcenter_wired
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
    try:
        other = Identity(user_id="u9", acl_groups=["hr"], role="employee")
        assert _client(other).get("/api/agent/runs/r1").status_code == 404
        assert _client(other).get("/api/agent/runs").json()["items"] == []
        api.app.dependency_overrides.clear()
        mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("kb_admin"))
        admin = Identity(user_id="admin", acl_groups=["public"], role="employee")
        assert _client(admin).get("/api/agent/runs/r1").status_code == 200
    finally:
        api.app.dependency_overrides.clear()


def test_invocation_reconcile_kb_admin_gate_and_resolution(runcenter_wired):
    store, mp = runcenter_wired
    import opensearch_pipeline.dingtalk_identity as di
    mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("employee"))
    try:
        # 非 kb_admin 403
        r = _client(_identity()).get("/api/agent/invocations")
        assert r.status_code == 403
        api.app.dependency_overrides.clear()
        mp.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("kb_admin"))
        admin = Identity(user_id="admin", acl_groups=["public"], role="employee")
        c = _client(admin)
        items = c.get("/api/agent/invocations").json()["items"]
        assert items and items[0]["status"] == "uncertain"
        # note 必填
        assert c.post("/api/agent/invocations/resolve",
                      json={"invocation_id": "inv_u1", "resolution": "confirmed_failed",
                            "note": " "}).status_code == 422
        r = c.post("/api/agent/invocations/resolve",
                   json={"invocation_id": "inv_u1", "resolution": "confirmed_failed",
                         "note": "U8 侧核实未生成单据"})
        assert r.status_code == 200 and r.json()["status"] == "failed"
        assert store.resolved[0][:2] == ("inv_u1", "failed")
        # 已处置再来 → 409
        assert c.post("/api/agent/invocations/resolve",
                      json={"invocation_id": "inv_u1", "resolution": "confirmed_failed",
                            "note": "再次"}).status_code == 409
    finally:
        api.app.dependency_overrides.clear()
