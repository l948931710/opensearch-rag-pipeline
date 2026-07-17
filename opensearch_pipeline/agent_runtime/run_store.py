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
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

from opensearch_pipeline.config import get_config

if TYPE_CHECKING:  # 运行期不导入：context.py 由 Task 8 落地
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

logger = logging.getLogger(__name__)

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


def _is_lock_contention_error(exc: BaseException) -> bool:
    """事务级锁竞争：1213（死锁，victim 事务已被 InnoDB **整体回滚**）/1205（锁等待
    超时，语句已回滚、事务余部由调用方显式 rollback 收干净）。错误码在 args[0]
    （pymysql 惯例，_is_thread_busy_error 同式）；两码之外一律不算——断连族（2013 等）
    另有 _begin 钉连接纪律与 P0-03 消歧收口，不混入重放面。"""
    args = getattr(exc, "args", None) or ()
    return bool(args) and args[0] in (1213, 1205)


class _CompleteLockContention(Exception):
    """complete_run_atomic **语句阶段（commit 前）** 撞 1213/1205 的内部重放信号。
    该窗口「事务确定未生效」成立（COMMIT 从未发出，victim 已回滚/显式 rollback 已清），
    全新事务重放恰一次安全。绝不出方法边界：重放仍撞 → 还原 original 走 P0-01 诚实
    failed；commit 阶段异常（两将军，归 P0-03 消歧）不裹本信号、不进重放面。"""

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


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


def _begin(conn) -> None:
    """把事务边界显式告知连接（外审复查 P1，2026-07-16）：db._get_db_conn 的池化
    SteadyDB 连接不 begin() 时，断连触发 OperationalError 会被 DBUtils 透明重连+
    **单句重试**——多语句事务被悄悄劈成两半（FOR UPDATE 锁/前置写全丢，却 rowcount=1
    照常 commit），complete_run_atomic/suspend_run_atomic 的原子性承诺整体作废。
    begin() 置 _transaction 位后 tough 重试关闭，断连如实抛错走回滚路径。
    仓内先例：spot_checker/cost_breaker 事务方法同款前置。桩连接无 begin 则跳过。"""
    fn = getattr(conn, "begin", None)
    if fn is not None:
        fn()


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
                # P1-12（外审核查 2026-07-16）：message_id 随建 run 同事务写入（ctx 携带，
                # routes 建 ctx 时 setattr）——此前 submit 后才回填 UPDATE，交棒与回填
                # 之间崩溃留下空 message_id（答案锚点丢失）。无值（旧调用方/桩）落 NULL，
                # 由既有 set_message_id 回填兜底，语义不变。
                cur.execute(
                    f"INSERT INTO {db}.agent_run "
                    "(run_id, thread_id, conversation_id, user_id, channel, agent_profile, status, "
                    " acl_groups_snapshot, model_profile, prompt_version, git_sha, message_id, "
                    " heartbeat_at, started_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s,%s,%s,%s,NOW(3),NOW(3))",
                    (run_id, ctx.thread_id, getattr(ctx, "conversation_id", None), ctx.user_id,
                     ctx.channel, agent_profile,
                     json.dumps(list(ctx.acl_groups), ensure_ascii=False),
                     getattr(ctx, "model_profile", None), getattr(ctx, "prompt_version", None),
                     getattr(ctx, "git_sha", None), getattr(ctx, "message_id", None)),
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
        _begin(conn)                        # 多语句事务：钉住连接（禁 SteadyDB 单句重试）
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

    def record_turn(self, run_id: str, *, turn_index: int, tokens_prompt=None,
                    tokens_completion=None, tokens_total: int = 0,
                    final: bool = False) -> None:
        """原子记一个模型轮（perf 批次 C §4.5）：model_call step + 预算累加 + 心跳，
        **单连接单事务**——取代此前 heartbeat / append_step / consume_budget 三次
        连接+提交（每轮 4-5 次往返 → 1 次）。step_no 取号与 append_step 同式
        （MAX+1；同 run 由 executor 单线程驱动，无并发取号）。失败整体回滚——
        executor 侧包 fail-open 并回退分段写，预算强制仍走本地计数。"""
        db = _op_db()
        conn = self._conn()
        _begin(conn)                        # 多语句事务：钉住连接（禁 SteadyDB 单句重试）
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COALESCE(MAX(step_no),0)+1 FROM {db}.agent_step WHERE run_id=%s",
                    (run_id,))
                step_no = int(cur.fetchone()[0])
                cur.execute(
                    f"INSERT INTO {db}.agent_step "
                    "(run_id, step_no, kind, payload_json, tokens_prompt, tokens_completion, created_at) "
                    "VALUES (%s,%s,'model_call',%s,%s,%s,NOW(3))",
                    (run_id, step_no,
                     json.dumps({"turn_index": turn_index, "final": final}, ensure_ascii=False),
                     tokens_prompt, tokens_completion))
                cur.execute(
                    f"UPDATE {db}.agent_run SET turns_used=turns_used+1, "
                    "tokens_used=tokens_used+%s, heartbeat_at=NOW(3) WHERE run_id=%s",
                    (int(tokens_total), run_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def increment_budget(self, run_id: str, *, turns: int = 0, tool_calls: int = 0,
                         tokens: int = 0) -> None:
        """预算累加的**只写版**（perf 批次 C §4.5）：单条 UPDATE、无 SELECT 读回。
        执行器的强制判断走 _drive_gen 本地计数，逐次读回累计值纯属浪费；
        resume 播种仍走 consume_budget（零增量即读，语义不变）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_run SET turns_used=turns_used+%s, "
                    "tool_calls_used=tool_calls_used+%s, tokens_used=tokens_used+%s "
                    "WHERE run_id=%s",
                    (int(turns), int(tool_calls), int(tokens), run_id))
            conn.commit()
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
        _begin(conn)            # 原子性承诺的前提：钉住连接（禁 SteadyDB 断连单句重试劈事务）
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

    def complete_run_atomic(self, run_id: str, extra_writer=None) -> bool:
        """P0-01（unknown-unknowns 批次1）：完成持久化**单事务**——最终答案落库
        （extra_writer 游标回调，serving 层写 qa_session_log 行）+ running→succeeded CAS
        一次 commit。此前两段分事务：succeeded 先 commit、答案写在回调里 best-effort——
        回调失败时 durable 说「成功」而答案永不可恢复（断流用户经 run 详情/会话历史
        全部读空）。与 suspend_run_atomic 同构：
        - 返回 False = run 已不在 running（收尸/取消/排水抢先），**未提交任何写**——
          调用方沿用完成侧 fencing 语义（结果作废）；
        - extra_writer 异常 → 整体回滚后原样抛出——调用方落 failed，绝不发 done；
        - commit 异常但 read-after-write 证实已提交（P0-03，外审核查 2026-07-16）→
          返回 True（照常发 done）；证实不了 → 原样抛（保守失败语义）。

        锁竞争单次重放（staging 灰度 2026-07-17：3 并发 run 完成事务撞 1213，答案被
        打成 AGENT_ERROR）：**语句阶段**撞 1213/1205 → **全新事务重放一次**——新连接、
        _begin 重钉、FOR UPDATE→extra_writer→CAS→commit 整段重走，绝非 SteadyDB 式
        单句重试。安全性：该窗口 COMMIT 从未发出、victim 已被 InnoDB 回滚（1205 由
        显式 rollback 收干净），「事务确定未生效」成立 → 重放即恰一次；幂等由 CAS
        兜底——间隙期他方收口（取消/收尸）→ 重放 FOR UPDATE 读到非 running → False，
        沿用 fencing 语义（重试输家自然 CAS 失败）。commit 阶段异常**不在重放面**
        （结果未知，归 P0-03 消歧；此时重放会把「未知」误判成失去所有权）。重放仍撞
        锁 → 还原原始异常，维持 P0-01 诚实 failed。"""
        try:
            return self._complete_run_txn(run_id, extra_writer)
        except _CompleteLockContention as first:
            logger.warning("run %s 完成事务撞锁竞争（%s）——全新事务重放一次"
                           "（commit 前，事务确定未生效）", run_id, first.original)
            try:
                return self._complete_run_txn(run_id, extra_writer)
            except _CompleteLockContention as second:
                raise second.original from None   # 连续两撞：还原原异常走 P0-01 诚实 failed

    def _complete_run_txn(self, run_id: str, extra_writer=None) -> bool:
        """complete_run_atomic 的单次事务尝试（语义与异常契约见公开方法 docstring）。
        唯一私有约定：语句阶段撞 1213/1205 → 回滚后抛 _CompleteLockContention 重放信号；
        其余异常与返回值原样。"""
        db = _op_db()
        conn = self._conn()
        _begin(conn)            # 原子性承诺的前提：钉住连接（禁 SteadyDB 断连单句重试劈事务）
        try:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT status FROM {db}.agent_run WHERE run_id=%s FOR UPDATE",
                                (run_id,))
                    row = cur.fetchone()
                    if not row or row[0] != "running":
                        conn.rollback()
                        return False
                    if extra_writer is not None:
                        extra_writer(cur)
                    cur.execute(
                        f"UPDATE {db}.agent_run SET status='succeeded', heartbeat_at=NOW(3), "
                        "ended_at=NOW(3) WHERE run_id=%s AND status='running'", (run_id,))
                    ok = cur.rowcount == 1
                if not ok:
                    conn.rollback()
                    return False
            except Exception as stmt_exc:
                try:
                    conn.rollback()         # commit 前异常：事务确定未生效，维持原失败语义
                except Exception:   # noqa: BLE001 — 连接已死时 rollback 也抛，别掩盖原异常
                    pass
                if _is_lock_contention_error(stmt_exc):
                    raise _CompleteLockContention(stmt_exc) from stmt_exc
                raise
            # P0-03（外审核查 2026-07-16）：commit 异常 ≠ 未提交（两将军——服务端可能已
            # 提交、ACK 丢在断连里）。此前与 commit 前异常同路上抛 → 调用方发 RunFailed
            # (answer_persist_failed)，而 durable 已 succeeded+答案已落——客户端被告知
            # 「失败请重试」，DB 却是成功，真假分裂。收口：开**新连接** read-after-write
            # 消歧，读到 succeeded 即整个事务（含答案行）已落地 → 按成功返回；读到其他/
            # 读不出 → 保守维持失败语义原样上抛（调用方 CAS running→failed 在已提交时
            # 必 False，绝不会用 failed 覆盖 succeeded）。
            try:
                conn.commit()
            except Exception as commit_exc:   # noqa: BLE001 — commit 结果未知，先消歧再定
                try:
                    conn.rollback()           # 连接多半已死：best-effort，绝不掩盖原异常
                except Exception:   # noqa: BLE001
                    pass
                if self._completed_despite_commit_error(run_id):
                    logger.warning("run %s complete commit ACK 丢失，read-after-write 证实"
                                   "已提交（status=succeeded）——按成功返回（P0-03）", run_id)
                    return True
                raise commit_exc
            return True
        finally:
            conn.close()

    def _completed_despite_commit_error(self, run_id: str) -> bool:
        """P0-03 消歧读：commit ACK 丢失后开**新连接**读 run status。succeeded 只能由
        complete_run_atomic 的 CAS（running FOR UPDATE 下）写入，读到即证明整个事务
        （含 extra_writer 的答案行）已落地。非 succeeded / 消歧读也失败 → False
        （结果仍未知时宁按失败保守收口，绝不谎报成功）。
        复查修正：典型丢 ACK 面是「客户端读超时、commit 仍在服务端收尾」——立即读会看到
        提交前版本 running；短暂重试（3 次 / ~1s）再定论，收窄慢收尾被误判失败的残窗。
        读到其他终态（failed/cancelled…）= 他方已收口 → 本事务确定未生效，提前定论。"""
        for attempt in range(3):
            if attempt:
                time.sleep(0.5)
            try:
                conn = self._conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT status FROM {_op_db()}.agent_run WHERE run_id=%s",
                            (run_id,))
                        row = cur.fetchone()
                finally:
                    conn.close()
                if row and row[0] == "succeeded":
                    return True
                if row and row[0] != "running":
                    return False            # 他方终态在座：本事务不可能再生效
            except Exception:   # noqa: BLE001 — 消歧读失败：重试；全失败按未知保守收口
                logger.error("run %s commit 结果消歧读失败（第 %s 次）",
                             run_id, attempt + 1, exc_info=True)
        return False

    def transition(self, run_id: str, from_status: str, to_status: str) -> bool:
        """单向状态机迁移（CAS）。合法且当前==from → 迁移并 True；否则 False；非法 pair → 抛。"""
        if to_status not in _ALLOWED_TRANSITIONS.get(from_status, frozenset()):
            raise InvalidTransition(f"agent_run 状态迁移非法: {from_status} → {to_status}")
        db = _op_db()
        set_ended = ", ended_at=NOW(3)" if to_status in _TERMINAL else ""
        conn = self._conn()
        _begin(conn)                        # FOR UPDATE+UPDATE 两语句：钉住连接
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

    # ── durable cancel（PR-3 Stage D，schema/047）───────────────────────────────
    def request_cancel(self, run_id: str, *, requested_by: str = "") -> bool:
        """跨实例取消标记（CAS：仅活动态、仅首标——重复请求不覆写首标，幂等语义由
        调用方按「已标记」处理）。标记 ≠ 承诺：持有者已死的 run 仍由 reaper 按心跳
        收尸；活持有者的 per-run 心跳 ticker（executor）拾取后转进程内协作旗标，
        沿既有轮边界收口为 cancelled。返回 True=本次完成首标。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_run "
                    "SET cancel_requested_at=NOW(3), cancel_requested_by=%s "
                    "WHERE run_id=%s AND status IN ('running','resuming') "
                    "AND cancel_requested_at IS NULL",
                    ((requested_by or "")[:64] or None, run_id))
                ok = cur.rowcount == 1
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel_requested(self, run_id: str) -> bool:
        """durable 取消标记读侧（驱动副本心跳 ticker 轮询；PK 点查，30s/run 一次）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT cancel_requested_at FROM {db}.agent_run WHERE run_id=%s",
                    (run_id,))
                row = cur.fetchone()
            return bool(row and row[0] is not None)
        finally:
            conn.close()

    def count_active_runs(self) -> int:
        """distributed admission（PR-3 Stage D）：全库在跑 run 数（running+resuming——
        suspended 不占执行容量不计）。idx_status_hb 前缀扫，近似值语义：受理点读到的
        计数与并发受理之间无原子性，全局上限是**软上限**（成本护栏），每实例线程池
        仍是硬界。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {db}.agent_run "
                            "WHERE status IN ('running','resuming')")
                row = cur.fetchone()
            return int(row[0]) if row else 0
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

    def expire_suspended_with_expired_approval(self) -> int:
        """批次5 cross-heal（unknown-unknowns P1-08）：审批先过期而 run 仍 suspended——
        schema/037 的 active_thread 生成列让该 thread 一直 409 新提问，直到 run 自身
        TTL（reaper 的 resuming→suspended 回边还会重置 heartbeat 时钟，双 TTL 即便
        数值相等也会漂移）。审批过期=拒绝已成事实 → run 提前收口 expired 释放线程。
        只清「全部审批行均已 expired/rejected_terminate」的挂起 run；已决
        （approved/edited/rejected_feedback）的挂起 run 归 B6 对账重驱，绝不误杀。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.agent_run r "
                    "SET r.status='expired', r.ended_at=NOW(3), r.heartbeat_at=NOW(3) "
                    "WHERE r.status='suspended' "
                    f"AND EXISTS (SELECT 1 FROM {db}.approval_request a "
                    "  WHERE a.run_id=r.run_id AND a.status='expired') "
                    f"AND NOT EXISTS (SELECT 1 FROM {db}.approval_request p "
                    "  WHERE p.run_id=r.run_id "
                    "  AND p.status NOT IN ('expired','rejected_terminate'))")
                n = cur.rowcount
            conn.commit()
            return int(n)
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
        # heartbeat_at/turns_used/tool_calls_used/tokens_used：perf 批次 B §4.3——detail 端点
        # 用它们合成 state_key（与 /status 同口径），同行读取零额外成本
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT run_id, status, user_id, channel, agent_profile, started_at, ended_at, "
                    f"thread_id, conversation_id, model_profile, message_id, "
                    f"heartbeat_at, turns_used, tool_calls_used, tokens_used "
                    f"FROM {db}.agent_run WHERE run_id=%s",
                    (run_id,),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {"run_id": row[0], "status": row[1], "user_id": row[2], "channel": row[3],
                    "agent_profile": row[4], "started_at": row[5], "ended_at": row[6],
                    "thread_id": row[7], "conversation_id": row[8], "model_profile": row[9],
                    "message_id": row[10], "heartbeat_at": row[11], "turns_used": row[12],
                    "tool_calls_used": row[13], "tokens_used": row[14]}
        finally:
            conn.close()

    def get_run_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """轻量状态摘要（perf 批次 B §4.3）：/api/agent/runs/{id}/status 专用——单次往返
        （agent_run 行 + MAX(step_no) 关联子查询走 (run_id,step_no) PK 前缀），供前端两段式
        轮询先比 state_key、未变化即止，取代每拍 4-5 次读的 detail 全量。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT r.run_id, r.status, r.user_id, r.started_at, r.ended_at, "
                    f"r.heartbeat_at, r.turns_used, r.tool_calls_used, r.tokens_used, "
                    f"(SELECT MAX(s.step_no) FROM {db}.agent_step s WHERE s.run_id=r.run_id) "
                    f"FROM {db}.agent_run r WHERE r.run_id=%s",
                    (run_id,),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {"run_id": row[0], "status": row[1], "user_id": row[2], "started_at": row[3],
                    "ended_at": row[4], "heartbeat_at": row[5], "turns_used": row[6],
                    "tool_calls_used": row[7], "tokens_used": row[8], "max_step_no": row[9]}
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
        """key_required 工具幂等：已成功的同键调用 → 返回回执（重放/重试不重复副作用）。
        A4（复核批次3）：args_digest 一并返回——命中方必须比对参数摘要，同键不同参
        =键碰撞/复用，直接复用回执会把 A 操作的结果谎报给 B 操作。"""
        if not idempotency_key:
            return None
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT invocation_id, result_digest, receipt_json, args_digest "
                    f"FROM {db}.tool_invocation "
                    "WHERE tool_name=%s AND idempotency_key=%s AND status='succeeded' "
                    "ORDER BY ended_at DESC LIMIT 1",
                    (tool_name, idempotency_key),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {"invocation_id": row[0], "result_digest": row[1], "receipt_json": row[2],
                    "args_digest": row[3]}
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
                                     note: str, resolved_by: str, audit_writer=None,
                                     receipt_json: Optional[str] = None) -> bool:
        """对账处置（人工=console 端点 / 自动=operation_reconciler，PR-3 Stage C）：
        uncertain → succeeded（核实副作用已生效）/ failed（核实未生效，放行同键重试）。
        CAS 单向、处置人与依据进 error_text（[对账 by <resolved_by>] 前缀——人工/自动
        由 resolved_by 区分）。receipt_json（Stage C）：自动查证到的台账回执随处置补进
        invocation 行——幂等命中/运行中心从此见真实回执（仅 succeeded 方向有意义）。
        P1-13（外审核查 2026-07-16）：agent_audit_log 行经 audit_writer(cur) **同事务**写——
        此前先改状态后路由 best-effort 补审计，「uncertain→succeeded 已翻、审计缺失」
        静默发生。审计写失败=整体回滚（状态不翻），处置与审计要么都在、要么都不在。"""
        if to_status not in ("succeeded", "failed"):
            raise ValueError(f"uncertain 只能对账为 succeeded/failed，收到 {to_status!r}")
        db = _op_db()
        conn = self._conn()
        _begin(conn)                        # 多语句事务（UPDATE+审计 INSERT）：钉住连接
        try:
            with conn.cursor() as cur:
                sets = "status=%s, ended_at=NOW(3), error_text=%s"
                params = [to_status, f"[对账 by {resolved_by}] {note}"[:500]]
                if receipt_json is not None and to_status == "succeeded":
                    sets += ", receipt_json=%s"
                    params.append(receipt_json)
                cur.execute(
                    f"UPDATE {db}.tool_invocation SET {sets} "
                    "WHERE invocation_id=%s AND status='uncertain'",
                    (*params, invocation_id))
                ok = cur.rowcount == 1
                if ok and audit_writer is not None:
                    audit_writer(cur)
            if ok:
                conn.commit()
            else:
                conn.rollback()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_invocation_operation(self, invocation_id: str) -> None:
        """Stage C 对账资格章（schema/046）：executor 对副作用工具**注入成功后**回填
        operation_id=invocation_id。只有盖过章的行才进自动对账——「台账无行 ⇒ 未提交」
        的推断只对确实拿到 operation_id 的执行成立；老行/未注入执行恒 NULL 永留人工通道。
        调用方 fail-open（盖章失败=少条自动化，绝不挡执行）。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {db}.tool_invocation SET operation_id=%s WHERE invocation_id=%s",
                    (invocation_id, invocation_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_uncertain_invocations_for_reconcile(self, *, min_age_s: int = 300,
                                                 limit: int = 50) -> "list":
        """自动对账扫描面（PR-3 Stage C）：uncertain、**已盖资格章**（operation_id 非
        NULL，见 mark_invocation_operation——未盖章的行「台账无行」证明不了未提交，
        永留人工通道）且标记后已静置 min_age_s 的调用（最老优先）。静置期让「刚超时、
        僵尸线程可能还在跑」的窗口先过去——台账 PK 对同事务写台账的工具本已 fencing
        兜底，静置是对非台账 check_operation 实现（日后外部系统查证）的保守面。"""
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT invocation_id, tool_name, run_id, operation_id "
                    f"FROM {db}.tool_invocation "
                    "WHERE status='uncertain' AND operation_id IS NOT NULL "
                    "AND ended_at < DATE_SUB(NOW(3), INTERVAL %s SECOND) "
                    "ORDER BY ended_at LIMIT %s",
                    (int(min_age_s), max(1, min(int(limit), 200))))
                rows = cur.fetchall() or []
            return [{"invocation_id": r[0], "tool_name": r[1], "run_id": r[2],
                     "operation_id": r[3]} for r in rows]
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

    _LLM_CALL_COLS = ("run_id", "request_id", "provider", "model", "category",
                      "prompt_version", "tokens_prompt", "tokens_completion",
                      "cost_estimate", "latency_ms", "status", "user_id", "dept_group")

    def record_llm_calls(self, rows: "list") -> int:
        """llm_call_log **批量**落库（perf 批次 C §4.5：llm_log_outbox 冲刷用）：
        单连接单事务 executemany。rows = record_llm_call 同名 kwargs 的 dict 列表；
        created_at 取冲刷时刻（账本按日对账，亚秒偏移无关紧要）。返回写入行数。"""
        if not rows:
            return 0
        db = _op_db()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {db}.llm_call_log "
                    "(call_id, run_id, request_id, provider, model, category, prompt_version, "
                    " tokens_prompt, tokens_completion, cost_estimate, latency_ms, status, "
                    " user_id, dept_group, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(3))",
                    [(uuid.uuid4().hex, *[r.get(c) for c in self._LLM_CALL_COLS])
                     for r in rows])
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
