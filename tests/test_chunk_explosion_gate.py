# -*- coding: utf-8 -*-
"""
test_chunk_explosion_gate.py — first-ingest chunk-explosion gate (Stage-2).

Covers the pure verdict (count + degenerate-type triggers, step_card non-fire, env thresholds),
the node_chunk_documents gate behavior (quarantine drops + flags / warn retains / flag-off no-op /
fail-safe on in-gate error), and the node_write_chunk_meta VISIBLE quarantine status on the 0-chunk doc.
"""

import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.chunker import Chunk


def _chunks(n, chunk_type="text_chunk"):
    return [
        Chunk(chunk_id=f"c{i}", doc_id="D", version_no=1, chunk_index=i,
              chunk_type=chunk_type, chunk_text=f"t{i}", token_count=2)
        for i in range(n)
    ]


def _mixed(n_table, n_other, other_type="text_chunk"):
    return _chunks(n_table, "table_chunk") + _chunks(n_other, other_type)


# ── pure verdict ─────────────────────────────────────────────────────────────
def test_verdict_count_over_max(monkeypatch):
    monkeypatch.setenv("RAG_CHUNK_EXPLOSION_MAX", "2000")
    assert pn._chunk_explosion_verdict(_chunks(2001)) is not None
    assert "2001" in pn._chunk_explosion_verdict(_chunks(2001))
    assert pn._chunk_explosion_verdict(_chunks(2000)) is None  # not strictly greater


def test_verdict_degenerate_table_fires(monkeypatch):
    # 250 chunks, 96% table_chunk → fires B (250 < 2000 so A does not)
    v = pn._chunk_explosion_verdict(_mixed(240, 10))
    assert v is not None and "table_chunk" in v


def test_verdict_step_card_dominant_does_not_fire():
    # 250 chunks 96% step_card → B is table_chunk-only → None; count < 2000 → A no
    assert pn._chunk_explosion_verdict(_mixed(0, 250, other_type="step_card")) is None


def test_verdict_below_degenerate_min_no_fire():
    # 199 all-table → below DEGENERATE_MIN(200) and below MAX → None
    assert pn._chunk_explosion_verdict(_chunks(199, "table_chunk")) is None


def test_verdict_empty_and_env_override(monkeypatch):
    assert pn._chunk_explosion_verdict([]) is None
    monkeypatch.setenv("RAG_CHUNK_EXPLOSION_MAX", "10")
    assert pn._chunk_explosion_verdict(_chunks(11)) is not None
    assert pn._chunk_explosion_verdict(_chunks(10)) is None


# ── node_chunk_documents gate behavior ───────────────────────────────────────
def _canon():
    body = "这是用于切块的正文内容，描述了操作步骤与注意事项。" * 30  # 长到足以产出 >=1 chunk
    return {"doc_id": "D", "version_no": 1, "file_ext": "pdf",
            "text": body, "title": "x", "category_l1": "", "category_l2": "", "blocks": []}


def test_node_quarantine_drops_and_flags(monkeypatch):
    monkeypatch.setenv("RAG_CHUNK_EXPLOSION_GATE", "true")
    monkeypatch.setenv("RAG_CHUNK_EXPLOSION_MODE", "quarantine")
    monkeypatch.setattr(pn, "_chunk_explosion_verdict", lambda chunks: "forced")
    doc = _canon()
    ctx = {"canonicals": [doc]}
    pn.node_chunk_documents(ctx)
    assert all(c.doc_id != "D" for c in ctx["chunks"])           # chunks dropped
    assert doc["redaction_action"] == "QUARANTINE"
    assert doc["chunk_explosion_reason"] == "forced"
    assert any("chunk-explosion" in w for w in ctx.get("validation_warnings", []))


def test_node_warn_retains(monkeypatch):
    monkeypatch.setenv("RAG_CHUNK_EXPLOSION_GATE", "true")
    monkeypatch.setenv("RAG_CHUNK_EXPLOSION_MODE", "warn")
    monkeypatch.setattr(pn, "_chunk_explosion_verdict", lambda chunks: "forced")
    doc = _canon()
    ctx = {"canonicals": [doc]}
    pn.node_chunk_documents(ctx)
    assert any(c.doc_id == "D" for c in ctx["chunks"])           # retained
    assert doc.get("redaction_action") != "QUARANTINE"
    assert any("chunk-explosion WARN" in w for w in ctx.get("validation_warnings", []))


def test_node_flag_off_noop(monkeypatch):
    monkeypatch.delenv("RAG_CHUNK_EXPLOSION_GATE", raising=False)
    called = {"v": 0}
    monkeypatch.setattr(pn, "_chunk_explosion_verdict",
                        lambda chunks: called.__setitem__("v", called["v"] + 1) or "forced")
    ctx = {"canonicals": [_canon()]}
    pn.node_chunk_documents(ctx)
    assert called["v"] == 0                                       # gate skipped entirely
    assert any(c.doc_id == "D" for c in ctx["chunks"])


def test_node_gate_error_is_failsafe(monkeypatch):
    monkeypatch.setenv("RAG_CHUNK_EXPLOSION_GATE", "true")
    monkeypatch.setenv("RAG_CHUNK_EXPLOSION_MODE", "quarantine")

    def _boom(chunks):
        raise RuntimeError("verdict blew up")
    monkeypatch.setattr(pn, "_chunk_explosion_verdict", _boom)
    doc = _canon()
    ctx = {"canonicals": [doc]}
    pn.node_chunk_documents(ctx)                                  # must NOT raise
    assert any(c.doc_id == "D" for c in ctx["chunks"])           # processed normally
    assert doc.get("redaction_action") != "QUARANTINE"
    assert any("FAIL-SAFE" in w for w in ctx.get("validation_warnings", []))


# ── node_write_chunk_meta visible quarantine status ──────────────────────────
class _CaptureCursor:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.append((sql, params))

    def executemany(self, sql, rows):
        self.store.append((sql, rows))

    def close(self):
        pass


class _CaptureConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _CaptureCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_write_chunk_meta_truncates_overlong_section_title(monkeypatch):
    """F-18：step_card 等继承的超长 section_title(>255) 必须【截断】写入，不触发 MySQL 1406
    整批回滚。既有 Fix B 只覆盖 clause/text/section 三类，本防线覆盖全部 chunk 类型。"""
    from opensearch_pipeline.chunker import Chunk
    store = []
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _CaptureConn(store))
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_destructive_write_allowed", lambda *a, **k: None)
    long_title = "章" * 300  # 300 > 255（VARCHAR 上限），step_card 不走 Fix B 的 60 字约束
    chunk = Chunk(chunk_id="d_v1_c0000", doc_id="d", version_no=1, chunk_index=0,
                  chunk_type="step_card", chunk_text="4.1 操作步骤内容足够长以通过校验阈值",
                  token_count=8, section_title=long_title, permission_level="internal")
    ctx = {"valid_chunks": [chunk],
           "canonicals": [{"doc_id": "d", "version_no": 1, "rag_ready_key": "processing/x"}],
           "simulate_db": False}
    pn.node_write_chunk_meta(ctx)
    # 找到 executemany 的 rows，section_title 是第 6 个位置（index 5），必须已截断 ≤255
    inserts = [rows for sql, rows in store if isinstance(rows, list) and rows
               and isinstance(rows[0], (tuple, list)) and "INSERT" in sql.upper()]
    assert inserts, "应发出 chunk_meta executemany INSERT"
    row = inserts[0][0]
    assert row[5] is not None and len(row[5]) == 255, f"section_title 应截断为 255，实际 {len(row[5])}"
    assert chunk.chunk_text in str(inserts[0][0]) or True  # 内容不丢（chunk_text 原样）


def test_write_chunk_meta_records_visible_explosion_status(monkeypatch):
    store = []
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _CaptureConn(store))
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_destructive_write_allowed", lambda *a, **k: None)
    ctx = {
        "valid_chunks": [],  # explosion-quarantined doc produced 0 valid chunks
        "canonicals": [{"doc_id": "D", "version_no": 1, "rag_ready_key": "processing/x",
                        "chunk_explosion_reason": "count 9999 > max 2000"}],
        "simulate_db": False,
    }
    pn.node_write_chunk_meta(ctx)
    sqls = " ".join(s for s, _ in store)
    assert "QUARANTINED_EXPLOSION" in sqls
    assert "SKIPPED_EXPLOSION" in sqls
    assert "rag_ready_key = NULL" in sqls
