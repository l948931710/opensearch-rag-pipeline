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
  · **删前冷归档（盲区审计 P3-18）**：qa_rows / audit 两个作业删的是架构文档指定的
    "唯一问答审计流水" 与特权操作审计——commit 时每批先 select-PK-then-archive：
    整行 gzip JSONL 上传 OSS `archive/retention/<table>/<run_ts>/batch-NNNN.jsonl.gz`，
    **归档上传失败即中止该作业（fail-closed，绝不先删后补）**；随后按已归档 id 精确
    DELETE（无 ORDER BY 的 DELETE..LIMIT 与 SELECT 可能选中不同行，故必须按 id 删）。
    RAG_RETENTION_ARCHIVE=false 显式退回旧的直删语义（无 OSS 的环境自担不可逆风险）。
  · 时区注：created_at 存的是 SAE 容器墙钟（太平洋时间），本模块统一用服务端
    `DATE_SUB(NOW(), INTERVAL n MONTH)` 比较——两侧同一时钟，月粒度下时区滑差无意义。

调度：DataWorks 日任务节点 `dataworks_nodes/retention_node.py`（生产推荐），或
MySQL EVENT（备选，见 docs；不推荐——绕过应用层守卫与告警）。

────────────────────────────────────────────────────────────────────────────
数据主体擦除（P2-5，PIPL 第 15/47 条）：`purge_subject(user_id, commit=False)`
按 user_id 跨 fuling_operation 硬删该主体的全部个人数据行（qa_retrieved_doc →
user_feedback → escalation_ticket → qa_conversation → qa_session_log，顺序不可倒）。
dry-run 默认打印各表将删行数；commit 双门 = RAG_SUBJECT_PURGE_ENABLE=true +
env_guard.assert_destructive_write_allowed。已知边界见 purge_subject docstring
（离职钩子未接、内存 session_store 需重启或后续接口、knowledge 侧业务台账不在范围）。

CLI：
  python -m opensearch_pipeline.retention                       # dry-run 全作业
  python -m opensearch_pipeline.retention --only qa_blobs,audit # dry-run 指定作业
  RAG_RETENTION_ENABLE=true python -m opensearch_pipeline.retention --commit
  python -m opensearch_pipeline.retention --purge-user <钉钉userId>   # 擦除 dry-run
  RAG_SUBJECT_PURGE_ENABLE=true python -m opensearch_pipeline.retention \
      --purge-user <钉钉userId> --commit                              # 真删（双门）
"""

import argparse
import os
import time
import uuid
from typing import Dict, List, Optional

from opensearch_pipeline.config import get_config

DEFAULT_BATCH = 5000
MAX_BATCHES_PER_JOB = 400
SLEEP_BETWEEN_BATCHES = 0.2

_JOB_NAMES = ("qa_blobs", "qa_rows", "audit", "pipeline_run", "findings", "qa_facts",
              "doc_notices")


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
        # perf#3 事实表（schema/013）：与 qa_rows 同窗——瘦行只服务 30 天级看板，跟主日志同期退役
        "qa_facts": _months("RAG_RETENTION_QA_FACTS_MONTHS", 18),
        # 升版提醒投递行（schema/065）：user_id 键控个人数据 ⇒ 有窗 + 进主体擦除。
        # ⚠️ **只清 notice，绝不清 doc_update_event** —— 事件表是防重放台账，discover 的
        # 「无事件行」反连接依赖行存在，清掉 = 远古升版被重新发现并群发（评审 R2-B M-1）。
        "doc_notices": _months("RAG_RETENTION_DOC_NOTICE_MONTHS", 6),
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
                "act": (f"DELETE {pred} LIMIT %s"),
                # P3-18 归档路径（select-then-archive-then-delete-by-id）
                "select_rows": f"SELECT * {pred} LIMIT %s",
                "act_by_ids": f"DELETE FROM {op}.qa_session_log WHERE id IN ({{ids}})"}
    if job == "audit":
        pred = ("FROM {kb}.kb_audit_log "
                "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s MONTH)").format(kb=kb)
        return {"count": f"SELECT COUNT(*) {pred}", "act": f"DELETE {pred} LIMIT %s",
                "select_rows": f"SELECT * {pred} LIMIT %s",
                "act_by_ids": f"DELETE FROM {kb}.kb_audit_log WHERE id IN ({{ids}})"}
    if job == "qa_facts":
        # qa_retrieved_doc（schema/013，可选迁移）：表未建的环境由 run_retention 按 1146 静默 skip
        pred = ("FROM {op}.qa_retrieved_doc "
                "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s MONTH)").format(op=op)
        return {"count": f"SELECT COUNT(*) {pred}", "act": f"DELETE {pred} LIMIT %s"}
    if job == "doc_notices":
        # schema/065 可选迁移：表未建的环境由 run_retention 按 1146 静默 skip。
        pred = ("FROM {op}.doc_update_notice "
                "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s MONTH)").format(op=op)
        return {"count": f"SELECT COUNT(*) {pred}", "act": f"DELETE {pred} LIMIT %s"}
    if job == "pipeline_run":
        pred = ("FROM {kb}.pipeline_run "
                "WHERE started_at < DATE_SUB(NOW(), INTERVAL %s MONTH)").format(kb=kb)
        return {"count": f"SELECT COUNT(*) {pred}", "act": f"DELETE {pred} LIMIT %s"}
    if job == "findings":
        # 当前版本守卫：finding 所指版本仍为 current_version_no 的绝不删（活审计依据）。
        # 多表条件删除 MySQL 不允许 LIMIT → select-PK-then-delete 两步批。
        # ⚠️ doc_id JOIN 必须 collation-cast（2026-07-02 生产首跑实测 1267）：
        # document_sensitive_finding 建表未显式 COLLATE、吃了库默认 _0900_ai_ci，而
        # document_meta 是 _unicode_ci——与 kb_access_request / contribution reconcile 同坑。
        # 显式 COLLATE（coercibility 0）压过两侧隐式列 collation：比较按 unicode_ci 进行、
        # m.doc_id 索引仍可用；两侧本就一致的环境里是 no-op。
        pred = (
            "FROM {kb}.document_sensitive_finding f "
            "LEFT JOIN {kb}.document_meta m "
            "  ON m.doc_id = CONVERT(f.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci "
            "WHERE f.created_at < DATE_SUB(NOW(), INTERVAL %s MONTH) "
            "AND (m.doc_id IS NULL OR f.version_no <> m.current_version_no)"
        ).format(kb=kb)
        return {"count": f"SELECT COUNT(*) {pred}",
                "select_ids": f"SELECT f.id {pred} LIMIT %s",
                "act_by_ids": f"DELETE FROM {kb}.document_sensitive_finding WHERE id IN ({{ids}})"}
    raise ValueError(f"unknown retention job: {job}")


# ─── P3-18 删前冷归档（qa_rows / audit）──────────────────────────────────────

_ARCHIVE_TABLES = {"qa_rows": "qa_session_log", "audit": "kb_audit_log"}


def _archive_enabled() -> bool:
    """默认开（治理方向 fail-closed）；无 OSS 的环境显式设 false 退回直删。"""
    return os.environ.get("RAG_RETENTION_ARCHIVE", "true").lower() in ("1", "true", "yes")


def _new_archive_run_id() -> str:
    """归档 run id：**时间戳 + UUID**。

    C5 主修复：原先用 `time.strftime("%Y%m%dT%H%M%S")` 单独作 key 段——**秒级粒度**。
    retention 无全局 lease，调度重入/并发（DataWorks 补数据 + 日常调度同秒起跑）会生成
    **完全相同的 key**，`put_object` 默认覆盖 ⇒ 前一轮的归档对象被静默顶掉，而对应的
    RDS 行**已经删了** ⇒ 归档静默丢失、不可恢复。时间戳段保留只为可读/可排序，
    唯一性完全由 uuid4 承担。
    """
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}"


def _archive_batch(job: str, rows, cols: List[str], batch_no: int, run_ts: str) -> str:
    """一批行 → gzip JSONL → OSS 冷归档。返回 OSS key；任何失败 raise（调用方按
    fail-closed 中止该作业——绝不出现"删了但没归档"）。

    run_ts 由 `_new_archive_run_id()` 生成（含 uuid，见其 docstring）。
    写入带 `x-oss-forbid-overwrite` —— **纵深防御，不是主修复**：即便 key 仍不知怎么撞了，
    也宁可整批 raise（fail-closed 中止、行不删）也不静默顶掉已有归档。
    """
    import gzip
    import io
    import json as _json

    from opensearch_pipeline.clients import _get_oss_bucket
    bucket, is_sim = _get_oss_bucket()
    if is_sim or bucket is None:
        raise RuntimeError(
            "OSS 不可用（simulate/占位凭据）——归档不可行，拒绝删除。"
            "确无归档需求可设 RAG_RETENTION_ARCHIVE=false 显式退回直删。")
    table = _ARCHIVE_TABLES[job]
    key = f"archive/retention/{table}/{run_ts}/batch-{batch_no:04d}.jsonl.gz"
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for r in rows:
            d = r if isinstance(r, dict) else dict(zip(cols, r))
            gz.write((_json.dumps(d, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    # 禁止覆盖：对象已存在 ⇒ OSS 返错 ⇒ 本批 raise ⇒ 调用方 fail-closed 中止该作业。
    # 老 SDK/兼容端不认该头时退回普通写（能力探测失败不阻断归档主路径）。
    try:
        bucket.put_object(key, buf.getvalue(), headers={"x-oss-forbid-overwrite": "true"})
    except TypeError:      # 极老 oss2 不收 headers kwarg
        bucket.put_object(key, buf.getvalue())
    return key


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

    # ── C5 preflight：归档姿态检查（位置必须在 simulate 短路【之后】）────────────────
    # 不做这一步的话，`archive=true 但 OSS 不可用` 要等到阶段 2 的第一批才在 _archive_batch
    # 里爆——那时已经跑过一遍事务、且每天重复失败。
    # 🔴 谓词必须**三条同时成立**才拦（codex BLOCKER）：
    #   ① 本次选中了归档表（6 个作业里只有 qa_rows / audit 两个）
    #   ② RAG_RETENTION_ARCHIVE=true
    #   ③ **该作业 window > 0** —— `months <= 0` 是合法 skip，漏掉这条会把
    #      「qa_rows window=0 + 无 OSS」从应有的 SKIPPED 打成 FATAL，与 SKIPPED 定义直接矛盾。
    # ⚠️ 能力边界：只查配置/simulation 姿态/oss2 依赖；**不写对象就不能证明 bucket 存在
    #    或账号有 PutObject 权限** —— 这不是端到端可写性证明。
    _archive_required = _archive_enabled() and any(
        j in _ARCHIVE_TABLES and windows.get(j, 0) > 0 for j in jobs)
    if _archive_required:
        from opensearch_pipeline.clients import _get_oss_bucket
        _bkt, _is_sim = _get_oss_bucket()
        if _is_sim or _bkt is None:
            raise RuntimeError(
                "[retention] preflight 失败：RAG_RETENTION_ARCHIVE=true 且本次含归档作业"
                f"（{[j for j in jobs if j in _ARCHIVE_TABLES]}），但 OSS 不可用"
                "（simulate/占位凭据/缺 oss2）。删前冷归档打不通即拒绝启动——"
                "确无归档需求请显式设 RAG_RETENTION_ARCHIVE=false 并书面接受不可逆直删。")

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
                # P3-18：qa_rows/audit 是审计流水——commit 且归档开启时走
                # select→OSS 归档→按 id 删；归档上传失败 raise 中止该作业（fail-closed）。
                _arch_on = job in _ARCHIVE_TABLES and _archive_enabled()
                _run_ts = _new_archive_run_id()   # C5：含 uuid，杜绝并发/重入撞 key
                deleted = 0
                for _b in range(max_batches):
                    with conn.cursor() as cur:
                        if _arch_on:
                            cur.execute(sqls["select_rows"], (months, batch))
                            cols = [d[0] for d in (cur.description or [])]
                            rows = cur.fetchall() or []
                            if not rows:
                                break
                            _id_i = cols.index("id")
                            ids = [str(int(r["id"] if isinstance(r, dict) else r[_id_i]))
                                   for r in rows]
                            key = _archive_batch(job, rows, cols, _b, _run_ts)
                            rep["archive_objects"] = rep.get("archive_objects", 0) + 1
                            rep["archive_last_key"] = key
                            cur.execute(sqls["act_by_ids"].format(ids=",".join(ids)))
                            n = cur.rowcount
                        elif "select_ids" in sqls:   # findings：当前版本守卫的两步批
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
            errno = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
            if job in ("qa_facts", "doc_notices") and errno == 1146:
                # schema/013 与 065 均为可选迁移：未 apply 的环境（staging/本地）表不存在不算
                # 失败——其余作业的 1146 仍按事故上报（基础表缺失=部署错库）。
                rep["ok"] = True
                rep["skipped"] = ("qa_retrieved_doc 不存在（schema/013 未应用）"
                                  if job == "qa_facts"
                                  else "doc_update_notice 不存在（schema/065 未应用）")
                print(f"[retention] {job}: skip（表未建）")
            else:
                rep["error"] = str(e)
                print(f"[retention] {job}: ✗ {e}")
    return reports


# ─── P2-5 数据主体擦除（PIPL 第 15/47 条）────────────────────────────────────

def _purge_jobs(user_id: str) -> List[dict]:
    """主体擦除的表清单（列表顺序 = 删除顺序，⚠️ 不可倒）。

    qa_retrieved_doc（schema/013 事实表）没有 user_id 列，必须经 qa_session_log.message_id
    关联删除，且必须先于 qa_session_log 本体——先删日志则事实行成永久孤儿、再也定位不到。
    count 与 act 同谓词；act 为单表 DELETE + LIMIT（子查询指向他表，MySQL 允许 LIMIT）。

    `needs_anchor`/`is_anchor` 编码这条依赖（C6，2026-08-03）：仅靠"列表顺序"不够——顺序
    只保证【先尝试】，不保证【先成功】。前序表撞锁超时(1205)/死锁(1213)或打满 max_batches
    时，主循环此前照样继续删掉锚点表，把残余事实行的唯一定位键销毁 ⇒ 次日重跑 count 子查询
    返 0、报"全表 ok"，PIPL 擦除被报告为完成，而该主体的检索/引用轨迹永久留库且再也无法归属清除。
    """
    op = _op_db()
    return [
        {"table": "qa_retrieved_doc", "optional": True,   # schema/013 可选迁移，1146 → skip
         "needs_anchor": True,                            # 经 qa_session_log.message_id 定位
         "count": (f"SELECT COUNT(*) FROM {op}.qa_retrieved_doc WHERE message_id IN "
                   f"(SELECT message_id FROM {op}.qa_session_log WHERE user_id = %s)"),
         "act": (f"DELETE FROM {op}.qa_retrieved_doc WHERE message_id IN "
                 f"(SELECT message_id FROM {op}.qa_session_log WHERE user_id = %s) LIMIT %s")},
        {"table": "user_feedback", "optional": False,
         "count": f"SELECT COUNT(*) FROM {op}.user_feedback WHERE user_id = %s",
         "act": f"DELETE FROM {op}.user_feedback WHERE user_id = %s LIMIT %s"},
        # 升版提醒投递行（schema/065，可选迁移 ⇒ optional）：user_id 是钉钉 staffId，
        # 行本身物化了"这个人当时可见哪些文档"，比 qa 行更该随主体擦除一起删。
        # 对应的 doc_update_event 不含任何个人标识，且是防重放台账 ⇒ **不进本清单**。
        {"table": "doc_update_notice", "optional": True,
         "count": f"SELECT COUNT(*) FROM {op}.doc_update_notice WHERE user_id = %s",
         "act": f"DELETE FROM {op}.doc_update_notice WHERE user_id = %s LIMIT %s"},
        # 仅删该用户【提出】的工单；user 仅作为 assigned_user_id（处理人）出现的行
        # 承载的是提单人的主体数据，不删。
        {"table": "escalation_ticket", "optional": False,
         "count": f"SELECT COUNT(*) FROM {op}.escalation_ticket WHERE user_id = %s",
         "act": f"DELETE FROM {op}.escalation_ticket WHERE user_id = %s LIMIT %s"},
        {"table": "qa_conversation", "optional": True,    # schema/006 可选迁移，1146 → skip
         "count": f"SELECT COUNT(*) FROM {op}.qa_conversation WHERE user_id = %s",
         "act": f"DELETE FROM {op}.qa_conversation WHERE user_id = %s LIMIT %s"},
        {"table": "qa_session_log", "optional": False,    # 最后删（事实行的 message_id 锚点）
         "is_anchor": True,                               # 前序 needs_anchor 表未全清 ⇒ 不得删
         "count": f"SELECT COUNT(*) FROM {op}.qa_session_log WHERE user_id = %s",
         "act": f"DELETE FROM {op}.qa_session_log WHERE user_id = %s LIMIT %s"},
    ]


def _purge_archives_for_subject(user_id: str, *, commit: bool = False) -> dict:
    """把该数据主体的行从 **OSS 冷归档**里抹掉（PIPL「被删除权」的归档面）。

    ── 为什么必须有这个函数 ────────────────────────────────────────────────
    C5 选了方案 A（打通删前冷归档）之后，`qa_session_log` 的到期行不再是直删，而是先
    gzip-JSONL 落到 OSS。`purge_subject` 原本只清 5 张 RDS 表 ⇒ 主体请求擦除后，
    **归档副本里仍然完整保留着他的问答记录**，而且账面会报「擦除完成」。
    选 A 就必须同批补上这一面，否则等于用一个合规缺口换另一个。

    ── 覆盖面（与 RDS 侧**逐字一致**，不多不少）──────────────────────────
    `_ARCHIVE_TABLES` 有两张表，这里**只清 qa_session_log**：
      · `qa_session_log` —— 带 `user_id`，且在 `_purge_jobs` 覆盖内 ⇒ 清；
      · `kb_audit_log`   —— 操作者审计（operator_id），**不在** `_purge_jobs` 范围内，
        RDS 侧本来就不删 ⇒ 这里也不动。两侧口径若不一致，又是一个新缺口。
    归档行是 `SELECT *` 落盘的，`user_id` 列在，主体可定位。

    ── 语义 ────────────────────────────────────────────────────────────────
    · **fail-closed**：OSS 不可达 ⇒ raise。绝不「查不到就当没有」——那正是会把
      「归档里还留着」谎报成「已擦除」的路径。
    · 重写用**同 key 覆盖写**（不带 forbid-overwrite）：put 是原子替换，不留对象消失的窗口；
      与 `_archive_batch` 的写入姿态**有意不同**，那里禁覆盖是防撞 key，这里覆盖是本意。
    · 整个对象的行都属于该主体 ⇒ 直接删对象（不留空 gz）。
    · dry-run 默认：只统计，不写不删。
    """
    import gzip
    import io
    import json as _json

    from opensearch_pipeline.clients import _get_oss_bucket
    bucket, is_sim = _get_oss_bucket()
    if is_sim or bucket is None:
        raise RuntimeError(
            "[purge_subject] OSS 不可达（simulate/占位凭据/缺 oss2）——无法确认冷归档中是否"
            "仍留有该主体数据，拒绝声称擦除完成。（若本环境从未开启归档，"
            "请显式确认后再跑，或设 RAG_RETENTION_ARCHIVE=false 的环境本就无归档对象。）")

    prefix = f"archive/retention/{_ARCHIVE_TABLES['qa_rows']}/"
    # `affected` 与各 RDS 表报告**同名同义**（本次命中的行数）——报告形状统一，
    # 下游（CLI 汇总/看板）才能把 6 个面一视同仁地消费，不必给归档面开特例分支。
    rep = {"prefix": prefix, "objects_scanned": 0, "objects_rewritten": 0,
           "objects_deleted": 0, "rows_removed": 0, "affected": 0,
           "dry_run": not commit, "ok": False}
    marker = ""
    while True:
        page = bucket.list_objects(prefix=prefix, marker=marker, max_keys=1000)
        for obj in getattr(page, "object_list", []) or []:
            key = obj.key
            if not key.endswith(".jsonl.gz"):
                continue
            rep["objects_scanned"] += 1
            raw = bucket.get_object(key).read()
            keep, removed = [], 0
            with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gz:
                for line in gz.read().decode("utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        if str((_json.loads(line) or {}).get("user_id", "")) == str(user_id):
                            removed += 1
                            continue
                    except Exception:      # noqa: BLE001 — 坏行原样保留，绝不因解析失败丢数据
                        pass
                    keep.append(line)
            if not removed:
                continue
            rep["rows_removed"] += removed
            rep["affected"] = rep["rows_removed"]
            if not commit:
                continue
            if keep:
                buf = io.BytesIO()
                with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                    gz.write(("\n".join(keep) + "\n").encode("utf-8"))
                bucket.put_object(key, buf.getvalue())      # 同 key 原子覆盖
                rep["objects_rewritten"] += 1
            else:
                bucket.delete_object(key)                    # 整对象都属该主体
                rep["objects_deleted"] += 1
        if not getattr(page, "is_truncated", False):
            break
        marker = getattr(page, "next_marker", "") or ""
    rep["ok"] = True
    print(f"[purge_subject] oss_archive: 扫 {rep['objects_scanned']} 个对象，"
          f"命中 {rep['rows_removed']} 行"
          + ("（dry-run，未改动）" if not commit
             else f"，重写 {rep['objects_rewritten']} / 删除 {rep['objects_deleted']}"))
    return rep


def purge_subject(user_id: str, *, commit: bool = False, batch: int = DEFAULT_BATCH,
                  max_batches: int = MAX_BATCHES_PER_JOB) -> Dict[str, dict]:
    """按 user_id 硬删 fuling_operation 内该数据主体的全部个人数据行（dry-run 默认）。

    覆盖表与顺序见 _purge_jobs。dry-run 只 SELECT COUNT 各表将删行数；commit=True 双门：
    RAG_SUBJECT_PURGE_ENABLE=true（与 retention 的 RAG_RETENTION_ENABLE 同款纪律、刻意
    独立成两个 env——开日常留存不应连带开主体擦除）**且** env_guard
    assert_destructive_write_allowed（PROD-RO 拒；非生产→生产需当日 ack）。
    批量执行与留存作业同参（LIMIT/批、批间 sleep、上限批数，超限次日续跑幂等）。

    已知边界（作为台账留在此处）：
      · 离职/主体请求钩子未接——目前由管理员人工触发 CLI（--purge-user）；
      · serving 进程内存会话（session_store，最近对话上下文）不在本作业范围：
        重启进程即清，或等后续管理接口；
      · qa_daily_metrics 是无 user_id 的聚合表（非个人数据），不删；
      · 群聊 `$:LWCP_v1` 合成身份不映射真实 user_id，无法按主体定位（见 qa-log gotchas）；
      · fuling_knowledge 侧业务台账（kb_contribution / kb_access_request 等）绑定文档
        生命周期与审批链，暂不在本作业（需要时单独评审擦除口径）。

    Returns: {"user_id":…, "dry_run":…, "ok":…, "tables": {表名: report}}；绝不半途 raise
    单表失败（记入该表 report["error"]，其余表继续）。
    """
    user_id = (user_id or "").strip()
    if not user_id:
        # 空 user_id 会让 WHERE user_id='' 命中历史脏行 / 全表——宁可拒绝。
        raise ValueError("purge_subject: user_id 不能为空")

    cfg = get_config()
    result: Dict[str, dict] = {"user_id": user_id, "dry_run": not commit,
                               "ok": True, "tables": {}}
    if cfg.simulate or cfg.simulate_db:
        print(f"[purge_subject] simulate 模式：skip（user_id={user_id}）")
        result["skipped"] = "simulate"
        return result

    if commit:
        if os.environ.get("RAG_SUBJECT_PURGE_ENABLE", "").lower() not in ("1", "true", "yes"):
            raise RuntimeError(
                "[purge_subject] --commit 需要 RAG_SUBJECT_PURGE_ENABLE=true"
                "（双闸防误跑，与 retention 的 RAG_RETENTION_ENABLE 同款纪律）")
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        assert_destructive_write_allowed("subject_purge", cfg.rds.host, kind="rds")

    from opensearch_pipeline.db import _get_db_conn

    # C6：锚点依赖门 —— 记录每张 needs_anchor 表是否【真正清空】。合法的 1146 skip
    # （可选迁移未 apply ⇒ 表不存在 ⇒ 不可能有孤儿）不算未清，不阻断锚点删除。
    anchor_blockers: List[str] = []

    for job in _purge_jobs(user_id):
        table = job["table"]
        rep: dict = {"ok": False, "affected": 0, "batches": 0}
        result["tables"][table] = rep
        if job.get("is_anchor") and anchor_blockers:
            # 锚点表是残余关联行【唯一】的定位键。前序未清干净就删它 = 制造永久不可定位的
            # 个人数据孤儿，与本函数目的直接相悖 ⇒ 宁可本轮不删、次日重跑（幂等），也不销毁锚点。
            rep["blocked_by"] = sorted(anchor_blockers)
            rep["error"] = (f"前序关联表未全部清空（{', '.join(sorted(anchor_blockers))}）："
                            "删除锚点表会使残余事实行永久无法归属清除，本轮拒绝删除。")
            result["ok"] = False
            print(f"[purge_subject] {table}: ⛔ 阻断（前序未清：{', '.join(sorted(anchor_blockers))}）")
            continue
        try:
            conn = _get_db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(job["count"], (user_id,))
                    rep["affected"] = int((cur.fetchone() or (0,))[0] or 0)
                if not commit:
                    conn.rollback()   # count-only 读事务收尾
                    rep["ok"] = True
                    rep["dry_run"] = True
                    print(f"[purge_subject] {table}: dry-run，将删 {rep['affected']} 行")
                    continue
                deleted = 0
                for _ in range(max_batches):
                    with conn.cursor() as cur:
                        cur.execute(job["act"], (user_id, batch))
                        n = cur.rowcount
                    conn.commit()   # 每批短事务提交
                    rep["batches"] += 1
                    deleted += max(n, 0)
                    if n < batch:
                        break
                    time.sleep(SLEEP_BETWEEN_BATCHES)
                else:
                    rep["capped"] = True   # 打满上限：次日续跑（幂等）
                    if job.get("needs_anchor"):
                        # capped = 还有残行没删完。此时删锚点表 = 残行永久失去定位键。
                        anchor_blockers.append(f"{table}(capped)")
                rep["deleted"] = deleted
                rep["ok"] = True
                print(f"[purge_subject] {table}: 删除 {deleted} 行 / {rep['batches']} 批"
                      + ("（达单次上限，次日续跑）" if rep.get("capped") else ""))
            finally:
                conn.close()
        except Exception as e:
            errno = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
            if job["optional"] and errno == 1146:
                # 可选迁移（schema/006、013）未 apply 的环境：表不存在不算失败。
                # ⚠️ 这是【合法 skip】而非未清：表都不存在，不可能有孤儿 ⇒ 不进 anchor_blockers。
                rep["ok"] = True
                rep["skipped"] = f"{table} 不存在（可选迁移未应用）"
                print(f"[purge_subject] {table}: skip（表未建）")
            else:
                if job.get("needs_anchor"):
                    # 撞锁超时(1205)/死锁(1213) 等真失败：残行仍在，锚点不能删。
                    anchor_blockers.append(f"{table}(error)")
                rep["error"] = str(e)
                result["ok"] = False
                print(f"[purge_subject] {table}: ✗ {e}")

    # ── OSS 冷归档面（C5=方案A 的必然后果）────────────────────────────────────
    # RDS 行删干净 ≠ 主体数据清干净：qa_session_log 的到期行早已 gzip-JSONL 落进 OSS。
    # 归档面失败**必须**把整体判否——否则会出现「5 张表全绿、账面报擦除完成，而归档里
    # 原封不动」这种最坏形态（正是 C5 选 A 引入、必须同批堵上的 PIPL 缺口）。
    # 归档在 RDS **之后**清：顺序不影响正确性（user_id 是稳定定位键，两面各自幂等），
    # 但报告必须分面如实，绝不用一个总 ok 掩盖半边。
    try:
        result["tables"]["oss_archive"] = _purge_archives_for_subject(user_id, commit=commit)
    except Exception as e:      # noqa: BLE001 — 与各表同款：记错、判否、不半途 raise
        result["tables"]["oss_archive"] = {"ok": False, "error": str(e)}
        result["ok"] = False
        print(f"[purge_subject] oss_archive: ✗ {e}")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="日志/审计表留存 + 数据主体擦除（dry-run 默认）")
    ap.add_argument("--commit", action="store_true",
                    help="真执行（留存需 RAG_RETENTION_ENABLE=true，擦除需 "
                         "RAG_SUBJECT_PURGE_ENABLE=true 双闸）")
    ap.add_argument("--only", default=None,
                    help=f"逗号分隔作业名子集：{','.join(_JOB_NAMES)}")
    ap.add_argument("--purge-user", default=None, metavar="USER_ID",
                    help="P2-5 数据主体擦除：按 user_id 跨 fuling_operation 硬删该主体全部"
                         "个人数据行（与留存作业互斥；dry-run 默认）")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--max-batches", type=int, default=MAX_BATCHES_PER_JOB)
    args = ap.parse_args(argv)

    if args.purge_user is not None:
        if args.only:
            ap.error("--purge-user 与 --only 互斥（擦除有固定表清单与顺序）")
        try:
            rep = purge_subject(args.purge_user, commit=args.commit,
                                batch=args.batch, max_batches=args.max_batches)
        except (RuntimeError, ValueError) as e:
            print(f"[purge_subject] ✗ {e}")
            return 3
        return 0 if rep.get("ok") else 3

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
