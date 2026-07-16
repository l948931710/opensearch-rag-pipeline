# -*- coding: utf-8 -*-
"""
test_agent_completion_truth.py — unknown-unknowns 批次1（P0-01/P0-02/P1-05/P1-07）

完成真值：最终答案 durable 写与 running→succeeded 同一事务（complete_run_atomic +
handle._on_complete_durable 游标回调）——持久化失败落 failed 绝不发 done；CAS 失败
（失去所有权）结果作废；commit 后缓存副作用（记忆）失败不影响真值。
预算硬上限：最终模型轮 post-call 复判 token/deadline（首轮即 final、工具后 final、
恰好用满、deadline 已过四个边界）；turns 语义有意维持「工具轮循环上界」。
有界事件队列：满则丢 ModelDelta、终态帧挤位入队、收尾哨兵绝不阻塞。
read-trace 排水：_drain_runtime 冲异步 trace 队列。
"""
from datetime import datetime, timedelta, timezone

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import (
    ModelDelta, RunCompleted, RunFailed, Usage)
from opensearch_pipeline.agent_runtime.executor import RunHandle, ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop, ModelTurn, ProposedCall
from opensearch_pipeline.agent_runtime.tool import ToolResult


class _BaseStore:
    """无 complete_run_atomic 的旧式桩（legacy 路径）。"""

    def __init__(self):
        self.transitions = []
        self._n = 0
        self._step_no = 0

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def append_step(self, run_id, step):
        self._step_no += 1
        return self._step_no

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        return True

    def consume_budget(self, run_id, *, turns=0, tool_calls=0, tokens=0):
        return {}


class _AtomicStore(_BaseStore):
    """带 complete_run_atomic 的桩：语义对齐 RDSRunStore——先所有权检查、再 extra_writer、
    再 succeeded；writer 异常原样上抛（模拟整体回滚，completed 不记）。"""

    def __init__(self, *, cas_ok=True, raise_on_commit=False):
        super().__init__()
        self._cas_ok = cas_ok
        self._raise = raise_on_commit
        self.completed = []
        self.writer_ran = 0

    def complete_run_atomic(self, run_id, extra_writer=None):
        if self._raise:
            raise RuntimeError("commit 失败（注入）")
        if not self._cas_ok:
            return False                     # 所有权检查在 writer 之前（与 RDS 实现一致）
        if extra_writer is not None:
            self.writer_ran += 1
            extra_writer(object())           # 假事务游标
        self.completed.append(run_id)
        return True


def _ctx(budget=None):
    return ExecutionContext.create(
        request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
        channel="console", thread_id="t", budget=budget or RunBudget(max_turns=8))


def _scripted(turns):
    box = {"i": 0}

    def _fn(msgs, tools):
        t = turns[box["i"]]
        box["i"] += 1
        return t

    return _fn


def _run(store, turns, *, budget=None, writer=None, cache=None, on_fail=None,
         adjudicate=None):
    ex = ThreadedRunExecutor(store, adjudicate or (lambda c, e: ToolResult.text_ok("ok")),
                             max_concurrent=2)
    loop = DefaultAgentLoop(_scripted(turns))
    handle = ex.submit(_ctx(budget), loop, [{"role": "user", "content": "q"}], [],
                       on_complete=cache, on_failure=on_fail, on_complete_durable=writer)
    events = list(handle.events())
    ex.shutdown()
    return events


# ── P0-01 故障注入 ──────────────────────────────────────────────────────────────


def test_answer_persist_failure_marks_failed_never_done():
    """extra_writer 抛（qa 行写不进）→ 事务回滚、run 落 failed，绝不发 RunCompleted。"""
    store = _AtomicStore()
    calls = {"cache": 0, "fail": []}

    def writer(cur, final_text, retrieved):
        raise RuntimeError("qa_session_log 写失败（注入）")

    events = _run(store, [ModelTurn(text="答案", usage=Usage(tokens_completion=3))],
                  writer=writer, cache=lambda t, r=None: calls.__setitem__("cache", 1),
                  on_fail=lambda e: calls["fail"].append(e))
    assert not any(isinstance(e, RunCompleted) for e in events)
    fails = [e for e in events if isinstance(e, RunFailed)]
    assert fails and "answer_persist_failed" in fails[0].error
    assert ("run1", "running", "failed") in store.transitions
    assert ("run1", "running", "succeeded") not in store.transitions
    assert store.completed == []             # 事务未提交
    assert calls["cache"] == 0               # 缓存回调绝不在真值失败后跑
    assert calls["fail"] and "answer_persist_failed" in calls["fail"][0]


def test_commit_failure_marks_failed_never_done():
    """complete_run_atomic 自身抛（commit 失败）→ 同上：failed、无 done 帧。"""
    store = _AtomicStore(raise_on_commit=True)
    events = _run(store, [ModelTurn(text="答案")], writer=lambda c, t, r: None)
    assert not any(isinstance(e, RunCompleted) for e in events)
    assert any(isinstance(e, RunFailed) for e in events)
    assert ("run1", "running", "failed") in store.transitions
    assert ("run1", "running", "succeeded") not in store.transitions


def test_cas_lost_voids_result_writer_never_runs():
    """完成时已失去所有权（收尸/取消/排水抢先）→ 结果作废：writer 不跑、无 done、无 failed 迁移
    （原 fencing 语义：另一方已持有终态）。"""
    store = _AtomicStore(cas_ok=False)
    calls = {"writer": 0}

    def writer(cur, final_text, retrieved):
        calls["writer"] += 1

    events = _run(store, [ModelTurn(text="答案")], writer=writer)
    assert not any(isinstance(e, RunCompleted) for e in events)
    fails = [e for e in events if isinstance(e, RunFailed)]
    assert fails and "作废" in fails[0].error
    assert calls["writer"] == 0 and store.writer_ran == 0
    assert ("run1", "running", "succeeded") not in store.transitions


def test_memory_failure_after_commit_keeps_succeeded():
    """durable 已成真后缓存回调（会话记忆）抛 → run 保持 succeeded、done 帧照发
    （commit 后副作用失败只降级告警，绝不撤销真值）。"""
    store = _AtomicStore()

    def cache(final_text, retrieved=None):
        raise RuntimeError("memory.append 失败（注入）")

    events = _run(store, [ModelTurn(text="答案")], writer=lambda c, t, r: None, cache=cache)
    assert any(isinstance(e, RunCompleted) for e in events)
    assert store.completed == ["run1"]
    assert not any(isinstance(e, RunFailed) for e in events)


def test_legacy_store_writer_best_effort():
    """无 complete_run_atomic 的桩：CAS 后 writer 以 cur=None 调用（best-effort，
    writer 抛也不掀翻 run——桩环境无事务缝可用，保持历史语义）。"""
    store = _BaseStore()
    seen = {"cur": "sentinel", "n": 0}

    def writer(cur, final_text, retrieved):
        seen["cur"], seen["n"] = cur, seen["n"] + 1
        raise RuntimeError("legacy writer 抛（应被吞）")

    events = _run(store, [ModelTurn(text="答案")], writer=writer)
    assert any(isinstance(e, RunCompleted) for e in events)
    assert seen["n"] == 1 and seen["cur"] is None
    assert ("run1", "running", "succeeded") in store.transitions


# ── P0-02 最终轮预算边界 ─────────────────────────────────────────────────────────


def test_first_turn_final_token_overrun_fails():
    """首轮即 final 且 usage 超 token_budget → 诚实落 failed（此前完全绕过强制）。"""
    store = _AtomicStore()
    calls = {"writer": 0}
    events = _run(store,
                  [ModelTurn(text="答案", usage=Usage(tokens_prompt=10, tokens_completion=6))],
                  budget=RunBudget(max_turns=8, token_budget=10),
                  writer=lambda c, t, r: calls.__setitem__("writer", 1))
    fails = [e for e in events if isinstance(e, RunFailed)]
    assert fails and "token 预算超限" in fails[0].error and "最终轮后判" in fails[0].error
    assert not any(isinstance(e, RunCompleted) for e in events)
    assert ("run1", "running", "failed") in store.transitions
    assert calls["writer"] == 0 and store.completed == []


def test_tool_then_final_cumulative_token_overrun_fails():
    """工具轮 6 + 最终轮 6 > budget 10 → 累计口径在 final 后拦住。"""
    store = _AtomicStore()
    events = _run(store, [
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="t", arguments={})],
                  usage=Usage(tokens_prompt=3, tokens_completion=3)),
        ModelTurn(text="答案", usage=Usage(tokens_prompt=3, tokens_completion=3)),
    ], budget=RunBudget(max_turns=8, token_budget=10))
    fails = [e for e in events if isinstance(e, RunFailed)]
    assert fails and "token 预算超限" in fails[0].error
    assert not any(isinstance(e, RunCompleted) for e in events)


def test_final_exactly_at_budget_succeeds():
    """恰好用满（== token_budget）不算超（沿用 `>` 语义）→ 正常成功。"""
    store = _AtomicStore()
    events = _run(store,
                  [ModelTurn(text="答案", usage=Usage(tokens_prompt=5, tokens_completion=5))],
                  budget=RunBudget(max_turns=8, token_budget=10),
                  writer=lambda c, t, r: None)
    assert any(isinstance(e, RunCompleted) for e in events)
    assert store.completed == ["run1"]


def test_final_after_deadline_fails():
    """deadline 已过时的最终轮 → post-call 复判拦住（此前 gateway 只 pre-call 检查）。"""
    store = _AtomicStore()
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    events = _run(store, [ModelTurn(text="答案")],
                  budget=RunBudget(max_turns=8, deadline=past))
    fails = [e for e in events if isinstance(e, RunFailed)]
    assert fails and "deadline" in fails[0].error
    assert not any(isinstance(e, RunCompleted) for e in events)


def test_turns_cap_counts_final_at_loop_level():
    """turns 维度双保险的分工：**loop 在每轮起跑前**按 max_turns 拦（含 final——
    max_turns=1 时工具轮后连 final 都起不来，run 落 failed）；executor 因此不必在
    RunCompleted 后再检 turns（final 不可能越过 loop 门起跑）。cap 内（=2）照常成功。"""
    turns = [
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="t", arguments={})]),
        ModelTurn(text="答案"),
    ]
    store1 = _AtomicStore()
    events1 = _run(store1, list(turns), budget=RunBudget(max_turns=1))
    fails = [e for e in events1 if isinstance(e, RunFailed)]
    assert fails and "max_turns" in fails[0].error       # loop 级 cap 拦住 final 起跑
    assert not any(isinstance(e, RunCompleted) for e in events1)

    store2 = _AtomicStore()
    events2 = _run(store2, list(turns), budget=RunBudget(max_turns=2),
                   writer=lambda c, t, r: None)
    assert any(isinstance(e, RunCompleted) for e in events2)
    assert store2.completed == ["run1"]


# ── P1-05 有界事件队列 ──────────────────────────────────────────────────────────


def test_event_queue_bounded_drops_deltas_keeps_terminal(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_EVENT_QUEUE_MAX", "4")
    h = RunHandle("rq")
    for i in range(10):
        h._emit(ModelDelta(text=f"d{i}"))
    h._emit(RunCompleted(final_text="答案"))     # 非 delta：挤位也要入队
    h._finish()                                   # 哨兵：绝不阻塞
    events = list(h.events())
    assert h._dropped_deltas > 0
    assert any(isinstance(e, RunCompleted) for e in events)   # 终态帧幸存
    assert len(events) <= 4                       # 队列上界生效


def test_event_queue_unbounded_escape_hatch(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_EVENT_QUEUE_MAX", "0")
    h = RunHandle("rq2")
    for i in range(50):
        h._emit(ModelDelta(text=f"d{i}"))
    h._emit(RunCompleted(final_text="x"))
    h._finish()
    events = list(h.events())
    assert h._dropped_deltas == 0 and len(events) == 51


# ── P1-07 read-trace 排水挂点 ────────────────────────────────────────────────────


def test_drain_runtime_flushes_read_trace(monkeypatch):
    import opensearch_pipeline.agent_runtime.tool_executor as te
    import opensearch_pipeline.routes.agent as agent_route

    class _Exec:
        def drain(self, timeout=0):
            return {}

    flag = {"drained": False}
    monkeypatch.setattr(agent_route, "_DRAINED", False)
    monkeypatch.setattr(agent_route, "_RUNTIME", (None, None, _Exec(), None))
    monkeypatch.setattr(te, "drain_read_trace",
                        lambda timeout=5.0: flag.__setitem__("drained", True))
    agent_route._drain_runtime()
    assert flag["drained"] is True
