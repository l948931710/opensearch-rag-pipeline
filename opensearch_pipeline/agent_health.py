# -*- coding: utf-8 -*-
"""agent_health.py — Agent 权威监控面作业（Majors γ3/M9.3+M9.4+M6.iii，codex 共识 2026-07-21）。

通用 qa_rollup 不取 model_name、通用看板显式排除 agent 行——agent 的可用率/延迟/队列
健康在权威 SLO 面完全缺席（M9）。本作业是 ops_monitor._JOBS 第 8 项（op0-only）：
按北京业务日聚合 agent_run 终态事实 + 采集八维瞬时快照，UPSERT 进 agent_daily_metrics
（schema/056，健康 flag-on 前置），越阈 send_ops_alert(dedup_key="agent-health")。

八维定案（NULL=该维 N/A，依赖对应 flag；采集 SQL 见各 _dim_* 函数）：
  1 dispatch_backlog        durable dispatch on：outbox.backlog_count()（首个消费者）
  2 approval_pending        approval_request status='pending' count + oldest 龄
  3 stale_running           running 且 heartbeat 龄 > RAG_AGENT_STALE_RUNNING_S（复用 reaper 阈）
  4 uncertain               tool_invocation status='uncertain' count + oldest 龄（复用 INV_STALE 语义）
  5 relay_health            relay on：**权威=近窗终态 run 的 Redis 终局帧缺失率**（逐流
                            xrevrange 找 run_completed/run_failed/__end__）+ Redis 可达性；
                            publish_failures INCR 计数器=best-effort **下界**（发布失败时
                            对 Redis 的 INCR 本身也可能失败）如实标注。
                            ⚠️ Redis 探测失败 → 该维 error、作业 exit 3——绝不报 0/假绿。
  6 worker_heartbeat        durable dispatch on：dispatcher tick 逐轮写 rag_runtime_contract
                            （operation 库，key=agent_dispatcher_heartbeat，value=holder），
                            读 updated_at 服务端龄（免时钟漂移）；行缺失=心跳从未写=breach。
  7 cancellation_latency    当日终态且 cancel_requested_at 非空：ended_at−cancel_requested_at p95（047 列）
  8 recovery_convergence    durable dispatch on：done 命令 created_at→run ended_at p95
                            （**代理口径**：含正常快路径受理，非纯恢复段——如实标注）

可用率事实（codex v4 blocker：M9 必须有 agent 独立可用率）：当日 agent_run 终态
total/succeeded/failed/cancelled/expired；success_rate=succeeded/total（total=0 → NULL）。
延迟：active_latency（各腿活跃耗时累计，054，不含审批等待）p50/p95；TTR=ended_at−started_at
墙钟（含审批等待）**只进 056 不进通用 p95**。分位在 Python 侧算（MySQL 8 无 percentile，
agent 日终态行数 ≤ 数千，取列内存排序足够）。

日界：北京业务日，与 qa_rollup 同约定（存储时区=太平洋墙钟，_beijing_day_pacific_bounds
换算半开区间；换算失败回退 CONVERT_TZ 谓词，fail-open 只慢不歪）。

exit 映射（ops_monitor._job_exit）：flag off/simulate → skipped=0；采集/UPSERT 硬故障或
Redis 探测失败 → error=3；任一维 breach → ok=False=2；全绿 → 0。

阈值 env（master off 下含理性默认；越阈=breach）：
  RAG_AGENT_BACKLOG_MAX=50 / RAG_AGENT_RELAY_MISS_RATE_MAX=0.01 /
  RAG_AGENT_WORKER_HB_MAX_S=300 / RAG_AGENT_CANCEL_LAT_MAX_S=120 /
  RAG_AGENT_RECOVERY_MAX_S=900 / RAG_AGENT_AVAIL_MIN=0.95 /
  RAG_AGENT_APPROVAL_SLA_HOURS=24（approval oldest 超时=积压老化 breach，M6.iii）
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TERMINAL = ("succeeded", "failed", "cancelled", "expired")
_HB_KEY = "agent_dispatcher_heartbeat"
_RELAY_TERMINAL_TYPES = ("run_completed", "run_failed", "__end__")
_RELAY_SAMPLE_MAX = 50


def _flag_on(name: str, default: str = "") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def enabled() -> bool:
    """RAG_AGENT_HEALTH_ENABLE，默认 off（skipped/exit 0——056 未 apply 也零触碰）。"""
    return _flag_on("RAG_AGENT_HEALTH_ENABLE")


def _op_db() -> str:
    from opensearch_pipeline.config import get_config
    return get_config().rds.operation_database


def _percentile(values: List[float], q: float) -> Optional[int]:
    """最近秩法（与 qa 看板同姿态：小样本诚实取实测值，不插值）。空 → None。"""
    if not values:
        return None
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, int(round(q * (len(vs) - 1)))))
    return int(vs[idx])


def _day_predicate(col: str, day: str) -> Tuple[str, tuple]:
    """北京业务日 → 存储时区谓词（qa_rollup 同约定：sargable 区间优先，换算失败
    回退 DST-correct CONVERT_TZ 谓词）。"""
    from opensearch_pipeline.qa_rollup import _beijing_day_pacific_bounds
    bounds = _beijing_day_pacific_bounds(day)
    if bounds is not None:
        return f"{col} >= %s AND {col} < %s", (bounds[0], bounds[1])
    return (f"DATE(CONVERT_TZ({col}, 'America/Los_Angeles', 'Asia/Shanghai')) = %s",
            (day,))


def _beijing_today() -> str:
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:   # noqa: BLE001 — 缺 tzdata 时退本机时区（仅影响默认日选择）
        return datetime.now().strftime("%Y-%m-%d")


# ── 采集：可用率 + 延迟分位（当日 agent_run 终态） ────────────────────────────


def _collect_day_facts(conn, db: str, day: str) -> Dict[str, Any]:
    pred, params = _day_predicate("ended_at", day)
    out: Dict[str, Any] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT status, COUNT(*) FROM {db}.agent_run "
            f"WHERE status IN ('succeeded','failed','cancelled','expired') AND {pred} "
            "GROUP BY status", params)
        counts = {r[0]: int(r[1]) for r in (cur.fetchall() or [])}
    total = sum(counts.values())
    out["runs_total"] = total
    out["runs_succeeded"] = counts.get("succeeded", 0)
    out["runs_failed"] = counts.get("failed", 0)
    out["runs_cancelled"] = counts.get("cancelled", 0)
    out["runs_expired"] = counts.get("expired", 0)
    out["success_rate"] = (round(out["runs_succeeded"] / total, 4) if total else None)
    out["error_rate"] = (round(out["runs_failed"] / total, 4) if total else None)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT active_latency_ms, TIMESTAMPDIFF(MICROSECOND, started_at, ended_at) "
            f"FROM {db}.agent_run WHERE status IN ('succeeded','failed','cancelled','expired') "
            f"AND {pred}", params)
        rows = cur.fetchall() or []
    actives = [float(r[0]) for r in rows if r[0] is not None]
    ttrs = [float(r[1]) / 1000.0 for r in rows if r[1] is not None]
    out["active_latency_p50_ms"] = _percentile(actives, 0.5)
    out["active_latency_p95_ms"] = _percentile(actives, 0.95)
    out["ttr_p50_ms"] = _percentile(ttrs, 0.5)
    out["ttr_p95_ms"] = _percentile(ttrs, 0.95)
    with conn.cursor() as cur:   # 维 7：取消收敛延迟（047 cancel_requested_at）
        cur.execute(
            f"SELECT TIMESTAMPDIFF(SECOND, cancel_requested_at, ended_at) "
            f"FROM {db}.agent_run WHERE cancel_requested_at IS NOT NULL "
            f"AND status IN ('succeeded','failed','cancelled','expired') AND {pred}", params)
        cl = [float(r[0]) for r in (cur.fetchall() or []) if r[0] is not None and r[0] >= 0]
    out["cancellation_latency_p95_s"] = _percentile(cl, 0.95)
    return out


# ── 采集：瞬时八维 ────────────────────────────────────────────────────────────


def _dim_dispatch(conn, db: str, day: str) -> Dict[str, Any]:
    """维 1 backlog + 维 8 recovery（均依赖 durable dispatch flag；off=N/A）。"""
    from opensearch_pipeline.agent_runtime.durable_dispatcher import durable_dispatch_enabled
    if not durable_dispatch_enabled():
        return {"dispatch_backlog": None, "recovery_convergence_p95_s": None,
                "worker_heartbeat_age_s": None}
    out: Dict[str, Any] = {}
    from opensearch_pipeline.agent_runtime.dispatch_outbox import RDSDispatchOutbox
    out["dispatch_backlog"] = int(RDSDispatchOutbox().backlog_count())
    pred, params = _day_predicate("c.updated_at", day)
    with conn.cursor() as cur:   # 维 8：done 命令受理→run 终态（代理口径，见模块注）
        cur.execute(
            f"SELECT TIMESTAMPDIFF(SECOND, c.created_at, r.ended_at) "
            f"FROM {db}.agent_dispatch_command c JOIN {db}.agent_run r ON r.run_id=c.run_id "
            f"WHERE c.status='done' AND r.ended_at IS NOT NULL AND {pred}", params)
        vals = [float(r[0]) for r in (cur.fetchall() or []) if r[0] is not None and r[0] >= 0]
    out["recovery_convergence_p95_s"] = _percentile(vals, 0.95)
    with conn.cursor() as cur:   # 维 6：dispatcher 心跳龄（服务端 updated_at，免时钟漂移）
        cur.execute(
            f"SELECT TIMESTAMPDIFF(SECOND, updated_at, NOW()) "
            f"FROM {db}.rag_runtime_contract WHERE contract_key=%s", (_HB_KEY,))
        row = cur.fetchone()
    out["worker_heartbeat_age_s"] = (int(row[0]) if row and row[0] is not None else None)
    out["_worker_hb_row_present"] = bool(row)
    return out


def _dim_approvals(conn, db: str) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*), MAX(TIMESTAMPDIFF(SECOND, created_at, NOW())) "
            f"FROM {db}.approval_request WHERE status='pending'")
        row = cur.fetchone() or (0, None)
    return {"approval_pending": int(row[0] or 0),
            "approval_oldest_age_s": (int(row[1]) if row[1] is not None else None)}


def _dim_stale_running(conn, db: str) -> Dict[str, Any]:
    stale_s = _int_env("RAG_AGENT_STALE_RUNNING_S", 900)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM {db}.agent_run WHERE status='running' "
            "AND heartbeat_at < DATE_SUB(NOW(3), INTERVAL %s SECOND)", (stale_s,))
        n = cur.fetchone()[0]
    return {"stale_running": int(n or 0)}


def _dim_uncertain(conn, db: str) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*), MAX(TIMESTAMPDIFF(SECOND, COALESCE(ended_at, started_at), NOW())) "
            f"FROM {db}.tool_invocation WHERE status='uncertain'")
        row = cur.fetchone() or (0, None)
    return {"uncertain_invocations": int(row[0] or 0),
            "uncertain_oldest_age_s": (int(row[1]) if row[1] is not None else None)}


def _dim_relay(conn, db: str) -> Dict[str, Any]:
    """维 5：relay off → N/A；on → Redis PING + 近窗终态 run 逐流验终局帧。
    探测失败 → {"error": ...}（作业 exit 3，绝不报 0）。"""
    from opensearch_pipeline.agent_runtime.event_relay import relay_enabled
    if not relay_enabled():
        return {"relay_miss_rate": None}
    from opensearch_pipeline import redis_client
    try:
        if not redis_client.ping():
            return {"relay_miss_rate": None, "error": "relay_redis_unreachable"}
        cli = redis_client.get_client()
        ttl_s = _int_env("RAG_AGENT_EVENT_RELAY_TTL_S", 7200)
        window_s = max(60, min(int(ttl_s * 0.5), 3600))   # 避开 TTL 过期误报的采样窗
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT run_id FROM {db}.agent_run "
                "WHERE status IN ('succeeded','failed','cancelled','expired') "
                "AND ended_at >= DATE_SUB(NOW(3), INTERVAL %s SECOND) "
                "ORDER BY ended_at DESC LIMIT %s", (window_s, _RELAY_SAMPLE_MAX))
            run_ids = [r[0] for r in (cur.fetchall() or [])]
        if not run_ids:
            return {"relay_miss_rate": None, "relay_sampled": 0}   # 无样本=诚实 N/A
        miss = 0
        for rid in run_ids:
            key = redis_client.key("agent", "run", rid, "ev")
            frames = cli.xrevrange(key, max="+", min="-", count=8) or []
            found = False
            for _fid, fields in frames:
                data = fields.get("data") or fields.get(b"data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                try:
                    ftype = (json.loads(data) or {}).get("type")
                except Exception:   # noqa: BLE001
                    ftype = None
                if ftype in _RELAY_TERMINAL_TYPES:
                    found = True
                    break
            if not found:
                miss += 1
        out: Dict[str, Any] = {"relay_miss_rate": round(miss / len(run_ids), 4),
                               "relay_sampled": len(run_ids)}
        try:
            # best-effort 下界（发布失败对 Redis 的 INCR 本身也可能失败——如实标注，
            # 权威判定以上面的终局帧缺失率为准）
            pf = cli.get(redis_client.key("agent", "relay", "publish_failures"))
            out["relay_publish_failures_lower_bound"] = int(pf) if pf else 0
        except Exception:   # noqa: BLE001
            pass
        return out
    except Exception as e:   # noqa: BLE001 — 探测失败绝不报 0
        logger.error("agent_health: relay 探测失败（该维 error，作业 exit 3）: %s", e)
        return {"relay_miss_rate": None, "error": f"relay_probe_failed: {e}"}


# ── 裁决 + UPSERT + 告警 ──────────────────────────────────────────────────────


def _verdict(dims: Dict[str, Any]) -> Tuple[str, List[str]]:
    breaches: List[str] = []
    if dims.get("runs_total") and dims.get("success_rate") is not None \
            and dims["success_rate"] < _float_env("RAG_AGENT_AVAIL_MIN", 0.95):
        breaches.append(f"availability {dims['success_rate']} < AVAIL_MIN")
    if dims.get("dispatch_backlog") is not None \
            and dims["dispatch_backlog"] > _int_env("RAG_AGENT_BACKLOG_MAX", 50):
        breaches.append(f"dispatch_backlog {dims['dispatch_backlog']} > BACKLOG_MAX")
    sla_s = _int_env("RAG_AGENT_APPROVAL_SLA_HOURS", 24) * 3600
    if dims.get("approval_oldest_age_s") is not None \
            and dims["approval_oldest_age_s"] > sla_s:
        breaches.append(f"approval oldest {dims['approval_oldest_age_s']}s > SLA（M6.iii 积压老化）")
    if dims.get("stale_running"):
        breaches.append(f"stale_running {dims['stale_running']} > 0")
    if dims.get("relay_miss_rate") is not None \
            and dims["relay_miss_rate"] > _float_env("RAG_AGENT_RELAY_MISS_RATE_MAX", 0.01):
        breaches.append(f"relay_miss_rate {dims['relay_miss_rate']} > MAX")
    hb = dims.get("worker_heartbeat_age_s")
    if dims.get("_worker_hb_expected"):
        if hb is None:
            breaches.append("worker_heartbeat 行缺失（dispatcher 从未写心跳）")
        elif hb > _int_env("RAG_AGENT_WORKER_HB_MAX_S", 300):
            breaches.append(f"worker_heartbeat_age {hb}s > MAX")
    if dims.get("cancellation_latency_p95_s") is not None \
            and dims["cancellation_latency_p95_s"] > _int_env("RAG_AGENT_CANCEL_LAT_MAX_S", 120):
        breaches.append(f"cancellation_latency_p95 {dims['cancellation_latency_p95_s']}s > MAX")
    if dims.get("recovery_convergence_p95_s") is not None \
            and dims["recovery_convergence_p95_s"] > _int_env("RAG_AGENT_RECOVERY_MAX_S", 900):
        breaches.append(f"recovery_convergence_p95 {dims['recovery_convergence_p95_s']}s > MAX")
    return ("breach" if breaches else "ok"), breaches


_UPSERT_COLS = (
    "runs_total", "runs_succeeded", "runs_failed", "runs_cancelled", "runs_expired",
    "success_rate", "error_rate",
    "active_latency_p50_ms", "active_latency_p95_ms", "ttr_p50_ms", "ttr_p95_ms",
    "dispatch_backlog", "approval_pending", "approval_oldest_age_s", "stale_running",
    "uncertain_invocations", "uncertain_oldest_age_s", "relay_miss_rate",
    "worker_heartbeat_age_s", "cancellation_latency_p95_s", "recovery_convergence_p95_s",
    "slo_verdict",
)


def _upsert(conn, db: str, day: str, dims: Dict[str, Any]) -> None:
    cols = ", ".join(("metric_date",) + _UPSERT_COLS + ("computed_at",))
    ph = ", ".join(["%s"] * (len(_UPSERT_COLS) + 1)) + ", NOW(3)"
    updates = ", ".join(f"{c}=VALUES({c})" for c in _UPSERT_COLS + ("computed_at",))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {db}.agent_daily_metrics ({cols}) VALUES ({ph}) "
            f"ON DUPLICATE KEY UPDATE {updates}",
            (day, *[dims.get(c) for c in _UPSERT_COLS]))
    conn.commit()


def run_agent_health_check(*, alert: bool = True, target_day: Optional[str] = None) -> dict:
    """作业入口（ops_monitor 契约）：flag off / simulate → skipped；硬故障 → error；
    任一 breach → ok=False（exit 2）；全绿 → ok=True。"""
    if not enabled():
        return {"skipped": True, "reason": "RAG_AGENT_HEALTH_ENABLE off"}
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        if cfg.simulate or cfg.simulate_db:
            return {"skipped": True, "reason": "simulate"}
    except Exception as e:   # noqa: BLE001
        return {"error": f"config: {e}"}
    return _execute(target_day or _beijing_today(), alert=alert)


def _execute(day: str, *, alert: bool) -> dict:
    """作业主体（gate 之后）：采集八维+当日事实 → 裁决 → UPSERT（056）→ 越阈告警。
    真库契约测试直测本函数（gate 由 run_agent_health_check 单测覆盖）。"""
    dims: Dict[str, Any] = {}
    try:
        db = _op_db()
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            dims.update(_collect_day_facts(conn, db, day))
            dims.update(_dim_approvals(conn, db))
            dims.update(_dim_stale_running(conn, db))
            dims.update(_dim_uncertain(conn, db))
            _dd = _dim_dispatch(conn, db, day)
            from opensearch_pipeline.agent_runtime.durable_dispatcher import (
                durable_dispatch_enabled,
            )
            dims["_worker_hb_expected"] = durable_dispatch_enabled()
            dims.update({k: v for k, v in _dd.items() if k != "_worker_hb_row_present"})
            relay = _dim_relay(conn, db)
            dims.update({k: v for k, v in relay.items() if k != "error"})
            verdict, breaches = _verdict(dims)
            dims["slo_verdict"] = verdict
            _upsert(conn, db, day, dims)
        finally:
            conn.close()
        report: Dict[str, Any] = {
            "date": day, "ok": not breaches, "breaches": breaches,
            **{k: v for k, v in dims.items() if not k.startswith("_")}}
        if relay.get("error"):
            # Redis 探测失败：该维 error → 作业 exit 3（绝不把不可知报成健康）
            report["error"] = relay["error"]
            report["ok"] = False
        if breaches and alert:
            try:
                from opensearch_pipeline.alerting import send_ops_alert
                send_ops_alert(
                    "Agent 健康越阈（agent_health）",
                    f"业务日 {day}：\n- " + "\n- ".join(breaches)
                    + f"\n\n(runs={dims.get('runs_total')} succ_rate={dims.get('success_rate')}"
                    f" backlog={dims.get('dispatch_backlog')})",
                    severity="warning", dedup_key="agent-health")
            except Exception:   # noqa: BLE001 — 告警失败不改变裁决
                logger.warning("agent_health 告警发送失败（忽略）", exc_info=True)
        return report
    except Exception as e:   # noqa: BLE001 — 采集/UPSERT 硬故障：exit 3
        logger.error("agent_health 作业失败: %s", e, exc_info=True)
        return {"error": str(e), "date": day}
