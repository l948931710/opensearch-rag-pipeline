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


# ── run-level circuit breaker ─────────────────────────────────────
def test_run_budget_trip():
    cfg = make_cfg(run_budget_rmb=1.0)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=cfg)  # 0.40
    a1, _ = br.check("d1", est); br.record("d1", est, a1)   # 0.40
    a2, _ = br.check("d2", est); br.record("d2", est, a2)   # 0.80
    assert a1 and a2
    a3, r3 = br.check("d3", est)                            # 0.80+0.40=1.20 > 1.0
    br.record("d3", est, a3)
    assert not a3 and br.tripped
    a4, r4 = br.check("d4", est)
    assert not a4 and r4.startswith("RUN budget exhausted")


def test_run_alert_once():
    cfg = make_cfg(run_budget_rmb=0.1)
    br = CostBreaker(cfg)
    est = estimate_doc_cost("pdf", unit_count=10, cached_count=0, cfg=cfg)  # 0.40 > 0.1
    a, _ = br.check("d", est); br.record("d", est, a)
    assert br.tripped
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
