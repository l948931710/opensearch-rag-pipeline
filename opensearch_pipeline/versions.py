# -*- coding: utf-8 -*-
"""versions.py — single source of pipeline component versions + per-run provenance (Phase-1 L1).

These constants pin the *code revision* of each output-shaping stage so a stored chunk / run
can be traced to (and the re-index scope derived from) the exact producer. They are the keystone
the lineage (kb_audit_log / pipeline_run), per-chunk provenance (chunk_meta.extra_json +
embedding_version), determinism (chunk_set_hash / detector_version), and affected-doc-set diff all
hang off.

BUMP the relevant constant whenever you change that component's OUTPUT (not just refactor):
  - EXTRACTOR_VERSION:      extraction/* change that alters canonical text / blocks / assets
  - CHUNKER_VERSION:        chunker.py change that alters chunk text / count / type
  - DETECTOR_VERSION:       the routing/boundary detectors specifically
                            (_CLAUSE_RE / _STEP_DETECT_RE / _detect_heading_level / node_chunk_documents routing)
  - EMBEDDING_MODEL_VERSION: embedding model / dimension / endpoint change

Pure / read-only: no DB, no prod write, no config mutation. Zero behavior change when unread.
"""
from typing import Optional

# ── component code-revision pins (bump on OUTPUT change; see module docstring) ──
EXTRACTOR_VERSION = "1.0.0"
CHUNKER_VERSION = "1.0.0"
DETECTOR_VERSION = "1.0.0"          # _CLAUSE_RE / _STEP_DETECT_RE / heading / routing detector revision
EMBEDDING_MODEL_VERSION = "text-embedding-v4"


def acl_policy_version() -> str:
    """dept→ACL组 映射策略的【内容指纹】（短 hash）。覆盖全部 5 个映射常量：
    dingtalk_identity._DEPT_NAME_TO_GROUPS / _PRODUCTION_WORKSHOP_DEPTS、
    retriever._VALID_ACL_GROUPS / _PRODUCTION_UMBRELLA_OWNERS / _DEPT_OWNER_EXPANSION。

    任一映射改动 → 版本自动变（内容 hash，无需手动 bump，杜绝忘记的失败模式）。per-doc 授权本就
    审计（kb_audit_log），缺的是「org 级 dept→组映射改动」这一维——本版本号盖进 ACL 审计行即补上。
    惰性 import 避免 import 环；任何异常 → 'unknown'（绝不因版本计算失败影响审计/服务，fail-open）。"""
    import hashlib
    import json
    try:
        from opensearch_pipeline import dingtalk_identity as _di
        from opensearch_pipeline import retriever as _rt
        payload = json.dumps(
            {
                "dept_to_groups": _di._DEPT_NAME_TO_GROUPS,
                "workshop_depts": sorted(_di._PRODUCTION_WORKSHOP_DEPTS),
                "valid_groups": sorted(_rt._VALID_ACL_GROUPS),
                "umbrella_owners": sorted(_rt._PRODUCTION_UMBRELLA_OWNERS),
                "owner_expansion": {k: sorted(v) for k, v in _rt._DEPT_OWNER_EXPANSION.items()},
            },
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return "unknown"


def git_commit() -> str:
    """Best-effort short git SHA. RAG_GIT_SHA env wins (deploy packages have no .git);
    falls back to `git rev-parse` in the repo, then 'unknown'. Never raises."""
    import os
    sha = os.environ.get("RAG_GIT_SHA")
    if sha and sha.strip():
        return sha.strip()
    try:
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def build_run_provenance(stage: Optional[int] = None, bizdate: Optional[str] = None) -> dict:
    """Per-run provenance dict: code/model versions + git sha + bizdate.

    Resolved model NAMES come from the live config factory (get_config), matching what actually
    runs (not the dataclass defaults). Read-only; safe to call anywhere. Callers stash it as
    ctx['run_provenance']; downstream consumers (per-chunk provenance, kb_audit_log, pipeline_run,
    affected-doc-set diff) read from there. Zero behavior change when unread.
    """
    embedding_model = llm_model = None
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        embedding_model = getattr(getattr(cfg, "embedding", None), "model", None)
        llm_model = getattr(getattr(cfg, "llm", None), "model", None)
    except Exception:
        # provenance is auxiliary — never let a config hiccup break the run
        pass
    return {
        "git_commit": git_commit(),
        "stage": stage,
        "bizdate": bizdate,
        "extractor_version": EXTRACTOR_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "detector_version": DETECTOR_VERSION,
        "embedding_model_version": EMBEDDING_MODEL_VERSION,
        "embedding_model": embedding_model,
        "llm_model": llm_model,
    }
