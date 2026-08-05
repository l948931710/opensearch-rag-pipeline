# -*- coding: utf-8 -*-
"""retention.purge_subject（P2-5 数据主体擦除，PIPL 15/47）单测。

不变量：simulate skip；空 user_id 拒绝；dry-run 只数不删；commit 双门
（RAG_SUBJECT_PURGE_ENABLE + env_guard）；删除顺序 qa_retrieved_doc 先于 qa_session_log
（事实表无 user_id、经 message_id 关联，反序即孤儿）；可选表 1146 → skip 不算失败。
"""
import pytest

from opensearch_pipeline import retention
from opensearch_pipeline.config import get_config


class _Cur:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self._conn.raise_1146_on and self._conn.raise_1146_on in sql:
            raise Exception(1146, "Table doesn't exist")
        self._conn.executed.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("SELECT COUNT(*)"):
            self._row = (self._conn.affected,)
        elif s.startswith("DELETE"):
            self.rowcount = (self._conn.act_rowcounts.pop(0)
                             if self._conn.act_rowcounts else 0)
            self._conn.acts += 1

    def fetchone(self):
        return getattr(self, "_row", None)


class _Conn:
    def __init__(self, *, affected=0, act_rowcounts=None, raise_1146_on=None):
        self.affected = affected
        self.act_rowcounts = list(act_rowcounts or [])
        self.raise_1146_on = raise_1146_on
        self.executed = []
        self.acts = 0
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def live_db(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    return cfg


class _FakeOssPage:
    def __init__(self, objs):
        self.object_list = objs
        self.is_truncated = False
        self.next_marker = ""


class _FakeOssObj:
    def __init__(self, key):
        self.key = key


class _FakeBucket:
    """极简 OSS 桩：key → gzip-JSONL 字节。"""
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts, self.deletes = {}, []

    def list_objects(self, prefix="", marker="", max_keys=1000):
        return _FakeOssPage([_FakeOssObj(k) for k in sorted(self.objects) if k.startswith(prefix)])

    def get_object(self, key):
        data = self.objects[key]

        class _R:
            def read(self_inner):
                return data
        return _R()

    def put_object(self, key, data, headers=None):
        self.puts[key] = data
        self.objects[key] = data

    def delete_object(self, key):
        self.deletes.append(key)
        self.objects.pop(key, None)


def _gz_jsonl(rows):
    import gzip
    import io
    import json
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for r in rows:
            gz.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
    return buf.getvalue()


@pytest.fixture
def oss_archive(monkeypatch):
    """C5=方案A 后：purge_subject 必然要查冷归档面。默认给一个**空桶**。

    ⚠️ 不给桶不是"跳过"——是 raise（fail-closed）：查不到归档就不许声称擦除完成。
    要测归档清理的用例覆盖本 fixture 的返回值即可。
    """
    bkt = _FakeBucket()
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (bkt, False))
    return bkt


def test_simulate_mode_skips():
    rep = retention.purge_subject("u1")
    assert rep.get("skipped") == "simulate" and rep["ok"]


def test_empty_user_id_rejected():
    with pytest.raises(ValueError, match="user_id"):
        retention.purge_subject("   ")


def test_dry_run_counts_without_deleting(monkeypatch, live_db, oss_archive):
    conn = _Conn(affected=42)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1")
    assert rep["ok"] and rep["dry_run"]
    # C5=方案A 后多出 `oss_archive` 一面：RDS 行删干净 ≠ 主体数据清干净，
    # qa_session_log 的到期行早已 gzip-JSONL 落进 OSS，报告必须**分面如实**。
    # doc_update_notice（schema/065，2026-08-04）：升版提醒投递行按 user_id 键控，
    # 物化了"此人当时可见哪些文档"，必须随主体擦除一起删（对应的 doc_update_event
    # 不含个人标识、且是防重放台账，**刻意不在本清单**）。
    assert set(rep["tables"]) == {"qa_retrieved_doc", "user_feedback", "doc_update_notice",
                                  "escalation_ticket", "qa_conversation", "qa_session_log",
                                  "oss_archive"}
    # RDS 五张表同源（桩 conn 恒回 42）；归档面是**另一层数据源**（本例空桶=0），
    # 不能把它硬凑成 42——那是编数字。分面断言。
    _rds = {k: v for k, v in rep["tables"].items() if k != "oss_archive"}
    assert all(t["affected"] == 42 and t["dry_run"] for t in _rds.values())
    _arch = rep["tables"]["oss_archive"]
    assert _arch["ok"] and _arch["dry_run"] and _arch["affected"] == 0
    assert conn.acts == 0 and conn.commits == 0, "dry-run 绝不 DELETE、绝不 commit"


def test_commit_requires_enable_flag(monkeypatch, live_db):
    monkeypatch.delenv("RAG_SUBJECT_PURGE_ENABLE", raising=False)
    with pytest.raises(RuntimeError, match="RAG_SUBJECT_PURGE_ENABLE"):
        retention.purge_subject("u1", commit=True)


def test_commit_deletes_fact_rows_before_session_log(monkeypatch, live_db, oss_archive):
    monkeypatch.setenv("RAG_SUBJECT_PURGE_ENABLE", "true")
    # act_rowcounts 按 _purge_jobs 顺序逐个 pop：
    # qa_retrieved_doc / user_feedback / doc_update_notice / escalation_ticket /
    # qa_conversation / qa_session_log
    conn = _Conn(affected=3, act_rowcounts=[3, 0, 0, 0, 0, 3])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1", commit=True, batch=100)
    assert rep["ok"]
    assert rep["tables"]["qa_retrieved_doc"]["deleted"] == 3
    assert rep["tables"]["qa_session_log"]["deleted"] == 3
    deletes = [s for s, _ in conn.executed if s.strip().startswith("DELETE")]
    assert len(deletes) == 6
    # ⚠️ 顺序不可倒：事实表（经 message_id 关联）必须先于 qa_session_log 本体
    idx_fact = next(i for i, s in enumerate(deletes) if "qa_retrieved_doc" in s)
    idx_log = next(i for i, s in enumerate(deletes)
                   if "qa_session_log" in s and "qa_retrieved_doc" not in s)
    assert idx_fact < idx_log
    # 事实表删除经 qa_session_log.message_id 关联（表本身无 user_id 列）
    assert "message_id IN" in deletes[idx_fact]


def test_optional_table_1146_is_skipped_not_failed(monkeypatch, live_db, oss_archive):
    monkeypatch.setenv("RAG_SUBJECT_PURGE_ENABLE", "true")
    conn = _Conn(affected=1, act_rowcounts=[1, 1, 1, 1],
                 raise_1146_on="qa_retrieved_doc")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1", commit=True, batch=100)
    assert rep["ok"], "可选迁移（schema/013）未 apply 不算失败"
    assert "不存在" in rep["tables"]["qa_retrieved_doc"]["skipped"]
    assert rep["tables"]["qa_session_log"]["ok"]


def test_non_optional_table_error_fails_report(monkeypatch, live_db, oss_archive):
    monkeypatch.setenv("RAG_SUBJECT_PURGE_ENABLE", "true")
    conn = _Conn(affected=1, act_rowcounts=[1, 1, 1, 1],
                 raise_1146_on="user_feedback")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1", commit=True, batch=100)
    assert not rep["ok"], "基础表缺失=部署错库，必须按事故上报"
    assert rep["tables"]["user_feedback"].get("error")


def test_cli_purge_user_dry_run(monkeypatch, live_db, capsys, oss_archive):
    conn = _Conn(affected=7)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    assert retention.main(["--purge-user", "u1"]) == 0
    assert conn.acts == 0
    assert "dry-run" in capsys.readouterr().out


def test_cli_purge_user_commit_without_enable_exits_3(monkeypatch, live_db):
    monkeypatch.delenv("RAG_SUBJECT_PURGE_ENABLE", raising=False)
    assert retention.main(["--purge-user", "u1", "--commit"]) == 3


# ── C5=方案A 的必然后果：主体擦除必须覆盖 OSS 冷归档面（PIPL 被删除权）────────────

_PFX = "archive/retention/qa_session_log/"


def test_archive_rows_of_subject_are_removed_others_kept(monkeypatch, live_db):
    """只抹该主体的行，同一对象里别人的行必须原样保留。"""
    import gzip
    import io
    import json
    bkt = _FakeBucket({_PFX + "R1/batch-0000.jsonl.gz": _gz_jsonl(
        [{"id": 1, "user_id": "u1", "q": "a"},
         {"id": 2, "user_id": "u2", "q": "b"},
         {"id": 3, "user_id": "u1", "q": "c"}])})
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (bkt, False))
    rep = retention._purge_archives_for_subject("u1", commit=True)
    assert rep["ok"] and rep["rows_removed"] == 2 and rep["objects_rewritten"] == 1
    key = _PFX + "R1/batch-0000.jsonl.gz"
    with gzip.GzipFile(fileobj=io.BytesIO(bkt.puts[key]), mode="rb") as gz:
        left = [json.loads(x) for x in gz.read().decode("utf-8").splitlines() if x.strip()]
    assert [r["user_id"] for r in left] == ["u2"], "他人数据被误删"


def test_archive_object_fully_owned_by_subject_is_deleted(monkeypatch, live_db):
    """整对象都属该主体 ⇒ 删对象，不留空 gz。"""
    bkt = _FakeBucket({_PFX + "R1/b.jsonl.gz": _gz_jsonl([{"id": 1, "user_id": "u1"}])})
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (bkt, False))
    rep = retention._purge_archives_for_subject("u1", commit=True)
    assert rep["objects_deleted"] == 1 and rep["objects_rewritten"] == 0
    assert bkt.deletes == [_PFX + "R1/b.jsonl.gz"]


def test_archive_dry_run_counts_but_never_writes(monkeypatch, live_db):
    bkt = _FakeBucket({_PFX + "R1/b.jsonl.gz": _gz_jsonl([{"id": 1, "user_id": "u1"}])})
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (bkt, False))
    rep = retention._purge_archives_for_subject("u1", commit=False)
    assert rep["rows_removed"] == 1 and rep["dry_run"]
    assert bkt.puts == {} and bkt.deletes == [], "dry-run 绝不写不删"


def test_archive_unavailable_fails_closed_not_silently_ok(monkeypatch, live_db):
    """OSS 不可达 ⇒ 整体判否。**绝不**「查不到就当没有」——那正是把「归档里还留着」
    谎报成「已擦除」的路径。"""
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (None, True))
    conn = _Conn(affected=0)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1")
    assert rep["ok"] is False, "OSS 不可达却报了擦除成功"
    assert "error" in rep["tables"]["oss_archive"]


def test_archive_scope_matches_rds_scope_only(monkeypatch, live_db):
    """覆盖面必须与 RDS 侧逐字一致：只清 qa_session_log 归档，**不动** kb_audit_log
    （操作者审计，不在 _purge_jobs 范围内）。两侧口径不一致就是新缺口。"""
    bkt = _FakeBucket({
        _PFX + "R1/b.jsonl.gz": _gz_jsonl([{"id": 1, "user_id": "u1"}]),
        "archive/retention/kb_audit_log/R1/b.jsonl.gz": _gz_jsonl([{"id": 9, "user_id": "u1"}]),
    })
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (bkt, False))
    rep = retention._purge_archives_for_subject("u1", commit=True)
    assert rep["objects_scanned"] == 1, "扫描面超出了 qa_session_log"
    assert "archive/retention/kb_audit_log/R1/b.jsonl.gz" in bkt.objects


def test_archive_malformed_line_is_kept_not_dropped(monkeypatch, live_db):
    """坏行（非 JSON）原样保留——解析失败绝不当成"可以丢"。"""
    import gzip
    import io
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(b'{"id":1,"user_id":"u1"}\n not-json \n{"id":2,"user_id":"u2"}\n')
    bkt = _FakeBucket({_PFX + "R1/b.jsonl.gz": buf.getvalue()})
    monkeypatch.setattr("opensearch_pipeline.clients._get_oss_bucket", lambda *a, **k: (bkt, False))
    rep = retention._purge_archives_for_subject("u1", commit=True)
    assert rep["rows_removed"] == 1
    with gzip.GzipFile(fileobj=io.BytesIO(bkt.puts[_PFX + "R1/b.jsonl.gz"]), mode="rb") as gz:
        left = [x for x in gz.read().decode("utf-8").splitlines() if x.strip()]
    assert any("not-json" in x for x in left), "坏行被静默丢弃"
