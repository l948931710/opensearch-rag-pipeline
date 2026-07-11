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


def _get_timeout_pool() -> ThreadPoolExecutor:
    global _TIMEOUT_POOL
    if _TIMEOUT_POOL is None:
        with _POOL_LOCK:
            if _TIMEOUT_POOL is None:
                _TIMEOUT_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-timeout")
    return _TIMEOUT_POOL


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


class ToolExecutor:
    """执行中间件栈 + tool_invocation 落库。ALLOW 裁决后由 adjudicator 调 execute。"""

    def __init__(self, run_store, *, breaker: Optional[_ToolBreaker] = None, audit=None):
        self._store = run_store
        self._breaker = breaker or _ToolBreaker()
        self._audit = audit or NULL_AUDIT           # 未注入=无操作（既有测试/降级零副作用）

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
            if hit:
                logger.info("工具 %s 幂等命中，复用回执", spec.name)
                receipt = json.loads(hit["receipt_json"]) if hit.get("receipt_json") else None
                # 命中必须回**有内容**的 tool 结果——空 content 让模型收到空 tool 消息，
                # 不知道操作其实已成功，会道歉/重试/编造。
                text = "（幂等命中）该操作此前已成功执行，本次未重复执行。"
                if receipt:
                    text += f" 回执: {json.dumps(receipt, ensure_ascii=False)}"
                return ToolResult.ok(content=[ContentBlock.of_text(text)], receipt=receipt)
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
        # 记 executing（args_json 脱敏后入库，022 契约；digest 按原文算供关联回放）；
        # 回收重试复用原行（inv_id 已置），不再 INSERT。
        if inv_id is None:
            try:
                inv_id = self._store.record_invocation(
                    run_id, step_no, tool_name=spec.name, tool_version=spec.version,
                    args_json=sanitize_args_json(args), args_digest=digest(args),
                    idempotency_key=idempotency_key, status="executing",
                    policy_decision=policy_decision, policy_id=policy_id,
                    approval_request_id=approval_request_id)
            except Exception as e:   # noqa: BLE001
                # 同键并发的插入竞态（两执行同时查无残行→双 INSERT，后者撞 uk_tool_idem）：
                # 转友好拒绝而非把整个 run 打死——另一执行已认领，本次不重复副作用。
                if idempotency_key and ("Duplicate entry" in str(e) or "1062" in str(e)):
                    return ToolResult.fail(
                        f"工具 {spec.name} 同幂等键并发执行冲突（另一执行已认领），本次不重复执行")
                raise
        # audit（write-ahead）：执行前记合规审计。HIGH_WRITE fail-closed=审计不可写则阻断执行
        # （绝不产生无审计的高风险副作用）；READ_ONLY/LOW_WRITE fail-open（写失败仅告警不阻断）。
        fail_closed = spec.risk_level == RiskLevel.HIGH_WRITE
        try:
            self._audit.record(
                ctx, event_type="tool_call", action=spec.qualified_name, decision="authorized",
                risk_level=spec.risk_level.value, policy_id=policy_id, args_digest=digest(args),
                detail={"permission_scope": spec.permission_scope,
                        "data_classification": spec.data_classification,
                        "policy_decision": policy_decision},
                run_id=run_id, step_no=step_no, fail_closed=fail_closed)
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
        for i in range(attempts):
            try:
                result = self._run_with_timeout(tool, ctx, args, idempotency_key, spec.timeout_s)
                break
            except FuturesTimeout:
                last_exc = TimeoutError(f"工具 {spec.name} 超时 {spec.timeout_s}s")
                timed_out = True
                self._breaker.record_fail(spec.name)
                break                                  # 超时不重试（线程仍挂，重试雪上加霜）
            except Exception as e:                     # noqa: BLE001
                last_exc = e
                self._breaker.record_fail(spec.name)
                if i < attempts - 1 and _is_retryable(e):
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
                    has_side_effects = spec.side_effects or spec.risk_level != RiskLevel.READ_ONLY
                    st = "uncertain" if has_side_effects else "failed"
                    self._finish_quiet(inv_id, status=st,
                                       error_text=f"output schema 违约: {schema_err}"[:500])
                    if has_side_effects:
                        return ToolResult.fail(
                            f"工具 {spec.name} 输出不符合契约且副作用可能已发生（已标记待对账）")
                    return ToolResult.fail(f"工具 {spec.name} 输出不符合契约: {schema_err}")
                # 义务后处理（limit_rows/mask_output）：预检已保证全部有执行器；执行器异常
                # fail-closed——内容扣留（绝不把未兑现义务的原文放给模型），回执保留供对账。
                if obligations:
                    try:
                        for ob in obligations:
                            name, _, param = ob.partition(":")
                            result = _OBLIGATION_HANDLERS[name](param, result)
                    except Exception as e:   # noqa: BLE001
                        logger.error("义务执行失败（输出扣留）：%s", obligations, exc_info=True)
                        result = ToolResult(
                            status=result.status,
                            content=[ContentBlock.of_text("[策略义务执行失败，输出已扣留]")],
                            receipt=result.receipt, error=f"obligation-error: {e}"[:200])
            st = "succeeded" if result.status == "succeeded" else "failed"
            try:
                self._store.finish_invocation(
                    inv_id, status=st,
                    result_digest=digest([b.model_dump() for b in result.content]) if result.content else None,
                    receipt_json=json.dumps(result.receipt, ensure_ascii=False) if result.receipt else None,
                    error_text=result.error)
            except Exception:   # noqa: BLE001 — trace 落库失败：结果照常返回，绝不重跑工具
                logger.warning("tool_invocation 收尾落库失败（结果照常返回）", exc_info=True)
            return result
        err = (str(last_exc)[:500] if last_exc else "unknown")
        # P0-E：有副作用的工具超时 → **uncertain**（副作用可能已发生，failed 是谎报——
        # 下游会盲目重试造成重复副作用）。uncertain 阻断同键自动重试，走人工对账。
        if timed_out and (spec.side_effects or spec.risk_level != RiskLevel.READ_ONLY):
            self._finish_quiet(inv_id, status="uncertain",
                               error_text=f"超时 {spec.timeout_s}s（线程无法中止，副作用不可知）")
            return ToolResult.fail(
                f"工具 {spec.name} 执行超时且结果不确定（副作用可能已发生）——"
                "已标记待对账，请勿假定失败后重试")
        self._finish_quiet(inv_id, status="failed", error_text=err)
        return ToolResult.fail(f"工具执行失败: {err}")

    def _finish_quiet(self, inv_id: str, **kw) -> None:
        """收尾落库的容错包装：trace 失败绝不影响结果返回/重跑工具。"""
        try:
            self._store.finish_invocation(inv_id, **kw)
        except Exception:   # noqa: BLE001
            logger.warning("tool_invocation 收尾落库失败（结果照常返回）", exc_info=True)

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
        fut = _get_timeout_pool().submit(tool.run, ctx, args, idempotency_key)
        try:
            return fut.result(timeout=timeout_s)       # 超时抛 FuturesTimeout
        except FuturesTimeout:
            # 仍在排队的任务 cancel 掉——否则超时判定后照样真执行（账面 failed、副作用事后
            # 发生的"幽灵执行"）；已在跑的线程杀不掉（B1 硬伤，见模块 docstring）。
            fut.cancel()
            raise
