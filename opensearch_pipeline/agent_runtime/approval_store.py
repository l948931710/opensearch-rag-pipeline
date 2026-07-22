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

import base64
import hashlib
import hmac as _hmac
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from opensearch_pipeline.config import get_config

if TYPE_CHECKING:
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

logger = logging.getLogger(__name__)

# 审批请求存活窗口（秒）。默认与 suspended run TTL 对齐（3 天）——两边同源过期语义。
DEFAULT_APPROVAL_TTL_S = 259200

_TTL_MISMATCH_WARNED = False


def approval_ttl_s() -> int:
    """批次5（unknown-unknowns P1-08）：TTL 单源派生——RAG_AGENT_APPROVAL_TTL_S 未设时
    **跟随** RAG_AGENT_SUSPENDED_TTL_S（此前两个独立 env 只靠注释「默认对齐」维系，
    改一处忘另一处即漂移；且 reaper 的 resuming→suspended 回边会重置 run 的 heartbeat
    时钟，即便数值相等两边也会漂）。两者都显式设置且不等 → 启动期 warning 一次
    （允许有意错开——先过期一侧的半状态由 reaper cross-heal 收口：
    expire_for_terminal_runs + run_store.expire_suspended_with_expired_approval）。"""
    global _TTL_MISMATCH_WARNED
    raw_a = os.environ.get("RAG_AGENT_APPROVAL_TTL_S", "").strip()
    raw_s = os.environ.get("RAG_AGENT_SUSPENDED_TTL_S", "").strip()
    if raw_a:
        try:
            v = int(raw_a)
        except ValueError:
            return DEFAULT_APPROVAL_TTL_S
        if raw_s and raw_s != raw_a and not _TTL_MISMATCH_WARNED:
            _TTL_MISMATCH_WARNED = True
            logger.warning(
                "RAG_AGENT_APPROVAL_TTL_S=%s ≠ RAG_AGENT_SUSPENDED_TTL_S=%s——审批与挂起 "
                "run 的过期时钟将漂移（先过期一侧的半状态由 reaper cross-heal 收口）",
                raw_a, raw_s)
        return v
    if raw_s:
        try:
            return int(raw_s)      # 单源：审批 TTL 未显式设置时跟随 suspended TTL
        except ValueError:
            return DEFAULT_APPROVAL_TTL_S
    return DEFAULT_APPROVAL_TTL_S


# decide() 的返回语义（kb_access 范式：CAS 失败不是异常，是并发/迟到事实）
DECIDE_ACCEPTED = "accepted"                  # 本次决策生效（pending → 目标态）
DECIDE_DUPLICATE = "duplicate"                # 同 (request, idempotency_key) 重放 → 幂等返回
DECIDE_ALREADY_DECIDED = "already_decided"    # 已被他人决出不同结果（迟到决策拒绝）
DECIDE_EXPIRED = "expired"                    # P0-C：pending 但已过 expires_at → 原子转 expired 并拒绝
                                              # （过期不再依赖 reaper 窗口——决策时刻即裁决）


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
_SNAPSHOT_FALLBACK_WARNED = False   # 快照回退一次性告警（重审计 §3 可观测性）


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
        # 重审计 §3：生产 registry 只有 knowledge_search（READ_ONLY，不产生审批），
        # 唯一注册点在 HIGH_WRITE ontology 工具的 __init__——PMC-1 工具面（PR11-13）
        # 接线前 _SCOPE_RESOLVERS 恒空、审批 scope 恒走提案快照。这是组织 gate 签字
        # 前的**计划内中间态**；一次性告警让「现算未生效」在生产日志里可见而非静默。
        global _SNAPSHOT_FALLBACK_WARNED
        if not _SNAPSHOT_FALLBACK_WARNED and not _SCOPE_RESOLVERS:
            _SNAPSHOT_FALLBACK_WARNED = True
            logger.warning(
                "per-tool approver_scope 现算解析器未注册（工具 %s 走提案快照 scope）——"
                "PMC-1 工具面（PR11-13）接线后自动生效；此前 stewardship 变更不即时"
                "反映到已挂起审批的 scope", tool_name)
        return None
    try:
        # [:160]：backup steward 的 CSV scope（"steward,backup"，schema/031 加宽）
        return str(fn(None, dict(args or {})) or "").strip()[:160]
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
        return str(fn(ctx, args) or "").strip()[:160]
    except Exception:   # noqa: BLE001
        logger.warning("approver_scope 解析器失败（fail-closed 到 kb_admin）：%s",
                       tool_name, exc_info=True)
        return ""


# ── α5（M6，codex 共识 2026-07-21）：审批入场三层配额 ────────────────────────


class ApprovalQuotaExceeded(RuntimeError):
    """三层配额（global / per-requester / per-requester-tool）任一超限——在
    suspend_run_atomic 同一事务内裁决，整体回滚零副作用，run 得明确可重试错误。"""


class ApprovalQuotaUnavailable(RuntimeError):
    """任一 cap>0 而 agent_quota_lock 表/哨兵行不可用（schema/058 未 apply）——
    **fail-closed**：配置了的护栏绝不静默降级为无限额（codex v4 blocker）；
    readiness approval_quota_contract 在 cap>0 时同步红灯。"""


def _quota_caps() -> Tuple[int, int, int]:
    """(global, per-requester, per-requester-tool)；默认全 0=off（byte-identical，
    caps 全 0 时完全不触 058）。"""
    def _i(name: str) -> int:
        try:
            return max(0, int(os.environ.get(name, "0") or "0"))
        except ValueError:
            return 0
    return (_i("RAG_AGENT_APPROVAL_GLOBAL_CAP"),
            _i("RAG_AGENT_APPROVAL_PENDING_CAP"),
            _i("RAG_AGENT_APPROVAL_PER_TOOL_CAP"))


def _is_unknown_table_error(exc: BaseException) -> bool:
    args = getattr(exc, "args", None) or ()
    return (bool(args) and args[0] == 1146) or "doesn't exist" in str(exc)


# ── α4（M5）：分页游标——不透明+版本化+HMAC 签名 ─────────────────────────────
# 键=进程内随机（单副本形态；重启键轮换 → 旧游标失效，客户端从首页重拉，列表轮询
# 场景可接受）。严格校验：版本/签名/结构任一不符 → ValueError（路由层 400）。

_CURSOR_KEY: Optional[bytes] = None


def _cursor_key() -> bytes:
    global _CURSOR_KEY
    if _CURSOR_KEY is None:
        _CURSOR_KEY = os.urandom(32)
    return _CURSOR_KEY


def _encode_cursor(created_at: Any, request_id: str) -> str:
    ca = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    payload = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "ca": ca, "id": request_id},
                   ensure_ascii=False).encode("utf-8")).decode("ascii").rstrip("=")
    sig = _hmac.new(_cursor_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    return f"v1.{payload}.{sig}"


def _decode_cursor(cursor: str) -> Tuple[Any, str]:
    try:
        ver, payload, sig = str(cursor).split(".", 2)
    except ValueError:
        raise ValueError("游标格式非法")
    if ver != "v1":
        raise ValueError("游标版本不支持")
    want = _hmac.new(_cursor_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()[:32]
    if not _hmac.compare_digest(want, sig):
        raise ValueError("游标签名不符（或服务已重启，请从首页重拉）")
    try:
        pad = "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
        from datetime import datetime as _dt
        return _dt.fromisoformat(str(obj["ca"])), str(obj["id"])
    except Exception as e:
        raise ValueError("游标载荷非法") from e


class RDSApprovalStore:
    """approval_request / approval_decision 的 RDS（fuling_operation）实现。"""

    def _conn(self):
        from opensearch_pipeline.db import _get_db_conn
        return _get_db_conn()

    # ── 挂起侧 ───────────────────────────────────────────────────
    def insert_request(self, cur, run_id: str, ctx: "ExecutionContext",
                       pending_call: Dict[str, Any], *, tool_version: str = "",
                       ttl_s: Optional[int] = None,
                       request_id: Optional[str] = None) -> str:
        """游标级写 pending 请求（**不 commit**）——供 run_store.suspend_run_atomic 把
        checkpoint + approval_request + step + running→suspended 收进**同一事务**（P0-E：
        原 _persist_suspend 三段分事务，中途崩溃留下有 checkpoint 无审批行/有审批行未挂起
        的半态）。独立使用走 create_request（自带事务，语义不变）。"""
        from opensearch_pipeline.agent_runtime.sanitize import sanitize_args_json
        from opensearch_pipeline.agent_runtime.tool_executor import digest

        if ttl_s is None:
            ttl_s = approval_ttl_s()   # 批次5 P1-08：单源派生（未设时跟随 suspended TTL）
        request_id = request_id or uuid.uuid4().hex
        args = pending_call.get("arguments") or {}
        tool_name = str(pending_call.get("tool_name") or "")[:64]
        summary = f"{tool_name}({', '.join(sorted(map(str, args)))})" if args else tool_name
        # requested_dept=发起人主属部门（归属展示）；approver_scope=审批路由键——两者在
        # 有 per-tool 解析器时分道（如本体身份确认路由到 per-attr steward 而非发起人部门）。
        # scope 钳制 [:160]（031 加宽；backup steward CSV "steward,backup"）。
        requested_dept = derive_approver_scope(ctx)
        scope = _resolve_scope(tool_name, ctx, args)
        db = _op_db()
        # α5（M6）：三层入场配额——与本 INSERT **同一事务**（suspend_run_atomic）内裁决。
        # caps 全 0（默认）→ 零 058 接触（byte-identical）；任一 cap>0 → 哨兵行 FOR
        # UPDATE 显式串行化（单锁无死锁面，不赌隐式 gap-lock）→ 三 COUNT（走
        # idx_approval_quota）→ 超限抛 ApprovalQuotaExceeded（整事务回滚零副作用）；
        # 058 缺失/哨兵缺行 → ApprovalQuotaUnavailable **fail-closed**。
        g_cap, u_cap, t_cap = _quota_caps()
        if g_cap or u_cap or t_cap:
            self._enforce_admission_quota(
                cur, db, requested_by=(ctx.user_id or "-")[:64], tool_name=tool_name,
                caps=(g_cap, u_cap, t_cap))
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
             (requested_dept or None), scope[:160], int(ttl_s)),
        )
        return request_id

    @staticmethod
    def _enforce_admission_quota(cur, db: str, *, requested_by: str, tool_name: str,
                                 caps: Tuple[int, int, int]) -> None:
        """α5（M6）：哨兵串行化 + global→user→user-tool 三层 COUNT。锁随调用方事务
        commit/rollback 释放。真库双连接穿透测试见 test_majors_alpha_m6_db.py。"""
        g_cap, u_cap, t_cap = caps
        try:
            cur.execute(f"SELECT lock_name FROM {db}.agent_quota_lock "
                        "WHERE lock_name='approval_admission' FOR UPDATE")
            row = cur.fetchone()
        except Exception as e:   # noqa: BLE001
            if _is_unknown_table_error(e):
                raise ApprovalQuotaUnavailable(
                    "agent_quota_lock 表缺失（schema/058 未 apply）——配额已配置，"
                    "fail-closed 拒绝提案；请先 apply 058 或清零配额 env") from e
            raise
        if not row:
            raise ApprovalQuotaUnavailable(
                "agent_quota_lock 哨兵行缺失（058 的 INSERT IGNORE 未跑）——fail-closed")
        if g_cap:
            cur.execute(f"SELECT COUNT(*) FROM {db}.approval_request WHERE status='pending'")
            if int(cur.fetchone()[0]) >= g_cap:
                raise ApprovalQuotaExceeded(
                    f"审批积压已达全局上限（{g_cap}），请稍后再提交需审批的操作")
        if u_cap:
            cur.execute(f"SELECT COUNT(*) FROM {db}.approval_request "
                        "WHERE status='pending' AND requested_by=%s", (requested_by,))
            if int(cur.fetchone()[0]) >= u_cap:
                raise ApprovalQuotaExceeded(
                    f"你的待审批请求已达上限（{u_cap}），请等待处置或撤回后再提交")
        if t_cap:
            cur.execute(f"SELECT COUNT(*) FROM {db}.approval_request "
                        "WHERE status='pending' AND requested_by=%s AND tool_name=%s",
                        (requested_by, tool_name))
            if int(cur.fetchone()[0]) >= t_cap:
                raise ApprovalQuotaExceeded(
                    f"你在工具 {tool_name} 上的待审批请求已达上限（{t_cap}）")

    def create_request(self, run_id: str, ctx: "ExecutionContext",
                       pending_call: Dict[str, Any], *, tool_version: str = "",
                       ttl_s: Optional[int] = None) -> str:
        """挂起时写一行 pending 请求，返回 request_id。参数**脱敏后**入库（渲染用），
        原文摘要（args_digest）供与 tool_invocation / agent_audit_log 关联回放。"""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                request_id = self.insert_request(cur, run_id, ctx, pending_call,
                                                 tool_version=tool_version, ttl_s=ttl_s)
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
               idempotency_key: Optional[str] = None, audit_writer=None,
               outbox_writer=None) -> str:
        """FOR UPDATE + pending CAS 决出处置。返回 DECIDE_ACCEPTED / DECIDE_DUPLICATE /
        DECIDE_ALREADY_DECIDED / DECIDE_EXPIRED。decision ∈ approved/edited/rejected_feedback/
        rejected_terminate。同事务写 approval_decision（重复 idempotency_key → 幂等 DUPLICATE）。

        P0-C（重评报告 §5C）两处硬化：
        - **过期在决策时刻裁决**：同一 FOR UPDATE 事务内读 expires_at 与 DB NOW(3) 比较——
          过期的 pending **原子转 expired** 并拒绝（DECIDE_EXPIRED）。此前只有周期 reaper 置
          expired，reaper 间隔内/停摆时过期行仍可被批准（动态探针 DECIDE_ACCEPTED 已复现）。
          CAS UPDATE 条件同时带 `expires_at > NOW(3)`（纵深：即便读后时钟跨界也不放行）。
        - **决定绑定最终参数摘要**：approval_decision.final_args_digest = approved→请求原
          args_digest / edited→人工改后参数原文 sha256（digest 无 PII）/ rejected_*→NULL。
          已决重放（routes 对非 pending 的重试）只认与该摘要一致的参数，堵改参重放。
        """
        from opensearch_pipeline.agent_runtime.run_store import _begin
        from opensearch_pipeline.agent_runtime.sanitize import sanitize_args_json
        from opensearch_pipeline.agent_runtime.tool_executor import digest

        idem = (idempotency_key or uuid.uuid4().hex)[:64]
        db = _op_db()
        conn = self._conn()
        _begin(conn)        # 多语句事务（FOR UPDATE+UPDATE+INSERT）：钉连接禁 SteadyDB 单句重试
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT status, args_digest, (expires_at <= NOW(3)) "
                    f"FROM {db}.approval_request WHERE request_id=%s FOR UPDATE",
                    (request_id,))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return DECIDE_ALREADY_DECIDED
                status, orig_digest, is_expired = row[0], row[1], bool(row[2])
                if status != "pending":
                    # 已决/已过期：同 idempotency_key 的重放幂等返回，其余按迟到拒绝
                    cur.execute(
                        f"SELECT decision FROM {db}.approval_decision "
                        "WHERE request_id=%s AND idempotency_key=%s", (request_id, idem))
                    dup = cur.fetchone()
                    conn.rollback()
                    return DECIDE_DUPLICATE if (dup and dup[0] == decision) else DECIDE_ALREADY_DECIDED
                if is_expired:
                    # 过期=拒绝（沉默不是同意）——决策时刻原子转 expired，不等 reaper
                    cur.execute(
                        f"UPDATE {db}.approval_request SET status='expired', decided_at=NOW(3) "
                        "WHERE request_id=%s AND status='pending'", (request_id,))
                    conn.commit()
                    return DECIDE_EXPIRED
                if decision == "edited":
                    final_digest = digest(edited_args or {})
                elif decision == "approved":
                    final_digest = orig_digest
                else:
                    final_digest = None
                cur.execute(
                    f"UPDATE {db}.approval_request SET status=%s, decided_at=NOW(3) "
                    "WHERE request_id=%s AND status='pending' AND expires_at > NOW(3)",
                    (decision, request_id))
                if cur.rowcount != 1:
                    conn.rollback()
                    return DECIDE_EXPIRED       # 读后跨过期界：宁拒不批
                cur.execute(
                    f"INSERT INTO {db}.approval_decision "
                    "(decision_id, request_id, decision, edited_args_json, final_args_digest, "
                    " reason, decided_by, idempotency_key, decided_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(3))",
                    (uuid.uuid4().hex, request_id, decision,
                     sanitize_args_json(edited_args) if edited_args is not None else None,
                     final_digest, (reason or None), (decided_by or "-")[:64], idem))
                # P1-13（外审核查 2026-07-16）：审批决定的合规审计与决定行**同事务**——
                # 此前 decide 提交后路由 best-effort 补审计，审计缺口静默。写失败=整体
                # 回滚（调用方 503 重试），决定与审计要么都在、要么都不在。
                if audit_writer is not None:
                    audit_writer(cur)
                # PR-3 Stage B：resume 命令与决定同事务（dispatch_outbox.insert_command_tx）
                # ——决定 commit ⇒ 命令 durable，「decide 后崩溃」升级为命令消费。
                if outbox_writer is not None:
                    outbox_writer(cur)
            conn.commit()
            return DECIDE_ACCEPTED
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_decision(self, request_id: str) -> Optional[Dict[str, Any]]:
        """读该请求的权威决定行（≤1 行：decide 只在 pending CAS 成功时 INSERT 一次）。
        P0-C：已决重放/对账**只消费这行不可变事实**——decided_by/reason/final_args_digest
        以库为准，HTTP body 只能携带与 final_args_digest 一致的参数。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT decision_id, request_id, decision, final_args_digest, reason, "
                    f"decided_by, decided_at, idempotency_key FROM {db}.approval_decision "
                    "WHERE request_id=%s ORDER BY decided_at DESC LIMIT 1",
                    (request_id,))
                row = cur.fetchone()
            if not row:
                return None
            # idempotency_key 供 P1-07 已受理命令的 HTTP 重试幂等回放（同键 → 202 回放）
            d = dict(zip(("decision_id", "request_id", "decision", "final_args_digest",
                          "reason", "decided_by", "decided_at", "idempotency_key"), row))
            if d.get("decided_at") is not None:
                d["decided_at"] = str(d["decided_at"])
            return d
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
        """审批队列（存量 scope 过滤形态）。scopes=None → 全部（kb_admin）；否则按
        approver_scope 覆盖匹配（dept_admin）。requested_by 给「我的申请」视图。

        ⚠️ α4（M5，codex 共识 2026-07-21）：**scope 可见性裁决不再走本方法**——注册了
        per-tool 解析器的工具以 live scope 为唯一权威（快照过滤会让轮换后的旧 steward
        继续看到参数、新 steward 永远看不到），审批人视角一律走 list_pending_page。
        本方法保留给 ?mine（requested_by 视角，无 scope 语义）与 kb_admin 全量等
        无需 live 裁决的调用方。

        P1-11 backup steward：approver_scope 可为 CSV（"steward,backup"，schema/031 加宽）——
        队列过滤按**分量**匹配（FIND_IN_SET），managed 覆盖任一分量即入待办；此前 IN 精确
        匹配会让带 backup 的请求从主/备 steward 的队列同时消失（只剩 kb_admin 可见）。"""
        db = _op_db()
        conds, params = ["status='pending'"], []
        if scopes is not None:
            if not scopes:
                return []
            conds.append("(" + " OR ".join(["FIND_IN_SET(%s, approver_scope)"] * len(scopes)) + ")")
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

    # ── α4（M5，codex 共识 2026-07-21）：live 权威分页 + scope 轮换收敛 ─────────

    def list_pending_page(self, viewer_depts: Optional[List[str]], *,
                          limit: int = 50, cursor: Optional[str] = None) -> Dict[str, Any]:
        """审批人视角分页（live scope 唯一权威）。

        与 list_pending 的本质差异：**不做 approver_scope 的 SQL 预过滤**——按
        (created_at, request_id) keyset 扫描 pending 候选，逐行以 live resolver 判可见
        （registered 工具：resolve_scope_live 现算；unregistered：快照回退），填满一页。
        轮换后新 steward **立即**在列表看到请求、旧 steward 立即看不到，与裁决口径一致。

        - viewer_depts=None → kb_admin（全可见，零 resolve 开销）；
        - next_cursor 指向最后一个**已扫描候选**（非最后可见项——否则被隐藏行反复重扫），
          版本化+HMAC 签名不透明编码；进程重启签名键轮换 → 旧游标失效，客户端从首页重拉
          （列表轮询场景可接受）；伪造/损坏游标 → ValueError（路由层 400）。
        - 授权路径零缓存（codex v2 blocker：轮换后旧 scope 不得有缓存授权窗口）。"""
        anchor: Optional[Tuple[Any, str]] = _decode_cursor(cursor) if cursor else None
        db = _op_db()
        out: List[Dict[str, Any]] = []
        last_scanned: Optional[Dict[str, Any]] = None
        exhausted = False
        batch = 200
        conn = self._conn()
        try:
            while len(out) < limit and not exhausted:
                conds, params = ["status='pending'"], []
                if anchor is not None:
                    conds.append("(created_at > %s OR (created_at = %s AND request_id > %s))")
                    params.extend([anchor[0], anchor[0], anchor[1]])
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT {self._COLS} FROM {db}.approval_request "
                        f"WHERE {' AND '.join(conds)} "
                        "ORDER BY created_at ASC, request_id ASC LIMIT %s",
                        tuple(params) + (batch,))
                    rows = cur.fetchall() or []
                for r in rows:
                    d = self._row_to_dict(r)
                    last_scanned = d
                    anchor = (d.get("created_at"), d.get("request_id"))
                    if self._visible_live(d, viewer_depts):
                        out.append(d)
                        if len(out) >= limit:
                            break
                if len(rows) < batch and len(out) < limit:
                    exhausted = True
        finally:
            conn.close()
        next_cursor = None
        if not exhausted and last_scanned is not None:
            next_cursor = _encode_cursor(last_scanned.get("created_at"),
                                         str(last_scanned.get("request_id")))
        return {"items": out, "next_cursor": next_cursor}

    @staticmethod
    def _visible_live(d: Dict[str, Any], viewer_depts: Optional[List[str]]) -> bool:
        """单行可见性：kb_admin（None）恒可见；registered 工具 live 唯一权威（''=仅
        kb_admin，dept_admin 不可见）；unregistered（resolve 返回 None）→ 快照回退。"""
        if viewer_depts is None:
            return True
        live = resolve_scope_live(d.get("tool_name"), d.get("proposed_args"))
        scope = live if live is not None else (d.get("approver_scope") or "")
        if not scope:
            return False
        viewer = {v for v in viewer_depts if v}
        return any(p.strip() in viewer for p in str(scope).split(",") if p.strip())

    def refresh_scope(self, request_id: str, old_scope: str, new_scope: str, *,
                      run_id: Optional[str] = None, actor: str = "system") -> bool:
        """scope 轮换落库（CAS + 同事务 old→new 审计）。

        WHERE status='pending' AND approver_scope=<old> ——并发裁决/重复刷新恰一次生效
        （codex v3：rowcount=1 才写审计，绝不产生「审计说改了、行没改」的错位）。
        存量列此后仅作展示/审计事实；可见性与裁决均以 live 为权威（本方法只是让
        存量面追上事实，FIND_IN_SET 的历史消费方不再被旧 scope 误导）。"""
        from opensearch_pipeline.agent_runtime.run_store import _begin
        db = _op_db()
        conn = self._conn()
        _begin(conn)        # 多语句事务（UPDATE+审计 INSERT）：钉连接禁 SteadyDB 单句重试
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.approval_request SET approver_scope=%s "
                    "WHERE request_id=%s AND status='pending' AND approver_scope=%s",
                    (str(new_scope)[:160], request_id, old_scope))
                if cur.rowcount != 1:
                    conn.rollback()
                    return False
                from opensearch_pipeline.agent_runtime.audit import insert_audit_row_tx
                insert_audit_row_tx(
                    cur, None, event_type="approval_scope_refreshed",
                    action="approval.scope_refresh", decision="updated",
                    run_id=run_id,
                    detail={"request_id": request_id, "old_scope": old_scope,
                            "new_scope": str(new_scope)[:160], "actor": actor})
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def refresh_stale_scopes(self, *, older_than_s: int = 1800, limit: int = 50) -> int:
        """轮换收敛的进程外兜底腿（reaper tick 调用；γ 批 agent_health 复用）：对挂起
        超过 older_than_s 的 pending 行批量 live 现算，漂移则 CAS 刷新+审计——彻底消除
        「无人点击审批则新 steward 永远看不到」的死角（live 列表已消可见性死角，本腿
        消的是存量列漂移的展示/审计口径）。返回刷新行数；单行失败不拖垮整批。"""
        db = _op_db()
        conn = self._conn()
        rows: List[Any] = []
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT request_id, run_id, tool_name, proposed_args_json, approver_scope "
                    f"FROM {db}.approval_request "
                    "WHERE status='pending' AND created_at < DATE_SUB(NOW(3), INTERVAL %s SECOND) "
                    "ORDER BY created_at ASC LIMIT %s",
                    (int(older_than_s), int(limit)))
                rows = list(cur.fetchall() or [])
        finally:
            conn.close()
        refreshed = 0
        for r in rows:
            try:
                request_id, run_id, tool_name, args_json, stored = r[0], r[1], r[2], r[3], r[4]
                try:
                    args = json.loads(args_json) if isinstance(args_json, (str, bytes)) else (args_json or {})
                except Exception:   # noqa: BLE001
                    args = {}
                live = resolve_scope_live(tool_name, args)
                if live is None or live == (stored or ""):
                    continue
                if self.refresh_scope(request_id, stored or "", live,
                                      run_id=run_id, actor="reaper"):
                    refreshed += 1
            except Exception:   # noqa: BLE001 — 单行失败不拖垮整批
                logger.warning("scope 轮换收敛单行失败（下轮重试）", exc_info=True)
        return refreshed

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

    def expire_for_terminal_runs(self) -> int:
        """批次5 cross-heal（unknown-unknowns P1-08）：run 已终态而审批仍 pending 的
        孤儿行——审批队列里看起来可处置、点开必 409（/approve 只认 suspended），
        此前一直挂到自身 expires_at。随 reaper 周期提前收口。幂等、跨实例安全。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.approval_request a "
                    f"JOIN {db}.agent_run r ON a.run_id = r.run_id "
                    "SET a.status='expired', a.decided_at=NOW(3) "
                    "WHERE a.status='pending' "
                    "AND r.status IN ('succeeded','failed','cancelled','expired')")
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
        同请求内的两步，正常间隔毫秒级；默认 120s 只捞真死单）。

        批次4（ultra P1 approval_store:488）：只捞该 run 的**最新**请求——已决请求永远
        停在 approved/... 终态（无 consumed 迁移），多审批周期 run（批过 request 1、
        resume、再挂起 request 2）里旧决定会永久满足本扫描，被对账重放到新挂起调用：
        同工具同参时 = 批一次、同参调用永放行；异参时 = 每周期无谓抢跑真实审批人。
        NOT EXISTS 按 (created_at, request_id) 排除存在更新请求的行（executor 侧另有
        call_id 锚定双保险，见 _verify_persisted_decision）。"""
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
                    f"AND NOT EXISTS (SELECT 1 FROM {db}.approval_request ar2 "
                    "                 WHERE ar2.run_id = ar.run_id "
                    "                   AND (ar2.created_at > ar.created_at "
                    "                        OR (ar2.created_at = ar.created_at "
                    "                            AND ar2.request_id > ar.request_id))) "
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
