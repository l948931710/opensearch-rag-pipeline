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

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterator, List, Optional

from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.events import (
    AgentEvent,
    RunCompleted,
    RunFailed,
    RunSuspended,
    ToolCallProposed,
)
from opensearch_pipeline.agent_runtime.loop import AgentLoop
from opensearch_pipeline.agent_runtime.run_store import RunStore
from opensearch_pipeline.agent_runtime.tool import ToolResult

# 裁决+执行一次工具调用（Policy 裁决 → Executor 中间件执行）。WS1 注入真实实现。
Adjudicator = Callable[[ExecutionContext, ToolCallProposed], ToolResult]

_SENTINEL = object()


class RunRejected(RuntimeError):
    """执行器已达 per-instance 上限 → HTTP 层映射 429（容量层 fail-closed）。"""


class RunHandle:
    """一次 run 的句柄：SSE 经 events() 消费；request_cancel() 协作取消。"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._q: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self._done = threading.Event()

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
                 max_concurrent: int = 4, agent_profile: str = "default", approvals=None):
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

    def _acquire(self) -> None:
        with self._lock:
            if self._active >= self._max:
                raise RunRejected(f"并发 run 已达上限 {self._max}")
            self._active += 1

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    def submit(self, ctx: ExecutionContext, loop: AgentLoop,
               messages: List, tools: List) -> RunHandle:
        self._acquire()
        try:
            run_id = self._store.create_run(ctx, self._profile)
            handle = RunHandle(run_id)
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
               tools: List) -> RunHandle:
        """WS3 两步 resume：① CAS suspended→resuming（认领，防两审批回调并发重入）② 载 checkpoint +
        记 approvals（批准/改参令 adjudicator 绕过审批执行）+ resuming→running + 驱动 loop.resume。
        返回续跑 run 的 handle。outcome ∈ Approved/Edited/RejectedFeedback/RejectedTerminate。"""
        from opensearch_pipeline.agent_runtime.approval import Approved, Edited, RejectedTerminate
        from opensearch_pipeline.agent_runtime.loop import decode_checkpoint_state
        if not self._store.transition(run_id, "suspended", "resuming"):    # ① 认领
            raise RunRejected(f"run {run_id} 非 suspended 或已被认领")
        self._acquire()
        try:
            object.__setattr__(ctx, "run_id", run_id)
            if isinstance(outcome, RejectedTerminate):
                # 硬终止：resuming→cancelled（非 failed——是有意停止非错误），不续跑
                self._store.transition(run_id, "resuming", "cancelled")
                handle = RunHandle(run_id)
                handle._emit(RunFailed(error="审批拒绝并终止", retryable=False))
                handle._finish()
                self._release()
                return handle
            cp = self._store.load_latest_checkpoint(run_id)
            state = (decode_checkpoint_state(cp.state_blob) if cp
                     else {"messages": [], "pending_call": None, "turn": 0})
            pending = state.get("pending_call")
            if pending and isinstance(outcome, (Approved, Edited)):
                self._approvals[f"{run_id}:{pending['call_id']}"] = outcome    # 令 adjudicator 放行该 call
            if not self._store.transition(run_id, "resuming", "running"):      # ② 接手
                raise RunRejected(f"run {run_id} resuming→running 失败（并发/迟到）")
            handle = RunHandle(run_id)
            gen = loop.resume(ctx, state, outcome, tools)
            self._pool.submit(self._drive_gen, ctx, gen, handle)
            return handle
        except Exception:
            self._release()
            raise

    def _drive_gen(self, ctx: ExecutionContext, gen, handle: RunHandle) -> None:
        run_id = ctx.run_id
        max_turns = ctx.budget.max_turns
        max_tool_calls = ctx.budget.max_tool_calls
        turns_counted = 0            # 已计入预算的模型轮数（turn_index 去重，同批多 call 只计一次）
        try:
            ev = next(gen)
            while True:
                if isinstance(ev, ToolCallProposed):
                    # 每个模型轮计一次 turn（同一 tool 批 turn_index 相同→只在首次计）；超 max_turns → fail-closed
                    if ev.turn_index >= turns_counted:
                        turns_counted = ev.turn_index + 1
                        self._record_model_step(run_id, ev.turn_index)     # ④b：模型轮记 model_call step
                        if self._budget_used(run_id, turns=1).get("turns_used", 0) > max_turns:
                            self._fail_over_budget(run_id, gen, handle, f"turns 预算超限（>{max_turns}）")
                            break
                    handle._emit(ev)                                    # trace/SSE tool_call 帧
                    if handle.cancelled():
                        gen.close()
                        self._safe_transition(run_id, "running", "cancelled")
                        handle._emit(RunFailed(error="用户取消", retryable=False))
                        break
                    # tool_calls 预算：消费后超限即 fail-closed（在 adjudicate 执行副作用**之前**拦住）
                    if self._budget_used(run_id, tool_calls=1).get("tool_calls_used", 0) > max_tool_calls:
                        self._fail_over_budget(run_id, gen, handle, f"tool_calls 预算超限（>{max_tool_calls}）")
                        break
                    result = self._adjudicate(ctx, ev)                  # Policy → Executor
                    ev = gen.send(result)                               # ← B2：回注结果
                    continue
                if isinstance(ev, RunSuspended):
                    # 挂起：持久化 checkpoint（loop 带来 state_messages+pending_call）+ running→suspended，
                    # 对外发**剥离 state_messages** 的干净 RunSuspended（带 approval_request_id/checkpoint_id）。
                    cp_id, aid = self._persist_suspend(run_id, ev)
                    self._safe_transition(run_id, "running", "suspended")
                    handle._emit(RunSuspended(approval_request_id=aid, checkpoint_id=cp_id,
                                              pending_call=ev.pending_call, turn_index=ev.turn_index))
                    break
                handle._emit(ev)
                if isinstance(ev, RunCompleted):
                    # 最终答案也是一个模型轮 → 记 model_call step + 计 turn + 记 tokens
                    self._record_model_step(run_id, turns_counted, usage=ev.usage, final=True)
                    self._budget_used(run_id, turns=1, tokens=ev.usage.total)
                    self._safe_transition(run_id, "running", "succeeded")
                    break
                if isinstance(ev, RunFailed):
                    self._safe_transition(run_id, "running", "failed")
                    break
                ev = next(gen)
        except StopIteration:
            pass
        except Exception as e:   # noqa: BLE001 — run 内部异常不外泄，落 failed + 事件
            self._safe_transition(run_id, "running", "failed")
            handle._emit(RunFailed(error=str(e), retryable=False))
        finally:
            handle._finish()
            self._release()

    def _persist_suspend(self, run_id: Optional[str], ev: RunSuspended) -> tuple:
        """挂起持久化：encode loop 带来的状态 → save_checkpoint；记 approval agent_step；
        返回 (checkpoint_id, approval_request_id)。"""
        import uuid

        from opensearch_pipeline.agent_runtime.loop import encode_checkpoint
        blob, digest = encode_checkpoint(ev.state_messages or [], pending_call=ev.pending_call,
                                         turn=ev.turn_index)
        cp_id = self._store.save_checkpoint(run_id, blob, digest)
        aid = uuid.uuid4().hex
        try:                                            # 步骤序含审批点（fail-open）
            from opensearch_pipeline.agent_runtime.run_store import AgentStep
            self._store.append_step(run_id, AgentStep(
                kind="approval", payload={"pending_call": ev.pending_call, "approval_request_id": aid}))
        except Exception:   # noqa: BLE001
            pass
        return cp_id, aid

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
        """consume_budget 容错包装：返回累计 dict；记账失败→{}（fail-open，不因预算记账崩杀 run）。"""
        try:
            return self._store.consume_budget(run_id, **kw) or {}
        except Exception:   # noqa: BLE001
            return {}

    def _fail_over_budget(self, run_id: Optional[str], gen, handle: RunHandle, msg: str) -> None:
        """预算超限 fail-closed：关生成器 + 落 failed + 发 RunFailed 事件。"""
        try:
            gen.close()
        except Exception:   # noqa: BLE001
            pass
        self._safe_transition(run_id, "running", "failed")
        handle._emit(RunFailed(error=msg, retryable=False))

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
