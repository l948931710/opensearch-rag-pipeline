# -*- coding: utf-8 -*-
"""
test_agent_runtime_loop.py — DefaultAgentLoop（Task 11 / 执行模型 §2）

覆盖：一次性 final · **B2 的 Generator.send 回注 ToolResult**（tool_call → send → 续跑）·
max_turns 超限 RunFailed · checkpoint json 编解码 · resume 四处置（terminate / approved 续跑）。
"""
from types import SimpleNamespace

from opensearch_pipeline.agent_runtime.approval import Approved, RejectedFeedback, RejectedTerminate
from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import RunCompleted, RunFailed, ToolCallProposed, Usage
from opensearch_pipeline.agent_runtime.loop import (
    DefaultAgentLoop,
    ModelTurn,
    ProposedCall,
    decode_checkpoint,
    encode_checkpoint,
)
from opensearch_pipeline.agent_runtime.tool import ToolResult


def _ctx(max_turns=8):
    return ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"], roles=["employee"],
                                   channel="console", thread_id="t", budget=RunBudget(max_turns=max_turns))


def _scripted(turns):
    box = {"i": 0}

    def _fn(msgs, tools):
        t = turns[box["i"]]
        box["i"] += 1
        return t

    return _fn


def test_final_text_immediately():
    loop = DefaultAgentLoop(_scripted([ModelTurn(text="答案", usage=Usage(tokens_prompt=5, tokens_completion=7))]))
    events = list(loop.run(_ctx(), [{"role": "user", "content": "hi"}], []))
    assert len(events) == 1 and isinstance(events[0], RunCompleted)
    assert events[0].final_text == "答案" and events[0].usage.total == 12


def test_tool_call_then_send_result_b2():
    """核心：Loop yield ToolCallProposed，驱动器 gen.send(ToolResult) 回注，续跑到 RunCompleted。"""
    loop = DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                           arguments={"query": "x"})]),
        ModelTurn(text="基于检索的答案"),
    ]))
    gen = loop.run(_ctx(), [{"role": "user", "content": "q"}], [])
    ev = next(gen)
    assert isinstance(ev, ToolCallProposed) and ev.tool_name == "knowledge_search"
    ev = gen.send(ToolResult.text_ok("检索结果"))       # ← B2 回注
    assert isinstance(ev, RunCompleted) and ev.final_text == "基于检索的答案"


def test_max_turns_yields_failed():
    def always_tool(msgs, tools):
        return ModelTurn(tool_calls=[ProposedCall(call_id="c", tool_name="t")])

    loop = DefaultAgentLoop(always_tool)
    gen = loop.run(_ctx(max_turns=2), [{"role": "user", "content": "q"}], [])
    seen = []
    try:
        ev = next(gen)
        while True:
            if isinstance(ev, ToolCallProposed):
                ev = gen.send(ToolResult.text_ok("r"))
            else:
                seen.append(ev)
                ev = next(gen)
    except StopIteration:
        pass
    assert any(isinstance(e, RunFailed) and "max_turns" in e.error for e in seen)


def test_checkpoint_codec_round_trip():
    msgs = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}]
    blob, digest = encode_checkpoint(msgs)
    assert decode_checkpoint(blob) == msgs and len(digest) == 64


def test_resume_terminate_fails():
    loop = DefaultAgentLoop(_scripted([]))       # model 不会被调
    events = list(loop.resume(_ctx(), None, RejectedTerminate()))
    assert len(events) == 1 and isinstance(events[0], RunFailed)


def test_resume_approved_continues():
    blob, _ = encode_checkpoint([{"role": "user", "content": "q"}])
    cp = SimpleNamespace(state_blob=blob)
    loop = DefaultAgentLoop(_scripted([ModelTurn(text="续跑答案")]))
    events = list(loop.resume(_ctx(), cp, Approved()))
    assert isinstance(events[-1], RunCompleted) and events[-1].final_text == "续跑答案"


def test_resume_feedback_injects_reason():
    blob, _ = encode_checkpoint([{"role": "user", "content": "q"}])
    cp = SimpleNamespace(state_blob=blob)
    captured = {}

    def _model(msgs, tools):
        captured["msgs"] = msgs
        return ModelTurn(text="换方案答案")

    events = list(DefaultAgentLoop(_model).resume(_ctx(), cp, RejectedFeedback(reason="口径不对")))
    assert isinstance(events[-1], RunCompleted)
    assert any("口径不对" in str(m.get("content", "")) for m in captured["msgs"])


# ── 真流式：model_fn 生成器形态（yield 增量 → ModelDelta，return ModelTurn）────
def _delta(text="", reasoning=""):
    return SimpleNamespace(text=text, reasoning=reasoning)


def _stream_fn(deltas, final):
    def _fn(msgs, tools):
        def g():
            for d in deltas:
                yield d
            return final
        return g()
    return _fn


def test_streaming_model_fn_yields_deltas_then_completed_streamed():
    from opensearch_pipeline.agent_runtime.events import ModelDelta
    loop = DefaultAgentLoop(_stream_fn(
        [_delta("你"), _delta("好"), _delta(reasoning="思考中")],
        ModelTurn(text="你好", usage=Usage(tokens_prompt=3, tokens_completion=2))))
    events = list(loop.run(_ctx(), [{"role": "user", "content": "q"}], []))
    deltas = [e for e in events if isinstance(e, ModelDelta)]
    assert [d.text for d in deltas] == ["你", "好", ""]
    assert deltas[2].reasoning == "思考中"
    done = events[-1]
    assert isinstance(done, RunCompleted)
    assert done.final_text == "你好" and done.usage.total == 5
    assert done.streamed is True, "已增量下发 → SSE 不重发全文的判据"


def test_streaming_zero_text_deltas_falls_back_to_full_text():
    """零文本增量的流（provider 退化同步）→ streamed=False，RunCompleted 全文照发。"""
    loop = DefaultAgentLoop(_stream_fn([], ModelTurn(text="整段答案")))
    events = list(loop.run(_ctx(), [{"role": "user", "content": "q"}], []))
    assert len(events) == 1
    assert events[0].final_text == "整段答案" and events[0].streamed is False


def test_streaming_deltas_precede_tool_call_and_b2_still_works():
    """模型先吐字后提工具：ModelDelta 在 ToolCallProposed 之前；.send 回注不受流式影响。"""
    from opensearch_pipeline.agent_runtime.events import ModelDelta
    turns = [
        ([_delta("让我查一下")],
         ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                            arguments={"query": "x"})])),
        ([_delta("根据"), _delta("检索")], ModelTurn(text="根据检索")),
    ]
    box = {"i": 0}

    def _fn(msgs, tools):
        ds, final = turns[box["i"]]
        box["i"] += 1
        def g():
            for d in ds:
                yield d
            return final
        return g()

    gen = DefaultAgentLoop(_fn).run(_ctx(), [{"role": "user", "content": "q"}], [])
    ev = next(gen)
    assert isinstance(ev, ModelDelta) and ev.text == "让我查一下"
    ev = next(gen)
    assert isinstance(ev, ToolCallProposed)
    ev = gen.send(ToolResult.text_ok("检索结果"))
    assert isinstance(ev, ModelDelta) and ev.text == "根据"
    ev = next(gen)
    assert isinstance(ev, ModelDelta) and ev.text == "检索"
    ev = next(gen)
    assert isinstance(ev, RunCompleted) and ev.streamed is True and ev.final_text == "根据检索"


def test_streaming_generator_without_return_value_fails_loud():
    def _fn(msgs, tools):
        def g():
            yield _delta("x")
            # 忘了 return ModelTurn
        return g()
    gen = DefaultAgentLoop(_fn).run(_ctx(), [{"role": "user", "content": "q"}], [])
    next(gen)                                    # 消费 delta
    try:
        while True:
            next(gen)
    except ValueError as e:
        assert "未返回 ModelTurn" in str(e)
    else:
        raise AssertionError("应当 fail loud 而非静默空答案")
