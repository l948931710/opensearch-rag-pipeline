# -*- coding: utf-8 -*-
"""R2（ARR 外审 P1-RT-03/04/06/07）回归。

03：每 provider attempt 前重查 deadline（探针「睡醒仍打一枪」翻绿）+ HTTP 超时钳到剩余；
04：按实际 provider attempt 计量（探针「attempt=2 charge=1」翻绿）；
06：挂起事务内持久化权威预算计数；
07：生产 checkpoint 加密失败不退明文。
"""
import datetime as dt
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.model_gateway import (
    BudgetExceeded, ChatRequest, ChatResponse, ModelError, ModelGateway, Usage,
    make_model_fn)


def _ctx(deadline=None):
    return ExecutionContext.create(
        request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
        channel="console", thread_id="t",
        budget=RunBudget(max_turns=8, deadline=deadline))


def _resp(text="ok"):
    return ChatResponse(text=text, tool_calls=[],
                        usage=Usage(tokens_prompt=1, tokens_completion=1),
                        model="m", finish_reason="stop")


class _FlakyProvider:
    """前 fail_n 次抛可重试 ModelError，之后成功；计数所有 chat 调用。"""

    def __init__(self, fail_n=0):
        self.calls = 0
        self.fail_n = fail_n
        self.last_req = None

    def capabilities(self, model):
        return SimpleNamespace(supports_stream=False)

    def chat(self, model, req):
        self.calls += 1
        self.last_req = req
        if self.calls <= self.fail_n:
            raise ModelError("transient", retryable=True)
        return _resp()


def _gateway(provider, max_retries=2):
    gw = ModelGateway(providers={"p": provider},
                      routes={"light": [("p", "m")]}, max_retries=max_retries)
    gw._sleep = lambda s: None
    return gw


class TestPerAttemptDeadline:
    def test_expired_before_retry_never_fires_second_call(self, monkeypatch):
        """探针翻绿：attempt1 失败后 deadline 过期 → 第二枪不出门（provider_calls==1）。"""
        prov = _FlakyProvider(fail_n=99)
        gw = _gateway(prov)
        checks = iter([False, False, False, True])   # 入口/attempt1顶/重试判/attempt2顶
        monkeypatch.setattr(gw, "_deadline_exceeded", lambda c, n=None: next(checks))
        with pytest.raises(BudgetExceeded, match="attempt 前重查"):
            gw.complete(_ctx(), "light", ChatRequest(messages=[]))
        assert prov.calls == 1

    def test_remaining_timeout_injected_per_attempt(self):
        prov = _FlakyProvider()
        gw = _gateway(prov)
        deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
        gw.complete(_ctx(deadline=deadline), "light", ChatRequest(messages=[]))
        rem = prov.last_req.extra.get("_transport_timeout_s")
        assert rem is not None and 1.0 <= rem <= 30.5

    def test_no_deadline_no_injection(self):
        prov = _FlakyProvider()
        gw = _gateway(prov)
        gw.complete(_ctx(), "light", ChatRequest(messages=[]))
        assert "_transport_timeout_s" not in prov.last_req.extra


class TestTransportTimeoutClamp:
    def test_effective_timeout_and_body_filtering(self):
        from opensearch_pipeline.agent_runtime.model_gateway import DashScopeProvider
        p = DashScopeProvider(api_key="k", base_url="https://x", timeout=60.0)
        req = ChatRequest(messages=[], extra={"_transport_timeout_s": 5.5,
                                              "enable_thinking": True})
        assert p._effective_timeout(req) == 5.5
        req2 = ChatRequest(messages=[], extra={"_transport_timeout_s": 999})
        assert p._effective_timeout(req2) == 60.0            # 钳到配置上限
        assert p._effective_timeout(ChatRequest(messages=[])) == 60.0
        body = p._body("m", req)
        assert "_transport_timeout_s" not in body            # 传输参数绝不进请求体
        assert body["enable_thinking"] is True


class TestPerAttemptCharging:
    def test_gateway_calls_hook_each_attempt(self):
        prov = _FlakyProvider(fail_n=2)
        gw = _gateway(prov, max_retries=2)
        charges = []
        gw.complete(_ctx(), "light", ChatRequest(messages=[]),
                    on_attempt=lambda: charges.append(1))
        assert prov.calls == 3 and len(charges) == 3         # 探针翻绿：逐 attempt 计量

    def test_model_fn_charges_per_attempt(self, monkeypatch):
        import opensearch_pipeline.agent_runtime.model_gateway as mg
        prov = _FlakyProvider(fail_n=1)
        gw = _gateway(prov)
        charges = []
        monkeypatch.setattr(mg, "_charge_model_call",
                            lambda ctx, cat: charges.append(cat))
        fn = make_model_fn(gw, _ctx(), "light", stream=False, charge_llm=True)
        fn([{"role": "user", "content": "q"}], [])
        assert prov.calls == 2 and len(charges) == 2

    def test_legacy_gateway_stub_single_charge_no_double(self, monkeypatch):
        """旧签名 gateway（无 on_attempt）→ 回退每逻辑轮 1 次，不双扣。"""
        import opensearch_pipeline.agent_runtime.model_gateway as mg
        charges = []
        monkeypatch.setattr(mg, "_charge_model_call",
                            lambda ctx, cat: charges.append(cat))

        class _LegacyGw:
            def complete(self, ctx, category, req):
                return _resp()

        fn = make_model_fn(_LegacyGw(), _ctx(), "light", stream=False, charge_llm=True)
        fn([{"role": "user", "content": "q"}], [])
        assert charges == ["light"]


class TestSuspendBudgetPersist:
    def test_atomic_receives_budget_counts(self):
        from opensearch_pipeline.agent_runtime.events import RunSuspended
        from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
        from opensearch_pipeline.agent_runtime.tool import ToolResult
        seen = {}

        class _S:
            def create_run(self, ctx, profile):
                return "r1"

            def suspend_run_atomic(self, run_id, blob, digest, step_payload=None,
                                   extra_writer=None, budget_counts=None):
                seen.update(budget_counts or {})
                return "cp1", True

        ex = ThreadedRunExecutor(_S(), lambda c, e: ToolResult.text_ok("x"),
                                 max_concurrent=1)
        ev = RunSuspended(state_messages=[], pending_call={"tool_name": "t"},
                          turn_index=1, remaining_calls=[])
        cp, aid, ok = ex._persist_suspend(
            _ctx(), "r1", ev, budget_counts={"turns": 3, "tool_calls": 2, "tokens": 500})
        assert ok and seen == {"turns": 3, "tool_calls": 2, "tokens": 500}

    def test_legacy_store_without_kwarg_still_works(self):
        from opensearch_pipeline.agent_runtime.events import RunSuspended
        from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
        from opensearch_pipeline.agent_runtime.tool import ToolResult

        class _S:
            def create_run(self, ctx, profile):
                return "r1"

            def suspend_run_atomic(self, run_id, blob, digest, step_payload=None,
                                   extra_writer=None):
                return "cp1", True

        ex = ThreadedRunExecutor(_S(), lambda c, e: ToolResult.text_ok("x"),
                                 max_concurrent=1)
        ev = RunSuspended(state_messages=[], pending_call={"tool_name": "t"},
                          turn_index=1, remaining_calls=[])
        cp, aid, ok = ex._persist_suspend(_ctx(), "r1", ev,
                                          budget_counts={"turns": 1})
        assert ok                                             # TypeError 容错回退


class TestCheckpointProdHardening:
    def test_prod_encrypt_failure_raises(self, monkeypatch):
        from opensearch_pipeline.agent_runtime import loop as lp
        monkeypatch.setattr(lp, "_prod_like_env", lambda: True)
        monkeypatch.setattr(lp.hashlib, "sha256",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        with pytest.raises(RuntimeError, match="不许退明文"):
            lp._encrypt_blob(b"x", b"k")

    def test_dev_encrypt_failure_falls_back_plaintext(self, monkeypatch):
        from opensearch_pipeline.agent_runtime import loop as lp
        monkeypatch.setattr(lp, "_prod_like_env", lambda: False)
        monkeypatch.setattr(lp.hashlib, "sha256",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
        assert lp._encrypt_blob(b"x", b"k") is None           # dev 维持旧回退
