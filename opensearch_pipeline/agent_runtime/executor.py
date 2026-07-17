# -*- coding: utf-8 -*-
"""
executor.py — RunExecutor（B1 决策(b)：每进程有界执行宿主）（执行模型 §1/§2）

run 主体在**专用有界线程池**里执行（绝不复用 Starlette 请求线程池），与 HTTP 请求生命周期
解耦；SSE 经 RunHandle.events() 消费事件流；满 → RunRejected（HTTP 层映射 429）。
驱动器语义：next → 若 ToolCallProposed 则 adjudicate(Policy)+execute → gen.send(result)。

拓扑无关接缝：本类是"进程内线程池"实现；日后需硬隔离可换"队列+独立 worker 层(c)"，
接口不变（评审 B1 未选 c，此为升级路径）。

⚠️ stub：adjudicator（Policy→Executor 裁决执行一次工具）由 WS1 注入真实实现；
C1 per-thread 串行化待补。跨实例事件中继（event_relay.py，Redis Stream，flag 默认 off）
与 E3 排水（drain()，ASGI shutdown 挂钩）已落——见 2026-07-11 重审计 §1 修复。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, List, Optional

from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.events import (
    AgentEvent,
    ModelDelta,
    RunCheckpointReady,
    RunCompleted,
    RunFailed,
    RunSuspended,
    ToolCallProposed,
    ToolResultEmitted,
)
from opensearch_pipeline.agent_runtime.loop import AgentLoop
from opensearch_pipeline.agent_runtime.run_store import RunStore
from opensearch_pipeline.agent_runtime.tool import ToolResult

# 裁决+执行一次工具调用（Policy 裁决 → Executor 中间件执行）。WS1 注入真实实现。
Adjudicator = Callable[[ExecutionContext, ToolCallProposed], ToolResult]

logger = logging.getLogger(__name__)

_SENTINEL = object()


class RunRejected(RuntimeError):
    """执行器已达 per-instance 上限 → HTTP 层映射 429（容量层 fail-closed）。"""


class RunHandle:
    """一次 run 的句柄：SSE 经 events() 消费；request_cancel() 协作取消。

    _on_complete：run 正常完成时驱动器在**run 完成侧**回调（落会话记忆/qa_log）——
    绝不挂在 SSE 消费侧：客户端断连（GeneratorExit）会让消费侧回调整段被跳过，
    答案静默不落库（深度审查 D 组）。
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        # P1-05（unknown-unknowns 批次1）：事件队列**有界**——慢客户端/断连后 run 仍在
        # 产出 delta，无界队列 = 每 run 一个内存放大器（×max_concurrent）。上限内正常
        # 缓冲；满则丢 ModelDelta（断流恢复走 durable 轮询，答案本体在完成侧落库）；
        # 非 delta 事件（终态/审批/工具帧）挤掉最旧事件也要入队。<=0 回退无界（历史行为）；
        # 正值下限 2（P1-01，外审核查 2026-07-16）：maxsize=1 时 _finish 的哨兵**必然**挤掉
        # 队列里唯一的终态帧（消费者一帧看不到就收流）；≥2 时哨兵挤掉的通常是更旧帧——
        # 仍有极窄的「消费者并发取走后挤位误弹终态」竞态窗（durable 轮询兜底），但
        # maxsize=1 的必然丢终态被收掉。
        try:
            _qmax = int(os.environ.get("RAG_AGENT_EVENT_QUEUE_MAX", "10000") or 10000)
        except ValueError:
            _qmax = 10000
        self._q: "queue.Queue" = queue.Queue(maxsize=(0 if _qmax <= 0 else max(2, _qmax)))
        self._dropped_deltas = 0
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._on_complete = None            # Callable[[str], None]，由 submit/resume 注入
        self._on_failure = None             # Callable[[str], None]，失败侧回调（运维可观测，深度审查治理组）
        # P0-01：durable 完成写回调 Callable[[cursor, str, Optional[list]], None]——
        # complete_run_atomic 在 running→succeeded 同一事务内调用（写 qa_session_log 行）。
        self._on_complete_durable = None
        self._client_disconnected_at = None   # SSE 消费者断连时刻（routes GeneratorExit 写入）
        self._relay = None                  # event_relay 发布器（flag off 恒 None；fail-open 镜像）

    def events(self) -> Iterator[AgentEvent]:
        """阻塞式消费事件流，直到 run 终止（SSE 端点在此迭代并转 SSE 帧）。"""
        while True:
            item = self._q.get()
            if item is _SENTINEL:
                return
            yield item

    def request_cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout)

    def _emit(self, ev: AgentEvent) -> None:
        self._put_local(ev)
        if self._relay is not None:
            self._relay.publish(ev)         # 跨实例镜像（内部 fail-open，绝不影响主路径）

    def _put_local(self, ev) -> None:
        """P1-05 有界入队。队列满时：ModelDelta 直接丢弃（计数告警）；其余事件
        （终态/审批/工具帧/哨兵）挤掉最旧事件腾位后入队——消费者已停摆时保终态语义
        比保早期增量重要。绝不阻塞驱动线程（put_nowait only）。"""
        try:
            self._q.put_nowait(ev)
            return
        except queue.Full:
            pass
        if isinstance(ev, ModelDelta):
            self._dropped_deltas += 1
            if self._dropped_deltas in (1, 1000) or self._dropped_deltas % 10000 == 0:
                logger.warning("run %s 事件队列已满，累计丢弃 %s 个文本增量（消费者过慢/已断连；"
                               "答案本体在完成侧落库，不受影响）", self.run_id, self._dropped_deltas)
            return
        while True:
            try:
                self._q.get_nowait()        # 挤掉最旧事件（有界队列必然在有限步内腾出位）
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(ev)
                return
            except queue.Full:
                continue

    def _finish(self, *, end_relay: bool = True) -> None:
        """收尾。本地队列恒投哨兵（进程内 SSE 消费者收流——挂起时原 /ask 流也该结束）；
        中继 ``__end__`` 只在**真终态**写（B3，2026-07-13 复核）：挂起不是终态，续跑段
        与挂起段共用同一 run_id 流，挂起点写 ``__end__`` 会让 /runs/{id}/events 回放在
        审批处永久收流——续跑段全部事件（含最终答案帧）经中继永不可达。"""
        self._put_local(_SENTINEL)          # P1-05：满队列也绝不阻塞收尾（挤最旧事件入哨兵）
        if self._relay is not None and end_relay:
            self._relay.end()               # __end__ 哨兵帧：消费侧据此收流
        self._done.set()


class ThreadedRunExecutor:
    """(b) 每进程有界执行器。"""

    def __init__(self, run_store: RunStore, adjudicator: Adjudicator,
                 max_concurrent: int = 4, agent_profile: str = "default", approvals=None,
                 approval_store=None):
        self._store = run_store
        self._adjudicate = adjudicator
        self._max = max_concurrent
        self._profile = agent_profile
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="agent-run")
        self._active = 0
        self._lock = threading.Lock()
        # WS3 审批：{f"{run_id}:{call_id}": ApprovalOutcome}。resume 写入已批准 call，adjudicator 据此
        # 绕过 require_approval 直接执行。须与 make_adjudicator(approvals=同一 dict) 共享同一引用。
        self._approvals = approvals if approvals is not None else {}
        # WS3 审批持久化（schema/025）：挂起侧写 approval_request（fail-closed——请求行写不成则
        # 挂起视为失败，绝不产生审批队列里看不见、只能等过期的黑洞 run）。None = 未接（直驱测试）。
        self._approval_store = approval_store
        # E3 排水（重审计 §1）：_live=在跑 run 的句柄表（drain 据此限时等/兜底标失败）；
        # _draining 置位后拒绝一切新 submit/resume。
        self._live: Dict[str, RunHandle] = {}
        self._draining = False

    def _acquire(self) -> None:
        with self._lock:
            if self._draining:
                raise RunRejected("执行器排水中（实例即将关停），请稍后重试")
            if self._active >= self._max:
                raise RunRejected(f"并发 run 已达上限 {self._max}")
            self._active += 1

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    def submit(self, ctx: ExecutionContext, loop: AgentLoop,
               messages: List, tools: List, on_complete=None, on_failure=None,
               on_complete_durable=None) -> RunHandle:
        self._acquire()
        run_id = None
        dispatch_maybe_scheduled = False   # P0-02 复查修正：pool.submit 异常≠必未入队
        try:
            # F1：投机检索在**准入成功后**才起跑（构造在 serving 层、零成本）——放在
            # _acquire 之后、任何 DB 写之前：被 429 拒的 submit 零检索负载，接纳的 run
            # 预取与 create_run+首轮模型调用最大化重叠。fail-open，起跑失败不碍 run。
            spec = getattr(ctx, "speculative_search", None)
            if spec is not None:
                try:
                    spec.start()
                except Exception:   # noqa: BLE001
                    logger.warning("投机检索起跑失败（忽略，工具侧走真检索）", exc_info=True)
            run_id = self._store.create_run(ctx, self._profile)
            handle = RunHandle(run_id)
            handle._on_complete = on_complete
            handle._on_failure = on_failure
            handle._on_complete_durable = on_complete_durable
            # run_id 建 run 后**就地回填**共享 ctx（route 把同一 ctx 既闭包进 model_fn 又传本方法；
            # 用 with_run_id 造副本会让 model_fn 闭包的 run_id 仍 None → llm_call_log 记账落空。
            # run_id 是"建 run 后回填"字段、非身份/ACL，就地 setattr 不违 frozen 初衷）。
            object.__setattr__(ctx, "run_id", run_id)
            self._attach_relay(handle)
            with self._lock:
                self._live[run_id] = handle
            gen = loop.run(ctx, messages, tools)
            try:
                self._pool.submit(self._drive_gen, ctx, gen, handle)
            except Exception:
                dispatch_maybe_scheduled = not self._dispatch_certainly_rejected()
                raise
            return handle
        except Exception:
            if run_id:
                with self._lock:
                    self._live.pop(run_id, None)
                # P0-02（外审核查 2026-07-16）：run 行已 durable 落 'running'（create_run 即
                # 写），而交棒失败（典型：drain 已 shutdown 线程池 → pool.submit 抛
                # RuntimeError）后无人驱动——不诚实收口就是无人持有的孤儿（只能等 reaper
                # ~15min 收尸，期间 uk_thread_active 占坑）。CAS 门控：已被他方迁移则不动。
                # 复查修正：线程创建失败类 submit 异常条目**可能已入队**（warm worker 仍会
                # 驱动）——误标 failed 会让 fenced 驱动器把真答案作废，那种情况维持旧行为
                # 只上抛（见 _dispatch_certainly_rejected）。
                if not dispatch_maybe_scheduled:
                    self._transition_checked(run_id, "running", "failed")
            self._release()
            raise

    def resume(self, run_id: str, ctx: ExecutionContext, outcome, loop: AgentLoop,
               tools: List, on_complete=None, on_failure=None,
               approval_meta: Optional[dict] = None,
               on_complete_durable=None) -> RunHandle:
        """WS3 两步 resume：① CAS suspended→resuming（认领，防两审批回调并发重入）② 载 checkpoint +
        记 approvals（批准/改参令 adjudicator 绕过审批执行）+ resuming→running + 驱动 loop.resume。
        返回续跑 run 的 handle。outcome ∈ Approved/Edited/RejectedFeedback/RejectedTerminate。

        ⚠️ 死态防护（深度审查 B 组 P1）：**先 _acquire 再认领**——池满时认领已发生而无人续跑，
        run 会被永久钉死在 resuming（/approve 只认 suspended → 409，无回边无对账）。认领后、
        接手前的失败（checkpoint 解码/版本不兼容）回滚 resuming→suspended 保住可重试性；
        **接手 running 后**交棒失败则诚实落 failed（P0-02，外审核查 2026-07-16——running→
        suspended 非合法边，与「接手后进程崩溃」同语义，不再滞留 running 等 reaper）。
        """
        from opensearch_pipeline.agent_runtime.approval import (
            ApprovalGrant, Approved, Edited, RejectedTerminate)
        from opensearch_pipeline.agent_runtime.loop import decode_checkpoint_state
        from opensearch_pipeline.agent_runtime.tool_executor import digest
        self._acquire()                                   # ① 先占槽：占不到就不动状态机
        claimed = False
        running_owned = False   # P0-02：resuming→running 已接手（交棒失败须诚实落 failed）
        dispatch_maybe_scheduled = False   # P0-02 复查修正：pool.submit 异常≠必未入队
        handle = None
        try:
            if not self._store.transition(run_id, "suspended", "resuming"):    # ② 认领
                raise RunRejected(f"run {run_id} 非 suspended 或已被认领")
            claimed = True
            object.__setattr__(ctx, "run_id", run_id)
            if isinstance(outcome, RejectedTerminate):
                # 硬终止：resuming→cancelled（非 failed——是有意停止非错误），不续跑。
                # B3 顺手修：裸建 handle 也要挂中继——否则拒绝终止的终态帧只进本地队列，
                # 跨实例回放端点在挂起帧后空等到超时，永远看不到「已被拒绝」。
                # P0-04（外审核查 2026-07-16）：终态帧以 CAS 成功（或读到等价 durable 终态）
                # 为前置——全文件终态迁移唯此处曾裸调不查结果，路由据返回值答 202
                # status=cancelled，CAS 失败即对外编造终态。False（reaper 回边/purge/并发
                # 抢先）时：本地只收流、中继**不写 __end__**（B3 语义——未收敛非终态，真
                # 终态帧留给对账段发），抛 RunRejected → 路由 409，收敛交 B6 对账
                # （_reconcile_decided 按已落库的 rejected_terminate 决定重驱）。
                # 复查修正：这里用**裸 transition**——DB 异常≠CAS 失败：异常时 claimed 仍
                # True，由 except 处理器立即回边 resuming→suspended 保住可重试性（撤回类
                # 场景可能无 approval_decision 行、B6 对账无从收敛，绝不能钉死在 resuming
                # 等 ~15min reaper）；只有真 CAS False 才走下面的 durable 现状消歧。
                cancelled = bool(self._store.transition(run_id, "resuming", "cancelled"))
                claimed = False
                handle = RunHandle(run_id)
                self._attach_relay(handle)
                if not cancelled:
                    getter = getattr(self._store, "get_run", None)
                    try:
                        cancelled = (getter is not None
                                     and (getter(run_id) or {}).get("status") == "cancelled")
                    except Exception:   # noqa: BLE001 — 读不出按未收敛处理（保守不宣告）
                        cancelled = False
                if not cancelled:
                    logger.error("run %s 拒绝终止 CAS resuming→cancelled 失败（reaper 回边/"
                                 "并发抢先），不发终态帧，交对账收敛", run_id)
                    handle._finish(end_relay=False)
                    raise RunRejected(
                        f"run {run_id} 拒绝终止时状态已被并发迁移，稍后由对账收敛")
                handle._emit(RunFailed(error="审批拒绝并终止", retryable=False))
                handle._finish()
                self._release()
                return handle
            cp = self._store.load_latest_checkpoint(run_id)
            if cp is not None:
                self._verify_checkpoint(cp)           # P1：digest 完整性校验（篡改/损坏不续跑）
            state = (decode_checkpoint_state(cp.state_blob) if cp
                     else {"messages": [], "pending_call": None, "turn": 0})
            pending = state.get("pending_call")
            if pending and isinstance(outcome, (Approved, Edited)):
                # 一次性放行凭据：绑定 (tool_name, args_digest)，adjudicator 消费即销毁——
                # 只放行被批的那一次调用，call_id 复用/改参重放不匹配即重新挂起（A 组 P1）。
                args = (outcome.edited_args if isinstance(outcome, Edited)
                        else pending.get("arguments", {}))
                meta = approval_meta or {}
                # P0-B：接了 approval_store 时，批准/改参凭据必须对应**已落库的
                # approval_decision 行**（同向 + final_args_digest 一致）——读失败/缺行/
                # 不一致一律拒绝续跑（认领回滚 suspended，可重试/对账），堵住「读失败/
                # 记录缺失时 kb_admin 一批准即产生无落库决策支撑的 grant」。
                dec = self._verify_persisted_decision(
                    run_id, outcome, args_digest=digest(args),
                    request_id=meta.get("request_id"))
                self._approvals[f"{run_id}:{pending['call_id']}"] = ApprovalGrant(
                    outcome=outcome, tool_name=pending["tool_name"], args_digest=digest(args),
                    request_id=meta.get("request_id") or (dec or {}).get("request_id"),
                    decided_by=(dec or {}).get("decided_by") or meta.get("decided_by"),
                    approver_scope=meta.get("approver_scope"),
                    decision_id=(dec or {}).get("decision_id"))
            # P1-05（外审核查 2026-07-16）：预算播种移到接手 running **之前**——读取异常
            # fail-closed（_budget_snapshot 抛 RunRejected），此时 claimed 仍 True，走回边
            # suspended 保住可重试；接手后才抛会把已批 run 打成 failed（审批凭据白耗）。
            base = self._budget_snapshot(run_id, fallback_turns=int(state.get("turn", 0)) + 1)
            if not self._store.transition(run_id, "resuming", "running"):      # ③ 接手
                raise RunRejected(f"run {run_id} resuming→running 失败（并发/迟到）")
            claimed = False                               # 已交棒 running：失败恢复归驱动器
            running_owned = True                          # P0-02：此后失败诚实落 failed
            handle = RunHandle(run_id)
            handle._on_complete = on_complete
            handle._on_failure = on_failure
            handle._on_complete_durable = on_complete_durable
            self._attach_relay(handle)
            with self._lock:
                self._live[run_id] = handle
            gen = loop.resume(ctx, state, outcome, tools)
            try:
                self._pool.submit(self._drive_gen, ctx, gen, handle, base)
            except Exception:
                dispatch_maybe_scheduled = not self._dispatch_certainly_rejected()
                raise
            return handle
        except Exception:
            with self._lock:
                self._live.pop(run_id, None)
            if claimed:
                self._safe_transition(run_id, "resuming", "suspended")   # 回边：保住可重试
            elif running_owned and not dispatch_maybe_scheduled:
                # P0-02（外审核查 2026-07-16）：resuming→running 已接手后交棒失败（典型：
                # drain 已 shutdown 线程池 → pool.submit 抛）——run 行停在 'running' 而无人
                # 驱动。running→suspended 非合法边，诚实落 failed（与「接手后进程崩溃」同
                # 语义，此前只能等 reaper ~15min 收尸）；handle 已建则发终态帧+收流
                # （中继消费者不空等）。审批凭据已消费属已知折衷（durable worker=PR-3）。
                # 复查修正：条目可能已入队（线程创建失败类）时不反标——warm worker 仍会驱动。
                self._transition_checked(run_id, "running", "failed")
                if handle is not None:
                    handle._emit(RunFailed(error="续跑交棒失败（执行器不可用），请重试",
                                           retryable=True))
                    handle._finish()
            self._release()
            raise

    def _verify_persisted_decision(self, run_id: str, outcome, *, args_digest: str,
                                   request_id: Optional[str] = None) -> Optional[dict]:
        """P0-B「无落库决策的 grant」：接了 approval_store 时，Approved/Edited 续跑必须能
        锚定一行**已持久化的 approval_decision**——同向（decision==outcome.kind）且
        final_args_digest 与将执行参数摘要完全一致。任何一环失败（读库异常/请求行缺失/
        决定行缺失/方向不符/摘要不符/031 前无摘要历史行）都 RunRejected fail-closed：
        调用方回滚认领（run 留在 suspended），由重试或人工对账处置，绝不带疑执行。
        未接 approval_store（直驱单测/简化桩）→ None 跳过——持久化契约只在持久化在场时强制。"""
        if self._approval_store is None:
            return None
        try:
            rid = request_id
            if not rid:
                latest = self._approval_store.get_latest_by_run(run_id)
                rid = (latest or {}).get("request_id")
            dec = self._approval_store.get_decision(rid) if rid else None
        except Exception as e:   # noqa: BLE001 — 审批事实读不出 → 宁停不批
            raise RunRejected(f"审批决定校验读库失败，拒绝续跑（fail-closed）: {e}")
        if not rid:
            raise RunRejected("该 run 无审批请求记录，拒绝续跑（无决策依据）")
        if not dec:
            raise RunRejected("无已落库的审批决定行，拒绝续跑（宁停不批）")
        if dec.get("decision") != outcome.kind:
            raise RunRejected(
                f"审批决定方向不符（库={dec.get('decision')}，请求={outcome.kind}），拒绝续跑")
        if not dec.get("final_args_digest"):
            raise RunRejected("审批决定缺最终参数摘要（早于 schema/031），拒绝自动续跑")
        if dec["final_args_digest"] != args_digest:
            raise RunRejected("将执行参数与已落库审批决定不一致，拒绝续跑（改参重放被拒）")
        return dec

    @staticmethod
    def _verify_checkpoint(cp) -> None:
        """P1-2 checkpoint 校验（真实性优先）：
        - ``hmac1:`` 摘要 → 带密钥重算 HMAC（encrypt-then-MAC，对库内 blob 原样计算）——
          能写表的攻击者改内容后重算裸 sha256 也过不了（没有密钥）；进程无密钥时拒绝续跑；
        - 裸 sha256（历史行/无密钥环境）→ 沿用完整性核对；密钥在场且
          RAG_AGENT_CHECKPOINT_REQUIRE_HMAC=true（默认 off，迁移窗口）时直接拒——
          防降级攻击（攻击者把摘要改回裸 sha256 绕过 HMAC）。
        任一不匹配抛错，认领回滚 suspended（可由对账/过期处置，绝不带着可疑状态执行）。"""
        import hashlib
        import hmac as _hmac
        import os

        from opensearch_pipeline.agent_runtime.loop import checkpoint_hmac_key
        digest = getattr(cp, "state_digest", None)
        if not digest:
            return                                   # 旧行/简化测试桩无 digest：跳过
        blob = cp.state_blob
        if isinstance(blob, str):
            blob = blob.encode("utf-8", "surrogateescape")
        cp_id = getattr(cp, "checkpoint_id", "?")
        if str(digest).startswith("hmac1:"):
            key = checkpoint_hmac_key()
            if key is None:
                raise RunRejected(
                    f"checkpoint {cp_id} 带 HMAC 摘要但当前进程无密钥（配置回退？），拒绝续跑")
            expect = "hmac1:" + _hmac.new(key, blob, hashlib.sha256).hexdigest()
            if not _hmac.compare_digest(expect, str(digest)):
                raise RunRejected(
                    f"checkpoint {cp_id} 完整性/真实性校验失败（HMAC 不匹配），拒绝续跑")
            return
        if checkpoint_hmac_key() is not None and os.environ.get(
                "RAG_AGENT_CHECKPOINT_REQUIRE_HMAC", "").strip().lower() in (
                "1", "true", "yes", "on"):
            raise RunRejected(
                f"checkpoint {cp_id} 只有裸 sha256 摘要（REQUIRE_HMAC 已开，拒绝降级），拒绝续跑")
        if hashlib.sha256(blob).hexdigest() != digest:
            raise RunRejected(
                f"checkpoint {cp_id} 完整性校验失败（digest 不匹配），拒绝续跑")

    def _drive_gen(self, ctx: ExecutionContext, gen, handle: RunHandle,
                   base: Optional[dict] = None) -> None:
        """驱动器。预算强制走**本地计数**（resume 时由 _budget_snapshot 播种）——
        consume_budget 落 durable 仍每轮照做（fail-open），但强制判断不依赖它：
        否则 DB 故障期间记账被吞成 {}、`.get(...,0) > cap` 恒 False，预算整体退化为
        无限（深度审查 C 组「fail-open 空转」）。本地计数进程内恒可用，不会误杀。"""
        run_id = ctx.run_id
        max_turns = ctx.budget.max_turns
        max_tool_calls = ctx.budget.max_tool_calls
        token_budget = ctx.budget.token_budget
        hb_stop = self._start_heartbeat_ticker(run_id, ctx)   # R2：秒级后台心跳（见方法注）
        turns_counted = int((base or {}).get("turns_used", 0))       # turn_index 去重，同批多 call 只计一次
        tool_calls_used = int((base or {}).get("tool_calls_used", 0))
        tokens_used = int((base or {}).get("tokens_used", 0))
        # P0-A：本执行段各次检索的 chunks artifacts（与 SSE 侧 per_call_chunks 同构）——
        # RunCompleted 时随 on_complete 交给完成侧回调落 qa_session_log.retrieved_docs。
        # 审批续跑无 SSE 消费者，sources 不在完成侧落库即彻底丢失（resume 段只含
        # 恢复后的检索；挂起前批次已在原 /ask SSE 实时下发过）。
        retrieved_chunks: list = []
        suspended = False        # B3：挂起收尾不写中继 __end__（续跑段共用同一流）
        try:
            ev = next(gen)
            while True:
                if isinstance(ev, ToolCallProposed):
                    # 每个模型轮计一次 turn + 累加该轮 usage（同一 tool 批 turn_index 相同→只在首次计）
                    if ev.turn_index >= turns_counted:
                        turns_counted = ev.turn_index + 1
                        tokens_used += ev.usage.total
                        # perf 批次 C §4.5：心跳+step+预算 单事务合并（fail-open，回退分段写）
                        self._record_turn(run_id, ev.turn_index, usage=ev.usage)
                        if turns_counted > max_turns:
                            self._fail_over_budget(run_id, gen, handle, f"turns 预算超限（>{max_turns}）")
                            break
                        if tokens_used > token_budget:
                            self._fail_over_budget(run_id, gen, handle,
                                                   f"token 预算超限（>{token_budget}）")
                            break
                        # deadline 真比较（此前 is_past_deadline 全链零调用点=deadline 形同虚设；
                        # resume 语义=每个活跃执行段一个新窗口，见 routes._requester_ctx 注）
                        if ctx.budget.is_past_deadline(datetime.now(timezone.utc)):
                            self._fail_over_budget(run_id, gen, handle, "run deadline 超时")
                            break
                    handle._emit(ev)                                    # trace/SSE tool_call 帧
                    if handle.cancelled():
                        gen.close()
                        self._safe_transition(run_id, "running", "cancelled")
                        handle._emit(RunFailed(error="用户取消", retryable=False))
                        break
                    # tool_calls 预算：消费后超限即 fail-closed（在 adjudicate 执行副作用**之前**拦住）
                    tool_calls_used += 1
                    self._budget_used(run_id, tool_calls=1)
                    if tool_calls_used > max_tool_calls:
                        self._fail_over_budget(run_id, gen, handle, f"tool_calls 预算超限（>{max_tool_calls}）")
                        break
                    _t0 = time.monotonic()
                    result = self._adjudicate(ctx, ev)                  # Policy → Executor
                    # 工具结局帧（P0-F 运行状态 UX）：只发 status+耗时，不发内容/参数——
                    # 前端据此把「调用工具」阶段收敛为「已完成/被拒/等待审批」，
                    # 兑现「批准≠成功，用户必须看到真实执行结果」的最短路径。
                    handle._emit(ToolResultEmitted(
                        call_id=ev.call_id, tool_name=ev.tool_name,
                        status=getattr(result, "status", "failed") or "failed",
                        elapsed_ms=int((time.monotonic() - _t0) * 1000),
                        turn_index=ev.turn_index,
                        artifacts=getattr(result, "artifacts", None)))   # 进程内旁路（exclude 不序列化）
                    _arts = getattr(result, "artifacts", None) or {}
                    if _arts.get("chunks"):
                        retrieved_chunks.append({
                            "chunks": list(_arts["chunks"]),
                            "included": list(_arts.get("included") or _arts["chunks"])})
                    # 去重键【送达点提交】（评审 R②-4）：只有成功送达模型的结果才登记
                    # seen——超时孤儿/义务扣留的结果永不被消费，keys 永不提交；所有写
                    # 收敛到本驱动线程（duck-typed，executor 不 import agent_tools）。
                    if getattr(result, "status", None) == "succeeded":
                        _keys = (getattr(result, "artifacts", None) or {}).get("dedup_keys")
                        _sess = getattr(ctx, "search_session", None)
                        if _keys is not None and _sess is not None \
                                and hasattr(_sess, "commit_keys"):
                            try:
                                _sess.commit_keys(_keys)
                            except Exception:   # noqa: BLE001 — fail-open
                                logger.warning("search_session 提交失败（忽略）", exc_info=True)
                    ev = gen.send(result)                               # ← B2：回注结果
                    continue
                if isinstance(ev, RunSuspended):
                    # 挂起：持久化 checkpoint（loop 带来 state_messages+pending_call+remaining_calls）
                    # + running→suspended，对外发**剥离内部载荷**的干净 RunSuspended（带
                    # approval_request_id/checkpoint_id）。P0-E：真库路径 checkpoint/审批行/step/
                    # 状态迁移收进**同一事务**（suspend_run_atomic）——不再有半态。
                    # ⚠️ 迁移必须成功才发 approval 帧：迁移被吞时 run 行仍 running、审批端从此 409，
                    # 成为无驱动僵尸而用户以为在等审批（深度审查 B 组）。
                    cp_id, aid, transitioned = self._persist_suspend(ctx, run_id, ev)
                    if not transitioned and not self._transition_checked(run_id, "running", "suspended"):
                        self._safe_transition(run_id, "running", "failed")
                        handle._emit(RunFailed(error="挂起状态落库失败，请重试", retryable=True))
                        self._notify_failure(handle, "挂起状态落库失败")
                        break
                    handle._emit(RunSuspended(approval_request_id=aid, checkpoint_id=cp_id,
                                              pending_call=ev.pending_call, turn_index=ev.turn_index))
                    suspended = True
                    break
                if isinstance(ev, RunCheckpointReady):
                    # R4 运行中 checkpoint（loop 侧 RAG_AGENT_MIDRUN_CHECKPOINT 门控才发）：
                    # 持久化后即消费，绝不外发（state_messages 是内部载荷）。
                    self._persist_midrun_checkpoint(run_id, ev)
                    ev = next(gen)
                    continue
                if isinstance(ev, RunCompleted):
                    # 最终答案也是一个模型轮 → 记 model_call step + 计 turn + 记 tokens。
                    # perf 批次 C §4.5：step+预算(+心跳)单事务合并（fail-open，回退分段写）
                    self._record_turn(run_id, turns_counted, usage=ev.usage, final=True)
                    # P0-02（unknown-unknowns 批次1）：最终模型轮此前完全绕过强制——
                    # token_budget/deadline 不是硬上限。费用已发生 → 诚实记账（上一行）后
                    # post-call 复判：超限落 failed，绝不发 done。turns 语义**有意**维持
                    # 「工具轮循环上界」：final 恰一轮、不可能经它无界增长，不计 turns cap
                    # （否则「恰用满 max_turns 个工具轮再作答」的正常 run 必败）。
                    tokens_used += ev.usage.total
                    if tokens_used > token_budget:
                        self._fail_over_budget(run_id, gen, handle,
                                               f"token 预算超限（>{token_budget}，最终轮后判）")
                        break
                    if ctx.budget.is_past_deadline(datetime.now(timezone.utc)):
                        self._fail_over_budget(run_id, gen, handle, "run deadline 超时（最终轮后判）")
                        break
                    self._complete_run(run_id, handle, ev, retrieved_chunks)
                    break
                handle._emit(ev)
                if isinstance(ev, RunFailed):
                    # D3（复核批次3）失败侧 fencing：与完成侧对齐——CAS 成立才证明本线程
                    # 仍持有 run；失败迁移不成立（已被 purge 删行/收尸/取消抢先）时**不调**
                    # 失败侧回调，否则 qa_session_log 会在主体擦除后再 INSERT 该用户新行。
                    if self._transition_checked(run_id, "running", "failed"):
                        self._notify_failure(handle, ev.error)
                    else:
                        logger.error("run %s 失败收尾时已失去所有权（purge/收尸/取消抢先），"
                                     "跳过失败侧落库", run_id)
                    break
                if handle.cancelled():
                    gen.close()
                    self._safe_transition(run_id, "running", "cancelled")
                    handle._emit(RunFailed(error="用户取消", retryable=False))
                    break
                ev = next(gen)
        except StopIteration:
            # P0-01（外审核查 2026-07-16）：loop 生成器**未发终态事件**即耗尽——协议违约
            # （shipped DefaultAgentLoop 所有退出路径都先发终态帧再 return，此处只防未来
            # loop bug / 第三方 loop）。此前直接 pass：durable 停在 running（只能等 reaper
            # ~15min 收尸，期间 uk_thread_active 占坑 409）、SSE 只见 [DONE] 无终态帧。
            # 所有 break 路径都已各自收口终态，能走到这里必然无终态 → 与异常路径同构
            # 诚实落 failed（D3 fencing：失去所有权不落失败侧回调）。
            handle._emit(RunFailed(error="loop 生成器未发终态事件即结束（协议违约）",
                                   retryable=True))
            if self._transition_checked(run_id, "running", "failed"):
                self._notify_failure(handle, "loop 生成器未发终态事件即结束")
            else:
                logger.error("run %s 生成器意外结束收尾时已失去所有权（purge/收尸/取消抢先），"
                             "跳过失败侧落库", run_id)
        except Exception as e:   # noqa: BLE001 — run 内部异常不外泄，落 failed + 事件
            handle._emit(RunFailed(error=str(e), retryable=False))
            # D3 失败侧 fencing（同 RunFailed 分支）：失去所有权不再落失败侧回调
            if self._transition_checked(run_id, "running", "failed"):
                self._notify_failure(handle, str(e))
            else:
                logger.error("run %s 异常收尾时已失去所有权（purge/收尸/取消抢先），"
                             "跳过失败侧落库", run_id)
        finally:
            hb_stop.set()
            if run_id:
                with self._lock:
                    self._live.pop(run_id, None)
            # perf 批次 A §4.8：run 末投机检索观测（started/hit/miss/wasted_ms/arms_est）——
            # 唯一「已消费 vs 空跑」终点，覆盖成功/失败/取消/挂起。命中率另有 receipt 落库；
            # getattr 保护（_FakeSpec 等桩无 finalize）+ 整体 fail-open（观测绝不污染收尾）。
            _spec = getattr(ctx, "speculative_search", None)
            _fin = getattr(_spec, "finalize", None)
            if _fin is not None:
                try:
                    logger.info("spec_retrieval run=%s %s", run_id, _fin())
                except Exception:   # noqa: BLE001 — 观测绝不抛进 run 收尾
                    pass
            handle._finish(end_relay=not suspended)
            self._release()

    def _complete_run(self, run_id: Optional[str], handle: RunHandle,
                      ev: RunCompleted, retrieved: Optional[list]) -> None:
        """P0-01（unknown-unknowns 批次1）完成真值：durable 答案写与 running→succeeded
        **同一事务**（run_store.complete_run_atomic + handle._on_complete_durable 游标回调），
        此前 succeeded 先 commit、答案写在回调里 best-effort 被吞——durable 说「成功」而
        答案永不可恢复。语义分支：
        - 事务成功 → 先跑缓存性回调（_notify_complete：会话记忆/conversation 增强，
          fail-open——commit 后的副作用失败不再影响真值）再发 done 帧；
        - extra_writer/事务异常 → **落 failed，绝不发 done**（费用已发生也不谎报成功；
          retryable=True，用户重问即可）；
        - CAS False = 失去所有权（收尸/取消/排水抢先）→ 结果作废（原 fencing 语义，
          见 2026-07 重审计 §1——CAS 先于答案落库不是缺陷而是所有权证明，本修复把
          答案写**并入** CAS 事务而非调换顺序）。
        store 无 complete_run_atomic（简化测试桩）→ 回退旧序：CAS → durable 回调以
        cur=None 调用（best-effort，桩环境无原子性可言）→ 缓存回调。"""
        if hasattr(self._store, "complete_run_atomic"):
            writer = handle._on_complete_durable
            extra = None
            if writer is not None:
                def extra(cur):   # noqa: E306
                    writer(cur, ev.final_text, retrieved)
            try:
                ok = self._store.complete_run_atomic(run_id, extra_writer=extra)
            except Exception as e:   # noqa: BLE001 — durable 答案写失败：诚实落 failed
                logger.error("run %s 最终答案落库失败——落 failed，绝不发 done（P0-01）",
                             run_id, exc_info=True)
                handle._emit(RunFailed(
                    error=f"最终答案落库失败，请重试（answer_persist_failed）: {str(e)[:200]}",
                    retryable=True))
                if self._transition_checked(run_id, "running", "failed"):
                    self._notify_failure(handle, f"answer_persist_failed: {e}")
                else:
                    logger.error("run %s 答案落库失败收尾时已失去所有权或结果未知（commit ACK "
                                 "丢失且消歧读也失败时 durable 可能已 succeeded——P0-03 消歧在 "
                                 "store 层，见 complete_run_atomic），跳过失败侧落库", run_id)
                return
            if ok:
                self._notify_complete(handle, ev, retrieved)
                handle._emit(ev)
            else:
                logger.error(
                    "run %s 完成时已失去所有权（收尸/取消/排水抢先迁移），结果作废不落库",
                    run_id)
                handle._emit(RunFailed(
                    error="run 已被系统收尸或取消（完成结果作废，请重试）", retryable=True))
            return
        # 旧路径（测试桩）：CAS 成立后 durable 回调降级为 best-effort（无事务缝可用）
        if self._transition_checked(run_id, "running", "succeeded"):
            writer = handle._on_complete_durable
            if writer is not None:
                try:
                    writer(None, ev.final_text, retrieved)
                except Exception:   # noqa: BLE001
                    logger.warning("run %s durable 完成回调失败（桩路径 best-effort）",
                                   run_id, exc_info=True)
            self._notify_complete(handle, ev, retrieved)
            handle._emit(ev)
        else:
            logger.error(
                "run %s 完成时已失去所有权（收尸/取消/排水抢先迁移），结果作废不落库", run_id)
            handle._emit(RunFailed(
                error="run 已被系统收尸或取消（完成结果作废，请重试）", retryable=True))

    @staticmethod
    def _notify_complete(handle: RunHandle, ev: RunCompleted,
                         retrieved: Optional[list] = None) -> None:
        """retrieved=本执行段各检索批次的 chunks artifacts（P0-A sources 落库）。
        回调双形态兼容：能收第二参的（(final_text, retrieved)）给两参；既有单参回调
        （历史测试/简化桩）照旧只给 final_text——签名探测失败按单参处理。"""
        cb = handle._on_complete
        if cb is None:
            return
        try:
            two_arg = False
            try:
                import inspect
                params = inspect.signature(cb).parameters
                two_arg = (len(params) >= 2
                           or any(p.kind == p.VAR_POSITIONAL for p in params.values()))
            except (TypeError, ValueError):
                two_arg = False
            if two_arg:
                cb(ev.final_text, retrieved)
            else:
                cb(ev.final_text)
        except Exception:   # noqa: BLE001 — 记忆/落库失败不影响回答（辅助失败不破主答案）
            import logging
            logging.getLogger(__name__).warning("run on_complete 回调失败", exc_info=True)

    @staticmethod
    def _notify_failure(handle: RunHandle, error: str) -> None:
        """失败侧回调（qa_session_log AGENT_ERROR 行等运维可观测挂点）。fail-open。"""
        cb = handle._on_failure
        if cb is None:
            return
        try:
            cb(error)
        except Exception:   # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("run on_failure 回调失败", exc_info=True)

    def _persist_suspend(self, ctx: ExecutionContext, run_id: Optional[str],
                         ev: RunSuspended) -> tuple:
        """挂起持久化：encode loop 带来的状态（messages+pending_call+remaining_calls）；
        写 approval_request（schema/025，接了 approval_store 时 **fail-closed**——写不成即抛，
        调用方把 run 落 failed，绝不产生审批队列不可见的黑洞 run）；记 approval agent_step
        （payload 脱敏，022 契约）。返回 (checkpoint_id, approval_request_id, transitioned)。

        P0-E（重评报告 §5E）：两侧 store 都支持时走 **suspend_run_atomic 单事务**——
        checkpoint + 审批行 + step + running→suspended 一次 commit（transitioned=True，
        调用方不再单独迁移）；否则回退旧分事务序（简化测试桩，transitioned=False）。"""
        import uuid

        from opensearch_pipeline.agent_runtime.loop import encode_checkpoint
        from opensearch_pipeline.agent_runtime.sanitize import sanitize_args
        blob, digest = encode_checkpoint(ev.state_messages or [], pending_call=ev.pending_call,
                                         turn=ev.turn_index, remaining_calls=ev.remaining_calls)
        pc = dict(ev.pending_call or {})
        if "arguments" in pc:
            pc["arguments"] = sanitize_args(pc["arguments"])

        atomic = (hasattr(self._store, "suspend_run_atomic")
                  and (self._approval_store is None
                       or hasattr(self._approval_store, "insert_request")))
        if atomic:
            aid = uuid.uuid4().hex
            extra = None
            if self._approval_store is not None and ev.pending_call:
                approval_store, pending_call = self._approval_store, ev.pending_call

                def _extra(cur):
                    approval_store.insert_request(cur, run_id, ctx, pending_call,
                                                  request_id=aid)
                extra = _extra
            cp_id, ok = self._store.suspend_run_atomic(
                run_id, blob, digest,
                step_payload={"pending_call": pc, "approval_request_id": aid},
                extra_writer=extra)
            return cp_id, aid, ok

        cp_id = self._store.save_checkpoint(run_id, blob, digest)
        if self._approval_store is not None and ev.pending_call:
            aid = self._approval_store.create_request(run_id, ctx, ev.pending_call)
        else:
            aid = uuid.uuid4().hex                      # 未接持久化（直驱测试）：沿用内存 id
        try:                                            # 步骤序含审批点（fail-open；参数脱敏后入 payload）
            from opensearch_pipeline.agent_runtime.run_store import AgentStep
            self._store.append_step(run_id, AgentStep(
                kind="approval", payload={"pending_call": pc, "approval_request_id": aid}))
        except Exception:   # noqa: BLE001
            pass
        return cp_id, aid, False

    def _record_model_step(self, run_id: Optional[str], turn_index: int,
                           usage=None, final: bool = False) -> None:
        """④b：每模型轮记一条 model_call agent_step（步骤序 trace；token 明细以 llm_call_log 为准）。
        fail-open：step trace 失败不阻断 run。假 run_store 无 append_step 时静默跳过。"""
        try:
            from opensearch_pipeline.agent_runtime.run_store import AgentStep
            self._store.append_step(run_id, AgentStep(
                kind="model_call", payload={"turn_index": turn_index, "final": final},
                tokens_prompt=(usage.tokens_prompt if usage else None),
                tokens_completion=(usage.tokens_completion if usage else None)))
        except Exception:   # noqa: BLE001
            pass

    def _record_turn(self, run_id: Optional[str], turn_index: int,
                     usage=None, final: bool = False) -> None:
        """每模型轮 durable 记账（perf 批次 C §4.5）：优先 store.record_turn 单事务
        （step+预算+心跳 1 次连接，取代 3 次）；store 无此法（测试桩/旧实现）或单事务
        失败 → 回退老三样（各自 fail-open）。预算强制恒走 _drive_gen 本地计数。"""
        rt = getattr(self._store, "record_turn", None)
        if rt is not None:
            try:
                rt(run_id, turn_index=turn_index,
                   tokens_prompt=(usage.tokens_prompt if usage else None),
                   tokens_completion=(usage.tokens_completion if usage else None),
                   tokens_total=(usage.total if usage else 0), final=final)
                return
            except Exception:   # noqa: BLE001 — 单事务失败 → 回退分段写（不阻断 run）
                logger.warning("record_turn 单事务失败（回退分段写）", exc_info=True)
        if not final:
            self._heartbeat(run_id)   # 轮边界活跃心跳；final 轮沿旧行为不单发（transition 随即刷）
        self._record_model_step(run_id, turn_index, usage=usage, final=final)
        self._budget_used(run_id, turns=1, tokens=(usage.total if usage else 0))

    def _budget_used(self, run_id: Optional[str], **kw) -> dict:
        """durable 预算累加的容错包装（记账失败→{}，fail-open——**强制**不依赖它，
        走 _drive_gen 的本地计数，DB 故障不等于无限预算）。
        perf 批次 C §4.5：优先只写版 increment_budget（单 UPDATE、免 SELECT 读回——
        调用点全都丢弃返回值）；store 无此法回退 consume_budget（老语义）。"""
        inc = getattr(self._store, "increment_budget", None)
        if inc is not None:
            try:
                inc(run_id, **kw)
                return {}
            except Exception:   # noqa: BLE001
                return {}
        try:
            return self._store.consume_budget(run_id, **kw) or {}
        except Exception:   # noqa: BLE001
            return {}

    def _budget_snapshot(self, run_id: Optional[str], fallback_turns: int) -> dict:
        """resume 播种本地预算计数：读 durable 已耗值（零增量 consume 即读取）。
        P1-05（外审核查 2026-07-16）：**读取异常 fail-closed**——DB 瞬断时按零播种会让
        续跑段整段逃逸 token/tool_calls 预算；抛 RunRejected，调用方回滚 suspended
        （可重试）。**合法全零快照维持回退**：首段 increment_budget fail-open 被吞时
        零值是真实库况，硬拒会把已批 run 钉死在瞬时抖动上；宁少计 tool_calls/tokens
        也不重复计 turns（深度审查 C 组双计修复的语义不动）。store 无 consume_budget
        （简化桩）→ 直接回退。"""
        fn = getattr(self._store, "consume_budget", None)
        if fn is not None:
            try:
                snap = fn(run_id) or {}
            except Exception as e:   # noqa: BLE001 — fail-closed：绝不按零播种续跑
                raise RunRejected(
                    f"run {run_id} resume 预算读取失败，拒绝按零播种续跑（可重试）: {e}")
            if snap.get("turns_used") or snap.get("tool_calls_used") or snap.get("tokens_used"):
                return snap
        return {"turns_used": int(fallback_turns), "tool_calls_used": 0, "tokens_used": 0}

    def _heartbeat(self, run_id: Optional[str]) -> None:
        """活跃 run 心跳（每模型轮一刷；配合 run_store.reap_stale_runs 收尸）。fail-open。"""
        try:
            hb = getattr(self._store, "heartbeat", None)
            if hb is not None:
                hb(run_id)
        except Exception:   # noqa: BLE001
            pass

    def _start_heartbeat_ticker(self, run_id: Optional[str], ctx) -> threading.Event:
        """R2（重审计 §1）：per-run 后台心跳 ticker。此前心跳只在模型轮边界刷——长工具
        调用/长最终生成超过 reaper stale 阈值（默认 900s）时，**活着的持有者被误判僵尸**
        → running→failed，随后完成侧 CAS 失败、结果作废。ticker 让「进程活着」与
        「模型轮节奏」解耦（默认 30s ≪ 900s）。deadline 之后停止续命：真僵死（挂死在
        无超时调用里）的 run 不被永久续命，最终仍交还 reaper 收尸。
        RAG_AGENT_HEARTBEAT_INTERVAL_S<=0 显式关闭（回到轮边界心跳的历史行为）。"""
        stop = threading.Event()
        if not run_id:
            return stop
        try:
            interval = float(os.environ.get("RAG_AGENT_HEARTBEAT_INTERVAL_S", "30") or 30)
        except ValueError:
            interval = 30.0
        if interval <= 0:
            return stop

        def _tick():
            while not stop.wait(interval):
                try:
                    if ctx.budget.is_past_deadline(datetime.now(timezone.utc)):
                        return
                except Exception:   # noqa: BLE001 — deadline 读不出不阻断续命
                    pass
                self._heartbeat(run_id)

        threading.Thread(target=_tick, name=f"agent-hb-{run_id[:8]}", daemon=True).start()
        return stop

    def _persist_midrun_checkpoint(self, run_id: Optional[str], ev: RunCheckpointReady) -> None:
        """R4（重审计 §1「无运行中 checkpoint」）：模型轮边界持久化对话状态——此前
        save_checkpoint 唯一调用点是审批挂起，非挂起 run 崩溃即全丢（只剩 agent_step
        局部 trace）。fail-open：写失败绝不阻断 run。
        ⚠️ 边界诚实：这是**状态保全**（事后取证/人工恢复的底座），不是自动回放——
        failed 是终态，崩溃 run 的自动续跑需要 failed→resumable 状态机扩展（未做）。"""
        try:
            from opensearch_pipeline.agent_runtime.loop import encode_checkpoint
            blob, digest = encode_checkpoint(ev.state_messages or [], pending_call=None,
                                             turn=ev.turn_index)
            self._store.save_checkpoint(run_id, blob, digest)
        except Exception:   # noqa: BLE001
            logger.warning("midrun checkpoint 持久化失败（忽略）", exc_info=True)

    def _attach_relay(self, handle: RunHandle) -> None:
        """R5：跨实例事件中继（Redis Stream，RAG_AGENT_EVENT_RELAY=redis 才生效）。
        fail-open：中继装不上只降级为进程内单副本语义（历史行为）。"""
        try:
            from opensearch_pipeline.agent_runtime.event_relay import attach_relay
            attach_relay(handle)
        except Exception:   # noqa: BLE001
            logger.warning("事件中继挂载失败（降级进程内）", exc_info=True)

    def _dispatch_certainly_rejected(self) -> bool:
        """pool.submit 抛异常后判定条目是否**必未入队**（P0-02 复查修正）：CPython 的
        shutdown/broken 检查先于 _work_queue.put——这两态下抛出时条目必未入队，可诚实
        CAS failed；线程创建失败（can't start new thread）时条目**已入队**、warm worker
        仍可能驱动——误标 failed 会让 fenced 驱动器把真答案作废。私有属性拿不到时按
        「可能已入队」保守处理（退回旧行为：只上抛不反标，交 reaper 兜底）。"""
        return bool(getattr(self._pool, "_shutdown", False)
                    or getattr(self._pool, "_broken", False))

    def _transition_checked(self, run_id: Optional[str], frm: str, to: str) -> bool:
        """关键迁移（如 running→suspended）：CAS False 或 DB 异常都返回 False，由调用方处置。"""
        try:
            return bool(self._store.transition(run_id, frm, to))
        except Exception:   # noqa: BLE001
            return False

    def _fail_over_budget(self, run_id: Optional[str], gen, handle: RunHandle, msg: str) -> None:
        """预算超限 fail-closed：关生成器 + 落 failed + 发 RunFailed 事件。
        D3：失败侧回调经 checked CAS 门控（失去所有权=purge/收尸/取消抢先 → 不落库）。"""
        try:
            gen.close()
        except Exception:   # noqa: BLE001
            pass
        handle._emit(RunFailed(error=msg, retryable=False))
        if self._transition_checked(run_id, "running", "failed"):
            self._notify_failure(handle, msg)
        else:
            logger.error("run %s 预算超限收尾时已失去所有权，跳过失败侧落库", run_id)

    def _safe_transition(self, run_id: Optional[str], frm: str, to: str) -> None:
        try:
            self._store.transition(run_id, frm, to)
        except Exception:   # noqa: BLE001 — 状态落库失败不阻断事件投递（普通路径 fail-open）
            pass

    def get_live_handle(self, run_id: str) -> Optional[RunHandle]:
        """A5 服务端 cancel：取本实例在跑 run 的句柄（None=不在本实例/已收尾）。
        cancel 是协作式的（handle.request_cancel 置旗标，驱动线程在轮边界检查）——
        阻塞中的模型/工具调用不被中断，下一个协作检查点收口为 cancelled 终态。"""
        with self._lock:
            return self._live.get(run_id)

    def active_count(self) -> int:
        with self._lock:
            return self._active

    def drain(self, timeout: float = 20.0) -> Dict[str, int]:
        """E3 排水（重审计 §1「无 SIGTERM drain」）：拒新 run → 限时等在跑 run 收尾 →
        超时仍在跑的 **诚实标 failed**（durable 不说谎：随后 SIGKILL 就到，这些 run 必然
        中断；若线程竟在进程死前跑完，完成侧 fencing CAS 会失败 → 结果作废，与标记一致）。
        幂等可重入；由 ASGI shutdown / atexit 调（routes/agent._drain_runtime）。"""
        with self._lock:
            self._draining = True
            live = dict(self._live)
        deadline = time.monotonic() + max(0.0, float(timeout))
        waited = 0
        for _run_id, h in live.items():
            if h.wait(max(0.0, deadline - time.monotonic())):
                waited += 1
        # P0-02（外审核查 2026-07-16）：_live 只见「已注册句柄」——admitted（_acquire 已计数）
        # 但尚在 create_run/交棒途中的 submit/resume 对两次快照都不可见，径直 shutdown 会让
        # 它们撞上已关的池。等 _active 归零（覆盖该窗口：成功收尾与交棒失败侧都 _release）
        # 再关池；超时未归零按原语义走 leftovers 兜底，交棒失败侧已各自诚实落 failed。
        while time.monotonic() < deadline:
            with self._lock:
                if self._active <= 0:
                    break
            time.sleep(0.05)
        with self._lock:
            leftovers = list(self._live)
        force_failed = 0
        for run_id in leftovers:
            if self._transition_checked(run_id, "running", "failed"):
                force_failed += 1
                logger.error("排水超时：run %s 仍在执行——已标 failed（实例关停，结果将作废）",
                             run_id)
        self._pool.shutdown(wait=False)
        return {"waited": waited, "force_failed": force_failed}

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
