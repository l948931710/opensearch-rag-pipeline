# -*- coding: utf-8 -*-
"""
tool_executor.py — 工具执行中间件栈（v2 报告 §4⑥/§9.3 模块 I）

中间件栈（沉淀自 vlm_retry / cost_breaker）：**幂等 → 熔断 → 超时 → 重试 → 执行**，
全程记 tool_invocation（proposed/executing→succeeded/failed），供 trace 与对账。

限制（B1 硬伤）：Python 线程杀不掉——超时用 future.result(timeout) 让**调用方**按时返回，
被超时的工具线程仍挂着直到其自身 socket 超时兜底（故超时不重试）。

audit 中间件（agent_audit_log）已挂：ALLOW 执行前写一条合规审计（write-ahead）——HIGH_WRITE
**fail-closed**（审计不可写→阻断，绝不产生无审计的高风险副作用），READ_ONLY/LOW_WRITE fail-open。
见 audit.py。args_json 按 022 契约**脱敏后入库**（sanitize.py；digest 按原文算供关联回放），
合规审计只落 digest。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Dict, Optional

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
                idempotency_key: Optional[str] = None) -> ToolResult:
        spec = tool.spec
        # 1. 幂等（key_required）：已成功同键 → 复用回执，不重复副作用
        if spec.idempotency == "key_required":
            if not idempotency_key:
                # 契约唯一强制点：key 缺失绝不能静默放行（NULL 键 uk_tool_idem 不去重，
                # 幂等契约在最需要它的地方形同虚设——深度审查 C 组）。
                from opensearch_pipeline.agent_runtime.sanitize import sanitize_args_json
                self._store.record_invocation(
                    run_id, step_no, tool_name=spec.name, tool_version=spec.version,
                    args_json=sanitize_args_json(args), args_digest=digest(args),
                    idempotency_key=None, status="denied",
                    policy_decision=policy_decision, policy_id=policy_id)
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
        # 2. 熔断
        if self._breaker.is_open(spec.name):
            return ToolResult.fail(f"工具 {spec.name} 熔断打开，暂不可用")
        # 记 executing（args_json 脱敏后入库，022 契约；digest 按原文算供关联回放）
        from opensearch_pipeline.agent_runtime.sanitize import sanitize_args_json
        inv_id = self._store.record_invocation(
            run_id, step_no, tool_name=spec.name, tool_version=spec.version,
            args_json=sanitize_args_json(args), args_digest=digest(args),
            idempotency_key=idempotency_key, status="executing",
            policy_decision=policy_decision, policy_id=policy_id)
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
        for i in range(attempts):
            try:
                result = self._run_with_timeout(tool, ctx, args, idempotency_key, spec.timeout_s)
                break
            except FuturesTimeout:
                last_exc = TimeoutError(f"工具 {spec.name} 超时 {spec.timeout_s}s")
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
        try:
            self._store.finish_invocation(inv_id, status="failed", error_text=err)
        except Exception:   # noqa: BLE001
            logger.warning("tool_invocation 失败收尾落库失败", exc_info=True)
        return ToolResult.fail(f"工具执行失败: {err}")

    def _run_with_timeout(self, tool, ctx, args, idempotency_key, timeout_s):
        fut = _get_timeout_pool().submit(tool.run, ctx, args, idempotency_key)
        try:
            return fut.result(timeout=timeout_s)       # 超时抛 FuturesTimeout
        except FuturesTimeout:
            # 仍在排队的任务 cancel 掉——否则超时判定后照样真执行（账面 failed、副作用事后
            # 发生的"幽灵执行"）；已在跑的线程杀不掉（B1 硬伤，见模块 docstring）。
            fut.cancel()
            raise
