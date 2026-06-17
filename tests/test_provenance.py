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
