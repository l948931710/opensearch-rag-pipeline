# -*- coding: utf-8 -*-
"""tests/test_observability_metrics.py — Phase-2 OBS-3 (pipeline_run metrics) + OBS-5 (qa_rollup).

OBS-3: ctx counters → pipeline_run metric columns (extract/accumulate; run_finish fail-open).
OBS-5: qa_session_log → qa_daily_metrics rollup + SLO verdicts (pure compute; simulate/fail-open).
"""
from opensearch_pipeline import pipeline_run, qa_rollup


# ── OBS-3: per-run ingestion metrics ──

def test_extract_run_metrics_maps_ctx_counters():
    ctx = {
        "embedded_chunks": 120,
        "embedding_failed_chunks": 3,
        "chunk_meta_written": 130,
        "deactivated_chunks": 45,
        "published_count": 12,
        "failed_doc_versions": {("docA", 1), ("docB", 2)},  # set → len
        "irrelevant": "ignored",
    }
    m = pipeline_run.extract_run_metrics(ctx)
    assert m == {"embedded_chunks": 120, "embedding_failed_chunks": 3, "chunks_written": 130,
                 "chunks_deactivated": 45, "docs_processed": 12, "docs_failed": 2}


def test_extract_run_metrics_robust_to_junk():
    assert pipeline_run.extract_run_metrics(None) == {}
    assert pipeline_run.extract_run_metrics({}) == {}
    # bool is not a count; missing keys omitted
    assert pipeline_run.extract_run_metrics({"embedded_chunks": True}) == {}


def test_as_count_variants():
    assert pipeline_run._as_count(5) == 5
    assert pipeline_run._as_count([1, 2, 3]) == 3
    assert pipeline_run._as_count({("a", 1)}) == 1
    assert pipeline_run._as_count(None) is None
    assert pipeline_run._as_count(True) is None


def test_accumulate_metrics_sums_across_batches():
    acc = {}
    pipeline_run.accumulate_metrics(acc, {"embedded_chunks": 10, "embedding_failed_chunks": 1})
    pipeline_run.accumulate_metrics(acc, {"embedded_chunks": 5, "docs_failed": 2})
    pipeline_run.accumulate_metrics(acc, {})  # empty batch no-op
    assert acc == {"embedded_chunks": 15, "embedding_failed_chunks": 1, "docs_failed": 2}


def test_run_finish_simulate_is_noop():
    # must not touch DB / raise in simulate
    pipeline_run.run_finish("rid", "SUCCESS", metrics={"embedded_chunks": 1}, simulate=True)
    pipeline_run.run_finish(None, "SUCCESS", simulate=False)  # no run_id → no-op


# ── OBS-5: QA daily rollup ──

def _row(status="SUCCESS", blocked=0, lat=1000, score=8.0, uid="u1", sid="s1", ct="1", hit=5):
    return {"answer_status": status, "risk_blocked": blocked, "latency_ms": lat, "top_score": score,
            "user_id": uid, "session_id": sid, "conversation_type": ct, "opensearch_hit_count": hit}


def test_compute_daily_metrics_classification():
    rows = [
        _row("SUCCESS"), _row("SUCCESS"),
        _row("NO_RESULT", hit=0),
        _row("REFUSAL"),
        _row("LLM_ERROR"),
        _row("SUCCESS", blocked=1),  # risk_blocked → refusal bucket + risk_blocked_count
    ]
    m = qa_rollup.compute_daily_metrics(rows, {"answer_rate_min": 0, "no_result_rate_max": 1,
                                               "p95_latency_ms_max": 1e9, "error_rate_max": 1})
    assert m["total_queries"] == 6
    assert m["success_count"] == 2
    assert m["no_result_count"] == 1
    assert m["refusal_count"] == 2  # explicit REFUSAL + risk_blocked row
    assert m["error_count"] == 1
    assert m["risk_blocked_count"] == 1
    assert m["answer_rate"] == round(2 / 6, 4)
    assert m["slo_ok"] == 1


def test_compute_daily_metrics_percentiles_and_distincts():
    rows = [_row(lat=100, uid="a", sid="s1"), _row(lat=200, uid="b", sid="s1"),
            _row(lat=300, uid="a", sid="s2"), _row(lat=10000, uid="c", sid="s3")]
    m = qa_rollup.compute_daily_metrics(rows)
    assert m["p50_latency_ms"] in (200, 300)  # nearest-rank
    assert m["p95_latency_ms"] == 10000
    assert m["distinct_users"] == 3
    assert m["distinct_sessions"] == 3


def test_compute_daily_metrics_empty_day():
    m = qa_rollup.compute_daily_metrics([])
    assert m["total_queries"] == 0
    assert m["answer_rate"] is None and m["p95_latency_ms"] is None
    assert m["slo_ok"] == 1  # zero traffic can't breach


def test_evaluate_slos_breach_and_clean():
    th = {"answer_rate_min": 0.7, "no_result_rate_max": 0.15,
          "p95_latency_ms_max": 8000, "error_rate_max": 0.02}
    bad = qa_rollup.evaluate_slos(
        {"answer_rate": 0.5, "no_result_rate": 0.3, "p95_latency_ms": 9000, "error_rate": 0.1}, th)
    assert bad["ok"] is False and len(bad["breaches"]) == 4
    good = qa_rollup.evaluate_slos(
        {"answer_rate": 0.9, "no_result_rate": 0.05, "p95_latency_ms": 3000, "error_rate": 0.0}, th)
    assert good["ok"] is True and good["breaches"] == []
    # None metrics never breach
    assert qa_rollup.evaluate_slos({"answer_rate": None}, th)["ok"] is True


def test_compute_daily_metrics_flags_breach():
    rows = [_row("NO_RESULT", hit=0) for _ in range(10)]  # 0% answer, 100% no-result
    m = qa_rollup.compute_daily_metrics(rows)  # default thresholds
    assert m["slo_ok"] == 0
    slos = {b["slo"] for b in m["slo_breaches"]}
    assert "answer_rate_min" in slos and "no_result_rate_max" in slos


# ── P1-1（盲区审计 2026-07-05）：限流拒绝并入 SLO ──

def test_compute_daily_metrics_rejected_global_cap_breaches():
    """熔断日不再假绿：global_cap 拒绝 > 0 → 专项 breach + slo_ok=0；
    比率指标分母仍是准入量（per_min 反滥用拒绝不该压垮 answer_rate）。"""
    rows = [_row("SUCCESS"), _row("SUCCESS")]
    m = qa_rollup.compute_daily_metrics(rows, rejected={"global_cap": 1800, "per_min": 30})
    assert m["rejected_count"] == 1830
    assert m["rejected_global_cap"] == 1800
    assert m["offered_queries"] == 1832
    assert m["answer_rate"] == 1.0                       # 分母不变（论证见 docstring）
    assert m["slo_ok"] == 0
    assert {b["slo"] for b in m["slo_breaches"]} == {"global_cap_rejected"}
    assert m["slo_breaches"][0]["value"] == 1800


def test_compute_daily_metrics_non_cap_rejects_no_breach():
    """仅 per_min/per_day 拒绝（扫描器被正常拦下）：记录量但不判 breach。"""
    m = qa_rollup.compute_daily_metrics([_row("SUCCESS")], rejected={"per_min": 500})
    assert m["rejected_count"] == 500 and m["rejected_global_cap"] == 0
    assert m["slo_ok"] == 1


def test_upsert_daily_falls_back_when_reject_cols_missing():
    """schema/017 未 apply：写拒绝列报 1054 → 回退基础列集，不炸整个 rollup。"""
    sqls = []

    class _Cur:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params=None):
            sqls.append(sql)
            if "rejected_count" in sql:
                raise RuntimeError("(1054, \"Unknown column 'rejected_count' in 'field list'\")")

    class _Conn:
        def cursor(self):
            return _Cur()
        def commit(self):
            pass
        def rollback(self):
            pass

    m = qa_rollup.compute_daily_metrics([_row("SUCCESS")], rejected={"global_cap": 3})
    qa_rollup._upsert_daily(_Conn(), "2026-07-05", m, 15)
    assert len(sqls) == 2
    assert "rejected_count" in sqls[0] and "rejected_count" not in sqls[1]


def test_fetch_rejected_missing_table_fail_open():
    """qa_admission_reject 表未建：读侧按无拒绝处理，绝不挡既有汇总。"""
    class _Cur:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params=None):
            raise RuntimeError("(1146, \"Table 'fuling_operation.qa_admission_reject' doesn't exist\")")

    class _Conn:
        def cursor(self):
            return _Cur()

    assert qa_rollup._fetch_rejected(_Conn(), "2026-07-05") == {}


def test_run_rollup_simulate_is_noop(monkeypatch):
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", True)
    rep = qa_rollup.run_rollup()
    assert rep["ok"] is True and rep.get("skipped") == "simulate"


def test_run_rollup_fail_open(monkeypatch):
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("db down")))
    rep = qa_rollup.run_rollup()
    assert rep["ok"] is False and "db down" in rep["error"]


def test_qa_rollup_cli_breach_exit_code(monkeypatch):
    monkeypatch.setattr(qa_rollup, "run_rollup",
                        lambda **k: {"ok": True, "metric_date": "2026-06-15",
                                     "metrics": {"total_queries": 9, "answer_rate": 0.1,
                                                 "no_result_rate": 0.9, "p95_latency_ms": 100,
                                                 "error_rate": 0.0, "slo_ok": 0},
                                     "slo_ok": 0, "breaches": [{"slo": "answer_rate_min",
                                                                "threshold": 0.7, "value": 0.1}]})
    assert qa_rollup.main([]) == 2  # SLO breach
    monkeypatch.setattr(qa_rollup, "run_rollup", lambda **k: {"ok": False, "error": "x"})
    assert qa_rollup.main([]) == 3  # error
