# -*- coding: utf-8 -*-
"""test_kb_retire.py — kb 软退役端点（#8，Option A：可逆、不删 HA3）的授权与状态行为，全程 sim。

退役只改 RDS（document_meta/version.status='retired' + chunk_meta.is_active=0），不触碰 HA3。
授权：kb_admin 任意；dept_admin 限其 managed owner_dept，且公开文档需 kb_admin。
桩 DB 按 document_meta FOR UPDATE 回放 (owner_dept, permission_level, status, current_version_no)。
"""
import pytest


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        self._last = sql
        return 1

    def fetchone(self):
        if "document_meta" in self._last and "FOR UPDATE" in self._last:
            return self.conn.meta_row
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, meta_row=None):
        self.meta_row = meta_row
        self.calls = []
        self.committed = False

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def _install(monkeypatch, conn):
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: conn)
    return conn


def _call(doc_id="DOC_X", user_id="da1"):
    from opensearch_pipeline import api
    return api.kb_retire(req=api.KbRetireRequest(doc_id=doc_id),
                         request=None, identity=api.Identity(user_id=user_id))


def _status(ei):
    return getattr(ei.value, "status_code", None)


def test_dept_admin_retires_own_dept_doc(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "dept_internal", "active", 2)))
    resp = _call()
    assert resp.retired is True and resp.already is False
    assert resp.status_badge == "已退役"
    # 三连写：meta / version / chunk 停用
    sql_all = " ".join(s for s, _ in conn.calls)
    assert "UPDATE fuling_knowledge.document_meta SET status='retired'" in sql_all
    assert "UPDATE fuling_knowledge.document_version SET status='retired'" in sql_all
    assert "chunk_meta SET is_active=0" in sql_all
    assert conn.committed is True


def test_dept_admin_cannot_retire_public(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _install(monkeypatch, _Conn(meta_row=("marketing", "public", "active", 1)))
    with pytest.raises(Exception) as ei:
        _call()
    assert _status(ei) == 403


def test_kb_admin_can_retire_public(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "public", "active", 1)))
    resp = _call(user_id="adm1")
    assert resp.retired is True
    assert conn.committed is True


def test_dept_admin_out_of_scope_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _install(monkeypatch, _Conn(meta_row=("finance", "dept_internal", "active", 1)))  # 非 managed
    with pytest.raises(Exception) as ei:
        _call()
    assert _status(ei) == 403


def test_already_retired_is_idempotent(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "dept_internal", "retired", 3)))
    resp = _call(user_id="adm1")
    assert resp.already is True and resp.retired is False
    # 幂等：除 SELECT ... FOR UPDATE 外，不应发出任何 SET ...='retired' / chunk 停用写
    assert not any("SET status='retired'" in s or "is_active=0" in s for s, _ in conn.calls)


def test_doc_not_found(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _install(monkeypatch, _Conn(meta_row=None))
    with pytest.raises(Exception) as ei:
        _call(user_id="adm1")
    assert _status(ei) == 404


def test_missing_doc_id_400(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _install(monkeypatch, _Conn())
    with pytest.raises(Exception) as ei:
        _call(doc_id="", user_id="adm1")
    assert _status(ei) == 400


def test_employee_forbidden_before_db(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    _install(monkeypatch, _Conn())
    with pytest.raises(Exception) as ei:
        _call(user_id="emp1")
    assert _status(ei) == 403
