# -*- coding: utf-8 -*-
"""
qa_facts.py — qa_session_log 检索/引用文档的物化事实行（perf#3，schema/013）

背景：看板/缺口归属链（retrieved_docs_json→doc_id→document_meta.owner_dept）此前在
6 处热查询里每次现场 JSON_TABLE 展开 + `CONVERT(... ) COLLATE utf8mb4_unicode_ci` 跨库
JOIN——JSON_TABLE 本身不可索引，窗口内每行都要解析 JSON，成本随 qa_session_log 无界增长。
qa_logger 写日志时本就持有 retrieved_docs/cited_docs 列表，顺手物化一张瘦事实表
`qa_retrieved_doc(message_id, doc_id, cited, created_at)`（显式 utf8mb4_unicode_ci，
与 qa_session_log.message_id / document_meta.doc_id 两侧对齐），归属查询变普通索引 JOIN，
同时消除对 JSON_TABLE collation 陷阱（1267）的持续依赖面。

写侧（qa_logger.log_qa_session）：审计行落库后 best-effort `INSERT IGNORE` 瘦行——
  · fail-open：任何异常只 warning，绝不影响主日志写入；
  · 表缺失（1146 = schema/013 未应用）→ 进程内熔断，不再逐条尝试；
  · PK(message_id, doc_id, cited) 天然去 chunk 扇出（top_k=7 中同 doc 多 chunk 只落一行）。

读侧（routes/kb_console、routes/contribution）：`qa_docs_join_sql()` 统一出 JOIN 片段——
  · RAG_QA_FACT_JOIN=true 且表探测通过 → 事实表索引 JOIN（别名 jt/m 与旧片段一致，
    调用方 WHERE/GROUP BY 原样复用）；
  · 否则回退既有 JSON_TABLE+collation-cast 片段（默认；未迁移环境零行为变化）。
  探测带 300s TTL：迁移在运行中途 apply 也能被拾起，无需重启。

部署顺序（user-gated）：apply schema/013（建表+存量回填）→ 部署含本模块的包 →
SAE 环境注入 RAG_QA_FACT_JOIN=true → （可选）重跑 013 的回填段补部署窗口空档。
留存：retention.py 的 qa_facts 作业与 qa_rows 同窗（默认 18 个月）。
"""

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FACT_TABLE = "qa_retrieved_doc"


def _op_db() -> str:
    """问答运营库名（qa_session_log/qa_retrieved_doc 所在库）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.operation_database


def _kb_db() -> str:
    """知识库库名（document_meta 所在库）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.database


# ── 写侧：进程内 1146 熔断（schema/013 未应用的环境只付一次失败往返）──────────────
_write_disabled = False


def insert_qa_doc_facts(conn, message_id: str,
                        retrieved_docs: Optional[List[Dict[str, Any]]],
                        cited_docs: Optional[List[Dict[str, Any]]]) -> None:
    """把本次回答的 (message_id, doc_id, cited) 瘦行 INSERT IGNORE 进事实表（fail-open）。

    调用方（qa_logger）已提交主审计行；本函数用同一连接独立小事务追加，失败仅 warning、
    自行 rollback，绝不波及已落库的 qa_session_log 行。
    """
    global _write_disabled
    if _write_disabled or not message_id:
        return
    rows = []
    seen = set()
    for docs, flag in ((retrieved_docs, 0), (cited_docs, 1)):
        for d in docs or []:
            did = (d.get("doc_id") or "").strip() if isinstance(d, dict) else ""
            if did and (did, flag) not in seen:
                seen.add((did, flag))
                rows.append((message_id, did, flag))
    if not rows:
        return
    try:
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT IGNORE INTO {_op_db()}.{FACT_TABLE} (message_id, doc_id, cited) "
                "VALUES (%s, %s, %s)",
                rows)
        conn.commit()
    except Exception as e:
        errno = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
        try:
            conn.rollback()
        except Exception:
            pass
        if errno == 1146:
            _write_disabled = True
            logger.warning(
                "qa_retrieved_doc 表缺失（schema/013 未应用）——本进程停用事实行物化，"
                "看板归属继续走 JSON_TABLE 回退: %s", e)
        else:
            logger.warning("qa_retrieved_doc 事实行写入失败 (non-fatal): message_id=%s, %s",
                           message_id, e)


# ── 读侧：flag + 表探测（300s TTL）───────────────────────────────────────────────
_probe_state = {"ts": 0.0, "ok": False}
_probe_lock = threading.Lock()
_PROBE_TTL_S = 300.0


def _fact_state_clear() -> None:
    """清空探测缓存与写侧熔断（conftest 每测调用，防跨测串状态）。"""
    global _write_disabled
    with _probe_lock:
        _probe_state["ts"] = 0.0
        _probe_state["ok"] = False
    _write_disabled = False


def _probe_fact_table() -> bool:
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT 1 FROM {_op_db()}.{FACT_TABLE} LIMIT 1")
                cur.fetchall()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("qa_retrieved_doc 探测失败（本轮回退 JSON_TABLE 路径）: %s", e)
        return False


def fact_join_enabled() -> bool:
    """读侧总开关：RAG_QA_FACT_JOIN 显式开启 + 事实表探测通过（防 flag 先于迁移打开）。"""
    if os.environ.get("RAG_QA_FACT_JOIN", "").strip().lower() not in ("1", "true", "yes"):
        return False
    now = time.time()
    with _probe_lock:
        if now - _probe_state["ts"] < _PROBE_TTL_S:
            return _probe_state["ok"]
    ok = _probe_fact_table()
    with _probe_lock:
        _probe_state["ts"] = now
        _probe_state["ok"] = ok
    return ok


def qa_docs_join_sql(cited: bool = False) -> str:
    """qa_session_log 别名 q → 文档归属的 JOIN 片段（jt=doc_id 行集，m=document_meta）。

    两种形态对调用方透明（别名/列名一致，WHERE/GROUP BY 原样复用）：
      事实表：JOIN qa_retrieved_doc jt ON jt.message_id=q.message_id AND jt.cited=<flag>
              JOIN document_meta m ON m.doc_id = jt.doc_id            —— 全索引，无 JSON 解析
      回退  ：JOIN JSON_TABLE(q.<col>, ...) jt
              JOIN document_meta m ON m.doc_id = CONVERT(jt.doc_id ...) —— 既有行为
    """
    if fact_join_enabled():
        flag = 1 if cited else 0
        return (f" JOIN {_op_db()}.{FACT_TABLE} jt"
                f" ON jt.message_id = q.message_id AND jt.cited={flag}"
                f" JOIN {_kb_db()}.document_meta m ON m.doc_id = jt.doc_id")
    col = "cited_docs_json" if cited else "retrieved_docs_json"
    return (f" JOIN JSON_TABLE(q.{col}, '$[*]' COLUMNS(doc_id VARCHAR(100) PATH '$.doc_id')) jt"
            f" JOIN {_kb_db()}.document_meta m"
            "   ON m.doc_id = CONVERT(jt.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci")
