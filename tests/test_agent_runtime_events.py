# -*- coding: utf-8 -*-
"""
test_agent_runtime_events.py — AgentEvent 判别联合（Task 7 / 报告 §3）

覆盖：5 个事件构造 + type 判别键 · isinstance(AgentEvent) · 判别序列化往返
（dump→parse 还原正确子类）· frozen 不可变 · Usage.total · 未知 type 拒绝。
"""
import pytest
from pydantic import ValidationError

from opensearch_pipeline.agent_runtime.events import (
    AgentEvent,
    ModelDelta,
    RunCompleted,
    RunFailed,
    RunSuspended,
    ToolCallProposed,
    Usage,
    dump_event,
    parse_event,
)


def _all_events():
    return [
        ModelDelta(text="hi", reasoning="想一下"),
        ToolCallProposed(call_id="c1", tool_name="knowledge_search", arguments={"query": "x"}),
        RunSuspended(approval_request_id="ar1", checkpoint_id="cp1"),
        RunCompleted(final_text="答案", usage=Usage(tokens_prompt=10, tokens_completion=20)),
        RunFailed(error="boom", retryable=True),
    ]


def test_events_carry_type_discriminator():
    types = {type(e).__name__: e.type for e in _all_events()}
    assert types == {
        "ModelDelta": "model_delta",
        "ToolCallProposed": "tool_call_proposed",
        "RunSuspended": "run_suspended",
        "RunCompleted": "run_completed",
        "RunFailed": "run_failed",
    }


def test_all_are_agent_events():
    for e in _all_events():
        assert isinstance(e, AgentEvent)


def test_discriminated_round_trip():
    for e in _all_events():
        restored = parse_event(dump_event(e))
        assert type(restored) is type(e)      # 判别键还原到正确子类
        assert restored == e                    # 字段全等


def test_dump_includes_type():
    d = dump_event(ToolCallProposed(call_id="c", tool_name="t", arguments={"a": 1}))
    assert d["type"] == "tool_call_proposed" and d["arguments"] == {"a": 1}


def test_parse_unknown_type_rejected():
    with pytest.raises(ValidationError):
        parse_event({"type": "nonsense", "text": "x"})


def test_events_are_frozen():
    e = ModelDelta(text="a")
    with pytest.raises(ValidationError):
        e.text = "b"                            # frozen：已发生的事实不可改


def test_defaults():
    assert RunFailed(error="e").retryable is False
    assert RunCompleted(final_text="x").usage.total == 0
    assert ToolCallProposed(call_id="c", tool_name="t").arguments == {}
    assert ModelDelta(text="a").reasoning is None


def test_usage_total():
    assert Usage(tokens_prompt=3, tokens_completion=4).total == 7


def test_toolcall_proposed_is_proposal_only():
    # 语义确认：事件只带提案参数，不带执行结果（结果经 B2 回注通道，不在事件流）
    ev = ToolCallProposed(call_id="c1", tool_name="u8_writeback", arguments={"qty": 5})
    d = dump_event(ev)
    # turn_index/usage 是提案元数据（第几个模型轮 + 该轮模型用量），非执行结果——仍无结果字段
    assert set(d.keys()) == {"type", "call_id", "tool_name", "arguments", "turn_index", "usage"}
