# -*- coding: utf-8 -*-
"""
executor.py — RunExecutor（B1 决策(b)：每进程有界执行宿主）（执行模型 §1/§2）

run 主体在**专用有界线程池**里执行（绝不复用 Starlette 请求线程池），与 HTTP 请求生命周期
解耦；SSE 经 RunHandle.events() 消费事件流；满 → RunRejected（HTTP 层映射 429）。
驱动器语义：next → 若 ToolCallProposed 则 adjudicate(Policy)+execute → gen.send(result)。

拓扑无关接缝：本类是"进程内线程池"实现；日后需硬隔离可换"队列+独立 worker 层(c)"，
接口不变（评审 B1 未选 c，此为升级路径）。

⚠️ stub：adjudicator（Policy→Executor 裁决执行一次工具）由 WS1 注入真实实现；跨实例事件
中继（Redis Stream）、C1 per-thread 串行化、E3 排水在 WS1 loop 实现时补。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterator, List, Optional

from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.events import (
    AgentEvent,
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
        self._q: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._on_complete = None            # Callable[[str], None]，由 submit/resume 注入
        self._on_failure = None             # Callable[[str], None]，失败侧回调（运维可观测，深度审查治理组）

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
        self._q.put(ev)

    def _finish(self) -> None:
        self._q.put(_SENTINEL)
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

    def _acquire(self) -> None:
        with self._lock:
            if self._active >= self._max:
                raise RunRejected(f"并发 run 已达上限 {self._max}")
            self._active += 1

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    def submit(self, ctx: ExecutionContext, loop: AgentLoop,
               messages: List, tools: List, on_complete=None, on_failure=None) -> RunHandle:
        self._acquire()
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
            # run_id 建 run 后**就地回填**共享 ctx（route 把同一 ctx 既闭包进 model_fn 又传本方法；
            # 用 with_run_id 造副本会让 model_fn 闭包的 run_id 仍 None → llm_call_log 记账落空。
            # run_id 是"建 run 后回填"字段、非身份/ACL，就地 setattr 不违 frozen 初衷）。
            object.__setattr__(ctx, "run_id", run_id)
            gen = loop.run(ctx, messages, tools)
            self._pool.submit(self._drive_gen, ctx, gen, handle)
            return handle
        except Exception:
            self._release()
            raise

    def resume(self, run_id: str, ctx: ExecutionContext, outcome, loop: AgentLoop,
               tools: List, on_complete=None, on_failure=None,
               approval_meta: Optional[dict] = None) -> RunHandle:
        """WS3 两步 resume：① CAS suspended→resuming（认领，防两审批回调并发重入）② 载 checkpoint +
        记 approvals（批准/改参令 adjudicator 绕过审批执行）+ resuming→running + 驱动 loop.resume。
        返回续跑 run 的 handle。outcome ∈ Approved/Edited/RejectedFeedback/RejectedTerminate。

        ⚠️ 死态防护（深度审查 B 组 P1）：**先 _acquire 再认领**——池满时认领已发生而无人续跑，
        run 会被永久钉死在 resuming（/approve 只认 suspended → 409，无回边无对账）。认领后任何
        失败（checkpoint 解码/版本不兼容/交棒失败）都回滚 resuming→suspended，保住可重试性。
        """
        from opensearch_pipeline.agent_runtime.approval import (
            ApprovalGrant, Approved, Edited, RejectedTerminate)
        from opensearch_pipeline.agent_runtime.loop import decode_checkpoint_state
        from opensearch_pipeline.agent_runtime.tool_executor import digest
        self._acquire()                                   # ① 先占槽：占不到就不动状态机
        claimed = False
        try:
            if not self._store.transition(run_id, "suspended", "resuming"):    # ② 认领
                raise RunRejected(f"run {run_id} 非 suspended 或已被认领")
            claimed = True
            object.__setattr__(ctx, "run_id", run_id)
            if isinstance(outcome, RejectedTerminate):
                # 硬终止：resuming→cancelled（非 failed——是有意停止非错误），不续跑
                self._store.transition(run_id, "resuming", "cancelled")
                claimed = False
                handle = RunHandle(run_id)
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
                self._approvals[f"{run_id}:{pending['call_id']}"] = ApprovalGrant(
                    outcome=outcome, tool_name=pending["tool_name"], args_digest=digest(args),
                    request_id=meta.get("request_id"), decided_by=meta.get("decided_by"),
                    approver_scope=meta.get("approver_scope"))
            if not self._store.transition(run_id, "resuming", "running"):      # ③ 接手
                raise RunRejected(f"run {run_id} resuming→running 失败（并发/迟到）")
            claimed = False                               # 已交棒 running：失败恢复归驱动器
            handle = RunHandle(run_id)
            handle._on_complete = on_complete
            handle._on_failure = on_failure
            gen = loop.resume(ctx, state, outcome, tools)
            base = self._budget_snapshot(run_id, fallback_turns=int(state.get("turn", 0)) + 1)
            self._pool.submit(self._drive_gen, ctx, gen, handle, base)
            return handle
        except Exception:
            if claimed:
                self._safe_transition(run_id, "resuming", "suspended")   # 回边：保住可重试
            self._release()
            raise

    @staticmethod
    def _verify_checkpoint(cp) -> None:
        """P1「checkpoint 明文且 digest 从不校验」：写入时算了 state_digest 但 load 从不核对，
        库内被改/损坏的状态会被原样续跑。resume 前核 sha256——不匹配抛错，认领回滚
        suspended（可由对账/过期处置，绝不带着可疑状态执行）。"""
        import hashlib
        digest = getattr(cp, "state_digest", None)
        if not digest:
            return                                   # 旧行/简化测试桩无 digest：跳过
        blob = cp.state_blob
        if isinstance(blob, str):
            blob = blob.encode("utf-8", "surrogateescape")
        if hashlib.sha256(blob).hexdigest() != digest:
            raise RunRejected(
                f"checkpoint {getattr(cp, 'checkpoint_id', '?')} 完整性校验失败（digest 不匹配），"
                "拒绝续跑")

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
        turns_counted = int((base or {}).get("turns_used", 0))       # turn_index 去重，同批多 call 只计一次
        tool_calls_used = int((base or {}).get("tool_calls_used", 0))
        tokens_used = int((base or {}).get("tokens_used", 0))
        try:
            ev = next(gen)
            while True:
                if isinstance(ev, ToolCallProposed):
                    # 每个模型轮计一次 turn + 累加该轮 usage（同一 tool 批 turn_index 相同→只在首次计）
                    if ev.turn_index >= turns_counted:
                        turns_counted = ev.turn_index + 1
                        tokens_used += ev.usage.total
                        self._heartbeat(run_id)                            # 活跃心跳（僵尸回收判据）
                        self._record_model_step(run_id, ev.turn_index, usage=ev.usage)
                        self._budget_used(run_id, turns=1, tokens=ev.usage.total)
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
                    break
                if isinstance(ev, RunCompleted):
                    # 最终答案也是一个模型轮 → 记 model_call step + 计 turn + 记 tokens。
                    # on_complete 在 emit 之前跑（run 完成侧）：客户端看到 done 帧时记忆已落，
                    # 立刻发起的下一轮不会丢上一轮上下文。
                    self._record_model_step(run_id, turns_counted, usage=ev.usage, final=True)
                    self._budget_used(run_id, turns=1, tokens=ev.usage.total)
                    self._safe_transition(run_id, "running", "succeeded")
                    self._notify_complete(handle, ev)
                    handle._emit(ev)
                    break
                handle._emit(ev)
                if isinstance(ev, RunFailed):
                    self._safe_transition(run_id, "running", "failed")
                    self._notify_failure(handle, ev.error)
                    break
                if handle.cancelled():
                    gen.close()
                    self._safe_transition(run_id, "running", "cancelled")
                    handle._emit(RunFailed(error="用户取消", retryable=False))
                    break
                ev = next(gen)
        except StopIteration:
            pass
        except Exception as e:   # noqa: BLE001 — run 内部异常不外泄，落 failed + 事件
            self._safe_transition(run_id, "running", "failed")
            handle._emit(RunFailed(error=str(e), retryable=False))
            self._notify_failure(handle, str(e))
        finally:
            handle._finish()
            self._release()

    @staticmethod
    def _notify_complete(handle: RunHandle, ev: RunCompleted) -> None:
        cb = handle._on_complete
        if cb is None:
            return
        try:
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

    def _budget_used(self, run_id: Optional[str], **kw) -> dict:
        """consume_budget 容错包装：返回累计 dict；记账失败→{}（durable 记账 fail-open——
        **强制**不依赖它，走 _drive_gen 的本地计数，DB 故障不等于无限预算）。"""
        try:
            return self._store.consume_budget(run_id, **kw) or {}
        except Exception:   # noqa: BLE001
            return {}

    def _budget_snapshot(self, run_id: Optional[str], fallback_turns: int) -> dict:
        """resume 播种本地预算计数：读 durable 已耗值（零增量 consume 即读取）。
        读不到 → 按 checkpoint turn 兜底（宁可少计 tool_calls/tokens 也不重复计 turns——
        修 resume 后 turn 双重计费 + 续跑段逃逸预算，深度审查 C 组 P1）。"""
        try:
            snap = self._store.consume_budget(run_id) or {}
            if snap.get("turns_used") or snap.get("tool_calls_used") or snap.get("tokens_used"):
                return snap
        except Exception:   # noqa: BLE001
            pass
        return {"turns_used": int(fallback_turns), "tool_calls_used": 0, "tokens_used": 0}

    def _heartbeat(self, run_id: Optional[str]) -> None:
        """活跃 run 心跳（每模型轮一刷；配合 run_store.reap_stale_runs 收尸）。fail-open。"""
        try:
            hb = getattr(self._store, "heartbeat", None)
            if hb is not None:
                hb(run_id)
        except Exception:   # noqa: BLE001
            pass

    def _transition_checked(self, run_id: Optional[str], frm: str, to: str) -> bool:
        """关键迁移（如 running→suspended）：CAS False 或 DB 异常都返回 False，由调用方处置。"""
        try:
            return bool(self._store.transition(run_id, frm, to))
        except Exception:   # noqa: BLE001
            return False

    def _fail_over_budget(self, run_id: Optional[str], gen, handle: RunHandle, msg: str) -> None:
        """预算超限 fail-closed：关生成器 + 落 failed + 发 RunFailed 事件。"""
        try:
            gen.close()
        except Exception:   # noqa: BLE001
            pass
        self._safe_transition(run_id, "running", "failed")
        handle._emit(RunFailed(error=msg, retryable=False))
        self._notify_failure(handle, msg)

    def _safe_transition(self, run_id: Optional[str], frm: str, to: str) -> None:
        try:
            self._store.transition(run_id, frm, to)
        except Exception:   # noqa: BLE001 — 状态落库失败不阻断事件投递（普通路径 fail-open）
            pass

    def active_count(self) -> int:
        with self._lock:
            return self._active

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
