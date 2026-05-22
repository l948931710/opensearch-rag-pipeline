# -*- coding: utf-8 -*-
"""
test_rag_api.py — RAG 问答 API 单元测试

测试检索器、LLM 生成器、FastAPI 端点（mock 外部 API）。
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# Test: retriever.py
# ═══════════════════════════════════════════════════════════════

class TestRetriever:
    """测试检索模块。"""

    @patch("opensearch_pipeline.retriever.requests.post")
    @patch("opensearch_pipeline.retriever.get_config")
    def test_get_query_embedding_returns_dense_and_sparse(self, mock_config, mock_post):
        """验证 get_query_embedding 正确解析 compatible-mode 响应。"""
        from opensearch_pipeline.retriever import get_query_embedding

        # Mock config
        mock_cfg = MagicMock()
        mock_cfg.embedding.api_key = "test-key"
        mock_cfg.embedding.model = "text-embedding-v4"
        mock_cfg.embedding.dimension = 1024
        mock_cfg.embedding.api_base_url = "https://dashscope.aliyuncs.com"
        mock_config.return_value = mock_cfg

        # Mock DashScope response (compatible-mode format)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [{
                "embedding": [0.01] * 1024,
                "sparse_embedding": {"100": 1.5, "200": 0.8, "50": 2.1},
            }]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dense, sparse_idx, sparse_val = get_query_embedding("测试问题")

        assert len(dense) == 1024
        assert sparse_idx == [50, 100, 200]  # sorted by index
        assert sparse_val == [2.1, 1.5, 0.8]

    @patch("opensearch_pipeline.retriever.requests.post")
    @patch("opensearch_pipeline.retriever.get_config")
    def test_get_query_embedding_no_sparse(self, mock_config, mock_post):
        """验证没有 sparse 时返回空列表。"""
        from opensearch_pipeline.retriever import get_query_embedding

        mock_cfg = MagicMock()
        mock_cfg.embedding.api_key = "test-key"
        mock_cfg.embedding.model = "text-embedding-v4"
        mock_cfg.embedding.dimension = 1024
        mock_cfg.embedding.api_base_url = "https://dashscope.aliyuncs.com"
        mock_config.return_value = mock_cfg

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"embedding": [0.02] * 1024}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        dense, sparse_idx, sparse_val = get_query_embedding("测试")

        assert len(dense) == 1024
        assert sparse_idx == []
        assert sparse_val == []

    @patch("opensearch_pipeline.retriever.requests.post")
    @patch("opensearch_pipeline.retriever.get_config")
    def test_get_query_embedding_raises_without_api_key(self, mock_config, mock_post):
        """API Key 未配置时应抛出错误。"""
        from opensearch_pipeline.retriever import get_query_embedding

        mock_cfg = MagicMock()
        mock_cfg.embedding.api_key = ""
        mock_config.return_value = mock_cfg

        with pytest.raises(RuntimeError, match="未配置"):
            get_query_embedding("测试")

    def test_parse_ha3_response_dict_format(self):
        """测试 HA3 响应解析 — dict 格式。"""
        from opensearch_pipeline.retriever import _parse_ha3_response

        mock_resp = MagicMock()
        mock_resp.body = {
            "result": [
                {
                    "fields": {
                        "chunk_text": "住宿申请流程...",
                        "title": "员工手册",
                        "section_title": "住宿管理",
                        "doc_id": "DOC_001",
                        "category_l1": "行政",
                    },
                    "score": 0.92,
                }
            ]
        }

        results = _parse_ha3_response(mock_resp)
        assert len(results) == 1
        assert results[0]["title"] == "员工手册"
        assert results[0]["score"] == 0.92

    def test_parse_ha3_response_json_string(self):
        """测试 HA3 响应解析 — JSON 字符串格式。"""
        from opensearch_pipeline.retriever import _parse_ha3_response

        mock_resp = MagicMock()
        mock_resp.body = json.dumps({
            "result": [
                {
                    "fields": {"chunk_text": "test", "title": "doc1"},
                    "score": 0.5,
                }
            ]
        })

        results = _parse_ha3_response(mock_resp)
        assert len(results) == 1

    def test_parse_ha3_response_to_map(self):
        """测试 HA3 响应解析 — to_map() 格式。"""
        from opensearch_pipeline.retriever import _parse_ha3_response

        body_obj = MagicMock()
        body_obj.to_map.return_value = {
            "result": [
                {
                    "fields": {"chunk_text": "内容", "title": "标题"},
                    "score": 0.8,
                }
            ]
        }

        mock_resp = MagicMock()
        mock_resp.body = body_obj

        results = _parse_ha3_response(mock_resp)
        assert len(results) == 1
        assert results[0]["chunk_text"] == "内容"


# ═══════════════════════════════════════════════════════════════
# Test: llm_generator.py
# ═══════════════════════════════════════════════════════════════

class TestLLMGenerator:
    """测试 LLM 生成模块。"""

    def test_format_context_basic(self):
        """测试 context 格式化。"""
        from opensearch_pipeline.llm_generator import _format_context

        chunks = [
            {"title": "员工手册", "section_title": "住宿", "chunk_text": "申请住宿需提交表单...", "score": 0.9},
            {"title": "行政制度", "chunk_text": "公司住宿标准...", "score": 0.7},
        ]

        context = _format_context(chunks)
        assert "员工手册" in context
        assert "住宿" in context
        assert "申请住宿需提交表单" in context
        assert "行政制度" in context

    def test_format_context_truncation(self):
        """测试超长 context 截断。"""
        from opensearch_pipeline.llm_generator import _format_context

        chunks = [
            {"title": f"Doc{i}", "chunk_text": "x" * 2000, "score": 0.5}
            for i in range(10)
        ]

        context = _format_context(chunks, max_chars=3000)
        assert len(context) <= 3500  # 允许少量 header 溢出

    def test_extract_sources_deduplication(self):
        """测试来源去重。"""
        from opensearch_pipeline.llm_generator import _extract_sources

        chunks = [
            {"doc_id": "D1", "title": "T1", "section_title": "S1", "score": 0.9},
            {"doc_id": "D1", "title": "T1", "section_title": "S2", "score": 0.8},
            {"doc_id": "D2", "title": "T2", "section_title": "", "score": 0.5},
        ]

        sources = _extract_sources(chunks)
        assert len(sources) == 2  # D1+T1 去重

    def test_build_messages_without_history(self):
        """测试无历史的 messages 构建。"""
        from opensearch_pipeline.llm_generator import _build_messages

        messages = _build_messages("怎么申请住宿", "住宿申请流程...")
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "参考文档" in messages[1]["content"]
        assert "怎么申请住宿" in messages[1]["content"]

    def test_build_messages_with_history(self):
        """测试带历史的 messages 构建。"""
        from opensearch_pipeline.llm_generator import _build_messages

        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，请问有什么问题？"},
        ]
        messages = _build_messages("怎么申请住宿", "流程...", history=history)
        assert len(messages) == 4  # system + 2 history + user
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"

    @patch("opensearch_pipeline.llm_generator.requests.post")
    @patch("opensearch_pipeline.llm_generator.get_config")
    def test_generate_answer(self, mock_config, mock_post):
        """测试非流式生成。"""
        from opensearch_pipeline.llm_generator import generate_answer

        mock_cfg = MagicMock()
        mock_cfg.llm.api_key = "test-key"
        mock_cfg.llm.api_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        mock_cfg.llm.model = "qwen3.6-plus"
        mock_config.return_value = mock_cfg

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "住宿申请需要填写申请表..."}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        chunks = [{"title": "员工手册", "chunk_text": "住宿规定...", "doc_id": "D1", "score": 0.9}]
        result = generate_answer("怎么申请住宿", chunks)

        assert result["answer"] == "住宿申请需要填写申请表..."
        assert result["model"] == "qwen3.6-plus"
        assert len(result["sources"]) == 1


# ═══════════════════════════════════════════════════════════════
# Test: api.py (FastAPI endpoints)
# ═══════════════════════════════════════════════════════════════

class TestAPI:
    """测试 FastAPI 端点。"""

    @pytest.fixture
    def client(self):
        """创建 FastAPI TestClient。"""
        from fastapi.testclient import TestClient
        from opensearch_pipeline.api import app
        return TestClient(app)

    def test_health_check(self, client):
        """健康检查应返回 200。"""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @patch("opensearch_pipeline.api.search_chunks")
    def test_search_endpoint(self, mock_search, client):
        """测试纯检索端点。"""
        mock_search.return_value = [
            {
                "chunk_text": "住宿申请...",
                "title": "员工手册",
                "section_title": "住宿管理",
                "doc_id": "D1",
                "category_l1": "行政",
                "score": 0.9,
            }
        ]

        resp = client.post("/api/search", json={"query": "住宿申请", "top_k": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["results"][0]["title"] == "员工手册"

    @patch("opensearch_pipeline.api.generate_answer")
    @patch("opensearch_pipeline.api.search_chunks")
    def test_ask_endpoint(self, mock_search, mock_gen, client):
        """测试非流式问答端点。"""
        mock_search.return_value = [
            {"chunk_text": "内容", "title": "手册", "doc_id": "D1", "score": 0.9}
        ]
        mock_gen.return_value = {
            "answer": "根据手册...",
            "sources": [{"doc_id": "D1", "title": "手册", "section": "", "score": 0.9}],
            "model": "qwen3.6-plus",
            "usage": {"prompt_tokens": 500, "completion_tokens": 100},
        }

        resp = client.post("/api/ask", json={"question": "怎么申请住宿"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "根据手册..."
        assert data["session_id"]  # 应自动生成 session_id

    @patch("opensearch_pipeline.api.generate_answer")
    @patch("opensearch_pipeline.api.search_chunks")
    def test_ask_with_session(self, mock_search, mock_gen, client):
        """测试多轮对话 — 会话保持。"""
        mock_search.return_value = [
            {"chunk_text": "内容", "title": "手册", "doc_id": "D1", "score": 0.9}
        ]
        mock_gen.return_value = {
            "answer": "第一轮回答",
            "sources": [{"doc_id": "D1", "title": "手册", "section": "", "score": 0.9}],
            "model": "qwen3.6-plus",
            "usage": {},
        }

        # 第一轮
        resp1 = client.post("/api/ask", json={"question": "住宿申请"})
        session_id = resp1.json()["session_id"]

        mock_gen.return_value["answer"] = "第二轮回答"

        # 第二轮用同一个 session_id
        resp2 = client.post("/api/ask", json={
            "question": "还有什么要求",
            "session_id": session_id,
        })
        assert resp2.json()["session_id"] == session_id

    @patch("opensearch_pipeline.api.search_chunks")
    def test_ask_no_results(self, mock_search, client):
        """检索无结果时应返回提示。"""
        mock_search.return_value = []

        resp = client.post("/api/ask", json={"question": "不存在的内容"})
        assert resp.status_code == 200
        assert "未找到" in resp.json()["answer"]

    @patch("opensearch_pipeline.api.generate_answer_stream")
    @patch("opensearch_pipeline.api.search_chunks")
    def test_stream_endpoint(self, mock_search, mock_stream, client):
        """测试 SSE 流式端点。"""
        mock_search.return_value = [
            {"chunk_text": "内容", "title": "手册", "doc_id": "D1", "score": 0.9}
        ]

        def mock_generator(*args, **kwargs):
            yield 'data: {"type": "sources", "sources": []}\n\n'
            yield 'data: {"type": "chunk", "content": "回答"}\n\n'
            yield 'data: {"type": "done", "model": "qwen3.6-plus", "usage": {}}\n\n'
            yield "data: [DONE]\n\n"

        mock_stream.return_value = mock_generator()

        resp = client.post("/api/ask/stream", json={"question": "测试"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        body = resp.text
        assert '"type": "session"' in body
        assert '"type": "chunk"' in body or '"type": "sources"' in body

    def test_ask_validation_empty_question(self, client):
        """空问题应返回 422。"""
        resp = client.post("/api/ask", json={"question": ""})
        assert resp.status_code == 422

    @patch("opensearch_pipeline.api.search_chunks")
    def test_search_error_handling(self, mock_search, client):
        """检索异常时应返回 500。"""
        mock_search.side_effect = RuntimeError("连接失败")

        resp = client.post("/api/search", json={"query": "测试"})
        assert resp.status_code == 500
        assert "检索失败" in resp.json()["detail"]
