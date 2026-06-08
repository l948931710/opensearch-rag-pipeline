"""Live HA3 (public HTTP endpoint) + RDS read-only helpers.

Used by the index-health layer and the gold-set builder. All operations are read-only.
The HA3 client is built with protocol=HTTP against the public endpoint (the proven
laptop-reachable path from scripts/validate_v2.py + scratch/ha3_query_prod.py).
"""
from __future__ import annotations

import json
import math
import os
import random
from typing import Any, Dict, List, Optional

from . import envboot  # noqa: F401  side-effecting: forces public/live env

_client = None


def table() -> str:
    return os.environ.get("RAG_HA3_TABLE_NAME", "fuling_kb_chunks")


def client():
    """Singleton HA3 vector client over the public HTTP endpoint."""
    global _client
    if _client is not None:
        return _client
    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config
    endpoint = os.environ["RAG_HA3_ENDPOINT"].replace("http://", "").replace("https://", "")
    _client = Client(Config(
        endpoint=endpoint,
        instance_id=os.environ.get("RAG_HA3_INSTANCE_ID") or endpoint.split(".")[0],
        access_user_name=os.environ["RAG_HA3_USER"],
        access_pass_word=os.environ["RAG_HA3_PASSWORD"],
        protocol="HTTP",
    ))
    return _client


def install_into_retriever():
    """Force the production retriever to use the public-HTTP client (laptop path)."""
    from opensearch_pipeline import retriever
    retriever._ha3_client = client()


# ── response parsing ────────────────────────────────────────────────────

def tomap(resp) -> Any:
    b = getattr(resp, "body", resp)
    if isinstance(b, str):
        try:
            return json.loads(b)
        except Exception:
            return b
    return b.to_map() if hasattr(b, "to_map") else b


def parse(resp) -> List[Dict]:
    b = tomap(resp)
    raw = b.get("result", b.get("hits", b.get("data", []))) if isinstance(b, dict) else []
    if isinstance(raw, dict):
        raw = raw.get("hits") or raw.get("items") or []
    return raw if isinstance(raw, list) else []


def fields_of(item: Dict) -> Dict:
    return item.get("fields", item)


# ── index health primitives ─────────────────────────────────────────────

def status_and_stats(tbl: Optional[str] = None) -> Dict:
    t = tbl or table()
    cli = client()
    gt = tomap(cli.get_table(t))
    st = tomap(cli.stats(t))
    res = (gt.get("result") or {}) if isinstance(gt, dict) else {}
    stats = (st.get("result") or {}) if isinstance(st, dict) else {}
    parts = stats.get("partitions") or []
    return {
        "table": t,
        "status": res.get("status"),
        "docCount": stats.get("totalDocCount"),
        "partitions": [
            {"name": p.get("name"), "docCount": p.get("docCount"),
             "segmentCount": p.get("segmentCount")}
            for p in parts
        ],
        "segments_ok": bool(parts) and all((p.get("segmentCount") or 0) > 0 for p in parts),
    }


def query_vector(dense: List[float], *, sparse=None, top_k: int = 5,
                 include_vector: bool = False, filter: Optional[str] = None,
                 order: str = "DESC", output_fields: Optional[List[str]] = None,
                 tbl: Optional[str] = None) -> List[Dict]:
    """kNN query by an explicit dense (+optional sparse) vector. order=DESC for InnerProduct."""
    from alibabacloud_ha3engine_vector.models import QueryRequest
    q = QueryRequest(
        table_name=tbl or table(),
        vector=dense,
        sparse_data=sparse,
        top_k=top_k,
        include_vector=include_vector,
        order=order,
        output_fields=output_fields or ["chunk_id", "doc_id", "title"],
        filter=filter,
    )
    return parse(client().query(q))


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ── RDS read-only helpers ────────────────────────────────────────────────

def rds_conn():
    import pymysql
    return pymysql.connect(
        host=os.environ["RAG_RDS_HOST"],
        port=int(os.environ.get("RAG_RDS_PORT", "3306")),
        user=os.environ["RAG_RDS_USER"],
        password=os.environ["RAG_RDS_PASSWORD"],
        database=os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge"),
        connect_timeout=10, read_timeout=30, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def doc_inventory() -> List[Dict]:
    """Live documents that have active chunks: doc_id, title, dept, permission, category, n_active."""
    sql = (
        "SELECT dm.doc_id, dm.title, dm.original_filename, dm.owner_dept, "
        "dm.category_l1, dm.permission_level, COUNT(cm.id) AS n_active "
        "FROM document_meta dm "
        "JOIN chunk_meta cm ON cm.doc_id = dm.doc_id AND cm.is_active = 1 "
        "GROUP BY dm.doc_id, dm.title, dm.original_filename, dm.owner_dept, "
        "dm.category_l1, dm.permission_level "
        "ORDER BY n_active DESC"
    )
    conn = rds_conn()
    try:
        with conn.cursor() as c:
            c.execute(sql)
            return list(c.fetchall())
    finally:
        conn.close()


def sample_active_chunks(n: int, seed: int = 7) -> List[Dict]:
    """Random sample of active chunks (with title joined) for self-query / blast-radius gates."""
    sql = (
        "SELECT cm.id, cm.chunk_id, cm.doc_id, dm.title, cm.section_title, "
        "cm.owner_dept, cm.permission_level, cm.chunk_text "
        "FROM chunk_meta cm LEFT JOIN document_meta dm ON dm.doc_id = cm.doc_id "
        "WHERE cm.is_active = 1"
    )
    conn = rds_conn()
    try:
        with conn.cursor() as c:
            c.execute(sql)
            rows = list(c.fetchall())
    finally:
        conn.close()
    random.Random(seed).shuffle(rows)
    return rows[:n]


def total_active_chunks() -> int:
    conn = rds_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) AS n FROM chunk_meta WHERE is_active = 1")
            return int(c.fetchone()["n"])
    finally:
        conn.close()
