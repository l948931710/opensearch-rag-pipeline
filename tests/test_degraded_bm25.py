# -*- coding: utf-8 -*-
"""test_degraded_bm25.py — P2-4：查询嵌入中断的纯 BM25 降级路径。

DashScope 嵌入失败（超时/HTTP 错/无 key）时，检索不再整链硬失败：
  1. search_chunks 用零向量占位 + knn 权重 0 / text 权重 1.0（纯 BM25 形态）继续查 HA3；
  2. 权限过滤（ACL 安全边界）在降级路径逐字节保留——绝不因降级放宽；
  3. 结果打 degraded_retrieval 标 + 分级失效处理（score→0，原分保真挪 degraded_raw_score）；
  4. RAG_DEGRADED_BM25_ENABLE=false 恢复历史 raise 行为；
  5. retrieve_and_enrich 端到端不炸，且降级时跳过 cosurface 补图（零向量挑不了图）。

复用 test_rrf_hybrid_search 的 HA3 SDK mock 注入 + config 构造（同一 mock 语义）。
"""

import sys
import types

import pytest
from unittest.mock import MagicMock, patch

from tests.test_rrf_hybrid_search import (  # noqa: F401 — 导入即完成 SDK mock 注入
    _ensure_ha3_mock_modules,
    _make_config,
    _make_ha3_response,
)

_ensure_ha3_mock_modules()

from opensearch_pipeline import retriever  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_degraded_log_throttle():
    """每测重置降级告警限流状态（免跨测试互相吞 ERROR 判定）。"""
    retriever._degraded_log_state["ts"] = 0.0
    yield


def _boom(*a, **k):
    raise RuntimeError("simulated DashScope embedding outage")


# ═══════════════════════════════════════════════════════════════
# A. search_chunks：降级请求形态 + 结果打标
# ═══════════════════════════════════════════════════════════════

class TestDegradedSearchChunks:

    @patch("opensearch_pipeline.retriever.get_query_embedding", side_effect=_boom)
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_degraded_pure_bm25_shape_and_marking(self, mock_client_fn, mock_config, _emb):
        """嵌入失败（flag 默认开）→ 不 raise；knn 权重 0 / text 权重 1.0；结果带
        degraded_retrieval=True、score=0.0、原分保真在 degraded_raw_score。"""
        mock_config.return_value = _make_config(hybrid_fusion="weighted")
        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        results = retriever.search_chunks("怎么开发票", top_k=5)

        assert client.search.called and not client.query.called
        req = client.search.call_args[0][0]
        assert req.knn.weight == 0.0
        assert req.text.weight == 1.0
        # 零向量占位（HA3 payload 需要 vector 字段）
        assert all(v == 0.0 for v in req.knn.vector)
        assert results, "降级路径必须照常返回文本命中"
        for r in results:
            assert r["degraded_retrieval"] is True
            assert r["score"] == 0.0                       # 分级失效：恒判「低」
            assert r["degraded_raw_score"] == 0.032        # 引擎原始分保真，不伪造

    @patch("opensearch_pipeline.retriever.get_query_embedding", side_effect=_boom)
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_degraded_preserves_permission_filter(self, mock_client_fn, mock_config, _emb):
        """ACL 边界：降级路径的 knn/text 两路 filter 与正常路径同一实现产物，绝不放宽。"""
        mock_config.return_value = _make_config(hybrid_fusion="weighted")
        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        retriever.search_chunks("发票", top_k=5, user_dept="finance")

        expected = retriever._build_permission_filter("finance")
        req = client.search.call_args[0][0]
        assert req.knn.filter == expected
        assert req.text.filter == expected
        assert 'permission_level="public"' in expected
        assert 'owner_dept="finance"' in expected

    @patch("opensearch_pipeline.retriever.get_query_embedding", side_effect=_boom)
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_flag_off_keeps_raise_behavior(self, mock_client_fn, mock_config, _emb, monkeypatch):
        """RAG_DEGRADED_BM25_ENABLE=false → 保持历史行为：嵌入异常原样上抛。"""
        monkeypatch.setenv("RAG_DEGRADED_BM25_ENABLE", "false")
        mock_config.return_value = _make_config()
        mock_client_fn.return_value = MagicMock()

        with pytest.raises(RuntimeError, match="embedding outage"):
            retriever.search_chunks("发票", top_k=5)
        assert not mock_client_fn.return_value.search.called

    @patch("opensearch_pipeline.retriever.get_query_embedding", side_effect=_boom)
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_vector_only_config_degrades_via_text_path(self, mock_client_fn, mock_config, _emb):
        """enable_hybrid=False（纯向量配置）+ 嵌入失败 → 仍走 hybrid SearchRequest 的
        text 路（纯向量 QueryRequest 没有 BM25 可用），knn 0 / text 1.0。"""
        mock_config.return_value = _make_config(enable_hybrid=False)
        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        results = retriever.search_chunks("发票", top_k=5)

        assert client.search.called and not client.query.called
        req = client.search.call_args[0][0]
        assert req.knn.weight == 0.0 and req.text.weight == 1.0
        assert results and all(r["degraded_retrieval"] for r in results)

    @patch("opensearch_pipeline.retriever.get_query_embedding",
           return_value=([0.1] * 1024, [1], [0.5]))
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_normal_path_untouched(self, mock_client_fn, mock_config, _emb):
        """回归护栏：嵌入正常 → 权重仍是配置值（0.7/0.3），结果不带 degraded 标。"""
        mock_config.return_value = _make_config(hybrid_fusion="weighted")
        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        results = retriever.search_chunks("发票", top_k=5)

        req = client.search.call_args[0][0]
        assert req.knn.weight == 0.7 and req.text.weight == 0.3
        assert results and all("degraded_retrieval" not in r for r in results)
        assert all("degraded_raw_score" not in r for r in results)


# ═══════════════════════════════════════════════════════════════
# B. 本地 OpenSearch 回退路径（零向量不进 kNN，ACL 过滤保留）
# ═══════════════════════════════════════════════════════════════

def test_opensearch_fallback_degraded_drops_knn_keeps_acl(monkeypatch):
    captured = {}

    class _FakeOS:
        def __init__(self, **kw):
            pass

        def search(self, index=None, body=None):
            captured["body"] = body
            return {"hits": {"hits": [{"_id": "c1", "_score": 3.2, "_source": {
                "chunk_id": "c1", "doc_id": "D1", "chunk_text": "文" * 300,
                "section_title": "s", "permission_level": "public"}}]}}

    fake_mod = types.ModuleType("opensearchpy")
    fake_mod.OpenSearch = _FakeOS
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_mod)

    mock_cfg = _make_config()
    mock_cfg.opensearch.host = "localhost"
    with patch("opensearch_pipeline.retriever.get_config", return_value=mock_cfg):
        rows = retriever._search_chunks_opensearch(
            "问题", [0.0] * 8, 5, user_dept="finance", degraded=True)

    should = captured["body"]["query"]["bool"]["should"]
    assert len(should) == 1 and "match" in should[0], "降级必须去掉 kNN 子句（零向量会炸引擎）"
    # ACL filter 原样保留
    flt = captured["body"]["query"]["bool"]["filter"][0]["bool"]["should"]
    assert {"term": {"permission_level": "public"}} in flt
    assert rows and rows[0]["degraded_retrieval"] is True and rows[0]["score"] == 0.0
    assert rows[0]["degraded_raw_score"] == 3.2


# ═══════════════════════════════════════════════════════════════
# C. retrieve_and_enrich 端到端：不炸 + 降级嵌入下传 + cosurface 跳过
# ═══════════════════════════════════════════════════════════════

@patch("opensearch_pipeline.retriever.get_query_embedding", side_effect=_boom)
@patch("opensearch_pipeline.retriever.cosurface_doc_images")
@patch("opensearch_pipeline.retriever.expand_step_context", side_effect=lambda c, q: c)
@patch("opensearch_pipeline.retriever.stitch_neighbor_chunks", side_effect=lambda c, window=1: c)
@patch("opensearch_pipeline.retriever.search_chunks")
def test_retrieve_and_enrich_degrades_end_to_end(
        mock_search, mock_stitch, mock_expand, mock_cosurf, _emb):
    """嵌入中断 → retrieve_and_enrich 照常返回；下传的 query_embedding 带 degraded 标；
    降级时跳过 cosurface 补图（零向量无法按相关度挑图）。"""
    mock_search.return_value = [{
        "doc_id": "D1", "chunk_index": 0, "chunk_text": "文" * 300, "score": 0.0,
        "degraded_retrieval": True, "degraded_raw_score": 4.1,
    }]

    out = retriever.retrieve_and_enrich("怎么开发票", cosurface_images=True)

    assert out and out[0]["degraded_retrieval"] is True
    emb_arg = mock_search.call_args.kwargs.get("query_embedding")
    assert emb_arg is not None and getattr(emb_arg, "degraded", False) is True
    dense, sparse_idx, sparse_val = emb_arg      # 三元组解构兼容
    assert all(v == 0.0 for v in dense) and sparse_idx == [] and sparse_val == []
    mock_cosurf.assert_not_called()


def test_degraded_wrapper_flag_semantics(monkeypatch):
    """封装函数本身：flag 默认开 → 降级三元组；显式关闭 → 原样上抛。"""
    monkeypatch.setattr(retriever, "get_query_embedding", _boom)
    emb = retriever._get_query_embedding_or_degraded("q")
    assert getattr(emb, "degraded", False) is True and len(emb) == 3

    monkeypatch.setenv("RAG_DEGRADED_BM25_ENABLE", "false")
    with pytest.raises(RuntimeError):
        retriever._get_query_embedding_or_degraded("q")
