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
