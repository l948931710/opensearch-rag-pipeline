# -*- coding: utf-8 -*-
"""
audit.py — 企业级 Agent 合规审计（agent_audit_log；v2 报告 §6 / §N 治理；WS1 收尾③）

与 tool_invocation（运营 trace）分工正交：本模块写**安全合规审计**——谁 × 在哪 × 对什么资源 ×
何种裁决 × 何种风险，只存 args **摘要**不存原文（PII-safe）。append-only，不可篡改。

写入语义（核心）：
- **HIGH_WRITE fail-closed**：高风险写型工具**写前审计**，审计不可写 → 抛 AuditWriteError →
  调用方（ToolExecutor）阻断执行，绝不产生无审计记录的高风险副作用（写前记录=write-ahead audit：
  进程若在审计后、执行前崩溃，留下一条“已授权但未执行”记录是安全方向——宁多记不漏记）。
- **READ_ONLY / LOW_WRITE fail-open**：审计写失败仅告警、返回 None，不阻断正常操作。

DB 访问沿用 serving/run_store 惯例：`db._get_db_conn()`、`%s` 占位、NOW(3)/DEFAULT、显式
commit/rollback/close。连接获取放在 try 内——DB 不可达（最常见失败）也能按 fail-closed/open 分流。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol, Tuple

from opensearch_pipeline.config import get_config

if TYPE_CHECKING:
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

logger = logging.getLogger(__name__)


class AuditWriteError(RuntimeError):
    """fail-closed 审计写失败：高风险操作必须阻断（绝不产生无审计的副作用）。"""


def _op_db() -> str:
    return get_config().rds.operation_database


def _actor(ctx: "Optional[ExecutionContext]") -> Tuple[str, Optional[str], Optional[str],
                                                        Optional[str], Optional[str]]:
    """从 ctx 抽审计主体字段（ctx 可能为 None——如单测/同步旁路——用占位兜底）。

    返回 (user_id, roles_csv, acl_groups_json, channel, request_id)。
    """
    if ctx is None:
        return "-", None, None, None, None
    roles = tuple(getattr(ctx, "roles", ()) or ())
    groups = list(getattr(ctx, "acl_groups", ()) or ())
    return (
        getattr(ctx, "user_id", None) or "-",
        ",".join(roles) if roles else None,
        json.dumps(groups, ensure_ascii=False) if groups else None,
        getattr(ctx, "channel", None),
        getattr(ctx, "request_id", None),
    )


class AuditLog(Protocol):
    """合规审计写契约。RDS 实现见 RDSAuditLog；无操作实现见 NULL_AUDIT。"""

    def record(self, ctx: "Optional[ExecutionContext]", *, event_type: str, action: str,
               decision: str, risk_level: Optional[str] = None, policy_id: Optional[str] = None,
               args_digest: Optional[str] = None, detail: Optional[Dict[str, Any]] = None,
               run_id: Optional[str] = None, step_no: Optional[int] = None,
               fail_closed: bool = False) -> Optional[str]:
        ...


class _NullAudit:
    """无操作审计（默认）——未注入真实审计时零副作用，供既有测试/降级路径使用。"""

    def record(self, ctx, **kw) -> None:                # noqa: D401,ANN001
        return None


NULL_AUDIT: AuditLog = _NullAudit()


class RDSAuditLog:
    """agent_audit_log 的 RDS（fuling_operation）实现。append-only。"""

    def _conn(self):
        from opensearch_pipeline.db import _get_db_conn
        return _get_db_conn()

    def record(self, ctx: "Optional[ExecutionContext]", *, event_type: str, action: str,
               decision: str, risk_level: Optional[str] = None, policy_id: Optional[str] = None,
               args_digest: Optional[str] = None, detail: Optional[Dict[str, Any]] = None,
               run_id: Optional[str] = None, step_no: Optional[int] = None,
               fail_closed: bool = False) -> Optional[str]:
        """写一条审计。成功→audit_id；fail-open 失败→None；fail-closed 失败→抛 AuditWriteError。"""
        audit_id = uuid.uuid4().hex
        user_id, roles_csv, acl_json, channel, request_id = _actor(ctx)
        rid = run_id if run_id is not None else getattr(ctx, "run_id", None)
        detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
        db = _op_db()
        conn = None
        try:
            conn = self._conn()                         # 放 try 内：DB 不可达也走 fail-closed/open 分流
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.agent_audit_log "
                    "(audit_id, run_id, step_no, request_id, event_type, action, risk_level, "
                    " decision, policy_id, user_id, roles, acl_groups_snapshot, channel, "
                    " args_digest, detail_json, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(3))",
                    (audit_id, rid, step_no, request_id, event_type, action, risk_level,
                     decision, policy_id, user_id, roles_csv, acl_json, channel,
                     args_digest, detail_json),
                )
            conn.commit()
            return audit_id
        except Exception as e:                          # noqa: BLE001
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:                       # noqa: BLE001
                    pass
            if fail_closed:
                # 高风险：无审计不得执行 → 上抛，ToolExecutor 阻断
                raise AuditWriteError(f"agent_audit_log 写失败(fail-closed，已阻断): {e}") from e
            logger.error("agent_audit_log 写失败(fail-open，已跳过审计不阻断): %s", e, exc_info=True)
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:                       # noqa: BLE001
                    pass
