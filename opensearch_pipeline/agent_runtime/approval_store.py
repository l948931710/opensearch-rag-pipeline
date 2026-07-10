# -*- coding: utf-8 -*-
"""
approval_store.py — 审批闭环持久化（schema/025；v2 报告 §5「Approval Engine」；深度审查 A 组 P1）

补齐「request→decision→invocation→audit 四表可关联回放」缺的前两环：
- **挂起侧**（executor._persist_suspend）：create_request —— 一次 require_approval 挂起写一行
  approval_request(pending)，携带脱敏后参数 + approver_scope（部门管理员裁决键）+ expires_at
  （过期=拒绝，「沉默不是同意」，由 reaper 置 expired）。
- **决策侧**（routes /api/agent/approve）：decide —— 复用 kb_access_request 状态机范式
  `SELECT ... FOR UPDATE` + from_status='pending' 单向 CAS（first-valid-wins）+ 同事务写
  approval_decision（uk_req_idem 幂等：重复决策/客户端重试返回 duplicate 不重复改）。

职责分离在 routes 层裁决（decided_by ≠ requested_by + resolve_kb_identity DB 现查
approver_scope 覆盖）；本模块只管事实持久化与状态机，不做授权。

DB 访问沿用 run_store 惯例：`db._get_db_conn()`、`%s` 占位、NOW(3)、显式 commit/rollback/close。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from opensearch_pipeline.config import get_config

if TYPE_CHECKING:
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

logger = logging.getLogger(__name__)

# 审批请求存活窗口（秒）。默认与 suspended run TTL 对齐（3 天）——两边同源过期语义。
DEFAULT_APPROVAL_TTL_S = 259200

# decide() 的返回语义（kb_access 范式：CAS 失败不是异常，是并发/迟到事实）
DECIDE_ACCEPTED = "accepted"                  # 本次决策生效（pending → 目标态）
DECIDE_DUPLICATE = "duplicate"                # 同 (request, idempotency_key) 重放 → 幂等返回
DECIDE_ALREADY_DECIDED = "already_decided"    # 已被他人决出不同结果 / 已过期（迟到决策拒绝）


def _op_db() -> str:
    return get_config().rds.operation_database


def derive_approver_scope(ctx: "ExecutionContext") -> str:
    """从发起人 ctx 推审批人裁决键：第一个非 public 的读权限组（=主属部门 owner_dept）。

    dept_admin 的 managed_owner_depts（dept_admin_grant 显式 seed，DB 现查）覆盖该值即可审；
    kb_admin 恒可审。空串 = 发起人只有 public 组 → 只有 kb_admin 能审（无审批人可达=过期拒绝）。
    """
    for g in getattr(ctx, "acl_groups", ()) or ():
        s = str(g).strip()
        if s and s.lower() not in ("public", "*"):
            return s[:64]
    return ""


# ── per-tool approver_scope 解析器（本体 P0 PR8 seam）────────────────────────────
# 有些工具的审批人不该是"发起人部门"而是领域治理方——如 ontology_identity_resolve 按
# per-attr steward（stewardship scope）路由。工具在构造时注册解析器；未注册的工具
# 走 derive_approver_scope 默认推导，**既有行为零变化**。
_SCOPE_RESOLVERS: Dict[str, Callable[..., Optional[str]]] = {}


def register_approver_scope_resolver(tool_name: str, fn: Callable[..., Optional[str]]) -> None:
    """注册 fn(ctx, args) -> Optional[str]。重复注册即覆盖（幂等）。"""
    _SCOPE_RESOLVERS[str(tool_name)] = fn


def resolve_scope_live(tool_name: str, args: Optional[Dict[str, Any]]) -> Optional[str]:
    """审批时现算 per-tool approver_scope（PR-C，P0-06 #4：scope 不吃提案快照）。

    未注册解析器的工具 → None（调用方沿用快照/默认——语义零变化）；注册了 →
    现算结果（''=仅 kb_admin）；解析器异常 → '' fail-closed。ctx 传 None：per-tool
    解析器按约定只依赖 args（如 ontology 按 target_object_id 查 stewardship）。"""
    fn = _SCOPE_RESOLVERS.get(str(tool_name or ""))
    if fn is None:
        return None
    try:
        return str(fn(None, dict(args or {})) or "").strip()[:64]
    except Exception:   # noqa: BLE001
        logger.warning("approver_scope 现算失败（fail-closed 到 kb_admin）：%s",
                       tool_name, exc_info=True)
        return ""


def _resolve_scope(tool_name: str, ctx: "ExecutionContext", args: Dict[str, Any]) -> str:
    """审批人裁决键单点：注册了解析器的工具用解析器结果——None/'' → ''（仅 kb_admin 可审），
    解析器异常 → '' **fail-closed**（scope 算不出宁可收敛到 kb_admin，绝不误路由回发起人
    部门——那会让发起人的 dept_admin 审到不归他管的领域写）；未注册 → 默认推导。"""
    fn = _SCOPE_RESOLVERS.get(tool_name)
    if fn is None:
        return derive_approver_scope(ctx)
    try:
        return str(fn(ctx, args) or "").strip()[:64]
    except Exception:   # noqa: BLE001
        logger.warning("approver_scope 解析器失败（fail-closed 到 kb_admin）：%s",
                       tool_name, exc_info=True)
        return ""


class RDSApprovalStore:
    """approval_request / approval_decision 的 RDS（fuling_operation）实现。"""

    def _conn(self):
        from opensearch_pipeline.db import _get_db_conn
        return _get_db_conn()

    # ── 挂起侧 ───────────────────────────────────────────────────
    def create_request(self, run_id: str, ctx: "ExecutionContext",
                       pending_call: Dict[str, Any], *, tool_version: str = "",
                       ttl_s: Optional[int] = None) -> str:
        """挂起时写一行 pending 请求，返回 request_id。参数**脱敏后**入库（渲染用），
        原文摘要（args_digest）供与 tool_invocation / agent_audit_log 关联回放。"""
        from opensearch_pipeline.agent_runtime.sanitize import sanitize_args_json
        from opensearch_pipeline.agent_runtime.tool_executor import digest

        if ttl_s is None:
            ttl_s = int(os.environ.get("RAG_AGENT_APPROVAL_TTL_S", str(DEFAULT_APPROVAL_TTL_S)))
        request_id = uuid.uuid4().hex
        args = pending_call.get("arguments") or {}
        tool_name = str(pending_call.get("tool_name") or "")[:64]
        summary = f"{tool_name}({', '.join(sorted(map(str, args)))})" if args else tool_name
        # requested_dept=发起人主属部门（归属展示）；approver_scope=审批路由键——两者在
        # 有 per-tool 解析器时分道（如本体身份确认路由到 per-attr steward 而非发起人部门）
        requested_dept = derive_approver_scope(ctx)
        scope = _resolve_scope(tool_name, ctx, args)
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.approval_request "
                    "(request_id, run_id, call_id, tool_name, tool_version, proposed_args_json, "
                    " args_digest, render_summary, requested_by, requested_dept, approver_scope, "
                    " status, expires_at, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',"
                    " DATE_ADD(NOW(3), INTERVAL %s SECOND), NOW(3))",
                    (request_id, run_id, str(pending_call.get("call_id") or "")[:64],
                     tool_name, tool_version[:16], sanitize_args_json(args), digest(args),
                     summary[:1000], (ctx.user_id or "-")[:64],
                     (requested_dept or None), scope, int(ttl_s)),
                )
            conn.commit()
            return request_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 决策侧 ───────────────────────────────────────────────────
    def decide(self, request_id: str, *, decision: str, decided_by: str,
               reason: Optional[str] = None, edited_args: Optional[Dict[str, Any]] = None,
               idempotency_key: Optional[str] = None) -> str:
        """FOR UPDATE + pending CAS 决出处置。返回 DECIDE_ACCEPTED / DECIDE_DUPLICATE /
        DECIDE_ALREADY_DECIDED。decision ∈ approved/edited/rejected_feedback/rejected_terminate。
        同事务写 approval_decision（重复 idempotency_key → 幂等 DUPLICATE，不重复改状态）。"""
        from opensearch_pipeline.agent_runtime.sanitize import sanitize_args_json

        idem = (idempotency_key or uuid.uuid4().hex)[:64]
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT status FROM {db}.approval_request WHERE request_id=%s FOR UPDATE",
                    (request_id,))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return DECIDE_ALREADY_DECIDED
                if row[0] != "pending":
                    # 已决/已过期：同 idempotency_key 的重放幂等返回，其余按迟到拒绝
                    cur.execute(
                        f"SELECT decision FROM {db}.approval_decision "
                        "WHERE request_id=%s AND idempotency_key=%s", (request_id, idem))
                    dup = cur.fetchone()
                    conn.rollback()
                    return DECIDE_DUPLICATE if (dup and dup[0] == decision) else DECIDE_ALREADY_DECIDED
                cur.execute(
                    f"UPDATE {db}.approval_request SET status=%s, decided_at=NOW(3) "
                    "WHERE request_id=%s AND status='pending'",
                    (decision, request_id))
                cur.execute(
                    f"INSERT INTO {db}.approval_decision "
                    "(decision_id, request_id, decision, edited_args_json, reason, decided_by, "
                    " idempotency_key, decided_at) VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(3))",
                    (uuid.uuid4().hex, request_id, decision,
                     sanitize_args_json(edited_args) if edited_args is not None else None,
                     (reason or None), (decided_by or "-")[:64], idem))
            conn.commit()
            return DECIDE_ACCEPTED
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 读 ───────────────────────────────────────────────────────
    _COLS = ("request_id, run_id, call_id, tool_name, tool_version, proposed_args_json, "
             "args_digest, render_summary, requested_by, requested_dept, approver_scope, "
             "status, expires_at, created_at, decided_at")

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        keys = ("request_id", "run_id", "call_id", "tool_name", "tool_version",
                "proposed_args_json", "args_digest", "render_summary", "requested_by",
                "requested_dept", "approver_scope", "status", "expires_at", "created_at",
                "decided_at")
        d = dict(zip(keys, row))
        try:
            if isinstance(d.get("proposed_args_json"), (str, bytes)):
                d["proposed_args"] = json.loads(d.pop("proposed_args_json"))
            else:
                d["proposed_args"] = d.pop("proposed_args_json")
        except Exception:   # noqa: BLE001
            d["proposed_args"] = None
        for k in ("expires_at", "created_at", "decided_at"):
            if d.get(k) is not None:
                d[k] = str(d[k])
        return d

    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._COLS} FROM {db}.approval_request WHERE request_id=%s",
                    (request_id,))
                row = cur.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def get_pending_by_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """一个 suspended run 最多一个 pending 请求（单 pending call 设计）；取最新。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._COLS} FROM {db}.approval_request "
                    "WHERE run_id=%s AND status='pending' ORDER BY created_at DESC LIMIT 1",
                    (run_id,))
                row = cur.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def get_latest_by_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """取该 run 最新一条请求（不限状态）——decision 已落库但 resume 失败回滚 suspended 后，
        重试 /approve 据此按「已决同向」幂等续跑（报告 §5⑧ resume 崩溃可重复）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._COLS} FROM {db}.approval_request "
                    "WHERE run_id=%s ORDER BY created_at DESC LIMIT 1",
                    (run_id,))
                row = cur.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            conn.close()

    def list_pending(self, scopes: Optional[List[str]] = None, *, requested_by: Optional[str] = None,
                     limit: int = 100) -> List[Dict[str, Any]]:
        """审批队列。scopes=None → 全部（kb_admin）；否则按 approver_scope IN scopes（dept_admin）。
        requested_by 给「我的申请」视图。"""
        db = _op_db()
        conds, params = ["status='pending'"], []
        if scopes is not None:
            if not scopes:
                return []
            conds.append("approver_scope IN (%s)" % ",".join(["%s"] * len(scopes)))
            params.extend(scopes)
        if requested_by:
            conds.append("requested_by=%s")
            params.append(requested_by)
        params.append(int(limit))
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._COLS} FROM {db}.approval_request "
                    f"WHERE {' AND '.join(conds)} ORDER BY created_at ASC LIMIT %s",
                    tuple(params))
                rows = cur.fetchall() or []
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # ── 对账/收尸 ────────────────────────────────────────────────
    def expire_stale(self) -> int:
        """pending 且过期 → expired（过期=拒绝）。幂等、跨实例安全；reaper 周期调用。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.approval_request SET status='expired', decided_at=NOW(3) "
                    "WHERE status='pending' AND expires_at < NOW(3)")
                n = cur.rowcount
            conn.commit()
            return int(n)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_decided_unresumed(self, *, grace_s: int = 120,
                               limit: int = 20) -> List[Dict[str, Any]]:
        """B6 对账扫描：**决定已落库但 run 仍挂着**的窗口——resume 在 decide 之后失败/进程
        崩溃（含 reaper 把 stale resuming 回边 suspended 的场景）。返回 (run + 决定) 候选，
        由 reconcile 按 approval_decision 重建 outcome 重发 resume。

        grace_s：决定落库后的静默期——避免与正在进行的 resume 赛跑（decide→resume 是
        同请求内的两步，正常间隔毫秒级；默认 120s 只捞真死单）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT ar.request_id, ar.run_id, ar.tool_name, ar.call_id, "
                    f"d.decision, d.reason, d.decided_by, d.decided_at "
                    f"FROM {db}.approval_request ar "
                    f"JOIN {db}.agent_run r ON r.run_id = ar.run_id AND r.status='suspended' "
                    f"JOIN {db}.approval_decision d ON d.request_id = ar.request_id "
                    "WHERE ar.status IN ('approved','edited','rejected_feedback','rejected_terminate') "
                    "AND ar.decided_at < DATE_SUB(NOW(3), INTERVAL %s SECOND) "
                    "ORDER BY ar.decided_at ASC LIMIT %s",
                    (int(grace_s), int(limit)))
                rows = cur.fetchall() or []
            keys = ("request_id", "run_id", "tool_name", "call_id",
                    "decision", "reason", "decided_by", "decided_at")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                if d.get("decided_at") is not None:
                    d["decided_at"] = str(d["decided_at"])
                out.append(d)
            return out
        finally:
            conn.close()
