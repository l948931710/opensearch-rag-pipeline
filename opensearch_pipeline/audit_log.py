# -*- coding: utf-8 -*-
"""audit_log.py — append-only kb_audit_log writer (Phase-1 L5).

Revives kb_audit_log (defined in schema/001 but previously ZERO writers): an append-only record of
doc/version lifecycle transitions (REGISTER / CHUNK / INDEX / DEACTIVATE / …) for forensic lineage —
who/what/when, and which run produced or retired a version. trace_id is the run fingerprint from
ctx['run_provenance'] (L1), so an audit row joins back to the producing code/model revision.

Mirrors the qa_logger pattern deliberately: opens its OWN short-lived connection, commits, and
swallows ALL exceptions (fail-open). An audit failure must NEVER abort ingestion (CLAUDE.md
graceful-degradation invariant) and must not poison the caller's transaction — hence a separate
connection rather than the caller's cursor. No-op in simulate. Honors RAG_READONLY (the
GuardedDBConnection blocks the INSERT under PROD-RO, which is then swallowed fail-open).
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def audit_trace_id(ctx: Optional[dict]) -> Optional[str]:
    """Run-scoped trace_id from ctx['run_provenance'] (L1): '<git_commit>:<bizdate>'.
    Falls back to bizdate, then None. Never raises."""
    try:
        prov = (ctx or {}).get("run_provenance") or {}
        commit = prov.get("git_commit")
        bizdate = prov.get("bizdate") or (ctx or {}).get("bizdate")
        if commit:
            return f"{commit}:{bizdate}" if bizdate else str(commit)
        return str(bizdate) if bizdate else None
    except Exception:
        return None


def _kb_db() -> str:
    """知识库库名（document_meta/version/chunk_meta/kb_audit_log 等所在库）。
    经 RAG_RDS_DATABASE 配置（STAGING 用 fuling_knowledge_stg）。镜像 qa_logger._op_db()。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.database


def _audit_insert_sql() -> str:
    """每次调用按 config 解析库名构建 INSERT —— 故意惰性（不在 import 期读 config），
    与服务端各处 {_kb_db()}. 同步随 RAG_ENV 指向 staging/prod 库。"""
    return (
        f"INSERT INTO {_kb_db()}.kb_audit_log ("
        "trace_id, doc_id, version_no, action_type, action_result, "
        "operator_type, operator_id, oss_key, message"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )


# ACL 授权类动作（dept_internal / 跨部门 / 管理员授权的增减）——这些审计行附带 acl_policy_version。
# 文档生命周期动作（REGISTER/CHUNK/INDEX/DEACTIVATE/RETIRE_REQUEST/RESTORE_REQUEST...）不在此列，
# 不加策略版本噪声。新增 ACL 动作类型时在此登记即自动获得盖戳。
_ACL_AUDIT_ACTIONS = frozenset({
    "APPROVE", "REJECT", "REVOKE",
    "ACCESS_REQUEST_SUBMIT", "ACCESS_REQUEST_APPROVED",
    "ACCESS_REQUEST_REJECTED", "ACCESS_REQUEST_REVOKED",
    "KB_ADMIN_GRANT", "KB_ADMIN_REVOKE",
})


def write_audit(*, doc_id: Optional[str], version_no: Optional[int],
                action_type: str, action_result: str = "SUCCESS",
                trace_id: Optional[str] = None, message: Optional[str] = None,
                operator_type: str = "pipeline", operator_id: Optional[str] = None,
                oss_key: Optional[str] = None, simulate: bool = False,
                cursor=None) -> None:
    """Append one kb_audit_log row.

    Two modes:
    - cursor=None (default, INGESTION path): open OWN short-lived connection, commit, and swallow ALL
      exceptions (fail-open). An audit failure must NEVER abort ingestion (CLAUDE.md graceful-degradation)
      and must not poison the caller's transaction. No-op in simulate.
    - cursor given (SERVING endpoints): write the row via the caller's cursor in the SAME transaction as
      the privileged change — the caller commits it. **Does NOT swallow** (atomic: the audit row commits
      iff the privileged change commits; an audit-insert failure rolls the whole op back → caller returns
      500, retryable). Closes the post-commit gap where a crash between commit and audit lost the record.
    """
    if simulate:
        return
    # ACL 授权类动作：盖上当时生效的 dept→组映射策略版本（acl_policy_version）。per-doc 授权本就审计，
    # 缺的是「org 级映射改动」这一维——映射常量改一次 commit 就静默放大/收窄全员可读范围却无审计行；
    # 内容 hash 版本盖进消息后，授权时点的映射版本即可溯源（且版本随映射自动变）。仅 ACL 动作，避免
    # 在 ingestion 审计行（REGISTER/CHUNK/INDEX/...）上加噪。
    if action_type in _ACL_AUDIT_ACTIONS:
        try:
            from opensearch_pipeline.versions import acl_policy_version
            message = f"[acl_policy={acl_policy_version()}] {message or ''}".rstrip()
        except Exception:   # noqa: BLE001 — 版本盖戳失败绝不阻断审计写入
            pass
    params = (trace_id, doc_id, version_no, action_type, action_result,
              operator_type, operator_id, oss_key, message)
    if cursor is not None:
        cursor.execute(_audit_insert_sql(), params)   # 同事务、不开连接/不提交/不吞异常（原子审计）
        return
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cur:
                cur.execute(_audit_insert_sql(), params)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # append-only audit is auxiliary — never break ingest (mirrors qa_logger swallow-on-error)
        logger.warning("kb_audit_log write failed (non-fatal): action=%s doc=%s v=%s err=%s",
                       action_type, doc_id, version_no, e)
