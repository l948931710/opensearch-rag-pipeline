# -*- coding: utf-8 -*-
"""
test_dingtalk_streaming.py — 钉钉流式 AI 卡片（打字机效果）测试

验证 dingtalk_bot._stream_answer_to_card：
  - 投放流式卡片 → 流式更新 → 定稿（is_finalize）
  - 流式变量默认 = "content"（钉钉 AI 流式卡片约定变量；推流 key 须 == 模板流式组件绑定变量）
  - 定稿后【不】调 update_card_data（会覆盖流式写入的 content → 卡片空白；完成态按钮门控已硬化）
  - B2 版式：定稿帧把 参考来源 + "模型 ｜ 耗时" 折进 content 末尾（答案→来源→耗时，耗时落最底下），
    不走 sources/meta 页脚，避免写页脚触发重渲染闪烁；create 时两页脚置空避免重复
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


@patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
@patch("opensearch_pipeline.dingtalk_bot.append_to_history")
@patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_fake_stream)
@patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg())
def test_streaming_card_happy_path(
    mock_cfg, mock_stream, mock_create, mock_update, mock_append, mock_log
):
    """正常流式：投放成功 → 更新 → 定稿，纯文本清理 + 落库/写历史一致，返回 True。"""
    handled = _call_stream()
    assert handled is True

    # 1. 投放流式卡片一次；流式变量默认 = "content"
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["message_id"] == "msg-123"
    assert mock_create.call_args.kwargs["stream_key"] == "content"

    # 2. 至少一次更新，最后一次为定稿帧（is_finalize=True, is_error 缺省 False），key=content
    assert mock_update.call_count >= 1
    final_call = mock_update.call_args
    assert final_call.kwargs["is_finalize"] is True
    assert final_call.kwargs.get("is_error", False) is False
    assert final_call.kwargs["key"] == "content"
    final_content = final_call.args[1]
    # 3. 纯文本：<<IMG:1>> 标记已清理
    assert "<<IMG" not in final_content and "IMG:1" not in final_content
    assert "住宿申请" in final_content
    # 4. B2 版式：定稿帧 content 末尾依次含 参考来源 + "模型 ｜ 耗时"（折进正文，不调 update_card_data；
    #    顺序 答案→来源→耗时，耗时落最底下；避免写页脚触发闪烁/空白）
    assert "参考来源" in final_content and "员工手册" in final_content
    assert "耗时" in final_content and "模型" in final_content
    # 来源在前、耗时页脚在后
    assert final_content.index("参考来源") < final_content.index("耗时")

    # 5. 写历史 + 落库（与非流式一致）
    mock_append.assert_called_once()
    assert mock_append.call_args.args[0] == "conv1:staff1"
    mock_log.assert_called_once()
    log_kwargs = mock_log.call_args.kwargs
    assert log_kwargs["message_id"] == "msg-123"
    assert log_kwargs["answer_status"] == "SUCCESS"
    assert log_kwargs["user_dept"] == "行政"
    assert "<<IMG" not in (log_kwargs["answer_text"] or "")


@patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
@patch("opensearch_pipeline.dingtalk_bot.append_to_history")
@patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=False)
@patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_fake_stream)
@patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg())
def test_streaming_card_fallback_when_create_fails(
    mock_cfg, mock_stream, mock_create, mock_update, mock_append, mock_log
):
    """投放失败 → 返回 False（降级），不触发流式更新/落库/置位（避免重复落库）。"""
    handled = _call_stream()
    assert handled is False

    mock_create.assert_called_once()
    mock_update.assert_not_called()
    mock_stream.assert_not_called()
    mock_append.assert_not_called()
    mock_log.assert_not_called()


@patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
@patch("opensearch_pipeline.dingtalk_bot.append_to_history")
@patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg())
def test_streaming_card_llm_error_finalizes_with_error(
    mock_cfg, mock_create, mock_update, mock_append, mock_log
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
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["answer_status"] == "LLM_ERROR"
    assert mock_log.call_args.kwargs["error_message"]


@patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
@patch("opensearch_pipeline.dingtalk_bot.append_to_history")
@patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=True)
@patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_fake_stream)
@patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg())
def test_streaming_forces_pure_text(
    mock_cfg, mock_stream, mock_create, mock_update, mock_append, mock_log
):
    """钉钉流式强制纯文本：传给 generate_answer_stream 的 pure_text 必须为 True。"""
    _call_stream()
    mock_stream.assert_called_once()
    assert mock_stream.call_args.kwargs["pure_text"] is True


# ── 回调重建：流式卡片反馈后必须恢复 is_answer_done="true" ────────────────────────
# 回归测试：修复前 _rebuild_card_param_map 从不回写 is_answer_done，流式模板把
# sources/meta/反馈按钮均门控在 is_answer_done=="true"，导致用户点击反馈后这些区域折叠。

def test_rebuild_card_param_map_restores_is_answer_done_on_db_hit():
    """命中 qa_session_log 时，重建后的 cardParamMap 同时含 is_answer_done=true 与重建的答案。"""
    from opensearch_pipeline import dingtalk_bot

    conn = MagicMock()
    cursor = MagicMock()
    # (query_text, answer_text, cited_docs_json, model_name, latency_ms, content_blocks_json)
    cursor.fetchone.return_value = ("怎么申请住宿", "住宿申请见附件", None, "qwen3.6-plus", 1500, None)
    conn.cursor.return_value.__enter__.return_value = cursor

    card_param_map = {"feedback_status": "👍 已反馈：有帮助"}
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=conn):
        dingtalk_bot._rebuild_card_param_map(card_param_map, "msg-123")

    assert card_param_map["is_answer_done"] == "true"   # ← 修复点
    assert card_param_map["answer"] == "住宿申请见附件"
    assert card_param_map["feedback_status"] == "👍 已反馈：有帮助"  # 调用方设置的字段保留


def test_rebuild_card_param_map_sets_is_answer_done_even_when_db_fails():
    """即使 DB 不可用（重建答案失败），仍恒置 is_answer_done=true，避免流式卡片折叠。"""
    from opensearch_pipeline import dingtalk_bot

    card_param_map = {}
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", side_effect=RuntimeError("rds down")):
        dingtalk_bot._rebuild_card_param_map(card_param_map, "msg-x")  # 异常被吞，不抛出

    assert card_param_map["is_answer_done"] == "true"


def test_rebuild_card_param_map_streaming_folds_sources_and_latency_into_content(monkeypatch):
    """流式模式：重建后 content 折入 答案+📚来源+"模型 ｜ 耗时"，sources/meta 页脚置空（B2 版式）。

    回归点：反馈点击触发回调重建，若仍把来源/耗时回填到页脚，流式卡会在点击后版式跳变
    （来源从正文跳回页脚、耗时挪位）。流式分支须与 _stream_answer_to_card 定稿帧版式一致。
    """
    from opensearch_pipeline import dingtalk_bot

    monkeypatch.setenv("DINGTALK_STREAM_CARD_TEMPLATE_ID", "tpl-x")
    conn = MagicMock()
    cursor = MagicMock()
    # cited_docs 直接给 list（重建支持 str/list）；(query, answer, cited_docs, model, latency_ms, content_blocks)
    cursor.fetchone.return_value = (
        "怎么申请住宿", "住宿申请见附件",
        [{"title": "员工手册"}], "qwen3.6-plus", 1500, None,
    )
    conn.cursor.return_value.__enter__.return_value = cursor

    streaming_cfg = MagicMock()
    streaming_cfg.rag.dingtalk_streaming = True

    card_param_map = {}
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=conn), \
         patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=streaming_cfg):
        dingtalk_bot._rebuild_card_param_map(card_param_map, "msg-s")

    c = card_param_map["content"]
    # 顺序：答案 → 参考来源 → 耗时（耗时落最底下）
    assert "住宿申请见附件" in c and "参考来源" in c and "员工手册" in c
    assert "耗时" in c and c.index("参考来源") < c.index("耗时")
    # B2：来源/耗时已折进 content，页脚置空，避免与正文重复
    assert card_param_map["sources_text"] == "" and card_param_map["meta"] == ""
    assert card_param_map["is_answer_done"] == "true"


# ── 启动时自动注册互动卡片 HTTP 回调地址 ──────────────────────────────────────

def test_register_card_callback_skips_without_url(monkeypatch):
    """未配置 DINGTALK_CARD_CALLBACK_URL 时跳过：不取 token、不发请求、返回 False。"""
    from opensearch_pipeline import dingtalk_card

    monkeypatch.delenv("DINGTALK_CARD_CALLBACK_URL", raising=False)
    monkeypatch.setenv("DINGTALK_CARD_CALLBACK_ROUTE_KEY", "rag_feedback_callback")
    with patch("opensearch_pipeline.dingtalk_card._get_access_token") as mock_tok, \
         patch("opensearch_pipeline.dingtalk_card.requests.post") as mock_post:
        assert dingtalk_card.register_card_callback() is False
        mock_tok.assert_not_called()
        mock_post.assert_not_called()


def test_register_card_callback_posts_when_configured(monkeypatch):
    """配置齐全时 POST /card/callbacks/register，载荷含正确的 callbackUrl/routeKey，200→True。"""
    from opensearch_pipeline import dingtalk_card

    monkeypatch.setenv("DINGTALK_CARD_CALLBACK_URL", "https://sae.example.com/dingtalk/card/callback")
    monkeypatch.setenv("DINGTALK_CARD_CALLBACK_ROUTE_KEY", "rag_feedback_callback")
    monkeypatch.delenv("DINGTALK_CARD_CALLBACK_FORCE_UPDATE", raising=False)

    resp = MagicMock()
    resp.status_code = 200
    resp.text = '{"success":true}'
    with patch("opensearch_pipeline.dingtalk_card._get_access_token", return_value="tok-xyz"), \
         patch("opensearch_pipeline.dingtalk_card.requests.post", return_value=resp) as mock_post:
        assert dingtalk_card.register_card_callback() is True
        mock_post.assert_called_once()
        url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs.get("url")
        assert "card/callbacks/register" in url
        payload = mock_post.call_args.kwargs["json"]
        assert payload["callbackUrl"] == "https://sae.example.com/dingtalk/card/callback"
        assert payload["callbackRouteKey"] == "rag_feedback_callback"
        assert payload["forceUpdate"] is False
        # access-token 走 Header
        assert mock_post.call_args.kwargs["headers"]["x-acs-dingtalk-access-token"] == "tok-xyz"
