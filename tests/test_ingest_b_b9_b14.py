# -*- coding: utf-8 -*-
"""B9 + B14（2026-07-25）：console 登记入口的两处查重缺口。同一函数，合并一次改动。

B9：`_kb_content_dups` 的判据是 `v.etag`（OSS **字节级**指纹），docx↔pdf 这类跨格式孪生
    逐字节必然不同 → 永远查不出；而 `ingest_policy.stem_twin_action` 那套同名防重此前只被
    `dataworks_nodes/register_new_files.py` 调用，真正在用的 console 自助上传入口零防线
    （FL-ZS-WI-005 被双注册就是这个形态）。修法：加 stem 提示，**advisory 不硬拦**。

B14：字节完全相同的重传（升版）此前要先付一整遍抽取（含扫描件页级 OCR），才被
    canonical_sha256 的 skip-gate 拦下 —— 而那个 gate 还是 flag-gated 的。修法：用**已经
    拿到的** OSS HEAD ETag 与当前版本比对做源头提示，零额外往返、零抽取。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import opensearch_pipeline.routes.kb_console as KC


class _Cur:
    def __init__(self, rows):
        self.rows, self.sqls = rows, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sqls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


# ═══════════════════ B9：stem 孪生提示 ═══════════════════

def test_b9_hint_lists_same_dept_same_stem_docs(monkeypatch):
    cur = _Cur([("DOC_OLD", "作业指导书.pdf")])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: _Conn(cur))
    hint = KC._kb_stem_twin_hint("production", "作业指导书.docx", "DOC_NEW")
    assert "作业指导书.pdf" in hint
    sql = cur.sqls[0][0]
    assert "owner_dept = %s" in sql and "m.doc_id <> %s" in sql
    assert "LOWER(m.status) = 'active'" in sql


def test_b9_no_twin_returns_empty(monkeypatch):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: _Conn(_Cur([])))
    assert KC._kb_stem_twin_hint("production", "唯一文件.docx", "DOC_NEW") == ""


def test_b9_is_fail_open(monkeypatch):
    """advisory：查不出绝不能影响上传。"""
    def _boom(**kw):
        raise RuntimeError("rds down")

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    assert KC._kb_stem_twin_hint("production", "x.docx", "DOC_NEW") == ""


def test_b9_advisory_never_blocks_registration():
    """必须**不**抛 HTTPException、**不** return —— console 有真人在场，
    静默跳过会被读成上传失败；归属/退役是运营决策，不该由防重代劳。"""
    src = inspect.getsource(KC._kb_stem_twin_hint)
    assert "HTTPException" not in src and "raise" not in src


def test_b9_only_on_new_not_on_version():
    """升版（同 doc_id 换文件）天然不算孪生。"""
    src = inspect.getsource(KC.kb_register)
    i_branch = src.index('if action != "version":')
    i_call = src.index("_kb_stem_twin_hint(")
    assert i_branch < i_call


# ═══════════════════ B14：同 ETag 升版源头拦截 ═══════════════════

def test_b14_compares_against_current_version_etag():
    src = " ".join(inspect.getsource(KC.kb_register).split())
    assert "SELECT etag FROM" in src and "_same_as_current" in src
    # 必须用**已拿到的** etag_val（OSS HEAD 的产物），不额外发 HEAD
    assert "str(_prev[0]) == str(etag_val)" in src


def test_b14_does_not_reuse_cross_doc_dup_helper():
    """_kb_content_dups 的 SQL 写死 `AND m.doc_id <> %s`（跨文档查重），
    这个场景一条都查不出 —— 复用它等于什么都没做。"""
    src = inspect.getsource(KC.kb_register)
    i_ver = src.index("_same_as_current = False\n                    try:")
    seg = src[i_ver:i_ver + 900]
    assert "_kb_content_dups" not in seg


def test_b14_is_advisory_not_a_hard_block():
    """只提示，不拒绝：真人可能就是要重传（例如修了元数据）。"""
    src = inspect.getsource(KC.kb_register)
    i = src.index("_same_as_current = bool(")
    seg = src[i:i + 400]
    assert "HTTPException" not in seg


def test_b14_fail_open_on_query_error():
    src = inspect.getsource(KC.kb_register)
    i = src.index("except Exception as _e14")
    seg = src[i:i + 200]
    assert "logger.warning" in seg and "raise" not in seg


# ═══════════════════ 合并改动的契约 ═══════════════════

def test_response_model_carries_both_fields():
    fields = KC.KbRegisterResponse.model_fields
    assert "stem_twin" in fields and "same_as_current" in fields
    assert fields["stem_twin"].default == ""
    assert fields["same_as_current"].default is False
