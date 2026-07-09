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
