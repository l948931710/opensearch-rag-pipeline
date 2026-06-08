"""Environment bootstrap for the live HA3 RAG eval harness.

Forces the laptop-reachable PUBLIC HA3 endpoint + public DashScope (embedding + LLM),
disables simulate mode, and points at the live rebuilt table. Proven-safe pattern lifted
from scripts/validate_v2.py + scratch/bot_query_test.py (read-only against prod).

Import this module BEFORE importing anything from opensearch_pipeline, e.g.:

    from eval_harness import envboot      # noqa: F401  (side-effecting)
    from opensearch_pipeline.retriever import retrieve_and_enrich

All access to HA3 / RDS is read-only. No writes, no deletes.
"""
from __future__ import annotations

import os

# Repo root = parent of this package dir
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_envfile(path: str) -> None:
    full = os.path.join(_ROOT, path)
    if not os.path.exists(full):
        return
    for ln in open(full, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        # do not clobber values already exported by the parent shell
        k = k.strip()
        if k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def boot(table: str | None = None) -> dict:
    """Load .env + .env.production, force public endpoints + live table.

    Args:
        table: HA3 table to target. Defaults to env EVAL_TABLE, then RAG_HA3_TABLE_NAME,
               then the live table 'fuling_kb_chunks'.

    Returns a small dict of the resolved live-connection facts (no secrets).
    """
    _load_envfile(".env")
    _load_envfile(".env.production")

    # Force public, laptop-reachable, non-simulated, read paths.
    # RAG_ENVIRONMENT=test => config resolves PUBLIC dashscope base urls (LLM + embedding),
    # which are reachable from a laptop (production would pick VPC-only urls).
    os.environ["RAG_ENVIRONMENT"] = os.environ.get("EVAL_RAG_ENVIRONMENT", "test")
    os.environ["RAG_ENV"] = ""
    for k in ("RAG_SIMULATE", "RAG_SIMULATE_DB", "RAG_SIMULATE_OPENSEARCH",
              "RAG_SIMULATE_OSS", "RAG_SIMULATE_API"):
        os.environ[k] = "false"
    os.environ["RAG_HA3_ENDPOINT"] = os.environ.get(
        "EVAL_HA3_ENDPOINT", "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com"
    )

    resolved_table = (
        table
        or os.environ.get("EVAL_TABLE")
        or os.environ.get("RAG_HA3_TABLE_NAME")
        or "fuling_kb_chunks"
    )
    os.environ["RAG_HA3_TABLE_NAME"] = resolved_table

    # Mirror the DashScope key into both env names the codebase looks for.
    ds = os.environ.get("RAG_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
    if ds:
        os.environ["RAG_DASHSCOPE_API_KEY"] = ds
        os.environ["DASHSCOPE_API_KEY"] = ds

    return facts()


def facts() -> dict:
    """Return non-secret live-connection facts for logging/report headers."""
    present = lambda k: bool(os.environ.get(k))  # noqa: E731
    return {
        "ha3_endpoint": os.environ.get("RAG_HA3_ENDPOINT"),
        "ha3_instance": os.environ.get("RAG_HA3_INSTANCE_ID"),
        "ha3_table": os.environ.get("RAG_HA3_TABLE_NAME"),
        "rds_host": os.environ.get("RAG_RDS_HOST"),
        "rds_db": os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge"),
        "llm_model": os.environ.get("RAG_LLM_MODEL"),
        "embedding_model": os.environ.get("RAG_EMBEDDING_MODEL"),
        "keys_present": {
            "dashscope": present("RAG_DASHSCOPE_API_KEY") or present("DASHSCOPE_API_KEY"),
            "ha3_user": present("RAG_HA3_USER"),
            "ha3_password": present("RAG_HA3_PASSWORD"),
            "rds_password": present("RAG_RDS_PASSWORD"),
        },
        "rag_environment": os.environ.get("RAG_ENVIRONMENT"),
        "simulate": os.environ.get("RAG_SIMULATE"),
    }


# Boot on import so callers can simply `from eval_harness import envboot`.
boot()
