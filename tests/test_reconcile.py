# -*- coding: utf-8 -*-
"""tests/test_reconcile.py — Phase-3 CS3: RDS↔HA3 parity reconciler.

Invariants: pure compute_parity diffs both directions; recall-loss (missing / vanished) fails ok,
stale alone does not; run_parity_check is simulate-safe + fail-open; CLI exit codes map to states.
"""
from opensearch_pipeline import reconcile


def _rds(id_, chunk_id, doc_id, *, active=1, indexed="INDEXED", ver=1, ctype="text_chunk"):
    return {"id": id_, "chunk_id": chunk_id, "doc_id": doc_id, "version_no": ver,
            "is_active": active, "index_status": indexed, "chunk_type": ctype}


def _ha3(chunk_id, doc_id, ctype="text_chunk", ver=1):
    return {"chunk_id": chunk_id, "doc_id": doc_id, "chunk_type": ctype, "version_no": ver}


# ── compute_parity: clean ──

def test_parity_clean_when_perfectly_aligned():
    rds = [_rds(1, "cA", "docX"), _rds(2, "cB", "docX")]
    ha3 = {1: _ha3("cA", "docX"), 2: _ha3("cB", "docX")}
    rep = reconcile.compute_parity(rds, ha3)
    assert rep["ok"] is True
    assert rep["counts"]["rds_active_missing"] == 0
    assert rep["counts"]["ha3_stale"] == 0
    assert rep["counts"]["vanished_docs"] == 0


# ── DIRECTION 1: recall loss (active+INDEXED missing from HA3) ──

def test_parity_flags_active_indexed_missing_from_ha3():
    rds = [_rds(1, "cA", "docX"), _rds(2, "cB", "docX")]
    ha3 = {1: _ha3("cA", "docX")}  # id=2 absent
    rep = reconcile.compute_parity(rds, ha3)
    assert rep["ok"] is False
    assert rep["counts"]["rds_active_missing"] == 1
    assert rep["rds_active_missing"][0]["id"] == 2
    # docX still has id=1 in HA3 → not a full vanish
    assert rep["counts"]["vanished_docs"] == 0


def test_parity_non_indexed_active_not_counted_missing():
    """An active row that isn't INDEXED yet (mid-ingest) is not 'missing' — only INDEXED counts."""
    rds = [_rds(1, "cA", "docX", indexed="EMBEDDING")]
    ha3 = {}
    rep = reconcile.compute_parity(rds, ha3)
    assert rep["counts"]["rds_active_missing"] == 0
    # but the doc has active chunks and zero HA3 rows → still a vanish signal
    assert rep["counts"]["vanished_docs"] == 1
    assert rep["ok"] is False


# ── WORST CASE: full doc vanish ──

def test_parity_flags_fully_vanished_doc():
    rds = [_rds(1, "cA", "docX"), _rds(2, "cB", "docX")]
    ha3 = {}  # whole doc gone from HA3
    rep = reconcile.compute_parity(rds, ha3)
    assert rep["counts"]["vanished_docs"] == 1
    assert rep["vanished_docs"][0]["doc_id"] == "docX"
    assert rep["vanished_docs"][0]["rds_active"] == 2
    assert rep["ok"] is False


# ── DIRECTION 2: stale subtypes (do NOT fail ok) ──

def test_parity_stale_subtypes_classified_and_ok_unaffected():
    rds = [
        _rds(1, "cA", "docX"),                          # active, kept
        _rds(2, "cB", "docX", active=0),                # inactive → its HA3 row is rds_inactive
    ]
    ha3 = {
        1: _ha3("cA", "docX"),                          # kept
        2: _ha3("cB", "docX"),                          # pk in rds but inactive → rds_inactive
        3: _ha3("cA", "docX"),                          # chunk_id is active elsewhere → dup
        9: _ha3("cZ", "docGone"),                       # neither pk nor chunk_id active → orphan_chunkid
    }
    rep = reconcile.compute_parity(rds, ha3)
    assert rep["stale_subtypes"] == {"rds_inactive": 1, "dup": 1, "orphan_chunkid": 1}
    assert rep["counts"]["ha3_stale"] == 3
    # docX still kept (id=1) so no recall-loss → ok stays True despite stale rows
    assert rep["ok"] is True
    assert "docGone" in rep["orphan_docs_sample"]


# ── run_parity_check: simulate-safe no-op ──

def test_run_parity_check_simulate_is_noop(monkeypatch):
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", True)
    rep = reconcile.run_parity_check()
    assert rep["ok"] is True and rep.get("skipped") == "simulate"


def test_run_parity_check_fail_open_on_db_error(monkeypatch):
    """A live-path failure must not raise — returns ok=False + error."""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "simulate_opensearch", False)
    import opensearch_pipeline.prod_access as pa
    monkeypatch.setattr(pa, "get_prod_readonly_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ro down")))
    rep = reconcile.run_parity_check()
    assert rep["ok"] is False and "ro down" in rep["error"]


def test_rds_conn_falls_back_to_pool_when_no_env_file(monkeypatch):
    """Cred portability: when prod_access has no .env file (DataWorks pod), _rds_conn falls back to
    the config/env pool (_get_db_conn) instead of failing."""
    import opensearch_pipeline.prod_access as pa
    import opensearch_pipeline.pipeline_nodes as pn

    def _raise_no_env(*a, **k):
        raise pa.ProdAccessError("未找到生产侧 env 文件")

    monkeypatch.setattr(pa, "get_prod_readonly_conn", _raise_no_env)
    sentinel = object()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **k: sentinel)
    assert reconcile._rds_conn() is sentinel


def test_rds_conn_prefers_prod_access_when_available(monkeypatch):
    """On the laptop (env file present), _rds_conn uses the dedicated read-only path, NOT the pool."""
    import opensearch_pipeline.prod_access as pa
    import opensearch_pipeline.pipeline_nodes as pn
    ro = object()
    monkeypatch.setattr(pa, "get_prod_readonly_conn", lambda *a, **k: ro)
    monkeypatch.setattr(pn, "_get_db_conn",
                        lambda **k: (_ for _ in ()).throw(AssertionError("must not use pool")))
    assert reconcile._rds_conn() is ro


def test_as_dict_rows_handles_tuple_and_dict_cursors():
    cols = ("id", "chunk_id")
    assert reconcile._as_dict_rows([(1, "c1")], cols) == [{"id": 1, "chunk_id": "c1"}]
    assert reconcile._as_dict_rows([{"id": 2, "chunk_id": "c2"}], cols) == [{"id": 2, "chunk_id": "c2"}]


def test_run_parity_check_alerts_on_drift(monkeypatch):
    """alert=True + drift → exactly one OBS-4 ops alert, fail-open if it errors."""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "simulate_opensearch", False)
    import opensearch_pipeline.prod_access as pa
    monkeypatch.setattr(pa, "get_prod_readonly_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert",
                        lambda *a, **k: sent.append((a, k)) or True)
    rep = reconcile.run_parity_check(alert=True)
    assert rep["ok"] is False
    assert len(sent) == 1
    assert sent[0][1].get("severity") == "critical"


# ── CS4: OSS↔RDS image-object parity ──

def test_collect_referenced_image_keys_active_only():
    import json
    rows = [
        {"chunk_id": "cA", "is_active": 1,
         "extra_json": json.dumps({"image_refs": [{"oss_key": "p/a.png"}, {"oss_key": "p/b.png"}]})},
        {"chunk_id": "cB", "is_active": 0,  # inactive → ignored
         "extra_json": json.dumps({"image_refs": [{"oss_key": "p/z.png"}]})},
        {"chunk_id": "cC", "is_active": 1, "extra_json": "{not json"},  # malformed → skipped
        {"chunk_id": "cD", "is_active": 1, "extra_json": None},
    ]
    ref = reconcile.collect_referenced_image_keys(rows)
    assert set(ref) == {"p/a.png", "p/b.png"}
    assert ref["p/a.png"] == "cA"


def test_compute_oss_parity_missing_and_orphan():
    ref = {"p/a.png": "cA", "p/b.png": "cB"}
    present = {"p/a.png", "p/c.png"}  # b missing (broken image), c orphan
    rep = reconcile.compute_oss_parity(ref, present)
    assert rep["ok"] is False
    assert rep["counts"] == {"referenced": 2, "present": 2, "candidates_offprefix": 0,
                             "missing": 1, "orphan": 1}
    assert rep["missing"][0]["oss_key"] == "p/b.png" and rep["missing"][0]["chunk_id"] == "cB"
    assert rep["orphan_sample"] == ["p/c.png"]


def test_compute_oss_parity_clean():
    ref = {"p/a.png": "cA"}
    rep = reconcile.compute_oss_parity(ref, {"p/a.png"})
    assert rep["ok"] is True and rep["counts"]["missing"] == 0


def test_compute_oss_parity_verify_fn_eliminates_offprefix_false_positive():
    """A referenced key outside the listed prefix (raw/*) is NOT missing if it really exists.
    Mirrors the live prod finding: raw/marketing/*.jpg flagged by set-diff but object_exists=True."""
    ref = {"processing/assets/a.png": "cA", "raw/marketing/x.jpg": "cB", "processing/assets/gone.png": "cC"}
    present = {"processing/assets/a.png"}  # only the listed prefix
    # raw/x.jpg exists (off-prefix), gone.png truly absent
    exists = {"raw/marketing/x.jpg"}
    rep = reconcile.compute_oss_parity(ref, present, verify_fn=lambda k: k in exists)
    assert rep["counts"]["missing"] == 1  # only gone.png
    assert rep["missing"][0]["oss_key"] == "processing/assets/gone.png"
    assert rep["counts"]["candidates_offprefix"] == 1  # raw/x.jpg verified-exists
    assert rep["ok"] is False


def test_compute_oss_parity_orphan_alone_is_ok():
    """Orphan OSS objects (storage bloat) do NOT fail ok — only missing-referenced does."""
    rep = reconcile.compute_oss_parity({}, {"p/x.png", "p/y.png"})
    assert rep["ok"] is True and rep["counts"]["orphan"] == 2


def test_run_oss_parity_check_simulate_is_noop(monkeypatch):
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", True)
    rep = reconcile.run_oss_parity_check()
    assert rep["ok"] is True and rep.get("skipped") == "simulate"


def test_run_oss_parity_check_fail_open(monkeypatch):
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    import opensearch_pipeline.prod_access as pa
    monkeypatch.setattr(pa, "get_prod_readonly_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("oss-ro down")))
    rep = reconcile.run_oss_parity_check()
    assert rep["ok"] is False and "oss-ro down" in rep["error"]


# ── CS4b: raw_key↔OSS parity ──

def test_compute_raw_parity_missing_and_null():
    rows = [
        {"doc_id": "dA", "version_no": 1, "raw_key": "raw/a.docx"},   # exists
        {"doc_id": "dB", "version_no": 2, "raw_key": "raw/gone.docx"},  # missing
        {"doc_id": "dC", "version_no": 1, "raw_key": None},            # null → not a missing-file
    ]
    exists = {"raw/a.docx"}
    rep = reconcile.compute_raw_parity(rows, lambda k: k in exists)
    assert rep["ok"] is False
    assert rep["counts"] == {"total": 3, "have_raw_key": 2, "null_raw_key": 1, "missing": 1}
    assert rep["missing"][0]["doc_id"] == "dB"
    assert rep["null_raw_key_sample"] == ["dC"]


def test_compute_raw_parity_clean():
    rows = [{"doc_id": "dA", "version_no": 1, "raw_key": "raw/a.docx"}]
    rep = reconcile.compute_raw_parity(rows, lambda k: True)
    assert rep["ok"] is True and rep["counts"]["missing"] == 0


def test_run_raw_parity_check_simulate_is_noop(monkeypatch):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config(), "simulate", True)
    rep = reconcile.run_raw_parity_check()
    assert rep["ok"] is True and rep.get("skipped") == "simulate"


def test_run_raw_parity_check_fail_open(monkeypatch):
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    import opensearch_pipeline.prod_access as pa
    monkeypatch.setattr(pa, "get_prod_readonly_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("raw-ro down")))
    rep = reconcile.run_raw_parity_check()
    assert rep["ok"] is False and "raw-ro down" in rep["error"]


# ── CLI exit codes ──

def test_cli_exit_codes(monkeypatch, capsys):
    # ok → 0
    monkeypatch.setattr(reconcile, "run_parity_check",
                        lambda **k: {"ok": True, "complete": True, "counts": {}})
    assert reconcile.main(["--check", "ha3"]) == 0
    # drift → 2
    monkeypatch.setattr(reconcile, "run_parity_check",
                        lambda **k: {"ok": False, "complete": True, "counts": {},
                                     "rds_active_missing": [], "vanished_docs": []})
    assert reconcile.main(["--check", "ha3"]) == 2
    # error/incomplete → 3
    monkeypatch.setattr(reconcile, "run_parity_check",
                        lambda **k: {"ok": False, "complete": False, "counts": {},
                                     "error": "x", "rds_active_missing": [], "vanished_docs": []})
    assert reconcile.main(["--check", "ha3"]) == 3
    # simulate skip → 0
    monkeypatch.setattr(reconcile, "run_parity_check",
                        lambda **k: {"ok": True, "skipped": "simulate", "counts": {}})
    assert reconcile.main(["--check", "ha3"]) == 0


def test_cli_all_takes_worst_exit_code(monkeypatch):
    """--check all → exit = max(ha3, oss) codes."""
    monkeypatch.setattr(reconcile, "run_parity_check",
                        lambda **k: {"ok": True, "complete": True, "counts": {}})  # 0
    monkeypatch.setattr(reconcile, "run_oss_parity_check",
                        lambda **k: {"ok": False, "complete": True, "counts": {},
                                     "missing": []})  # drift → 2
    assert reconcile.main(["--check", "all"]) == 2


# ── _scan_ha3_pks：倒排单页枚举（2026-07-22 替换零向量 loop-until-stable）──

def _inv_resp(rows, covered=1.0, error=None):
    """构造一条 /search 响应体（倒排路：result 为裸 list、id 在顶层、score=0.0）。"""
    import json as _json
    body = {"totalCount": len(rows), "totalTime": 1.0, "coveredPercent": covered,
            "result": [{"id": r["id"], "score": 0.0,
                        "fields": {k: v for k, v in r.items() if k != "id"}} for r in rows]}
    if error is not None:
        body["errorCode"] = error
    return type("R", (), {"body": _json.dumps(body)})()


class _InvCli:
    """记录 SearchRequest 形状的假客户端（每桶恰好一次 search）。"""

    def __init__(self, by_bucket, covered=1.0, error=None):
        self.by_bucket, self.covered, self.error = by_bucket, covered, error
        self.reqs = []

    def search(self, req):
        self.reqs.append(req)
        lo = int(str(req.text.filter).split("id>=")[1].split(" ")[0])
        return _inv_resp(self.by_bucket.get(lo, []), self.covered, self.error)


def _row(pk, active="1"):
    return {"id": pk, "chunk_id": f"c{pk}", "doc_id": "d1", "chunk_type": "text_chunk",
            "version_no": 1, "is_active": active}


def test_scan_ha3_pks_uses_single_page_inverted_request():
    """契约：每桶恰好一次 client.search；请求无 vector/knn、size=1000、无 from_、
    锚点与 filter 逐字正确。零向量 QueryRequest 与 loop-until-stable 均已废除。"""
    cli = _InvCli({0: [_row(1), _row(2)]})
    out = reconcile._scan_ha3_pks(cli, "tbl", hi=500, lo=0, bucket=500, max_rounds=3)
    assert set(out["rows"]) == {1, 2}
    assert out["truncated"] == [] and out["unhealthy"] == {}
    assert len(cli.reqs) == 1, "单页枚举：每桶恰好一次 search（max_rounds 已被忽略）"
    req = cli.reqs[0]
    assert getattr(req, "knn", None) is None and not hasattr(req.text, "vector")
    assert req.size == 1000 and getattr(req, "from_", None) in (None, 0)
    assert req.text.query_string == "is_active:'1' OR is_active:'0'"
    assert req.text.filter == "id>=0 AND id<500"


def test_scan_ha3_pks_bucket_clamped_and_half_open():
    """桶宽被钳到 500（防调用方重开宽桶导致翻页/饱和），末桶严格半开不越界。"""
    cli = _InvCli({0: [_row(1)], 500: [_row(600)], 1000: [_row(1100)]})
    reconcile._scan_ha3_pks(cli, "tbl", hi=1200, lo=0, bucket=5000)
    filters = [r.text.filter for r in cli.reqs]
    assert filters == ["id>=0 AND id<500", "id>=500 AND id<1000", "id>=1000 AND id<1200"]


def test_scan_ha3_pks_unhealthy_bucket_recorded_not_silently_empty():
    """coveredPercent<1 → 该桶 unhealthy 并带 reason（绝不当成"这桶就是空的"）。"""
    cli = _InvCli({0: [_row(1)]}, covered=0.5)
    out = reconcile._scan_ha3_pks(cli, "tbl", hi=500, lo=0, bucket=500)
    assert out["rows"] == {} and out["unhealthy"] and "coveredPercent" in out["unhealthy"][0]


def test_scan_ha3_pks_never_falls_back_to_zero_vector():
    """枚举异常时绝不回退零向量 query——client 只有 query 没有 search ⇒ 判 unhealthy。"""
    class _OnlyQuery:
        def query(self, req):
            raise AssertionError("绝不允许回退零向量 query 枚举")

    out = reconcile._scan_ha3_pks(_OnlyQuery(), "tbl", hi=500, lo=0, bucket=500)
    assert out["rows"] == {} and 0 in out["unhealthy"]


# ── 07-21 fetch 二次定性：missing_confirmed vs query_invisible ──

def _ensure_ha3_fetch_request():
    """确保 alibabacloud_ha3engine_vector.models 有 FetchRequest。xdist 同 worker 内
    test_ha3_engine/test_rrf_hybrid_search 可能先注入了无 FetchRequest 的 SDK stub
    （导入顺序不定），缺则按仓库惯例（_ensure_ha3_mock_modules 的就地补属性）补一个
    kwargs 直存的 mock；真 SDK 在场则天然有、no-op。"""
    import sys
    import types
    try:
        from alibabacloud_ha3engine_vector import models as ha3_models
    except ImportError:   # 无 SDK 环境（CI）且尚无 stub → 建 stub（含 client，防下游炸）
        from unittest.mock import MagicMock
        ha3_pkg = sys.modules.get("alibabacloud_ha3engine_vector",
                                  types.ModuleType("alibabacloud_ha3engine_vector"))
        ha3_models = sys.modules.get("alibabacloud_ha3engine_vector.models",
                                     types.ModuleType("alibabacloud_ha3engine_vector.models"))
        ha3_client = sys.modules.get("alibabacloud_ha3engine_vector.client",
                                     types.ModuleType("alibabacloud_ha3engine_vector.client"))
        if not hasattr(ha3_client, "Client"):
            ha3_client.Client = MagicMock
        sys.modules["alibabacloud_ha3engine_vector"] = ha3_pkg
        sys.modules["alibabacloud_ha3engine_vector.models"] = ha3_models
        sys.modules["alibabacloud_ha3engine_vector.client"] = ha3_client
    if not hasattr(ha3_models, "FetchRequest"):
        class MockFetchRequest:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
        ha3_models.FetchRequest = MockFetchRequest


_ensure_ha3_fetch_request()


def _missing(*ids):
    return [{"id": i, "chunk_id": f"c{i}", "doc_id": "d1"} for i in ids]


class _FetchResp:
    def __init__(self, body):
        self.body = body


def test_fetch_reclassify_splits_confirmed_and_invisible():
    """fetch 可取回的判 query_invisible（查询链路失明），fetch 也无的判 missing_confirmed。"""
    import json

    class _Cli:
        def __init__(self):
            self.reqs = []

        def fetch(self, req):
            self.reqs.append(req)
            docs = [{"id": "2", "chunk_id": "c2"}] if "2" in req.ids else []
            return _FetchResp(json.dumps({"result": docs}))

    cli = _Cli()
    out = reconcile._fetch_reclassify_missing(cli, "tbl", _missing(1, 2, 3))
    assert out["ok"] is True
    assert out["query_invisible"] == [2]
    assert out["missing_confirmed"] == [1, 3]
    assert out["unclassified"] == [] and out["fetch_errors"] == []
    assert cli.reqs[0].table_name == "tbl" and cli.reqs[0].include_vector is False


def test_fetch_reclassify_batches_le_100_ids():
    """250 个判缺 PK → 3 批（100/100/50），每批 ids ≤100。"""
    import json

    class _Cli:
        def __init__(self):
            self.batches = []

        def fetch(self, req):
            self.batches.append(list(req.ids))
            return _FetchResp(json.dumps({"result": []}))

    cli = _Cli()
    out = reconcile._fetch_reclassify_missing(cli, "tbl", _missing(*range(250)))
    assert [len(b) for b in cli.batches] == [100, 100, 50]
    assert len(out["missing_confirmed"]) == 250


def test_fetch_reclassify_batch_error_leaves_batch_unclassified():
    """单批 fetch 异常 → 该批 ids 未定性（绝不误判为 confirmed），其余批照常分类，不 raise。"""
    import json

    class _Cli:
        def __init__(self):
            self.n = 0

        def fetch(self, req):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boom")
            return _FetchResp(json.dumps({"result": [{"id": str(req.ids[0])}]}))

    out = reconcile._fetch_reclassify_missing(_Cli(), "tbl", _missing(*range(150)))
    assert out["ok"] is True
    assert set(out["unclassified"]) == set(range(100))          # 第一批 0..99 全未定性
    assert out["query_invisible"] == [100]                       # 第二批首 id fetch 可取回
    assert set(out["missing_confirmed"]) == set(range(101, 150))
    assert out["fetch_errors"] and "boom" in out["fetch_errors"][0]


def test_fetch_reclassify_errorcode_body_counts_as_batch_error():
    """响应体带 errorCode → 该批按异常处理（unclassified），不误读为空结果 confirmed。"""
    import json

    class _Cli:
        def fetch(self, req):
            return _FetchResp(json.dumps({"errorCode": 403, "errorMsg": "denied"}))

    out = reconcile._fetch_reclassify_missing(_Cli(), "tbl", _missing(1))
    assert out["ok"] is False                # 全部批失败 → 等同整体失败，维持 query 单口径
    assert "fetch batches failed" in out["error"]


def test_fetch_reclassify_total_failure_fail_open():
    """client 根本没有 fetch 能力（AttributeError）→ ok=False，绝不 raise。"""
    out = reconcile._fetch_reclassify_missing(object(), "tbl", _missing(1, 2))
    assert out["ok"] is False and out.get("error")


def test_alert_on_drift_text_carries_fetch_classification(monkeypatch):
    """定性成功 → 告警文案分类展示两计数；等级恒 critical。"""
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert",
                        lambda title, text, **k: sent.append((title, text, k)) or True)
    report = {"ok": False, "complete": True,
              "counts": {"rds_active_missing": 3, "vanished_docs": 0, "ha3_stale": 0,
                         "missing_confirmed": 1, "enum_invisible": 2, "missing_unclassified": 0},
              "fetch_reclassify": {"ok": True, "enum_invisible": [2, 3],
                                   "missing_confirmed": [1], "unclassified": [],
                                   "fetch_errors": []}}
    reconcile._alert_on_drift(report)
    (_, text, kw) = sent[0]
    assert kw["severity"] == "critical"
    assert "missing_confirmed" in text and "**1**" in text
    assert "enum_invisible" in text and "**2**" in text
    assert "锚点不完整" in text


def test_alert_on_drift_falls_back_when_reclassify_failed(monkeypatch):
    """定性失败 → 旧文案（query 单口径）+ 失败注记；等级仍 critical。"""
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert",
                        lambda title, text, **k: sent.append((title, text, k)) or True)
    report = {"ok": False, "complete": True,
              "counts": {"rds_active_missing": 5, "vanished_docs": 1, "ha3_stale": 0},
              "fetch_reclassify": {"ok": False, "error": "RuntimeError: down"}}
    reconcile._alert_on_drift(report)
    (_, text, kw) = sent[0]
    assert kw["severity"] == "critical"
    assert "**5**" in text and "missing_confirmed" not in text
    assert "二次定性失败" in text and "down" in text


# ── 终局裁决（2026-07-21「存在性判定唯 fetch 为准」）：_finalize_verdict + 告警分级 ──

def _vrep(missing=(), vanished=()):
    """最小 parity 报告：missing=[(pk, doc_id)…] vanished=[doc_id…]（query 口径 ok）。"""
    m = [{"id": pk, "chunk_id": f"c{pk}", "doc_id": d, "version_no": 1,
          "chunk_type": "text_chunk"} for pk, d in missing]
    v = [{"doc_id": d, "rds_active": 1, "ha3_kept": 0} for d in vanished]
    return {"ok": not m and not v, "complete": True,
            "counts": {"rds_active_missing": len(m), "vanished_docs": len(v),
                       "vanished_at_risk": len(v), "ha3_stale": 0},
            "rds_active_missing": m, "vanished_docs": v}


def _fr(confirmed=(), invisible=(), unclassified=()):
    return {"ok": True, "missing_confirmed": list(confirmed),
            "enum_invisible": list(invisible), "query_invisible": list(invisible),
            "unclassified": list(unclassified), "fetch_errors": []}


def test_finalize_verdict_enum_invisible_marks_degraded_incomplete():
    """倒排枚举下 enum_invisible>0 = 枚举/锚点失效（不再是良性伪影）：
    数据仍在场故 ok=True，但 enum_health=degraded + complete=False
    ⇒ extra/orphan 方向不可测 ⇒ 退出码 3 + critical 告警。"""
    rep = _vrep(missing=[(2, "docX"), (3, "docX")])
    reconcile._finalize_verdict(rep, _fr(invisible=[2, 3]))
    assert rep["ok"] is True and rep["verdict_basis"] == "fetch"
    assert rep["enum_health"] == "degraded"
    assert rep["complete"] is False
    assert rep["counts"]["vanished_at_risk"] == 0


def test_finalize_verdict_clean_run_is_healthy_and_complete():
    """无判缺候选 → healthy，complete 不被动过（保持 True）。"""
    rep = _vrep()
    reconcile._finalize_verdict(rep, None)
    assert rep["enum_health"] == "healthy" and rep["complete"] is True


def test_finalize_verdict_unclassified_is_unknown_not_degraded():
    """fetch 有未定性批 → enum_health=unknown（不冤枉锚点）+ complete=False。"""
    rep = _vrep(missing=[(2, "docX")])
    reconcile._finalize_verdict(rep, _fr(unclassified=[2]))
    assert rep["enum_health"] == "unknown" and rep["complete"] is False


def test_finalize_verdict_unhealthy_buckets_force_degraded():
    """桶协议不健康 → degraded + complete=False，即便本轮 diff 恰好为零。"""
    rep = _vrep()
    rep["unhealthy_buckets"] = {0: "coveredPercent=0.5"}
    reconcile._finalize_verdict(rep, None)
    assert rep["enum_health"] == "degraded" and rep["complete"] is False


def test_finalize_verdict_confirmed_or_unclassified_keeps_red():
    rep = _vrep(missing=[(2, "docX"), (3, "docX")])
    reconcile._finalize_verdict(rep, _fr(confirmed=[2], invisible=[3]))
    assert rep["ok"] is False and rep["verdict_basis"] == "fetch"
    rep2 = _vrep(missing=[(2, "docX")])
    reconcile._finalize_verdict(rep2, _fr(unclassified=[2]))
    assert rep2["ok"] is False


def test_finalize_verdict_vanished_all_invisible_is_artifact():
    """vanished doc 的全部判缺 PK fetch 在场 → artifact（数据在场，非真消失）：
    ok 不因它转红；但枚举本身失效仍使 complete=False。"""
    rep = _vrep(missing=[(5, "docV"), (6, "docV")], vanished=["docV"])
    reconcile._finalize_verdict(rep, _fr(invisible=[5, 6]))
    assert rep["vanished_fetch_verdicts"] == {"docV": "artifact"}
    assert rep["counts"]["vanished_at_risk"] == 0 and rep["ok"] is True
    assert rep["complete"] is False and rep["enum_health"] == "degraded"


def test_finalize_verdict_vanished_without_indexed_coverage_stays_at_risk():
    """纯 NOT_INDEXED vanished（无判缺 PK 覆盖，fetch 管不到）→ at_risk 保守留红。"""
    rep = _vrep(missing=[(2, "docX")], vanished=["docY"])
    reconcile._finalize_verdict(rep, _fr(invisible=[2]))
    assert rep["vanished_fetch_verdicts"]["docY"] == "at_risk:no_indexed_coverage"
    assert rep["counts"]["vanished_at_risk"] == 1 and rep["ok"] is False


def test_finalize_verdict_no_fetch_basis_leaves_ok_untouched():
    """fr 未跑（无判缺候选）/ fr 失败 → ok 维持 query 单口径（方向朝红），basis 注记区分。"""
    rep = _vrep(vanished=["docY"])           # 纯 NOT_INDEXED vanished，missing 为空
    reconcile._finalize_verdict(rep, None)
    assert rep["ok"] is False and rep["verdict_basis"] == "query_enum"
    assert rep["counts"]["vanished_at_risk"] == 1
    rep2 = _vrep(missing=[(2, "docX")])
    reconcile._finalize_verdict(rep2, {"ok": False, "error": "down"})
    assert rep2["ok"] is False and rep2["verdict_basis"] == "query_only"


def test_alert_enum_invisible_is_critical_not_info(monkeypatch):
    """13116d2 的「纯盲区降 info」在倒排枚举下**有意翻回 critical**：
    零向量时代盲区是预期伪影，倒排下它意味着枚举/锚点失效，必须叫醒人。

    2026-08-04 独立核验修正：severity 维持 critical，但该形态（ok=True、fetch 刚证明
    数据全在、只是枚举面不完整）**不得**顶「drift」标题/占真 drift 去重槽——07-21 六次
    「行蒸发」伪事件正是这么被误读的。标题/槽改走「核验不完整」三态。"""
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert",
                        lambda title, text, **k: sent.append((title, text, k)) or True)
    report = {"ok": True, "complete": False, "verdict_basis": "fetch",
              "enum_health": "degraded",
              "counts": {"rds_active_missing": 3, "vanished_docs": 0, "vanished_at_risk": 0,
                         "ha3_stale": 0, "missing_confirmed": 0, "enum_invisible": 3,
                         "missing_unclassified": 0},
              "fetch_reclassify": _fr(invisible=[1, 2, 3])}
    reconcile._alert_on_drift(report)
    (title, text, kw) = sent[0]
    assert kw["severity"] == "critical"
    assert "核验不完整" in title and "drift" not in title.lower(), title
    assert kw["dedup_key"] == "reconcile:rds-ha3-parity:incomplete"
    assert "锚点不完整" in text and "enum_health=degraded" in text


def test_alert_incomplete_family_never_claims_drift(monkeypatch):
    """`complete=False` 无 error 的家族（不健康桶/fetch 整体失败等）不得顶 drift 标题。
    （2026-08-04 独立核验的 DEFECT-1：此前该家族与真 drift 共用标题+去重槽。）"""
    sent = _capture_alerts(monkeypatch)
    reconcile._alert_on_drift({
        "ok": False, "complete": False, "enum_health": "degraded",
        "counts": {"rds_active_missing": 5, "vanished_docs": 0, "missing_confirmed": 0},
        "unhealthy_buckets": {"b3": "totalCount=0"}})
    title, _text, kw = sent[0]
    assert "核验不完整" in title and "drift" not in title.lower(), title
    assert kw["dedup_key"] == "reconcile:rds-ha3-parity:incomplete"


def test_alert_fetch_confirmed_loss_still_claims_drift_even_if_incomplete(monkeypatch):
    """例外路径：fetch 已确认真丢失（missing_confirmed>0）——存在性唯 fetch 为准，
    哪怕核验不完整也必须以 drift 名义叫醒人、占真 drift 去重槽。"""
    sent = _capture_alerts(monkeypatch)
    reconcile._alert_on_drift({
        "ok": False, "complete": False, "verdict_basis": "fetch",
        "counts": {"rds_active_missing": 4, "vanished_docs": 1, "vanished_at_risk": 1,
                   "ha3_stale": 0, "missing_confirmed": 4, "enum_invisible": 0,
                   "missing_unclassified": 0},
        "fetch_reclassify": _fr(confirmed=[1, 2, 3, 4])})
    title, _text, kw = sent[0]
    assert "drift" in title and "核验不完整" not in title, title
    assert kw["dedup_key"] == "reconcile:rds-ha3-parity"


def test_alert_true_drift_direction_keeps_drift_title(monkeypatch):
    """真漂移方向（complete=True、ok=False）标题必须仍是 drift——防止三态分流把
    真事件误降成「不完整」。（独立核验指出 ha3 真漂移方向此前零测试钉扎。）"""
    sent = _capture_alerts(monkeypatch)
    reconcile._alert_on_drift({
        "ok": False, "complete": True, "verdict_basis": "fetch",
        "counts": {"rds_active_missing": 2, "vanished_docs": 0, "vanished_at_risk": 0,
                   "ha3_stale": 0, "missing_confirmed": 2, "enum_invisible": 0,
                   "missing_unclassified": 0},
        "fetch_reclassify": _fr(confirmed=[1, 2])})
    title, _text, kw = sent[0]
    assert "drift" in title, title
    assert kw["dedup_key"] == "reconcile:rds-ha3-parity"


def test_raw_parity_alert_both_directions(monkeypatch):
    """raw 对账双向行为测试（独立核验指出此前**零**行为测试：条件整体反转全绿）。"""
    sent = _capture_alerts(monkeypatch)
    reconcile._alert_on_raw_drift({"ok": False, "complete": False,
                                   "error": "OperationalError: (2003, ...)", "counts": {}})
    title, _text, kw = sent[0]
    assert "探针失败" in title and "drift" not in title.lower(), title
    assert kw["dedup_key"].endswith(":error")

    sent.clear()
    reconcile._alert_on_raw_drift({"ok": False, "complete": True,
                                   "counts": {"missing": 3, "have_raw_key": 100,
                                              "null_raw_key": 2}})
    title, _text, kw = sent[0]
    assert "drift" in title and "探针失败" not in title, title
    assert kw["dedup_key"] == "reconcile:raw-oss-parity"


def test_qa_rollup_alert_splits_probe_error_from_breach(monkeypatch):
    """qa_rollup 同族缺陷（独立核验 DEFECT-2：本机 14 条被抑制 CRITICAL 全部顶着
    "QA serving SLO breach"，实际是 DNS 探针失败）——修后双向钉扎。"""
    from opensearch_pipeline import qa_rollup
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert",
                        lambda title, text, **k: sent.append((title, text, k)) or True)
    qa_rollup._alert_on_slo({"ok": False, "error": "gaierror: [Errno 8] ..."})
    title, _text, kw = sent[0]
    assert "探针失败" in title and "breach" not in title.lower(), title
    assert ":error:" in kw["dedup_key"]

    sent.clear()
    qa_rollup._alert_on_slo({"ok": True, "metric_date": "2026-08-03", "slo_ok": 0,
                             "breaches": [{"slo": "p95_latency", "value": 9.1,
                                           "threshold": 8.0}]})
    title, _text, kw = sent[0]
    assert title == "QA serving SLO breach"
    assert kw["dedup_key"] == "qa-slo:2026-08-03"


def test_alert_unhealthy_buckets_surface_reason(monkeypatch):
    """不健康桶原因必须进告警正文，否则排障只看到"缺了 N 行"无从下手。"""
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert",
                        lambda title, text, **k: sent.append((title, text, k)) or True)
    report = {"ok": True, "complete": False, "verdict_basis": "query_enum",
              "enum_health": "degraded", "counts": {"rds_active_missing": 0},
              "unhealthy_buckets": {0: "coveredPercent=0.5（分片未全覆盖）"}}
    reconcile._alert_on_drift(report)
    (_, text, kw) = sent[0]
    assert kw["severity"] == "critical" and "coveredPercent" in text


def test_alert_vanished_at_risk_or_unclassified_stays_critical(monkeypatch):
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert",
                        lambda title, text, **k: sent.append((title, text, k)) or True)
    base_counts = {"rds_active_missing": 3, "vanished_docs": 1, "ha3_stale": 0,
                   "missing_confirmed": 0, "query_invisible": 3, "missing_unclassified": 0}
    report = {"ok": False, "complete": True, "verdict_basis": "fetch",
              "counts": dict(base_counts, vanished_at_risk=1),
              "fetch_reclassify": _fr(invisible=[1, 2, 3])}
    reconcile._alert_on_drift(report)
    assert sent[0][2]["severity"] == "critical"
    assert "at_risk=1" in sent[0][1]

    sent.clear()
    report2 = {"ok": False, "complete": True, "verdict_basis": "fetch",
               "counts": dict(base_counts, vanished_docs=0, vanished_at_risk=0,
                              missing_unclassified=2),
               "fetch_reclassify": _fr(invisible=[1], unclassified=[2, 3])}
    reconcile._alert_on_drift(report2)
    assert sent[0][2]["severity"] == "critical"
    assert "未定性" in sent[0][1]


# ── 探针失败 ≠ 数据漂移：告警标题层（C1 同族，2026-08-04 B6 复核）─────────────

def _capture_alerts(monkeypatch):
    sent = []
    import opensearch_pipeline.alerting as al
    monkeypatch.setattr(al, "send_ops_alert",
                        lambda title, text, **kw: sent.append((title, text, kw)))
    return sent


def test_oss_parity_probe_error_does_not_alert_as_drift(monkeypatch):
    """★ 探针失败必须以「探针失败」为标题发，**不能**顶着「parity drift」。

    `_job_exit` 那一层早就把 error(3) 与 drift(2) 分开了（ops_monitor.py:83），
    但告警**标题**层没跟上：正文写 "errored"、标题却是 "…parity drift" + critical。
    看板 / 钉钉卡片只显标题 ⇒ 一次 DNS 解析失败被读成"数据真丢了"。

    实测来源：本机 launchd `com.fuling.ops-monitor` 日志里 24 条被抑制的 CRITICAL，
    最后一条正是 RDS 连不上（Errno 8）时发出的「OSS↔RDS image parity drift」。
    """
    from opensearch_pipeline import reconcile
    sent = _capture_alerts(monkeypatch)
    reconcile._alert_on_oss_drift(
        {"ok": False, "complete": False, "error": "OperationalError: (2003, ...)", "counts": {}})
    assert len(sent) == 1
    title, text, kw = sent[0]
    assert "探针失败" in title, f"探针失败却用了 drift 标题：{title!r}"
    assert "drift" not in title.lower(), f"标题里仍带 drift：{title!r}"
    assert kw.get("dedup_key", "").endswith(":error"), "去重槽没分开——探针失败会压掉真 drift"


def test_oss_parity_real_drift_keeps_drift_title(monkeypatch):
    """反向：真漂移仍用 drift 标题与原去重槽（防上一条把两类都改成"探针失败"）。"""
    from opensearch_pipeline import reconcile
    sent = _capture_alerts(monkeypatch)
    reconcile._alert_on_oss_drift({"ok": False, "complete": True, "counts": {"missing": 7, "orphan": 2}})
    title, text, kw = sent[0]
    assert title == "OSS↔RDS image parity drift"
    assert kw.get("dedup_key") == "reconcile:oss-rds-parity"
    assert "**7**" in text


def test_ha3_parity_probe_error_does_not_alert_as_drift(monkeypatch):
    """RDS↔HA3 那条同款（日志里 18/24 条被抑制的 CRITICAL 都是它）。"""
    from opensearch_pipeline import reconcile
    sent = _capture_alerts(monkeypatch)
    reconcile._alert_on_drift({"ok": False, "complete": False,
                                      "error": "OperationalError: (2003, ...)", "counts": {}})
    title, _text, kw = sent[0]
    assert "探针失败" in title and "drift" not in title.lower(), title
    assert kw.get("dedup_key", "").endswith(":error")


def test_every_parity_alert_splits_probe_error_from_drift():
    """🔴 守卫：监控模块里**每一个** `_alert_on*` 函数都必须把「探针失败」与「数据事件」
    分成两个标题 + 两个 dedup_key。

    加新告警时最容易照抄旧模板 —— 而旧模板正是「正文说 errored、标题说 drift/breach」。
    2026-08-04 独立核验证明旧守卫有三条旁路（关键字传参 / `alerting.send_ops_alert`
    属性调用 / 只扫 `*drift` 命名 + 只扫 reconcile.py —— qa_rollup 同病灶因此漏网），
    本版逐条闭合。判据：title 实参（位置或 `title=` 关键字）**不得是裸字符串常量或
    裸 f-string** —— 必须是条件表达式或分支赋值出的变量。语义正确性（哪个分支该配哪个
    标题）由本文件的双向行为测试钉住，本守卫只负责"新增点不许照抄单标题模板"。
    """
    import ast
    import pathlib
    scan = ["opensearch_pipeline/reconcile.py", "opensearch_pipeline/qa_rollup.py"]
    allow = {"_alert_on_duration"}   # 耗时超标不是「探针失败 vs 数据事件」二分，天然单标题
    bad, seen = [], 0
    for path in scan:
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("_alert_on")
                    and fn.name not in allow):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                callee = (getattr(node.func, "id", "")            # send_ops_alert(...)
                          or getattr(node.func, "attr", ""))      # alerting.send_ops_alert(...)
                if callee != "send_ops_alert":
                    continue
                title = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg == "title"), None)
                seen += 1
                if title is None or isinstance(title, (ast.Constant, ast.JoinedStr)):
                    bad.append(f"{path}::{fn.name} (line {node.lineno})")
    assert seen >= 4, f"只扫到 {seen} 个告警调用（应含 oss/raw/ha3/qa_rollup ≥4）——守卫可能已失效"
    assert not bad, ("这些告警没把探针失败与数据事件分开（标题是裸常量/裸 f-string，"
                     "或无法定位 title 实参）：\n  " + "\n  ".join(bad))
