# -*- coding: utf-8 -*-
"""
operation_reconciler.py — PR-3 Stage C：uncertain invocation 自动对账（回执查证）

P0-E 的 uncertain 通道此前只有人工出口（/api/agent/invocations/resolve：管理员核实
副作用后处置）。Stage C 给实现了 `check_operation(operation_id)` 的工具接上自动出口：
reaper 周期扫 uncertain（**已盖对账资格章**——operation_id 列非 NULL，见 schema/046：
「台账无行 ⇒ 未提交」只对确实拿到 operation_id 的执行成立，Stage C 之前的遗留行/
未注入执行永留人工通道——且静置 min_age 后），按台账三态处置——

- **applied** → resolve 为 succeeded（台账回执随处置补进 invocation 行，幂等命中/
  运行中心见真实结果）；
- **not_applied** → resolve 为 failed（放行同键重试；同事务写台账的工具由台账 PK
  fencing 兜底——纵使误判，重试与僵尸并发提交也至多一个 commit）；
- **unknown**（存储不可达/账实错配/工具无 check_operation）→ 不动，留人工通道。

处置走 run_store.resolve_uncertain_invocation 同一 CAS + **同事务审计**（P1-13 纪律，
event_type=invocation_reconcile，与人工路径同型；by=auto-reconciler）。每行独立
fail-open——单行故障不拖垮整轮；整体 flag 默认 **off**（RAG_AGENT_OP_RECONCILE_ENABLE，
新 flag 保守默认纪律），灰度顺序同 durable dispatch：staging 观察后再谈生产。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

RESOLVED_BY = "auto-reconciler"


def op_reconcile_enabled() -> bool:
    return os.environ.get("RAG_AGENT_OP_RECONCILE_ENABLE", "").strip().lower() in (
        "1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def reconcile_uncertain_invocations(run_store, registry, *,
                                    min_age_s: int = None,
                                    limit: int = None) -> Dict[str, int]:
    """跑一轮自动对账。返回计数（观测/测试用）：
    succeeded/failed=已处置；unknown=查证不能（留人工）；skipped=工具无 check_operation
    或已不在 registry；lost=CAS 输给并发处置（人工先手）——皆非错误。"""
    counts = {"succeeded": 0, "failed": 0, "unknown": 0, "skipped": 0, "lost": 0,
              "deferred": 0, "quarantined": 0}
    _max_att = _int_env("RAG_AGENT_RECONCILE_MAX_ATTEMPTS", 20)
    _defer = getattr(run_store, "defer_reconcile", None)

    def _backoff_row(row, why):
        """R3（P1-RT-09）：查不清/不支持的行指数退避（30s..1h），达上限即隔离出
        扫描面（quarantined 上浮，人工通道兜底）——毒头不再永久霸占最老 LIMIT 批。"""
        if _defer is None:
            return
        att = int(row.get("reconcile_attempts") or 0) + 1
        delay = min(3600, 30 * (2 ** min(att, 7)))
        try:
            _defer(row.get("invocation_id"), attempts=att, delay_s=delay)
            counts["deferred"] += 1
            if att >= _max_att:
                counts["quarantined"] += 1
                logger.error("对账隔离：invocation %s（%s，%s）尝试 %d 次仍不可判——"
                             "移出自动扫描面，转人工通道",
                             row.get("invocation_id"), row.get("tool_name"), why, att)
        except Exception:   # noqa: BLE001
            pass
    lister = getattr(run_store, "list_uncertain_invocations_for_reconcile", None)
    resolver = getattr(run_store, "resolve_uncertain_invocation", None)
    if lister is None or resolver is None or registry is None:
        return counts
    if min_age_s is None:
        min_age_s = _int_env("RAG_AGENT_OP_RECONCILE_MIN_AGE_S", 300)
    if limit is None:
        limit = _int_env("RAG_AGENT_OP_RECONCILE_LIMIT", 50)
    rows = lister(min_age_s=min_age_s, limit=limit)
    for row in rows:
        inv_id = row.get("invocation_id")
        try:
            tool = registry.get(row.get("tool_name"))
            checker = getattr(tool, "check_operation", None)
            # 资格章双保险：扫描 SQL 已过滤 operation_id IS NOT NULL，这里对注入的
            # store（测试桩/未来实现）再验一次——没章的行「台账无行」证明不了未提交。
            op_id = row.get("operation_id")
            if not callable(checker) or not op_id:
                counts["skipped"] += 1
                _backoff_row(row, "无 checker/资格章")
                continue
            probe: Dict[str, Any] = checker(op_id) or {}
            outcome = probe.get("outcome")
            if outcome == "applied":
                to_status = "succeeded"
            elif outcome == "not_applied":
                to_status = "failed"
            else:
                counts["unknown"] += 1
                _backoff_row(row, f"outcome={outcome}")
                continue
            note = f"自动查证 {outcome}: {probe.get('detail') or '-'}"
            receipt = probe.get("receipt")
            receipt_json = (json.dumps(receipt, ensure_ascii=False)
                            if to_status == "succeeded" and receipt is not None else None)

            def _audit(cur, _inv=inv_id, _to=to_status, _note=note, _outcome=outcome):
                from opensearch_pipeline.agent_runtime.audit import insert_audit_row_tx
                insert_audit_row_tx(
                    cur, None, event_type="invocation_reconcile", action=_inv,
                    decision=_to,
                    detail={"by": RESOLVED_BY, "note": _note[:500], "outcome": _outcome})

            ok = resolver(inv_id, to_status=to_status, note=note,
                          resolved_by=RESOLVED_BY, audit_writer=_audit,
                          receipt_json=receipt_json)
            if ok:
                counts[to_status] += 1
                logger.warning("自动对账：invocation %s（%s）回执查证 %s → %s",
                               inv_id, row.get("tool_name"), outcome, to_status)
            else:
                counts["lost"] += 1        # 并发处置（人工先手）＝正常
        except Exception as e:   # noqa: BLE001 — 单行故障不拖垮整轮（退避后下轮重试）
            counts["unknown"] += 1
            # RR-2（P1-RR-03）：异常分支同样退避——checker 持续 timeout 是最常见的
            # 「查不清」形态，此前不退避=老记录每轮立即回到最老批次（毒头修一半）
            _backoff_row(row, f"处置异常:{type(e).__name__}")
            logger.warning("自动对账：invocation %s 处置失败（已退避，下轮重试）",
                           inv_id, exc_info=True)
    return counts
