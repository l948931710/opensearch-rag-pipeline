# -*- coding: utf-8 -*-
"""
test_stream_feedback_parity.py — SSE 流式问答的反馈/落库一致性测试

验证 /api/ask/stream 现已与 /api/ask 对齐：
  - session 帧携带 message_id（反馈关联键）
  - 流式结束后补发 content_blocks 帧（图文模式、有被引用图片时）
  - 写历史（append_to_history）+ 落库（log_qa_session）在流式收尾时被调用
  - 空结果分支也发出 message_id 并以 NO_RESULT 落库

外部依赖（检索 / LLM / 落库 / 图片签名）全部 mock，无需真实服务。
"""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _parse_sse(body: str):
    """把 SSE 文本拆成 data 帧的 JSON 对象列表（忽略 [DONE]）。"""
    frames = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            frames.append({"type": "__DONE__"})
            continue
        try:
            frames.append(json.loads(payload))
        except json.JSONDecodeError:
            pass
    return frames


def _fake_stream(*args, **kwargs):
    """模拟 generate_answer_stream 的 SSE 输出（含 <<IMG:1>> 标记 + done 帧）。"""
    yield 'data: {"type": "sources", "sources": []}\n\n'
    yield 'data: {"type": "chunk", "content": "住宿申请"}\n\n'
    yield 'data: {"type": "chunk", "content": "见 <<IMG:1>> 图示"}\n\n'
    yield 'data: {"type": "done", "model": "qwen3.6-plus", "usage": {"total_tokens": 10}}\n\n'
    yield "data: [DONE]\n\n"


@pytest.fixture
def client():
    from opensearch_pipeline.api import app
    return TestClient(app)


@patch("opensearch_pipeline.api.content_blocks_to_json", return_value='[{"type":"image"}]')
@patch("opensearch_pipeline.api.build_content_blocks")
@patch("opensearch_pipeline.api.log_qa_session")
@patch("opensearch_pipeline.api._append_to_history")
@patch("opensearch_pipeline.api.generate_answer_stream", side_effect=_fake_stream)
@patch("opensearch_pipeline.api.retrieve_and_enrich")
def test_stream_emits_message_id_logs_and_content_blocks(
    mock_retrieve, mock_stream, mock_append, mock_log, mock_build, mock_cb_json, client
):
    """正常流式：session 帧带 message_id、补发 content_blocks 帧、落库+写历史均触发。"""
    mock_retrieve.return_value = [
        {"doc_id": "D1", "title": "员工手册", "chunk_text": "住宿规定...", "score": 9.0}
    ]
    mock_build.return_value = [
        {"type": "markdown", "content": "住宿申请见 图示"},
        {"type": "image", "url": "http://example.com/a.png", "title": "", "caption": "图"},
    ]

    resp = client.post("/api/ask/stream", json={"question": "怎么申请住宿"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(resp.text)
    types = [f.get("type") for f in frames]

    # 1. 第一帧是 session，且携带 message_id
    session_frame = next(f for f in frames if f.get("type") == "session")
    assert session_frame["session_id"]
    assert session_frame["message_id"]
    message_id = session_frame["message_id"]

    # 2. 补发了 content_blocks 帧，且在 done 之后、[DONE] 之前
    assert "content_blocks" in types
    cb_frame = next(f for f in frames if f.get("type") == "content_blocks")
    assert any(b.get("type") == "image" for b in cb_frame["content_blocks"])
    assert types.index("content_blocks") > types.index("done")
    assert types.index("content_blocks") < types.index("__DONE__")

    # 3. 写历史：完整回答（含原始 token，<<IMG:1>> 由下游清理）
    mock_append.assert_called_once()
    _, appended_q, appended_a = mock_append.call_args.args
    assert appended_q == "怎么申请住宿"
    assert "住宿申请" in appended_a

    # 4. 落库：SUCCESS + 同一个 message_id（即反馈关联键）
    mock_log.assert_called_once()
    log_kwargs = mock_log.call_args.kwargs
    assert log_kwargs["message_id"] == message_id
    assert log_kwargs["answer_status"] == "SUCCESS"
    assert log_kwargs["model_name"] == "qwen3.6-plus"
    assert log_kwargs["opensearch_hit_count"] == 1


@patch("opensearch_pipeline.api.build_content_blocks")
@patch("opensearch_pipeline.api.log_qa_session")
@patch("opensearch_pipeline.api._append_to_history")
@patch("opensearch_pipeline.api.generate_answer_stream", side_effect=_fake_stream)
@patch("opensearch_pipeline.api.retrieve_and_enrich")
def test_stream_pure_text_skips_content_blocks(
    mock_retrieve, mock_stream, mock_append, mock_log, mock_build, client
):
    """纯文本模式：不构建也不补发 content_blocks 帧。"""
    mock_retrieve.return_value = [
        {"doc_id": "D1", "title": "员工手册", "chunk_text": "住宿规定...", "score": 9.0}
    ]

    resp = client.post("/api/ask/stream", json={"question": "怎么申请住宿", "pure_text": True})
    assert resp.status_code == 200

    types = [f.get("type") for f in _parse_sse(resp.text)]
    assert "content_blocks" not in types
    mock_build.assert_not_called()
    # 仍然落库（反馈一致性与图文无关）
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["answer_status"] == "SUCCESS"


@patch("opensearch_pipeline.api.log_qa_session")
@patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=[])
def test_stream_no_result_emits_message_id_and_logs_no_result(mock_retrieve, mock_log, client):
    """空结果分支：发出 message_id 并以 NO_RESULT 落库，与 /api/ask 一致。"""
    resp = client.post("/api/ask/stream", json={"question": "不存在的问题"})
    assert resp.status_code == 200

    frames = _parse_sse(resp.text)
    session_frame = next(f for f in frames if f.get("type") == "session")
    assert session_frame["message_id"]
    assert any("未找到" in f.get("content", "") for f in frames if f.get("type") == "chunk")

    mock_log.assert_called_once()
    log_kwargs = mock_log.call_args.kwargs
    assert log_kwargs["answer_status"] == "NO_RESULT"
    assert log_kwargs["message_id"] == session_frame["message_id"]
    assert log_kwargs["opensearch_hit_count"] == 0


@patch("opensearch_pipeline.api.log_qa_session")
@patch("opensearch_pipeline.api._append_to_history")
@patch("opensearch_pipeline.api.retrieve_and_enrich")
def test_stream_llm_error_logs_llm_error(mock_retrieve, mock_append, mock_log, client):
    """生成阶段抛错：发出 error 帧并以 LLM_ERROR 落库（finally 保证落库）。"""
    mock_retrieve.return_value = [
        {"doc_id": "D1", "title": "手册", "chunk_text": "x", "score": 9.0}
    ]

    def _boom(*args, **kwargs):
        yield 'data: {"type": "chunk", "content": "部分"}\n\n'
        raise RuntimeError("upstream down")

    with patch("opensearch_pipeline.api.generate_answer_stream", side_effect=_boom):
        resp = client.post("/api/ask/stream", json={"question": "测试"})
    assert resp.status_code == 200

    types = [f.get("type") for f in _parse_sse(resp.text)]
    assert "error" in types

    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["answer_status"] == "LLM_ERROR"
    assert mock_log.call_args.kwargs["error_message"]
