# -*- coding: utf-8 -*-
"""retention.py（F-36 日志/审计表留存）回归测试。

覆盖：simulate skip、dry-run 只数不删、RAG_RETENTION_ENABLE 双闸、批量循环终止、
qa_rows 的 rollup 活性守卫、findings 的当前版本守卫 SQL、窗口停用、exit code。
"""
import datetime

import pytest

from opensearch_pipeline import retention
from opensearch_pipeline.config import get_config


class _ScriptedCursor:
    """按 SQL 关键词回放结果的假游标；记录所有执行过的 SQL。"""

    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("SELECT COUNT(*), MAX(metric_date)"):
            self._row = self._conn.rollup_state
        elif s.startswith("SELECT DATEDIFF"):
            self._row = (self._conn.rollup_lag_days,)
        elif s.startswith("SELECT COUNT(*)"):
            self._row = (self._conn.affected,)
        elif s.startswith(("SELECT f.id", "SELECT c.checkpoint_id")):
            batch = self._conn.id_batches.pop(0) if self._conn.id_batches else []
            self._rows = [(i,) for i in batch]
            self._row = None
        elif s.startswith("SELECT * "):   # P3-18 归档路径的整行拉取
            batch = self._conn.row_batches.pop(0) if self._conn.row_batches else []
            self._rows = batch
            self.description = [(c,) for c in self._conn.row_cols]
            self._row = None
        elif s.startswith(("DELETE", "UPDATE")):
            self.rowcount = self._conn.act_rowcounts.pop(0) if self._conn.act_rowcounts else 0
            self._conn.acts += 1
        return None

    def fetchone(self):
        return getattr(self, "_row", None)

    def fetchall(self):
        return getattr(self, "_rows", [])


class _ScriptedConn:
    def __init__(self, *, affected=0, act_rowcounts=None, rollup_state=(1, datetime.date.today()),
                 rollup_lag_days=0, id_batches=None, row_batches=None, row_cols=("id",)):
        self.affected = affected
        self.act_rowcounts = list(act_rowcounts or [])
        self.rollup_state = rollup_state
        self.rollup_lag_days = rollup_lag_days
        self.id_batches = list(id_batches or [])
        self.row_batches = list(row_batches or [])
        self.row_cols = list(row_cols)
        self.executed = []
        self.acts = 0
        self.commits = 0

    def cursor(self):
        return _ScriptedCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def live_db(monkeypatch):
    """把 config 切出 simulate（retention 才会真跑），host 保持 localhost（非生产目标）。"""
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    return cfg


def test_simulate_mode_skips():
    rep = retention.run_retention()
    assert all(r.get("skipped") == "simulate" for r in rep.values())


def test_dry_run_counts_without_acting(monkeypatch, live_db):
    conn = _ScriptedConn(affected=1234)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(only=["audit"])
    assert rep["audit"]["ok"] and rep["audit"]["dry_run"] and rep["audit"]["affected"] == 1234
    assert conn.acts == 0 and conn.commits == 0, "dry-run 绝不执行 DELETE/UPDATE、绝不 commit"


def test_commit_requires_enable_flag(monkeypatch, live_db):
    monkeypatch.delenv("RAG_RETENTION_ENABLE", raising=False)
    with pytest.raises(RuntimeError, match="RAG_RETENTION_ENABLE"):
        retention.run_retention(commit=True, only=["audit"])


def test_commit_batches_until_drained(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    monkeypatch.setenv("RAG_RETENTION_ARCHIVE", "false")   # 本测覆盖旧直删语义
    conn = _ScriptedConn(affected=7, act_rowcounts=[5, 2])   # 两批：5 + 2(<batch) 止
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["audit"], batch=5)
    assert rep["audit"]["ok"] and rep["audit"]["deleted"] == 7 and rep["audit"]["batches"] == 2
    assert conn.commits == 2, "每批一个短事务提交"


def test_qa_rows_blocked_when_rollup_empty(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=100, rollup_state=(0, None))
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["qa_rows"])
    assert rep["qa_rows"].get("blocked") and not rep["qa_rows"]["ok"]
    assert conn.acts == 0, "rollup 从未跑过时绝不删原始 qa 行"


def test_qa_rows_blocked_when_rollup_stale(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=100, rollup_state=(50, datetime.date(2026, 1, 1)),
                         rollup_lag_days=30)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["qa_rows"])
    assert "滞后" in rep["qa_rows"].get("blocked", "")
    assert conn.acts == 0


def test_qa_rows_proceeds_when_rollup_fresh(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    monkeypatch.setenv("RAG_RETENTION_ARCHIVE", "false")   # 本测覆盖旧直删语义
    conn = _ScriptedConn(affected=3, act_rowcounts=[3], rollup_lag_days=1)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["qa_rows"], batch=5000)
    assert rep["qa_rows"]["ok"] and rep["qa_rows"]["deleted"] == 3


# ── P3-18：删前冷归档（qa_rows / audit 默认走 select→归档→按 id 删）──


def test_archive_before_delete_uploads_then_deletes_by_id(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    monkeypatch.delenv("RAG_RETENTION_ARCHIVE", raising=False)   # 默认即开
    conn = _ScriptedConn(affected=2, rollup_lag_days=1,
                         row_batches=[[(11, "s1"), (12, "s2")]],
                         row_cols=("id", "session_id"),
                         act_rowcounts=[2])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    archived = []
    monkeypatch.setattr(retention, "_archive_batch",
                        lambda job, rows, cols, bno, ts: archived.append((job, rows, cols))
                        or f"archive/retention/x/batch-{bno:04d}.jsonl.gz")
    rep = retention.run_retention(commit=True, only=["qa_rows"], batch=5000)
    assert rep["qa_rows"]["ok"] and rep["qa_rows"]["deleted"] == 2
    assert rep["qa_rows"]["archive_objects"] == 1
    assert archived and archived[0][0] == "qa_rows" and len(archived[0][1]) == 2
    # 删除必须按已归档 id 精确执行（无 ORDER BY 的 DELETE..LIMIT 可能删到未归档行）
    delete_sqls = [s for s, _ in conn.executed if s.strip().startswith("DELETE")]
    assert delete_sqls and "IN (11,12)" in " ".join(delete_sqls[0].split())


def test_archive_failure_blocks_delete(monkeypatch, live_db):
    """fail-closed：归档上传失败 → 该作业中止，绝不出现「删了但没归档」。"""
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    monkeypatch.delenv("RAG_RETENTION_ARCHIVE", raising=False)
    conn = _ScriptedConn(affected=2, row_batches=[[(11,)]], row_cols=("id",))
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    monkeypatch.setattr(retention, "_archive_batch",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("oss down")))
    rep = retention.run_retention(commit=True, only=["audit"], batch=5000)
    assert not rep["audit"]["ok"] and "oss down" in rep["audit"]["error"]
    assert conn.acts == 0, "归档失败时绝不执行 DELETE"


def test_archive_batch_refuses_without_oss(monkeypatch):
    """OSS 不可用（simulate/占位凭据）→ raise（配合上一条 = 拒删）；真 bucket 收 gzip JSONL。"""
    import gzip
    import io
    import json as _json

    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket",
                        lambda *a, **k: (None, True))
    with pytest.raises(RuntimeError, match="OSS 不可用"):
        retention._archive_batch("qa_rows", [(1,)], ["id"], 0, "20260705T000000")

    class _Bucket:
        def put_object(self, key, data):
            self.key, self.data = key, data
    b = _Bucket()
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket",
                        lambda *a, **k: (b, False))
    key = retention._archive_batch("audit", [(7, "grant")], ["id", "action"],
                                   3, "20260705T000000")
    assert key == b.key and "kb_audit_log" in key and key.endswith("batch-0003.jsonl.gz")
    line = gzip.GzipFile(fileobj=io.BytesIO(b.data)).read().decode("utf-8").strip()
    assert _json.loads(line) == {"id": 7, "action": "grant"}


def test_findings_deletes_by_ids_with_current_version_guard(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=2, id_batches=[[11, 12]], act_rowcounts=[2])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["findings"], batch=5000)
    assert rep["findings"]["ok"] and rep["findings"]["deleted"] == 2
    sqls = " || ".join(s for s, _ in conn.executed)
    assert "current_version_no" in sqls, "findings 必须带当前版本守卫（现役版本的 finding 永不删）"
    assert "WHERE id IN (11,12)" in sqls, "多表条件删除走 select-PK-then-delete 两步批"
    assert "CONVERT(f.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci" in sqls, (
        "doc_id JOIN 必须 collation-cast——document_sensitive_finding 是 _0900_ai_ci、"
        "document_meta 是 _unicode_ci，裸 JOIN 生产实测 1267（2026-07-02）")


def test_window_zero_disables_job(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_AUDIT_MONTHS", "0")
    conn = _ScriptedConn(affected=999)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(only=["audit"])
    assert rep["audit"]["ok"] and "window<=0" in rep["audit"]["skipped"]
    assert not conn.executed, "停用作业连 COUNT 都不应执行"


def test_qa_blobs_uses_update_null_not_delete(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=1, act_rowcounts=[1])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    retention.run_retention(commit=True, only=["qa_blobs"], batch=5000)
    acts = [s for s, _ in conn.executed if s.strip().startswith("UPDATE")]
    assert acts and "SET content_blocks_json = NULL" in acts[0]
    assert not any(s.strip().startswith("DELETE") for s, _ in conn.executed), \
        "qa_blobs 是瘦身（置 NULL），绝不是删行"


def test_main_exit_codes(monkeypatch, live_db):
    # blocked → 2
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=100, rollup_state=(0, None))
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    assert retention.main(["--commit", "--only", "qa_rows"]) == 2
    # ok → 0
    conn2 = _ScriptedConn(affected=0)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn2)
    assert retention.main(["--only", "audit"]) == 0
    # error → 3
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    assert retention.main(["--only", "audit"]) == 3


# ── agent 表族（schema/022–025；深度审查 2026-07-09 治理组）──────────────────


_AGENT_JOBS = ("agent_checkpoints", "agent_steps", "tool_invocations", "llm_calls",
               "agent_audit", "approval_decisions", "approval_requests", "agent_runs")


def test_agent_jobs_registered_with_windows():
    """agent 组作业全部登记：_JOB_NAMES / 窗口 / SQL 生成不抛，count 与 act 同谓词形态。"""
    windows = retention._retention_windows()
    for job in _AGENT_JOBS:
        assert job in retention._JOB_NAMES
        assert windows[job] > 0
        sqls = retention._job_sqls(job)
        assert sqls["count"].lstrip().startswith("SELECT COUNT(*)")
        assert ("act" in sqls) or ("select_ids" in sqls)
    # 顺序：子表在前、agent_run 殿后
    names = list(retention._JOB_NAMES)
    assert names.index("agent_checkpoints") < names.index("agent_runs")
    assert names.index("llm_calls") < names.index("agent_runs")


def test_agent_jobs_are_optional_1146_skip(monkeypatch, live_db):
    """agent 表未建的环境（可选迁移）：1146 → skip 不算失败。"""
    class _NoTableConn:
        def cursor(self):
            raise Exception(1146, "Table 'fuling_operation.agent_step' doesn't exist")
        def rollback(self):
            pass
        def close(self):
            pass
    # pymysql err 形态是 e.args[0]==1146
    class _Err(Exception):
        pass
    def _boom(*a, **k):
        e = _Err("no table")
        e.args = (1146, "Table doesn't exist")
        raise e
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    rep = retention.run_retention(only=["agent_steps"])
    assert rep["agent_steps"]["ok"] and "可选迁移" in rep["agent_steps"]["skipped"]
    # 非可选作业的 1146 仍是事故
    rep2 = retention.run_retention(only=["audit"])
    assert not rep2["audit"]["ok"]


def test_fmt_id_str_pk_quotes_and_rejects_injection():
    assert retention._fmt_id(42) == "42"
    assert retention._fmt_id("a3f9", str_pk=True) == "'a3f9'"
    with pytest.raises(RuntimeError, match="拒绝拼接"):
        retention._fmt_id("x'; DROP TABLE --", str_pk=True)


def test_agent_checkpoints_two_step_delete_with_string_ids(monkeypatch, live_db):
    """checkpoint 两步批（select-PK-then-delete）：uuid hex 主键加引号拼进 DELETE。"""
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=2, id_batches=[["aa11", "bb22"]], act_rowcounts=[2])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["agent_checkpoints"], batch=5000)
    assert rep["agent_checkpoints"]["ok"] and rep["agent_checkpoints"]["deleted"] == 2
    dels = [s for s, _ in conn.executed if s.strip().startswith("DELETE")]
    assert dels and "checkpoint_id IN ('aa11','bb22')" in dels[0]
    # 终态守卫 + 孤儿兼收：谓词必须含 LEFT JOIN 与 status 终态集
    sel = [s for s, _ in conn.executed if "SELECT c.checkpoint_id" in s][0]
    assert "LEFT JOIN" in sel and "'succeeded'" in sel and "r.run_id IS NULL" in sel


def test_agent_audit_archives_before_delete_by_audit_id(monkeypatch, live_db):
    """agent_audit 与 kb_audit_log 同纪律：先归档再按 audit_id（字符串 PK）删。"""
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    monkeypatch.delenv("RAG_RETENTION_ARCHIVE", raising=False)   # 默认即开
    conn = _ScriptedConn(affected=2,
                         row_batches=[[("aud1", "tool_call"), ("aud2", "approval_decision")]],
                         row_cols=("audit_id", "event_type"),
                         act_rowcounts=[2])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    archived = []
    monkeypatch.setattr(retention, "_archive_batch",
                        lambda job, rows, cols, b, ts: archived.append((job, len(rows))) or "k")
    rep = retention.run_retention(commit=True, only=["agent_audit"], batch=5000)
    assert rep["agent_audit"]["ok"] and rep["agent_audit"]["deleted"] == 2
    assert archived == [("agent_audit", 2)], "删前必归档"
    dels = [s for s, _ in conn.executed if s.strip().startswith("DELETE")]
    assert dels and "audit_id IN ('aud1','aud2')" in dels[0]


def test_approval_requests_never_delete_pending():
    sqls = retention._job_sqls("approval_requests")
    assert "status <> 'pending'" in sqls["count"] and "status <> 'pending'" in sqls["act"]


def test_agent_runs_only_terminal():
    sqls = retention._job_sqls("agent_runs")
    for kw in ("'succeeded'", "'failed'", "'cancelled'", "'expired'"):
        assert kw in sqls["act"]
    assert "ended_at" in sqls["act"]


def test_purge_jobs_cover_agent_tables_children_first():
    """主体擦除覆盖 agent 表族：子表先删、agent_run 殿后且先于 qa 组无碍；
    审计/审批链（agent_audit_log / approval_*）刻意不在擦除范围（法定义务豁免）。"""
    jobs = retention._purge_jobs("u-x")
    tables = [j["table"] for j in jobs]
    for t in ("agent_checkpoint", "agent_step", "tool_invocation", "llm_call_log", "agent_run"):
        assert t in tables, f"purge 缺 {t}"
        assert jobs[tables.index(t)]["optional"] is True
    assert tables.index("agent_checkpoint") < tables.index("agent_run")
    assert tables.index("agent_step") < tables.index("agent_run")
    assert tables.index("tool_invocation") < tables.index("agent_run")
    assert "agent_audit_log" not in tables and "approval_request" not in tables
    # 子表经 run_id 关联删（无 user_id 列）
    ck = jobs[tables.index("agent_checkpoint")]
    assert "run_id IN" in ck["act"] and "agent_run WHERE user_id" in ck["act"]
    # qa 链原有顺序不被破坏（qa_retrieved_doc 仍先于 qa_session_log）
    assert tables.index("qa_retrieved_doc") < tables.index("qa_session_log")


# ── PR-H：本体证据擦除作业（P1「数据与证据无保留策略」/PIPL）──────────────────


def test_ontology_retention_jobs_registered():
    from opensearch_pipeline import retention
    assert "ontology_case_evidence" in retention._JOB_NAMES
    assert "ontology_candidate_features" in retention._JOB_NAMES
    # 可选迁移：无 027/028 的环境 1146 → skip 不算失败
    assert "ontology_case_evidence" in retention._OPTIONAL_JOBS
    assert "ontology_candidate_features" in retention._OPTIONAL_JOBS
    w = retention._retention_windows()
    assert w["ontology_case_evidence"] == 6 and w["ontology_candidate_features"] == 6


def test_ontology_retention_sqls_scrub_not_delete():
    """擦除语义：行留审计骨架，只 NULL 证据 blob；open case 永不在谓词内。"""
    from opensearch_pipeline import retention
    ev = retention._job_sqls("ontology_case_evidence")
    assert "UPDATE" in ev["act"] and "evidence_json = NULL" in ev["act"]
    assert "DELETE" not in ev["act"]
    assert "status <> 'open'" in ev["count"]
    ft = retention._job_sqls("ontology_candidate_features")
    assert "UPDATE" in ft["act_by_ids"] and "features_json = NULL" in ft["act_by_ids"]
    assert ft.get("pk_str") is True
    assert "status <> 'open'" in ft["count"]
