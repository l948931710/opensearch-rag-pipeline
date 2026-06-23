# -*- coding: utf-8 -*-
"""
test_ha3_verify_repush.py — DAG-3 节点 04b（node_verify_and_repush）单测。

无需阿里云实例：通过 mock HA3 client（query/push_documents）+ mock RDS conn 验证：
推送后 HA3 物理存在性校验（权威 point-read，PK 相符）→ 有界补推 → 三终态
（all-present / DROP / UNKNOWN）→ 失败写回 FAILED（分类、rowcount 闭环、阻断 node 05）。
"""

import re
import sys
import types

import pytest

import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.chunker import Chunk


# ── HA3 SDK mock（补齐 QueryRequest；与 test_ha3_engine 的注入兼容/互不覆盖）──────────
def _ensure_ha3_mock():
    name = "alibabacloud_ha3engine_vector"
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
        sys.modules[name + ".models"] = types.ModuleType(name + ".models")
    models = sys.modules[name + ".models"]
    if not hasattr(models, "PushDocumentsRequest"):
        class PushDocumentsRequest:
            def __init__(self, body=None):
                self.body = body
        models.PushDocumentsRequest = PushDocumentsRequest
    if not hasattr(models, "QueryRequest"):
        class QueryRequest:
            def __init__(self, table_name=None, vector=None, top_k=None,
                         include_vector=None, output_fields=None, filter=None):
                self.table_name = table_name
                self.vector = vector
                self.top_k = top_k
                self.include_vector = include_vector
                self.output_fields = output_fields
                self.filter = filter
        models.QueryRequest = QueryRequest


_ensure_ha3_mock()


def _resp(items):
    return types.SimpleNamespace(body={"result": items})


# ── Fakes ────────────────────────────────────────────────────────────────────
class FakeHA3:
    """可配置的 HA3 client。point-read(filter id=<pk>) + range-enum(id>=lo AND id<hi) + push。"""

    def __init__(self, present=(), never_heal=(), point_raise=(), wrong_id=(),
                 lag_first=(), enumerate_raise=False, stale=(), stale_heals=True, texts=None):
        self.present = set(present)            # 当前"真在" HA3 的 pk
        self.never_heal = set(never_heal)      # 补推也不会进 HA3（持久 DROP）
        self.point_raise = set(point_raise)    # 这些 pk 的 point-read 抛异常（UNKNOWN）
        self.wrong_id = set(wrong_id)          # 返回行 id 与目标不符（PK 不匹配 → MISSING）
        self.lag_first = set(lag_first)        # 在 present 但首次 point-read 返回空（最终一致性滞后）
        self.enumerate_raise = enumerate_raise
        self.stale = set(stale)               # present 但 chunk_text_store 陈旧（≠ 内存 t{pk}）→ drift
        self.stale_heals = stale_heals        # 补推后陈旧内容是否变一致
        self.texts = dict(texts or {})        # 显式覆盖某 pk 返回的 chunk_text_store
        self.push_calls = []                   # list[list[int]]
        self.point_reads = []                  # list[int]
        self.range_reads = []                  # list[(lo, hi)]
        self.query_vector_lens = []
        self._reads = {}
        self._repushed = set()

    def _text_for(self, pk):
        if pk in self.texts:
            return self.texts[pk]
        if pk in self.stale and not (self.stale_heals and pk in self._repushed):
            return "STALE_CONTENT"
        return f"t{pk}"                        # matches _mk_chunk's chunk_text → no drift

    def query(self, req):
        self.query_vector_lens.append(len(req.vector))
        m = re.fullmatch(r"\s*\w+=(\d+)\s*", req.filter)
        if m:
            pk = int(m.group(1))
            self.point_reads.append(pk)
            if pk in self.point_raise:
                raise RuntimeError("simulated point-read failure")
            self._reads[pk] = self._reads.get(pk, 0) + 1
            lagging = pk in self.lag_first and self._reads[pk] == 1
            if pk in self.present and not lagging:
                rid = (pk + 100000) if pk in self.wrong_id else pk
                return _resp([{"id": rid, "fields": {"chunk_id": f"c{pk}", "doc_id": "doc1",
                                                     "chunk_text_store": self._text_for(pk)}}])
            return _resp([])
        mr = re.search(r"id>=(\d+) AND id<(\d+)", req.filter)
        if self.enumerate_raise:
            raise RuntimeError("simulated enumerate failure")
        lo, hi = (int(mr.group(1)), int(mr.group(2))) if mr else (0, 10 ** 18)
        self.range_reads.append((lo, hi))
        items = [{"id": pk, "fields": {"chunk_id": f"c{pk}", "doc_id": "doc1"}}
                 for pk in self.present if lo <= pk < hi]
        return _resp(items)

    def push_documents(self, table, pk_field, request):
        pks = [int(d["fields"][pk_field]) for d in request.body]
        self.push_calls.append(pks)
        for pk in pks:
            self._repushed.add(pk)         # re-push heals stale content (if stale_heals)
            if pk not in self.never_heal:
                self.present.add(pk)
        return types.SimpleNamespace(status_code=200, body={})


class FakeCursor:
    def __init__(self, rowcount_override=None):
        self.executed = []
        self.rowcount = 0
        self._override = rowcount_override

    def execute(self, sql, params=None):
        self.executed.append((sql, list(params) if params else []))
        # 真实 UPDATE: params = [code, msg, id1, id2, ...] → 改动行 = len(ids)
        self.rowcount = self._override if self._override is not None else (
            max(0, len(params) - 2) if params else 0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rowcount_override=None):
        self.cur = FakeCursor(rowcount_override=rowcount_override)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _mk_chunk(rds_id, idx=0, doc_id="doc1", version_no=2, status="INDEXED"):
    return Chunk(
        chunk_id=f"c{rds_id}", doc_id=doc_id, version_no=version_no,
        chunk_index=idx, chunk_type="text_chunk", chunk_text=f"t{rds_id}",
        token_count=2, embedding_vector=[0.1] * 4, rds_id=rds_id, index_status=status,
    )


def _setup(monkeypatch, client, chunks, conn=None, **env):
    """打补丁 + 返回 ctx（不调用 node，便于 raise 用例事后查 ctx）。"""
    monkeypatch.setenv("RAG_STAGE3_PARITY_VERIFY", "true")
    monkeypatch.setenv("RAG_STAGE3_PARITY_SETTLE_SEC", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: client)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: conn if conn is not None else FakeConn())
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_destructive_write_allowed", lambda *a, **k: None)
    monkeypatch.setattr(pn.time, "sleep", lambda *a, **k: None)
    return {"bulk_batches": [{"chunks": chunks, "job_id": "J", "oss_key": ""}],
            "simulate_opensearch": False}


# ── 1. simulate / MOCK no-op ─────────────────────────────────────────────────
def test_simulate_is_noop(monkeypatch):
    monkeypatch.setenv("RAG_STAGE3_PARITY_VERIFY", "true")
    called = {"client": 0}
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: called.__setitem__("client", called["client"] + 1) or "X")
    ctx = {"bulk_batches": [{"chunks": [_mk_chunk(1)]}], "simulate_opensearch": True}
    pn.node_verify_and_repush(ctx)            # 不应抛异常
    assert called["client"] == 0              # simulate 早返回，根本没取 client


def test_mock_client_is_noop(monkeypatch):
    monkeypatch.setenv("RAG_STAGE3_PARITY_VERIFY", "true")
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: "MOCK_HA3_CLIENT")
    ctx = {"bulk_batches": [{"chunks": [_mk_chunk(1)]}], "simulate_opensearch": False}
    pn.node_verify_and_repush(ctx)            # MOCK → no-op，不抛异常


# ── 2. flag OFF ──────────────────────────────────────────────────────────────
def test_flag_off_returns_even_in_real_mode(monkeypatch):
    monkeypatch.delenv("RAG_STAGE3_PARITY_VERIFY", raising=False)
    client = FakeHA3(present={1})
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda ctx=None: client)
    ctx = {"bulk_batches": [{"chunks": [_mk_chunk(1)]}], "simulate_opensearch": False}
    pn.node_verify_and_repush(ctx)
    assert client.point_reads == [] and client.push_calls == []


# ── 3. all present ───────────────────────────────────────────────────────────
def test_all_present_no_repush_no_raise(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11), _mk_chunk(12)]
    client = FakeHA3(present={10, 11, 12})
    ctx = _setup(monkeypatch, client, chunks)
    pn.node_verify_and_repush(ctx)
    assert sorted(client.point_reads) == [10, 11, 12]
    assert client.push_calls == []


# ── 4. N missing → healed + in-memory restored ───────────────────────────────
def test_missing_then_healed(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11), _mk_chunk(12)]
    client = FakeHA3(present={10, 12})          # 11 dropped
    ctx = _setup(monkeypatch, client, chunks)
    pn.node_verify_and_repush(ctx)              # 不抛异常
    assert client.push_calls == [[11]]          # 只补推丢失的 11
    healed = chunks[1]
    assert healed.rds_id == 11
    assert healed.index_status == "INDEXED"
    assert healed.index_error_code is None


# ── 5. confirmed-DROP stays missing → raise + RDS committed ───────────────────
def test_persistent_drop_raises_and_persists(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10}, never_heal={11})
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=2)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    assert client.push_calls == [[11], [11]]    # 有界补推 2 次
    assert chunks[1].index_status == "FAILED"
    assert chunks[1].index_error_code == "PARITY_DROP"
    assert conn.committed and not conn.rolled_back
    upd = [e for e in conn.cur.executed if "UPDATE chunk_meta" in e[0]]
    assert len(upd) == 1 and upd[0][1][0] == "PARITY_DROP"
    assert ("doc1", 2) in ctx["failed_doc_versions"]


# ── 6. UNKNOWN (point-read raises) → raise, distinct code, NO re-push ─────────
def test_unknown_blocks_without_repush(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, point_raise={11})
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=2)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    assert client.push_calls == []              # UNKNOWN 不补推
    assert chunks[1].index_status == "FAILED"
    assert chunks[1].index_error_code == "PARITY_UNKNOWN"
    assert conn.committed
    assert ("doc1", 2) in ctx["failed_doc_versions"]


# ── 7. enumerate raises → degrade to full point-read (NOT fail-open) ──────────
def test_enumerate_failure_degrades_to_pointread(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11), _mk_chunk(12)]
    client = FakeHA3(present={10, 11, 12}, enumerate_raise=True)
    # POINTREAD_ALL_MAX=0 → 走大批 enum 路径；enum 抛 → 降级全量 point-read
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_POINTREAD_ALL_MAX=0)
    pn.node_verify_and_repush(ctx)              # 全 present → 不抛异常
    assert sorted(client.point_reads) == [10, 11, 12]   # 确实降级为逐个 point-read


# ── 8. point-read PK-mismatch → treated as MISSING ───────────────────────────
def test_pk_mismatch_treated_as_missing(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    # 11 "在" HA3 但返回行 id 不符 → 必须判为 MISSING；max_retries=0 直接 DROP
    client = FakeHA3(present={10, 11}, wrong_id={11})
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=0)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    assert client.push_calls == []              # max_retries=0
    assert chunks[1].index_error_code == "PARITY_DROP"


# ── 9. eventual-consistency: lag on first read, present on re-confirm ─────────
def test_eventual_consistency_heals_without_persistent(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    # 11 真在 HA3，但首次 point-read 返回空（滞后）→ 误判 missing → 补推 → 复确认 present
    client = FakeHA3(present={10, 11}, lag_first={11})
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_MAX_RETRIES=2)
    pn.node_verify_and_repush(ctx)              # 不抛异常（最终一致，非真丢失）
    assert chunks[1].index_status == "INDEXED"
    assert client.push_calls == [[11]]          # 只补推 1 次即愈合


# ── 10. small batch → no id-range enumerate ──────────────────────────────────
def test_small_batch_skips_enumerate(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11})
    called = {"enum": 0}
    import opensearch_pipeline.ha3_reconcile as rc
    monkeypatch.setattr(rc, "_enumerate_ha3_pks",
                        lambda *a, **k: called.__setitem__("enum", called["enum"] + 1) or {})
    ctx = _setup(monkeypatch, client, chunks)   # default POINTREAD_ALL_MAX=200 ≥ 2
    pn.node_verify_and_repush(ctx)
    assert called["enum"] == 0
    assert client.range_reads == []             # 没有任何区间扫描，只有 point-read


# ── 11. vector dimension read from config (not hardcoded 1024) ───────────────
def test_pointread_vector_dim_from_config(monkeypatch):
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg.embedding, "dimension", 8)   # 自动还原
    chunks = [_mk_chunk(10)]
    client = FakeHA3(present={10})
    ctx = _setup(monkeypatch, client, chunks)
    pn.node_verify_and_repush(ctx)
    assert client.query_vector_lens and all(n == 8 for n in client.query_vector_lens)


# ── 12. DROP + UNKNOWN coexist → two separate UPDATEs, distinct codes ─────────
def test_drop_and_unknown_coexist_two_updates(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11), _mk_chunk(12), _mk_chunk(13)]
    client = FakeHA3(present={10, 12}, never_heal={11}, point_raise={13})
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=1)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    upd = [e for e in conn.cur.executed if "UPDATE chunk_meta" in e[0]]
    assert len(upd) == 2                        # 两组分开写，保留故障分类
    codes = [e[1][0] for e in upd]
    assert codes == ["PARITY_DROP", "PARITY_UNKNOWN"]
    assert conn.committed
    assert chunks[1].index_error_code == "PARITY_DROP"     # rds_id 11
    assert chunks[3].index_error_code == "PARITY_UNKNOWN"  # rds_id 13


# ── 13. rowcount mismatch → rollback + raise, no commit ──────────────────────
def test_rowcount_mismatch_rolls_back(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10}, never_heal={11})
    conn = FakeConn(rowcount_override=0)         # UPDATE 改 0 行 ≠ 1 → 闭环失败
    ctx = _setup(monkeypatch, client, chunks, conn=conn, RAG_STAGE3_PARITY_MAX_RETRIES=0)
    with pytest.raises(RuntimeError, match="state-persistence"):
        pn.node_verify_and_repush(ctx)
    assert conn.rolled_back and not conn.committed


# ── 14. empty / all-non-INDEXED batch → return, no HA3 calls ─────────────────
def test_empty_batch_returns(monkeypatch):
    client = FakeHA3()
    ctx = _setup(monkeypatch, client, [])       # 空批
    pn.node_verify_and_repush(ctx)
    assert client.point_reads == [] and client.push_calls == []


def test_all_non_indexed_returns(monkeypatch):
    chunks = [_mk_chunk(10, status="FAILED"), _mk_chunk(11, status="NOT_INDEXED")]
    client = FakeHA3()
    ctx = _setup(monkeypatch, client, chunks)   # 无 INDEXED → expected 空
    pn.node_verify_and_repush(ctx)
    assert client.point_reads == [] and client.push_calls == []


# ── 15. DAG wiring guard ─────────────────────────────────────────────────────
def test_dag3_wiring_04b_before_05():
    from opensearch_pipeline.dag_definitions import build_dag3_chunk_to_opensearch
    dag = build_dag3_chunk_to_opensearch()
    assert "04b" in dag.nodes
    assert dag.nodes["04b"].depends_on == ["04"]
    assert dag.nodes["05"].depends_on == ["04b"]


# ── 16-20. content-drift sub-check (RAG_STAGE3_PARITY_DRIFT) ──────────────────
def test_drift_detected_then_healed(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, stale={11}, stale_heals=True)  # 11 present but stale content
    ctx = _setup(monkeypatch, client, chunks,
                 RAG_STAGE3_PARITY_DRIFT="true", RAG_STAGE3_PARITY_MAX_RETRIES=2)
    pn.node_verify_and_repush(ctx)              # drift re-pushed → content matches → no raise
    assert client.push_calls == [[11]]
    assert chunks[1].index_status == "INDEXED"


def test_drift_persistent_raises_parity_drift(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, stale={11}, stale_heals=False)  # never reconciles
    conn = FakeConn()
    ctx = _setup(monkeypatch, client, chunks, conn=conn,
                 RAG_STAGE3_PARITY_DRIFT="true", RAG_STAGE3_PARITY_MAX_RETRIES=1)
    with pytest.raises(RuntimeError, match="parity"):
        pn.node_verify_and_repush(ctx)
    assert chunks[1].index_error_code == "PARITY_DRIFT"
    upd = [e for e in conn.cur.executed if "UPDATE chunk_meta" in e[0]]
    assert any(e[1][0] == "PARITY_DRIFT" for e in upd)
    assert conn.committed
    assert ("doc1", 2) in ctx["failed_doc_versions"]


def test_drift_flag_off_no_check(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11}, stale={11}, stale_heals=False)  # content differs...
    ctx = _setup(monkeypatch, client, chunks)  # ...but RAG_STAGE3_PARITY_DRIFT unset
    pn.node_verify_and_repush(ctx)             # presence-only → no drift check → no raise
    assert client.push_calls == []             # nothing re-pushed


def test_drift_matching_content_no_drift(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    client = FakeHA3(present={10, 11})          # returns t{pk} == in-memory chunk_text
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_DRIFT="true")
    pn.node_verify_and_repush(ctx)             # no drift, no raise
    assert client.push_calls == []


def test_drift_failopen_on_unreadable_text(monkeypatch):
    chunks = [_mk_chunk(10), _mk_chunk(11)]
    # 11 present but returns no chunk_text_store (None) → drift skipped (fail-open), no raise
    client = FakeHA3(present={10, 11}, texts={11: None})
    ctx = _setup(monkeypatch, client, chunks, RAG_STAGE3_PARITY_DRIFT="true")
    pn.node_verify_and_repush(ctx)
    assert client.push_calls == []
