# -*- coding: utf-8 -*-
"""
test_dingtalk_streaming.py — 钉钉流式 AI 卡片（打字机效果）测试

验证 dingtalk_bot._stream_answer_to_card：
  - 投放流式卡片 → 流式更新 → 定稿（is_finalize）
  - 流式变量默认 = "answer"（本项目 AI 卡片把可见答案组件绑定到 answer 变量）
  - 完成后置位 is_answer_done="true"（触发反馈按钮显示）
  - 钉钉端纯文本：清理 <<IMG:N>> 标记
  - 落库 + 写历史与非流式路径一致
  - 投放失败时返回 False（降级），且不重复落库
  - 生成阶段异常时以 is_error 定稿并落 LLM_ERROR（不置 is_answer_done）

外部依赖（钉钉卡片 API / LLM）全部 mock，无需真实服务。
"""

from unittest.mock import patch, MagicMock


CHUNKS = [
    {"doc_id": "D1", "title": "员工手册", "chunk_text": "住宿规定...", "score": 9.0},
]


def _fake_stream(*args, **kwargs):
    yield 'data: {"type": "sources", "sources": []}\n\n'
    yield 'data: {"type": "chunk", "content": "住宿申请"}\n\n'
    yield 'data: {"type": "chunk", "content": "见 <<IMG:1>> 图示"}\n\n'
    yield 'data: {"type": "done", "model": "qwen3.6-plus", "usage": {}}\n\n'
    yield "data: [DONE]\n\n"


def _fake_cfg(interval_ms=0, model="qwen3.6-plus"):
    cfg = MagicMock()
    cfg.rag.dingtalk_stream_interval_ms = interval_ms
    cfg.llm.model = model
    return cfg


def _call_stream(**overrides):
    from opensearch_pipeline import dingtalk_bot

    kwargs = dict(
        question="怎么申请住宿",
        chunks=CHUNKS,
        history=[],
        session_key="conv1:staff1",
        message_id="msg-123",
        conversation_id="conv1",
        conversation_type="1",
        sender_staff_id="staff1",
        sender_nick="张三",
        user_dept="行政",
        t0=0.0,
        t_retrieval=0.0,
        retrieval_latency_ms=12,
    )
    kwargs.update(overrides)
    return dingtalk_bot._stream_answer_to_card(**kwargs)


@patch("opensearch_pipeline.dingtalk_bot.update_card_data")
@patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
@patch("opensearch_pipeline.dingtalk_bot.append_to_history")
@patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_fake_stream)
@patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg())
def test_streaming_card_happy_path(
    mock_cfg, mock_stream, mock_create, mock_update, mock_append, mock_log, mock_card_data
):
    """正常流式：投放成功 → 更新 → 定稿，纯文本清理 + 落库/写历史一致，返回 True。"""
    handled = _call_stream()
    assert handled is True

    # 1. 投放流式卡片一次；流式变量默认 = "answer"
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["message_id"] == "msg-123"
    assert mock_create.call_args.kwargs["stream_key"] == "answer"

    # 2. 至少一次更新，最后一次为定稿帧（is_finalize=True, is_error 缺省 False），key=answer
    assert mock_update.call_count >= 1
    final_call = mock_update.call_args
    assert final_call.kwargs["is_finalize"] is True
    assert final_call.kwargs.get("is_error", False) is False
    assert final_call.kwargs["key"] == "answer"
    final_content = final_call.args[1]
    # 3. 纯文本：<<IMG:1>> 标记已清理
    assert "<<IMG" not in final_content and "IMG:1" not in final_content
    assert "住宿申请" in final_content

    # 4. 完成后置位 is_answer_done=true（触发反馈按钮显示）
    mock_card_data.assert_called_once_with("msg-123", {"is_answer_done": "true"})

    # 5. 写历史 + 落库（与非流式一致）
    mock_append.assert_called_once()
    assert mock_append.call_args.args[0] == "conv1:staff1"
    mock_log.assert_called_once()
    log_kwargs = mock_log.call_args.kwargs
    assert log_kwargs["message_id"] == "msg-123"
    assert log_kwargs["answer_status"] == "SUCCESS"
    assert log_kwargs["user_dept"] == "行政"
    assert "<<IMG" not in (log_kwargs["answer_text"] or "")


@patch("opensearch_pipeline.dingtalk_bot.update_card_data")
@patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
@patch("opensearch_pipeline.dingtalk_bot.append_to_history")
@patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=False)
@patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_fake_stream)
@patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg())
def test_streaming_card_fallback_when_create_fails(
    mock_cfg, mock_stream, mock_create, mock_update, mock_append, mock_log, mock_card_data
):
    """投放失败 → 返回 False（降级），不触发流式更新/落库/置位（避免重复落库）。"""
    handled = _call_stream()
    assert handled is False

    mock_create.assert_called_once()
    mock_update.assert_not_called()
    mock_stream.assert_not_called()
    mock_append.assert_not_called()
    mock_log.assert_not_called()
    mock_card_data.assert_not_called()


@patch("opensearch_pipeline.dingtalk_bot.update_card_data")
@patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
@patch("opensearch_pipeline.dingtalk_bot.append_to_history")
@patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg())
def test_streaming_card_llm_error_finalizes_with_error(
    mock_cfg, mock_create, mock_update, mock_append, mock_log, mock_card_data
):
    """生成阶段抛错：以 is_error 定稿并落 LLM_ERROR，不写历史，不置 is_answer_done，仍返回 True。"""
    def _boom(*args, **kwargs):
        yield 'data: {"type": "chunk", "content": "部分"}\n\n'
        raise RuntimeError("upstream down")

    with patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_boom):
        handled = _call_stream()
    assert handled is True

    # 最后一次更新带 is_error=True 且 is_finalize=True
    final_call = mock_update.call_args
    assert final_call.kwargs["is_finalize"] is True
    assert final_call.kwargs["is_error"] is True

    mock_append.assert_not_called()      # 失败不写历史
    mock_card_data.assert_not_called()   # 失败不展示反馈按钮（不置 is_answer_done）
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["answer_status"] == "LLM_ERROR"
    assert mock_log.call_args.kwargs["error_message"]


@patch("opensearch_pipeline.dingtalk_bot.update_card_data")
@patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
@patch("opensearch_pipeline.dingtalk_bot.append_to_history")
@patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_fake_stream)
@patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg())
def test_streaming_forces_pure_text(
    mock_cfg, mock_stream, mock_create, mock_update, mock_append, mock_log, mock_card_data
):
    """钉钉流式强制纯文本：传给 generate_answer_stream 的 pure_text 必须为 True。"""
    _call_stream()
    mock_stream.assert_called_once()
    assert mock_stream.call_args.kwargs["pure_text"] is True
