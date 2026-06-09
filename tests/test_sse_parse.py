# -*- coding: utf-8 -*-
"""
test_sse_parse.py — parse_sse_data_frame（SSE 帧解析，api/bot/producer 共用）
"""

from opensearch_pipeline.llm_generator import parse_sse_data_frame


def test_parses_chunk_and_done_frames():
    assert parse_sse_data_frame('data: {"type": "chunk", "content": "你好"}\n\n') == {
        "type": "chunk", "content": "你好"}
    assert parse_sse_data_frame('data: {"type": "done", "model": "qwen"}\n\n')["model"] == "qwen"


def test_done_and_non_data_and_garbage_return_none():
    assert parse_sse_data_frame("data: [DONE]\n\n") is None
    assert parse_sse_data_frame(": comment line") is None
    assert parse_sse_data_frame("") is None
    assert parse_sse_data_frame("data: not-json") is None
    assert parse_sse_data_frame("data: ") is None
    # JSON 但不是对象（list/标量）→ None
    assert parse_sse_data_frame('data: [1, 2]') is None


def test_not_fooled_by_literal_in_content():
    # 答案正文里出现 '"type": "chunk"' 字面量不应误导：真正的 type 是 done
    f = parse_sse_data_frame('data: {"type": "done", "model": "m", "note": "see \\"type\\": \\"chunk\\""}')
    assert f["type"] == "done"
