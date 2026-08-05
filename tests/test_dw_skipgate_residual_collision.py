# -*- coding: utf-8 -*-
"""DW 侧 skip-gate 残留行撞号+收养防线(2026-08-04,同族 console 修复=901433b)。

缺陷链:skip-gate(RAG_SKIP_UNCHANGED_REINGEST)命中同正文重传时回退
document_meta.current_version_no 但保留 SKIPPED_DUPLICATE 的 document_version 行
(旧+1)。此后 node_scan_raw_files 按 current+1 取号与残留行同号;
node_register_metadata 旧写法先 UPDATE 后 INSERT,UPDATE 命中残留行只刷
file_ext/status——新文件被"收养"进残留行:raw_key 仍指旧件,且
SKIPPED_DUPLICATE 不在 stage-1 扫描(NOT_STARTED)/认领(NOT_STARTED/LOADING/
FAILED)谓词内,内容永不入索引。

防线两层(纯 fake-cursor,不碰真库,无需进 conftest 串行组):
  · scanner 取号纳 dv 侧最大号(GREATEST 单条 SQL,失败面与原单查询一致);
  · register UPDATE 谓词带 raw_key 身份(<=> NULL-safe),rowcount==0 回读三态:
    无行→INSERT / 同 raw_key→幂等 no-op(pymysql changed-rows 同秒假 miss)/
    异 raw_key→fail-loud 整批回滚。
"""
import hashlib

import pytest

import opensearch_pipeline.env_guard
import opensearch_pipeline.pipeline_nodes as pn


class _Cur:
    """脚本化 fake cursor:按 SQL 前缀路由返回值,并记录 (归一化SQL, params)。"""

    def __init__(self, script):
        self.script = script
        self.executed = []
        self.rowcount = 1
        self._next = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.executed.append((s, tuple(params or ())))
        self._next = None
        self.rowcount = 1
        if s.startswith("SELECT dm.doc_id, GREATEST"):
            self._next = self.script.get("meta_row")
        elif s.startswith("UPDATE document_version"):
            self.rowcount = self.script.get("dv_update_rowcount", 0)
        elif s.startswith("SELECT raw_key FROM document_version"):
            self._next = self.script.get("dv_row")

    def fetchone(self):
        r = self._next
        self._next = None
        return r

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _sqls(cur, prefix):
    return [(s, p) for s, p in cur.executed if s.startswith(prefix)]


# ── scanner 取号:GREATEST(current, MAX(dv)) + 1 ──────────────────────────────
def test_scanner_take_number_includes_dv_max(monkeypatch):
    """残留行复现:current 被 skip-gate 回退到 1、dv 侧残留 v2 → 取号必须=3。
    fake 回传 GREATEST 结果 2(模拟 MySQL 端计算),并钉住 SQL 形状——回归到
    只查 current_version_no 的裸取号时,SQL 形状断言先红。"""
    cur = _Cur({"meta_row": ("DOC_HR_20260101_ab12cd34", 2)})
    conn = _Conn(cur)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **k: conn)
    ctx = {"raw_tasks": [{"bucket_name": "b",
                          "raw_key": "raw/hr/D1/U1/同名重传.pdf"}],
           "simulate_db": False}
    pn.node_scan_raw_files(ctx)
    task = ctx["tasks"][0]
    assert task["doc_id"] == "DOC_HR_20260101_ab12cd34"
    assert task["version_no"] == 3
    sql, params = _sqls(cur, "SELECT dm.doc_id, GREATEST")[0]
    assert "MAX(dv.version_no)" in sql and "document_version" in sql
    assert "COALESCE" in sql            # NULL 免疫:GREATEST(NULL,x)=NULL 陷阱
    assert params == ("同名重传.pdf", "hr")


def test_scanner_explicit_version_not_overridden(monkeypatch):
    """任务自带 version_no 时取号分支不改号(现状语义回归钉)。"""
    cur = _Cur({"meta_row": ("DOC_HR_X", 5)})
    conn = _Conn(cur)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **k: conn)
    ctx = {"raw_tasks": [{"bucket_name": "b", "version_no": 2,
                          "raw_key": "raw/hr/D1/U1/a.pdf"}],
           "simulate_db": False}
    pn.node_scan_raw_files(ctx)
    assert ctx["tasks"][0]["version_no"] == 2


# ── register:raw_key 身份守卫 + rowcount==0 三态 ─────────────────────────────
_TASK = {"doc_id": "D1", "version_no": 2, "filename": "同名.pdf", "dept": "hr",
         "file_ext": "pdf", "bucket_name": "b", "raw_key": "raw/hr/D1/v2/new.pdf"}


def _run_register(monkeypatch, script, task=None):
    cur = _Cur(script)
    conn = _Conn(cur)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **k: conn)
    monkeypatch.setattr(opensearch_pipeline.env_guard,
                        "assert_destructive_write_allowed", lambda *a, **k: None)
    ctx = {"tasks": [dict(_TASK, **(task or {}))], "simulate_db": False}
    pn.node_register_metadata(ctx)
    return cur, conn


def test_register_update_predicate_carries_raw_key_identity(monkeypatch):
    """常态路径:UPDATE 命中(rowcount=1)→ 不回读、不 INSERT;谓词带 <=> 身份。"""
    cur, conn = _run_register(monkeypatch, {"dv_update_rowcount": 1})
    sql, params = _sqls(cur, "UPDATE document_version")[0]
    assert "raw_key <=> %s" in sql
    assert params[-1] == "raw/hr/D1/v2/new.pdf"
    assert not _sqls(cur, "SELECT raw_key FROM document_version")
    assert not _sqls(cur, "INSERT INTO document_version")
    assert conn.committed


def test_register_refuses_adoption_of_residual_row(monkeypatch):
    """撞号收养禁令:同号行 raw_key 指旧件 → fail-loud 整批回滚,绝不 INSERT/收养。
    修复前旧写法在此静默 UPDATE 残留行(只刷 file_ext/status),新文件从此失踪。"""
    with pytest.raises(RuntimeError, match="Database write failure in node_register_metadata"):
        _run_register(monkeypatch, {"dv_update_rowcount": 0,
                                    "dv_row": ("raw/hr/D1/v2/OLD.pdf",)})


def test_register_adoption_refusal_no_insert_and_rolls_back(monkeypatch):
    cur = _Cur({"dv_update_rowcount": 0, "dv_row": ("raw/hr/D1/v2/OLD.pdf",)})
    conn = _Conn(cur)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **k: conn)
    monkeypatch.setattr(opensearch_pipeline.env_guard,
                        "assert_destructive_write_allowed", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="撞号拒收养"):
        pn.node_register_metadata({"tasks": [dict(_TASK)], "simulate_db": False})
    assert not _sqls(cur, "INSERT INTO document_version")
    assert conn.rolled_back and not conn.committed


def test_register_same_second_rerun_idempotent_hit(monkeypatch):
    """pymysql changed-rows footgun:同秒重跑值全同 → UPDATE 报 0,但同身份行已在
    → 幂等 no-op。修复前旧写法在此直接 INSERT → 1062 撞 uk_doc_version 整批炸。"""
    cur, conn = _run_register(monkeypatch, {"dv_update_rowcount": 0,
                                            "dv_row": ("raw/hr/D1/v2/new.pdf",)})
    assert not _sqls(cur, "INSERT INTO document_version")
    assert conn.committed


def test_register_new_row_insert_path_unchanged(monkeypatch):
    """无行 → INSERT 全字段(现行为回归钉):raw_key/raw_key_hash 成对落盘。"""
    cur, conn = _run_register(monkeypatch, {"dv_update_rowcount": 0, "dv_row": None})
    (sql, params), = _sqls(cur, "INSERT INTO document_version")
    assert params[3] == "raw/hr/D1/v2/new.pdf"
    assert params[4] == hashlib.sha256(b"raw/hr/D1/v2/new.pdf").hexdigest()
    assert "'NOT_STARTED'" in sql       # 新行必须落在 stage-1 认领谓词内
    assert conn.committed


def test_register_null_raw_key_identity_matches(monkeypatch):
    """NULL 身份对称:任务 raw_key=None ↔ 行 raw_key=NULL 判同身份(幂等),
    不误判撞号——对应 SQL 侧 <=> 的 NULL-safe 语义。"""
    cur, conn = _run_register(monkeypatch,
                              {"dv_update_rowcount": 0, "dv_row": (None,)},
                              task={"raw_key": None})
    assert not _sqls(cur, "INSERT INTO document_version")
    assert conn.committed
