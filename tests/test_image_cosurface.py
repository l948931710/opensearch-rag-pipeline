# -*- coding: utf-8 -*-
"""
test_image_cosurface.py — 图片召回增强（lever A）单元测试

验证 retriever.cosurface_doc_images：
  - 把同文档最相关 image chunk **插入到首个正文 chunk 之后**（不是追加末尾，
    否则 <<IMG:N>> 提示会被 _format_context 的 max_context_chars 截断）
  - 每个文档只补一次、总量受 max_images 限制
  - 结果已含 image chunk 时短路返回（不打扰可视化查询）
  - 任意异常 fail-open 返回原 chunks
  - retrieve_and_enrich 仅在 cosurface_images=True 且全局开关开启时调用

HA3 / embedding 层全部 mock，无需真实服务。
"""

from unittest.mock import patch, MagicMock

from opensearch_pipeline import retriever


def _chunks():
    return [
        {"doc_id": "A", "chunk_index": 0, "chunk_type": "text_chunk", "chunk_text": "a0", "title": "DocA"},
        {"doc_id": "B", "chunk_index": 0, "chunk_type": "text_chunk", "chunk_text": "b0", "title": "DocB"},
        {"doc_id": "A", "chunk_index": 1, "chunk_type": "text_chunk", "chunk_text": "a1", "title": "DocA"},
    ]


_IMGS = [
    {"doc_id": "A", "chunk_index": 9, "chunk_type": "image", "source_image": "oss/a.png", "visual_summary": "imgA"},
    {"doc_id": "B", "chunk_index": 9, "chunk_type": "image", "source_image": "oss/b.png", "visual_summary": "imgB"},
]


@patch("opensearch_pipeline.retriever._parse_ha3_response")
@patch("opensearch_pipeline.retriever._get_ha3_client")
@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
def test_inserts_after_first_sibling(mock_emb, mock_client, mock_parse):
    mock_client.return_value = MagicMock()
    mock_parse.return_value = list(_IMGS)
    out = retriever.cosurface_doc_images("q", _chunks())
    seq = [(c["doc_id"], c["chunk_type"]) for c in out]
    # image inserted right after the FIRST text chunk of each doc; A's 2nd chunk gets none
    assert seq == [
        ("A", "text_chunk"), ("A", "image"),
        ("B", "text_chunk"), ("B", "image"),
        ("A", "text_chunk"),
    ]
    assert sum(1 for c in out if c["chunk_type"] == "image") == 2


@patch("opensearch_pipeline.retriever._parse_ha3_response")
@patch("opensearch_pipeline.retriever._get_ha3_client")
@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
def test_caps_max_images(mock_emb, mock_client, mock_parse):
    mock_client.return_value = MagicMock()
    mock_parse.return_value = list(_IMGS)
    out = retriever.cosurface_doc_images("q", _chunks(), max_images=1)
    assert sum(1 for c in out if c["chunk_type"] == "image") == 1


@patch("opensearch_pipeline.retriever._parse_ha3_response")
@patch("opensearch_pipeline.retriever._get_ha3_client")
@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
def test_skips_image_without_source(mock_emb, mock_client, mock_parse):
    mock_client.return_value = MagicMock()
    mock_parse.return_value = [{"doc_id": "A", "chunk_index": 9, "chunk_type": "image", "source_image": ""}]
    out = retriever.cosurface_doc_images("q", _chunks())
    assert all(c["chunk_type"] != "image" for c in out)  # empty source_image → not inserted


@patch("opensearch_pipeline.retriever.get_query_embedding")
def test_short_circuits_when_image_present(mock_emb):
    chunks = _chunks() + [{"doc_id": "A", "chunk_index": 5, "chunk_type": "image", "source_image": "x"}]
    out = retriever.cosurface_doc_images("q", chunks)
    assert out is chunks               # unchanged object
    mock_emb.assert_not_called()       # no HA3 query issued


@patch("opensearch_pipeline.retriever.get_query_embedding", side_effect=RuntimeError("boom"))
def test_fail_open_on_error(mock_emb):
    chunks = _chunks()
    out = retriever.cosurface_doc_images("q", chunks)
    assert out == chunks               # error swallowed, original returned


@patch("opensearch_pipeline.retriever.cosurface_doc_images", side_effect=lambda q, c, **k: c)
@patch("opensearch_pipeline.retriever.expand_step_context", side_effect=lambda c, q: c)
@patch("opensearch_pipeline.retriever.stitch_neighbor_chunks", side_effect=lambda c, window=1: c)
@patch("opensearch_pipeline.retriever.search_chunks")
def test_retrieve_and_enrich_opt_in_gating(mock_search, mock_stitch, mock_expand, mock_cosurf):
    mock_search.return_value = _chunks()
    # default (False) → cosurface NOT called
    retriever.retrieve_and_enrich("q")
    mock_cosurf.assert_not_called()
    # opt-in True → cosurface called
    retriever.retrieve_and_enrich("q", cosurface_images=True)
    assert mock_cosurf.called
