# -*- coding: utf-8 -*-
"""
test_agent_llm_charge.py — A2 逐模型调用计费（审计复核批次1，2026-07-12）

三层覆盖：
1. 后端契约（memory + redis 同套断言）：charge_llm_call 只扣不判——全局熔断 +
   每用户日计数逐调用累加；累计 > cap 报超帽；thinking 档按 thinking_cap_weight 加权；
   与准入侧（admit_ask）共享同一份计数。
2. 模块级入口：actor 键 ``u:<user_id>``；LIMITER 无该方法/内部异常 → fail-open True。
3. make_model_fn 挂点：每次真实模型调用恰好一次扣减（多轮 / **空终轮重试** / 流式），
   超帽 → BudgetExceeded → executor 落 failed（状态诚实）；charge_llm=False（eval 豁免）
   零扣减。
"""
import fakeredis
import pytest

from opensearch_pipeline import rate_limiter as rl
from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import RunCompleted, RunFailed, Usage
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop
from opensearch_pipeline.agent_runtime.model_gateway import (
    BudgetExceeded,
    ChatResponse,
    ModelGateway,
    ToolCall,
    make_model_fn,
)
from opensearch_pipeline.agent_runtime.tool import ToolResult


# ─────────────────────────────────────────────────────────────────────────────
# 1. 后端契约（memory / redis 双后端同套断言）
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(params=["memory", "redis"])
def make_backend(request, monkeypatch):
    def _make(**env):
        monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        if request.param == "memory":
            lim = rl.ServingRateLimiter()
        else:
            lim = rl.RedisRateLimiter(fakeredis.FakeStrictRedis(decode_responses=True))
        lim.reload_limits()
        return lim
    return _make


def test_charge_within_then_over_global_cap(make_backend):
    lim = make_backend(RAG_GLOBAL_DAILY_LLM_CAP=3, RAG_RATE_USER_PER_DAY=100)
    assert lim.charge_llm_call("u:a") is True          # 1
    assert lim.charge_llm_call("u:a") is True          # 2
    assert lim.charge_llm_call("u:a") is True          # 3 = cap，仍放行（与准入 ≥cap 拒绝衔接）
    assert lim.charge_llm_call("u:a") is False         # 4 > cap → 超帽


def test_charge_shares_counter_with_admission(make_backend):
    """准入（count_llm=True）与逐调用计费扣的是同一份全局账。"""
    lim = make_backend(RAG_GLOBAL_DAILY_LLM_CAP=2, RAG_RATE_USER_PER_DAY=100,
                       RAG_RATE_USER_PER_MIN=100)
    assert lim.admit_ask("u:a", is_user=True, count_llm=True) is None   # 全局计 1
    assert lim.charge_llm_call("u:a") is True                           # 2 = cap
    assert lim.charge_llm_call("u:a") is False                          # 3 > cap


def test_charge_user_day_cap_per_actor(make_backend):
    lim = make_backend(RAG_GLOBAL_DAILY_LLM_CAP=1000, RAG_RATE_USER_PER_DAY=2)
    assert lim.charge_llm_call("u:a") is True
    assert lim.charge_llm_call("u:a") is True
    assert lim.charge_llm_call("u:a") is False         # a 的日计数超帽
    assert lim.charge_llm_call("u:b") is True          # 键按 actor 隔离


def test_charge_thinking_weighted(make_backend):
    """thinking 档按 thinking_cap_weight（默认 8）加权——与准入侧 P2-11 同口径。"""
    lim = make_backend(RAG_GLOBAL_DAILY_LLM_CAP=10, RAG_RATE_USER_PER_DAY=1000)
    assert lim.charge_llm_call("u:a", thinking=True) is True    # 8 ≤ 10
    assert lim.charge_llm_call("u:a", thinking=True) is False   # 16 > 10


def test_charge_disabled_master_switch(make_backend):
    lim = make_backend(RAG_GLOBAL_DAILY_LLM_CAP=1, RAG_RATE_LIMIT_ENABLE="false")
    for _ in range(5):
        assert lim.charge_llm_call("u:a") is True      # 总开关关：不扣不判


def test_charge_zero_caps_disable_layers(make_backend):
    lim = make_backend(RAG_GLOBAL_DAILY_LLM_CAP=0, RAG_RATE_USER_PER_DAY=0)
    for _ in range(5):
        assert lim.charge_llm_call("u:a") is True      # 两层都关：恒放行


# ─────────────────────────────────────────────────────────────────────────────
# 2. 模块级入口（actor 键 / fail-open）
# ─────────────────────────────────────────────────────────────────────────────
class _ChargeStub:
    def __init__(self, allow=None):
        self.calls = []
        self._allow = allow          # None=恒 True；list=逐次出队，耗尽 False

    def charge_llm_call(self, actor, *, weight=1, thinking=False):
        self.calls.append((actor, weight, thinking))
        if self._allow is None:
            return True
        return self._allow.pop(0) if self._allow else False


def test_module_charge_actor_key(monkeypatch):
    stub = _ChargeStub()
    monkeypatch.setattr(rl, "LIMITER", stub)
    assert rl.charge_llm_call("alice") is True
    assert rl.charge_llm_call("bob", thinking=True) is True
    assert stub.calls == [("u:alice", 1, False), ("u:bob", 1, True)]


def test_module_charge_failopen_on_missing_method(monkeypatch):
    monkeypatch.setattr(rl, "LIMITER", object())       # 无 charge_llm_call 的替身
    assert rl.charge_llm_call("alice") is True


def test_module_charge_failopen_on_exception(monkeypatch):
    class _Boom:
        def charge_llm_call(self, *a, **k):
            raise RuntimeError("backend down")
    monkeypatch.setattr(rl, "LIMITER", _Boom())
    assert rl.charge_llm_call("alice") is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. make_model_fn 挂点（多轮 / 空终轮重试 / 流式 / 超帽 / eval 豁免）
# ─────────────────────────────────────────────────────────────────────────────
def _ctx(user="u1", max_turns=8):
    return ExecutionContext.create(request_id="r", user_id=user, acl_groups=["g"],
                                   roles=["employee"], channel="console", thread_id="t",
                                   budget=RunBudget(max_turns=max_turns))


class _Provider:
    """脚本化 provider：按序出队 ChatResponse。"""

    name = "dashscope"

    def __init__(self, responses):
        self._r = list(responses)
        self.calls = 0

    def capabilities(self, m):
        return None

    def chat(self, model, req):
        self.calls += 1
        return self._r.pop(0)


def _resp(text="", tool_calls=None):
    return ChatResponse(text=text, tool_calls=tool_calls or [], usage=Usage(), model="m")


def _gateway(responses):
    return ModelGateway({"dashscope": _Provider(responses)},
                        routes={"light": [("dashscope", "m")], "high": [("dashscope", "m")]})


def _drive_to_completion(loop, ctx):
    """驱动 loop 到终事件（tool_call 回注成功结果）。返回终事件。"""
    gen = loop.run(ctx, [{"role": "user", "content": "q"}], [])
    ev = next(gen)
    try:
        while True:
            if isinstance(ev, (RunCompleted, RunFailed)):
                return ev
            ev = gen.send(ToolResult.text_ok("检索结果"))
    except StopIteration:
        return ev


def test_per_call_charge_multi_turn(monkeypatch):
    """两轮 run（1 次工具轮 + 1 次终答轮）= 2 次模型调用 = 2 次扣减。"""
    stub = _ChargeStub()
    monkeypatch.setattr(rl, "LIMITER", stub)
    gw = _gateway([
        _resp(tool_calls=[ToolCall(id="c1", name="knowledge_search", arguments={"query": "x"})]),
        _resp(text="答案"),
    ])
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gw, ctx, "light", stream=False))
    ev = _drive_to_completion(loop, ctx)
    assert isinstance(ev, RunCompleted) and ev.final_text == "答案"
    assert len(stub.calls) == 2 and all(a == "u:u1" for a, _, _ in stub.calls)


def test_empty_final_retries_each_charged(monkeypatch):
    """空终轮重试是**独立模型调用**：2 次空响应 + 1 次真答 = 3 次扣减（复核 A2 核心：
    executor 事件层看不见这些调用，挂点必须在 model_fn/gateway 层才数得准）。"""
    stub = _ChargeStub()
    monkeypatch.setattr(rl, "LIMITER", stub)
    gw = _gateway([_resp(text=""), _resp(text=""), _resp(text="终于有答案")])
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gw, ctx, "light", stream=False))
    ev = _drive_to_completion(loop, ctx)
    assert isinstance(ev, RunCompleted) and ev.final_text == "终于有答案"
    assert len(stub.calls) == 3


def test_stream_mode_charges_per_call(monkeypatch):
    """流式 model_fn（生成器形态）同样逐调用扣减（provider 无 chat_stream → 同步退化，
    计费在生成器体首行，首个 next() 即扣）。"""
    stub = _ChargeStub()
    monkeypatch.setattr(rl, "LIMITER", stub)
    gw = _gateway([
        _resp(tool_calls=[ToolCall(id="c1", name="knowledge_search", arguments={"query": "x"})]),
        _resp(text="答案"),
    ])
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gw, ctx, "light", stream=True))
    ev = _drive_to_completion(loop, ctx)
    assert isinstance(ev, RunCompleted)
    assert len(stub.calls) == 2


def test_thinking_tier_flag_passed(monkeypatch):
    stub = _ChargeStub()
    monkeypatch.setattr(rl, "LIMITER", stub)
    gw = _gateway([_resp(text="答案")])
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gw, ctx, "high", stream=False))
    ev = _drive_to_completion(loop, ctx)
    assert isinstance(ev, RunCompleted)
    assert stub.calls == [("u:u1", 1, True)]           # high 档 → thinking=True


def test_charge_llm_false_eval_exemption(monkeypatch):
    stub = _ChargeStub()
    monkeypatch.setattr(rl, "LIMITER", stub)
    gw = _gateway([_resp(text="答案")])
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gw, ctx, "light", stream=False, charge_llm=False))
    ev = _drive_to_completion(loop, ctx)
    assert isinstance(ev, RunCompleted)
    assert stub.calls == []                            # eval 豁免：零扣减


def test_resume_segment_charges(monkeypatch):
    """审批续跑段的模型调用同样逐调用扣减（复核「A2 加重」点：此前 /approve 续跑
    以 count_llm=False 准入且续跑段对熔断零扣减）。重提 pending call 本身不是模型
    调用、不扣；续跑后的下一模型轮扣 1。"""
    from opensearch_pipeline.agent_runtime.approval import Approved
    stub = _ChargeStub()
    monkeypatch.setattr(rl, "LIMITER", stub)
    gw = _gateway([_resp(text="续跑后的答案")])
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gw, ctx, "light", stream=False))
    state = {"messages": [{"role": "user", "content": "q"}],
             "pending_call": {"call_id": "c1", "tool_name": "knowledge_search",
                              "arguments": {"query": "x"}},
             "turn": 0, "remaining_calls": []}
    gen = loop.resume(ctx, state, Approved(), tools=[])
    ev = next(gen)                                     # 重提 pending call（非模型调用）
    assert ev.tool_name == "knowledge_search" and stub.calls == []
    ev = gen.send(ToolResult.text_ok("检索结果"))        # run(start_turn=1) → 模型调用
    assert isinstance(ev, RunCompleted) and ev.final_text == "续跑后的答案"
    assert len(stub.calls) == 1


def test_over_cap_raises_budget_exceeded_before_call(monkeypatch):
    """超帽的那次调用**不发出去**（先扣后调）：provider 只被调 1 次。"""
    stub = _ChargeStub(allow=[True, False])
    monkeypatch.setattr(rl, "LIMITER", stub)
    provider = _Provider([
        _resp(tool_calls=[ToolCall(id="c1", name="knowledge_search", arguments={"query": "x"})]),
        _resp(text="不该到达"),
    ])
    gw = ModelGateway({"dashscope": provider}, routes={"light": [("dashscope", "m")]})
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gw, ctx, "light", stream=False))
    gen = loop.run(ctx, [{"role": "user", "content": "q"}], [])
    next(gen)                                          # 第 1 调：工具轮（放行）
    with pytest.raises(BudgetExceeded):
        gen.send(ToolResult.text_ok("检索结果"))         # 第 2 调：超帽 → 不发调用
    assert provider.calls == 1


def test_over_cap_run_fails_honestly_via_executor(monkeypatch):
    """executor 全链：超帽 → run 落 failed（durable 迁移 running→failed）+ RunFailed 事件
    （复用预算超限的诚实收尾姿态）。"""
    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor

    stub = _ChargeStub(allow=[False])
    monkeypatch.setattr(rl, "LIMITER", stub)

    class _Store:
        def __init__(self):
            self.transitions = []

        def create_run(self, ctx, profile):
            return "run1"

        def transition(self, run_id, frm, to):
            self.transitions.append((frm, to))
            return True

        def consume_budget(self, run_id, **k):
            return {}

        def append_step(self, run_id, step):
            return 1

        def heartbeat(self, run_id):
            pass

    store = _Store()
    executor = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"),
                                   max_concurrent=1)
    gw = _gateway([_resp(text="不该到达")])
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gw, ctx, "light", stream=False))
    try:
        handle = executor.submit(ctx, loop, [{"role": "user", "content": "q"}], [])
        events = list(handle.events())
    finally:
        executor.shutdown()
    fails = [e for e in events if isinstance(e, RunFailed)]
    assert fails and "额度" in fails[0].error
    assert ("running", "failed") in store.transitions
