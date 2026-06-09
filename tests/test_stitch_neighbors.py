# -*- coding: utf-8 -*-
"""
test_stitch_neighbors.py — stitch_neighbor_chunks 批量化（消除 N+1）回归

验证批量单次查询与原逐 chunk 查询行为一致：拼接文本、邻居计数、输出顺序、
重复中心丢弃、pass-through（无 doc_id / step·proc·visual），并断言只有 1 次 RDS 往返。
"""

from unittest.mock import MagicMock, patch

from opensearch_pipeline import retriever


class _FakeCursor:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls

    def execute(self, sql, params=None):
        self._calls.append((sql, params))

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


def _fake_conn(rows, calls):
    conn = MagicMock()
    conn.cursor.return_value = _FakeCursor(rows, calls)
    conn.close.return_value = None
    return conn


def test_batched_stitch_matches_and_single_round_trip():
    chunks = [
        {"doc_id": "A", "chunk_index": 1, "chunk_type": "text_chunk", "chunk_text": "a1-orig", "score": 9.0},
        {"doc_id": "B", "chunk_index": 5, "chunk_type": "text_chunk", "chunk_text": "b5-orig", "score": 8.0},
        {"doc_id": "A", "chunk_index": 1, "chunk_type": "text_chunk", "chunk_text": "dup"},      # 重复中心 → 丢弃
        {"doc_id": "A", "chunk_index": 10, "chunk_type": "step_card", "chunk_text": "stepcard"},  # pass-through
        {"chunk_index": 0, "chunk_type": "text_chunk", "chunk_text": "no-doc"},                   # 无 doc_id → pass-through
    ]
    db_rows = [
        {"doc_id": "A", "chunk_index": 0, "chunk_text": "a0", "section_title": ""},
        {"doc_id": "A", "chunk_index": 1, "chunk_text": "a1", "section_title": ""},
        {"doc_id": "A", "chunk_index": 2, "chunk_text": "a2", "section_title": ""},
        {"doc_id": "B", "chunk_index": 4, "chunk_text": "b4", "section_title": ""},
        {"doc_id": "B", "chunk_index": 5, "chunk_text": "b5", "section_title": ""},
        {"doc_id": "B", "chunk_index": 6, "chunk_text": "b6", "section_title": ""},
    ]
    calls = []
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=_fake_conn(db_rows, calls)):
        out = retriever.stitch_neighbor_chunks(chunks, window=1)

    # 单次 RDS 往返
    assert len(calls) == 1, f"expected 1 query, got {len(calls)}"

    # 输出顺序：A/1(拼接) → B/5(拼接) → step_card(原样) → no-doc(原样)；重复 A/1 被丢弃
    assert len(out) == 4
    assert out[0]["chunk_text"] == "a0\na1\na2" and out[0]["_neighbor_count"] == 3
    assert out[0]["score"] == 9.0 and out[0]["_stitched"] is True   # 保留原 score/metadata
    assert out[1]["chunk_text"] == "b4\nb5\nb6" and out[1]["_neighbor_count"] == 3
    assert out[2]["chunk_type"] == "step_card" and out[2]["chunk_text"] == "stepcard"  # pass-through 原样
    assert "_stitched" not in out[2]
    assert out[3]["chunk_text"] == "no-doc" and "_stitched" not in out[3]


def test_no_eligible_chunks_skips_db():
    chunks = [
        {"doc_id": "A", "chunk_index": 1, "chunk_type": "step_card", "chunk_text": "s"},
        {"chunk_index": 0, "chunk_type": "text_chunk", "chunk_text": "no-doc"},
    ]
    calls = []
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=_fake_conn([], calls)):
        out = retriever.stitch_neighbor_chunks(chunks, window=1)
    assert len(calls) == 0  # 全 pass-through → 不连库
    assert out == chunks


def test_window_zero_returns_unchanged():
    chunks = [{"doc_id": "A", "chunk_index": 1, "chunk_type": "text_chunk", "chunk_text": "x"}]
    assert retriever.stitch_neighbor_chunks(chunks, window=0) is chunks


def test_missing_neighbors_falls_back_to_original_text():
    chunks = [{"doc_id": "Z", "chunk_index": 3, "chunk_type": "text_chunk", "chunk_text": "orig"}]
    calls = []
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=_fake_conn([], calls)):
        out = retriever.stitch_neighbor_chunks(chunks, window=1)
    assert len(calls) == 1
    assert out[0]["chunk_text"] == "orig" and out[0]["_neighbor_count"] == 0
