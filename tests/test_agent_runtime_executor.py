# -*- coding: utf-8 -*-
"""
test_agent_runtime_executor.py — ThreadedRunExecutor（Task 11 / B1(b) / 执行模型 §1）

覆盖：端到端驱动（run_store create/transition/consume_budget 集成）· 有界拒绝（满→RunRejected）·
并发释放（run 结束后 active 归零）。用假 run_store + 假 adjudicator + DefaultAgentLoop(假 model)。
"""
import threading

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import RunCompleted, ToolCallProposed, Usage
from opensearch_pipeline.agent_runtime.executor import RunRejected, ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop, ModelTurn, ProposedCall
from opensearch_pipeline.agent_runtime.tool import ToolResult


class _FakeStore:
    def __init__(self):
        self.transitions = []
        self.budget = {"turns_used": 0, "tool_calls_used": 0, "tokens_used": 0}
        self.steps = []          # (run_id, kind, payload)
        self._n = 0
        self._step_no = 0

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def append_step(self, run_id, step):
        self._step_no += 1
        self.steps.append((run_id, step.kind, step.payload))
        return self._step_no

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        return True

    def consume_budget(self, run_id, *, turns=0, tool_calls=0, tokens=0):
        self.budget["turns_used"] += turns
        self.budget["tool_calls_used"] += tool_calls
        self.budget["tokens_used"] += tokens
        return dict(self.budget)


def _ctx():
    return ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
                                   channel="console", thread_id="t", budget=RunBudget(max_turns=8))


def _scripted(turns):
    box = {"i": 0}

    def _fn(msgs, tools):
        t = turns[box["i"]]
        box["i"] += 1
        return t

    return _fn


def test_end_to_end_run_succeeds():
    store = _FakeStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("检索结果"), max_concurrent=2)
    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                           arguments={"query": "x"})]),
        ModelTurn(text="答案", usage=Usage(tokens_prompt=3, tokens_completion=4)),
    ]))
    handle = ex.submit(_ctx(), loop, [{"role": "user", "content": "q"}], [])
    events = list(handle.events())          # 阻塞消费到终止
    ex.shutdown()                           # 等线程 finally 跑完（active 归零）

    assert any(isinstance(e, ToolCallProposed) for e in events)
    assert any(isinstance(e, RunCompleted) for e in events)
    assert ("run1", "running", "succeeded") in store.transitions
    assert store.budget["turns_used"] == 2           # ③ tool 轮 + 最终答案轮
    assert [k for _, k, _ in store.steps].count("model_call") == 2   # ④b 两模型轮各一 model_call step
    assert store.budget["tool_calls_used"] == 1      # 一次工具调用
    assert store.budget["tokens_used"] == 7          # RunCompleted.usage.total
    assert ex.active_count() == 0


def test_bounded_rejection_when_full():
    store = _FakeStore()
    gate = threading.Event()

    def blocking_adjudicate(ctx, ev):
        gate.wait(2)                        # 占住唯一 slot 直到放行
        return ToolResult.text_ok("r")

    ex = ThreadedRunExecutor(store, blocking_adjudicate, max_concurrent=1)
    loop1 = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c", tool_name="t")]),
        ModelTurn(text="done"),
    ]))
    h1 = ex.submit(_ctx(), loop1, [{"role": "user", "content": "q"}], [])   # active=1（同步递增）
    loop2 = DefaultAgentLoop(_scripted([ModelTurn(text="x")]))
    with pytest.raises(RunRejected):
        ex.submit(_ctx(), loop2, [], [])    # 满 → 拒绝（HTTP 层映射 429）

    gate.set()                              # 放行 h1
    h1.wait(2)
    ex.shutdown()
    assert ex.active_count() == 0           # 释放后归零，第三次可提交


class _FakeSpec:
    """SpeculativeSearch 形状替身：只记 start 次数。"""

    def __init__(self):
        self.starts = 0

    def start(self):
        self.starts += 1


def test_speculative_starts_on_admission_never_on_rejection():
    """F1：投机检索由 submit 在 _acquire 占槽成功后起跑——被拒的 submit 零预取
    （曾经构造即起跑：并发墙上每个 429 仍各烧一次 embedding+检索）。"""
    store = _FakeStore()
    gate = threading.Event()

    def blocking_adjudicate(ctx, ev):
        gate.wait(2)                        # 占住唯一 slot
        return ToolResult.text_ok("r")

    def _spec_ctx(spec):
        return ExecutionContext.create(
            request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
            channel="console", thread_id="t", budget=RunBudget(max_turns=8),
            speculative_search=spec)

    ex = ThreadedRunExecutor(store, blocking_adjudicate, max_concurrent=1)
    spec_admitted, spec_rejected = _FakeSpec(), _FakeSpec()
    loop1 = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c", tool_name="t")]),
        ModelTurn(text="done"),
    ]))
    h1 = ex.submit(_spec_ctx(spec_admitted), loop1, [{"role": "user", "content": "q"}], [])
    assert spec_admitted.starts == 1        # 占槽成功即起跑（先于工具消费，无竞态）
    with pytest.raises(RunRejected):
        ex.submit(_spec_ctx(spec_rejected),
                  DefaultAgentLoop(_scripted([ModelTurn(text="x")])), [], [])
    assert spec_rejected.starts == 0        # 被 429 拒 → 预取从未起跑
    gate.set()
    h1.wait(2)
    ex.shutdown()


def test_run_failure_transitions_failed():
    store = _FakeStore()

    def boom_adjudicate(ctx, ev):
        raise RuntimeError("工具炸了")

    ex = ThreadedRunExecutor(store, boom_adjudicate, max_concurrent=2)
    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c", tool_name="t")]),
    ]))
    handle = ex.submit(_ctx(), loop, [{"role": "user", "content": "q"}], [])
    events = list(handle.events())
    ex.shutdown()
    from opensearch_pipeline.agent_runtime.events import RunFailed
    assert any(isinstance(e, RunFailed) for e in events)
    assert ("run1", "running", "failed") in store.transitions


def test_tool_calls_budget_fail_closed():
    """③：单轮提 2 工具、max_tool_calls=1 → 第 2 个执行前 fail-closed；该轮只计 1 次 turn。"""
    store = _FakeStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=1)
    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="t"),
                              ProposedCall(call_id="c2", tool_name="t")]),
        ModelTurn(text="never"),
    ]))
    ctx = ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
                                  channel="console", thread_id="t",
                                  budget=RunBudget(max_turns=8, max_tool_calls=1))
    events = list(ex.submit(ctx, loop, [], []).events())
    ex.shutdown()
    from opensearch_pipeline.agent_runtime.events import RunFailed
    assert any(isinstance(e, RunFailed) and "tool_calls 预算" in e.error for e in events)
    assert ("run1", "running", "failed") in store.transitions
    assert store.budget["turns_used"] == 1           # 同一 tool 批只计一次 turn
    assert store.budget["tool_calls_used"] == 2       # 消费到 2（>1）才拦


# ── 去重键送达点提交（2026-07-11 上下文预算，评审 R②-4）────────────────────────────
def _session_ctx(session):
    return ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"],
                                   roles=["employee"], channel="console", thread_id="t",
                                   budget=RunBudget(max_turns=8), search_session=session)


class _Session:
    def __init__(self):
        self.seen = set()
        self.committed_calls = 0

    def commit_keys(self, keys):
        self.seen.update(keys or ())
        self.committed_calls += 1


def test_dedup_keys_committed_on_delivery():
    """成功结果送达（gen.send 前）→ 驱动线程提交 keys；session 状态由 executor 推进。"""
    store = _FakeStore()
    res = ToolResult.text_ok("检索结果")
    res.artifacts = {"chunks": [], "dedup_keys": [("cid", "C1", "h1"), ("cid", "C2", "h2")]}
    ex = ThreadedRunExecutor(store, lambda ctx, ev: res, max_concurrent=2)
    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                           arguments={"query": "x"})]),
        ModelTurn(text="答案"),
    ]))
    session = _Session()
    handle = ex.submit(_session_ctx(session), loop, [{"role": "user", "content": "q"}], [])
    list(handle.events())
    ex.shutdown()
    assert session.seen == {("cid", "C1", "h1"), ("cid", "C2", "h2")}
    assert session.committed_calls == 1


def test_dedup_keys_not_committed_on_failed_result():
    """失败结果（如超时被 executor 换成 fail）→ keys 永不提交——毒化路径闭死。"""
    store = _FakeStore()
    res = ToolResult.fail("超时")
    res.artifacts = {"dedup_keys": [("cid", "C1", "h1")]}
    ex = ThreadedRunExecutor(store, lambda ctx, ev: res, max_concurrent=2)
    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                           arguments={"query": "x"})]),
        ModelTurn(text="答案"),
    ]))
    session = _Session()
    handle = ex.submit(_session_ctx(session), loop, [{"role": "user", "content": "q"}], [])
    list(handle.events())
    ex.shutdown()
    assert session.seen == set() and session.committed_calls == 0


# ─────────────────────────────────────────────────────────────────────────────
# perf 批次 C §4.5：record_turn 单事务优先路径 + increment_budget 只写路径
# （上面的 _FakeStore 无这两法 → 既有用例继续钉住回退老三样的兼容性）
# ─────────────────────────────────────────────────────────────────────────────
class _TurnStore(_FakeStore):
    """带 record_turn / increment_budget 的桩（RDSRunStore 同契约）。"""

    def __init__(self):
        super().__init__()
        self.turn_calls = []
        self.consume_calls = 0
        self.inc_calls = []

    def record_turn(self, run_id, *, turn_index, tokens_prompt=None, tokens_completion=None,
                    tokens_total=0, final=False):
        self.turn_calls.append((run_id, turn_index, int(tokens_total), bool(final)))
        self._step_no += 1
        self.steps.append((run_id, "model_call", {"turn_index": turn_index, "final": final}))
        self.budget["turns_used"] += 1
        self.budget["tokens_used"] += int(tokens_total)

    def increment_budget(self, run_id, *, turns=0, tool_calls=0, tokens=0):
        self.inc_calls.append((turns, tool_calls, tokens))
        self.budget["turns_used"] += turns
        self.budget["tool_calls_used"] += tool_calls
        self.budget["tokens_used"] += tokens

    def consume_budget(self, run_id, **kw):
        self.consume_calls += 1
        return super().consume_budget(run_id, **kw)


def test_record_turn_single_txn_replaces_per_turn_triple_write():
    """store 带 record_turn → 每模型轮恰一次单事务调用（turn_index/final/usage 正确），
    tool_calls 走只写 increment_budget；consume_budget（UPDATE+SELECT 老路径）零调用；
    账面（budget/steps）与老三样逐字节一致（对照 test_end_to_end_run_succeeds）。"""
    store = _TurnStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("检索结果"), max_concurrent=2)
    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                           arguments={"query": "x"})]),
        ModelTurn(text="答案", usage=Usage(tokens_prompt=3, tokens_completion=4)),
    ]))
    handle = ex.submit(_ctx(), loop, [{"role": "user", "content": "q"}], [])
    events = list(handle.events())
    ex.shutdown()

    assert any(isinstance(e, RunCompleted) for e in events)
    assert store.turn_calls == [("run1", 0, 0, False), ("run1", 1, 7, True)]
    assert store.inc_calls == [(0, 1, 0)]        # 仅 tool_calls 增量，且只写不读回
    assert store.consume_calls == 0              # 逐轮 UPDATE+SELECT 老路径零调用
    assert store.budget == {"turns_used": 2, "tool_calls_used": 1, "tokens_used": 7}
    assert [k for _, k, _ in store.steps].count("model_call") == 2


def test_record_turn_failure_falls_back_to_segmented_writes():
    """record_turn 单事务失败 → 回退老三样（step/budget 仍落齐，run 不受影响）。"""
    store = _TurnStore()

    def _boom(run_id, **kw):
        raise RuntimeError("txn 失败")

    store.record_turn = _boom
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    loop = DefaultAgentLoop(_scripted([ModelTurn(text="答案", usage=Usage(tokens_prompt=1,
                                                                          tokens_completion=1))]))
    handle = ex.submit(_ctx(), loop, [{"role": "user", "content": "q"}], [])
    events = list(handle.events())
    ex.shutdown()
    assert any(isinstance(e, RunCompleted) for e in events)
    # 回退路径：model_call step 照记，预算经 increment_budget 落齐（budget 不缺账）
    assert [k for _, k, _ in store.steps].count("model_call") == 1
    assert store.budget["turns_used"] == 1 and store.budget["tokens_used"] == 2
