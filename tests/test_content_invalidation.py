# -*- coding: utf-8 -*-
"""tests/test_content_invalidation.py — Phase-1 L2 (recording half): content fingerprints.

node_build_canonical now records checksum_sha256 (raw bytes) + canonical_sha256 (canonical text)
on document_version — the substrate for content-based invalidation and the affected-doc-set diff.
The skip-gate (auto-skip an unchanged re-ingest) builds on these and is a separate, fail-safe pass.
"""
import hashlib
from unittest.mock import MagicMock


class _ExecCapConn:
    """Fake conn capturing execute(sql, params)."""
    def __init__(self):
        self.calls = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_l2_canonical_build_records_content_hashes(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn

    cap = _ExecCapConn()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: cap)
    # real-OSS branch with a no-op mock bucket → no local FS writes, no real OSS
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (MagicMock(), False))

    text = "hello canonical content"
    ctx = {
        "extractions": [{
            "doc_id": "d", "version_no": 1, "text": text, "text_length": len(text),
            "extract_method": "native",
        }],
        "simulate_db": False,
        "_raw_checksum": {("d", 1): "rawhash_abc"},
    }
    pn.node_build_canonical(ctx)

    upd = [c for c in cap.calls if "UPDATE document_version" in c[0]]
    assert upd, "node_build_canonical must UPDATE document_version"
    sql, params = upd[-1]
    assert "checksum_sha256" in sql and "canonical_sha256" in sql
    assert "rawhash_abc" in params, "raw-bytes checksum must be written"
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() in params, "canonical-text hash must be written"


def test_l2_canonical_hash_is_content_sensitive(monkeypatch):
    """Different canonical text → different canonical_sha256 (the change-detection signal)."""
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (MagicMock(), False))

    def _hash_for(text):
        cap = _ExecCapConn()
        monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: cap)
        pn.node_build_canonical({
            "extractions": [{"doc_id": "d", "version_no": 1, "text": text,
                             "text_length": len(text), "extract_method": "native"}],
            "simulate_db": False,
        })
        params = [c for c in cap.calls if "UPDATE document_version" in c[0]][-1][1]
        # canonical_sha256 is the 4th param (after canonical_key, canonical_md_key, checksum_sha256)
        return params[3]

    assert _hash_for("alpha") != _hash_for("beta")
    assert _hash_for("alpha") == hashlib.sha256(b"alpha").hexdigest()


# ── L2 skip-gate (flag-gated RAG_SKIP_UNCHANGED_REINGEST, default OFF) ──

class _SkipFakeConn:
    """Returns a configured prior on the prior-version SELECT; captures all executes."""
    def __init__(self, prior=None):
        self.prior = prior  # (version_no, canonical_sha256) or None
        self.executed = []
        self._last = ""

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = sql

    def fetchone(self):
        return self.prior if "canonical_sha256 FROM document_version" in self._last else None

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _run_build(monkeypatch, *, flag, version_no, prior, text="same content"):
    import opensearch_pipeline.pipeline_nodes as pn
    if flag:
        monkeypatch.setenv("RAG_SKIP_UNCHANGED_REINGEST", "true")
    else:
        monkeypatch.delenv("RAG_SKIP_UNCHANGED_REINGEST", raising=False)
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (MagicMock(), False))
    fake = _SkipFakeConn(prior=prior)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: fake)
    ctx = {
        "extractions": [{"doc_id": "d", "version_no": version_no, "text": text,
                         "text_length": len(text), "extract_method": "native"}],
        "simulate_db": False,
    }
    pn.node_build_canonical(ctx)
    return ctx, fake


def test_skip_gate_skips_on_canonical_match(monkeypatch):
    text = "same content"
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ctx, fake = _run_build(monkeypatch, flag=True, version_no=2, prior=(1, sha), text=text)
    assert ctx["canonicals"] == [], "matched re-ingest must be excluded (skipped)"
    assert any("SKIPPED_DUPLICATE" in s for s, _ in fake.executed)
    assert any("current_version_no" in s for s, _ in fake.executed), "version pointer reverted to prior"


def test_skip_gate_processes_on_mismatch(monkeypatch):
    ctx, fake = _run_build(monkeypatch, flag=True, version_no=2, prior=(1, "different_hash"))
    assert len(ctx["canonicals"]) == 1, "mismatched content must be processed"
    assert not any("SKIPPED_DUPLICATE" in s for s, _ in fake.executed)


def test_skip_gate_processes_on_no_prior(monkeypatch):
    ctx, fake = _run_build(monkeypatch, flag=True, version_no=2, prior=None)
    assert len(ctx["canonicals"]) == 1, "no prior hash → process (fail-safe)"
    assert not any("SKIPPED_DUPLICATE" in s for s, _ in fake.executed)


def test_skip_gate_off_never_skips(monkeypatch):
    text = "same content"
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ctx, fake = _run_build(monkeypatch, flag=False, version_no=2, prior=(1, sha), text=text)
    assert len(ctx["canonicals"]) == 1, "flag OFF → never skip even on a content match"
    assert not any("SKIPPED_DUPLICATE" in s for s, _ in fake.executed)


def test_skip_gate_version1_processes(monkeypatch):
    text = "same content"
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ctx, fake = _run_build(monkeypatch, flag=True, version_no=1, prior=(1, sha), text=text)
    assert len(ctx["canonicals"]) == 1, "version 1 (not a re-ingest) → process"
    assert not any("SKIPPED_DUPLICATE" in s for s, _ in fake.executed)


def test_skip_gate_is_fail_safe_in_source():
    import inspect
    from opensearch_pipeline.pipeline_nodes import node_build_canonical
    src = inspect.getsource(node_build_canonical)
    assert "FAIL-SAFE: processing normally" in src and "_do_skip = False" in src
