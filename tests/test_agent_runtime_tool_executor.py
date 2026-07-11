# -*- coding: utf-8 -*-
"""
test_agent_runtime_tool_executor.py — ToolExecutor 中间件栈（Task 17 / 报告 §4⑥/§9.3）

覆盖:执行成功记 executing→succeeded · 幂等命中复用回执不执行 · 熔断打开快速失败 ·
可重试错误重试 / 非可重试不重试 · 超时失败。tool_invocation 落库用假 store 断言。
"""
import time

from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec
from opensearch_pipeline.agent_runtime.tool_executor import (
    ToolExecutor,
    _ToolBreaker,
    drain_read_trace,
)


class _Store:
    def __init__(self, succeeded_hit=None):
        self.invocations = []
        self._hit = succeeded_hit

    def record_invocation(self, run_id, step_no, **kw):
        iid = f"inv{len(self.invocations) + 1}"
        self.invocations.append({"id": iid, "status": kw["status"]})
        return iid

    def finish_invocation(self, invocation_id, **kw):
        for inv in self.invocations:
            if inv["id"] == invocation_id:
                inv.update(kw)

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return self._hit


class _Tool:
    def __init__(self, spec, fn):
        self.spec = spec
        self._fn = fn
        self.calls = 0

    def run(self, ctx, args, idempotency_key=None):
        self.calls += 1
        return self._fn(args)


def _spec(risk=RiskLevel.READ_ONLY, idem="none", timeout_s=30.0, max_retries=0, name="t"):
    return ToolSpec(name=name, version="1.0.0", description="d", input_schema={"type": "object"},
                    output_schema={"type": "object"}, risk_level=risk, permission_scope="kb.search",
                    data_classification="internal", idempotency=idem, timeout_s=timeout_s,
                    max_retries=max_retries)


def _exec(store=None, breaker=None):
    return ToolExecutor(store or _Store(), breaker=breaker)


def _call(ex, tool, args=None, **kw):
    base = dict(run_id="r", step_no=1, policy_decision="allow", policy_id="p")
    base.update(kw)
    return ex.execute(None, tool, args or {}, **base)


def test_execute_success_records():
    store = _Store()
    tool = _Tool(_spec(), lambda a: ToolResult.text_ok("ok"))
    r = _call(_exec(store), tool)
    assert r.status == "succeeded" and tool.calls == 1
    assert drain_read_trace()                       # READ_ONLY trace 异步：断言前排水
    assert store.invocations[0]["status"] == "succeeded"


def test_idempotency_hit_reuses_no_execute():
    store = _Store(succeeded_hit={"invocation_id": "old", "result_digest": "d",
                                  "receipt_json": '{"order":"SO-1"}'})
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="u8"),
                 lambda a: ToolResult.ok(receipt={"order": "SO-2"}))
    r = _call(_exec(store), tool, idempotency_key="k1")
    assert r.status == "succeeded" and tool.calls == 0        # 幂等命中,未执行
    assert r.receipt == {"order": "SO-1"}                     # 复用旧回执


def test_circuit_breaker_open_fails_fast():
    br = _ToolBreaker(threshold=1, cooldown_s=100)
    br.record_fail("t")                                        # 打开
    tool = _Tool(_spec(), lambda a: ToolResult.text_ok("ok"))
    r = _call(_exec(_Store(), br), tool)
    assert r.status == "failed" and "熔断" in r.error and tool.calls == 0


def test_retry_on_retryable():
    calls = {"n": 0}

    def fn(a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("connection reset")
        return ToolResult.text_ok("ok")

    r = _call(_exec(), _Tool(_spec(max_retries=1), fn))
    assert r.status == "succeeded" and calls["n"] == 2         # 重试一次成功


def test_non_retryable_no_retry():
    calls = {"n": 0}

    def fn(a):
        calls["n"] += 1
        raise ValueError("bad input")                          # 非可重试

    r = _call(_exec(), _Tool(_spec(max_retries=2), fn))
    assert r.status == "failed" and calls["n"] == 1            # 不重试


def test_timeout_fails_and_records():
    def slow(a):
        time.sleep(0.5)
        return ToolResult.text_ok("ok")

    store = _Store()
    r = _call(_exec(store), _Tool(_spec(timeout_s=0.1), slow))
    assert r.status == "failed"
    assert drain_read_trace()                       # READ_ONLY trace 异步：断言前排水
    assert store.invocations[0]["status"] == "failed"


# ── READ_ONLY trace/审计异步化（2026-07-11 延迟优化）──────────────────────────────
class _SlowStore(_Store):
    """每笔 trace 写 sleep——同步路径会把延迟叠进 execute()，异步路径不会。"""

    def __init__(self, delay=0.15):
        super().__init__()
        self._d = delay

    def record_invocation(self, run_id, step_no, **kw):
        time.sleep(self._d)
        return super().record_invocation(run_id, step_no, **kw)

    def finish_invocation(self, invocation_id, **kw):
        time.sleep(self._d)
        super().finish_invocation(invocation_id, **kw)


class _SpyAudit:
    def __init__(self):
        self.calls = []

    def record(self, ctx, **kw):
        self.calls.append(kw)


def test_read_trace_async_off_critical_path():
    """READ_ONLY：慢 trace 店（0.15s×2）不再拖慢 execute()；排水后行齐且 FIFO 保
    record→finish 顺序（终态 succeeded + 回执在）。"""
    store = _SlowStore()
    audit = _SpyAudit()
    ex = ToolExecutor(store, audit=audit)
    tool = _Tool(_spec(), lambda a: ToolResult.ok(
        content=[], receipt={"chunk_count": 3}))
    t0 = time.monotonic()
    r = _call(ex, tool)
    wall = time.monotonic() - t0
    assert r.status == "succeeded"
    assert wall < 0.12, f"trace 应已挪出关键路径，实测 {wall:.3f}s"
    assert drain_read_trace()
    assert store.invocations[0]["status"] == "succeeded"
    assert '"chunk_count": 3' in (store.invocations[0].get("receipt_json") or "")
    assert len(audit.calls) == 1 and audit.calls[0]["risk_level"] == "read_only"


def test_read_trace_flag_off_keeps_sync(monkeypatch):
    """RAG_AGENT_ASYNC_READ_TRACE=false → 与旧行为一致：execute 返回时行已同步落库。"""
    monkeypatch.setenv("RAG_AGENT_ASYNC_READ_TRACE", "false")
    store = _Store()
    r = _call(_exec(store), _Tool(_spec(), lambda a: ToolResult.text_ok("ok")))
    assert r.status == "succeeded"
    assert store.invocations[0]["status"] == "succeeded"   # 无需排水


def test_key_required_read_tool_stays_sync():
    """声明 key_required 的读工具：幂等状态机依赖行同步可见 → 不走异步。"""
    store = _Store()
    tool = _Tool(_spec(idem="key_required"), lambda a: ToolResult.text_ok("ok"))
    r = _call(_exec(store), tool, idempotency_key="k1")
    assert r.status == "succeeded"
    assert store.invocations[0]["status"] == "succeeded"   # 无需排水=同步落库


def test_write_tool_stays_sync_with_flag_on():
    """写型工具：record 同步（uk 竞态语义）+ write-ahead 审计不动——异步开关与其无关。"""
    store = _Store()
    audit = _SpyAudit()
    ex = ToolExecutor(store, audit=audit)
    spec = _spec(risk=RiskLevel.HIGH_WRITE, idem="key_required")
    r = _call(ex, _Tool(spec, lambda a: ToolResult.text_ok("ok")), idempotency_key="k2")
    assert r.status == "succeeded"
    assert store.invocations[0]["status"] == "succeeded"   # 无需排水
    assert len(audit.calls) == 1 and audit.calls[0]["fail_closed"] is True
