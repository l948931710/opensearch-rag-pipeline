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
        elif s.startswith("SELECT f.id"):
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


@pytest.fixture
def oss_available(monkeypatch):
    """C5 preflight 起效后：真正测「归档」的用例必须声明 OSS 可用。

    preflight 里是 `from opensearch_pipeline.clients import _get_oss_bucket`（函数内惰性
    import），所以要打 clients 模块上的名字。
    """
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket",
                        lambda *a, **k: (object(), False))


@pytest.fixture
def no_archive(monkeypatch):
    """不关心归档的用例：显式选 RAG_RETENTION_ARCHIVE=false。

    ⚠️ 这是 C5 拍板单写死的**唯一**非生产出口——不设「跳过 preflight 但保留 archive=true」
    的通用旁路，否则 preflight 形同虚设。测试也走同一条路，不开后门。
    """
    monkeypatch.setenv("RAG_RETENTION_ARCHIVE", "false")


def test_simulate_mode_skips():
    rep = retention.run_retention()
    assert all(r.get("skipped") == "simulate" for r in rep.values())


def test_dry_run_counts_without_acting(monkeypatch, live_db, no_archive):
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


def test_qa_rows_blocked_when_rollup_empty(monkeypatch, live_db, no_archive):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=100, rollup_state=(0, None))
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["qa_rows"])
    assert rep["qa_rows"].get("blocked") and not rep["qa_rows"]["ok"]
    assert conn.acts == 0, "rollup 从未跑过时绝不删原始 qa 行"


def test_qa_rows_blocked_when_rollup_stale(monkeypatch, live_db, no_archive):
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


def test_archive_before_delete_uploads_then_deletes_by_id(monkeypatch, live_db, oss_available):
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


def test_archive_failure_blocks_delete(monkeypatch, live_db, oss_available):
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


def test_main_exit_codes(monkeypatch, live_db, no_archive):
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


# ── C6（2026-08-03）：主体擦除的锚点依赖门 ───────────────────────────────────
class _PurgeConn(_ScriptedConn):
    """按表名脚本化 purge_subject：可让指定表在 DELETE 时抛错，或恒返回满批（capped）。"""

    def __init__(self, *, counts, fail_on=None, always_full=(), batch=1000):
        super().__init__()
        self.counts = dict(counts)
        self.fail_on = dict(fail_on or {})
        self.always_full = set(always_full)
        self.batch = batch
        self.deleted_tables = []

    def cursor(self):
        return _PurgeCursor(self)


class _PurgeCursor:
    def __init__(self, conn):
        self._c = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @staticmethod
    def _table_of(sql):
        for t in ("qa_retrieved_doc", "user_feedback", "escalation_ticket",
                  "qa_conversation", "qa_session_log"):
            if t in sql:
                return t
        return ""

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        # 子查询也含 qa_session_log —— DELETE 目标表恒在 "DELETE FROM <db>.<table>" 处
        t = (self._table_of(s.split("WHERE")[0]) if s.startswith(("DELETE", "SELECT COUNT"))
             else self._table_of(s))
        if s.startswith("DELETE"):
            if t in self._c.fail_on:
                raise Exception(self._c.fail_on[t], f"simulated {t} failure")
            self._c.deleted_tables.append(t)
            self.rowcount = self._c.batch if t in self._c.always_full else 1
        elif s.startswith("SELECT COUNT"):
            self._row = (self._c.counts.get(t, 0),)
        return None

    def fetchone(self):
        return getattr(self, "_row", None)

    def fetchall(self):
        return []


class _EmptyOssPage:
    object_list: list = []
    is_truncated = False
    next_marker = ""


class _EmptyBucket:
    """空归档桶：本文件的 purge 用例测的是 **RDS 锚点顺序**，不是归档面。"""
    def list_objects(self, **kw):
        return _EmptyOssPage()


def _purge(monkeypatch, live_db, conn, **kw):
    monkeypatch.setenv("RAG_SUBJECT_PURGE_ENABLE", "true")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    # C5=方案A 起，purge_subject 必查冷归档面；不给桶 = fail-closed raise（见
    # _purge_archives_for_subject）。这里给空桶，让本文件的用例专注 RDS 锚点纪律。
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket",
                        lambda *a, **k: (_EmptyBucket(), False))
    monkeypatch.setattr("opensearch_pipeline.env_guard.assert_destructive_write_allowed",
                        lambda *a, **k: None)
    return retention.purge_subject("U1", commit=True, **kw)


def test_purge_blocks_anchor_when_fact_table_errors(monkeypatch, live_db):
    """★ C6：qa_retrieved_doc 撞锁失败时，绝不能继续删 qa_session_log 锚点。

    锚点是残余事实行【唯一】的定位键（该表无 user_id 列）。先删日志 ⇒ 残行永久孤儿，
    次日重跑 count 子查询返 0、报"全表 ok" ⇒ PIPL 擦除被报告为完成而实际未完成。
    """
    conn = _PurgeConn(counts={"qa_retrieved_doc": 5, "qa_session_log": 3},
                      fail_on={"qa_retrieved_doc": 1205})   # 锁等待超时
    out = _purge(monkeypatch, live_db, conn)
    assert "qa_session_log" not in conn.deleted_tables, "锚点被删 —— 制造永久不可定位的孤儿行"
    assert out["tables"]["qa_session_log"].get("blocked_by") == ["qa_retrieved_doc(error)"]
    assert out["ok"] is False


def test_purge_blocks_anchor_when_fact_table_capped(monkeypatch, live_db):
    """★ C6：打满 max_batches（还有残行没删完）同样必须阻断锚点删除。"""
    conn = _PurgeConn(counts={"qa_retrieved_doc": 10_000, "qa_session_log": 3},
                      always_full={"qa_retrieved_doc"}, batch=2)
    out = _purge(monkeypatch, live_db, conn, batch=2, max_batches=2)
    assert out["tables"]["qa_retrieved_doc"].get("capped") is True
    assert "qa_session_log" not in conn.deleted_tables
    assert out["tables"]["qa_session_log"].get("blocked_by") == ["qa_retrieved_doc(capped)"]
    assert out["ok"] is False


def test_purge_missing_optional_table_does_not_block_anchor(monkeypatch, live_db):
    """1146（可选迁移未 apply）是【合法 skip】不是未清：表都不存在，不可能有孤儿 ⇒ 不得阻断。"""
    conn = _PurgeConn(counts={"qa_session_log": 3}, fail_on={"qa_retrieved_doc": 1146})
    out = _purge(monkeypatch, live_db, conn)
    assert "qa_session_log" in conn.deleted_tables, "合法 skip 被误判为未清 —— 擦除被无谓阻断"
    assert out["tables"]["qa_retrieved_doc"]["ok"] is True
    assert "blocked_by" not in out["tables"]["qa_session_log"]


def test_purge_happy_path_deletes_anchor_last(monkeypatch, live_db):
    """全部前序成功时，锚点照常删除，且顺序恒为最后一项。"""
    conn = _PurgeConn(counts={"qa_retrieved_doc": 2, "qa_session_log": 3})
    out = _purge(monkeypatch, live_db, conn)
    assert conn.deleted_tables[0] == "qa_retrieved_doc"
    assert conn.deleted_tables[-1] == "qa_session_log"
    assert out["ok"] is True


# ── C5：归档 preflight + 碰撞不可行的 run id（Sam 2026-08-03 拍板 方案 A）────────

def test_preflight_fails_fast_when_archive_on_but_oss_unavailable(monkeypatch, live_db):
    """archive=true + 含归档作业 + OSS 不可用 ⇒ **启动即失败**。

    此前要等阶段 2 真删的第一批才在 _archive_batch 里爆，且每天重复失败。
    """
    monkeypatch.setenv("RAG_RETENTION_ARCHIVE", "true")
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (None, True))
    with pytest.raises(RuntimeError, match="preflight"):
        retention.run_retention(only=["qa_rows"])


def test_preflight_ignores_non_archive_jobs(monkeypatch, live_db):
    """只跑非归档作业（6 个里只有 qa_rows/audit 归档）⇒ 缺 OSS **不得**失败。"""
    monkeypatch.setenv("RAG_RETENTION_ARCHIVE", "true")
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (None, True))
    conn = _ScriptedConn(affected=0)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(only=["findings"])       # 非归档作业
    assert rep["findings"]["ok"]


def test_preflight_requires_positive_window(monkeypatch, live_db):
    """🔴 codex BLOCKER：`months <= 0` 是**合法 skip**，不得因缺 OSS 打成 FATAL。

    漏掉 window 条件的话，`qa_rows window=0 + 无 OSS` 会从应有的 SKIPPED 变成 preflight
    RuntimeError —— 与 SKIPPED 的定义直接矛盾。
    """
    monkeypatch.setenv("RAG_RETENTION_ARCHIVE", "true")
    monkeypatch.setenv("RAG_RETENTION_QA_MONTHS", "0")     # 合法 skip
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (None, True))
    conn = _ScriptedConn(affected=0)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(only=["qa_rows"])         # 不得抛
    assert "qa_rows" in rep


def test_preflight_is_after_simulate_shortcircuit():
    """位置纪律：simulate 仍是纯跳过，preflight 不得把它变成失败。"""
    rep = retention.run_retention(only=["qa_rows"])         # 默认 simulate
    assert rep["qa_rows"]["skipped"] == "simulate"


def test_archive_run_id_is_collision_proof():
    """主修复：run id 不再是秒级时间戳——同秒两次必须不同（并发/重入不撞 key）。"""
    a, b = retention._new_archive_run_id(), retention._new_archive_run_id()
    assert a != b, "同秒两次生成了相同 run id ⇒ 并发归档会互相覆盖"
    assert len(a) > len("20260803T100000"), "应带唯一后缀，不只是时间戳"


def test_archive_put_forbids_overwrite(monkeypatch, live_db):
    """纵深防御：写归档对象带 x-oss-forbid-overwrite（撞 key 宁可 raise 也不静默顶掉）。"""
    seen = {}

    class _Bkt:
        def put_object(self, key, data, headers=None):
            seen["key"] = key
            seen["headers"] = headers or {}

    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (_Bkt(), False))
    retention._archive_batch("qa_rows", [{"id": 1}], ["id"], 0, "RUNID")
    assert seen["headers"].get("x-oss-forbid-overwrite") == "true"
    assert "RUNID" in seen["key"]
