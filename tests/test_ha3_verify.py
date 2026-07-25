# -*- coding: utf-8 -*-
"""Tests for the G30 harness fix: authoritative self-query presence + loop-until-stable enum."""
import pytest

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
    # 契约键 foreign_ids 必须返回（此前漏返回 → 上线 verify 读它 KeyError、ACL 泄漏检查失效）
    assert r["foreign_ids"] == [999]


# ── enumerator：倒排单页（2026-07-22 替换零向量 loop-until-stable）───────────────
class _Cfg:
    table_name = "t"


def _inv_body(pks, covered=1.0, error=None):
    import json as _json
    body = {"totalCount": len(pks), "coveredPercent": covered,
            "result": [{"id": p, "score": 0.0,
                        "fields": {"chunk_id": f"c{p}", "doc_id": "D", "is_active": 1}}
                       for p in pks]}
    if error is not None:
        body["errorCode"] = error
    return type("R", (), {"body": _json.dumps(body)})()


class _FakeClient:
    """倒排路假客户端：按桶起点返回 PK；记录每次 search 的请求。"""

    def __init__(self, by_bucket, covered=1.0, error=None):
        self.by_bucket, self.covered, self.error = by_bucket, covered, error
        self.reqs = []

    def search(self, req):
        self.reqs.append(req)
        lo = int(str(req.text.filter).split("id>=")[1].split(" ")[0])
        return _inv_body(self.by_bucket.get(lo, []), self.covered, self.error)

    def query(self, req):   # 绝不应被调用：零向量枚举已废除
        raise AssertionError("不得回退零向量 query 枚举")


def _parse(resp):
    return []      # 已弃用参数，保留仅为签名兼容


class _QReq:
    def __init__(self, **kw): self.kw = kw


def test_enumerate_uses_single_page_inverted_search():
    """每桶恰好一次 search、无翻页；max_rounds 传入即被忽略（倒排是确定性的）。"""
    client = _FakeClient({0: [1, 2], 500: [600]})
    out = _enumerate_ha3_pks(client, _Cfg(), _parse, ["id"], _QReq,
                             id_hi=800, bucket=500, max_rounds=5)
    assert set(out) == {1, 2, 600}
    assert len(client.reqs) == 2, "两个桶 → 恰好两次 search（max_rounds 被忽略）"
    assert client.reqs[0].text.query_string == "is_active:'1' OR is_active:'0'"
    assert client.reqs[0].text.filter == "id>=0 AND id<500"
    assert client.reqs[1].text.filter == "id>=500 AND id<800"   # 末桶严格半开


def test_enumerate_raises_on_unhealthy_bucket():
    """桶不健康 → 抛 Ha3EnumerationUnhealthy（而非静默返回缺失结果）。
    契约刻意选异常：DAG-3 用 `set(seen)` 直接消费返回值，改结构体会把键当 PK。"""
    from opensearch_pipeline.ha3_reconcile import Ha3EnumerationUnhealthy
    client = _FakeClient({0: [1]}, covered=0.5)
    with pytest.raises(Ha3EnumerationUnhealthy):
        _enumerate_ha3_pks(client, _Cfg(), _parse, ["id"], _QReq, id_hi=100, bucket=500)
