# -*- coding: utf-8 -*-
"""
test_answer_flow_parity.py — 四条回答链路（/api/ask、/api/ask/stream、钉钉同步、钉钉流式）
落库/历史/文案行为的特征化（characterization）测试。

先于 answer_flow 重构落地：精确钉死【当前】行为 —— 包括已知缺陷（标 KNOWN BUG）。
重构的机械改造提交（Commit C）必须保持本文件全绿；之后每个修复提交（D1-D4）
显式翻转对应的 KNOWN BUG 断言。

外部依赖（检索/LLM/落库/卡片）全部 mock，无需真实服务。
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from opensearch_pipeline import auth_token
from opensearch_pipeline.dingtalk_bot import _process_rag_query


# ═══════════════════════════════════════════════════════════════
# 公共假件
# ═══════════════════════════════════════════════════════════════

API_CHUNKS = [
    {"doc_id": "d1", "title": "员工手册", "section_title": "第三章",
     "chunk_text": "住宿规定...", "score": 9.0},
]

GEN_RESULT = {
    "answer": "答案正文",
    "sources": [{"doc_id": "d1", "title": "员工手册", "section": "第三章", "score": 9.0}],
    "model": "qwen-test",
    "usage": {"total_tokens": 10},
}


def _parse_sse(body: str):
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


@pytest.fixture
def client():
    from opensearch_pipeline.api import app
    return TestClient(app)


def _token():
    return auth_token.issue_session_token("U9", dept="行政部", name="李四")


# ═══════════════════════════════════════════════════════════════
# 1. /api/ask（API 同步）
# ═══════════════════════════════════════════════════════════════

class TestApiAskBookkeeping:
    @patch("opensearch_pipeline.api.build_mini_program_blocks",
           return_value=[{"type": "text", "format": "plain", "text": "答案正文"}])
    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api._append_to_history")
    @patch("opensearch_pipeline.api.generate_answer", return_value=GEN_RESULT)
    @patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=API_CHUNKS)
    def test_ask_success_log_kwargs_with_token(
        self, mock_retrieve, mock_gen, mock_append, mock_log, mock_blocks, client
    ):
        """令牌请求的成功路径落库字段全集（含三处 KNOWN BUG 的现状钉死）。"""
        resp = client.post(
            "/api/ask", json={"question": "怎么申请住宿"},
            headers={"Authorization": "Bearer " + _token()},
        )
        assert resp.status_code == 200
        j = resp.json()

        mock_log.assert_called_once()
        kw = mock_log.call_args.kwargs
        assert kw["session_id"] == j["session_id"]
        assert kw["message_id"] == j["message_id"]
        assert kw["query_text"] == "怎么申请住宿"
        assert kw["answer_text"] == "答案正文"
        assert kw["answer_status"] == "SUCCESS"
        assert kw["model_name"] == "qwen-test"
        assert kw["cited_docs"] == GEN_RESULT["sources"]
        assert kw["opensearch_hit_count"] == 1
        assert kw["top_score"] == 9.0
        assert kw["retrieved_docs"] == API_CHUNKS
        # 已修复（原 KNOWN BUG）：成功路径落已解析身份 uid（令牌里的 U9），不再用请求体 user_id
        assert kw["user_id"] == "U9"
        # 已修复（原 KNOWN BUG）：令牌部门现已落库
        assert kw["user_dept"] == "行政部"
        # 已修复（原 KNOWN BUG）：成功路径落实际下发的小程序块（序列化 JSON）
        assert kw["content_blocks_json"] and "答案正文" in kw["content_blocks_json"]

    @patch("opensearch_pipeline.api.build_mini_program_blocks", return_value=[])
    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api._append_to_history")
    @patch("opensearch_pipeline.api.generate_answer", return_value=GEN_RESULT)
    @patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=API_CHUNKS)
    def test_ask_anonymous_ignores_body_user_id(
        self, mock_retrieve, mock_gen, mock_append, mock_log, mock_blocks, client
    ):
        """匿名 + 请求体 user_id：绝不采信请求体 user_id 作落库身份（F-5 IDOR 修复）。

        /api/history 按令牌 identity.user_id 过滤本人记录；若匿名请求能写入任意 staffId，
        攻击者即可把伪造问答注入受害者历史。改为落 anon:<ip 短哈希>，与真实 staffId 命名空间
        天然不相交（永不出现在任何人历史里），请求体自报的 EMP1 被彻底忽略。"""
        resp = client.post("/api/ask", json={"question": "q", "user_id": "EMP1"})
        assert resp.status_code == 200
        uid = mock_log.call_args.kwargs["user_id"]
        assert uid != "EMP1"
        assert uid.startswith("anon:")

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=[])
    def test_ask_no_result_text_and_log(self, mock_retrieve, mock_log, client):
        """NO_RESULT：精确文案 + 落库字段；user_id 此分支本来就用 uid（无 bug）。"""
        resp = client.post(
            "/api/ask", json={"question": "不存在的问题"},
            headers={"Authorization": "Bearer " + _token()},
        )
        assert resp.status_code == 200
        j = resp.json()
        assert j["answer"] == "抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述您的问题。"
        assert j["model"] == "N/A"

        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "NO_RESULT"
        assert kw["opensearch_hit_count"] == 0
        assert kw["message_id"] == j["message_id"]
        assert kw["user_id"] == "U9"
        # 已修复（原 KNOWN BUG）：NO_RESULT 分支现也落 user_dept
        assert kw["user_dept"] == "行政部"

    @patch("opensearch_pipeline.api.build_mini_program_blocks", return_value=[])
    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api._append_to_history")
    @patch("opensearch_pipeline.api.generate_answer", return_value=GEN_RESULT)
    @patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=API_CHUNKS)
    def test_ask_appends_history_once(
        self, mock_retrieve, mock_gen, mock_append, mock_log, mock_blocks, client
    ):
        """成功路径写一次会话历史 (session_id, q, answer)。"""
        resp = client.post("/api/ask", json={"question": "q"})
        assert resp.status_code == 200
        mock_append.assert_called_once()
        sid, q, a = mock_append.call_args.args
        assert sid == resp.json()["session_id"]
        assert q == "q"
        assert a == "答案正文"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api._append_to_history")
    @patch("opensearch_pipeline.api.generate_answer", side_effect=RuntimeError("LLM down"))
    @patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=API_CHUNKS)
    def test_ask_generation_error_returns_500(
        self, mock_retrieve, mock_gen, mock_append, mock_log, client
    ):
        """生成异常 → HTTP 500；已修复（原 KNOWN BUG）：现以 LLM_ERROR 落库一行。"""
        resp = client.post("/api/ask", json={"question": "q"})
        assert resp.status_code == 500
        assert "trace" in resp.json()["detail"]
        mock_append.assert_not_called()
        mock_log.assert_called_once()
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "LLM_ERROR"
        assert kw["error_message"] and "trace=" in kw["error_message"]
        assert kw["opensearch_hit_count"] == 1


# ═══════════════════════════════════════════════════════════════
# 2. /api/ask/stream（API 流式）— 补 test_stream_feedback_parity 未钉死的缺口
# ═══════════════════════════════════════════════════════════════

def _fake_stream(*args, **kwargs):
    yield 'data: {"type": "sources", "sources": []}\n\n'
    yield 'data: {"type": "chunk", "content": "住宿申请"}\n\n'
    yield 'data: {"type": "done", "model": "qwen-test", "usage": {}}\n\n'
    yield "data: [DONE]\n\n"


class TestApiStreamBookkeeping:
    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api._append_to_history")
    @patch("opensearch_pipeline.api.generate_answer_stream", side_effect=_fake_stream)
    @patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=API_CHUNKS)
    def test_stream_log_dept_with_token(
        self, mock_retrieve, mock_stream, mock_append, mock_log, client
    ):
        """已修复（原 KNOWN BUG）：流式收尾落库现含令牌部门（user_id 一直用 uid）。"""
        resp = client.post(
            "/api/ask/stream", json={"question": "q", "pure_text": True},
            headers={"Authorization": "Bearer " + _token()},
        )
        assert resp.status_code == 200
        kw = mock_log.call_args.kwargs
        assert kw["user_id"] == "U9"
        assert kw["user_dept"] == "行政部"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api._append_to_history")
    @patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=API_CHUNKS)
    def test_stream_partial_answer_on_error_history(
        self, mock_retrieve, mock_append, mock_log, client
    ):
        """已统一（原 KNOWN DRIFT）：生成中途抛错，部分回答不再写入会话历史
        （与钉钉流式一致：仅非空 SUCCESS 回答入史，残句不污染后续轮次）。"""
        def _boom(*args, **kwargs):
            yield 'data: {"type": "chunk", "content": "部分"}\n\n'
            raise RuntimeError("upstream down")

        with patch("opensearch_pipeline.api.generate_answer_stream", side_effect=_boom):
            resp = client.post("/api/ask/stream", json={"question": "q", "pure_text": True})
        assert resp.status_code == 200
        assert mock_log.call_args.kwargs["answer_status"] == "LLM_ERROR"
        mock_append.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 3. 钉钉同步（_process_rag_query 非流式分支）
# ═══════════════════════════════════════════════════════════════

BOT_CHUNKS = [
    {"doc_id": "D1", "title": "员工手册", "section_title": "第三章",
     "chunk_text": "年假 5 天", "score": 8.5},
]


class TestBotSyncBookkeeping:
    @patch("opensearch_pipeline.dingtalk_bot.send_interactive_card", return_value=True)
    @patch("opensearch_pipeline.dingtalk_bot._resolve_user_dept", return_value="生产中心")
    @patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
    @patch("opensearch_pipeline.dingtalk_bot.append_to_history")
    @patch("opensearch_pipeline.dingtalk_bot.generate_answer", return_value=GEN_RESULT)
    @patch("opensearch_pipeline.dingtalk_bot.retrieve_and_enrich", return_value=BOT_CHUNKS)
    def test_bot_sync_success_full_log_kwargs(
        self, mock_retrieve, mock_gen, mock_append, mock_log, mock_dept, mock_card,
        monkeypatch,
    ):
        """同步成功路径的落库字段全集（user_dept/conversation_type 都在）。

        机器人渠道纯文本已写死在调用点（088a6ec）：content_blocks 不再构建，
        落库 content_blocks_json 必须为 None。monkeypatch 保留作绊线 ——
        若有人把 bot 路径改回图文，这里会拿到非 None 立即失败。
        """
        import opensearch_pipeline.content_blocks_builder as cb
        monkeypatch.setattr(cb, "build_content_blocks",
                            lambda ans, chunks: [{"type": "image", "url": "http://x/a.png"}])

        _process_rag_query("年假几天", "https://webhook/test", "张三", "cid1",
                           sender_staff_id="staff9", conversation_type="2")

        mock_log.assert_called_once()
        kw = mock_log.call_args.kwargs
        assert kw["session_id"] == "cid1:staff9"
        assert kw["user_id"] == "staff9"
        assert kw["user_name"] == "张三"
        assert kw["user_dept"] == "生产中心"
        assert kw["conversation_type"] == "2"
        assert kw["answer_status"] == "SUCCESS"
        assert kw["answer_text"] == "答案正文"
        assert kw["cited_docs"] == GEN_RESULT["sources"]
        assert kw["model_name"] == "qwen-test"
        assert kw["opensearch_hit_count"] == 1
        assert kw["top_score"] == 8.5
        assert kw["content_blocks_json"] is None
        mock_append.assert_called_once()
        assert mock_append.call_args.args[0] == "cid1:staff9"

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    @patch("opensearch_pipeline.dingtalk_bot._resolve_user_dept", return_value="生产中心")
    @patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
    @patch("opensearch_pipeline.dingtalk_bot.retrieve_and_enrich", return_value=[])
    def test_bot_sync_no_result_exact_text_and_log(
        self, mock_retrieve, mock_log, mock_dept, mock_reply
    ):
        """NO_RESULT：精确文案（已统一 = 🤷 + 各端共用 NO_RESULT_MESSAGE）+ 落库含 user_dept。"""
        _process_rag_query("不存在", "https://webhook/test", "李四", "cid2",
                           sender_staff_id="staff9")
        mock_reply.assert_called_once()
        assert mock_reply.call_args.args[1] == (
            "🤷 抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述您的问题。"
        )
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "NO_RESULT"
        assert kw["answer_text"] is None
        assert kw["user_dept"] == "生产中心"
        assert kw["opensearch_hit_count"] == 0

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    @patch("opensearch_pipeline.dingtalk_bot._resolve_user_dept", return_value="生产中心")
    @patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
    @patch("opensearch_pipeline.dingtalk_bot.generate_answer", side_effect=RuntimeError("boom"))
    @patch("opensearch_pipeline.dingtalk_bot.retrieve_and_enrich", return_value=BOT_CHUNKS)
    def test_bot_sync_error_log_kwargs(
        self, mock_retrieve, mock_gen, mock_log, mock_dept, mock_reply
    ):
        """生成异常：LLM_ERROR 落库 + trace 回复；已修复（原 KNOWN BUG）：
        except 落库现补全 user_dept 与 retrieval_latency_ms。"""
        _process_rag_query("LLM失败", "https://webhook/test", "赵六", "cid4",
                           sender_staff_id="staff9")
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "LLM_ERROR"
        assert kw["error_message"] and "trace=" in kw["error_message"]
        assert kw["user_dept"] == "生产中心"
        assert isinstance(kw["retrieval_latency_ms"], int)
        assert "trace:" in mock_reply.call_args.args[1]

    @patch("opensearch_pipeline.dingtalk_bot.send_interactive_card", return_value=True)
    @patch("opensearch_pipeline.dingtalk_bot._stream_answer_to_card")
    @patch("opensearch_pipeline.dingtalk_bot._resolve_user_dept", return_value=None)
    @patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
    @patch("opensearch_pipeline.dingtalk_bot.append_to_history")
    @patch("opensearch_pipeline.dingtalk_bot.generate_answer", return_value=GEN_RESULT)
    @patch("opensearch_pipeline.dingtalk_bot.retrieve_and_enrich", return_value=BOT_CHUNKS)
    def test_bot_sync_stream_guard_no_double_log(
        self, mock_retrieve, mock_gen, mock_append, mock_log, mock_dept,
        mock_stream_card, mock_card, monkeypatch,
    ):
        """流式护栏：流式处理成功 ⇒ 调用方零落库零生成；失败 ⇒ 恰好一次落库（不重复）。"""
        import opensearch_pipeline.content_blocks_builder as cb
        monkeypatch.setattr(cb, "build_content_blocks", lambda ans, chunks: [])
        monkeypatch.setenv("DINGTALK_STREAM_CARD_TEMPLATE_ID", "tpl-x")

        cfg = MagicMock()
        cfg.rag.dingtalk_streaming = True
        cfg.rag.pure_text = False
        monkeypatch.setattr("opensearch_pipeline.dingtalk_bot.get_config", lambda: cfg)

        # a) 流式路径完整处理 → 调用方不再落库/生成
        mock_stream_card.return_value = True
        _process_rag_query("q", "https://wh", "张三", "cid", sender_staff_id="s1")
        mock_log.assert_not_called()
        mock_gen.assert_not_called()

        # b) 流式投放失败 → 降级到非流式，恰好一次落库
        mock_stream_card.return_value = False
        _process_rag_query("q", "https://wh", "张三", "cid", sender_staff_id="s1")
        mock_gen.assert_called_once()
        mock_log.assert_called_once()
        assert mock_log.call_args.kwargs["answer_status"] == "SUCCESS"


# ═══════════════════════════════════════════════════════════════
# 4. 钉钉流式（_stream_answer_to_card）— 补 test_dingtalk_streaming 未钉死的字段
# ═══════════════════════════════════════════════════════════════

def _fake_bot_cfg():
    cfg = MagicMock()
    cfg.rag.dingtalk_stream_interval_ms = 0
    cfg.llm.model = "qwen-test"
    return cfg


class TestBotStreamBookkeeping:
    @patch("opensearch_pipeline.dingtalk_bot.log_qa_session")
    @patch("opensearch_pipeline.dingtalk_bot.append_to_history")
    @patch("opensearch_pipeline.dingtalk_bot.streaming_update_card", return_value=True)
    @patch("opensearch_pipeline.dingtalk_bot.create_streaming_card", return_value=True)
    @patch("opensearch_pipeline.dingtalk_bot.generate_answer_stream", side_effect=_fake_stream)
    @patch("opensearch_pipeline.dingtalk_bot.get_config", return_value=_fake_bot_cfg())
    def test_streaming_log_kwargs_full(
        self, mock_cfg, mock_stream, mock_create, mock_update, mock_append, mock_log
    ):
        """落库字段全集：user_name/conversation_type/hit/top_score/cited_docs/延迟/session。"""
        from opensearch_pipeline import dingtalk_bot

        handled = dingtalk_bot._stream_answer_to_card(
            question="怎么申请住宿",
            chunks=[{"doc_id": "D1", "title": "员工手册", "chunk_text": "住宿规定...", "score": 9.0}],
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
        assert handled is True
        kw = mock_log.call_args.kwargs
        assert kw["session_id"] == "conv1:staff1"
        assert kw["user_name"] == "张三"
        assert kw["conversation_type"] == "1"
        assert kw["opensearch_hit_count"] == 1
        assert kw["top_score"] == 9.0
        assert kw["retrieval_latency_ms"] == 12
        assert isinstance(kw["llm_latency_ms"], int)
        assert kw["cited_docs"] and kw["cited_docs"][0]["title"] == "员工手册"
