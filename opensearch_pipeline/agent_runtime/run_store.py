# -*- coding: utf-8 -*-
"""
run_store.py — L3 Durable Run（RDS，fuling_operation）（v2 报告 §6 模块 B）

durable 真相源：agent_run / agent_step / agent_checkpoint（+ tool_invocation 由 executor 写）。
状态机复用 kb_access 的 `SELECT ... FOR UPDATE` + CAS 单向迁移（access_grants.py 同构）。

语义边界：
- transition(from, to)：`to ∉ 允许集[from]` → InvalidTransition（编码错误）；run 不存在或
  当前状态 ≠ from → 返回 False（并发/迟到/已迁移，非异常）。first-writer-wins 由 CAS 保证。
- checkpoint blob 的字段级结构（turn 内 call_id 槽位，B4）由 loop 层编解码；store 层只存字节。
- ⚠️ 执行宿主（B1）与 resuming 中间态（B3）随「从长计议」拍板后再定；本表 status 严格按报告
  §6 的六态 ENUM，暂不引入 resuming。

DB 访问沿用 serving 惯例：`db._get_db_conn()`（元组游标）、`%s` 占位、显式 commit/rollback/close。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

from opensearch_pipeline.config import get_config

if TYPE_CHECKING:  # 运行期不导入：context.py 由 Task 8 落地
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

# 状态机（报告 §6 + B1(b)/B3：resuming 中间态）
# resuming = 审批已批、执行宿主尚未接手续跑的中间态。resume 两步：
#   ① 审批回调 CAS suspended→resuming（认领，防两回调并发重入）+ 立即 ACK；
#   ② B1 有界执行器消费 resume 事件 → resuming→running 续跑。
# decided-but-not-resumed（B6）：对账扫 resuming 超阈值 → 重发 resume 事件。
RUN_STATUSES = ("running", "suspended", "resuming", "succeeded", "failed", "cancelled", "expired")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired"})
_ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    "running": frozenset({"suspended", "succeeded", "failed", "cancelled"}),
    "suspended": frozenset({"resuming", "cancelled", "expired", "failed"}),  # 认领走 resuming（非直达 running）
    # 执行器接手 → running；认领后失败（池满/checkpoint 解码）→ **回边 suspended**——
    # 没有回边则任何认领后失败把 run 永久钉死在 resuming（/approve 只认 suspended → 409）。
    "resuming": frozenset({"running", "suspended", "failed", "cancelled"}),
    # 终态无出边（succeeded/failed/cancelled/expired）→ 任何迁移即 InvalidTransition
}

_STEP_KINDS = ("model_call", "tool_call", "approval", "compaction", "system")


class InvalidTransition(ValueError):
    """请求了状态机不允许的迁移（编码错误；并发竞态用 transition 返回 False 表达）。"""


class ThreadBusy(RuntimeError):
    """A1 per-thread 串行化（schema/037）：该 thread 已有非终态（running/suspended/
    resuming）run——uk_thread_active 生成列唯一键撞 1062。路由层映射 409
    「该会话已有回答在进行中」；suspended（等审批）同样互斥（non-terminal 一律占坑）。"""


def _is_thread_busy_error(exc: BaseException) -> bool:
    """1062 且撞的是 uk_thread_active 才算 ThreadBusy（PK run_id 的 1062 概率上不存在，
    但不做键名判定会把任何撞键都误报成会话忙）。"""
    args = getattr(exc, "args", None) or ()
    if not args or args[0] != 1062:
        return False
    return "uk_thread_active" in str(exc)


@dataclass
class AgentStep:
    kind: str                              # ∈ _STEP_KINDS
    payload: Dict[str, Any]                # 脱敏后
    tokens_prompt: Optional[int] = None
    tokens_completion: Optional[int] = None
    latency_ms: Optional[int] = None


@dataclass
class RunCheckpoint:
    checkpoint_id: str
    state_blob: bytes
    state_digest: str
    created_at: Optional[str] = None


class RunStore(Protocol):
    """durable run 读写契约（报告 §6）。RDS 实现见 RDSRunStore。"""

    def create_run(self, ctx: "ExecutionContext", agent_profile: str) -> str: ...
    def append_step(self, run_id: str, step: AgentStep) -> int: ...
    def save_checkpoint(self, run_id: str, state_blob: bytes, state_digest: str) -> str: ...
    def load_latest_checkpoint(self, run_id: str) -> Optional[RunCheckpoint]: ...
    def transition(self, run_id: str, from_status: str, to_status: str) -> bool: ...


def _op_db() -> str:
    return get_config().rds.operation_database


class RDSRunStore:
    """RunStore 的 RDS（fuling_operation）实现。"""

    def _conn(self):
        from opensearch_pipeline.db import _get_db_conn
        return _get_db_conn()

    # ── 写 ───────────────────────────────────────────────────────
    def create_run(self, ctx: "ExecutionContext", agent_profile: str) -> str:
        """建 run（status=running），返回 run_id（uuid hex，CHAR(32)）。

        acl_groups 落 snapshot 仅供审计——resume 时不用它授权，重新解析（铁律 5）。
        A1（schema/037）：uk_thread_active 撞 1062 → ThreadBusy——同 thread 已有非终态
        run，并发双 submit 由 DB 唯一键裁决恰一个成功（应用层 check-then-insert 有竞态窗）。
        """
        run_id = uuid.uuid4().hex
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.agent_run "
                    "(run_id, thread_id, conversation_id, user_id, channel, agent_profile, status, "
                    " acl_groups_snapshot, model_profile, prompt_version, git_sha, heartbeat_at, started_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s,%s,%s,NOW(3),NOW(3))",
                    (run_id, ctx.thread_id, getattr(ctx, "conversation_id", None), ctx.user_id,
                     ctx.channel, agent_profile,
                     json.dumps(list(ctx.acl_groups), ensure_ascii=False),
                     getattr(ctx, "model_profile", None), getattr(ctx, "prompt_version", None),
                     getattr(ctx, "git_sha", None)),
                )
            conn.commit()
            return run_id
        except Exception as e:
            conn.rollback()
            if _is_thread_busy_error(e):
                raise ThreadBusy(
                    f"thread {ctx.thread_id!r} 已有进行中的 run（uk_thread_active 串行化）") from e
            raise
        finally:
            conn.close()

    def append_step(self, run_id: str, step: AgentStep) -> int:
        """追加一步 trace，返回 step_no。同 run 由 executor 单线程串行驱动（step_no=MAX+1）。"""
        if step.kind not in _STEP_KINDS:
            raise ValueError(f"未知 step.kind={step.kind!r}（合法：{_STEP_KINDS}）")
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COALESCE(MAX(step_no),0)+1 FROM {db}.agent_step WHERE run_id=%s", (run_id,))
                step_no = int(cur.fetchone()[0])
                cur.execute(
                    f"INSERT INTO {db}.agent_step "
                    "(run_id, step_no, kind, payload_json, tokens_prompt, tokens_completion, latency_ms, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(3))",
                    (run_id, step_no, step.kind, json.dumps(step.payload, ensure_ascii=False),
                     step.tokens_prompt, step.tokens_completion, step.latency_ms),
                )
            conn.commit()
            return step_no
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_checkpoint(self, run_id: str, state_blob: bytes, state_digest: str) -> str:
        """写挂起 checkpoint，返回 checkpoint_id。"""
        cp_id = uuid.uuid4().hex
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.agent_checkpoint "
                    "(run_id, checkpoint_id, state_blob, state_digest, created_at) "
                    "VALUES (%s,%s,%s,%s,NOW(3))",
                    (run_id, cp_id, state_blob, state_digest),
                )
            conn.commit()
            return cp_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def suspend_run_atomic(self, run_id: str, state_blob: bytes, state_digest: str,
                           step_payload: Optional[Dict[str, Any]] = None,
                           extra_writer=None) -> "tuple":
        """P0-E（重评报告 §5E）：挂起持久化**单事务**——checkpoint + approval_request
        （extra_writer 游标回调，approval_store.insert_request）+ approval agent_step +
        running→suspended CAS 一次 commit。此前四段分事务：中途崩溃留下「有 checkpoint 无
        审批行」「有审批行 run 仍 running」等半态。返回 (checkpoint_id, ok)——ok=False 表示
        run 已不在 running（并发取消/失败），**未提交任何写**；任何一步异常整体回滚后抛出。"""
        cp_id = uuid.uuid4().hex
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT status FROM {db}.agent_run WHERE run_id=%s FOR UPDATE",
                            (run_id,))
                row = cur.fetchone()
                if not row or row[0] != "running":
                    conn.rollback()
                    return cp_id, False
                cur.execute(
                    f"INSERT INTO {db}.agent_checkpoint "
                    "(run_id, checkpoint_id, state_blob, state_digest, created_at) "
                    "VALUES (%s,%s,%s,%s,NOW(3))",
                    (run_id, cp_id, state_blob, state_digest))
                if extra_writer is not None:
                    extra_writer(cur)
                if step_payload is not None:
                    cur.execute(
                        f"SELECT COALESCE(MAX(step_no),0)+1 FROM {db}.agent_step WHERE run_id=%s",
                        (run_id,))
                    step_no = int(cur.fetchone()[0])
                    cur.execute(
                        f"INSERT INTO {db}.agent_step "
                        "(run_id, step_no, kind, payload_json, created_at) "
                        "VALUES (%s,%s,'approval',%s,NOW(3))",
                        (run_id, step_no, json.dumps(step_payload, ensure_ascii=False)))
                cur.execute(
                    f"UPDATE {db}.agent_run SET status='suspended', heartbeat_at=NOW(3) "
                    "WHERE run_id=%s AND status='running'", (run_id,))
                ok = cur.rowcount == 1
            if ok:
                conn.commit()
            else:
                conn.rollback()
            return cp_id, ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def transition(self, run_id: str, from_status: str, to_status: str) -> bool:
        """单向状态机迁移（CAS）。合法且当前==from → 迁移并 True；否则 False；非法 pair → 抛。"""
        if to_status not in _ALLOWED_TRANSITIONS.get(from_status, frozenset()):
            raise InvalidTransition(f"agent_run 状态迁移非法: {from_status} → {to_status}")
        db = _op_db()
        set_ended = ", ended_at=NOW(3)" if to_status in _TERMINAL else ""
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT status FROM {db}.agent_run WHERE run_id=%s FOR UPDATE", (run_id,))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return False                       # run 不存在
                if row[0] != from_status:
                    conn.rollback()
                    return False                       # CAS 失败（并发/迟到/已迁移）
                cur.execute(
                    f"UPDATE {db}.agent_run SET status=%s, heartbeat_at=NOW(3){set_ended} "
                    "WHERE run_id=%s AND status=%s",
                    (to_status, run_id, from_status),
                )
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat(self, run_id: str) -> None:
        """刷新活动 run 的心跳（僵尸回收判据）。running/resuming 视为活动态。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_run SET heartbeat_at=NOW(3) "
                    "WHERE run_id=%s AND status IN ('running','suspended','resuming')",
                    (run_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reap_stale_runs(self, *, running_stale_s: int = 900,
                        suspended_ttl_s: int = 259200) -> Dict[str, int]:
        """收尸（对齐主仓 stage-3 的 2h stale-lock takeover 纪律）：
        - **resuming 心跳超时 → 回边 suspended**（B6）：resuming 是「已批、执行宿主尚未接手」
          的中间态——进程在认领后崩溃时直接标 failed 会**吞掉已落库的审批决定**（批准了却
          永不执行，违背审批闭环承诺）。回边 suspended 保住可重驱性：对账（reconcile）按
          approval_decision 重发 resume，或由 suspended TTL 兜底过期。
        - running 心跳超时（默认 15 分钟）→ failed：崩溃/SAE 滚动发布 SIGKILL 留下的
          僵尸，无人收尸则永久滞留 running（B 组 P1「heartbeat 死代码」的另一半）。
        - suspended 超期（默认 3 天）→ expired：审批黑洞的兜底——过期即视为拒绝。
        纯 UPDATE、幂等、跨实例安全（多实例并发跑只会有一个 rowcount>0）。
        由 routes/agent 的后台 reaper 线程周期调用。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_run SET status='suspended', heartbeat_at=NOW(3) "
                    "WHERE status='resuming' "
                    "AND heartbeat_at < DATE_SUB(NOW(3), INTERVAL %s SECOND)",
                    (int(running_stale_s),),
                )
                resuming_reset = cur.rowcount
                cur.execute(
                    f"UPDATE {db}.agent_run SET status='failed', ended_at=NOW(3) "
                    "WHERE status='running' "
                    "AND heartbeat_at < DATE_SUB(NOW(3), INTERVAL %s SECOND)",
                    (int(running_stale_s),),
                )
                failed = cur.rowcount
                cur.execute(
                    f"UPDATE {db}.agent_run SET status='expired', ended_at=NOW(3) "
                    "WHERE status='suspended' "
                    "AND heartbeat_at < DATE_SUB(NOW(3), INTERVAL %s SECOND)",
                    (int(suspended_ttl_s),),
                )
                expired = cur.rowcount
            conn.commit()
            return {"failed": int(failed), "expired": int(expired),
                    "resuming_reset": int(resuming_reset)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def consume_budget(self, run_id: str, *, turns: int = 0, tool_calls: int = 0,
                       tokens: int = 0) -> Dict[str, int]:
        """原子累加预算消耗，返回累计值（B8：消耗跨 suspend/resume 持久 → 落 durable，不放 frozen ctx）。

        执行器每 turn/每工具/每次模型调用后调用；再与 ctx.budget 的 caps 比较判超预算（fail-closed）。
        """
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_run SET turns_used=turns_used+%s, "
                    "tool_calls_used=tool_calls_used+%s, tokens_used=tokens_used+%s WHERE run_id=%s",
                    (int(turns), int(tool_calls), int(tokens), run_id),
                )
                cur.execute(
                    f"SELECT turns_used, tool_calls_used, tokens_used FROM {db}.agent_run WHERE run_id=%s",
                    (run_id,),
                )
                row = cur.fetchone()
            conn.commit()
            if not row:
                return {"turns_used": 0, "tool_calls_used": 0, "tokens_used": 0}
            return {"turns_used": int(row[0]), "tool_calls_used": int(row[1]), "tokens_used": int(row[2])}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 读 ───────────────────────────────────────────────────────
    def load_latest_checkpoint(self, run_id: str) -> Optional[RunCheckpoint]:
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT checkpoint_id, state_blob, state_digest, created_at "
                    f"FROM {db}.agent_checkpoint WHERE run_id=%s "
                    "ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1",
                    (run_id,),
                )
                row = cur.fetchone()
            if not row:
                return None
            blob = row[1]
            if isinstance(blob, str):     # 部分驱动把 BLOB 解成 str
                blob = blob.encode("utf-8", "surrogateescape")
            return RunCheckpoint(
                checkpoint_id=row[0], state_blob=blob, state_digest=row[2],
                created_at=str(row[3]) if row[3] is not None else None)
        finally:
            conn.close()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        # message_id 列 = schema/036（U1/U2 答案读回 + 续跑反馈锚定）——先 apply 后部署纪律
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT run_id, status, user_id, channel, agent_profile, started_at, ended_at, "
                    f"thread_id, conversation_id, model_profile, message_id "
                    f"FROM {db}.agent_run WHERE run_id=%s",
                    (run_id,),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {"run_id": row[0], "status": row[1], "user_id": row[2], "channel": row[3],
                    "agent_profile": row[4], "started_at": row[5], "ended_at": row[6],
                    "thread_id": row[7], "conversation_id": row[8], "model_profile": row[9],
                    "message_id": row[10]}
        finally:
            conn.close()

    def set_message_id(self, run_id: str, message_id: str) -> None:
        """U1/U2（schema/036）：submit 后回填该 run 的 qa message_id——审批续跑复用它落
        qa_session_log（前端反馈投票锚定不悬空），run 详情经它取回最终答案。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_run SET message_id=%s WHERE run_id=%s",
                    ((message_id or "")[:64] or None, run_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── tool_invocation（WS1 收尾：工具调用 trace + 幂等 uk_tool_idem）──────────
    def record_invocation(self, run_id: str, step_no: int, *, tool_name: str, tool_version: str,
                          args_json: str, args_digest: str, idempotency_key: Optional[str],
                          status: str, policy_decision: str, policy_id: str,
                          approval_request_id: Optional[str] = None) -> str:
        """记一条工具调用（返回 invocation_id）。status ∈ proposed/denied/pending_approval/executing。

        approval_request_id（P1「invocation→approval 回链」）：审批放行的执行由 adjudicator
        从 ApprovalGrant 注入 ctx 后传入——022 建列以来一直恒 NULL，四表回放链在这环断掉。"""
        inv_id = uuid.uuid4().hex
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.tool_invocation "
                    "(invocation_id, run_id, step_no, tool_name, tool_version, args_json, args_digest, "
                    " idempotency_key, status, policy_decision, policy_id, approval_request_id, "
                    " started_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(3))",
                    (inv_id, run_id, step_no, tool_name, tool_version, args_json, args_digest,
                     idempotency_key, status, policy_decision, policy_id, approval_request_id),
                )
            conn.commit()
            return inv_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish_invocation(self, invocation_id: str, *, status: str,
                          result_digest: Optional[str] = None, receipt_json: Optional[str] = None,
                          error_text: Optional[str] = None) -> None:
        """收尾一条工具调用（executing → succeeded/failed/compensated + 回执/摘要）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.tool_invocation SET status=%s, result_digest=%s, receipt_json=%s, "
                    "error_text=%s, ended_at=NOW(3) WHERE invocation_id=%s",
                    (status, result_digest, receipt_json, error_text, invocation_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def find_succeeded_invocation(self, tool_name: str,
                                  idempotency_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """key_required 工具幂等：已成功的同键调用 → 返回回执（重放/重试不重复副作用）。"""
        if not idempotency_key:
            return None
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT invocation_id, result_digest, receipt_json FROM {db}.tool_invocation "
                    "WHERE tool_name=%s AND idempotency_key=%s AND status='succeeded' "
                    "ORDER BY ended_at DESC LIMIT 1",
                    (tool_name, idempotency_key),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {"invocation_id": row[0], "result_digest": row[1], "receipt_json": row[2]}
        finally:
            conn.close()

    # ── P0-E：invocation 状态机（uncertain + 对账 + fencing）────────────────────
    def find_invocation_by_key(self, tool_name: str,
                               idempotency_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """同 (tool, key) 的既有行（uk_tool_idem 保证 ≤1 行，任意状态）+ started_at 年龄。
        ToolExecutor 执行前据此裁决：executing 新鲜=在跑（拒双跑）/ executing 陈旧=僵尸
        （CAS 转 uncertain）/ uncertain=待对账（阻断自动重试）/ failed=可回收重试。"""
        if not idempotency_key:
            return None
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT invocation_id, status, "
                    f"TIMESTAMPDIFF(SECOND, started_at, NOW(3)) FROM {db}.tool_invocation "
                    "WHERE tool_name=%s AND idempotency_key=%s LIMIT 1",
                    (tool_name, idempotency_key),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {"invocation_id": row[0], "status": row[1],
                    "age_s": int(row[2]) if row[2] is not None else 0}
        finally:
            conn.close()

    def reclaim_failed_invocation(self, invocation_id: str) -> bool:
        """failed → executing 的重试认领（CAS，fencing：并发重试只有一个成功）。
        uk_tool_idem 是 (tool, key) 唯一键——同键重试**复用原行**而非插新行（插新行必撞
        IntegrityError，这正是「stale 行阻塞重试」的根：任何非 succeeded 残行都堵死后续）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.tool_invocation SET status='executing', started_at=NOW(3), "
                    "ended_at=NULL, error_text=NULL, result_digest=NULL, receipt_json=NULL "
                    "WHERE invocation_id=%s AND status='failed'",
                    (invocation_id,))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_invocation_uncertain(self, invocation_id: str, note: str = "") -> bool:
        """executing → uncertain（CAS）：超时/僵尸的副作用不可知，绝不静默判 failed
        （盲重试=重复副作用）。uncertain 行阻断同键自动重试，走人工对账。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.tool_invocation SET status='uncertain', ended_at=NOW(3), "
                    "error_text=%s WHERE invocation_id=%s AND status='executing'",
                    ((note or "结果不确定")[:500], invocation_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_stale_invocations_uncertain(self, *, stale_s: int = 900) -> int:
        """对账扫描（reaper 周期调）：executing 超过 stale_s 仍未收尾 → uncertain。
        进程崩溃/SIGKILL 在 record executing 之后、finish 之前留下的僵尸行，此前既无人
        收尸也阻塞同键重试（P0-E「无 reconciliation/fencing」）。幂等、跨实例安全。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.tool_invocation SET status='uncertain', ended_at=NOW(3), "
                    "error_text=CONCAT('stale executing（进程崩溃/超时僵尸，', %s, 's 无收尾）') "
                    "WHERE status='executing' "
                    "AND started_at < DATE_SUB(NOW(3), INTERVAL %s SECOND)",
                    (str(int(stale_s)), int(stale_s)))
                n = cur.rowcount
            conn.commit()
            return int(n)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def resolve_uncertain_invocation(self, invocation_id: str, *, to_status: str,
                                     note: str, resolved_by: str) -> bool:
        """人工对账处置：uncertain → succeeded（业务侧核实副作用已生效）/ failed（核实未生效，
        放行同键重试）。CAS 单向、审计信息进 error_text（[人工对账] 前缀），路由层另记
        agent_audit_log。"""
        if to_status not in ("succeeded", "failed"):
            raise ValueError(f"uncertain 只能对账为 succeeded/failed，收到 {to_status!r}")
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.tool_invocation SET status=%s, ended_at=NOW(3), "
                    "error_text=%s WHERE invocation_id=%s AND status='uncertain'",
                    (to_status, f"[人工对账 by {resolved_by}] {note}"[:500], invocation_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_invocations(self, *, status: Optional[str] = None, run_id: Optional[str] = None,
                         limit: int = 50) -> "list":
        """按状态/run 列工具调用（对账视图 + run center 时间线）。"""
        db = _op_db()
        conds, params = [], []
        if status:
            conds.append("status=%s")
            params.append(status)
        if run_id:
            conds.append("run_id=%s")
            params.append(run_id)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        params.append(max(1, min(int(limit), 200)))
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT invocation_id, run_id, step_no, tool_name, status, policy_decision, "
                    f"approval_request_id, idempotency_key, args_digest, error_text, "
                    f"started_at, ended_at FROM {db}.tool_invocation {where} "
                    "ORDER BY started_at DESC LIMIT %s",
                    tuple(params))
                rows = cur.fetchall() or []
            keys = ("invocation_id", "run_id", "step_no", "tool_name", "status",
                    "policy_decision", "approval_request_id", "idempotency_key",
                    "args_digest", "error_text", "started_at", "ended_at")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                for k in ("started_at", "ended_at"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                out.append(d)
            return out
        finally:
            conn.close()

    # ── P0-F run center：我的 runs / 步骤时间线 ────────────────────────────────
    def list_runs_by_user(self, user_id: str, *, limit: int = 20) -> "list":
        """按用户列最近 runs（运行中心「我的 runs」；断线/刷新后按 run_id 重连的入口）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT run_id, status, thread_id, conversation_id, agent_profile, "
                    f"turns_used, tool_calls_used, tokens_used, started_at, ended_at, "
                    f"model_profile "
                    f"FROM {db}.agent_run WHERE user_id=%s "
                    "ORDER BY started_at DESC LIMIT %s",
                    (user_id, max(1, min(int(limit), 100))))
                rows = cur.fetchall() or []
            keys = ("run_id", "status", "thread_id", "conversation_id", "agent_profile",
                    "turns_used", "tool_calls_used", "tokens_used", "started_at", "ended_at",
                    "model_profile")
            out = []
            for r in rows:
                d = dict(zip(keys, r))
                for k in ("started_at", "ended_at"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                out.append(d)
            return out
        finally:
            conn.close()

    def list_steps(self, run_id: str, *, limit: int = 200) -> "list":
        """run 的步骤时间线（model_call/tool_call/approval/…；payload 落库前已脱敏）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT step_no, kind, payload_json, tokens_prompt, tokens_completion, "
                    f"created_at FROM {db}.agent_step WHERE run_id=%s "
                    "ORDER BY step_no ASC LIMIT %s",
                    (run_id, max(1, min(int(limit), 500))))
                rows = cur.fetchall() or []
            out = []
            for r in rows:
                payload = r[2]
                if isinstance(payload, (str, bytes)):
                    try:
                        payload = json.loads(payload)
                    except Exception:   # noqa: BLE001
                        payload = {"_raw": str(payload)[:200]}
                out.append({"step_no": int(r[0]), "kind": r[1], "payload": payload,
                            "tokens_prompt": r[3], "tokens_completion": r[4],
                            "created_at": str(r[5]) if r[5] is not None else None})
            return out
        finally:
            conn.close()

    # ── llm_call_log（WS1 收尾②：LLM 调用账本，成本按 user/dept 归集）──────────
    def record_llm_call(self, *, run_id: Optional[str], request_id: Optional[str], provider: str,
                        model: str, category: Optional[str], prompt_version: Optional[str],
                        tokens_prompt: Optional[int], tokens_completion: Optional[int],
                        cost_estimate: Optional[float], latency_ms: Optional[int], status: str,
                        user_id: Optional[str], dept_group: Optional[str]) -> str:
        call_id = uuid.uuid4().hex
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.llm_call_log "
                    "(call_id, run_id, request_id, provider, model, category, prompt_version, "
                    " tokens_prompt, tokens_completion, cost_estimate, latency_ms, status, "
                    " user_id, dept_group, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(3))",
                    (call_id, run_id, request_id, provider, model, category, prompt_version,
                     tokens_prompt, tokens_completion, cost_estimate, latency_ms, status,
                     user_id, dept_group),
                )
            conn.commit()
            return call_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
