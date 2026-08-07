# -*- coding: utf-8 -*-
"""qa_rollup.py — Phase-2 OBS-5: nightly QA serving-quality rollup + SLO verdicts.

Aggregates fuling_operation.qa_session_log into one qa_daily_metrics row per Beijing business day and
evaluates the serving SLOs. `compute_daily_metrics` is a PURE function (no I/O) and is the unit-tested
core; percentiles are computed here because MySQL 8.0 lacks PERCENTILE_CONT.

SLOs (thresholds env-overridable; defaults RATIFIED 2026-06-16, grounded in the last-21d prod
qa_session_log distribution — they fire on the 6-08/6-09 incident days without false-alarming normal
days; p95 reflects that latency_ms is END-TO-END incl. the typewriter stream, not compute time):
  RAG_SLO_ANSWER_RATE_MIN    answer_rate    ≥ 0.75   (success / total)
  RAG_SLO_NO_RESULT_RATE_MAX no_result_rate ≤ 0.15   (retrieval misses)
  RAG_SLO_P95_LATENCY_MS_MAX p95 latency    ≤ 25000  (end-to-end incl. stream)
  RAG_SLO_ERROR_RATE_MAX     error_rate     ≤ 0.05   (LLM_ERROR / total)

answer_status ∈ {SUCCESS, NO_RESULT, REFUSAL, LLM_ERROR} + separate risk_blocked flag.
created_at is the SAE container's Pacific wall-clock; rows are bucketed to the Beijing business day
via DST-correct CONVERT_TZ(created_at,'America/Los_Angeles','Asia/Shanghai') (named zones; RDS tz
tables verified loaded). tz_shift_hours is now legacy/nominal — recorded in the audit column only, it
no longer drives bucketing (the old hardcoded +15h was off by one hour in US winter, PST=+16h).

P2-18（盲区审计 2026-07-05，存储时区争议）：机制上 qa_logger 的 INSERT 列表**不含** created_at
（值走 DDL 的 DEFAULT CURRENT_TIMESTAMP，见 schema/002:105），db.py 连接池也**不** pin session
time_zone —— 即存储时区由 RDS 服务器时区决定；审计据此推断浙江 RDS 应为 +08:00、上面的
LA→Shanghai 分桶会整体错桶。但生产多次实测（Phase E data probe / qa-log-analytics）一致表明
created_at ≈ 太平洋墙钟（北京 +15/16h），分桶与 SLO 阈值正是按实测校准的——若 RDS 真是 +08:00，
实测差应为 +8h 而非 +15h，两者矛盾（可能 RDS time_zone=SYSTEM 且系统时钟为 UTC-7/-8）。此争议
未在服务器侧证实前**不改存储、不 pin 时区**（打断历史行连续性，属用户级决策）；run_rollup 每次
连接后做一次 fail-open 探测（_probe_server_tz：@@time_zone/NOW()/UTC_TIMESTAMP()），结果进日志
与 report['tz_probe']，供下一次生产运行确认地面真相后再决策是否迁移。

Runner is fail-open + simulate-safe (no-op), read/UPSERT only (no destructive ops). alert=True fires
one OBS-4 ops alert per breached day.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_TZ_SHIFT_HOURS = 15  # legacy/nominal audit value only — bucketing now uses DST-correct CONVERT_TZ


def _op_db() -> str:
    """问答运营库名（qa_session_log/qa_daily_metrics 所在库）；经 RAG_RDS_OPERATION_DATABASE
    配置（STAGING=fuling_operation_stg）。镜像 qa_logger._op_db()，惰性读 config。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.operation_database


def _beijing_day_pacific_bounds(target: str):
    """把北京业务日 target（'YYYY-MM-DD'）换算为存储时区（太平洋墙钟）的半开区间 [start, end)。

    perf#6/7（sargable 化）：等价改写 `DATE(CONVERT_TZ(created_at, LA, Shanghai)) = %s` ——
    函数包裹列使 created_at 索引永久失效、日汇总全表扫；把同一 DST-correct 换算搬到常量侧后，
    谓词变 `created_at >= %s AND created_at < %s`，吃到 schema/012 索引族。语义等价论证：
    CONVERT_TZ 单调，日期分桶 ⟺ 北京日两端 00:00 边界换算成太平洋墙钟的区间成员判定；边界落在
    北京 00:00 = 太平洋 08:00/09:00（随 DST），永不落入太平洋 01:00-02:00 的 fall-back 歧义小时，
    故与 CONVERT_TZ 的逐行换算分桶一致。zoneinfo 缺 tz 数据等异常 → 返回 None，调用方回退
    旧 CONVERT_TZ 谓词（fail-open，语义不变只慢）。"""
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        day = datetime.strptime(str(target), "%Y-%m-%d")
        start_bj = day.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        pac = ZoneInfo("America/Los_Angeles")
        start = start_bj.astimezone(pac).replace(tzinfo=None)
        end = (start_bj + timedelta(days=1)).astimezone(pac).replace(tzinfo=None)
        return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:  # noqa: BLE001 — 缺 tzdata / 非法日期 → 回退旧谓词
        logger.warning("qa_rollup: 北京日→太平洋区间换算失败（回退 CONVERT_TZ 谓词）: %s", e)
        return None


def _probe_server_tz(conn) -> Optional[Dict[str, str]]:
    """P2-18：存储时区地面真相探测（一次连接一查，fail-open）。

    背景见模块 docstring 的 P2-18 段：created_at 由 RDS DEFAULT CURRENT_TIMESTAMP 按服务器/会话
    时区写入，与生产实测「太平洋墙钟 +15/16h」存在未解释的张力。这里只读取
    @@session.time_zone / @@global.time_zone / NOW() / UTC_TIMESTAMP() 并 logger.info 记录 +
    返回 dict（进 report['tz_probe']），不改任何存储行为。探测失败返回 None，绝不影响汇总。"""
    cols = ["session_tz", "global_tz", "db_now", "db_utc"]
    try:
        with conn.cursor() as c:
            c.execute("SELECT @@session.time_zone AS session_tz, @@global.time_zone AS global_tz,"
                      " NOW() AS db_now, UTC_TIMESTAMP() AS db_utc")
            row = c.fetchone()
        if row is None:
            return None
        # 共享池是 tuple cursor（无 DictCursor）→ 按已知列序映射；dict cursor 亦兼容。
        if not isinstance(row, dict):
            row = dict(zip(cols, row))
        probe = {k: str(row.get(k)) for k in cols}
        logger.info("qa_rollup tz_probe: session_tz=%s global_tz=%s NOW()=%s UTC_TIMESTAMP()=%s",
                    probe["session_tz"], probe["global_tz"], probe["db_now"], probe["db_utc"])
        return probe
    except Exception as e:  # noqa: BLE001 — fail-open：探测是诊断增强，绝不挡既有汇总
        logger.warning("qa_rollup: 存储时区探测失败（non-fatal）: %s", e)
        return None


def _slo_thresholds() -> Dict[str, float]:
    def _f(env, default):
        try:
            return float(os.environ.get(env, default))
        except (TypeError, ValueError):
            return default
    return {
        "answer_rate_min": _f("RAG_SLO_ANSWER_RATE_MIN", 0.75),
        "no_result_rate_max": _f("RAG_SLO_NO_RESULT_RATE_MAX", 0.15),
        "p95_latency_ms_max": _f("RAG_SLO_P95_LATENCY_MS_MAX", 25000),
        "error_rate_max": _f("RAG_SLO_ERROR_RATE_MAX", 0.05),
        # P2-13（盲区审计）：流量下限——死掉的前端与健康前端必须可区分。
        # 基准 = 近 4 周同星期日 total_queries 中位数（同星期对齐吸收工厂周末低谷）；
        # 基准 ≥ min_base 且当日 < ratio×基准 → traffic_collapse；当日 =0 且基准 >0 → zero_traffic。
        # 无历史（全新部署/qa_daily_metrics 空）→ 不判（bootstrap 不误报）。
        "traffic_drop_ratio": _f("RAG_SLO_TRAFFIC_DROP_RATIO", 0.3),
        "traffic_min_base": _f("RAG_SLO_TRAFFIC_MIN_BASE", 10),
        # 2026-08-06：比率型 SLO 的**最小样本量**。现网实测日均 ~7 条查询
        # （qa_session_log 近 30 天 n=207，峰值 0.05 QPS），n=3 时一条 no-result 就把
        # no_result_rate 打到 1.000 —— 阈值 0.15 必然击穿。这不是服务劣化，是样本量噪声：
        # 08-04(n=3) / 07-27(n=5) 的历史 breach 全属此类，把 exit=2 变成了日常。
        "min_sample": _f("RAG_SLO_MIN_SAMPLE", 20),
        # 但**全军覆没不是噪声**：应答率恰好 0 / 无结果率恰好 1.0 是定性信号（"一条都没成"），
        # 与 0.6 vs 0.75 这种边缘值不同 —— 低样本下仍按真 breach 报。
        "total_failure_min_n": _f("RAG_SLO_TOTAL_FAILURE_MIN_N", 3),
    }


def _percentile(sorted_vals: List[float], q: float) -> Optional[float]:
    """Nearest-rank percentile on an already-sorted list. q in [0,1]."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = int(round(q * (len(sorted_vals) - 1)))
    return sorted_vals[idx]


def evaluate_slos(metrics: Dict[str, Any], thresholds: Dict[str, float]) -> Dict[str, Any]:
    """Return {ok: bool, breaches: [{slo, threshold, value}]}. A None metric (no data) does NOT
    breach — you can't violate an SLO with zero traffic.

    P1-1（盲区审计 2026-07-05）：global_cap 拒绝 > 0 无条件 breach——熔断触顶日全站对外
    503，但被拒请求不落 qa_session_log，answer_rate 等既有指标只看得见触顶前的成功量；
    没有这一条，宕机日在看板上是绿的。阈值恒 0：任何一次全局熔断拒绝都是事故级信号。"""
    breaches = []
    rgc = metrics.get("rejected_global_cap")
    if rgc:
        breaches.append({"slo": "global_cap_rejected", "threshold": 0, "value": rgc})
    # P2-13：零/塌缩流量判定（基准=近 4 周同星期日中位数，见 _slo_thresholds 注释）。
    # 「零流量不违约」只对指标缺失成立；流量本身的缺席在有历史基准时就是事件。
    base = metrics.get("traffic_median_ref")
    total_q = metrics.get("total_queries")
    if base is not None and base > 0 and total_q is not None:
        if total_q == 0:
            breaches.append({"slo": "zero_traffic", "threshold": base, "value": 0})
        elif (base >= thresholds.get("traffic_min_base", 10)
                and total_q < thresholds.get("traffic_drop_ratio", 0.3) * base):
            breaches.append({"slo": "traffic_collapse",
                             "threshold": round(thresholds.get("traffic_drop_ratio", 0.3) * base, 1),
                             "value": total_q})
    # ── 比率/分位型 SLO：受最小样本量门槛约束 ────────────────────────────────
    # 🔴 **低样本下不静默丢弃**：够不到样本量的击穿进 `low_confidence`，仍出现在报告里、
    # 仍随真 breach 一起发出去，只是**不单独触发 page**。silently dropping 会把
    # "指标失效" 伪装成 "服务健康"，那与本仓一路在修的静默截断是同一族错误。
    # ⚠️ `total_queries` **缺失 = unknown，不是 0**。按 0 处理等于"没给这个字段 ⇒ 所有比率
    # SLO 静音"，那是把门槛做成了全局静音开关（既有单测
    # `test_evaluate_slos_breach_and_clean` 当场抓到）。同仓「缺 assets 键=unknown≠空集」纪律。
    n = metrics.get("total_queries")
    min_n = thresholds.get("min_sample", 20)
    tf_n = thresholds.get("total_failure_min_n", 3)
    low_conf: List[Dict[str, Any]] = []

    def _ratio(slo, value, threshold, breached, *, total_failure=False):
        """total_failure=True ⇒ 定性信号（一条都没成），低样本下**仍是真 breach**。"""
        if value is None or not breached:
            return
        item = {"slo": slo, "threshold": threshold, "value": value, "sample": n}
        if n is None or n >= min_n or (total_failure and n >= tf_n):
            if total_failure and n < min_n:
                item["note"] = "全军覆没（低样本但定性成立）"
            breaches.append(item)
        else:
            item["note"] = f"样本量 {n} < {min_n:.0f}，比率不可信，不单独告警"
            low_conf.append(item)

    ar = metrics.get("answer_rate")
    _ratio("answer_rate_min", ar, thresholds["answer_rate_min"],
           ar is not None and ar < thresholds["answer_rate_min"],
           total_failure=(ar == 0.0))
    nrr = metrics.get("no_result_rate")
    _ratio("no_result_rate_max", nrr, thresholds["no_result_rate_max"],
           nrr is not None and nrr > thresholds["no_result_rate_max"],
           total_failure=(nrr == 1.0))
    er = metrics.get("error_rate")
    _ratio("error_rate_max", er, thresholds["error_rate_max"],
           er is not None and er > thresholds["error_rate_max"],
           total_failure=(er == 1.0))
    p95 = metrics.get("p95_latency_ms")
    # p95 没有"全军覆没"对应物：n=3 的 p95 就是最大值，纯噪声，只受样本量门槛。
    _ratio("p95_latency_ms_max", p95, thresholds["p95_latency_ms_max"],
           p95 is not None and p95 > thresholds["p95_latency_ms_max"])

    # ⚠️ 已知局限，写下来而不是假装没有：低样本下**持续**劣化（如连续两周应答率 0.6）
    # 不会由日级告警发现——它落在周报/看板的趋势上。日级告警只负责"今天出事了"。
    return {"ok": not breaches, "breaches": breaches, "low_confidence": low_conf}


def compute_daily_metrics(rows: List[Dict[str, Any]],
                          thresholds: Optional[Dict[str, float]] = None,
                          rejected: Optional[Dict[str, int]] = None,
                          traffic_median: Optional[int] = None) -> Dict[str, Any]:
    """PURE: aggregate one day's qa_session_log rows → metric dict + SLO verdict. No I/O.

    Each row needs: answer_status, risk_blocked, latency_ms, top_score, user_id, session_id,
    conversation_type, opensearch_hit_count.

    rejected: 当日限流拒绝聚合 {reason: count}（qa_admission_reject，P1-1）。被拒请求不在
    rows 里（准入即 503，从不落 qa_session_log），这是唯一的 offered-load 补充来源；
    global_cap > 0 → SLO breach（见 evaluate_slos）。既有比率指标（answer_rate 等）分母
    保持"准入量"不变——per_min/per_day 拒绝是反滥用的正常工作，掺进分母会让一个扫描器
    压垮 answer_rate 产生假告警；熔断日的"红"由 global_cap_rejected 专项 breach 保证。
    """
    thresholds = thresholds or _slo_thresholds()
    rejected = rejected or {}
    total = len(rows)
    success = refusal = no_result = error = risk_blocked = single = group = 0
    latencies: List[float] = []
    scores: List[float] = []
    users, sessions = set(), set()

    for r in rows:
        st = (r.get("answer_status") or "").upper()
        blocked = bool(r.get("risk_blocked"))
        hit = r.get("opensearch_hit_count")
        if blocked:
            risk_blocked += 1
        # precedence: a hard risk-block is a refusal regardless of answer_status (it is NOT a
        # successful answer); then classify by answer_status.
        if blocked or st == "REFUSAL":
            refusal += 1
        elif st == "SUCCESS":
            success += 1
        elif st == "NO_RESULT" or (hit == 0 and "ERROR" not in st):
            no_result += 1
        elif "ERROR" in st:
            error += 1
        lat = r.get("latency_ms")
        if isinstance(lat, (int, float)) and lat > 0:
            latencies.append(float(lat))
        sc = r.get("top_score")
        if isinstance(sc, (int, float)):
            scores.append(float(sc))
        if r.get("user_id"):
            users.add(r["user_id"])
        if r.get("session_id"):
            sessions.add(r["session_id"])
        ct = str(r.get("conversation_type") or "")
        if ct == "1":
            single += 1
        elif ct == "2":
            group += 1

    latencies.sort()
    metrics: Dict[str, Any] = {
        "total_queries": total,
        "success_count": success,
        "refusal_count": refusal,
        "no_result_count": no_result,
        "error_count": error,
        "risk_blocked_count": risk_blocked,
        "p50_latency_ms": int(_percentile(latencies, 0.50)) if latencies else None,
        "p95_latency_ms": int(_percentile(latencies, 0.95)) if latencies else None,
        "avg_top_score": round(sum(scores) / len(scores), 4) if scores else None,
        "distinct_users": len(users),
        "distinct_sessions": len(sessions),
        "single_chat_count": single,
        "group_chat_count": group,
        "answer_rate": round(success / total, 4) if total else None,
        "no_result_rate": round(no_result / total, 4) if total else None,
        "error_rate": round(error / total, 4) if total else None,
        # P1-1：offered vs admitted —— 被拒量单列，不掺进上面比率的分母（docstring 论证）
        "rejected_count": sum(int(v or 0) for v in rejected.values()),
        "rejected_global_cap": int(rejected.get("global_cap", 0) or 0),
        # P2-13：流量基准（近 4 周同星期日中位数；None=无历史，零/塌缩判定跳过）
        "traffic_median_ref": traffic_median,
    }
    metrics["offered_queries"] = total + metrics["rejected_count"]
    verdict = evaluate_slos(metrics, thresholds)
    metrics["slo_ok"] = 1 if verdict["ok"] else 0
    metrics["slo_breaches"] = verdict["breaches"]
    # 低置信项必须一路带到 report：只要它在结构里，看板/周报/告警正文就都能消费到，
    # 不会因为"没 page"而彻底消失（否则就是把指标失效伪装成服务健康）。
    metrics["low_confidence"] = verdict.get("low_confidence", [])
    return metrics


_BASE_METRIC_COLS = ["total_queries", "success_count", "refusal_count", "no_result_count",
                     "error_count", "risk_blocked_count", "p50_latency_ms", "p95_latency_ms",
                     "avg_top_score", "distinct_users", "distinct_sessions", "single_chat_count",
                     "group_chat_count", "answer_rate", "no_result_rate", "error_rate", "slo_ok"]
# schema/017 新列：生产未 apply 时写入报 1054 → 回退基础列集（拒绝量仍在 slo_breaches_json 里可见）
_REJECT_METRIC_COLS = ["rejected_count", "rejected_global_cap"]


def _upsert_daily(conn, metric_date: str, m: Dict[str, Any], tz_shift: int) -> None:
    def _do(cols: List[str]) -> None:
        vals = [m.get(c) for c in cols]
        breaches_json = json.dumps(m.get("slo_breaches") or [], ensure_ascii=False)
        set_clause = ", ".join(f"{c}=VALUES({c})" for c in cols) + \
            ", slo_breaches_json=VALUES(slo_breaches_json), tz_shift_hours=VALUES(tz_shift_hours)"
        # 显式限定运营库（qa_daily_metrics 定义在 fuling_operation，见 schema/004）。生产 writer
        # 经 LaunchAgent RAG_ENV=metrics 把连接默认库设为 fuling_operation，非限定写本就落到此库；
        # 显式 {_op_db()}. 是纵深加固：去掉对 RDS_DATABASE 取值的隐式依赖（不再因默认库变动而错位写
        # 到知识库），且 staging 自动指向 fuling_operation_stg。生产目标不变（_op_db()=fuling_operation）。
        sql = (f"INSERT INTO {_op_db()}.qa_daily_metrics (metric_date, {', '.join(cols)}, "
               f"slo_breaches_json, tz_shift_hours) "
               f"VALUES (%s, {', '.join(['%s'] * len(cols))}, %s, %s) "
               f"ON DUPLICATE KEY UPDATE {set_clause}")
        with conn.cursor() as c:
            c.execute(sql, [metric_date, *vals, breaches_json, tz_shift])
        conn.commit()

    try:
        _do(_BASE_METRIC_COLS + _REJECT_METRIC_COLS)
        return
    except Exception as e:  # noqa: BLE001 — 仅未知列（017 未 apply）降级，其余照抛
        msg = str(e)
        if "1054" not in msg and "Unknown column" not in msg:
            raise
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("qa_daily_metrics 缺拒绝列（schema/017 未 apply），回退基础列集写入")
    _do(_BASE_METRIC_COLS)


def _fetch_traffic_median(conn, target: str) -> Optional[int]:
    """P2-13 流量基准：近 4 周同星期日的 qa_daily_metrics.total_queries 中位数。
    同星期对齐吸收工厂周末低谷；无历史行/查询失败 → None（bootstrap/降级不判流量 SLO）。"""
    try:
        with conn.cursor() as c:
            c.execute(
                f"SELECT total_queries FROM {_op_db()}.qa_daily_metrics"
                " WHERE metric_date IN (DATE_SUB(%s, INTERVAL 7 DAY), DATE_SUB(%s, INTERVAL 14 DAY),"
                "                       DATE_SUB(%s, INTERVAL 21 DAY), DATE_SUB(%s, INTERVAL 28 DAY))",
                (target, target, target, target))
            rows = c.fetchall() or []
        vals = sorted(int((r["total_queries"] if isinstance(r, dict) else r[0]) or 0) for r in rows)
        if not vals:
            return None
        return vals[len(vals) // 2]
    except Exception as e:  # noqa: BLE001
        logger.warning("qa_rollup: 流量基准读取失败（跳过流量 SLO）: %s", e)
        return None


def _fetch_rejected(conn, target: str) -> Dict[str, int]:
    """当日限流拒绝聚合 {reason: count}（qa_admission_reject，schema/017，写侧 rate_limiter）。
    表未建/查询失败 → 空 dict（fail-open：P1-1 是增强项，绝不反过来挡住既有汇总）。"""
    try:
        with conn.cursor() as c:
            c.execute(f"SELECT reason, reject_count FROM {_op_db()}.qa_admission_reject"
                      " WHERE stat_date = %s", (target,))
            rows = c.fetchall() or []
        out: Dict[str, int] = {}
        for r in rows:
            reason, cnt = (r["reason"], r["reject_count"]) if isinstance(r, dict) else (r[0], r[1])
            if str(reason).startswith("__"):
                continue   # __ 前缀 = 非拒绝行（如 __admitted__ 准入量持久化，P2-11）不计入拒绝
            out[str(reason)] = out.get(str(reason), 0) + int(cnt or 0)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("qa_rollup: 读取 qa_admission_reject 失败（按无拒绝处理；1146=schema/017 未 apply，**1142=账号缺 SELECT 授权**）: %s", e)
        return {}


def run_rollup(*, metric_date: Optional[str] = None, tz_shift_hours: int = _DEFAULT_TZ_SHIFT_HOURS,
               alert: bool = False) -> Dict[str, Any]:
    """Roll up one Beijing business day of qa_session_log → qa_daily_metrics. metric_date defaults to
    'yesterday' computed by the DB (avoids host-clock skew). Fail-open, simulate-safe."""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    if cfg.simulate or cfg.simulate_db:
        logger.info("qa_rollup: simulate mode → skipped no-op")
        return {"ok": True, "skipped": "simulate"}

    tz_probe: Optional[Dict[str, str]] = None
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
            # P2-18：先做一次存储时区探测（详见 _probe_server_tz），fail-open、只读。
            tz_probe = _probe_server_tz(conn)
            # resolve target Beijing day; default = yesterday (Beijing), computed by the DB to avoid
            # host-clock skew. Cursor may be tuple- or dict-style → read positionally via the alias.
            if metric_date:
                target = metric_date
            else:
                with conn.cursor() as c:
                    c.execute("SELECT DATE(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 8 HOUR) "
                              "- INTERVAL 1 DAY) AS d")
                    row = c.fetchone()
                    target = str(row["d"] if isinstance(row, dict) else row[0])

            # pull the day's rows, bucketed to Beijing from the stored Pacific time via DST-correct
            # CONVERT_TZ (named zones; RDS tz tables verified loaded). Replaces the old hardcoded
            # +tz_shift — which was off by one hour during US winter (PST=+16h, not +15h). tz_shift_hours
            # is now legacy/nominal: it no longer drives bucketing, only the audit column below.
            # The shared pool uses a tuple cursor (no DictCursor) → map to dicts via the known
            # column order so compute_daily_metrics' keyed access works regardless of cursor type.
            _cols = ["answer_status", "risk_blocked", "latency_ms", "top_score", "user_id",
                     "session_id", "conversation_type", "opensearch_hit_count"]
            # perf#6/7：优先 sargable 半开区间（吃 created_at/(answer_status,created_at) 索引），
            # 区间换算失败才回退函数包裹列的旧谓词（全表扫但语义相同）。
            _bounds = _beijing_day_pacific_bounds(target)
            with conn.cursor() as c:
                if _bounds:
                    c.execute(
                        f"""SELECT {', '.join(_cols)}
                              FROM {_op_db()}.qa_session_log
                             WHERE created_at >= %s AND created_at < %s""",
                        _bounds)
                else:
                    c.execute(
                        f"""SELECT {', '.join(_cols)}
                              FROM {_op_db()}.qa_session_log
                             WHERE DATE(CONVERT_TZ(created_at, 'America/Los_Angeles', 'Asia/Shanghai')) = %s""",
                        (target,))
                raw = c.fetchall()
            rows = [r if isinstance(r, dict) else dict(zip(_cols, r)) for r in raw]

            m = compute_daily_metrics(rows, rejected=_fetch_rejected(conn, target),
                                      traffic_median=_fetch_traffic_median(conn, target))
            _upsert_daily(conn, target, m, tz_shift_hours)
        finally:
            conn.close()

        report = {"ok": True, "metric_date": target, "metrics": m,
                  "slo_ok": m["slo_ok"], "breaches": m["slo_breaches"], "tz_probe": tz_probe}
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.exception("qa_rollup: failed")
        report = {"ok": False, "error": f"{type(e).__name__}: {e}", "tz_probe": tz_probe}

    if alert and (not report.get("ok") or report.get("slo_ok") == 0):
        _alert_on_slo(report)
    return report


def _alert_on_slo(report: Dict[str, Any]) -> None:
    # C1 同族（2026-08-04 独立核验实测）：探针失败不得顶着「SLO breach」的标题发。
    # 本机 launchd `com.fuling.qa-rollup` 日志里 14 条被抑制的 CRITICAL 全部题为
    # "QA serving SLO breach"，实际全是 DNS 解析失败（Errno 8）——与 reconcile 三处
    # 被 9cd9436 修掉的是同一模板病。标题可辨 + 去重槽分开，双向不互压。
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        _errored = bool(report.get("error"))
        if _errored:
            text = f"QA rollup errored: {report['error']}"
            title = "QA rollup 探针失败"
            dk = f"qa-slo:error:{report.get('metric_date') or 'unknown'}"
        else:
            lines = "\n".join(f"- {b['slo']}: {b['value']} (threshold {b['threshold']})"
                              + (f"  ⚠️{b['note']}" if b.get("note") else "")
                              for b in report.get("breaches", []))
            text = f"SLO breach on {report.get('metric_date')}:\n{lines}"
            # 低置信项**随真 breach 一起发出去**：它们不单独 page，但排查时是上下文
            # （"今天还有哪些指标其实也击穿了，只是样本量不够"）。绝不静默丢。
            lc = report.get("low_confidence") or []
            if lc:
                text += ("\n\n低置信（样本量不足，未单独告警）:\n"
                         + "\n".join(f"- {b['slo']}: {b['value']} "
                                     f"(threshold {b['threshold']}, n={b.get('sample')})" for b in lc))
            title = "QA serving SLO breach"
            dk = f"qa-slo:{report.get('metric_date')}"
        send_ops_alert(title, text, severity="critical", dedup_key=dk)
    except Exception:  # noqa: BLE001
        logger.warning("qa_rollup: ops-alert dispatch failed (non-fatal)", exc_info=True)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="OBS-5 nightly QA rollup + SLO verdict")
    ap.add_argument("--date", default=None, help="Beijing business day YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--tz-shift", type=int, default=_DEFAULT_TZ_SHIFT_HOURS,
                    help="legacy/nominal audit value only (bucketing now uses DST-correct CONVERT_TZ)")
    ap.add_argument("--alert", action="store_true", help="fire OBS-4 alert on SLO breach/error")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = run_rollup(metric_date=args.date, tz_shift_hours=args.tz_shift, alert=args.alert)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        if report.get("skipped"):
            print(f"[qa_rollup] skipped ({report['skipped']})")
            return 0
        if report.get("error"):
            print(f"[qa_rollup] ERROR: {report['error']}")
            return 3
        m = report["metrics"]
        print(f"[qa_rollup] {report['metric_date']}: queries={m['total_queries']} "
              f"answer_rate={m['answer_rate']} no_result_rate={m['no_result_rate']} "
              f"p95={m['p95_latency_ms']}ms error_rate={m['error_rate']} "
              f"rejected={m.get('rejected_count', 0)} slo_ok={m['slo_ok']}")
        for b in report.get("breaches", []):
            print(f"  ⚠️ SLO breach {b['slo']}: {b['value']} (threshold {b['threshold']})")
    return 0 if report.get("ok") and report.get("slo_ok", 1) == 1 else (2 if report.get("ok") else 3)


if __name__ == "__main__":
    import sys
    sys.exit(main())
