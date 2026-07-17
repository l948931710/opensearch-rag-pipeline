# -*- coding: utf-8 -*-
"""
tool_executor.py — 工具执行中间件栈（v2 报告 §4⑥/§9.3 模块 I）

中间件栈（沉淀自 vlm_retry / cost_breaker）：**义务预检 → 幂等/状态机 → 熔断 → 超时 →
重试 → 执行 → 输出契约 → 义务后处理**，全程记 tool_invocation
（proposed/executing→succeeded/failed/uncertain），供 trace 与对账。

限制（B1 硬伤）：Python 线程杀不掉——超时用 future.result(timeout) 让**调用方**按时返回，
被超时的工具线程仍挂着直到其自身 socket 超时兜底（故超时不重试）。P0-E 补的诚实语义：
**有副作用的工具超时不再谎报 failed**（副作用可能已发生）——invocation 落 **uncertain**，
同幂等键的自动重试被阻断，走人工对账（/api/agent/invocations）后方可重发；进程崩溃留下的
stale executing 行由 reaper（mark_stale_invocations_uncertain）收进同一对账通道。
P1-03（外审核查 2026-07-16）把该语义扩到**任何耗尽的异常**（普通异常 ≠ 确定未生效），
并禁掉副作用工具的 in-loop 自动重试；P1-02 过渡加固补 per-tool 并发舱壁
（RAG_AGENT_TOOL_MAX_CONCURRENCY，默认 4）+ 池大小可配（RAG_AGENT_TOOL_TIMEOUT_POOL_SIZE）。
HIGH_WRITE 迁独立 durable worker（lease+outbox）是重评报告 PR-3 的范围，不在本层。

audit 中间件（agent_audit_log）已挂：ALLOW 执行前写一条合规审计（write-ahead）——HIGH_WRITE
**fail-closed**（审计不可写→阻断，绝不产生无审计的高风险副作用），READ_ONLY/LOW_WRITE fail-open。
见 audit.py。args_json 按 022 契约**脱敏后入库**（sanitize.py；digest 按原文算供关联回放），
合规审计只落 digest。

Policy obligations（P1「只收集不执行」修复）：授予携带的义务在本层强制——已注册执行器的
（limit_rows / mask_output / redact_output）在成功结果上生效；**未注册执行器的义务
fail-closed 拒绝执行**（义务无人兑现=授予不成立，绝不静默放行给「已防护」假象）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable, Dict, Optional, Tuple

from opensearch_pipeline.agent_runtime.audit import NULL_AUDIT, AuditWriteError
from opensearch_pipeline.agent_runtime.tool import (
    ContentBlock,
    EnterpriseTool,
    RiskLevel,
    ToolResult,
)

logger = logging.getLogger(__name__)

_TIMEOUT_POOL: Optional[ThreadPoolExecutor] = None
_POOL_LOCK = threading.Lock()


def _timeout_pool_size() -> int:
    """P1-02 过渡加固（外审核查 2026-07-16）：共享超时池大小可配（此前硬编码 8）。"""
    try:
        n = int(os.environ.get("RAG_AGENT_TOOL_TIMEOUT_POOL_SIZE", "8") or 8)
    except ValueError:
        n = 8
    return max(1, n)


def _get_timeout_pool() -> ThreadPoolExecutor:
    global _TIMEOUT_POOL
    if _TIMEOUT_POOL is None:
        with _POOL_LOCK:
            if _TIMEOUT_POOL is None:
                _TIMEOUT_POOL = ThreadPoolExecutor(
                    max_workers=_timeout_pool_size(), thread_name_prefix="tool-timeout")
    return _TIMEOUT_POOL


# ── READ_ONLY 工具 trace/审计异步化（延迟优化第二刀，2026-07-11）───────────────────
# 读工具的 record_invocation/audit/finish_invocation 本就 fail-open（trace 失败绝不影响
# 结果返回），把这 3 笔 RDS 往返挪出关键路径（公网环境省 1-3s/次调用）。
# 边界：**只限 READ_ONLY 且 idempotency != key_required**——幂等状态机（残行裁决/uk 竞态/
# CAS 回收）依赖行同步可见；LOW/HIGH_WRITE 与 write-ahead fail-closed 审计一字不动。
# 单 worker FIFO：record(INSERT) 恒先于 finish(UPDATE) 到库。进程崩溃时在途 trace 可能
# 丢行（读工具无副作用，纯可观测性损失）；正常退出由非 daemon 线程自然排水。
_READ_TRACE_POOL: Optional[ThreadPoolExecutor] = None


def _async_read_trace_enabled() -> bool:
    return os.environ.get("RAG_AGENT_ASYNC_READ_TRACE", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _read_trace_pool() -> ThreadPoolExecutor:
    global _READ_TRACE_POOL
    if _READ_TRACE_POOL is None:
        with _POOL_LOCK:
            if _READ_TRACE_POOL is None:
                _READ_TRACE_POOL = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="tool-read-trace")
    return _READ_TRACE_POOL


def drain_read_trace(timeout: float = 5.0) -> bool:
    """等待已入队的读工具 trace 写全部落库（测试断言/优雅退出前用）。"""
    pool = _READ_TRACE_POOL
    if pool is None:
        return True
    try:
        pool.submit(lambda: None).result(timeout=timeout)
        return True
    except Exception:   # noqa: BLE001
        return False


class _AsyncInv:
    """异步 trace 的 invocation 句柄：单 worker FIFO 下 finish 任务必在 record 之后执行，
    届时 id 已就绪；record 失败则 finish 静默跳过（与同步路径 fail-open 同语义）。"""

    __slots__ = ("id",)

    def __init__(self) -> None:
        self.id: Optional[str] = None


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()


def _is_retryable(exc: Exception) -> bool:
    """借 vlm_retry 白名单思路：连接/超时/限流类可重试，其余不。"""
    s = (type(exc).__name__ + " " + str(exc)).lower()
    return any(k in s for k in ("timeout", "connection", "temporarily", "try again", "503", "429", "reset"))


# ── Policy obligations 执行器（P1：obligations 只收集不执行 → 强制执行点）────────
# 义务串形态 "name[:param]"（policy.py 注释既有约定：如 "limit_rows:1000"、"mask_output:phone"）。
# 注册表可扩展（register_obligation_handler）；**未注册的义务 fail-closed**——在执行副作用
# 之前拒绝整个调用，义务绝不静默蒸发。

def _ob_limit_rows(param: str, result: ToolResult) -> ToolResult:
    """表格类内容截断到 N 行（如 readonly_sql 大结果集出域限制）。"""
    n = max(0, int(param or "0"))
    if n <= 0:
        return result
    blocks = []
    for b in result.content:
        if b.type == "table" and b.table and isinstance(b.table.get("rows"), list) \
                and len(b.table["rows"]) > n:
            blocks.append(ContentBlock.of_table(list(b.table.get("columns") or []),
                                                list(b.table["rows"][:n])))
        else:
            blocks.append(b)
    return ToolResult(status=result.status, content=blocks,
                      receipt=result.receipt, error=result.error)


def _ob_mask_output(param: str, result: ToolResult) -> ToolResult:
    """输出文本 PII 掩码（复用 pii_patterns 全表；param 现为提示性标签，整表掩码）。"""
    from opensearch_pipeline.pii_patterns import scrub_image_text
    blocks = []
    for b in result.content:
        if b.type == "text" and b.text:
            blocks.append(ContentBlock.of_text(scrub_image_text(b.text)))
        elif b.type == "table" and b.table:
            rows = [[scrub_image_text(c) if isinstance(c, str) else c for c in row]
                    for row in (b.table.get("rows") or [])]
            blocks.append(ContentBlock.of_table(list(b.table.get("columns") or []), rows))
        else:
            blocks.append(b)
    return ToolResult(status=result.status, content=blocks,
                      receipt=result.receipt, error=result.error)


_OBLIGATION_HANDLERS: Dict[str, Callable[[str, ToolResult], ToolResult]] = {
    "limit_rows": _ob_limit_rows,
    "mask_output": _ob_mask_output,
    "redact_output": _ob_mask_output,     # 别名：语义同 mask_output
}


def register_obligation_handler(name: str,
                                fn: Callable[[str, ToolResult], ToolResult]) -> None:
    """注册义务执行器（幂等覆盖）。新义务种类必须先有执行器才能进策略规则。"""
    _OBLIGATION_HANDLERS[str(name)] = fn


def unsupported_obligations(obligations: Tuple[str, ...]) -> "list":
    """无执行器的义务清单（fail-closed 预检用）。"""
    return [o for o in (obligations or ())
            if o.partition(":")[0] not in _OBLIGATION_HANDLERS]


class _ToolBreaker:
    """per-tool 熔断（借 cost_breaker 形态）。"""

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0):
        self._t, self._cd = threshold, cooldown_s
        self._fail: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}

    def is_open(self, name: str) -> bool:
        u = self._open_until.get(name, 0.0)
        return bool(u and time.monotonic() < u)

    def record_ok(self, name: str) -> None:
        self._fail.pop(name, None)
        self._open_until.pop(name, None)

    def record_fail(self, name: str) -> None:
        n = self._fail.get(name, 0) + 1
        self._fail[name] = n
        if n >= self._t:
            self._open_until[name] = time.monotonic() + self._cd


class _BulkheadFull(RuntimeError):
    """per-tool 并发舱壁满（P1-02 过渡加固）：拒绝发生在提交执行**之前**——确定无副作用，
    按普通 failed 收口（可立即重试），绝不进 uncertain 对账通道。"""


class ToolExecutor:
    """执行中间件栈 + tool_invocation 落库。ALLOW 裁决后由 adjudicator 调 execute。"""

    def __init__(self, run_store, *, breaker: Optional[_ToolBreaker] = None, audit=None):
        self._store = run_store
        self._breaker = breaker or _ToolBreaker()
        self._audit = audit or NULL_AUDIT           # 未注入=无操作（既有测试/降级零副作用）
        # P1-02 过渡加固（外审核查 2026-07-16）：per-tool 并发舱壁——共享超时池没有配额时
        # 单个慢挂工具即可蚕食整池（熔断只按失败计数，慢而不败不触发），后续无关工具排队
        # 后一起超时。配额在 future **真正完成**时才释放（挂死线程持续占用本工具配额，
        # 这正是舱壁语义：最多吃掉本工具的额度，绝不外溢）。HIGH_WRITE 迁独立 durable
        # worker 仍是 PR-3 的活，本层只做进程内隔离。
        self._tool_sems: Dict[str, threading.Semaphore] = {}
        self._sems_lock = threading.Lock()

    def _tool_sem(self, name: str) -> threading.Semaphore:
        with self._sems_lock:
            sem = self._tool_sems.get(name)
            if sem is None:
                try:
                    quota = int(os.environ.get("RAG_AGENT_TOOL_MAX_CONCURRENCY", "4") or 4)
                except ValueError:
                    quota = 4
                sem = threading.Semaphore(max(1, quota))
                self._tool_sems[name] = sem
            return sem

    def execute(self, ctx, tool: EnterpriseTool, args: Dict[str, Any], *,
                run_id: str, step_no: int, policy_decision: str, policy_id: str,
                idempotency_key: Optional[str] = None,
                obligations: Tuple[str, ...] = ()) -> ToolResult:
        spec = tool.spec
        from opensearch_pipeline.agent_runtime.sanitize import sanitize_args_json
        # P1「invocation→approval 回链」：审批放行的调用由 adjudicator 把 request_id 注入 ctx
        approval_request_id = getattr(ctx, "approval_request_id", None)
        # 0. 义务预检（fail-closed）：无执行器的义务=授予不成立，在任何副作用之前拒绝。
        unsupported = unsupported_obligations(obligations)
        if unsupported:
            self._store.record_invocation(
                run_id, step_no, tool_name=spec.name, tool_version=spec.version,
                args_json=sanitize_args_json(args), args_digest=digest(args),
                idempotency_key=None, status="denied",
                policy_decision=policy_decision, policy_id=policy_id,
                approval_request_id=approval_request_id)
            return ToolResult.denied(
                f"策略义务缺执行器（fail-closed 拒绝）: {', '.join(unsupported)}")
        # 1. 幂等（key_required）：已成功同键 → 复用回执，不重复副作用
        inv_id: Optional[str] = None
        if spec.idempotency == "key_required":
            if not idempotency_key:
                # 契约唯一强制点：key 缺失绝不能静默放行（NULL 键 uk_tool_idem 不去重，
                # 幂等契约在最需要它的地方形同虚设——深度审查 C 组）。
                self._store.record_invocation(
                    run_id, step_no, tool_name=spec.name, tool_version=spec.version,
                    args_json=sanitize_args_json(args), args_digest=digest(args),
                    idempotency_key=None, status="denied",
                    policy_decision=policy_decision, policy_id=policy_id,
                    approval_request_id=approval_request_id)
                return ToolResult.fail(
                    f"工具 {spec.name} 声明 idempotency=key_required 但未提供幂等键，拒绝执行")
            hit = self._store.find_succeeded_invocation(spec.name, idempotency_key)
            # A4（复核批次3）：命中必须复核 args_digest——同键不同参=键碰撞/键复用
            # （位置序号兜底键跨轮撞车、上游误传同键），直接复用会把 A 操作的回执谎报给
            # B 操作。不匹配 → 拒绝复用，响亮告警，按内容派生新键执行（新键对真重放仍
            # 稳定：同参数同摘要）。旧行无 args_digest（历史行）沿用复用行为（fail-open）。
            if hit and hit.get("args_digest") and hit["args_digest"] != digest(args):
                logger.warning(
                    "工具 %s 幂等键 %s 命中但参数摘要不一致（库=%s 本次=%s）——键碰撞/复用，"
                    "拒绝复用回执，按内容派生新键执行",
                    spec.name, idempotency_key, hit["args_digest"], digest(args))
                idempotency_key = f"{idempotency_key}:a{digest(args)[:16]}"
                # P1-04（外审核查 2026-07-16）：派生键也要查 succeeded——碰撞过的调用被
                # **真重放**时，此前直落 INSERT 撞 uk_tool_idem 转「同幂等键并发执行冲突」
                # 误导模型（回执其实就在库里）。派生键内嵌参数摘要，命中即同参重放。
                hit = self._store.find_succeeded_invocation(spec.name, idempotency_key)
                if hit and hit.get("args_digest") and hit["args_digest"] != digest(args):
                    hit = None                      # 派生键仍不同参（理论角落）：走残行裁决
            if hit:
                logger.info("工具 %s 幂等命中，复用回执", spec.name)
                receipt = json.loads(hit["receipt_json"]) if hit.get("receipt_json") else None
                # 命中必须回**有内容**的 tool 结果——空 content 让模型收到空 tool 消息，
                # 不知道操作其实已成功，会道歉/重试/编造。
                text = "（幂等命中）该操作此前已成功执行，本次未重复执行。"
                if receipt:
                    text += f" 回执: {json.dumps(receipt, ensure_ascii=False)}"
                result = ToolResult.ok(content=[ContentBlock.of_text(text)], receipt=receipt)
                # P1-04（外审核查 2026-07-16）：命中路径是机读回执唯一被渲染进模型可见
                # 文本的地方，此前零后处理——首执时模型只见义务处理过的 content、从不见
                # 回执原文，命中却把回执未掩码原样入文（义务一旦挂上即成旁路）。收口：
                # ① 回执过**当前** output_schema（版本漂移的旧回执拒绝复用，宁停不错；
                #    无回执的历史行不适用，跳过）；
                # ② 统一走当前决策的 obligations（apply-on-hit：策略收紧后回放按新姿态
                #    处理，绝不固化写入时姿态）。
                if receipt is not None and self._validate_output(spec, result):
                    logger.warning(
                        "工具 %s 幂等回执不符当前 output_schema（键=%s，疑似版本漂移）——"
                        "拒绝复用，交人工核对", spec.name, idempotency_key)
                    return ToolResult.fail(
                        f"工具 {spec.name} 幂等回执与当前输出契约不符（工具版本漂移？），"
                        "拒绝复用——请人工核对后处置")
                return self._apply_obligations_safe(obligations, result)
            # 1b. P0-E 状态机护栏：uk_tool_idem 下同键 ≤1 行——任何非 succeeded 残行都会让
            # 直接 INSERT 撞 IntegrityError（=「stale 行阻塞重试且无对账」的根）。按残行状态裁决：
            finder = getattr(self._store, "find_invocation_by_key", None)
            prior = finder(spec.name, idempotency_key) if finder is not None else None
            if prior is not None:
                pst = prior.get("status")
                stale_s = int(os.environ.get("RAG_AGENT_INV_STALE_S", "900"))
                if pst == "uncertain":
                    return ToolResult.fail(
                        f"工具 {spec.name} 此前一次同键执行结果不确定（超时/进程崩溃），已阻断"
                        "自动重试——请管理员在对账视图核实副作用后处置（确认未生效才可重发）")
                if pst == "executing":
                    if int(prior.get("age_s") or 0) >= stale_s \
                            and hasattr(self._store, "mark_invocation_uncertain"):
                        self._store.mark_invocation_uncertain(
                            prior["invocation_id"], note=f"同键重试发现 stale executing（>{stale_s}s）")
                        return ToolResult.fail(
                            f"工具 {spec.name} 发现僵尸执行（已标记待对账），本次不重复执行")
                    return ToolResult.fail(
                        f"工具 {spec.name} 同幂等键的执行仍在进行中，本次不重复执行")
                if pst == "failed":
                    # 明确失败（无副作用语义）→ CAS 回收原行重试（fencing：并发重试单胜者）
                    if hasattr(self._store, "reclaim_failed_invocation") \
                            and self._store.reclaim_failed_invocation(prior["invocation_id"]):
                        inv_id = prior["invocation_id"]
                    else:
                        return ToolResult.fail(
                            f"工具 {spec.name} 同键重试认领冲突（并发），请稍后再试")
        # 2. 熔断
        if self._breaker.is_open(spec.name):
            return ToolResult.fail(f"工具 {spec.name} 熔断打开，暂不可用")
        # 异步 trace 切面（READ_ONLY 且非 key_required 且开关开）：三笔 trace 写挪出关键路径。
        # 幂等状态机分支（key_required）在上方已把 inv_id 置为字符串 → 天然走同步路径。
        async_trace = (spec.risk_level is RiskLevel.READ_ONLY
                       and spec.idempotency != "key_required"
                       and _async_read_trace_enabled())
        # 记 executing（args_json 脱敏后入库，022 契约；digest 按原文算供关联回放）；
        # 回收重试复用原行（inv_id 已置），不再 INSERT。
        if inv_id is None:
            rec_kw = dict(tool_name=spec.name, tool_version=spec.version,
                          args_json=sanitize_args_json(args), args_digest=digest(args),
                          idempotency_key=idempotency_key, status="executing",
                          policy_decision=policy_decision, policy_id=policy_id,
                          approval_request_id=approval_request_id)
            if async_trace:
                inv_id = _AsyncInv()
                _read_trace_pool().submit(self._record_async, inv_id, run_id, step_no, rec_kw)
            else:
                try:
                    inv_id = self._store.record_invocation(run_id, step_no, **rec_kw)
                except Exception as e:   # noqa: BLE001
                    # 同键并发的插入竞态（两执行同时查无残行→双 INSERT，后者撞 uk_tool_idem）：
                    # 转友好拒绝而非把整个 run 打死——另一执行已认领，本次不重复副作用。
                    if idempotency_key and ("Duplicate entry" in str(e) or "1062" in str(e)):
                        return ToolResult.fail(
                            f"工具 {spec.name} 同幂等键并发执行冲突（另一执行已认领），本次不重复执行")
                    raise
        # audit（write-ahead）：执行前记合规审计。HIGH_WRITE fail-closed=审计不可写则阻断执行
        # （绝不产生无审计的高风险副作用）；READ_ONLY/LOW_WRITE fail-open（写失败仅告警不阻断）。
        # async_trace（恒 READ_ONLY ⇒ fail_closed=False）时审计入队伍随 FIFO 尾随 record。
        fail_closed = spec.risk_level == RiskLevel.HIGH_WRITE
        audit_kw = dict(
            event_type="tool_call", action=spec.qualified_name, decision="authorized",
            risk_level=spec.risk_level.value, policy_id=policy_id, args_digest=digest(args),
            detail={"permission_scope": spec.permission_scope,
                    "data_classification": spec.data_classification,
                    "policy_decision": policy_decision},
            run_id=run_id, step_no=step_no, fail_closed=fail_closed)
        if async_trace:
            _read_trace_pool().submit(self._audit_quiet, ctx, audit_kw)
        else:
            try:
                self._audit.record(ctx, **audit_kw)
            except AuditWriteError as e:
                self._store.finish_invocation(inv_id, status="failed",
                                              error_text=f"audit-blocked: {e}"[:500])
                return ToolResult.fail("审计不可用，高风险操作已阻断")
        # 3-4. 超时 + 重试。⚠️ try 只包工具执行本体：finish_invocation 落库异常绝不能被
        # 当成"工具失败"而重跑**已产生副作用**的工具（深度审查 C 组）。
        attempts = max(1, spec.max_retries + 1)
        last_exc: Optional[Exception] = None
        result: Optional[ToolResult] = None
        timed_out = False
        bulkhead_full = False
        has_side_effects = spec.side_effects or spec.risk_level != RiskLevel.READ_ONLY
        for i in range(attempts):
            try:
                result = self._run_with_timeout(tool, ctx, args, idempotency_key, spec.timeout_s)
                break
            except FuturesTimeout:
                last_exc = TimeoutError(f"工具 {spec.name} 超时 {spec.timeout_s}s")
                timed_out = True
                self._breaker.record_fail(spec.name)
                break                                  # 超时不重试（线程仍挂，重试雪上加霜）
            except _BulkheadFull as e:
                last_exc = e
                bulkhead_full = True
                break                                  # 满载不是工具故障：不计熔断不重试
            except Exception as e:                     # noqa: BLE001
                last_exc = e
                self._breaker.record_fail(spec.name)
                # P1-03（外审核查 2026-07-16）：副作用工具禁 in-loop 自动重试——"可重试"
                # 白名单（connection reset/timeout 类）恰是「下游可能已提交、响应阶段抛」
                # 的经典形态，in-loop 立刻重放与同键回收重试同罪（重复副作用）。
                if i < attempts - 1 and _is_retryable(e) and not has_side_effects:
                    logger.warning("工具 %s 可重试错误，重试 %d/%d: %s", spec.name, i + 1, attempts - 1, e)
                    continue
                break
        if result is not None:
            # 熔断按 result.status 计数——不能"没抛异常就 record_ok"：catch-all 工具
            # （如 knowledge_search）内部吞异常返 ToolResult.fail，无条件 record_ok 会把
            # 失败计数清零，熔断器永远打不开（深度审查 C 组）。denied/pending 不计。
            if result.status == "succeeded":
                self._breaker.record_ok(spec.name)
            elif result.status == "failed":
                self._breaker.record_fail(spec.name)
            # 输出契约（P1：ToolResult output schema 从不校验）：receipt 是工具的机读输出，
            # 违约按风险处置——无副作用工具判 failed（安全）；有副作用工具判 uncertain
            # （副作用已发生但回执不可信，进对账，绝不给模型一个残缺"成功"回执）。
            if result.status == "succeeded":
                schema_err = self._validate_output(spec, result)
                if schema_err:
                    st = "uncertain" if has_side_effects else "failed"
                    self._finish(inv_id, status=st,
                                 error_text=f"output schema 违约: {schema_err}"[:500])
                    if has_side_effects:
                        return ToolResult.fail(
                            f"工具 {spec.name} 输出不符合契约且副作用可能已发生（已标记待对账）")
                    return ToolResult.fail(f"工具 {spec.name} 输出不符合契约: {schema_err}")
                # 义务后处理（limit_rows/mask_output）：见 _apply_obligations_safe——
                # 与幂等命中路径共用同一强制点（P1-04）。
                result = self._apply_obligations_safe(obligations, result)
            st = "succeeded" if result.status == "succeeded" else "failed"
            self._finish(
                inv_id, status=st,
                result_digest=digest([b.model_dump() for b in result.content]) if result.content else None,
                receipt_json=json.dumps(result.receipt, ensure_ascii=False) if result.receipt else None,
                error_text=result.error)
            return result
        err = (str(last_exc)[:500] if last_exc else "unknown")
        if bulkhead_full:
            # 舱壁拒绝发生在提交执行之前——确定无副作用，普通 failed（可立即重试），
            # 绝不进 uncertain 对账通道。
            self._finish(inv_id, status="failed", error_text=err)
            return ToolResult.fail(err)
        # P0-E：有副作用的工具超时 → **uncertain**（副作用可能已发生，failed 是谎报——
        # 下游会盲目重试造成重复副作用）。P1-03（外审核查 2026-07-16）扩面：**任何**耗尽
        # 的异常同收 uncertain——普通异常 ≠ 确定未生效（requests.ReadTimeout/connection
        # reset 等正是「下游已提交、响应读取阶段抛」的经典形态，且不是 FuturesTimeout）。
        # uncertain 阻断同键自动重试，走人工对账。工具的**预边界失败**（参数校验等）应以
        # ToolResult.fail 表达（唯一写工具的全部预提交路径已如此），不受本收口影响。
        if has_side_effects:
            reason = (f"超时 {spec.timeout_s}s（线程无法中止，副作用不可知）" if timed_out
                      else f"执行异常且副作用不可知: {err}"[:500])
            self._finish(inv_id, status="uncertain", error_text=reason)
            return ToolResult.fail(
                f"工具 {spec.name} 执行{'超时' if timed_out else '异常'}且结果不确定"
                "（副作用可能已发生）——已标记待对账，请勿假定失败后重试")
        self._finish(inv_id, status="failed", error_text=err)
        return ToolResult.fail(f"工具执行失败: {err}")

    def _finish(self, inv, **kw) -> None:
        """收尾统一分发：异步句柄（READ_ONLY trace 队列，FIFO 保证在 record 之后）或同步容错。"""
        if isinstance(inv, _AsyncInv):
            _read_trace_pool().submit(self._finish_async, inv, kw)
        else:
            self._finish_quiet(inv, **kw)

    def _finish_quiet(self, inv_id: str, **kw) -> None:
        """收尾落库的容错包装：trace 失败绝不影响结果返回/重跑工具。"""
        try:
            self._store.finish_invocation(inv_id, **kw)
        except Exception:   # noqa: BLE001
            logger.warning("tool_invocation 收尾落库失败（结果照常返回）", exc_info=True)

    def _record_async(self, inv: "_AsyncInv", run_id: str, step_no: int, kw: dict) -> None:
        """异步 record（trace 队列 worker 内）：失败仅告警（读工具 trace fail-open）。"""
        try:
            inv.id = self._store.record_invocation(run_id, step_no, **kw)
        except Exception:   # noqa: BLE001
            logger.warning("读工具 trace record 异步落库失败（fail-open）", exc_info=True)

    def _finish_async(self, inv: "_AsyncInv", kw: dict) -> None:
        """异步 finish：record 未成功落库（id 缺失）则跳过——与同步 fail-open 同语义。"""
        if inv.id is None:
            logger.warning("读工具 trace：record 未落库，收尾跳过（fail-open）")
            return
        self._finish_quiet(inv.id, **kw)

    def _audit_quiet(self, ctx, kw: dict) -> None:
        """异步审计（READ_ONLY 恒 fail-open）：任何异常只告警，绝不影响已返回的结果。"""
        try:
            self._audit.record(ctx, **kw)
        except Exception:   # noqa: BLE001
            logger.warning("读工具审计异步写失败（fail-open）", exc_info=True)

    @staticmethod
    def _apply_obligations_safe(obligations: Tuple[str, ...], result: ToolResult) -> ToolResult:
        """义务后处理（limit_rows/mask_output）：预检已保证全部有执行器；执行器异常
        fail-closed——内容扣留（绝不把未兑现义务的原文放给模型），回执保留供对账。
        首执成功路径与幂等命中路径共用本强制点（P1-04 apply-on-hit）。"""
        if not obligations:
            return result
        try:
            for ob in obligations:
                name, _, param = ob.partition(":")
                result = _OBLIGATION_HANDLERS[name](param, result)
            return result
        except Exception as e:   # noqa: BLE001
            logger.error("义务执行失败（输出扣留）：%s", obligations, exc_info=True)
            return ToolResult(
                status=result.status,
                content=[ContentBlock.of_text("[策略义务执行失败，输出已扣留]")],
                receipt=result.receipt, error=f"obligation-error: {e}"[:200])

    @staticmethod
    def _validate_output(spec, result: ToolResult) -> Optional[str]:
        """成功结果的 receipt 过 output_schema（宽 schema 如 {"type":"object"} 自然全过）。
        返回违约信息或 None。校验器自身异常 fail-open（契约校验的基础设施故障不该杀掉
        已成功的调用——与「辅助失败不破主答案」同一铁律）。"""
        try:
            from jsonschema import Draft202012Validator
            from jsonschema.exceptions import ValidationError
            try:
                Draft202012Validator(spec.output_schema).validate(result.receipt or {})
                return None
            except ValidationError as e:
                return e.message
        except Exception:   # noqa: BLE001
            logger.warning("output schema 校验器异常（fail-open）", exc_info=True)
            return None

    def _run_with_timeout(self, tool, ctx, args, idempotency_key, timeout_s):
        # P1-02 舱壁：拿不到本工具配额即 fail-fast（绝不排队——排队等待会被算进调用超时，
        # 而队列堆积正是共享池被蚕食的形态）。
        sem = self._tool_sem(tool.spec.name)
        if not sem.acquire(blocking=False):
            raise _BulkheadFull(
                f"工具 {tool.spec.name} 并发额度已满（舱壁保护共享线程池），请稍后重试")
        released = threading.Event()

        def _release_once(_fut=None):
            if not released.is_set():          # submit 失败与 done 回调互斥，无并发竞争
                released.set()
                sem.release()

        try:
            fut = _get_timeout_pool().submit(tool.run, ctx, args, idempotency_key)
        except Exception:
            _release_once()
            raise
        # 配额随 future **真正完成**释放（含排队被 cancel）：超时返回调用方后，挂死线程
        # 仍占本工具配额直到自身 socket 超时兜底——这正是舱壁要的占用语义。
        fut.add_done_callback(_release_once)
        try:
            return fut.result(timeout=timeout_s)       # 超时抛 FuturesTimeout
        except FuturesTimeout:
            # 仍在排队的任务 cancel 掉——否则超时判定后照样真执行（账面 failed、副作用事后
            # 发生的"幽灵执行"）；已在跑的线程杀不掉（B1 硬伤，见模块 docstring）。
            fut.cancel()
            raise
