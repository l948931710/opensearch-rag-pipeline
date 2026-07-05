# -*- coding: utf-8 -*-
"""reconcile / ha3_reconcile 的 P2-29（扫描指标+时长告警）与 P2-30（流式分桶 parity）单测。

不变量：流式 run_parity_check 的报告与纯函数 compute_parity 在同一 fixture 上逐键一致
（下游消费者/既有测试不破）；报告新增 buckets_scanned / elapsed_s / scan_lo / rds_batch；
耗时超 RAG_RECONCILE_DURATION_ALERT_S → warning 级 ops 告警（fail-open）；
RAG_RECONCILE_SCAN_FROM_MIN 只对召回丢失方向安全 → 报告标 stale_scan_curtailed；
ha3_reconcile 孤儿方向保持全扫并记录同款指标。
"""
import pytest

from opensearch_pipeline import reconcile
from opensearch_pipeline.config import get_config


def _rds(id_, chunk_id, doc_id, *, active=1, indexed="INDEXED", ver=1, ctype="text_chunk"):
    return {"id": id_, "chunk_id": chunk_id, "doc_id": doc_id, "version_no": ver,
            "is_active": active, "index_status": indexed, "chunk_type": ctype}


def _ha3(chunk_id, doc_id, ctype="text_chunk", ver=1):
    return {"chunk_id": chunk_id, "doc_id": doc_id, "chunk_type": ctype, "version_no": ver}


# 覆盖全部判定分支的 fixture：missing / vanished / kept / dup / rds_inactive / orphan_chunkid
_RDS_ROWS = [
    _rds(1, "cA", "docX"),                     # kept
    _rds(2, "cB", "docX"),                     # active+INDEXED 但 HA3 缺失 → missing
    _rds(3, "cC", "docX", active=0),           # inactive（其 HA3 行 = rds_inactive）
    _rds(7, "cD", "docY", indexed="EMBEDDING"),  # docY 无任何 HA3 kept → vanished
]
_HA3_ROWS = {
    1: _ha3("cA", "docX"),
    3: _ha3("cC", "docX"),                     # rds_inactive
    4: _ha3("cA", "docX"),                     # dup（cA 在别处 active）
    9: _ha3("cZ", "docGone"),                  # orphan_chunkid + orphan doc
}


class _FakeCursor:
    """按 SQL 形态回放 _RDS_ROWS 的假游标（MIN/MAX 聚合、active 轻量预扫、整行桶读）。"""

    def __init__(self, table):
        self._table = table
        self._rows = []
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "SELECT MIN(id), MAX(id)" in s:
            ids = [r["id"] for r in self._table]
            self._one = (min(ids), max(ids)) if ids else (None, None)
        elif "WHERE is_active=1 AND id>=" in s:
            lo, hi = params
            self._rows = [(r["id"], r["chunk_id"], r["doc_id"]) for r in self._table
                          if r["is_active"] == 1 and lo <= r["id"] < hi]
        elif "WHERE id>=" in s:
            lo, hi = params
            self._rows = [(r["id"], r["chunk_id"], r["doc_id"], r["version_no"], r["is_active"],
                           r["index_status"], r["chunk_type"])
                          for r in self._table if lo <= r["id"] < hi]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, table):
        self._table = table
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._table)

    def close(self):
        self.closed = True


@pytest.fixture
def live_stores(monkeypatch):
    """切出 simulate + mock 掉 RDS/HA3 两侧 I/O（HA3 扫描按 [lo,hi) 过滤 fixture）。"""
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "simulate_opensearch", False)

    conn = _FakeConn(_RDS_ROWS)
    import opensearch_pipeline.prod_access as pa
    monkeypatch.setattr(pa, "get_prod_readonly_conn", lambda *a, **k: conn)

    from opensearch_pipeline import retriever
    monkeypatch.setattr(retriever, "_get_ha3_client", lambda: object())

    def _fake_scan(cli, table, hi, *, lo=0, bucket=500, client_factory=None, **kw):
        return {"rows": {pk: h for pk, h in _HA3_ROWS.items() if lo <= pk < hi},
                "truncated": []}

    monkeypatch.setattr(reconcile, "_scan_ha3_pks", _fake_scan)
    return conn


def test_streaming_parity_matches_pure_compute_parity(live_stores):
    """P2-30 等价性：流式分桶报告与 compute_parity 在同一 fixture 上逐键一致。"""
    streamed = reconcile.run_parity_check(hi=20)
    pure = reconcile.compute_parity(_RDS_ROWS, _HA3_ROWS)

    assert streamed["ok"] == pure["ok"] is False
    assert streamed["counts"] == pure["counts"]
    assert streamed["stale_subtypes"] == pure["stale_subtypes"]
    _key = lambda x: x["id"]  # noqa: E731
    assert sorted(streamed["rds_active_missing"], key=_key) == \
        sorted(pure["rds_active_missing"], key=_key)
    _dkey = lambda x: x["doc_id"]  # noqa: E731
    assert sorted(streamed["vanished_docs"], key=_dkey) == \
        sorted(pure["vanished_docs"], key=_dkey)
    assert streamed["orphan_docs_sample"] == pure["orphan_docs_sample"]
    assert streamed["complete"] is True
    assert live_stores.closed, "流式读完必须归还只读连接"


def test_parity_report_carries_scan_metrics(live_stores):
    """P2-29：报告必须带 buckets_scanned / elapsed_s / scan_lo / rds_batch。"""
    rep = reconcile.run_parity_check(hi=20)
    assert rep["buckets_scanned"] >= 1
    assert isinstance(rep["elapsed_s"], float)
    assert rep["scan_lo"] == 0, "默认必须从 0 全扫（孤儿方向）"
    assert rep["rds_batch"] >= 500


def test_duration_alert_fires_when_over_threshold(live_stores, monkeypatch):
    monkeypatch.setenv("RAG_RECONCILE_DURATION_ALERT_S", "1e-9")
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert", lambda *a, **k: sent.append((a, k)) or True)
    reconcile.run_parity_check(hi=20)
    assert len(sent) == 1
    assert sent[0][1].get("severity") == "warning"
    assert "duration" in sent[0][1].get("dedup_key", "")


def test_duration_alert_disabled_when_threshold_nonpositive(live_stores, monkeypatch):
    monkeypatch.setenv("RAG_RECONCILE_DURATION_ALERT_S", "0")
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert", lambda *a, **k: sent.append(a) or True)
    reconcile.run_parity_check(hi=20)
    assert sent == []


def test_scan_from_min_marks_report_curtailed(live_stores, monkeypatch):
    """起点优化只对召回丢失方向安全：开 RAG_RECONCILE_SCAN_FROM_MIN 必须显式标记
    stale_scan_curtailed（低于 min_id 的孤儿/stale 本轮未检），且 DIRECTION 1 结论不变。"""
    monkeypatch.setenv("RAG_RECONCILE_SCAN_FROM_MIN", "true")
    rep = reconcile.run_parity_check(hi=20)
    assert rep["scan_lo"] == 1
    assert rep["stale_scan_curtailed"] is True
    assert rep["counts"]["rds_active_missing"] == 1   # 召回丢失方向完整


# ── ha3_reconcile：孤儿方向保持全扫 + 同款指标 ──

def test_ha3_reconcile_records_scan_metrics(monkeypatch):
    from opensearch_pipeline import ha3_reconcile as hr
    import opensearch_pipeline.pipeline_nodes as pn

    class _Cur:
        def __init__(self):
            self._last = ""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._last = sql

        def fetchall(self):
            return [(1, "c1", 1)]

        def fetchone(self):
            return (1200,) if "MAX(id)" in self._last else None

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    class _Cli:
        def push_documents(self, *a, **k):
            raise AssertionError("dry-run 不应触发删除")

    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: _Conn())
    monkeypatch.setattr(pn, "_get_opensearch_client", lambda *a, **k: _Cli())
    monkeypatch.setattr(hr, "_enumerate_ha3_pks", lambda *a, **k: {1: ("c1", "d1")})

    rep = hr.reconcile_ha3_orphan_pks(simulate=False, dry_run=True)
    # id_hi = 1200 + headroom(1000) = 2200 → 500 宽桶 = 5 个
    assert rep["buckets_scanned"] == 5
    assert isinstance(rep["elapsed_s"], float)
    assert rep["checked"] == 1 and rep["deleted"] == 0


def test_ha3_reconcile_duration_alert(monkeypatch):
    from opensearch_pipeline import ha3_reconcile as hr
    import opensearch_pipeline.pipeline_nodes as pn

    monkeypatch.setenv("RAG_RECONCILE_DURATION_ALERT_S", "1e-9")
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert", lambda *a, **k: sent.append((a, k)) or True)

    class _Cur:
        def __init__(self):
            self._last = ""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._last = sql

        def fetchall(self):
            return []

        def fetchone(self):
            return (100,) if "MAX(id)" in self._last else None

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: _Conn())
    monkeypatch.setattr(pn, "_get_opensearch_client",
                        lambda *a, **k: type("C", (), {"push_documents": lambda *a, **k: None})())
    monkeypatch.setattr(hr, "_enumerate_ha3_pks", lambda *a, **k: {})

    hr.reconcile_ha3_orphan_pks(simulate=False, dry_run=True)
    assert len(sent) == 1 and sent[0][1].get("severity") == "warning"
