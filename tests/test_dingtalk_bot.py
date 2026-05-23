# -*- coding: utf-8 -*-
"""
test_dingtalk_bot.py — 钉钉企业机器人回调适配层完整测试套件

覆盖：
  1. 签名验证（HMAC-SHA256 正例/反例/重放攻击/空密钥）
  2. 消息解析（@前缀去除/私聊/群聊/边界值）
  3. 回答 Markdown 格式化（来源去重/元信息/无来源）
  4. Webhook 端点集成测试（FastAPI TestClient）
  5. 后台 RAG 处理（成功/无结果/异常 trace_id）
"""

import base64
import hashlib
import hmac
import json
import os
import time
import unittest.mock as mock
from unittest.mock import MagicMock, patch, call

import pytest
from starlette.testclient import TestClient

from opensearch_pipeline.dingtalk_bot import (
    _verify_signature,
    _extract_question,
    _get_conversation_type,
    _format_answer_markdown,
    _send_reply,
    _send_text_reply,
    _process_rag_query,
)


# ═══════════════════════════════════════════════════════════════
# 辅助工具
# ═══════════════════════════════════════════════════════════════

def _compute_valid_signature(timestamp_ms: str, app_secret: str) -> str:
    """按钉钉官方算法计算合法签名。"""
    string_to_sign = f"{timestamp_ms}\n{app_secret}"
    hmac_code = hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


_TEST_SECRET = "test_dingtalk_app_secret_2026"


@pytest.fixture(autouse=True)
def set_app_secret():
    """注入测试密钥，测试完成后恢复。"""
    original = os.environ.get("DINGTALK_APP_SECRET")
    os.environ["DINGTALK_APP_SECRET"] = _TEST_SECRET
    yield
    if original is None:
        os.environ.pop("DINGTALK_APP_SECRET", None)
    else:
        os.environ["DINGTALK_APP_SECRET"] = original


# ═══════════════════════════════════════════════════════════════
# 1. 签名验证测试（P1 安全关键路径）
# ═══════════════════════════════════════════════════════════════

class TestSignatureVerification:
    """HMAC-SHA256 签名验证：正例、反例、重放攻击、空密钥。"""

    def test_valid_signature_passes(self):
        """使用真实 HMAC 算法生成签名，验证通过。"""
        ts = str(int(time.time() * 1000))
        sign = _compute_valid_signature(ts, _TEST_SECRET)
        assert _verify_signature(ts, sign) is True

    def test_invalid_signature_rejected(self):
        """篡改的签名必须被拒绝。"""
        ts = str(int(time.time() * 1000))
        sign = _compute_valid_signature(ts, _TEST_SECRET)
        # 篡改最后一个字符
        tampered = sign[:-1] + ("A" if sign[-1] != "A" else "B")
        assert _verify_signature(ts, tampered) is False

    def test_expired_timestamp_rejected(self):
        """超过 3600 秒的过期时间戳必须被拒绝。"""
        expired_ts = str(int((time.time() - 3601) * 1000))
        sign = _compute_valid_signature(expired_ts, _TEST_SECRET)
        assert _verify_signature(expired_ts, sign) is False

    def test_future_timestamp_rejected(self):
        """超过 3600 秒的未来时间戳必须被拒绝。"""
        future_ts = str(int((time.time() + 3601) * 1000))
        sign = _compute_valid_signature(future_ts, _TEST_SECRET)
        assert _verify_signature(future_ts, sign) is False

    def test_replay_attack_different_secret(self):
        """使用不同密钥生成的签名（模拟攻击者窃取了旧签名）必须被拒绝。"""
        ts = str(int(time.time() * 1000))
        attacker_sign = _compute_valid_signature(ts, "attacker_stolen_secret")
        assert _verify_signature(ts, attacker_sign) is False

    def test_missing_app_secret_rejects_all(self):
        """当 DINGTALK_APP_SECRET 未配置时，所有请求必须被拒绝。"""
        os.environ["DINGTALK_APP_SECRET"] = ""
        ts = str(int(time.time() * 1000))
        sign = _compute_valid_signature(ts, "")
        assert _verify_signature(ts, sign) is False

    def test_non_numeric_timestamp_rejected(self):
        """非数字时间戳字符串必须被拒绝。"""
        assert _verify_signature("not-a-number", "any_sign") is False

    def test_empty_timestamp_rejected(self):
        """空字符串时间戳必须被拒绝。"""
        assert _verify_signature("", "any_sign") is False

    def test_boundary_timestamp_within_tolerance(self):
        """刚好在 3600 秒窗口边缘内的时间戳应通过。"""
        boundary_ts = str(int((time.time() - 3599) * 1000))
        sign = _compute_valid_signature(boundary_ts, _TEST_SECRET)
        assert _verify_signature(boundary_ts, sign) is True


# ═══════════════════════════════════════════════════════════════
# 2. 消息解析测试
# ═══════════════════════════════════════════════════════════════

class TestMessageExtraction:
    """从钉钉回调 body 中提取用户问题文本。"""

    def test_extract_plain_private_message(self):
        """私聊场景：直接提取 content 内容。"""
        body = {"text": {"content": "请问年假政策是什么？"}}
        assert _extract_question(body) == "请问年假政策是什么？"

    def test_extract_group_message_strips_at_prefix(self):
        """群聊场景：自动去除 @机器人 前缀。"""
        body = {"text": {"content": "@知识库助手 请问年假政策是什么？"}}
        assert _extract_question(body) == "请问年假政策是什么？"

    def test_extract_group_message_strips_at_english_name(self):
        """群聊场景：@英文名 前缀。"""
        body = {"text": {"content": "@KBBot how to apply for leave?"}}
        assert _extract_question(body) == "how to apply for leave?"

    def test_extract_empty_content(self):
        """空 content 返回空字符串。"""
        body = {"text": {"content": ""}}
        assert _extract_question(body) == ""

    def test_extract_missing_text_key(self):
        """body 中不包含 text 键返回空字符串。"""
        body = {"msgId": "some_id"}
        assert _extract_question(body) == ""

    def test_extract_whitespace_only(self):
        """仅空白字符的 content 返回空字符串。"""
        body = {"text": {"content": "   \n\t  "}}
        assert _extract_question(body) == ""

    def test_get_conversation_type_group(self):
        """群聊返回 '2'。"""
        body = {"conversationType": "2"}
        assert _get_conversation_type(body) == "2"

    def test_get_conversation_type_private(self):
        """私聊返回 '1'。"""
        body = {"conversationType": "1"}
        assert _get_conversation_type(body) == "1"

    def test_get_conversation_type_default(self):
        """缺失 conversationType 默认返回 '1'。"""
        body = {}
        assert _get_conversation_type(body) == "1"


# ═══════════════════════════════════════════════════════════════
# 3. 回答 Markdown 格式化测试
# ═══════════════════════════════════════════════════════════════

class TestAnswerFormatting:
    """将 RAG 回答格式化为钉钉 Markdown。"""

    def test_format_with_sources(self):
        """包含参考来源时，正确渲染来源列表和相关度。"""
        sources = [
            {"title": "员工手册", "section": "第三章", "score": 0.92},
            {"title": "考勤规定", "section": "", "score": 0.85},
        ]
        md = _format_answer_markdown(
            question="年假天数",
            answer="正式员工享有 5 天年假。",
            sources=sources,
            latency_ms=1200,
            model="qwen-max",
        )
        assert "💡 年假天数" in md
        assert "正式员工享有 5 天年假。" in md
        assert "员工手册" in md
        assert "> 第三章" in md
        assert "92%" in md
        assert "考勤规定" in md
        assert "qwen-max" in md
        assert "1.2s" in md

    def test_format_without_sources(self):
        """无参考来源时不渲染来源区块。"""
        md = _format_answer_markdown(
            question="什么是 AI",
            answer="人工智能。",
            sources=[],
            latency_ms=500,
            model="qwen-plus",
        )
        assert "📚" not in md
        assert "人工智能。" in md

    def test_format_duplicate_source_dedup(self):
        """相同 title 的来源只出现一次。"""
        sources = [
            {"title": "同一文档", "section": "第一节", "score": 0.9},
            {"title": "同一文档", "section": "第二节", "score": 0.8},
        ]
        md = _format_answer_markdown(
            question="问题",
            answer="答案",
            sources=sources,
            latency_ms=100,
            model="test",
        )
        # 只出现一次
        assert md.count("同一文档") == 1


# ═══════════════════════════════════════════════════════════════
# 4. Webhook 端点集成测试
# ═══════════════════════════════════════════════════════════════

class TestWebhookEndpoint:
    """钉钉 /dingtalk/webhook 端点的 FastAPI 集成测试。"""

    @pytest.fixture
    def client(self):
        """创建 TestClient。"""
        from opensearch_pipeline.api import app
        return TestClient(app)

    def _make_headers(self, timestamp_ms: str = None) -> dict:
        """构造带有合法签名的请求头。"""
        ts = timestamp_ms or str(int(time.time() * 1000))
        sign = _compute_valid_signature(ts, _TEST_SECRET)
        return {"timestamp": ts, "sign": sign}

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    @patch("opensearch_pipeline.dingtalk_bot._process_rag_query")
    def test_valid_webhook_returns_200(self, mock_rag, mock_reply, client):
        """合法签名 + 有效消息体 → 200 + 启动后台线程。"""
        body = {
            "text": {"content": "年假政策"},
            "sessionWebhook": "https://oapi.dingtalk.com/robot/send?session=test",
            "senderNick": "张三",
            "conversationId": "cid123",
        }
        resp = client.post("/dingtalk/webhook", json=body, headers=self._make_headers())
        assert resp.status_code == 200
        assert resp.json()["msgtype"] == "empty"
        # 验证发送了 "正在查询" 提示
        mock_reply.assert_called_once()
        assert "正在为您查询" in mock_reply.call_args[0][1]

    def test_invalid_signature_returns_403(self, client):
        """错误签名 → 403。"""
        headers = {"timestamp": str(int(time.time() * 1000)), "sign": "INVALID_SIGN"}
        body = {"text": {"content": "test"}, "sessionWebhook": "https://example.com"}
        resp = client.post("/dingtalk/webhook", json=body, headers=headers)
        assert resp.status_code == 403

    def test_missing_signature_returns_403(self, client):
        """缺少签名头 → 403。"""
        body = {"text": {"content": "test"}, "sessionWebhook": "https://example.com"}
        resp = client.post("/dingtalk/webhook", json=body, headers={})
        assert resp.status_code == 403

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    def test_empty_question_sends_greeting(self, mock_reply, client):
        """空问题 → 发送欢迎消息 + 返回 200。"""
        body = {
            "text": {"content": ""},
            "sessionWebhook": "https://oapi.dingtalk.com/session",
            "senderNick": "李四",
        }
        resp = client.post("/dingtalk/webhook", json=body, headers=self._make_headers())
        assert resp.status_code == 200
        assert resp.json()["msgtype"] == "empty"
        mock_reply.assert_called_once()
        assert "您好" in mock_reply.call_args[0][1]

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    def test_missing_session_webhook_returns_400(self, mock_reply, client):
        """有问题但无 sessionWebhook → 400。"""
        body = {
            "text": {"content": "有问题"},
            "senderNick": "王五",
        }
        resp = client.post("/dingtalk/webhook", json=body, headers=self._make_headers())
        assert resp.status_code == 400

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    def test_at_prefix_stripped_in_webhook(self, mock_reply, client):
        """群聊 @机器人 消息经过端点后 @前缀被正确去除。"""
        body = {
            "text": {"content": "@Bot 测试问题"},
            "sessionWebhook": "https://oapi.dingtalk.com/session",
            "senderNick": "赵六",
            "conversationType": "2",
        }
        with patch("opensearch_pipeline.dingtalk_bot.threading.Thread") as mock_thread:
            mock_thread_instance = MagicMock()
            mock_thread.return_value = mock_thread_instance
            resp = client.post("/dingtalk/webhook", json=body, headers=self._make_headers())
            assert resp.status_code == 200
            # 验证传递给后台线程的 question 已去除 @前缀
            _, kwargs = mock_thread.call_args
            assert kwargs["args"][0] == "测试问题"


# ═══════════════════════════════════════════════════════════════
# 5. 后台 RAG 处理测试
# ═══════════════════════════════════════════════════════════════

class TestBackgroundRAGProcessing:
    """后台 RAG 检索 + LLM 生成的线程逻辑测试。"""

    @patch("opensearch_pipeline.dingtalk_bot._send_reply")
    @patch("opensearch_pipeline.dingtalk_bot.generate_answer")
    @patch("opensearch_pipeline.dingtalk_bot.search_chunks")
    def test_rag_success_sends_markdown_reply(self, mock_search, mock_gen, mock_reply):
        """RAG 成功 → 通过 _send_reply 发送 Markdown 回复。"""
        mock_search.return_value = [
            {"chunk_text": "年假 5 天", "title": "员工手册", "section_title": "第三章",
             "doc_id": "doc1", "category_l1": "sop", "score": 0.92}
        ]
        mock_gen.return_value = {
            "answer": "正式员工享有 5 天年假。",
            "sources": [{"title": "员工手册", "section": "第三章", "score": 0.92, "doc_id": "doc1"}],
            "model": "qwen-max",
            "usage": {},
        }

        _process_rag_query("年假几天", "https://webhook/test", "张三", "cid1")

        mock_search.assert_called_once_with("年假几天", top_k=5)
        mock_gen.assert_called_once()
        mock_reply.assert_called_once()
        # 验证 Markdown 内容
        md_text = mock_reply.call_args[0][2]
        assert "年假" in md_text
        assert "员工手册" in md_text

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    @patch("opensearch_pipeline.dingtalk_bot.search_chunks")
    def test_rag_no_results_sends_fallback_text(self, mock_search, mock_reply):
        """检索无结果 → 发送 '未找到相关信息' 文本回复。"""
        mock_search.return_value = []

        _process_rag_query("不存在的问题", "https://webhook/test", "李四", "cid2")

        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][1]
        assert "未找到" in reply_text

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    @patch("opensearch_pipeline.dingtalk_bot.search_chunks")
    def test_rag_exception_sends_error_with_trace_id(self, mock_search, mock_reply):
        """search_chunks 抛出异常 → 发送包含 trace ID 的错误回复。"""
        mock_search.side_effect = ConnectionError("HA3 connection refused")

        _process_rag_query("异常问题", "https://webhook/test", "王五", "cid3")

        mock_reply.assert_called_once()
        reply_text = mock_reply.call_args[0][1]
        assert "出错" in reply_text
        assert "trace:" in reply_text

    @patch("opensearch_pipeline.dingtalk_bot._send_text_reply")
    @patch("opensearch_pipeline.dingtalk_bot._send_reply")
    @patch("opensearch_pipeline.dingtalk_bot.generate_answer")
    @patch("opensearch_pipeline.dingtalk_bot.search_chunks")
    def test_rag_llm_failure_sends_error(self, mock_search, mock_gen, mock_md_reply, mock_txt_reply):
        """LLM 生成失败 → 发送错误回复。"""
        mock_search.return_value = [{"chunk_text": "一些内容", "title": "文档", "doc_id": "d1", "score": 0.9}]
        mock_gen.side_effect = RuntimeError("DashScope API timeout")

        _process_rag_query("LLM失败", "https://webhook/test", "赵六", "cid4")

        # _send_reply 不应被调用（LLM 失败了）
        mock_md_reply.assert_not_called()
        # _send_text_reply 应发送错误消息
        mock_txt_reply.assert_called_once()
        assert "trace:" in mock_txt_reply.call_args[0][1]


# ═══════════════════════════════════════════════════════════════
# 6. 回复发送函数测试
# ═══════════════════════════════════════════════════════════════

class TestReplySending:
    """_send_reply 和 _send_text_reply 的 HTTP 调用测试。"""

    @patch("opensearch_pipeline.dingtalk_bot.http_requests.post")
    def test_send_reply_posts_markdown(self, mock_post):
        """_send_reply 发送 Markdown 格式的消息。"""
        mock_post.return_value = MagicMock(status_code=200)
        _send_reply("https://webhook/session", "标题", "**正文**")

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://webhook/session"
        payload = kwargs["json"]
        assert payload["msgtype"] == "markdown"
        assert payload["markdown"]["title"] == "标题"
        assert payload["markdown"]["text"] == "**正文**"

    @patch("opensearch_pipeline.dingtalk_bot.http_requests.post")
    def test_send_text_reply_posts_text(self, mock_post):
        """_send_text_reply 发送纯文本消息。"""
        mock_post.return_value = MagicMock(status_code=200)
        _send_text_reply("https://webhook/session", "hello")

        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["msgtype"] == "text"
        assert payload["text"]["content"] == "hello"

    @patch("opensearch_pipeline.dingtalk_bot.http_requests.post")
    def test_send_reply_handles_network_error(self, mock_post):
        """网络异常不应导致 _send_reply 崩溃。"""
        mock_post.side_effect = ConnectionError("network down")
        # 不应抛出异常
        _send_reply("https://webhook/session", "标题", "正文")

    @patch("opensearch_pipeline.dingtalk_bot.http_requests.post")
    def test_send_text_reply_handles_timeout(self, mock_post):
        """超时不应导致 _send_text_reply 崩溃。"""
        mock_post.side_effect = TimeoutError("timeout")
        _send_text_reply("https://webhook/session", "text")
