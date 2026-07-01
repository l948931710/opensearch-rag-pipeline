# -*- coding: utf-8 -*-
"""
test_dingtalk_streaming.py — 钉钉流式 AI 卡片（打字机效果）测试

验证 dingtalk_bot._stream_answer_to_card：
  - 投放流式卡片 → 流式更新 → 定稿（is_finalize）
  - 流式变量默认 = "content"（钉钉 AI 流式卡片约定变量；推流 key 须 == 模板流式组件绑定变量）
  - 定稿后【不】调 update_card_data（会覆盖流式写入的 content → 卡片空白；完成态按钮门控已硬化）
  - B2 版式：定稿帧把 参考来源 + "模型 ｜ 检索·生成"页脚 折进 content 末尾（答案→来源→页脚，落最底下），
    不走 sources/meta 页脚，避免写页脚触发重渲染闪烁；create 时两页脚置空避免重复
  - 页脚显示「检索/生成」分段耗时（生成=模型输出延迟）而非总耗时
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
    # 4. B2 版式：定稿帧 content 末尾依次含 参考来源 + "模型 ｜ 检索·生成"页脚（折进正文，不调 update_card_data；
    #    顺序 答案→来源→页脚，落最底下；避免写页脚触发闪烁/空白）。显示生成(模型输出)延迟而非总耗时。
    assert "参考来源" in final_content and "员工手册" in final_content
    assert "生成" in final_content and "模型" in final_content
    # 来源在前、耗时页脚在后
    assert final_content.index("参考来源") < final_content.index("生成")

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


def test_streaming_nonblocking_pusher_finalize_is_last():
    """非阻塞后台推流：生成期间后台线程会推送多帧，但 finalize 必为【最后一次】调用。

    回归点：推流改后台线程后，若不在 finalize 前 stop+join，推流帧可能覆盖定稿帧 → 卡片空白/掉页脚。
    用带延时的流让后台线程真正触发多次推送，验证 (a) 后台确有推流，(b) 仅一次定稿且在最后，其后无推送。
    """
    import time as _t

    calls = []

    def _rec(message_id, content, *, key="content", is_full=True, is_finalize=False, is_error=False):
        calls.append({"is_finalize": is_finalize, "is_error": is_error})
        return True

    def _slow_stream(*args, **kwargs):
        yield 'data: {"type": "sources", "sources": []}\n\n'
        for w in ["甲", "乙", "丙", "丁", "戊"]:
            yield f'data: {{"type": "chunk", "content": "{w}"}}\n\n'
            _t.sleep(0.25)  # 5×0.25=1.25s > 推流间隔 0.3s → 后台线程必触发
        yield 'data: {"type": "done", "model": "qwen3.6-plus", "usage": {}}\n\n'
        yield "data: [DONE]\n\n"

    with patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_cfg()), \
         patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=True), \
         patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", side_effect=_rec), \
         patch("opensearch_pipeline.dingtalk_bot.append_to_history"), \
         patch("opensearch_pipeline.dingtalk_bot.log_qa_session"), \
         patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_slow_stream):
        handled = _call_stream()

    assert handled is True
    assert len(calls) >= 2, "后台推流应至少触发一次 + 定稿"
    assert sum(c["is_finalize"] for c in calls) == 1, "有且仅有一次定稿"
    assert calls[-1]["is_finalize"] is True, "定稿必为最后一次调用（其后无推流帧覆盖）"


# ── 回调重建：流式卡片反馈后必须恢复 is_answer_done="true" ────────────────────────
# 回归测试：修复前 _rebuild_card_param_map 从不回写 is_answer_done，流式模板把
# sources/meta/反馈按钮均门控在 is_answer_done=="true"，导致用户点击反馈后这些区域折叠。

def test_rebuild_card_param_map_restores_is_answer_done_on_db_hit():
    """命中 qa_session_log 时，重建后的 cardParamMap 同时含 is_answer_done=true 与重建的答案。"""
    from opensearch_pipeline import dingtalk_bot

    conn = MagicMock()
    cursor = MagicMock()
    # (query, answer, cited_docs, model, latency_ms, retrieval_latency_ms, llm_latency_ms, content_blocks)
    cursor.fetchone.return_value = ("怎么申请住宿", "住宿申请见附件", None, "qwen3.6-plus", 1500, 800, 1200, None)
    conn.cursor.return_value.__enter__.return_value = cursor

    card_param_map = {"feedback_status": "👍 已反馈：有帮助"}
    with patch("opensearch_pipeline.db._get_db_conn", return_value=conn):
        dingtalk_bot._rebuild_card_param_map(card_param_map, "msg-123")

    assert card_param_map["is_answer_done"] == "true"   # ← 修复点
    assert card_param_map["answer"] == "住宿申请见附件"
    assert card_param_map["feedback_status"] == "👍 已反馈：有帮助"  # 调用方设置的字段保留


def test_rebuild_card_param_map_sets_is_answer_done_even_when_db_fails():
    """即使 DB 不可用（重建答案失败），仍恒置 is_answer_done=true，避免流式卡片折叠。"""
    from opensearch_pipeline import dingtalk_bot

    card_param_map = {}
    with patch("opensearch_pipeline.db._get_db_conn", side_effect=RuntimeError("rds down")):
        dingtalk_bot._rebuild_card_param_map(card_param_map, "msg-x")  # 异常被吞，不抛出

    assert card_param_map["is_answer_done"] == "true"
    # DB 异常 → 兜底占位，避免回调覆盖整卡→白屏
    assert card_param_map.get("content")


def test_rebuild_card_param_map_no_row_sets_placeholder_not_blank():
    """qa_session_log 查无此 message_id（演示卡未落库 / message_id 不匹配）→ 兜底写非空占位 content，
    避免钉钉回调覆盖整卡→白屏（即"点其他原因后白屏"的根因：未落库的卡重建不出正文）。"""
    from opensearch_pipeline import dingtalk_bot

    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None  # 查无此行
    conn.cursor.return_value.__enter__.return_value = cursor

    # 模拟"其他原因"回调已设的字段（content/answer 未设 → 不兜底就白屏）
    card_param_map = {"feedback_status": "", "show_other_feedback_form": "true", "form_status": "normal"}
    with patch("opensearch_pipeline.db._get_db_conn", return_value=conn):
        dingtalk_bot._rebuild_card_param_map(card_param_map, "msg-unlogged")

    assert card_param_map["is_answer_done"] == "true"
    assert card_param_map.get("content")   # 非空占位 → 不白屏
    assert card_param_map.get("answer")
    # 调用方设置的表单字段保留（"其他原因"表单仍可用）
    assert card_param_map["show_other_feedback_form"] == "true"
    assert card_param_map["form_status"] == "normal"


def test_rebuild_card_param_map_streaming_folds_sources_and_latency_into_content(monkeypatch):
    """流式模式：重建后 content 折入 答案+📚来源+"模型 ｜ 耗时"，sources/meta 页脚置空（B2 版式）。

    回归点：反馈点击触发回调重建，若仍把来源/耗时回填到页脚，流式卡会在点击后版式跳变
    （来源从正文跳回页脚、耗时挪位）。流式分支须与 _stream_answer_to_card 定稿帧版式一致。
    """
    from opensearch_pipeline import dingtalk_bot

    monkeypatch.setenv("DINGTALK_STREAM_CARD_TEMPLATE_ID", "tpl-x")
    conn = MagicMock()
    cursor = MagicMock()
    # cited_docs 直接给 list（重建支持 str/list）；
    # (query, answer, cited_docs, model, latency_ms, retrieval_latency_ms, llm_latency_ms, content_blocks)
    cursor.fetchone.return_value = (
        "怎么申请住宿", "住宿申请见附件",
        [{"title": "员工手册"}], "qwen3.6-plus", 1500, 800, 1200, None,
    )
    conn.cursor.return_value.__enter__.return_value = cursor

    streaming_cfg = MagicMock()
    streaming_cfg.rag.dingtalk_streaming = True

    card_param_map = {}
    with patch("opensearch_pipeline.db._get_db_conn", return_value=conn), \
         patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=streaming_cfg):
        dingtalk_bot._rebuild_card_param_map(card_param_map, "msg-s")

    c = card_param_map["content"]
    # 顺序：答案 → 参考来源 → 检索·生成页脚（落最底下）
    assert "住宿申请见附件" in c and "参考来源" in c and "员工手册" in c
    assert "生成" in c and c.index("参考来源") < c.index("生成")
    # B2：来源/耗时已折进 content，页脚置空，避免与正文重复
    assert card_param_map["sources_text"] == "" and card_param_map["meta"] == ""
    assert card_param_map["is_answer_done"] == "true"


# ── ACK-only 回调（流式卡反馈不白屏）+ 「补充原因」自由文本回收 ────────────────────

def test_card_callback_acks_without_carddata():
    """关键回归：回调一律 ACK-only —— 响应【不含 cardData】→ 钉钉不重渲染卡片 → 不冲掉流式正文（白屏）。
    覆盖 转人工/赞/踩/补充原因/未知原生动作。"""
    import asyncio
    import json as _json
    from opensearch_pipeline import dingtalk_bot

    def _req(action, mid="m1"):
        body = {
            "outTrackId": mid, "userId": "u1",
            "content": _json.dumps({"cardPrivateData": {"params": {"action": action, "message_id": mid}}}),
        }

        class _R:
            async def json(self):
                return body

        return _R()

    with patch("opensearch_pipeline.dingtalk_bot.handle_feedback", return_value=True) as mock_hf, \
         patch("opensearch_pipeline.dingtalk_bot.mark_awaiting_comment", return_value=True) as mock_mark, \
         patch("opensearch_pipeline.dingtalk_bot._card_callback_authorized", return_value=True), \
         patch("opensearch_pipeline.dingtalk_bot.send_text_to_user", return_value=True):
        for action in ("handoff", "upvote", "downvote", "add_reason", "some_native_like"):
            resp = asyncio.run(dingtalk_bot.card_callback(_req(action)))
            assert "cardData" not in resp, f"action={action} 不应返回 cardData（会白屏）"

    assert mock_hf.called          # 转人工 + 赞踩 落库
    assert mock_mark.called        # 补充原因 标记待补充


def test_card_callback_official_feedback_template():
    """钉钉【官方赞踩模版】回调：用 feedback=good/bad（不是 action）、不传 message_id（用 outTrackId 兜底），
    踩+提交带 comment。验证：good→upvote；bad+comment→downvote/reason=other/comment 落库；均 ACK-only。"""
    import asyncio
    import json as _json
    from opensearch_pipeline import dingtalk_bot

    def _req(params, mid="m-official"):
        body = {"outTrackId": mid, "userId": "u9",
                "content": _json.dumps({"cardPrivateData": {"params": params}})}

        class _R:
            async def json(self):
                return body

        return _R()

    with patch("opensearch_pipeline.dingtalk_bot.handle_feedback", return_value=True) as mock_hf, \
         patch("opensearch_pipeline.dingtalk_bot._card_callback_authorized", return_value=True):
        # 👍：feedback=good（无 action / 无 message_id）→ upvote，message_id 用 outTrackId
        resp = asyncio.run(dingtalk_bot.card_callback(
            _req({"feedback": "good", "content": "答案…", "query": "怎么报销"})))
        assert "cardData" not in resp
        _, kw = mock_hf.call_args
        assert kw["action"] == "upvote" and kw["message_id"] == "m-official"

        # 👎+提交：feedback=bad + comment → downvote / reason=other / comment 落库
        mock_hf.reset_mock()
        resp = asyncio.run(dingtalk_bot.card_callback(
            _req({"feedback": "bad", "comment": "答非所问", "content": "答案…", "query": "怎么报销"})))
        assert "cardData" not in resp
        _, kw = mock_hf.call_args
        assert kw["action"] == "downvote"
        assert kw["reason"] == "other"
        assert kw["comment"] == "答非所问"


class _GateCur:
    def __init__(self, row, boom):
        self._row, self._boom = row, boom

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self._boom:
            raise RuntimeError("db down")

    def fetchone(self):
        return self._row


class _GateConn:
    def __init__(self, row, boom):
        self._row, self._boom = row, boom

    def cursor(self):
        return _GateCur(self._row, self._boom)

    def close(self):
        pass


def test_card_callback_authorized_predicate(monkeypatch):
    """Track-2 归属校验：不存在的 message_id 拒；跨用户拒；归属一致放行；群聊(无归属)仅存在性放行；
    空 id 拒；查库异常 fail-open 放行。"""
    from opensearch_pipeline.dingtalk_bot import _card_callback_authorized

    def _wire(row, boom=False):
        monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _GateConn(row, boom))

    _wire(None);            assert _card_callback_authorized("m1", "u1") is False   # 不存在 → 伪造
    _wire(("u1",));         assert _card_callback_authorized("m1", "u1") is True    # 归属一致
    _wire(("u1",));         assert _card_callback_authorized("m1", "u2") is False   # 跨用户伪造
    _wire((None,));         assert _card_callback_authorized("m1", "u9") is True    # 群聊/无归属 → 存在性门控
    _wire(("",));           assert _card_callback_authorized("m1", "u9") is True    # 同上（空归属）
    _wire(("u1",));         assert _card_callback_authorized("", "u1") is False     # 空 message_id
    _wire(None, boom=True); assert _card_callback_authorized("m1", "u1") is True    # 查库异常 → fail-open


def test_card_callback_forged_message_id_no_write(monkeypatch):
    """端到端：伪造（不存在的 message_id）的 downvote 回调 → 归属校验拒 → handle_feedback 绝不被调用。"""
    import asyncio
    import json as _json
    from opensearch_pipeline import dingtalk_bot

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _GateConn(None, False))  # message_id 不存在
    body = {"outTrackId": "forged-x", "userId": "attacker",
            "content": _json.dumps({"cardPrivateData": {"params": {"action": "downvote", "message_id": "forged-x"}}})}

    class _R:
        async def json(self):
            return body

    with patch("opensearch_pipeline.dingtalk_bot.handle_feedback", return_value=True) as mock_hf, \
         patch("opensearch_pipeline.dingtalk_bot.mark_awaiting_comment", return_value=True) as mock_mark:
        resp = asyncio.run(dingtalk_bot.card_callback(_R()))
    assert "cardData" not in resp           # 仍 ACK-only
    assert not mock_hf.called               # 伪造 → 绝不落反馈
    assert not mock_mark.called


def test_take_awaiting_comment_hit_and_miss():
    """补充原因回收：命中 AWAITING_COMMENT 行 → 写 comment 返回 True；无命中/空输入 → False。"""
    from opensearch_pipeline import feedback_handler

    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (42,)  # 找到一条待补充
    conn.cursor.return_value.__enter__.return_value = cur
    with patch("opensearch_pipeline.db._get_db_conn", return_value=conn):
        assert feedback_handler.take_awaiting_comment(user_id="u1", comment="缺少XX制度") is True
    conn.commit.assert_called_once()

    conn2 = MagicMock()
    cur2 = MagicMock()
    cur2.fetchone.return_value = None  # 无待补充
    conn2.cursor.return_value.__enter__.return_value = cur2
    with patch("opensearch_pipeline.db._get_db_conn", return_value=conn2):
        assert feedback_handler.take_awaiting_comment(user_id="u1", comment="这其实是个新问题") is False

    assert feedback_handler.take_awaiting_comment(user_id="", comment="x") is False
    assert feedback_handler.take_awaiting_comment(user_id="u1", comment="  ") is False


def test_take_awaiting_comment_window_uses_updated_at_and_expires_stale():
    """窗口必须按 updated_at 计（mark_awaiting_comment 的 upsert 只刷新 updated_at，旧投票行
    created_at 可能远早于点击「补充原因」→ 按 created_at 会把迟到的补充静默丢弃）；
    且超窗滞留 AWAITING_COMMENT 的行要先归位 PENDING（否则永久卡住、多日后误吞私聊）。"""
    from opensearch_pipeline import feedback_handler

    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = None  # miss 路径
    conn.cursor.return_value.__enter__.return_value = cur
    with patch("opensearch_pipeline.db._get_db_conn", return_value=conn):
        assert feedback_handler.take_awaiting_comment(user_id="u1", comment="迟到的补充") is False

    sqls = [c[0][0] for c in cur.execute.call_args_list]
    sweep_idx = next(i for i, s in enumerate(sqls)
                     if "SET handled_status = 'PENDING'" in s and "updated_at <" in s)
    select_idx = next(i for i, s in enumerate(sqls) if s.strip().startswith("SELECT"))
    assert sweep_idx < select_idx, "过期回收必须先于命中查询"
    select_sql = sqls[select_idx]
    assert "updated_at >=" in select_sql, "窗口必须按 updated_at 计"
    assert "created_at" not in select_sql, "created_at 窗口是被修的 bug，禁止回归"
    conn.commit.assert_called_once()  # miss 路径也要把过期回收落地


def test_take_awaiting_comment_db_down_returns_false():
    """取连接失败（RDS 故障）必须按未命中返回 False —— 本函数在私聊主路径上，
    抛出去会让每条私聊问题 500。"""
    from opensearch_pipeline import feedback_handler

    with patch("opensearch_pipeline.db._get_db_conn",
               side_effect=Exception("RDS down")):
        assert feedback_handler.take_awaiting_comment(user_id="u1", comment="补充") is False


def test_save_feedback_odku_clears_awaiting():
    """明确投票（卡片内联表单/小程序 /api/feedback）必须取消挂起的 AWAITING_COMMENT，
    否则用户点过「补充原因」后又提交了表单，下一条私聊仍会被误吞成补充原因。"""
    import inspect
    from opensearch_pipeline import feedback_handler

    source = inspect.getsource(feedback_handler._save_feedback)
    assert "IF(handled_status = 'AWAITING_COMMENT'" in source


def test_webhook_db_outage_degrades_to_normal_question():
    """私聊路径上 take_awaiting_comment 抛异常（RDS 故障）→ 按普通问题继续（ack+起线程），
    绝不向钉钉返回 500。"""
    from opensearch_pipeline import dingtalk_bot

    body = {
        "conversationType": "1",
        "senderStaffId": "staff1",
        "senderNick": "张三",
        "conversationId": "cid1",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?x=1",
        "text": {"content": "差旅费怎么报销"},
        "msgtype": "text",
    }
    with patch("opensearch_pipeline.dingtalk_bot.take_awaiting_comment",
               side_effect=Exception("RDS down")), \
         patch("opensearch_pipeline.dingtalk_bot._send_text_reply") as mock_reply, \
         patch("opensearch_pipeline.dingtalk_bot.threading.Thread") as mock_thread:
        resp = dingtalk_bot._process_webhook_body(body)

    assert resp == {"msgtype": "empty"}, "DB 故障必须降级为普通问答，不能抛 500"
    assert any("正在为您查询" in c[0][1] for c in mock_reply.call_args_list)
    mock_thread.assert_called_once()


def test_webhook_supplement_comment_hit():
    """补充原因命中：单聊消息被收为 comment → 致谢回复、不起 RAG 线程。"""
    from opensearch_pipeline import dingtalk_bot

    body = {
        "conversationType": "1",
        "senderStaffId": "staff1",
        "senderNick": "张三",
        "conversationId": "cid1",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?x=1",
        "text": {"content": "答案里缺少新版流程"},
        "msgtype": "text",
    }
    with patch("opensearch_pipeline.dingtalk_bot.take_awaiting_comment",
               return_value=True) as mock_take, \
         patch("opensearch_pipeline.dingtalk_bot._send_text_reply") as mock_reply, \
         patch("opensearch_pipeline.dingtalk_bot.threading.Thread") as mock_thread:
        resp = dingtalk_bot._process_webhook_body(body)

    assert resp == {"msgtype": "feedback_comment"}
    mock_take.assert_called_once_with(user_id="staff1", comment="答案里缺少新版流程")
    assert any("已记录你补充的原因" in c[0][1] for c in mock_reply.call_args_list)
    mock_thread.assert_not_called()


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
