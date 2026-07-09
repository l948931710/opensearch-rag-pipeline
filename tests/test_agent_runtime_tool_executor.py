# -*- coding: utf-8 -*-
"""
test_agent_runtime_tool_executor.py — ToolExecutor 中间件栈（Task 17 / 报告 §4⑥/§9.3）

覆盖:执行成功记 executing→succeeded · 幂等命中复用回执不执行 · 熔断打开快速失败 ·
可重试错误重试 / 非可重试不重试 · 超时失败。tool_invocation 落库用假 store 断言。
"""
import time

from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec
from opensearch_pipeline.agent_runtime.tool_executor import ToolExecutor, _ToolBreaker


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
    assert store.invocations[0]["status"] == "failed"
