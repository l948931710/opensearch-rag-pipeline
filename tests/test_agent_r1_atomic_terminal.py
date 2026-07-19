# -*- coding: utf-8 -*-
"""R1（Agent Runtime 外审 ARR-20260718 两 RB 收口）回归。

RB-RT-01：command→run 原子绑定——create_run 同 INSERT 落 dispatch_command_id
（052 反向锚，1054 容错回退）、uk 撞 1062 → DispatchCommandBound、恢复扫描先查后建
（评审探针「bind 失败 → original run 存活 + redriven=1」的翻绿场景）。
RB-RT-02：终态 durable-first——先赢 CAS 再对外宣告；CAS 落空 read-after-write 按
DB 事实宣告/闭嘴（评审探针「event=run_failed 而 transition=false」的翻绿场景）。
"""
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import RunCompleted, RunFailed, Usage
from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop, ModelTurn
from opensearch_pipeline.agent_runtime.run_store import (
    DispatchCommandBound, RDSRunStore)
from opensearch_pipeline.agent_runtime.tool import ToolResult


def _ctx(anchor=None, thread="t"):
    ctx = ExecutionContext.create(
        request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
        channel="console", thread_id=thread, budget=RunBudget(max_turns=8))
    if anchor:
        object.__setattr__(ctx, "dispatch_command_id", anchor)
    return ctx


# ═════════════════ RB-RT-01：create_run 原子落锚 ═════════════════


class _Cur:
    """脚本化 cursor：steps 按 execute 次序给 {exc} 或静默成功。"""

    def __init__(self, steps=None):
        self.steps = list(steps or [])
        self.sqls = []
        self.params = []

    def execute(self, sql, args=None):
        self.sqls.append(sql)
        self.params.append(args)
        step = self.steps.pop(0) if self.steps else {}
        if step.get("exc"):
            raise step["exc"]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _wire_store(monkeypatch, cur):
    store = RDSRunStore()
    monkeypatch.setattr(store, "_conn", lambda: _Conn(cur))
    monkeypatch.setattr("opensearch_pipeline.agent_runtime.run_store._op_db",
                        lambda: "op")
    return store


class TestCreateRunAnchor:
    def test_anchor_lands_in_same_insert(self, monkeypatch):
        """锚随建 run 同一条 INSERT 原子落——绑定不再依赖第二步 bind UPDATE。"""
        cur = _Cur()
        store = _wire_store(monkeypatch, cur)
        run_id = store.create_run(_ctx(anchor="cmdA"), "default")
        assert run_id
        assert len(cur.sqls) == 1
        assert "dispatch_command_id" in cur.sqls[0]
        assert cur.params[0][-1] == "cmdA"

    def test_no_anchor_keeps_legacy_insert(self, monkeypatch):
        cur = _Cur()
        store = _wire_store(monkeypatch, cur)
        assert store.create_run(_ctx(), "default")
        assert "dispatch_command_id" not in cur.sqls[0]

    def test_1054_falls_back_to_legacy_insert(self, monkeypatch):
        """052 未 apply（1054 未知列）→ 回退无锚 INSERT（代码可先行），run 照建。"""
        cur = _Cur(steps=[{"exc": RuntimeError(1054, "Unknown column")}])
        cur.steps[0]["exc"].args = (1054, "Unknown column 'dispatch_command_id'")
        store = _wire_store(monkeypatch, cur)
        assert store.create_run(_ctx(anchor="cmdB"), "default")
        assert len(cur.sqls) == 2
        assert "dispatch_command_id" in cur.sqls[0]
        assert "dispatch_command_id" not in cur.sqls[1]

    def test_uk_collision_raises_dispatch_bound(self, monkeypatch):
        """uk_run_dispatch_cmd 撞 1062=命令已有 run——DB 裁决单赢家，输家拿专用异常。"""
        exc = RuntimeError()
        exc.args = (1062, "Duplicate entry 'cmdC' for key 'uk_run_dispatch_cmd'")
        cur = _Cur(steps=[{"exc": exc}])
        store = _wire_store(monkeypatch, cur)
        with pytest.raises(DispatchCommandBound):
            store.create_run(_ctx(anchor="cmdC"), "default")

    def test_thread_busy_still_distinguished(self, monkeypatch):
        from opensearch_pipeline.agent_runtime.run_store import ThreadBusy
        exc = RuntimeError()
        exc.args = (1062, "Duplicate entry 't#1' for key 'uk_thread_active'")
        cur = _Cur(steps=[{"exc": exc}])
        store = _wire_store(monkeypatch, cur)
        with pytest.raises(ThreadBusy):
            store.create_run(_ctx(anchor="cmdD"), "default")


class TestFindRunByCommand:
    def test_found_and_missing(self, monkeypatch):
        class _QCur(_Cur):
            def fetchone(self):
                return ("run9", "running")

        cur = _QCur()
        store = _wire_store(monkeypatch, cur)
        assert store.find_run_by_dispatch_command("cmdE") == {
            "run_id": "run9", "status": "running"}
        assert store.find_run_by_dispatch_command("") is None

    def test_1054_returns_none(self, monkeypatch):
        exc = RuntimeError()
        exc.args = (1054, "Unknown column 'dispatch_command_id'")
        cur = _Cur(steps=[{"exc": exc}])
        store = _wire_store(monkeypatch, cur)
        assert store.find_run_by_dispatch_command("cmdF") is None


class TestRecoveryLookupFirst:
    """评审探针翻绿：bind 失败后恢复扫描**先查后建**——原 run 存活时绝不重驱第二个。"""

    def _cmd(self):
        return {"kind": "submit", "command_id": "cmd-probe",
                "payload": {"question": "q"}, "user_id": "u", "thread_id": "t",
                "channel": "console"}

    def test_bound_run_returned_without_new_submit(self, monkeypatch):
        from opensearch_pipeline.routes import agent as agent_routes

        class _Exec:
            def submit(self, *a, **k):
                raise AssertionError("不得重驱第二个 run")

        store = SimpleNamespace(
            find_run_by_dispatch_command=lambda cid: {"run_id": "run-original",
                                                      "status": "running"})
        monkeypatch.setattr(agent_routes, "_get_runtime",
                            lambda: (None, None, _Exec(), store))
        run_id = agent_routes._dispatch_recovered_submit(self._cmd())
        assert run_id == "run-original"          # rebind 收敛，零新 run

    def test_no_bound_run_proceeds_to_admission(self, monkeypatch):
        """未命中锚 → 走原重驱路径（这里用全局并发闸截断证明「继续往下走了」）。"""
        from opensearch_pipeline.agent_runtime.durable_dispatcher import (
            DispatchRetryLater)
        from opensearch_pipeline.routes import agent as agent_routes

        store = SimpleNamespace(find_run_by_dispatch_command=lambda cid: None)
        monkeypatch.setattr(agent_routes, "_get_runtime",
                            lambda: (None, None, None, store))
        monkeypatch.setattr(agent_routes, "_global_admission_full", lambda s: True)
        with pytest.raises(DispatchRetryLater):
            agent_routes._dispatch_recovered_submit(self._cmd())


# ═════════════════ RB-RT-02：终态 durable-first ═════════════════


class _TerminalStore:
    """transition 可编程 + get_run 可编程 + 调用顺序留痕。"""

    def __init__(self, cas=True, actual=None):
        self.cas = cas
        self.actual = actual
        self.ops = []
        self._n = 0

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def append_step(self, run_id, step):
        return 1

    def transition(self, run_id, frm, to):
        self.ops.append(("cas", frm, to))
        return self.cas

    def consume_budget(self, run_id, **kw):
        return {"turns_used": 0, "tool_calls_used": 0, "tokens_used": 0}

    def get_run(self, run_id):
        self.ops.append(("read", run_id))
        return {"status": self.actual} if self.actual else None


class _SpyHandle:
    def __init__(self):
        self.emitted = []
        self._on_failure = None

    def _emit(self, ev):
        self.emitted.append(ev)


def _mk_executor(store):
    return ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("ok"),
                               max_concurrent=2)


class TestTerminalDurableFirst:
    def test_cas_win_emits_after_persist(self):
        store = _TerminalStore(cas=True)
        ex = _mk_executor(store)
        h = _SpyHandle()
        seq = []
        store.ops = seq                       # cas 落痕
        h.emitted = _EmitLog(seq)             # emit 落同一序列
        assert ex._terminal_fail_durable("r1", h, "boom") is True
        kinds = [op[0] for op in seq]
        assert kinds.index("cas") < kinds.index("emit")   # 先落库后宣告

    def test_cas_lost_unknown_truth_emits_nothing(self):
        """评审探针翻绿：CAS false 且真相未知 → **不发 run_failed**（不制造双真相）。"""
        store = _TerminalStore(cas=False, actual="running")
        ex = _mk_executor(store)
        h = _SpyHandle()
        assert ex._terminal_fail_durable("r1", h, "budget-exceeded") is False
        assert h.emitted == []

    def test_cas_lost_succeeded_winner_stays_silent(self):
        store = _TerminalStore(cas=False, actual="succeeded")
        ex = _mk_executor(store)
        h = _SpyHandle()
        assert ex._terminal_fail_durable("r1", h, "boom") is False
        assert h.emitted == []

    def test_cas_lost_other_terminal_declares_db_truth(self):
        store = _TerminalStore(cas=False, actual="cancelled")
        ex = _mk_executor(store)
        h = _SpyHandle()
        ex._terminal_fail_durable("r1", h, "boom")
        assert len(h.emitted) == 1
        assert "cancelled" in h.emitted[0].error   # 宣告的是库中事实，不是本方臆断

    def test_cancel_path_no_failure_callback(self):
        store = _TerminalStore(cas=True)
        ex = _mk_executor(store)
        h = _SpyHandle()
        called = []
        h._on_failure = lambda err: called.append(err)
        ex._terminal_fail_durable("r1", h, "用户取消", to="cancelled", notify=False)
        assert called == []                      # cancel 历来不走失败侧回调
        assert ("cas", "running", "cancelled") in store.ops


class _EmitLog(list):
    """把 emit 记进共享序列（与 store.ops 同一时间线做次序断言）。"""

    def __init__(self, seq):
        super().__init__()
        self._seq = seq

    def append(self, ev):
        self._seq.append(("emit", ev))
        super().append(ev)


class TestEndToEndFailurePath:
    def test_loop_exception_durable_first_when_cas_lost(self):
        """整链探针：loop 抛异常 + CAS 恒 False + 现状 running ⇒ SSE 不出现 run_failed。"""
        store = _TerminalStore(cas=False, actual="running")

        def _boom(msgs, tools):
            raise RuntimeError("provider down")

        ex = _mk_executor(store)
        handle = ex.submit(_ctx(thread="t-e2e"), DefaultAgentLoop(_boom),
                           [{"role": "user", "content": "q"}], [])
        events = list(handle.events())
        ex.shutdown()
        assert not any(isinstance(e, RunFailed) for e in events)

    def test_loop_exception_cas_win_still_fails_loud(self):
        store = _TerminalStore(cas=True)

        def _boom(msgs, tools):
            raise RuntimeError("provider down")

        ex = _mk_executor(store)
        handle = ex.submit(_ctx(thread="t-e2e2"), DefaultAgentLoop(_boom),
                           [{"role": "user", "content": "q"}], [])
        events = list(handle.events())
        ex.shutdown()
        assert any(isinstance(e, RunFailed) for e in events)
        assert ("cas", "running", "failed") in store.ops

    def test_happy_path_unchanged(self):
        store = _TerminalStore(cas=True)
        ex = _mk_executor(store)
        loop = DefaultAgentLoop(lambda m, t: ModelTurn(
            text="答案", usage=Usage(tokens_prompt=1, tokens_completion=1)))
        handle = ex.submit(_ctx(thread="t-ok"), loop,
                           [{"role": "user", "content": "q"}], [])
        events = list(handle.events())
        ex.shutdown()
        assert any(isinstance(e, RunCompleted) for e in events)
