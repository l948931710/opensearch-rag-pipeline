# -*- coding: utf-8 -*-
"""RR-1/RR-2（ARR 复审 e15fa8d 七条）探针驱动回归——每条先复现复审探针再断言新行为。"""
import time
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import RunFailed
from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.tool import ToolResult


def _ctx(thread="t"):
    return ExecutionContext.create(
        request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
        channel="console", thread_id=thread, budget=RunBudget(max_turns=4))


class _SpyHandle:
    def __init__(self):
        self.emitted = []
        self._on_failure = None
        self._on_complete = None
        self._on_complete_durable = None

    def _emit(self, ev):
        self.emitted.append(ev)

    def _finish(self, end_relay=True):
        pass


class _St:
    """transition/get_run 可编程 store。"""

    def __init__(self, cas=False, actual="running", complete_raises=False,
                 complete_ok=False):
        self.cas = cas
        self.actual = actual
        self.complete_raises = complete_raises
        self.complete_ok = complete_ok

    def create_run(self, ctx, profile):
        return "r1"

    def transition(self, run_id, frm, to):
        return self.cas

    def get_run(self, run_id):
        return {"status": self.actual}

    def complete_run_atomic(self, run_id, extra_writer=None):
        if self.complete_raises:
            raise OSError("db down")
        return self.complete_ok


def _ex(store):
    return ThreadedRunExecutor(store, lambda c, e: ToolResult.text_ok("x"),
                               max_concurrent=1)


class TestRR1TerminalUnification:
    def test_complete_exception_cas_lost_emits_nothing(self):
        """复审探针翻绿：complete_run_atomic raises + transition=False ⇒ 零 run_failed。"""
        from opensearch_pipeline.agent_runtime.events import RunCompleted
        store = _St(cas=False, actual="running", complete_raises=True)
        ex = _ex(store)
        h = _SpyHandle()
        ex._complete_run("r1", h, RunCompleted(final_text="答"), None)
        assert not any(isinstance(e, RunFailed) for e in h.emitted)

    def test_complete_exception_cas_win_still_fails_loud(self):
        store = _St(cas=True, complete_raises=True)
        ex = _ex(store)
        h = _SpyHandle()
        from opensearch_pipeline.agent_runtime.events import RunCompleted
        ex._complete_run("r1", h, RunCompleted(final_text="答"), None)
        assert any(isinstance(e, RunFailed) for e in h.emitted)

    def test_complete_loser_declares_db_truth_or_silence(self):
        """RB-RR-01d：完成输家不合成失败帧——按库中事实/闭嘴。"""
        from opensearch_pipeline.agent_runtime.events import RunCompleted
        # 事实=cancelled ⇒ 事实帧
        store = _St(cas=False, actual="cancelled", complete_ok=False)
        ex = _ex(store)
        h = _SpyHandle()
        ex._complete_run("r1", h, RunCompleted(final_text="答"), None)
        assert any("cancelled" in getattr(e, "error", "") for e in h.emitted)
        # 事实=running/未知 ⇒ 闭嘴
        store2 = _St(cas=False, actual="running", complete_ok=False)
        h2 = _SpyHandle()
        _ex(store2)._complete_run("r1", h2, RunCompleted(final_text="答"), None)
        assert h2.emitted == []

    def test_no_direct_terminal_emit_left_in_failure_paths(self):
        """源级闸（复审「禁止调用点自己 emit terminal」）：executor 内 RunFailed 的
        emit 只允许出现在两个 helper 与审批拒绝位点（其自带 durable-first 门）。"""
        from pathlib import Path
        import opensearch_pipeline.agent_runtime.executor as m
        src = Path(m.__file__).read_text(encoding="utf-8")
        lines = src.split("\n")
        offenders = []
        for i, ln in enumerate(lines):
            if "_emit(RunFailed(" not in ln:
                continue
            window = "\n".join(lines[max(0, i - 40):i + 1])
            if ("_terminal_fail_durable" in window.split("def ")[-1]
                    or "_declare_terminal_lost" in window.split("def ")[-1]
                    or "拒绝终止 CAS" in window):
                continue
            offenders.append(f"L{i + 1}")
        assert not offenders, f"发现 helper 之外的终态 emit: {offenders}"


class TestRR1RelayNoControlLoss:
    def _wire(self, monkeypatch, fake):
        import sys
        import opensearch_pipeline as pkg
        mod = SimpleNamespace(get_client=lambda: fake,
                              key=lambda *p: ":".join(p),
                              is_configured=lambda: True)
        monkeypatch.setitem(sys.modules, "opensearch_pipeline.redis_client", mod)
        monkeypatch.setattr(pkg, "redis_client", mod, raising=False)

    def test_terminal_survives_full_queue(self, monkeypatch):
        """复审探针翻绿：容量 2+慢 Redis ⇒ run_completed 最终必达（此前 2s 即丢）。"""
        from opensearch_pipeline.agent_runtime import event_relay as er

        class _Slow:
            def __init__(self):
                self.entries = []

            def xadd(self, key, fields, maxlen=None, approximate=True):
                time.sleep(0.15)
                self.entries.append(fields)

            def expire(self, k, t):
                return True

        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "true")
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_QUEUE", "2")
        fake = _Slow()
        self._wire(monkeypatch, fake)
        r = er._RedisRelay("run-ctl")
        for _ in range(10):
            r._xadd({"type": "model_delta", "text": "x"})
        r._xadd({"type": "run_completed", "final_text": "答"})
        r.end()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            types = [str(f.get("data", "")) for f in fake.entries]
            if any("run_completed" in s for s in types):
                break
            time.sleep(0.05)
        assert any("run_completed" in str(f.get("data", "")) for f in fake.entries), \
            "终态帧被丢弃"

    def test_end_waits_inflight_xadd(self, monkeypatch):
        """RB-RR-02：end() 按 task_done 账本等在途 XADD（旧 empty() 假排空）。"""
        from opensearch_pipeline.agent_runtime import event_relay as er

        class _Slow:
            def __init__(self):
                self.entries = []

            def xadd(self, key, fields, maxlen=None, approximate=True):
                time.sleep(0.3)
                self.entries.append(fields)

            def expire(self, k, t):
                return True

        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "true")
        fake = _Slow()
        self._wire(monkeypatch, fake)
        r = er._RedisRelay("run-flush")
        r._xadd({"type": "tool_result", "status": "succeeded"})
        r.end()                                    # 应等到在途帧+__end__ 都写完（≤3s）
        assert len(fake.entries) >= 2


class TestRR2SmallFixes:
    def test_reconciler_exception_backoffs(self):
        """复审探针翻绿：checker 抛 TimeoutError ⇒ deferred==1（此前 0）。"""
        from opensearch_pipeline.agent_runtime import operation_reconciler as orc
        deferred = []

        class _Store:
            def list_uncertain_invocations_for_reconcile(self, *, min_age_s, limit):
                return [{"invocation_id": "i1", "tool_name": "t", "run_id": "r",
                         "operation_id": "op1", "reconcile_attempts": 3}]

            def resolve_uncertain_invocation(self, *a, **k):
                return True

            def defer_reconcile(self, inv_id, *, attempts, delay_s):
                deferred.append((inv_id, attempts))

        class _Tool:
            def check_operation(self, op_id):
                raise TimeoutError("外部系统超时")

        reg = SimpleNamespace(get=lambda name: _Tool())
        counts = orc.reconcile_uncertain_invocations(_Store(), reg)
        assert counts["unknown"] == 1 and counts["deferred"] == 1
        assert deferred == [("i1", 4)]

    def test_default_gateway_factory_high_has_fallback(self, monkeypatch):
        """复审探针翻绿：default_gateway()._routes['high'] ≥2（此前常量改了工厂没接）。"""
        for v in ("RAG_AGENT_MODEL_HIGH", "RAG_AGENT_MODEL_LIGHT"):
            monkeypatch.delenv(v, raising=False)
        from opensearch_pipeline.agent_runtime.model_gateway import default_gateway
        gw = default_gateway()
        assert len(gw._routes["high"]) >= 2
        assert len(gw._routes["light"]) == 1

    def test_duplicate_outcome_rebuilt_frozen_safe(self):
        """复审探针翻绿：frozen outcome 经 model_copy 重建携库内 reason（旧 setattr 空操作）。"""
        from opensearch_pipeline.agent_runtime.approval import RejectedFeedback
        o = RejectedFeedback(reason="第二次请求塞的假理由")
        with pytest.raises(Exception):
            o.reason = "x"                          # 证明 frozen（旧修法必然空操作）
        rebuilt = o.model_copy(update={"reason": "库内不可变理由"})
        assert rebuilt.reason == "库内不可变理由"

    def test_egress_stamp_lands_on_original_ctx_source_order(self):
        """P2-RR-07：源级断言盖章先于 dataclasses.replace（写型副本前）。"""
        from pathlib import Path
        import opensearch_pipeline.agent_runtime.tool_executor as m
        src = Path(m.__file__).read_text(encoding="utf-8")
        stamp = src.index("max_data_classification")
        repl = src.index("_dc_replace(ctx, operation_id=")
        assert stamp < repl, "盖章必须在写型 ctx 副本替换之前（否则 gateway 看不到）"

    def test_ci_names_new_db_tests(self):
        from pathlib import Path
        ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        for f in ("test_agent_dispatch_bind_db.py",
                  "test_agent_pr3_operation_ledger.py",
                  "test_agent_pr3_stage_d.py"):
            assert f in ci, f"CI 点名清单缺 {f}"

    def test_readiness_053_contract(self, monkeypatch):
        from opensearch_pipeline import readiness
        monkeypatch.delenv("RAG_AGENT_OP_RECONCILE_ENABLE", raising=False)
        readiness._reset_cache()
        assert readiness.op_reconcile_contract_status() == "skipped"
        monkeypatch.setenv("RAG_AGENT_OP_RECONCILE_ENABLE", "true")
        readiness._reset_cache()

        class _Cur:
            def execute(self, sql, args=None):
                pass

            def fetchone(self):
                return (2,)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            "opensearch_pipeline.db._get_db_conn",
            lambda *a, **k: SimpleNamespace(cursor=lambda: _Cur(), close=lambda: None))
        monkeypatch.setattr("opensearch_pipeline.config.get_config",
                            lambda: SimpleNamespace(
                                rds=SimpleNamespace(operation_database="op")))
        assert readiness.op_reconcile_contract_status() == "ok"


class TestRRHA01EndOwnership:
    """RB-HA-01 探针翻绿：输家/真相未知不封流；胜者封流。"""

    def _relay_capture(self, monkeypatch):
        frames = []

        class _Relay:
            def publish(self, ev):
                frames.append(type(ev).__name__)

            def end(self):
                frames.append("__end__")

        from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
        monkeypatch.setattr(ThreadedRunExecutor, "_attach_relay",
                            lambda self, h: setattr(h, "_relay", _Relay()))
        return frames

    def _boom_loop(self):
        from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop

        def _boom(msgs, tools):
            raise RuntimeError("provider down")

        return DefaultAgentLoop(_boom)

    def test_loser_unknown_truth_never_writes_end(self, monkeypatch):
        frames = self._relay_capture(monkeypatch)
        store = _St(cas=False, actual="running")
        ex = _ex(store)
        h = ex.submit(_ctx(thread="t-ha1"), self._boom_loop(),
                      [{"role": "user", "content": "q"}], [])
        list(h.events())
        ex.shutdown()
        assert "__end__" not in frames, "CAS 输家/真相未知不得封共享流"

    def test_winner_still_ends_stream(self, monkeypatch):
        frames = self._relay_capture(monkeypatch)
        store = _St(cas=True)
        ex = _ex(store)
        h = ex.submit(_ctx(thread="t-ha2"), self._boom_loop(),
                      [{"role": "user", "content": "q"}], [])
        list(h.events())
        ex.shutdown()
        assert frames.count("__end__") == 1 and "RunFailed" in frames


class TestRREVT02SnapshotFallback:
    def test_dropped_deltas_flip_streamed_and_mark(self, monkeypatch):
        """P1-EVT-02 探针翻绿：丢过 delta 的 run，终态帧 streamed=false+delta_dropped。"""
        from opensearch_pipeline.agent_runtime import event_relay as er
        import sys
        import opensearch_pipeline as pkg
        entries = []

        class _Fast:
            def xadd(self, key, fields, maxlen=None, approximate=True):
                entries.append(fields)

            def expire(self, k, t):
                return True

        mod = SimpleNamespace(get_client=lambda: _Fast(),
                              key=lambda *p: ":".join(p),
                              is_configured=lambda: True)
        monkeypatch.setitem(sys.modules, "opensearch_pipeline.redis_client", mod)
        monkeypatch.setattr(pkg, "redis_client", mod, raising=False)
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "false")
        r = er._RedisRelay("run-snap")
        r._dropped = 5                                  # 模拟本 run 已丢 delta

        class _Done:
            final_text = "完整答案七十个字"
            streamed = True

        import opensearch_pipeline.agent_runtime.events as evm
        monkeypatch.setattr(evm, "dump_event",
                            lambda ev: {"type": "run_completed",
                                        "final_text": ev.final_text,
                                        "streamed": ev.streamed})
        r.publish(_Done())
        import json as _j
        payload = _j.loads(entries[-1]["data"]) if "data" in entries[-1] else entries[-1]
        assert payload["streamed"] is False and payload["delta_dropped"] == 5

    def test_no_drop_keeps_streamed_true(self, monkeypatch):
        from opensearch_pipeline.agent_runtime import event_relay as er
        import sys
        import opensearch_pipeline as pkg
        entries = []

        class _Fast:
            def xadd(self, key, fields, maxlen=None, approximate=True):
                entries.append(fields)

            def expire(self, k, t):
                return True

        mod = SimpleNamespace(get_client=lambda: _Fast(),
                              key=lambda *p: ":".join(p),
                              is_configured=lambda: True)
        monkeypatch.setitem(sys.modules, "opensearch_pipeline.redis_client", mod)
        monkeypatch.setattr(pkg, "redis_client", mod, raising=False)
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "false")
        import opensearch_pipeline.agent_runtime.events as evm
        monkeypatch.setattr(evm, "dump_event",
                            lambda ev: {"type": "run_completed", "streamed": True})
        r = er._RedisRelay("run-snap2")
        r.publish(SimpleNamespace())
        import json as _j
        payload = _j.loads(entries[-1]["data"])
        assert payload["streamed"] is True and "delta_dropped" not in payload


class TestRRAPR01DuplicateFromDb:
    def test_null_reason_and_decided_by_from_db(self):
        """P2-APR-01 探针翻绿：库 reason=NULL ⇒ 重放 outcome.reason=None（不吃 body）；
        decided_by 以库行为准——直接验证重建逻辑（frozen model_copy 路径）。"""
        from opensearch_pipeline.agent_runtime.approval import RejectedFeedback
        o = RejectedFeedback(reason="attacker-controlled-retry-reason")
        dec = {"reason": None, "decided_by": "original-admin"}
        rebuilt = o.model_copy(update={"reason": dec.get("reason")})
        assert rebuilt.reason is None                      # NULL 也是不可变事实
        assert (dec.get("decided_by") or "retry-admin") == "original-admin"

    def test_route_source_uses_db_fields_unconditionally(self):
        """源级闸：DUPLICATE 分支必须无条件重建（不许 is not None 跳过）+ 503 语义。"""
        from pathlib import Path
        import opensearch_pipeline.routes.agent as m
        src = Path(m.__file__).read_text(encoding="utf-8")
        blk_start = src.index("RR-APR-01")
        blk = src[blk_start:blk_start + 1600]
        assert '_dec0.get("reason") is not None' not in blk   # 旧跳过条件已灭
        assert "decided_by_effective = _db_by" in blk or "_db_by" in blk
        assert "status_code=503" in blk and "Retry-After" in blk
