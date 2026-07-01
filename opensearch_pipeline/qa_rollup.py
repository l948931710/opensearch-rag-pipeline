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
    breach — you can't violate an SLO with zero traffic."""
    breaches = []
    ar = metrics.get("answer_rate")
    if ar is not None and ar < thresholds["answer_rate_min"]:
        breaches.append({"slo": "answer_rate_min", "threshold": thresholds["answer_rate_min"], "value": ar})
    nrr = metrics.get("no_result_rate")
    if nrr is not None and nrr > thresholds["no_result_rate_max"]:
        breaches.append({"slo": "no_result_rate_max", "threshold": thresholds["no_result_rate_max"], "value": nrr})
    p95 = metrics.get("p95_latency_ms")
    if p95 is not None and p95 > thresholds["p95_latency_ms_max"]:
        breaches.append({"slo": "p95_latency_ms_max", "threshold": thresholds["p95_latency_ms_max"], "value": p95})
    er = metrics.get("error_rate")
    if er is not None and er > thresholds["error_rate_max"]:
        breaches.append({"slo": "error_rate_max", "threshold": thresholds["error_rate_max"], "value": er})
    return {"ok": not breaches, "breaches": breaches}


def compute_daily_metrics(rows: List[Dict[str, Any]],
                          thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """PURE: aggregate one day's qa_session_log rows → metric dict + SLO verdict. No I/O.

    Each row needs: answer_status, risk_blocked, latency_ms, top_score, user_id, session_id,
    conversation_type, opensearch_hit_count.
    """
    thresholds = thresholds or _slo_thresholds()
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
    }
    verdict = evaluate_slos(metrics, thresholds)
    metrics["slo_ok"] = 1 if verdict["ok"] else 0
    metrics["slo_breaches"] = verdict["breaches"]
    return metrics


def _upsert_daily(conn, metric_date: str, m: Dict[str, Any], tz_shift: int) -> None:
    cols = ["total_queries", "success_count", "refusal_count", "no_result_count", "error_count",
            "risk_blocked_count", "p50_latency_ms", "p95_latency_ms", "avg_top_score",
            "distinct_users", "distinct_sessions", "single_chat_count", "group_chat_count",
            "answer_rate", "no_result_rate", "error_rate", "slo_ok"]
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


def run_rollup(*, metric_date: Optional[str] = None, tz_shift_hours: int = _DEFAULT_TZ_SHIFT_HOURS,
               alert: bool = False) -> Dict[str, Any]:
    """Roll up one Beijing business day of qa_session_log → qa_daily_metrics. metric_date defaults to
    'yesterday' computed by the DB (avoids host-clock skew). Fail-open, simulate-safe."""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    if cfg.simulate or cfg.simulate_db:
        logger.info("qa_rollup: simulate mode → skipped no-op")
        return {"ok": True, "skipped": "simulate"}

    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
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
            with conn.cursor() as c:
                c.execute(
                    f"""SELECT {', '.join(_cols)}
                          FROM {_op_db()}.qa_session_log
                         WHERE DATE(CONVERT_TZ(created_at, 'America/Los_Angeles', 'Asia/Shanghai')) = %s""",
                    (target,))
                raw = c.fetchall()
            rows = [r if isinstance(r, dict) else dict(zip(_cols, r)) for r in raw]

            m = compute_daily_metrics(rows)
            _upsert_daily(conn, target, m, tz_shift_hours)
        finally:
            conn.close()

        report = {"ok": True, "metric_date": target, "metrics": m,
                  "slo_ok": m["slo_ok"], "breaches": m["slo_breaches"]}
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.exception("qa_rollup: failed")
        report = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if alert and (not report.get("ok") or report.get("slo_ok") == 0):
        _alert_on_slo(report)
    return report


def _alert_on_slo(report: Dict[str, Any]) -> None:
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        if report.get("error"):
            text = f"QA rollup errored: {report['error']}"
        else:
            lines = "\n".join(f"- {b['slo']}: {b['value']} (threshold {b['threshold']})"
                              for b in report.get("breaches", []))
            text = f"SLO breach on {report.get('metric_date')}:\n{lines}"
        send_ops_alert("QA serving SLO breach", text, severity="critical",
                       dedup_key=f"qa-slo:{report.get('metric_date')}")
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
              f"p95={m['p95_latency_ms']}ms error_rate={m['error_rate']} slo_ok={m['slo_ok']}")
        for b in report.get("breaches", []):
            print(f"  ⚠️ SLO breach {b['slo']}: {b['value']} (threshold {b['threshold']})")
    return 0 if report.get("ok") and report.get("slo_ok", 1) == 1 else (2 if report.get("ok") else 3)


if __name__ == "__main__":
    import sys
    sys.exit(main())
