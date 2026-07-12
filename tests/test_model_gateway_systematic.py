# -*- coding: utf-8 -*-
"""ModelGateway 系统性盲区测试批（2026-07-11 测试战役）。

对照契约面（复述见 docs/audits/model_gateway_systematic_test_2026-07-11.md）与存量
test_agent_runtime_model_gateway.py 的 30 例，本文件补未覆盖的对抗性边界：

熔断器细语义（open 跳链/冷却重闭/成功清零/线程安全冒烟）· 链路残缺（provider 未注册/
空路由）· 泛异常（非 ModelError）重试与 fallback · deadline 在重试间隙过期截停 ·
tier_params={} 零注入（serving 语义）· 流式 deadline/缺终值/泛异常中断/预增量失败计入
熔断/消费者提前挂断（GeneratorExit）· DashScope 线协议容错（坏帧/无 [DONE]/多工具分片
交织/坏 args）· 资源卫生（close 恒被调）· usage_raw 直通 · _body 边界 · default_gateway
env 覆盖与 allowlist。
"""
import threading
import time
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
    StreamDelta,
    ToolCall,
    _Breaker,
    _resp_to_turn,
    default_gateway,
)

_GW_POST = "opensearch_pipeline.agent_runtime.model_gateway._http_post"


def _ctx(budget=None):
    return ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"], roles=["e"],
                                   channel="console", thread_id="t", budget=budget)


def _resp(text="ok", model="m", usage=None, usage_raw=None):
    return ChatResponse(text=text, tool_calls=[], usage=usage or Usage(), model=model,
                        usage_raw=usage_raw or {})


class _P:
    """脚本化 provider：errors 依次抛（耗尽后回答案）；记录每次收到的 req。"""

    def __init__(self, name="p", text="ok", errors=None):
        self.name = name
        self._text = text
        self._errors = list(errors or [])
        self.calls = 0
        self.reqs = []

    def capabilities(self, model):
        return ModelCapabilities(True, True, False, 8192, True)

    def chat(self, model, req):
        self.calls += 1
        self.reqs.append(req)
        if self._errors:
            raise self._errors.pop(0)
        return _resp(self._text, model=model)


class _SP:
    """脚本化流式 provider（同存量 _StreamProvider，多 no_final/generic_exc 旋钮）。"""

    def __init__(self, name="sp", deltas=None, final=None, fail_at_start=False,
                 fail_after=None, no_final=False, generic_exc_after=None):
        self.name = name
        self._deltas = list(deltas or [])
        self._final = final or _resp("".join(d.text for d in (deltas or [])))
        self._fail_at_start = fail_at_start
        self._fail_after = fail_after
        self._no_final = no_final
        self._generic_exc_after = generic_exc_after
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
            if self._generic_exc_after is not None and i == self._generic_exc_after:
                raise RuntimeError("非 ModelError 的流中断")
            yield d
        if self._no_final:
            return None
        return self._final


def _drain(gen):
    deltas = []
    while True:
        try:
            deltas.append(next(gen))
        except StopIteration as fin:
            return deltas, fin.value


# ═══ 熔断器细语义 ═══════════════════════════════════════════════
def test_breaker_open_skips_to_next_chain_entry():
    """熔断打开的链路项被跳过、后继项正常服务（存量只测「会打开」，未测「开了之后链还活着」）。"""
    br = _Breaker(threshold=1)
    br.record_failure("a:m1")                      # 直接打开 a:m1
    pa, pb = _P("a"), _P("b", text="B")
    gw = ModelGateway({"a": pa, "b": pb}, routes={"default": [("a", "m1"), ("b", "m2")]},
                      breaker=br)
    r = gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert r.text == "B" and pa.calls == 0 and pb.calls == 1


def test_breaker_cooldown_expires_then_closes():
    br = _Breaker(threshold=1, cooldown_s=0.02)
    br.record_failure("k")
    assert br.is_open("k")
    time.sleep(0.03)
    assert not br.is_open("k"), "冷却期过后必须放行（否则一次抖动永久拉黑）"


def test_breaker_success_resets_failure_count():
    br = _Breaker(threshold=3)
    br.record_failure("k")
    br.record_failure("k")
    br.record_success("k")                         # 成功清零
    br.record_failure("k")
    br.record_failure("k")
    assert not br.is_open("k"), "成功后计数须清零：2+2 次失败不该凑满阈值 3"


def test_breaker_thread_safety_smoke():
    """并发 record/is_open 不炸（executor 多 run 共享同一 gateway/breaker）。"""
    br = _Breaker(threshold=5, cooldown_s=0.01)
    errors = []

    def _hammer(i):
        try:
            for j in range(300):
                if j % 3 == 0:
                    br.record_failure(f"k{j % 7}")
                elif j % 3 == 1:
                    br.is_open(f"k{j % 7}")
                else:
                    br.record_success(f"k{j % 7}")
        except Exception as e:   # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=_hammer, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []


# ═══ 链路残缺 / 泛异常 / deadline 截停 ═══════════════════════════
def test_unregistered_provider_skipped_then_next_serves():
    p = _P("real", text="R")
    gw = ModelGateway({"real": p}, routes={"default": [("ghost", "m"), ("real", "m")]})
    assert gw.complete(_ctx(), "default", ChatRequest(messages=[])).text == "R"


def test_all_providers_unregistered_raises_unavailable():
    gw = ModelGateway({}, routes={"default": [("ghost", "m")]})
    with pytest.raises(ModelUnavailable):
        gw.complete(_ctx(), "default", ChatRequest(messages=[]))


def test_empty_routes_raises_unavailable():
    gw = ModelGateway({"a": _P()}, routes={})
    with pytest.raises(ModelUnavailable):
        gw.complete(_ctx(), "default", ChatRequest(messages=[]))


def test_generic_exception_retries_then_falls_back():
    """非 ModelError 的泛异常（SDK bug/解析崩）：同 provider 重试、耗尽沿链——不带 retryable
    标记也不能让单 provider 异常直接打穿整个 gateway。"""
    p1 = _P("a", errors=[ValueError("x"), ValueError("x")])   # 首攻+1 次重试都炸
    p2 = _P("b", text="B")
    gw = ModelGateway({"a": p1, "b": p2}, routes={"default": [("a", "m1"), ("b", "m2")]},
                      max_retries=1, sleep_fn=lambda s: None)
    r = gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert r.text == "B" and p1.calls == 2 and p2.calls == 1


def test_deadline_expiry_between_retries_stops_retrying(monkeypatch):
    """重试间隙 deadline 过期 → 停止重试沿链收尾（fail-closed 不止在进门处生效）。"""
    p = _P("a", errors=[ModelError("x", retryable=True)] * 10)
    gw = ModelGateway({"a": p}, routes={"default": [("a", "m")]},
                      max_retries=3, sleep_fn=lambda s: None)
    gates = iter([False, False, True])             # 进门放行 · 重试1放行 · 重试2拒绝
    monkeypatch.setattr(gw, "_deadline_exceeded", lambda ctx, now=None: next(gates, True))
    with pytest.raises(ModelUnavailable):
        gw.complete(_ctx(), "default", ChatRequest(messages=[]))
    assert p.calls == 2, "deadline 过期后不得再发起第 3 次尝试"


def test_tier_params_empty_dict_injects_nothing():
    """tier_params={}（serving 收敛语义）：请求 extra 原样直通，零注入。"""
    p = _P("a")
    gw = ModelGateway({"a": p}, routes={"serving": [("a", "m")]}, tier_params={})
    gw.complete(_ctx(), "serving", ChatRequest(messages=[], extra={"stream": False}))
    assert p.reqs[0].extra == {"stream": False}


# ═══ 流式边界 ═══════════════════════════════════════════════════
def test_complete_stream_deadline_fail_closed():
    past = RunBudget.with_ttl(datetime(2020, 1, 1, tzinfo=timezone.utc), ttl_s=1)
    gw = ModelGateway({"a": _SP()}, routes={"default": [("a", "m")]})
    gen = gw.complete_stream(_ctx(budget=past), "default", ChatRequest(messages=[]),
                             now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(BudgetExceeded):
        next(gen)


def test_complete_stream_missing_final_response_is_non_retryable():
    """provider 生成器没 return ChatResponse → 非可重试 ModelError，绝不 fallback
    （零增量也一样：契约破损是代码 bug，换 provider 只会掩盖）。"""
    p1 = _SP("p1", deltas=[], no_final=True)
    p2 = _SP("p2", deltas=[StreamDelta(text="备")])
    gw = ModelGateway({"p1": p1, "p2": p2},
                      routes={"default": [("p1", "m1"), ("p2", "m2")]}, max_retries=2,
                      sleep_fn=lambda s: None)
    gen = gw.complete_stream(_ctx(), "default", ChatRequest(messages=[]))
    with pytest.raises(ModelError):
        _drain(gen)
    assert p1.stream_calls == 1 and p2.stream_calls == 0


def test_complete_stream_generic_exception_after_emit_raises_no_fallback():
    """已下发增量后的**泛异常**（非 ModelError）同样直接抛、不重启流（补 except Exception 分支）。"""
    p1 = _SP("p1", deltas=[StreamDelta(text="半"), StreamDelta(text="截")], generic_exc_after=1)
    p2 = _SP("p2", deltas=[StreamDelta(text="备")])
    gw = ModelGateway({"p1": p1, "p2": p2},
                      routes={"default": [("p1", "m1"), ("p2", "m2")]}, max_retries=2,
                      sleep_fn=lambda s: None)
    gen = gw.complete_stream(_ctx(), "default", ChatRequest(messages=[]))
    assert next(gen).text == "半"
    with pytest.raises(RuntimeError):
        _drain(gen)
    assert p2.stream_calls == 0


def test_complete_stream_success_records_terminal_usage():
    calls = []
    final = _resp("你好", usage=Usage(tokens_prompt=11, tokens_completion=4))
    p = _SP("p", deltas=[StreamDelta(text="你好")], final=final)
    gw = ModelGateway({"p": p}, routes={"default": [("p", "m")]},
                      call_logger=lambda **kw: calls.append(kw))
    _drain(gw.complete_stream(_ctx(), "default", ChatRequest(messages=[])))
    assert calls and calls[-1]["status"] == "ok"
    assert calls[-1]["tokens_prompt"] == 11 and calls[-1]["tokens_completion"] == 4


def test_complete_stream_pre_delta_failures_trip_breaker():
    br = _Breaker(threshold=2)
    p = _SP("p", fail_at_start=True)
    gw = ModelGateway({"p": p}, routes={"default": [("p", "m")]}, breaker=br, max_retries=0)
    for _ in range(2):
        with pytest.raises(ModelUnavailable):
            _drain(gw.complete_stream(_ctx(), "default", ChatRequest(messages=[])))
    with pytest.raises(ModelUnavailable):        # 第 3 次：熔断已开，直接跳过不再打 provider
        _drain(gw.complete_stream(_ctx(), "default", ChatRequest(messages=[])))
    assert p.stream_calls == 2


def test_complete_stream_consumer_close_is_silent_and_not_a_failure():
    """消费者中途挂断（SSE 客户端断开→GeneratorExit）：生成器安静收尾，不记失败、不触熔断。"""
    calls = []
    br = _Breaker(threshold=1)
    p = _SP("p", deltas=[StreamDelta(text="一"), StreamDelta(text="二")])
    gw = ModelGateway({"p": p}, routes={"default": [("p", "m")]}, breaker=br,
                      call_logger=lambda **kw: calls.append(kw))
    gen = gw.complete_stream(_ctx(), "default", ChatRequest(messages=[]))
    assert next(gen).text == "一"
    gen.close()                                    # 不抛
    assert not br.is_open("p:m"), "客户端挂断不是 provider 故障，不得计入熔断"
    assert all(c.get("status") != "error" for c in calls)


# ═══ DashScope 线协议容错 / 资源卫生 / usage_raw ═══════════════════
class _SSEResp:
    status_code = 200
    text = ""

    def __init__(self, lines, explode_after=None):
        self._lines = list(lines)
        self._explode_after = explode_after
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        for i, ln in enumerate(self._lines):
            if self._explode_after is not None and i == self._explode_after:
                raise OSError("connection reset")
            yield ln

    def close(self):
        self.closed = True


def _sse(obj):
    import json as _json
    return "data: " + _json.dumps(obj, ensure_ascii=False)


def test_chat_stream_tolerates_malformed_frames_and_missing_done(monkeypatch):
    """坏 JSON 帧跳过（fail-open）、非 data 行忽略、流末尾没有 [DONE] 也正常组装。"""
    lines = [
        "event: ping",                                   # 非 data 行
        _sse({"choices": [{"delta": {"content": "你"}}]}),
        "data: {broken-json",                            # 坏帧
        "data:",                                         # 空 payload
        _sse({"choices": [{"delta": {"content": "好"}}]}),
        _sse({"usage": {"prompt_tokens": 5, "completion_tokens": 2}, "choices": []}),
        # 无 [DONE]
    ]
    resp = _SSEResp(lines)
    monkeypatch.setattr(_GW_POST.rsplit(".", 1)[0] + "._http_post",
                        lambda *a, **k: resp, raising=True)
    p = DashScopeProvider(api_key="k", base_url="https://x")
    deltas, final = _drain(p.chat_stream("m", ChatRequest(messages=[])))
    assert [d.text for d in deltas] == ["你", "好"]
    assert final.text == "你好"
    assert final.usage.tokens_prompt == 5 and resp.closed


def test_chat_stream_interleaved_multi_toolcall_fragments(monkeypatch):
    """两个 tool_call 的 arguments 分片交织到达：按 index 各自累积重组；坏 args → {}。"""
    lines = [
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c0", "function": {"name": "search"}},
            {"index": 1, "id": "c1", "function": {"name": "lookup"}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"q":'}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": "{bad json"}}]}}]}),
        _sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"年假"}'}}]}}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]",
    ]
    monkeypatch.setattr(_GW_POST, lambda *a, **k: _SSEResp(lines), raising=True)
    p = DashScopeProvider(api_key="k", base_url="https://x")
    _, final = _drain(p.chat_stream("m", ChatRequest(messages=[])))
    assert [(t.id, t.name, t.arguments) for t in final.tool_calls] == [
        ("c0", "search", {"q": "年假"}),
        ("c1", "lookup", {}),                      # 坏 JSON args 容错为 {}
    ]
    assert final.finish_reason == "tool_calls"


def test_chat_stream_mid_stream_error_closes_response(monkeypatch):
    resp = _SSEResp([_sse({"choices": [{"delta": {"content": "半"}}]}),
                     _sse({"choices": [{"delta": {"content": "截"}}]})], explode_after=1)
    monkeypatch.setattr(_GW_POST, lambda *a, **k: resp, raising=True)
    p = DashScopeProvider(api_key="k", base_url="https://x")
    gen = p.chat_stream("m", ChatRequest(messages=[]))
    assert next(gen).text == "半"
    with pytest.raises(ModelError):
        _drain(gen)
    assert resp.closed, "流中断也必须 close 连接（连接池卫生）"


def test_usage_raw_passthrough_chat_and_stream(monkeypatch):
    rich = {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 8}}

    class _JResp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "A"}, "finish_reason": "stop"}],
                    "usage": rich}

    monkeypatch.setattr(_GW_POST, lambda *a, **k: _JResp(), raising=True)
    p = DashScopeProvider(api_key="k", base_url="https://x")
    r = p.chat("m", ChatRequest(messages=[]))
    assert r.usage_raw == rich and r.usage.tokens_prompt == 9

    monkeypatch.setattr(_GW_POST,
                        lambda *a, **k: _SSEResp([_sse({"choices": [{"delta": {"content": "A"}}]}),
                                                  _sse({"usage": rich, "choices": []}),
                                                  "data: [DONE]"]), raising=True)
    _, final = _drain(p.chat_stream("m", ChatRequest(messages=[])))
    assert final.usage_raw == rich and final.usage.tokens_completion == 3


def test_body_construction_edges(monkeypatch):
    seen = {}

    class _JResp:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "A"}}]}

    def _cap(url, **kw):
        seen["json"] = kw["json"]
        return _JResp()

    monkeypatch.setattr(_GW_POST, _cap, raising=True)
    p = DashScopeProvider(api_key="k", base_url="https://x")
    p.chat("m", ChatRequest(messages=[{"role": "user", "content": "q"}]))
    assert "max_tokens" not in seen["json"] and "tools" not in seen["json"]

    p.chat("m", ChatRequest(messages=[], max_tokens=0))
    assert "max_tokens" not in seen["json"], "max_tokens=0 视为未设（falsy 语义，记档）"

    p.chat("m", ChatRequest(messages=[], tools=[{"type": "function", "function": {}}],
                            extra={"temperature": 0.9}))
    assert seen["json"]["tool_choice"] == "auto"
    assert seen["json"]["temperature"] == 0.9, "extra 合并在最后：可覆盖顶层采样参数（记档）"


# ═══ default_gateway：env 覆盖 / allowlist ═══════════════════════
def test_default_gateway_env_overrides(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_MODEL_LIGHT", "my-light")
    monkeypatch.setenv("RAG_AGENT_THINK_HIGH", "not-an-int")   # 坏值 → 默认 1024
    monkeypatch.setenv("RAG_AGENT_THINK_XHIGH", "7")
    gw = default_gateway()
    assert gw._routes["light"] == [("dashscope", "my-light")]
    assert gw._tier_params["high"]["thinking_budget"] == 1024
    assert gw._tier_params["xhigh"]["thinking_budget"] == 7
    assert gw._tier_params["light"] == {"enable_thinking": False}


def test_default_gateway_allowlist_excluding_dashscope_fails_loud(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_PROVIDER_ALLOWLIST", "selfhosted_vllm")
    gw = default_gateway()
    with pytest.raises(ModelUnavailable):
        gw.complete(_ctx(), "light", ChatRequest(messages=[]))


# ═══ 适配层 ═══════════════════════════════════════════════════
def test_resp_to_turn_synthesizes_call_id_when_missing():
    r = ChatResponse(text="", tool_calls=[ToolCall(id="", name="t", arguments={"a": 1})],
                     usage=Usage(), model="m")
    turn = _resp_to_turn(r)
    assert turn.tool_calls[0].call_id == "call_0" and turn.tool_calls[0].arguments == {"a": 1}
