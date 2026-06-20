# -*- coding: utf-8 -*-
"""Tests for the G30 harness fix: authoritative self-query presence + loop-until-stable enum."""
from opensearch_pipeline.ha3_verify import verify_chunks_present
from opensearch_pipeline.ha3_reconcile import _enumerate_ha3_pks


# ── fake RDS ────────────────────────────────────────────────────
class _Cur:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): pass
    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _Cur(self._rows)


def _chunks():
    return [
        {"id": 10, "chunk_id": "D_c0", "chunk_text": "procedure parent alpha", "chunk_type": "procedure_parent", "owner_dept": "hr"},
        {"id": 11, "chunk_id": "D_c1", "chunk_text": "step one beta content", "chunk_type": "step_card", "owner_dept": "hr"},
        {"id": 12, "chunk_id": "D_c2", "chunk_text": "step two gamma content", "chunk_type": "step_card", "owner_dept": "hr"},
    ]


def _retrieve(chunks, doc_id, miss=(), foreign=None):
    def rf(query, *, top_k=5, user_dept=None):
        out = []
        for c in chunks:
            if c["id"] in miss:
                continue
            if (c["chunk_text"] or "")[:160] == query:
                out.append({"id": str(c["id"]), "doc_id": doc_id, "chunk_id": c["chunk_id"]})
        if foreign:
            out.append(foreign)
        return out
    return rf


def test_all_chunks_present_ok():
    ch = _chunks()
    r = verify_chunks_present("D", conn=_Conn(ch), retrieve_fn=_retrieve(ch, "D"))
    assert r["ok"] and r["present"] == 3 and r["missing_ids"] == []
    assert r["expected_ids"] == [10, 11, 12] and r["served_ids"] == [10, 11, 12]
    assert "self-query" in r["method"]


def test_missing_chunk_detected():
    ch = _chunks()
    r = verify_chunks_present("D", conn=_Conn(ch), retrieve_fn=_retrieve(ch, "D", miss={12}))
    assert not r["ok"] and r["missing_ids"] == [12] and r["present"] == 2


def test_self_query_false_negative_when_retrieve_empty_is_caught():
    # simulate the G30 symptom at the SERVING layer would show as missing — verifier reports it,
    # never silently passes
    ch = _chunks()
    r = verify_chunks_present("D", conn=_Conn(ch), retrieve_fn=lambda q, **k: [])
    assert not r["ok"] and r["missing_ids"] == [10, 11, 12]


def test_foreign_doc_surfaced_recorded():
    ch = _chunks()
    foreign = {"id": "999", "doc_id": "OTHER", "chunk_id": "OTHER_c0"}
    r = verify_chunks_present("D", conn=_Conn(ch), retrieve_fn=_retrieve(ch, "D", foreign=foreign))
    assert r["ok"]                          # all D chunks still present
    # foreign id observed but not counted as present/served for D
    assert 999 not in r["served_ids"]


# ── enumerator loop-until-stable (G30 mitigation) ───────────────
class _Cfg:
    table_name = "t"


class _FakeClient:
    """Returns a different partial subset per call (simulates G30 non-determinism)."""
    def __init__(self, rounds): self.rounds = rounds; self.i = 0
    def query(self, req):
        out = self.rounds[min(self.i, len(self.rounds) - 1)]; self.i += 1; return out


def _parse(resp):
    return [{"id": i, "chunk_id": f"c{i}", "doc_id": "D"} for i in resp]


class _QReq:
    def __init__(self, **kw): self.kw = kw


def test_enumerate_loops_until_stable_unions_partial_scans():
    # round1 sees {1,2}; round2 sees {2,3}; round3 sees {1,2,3} (nothing new → stop)
    client = _FakeClient([[1, 2], [2, 3], [1, 2, 3]])
    out = _enumerate_ha3_pks(client, _Cfg(), _parse, ["id"], _QReq, id_hi=50, bucket=100, max_rounds=3)
    assert set(out) == {1, 2, 3}            # union across non-deterministic rounds
    assert client.i >= 2                    # it re-scanned the bucket


def test_enumerate_stops_early_when_stable():
    client = _FakeClient([[5, 6], [5, 6]])  # second round adds nothing → stop after 2
    out = _enumerate_ha3_pks(client, _Cfg(), _parse, ["id"], _QReq, id_hi=10, bucket=100, max_rounds=5)
    assert set(out) == {5, 6} and client.i == 2
