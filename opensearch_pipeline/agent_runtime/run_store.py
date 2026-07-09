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
    "resuming": frozenset({"running", "failed", "cancelled"}),               # 执行器接手 → running
    # 终态无出边（succeeded/failed/cancelled/expired）→ 任何迁移即 InvalidTransition
}

_STEP_KINDS = ("model_call", "tool_call", "approval", "compaction", "system")


class InvalidTransition(ValueError):
    """请求了状态机不允许的迁移（编码错误；并发竞态用 transition 返回 False 表达）。"""


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
        except Exception:
            conn.rollback()
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
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT run_id, status, user_id, channel, agent_profile, started_at, ended_at "
                    f"FROM {db}.agent_run WHERE run_id=%s",
                    (run_id,),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {"run_id": row[0], "status": row[1], "user_id": row[2], "channel": row[3],
                    "agent_profile": row[4], "started_at": row[5], "ended_at": row[6]}
        finally:
            conn.close()

    # ── tool_invocation（WS1 收尾：工具调用 trace + 幂等 uk_tool_idem）──────────
    def record_invocation(self, run_id: str, step_no: int, *, tool_name: str, tool_version: str,
                          args_json: str, args_digest: str, idempotency_key: Optional[str],
                          status: str, policy_decision: str, policy_id: str) -> str:
        """记一条工具调用（返回 invocation_id）。status ∈ proposed/denied/pending_approval/executing。"""
        inv_id = uuid.uuid4().hex
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {db}.tool_invocation "
                    "(invocation_id, run_id, step_no, tool_name, tool_version, args_json, args_digest, "
                    " idempotency_key, status, policy_decision, policy_id, started_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(3))",
                    (inv_id, run_id, step_no, tool_name, tool_version, args_json, args_digest,
                     idempotency_key, status, policy_decision, policy_id),
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
