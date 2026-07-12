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
        self._q: "queue.Queue" = queue.Queue()
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._on_complete = None            # Callable[[str], None]，由 submit/resume 注入
        self._on_failure = None             # Callable[[str], None]，失败侧回调（运维可观测，深度审查治理组）
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
        self._q.put(ev)
        if self._relay is not None:
            self._relay.publish(ev)         # 跨实例镜像（内部 fail-open，绝不影响主路径）

    def _finish(self) -> None:
        self._q.put(_SENTINEL)
        if self._relay is not None:
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
               messages: List, tools: List, on_complete=None, on_failure=None) -> RunHandle:
        self._acquire()
        run_id = None
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
            self._attach_relay(handle)
            with self._lock:
                self._live[run_id] = handle
            gen = loop.run(ctx, messages, tools)
            self._pool.submit(self._drive_gen, ctx, gen, handle)
            return handle
        except Exception:
            if run_id:
                with self._lock:
                    self._live.pop(run_id, None)
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
            if not self._store.transition(run_id, "resuming", "running"):      # ③ 接手
                raise RunRejected(f"run {run_id} resuming→running 失败（并发/迟到）")
            claimed = False                               # 已交棒 running：失败恢复归驱动器
            handle = RunHandle(run_id)
            handle._on_complete = on_complete
            handle._on_failure = on_failure
            self._attach_relay(handle)
            with self._lock:
                self._live[run_id] = handle
            gen = loop.resume(ctx, state, outcome, tools)
            base = self._budget_snapshot(run_id, fallback_turns=int(state.get("turn", 0)) + 1)
            self._pool.submit(self._drive_gen, ctx, gen, handle, base)
            return handle
        except Exception:
            with self._lock:
                self._live.pop(run_id, None)
            if claimed:
                self._safe_transition(run_id, "resuming", "suspended")   # 回边：保住可重试
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
                    break
                if isinstance(ev, RunCheckpointReady):
                    # R4 运行中 checkpoint（loop 侧 RAG_AGENT_MIDRUN_CHECKPOINT 门控才发）：
                    # 持久化后即消费，绝不外发（state_messages 是内部载荷）。
                    self._persist_midrun_checkpoint(run_id, ev)
                    ev = next(gen)
                    continue
                if isinstance(ev, RunCompleted):
                    # 最终答案也是一个模型轮 → 记 model_call step + 计 turn + 记 tokens。
                    # on_complete 在 emit 之前跑（run 完成侧）：客户端看到 done 帧时记忆已落，
                    # 立刻发起的下一轮不会丢上一轮上下文。
                    self._record_model_step(run_id, turns_counted, usage=ev.usage, final=True)
                    self._budget_used(run_id, turns=1, tokens=ev.usage.total)
                    # fencing（重审计 §1 怀疑者 bug）：running→succeeded 的 CAS 成立才证明
                    # 本线程仍持有该 run。CAS 失败 = 已被 reaper 收尸/用户取消/排水标失败——
                    # 此前 _safe_transition 静默吞掉失败而 _notify_complete 照跑，答案落
                    # qa_log/会话记忆而 durable 状态是 failed，两边永久分叉。现在失去所有权
                    # 即作废结果：不落库、不发 done 帧，响亮 error 日志可观测。
                    if self._transition_checked(run_id, "running", "succeeded"):
                        self._notify_complete(handle, ev, retrieved_chunks)
                        handle._emit(ev)
                    else:
                        logger.error(
                            "run %s 完成时已失去所有权（收尸/取消/排水抢先迁移），结果作废不落库",
                            run_id)
                        handle._emit(RunFailed(
                            error="run 已被系统收尸或取消（完成结果作废，请重试）",
                            retryable=True))
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
            hb_stop.set()
            if run_id:
                with self._lock:
                    self._live.pop(run_id, None)
            handle._finish()
            self._release()

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
