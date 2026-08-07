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
from typing import Any, Dict, List, Optional

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


def _op_db() -> str:
    """运营库名。心跳表钉在这里（与 `runtime_contract.py` 同库同表），**不随 RAG_ENV 漂移**。"""
    return _dbs()[1]


def _one(cur, sql: str, params=()) -> tuple:
    cur.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return (0, None)
    if isinstance(row, dict):
        vals = list(row.values())
        return (vals[0] or 0, vals[1] if len(vals) > 1 else None)
    return (row[0] or 0, row[1] if len(row) > 1 else None)


def _is_missing_column(e: Exception) -> bool:
    """1054 / Unknown column ⇒ 该 schema 未 apply（能力降级），不是探针坏了。"""
    s = str(e)
    return "1054" in s or "Unknown column" in s or "1146" in s or "doesn't exist" in s


def run_acl_projection_check(*, alert: bool = True) -> Dict[str, Any]:
    """ACL 投影收敛度检查（C3′ G10，2026-08-07）——**纯 SQL 判据**。

    为什么不能用 `reconcile_allowed_depts` 的返回值：那个函数只在 **DataWorks stage-3**
    里跑（`dataworks_orchestrator.py` 的 pre-drain hook 是仓内唯一 commit=True 调用点），
    而 ops_monitor 是**本机 launchd 作业**——`partially_locked` / `capped_versions` 这类
    **进程内计数器跨进程读不到**。所以「收敛失败」必须表达成库里查得到的状态。
    （同型错误本会话已犯过一次：`serving_pools.pool_stats().rejected` 同样是进程内的。）

    四条判据（任一超阈即 drift）：
      · `unconverged_docs` —— 有 active chunk 的 `acl_epoch IS NULL`（从未投影）或
        `< document_meta.acl_epoch`（落后于失效代次）。健康系统该恒为 0。
      · `invariant_violations` —— `cm.acl_epoch > dm.acl_epoch`。章不可能新过水位 ⇒
        手工改库 / 回滚 / 迁移错序，**必须有人看**。
      · `outbox_retrying` —— 投影 outbox 里 `attempts>=N` 仍未 done 的意图（反复失败）。
      · `outbox_stale_serving_hours` —— **有 active chunk** 的文档里，最老未落实意图的龄期。
        补上一条的盲区（从未被尝试过的意图 `attempts` 恒为 0，只看重试次数会把"没人 drain"
        读成健康），同时**不因真空期误报**：没有 active chunk 就没有投影对象。

    ⚠️ `outbox_pending` 只作为**观测指标**上报、不设阈值：2026-08-07 实测生产 2201→2219
    条未 done、`attempts` **全 0**、最老 30h、`reason` 99% 是 `node_register`，而历史上只
    done 过 1 条（08-05 16:18）⇒ 成因是**没人 drain**（drain 只在 DataWorks stage-3 跑，
    真空期里 stage-3 没跑），不是 drain 失败。表有 `UNIQUE KEY uniq_doc`，上界=文档数，
    不是泄漏；stage-3 一恢复即自愈。给 pending 设阈值会天天 page 而不解决问题。
    ⚠️ 062 未 apply（无 acl_epoch 列）⇒ 相关探针记入 `skipped_probes` 而**不是** errors ——
    能力降级与"探针坏了"必须可辨（附录B 的假绿教训：探针坏掉是 exit 3，不是 exit 2）。
    """
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    if cfg.simulate or cfg.simulate_db:
        return {"ok": True, "skipped": "simulate"}

    retry_max = int(_env_num("RAG_ACL_OUTBOX_RETRY_MAX", 3))
    # 龄期阈值：drain 每次 stage-3 跑一轮，48h 未落实说明 drain 根本没跑（或一直被锁）。
    stale_hours = int(_env_num("RAG_ACL_OUTBOX_STALE_HOURS", 48))
    unconverged_max = int(_env_num("RAG_ACL_UNCONVERGED_MAX", 0))
    kb_db, _ = _dbs()
    probes = {
        "unconverged_docs": (
            f"SELECT COUNT(DISTINCT cm.doc_id), NULL FROM {kb_db}.chunk_meta cm "
            f"JOIN {kb_db}.document_meta dm ON dm.doc_id=cm.doc_id "
            "WHERE cm.is_active=1 "
            "  AND (cm.acl_epoch IS NULL OR cm.acl_epoch < dm.acl_epoch)"),
        "invariant_violations": (
            f"SELECT COUNT(DISTINCT cm.doc_id), NULL FROM {kb_db}.chunk_meta cm "
            f"JOIN {kb_db}.document_meta dm ON dm.doc_id=cm.doc_id "
            "WHERE cm.is_active=1 AND cm.acl_epoch IS NOT NULL "
            "  AND cm.acl_epoch > dm.acl_epoch"),
        "outbox_retrying": (
            f"SELECT COUNT(*), NULL FROM {kb_db}.kb_acl_projection_outbox "
            f"WHERE done_at IS NULL AND attempts >= {retry_max}"),
        "outbox_pending": (
            f"SELECT COUNT(*), NULL FROM {kb_db}.kb_acl_projection_outbox "
            "WHERE done_at IS NULL"),
        # 🔴 龄期判据 —— 上面 `outbox_retrying` 有个盲区：**从未被尝试过**的意图
        # `attempts` 恒为 0，永远进不了那条。2026-08-07 实测生产正是这个形态：
        # 2201 条 pending、retrying=0、且一小时内还在涨 ⇒ 有人入队、没人 drain
        # （drain 只在 DataWorks stage-3 里跑）。只看重试次数会把它读成健康。
        "outbox_oldest_hours": (
            f"SELECT COALESCE(MAX(TIMESTAMPDIFF(HOUR, enqueued_at, NOW())), 0), NULL "
            f"FROM {kb_db}.kb_acl_projection_outbox WHERE done_at IS NULL"),
        # ⚠️ **判 breach 用的是下面这条，不是上面那条。**
        # 滞留的投影意图只有在该文档**还有 active chunk** 时才影响检索——没有 active chunk
        # 就没有投影对象，materialize 本来就是空操作。2026-08-07 实测：2201 条积压里
        # **有 active chunk 的 0 篇**（语料真空期），此时按总龄期告警＝制造一条已知成因的
        # 每日红，而"训练出对红色的免疫"比不告警更糟（同 `deploy/com.fuling.ops-monitor.plist`
        # 把 reconcile_raw 排除在日常集之外的理由）。
        # 上面那条继续作为**指标**上报，用来观察积压本身。
        "outbox_stale_serving_hours": (
            f"SELECT COALESCE(MAX(TIMESTAMPDIFF(HOUR, o.enqueued_at, NOW())), 0), NULL "
            f"FROM {kb_db}.kb_acl_projection_outbox o WHERE o.done_at IS NULL "
            f"  AND EXISTS (SELECT 1 FROM {kb_db}.chunk_meta c "
            "              WHERE c.doc_id=o.doc_id AND c.is_active=1)"),
    }
    metrics: Dict[str, Any] = {}
    skipped_probes: List[str] = []
    probe_errors: List[str] = []
    breaches: List[Dict[str, Any]] = []
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
            with conn.cursor() as cur:
                for name, sql in probes.items():
                    try:
                        n, _ = _one(cur, sql)
                        metrics[name] = int(n)
                    except Exception as e:  # noqa: BLE001
                        if _is_missing_column(e):
                            skipped_probes.append(f"{name}: schema 未 apply（{type(e).__name__}）")
                        else:
                            probe_errors.append(f"{name}: {type(e).__name__}: {e}")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.exception("acl_projection: 连接失败")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if metrics.get("unconverged_docs", 0) > unconverged_max:
        breaches.append({"kind": "unconverged", "docs": metrics["unconverged_docs"],
                         "max": unconverged_max})
    if metrics.get("invariant_violations", 0) > 0:
        breaches.append({"kind": "invariant", "docs": metrics["invariant_violations"]})
    if metrics.get("outbox_retrying", 0) > 0:
        breaches.append({"kind": "outbox_retry", "rows": metrics["outbox_retrying"],
                         "attempts_ge": retry_max})
    # 判据取 **serving 口径**（有 active chunk 的那批），不是总龄期——理由见上面的探针注释。
    if metrics.get("outbox_stale_serving_hours", 0) > stale_hours:
        breaches.append({"kind": "outbox_stale",
                         "oldest_hours": metrics["outbox_stale_serving_hours"],
                         "max_hours": stale_hours, "pending": metrics.get("outbox_pending")})
    # 探针失败进顶层 `errors` ⇒ exit 3（"探针坏了"），与 breaches ⇒ exit 2（"数据漂移"）可辨。
    report: Dict[str, Any] = {"ok": (not breaches) and (not probe_errors),
                              "metrics": metrics, "breaches": breaches}
    if skipped_probes:
        report["skipped_probes"] = skipped_probes
    if probe_errors:
        report["errors"] = probe_errors
    if alert and breaches:
        try:
            from opensearch_pipeline.alerting import send_ops_alert
            _desc = {
                "unconverged": lambda b: (f"{b['docs']} 篇文档的 ACL 投影未收敛"
                                          f"（acl_epoch 为 NULL 或落后于权威水位）"),
                "invariant": lambda b: (f"🔴 {b['docs']} 篇文档 chunk.acl_epoch > "
                                        f"document_meta.acl_epoch —— 章不可能新过水位，"
                                        f"疑手工改库/回滚/迁移错序"),
                "outbox_retry": lambda b: (f"{b['rows']} 条投影意图重试 ≥{b['attempts_ge']} 次仍未落实"),
                "outbox_stale": lambda b: (f"🔴 **影响检索的**投影意图已积压 {b['oldest_hours']} 小时"
                                           f"（阈值 {b['max_hours']}h；outbox 共 {b['pending']} 条待处理）"
                                           f" —— 这些文档有 active chunk 却拿不到新 ACL。"
                                           f"attempts 若全是 0 即**没人 drain**，"
                                           f"查 DataWorks stage-3 是否在跑"),
            }
            lines = "\n".join(f"- {_desc[b['kind']](b)}" for b in breaches)
            send_ops_alert(
                "ACL 投影收敛度异常", lines +
                "\n\n排查入口：allowed_depts_reconcile（stage-3 pre-drain 每轮跑）"
                " / kb_acl_projection_outbox 的 last_error。",
                severity="warning", dedup_key="acl-projection")
        except Exception:  # noqa: BLE001
            logger.warning("acl_projection: 告警发送失败（fail-open）", exc_info=True)
    return report


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

    probe_errors: List[str] = []
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
                    except Exception as e:  # noqa: BLE001 — 单队列失败不拖垮其余，但必须冒到顶层
                        queues[name] = {"error": str(e)}
                        probe_errors.append(f"{name}: {type(e).__name__}: {e}")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.exception("queue_aging: 连接失败")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ⚠️ **探针失败必须进顶层 `errors`**（附录B，2026-08-03 核实的假绿）：此前每个失败只写进
    # `queues[name]["error"]`，而 `ok` 只看 `breaches` ⇒ 探针 SQL 挂掉时 breaches 空、ok=True、
    # 顶层无 error 键 ⇒ `ops_monitor._job_exit` 落到 `return 0 if ok` ⇒ **exit 0 全绿**。
    # 极端情况下所有探针都失败（如 schema 变更打挂全部 SQL），监控什么都没测却报健康。
    # 探针坏掉是 **error(exit 3)**，不是 drift(exit 2) —— 两者必须可辨。
    report = {"ok": (not breaches) and (not probe_errors),
              "queues": queues, "breaches": breaches}
    if probe_errors:
        report["errors"] = probe_errors
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

    probe_errors: List[str] = []
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
                    except Exception as e:  # noqa: BLE001 — 同 queue_aging：必须冒到顶层
                        buckets[name] = {"error": str(e)}
                        probe_errors.append(f"{name}: {type(e).__name__}: {e}")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.exception("ingest_funnel: 连接失败")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 同 queue_aging：探针失败进顶层 errors（否则 ok=True + 无 error 键 ⇒ exit 0 假绿）
    report = {"ok": (not problems) and (not probe_errors),
              "buckets": buckets, "problems": problems}
    if probe_errors:
        report["errors"] = probe_errors
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
        # 🔴 2026-08-07（第二处）：**只读会话下根本不该尝试写**。
        # `com.fuling.ops-monitor` 跑在 `RAG_ENV=prod_ro`（RAG_READONLY=true）⇒ 这里的 UPSERT
        # 每次都被 ENV GUARD 挡掉，然后打一条 WARNING「心跳写入失败」——**那条告警本身是误导**：
        # 它读起来像"监控链断了"，实际是"本作业按设计不可写"。
        # 现在的正解是 agent 层两段式（见 `deploy/com.fuling.ops-monitor.plist`）：对账器继续
        # 跑 prod_ro，心跳由第二条命令用可写 env 单独盖章。所以这里**静默跳过**（info 级），
        # 让日志只在真正异常时才刺眼。
        if getattr(cfg, "readonly", False):
            logger.info("心跳写入跳过：本会话声明只读（RAG_READONLY=true）——"
                        "心跳由 agent 的第二段命令用可写 RAG_ENV 盖章，见 "
                        "deploy/com.fuling.ops-monitor.plist")
            return False
        # 🔴 2026-08-07：心跳库**必须钉死**，不能用 `cfg.rds.database`。
        # 原实现随 `RAG_ENV` 漂移：qa-rollup 跑 `RAG_ENV=metrics`（RAG_RDS_DATABASE=
        # fuling_operation）⇒ 写 fuling_operation；ops-monitor 跑 prod_ro / serving 跑 production
        # （=fuling_knowledge）⇒ 读 fuling_knowledge。**写的表没人读、读的表没人写**，
        # 死人开关等于空转。实测两个库里各有一行互不相干的心跳，时间戳相差 13 小时。
        # 钉到 `_op_db()`：`rag_runtime_contract` 的既有主人就是它
        # （runtime_contract.py:49 的 embedding 契约行同表同库），心跳是运维产物、本就该在运营库。
        hb_db = _op_db()
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {hb_db}.rag_runtime_contract (contract_key, contract_value)"
                    " VALUES (%s, DATE_FORMAT(UTC_TIMESTAMP(), '%%Y-%%m-%%dT%%H:%%i:%%sZ'))"
                    " ON DUPLICATE KEY UPDATE contract_value = VALUES(contract_value)",
                    (_HEARTBEAT_KEY,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        # ⚠️ 2026-08-06 实地：这里连续两个月报的其实是 **1142 缺授权**（fuling_metrics 对
        # rag_runtime_contract 没有 INSERT/UPDATE），而原文案只写「schema/018 未 apply?」，
        # 把排查方向带偏。后果不小：心跳一直没写进去，而 `_monitor_dead` 的判据是
        # 「期望有心跳而完全缺席 → 判死」——这条监控链实际上是断的。授权已补（GRANT）。
        # ⚠️ 2026-08-07 又发现第三种、而且是**当前实际命中的那种**：`ENV GUARD RAG_READONLY=true`。
        # `com.fuling.ops-monitor` 跑在 `RAG_ENV=prod_ro`（.env.prod_ro 设了 RAG_READONLY=true）
        # ⇒ 它的心跳写**每次都被守卫挡掉**、一次都没成功过。看板读到的新鲜心跳其实来自
        # `com.fuling.qa-rollup`（RAG_ENV=metrics，可写）。
        # ⇒ 死人开关按注释原意（「笔记本 cron 停/凭据过期」）仍有效——两个 agent 会一起死；
        #   但它**测不出"只有 ops-monitor 这一个 agent 坏了"**。处置待 Sam 拍（给 ops-monitor
        #   一条可写路径 / 拆成两个心跳键 / 明确把它定义为"调度器存活"而非"ops-monitor 存活"）。
        # 文案把三种成因都列上：上两个月就因为只列了 schema 一种而把排查带偏两次。
        logger.warning("ops 心跳写入失败（fail-open；1054/1146=schema/018 未 apply，"
                       "**1142=账号缺 INSERT/UPDATE 授权**，"
                       "**ENV GUARD/RAG_READONLY=true=本作业按设计不可写、需换 RAG_ENV**）: %s", e)
        return False


def read_heartbeat_age_hours() -> Optional[float]:
    """读心跳龄（小时，UTC 基准）；表缺失/无行/失败 → None（消费方按「未知」处理）。"""
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        if cfg.simulate or cfg.simulate_db:
            return None
        # 与 write_heartbeat 同库（见那边的说明）——读写必须钉同一个库，否则死人开关空转。
        hb_db = _op_db()
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn(select_db=False)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT TIMESTAMPDIFF(MINUTE, STR_TO_DATE(contract_value, '%%Y-%%m-%%dT%%H:%%i:%%sZ'),"
                    f" UTC_TIMESTAMP()) FROM {hb_db}.rag_runtime_contract WHERE contract_key = %s",
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
