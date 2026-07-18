# -*- coding: utf-8 -*-
"""
test_agent_runtime_hardening.py — 重评报告 P0-E + P1 修复的单元守护（2026-07-11）

覆盖（对应重评报告条目）：
- P1「一轮多 tool call 挂起丢调用」：多 call 批命中审批 → RunSuspended 携带 remaining_calls，
  resume 处置 pending 后续处理剩余 calls，**每个 call_id 都有 tool 消息**（OpenAI 消息序合法）。
- P1「checkpoint digest 不验证」：executor.resume 前核 sha256——被篡改的 checkpoint 拒绝续跑，
  run 回滚 suspended。
- P0-E「超时/僵尸不是可恢复执行」：有副作用工具超时 → invocation **uncertain**（非 failed）；
  uncertain/在跑/僵尸残行阻断同键重试；failed 残行 CAS 回收重试。
- P1「obligations 只收集不执行」：未注册义务 fail-closed 拒绝；limit_rows/mask_output 真生效；
  义务执行器异常 → 输出扣留。
- P1「output schema 不校验」：违约读工具 → failed；违约写工具 → uncertain（副作用已发生）。
- P1「工具可见集未按 policy 收敛」：would_grant + registry.attach_visibility_filter（含 fail-open）。
"""
import hashlib
import time
from types import SimpleNamespace

from opensearch_pipeline.agent_runtime.approval import Approved, RejectedFeedback
from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import (
    RunCompleted,
    RunSuspended,
    ToolCallProposed,
)
from opensearch_pipeline.agent_runtime.loop import (
    DefaultAgentLoop,
    ModelTurn,
    ProposedCall,
    decode_checkpoint_state,
    encode_checkpoint,
)
from opensearch_pipeline.agent_runtime.policy import PolicyEngine, PolicyRule
from opensearch_pipeline.agent_runtime.registry import ToolRegistry
from opensearch_pipeline.agent_runtime.tool import (
    ContentBlock,
    RiskLevel,
    ToolResult,
    ToolSpec,
)
from opensearch_pipeline.agent_runtime.tool_executor import ToolExecutor

# ═════════════════ 公共件 ═════════════════


def _ctx(max_turns=8):
    return ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"],
                                   roles=["employee"], channel="console", thread_id="t",
                                   budget=RunBudget(max_turns=max_turns))


def _scripted(turns):
    box = {"i": 0}

    def _fn(msgs, tools):
        t = turns[box["i"]]
        box["i"] += 1
        return t

    return _fn


class _InvStore:
    """tool_invocation 侧假 store（支持 P0-E 状态机方法）。"""

    def __init__(self, prior=None):
        self.rows = {}
        self.prior = prior            # find_invocation_by_key 的返回
        self.reclaim_ok = True
        self.marked_uncertain = []
        self._n = 0

    def record_invocation(self, run_id, step_no, **kw):
        self._n += 1
        iid = f"inv{self._n}"
        self.rows[iid] = {"status": kw.get("status"), **kw}
        return iid

    def finish_invocation(self, invocation_id, **kw):
        self.rows.setdefault(invocation_id, {}).update(kw)

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return None

    def find_invocation_by_key(self, tool_name, idempotency_key):
        return self.prior

    def reclaim_failed_invocation(self, invocation_id):
        if self.reclaim_ok:
            self.rows[invocation_id] = {"status": "executing", "reclaimed": True}
            return True
        return False

    def mark_invocation_uncertain(self, invocation_id, note=""):
        self.marked_uncertain.append((invocation_id, note))
        return True


def _write_spec(**kw):
    base = dict(name="t_write", version="1.0", description="测试写工具",
                input_schema={"type": "object"}, output_schema={"type": "object"},
                risk_level=RiskLevel.LOW_WRITE, permission_scope="t.write",
                data_classification="internal", idempotency="key_required",
                side_effects=True, timeout_s=5.0)
    base.update(kw)
    return ToolSpec(**base)


class _Tool:
    def __init__(self, spec, fn):
        self.spec = spec
        self._fn = fn

    def run(self, ctx, args, idempotency_key=None):
        return self._fn(ctx, args)


# ═════════════════ P1-3 多 call 挂起不丢调用 ═════════════════


def _drive_collect(gen, results_by_tool):
    """驱动生成器：按工具名回注脚本结果，收集全部事件。"""
    events = []
    try:
        ev = next(gen)
        while True:
            events.append(ev)
            if isinstance(ev, ToolCallProposed):
                ev = gen.send(results_by_tool(ev))
            else:
                ev = next(gen)
    except StopIteration:
        pass
    return events


def test_multicall_suspend_carries_remaining_calls():
    """3 call 批：read 执行 → write 挂起 → RunSuspended.remaining_calls=[第三个]；
    messages 含 read 的 tool 消息（挂起前已执行的不丢）。"""
    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[
            ProposedCall(call_id="c1", tool_name="read_a", arguments={"q": "1"}),
            ProposedCall(call_id="c2", tool_name="write_b", arguments={"v": 2}),
            ProposedCall(call_id="c3", tool_name="read_c", arguments={"q": "3"}),
        ]),
    ]))

    def _adjudicate(ev):
        return ToolResult.pending() if ev.tool_name == "write_b" else ToolResult.text_ok("ok")

    events = _drive_collect(loop.run(_ctx(), [{"role": "user", "content": "q"}], []),
                            _adjudicate)
    sus = [e for e in events if isinstance(e, RunSuspended)]
    assert len(sus) == 1
    assert sus[0].pending_call["call_id"] == "c2"
    assert [c["call_id"] for c in sus[0].remaining_calls] == ["c3"]
    tool_msgs = [m for m in sus[0].state_messages if m.get("role") == "tool"]
    assert [m["call_id"] for m in tool_msgs] == ["c1"]     # 挂起前已执行的有响应


def test_multicall_resume_processes_remaining_every_call_answered():
    """resume(Approved)：pending 执行 + remaining 续处理 + 下一轮完成——最终消息序里
    c1/c2/c3 **全部**有 tool 消息（OpenAI「每 tool_call 必有响应」）。"""
    state = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "tool_calls": [
                {"call_id": "c1", "name": "read_a", "arguments": {}},
                {"call_id": "c2", "name": "write_b", "arguments": {"v": 2}},
                {"call_id": "c3", "name": "read_c", "arguments": {}},
            ]},
            {"role": "tool", "call_id": "c1", "content": "ok"},
        ],
        "pending_call": {"call_id": "c2", "tool_name": "write_b", "arguments": {"v": 2}},
        "remaining_calls": [{"call_id": "c3", "tool_name": "read_c", "arguments": {}}],
        "turn": 0,
    }
    final_msgs = {}
    loop = DefaultAgentLoop(lambda msgs, tools: (final_msgs.setdefault("msgs", list(msgs)),
                                                 ModelTurn(text="完成"))[1])
    proposed = []

    def _adjudicate(ev):
        proposed.append(ev.call_id)
        return ToolResult.text_ok(f"r-{ev.call_id}")

    events = _drive_collect(loop.resume(_ctx(), state, Approved(), []), _adjudicate)
    assert proposed == ["c2", "c3"]                         # pending 先执行，remaining 续上
    assert isinstance(events[-1], RunCompleted)
    answered = [m["call_id"] for m in final_msgs["msgs"] if m.get("role") == "tool"]
    assert answered == ["c1", "c2", "c3"]                   # 每个 call 都有 tool 响应


def test_multicall_resume_rejected_feedback_still_processes_remaining():
    """拒绝反馈：pending 得 [审批未通过] tool 消息，remaining 照常独立处理。"""
    state = {
        "messages": [{"role": "user", "content": "q"},
                     {"role": "assistant", "tool_calls": [
                         {"call_id": "c2", "name": "write_b", "arguments": {}},
                         {"call_id": "c3", "name": "read_c", "arguments": {}}]}],
        "pending_call": {"call_id": "c2", "tool_name": "write_b", "arguments": {}},
        "remaining_calls": [{"call_id": "c3", "tool_name": "read_c", "arguments": {}}],
        "turn": 0,
    }
    final_msgs = {}
    loop = DefaultAgentLoop(lambda msgs, tools: (final_msgs.setdefault("msgs", list(msgs)),
                                                 ModelTurn(text="完成"))[1])
    proposed = []
    events = _drive_collect(
        loop.resume(_ctx(), state, RejectedFeedback(reason="不允许"), []),
        lambda ev: (proposed.append(ev.call_id), ToolResult.text_ok("ok"))[1])
    assert proposed == ["c3"]                               # 被拒的不执行，剩余照常
    msgs = final_msgs["msgs"]
    c2_msg = next(m for m in msgs if m.get("role") == "tool" and m.get("call_id") == "c2")
    assert "审批未通过" in c2_msg["content"]
    assert any(m.get("role") == "tool" and m.get("call_id") == "c3" for m in msgs)
    assert isinstance(events[-1], RunCompleted)


def test_checkpoint_roundtrip_with_remaining_calls(monkeypatch):
    # 钉【无密钥】legacy sha256 路径（HMAC 路径在 test_agent_checkpoint_security 钉）
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_KEY", raising=False)
    monkeypatch.delenv("RAG_SESSION_SIGNING_KEY", raising=False)
    remaining = [{"call_id": "c3", "tool_name": "read_c", "arguments": {"q": 1}}]
    blob, digest = encode_checkpoint([{"role": "user", "content": "q"}],
                                     pending_call={"call_id": "c2", "tool_name": "w",
                                                   "arguments": {}},
                                     turn=1, remaining_calls=remaining)
    state = decode_checkpoint_state(blob)
    assert state["remaining_calls"] == remaining
    assert hashlib.sha256(blob).hexdigest() == digest
    # 旧 blob（无 remaining_calls 键）→ 空列表（形状兼容）
    old_blob, _ = encode_checkpoint([], pending_call=None, turn=0)
    import json
    d = json.loads(old_blob)
    d.pop("remaining_calls")
    legacy = json.dumps(d, ensure_ascii=False).encode("utf-8")
    assert decode_checkpoint_state(legacy)["remaining_calls"] == []


# ═════════════════ P1-2 checkpoint digest 校验 ═════════════════


def test_resume_rejects_tampered_checkpoint_and_rolls_back():
    """digest 不匹配 → RunRejected；run 回滚 resuming→suspended（可对账/过期，不带毒续跑）。"""
    import pytest

    from opensearch_pipeline.agent_runtime.executor import RunRejected, ThreadedRunExecutor

    blob, digest = encode_checkpoint([{"role": "user", "content": "q"}],
                                     pending_call={"call_id": "c", "tool_name": "w",
                                                   "arguments": {}}, turn=0)
    tampered = blob.replace(b'"q"', b'"HACKED"')            # 内容改、digest 未改

    class _Store:
        def __init__(self):
            self.transitions = []

        def transition(self, run_id, frm, to):
            self.transitions.append((frm, to))
            return True

        def load_latest_checkpoint(self, run_id):
            return SimpleNamespace(checkpoint_id="cp1", state_blob=tampered,
                                   state_digest=digest)

    store = _Store()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"), max_concurrent=2)
    with pytest.raises(RunRejected, match="完整性"):
        ex.resume("run1", _ctx(), Approved(), DefaultAgentLoop(lambda m, t: ModelTurn(text="a")), [])
    assert ("resuming", "suspended") in store.transitions   # 回边保住可重试
    ex.shutdown(wait=False)


# ═════════════════ P0-B grant 必须锚定已落库的 approval_decision ═════════════════


class _ResumeStore:
    """resume 路径最小假 run_store：带 pending_call 的 checkpoint + 状态机记录。"""

    def __init__(self, args=None):
        self.transitions = []
        blob, digest = encode_checkpoint(
            [{"role": "user", "content": "q"}],
            pending_call={"call_id": "c1", "tool_name": "w", "arguments": args or {"qty": 7}},
            turn=0)
        self._cp = SimpleNamespace(checkpoint_id="cp1", state_blob=blob, state_digest=digest)

    def transition(self, run_id, frm, to):
        self.transitions.append((frm, to))
        return True

    def load_latest_checkpoint(self, run_id):
        return self._cp

    def consume_budget(self, run_id, **kw):
        return {}


class _DecisionStore:
    """approval_store 假件：get_latest_by_run / get_decision 可脚本化，读可抛。"""

    def __init__(self, decision=None, request_id="req1", raises=False):
        self.decision = decision
        self.request_id = request_id
        self.raises = raises

    def get_latest_by_run(self, run_id):
        if self.raises:
            raise RuntimeError("db down")
        return {"request_id": self.request_id} if self.request_id else None

    def get_decision(self, request_id):
        if self.raises:
            raise RuntimeError("db down")
        return dict(self.decision) if self.decision else None


def _resume_with(astore, args=None, outcome=None):
    import pytest  # noqa: F401

    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
    store = _ResumeStore(args=args)
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"),
                             max_concurrent=2, approval_store=astore)
    try:
        ex.resume("run1", _ctx(), outcome or Approved(),
                  DefaultAgentLoop(lambda m, t: ModelTurn(text="a")), [])
    finally:
        ex.shutdown(wait=False)
    return store


def test_grant_requires_persisted_decision_missing_row_rejected():
    """接了 approval_store 但库里无 decision 行 → RunRejected + 回滚 suspended
    （堵「读失败/缺行时 kb_admin 一批准即产生无落库决策支撑的 grant」）。"""
    import pytest

    from opensearch_pipeline.agent_runtime.executor import RunRejected
    with pytest.raises(RunRejected, match="决定行"):
        _resume_with(_DecisionStore(decision=None))


def test_grant_requires_persisted_decision_read_failure_rejected():
    import pytest

    from opensearch_pipeline.agent_runtime.executor import RunRejected
    with pytest.raises(RunRejected, match="fail-closed"):
        _resume_with(_DecisionStore(raises=True))


def test_grant_rejects_direction_mismatch_and_digest_mismatch():
    import pytest

    from opensearch_pipeline.agent_runtime.executor import RunRejected
    from opensearch_pipeline.agent_runtime.tool_executor import digest as _digest
    # 方向不符：库=rejected_feedback，请求=approved
    with pytest.raises(RunRejected, match="方向不符"):
        _resume_with(_DecisionStore(decision={"decision": "rejected_feedback",
                                              "final_args_digest": _digest({"qty": 7})}))
    # 摘要不符：库批的是 qty=7，checkpoint pending 参数被换成 qty=999
    with pytest.raises(RunRejected, match="不一致"):
        _resume_with(_DecisionStore(decision={"decision": "approved",
                                              "final_args_digest": _digest({"qty": 7})}),
                     args={"qty": 999})
    # 031 前历史行（无 final_args_digest）：宁停不猜
    with pytest.raises(RunRejected, match="schema/031"):
        _resume_with(_DecisionStore(decision={"decision": "approved",
                                              "final_args_digest": None}))


def test_grant_with_matching_persisted_decision_binds_decision_id():
    """同向 + 摘要一致 → 放行；grant 绑 decision_id/decided_by（库内权威行）。"""
    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
    from opensearch_pipeline.agent_runtime.tool_executor import digest as _digest
    astore = _DecisionStore(decision={"decision": "approved", "decision_id": "dec9",
                                      "final_args_digest": _digest({"qty": 7}),
                                      "decided_by": "admin9", "request_id": "req1"})
    store = _ResumeStore()
    approvals = {}
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"),
                             max_concurrent=2, approvals=approvals, approval_store=astore)
    try:
        h = ex.resume("run1", _ctx(), Approved(),
                      DefaultAgentLoop(lambda m, t: ModelTurn(text="a")), [])
        list(h.events())                                    # 排空（后台驱动完成）
    finally:
        ex.shutdown(wait=True)
    # grant 在 adjudicator 未消费（本测试 adjudicator 直接放行）→ 仍在 approvals 里可断言
    grant = approvals.get("run1:c1")
    assert grant is not None
    assert grant.decision_id == "dec9" and grant.decided_by == "admin9"
    assert ("resuming", "running") in store.transitions


# ═════════════════ P0-A sources 随完成侧回调落库 ═════════════════


def test_on_complete_receives_retrieved_chunks_two_arg_callback():
    """executor 收集工具 artifacts.chunks → RunCompleted 时交给能收第二参的 on_complete
    （P0-A：审批续跑无 SSE 消费者，sources 不随完成侧落库即彻底丢失）；单参回调照旧。"""
    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor

    chunks = [{"chunk_id": "ck1", "doc_id": "D1", "chunk_text": "内容"}]

    def _adjudicate(ctx, ev):
        r = ToolResult.text_ok("找到 1 条")
        r.artifacts = {"chunks": chunks, "included": chunks}
        return r

    class _Store:
        def transition(self, run_id, frm, to):
            return True

        def consume_budget(self, run_id, **kw):
            return {}

    turns = [ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="kb",
                                                arguments={"q": "x"})]),
             ModelTurn(text="答案")]
    got = {}

    def _on_complete(final_text, retrieved=None):
        got["text"], got["retrieved"] = final_text, retrieved

    ex = ThreadedRunExecutor(_Store(), _adjudicate, max_concurrent=2)
    # 假 store 无 create_run：直接驱动 submit 需要 create_run——补上
    _Store.create_run = lambda self, ctx, profile: "run-src"
    _Store.append_step = lambda self, run_id, step: 1
    h = ex.submit(_ctx(), DefaultAgentLoop(_scripted(turns)),
                  [{"role": "user", "content": "q"}], [], on_complete=_on_complete)
    list(h.events())
    ex.shutdown(wait=True)
    assert got["text"] == "答案"
    assert got["retrieved"] and got["retrieved"][0]["included"][0]["chunk_id"] == "ck1"

    # 单参回调（既有契约）：不炸、正常收文本
    got2 = {}
    ex2 = ThreadedRunExecutor(_Store(), _adjudicate, max_concurrent=2)
    h2 = ex2.submit(_ctx(), DefaultAgentLoop(_scripted(list(turns))),
                    [{"role": "user", "content": "q"}], [],
                    on_complete=lambda text: got2.setdefault("text", text))
    list(h2.events())
    ex2.shutdown(wait=True)
    assert got2["text"] == "答案"


# ═════════════════ P0-E uncertain 状态机 ═════════════════


def test_timeout_side_effect_tool_marks_uncertain():
    """有副作用工具超时 → invocation=uncertain + 结果文案含「待对账」，绝不谎报纯 failed。"""
    store = _InvStore()
    ex = ToolExecutor(store)
    spec = _write_spec(timeout_s=0.05)
    tool = _Tool(spec, lambda c, a: time.sleep(2))
    res = ex.execute(_ctx(), tool, {"v": 1}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", idempotency_key="k1")
    assert res.status == "failed" and "不确定" in (res.error or "")
    inv = list(store.rows.values())[0]
    assert inv["status"] == "uncertain"


def test_timeout_readonly_tool_stays_failed():
    """无副作用（READ_ONLY）超时维持 failed——不污染对账队列。"""
    store = _InvStore()
    ex = ToolExecutor(store)
    spec = _write_spec(name="t_read", risk_level=RiskLevel.READ_ONLY, idempotency="none",
                       side_effects=False, timeout_s=0.05)
    tool = _Tool(spec, lambda c, a: time.sleep(2))
    res = ex.execute(_ctx(), tool, {}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p")
    assert res.status == "failed"
    from opensearch_pipeline.agent_runtime.tool_executor import drain_read_trace
    assert drain_read_trace()                       # READ_ONLY trace 异步：断言前排水
    inv = list(store.rows.values())[0]
    assert inv["status"] == "failed"


def test_prior_uncertain_blocks_retry():
    store = _InvStore(prior={"invocation_id": "inv0", "status": "uncertain", "age_s": 10})
    ex = ToolExecutor(store)
    called = []
    tool = _Tool(_write_spec(), lambda c, a: called.append(1) or ToolResult.text_ok("x"))
    res = ex.execute(_ctx(), tool, {"v": 1}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", idempotency_key="k1")
    assert res.status == "failed" and "不确定" in (res.error or "")
    assert not called                                       # 副作用零发生


def test_prior_fresh_executing_blocks_double_run():
    store = _InvStore(prior={"invocation_id": "inv0", "status": "executing", "age_s": 10})
    ex = ToolExecutor(store)
    called = []
    tool = _Tool(_write_spec(), lambda c, a: called.append(1) or ToolResult.text_ok("x"))
    res = ex.execute(_ctx(), tool, {"v": 1}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", idempotency_key="k1")
    assert res.status == "failed" and "进行中" in (res.error or "")
    assert not called


def test_prior_stale_executing_marked_uncertain_and_blocked():
    store = _InvStore(prior={"invocation_id": "inv0", "status": "executing", "age_s": 9999})
    ex = ToolExecutor(store)
    called = []
    tool = _Tool(_write_spec(), lambda c, a: called.append(1) or ToolResult.text_ok("x"))
    res = ex.execute(_ctx(), tool, {"v": 1}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", idempotency_key="k1")
    assert res.status == "failed"
    assert store.marked_uncertain and store.marked_uncertain[0][0] == "inv0"
    assert not called


def test_prior_failed_reclaimed_and_executed_on_same_row():
    """failed 残行：CAS 回收原行重试（不插新行防 uk_tool_idem 撞键），执行成功。"""
    store = _InvStore(prior={"invocation_id": "inv0", "status": "failed", "age_s": 100})
    ex = ToolExecutor(store)
    tool = _Tool(_write_spec(), lambda c, a: ToolResult.text_ok("done", receipt={"ok": 1}))
    res = ex.execute(_ctx(), tool, {"v": 1}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", idempotency_key="k1")
    assert res.status == "succeeded"
    assert store.rows["inv0"].get("reclaimed") is True      # 复用原行
    assert store.rows["inv0"]["status"] == "succeeded"
    assert store._n == 0                                    # 未插新行


def test_prior_failed_reclaim_lost_race_blocked():
    store = _InvStore(prior={"invocation_id": "inv0", "status": "failed", "age_s": 100})
    store.reclaim_ok = False
    ex = ToolExecutor(store)
    called = []
    tool = _Tool(_write_spec(), lambda c, a: called.append(1) or ToolResult.text_ok("x"))
    res = ex.execute(_ctx(), tool, {"v": 1}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", idempotency_key="k1")
    assert res.status == "failed" and "冲突" in (res.error or "")
    assert not called


def test_approval_request_id_from_ctx_lands_in_invocation():
    """P1 回链：ctx.approval_request_id（adjudicator 消费凭据时注入）落 invocation 行。"""
    from dataclasses import replace
    store = _InvStore()
    ex = ToolExecutor(store)
    ctx = replace(_ctx(), approval_request_id="areq_1")
    tool = _Tool(_write_spec(), lambda c, a: ToolResult.text_ok("x"))
    res = ex.execute(ctx, tool, {"v": 1}, run_id="r1", step_no=1,
                     policy_decision="approved", policy_id="p", idempotency_key="k1")
    assert res.status == "succeeded"
    inv = list(store.rows.values())[0]
    assert inv.get("approval_request_id") == "areq_1"


# ═════════════════ P1-6 obligations + output schema ═════════════════


def test_unknown_obligation_fail_closed_before_side_effect():
    store = _InvStore()
    ex = ToolExecutor(store)
    called = []
    tool = _Tool(_write_spec(), lambda c, a: called.append(1) or ToolResult.text_ok("x"))
    res = ex.execute(_ctx(), tool, {"v": 1}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", idempotency_key="k1",
                     obligations=("require_mfa",))
    assert res.status == "denied" and "缺执行器" in (res.error or "")
    assert not called                                       # 副作用之前拦截
    inv = list(store.rows.values())[0]
    assert inv["status"] == "denied"


def test_obligation_limit_rows_enforced():
    store = _InvStore()
    ex = ToolExecutor(store)
    spec = _write_spec(name="t_sql", risk_level=RiskLevel.READ_ONLY, idempotency="none",
                       side_effects=False)
    rows = [[i] for i in range(10)]
    tool = _Tool(spec, lambda c, a: ToolResult.ok(
        content=[ContentBlock.of_table(["n"], rows)], receipt={}))
    res = ex.execute(_ctx(), tool, {}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", obligations=("limit_rows:3",))
    assert res.status == "succeeded"
    assert len(res.content[0].table["rows"]) == 3


def test_obligation_mask_output_scrubs_pii():
    store = _InvStore()
    ex = ToolExecutor(store)
    spec = _write_spec(name="t_read2", risk_level=RiskLevel.READ_ONLY, idempotency="none",
                       side_effects=False)
    tool = _Tool(spec, lambda c, a: ToolResult.text_ok("联系人电话 13812345678，请回电"))
    res = ex.execute(_ctx(), tool, {}, run_id="r1", step_no=1,
                     policy_decision="allow", policy_id="p", obligations=("mask_output:phone",))
    assert res.status == "succeeded"
    assert "13812345678" not in res.content[0].text         # PII 已掩码


def test_obligation_handler_failure_withholds_output():
    from opensearch_pipeline.agent_runtime import tool_executor as te
    te.register_obligation_handler("boom", lambda p, r: (_ for _ in ()).throw(RuntimeError("x")))
    try:
        store = _InvStore()
        ex = ToolExecutor(store)
        spec = _write_spec(name="t_read3", risk_level=RiskLevel.READ_ONLY, idempotency="none",
                           side_effects=False)
        tool = _Tool(spec, lambda c, a: ToolResult.text_ok("敏感原文"))
        res = ex.execute(_ctx(), tool, {}, run_id="r1", step_no=1,
                         policy_decision="allow", policy_id="p", obligations=("boom",))
        assert all("敏感原文" not in (b.text or "") for b in res.content)   # 原文被扣留
    finally:
        te._OBLIGATION_HANDLERS.pop("boom", None)


def test_output_schema_violation_readonly_failed_write_uncertain():
    strict = {"type": "object", "required": ["order_no"], "additionalProperties": False,
              "properties": {"order_no": {"type": "string"}}}
    # 读工具违约 → failed
    store1 = _InvStore()
    spec_r = _write_spec(name="t_r", risk_level=RiskLevel.READ_ONLY, idempotency="none",
                         side_effects=False, output_schema=strict)
    tool_r = _Tool(spec_r, lambda c, a: ToolResult.text_ok("x", receipt={"bad": 1}))
    res = ToolExecutor(store1).execute(_ctx(), tool_r, {}, run_id="r", step_no=1,
                                       policy_decision="allow", policy_id="p")
    assert res.status == "failed" and "契约" in (res.error or "")
    from opensearch_pipeline.agent_runtime.tool_executor import drain_read_trace
    assert drain_read_trace()                       # READ_ONLY trace 异步：断言前排水
    assert list(store1.rows.values())[0]["status"] == "failed"
    # 写工具违约 → uncertain（副作用已发生）
    store2 = _InvStore()
    spec_w = _write_spec(name="t_w", output_schema=strict)
    tool_w = _Tool(spec_w, lambda c, a: ToolResult.text_ok("x", receipt={"bad": 1}))
    res = ToolExecutor(store2).execute(_ctx(), tool_w, {"v": 1}, run_id="r", step_no=1,
                                       policy_decision="allow", policy_id="p",
                                       idempotency_key="k9")
    assert res.status == "failed" and "对账" in (res.error or "")
    assert list(store2.rows.values())[0]["status"] == "uncertain"


# ═════════════════ P1-5 可见集按 policy 收敛 ═════════════════


def _spec(scope, name):
    return ToolSpec(name=name, version="1.0", description="d",
                    input_schema={"type": "object"}, output_schema={"type": "object"},
                    risk_level=RiskLevel.READ_ONLY, permission_scope=scope,
                    data_classification="internal")


def test_would_grant_projection():
    eng = PolicyEngine([
        PolicyRule(effect="allow", scopes=("kb.search",), policy_id="p1"),
        PolicyRule(effect="require_approval", scopes=("u8.writeback",),
                   roles=("dept_admin",), policy_id="p2"),
        PolicyRule(effect="deny", scopes=("danger.*",), policy_id="p3"),
        PolicyRule(effect="allow", scopes=("danger.x",), policy_id="p4"),
    ])
    emp = _ctx()
    assert eng.would_grant(emp, _spec("kb.search", "ks")) is True
    assert eng.would_grant(emp, _spec("u8.writeback", "wb")) is False      # 角色不符
    assert eng.would_grant(emp, _spec("danger.x", "dx")) is False          # deny 压过 allow
    assert eng.would_grant(emp, _spec("other", "ot")) is False             # default-deny
    admin = ExecutionContext.create(request_id="r", user_id="a", acl_groups=["g"],
                                    roles=["dept_admin"], channel="console", thread_id="t")
    assert eng.would_grant(admin, _spec("u8.writeback", "wb")) is True     # 会走审批但可见


def test_registry_visibility_filter_and_fail_open():
    reg = ToolRegistry()
    reg.register(_Tool(_spec("kb.search", "ks"), lambda c, a: ToolResult.text_ok("")))
    reg.register(_Tool(_spec("secret.op", "so"), lambda c, a: ToolResult.text_ok("")))
    eng = PolicyEngine([PolicyRule(effect="allow", scopes=("kb.search",), policy_id="p1")])
    reg.attach_visibility_filter(lambda c, specs: [s for s in specs if eng.would_grant(c, s)])
    visible = [s.name for s in reg.list_specs(_ctx())]
    assert visible == ["ks"]                                # 必然被拒的不可见
    assert {s.name for s in reg.list_specs(None)} == {"ks", "so"}   # ctx=None 不过滤（治理面）
    # fail-open：过滤器抛错 → 回退全集（Policy 调用时仍兜底）
    reg.attach_visibility_filter(lambda c, specs: (_ for _ in ()).throw(RuntimeError("x")))
    assert {s.name for s in reg.list_specs(_ctx())} == {"ks", "so"}


# ═════════════════ P0-F 运行状态：tool_result 帧 ═════════════════


def test_driver_emits_tool_result_after_adjudication():
    """driver 在裁决返回后发射 ToolResultEmitted（status+耗时，不带内容/参数）——
    前端据此把「调用工具」阶段收敛为「已完成/被拒/等待审批」。"""
    from opensearch_pipeline.agent_runtime.events import ToolResultEmitted
    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor

    class _Store:
        def create_run(self, ctx, profile):
            return "runX"

        def transition(self, run_id, frm, to):
            return True

        def consume_budget(self, run_id, **k):
            return {}

        def append_step(self, run_id, step):
            return 1

    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                           arguments={"query": "x"})]),
        ModelTurn(text="答案"),
    ]))
    ex = ThreadedRunExecutor(_Store(), lambda ctx, ev: ToolResult.text_ok("检索结果"),
                             max_concurrent=2)
    try:
        handle = ex.submit(_ctx(), loop, [{"role": "user", "content": "q"}], [])
        events = list(handle.events())
        trs = [e for e in events if isinstance(e, ToolResultEmitted)]
        assert len(trs) == 1
        assert trs[0].call_id == "c1" and trs[0].status == "succeeded"
        assert trs[0].elapsed_ms >= 0
        # 序：tool_call 提案在前、结局在后
        kinds = [type(e).__name__ for e in events]
        assert kinds.index("ToolCallProposed") < kinds.index("ToolResultEmitted")
        assert not hasattr(trs[0], "arguments")            # 帧上无参数/内容（敏感面不外发）
    finally:
        ex.shutdown()


# ═════════════════ 批次4（ultra 评审）executor 三修 ═════════════════


class _CallIdDecisionStore(_DecisionStore):
    """_DecisionStore + 请求行（含 call_id）——旧决定重放锚定校验用。"""

    def __init__(self, decision, req_call_id):
        super().__init__(decision=decision)
        self._req_call_id = req_call_id

    def get_request(self, request_id):
        return {"request_id": self.request_id, "call_id": self._req_call_id}


def test_b4_grant_rejects_stale_decision_for_other_call():
    """批次4（ultra approval_store:488 联动）：决定锚定的请求 call_id ≠ 当前挂起调用
    （多审批周期的旧决定被重放到新调用）→ RunRejected，同工具同参也不放行。"""
    import pytest

    from opensearch_pipeline.agent_runtime.executor import RunRejected
    from opensearch_pipeline.agent_runtime.tool_executor import digest as _digest
    astore = _CallIdDecisionStore(
        {"decision": "approved", "final_args_digest": _digest({"qty": 7}),
         "request_id": "req1"}, req_call_id="c-OLD")
    with pytest.raises(RunRejected, match="另一次调用"):
        _resume_with(astore)


def test_b4_grant_accepts_decision_anchored_to_current_call():
    """对照：请求行 call_id == checkpoint pending call_id（c1）→ 照常放行续跑。"""
    from opensearch_pipeline.agent_runtime.tool_executor import digest as _digest
    astore = _CallIdDecisionStore(
        {"decision": "approved", "final_args_digest": _digest({"qty": 7}),
         "request_id": "req1"}, req_call_id="c1")
    store = _resume_with(astore)
    assert ("resuming", "running") in store.transitions


def _digestless_cp_store():
    """state_digest=None 的 checkpoint store（合法可解码 blob，只抹摘要）。"""
    blob, _dig = encode_checkpoint([{"role": "user", "content": "q"}],
                                   pending_call=None, turn=0)

    class _S:
        def __init__(self):
            self.transitions = []

        def transition(self, run_id, frm, to):
            self.transitions.append((frm, to))
            return True

        def load_latest_checkpoint(self, run_id):
            return SimpleNamespace(checkpoint_id="cp1", state_blob=blob, state_digest=None)

        def consume_budget(self, run_id, **kw):
            return {}

    return _S()


def test_b4_missing_digest_rejected_when_require_hmac_on(monkeypatch):
    """批次4（ultra executor:410）：REQUIRE_HMAC 开时缺 digest → 拒绝续跑（能写表的
    攻击者置空 digest 列即可绕过全部校验——正是 HMAC 的威胁模型），run 回滚 suspended。"""
    import pytest

    from opensearch_pipeline.agent_runtime.executor import RunRejected, ThreadedRunExecutor
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_KEY", raising=False)
    monkeypatch.delenv("RAG_SESSION_SIGNING_KEY", raising=False)
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_REQUIRE_HMAC", "true")
    store = _digestless_cp_store()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"), max_concurrent=2)
    try:
        with pytest.raises(RunRejected, match="无完整性摘要"):
            ex.resume("run1", _ctx(), Approved(),
                      DefaultAgentLoop(lambda m, t: ModelTurn(text="a")), [])
        assert ("resuming", "suspended") in store.transitions    # 回边保住可重试
    finally:
        ex.shutdown(wait=False)


def test_b4_missing_digest_rejected_when_key_present(monkeypatch):
    """批次4：密钥在场（即便 REQUIRE_HMAC 未开）缺 digest 同样拒绝——密钥在场即表示
    完整性校验已启用，摘要被抹=可疑。"""
    import pytest

    from opensearch_pipeline.agent_runtime.executor import RunRejected, ThreadedRunExecutor
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_KEY", "unit-test-key-material")
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_REQUIRE_HMAC", raising=False)
    store = _digestless_cp_store()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"), max_concurrent=2)
    try:
        with pytest.raises(RunRejected, match="无完整性摘要"):
            ex.resume("run1", _ctx(), Approved(),
                      DefaultAgentLoop(lambda m, t: ModelTurn(text="a")), [])
    finally:
        ex.shutdown(wait=False)


def test_b4_missing_digest_still_skipped_without_key(monkeypatch):
    """对照（无密钥环境旧语义）：digest 缺失 + 无密钥无 flag → 跳过校验照常续跑
    （直驱单测/简化桩/未启用 HMAC 的部署零回归）。"""
    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_KEY", raising=False)
    monkeypatch.delenv("RAG_SESSION_SIGNING_KEY", raising=False)
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_REQUIRE_HMAC", raising=False)
    store = _digestless_cp_store()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"), max_concurrent=2)
    try:
        h = ex.resume("run1", _ctx(), Approved(),
                      DefaultAgentLoop(lambda m, t: ModelTurn(text="a")), [])
        list(h.events())
        assert ("resuming", "running") in store.transitions
    finally:
        ex.shutdown(wait=True)


def test_b4_pool_failure_maybe_scheduled_keeps_slot():
    """批次4（ultra executor:231）：pool.submit 抛错但条目**可能已入队**（非 shutdown/
    broken，如线程创建失败）→ 并发槽不释放（所有权随条目交给可能在跑的驱动器，其
    finally 释放同一份 _acquire；双释放会让 _active 欠计、max_concurrent 闸被侵蚀）；
    certainly-rejected（shutdown/broken 态）→ 必未入队，照旧诚实释放。"""
    import pytest

    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor

    class _Store:
        def __init__(self):
            self.transitions = []

        def create_run(self, ctx, profile):
            return "runP"

        def transition(self, run_id, frm, to):
            self.transitions.append((run_id, frm, to))
            return True

    class _BoomPool:
        _shutdown = False
        _broken = False

        def submit(self, *a, **k):
            raise RuntimeError("can't start new thread")

        def shutdown(self, *a, **k):
            pass

    store = _Store()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"), max_concurrent=2)
    ex._pool.shutdown(wait=False)
    ex._pool = _BoomPool()
    with pytest.raises(RuntimeError):
        ex.submit(_ctx(), DefaultAgentLoop(lambda m, t: ModelTurn(text="a")),
                  [{"role": "user", "content": "q"}], [])
    assert ex._active == 1, "可能已入队：槽所有权移交驱动器，不得在 except 路径重复释放"
    assert ("runP", "running", "failed") not in store.transitions   # 不反标（真答案不作废）

    class _DeadPool(_BoomPool):
        _shutdown = True

        def submit(self, *a, **k):
            raise RuntimeError("cannot schedule new futures after shutdown")

    store2 = _Store()
    ex2 = ThreadedRunExecutor(store2, lambda ctx, ev: ToolResult.text_ok("x"), max_concurrent=2)
    ex2._pool.shutdown(wait=False)
    ex2._pool = _DeadPool()
    with pytest.raises(RuntimeError):
        ex2.submit(_ctx(), DefaultAgentLoop(lambda m, t: ModelTurn(text="a")),
                   [{"role": "user", "content": "q"}], [])
    assert ex2._active == 0, "certainly-rejected：条目必未入队，槽诚实释放"
    assert ("runP", "running", "failed") in store2.transitions      # 诚实收口
