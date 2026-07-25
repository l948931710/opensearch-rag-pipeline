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
    零向量时代盲区是预期伪影，倒排下它意味着枚举/锚点失效，必须叫醒人。"""
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
    (_, text, kw) = sent[0]
    assert kw["severity"] == "critical"
    assert kw["dedup_key"] == "reconcile:rds-ha3-parity"
    assert "锚点不完整" in text and "enum_health=degraded" in text


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
