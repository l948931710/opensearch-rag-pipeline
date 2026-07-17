# -*- coding: utf-8 -*-
"""
test_agent_batch3_fixes.py — 外审核查修复批次3（2026-07-17）回归

台账 docs/audits/enterprise_agent_review_verification_2026-07-16.md 批次3：
- P1-05 预算 pre-call 闸：调用前判「种子+段内已耗+本轮 prompt 估算 ≥ token_budget」→
  BudgetExceeded（费用未发生）；临近耗尽给 max_tokens 设余额上限（带 FLOOR）；
  resume 预算读取异常 fail-closed（回滚 suspended 可重试），合法全零快照维持回退。
- P1-06 审批续跑 provenance 并集：completion 事务同游标把本 run 全部成功检索回执的
  doc_ids 并进 retrieved_docs（挂起前段来源不再丢失）。
- P1-07 已受理审批命令的 HTTP 重试幂等回放（同键/同向 → 202 回放，不再恒 409）。
- P1-08 Approved/Edited 续跑身份 fail-closed（解析异常=可重试 503；墓碑停用=终局 403）
  + user_role is_active=0 墓碑令牌立即失效（api 层 strict 分支）。
"""
import threading

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import Usage
from opensearch_pipeline.agent_runtime.executor import RunRejected, ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop, ModelTurn
from opensearch_pipeline.agent_runtime.model_gateway import (
    BudgetExceeded,
    ChatResponse,
    make_model_fn,
)
from opensearch_pipeline.agent_runtime.tool import ToolResult


def _ctx(token_budget=200_000):
    return ExecutionContext.create(
        request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
        channel="console", thread_id="t",
        budget=RunBudget(max_turns=8, token_budget=token_budget))


class _FakeGateway:
    """记录请求、按脚本回响应的假网关（无 complete_stream → make_model_fn 走同步路径）。"""

    def __init__(self, usage_total=10):
        self.requests = []
        self._usage_total = usage_total

    def complete(self, ctx, category, req):
        self.requests.append(req)
        return ChatResponse(text="答", tool_calls=[],
                            usage=Usage(tokens_prompt=self._usage_total, tokens_completion=0),
                            model="m")


# ── P1-05：pre-call 预算闸 ──────────────────────────────────────────────────


def test_p1_05_precall_blocks_before_dispatch():
    """巨 prompt 越过预算 → 调用前 BudgetExceeded，provider 一次都不该被调（费用未发生）。"""
    gw = _FakeGateway()
    fn = make_model_fn(gw, _ctx(token_budget=100), stream=False, charge_llm=False)
    big = [{"role": "user", "content": "字" * 4000}]        # est ~1000 tokens ≥ 100
    with pytest.raises(BudgetExceeded, match="调用前拦截"):
        fn(big, [])
    assert gw.requests == []                                # 未发出任何调用


def test_p1_05_precall_accumulates_segment_usage():
    """段内累计：每轮 usage 计入闸门——600+600 后第三轮必拦（1200 ≥ 1000）。"""
    gw = _FakeGateway(usage_total=600)
    fn = make_model_fn(gw, _ctx(token_budget=1000), stream=False, charge_llm=False)
    msgs = [{"role": "user", "content": "q"}]
    fn(msgs, [])                                            # used=600
    try:
        fn(msgs, [])                                        # 600 < 1000 → 放行，used=1200
        third_blocked = False
        try:
            fn(msgs, [])
        except BudgetExceeded:
            third_blocked = True
        assert third_blocked
        assert len(gw.requests) == 2
    except BudgetExceeded:
        # est>0 的实现容差：第二轮已拦也满足「不无限越过预算」的不变量
        assert len(gw.requests) == 1


def test_p1_05_seed_counts_prior_segment():
    """resume 段种子（durable tokens_used）计入闸门——续跑段不从零起数。"""
    gw = _FakeGateway()
    fn = make_model_fn(gw, _ctx(token_budget=1000), stream=False, charge_llm=False,
                       tokens_used_seed=999)
    with pytest.raises(BudgetExceeded):
        fn([{"role": "user", "content": "字" * 400}], [])    # 999 + ~100 ≥ 1000
    assert gw.requests == []


def test_p1_05_near_exhaustion_caps_max_tokens():
    """临近耗尽（余额 < 16384）→ 请求体带 max_tokens=max(FLOOR, remaining)；
    正常余额维持 None（行为零变化）。"""
    gw = _FakeGateway()
    fn = make_model_fn(gw, _ctx(token_budget=10_000), stream=False, charge_llm=False)
    fn([{"role": "user", "content": "q"}], [])
    assert gw.requests[0].max_tokens is not None
    assert 1024 <= gw.requests[0].max_tokens <= 10_000

    gw2 = _FakeGateway()
    fn2 = make_model_fn(gw2, _ctx(token_budget=200_000), stream=False, charge_llm=False)
    fn2([{"role": "user", "content": "q"}], [])
    assert gw2.requests[0].max_tokens is None               # 正常余额不动请求体


class _ResumeStore:
    """resume 预算测试桩：transition 全 True，checkpoint 无。"""

    def __init__(self, consume_raises=False):
        self.transitions = []
        self._raises = consume_raises

    def create_run(self, ctx, profile):
        return "run1"

    def append_step(self, run_id, step):
        return 1

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        return True

    def consume_budget(self, run_id, *, turns=0, tool_calls=0, tokens=0):
        if self._raises:
            raise RuntimeError("db blip on budget read")
        return {}

    def load_latest_checkpoint(self, run_id):
        return None


def test_p1_05_resume_budget_read_failure_fail_closed():
    """预算读取异常 → RunRejected + 回滚 suspended（绝不按零播种续跑/绝不打成 failed）。"""
    from opensearch_pipeline.agent_runtime.approval import Approved

    store = _ResumeStore(consume_raises=True)
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    with pytest.raises(RunRejected, match="预算读取失败"):
        ex.resume("run9", _ctx(), Approved(),
                  DefaultAgentLoop(lambda m, t: ModelTurn(text="x")), [])
    assert ("run9", "resuming", "suspended") in store.transitions   # 回边可重试
    assert ("run9", "resuming", "running") not in store.transitions  # 未接手
    assert ex.active_count() == 0


def test_p1_05_resume_zero_snapshot_keeps_fallback():
    """合法全零快照维持回退（首段 increment fail-open 被吞时零值是真实库况）。"""
    from opensearch_pipeline.agent_runtime.approval import Approved

    store = _ResumeStore(consume_raises=False)
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.resume("run9", _ctx(), Approved(),
                       DefaultAgentLoop(lambda m, t: ModelTurn(text="x")), [])
    list(handle.events())
    ex.shutdown()
    assert ("run9", "resuming", "running") in store.transitions
    assert ("run9", "running", "succeeded") in store.transitions


# ── P1-06：provenance 并集 ──────────────────────────────────────────────────


class _ReceiptCursor:
    def __init__(self, rows, raises=False):
        self._rows = rows
        self._raises = raises
        self.sql = None

    def execute(self, sql, params=None):
        if self._raises:
            raise RuntimeError("cursor down")
        self.sql = sql

    def fetchall(self):
        return self._rows


def test_p1_06_union_receipt_doc_ids():
    from opensearch_pipeline.routes.agent import _union_invocation_doc_ids

    cur = _ReceiptCursor([('{"doc_ids": ["D1", "D2"]}',), ('{"doc_ids": ["D2", "D3"]}',),
                          ('{"no_docs": 1}',), ("not-json",)])
    docs = [{"doc_id": "D1", "chunk_id": "c1", "chunk_text": "原段内容"}]
    merged = _union_invocation_doc_ids(cur, "run1", docs)
    ids = [d.get("doc_id") for d in merged]
    assert ids == ["D1", "D2", "D3"]                        # 段内在前，回执补差
    assert merged[0].get("chunk_text") == "原段内容"          # 段内条目原样保留
    assert merged[1].get("source") == "tool_receipt"
    assert "tool_invocation" in cur.sql and "succeeded" in cur.sql


def test_p1_06_union_fail_open_keeps_segment_docs():
    from opensearch_pipeline.routes.agent import _union_invocation_doc_ids

    docs = [{"doc_id": "D1"}]
    assert _union_invocation_doc_ids(_ReceiptCursor([], raises=True), "run1", docs) == docs
    assert _union_invocation_doc_ids(None, "run1", docs) == docs      # 桩路径（cur=None）
    assert _union_invocation_doc_ids(_ReceiptCursor([]), None, docs) == docs


def test_p1_06_persist_answer_wires_union(monkeypatch):
    """_resume_callbacks 的事务写路径把并集结果传给 insert_qa_row_tx。"""
    from opensearch_pipeline.routes import agent as agent_routes

    captured = {}
    monkeypatch.setattr("opensearch_pipeline.qa_logger.insert_qa_row_tx",
                        lambda cur, **kw: captured.update(kw))

    class _RS:
        def load_latest_checkpoint(self, run_id):
            return None

    run = {"run_id": "run1", "message_id": "m1", "conversation_id": None}
    _mid, _remember, _fail, persist = agent_routes._resume_callbacks(
        _RS(), run, "t1", "u1", ["g"])
    cur = _ReceiptCursor([('{"doc_ids": ["D9"]}',)])
    persist(cur, "最终答案", retrieved=[{"chunks": [{"doc_id": "D1", "chunk_id": "c1"}],
                                        "included": [{"doc_id": "D1", "chunk_id": "c1"}]}])
    ids = [d.get("doc_id") for d in captured["retrieved_docs"]]
    assert "D1" in ids and "D9" in ids                      # 段内 + 挂起前段回执


# ── P1-07：审批命令幂等回放 ─────────────────────────────────────────────────


def _mk_req(run_id="run1", kind="approved", idem=None):
    from opensearch_pipeline.routes.agent import ApproveRequest
    return ApproveRequest(run_id=run_id, outcome={"kind": kind}, idempotency_key=idem)


class _ApprovalStoreStub:
    def __init__(self, decision, raises=False):
        self._dec = decision
        self._raises = raises

    def get_latest_by_run(self, run_id):
        if self._raises:
            raise RuntimeError("store down")
        return {"request_id": "req1"} if self._dec is not None else None

    def get_decision(self, request_id):
        return self._dec


def _patch_store(monkeypatch, store):
    monkeypatch.setattr("opensearch_pipeline.routes.agent._get_approval_store",
                        lambda: store)


def test_p1_07_same_key_replays_202(monkeypatch):
    from opensearch_pipeline.routes.agent import _replay_decided_response

    _patch_store(monkeypatch, _ApprovalStoreStub(
        {"decision": "approved", "idempotency_key": "ik1"}))
    resp = _replay_decided_response(_mk_req(kind="rejected_feedback", idem="ik1"),
                                    {"status": "running"})
    assert resp is not None and resp.status_code == 202     # 同键即回放（键优先于方向）
    import json as _json
    body = _json.loads(resp.body)
    assert body["replayed"] is True and body["outcome"] == "approved"


def test_p1_07_same_direction_replays_202(monkeypatch):
    from opensearch_pipeline.routes.agent import _replay_decided_response

    _patch_store(monkeypatch, _ApprovalStoreStub(
        {"decision": "rejected_terminate", "idempotency_key": "other"}))
    resp = _replay_decided_response(_mk_req(kind="rejected_terminate"),
                                    {"status": "cancelled"})
    assert resp is not None and resp.status_code == 202
    import json as _json
    assert _json.loads(resp.body)["status"] == "cancelled"


def test_p1_07_mismatch_returns_none_keeps_409(monkeypatch):
    from opensearch_pipeline.routes.agent import _replay_decided_response

    _patch_store(monkeypatch, _ApprovalStoreStub(
        {"decision": "approved", "idempotency_key": "ik1"}))
    assert _replay_decided_response(_mk_req(kind="rejected_terminate", idem="ik2"),
                                    {"status": "running"}) is None


def test_p1_07_store_failure_returns_none(monkeypatch):
    from opensearch_pipeline.routes.agent import _replay_decided_response

    _patch_store(monkeypatch, _ApprovalStoreStub(None, raises=True))
    assert _replay_decided_response(_mk_req(idem="ik1"), {"status": "running"}) is None


# ── P1-08：resume 身份 fail-closed + 墓碑 ──────────────────────────────────


def test_p1_08_requester_ctx_exception_fail_closed(monkeypatch):
    from opensearch_pipeline.routes.agent import IdentityUnresolvable, _requester_ctx

    def boom(uid):
        raise RuntimeError("dingtalk api down")

    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity._resolve_user_identity", boom)
    run = {"user_id": "U1", "channel": "console"}
    with pytest.raises(IdentityUnresolvable) as ei:
        _requester_ctx(run, "t1", fail_closed_on_error=True)
    assert ei.value.retryable is True
    ctx, _, groups = _requester_ctx(run, "t1", fail_closed_on_error=False)   # 只读向不变
    assert groups == []


def test_p1_08_requester_ctx_tombstone_terminal(monkeypatch):
    from opensearch_pipeline.routes.agent import IdentityUnresolvable, _requester_ctx

    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity._resolve_user_identity",
                        lambda uid: {})

    class _Kb:
        role = "employee"

    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity.resolve_kb_identity",
                        lambda uid: _Kb())
    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity.user_row_revoked",
                        lambda uid: True)
    with pytest.raises(IdentityUnresolvable) as ei:
        _requester_ctx({"user_id": "U1", "channel": "console"}, "t1",
                       fail_closed_on_error=True)
    assert ei.value.retryable is False                      # 墓碑=终局，不重试

    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity.user_row_revoked",
                        lambda uid: False)
    ctx, _, _ = _requester_ctx({"user_id": "U1", "channel": "console"}, "t1",
                               fail_closed_on_error=True)   # 未停用照常
    assert ctx.user_id == "U1"


class _RevokeConn:
    def __init__(self, max_active):
        self._v = max_active

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return (conn._v,)

        return _Cur()

    def close(self):
        pass


def test_p1_08_user_row_revoked_semantics(monkeypatch):
    from opensearch_pipeline.dingtalk_identity import user_row_revoked

    owner = threading.get_ident()

    def _factory(v):
        def _get():
            assert threading.get_ident() == owner
            return _RevokeConn(v)
        return _get

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _factory(None))
    assert user_row_revoked("U1") is False                  # 无行=未缓存 ≠ 停用
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _factory(0))
    assert user_row_revoked("U1") is True                   # 有行且全停用=墓碑
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _factory(1))
    assert user_row_revoked("U1") is False                  # 有活跃行
    assert user_row_revoked("") is False                    # 空 uid/群聊无账号概念
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert user_row_revoked("U1") is False                  # 读失败 fail-open 到 TTL 语义


def test_p1_08_api_identity_rejects_revoked(monkeypatch):
    """strict + 无活跃行 + 墓碑 → 令牌立即失效（401 语义：current_identity 返回 None）；
    真无行维持保留令牌组（文档化语义不动，test_main_p0_hardening 原测继续守卫）。"""
    from opensearch_pipeline import api
    from opensearch_pipeline.api import issue_session_token

    monkeypatch.setenv("RAG_ACL_FAIL_CLOSED", "true")
    monkeypatch.setenv("RAG_LIVE_ACL_REREAD", "true")
    monkeypatch.setattr(
        "opensearch_pipeline.dingtalk_identity._resolve_user_dept_cached",
        lambda uid, with_status=False: (None, True) if with_status else None)
    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity.user_row_revoked",
                        lambda uid: True)
    token = issue_session_token("U1", dept="行政部", name="测试")
    assert api.current_identity(authorization="Bearer " + token) is None

    monkeypatch.setattr("opensearch_pipeline.dingtalk_identity.user_row_revoked",
                        lambda uid: False)
    ident = api.current_identity(authorization="Bearer " + token)
    assert ident is not None and "行政部" in ident.acl_groups
