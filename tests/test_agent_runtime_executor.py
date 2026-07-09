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
