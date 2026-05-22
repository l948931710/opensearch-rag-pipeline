# -*- coding: utf-8 -*-
"""
test_ha3_engine.py — HA3 Engine 推送/删除逻辑的单元测试

无需阿里云实例。通过 Mock SDK 对象验证：
1. HA3 请求构造 (to_ha3_doc 字段映射)
2. pushDocuments 成功/部分失败/全部失败的响应解析
3. 瞬时错误指数退避重试机制
4. HA3 删除的幂等判断 (not_found → 视为成功)
"""

import sys
import time
import types
import pytest
from unittest.mock import MagicMock, patch, call
from opensearch_pipeline.chunker import Chunk


# ═══════════════════════════════════════════════════════════════
# 预注入 HA3 SDK 的 mock module（因为本地未安装 alibabacloud_ha3engine_vector）
# ═══════════════════════════════════════════════════════════════

def _ensure_ha3_mock_modules():
    """在 sys.modules 中注入 HA3 SDK 的 mock 模块。"""
    if "alibabacloud_ha3engine_vector" not in sys.modules:
        ha3_pkg = types.ModuleType("alibabacloud_ha3engine_vector")
        ha3_models = types.ModuleType("alibabacloud_ha3engine_vector.models")
        ha3_client = types.ModuleType("alibabacloud_ha3engine_vector.client")

        # Mock PushDocumentsRequest
        class MockPushDocumentsRequest:
            def __init__(self):
                self._body = None
            def set_body(self, body):
                self._body = body

        ha3_models.PushDocumentsRequest = MockPushDocumentsRequest
        ha3_models.Config = MagicMock

        ha3_client.Client = MagicMock

        sys.modules["alibabacloud_ha3engine_vector"] = ha3_pkg
        sys.modules["alibabacloud_ha3engine_vector.models"] = ha3_models
        sys.modules["alibabacloud_ha3engine_vector.client"] = ha3_client

_ensure_ha3_mock_modules()


# ═══════════════════════════════════════════════════════════════
# Section A: to_ha3_doc 字段映射测试
# ═══════════════════════════════════════════════════════════════

class TestHA3DocMapping:
    """验证 Chunk.to_ha3_doc() 的字段映射与格式正确性。"""

    def _make_chunk(self, **overrides):
        defaults = dict(
            chunk_id="doc1_v1_c0001", doc_id="doc1", version_no=1,
            chunk_index=1, chunk_type="text_chunk", chunk_text="测试文本",
            token_count=5, page_num=1, permission_level="PUBLIC",
            owner_dept="hr", category_l1="policy", category_l2="hr_policy",
            embedding_vector=[0.1, 0.2, 0.3],
        )
        defaults.update(overrides)
        return Chunk(**defaults)

    def test_pk_field_name_is_configurable(self):
        """HA3 主键字段名由 pk_field 决定，而非硬编码 'id'。"""
        chunk = self._make_chunk()
        doc = chunk.to_ha3_doc(pk_field="custom_pk")
        assert "custom_pk" in doc
        assert doc["custom_pk"] == "doc1_v1_c0001"
        assert "id" not in doc

    def test_vector_serialized_as_comma_string(self):
        """HA3 不支持 JSON 数组，向量必须序列化为逗号分隔的浮点字符串。"""
        chunk = self._make_chunk(embedding_vector=[0.1, -0.2, 0.33333])
        doc = chunk.to_ha3_doc()
        assert isinstance(doc["chunk_vector"], str)
        assert doc["chunk_vector"] == "0.1,-0.2,0.33333"

    def test_boolean_serialized_as_int(self):
        """HA3 布尔字段使用 int (0/1) 而非 JSON boolean。"""
        chunk_active = self._make_chunk(is_active=True)
        chunk_inactive = self._make_chunk(is_active=False)
        assert chunk_active.to_ha3_doc()["is_active"] == 1
        assert chunk_inactive.to_ha3_doc()["is_active"] == 0

    def test_no_vector_field_when_embedding_is_none(self):
        """无向量时不应包含 chunk_vector 字段。"""
        chunk = self._make_chunk(embedding_vector=None)
        doc = chunk.to_ha3_doc()
        assert "chunk_vector" not in doc

    def test_null_metadata_defaults(self):
        """metadata 缺失时应使用安全默认值。"""
        chunk = self._make_chunk(
            owner_dept=None, category_l1=None,
            permission_level=None, section_title=None,
        )
        doc = chunk.to_ha3_doc()
        assert doc["owner_dept"] == ""
        assert doc["category_l1"] == ""
        assert doc["permission_level"] == "public"
        assert doc["section_title"] == ""


# ═══════════════════════════════════════════════════════════════
# Section B: HA3 Push 响应解析测试
# ═══════════════════════════════════════════════════════════════

class TestHA3PushResponseParsing:
    """验证 node_push_to_opensearch 中 HA3 路径的响应解析逻辑。"""

    def _make_chunks(self, count=3):
        return [
            Chunk(
                chunk_id=f"chunk_{i}", doc_id="doc1", version_no=1,
                chunk_index=i, chunk_type="text_chunk",
                chunk_text=f"text {i}", token_count=2,
                embedding_vector=[0.1] * 10,
            )
            for i in range(count)
        ]

    @patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
    @patch("opensearch_pipeline.pipeline_nodes._ensure_opensearch_index")
    def test_ha3_push_all_success(self, mock_ensure, mock_get_client):
        """HA3 全部成功：所有 chunk 标记为 INDEXED。"""
        from opensearch_pipeline.pipeline_nodes import node_push_to_opensearch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = {}

        client = MagicMock()
        client.pushDocuments = MagicMock(return_value=mock_resp)
        mock_get_client.return_value = client

        chunks = self._make_chunks(3)
        ctx = {
            "bulk_batches": [{
                "chunks": chunks,
                "payload": "", "payload_size": 100,
                "job_id": "JOB1", "oss_key": "test.jsonl",
            }],
            "simulate_opensearch": False,
            "opensearch_index": "test_idx",
        }

        node_push_to_opensearch(ctx)

        for c in chunks:
            assert c.index_status == "INDEXED"
            assert c.index_error_code is None

    @patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
    @patch("opensearch_pipeline.pipeline_nodes._ensure_opensearch_index")
    def test_ha3_push_per_doc_errors(self, mock_ensure, mock_get_client):
        """HA3 部分失败：per-document error 被正确解析。"""
        from opensearch_pipeline.pipeline_nodes import node_push_to_opensearch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.body = {
            "errors": [
                {"index": 1, "code": "FIELD_ERROR", "message": "vector dim mismatch"}
            ]
        }

        client = MagicMock()
        client.pushDocuments = MagicMock(return_value=mock_resp)
        mock_get_client.return_value = client

        chunks = self._make_chunks(3)
        ctx = {
            "bulk_batches": [{
                "chunks": chunks,
                "payload": "", "payload_size": 100,
                "job_id": "JOB2", "oss_key": "test.jsonl",
            }],
            "simulate_opensearch": False,
            "opensearch_index": "test_idx",
        }

        node_push_to_opensearch(ctx)

        assert chunks[0].index_status == "INDEXED"
        assert chunks[1].index_status == "FAILED"
        assert chunks[1].index_error_code == "FIELD_ERROR"
        assert "vector dim mismatch" in chunks[1].index_error_message
        assert chunks[2].index_status == "INDEXED"

    @patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
    @patch("opensearch_pipeline.pipeline_nodes._ensure_opensearch_index")
    def test_ha3_push_http_400_no_retry(self, mock_ensure, mock_get_client):
        """HA3 HTTP 400 (非瞬时错误)：不重试，标记失败。"""
        from opensearch_pipeline.pipeline_nodes import node_push_to_opensearch

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.body = "Bad Request: invalid schema"

        client = MagicMock()
        client.pushDocuments = MagicMock(return_value=mock_resp)
        mock_get_client.return_value = client

        chunks = self._make_chunks(2)
        ctx = {
            "bulk_batches": [{
                "chunks": chunks,
                "payload": "", "payload_size": 100,
                "job_id": "JOB3", "oss_key": "test.jsonl",
            }],
            "simulate_opensearch": False,
            "opensearch_index": "test_idx",
        }

        node_push_to_opensearch(ctx)

        for c in chunks:
            assert c.index_status == "FAILED"
            assert c.index_error_code == "400"

    @patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
    @patch("opensearch_pipeline.pipeline_nodes._ensure_opensearch_index")
    @patch("time.sleep")  # 避免真实等待
    def test_ha3_push_retry_on_429_then_success(self, mock_sleep, mock_ensure, mock_get_client):
        """HA3 HTTP 429 (限流)：触发重试，最终成功。"""
        from opensearch_pipeline.pipeline_nodes import node_push_to_opensearch

        resp_429 = MagicMock()
        resp_429.status_code = 429

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.body = {}

        client = MagicMock()
        client.pushDocuments = MagicMock(side_effect=[resp_429, resp_200])
        mock_get_client.return_value = client

        chunks = self._make_chunks(2)
        ctx = {
            "bulk_batches": [{
                "chunks": chunks,
                "payload": "", "payload_size": 100,
                "job_id": "JOB4", "oss_key": "test.jsonl",
            }],
            "simulate_opensearch": False,
            "opensearch_index": "test_idx",
        }

        node_push_to_opensearch(ctx)

        assert client.pushDocuments.call_count == 2
        mock_sleep.assert_called()
        for c in chunks:
            assert c.index_status == "INDEXED"

    @patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
    @patch("opensearch_pipeline.pipeline_nodes._ensure_opensearch_index")
    @patch("time.sleep")
    def test_ha3_push_retry_exhausted(self, mock_sleep, mock_ensure, mock_get_client):
        """HA3 所有重试耗尽：chunk 标记为 RETRY_EXHAUSTED。"""
        from opensearch_pipeline.pipeline_nodes import node_push_to_opensearch

        client = MagicMock()
        client.pushDocuments = MagicMock(side_effect=ConnectionError("Network unreachable"))
        mock_get_client.return_value = client

        chunks = self._make_chunks(1)
        ctx = {
            "bulk_batches": [{
                "chunks": chunks,
                "payload": "", "payload_size": 100,
                "job_id": "JOB5", "oss_key": "test.jsonl",
            }],
            "simulate_opensearch": False,
            "opensearch_index": "test_idx",
        }

        node_push_to_opensearch(ctx)

        assert chunks[0].index_status == "FAILED"
        assert chunks[0].index_error_code == "RETRY_EXHAUSTED"
        assert "Network unreachable" in chunks[0].index_error_message


# ═══════════════════════════════════════════════════════════════
# Section C: HA3 Delete 幂等判断测试
# ═══════════════════════════════════════════════════════════════

class TestHA3DeleteIdempotency:
    """验证 node_deactivate_old_chunks 中 HA3 删除的幂等判断逻辑。"""

    def _make_deactivate_ctx(self, chunks, existing_old_chunks, simulate_opensearch=False):
        return {
            "valid_chunks": chunks,
            "existing_opensearch_chunks": existing_old_chunks,
            "preempted_doc_versions": set((c.doc_id, c.version_no) for c in chunks),
            "simulate_db": True,
            "simulate_opensearch": simulate_opensearch,
            "dag3_no_work": False,
        }

    @patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
    def test_ha3_delete_not_found_is_idempotent(self, mock_get_client):
        """HA3 删除时返回 404 + 'not_found' 应视为成功（幂等）。"""
        from opensearch_pipeline.pipeline_nodes import node_deactivate_old_chunks

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.body = "not_found"
        mock_resp.text = "Document not_found"
        mock_resp.json = MagicMock(side_effect=Exception("no json"))

        client = MagicMock()
        client.pushDocuments = MagicMock(return_value=mock_resp)
        mock_get_client.return_value = client

        new_chunk = Chunk(
            chunk_id="c_new", doc_id="doc1", version_no=2,
            chunk_index=0, chunk_type="text_chunk",
            chunk_text="new text", token_count=2,
        )
        old_chunks = [
            {"chunk_id": "c_old_1", "doc_id": "doc1", "version_no": 1},
        ]

        ctx = self._make_deactivate_ctx([new_chunk], old_chunks)
        # 不应抛异常（幂等成功）
        node_deactivate_old_chunks(ctx)
        assert len(ctx["deactivated_chunks"]) == 1

    @patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
    def test_ha3_delete_json_documentnotfound_is_idempotent(self, mock_get_client):
        """HA3 删除时 JSON 返回 DocumentNotFound code 应视为成功。"""
        from opensearch_pipeline.pipeline_nodes import node_deactivate_old_chunks

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.body = {"code": "DocumentNotFound", "message": "doc not exists"}
        mock_resp.text = ""
        mock_resp.json = MagicMock(return_value={"code": "DocumentNotFound", "message": "doc not exists"})

        client = MagicMock()
        client.pushDocuments = MagicMock(return_value=mock_resp)
        mock_get_client.return_value = client

        new_chunk = Chunk(
            chunk_id="c_new", doc_id="doc1", version_no=2,
            chunk_index=0, chunk_type="text_chunk",
            chunk_text="new text", token_count=2,
        )
        old_chunks = [
            {"chunk_id": "c_old_1", "doc_id": "doc1", "version_no": 1},
        ]

        ctx = self._make_deactivate_ctx([new_chunk], old_chunks)
        node_deactivate_old_chunks(ctx)
        assert len(ctx["deactivated_chunks"]) == 1

    @patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
    def test_ha3_delete_real_error_raises(self, mock_get_client):
        """HA3 删除时 500 + 非幂等错误应该 raise RuntimeError。"""
        from opensearch_pipeline.pipeline_nodes import node_deactivate_old_chunks

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.body = "Internal Server Error"
        mock_resp.text = "Internal Server Error"
        mock_resp.json = MagicMock(side_effect=Exception("no json"))

        client = MagicMock()
        client.pushDocuments = MagicMock(return_value=mock_resp)
        mock_get_client.return_value = client

        new_chunk = Chunk(
            chunk_id="c_new", doc_id="doc1", version_no=2,
            chunk_index=0, chunk_type="text_chunk",
            chunk_text="new text", token_count=2,
        )
        old_chunks = [
            {"chunk_id": "c_old_1", "doc_id": "doc1", "version_no": 1},
        ]

        ctx = self._make_deactivate_ctx([new_chunk], old_chunks)
        # RuntimeError 可能被包装，检查任何 RuntimeError
        with pytest.raises(RuntimeError, match="deactivate old chunks"):
            node_deactivate_old_chunks(ctx)
