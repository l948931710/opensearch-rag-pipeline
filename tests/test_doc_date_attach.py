# -*- coding: utf-8 -*-
"""P2-31（盲区审计）serving 侧文档日期：retriever._attach_doc_dates + 上下文头/来源透传。

HA3 可索引日期字段需整表重建（user-gated）；这半边是 RDS 现查附 `doc_date`，
fail-open——查不到/连接炸绝不影响回答主链路。
"""
import pytest

from opensearch_pipeline import retriever as rt
from opensearch_pipeline.llm_generator import _chunk_header, _extract_sources


class _Cur:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur

    def close(self):
        pass


@pytest.fixture
def real_db_mode(monkeypatch):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config(), "simulate_db", False)


def test_attach_doc_dates_joins_current_version(real_db_mode, monkeypatch):
    conn = _Conn([("D1", "2025-03-14"), ("D2", "2023-06-01")])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    chunks = [{"doc_id": "D1"}, {"doc_id": "D2"}, {"doc_id": "D3"}, {"doc_id": "D1"}]
    rt._attach_doc_dates(chunks)
    assert chunks[0]["doc_date"] == "2025-03-14" and chunks[3]["doc_date"] == "2025-03-14"
    assert chunks[1]["doc_date"] == "2023-06-01"
    assert "doc_date" not in chunks[2]                      # 查不到不硬造
    assert "current_version_no = dv.version_no" in conn.cur.sql   # 当前版本的落库日


def test_attach_doc_dates_fail_open(real_db_mode, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("rds down")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    chunks = [{"doc_id": "D1", "chunk_text": "x"}]
    rt._attach_doc_dates(chunks)                            # 不抛即过
    assert "doc_date" not in chunks[0]


def test_doc_date_threads_to_header_and_sources():
    chunk = {"doc_id": "D1", "title": "注塑 SOP", "chunk_text": "x", "score": 8.0,
             "chunk_type": "text_chunk", "doc_date": "2025-03-14"}
    assert "(文档日期: 2025-03-14)" in _chunk_header(0, chunk, pure_text=True)
    src = _extract_sources([chunk])[0]
    assert src["doc_date"] == "2025-03-14"
    # 无日期 → 头部不出现空标签、来源为字符串空
    chunk2 = {"doc_id": "D2", "title": "t", "chunk_text": "x", "score": 8.0,
              "chunk_type": "text_chunk"}
    assert "文档日期" not in _chunk_header(0, chunk2, pure_text=True)
    assert _extract_sources([chunk2])[0]["doc_date"] == ""
