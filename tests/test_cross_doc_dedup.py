# -*- coding: utf-8 -*-
"""
test_cross_doc_dedup.py — Stage-1 cross-doc canonical_sha256 dedup (RAG_DEDUP_CROSS_DOC, default OFF).

Covers the permission partial-order _xd_covers (the novel logic) and the node_build_canonical
behavior: SKIP only behind a covering incumbent; WARN-and-process otherwise; no-match / flag-off /
DB-error all process normally (fail-safe).
"""
from unittest.mock import MagicMock

import opensearch_pipeline.pipeline_nodes as pn


# ── permission partial-order ─────────────────────────────────────────────────
def test_covers_public_incumbent_covers_anything():
    assert pn._xd_covers(("public", None), ("dept_internal", "finance")) is True
    assert pn._xd_covers(("public", None), ("public", None)) is True


def test_covers_dept_incumbent_does_not_cover_public_new():
    # never hide a public doc behind a dept_internal incumbent (would restrict it)
    assert pn._xd_covers(("dept_internal", "finance"), ("public", None)) is False


def test_covers_same_dept():
    assert pn._xd_covers(("dept_internal", "finance"), ("dept_internal", "finance")) is True


def test_covers_diff_dept_same_level_not_covered():
    assert pn._xd_covers(("dept_internal", "finance"), ("dept_internal", "hr")) is False


def test_covers_production_umbrella_covers_subline():
    # owner 'production' (umbrella) audience ⊇ owner 'production_mold' audience
    assert pn._xd_covers(("dept_internal", "production"), ("dept_internal", "production_mold")) is True


def test_covers_unresolved_new_audience_not_covered():
    # new doc ACL unresolved (empty audience) → conservatively NOT covered → WARN, not SKIP
    assert pn._xd_covers(("public", None), ("dept_internal", "")) is True   # public still covers
    assert pn._xd_covers(("dept_internal", "finance"), ("dept_internal", "")) is False


# ── node_build_canonical cross-doc behavior ──────────────────────────────────
class _FakeConn:
    def __init__(self, self_acl=None, incumbents=None, raise_on=None):
        self.calls = []
        self.self_acl = self_acl              # (permission_level, owner_dept) for the new doc
        self.incumbents = incumbents or []    # list of (doc_id, permission_level, owner_dept)
        self.raise_on = raise_on
        self._last = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("simulated DB error")
        self.calls.append((sql, params))
        self._last = sql

    def fetchone(self):
        if self._last and "FROM document_meta WHERE doc_id=" in self._last:
            return self.self_acl
        return None  # intra-doc skip-gate prior SELECT → no prior (version 1)

    def fetchall(self):
        if self._last and "dv.canonical_sha256=%s AND dv.doc_id<>%s" in self._last:
            return list(self.incumbents)
        return []

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _ctx(fake, monkeypatch):
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: fake)
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (MagicMock(), False))
    return {
        "extractions": [{"doc_id": "NEW", "version_no": 1, "text": "duplicate canonical content",
                         "text_length": 27, "extract_method": "native"}],
        "simulate_db": False,
        "_raw_checksum": {("NEW", 1): "rawhash"},
    }


def test_node_skips_behind_covering_incumbent(monkeypatch):
    monkeypatch.setenv("RAG_DEDUP_CROSS_DOC", "true")
    fake = _FakeConn(self_acl=("dept_internal", "finance"),
                     incumbents=[("INC_PUB", "public", None)])  # public covers finance
    ctx = _ctx(fake, monkeypatch)
    pn.node_build_canonical(ctx)
    assert all(c["doc_id"] != "NEW" for c in ctx["canonicals"])   # skipped
    skip_upd = [c for c in fake.calls if "SKIPPED_DUPLICATE" in c[0]]
    assert skip_upd and "canonical_sha256" in skip_upd[0][0]       # hash written on skipped row


def test_node_warns_and_processes_non_covering(monkeypatch):
    monkeypatch.setenv("RAG_DEDUP_CROSS_DOC", "true")
    fake = _FakeConn(self_acl=("dept_internal", "finance"),
                     incumbents=[("INC_HR", "dept_internal", "hr")])  # hr does NOT cover finance
    ctx = _ctx(fake, monkeypatch)
    pn.node_build_canonical(ctx)
    assert any(c["doc_id"] == "NEW" for c in ctx["canonicals"])    # processed
    assert not any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls)
    assert any("cross-doc content match" in w for w in ctx.get("validation_warnings", []))


def test_node_no_match_processes(monkeypatch):
    monkeypatch.setenv("RAG_DEDUP_CROSS_DOC", "true")
    fake = _FakeConn(self_acl=("dept_internal", "finance"), incumbents=[])
    ctx = _ctx(fake, monkeypatch)
    pn.node_build_canonical(ctx)
    assert any(c["doc_id"] == "NEW" for c in ctx["canonicals"])
    assert not any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls)


def test_node_flag_off_processes(monkeypatch):
    monkeypatch.delenv("RAG_DEDUP_CROSS_DOC", raising=False)
    fake = _FakeConn(self_acl=("dept_internal", "finance"),
                     incumbents=[("INC_PUB", "public", None)])
    ctx = _ctx(fake, monkeypatch)
    pn.node_build_canonical(ctx)
    assert any(c["doc_id"] == "NEW" for c in ctx["canonicals"])    # gate never ran
    # no cross-doc SELECT issued
    assert not any("dv.canonical_sha256=%s AND dv.doc_id<>%s" in c[0] for c in fake.calls)


def test_node_db_error_is_failsafe(monkeypatch):
    monkeypatch.setenv("RAG_DEDUP_CROSS_DOC", "true")
    fake = _FakeConn(self_acl=("dept_internal", "finance"),
                     incumbents=[("INC_PUB", "public", None)],
                     raise_on="FROM document_meta WHERE doc_id=")
    ctx = _ctx(fake, monkeypatch)
    pn.node_build_canonical(ctx)                                   # must not raise
    assert any(c["doc_id"] == "NEW" for c in ctx["canonicals"])   # processed normally
