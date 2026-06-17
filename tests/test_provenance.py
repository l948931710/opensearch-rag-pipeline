# -*- coding: utf-8 -*-
"""tests/test_provenance.py — Phase-1 L1: pipeline version constants + per-run provenance.

L1 is the keystone for lineage/incremental/determinism: it pins each output-shaping component's
code revision and exposes a per-run provenance dict (git sha + versions + bizdate) that downstream
provenance/audit/affected-doc-set work consumes via ctx['run_provenance'].
"""
import inspect


def test_version_constants_present_and_nonempty():
    from opensearch_pipeline import versions
    for name in ("EXTRACTOR_VERSION", "CHUNKER_VERSION", "DETECTOR_VERSION", "EMBEDDING_MODEL_VERSION"):
        v = getattr(versions, name)
        assert isinstance(v, str) and v.strip(), f"{name} must be a non-empty string"


def test_git_commit_never_empty():
    from opensearch_pipeline.versions import git_commit
    sha = git_commit()
    assert isinstance(sha, str) and sha.strip(), "git_commit must always return a non-empty string ('unknown' fallback)"


def test_git_commit_honors_env(monkeypatch):
    from opensearch_pipeline import versions
    monkeypatch.setenv("RAG_GIT_SHA", "deadbeef")
    assert versions.git_commit() == "deadbeef"


def test_build_run_provenance_has_required_keys():
    from opensearch_pipeline.versions import build_run_provenance
    p = build_run_provenance(stage=2, bizdate="20260616")
    required = {
        "git_commit", "stage", "bizdate",
        "extractor_version", "chunker_version", "detector_version",
        "embedding_model_version", "embedding_model", "llm_model",
    }
    assert required <= set(p), f"run_provenance missing keys: {required - set(p)}"
    assert p["stage"] == 2 and p["bizdate"] == "20260616"
    assert p["git_commit"]  # 'unknown' or a real sha, never empty
    assert p["chunker_version"] and p["detector_version"]


def test_orchestrator_stashes_run_provenance_in_ctx():
    """run_stage must build ctx['run_provenance'] from build_run_provenance (single trace_id source).
    Source-assert (mirrors test_j_rollback_reads_result_ctx) — avoids running a full stage."""
    from opensearch_pipeline.dataworks_orchestrator import run_stage
    src = inspect.getsource(run_stage)
    assert 'ctx["run_provenance"] = build_run_provenance(' in src, (
        "run_stage must stash per-run provenance into ctx['run_provenance']"
    )


# ── L3: per-chunk provenance + chunk_set_hash into extra_json ──

def test_l3_chunk_set_hash_deterministic_and_content_sensitive():
    from opensearch_pipeline.chunker import Chunk
    from opensearch_pipeline.pipeline_nodes import _compute_chunk_set_hashes

    def mk(i, t):
        return Chunk(chunk_id=f"d_v1_c{i:04d}", doc_id="d", version_no=1, chunk_index=i,
                     chunk_type="text_chunk", chunk_text=t, token_count=1)

    a = _compute_chunk_set_hashes([mk(0, "x"), mk(1, "y")])
    b = _compute_chunk_set_hashes([mk(1, "y"), mk(0, "x")])  # input order must not matter
    assert a == b and a[("d", 1)]
    c = _compute_chunk_set_hashes([mk(0, "x"), mk(1, "Y")])  # content change flips it
    assert c[("d", 1)] != a[("d", 1)]


class _CaptureConn:
    """Minimal fake conn capturing chunk_meta INSERT rows (tolerates the status-closure UPDATEs)."""
    def __init__(self):
        self.insert_rows = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass  # DELETE / status-closure UPDATE — ignored

    def executemany(self, sql, rows):
        if "INSERT INTO chunk_meta" in sql:
            self.insert_rows.extend(rows)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_l3_chunk_meta_extra_json_carries_provenance_and_set_hash(monkeypatch):
    import json
    from opensearch_pipeline.chunker import Chunk
    import opensearch_pipeline.pipeline_nodes as pn

    cap = _CaptureConn()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: cap)

    chunks = [
        Chunk(chunk_id="d_v1_c0000", doc_id="d", version_no=1, chunk_index=0,
              chunk_type="text_chunk", chunk_text="alpha", token_count=1),
        Chunk(chunk_id="d_v1_c0001", doc_id="d", version_no=1, chunk_index=1,
              chunk_type="text_chunk", chunk_text="beta", token_count=1),
    ]
    ctx = {
        "valid_chunks": chunks,
        "canonicals": [{"doc_id": "d", "version_no": 1}],
        "simulate_db": False,
        "run_provenance": {
            "git_commit": "abc123", "chunker_version": "1.0.0", "detector_version": "1.0.0",
            "extractor_version": "1.0.0", "embedding_model_version": "text-embedding-v4",
            "bizdate": "20260616",
        },
    }
    pn.node_write_chunk_meta(ctx)

    assert len(cap.insert_rows) == 2, "both chunks must be inserted"
    extra0 = json.loads(cap.insert_rows[0][22])  # extra_json is column index 22 in the INSERT
    assert extra0["_provenance"]["git_commit"] == "abc123"
    assert extra0["_provenance"]["chunker_version"] == "1.0.0"
    assert extra0["_provenance"]["detector_version"] == "1.0.0"
    assert extra0["_chunk_set_hash"], "chunk_set_hash must be persisted"
    # both chunks of the same (doc,version) share one chunk_set_hash
    extra1 = json.loads(cap.insert_rows[1][22])
    assert extra1["_chunk_set_hash"] == extra0["_chunk_set_hash"]
