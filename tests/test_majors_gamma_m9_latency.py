# -*- coding: utf-8 -*-
"""Majors γ1(M9.1) agent latency——executor↔store↔QA 写手接线契约(2026-07-21 codex 共识)。

- 完成：executor 传 active_ms 给 complete_run_atomic；writer(四参形态)收 store 锁内
  算出的累计值；store 不给(None/旧签名)→ 回退 base+本腿 monotonic(仍>0)。
- 失败/超预算/取消：终态 CAS 收到 active_ms；失败侧回调(二参形态)收累计口径。
- 旧签名桩(无 active_ms/单参 writer/单参 on_failure)全程零破坏。
真库累计语义(COALESCE+delta/1054 摘除)在 test_majors_gamma_m9_db.py。
"""
from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import RunCompleted, RunFailed
from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop, ModelTurn, Usage
from opensearch_pipeline.agent_runtime.tool import ToolResult


class _BaseStore:
    def __init__(self):
        self.transitions = []
        self._n = 0

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def append_step(self, run_id, step):
        return 1

    def transition(self, run_id, frm, to, active_ms=None):
        self.transitions.append((run_id, frm, to, active_ms))
        return True

    def consume_budget(self, run_id, *, turns=0, tool_calls=0, tokens=0):
        return {}


class _GammaStore(_BaseStore):
    """新签名桩：complete_run_atomic 收 active_ms 并模拟锁内累计(stored+delta)传 writer。"""

    def __init__(self, stored_ms=0):
        super().__init__()
        self.stored_ms = stored_ms
        self.complete_active_ms = None

    def complete_run_atomic(self, run_id, extra_writer=None, active_ms=None):
        self.complete_active_ms = active_ms
        if extra_writer is not None:
            total = (self.stored_ms + int(active_ms)) if active_ms is not None else None
            extra_writer(object(), total)     # RDS 实现同形：writer 收累计
        return True


class _LegacyAtomicStore(_BaseStore):
    """旧签名桩(α 时代)：无 active_ms、writer 单参——TypeError 降级面契约。"""

    def __init__(self):
        super().__init__()
        self.completed = []

    def transition(self, run_id, frm, to):          # 旧签名：连 active_ms 都不识
        self.transitions.append((run_id, frm, to))
        return True

    def complete_run_atomic(self, run_id, extra_writer=None):
        if extra_writer is not None:
            extra_writer(object())
        self.completed.append(run_id)
        return True


def _ctx():
    return ExecutionContext.create(
        request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
        channel="console", thread_id="t", budget=RunBudget(max_turns=8))


def _scripted(turns):
    box = {"i": 0}

    def _fn(msgs, tools):
        t = turns[box["i"]]
        box["i"] += 1
        return t

    return _fn


def _run(store, turns, *, writer=None, on_fail=None):
    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("ok"), max_concurrent=2)
    loop = DefaultAgentLoop(_scripted(turns))
    handle = ex.submit(_ctx(), loop, [{"role": "user", "content": "q"}], [],
                       on_failure=on_fail, on_complete_durable=writer)
    events = list(handle.events())
    ex.shutdown()
    return events


def test_complete_passes_active_ms_and_writer_gets_store_total():
    """完成：store 收 active_ms(≥0)；四参 writer 收 store 锁内累计(stored+delta)。"""
    store = _GammaStore(stored_ms=5000)
    seen = {}

    def writer(cur, final_text, retrieved, active_total_ms):
        seen["total"] = active_total_ms

    events = _run(store, [ModelTurn(text="答案", usage=Usage(tokens_completion=2))],
                  writer=writer)
    assert any(isinstance(e, RunCompleted) for e in events)
    assert isinstance(store.complete_active_ms, int) and store.complete_active_ms >= 0
    assert seen["total"] == 5000 + store.complete_active_ms

    # 三参旧 writer 照旧可用（签名探测降级）
    store2 = _GammaStore()
    seen2 = {}

    def writer3(cur, final_text, retrieved):
        seen2["ran"] = True

    events2 = _run(store2, [ModelTurn(text="答案")], writer=writer3)
    assert any(isinstance(e, RunCompleted) for e in events2) and seen2["ran"]


def test_store_none_total_falls_back_to_leg_monotonic():
    """store 传 None(054 未 apply 形态)→ writer 收 executor 回退值 base+本腿(仍≥0)。"""

    class _NoneTotalStore(_GammaStore):
        def complete_run_atomic(self, run_id, extra_writer=None, active_ms=None):
            self.complete_active_ms = active_ms
            if extra_writer is not None:
                extra_writer(object(), None)      # 模拟缺列：无累计可算
            return True

    store = _NoneTotalStore()
    seen = {}

    def writer(cur, final_text, retrieved, active_total_ms):
        seen["total"] = active_total_ms

    _run(store, [ModelTurn(text="答案")], writer=writer)
    assert isinstance(seen["total"], int) and seen["total"] >= 0   # 本腿 monotonic 回退


def test_failure_terminal_cas_carries_active_ms_and_callback_total():
    """失败：终态 CAS(transition) 收 active_ms；二参 on_failure 收累计口径。"""
    store = _GammaStore()
    seen = {}

    def on_fail(err, active_total_ms=None):
        seen["err"], seen["total"] = err, active_total_ms

    class _FailLoop:
        def run(self, ctx, messages, tools):
            def _gen():
                raise RuntimeError("炸")
                yield  # noqa: E501 — 生成器形态
            return _gen()

    ex = ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("ok"), max_concurrent=2)
    handle = ex.submit(_ctx(), _FailLoop(), [], [], on_failure=on_fail)
    events = list(handle.events())
    ex.shutdown()
    assert any(isinstance(e, RunFailed) for e in events)
    term = [t for t in store.transitions if t[1] == "running" and t[2] == "failed"]
    assert term and isinstance(term[0][3], int) and term[0][3] >= 0   # CAS 收 delta
    assert isinstance(seen["total"], int) and seen["total"] >= 0


def test_legacy_store_and_single_arg_callbacks_unbroken():
    """旧签名桩 + 单参 on_failure/writer：TypeError/签名探测全程降级，行为如旧。"""
    store = _LegacyAtomicStore()
    ran = {}

    def writer(cur, final_text, retrieved):
        ran["w"] = True

    events = _run(store, [ModelTurn(text="答案")], writer=writer)
    assert any(isinstance(e, RunCompleted) for e in events)
    assert store.completed and ran["w"]

    seen = {}

    class _FailLoop:
        def run(self, ctx, messages, tools):
            def _gen():
                raise RuntimeError("炸")
                yield
            return _gen()

    ex = ThreadedRunExecutor(_LegacyAtomicStore(), lambda c, e: ToolResult.text_ok("ok"),
                             max_concurrent=2)
    handle = ex.submit(_ctx(), _FailLoop(), [], [],
                       on_failure=lambda err: seen.setdefault("err", err))   # 单参回调
    list(handle.events())
    ex.shutdown()
    assert "err" in seen


def test_qa_writer_latency_kwarg_matches_qa_logger():
    """接线端到端形态：routes 写手把 active_total_ms 落 insert_qa_row_tx(latency_ms=...)
    ——qa_logger 侧参数存在性由本断言锁住（γ1 两树共享半）。"""
    import inspect

    from opensearch_pipeline.qa_logger import insert_qa_row_tx, log_qa_session
    assert "latency_ms" in inspect.signature(insert_qa_row_tx).parameters
    assert "latency_ms" in inspect.signature(log_qa_session).parameters
