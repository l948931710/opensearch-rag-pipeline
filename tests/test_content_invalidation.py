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
