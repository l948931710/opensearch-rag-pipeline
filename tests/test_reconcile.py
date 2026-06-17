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
