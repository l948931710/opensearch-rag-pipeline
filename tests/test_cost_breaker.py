# -*- coding: utf-8 -*-
"""Unit tests for the VLM/OCR cost-ceiling breaker (extraction/cost_breaker.py).

All runnable with no real API/DB: the estimator is pure and quarantine_for_cost
honors simulate_db=True (writes nothing).
"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from opensearch_pipeline.config import RebuildConfig
from opensearch_pipeline.extraction.cost_breaker import (
    CostBreaker,
    estimate_doc_cost,
    quarantine_for_cost,
    gate_vlm_rebuild,
)


def make_cfg(**rb):
    """Stub PipelineConfig exposing only .rebuild + .ocr (what the breaker reads)."""
    defaults = dict(enabled=True, max_pages=50, doc_budget_rmb=5.0,
                    run_budget_rmb=200.0, ocr_page_rmb=0.06, vlm_image_rmb=0.04)
    defaults.update(rb)
    return SimpleNamespace(rebuild=RebuildConfig(**defaults),
                           ocr=SimpleNamespace(max_ocr_pages=5))


# ── estimator (pure) ──────────────────────────────────────────────
def test_estimate_math():
    cfg = make_cfg()
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=cfg, ocr_page_count=5)
    assert est.est_cost_rmb == pytest.approx(10 * 0.04 + 5 * 0.06)  # 0.70
    assert est.billable_units == 15
    assert est.raw_units == 15
    assert est.breakdown == {"vlm_image": 10, "ocr_page": 5}


def test_cache_aware_zero_cost():
    cfg = make_cfg()
    est = estimate_doc_cost("docx", unit_count=20, cached_count=20, cfg=cfg)
    assert est.billable_units == 0
    assert est.est_cost_rmb == 0.0
    allowed, reason = CostBreaker(cfg).check("d", est)
    assert allowed and reason is None


def test_cache_aware_partial():
    cfg = make_cfg()
    est = estimate_doc_cost("xlsx", unit_count=20, cached_count=15, cfg=cfg)
    assert est.billable_units == 5
    assert est.est_cost_rmb == pytest.approx(0.20)


# ── per-doc gates ─────────────────────────────────────────────────
def test_per_doc_deny_budget():
    # max_pages high so the BUDGET gate (not the hard-cap gate) is what fires
    cfg = make_cfg(doc_budget_rmb=0.5, max_pages=1000)
    est = estimate_doc_cost("pdf", unit_count=100, cached_count=0, cfg=cfg)  # 4.0 RMB
    allowed, reason = CostBreaker(cfg).check("d", est)
    assert not allowed
    assert "per-doc budget" in reason


def test_per_doc_deny_max_pages_cache_independent():
    cfg = make_cfg(max_pages=50)
    # all cached → billable 0, cost 0, but raw_units 60 > 50 → still DENY
    est = estimate_doc_cost("pdf", unit_count=60, cached_count=60, cfg=cfg)
    assert est.est_cost_rmb == 0.0
    allowed, reason = CostBreaker(cfg).check("d", est)
    assert not allowed
    assert "hard cap" in reason


# ── run-level circuit breaker (atomic try_reserve) ────────────────
def test_run_budget_trips_at_cap():
    cfg = make_cfg(run_budget_rmb=1.2)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=cfg)  # 0.40
    assert br.try_reserve("d1", est)[0]   # 0.40
    assert br.try_reserve("d2", est)[0]   # 0.80
    a3, _ = br.try_reserve("d3", est)     # 1.20 == cap → allowed and trips
    assert a3 and br.tripped
    a4, r4 = br.try_reserve("d4", est)
    assert not a4 and r4.startswith("RUN budget exhausted")


def test_big_doc_does_not_overtrip():
    # #10d: a doc too big for the REMAINING budget is denied but must NOT permanently
    # trip the breaker — a smaller later doc that still fits is allowed.
    cfg = make_cfg(run_budget_rmb=1.0)
    br = CostBreaker(cfg)
    big = estimate_doc_cost("pdf", unit_count=20, cached_count=0, cfg=cfg)  # 0.80
    assert br.try_reserve("d1", big)[0]   # 0.80
    too_big = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=cfg)  # 0.40 → 1.20 > 1.0
    a2, r2 = br.try_reserve("d2", too_big)
    assert not a2 and "exceed RUN budget" in r2
    assert not br.tripped, "one over-remaining-budget doc must not permanently trip the breaker"
    small = estimate_doc_cost("pdf", unit_count=0, cached_count=0, cfg=cfg, ocr_page_count=3)  # 0.18 → 0.98
    assert br.try_reserve("d3", small)[0], "smaller later doc fitting remaining budget must still pass"


def test_try_reserve_atomic_no_overshoot():
    # #10b: budget admits exactly 10 docs of 0.04; 100 threads race → exactly 10 allowed,
    # cumulative never exceeds the cap (atomic check+reserve, no TOCTOU overshoot).
    cfg = make_cfg(run_budget_rmb=0.40)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=1, cached_count=0, cfg=cfg)  # 0.04
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda i: br.try_reserve(f"d{i}", est)[0], range(100)))
    assert sum(results) == 10
    assert br.run_total_rmb <= 0.40 + 1e-9


def test_refund_returns_budget():
    # #10c: a reserved-but-unspent estimate is refunded so it doesn't consume the run budget.
    cfg = make_cfg(run_budget_rmb=1.0)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=cfg)  # 0.40
    assert br.try_reserve("d", est)[0]
    assert br.run_total_rmb == pytest.approx(0.40)
    br.refund("d", est)
    assert br.run_total_rmb == pytest.approx(0.0)


def test_refund_untrips_when_back_under_cap():
    cfg = make_cfg(run_budget_rmb=0.40)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=cfg)  # 0.40 == cap
    assert br.try_reserve("d", est)[0] and br.tripped
    br.refund("d", est)
    assert not br.tripped


def test_refund_of_unreserved_doc_does_not_wipe_others():
    # hardened refund: refunding a doc_id that never reserved must be a no-op, not decrement
    # the shared run total (which would silently wipe other docs' reservations).
    cfg = make_cfg(run_budget_rmb=10.0)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=cfg)  # 0.40
    assert br.try_reserve("d1", est)[0]
    br.refund("d2_never_reserved", est)
    assert br.run_total_rmb == pytest.approx(0.40), "orphan refund must not touch other reservations"
    br.refund("d1", est)  # legitimate refund
    assert br.run_total_rmb == pytest.approx(0.0)


def test_per_doc_cumulative_across_calls():
    # #10a: the SAME doc reserved twice (rebuild + refine) shares one per-doc budget,
    # so combined spend can't exceed doc_budget (was: each call checked independently → 2x).
    cfg = make_cfg(doc_budget_rmb=0.5, max_pages=1000, run_budget_rmb=100.0)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=0, cached_count=0, cfg=cfg, ocr_page_count=5)  # 0.30
    a1, _ = br.try_reserve("d", est)   # 0.30 <= 0.5
    a2, r2 = br.try_reserve("d", est)  # 0.30+0.30=0.60 > 0.5 → denied
    assert a1 and not a2 and "per-doc" in r2


def test_run_alert_once():
    cfg = make_cfg(run_budget_rmb=0.30)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=0, cached_count=0, cfg=cfg, ocr_page_count=5)  # 0.30 == cap
    assert br.try_reserve("d", est)[0] and br.tripped
    assert br.maybe_alert_run_tripped() is True
    assert br.maybe_alert_run_tripped() is False


# ── flag-off no-op (critical: ships before VLM rebuild exists) ────
def test_noop_when_disabled():
    cfg = make_cfg(enabled=False)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=10_000, cached_count=0, cfg=cfg)
    allowed, reason = br.check("d", est)
    assert allowed and reason is None
    br.record("d", est, allowed)
    assert br.run_total_rmb == 0.0


# ── quarantine in simulate mode touches no DB ─────────────────────
def test_quarantine_simulate_no_db(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn

    def _boom(*a, **k):
        raise AssertionError("_get_db_conn must NOT be called in simulate_db=True")

    monkeypatch.setattr(pn, "_get_db_conn", _boom, raising=False)
    assert quarantine_for_cost("doc1", 1, "production", "too big", simulate_db=True) is True


def test_gate_vlm_rebuild_deny_quarantines():
    cfg = make_cfg(doc_budget_rmb=0.5)
    br = CostBreaker(cfg)
    doc = {"doc_id": "d", "version_no": 1, "file_ext": "pdf", "owner_dept": "production",
           "unit_count": 100, "cached_count": 0, "ocr_page_count": 0}
    allowed, est = gate_vlm_rebuild(br, doc, simulate_db=True)
    assert not allowed
    assert est.est_cost_rmb == pytest.approx(4.0)
    assert br._doc_denied == 1


def test_thread_safety_record():
    cfg = make_cfg(run_budget_rmb=10_000.0)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=1, cached_count=0, cfg=cfg)  # 0.04
    n = 500
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda i: br.record(f"d{i}", est, True), range(n)))
    assert br.run_total_rmb == pytest.approx(n * 0.04)


# ── P2-10: drain 循环共享同一 CostBreaker（run 预算跨批累加） ──────
def test_drain_loop_shares_one_breaker_and_budget_accumulates(monkeypatch):
    """run_stage_drained 必须在循环外建一次 CostBreaker 并逐批传给 run_stage——
    此前每批 run_stage 内部新建实例把 _run_total_rmb 归零，run_budget_rmb 门每批
    重置，整个 drain 的聚合花费上界被击穿为 批数×预算。本测试跑两批：每批预留
    0.60 RMB（run 预算 1.00），第一批放行、第二批必须因跨批累计超支被拒。"""
    from unittest.mock import patch

    import opensearch_pipeline.dataworks_orchestrator as orch

    cfg = make_cfg(run_budget_rmb=1.0, doc_budget_rmb=1.0, max_pages=1000)
    # run_stage_drained 用 get_config() 构造共享熔断器；换成启用了 rebuild 的 stub
    monkeypatch.setattr(orch, "get_config", lambda: cfg)
    # B1a（2026-07-25）：run_stage_drained 现在会在非 simulate 下取 A12 单实例互斥锁
    # （现网入口是 DataWorks 节点直调 drained，锁必须在这里）。本用例只验成本熔断器，
    # 用的是不含 rds 的 stub 配置 → 显式关掉互斥，避免 fail-closed 退出。
    monkeypatch.setenv("RAG_INGEST_SINGLETON_LOCK", "false")

    breakers, results = [], []

    def fake_run_stage(stage, bizdate, simulate, cost_breaker=None):
        assert isinstance(cost_breaker, CostBreaker), "drain 必须注入共享熔断器"
        breakers.append(cost_breaker)
        est = estimate_doc_cost("pdf", unit_count=15, cached_count=0, cfg=cfg)  # 0.60 RMB
        allowed, _reason = cost_breaker.try_reserve(f"doc-batch{len(breakers)}", est)
        results.append(allowed)
        return {}

    remaining = iter([2, 1, 0])  # 两批后排空（每批 -1，无 no-progress 触发）
    with patch.object(orch, "_count_pending_rows", lambda stage: next(remaining)), \
         patch.object(orch, "run_stage", fake_run_stage):
        orch.run_stage_drained(stage=1, bizdate="20260705", simulate=False)

    assert len(breakers) == 2
    assert breakers[0] is breakers[1], "两批必须拿到同一个 CostBreaker 实例"
    assert results == [True, False], (
        "0.60 放行后，第二批 0.60+0.60 > 1.00 必须被运行级预算拒绝（旧行为每批归零会双双放行）"
    )
    assert breakers[0].run_total_rmb == pytest.approx(0.60)


# ── ultra P1（2026-07-17）：瞬态共享预算拒绝不封存健康文档 ──────────────────
def test_gate_transient_run_budget_deny_does_not_quarantine(monkeypatch):
    """RUN 预算瞬态耗尽（共享状态）拒绝**不**封存——文档保持可重认领，下一 run 预算清零后正常
    处理。此前对任何 deny 都 quarantine（retry_count=3 永久隔离 + kb_type=private），一次预算
    打满就永久剔除一批只含空白/封面页的健康 PDF。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    calls = []
    monkeypatch.setattr(cb, "quarantine_for_cost", lambda *a, **k: calls.append(a) or True)
    cfg = make_cfg(run_budget_rmb=0.04, doc_budget_rmb=5.0)
    br = CostBreaker(cfg)
    seed = estimate_doc_cost("pdf", unit_count=1, cached_count=0, cfg=cfg)  # 0.04 → 打满 run 预算
    assert br.try_reserve("seed", seed)[0] and br.tripped
    doc = {"doc_id": "healthy", "version_no": 1, "file_ext": "pdf", "owner_dept": "production",
           "unit_count": 1, "cached_count": 0, "ocr_page_count": 0}
    allowed, _ = gate_vlm_rebuild(br, doc, simulate_db=True)
    assert not allowed
    assert calls == [], "RUN 预算瞬态耗尽绝不封存健康文档"


def test_gate_doc_intrinsic_deny_still_quarantines(monkeypatch):
    """对照：文档自身超 per-doc 预算（doc-intrinsic）仍封存——文档确实不可提取。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    calls = []
    monkeypatch.setattr(cb, "quarantine_for_cost", lambda *a, **k: calls.append(a) or True)
    cfg = make_cfg(doc_budget_rmb=0.5)
    br = CostBreaker(cfg)
    doc = {"doc_id": "toobig", "version_no": 1, "file_ext": "pdf", "owner_dept": "production",
           "unit_count": 100, "cached_count": 0, "ocr_page_count": 0}  # est 4.0 > 0.5
    allowed, _ = gate_vlm_rebuild(br, doc, simulate_db=True)
    assert not allowed
    assert len(calls) == 1, "doc-intrinsic 超限必须封存"


def test_is_transient_cost_deny_classification():
    """分类函数与 try_reserve 各 return 文案的耦合契约锁定。"""
    from opensearch_pipeline.extraction.cost_breaker import _is_transient_cost_deny
    # 瞬态（共享预算）
    assert _is_transient_cost_deny("RUN budget exhausted: cumulative 200.00 RMB >= run cap 200.00")
    assert _is_transient_cost_deny("would exceed RUN budget: cumulative 199 + 5 > run cap 200")
    assert _is_transient_cost_deny("DAILY budget exhausted (shared ledger): today 50 + 5 > cap 50")
    # doc-intrinsic（文档自身）
    assert not _is_transient_cost_deny("per-doc budget exceeded for d: reserved 4 + 2 > 5.00 RMB")
    assert not _is_transient_cost_deny("unit count 100 exceeds per-doc hard cap 50")
    assert not _is_transient_cost_deny("VLM rebuild est 4.00 RMB > per-doc budget 0.50 RMB")
    assert not _is_transient_cost_deny(None)


def test_gate_deny_out_reports_transient_and_reason(monkeypatch):
    """deny_out 出参契约（ultra P1 纠偏 2026-07-17）：拒绝路径回填 reason 原文 + transient 定性，
    供 vlm_rebuilder 分流 cost_deferred（瞬态）/cost_quarantined（doc-intrinsic）。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.setattr(cb, "quarantine_for_cost", lambda *a, **k: True)
    # 瞬态：RUN 预算被 seed 打满
    cfg = make_cfg(run_budget_rmb=0.04, doc_budget_rmb=5.0)
    br = CostBreaker(cfg)
    seed = estimate_doc_cost("pdf", unit_count=1, cached_count=0, cfg=cfg)
    assert br.try_reserve("seed", seed)[0] and br.tripped
    info = {}
    allowed, _ = gate_vlm_rebuild(
        br, {"doc_id": "h", "version_no": 1, "file_ext": "pdf", "owner_dept": "production",
             "unit_count": 1, "cached_count": 0, "ocr_page_count": 0},
        simulate_db=True, deny_out=info)
    assert not allowed
    assert info["transient"] is True and "RUN budget exhausted" in info["reason"]
    # doc-intrinsic：超 per-doc 预算
    cfg2 = make_cfg(doc_budget_rmb=0.5)
    info2 = {}
    allowed2, _ = gate_vlm_rebuild(
        CostBreaker(cfg2),
        {"doc_id": "big", "version_no": 1, "file_ext": "pdf", "owner_dept": "production",
         "unit_count": 100, "cached_count": 0, "ocr_page_count": 0},
        simulate_db=True, deny_out=info2)
    assert not allowed2
    assert info2["transient"] is False


# ── B1 P2-10（生产级外审 2026-07-17）：日账本不可用 fail-closed ──────────────


def _est_small(cfg):
    return estimate_doc_cost("pdf", unit_count=1, cached_count=0, cfg=cfg)  # 0.04 RMB


def test_b1_daily_ledger_unavailable_fail_closed_transient(monkeypatch):
    """日预算已配置 + 账本不可用（非模拟）→ 瞬态拒绝（cost_deferred 顺延，不封存），
    不再静默跳过日闸。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.delenv("RAG_COST_DAILY_LEDGER_FAILOPEN", raising=False)
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: None)
    monkeypatch.setattr(cb, "_simulate_db_active", lambda: False)
    breaker = CostBreaker(make_cfg(daily_budget_rmb=10.0))
    ok, reason = breaker.try_reserve("D-b1a", _est_small(breaker.cfg))
    assert ok is False and "DAILY budget ledger unavailable" in reason
    assert cb._is_transient_cost_deny(reason) is True        # → cost_deferred，不封存


def test_b1_daily_ledger_unavailable_failopen_escape(monkeypatch):
    """逃生口 RAG_COST_DAILY_LEDGER_FAILOPEN=true 还原旧「跳过日闸」行为。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.setenv("RAG_COST_DAILY_LEDGER_FAILOPEN", "true")
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: None)
    monkeypatch.setattr(cb, "_simulate_db_active", lambda: False)
    monkeypatch.setattr(cb, "_ledger_add", lambda amt: None)
    breaker = CostBreaker(make_cfg(daily_budget_rmb=10.0))
    ok, reason = breaker.try_reserve("D-b1b", _est_small(breaker.cfg))
    assert ok is True and reason is None


def test_b1_daily_ledger_unavailable_simulate_still_skips(monkeypatch):
    """simulate（账本本就不存在）维持跳闸——本地 make sim / 单测零回归。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.delenv("RAG_COST_DAILY_LEDGER_FAILOPEN", raising=False)
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: None)
    monkeypatch.setattr(cb, "_simulate_db_active", lambda: True)
    monkeypatch.setattr(cb, "_ledger_add", lambda amt: None)
    breaker = CostBreaker(make_cfg(daily_budget_rmb=10.0))
    ok, reason = breaker.try_reserve("D-b1c", _est_small(breaker.cfg))
    assert ok is True and reason is None


def test_b1_no_daily_cap_ledger_error_ignored(monkeypatch):
    """daily_budget=0（默认关）时日闸整体不评估——账本故障零影响。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.delenv("RAG_COST_DAILY_LEDGER_FAILOPEN", raising=False)
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: None)
    monkeypatch.setattr(cb, "_simulate_db_active", lambda: False)
    monkeypatch.setattr(cb, "_ledger_add", lambda amt: None)
    breaker = CostBreaker(make_cfg())          # daily_budget_rmb 默认 0
    ok, reason = breaker.try_reserve("D-b1d", _est_small(breaker.cfg))
    assert ok is True and reason is None


# ── γ2（M15，Majors 批次 γ，codex 共识 2026-07-21）：熔断接 ops 告警 ──────────


def _spy_ops_alert(monkeypatch):
    """替换 alerting.send_ops_alert 为 spy（_maybe_ops_alert lazy import 该模块名）。"""
    import opensearch_pipeline.alerting as alerting
    calls = []
    monkeypatch.setattr(alerting, "send_ops_alert",
                        lambda title, text, **kw: calls.append((title, text, kw)) or True)
    return calls


def test_g2_alert_flag_off_by_default(monkeypatch):
    """默认（RAG_COST_ALERT_ENABLE 未设）：任何触发点零 ops 调用——byte-identical。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.delenv("RAG_COST_ALERT_ENABLE", raising=False)
    calls = _spy_ops_alert(monkeypatch)
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: 999.0)   # DAILY 必打满
    monkeypatch.setattr(cb, "_simulate_db_active", lambda: False)
    breaker = CostBreaker(make_cfg(daily_budget_rmb=10.0))
    ok, reason = breaker.try_reserve("D-g2a", _est_small(breaker.cfg))
    assert ok is False and "DAILY budget exhausted" in reason
    # RUN 触发面同验
    cfg = make_cfg(run_budget_rmb=0.04)
    br = CostBreaker(cfg)
    assert br.try_reserve("seed", _est_small(cfg))[0] and br.tripped
    assert br.maybe_alert_run_tripped() is True
    assert calls == []


def test_g2_run_trip_alert_on(monkeypatch):
    """flag on：RUN 熔断首触 → warning + dedup_key=cost-breaker-run，恰一次。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.setenv("RAG_COST_ALERT_ENABLE", "true")
    monkeypatch.setattr(cb, "_ledger_add", lambda amt: None)
    calls = _spy_ops_alert(monkeypatch)
    cfg = make_cfg(run_budget_rmb=0.04)
    br = CostBreaker(cfg)
    assert br.try_reserve("seed", _est_small(cfg))[0] and br.tripped
    assert br.maybe_alert_run_tripped() is True
    assert br.maybe_alert_run_tripped() is False        # 二触恒 False，不重发
    assert len(calls) == 1
    _title, _text, kw = calls[0]
    assert kw["severity"] == "warning" and kw["dedup_key"] == "cost-breaker-run"


def test_g2_daily_exhausted_alert_critical(monkeypatch):
    """flag on：DAILY 打满 → critical + dedup_key=cost-breaker-daily。"""
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.setenv("RAG_COST_ALERT_ENABLE", "1")
    calls = _spy_ops_alert(monkeypatch)
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: 999.0)
    monkeypatch.setattr(cb, "_simulate_db_active", lambda: False)
    breaker = CostBreaker(make_cfg(daily_budget_rmb=10.0))
    ok, _ = breaker.try_reserve("D-g2c", _est_small(breaker.cfg))
    assert ok is False
    assert len(calls) == 1
    _title, _text, kw = calls[0]
    assert kw["severity"] == "critical" and kw["dedup_key"] == "cost-breaker-daily"


def test_g2_daily_ledger_unavailable_alert_and_failopen_send(monkeypatch):
    """flag on：账本不可读 fail-closed 同发 critical；告警自身抛异常绝不改变拒绝判定。"""
    import opensearch_pipeline.alerting as alerting
    import opensearch_pipeline.extraction.cost_breaker as cb
    monkeypatch.setenv("RAG_COST_ALERT_ENABLE", "yes")
    monkeypatch.delenv("RAG_COST_DAILY_LEDGER_FAILOPEN", raising=False)
    monkeypatch.setattr(cb, "_ledger_read_today", lambda: None)
    monkeypatch.setattr(cb, "_simulate_db_active", lambda: False)

    def _boom(title, text, **kw):
        raise RuntimeError("webhook down")

    monkeypatch.setattr(alerting, "send_ops_alert", _boom)
    breaker = CostBreaker(make_cfg(daily_budget_rmb=10.0))
    ok, reason = breaker.try_reserve("D-g2d", _est_small(breaker.cfg))
    assert ok is False and "DAILY budget ledger unavailable" in reason   # 判定不受告警异常影响
