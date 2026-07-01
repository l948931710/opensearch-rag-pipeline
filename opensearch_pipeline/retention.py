# -*- coding: utf-8 -*-
"""
retention.py — 日志/审计表留存策略（F-36，2026-07-01）

背景：qa_session_log（携 MEDIUMTEXT content_blocks_json）、kb_audit_log、
document_sensitive_finding、pipeline_run 只进不出——看板窗口查询延迟随表体积
逐月爬升（012 索引只缓解不根治），且「无限期保留全部员工问答」本身是数据治理
暴露面。本模块把留存变成可执行策略：

  作业            表 (库)                                动作                        默认窗口 (env)
  ─────────────  ─────────────────────────────────────  ─────────────────────────  ─────────────────────────────────
  qa_blobs       qa_session_log (operation)             content_blocks_json→NULL    6 月  RAG_RETENTION_QA_BLOBS_MONTHS
  qa_rows        qa_session_log (operation)             整行 DELETE（仅留 rollup）  18 月 RAG_RETENTION_QA_MONTHS
  audit          kb_audit_log (knowledge)               整行 DELETE                 24 月 RAG_RETENTION_AUDIT_MONTHS
  pipeline_run   pipeline_run (knowledge)               整行 DELETE                 12 月 RAG_RETENTION_PIPELINE_RUN_MONTHS
  findings       document_sensitive_finding (knowledge) 整行 DELETE（见下守卫）     24 月 RAG_RETENTION_FINDING_MONTHS

  任一窗口设 0/负数 = 该作业停用。

安全设计（与全仓守卫哲学同源）：
  · **dry-run 默认**：只 SELECT COUNT 报告将影响的行数；真删需 `--commit` **且**
    RAG_RETENTION_ENABLE=true（双闸，防误跑）。
  · 走 `db._get_db_conn`（GuardedDBConnection）：production 放行、PROD-RO 物理只读、
    非生产→生产需当日 RAG_DESTRUCTIVE_PROD_ACK——三层守卫原样生效；此外 commit 前
    显式 `assert_destructive_write_allowed("log_retention", ...)` 早失败早响亮。
  · simulate 模式直接 skip（与 qa_rollup 同款语义）。
  · **qa_rows 的 rollup 活性守卫**：qa_daily_metrics 必须非空且最新 metric_date 距今
    ≤7 天（rollup 管线活着），否则拒删原始行——绝不在聚合断供时销毁唯一事实。
  · **findings 的当前版本守卫**：finding 所指 (doc_id, version_no) 若仍是
    document_meta.current_version_no（在服务的版本），永不删除——它记录着现役文档
    "哪里被脱敏过"，是活的审计依据；只清理已退役/被取代版本的历史 finding。
  · 批量执行（默认 LIMIT 5000/批、批间 0.2s、单作业单次上限 400 批）：短事务、
    不压 binlog/主备复制；超上限即停，次日续跑（幂等）。
  · 时区注：created_at 存的是 SAE 容器墙钟（太平洋时间），本模块统一用服务端
    `DATE_SUB(NOW(), INTERVAL n MONTH)` 比较——两侧同一时钟，月粒度下时区滑差无意义。

调度：DataWorks 日任务节点 `dataworks_nodes/retention_node.py`（生产推荐），或
MySQL EVENT（备选，见 docs；不推荐——绕过应用层守卫与告警）。

CLI：
  python -m opensearch_pipeline.retention                       # dry-run 全作业
  python -m opensearch_pipeline.retention --only qa_blobs,audit # dry-run 指定作业
  RAG_RETENTION_ENABLE=true python -m opensearch_pipeline.retention --commit
"""

import argparse
import os
import time
from typing import Dict, List, Optional

from opensearch_pipeline.config import get_config

DEFAULT_BATCH = 5000
MAX_BATCHES_PER_JOB = 400
SLEEP_BETWEEN_BATCHES = 0.2

_JOB_NAMES = ("qa_blobs", "qa_rows", "audit", "pipeline_run", "findings")


def _kb_db() -> str:
    return get_config().rds.database


def _op_db() -> str:
    from opensearch_pipeline.qa_logger import _op_db as qa_op_db
    return qa_op_db()


def _months(env_key: str, default: int) -> int:
    try:
        return int(os.environ.get(env_key, str(default)))
    except ValueError:
        return default


def _retention_windows() -> Dict[str, int]:
    return {
        "qa_blobs": _months("RAG_RETENTION_QA_BLOBS_MONTHS", 6),
        "qa_rows": _months("RAG_RETENTION_QA_MONTHS", 18),
        "audit": _months("RAG_RETENTION_AUDIT_MONTHS", 24),
        "pipeline_run": _months("RAG_RETENTION_PIPELINE_RUN_MONTHS", 12),
        "findings": _months("RAG_RETENTION_FINDING_MONTHS", 24),
    }


# ─── 作业 SQL（count 与 act 必须同谓词；%s = months）─────────────────────────

def _job_sqls(job: str) -> Dict[str, str]:
    op, kb = _op_db(), _kb_db()
    if job == "qa_blobs":
        pred = ("FROM {op}.qa_session_log WHERE content_blocks_json IS NOT NULL "
                "AND created_at < DATE_SUB(NOW(), INTERVAL %s MONTH)").format(op=op)
        return {"count": f"SELECT COUNT(*) {pred}",
                "act": (f"UPDATE {op}.qa_session_log SET content_blocks_json = NULL "
                        "WHERE content_blocks_json IS NOT NULL "
                        "AND created_at < DATE_SUB(NOW(), INTERVAL %s MONTH) LIMIT %s")}
    if job == "qa_rows":
        pred = ("FROM {op}.qa_session_log "
                "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s MONTH)").format(op=op)
        return {"count": f"SELECT COUNT(*) {pred}",
                "act": (f"DELETE {pred} LIMIT %s")}
    if job == "audit":
        pred = ("FROM {kb}.kb_audit_log "
                "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s MONTH)").format(kb=kb)
        return {"count": f"SELECT COUNT(*) {pred}", "act": f"DELETE {pred} LIMIT %s"}
    if job == "pipeline_run":
        pred = ("FROM {kb}.pipeline_run "
                "WHERE started_at < DATE_SUB(NOW(), INTERVAL %s MONTH)").format(kb=kb)
        return {"count": f"SELECT COUNT(*) {pred}", "act": f"DELETE {pred} LIMIT %s"}
    if job == "findings":
        # 当前版本守卫：finding 所指版本仍为 current_version_no 的绝不删（活审计依据）。
        # 多表条件删除 MySQL 不允许 LIMIT → select-PK-then-delete 两步批。
        pred = (
            "FROM {kb}.document_sensitive_finding f "
            "LEFT JOIN {kb}.document_meta m ON m.doc_id = f.doc_id "
            "WHERE f.created_at < DATE_SUB(NOW(), INTERVAL %s MONTH) "
            "AND (m.doc_id IS NULL OR f.version_no <> m.current_version_no)"
        ).format(kb=kb)
        return {"count": f"SELECT COUNT(*) {pred}",
                "select_ids": f"SELECT f.id {pred} LIMIT %s",
                "act_by_ids": f"DELETE FROM {kb}.document_sensitive_finding WHERE id IN ({{ids}})"}
    raise ValueError(f"unknown retention job: {job}")


def _rollup_alive(cur) -> Optional[str]:
    """qa_rows 前置：rollup 必须非空且最新 metric_date ≤7 天前。返回 None=活，str=拒因。"""
    op = _op_db()
    cur.execute(f"SELECT COUNT(*), MAX(metric_date) FROM {op}.qa_daily_metrics")
    row = cur.fetchone() or (0, None)
    n, latest = int(row[0] or 0), row[1]
    if n == 0:
        return "qa_daily_metrics 为空——rollup 从未跑过，拒绝删除原始 qa 行"
    cur.execute("SELECT DATEDIFF(CURDATE(), %s)", (latest,))
    lag = int((cur.fetchone() or (999,))[0] or 999)
    if lag > 7:
        return f"qa_daily_metrics 最新 {latest}（滞后 {lag} 天>7）——rollup 疑似死掉，拒删原始行"
    return None


def run_retention(*, commit: bool = False, only: Optional[List[str]] = None,
                  batch: int = DEFAULT_BATCH,
                  max_batches: int = MAX_BATCHES_PER_JOB) -> Dict[str, dict]:
    """执行（或 dry-run）全部留存作业。返回 {job: report}；report['ok'] 恒有。"""
    cfg = get_config()
    if cfg.simulate or cfg.simulate_db:
        print("[retention] simulate 模式：skip（与 qa_rollup 同语义）")
        return {j: {"ok": True, "skipped": "simulate"} for j in (only or _JOB_NAMES)}

    if commit:
        if os.environ.get("RAG_RETENTION_ENABLE", "").lower() not in ("1", "true", "yes"):
            raise RuntimeError(
                "[retention] --commit 需要 RAG_RETENTION_ENABLE=true（双闸防误跑；"
                "DataWorks 节点里显式注入，交互跑请自证意图）")
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        assert_destructive_write_allowed("log_retention", cfg.rds.host, kind="rds")

    windows = _retention_windows()
    jobs = [j for j in _JOB_NAMES if (only is None or j in only)]
    reports: Dict[str, dict] = {}

    from opensearch_pipeline.db import _get_db_conn

    for job in jobs:
        months = windows[job]
        if months <= 0:
            reports[job] = {"ok": True, "skipped": f"window<=0 ({months})"}
            print(f"[retention] {job}: 停用（窗口 {months} 月）")
            continue
        rep: dict = {"ok": False, "months": months, "affected": 0, "batches": 0}
        reports[job] = rep
        try:
            conn = _get_db_conn()
            try:
                sqls = _job_sqls(job)
                with conn.cursor() as cur:
                    cur.execute(sqls["count"], (months,))
                    rep["affected"] = int((cur.fetchone() or (0,))[0] or 0)
                    if job == "qa_rows" and rep["affected"]:
                        reason = _rollup_alive(cur)
                        if reason:
                            rep["blocked"] = reason
                            print(f"[retention] qa_rows: ⛔ {reason}")
                            conn.rollback()
                            continue
                if not commit:
                    conn.rollback()   # count-only 读事务收尾
                    rep["ok"] = True
                    rep["dry_run"] = True
                    print(f"[retention] {job}: dry-run，将影响 {rep['affected']} 行"
                          f"（>{months} 月）")
                    continue
                deleted = 0
                for _ in range(max_batches):
                    with conn.cursor() as cur:
                        if "act_by_ids" in sqls:
                            cur.execute(sqls["select_ids"], (months, batch))
                            ids = [str(int(r[0])) for r in cur.fetchall()]
                            if not ids:
                                break
                            cur.execute(sqls["act_by_ids"].format(ids=",".join(ids)))
                            n = cur.rowcount
                        else:
                            cur.execute(sqls["act"], (months, batch))
                            n = cur.rowcount
                    conn.commit()   # 每批短事务提交
                    rep["batches"] += 1
                    deleted += max(n, 0)
                    if n < batch:
                        break
                    time.sleep(SLEEP_BETWEEN_BATCHES)
                else:
                    rep["capped"] = True   # 打满上限：今天到此为止，明天续（幂等）
                rep["deleted"] = deleted
                rep["ok"] = True
                print(f"[retention] {job}: 处理 {deleted} 行 / {rep['batches']} 批"
                      + ("（达单次上限，次日续跑）" if rep.get("capped") else ""))
            finally:
                conn.close()
        except Exception as e:
            rep["error"] = str(e)
            print(f"[retention] {job}: ✗ {e}")
    return reports


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="日志/审计表留存（dry-run 默认）")
    ap.add_argument("--commit", action="store_true",
                    help="真执行（还需 RAG_RETENTION_ENABLE=true 双闸）")
    ap.add_argument("--only", default=None,
                    help=f"逗号分隔作业名子集：{','.join(_JOB_NAMES)}")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--max-batches", type=int, default=MAX_BATCHES_PER_JOB)
    args = ap.parse_args(argv)

    only = None
    if args.only:
        only = [s.strip() for s in args.only.split(",") if s.strip()]
        bad = [j for j in only if j not in _JOB_NAMES]
        if bad:
            ap.error(f"未知作业 {bad}；可选：{','.join(_JOB_NAMES)}")

    reports = run_retention(commit=args.commit, only=only,
                            batch=args.batch, max_batches=args.max_batches)
    blocked = [j for j, r in reports.items() if r.get("blocked")]
    failed = [j for j, r in reports.items() if not r.get("ok") and not r.get("blocked")]
    if failed:
        print(f"[retention] 失败作业：{failed}")
        return 3
    if blocked:
        print(f"[retention] 被守卫拦下的作业：{blocked}（本身不算失败，需先修 rollup）")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
