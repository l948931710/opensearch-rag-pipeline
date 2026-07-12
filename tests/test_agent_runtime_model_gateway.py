# -*- coding: utf-8 -*-
"""
test_agent_runtime_model_gateway.py — ModelGateway 境内多 provider（Task 12 / 报告 §7）

覆盖：category 路由 · 沿链 fallback（可重试）· 非可重试立即抛 · 链耗尽 ModelUnavailable ·
per-provider 熔断 · budget deadline fail-closed · DashScopeProvider 解析（mock _http_post）·
tool_spec_to_openai · make_model_fn 适配（text / tool_call）+ loop msgs→OpenAI 归一。
"""
from datetime import datetime, timezone

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import Usage
from opensearch_pipeline.agent_runtime.model_gateway import (
    BudgetExceeded,
    ChatRequest,
    ChatResponse,
    DashScopeProvider,
    ModelCapabilities,
    ModelError,
    ModelGateway,
    ModelUnavailable,
    ToolCall,
    _Breaker,
    _loop_msgs_to_openai,
    make_model_fn,
    tool_spec_to_openai,
)
from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolSpec

_GW_MOD = "opensearch_pipeline.agent_runtime.model_gateway._http_post"


def _ctx(budget=None):
    return ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"], roles=["e"],
                                   channel="console", thread_id="t", budget=budget)


def _resp(text="ok", tool_calls=None, model="m"):
    return ChatResponse(text=text, tool_calls=tool_calls or [], usage=Usage(), model=model)


class _FakeProvider:
    def __init__(self, name, responses=None, error=None):
        self.name = name
        self._responses = list(responses or [])
        self._error = error
        self.calls = 0

    def capabilities(self, model):
        return ModelCapabilities(True, True, False, 8192, True)

    def chat(self, model, req):
        self.calls += 1
        if self._error:
            raise self._error
        return self._responses.pop(0) if self._responses else _resp(model=model)


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


# ── 路由 / fallback / 熔断 ──────────────────────────────────────
def test_routing_picks_first_in_chain():
    p = _FakeProvider("dashscope", responses=[_resp("A")])
    gw = ModelGateway({"dashscope": p}, routes={"default": [("dashscope", "qwen3.6-plus")]})
    r = gw.complete(_ctx(), "default", ChatRequest(messages=[{"role": "user", "content": "hi"}]))
    assert r.text == "A" and p.calls == 1


def test_fallback_on_retryable():
    p1 = _FakeProvider("a", error=ModelError("boom", retryable=True))
    p2 = _FakeProvider("b", responses=[_resp("B")])
    gw = ModelGateway({"a": p1, "b": p2}, routes={"default": [("a", "m1"), ("b", "m2")]},
                      max_retries=0)   # 本例测沿链 fallback（非重试）
    r = gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert r.text == "B" and p1.calls == 1 and p2.calls == 1


def test_non_retryable_raises_no_fallback():
    p1 = _FakeProvider("a", error=ModelError("auth", retryable=False))
    p2 = _FakeProvider("b", responses=[_resp("B")])
    gw = ModelGateway({"a": p1, "b": p2}, routes={"default": [("a", "m1"), ("b", "m2")]})
    with pytest.raises(ModelError):
        gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert p2.calls == 0


def test_chain_exhausted_raises_unavailable():
    p = _FakeProvider("a", error=ModelError("x", retryable=True))
    gw = ModelGateway({"a": p}, routes={"default": [("a", "m")]}, max_retries=0)
    with pytest.raises(ModelUnavailable):
        gw.complete(_ctx(), "default", ChatRequest(messages=[]))


def test_unknown_category_falls_back_to_default_tier():
    p = _FakeProvider("a", responses=[_resp("D")])
    gw = ModelGateway({"a": p}, routes={"light": [("a", "m")]})   # 未知档 → 回退默认档 light
    r = gw.complete(_ctx(), "nonexistent", ChatRequest(messages=[]))
    assert r.text == "D"


def test_thinking_tier_params_merged_into_request():
    """light 档不带 thinking 参数；high 档合入 enable_thinking/thinking_budget；调用方 extra 优先。"""
    seen = {}

    class _P:
        name = "a"

        def capabilities(self, m):
            return ModelCapabilities(True, True, False, 8192, True)

        def chat(self, model, req):
            seen["extra"] = dict(req.extra)
            return _resp("ok")

    gw = ModelGateway({"a": _P()}, routes={"light": [("a", "m")], "high": [("a", "m")]})
    gw.complete(_ctx(), "light", ChatRequest(messages=[]))
    assert seen["extra"] == {"enable_thinking": False}            # light 显式关思考（qwen3.7 默认开）
    gw.complete(_ctx(), "high", ChatRequest(messages=[]))
    assert seen["extra"].get("enable_thinking") is True and seen["extra"].get("thinking_budget", 0) > 0
    gw.complete(_ctx(), "high", ChatRequest(messages=[], extra={"thinking_budget": 99}))
    assert seen["extra"]["thinking_budget"] == 99                 # 调用方 extra 覆盖档默认


def test_breaker_opens_after_threshold():
    p = _FakeProvider("a", error=ModelError("x", retryable=True))
    br = _Breaker(threshold=2, cooldown_s=100)
    gw = ModelGateway({"a": p}, routes={"default": [("a", "m")]}, breaker=br, max_retries=0)
    for _ in range(2):
        with pytest.raises(ModelUnavailable):
            gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    calls_before = p.calls
    with pytest.raises(ModelUnavailable):
        gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert p.calls == calls_before      # 熔断打开 → 跳过 provider，不再调


class _FlakyProvider:
    """前 fail_times 次抛 retryable 429，之后成功——测同 provider 退避重试。"""

    def __init__(self, name, fail_times, resp_text="OK"):
        self.name = name
        self._fail = fail_times
        self._text = resp_text
        self.calls = 0

    def capabilities(self, model):
        return ModelCapabilities(True, True, False, 8192, True)

    def chat(self, model, req):
        self.calls += 1
        if self.calls <= self._fail:
            raise ModelError("429 throttled", retryable=True, status=429)
        return _resp(self._text, model=model)


def test_retry_then_success():
    """瞬时错 2 次后成功 → 同 provider 退避重试拿到结果（②）。"""
    p = _FlakyProvider("a", fail_times=2)
    gw = ModelGateway({"a": p}, routes={"default": [("a", "m")]},
                      max_retries=2, sleep_fn=lambda _s: None)   # 免真 sleep
    r = gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert r.text == "OK" and p.calls == 3      # 2 次失败 + 第 3 次成功


def test_retry_exhausted_then_fallback():
    """同 provider 重试耗尽 → 才沿链 fallback（②：一次瞬时错不整链挂）。"""
    p1 = _FakeProvider("a", error=ModelError("429", retryable=True, status=429))
    p2 = _FakeProvider("b", responses=[_resp("B")])
    gw = ModelGateway({"a": p1, "b": p2}, routes={"default": [("a", "m1"), ("b", "m2")]},
                      max_retries=2, sleep_fn=lambda _s: None)
    r = gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert r.text == "B" and p1.calls == 3 and p2.calls == 1   # p1 试满 3 次 → fallback p2


def test_budget_deadline_fail_closed():
    past = RunBudget.with_ttl(datetime(2020, 1, 1, tzinfo=timezone.utc), ttl_s=1)  # deadline 在 2020
    p = _FakeProvider("a", responses=[_resp("nope")])
    gw = ModelGateway({"a": p}, routes={"default": [("a", "m")]})
    with pytest.raises(BudgetExceeded):
        gw.complete(_ctx(budget=past), "default", ChatRequest(messages=[]),
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert p.calls == 0


# ── DashScopeProvider 解析（mock _http_post）─────────────────────
def test_dashscope_parse_text(monkeypatch):
    payload = {"choices": [{"message": {"content": "答案"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": "qwen3.6-plus"}
    monkeypatch.setattr(_GW_MOD, lambda url, **k: _Resp(200, payload))
    p = DashScopeProvider(api_key="k", base_url="https://x/v1")
    r = p.chat("qwen3.6-plus", ChatRequest(messages=[{"role": "user", "content": "q"}]))
    assert r.text == "答案" and not r.tool_calls
    assert r.usage.tokens_prompt == 10 and r.usage.tokens_completion == 5


def test_dashscope_parse_reasoning_content(monkeypatch):
    """思考模式：reasoning_content 被捕获进 ChatResponse.reasoning（不再丢弃）。"""
    payload = {"choices": [{"message": {"content": "最终答案", "reasoning_content": "先想一想…"},
                            "finish_reason": "stop"}], "usage": {}}
    monkeypatch.setattr(_GW_MOD, lambda url, **k: _Resp(200, payload))
    p = DashScopeProvider(api_key="k", base_url="https://x/v1")
    r = p.chat("qwen3.7-plus", ChatRequest(messages=[]))
    assert r.text == "最终答案" and r.reasoning == "先想一想…"


def test_dashscope_parse_tool_calls(monkeypatch):
    payload = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "knowledge_search", "arguments": '{"query":"x"}'}}]},
        "finish_reason": "tool_calls"}], "usage": {}}
    monkeypatch.setattr(_GW_MOD, lambda url, **k: _Resp(200, payload))
    p = DashScopeProvider(api_key="k", base_url="https://x/v1")
    r = p.chat("qwen3.6-plus", ChatRequest(messages=[]))
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "knowledge_search" and r.tool_calls[0].arguments == {"query": "x"}


def test_dashscope_http_error_retryable(monkeypatch):
    monkeypatch.setattr(_GW_MOD, lambda url, **k: _Resp(503, {}, "busy"))
    p = DashScopeProvider(api_key="k", base_url="https://x/v1")
    with pytest.raises(ModelError) as ei:
        p.chat("m", ChatRequest(messages=[]))
    assert ei.value.retryable is True and ei.value.status == 503


def test_dashscope_http_error_non_retryable(monkeypatch):
    monkeypatch.setattr(_GW_MOD, lambda url, **k: _Resp(401, {}, "unauth"))
    p = DashScopeProvider(api_key="k", base_url="https://x/v1")
    with pytest.raises(ModelError) as ei:
        p.chat("m", ChatRequest(messages=[]))
    assert ei.value.retryable is False


def test_dashscope_transport_error_retryable(monkeypatch):
    def _boom(url, **k):
        raise ConnectionError("reset")
    monkeypatch.setattr(_GW_MOD, _boom)
    p = DashScopeProvider(api_key="k", base_url="https://x/v1")
    with pytest.raises(ModelError) as ei:
        p.chat("m", ChatRequest(messages=[]))
    assert ei.value.retryable is True


# ── ToolSpec→OpenAI + make_model_fn ──────────────────────────────
def _spec():
    return ToolSpec(name="knowledge_search", version="1.0.0", description="检索知识库",
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                    output_schema={"type": "object"}, risk_level=RiskLevel.READ_ONLY,
                    permission_scope="kb.search", data_classification="internal")


def test_tool_spec_to_openai():
    d = tool_spec_to_openai(_spec())
    assert d["type"] == "function"
    assert d["function"]["name"] == "knowledge_search"
    assert d["function"]["parameters"] == _spec().input_schema


def test_make_model_fn_text():
    class _GW:
        def complete(self, ctx, cat, req, now=None):
            return ChatResponse(text="答案", tool_calls=[], usage=Usage(tokens_prompt=1, tokens_completion=2), model="m")

    mt = make_model_fn(_GW(), _ctx())([{"role": "user", "content": "q"}], [])
    assert mt.text == "答案" and not mt.tool_calls and mt.usage.total == 3


def test_make_model_fn_tool_call_and_openai_tools():
    class _GW:
        def __init__(self):
            self.captured = None

        def complete(self, ctx, cat, req, now=None):
            self.captured = req
            return ChatResponse(text="", tool_calls=[ToolCall(id="c1", name="knowledge_search",
                                                              arguments={"query": "x"})],
                                usage=Usage(), model="m")

    gw = _GW()
    mt = make_model_fn(gw, _ctx())([{"role": "user", "content": "q"}], [_spec()])
    assert len(mt.tool_calls) == 1 and mt.tool_calls[0].tool_name == "knowledge_search"
    assert mt.tool_calls[0].arguments == {"query": "x"}
    assert gw.captured.tools[0]["function"]["name"] == "knowledge_search"   # spec 转 OpenAI tool


def test_loop_msgs_to_openai_normalization():
    msgs = [{"role": "user", "content": "q"},
            {"role": "assistant", "tool_calls": [{"call_id": "c1", "name": "t", "arguments": {"a": 1}}]},
            {"role": "tool", "call_id": "c1", "content": "result"}]
    out = _loop_msgs_to_openai(msgs)
    assert out[1]["tool_calls"][0]["id"] == "c1"
    assert out[1]["tool_calls"][0]["function"]["name"] == "t"
    assert out[2]["tool_call_id"] == "c1"       # call_id → tool_call_id


# ── llm_call_log 记账（Task 18）──────────────────────────────────
def test_call_logger_records_on_success():
    calls = []
    p = _FakeProvider("dashscope", responses=[ChatResponse(
        text="A", tool_calls=[], usage=Usage(tokens_prompt=10, tokens_completion=5), model="qwen3.6-plus")])
    gw = ModelGateway({"dashscope": p}, routes={"default": [("dashscope", "qwen3.6-plus")]},
                      call_logger=lambda **kw: calls.append(kw))
    gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert len(calls) == 1
    r = calls[0]
    assert r["status"] == "ok" and r["provider"] == "dashscope" and r["category"] == "default"
    assert r["tokens_prompt"] == 10 and r["tokens_completion"] == 5
    assert r["user_id"] == "u" and r["dept_group"] == "g" and isinstance(r["latency_ms"], int)


def test_call_logger_records_each_attempt_on_fallback():
    calls = []
    p1 = _FakeProvider("a", error=ModelError("boom", retryable=True))
    p2 = _FakeProvider("b", responses=[_resp("B")])
    gw = ModelGateway({"a": p1, "b": p2}, routes={"default": [("a", "m1"), ("b", "m2")]},
                      call_logger=lambda **kw: calls.append(kw), max_retries=0)  # 测 fallback 记账
    gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert len(calls) == 2                                   # 每次尝试各记一行
    assert calls[0]["status"] == "error" and calls[1]["status"] == "ok"


def test_call_logger_failure_is_fail_open():
    def boom(**kw):
        raise RuntimeError("db down")
    p = _FakeProvider("dashscope", responses=[_resp("A")])
    gw = ModelGateway({"dashscope": p}, routes={"default": [("dashscope", "m")]}, call_logger=boom)
    assert gw.complete(_ctx(), "default", ChatRequest(messages=[])).text == "A"   # 记账炸不影响调用


# ── 真流式（chat_stream / complete_stream / make_model_fn 流式模式）──────────
def _drain(gen):
    """驱动流式生成器：返回 (deltas, 最终 ChatResponse/ModelTurn)。"""
    deltas = []
    while True:
        try:
            deltas.append(next(gen))
        except StopIteration as fin:
            return deltas, fin.value


class _SSEResp:
    status_code = 200
    text = ""

    def __init__(self, lines):
        self._lines = list(lines)
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def close(self):
        self.closed = True


def test_dashscope_chat_stream_parses_sse(monkeypatch):
    """SSE 增量：content/reasoning 逐帧 yield；tool_call 分片按 index 重组；usage 终帧入账。"""
    lines = [
        'data: {"model":"qwen3.7-plus","choices":[{"delta":{"content":"你"}}]}',
        '',                                                        # keep-alive 空行
        'data: {"choices":[{"delta":{"reasoning_content":"思考中"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"knowledge_search","arguments":"{\\"que"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"ry\\": \\"x\\"}"}}]},"finish_reason":"tool_calls"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}',
        'data: [DONE]',
    ]
    resp = _SSEResp(lines)
    monkeypatch.setattr(_GW_MOD, lambda url, **kw: resp)
    p = DashScopeProvider(api_key="k", base_url="http://x")
    deltas, final = _drain(p.chat_stream("qwen3.7-plus", ChatRequest(messages=[])))
    assert [d.text for d in deltas] == ["你", "", "好"]
    assert deltas[1].reasoning == "思考中"
    assert final.text == "你好" and final.reasoning == "思考中"
    assert final.finish_reason == "tool_calls"
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0].id == "c1"
    assert final.tool_calls[0].arguments == {"query": "x"}        # 分片重组后整体 json.loads
    assert final.usage.tokens_prompt == 11 and final.usage.tokens_completion == 7
    assert resp.closed


def test_dashscope_chat_stream_http_429_retryable(monkeypatch):
    class _R:
        status_code = 429
        text = "throttled"

        def close(self):
            pass
    monkeypatch.setattr(_GW_MOD, lambda url, **kw: _R())
    p = DashScopeProvider(api_key="k", base_url="http://x")
    with pytest.raises(ModelError) as ei:
        _drain(p.chat_stream("m", ChatRequest(messages=[])))
    assert ei.value.retryable


class _StreamProvider:
    """脚本化流式 provider：可在首字节前抛（可 fallback）或流中抛（不可重启）。"""

    def __init__(self, name, deltas=None, final=None, fail_at_start=False, fail_after=None):
        self.name = name
        self._deltas = list(deltas or [])
        self._final = final or _resp(text="".join(getattr(d, "text", "") for d in (deltas or [])))
        self._fail_at_start = fail_at_start
        self._fail_after = fail_after
        self.stream_calls = 0

    def capabilities(self, model):
        return ModelCapabilities(True, True, False, 8192, True)

    def chat(self, model, req):
        return self._final

    def chat_stream(self, model, req):
        self.stream_calls += 1
        if self._fail_at_start:
            raise ModelError("prime fail", retryable=True)
        for i, d in enumerate(self._deltas):
            if self._fail_after is not None and i == self._fail_after:
                raise ModelError("stream broken", retryable=True)
            yield d
        return self._final


def test_complete_stream_fallback_before_first_delta():
    from opensearch_pipeline.agent_runtime.model_gateway import StreamDelta
    p1 = _StreamProvider("p1", fail_at_start=True)
    p2 = _StreamProvider("p2", deltas=[StreamDelta(text="好")], final=_resp(text="好"))
    gw = ModelGateway({"p1": p1, "p2": p2},
                      routes={"default": [("p1", "m1"), ("p2", "m2")]}, max_retries=0)
    deltas, final = _drain(gw.complete_stream(_ctx(), "default", ChatRequest(messages=[])))
    assert [d.text for d in deltas] == ["好"] and final.text == "好"
    assert p1.stream_calls == 1 and p2.stream_calls == 1


def test_complete_stream_mid_stream_failure_no_restart():
    """已下发增量后的流中断：直接抛，绝不 fallback 重启（前端会看到答案重播）。"""
    from opensearch_pipeline.agent_runtime.model_gateway import StreamDelta
    p1 = _StreamProvider("p1", deltas=[StreamDelta(text="半"), StreamDelta(text="截")],
                         fail_after=1)
    p2 = _StreamProvider("p2", deltas=[StreamDelta(text="备")])
    gw = ModelGateway({"p1": p1, "p2": p2},
                      routes={"default": [("p1", "m1"), ("p2", "m2")]}, max_retries=2,
                      sleep_fn=lambda s: None)
    gen = gw.complete_stream(_ctx(), "default", ChatRequest(messages=[]))
    assert next(gen).text == "半"                              # 首增量已下发
    with pytest.raises(ModelError):
        _drain(gen)
    assert p2.stream_calls == 0, "已下发增量后绝不能换 provider 重启流"


def test_complete_stream_sync_degradation_for_non_stream_provider():
    """provider 无 chat_stream → 退化同步：零增量、返回全文（假 provider/测试桩零改动）。"""
    calls = []
    p = _FakeProvider("dashscope", responses=[ChatResponse(
        text="整段", tool_calls=[], usage=Usage(tokens_prompt=4, tokens_completion=6), model="m")])
    gw = ModelGateway({"dashscope": p}, routes={"default": [("dashscope", "m")]},
                      call_logger=lambda **kw: calls.append(kw))
    deltas, final = _drain(gw.complete_stream(_ctx(), "default", ChatRequest(messages=[])))
    assert deltas == [] and final.text == "整段"
    assert calls and calls[0]["status"] == "ok" and calls[0]["tokens_prompt"] == 4  # 记账不丢


def test_make_model_fn_stream_mode_returns_generator_of_deltas():
    """默认流式（RAG_AGENT_STREAM 未设=开）：model_fn 返回生成器，yield 增量、return ModelTurn。"""
    from opensearch_pipeline.agent_runtime.model_gateway import StreamDelta
    p = _StreamProvider("p", deltas=[StreamDelta(text="你"), StreamDelta(text="好")],
                        final=_resp(text="你好"))
    gw = ModelGateway({"p": p}, routes={"light": [("p", "m")]})
    out = make_model_fn(gw, _ctx(), "light")([{"role": "user", "content": "q"}], [])
    assert hasattr(out, "__next__")
    deltas, turn = _drain(out)
    assert [d.text for d in deltas] == ["你", "好"]
    assert turn.text == "你好" and not turn.tool_calls


def test_make_model_fn_stream_false_stays_sync(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_STREAM", "false")
    p = _FakeProvider("p", responses=[_resp(text="同步答案")])
    gw = ModelGateway({"p": p}, routes={"light": [("p", "m")]})
    mt = make_model_fn(gw, _ctx(), "light")([{"role": "user", "content": "q"}], [])
    assert not hasattr(mt, "__next__") and mt.text == "同步答案"
