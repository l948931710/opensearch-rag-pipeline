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


def _audit_params(*, doc_id: Optional[str], version_no: Optional[int],
                  action_type: str, action_result: str = "SUCCESS",
                  trace_id: Optional[str] = None, message: Optional[str] = None,
                  operator_type: str = "pipeline", operator_id: Optional[str] = None,
                  oss_key: Optional[str] = None) -> tuple:
    """构造一行 kb_audit_log 的参数元组（含 ACL 动作的策略版本盖戳）。

    write_audit 与 write_audit_many 共用**同一份**构造逻辑，保证两条路径写出的行逐字段一致
    （否则批量化会悄悄改变审计行内容）。

    ACL 授权类动作：盖上当时生效的 dept→组映射策略版本（acl_policy_version）。per-doc 授权本就
    审计，缺的是「org 级映射改动」这一维——映射常量改一次 commit 就静默放大/收窄全员可读范围却无
    审计行；内容 hash 版本盖进消息后，授权时点的映射版本即可溯源（且版本随映射自动变）。仅 ACL
    动作，避免在 ingestion 审计行（REGISTER/CHUNK/INDEX/...）上加噪。
    """
    if action_type in _ACL_AUDIT_ACTIONS:
        try:
            from opensearch_pipeline.versions import acl_policy_version
            message = f"[acl_policy={acl_policy_version()}] {message or ''}".rstrip()
        except Exception:   # noqa: BLE001 — 版本盖戳失败绝不阻断审计写入
            pass
    return (trace_id, doc_id, version_no, action_type, action_result,
            operator_type, operator_id, oss_key, message)


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
    params = _audit_params(
        doc_id=doc_id, version_no=version_no, action_type=action_type,
        action_result=action_result, trace_id=trace_id, message=message,
        operator_type=operator_type, operator_id=operator_id, oss_key=oss_key)
    if cursor is not None:
        cursor.execute(_audit_insert_sql(), params)   # 同事务、不开连接/不提交/不吞异常（原子审计）
        return
    try:
        from opensearch_pipeline.db import _get_db_conn
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


_AUDIT_BATCH_SIZE = 500


def write_audit_many(rows, simulate: bool = False) -> None:
    """A7（2026-07-25）：批量 append kb_audit_log（ingestion 路径专用，fail-open）。

    动机：DEACTIVATE 审计按【旧 chunk 条数】逐条调 write_audit，而 ingestion 模式每次都是
    「取连接 → INSERT → COMMIT → close」一整套；万级旧 chunk 就是万级提交（ping + BEGIN +
    INSERT + COMMIT 四个 round-trip 级操作）。本函数把 DB 往返合并，**行粒度不变**
    （N 条旧 chunk 仍写 N 行 —— 取证表的行粒度是既有契约，测试钉死）。

    失败语义（与逐条版的隔离性对齐，逐条对比过）：
      · executemany 本身异常 → rollback 后【该块逐行重试】（每行独立事务，等价旧行为）；
        rollback 自身失败 → 丢弃该连接不再复用（防把坏事务状态带到后续块）。
      · commit() 异常 → **只告警，绝不回放该块** —— 提交结果未知，而 kb_audit_log 只有自增
        主键、无任何业务唯一键，盲目回放必然可能写出重复审计行。
      · 全程吞异常（审计失败绝不打断摄取），finally 关连接。

    rows: [{doc_id, version_no, action_type, action_result?, trace_id?, message?,
            operator_type?, operator_id?, oss_key?}, ...]
    """
    if simulate or not rows:
        return
    conn = None
    try:
        from opensearch_pipeline.db import _get_db_conn
        sql = _audit_insert_sql()
        params_list = [_audit_params(**r) for r in rows]
        conn = _get_db_conn(select_db=True)
        for i in range(0, len(params_list), _AUDIT_BATCH_SIZE):
            block = params_list[i:i + _AUDIT_BATCH_SIZE]
            try:
                with conn.cursor() as cur:
                    cur.executemany(sql, block)
            except Exception as exec_err:
                logger.warning("kb_audit_log executemany failed (non-fatal, falling back to "
                               "row-by-row): rows=%d err=%s", len(block), exec_err)
                try:
                    conn.rollback()
                except Exception as rb_err:
                    # 回滚都失败 → 该连接状态不可信，丢弃后逐行各自开新连接
                    logger.warning("kb_audit_log rollback failed; discarding connection: %s", rb_err)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                for r in rows[i:i + _AUDIT_BATCH_SIZE]:
                    write_audit(**r)      # 逐行独立连接+事务，沿用既有 fail-open
                if conn is None:
                    conn = _get_db_conn(select_db=True)
                continue
            try:
                conn.commit()
            except Exception as commit_err:
                # 提交结果未知：不回放（见 docstring）。丢弃连接，后续块重新取。
                logger.warning("kb_audit_log commit failed after executemany — NOT replaying "
                               "(no idempotency key on kb_audit_log): rows=%d err=%s",
                               len(block), commit_err)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                if i + _AUDIT_BATCH_SIZE < len(params_list):
                    conn = _get_db_conn(select_db=True)
    except Exception as e:
        logger.warning("kb_audit_log batch write failed (non-fatal): rows=%d err=%s", len(rows), e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
