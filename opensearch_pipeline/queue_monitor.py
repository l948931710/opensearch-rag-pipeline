# -*- coding: utf-8 -*-
"""queue_monitor.py — 人工队列老化 + 摄取漏斗健康检查（盲区审计 P2-34 / P2-15 / P2-14）。

ops_monitor 此前只覆盖 reconcile + qa_rollup，结构性省略了每个人工协同队列与
「从未成功过的文档」：

  * run_queue_aging_check（P2-34）：review_task（spot_checker 权限泄露安全网等）/
    user_feedback 未处置差评——两条队列的积压量与最老 PENDING 龄（转人工已下线 2026-07，
    escalation_ticket 探针随之移除）。
    超 SLA（RAG_QUEUE_SLA_DAYS，默认 7 天）或积压超阈（RAG_QUEUE_BACKLOG_MAX，默认 50）
    → OBS-4 告警。没有这一层，人机协同面可静默停摆数月而 ops 全绿（P1-2 的复盘场景）。
  * run_ingest_funnel_check（P2-15）：所有 reconciler 都从 active-INDEXED 起算，从未到达
    该态的文档在 parity 宇宙之外——卡 LOADING/PROCESSING 超时（呼应 2h 失效锁但覆盖
    bare 运行）、注册超 24h 仍未 INDEXED、NEEDS_REVIEW/FAILED 积压，三类各自计数告警。
  * write_heartbeat（P2-14）：ops_monitor 每次运行 UPSERT 心跳行（rag_runtime_contract，
    schema/018）；kb_console 治理看板读它——心跳超 26h = 监控链路本身死了（笔记本 cron
    停/凭据过期），看板亮红 + 兜底告警。真正的外部死人开关（cron-ping 服务）仍是
    user-gated 基建，这里给出代码侧可达的最大覆盖：serving 进程是全系统最活的组件，
    让它当被动监工。

全部 fail-open + simulate no-op（与 ops_monitor 既有作业同款姿态）；只读（心跳 UPSERT 除外）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_HEARTBEAT_KEY = "ops_monitor_heartbeat"


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _dbs():
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    return cfg.rds.database, cfg.rds.operation_database


def _one(cur, sql: str, params=()) -> tuple:
    cur.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return (0, None)
    if isinstance(row, dict):
        vals = list(row.values())
        return (vals[0] or 0, vals[1] if len(vals) > 1 else None)
    return (row[0] or 0, row[1] if len(row) > 1 else None)


def run_queue_aging_check(*, alert: bool = True) -> Dict[str, Any]:
    """人工队列的积压/最老龄检查（P2-34）。返回 {ok, queues, breaches}。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    if cfg.simulate or cfg.simulate_db:
        return {"ok": True, "skipped": "simulate"}

    sla_days = _env_num("RAG_QUEUE_SLA_DAYS", 7)
    backlog_max = _env_num("RAG_QUEUE_BACKLOG_MAX", 50)
    kb_db, op_db = _dbs()
    # 队列名 → (SQL, 库说明)：COUNT + 最老龄（天）。open 语义与各自消费端一致。
    probes = {
        "review_task": (
            f"SELECT COUNT(*), MAX(DATEDIFF(NOW(), created_at)) FROM {kb_db}.review_task"
            " WHERE review_status = 'PENDING'"),
        "user_feedback_unhandled": (
            f"SELECT COUNT(*), MAX(DATEDIFF(NOW(), created_at)) FROM {op_db}.user_feedback"
            " WHERE feedback_type='downvote'"
            "   AND (handled_status IS NULL OR handled_status NOT IN ('RESOLVED','DISMISSED'))"),
    }
    queues: Dict[str, Any] = {}
    breaches = []
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
            with conn.cursor() as cur:
                for name, sql in probes.items():
                    try:
                        backlog, oldest = _one(cur, sql)
                        backlog, oldest = int(backlog), int(oldest or 0)
                        queues[name] = {"backlog": backlog, "oldest_days": oldest}
                        if oldest > sla_days:
                            breaches.append({"queue": name, "kind": "sla_age",
                                             "oldest_days": oldest, "sla_days": sla_days})
                        if backlog > backlog_max:
                            breaches.append({"queue": name, "kind": "backlog",
                                             "backlog": backlog, "max": backlog_max})
                    except Exception as e:  # noqa: BLE001 — 单队列失败诚实记 error，不拖垮其余
                        queues[name] = {"error": str(e)}
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.exception("queue_aging: 连接失败")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    report = {"ok": not breaches, "queues": queues, "breaches": breaches}
    if alert and breaches:
        try:
            from opensearch_pipeline.alerting import send_ops_alert
            lines = "\n".join(
                f"- {b['queue']}: " + (f"最老 PENDING {b['oldest_days']} 天（SLA {b['sla_days']}）"
                                       if b["kind"] == "sla_age"
                                       else f"积压 {b['backlog']} 条（阈值 {b['max']}）")
                for b in breaches)
            send_ops_alert("人工队列老化/积压超标", lines + "\n\n请到知识库控制台处理对应队列。",
                           severity="warning", dedup_key="queue-aging")
        except Exception:  # noqa: BLE001
            logger.warning("queue_aging: 告警发送失败（fail-open）", exc_info=True)
    return report


def run_ingest_funnel_check(*, alert: bool = True) -> Dict[str, Any]:
    """摄取漏斗完整性（P2-15）：从未到达 active-INDEXED 的文档在既有 parity 宇宙之外，
    与「文档不存在」无从区分。三类探针：卡处理态超时 / 注册超龄未入索引 / 复审与失败积压。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    if cfg.simulate or cfg.simulate_db:
        return {"ok": True, "skipped": "simulate"}

    stuck_hours = _env_num("RAG_FUNNEL_STUCK_HOURS", 4)      # >2h 失效锁阈值再放宽一档
    aging_hours = _env_num("RAG_FUNNEL_AGING_HOURS", 24)     # 日批节奏：注册次日仍未入索引=异常
    kb_db, _ = _dbs()
    probes = {
        # 卡在处理中：LOADING/PROCESSING 超时（2h 失效锁只覆盖 orchestrator 路径，这里全覆盖）
        "stuck_processing": (
            f"SELECT COUNT(*), MAX(TIMESTAMPDIFF(HOUR, updated_at, NOW()))"
            f" FROM {kb_db}.document_version"
            f" WHERE content_process_status IN ('LOADING','PROCESSING')"
            f"   AND updated_at < NOW() - INTERVAL {int(stuck_hours)} HOUR"),
        # 注册超龄仍未入索引（含 SKIPPED_DUPLICATE/classify FAILED 后不再推进的整族）：
        # 只看 active 文档的当前版本，排除已进删除握手/隔离的
        "registered_not_indexed": (
            f"SELECT COUNT(*), MAX(TIMESTAMPDIFF(HOUR, dv.created_at, NOW()))"
            f" FROM {kb_db}.document_version dv"
            f" JOIN {kb_db}.document_meta dm"
            f"   ON dm.doc_id = dv.doc_id AND dm.current_version_no = dv.version_no"
            f" WHERE dm.status = 'active'"
            f"   AND dv.created_at < NOW() - INTERVAL {int(aging_hours)} HOUR"
            f"   AND (dv.index_status IS NULL OR dv.index_status NOT IN"
            f"        ('SUCCESS','PENDING_DELETE','DELETED'))"
            f"   AND COALESCE(dv.publish_status,'') <> 'QUARANTINED'"),
        # 需人工/失败积压（NEEDS_REVIEW 是 0-chunk/降级等出口；FAILED 靠重试认领但可能反复失败）
        "needs_review_or_failed": (
            f"SELECT COUNT(*), MAX(TIMESTAMPDIFF(HOUR, dv.updated_at, NOW()))"
            f" FROM {kb_db}.document_version dv"
            f" JOIN {kb_db}.document_meta dm"
            f"   ON dm.doc_id = dv.doc_id AND dm.current_version_no = dv.version_no"
            f" WHERE dm.status = 'active'"
            f"   AND dv.content_process_status IN ('NEEDS_REVIEW','FAILED')"),
    }
    buckets: Dict[str, Any] = {}
    problems = []
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
            with conn.cursor() as cur:
                for name, sql in probes.items():
                    try:
                        cnt, worst = _one(cur, sql)
                        buckets[name] = {"count": int(cnt), "worst_hours": int(worst or 0)}
                        if int(cnt) > 0:
                            problems.append(name)
                    except Exception as e:  # noqa: BLE001
                        buckets[name] = {"error": str(e)}
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.exception("ingest_funnel: 连接失败")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    report = {"ok": not problems, "buckets": buckets, "problems": problems}
    if alert and problems:
        try:
            from opensearch_pipeline.alerting import send_ops_alert
            lines = "\n".join(f"- {n}: {buckets[n]['count']} 个（最久 {buckets[n]['worst_hours']}h）"
                              for n in problems)
            send_ops_alert("摄取漏斗异常：存在从未入索引/卡住的文档",
                           lines + "\n\n这些文档对所有 parity 检查不可见（P2-15），请排查。",
                           severity="warning", dedup_key="ingest-funnel")
        except Exception:  # noqa: BLE001
            logger.warning("ingest_funnel: 告警发送失败（fail-open）", exc_info=True)
    return report


def write_heartbeat() -> bool:
    """P2-14：监控链路存活证明。ops_monitor 每次运行 UPSERT 心跳行；kb_console 治理
    看板读取并在 >26h 时亮红+兜底告警。表（rag_runtime_contract，schema/018）未 apply
    → fail-open False。"""
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        if cfg.simulate or cfg.simulate_db:
            return False
        kb_db = cfg.rds.database
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {kb_db}.rag_runtime_contract (contract_key, contract_value)"
                    " VALUES (%s, DATE_FORMAT(UTC_TIMESTAMP(), '%%Y-%%m-%%dT%%H:%%i:%%sZ'))"
                    " ON DUPLICATE KEY UPDATE contract_value = VALUES(contract_value)",
                    (_HEARTBEAT_KEY,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("ops 心跳写入失败（fail-open；schema/018 未 apply?）: %s", e)
        return False


def read_heartbeat_age_hours() -> Optional[float]:
    """读心跳龄（小时，UTC 基准）；表缺失/无行/失败 → None（消费方按「未知」处理）。"""
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        if cfg.simulate or cfg.simulate_db:
            return None
        kb_db = cfg.rds.database
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT TIMESTAMPDIFF(MINUTE, STR_TO_DATE(contract_value, '%%Y-%%m-%%dT%%H:%%i:%%sZ'),"
                    f" UTC_TIMESTAMP()) FROM {kb_db}.rag_runtime_contract WHERE contract_key = %s",
                    (_HEARTBEAT_KEY,))
                row = cur.fetchone()
                if not row:
                    return None
                minutes = row[0] if not isinstance(row, dict) else list(row.values())[0]
                return None if minutes is None else round(float(minutes) / 60.0, 1)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("ops 心跳读取失败（按未知处理）: %s", e)
        return None
