# -*- coding: utf-8 -*-
"""test_kb_set_visibility.py — 重设已上线文档基础可见范围（/api/kb/set-visibility）授权 + 三向语义，全程 sim。

改文档【自身基础级别】(dept_internal/public/restricted)，与跨部门授权(allowed_depts)正交。授权同 retire/restore
不对称：_kb_can_manage + 涉及 public（目标或当前）需 kb_admin。桩 DB 回放 document_meta FOR UPDATE 行。
默认 RAG_ALLOWED_DEPTS_ACL 关 → 投影 no-op（只验 RDS 元数据写）；另有一测开 flag 验证投影注入。
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
        if "document_version" in self._last and "publish_status" in self._last:
            return self.conn.version_row   # 隔离防线 SELECT (publish_status, gate_status)
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, meta_row=None, version_row=None):
        self.meta_row = meta_row      # (owner_dept, permission_level, status, current_version_no)
        self.version_row = version_row  # (publish_status, gate_status)；None = 无隔离标记
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
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    return conn


def _call(target, doc_id="DOC_X", user_id="da1"):
    from opensearch_pipeline import api
    return api.kb_set_visibility(
        req=api.KbSetVisibilityRequest(doc_id=doc_id, permission_level=target),
        request=None, identity=api.Identity(user_id=user_id))


def _sql(conn):
    return " ".join(s for s, _ in conn.calls)


def _status(ei):
    return getattr(ei.value, "status_code", None)


# ── 授权 ──────────────────────────────────────────────────────────
def test_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_set_visibility(req=api.KbSetVisibilityRequest(doc_id="D", permission_level="public"),
                              request=None, identity=api.Identity(user_id="e1"))
    assert _status(ei) == 403


def test_dept_admin_foreign_dept_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _install(monkeypatch, _Conn(meta_row=("hr", "dept_internal", "active", 1)))
    with pytest.raises(Exception) as ei:
        _call("restricted")
    assert _status(ei) == 403


def test_dept_admin_to_public_needs_kb_admin(monkeypatch):
    """dept_admin 放宽到 public → 403（涉及全公司，与上传公开需审批同款不对称）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _install(monkeypatch, _Conn(meta_row=("marketing", "dept_internal", "active", 1)))
    with pytest.raises(Exception) as ei:
        _call("public")
    assert _status(ei) == 403


def test_dept_admin_from_public_needs_kb_admin(monkeypatch):
    """当前为 public 的文档收窄，也需 kb_admin（收窄影响全公司可见）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _install(monkeypatch, _Conn(meta_row=("marketing", "public", "active", 1)))
    with pytest.raises(Exception) as ei:
        _call("dept_internal")
    assert _status(ei) == 403


# ── 校验 / 幂等 / 状态 ────────────────────────────────────────────
def test_invalid_level_rejected(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_set_visibility(req=api.KbSetVisibilityRequest(doc_id="D", permission_level="whatever"),
                              request=None, identity=api.Identity(user_id="da1"))
    assert _status(ei) == 400


def test_idempotent_same_level(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "dept_internal", "active", 1)))
    resp = _call("dept_internal")
    assert resp.already is True and resp.changed is False
    assert "UPDATE fuling_knowledge.document_meta SET permission_level" not in _sql(conn)


def test_non_active_rejected(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _install(monkeypatch, _Conn(meta_row=("marketing", "dept_internal", "retired", 1)))
    with pytest.raises(Exception) as ei:
        _call("public", user_id="dev1")
    assert _status(ei) == 409


# ── 三向语义 ──────────────────────────────────────────────────────
def test_to_restricted_deactivates_chunks(monkeypatch):
    """→ restricted：改级别 + 停用本版本 chunk（离开检索），不激活。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "dept_internal", "active", 3)))
    resp = _call("restricted")
    assert resp.changed is True and resp.permission_level == "restricted"
    s = _sql(conn)
    assert "document_meta SET permission_level" in s
    assert "chunk_meta SET permission_level" in s
    assert "chunk_meta SET is_active=0" in s
    assert "SET is_active=1" not in s                   # 收紧方向绝不激活（区别于 deactivate 的 WHERE is_active=1）
    assert conn.committed is True


def test_restricted_to_dept_internal_reactivates(monkeypatch):
    """restricted → dept_internal：重新激活 chunk + 标脏 NOT_INDEXED（stage-3 重推）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "restricted", "active", 1)))
    resp = _call("dept_internal")
    assert resp.changed is True
    s = _sql(conn)
    assert "is_active=1" in s and "NOT_INDEXED" in s
    assert "chunk_meta SET is_active=0" not in s


def test_public_to_dept_internal_marks_dirty(monkeypatch):
    """public ↔ dept_internal（都在检索内）：标脏重推让 HA3 chunk 带新级别；不停用。kb_admin。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "public", "active", 1)))
    resp = _call("dept_internal", user_id="dev1")
    assert resp.changed is True
    s = _sql(conn)
    assert "document_meta SET permission_level" in s and "NOT_INDEXED" in s
    assert "chunk_meta SET is_active=0" not in s        # 不离开检索


def test_projection_injected_when_flag_on(monkeypatch):
    """RAG_ALLOWED_DEPTS_ACL 开 → 同事务 enqueue + materialize 注入（读己写的新级别）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    # config 是单例（get_config 缓存）→ 直接翻 live 属性，比设 env 后重建更稳
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config().rag, "allowed_depts_acl", True)
    calls = {"enqueue": 0, "materialize": 0}
    monkeypatch.setattr("opensearch_pipeline.access_grants.enqueue_acl_projection",
                        lambda cur, doc_id, reason=None: calls.__setitem__("enqueue", calls["enqueue"] + 1))
    monkeypatch.setattr("opensearch_pipeline.access_grants.materialize_doc_allowed_depts",
                        lambda cur, doc_id, **k: calls.__setitem__("materialize", calls["materialize"] + 1))
    # restricted→dept_internal 走 dept_admin 权限（public 需 kb_admin，避开）
    _install(monkeypatch, _Conn(meta_row=("marketing", "restricted", "active", 1)))
    resp = _call("dept_internal")
    assert resp.changed is True
    assert calls["enqueue"] == 1 and calls["materialize"] == 1


def test_quarantined_version_blocked(monkeypatch):
    """PII 复活防线：隔离文档（publish_status='QUARANTINED'，doc status 仍 'active'、级别被隔离流程
    收紧为 restricted）→ 一律 409，绝不发出 is_active=1 重激活写。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "restricted", "active", 1),
                                       version_row=("QUARANTINED", "quarantined")))
    with pytest.raises(Exception) as ei:
        _call("dept_internal")
    assert _status(ei) == 409
    assert "is_active=1" not in _sql(conn)


def test_quarantined_gate_status_only_blocked(monkeypatch):
    """gate_status='quarantined' 单独存在（publish_status 缺省）也要拦（OR 语义）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(meta_row=("hr", "restricted", "active", 2),
                                       version_row=(None, "quarantined")))
    with pytest.raises(Exception) as ei:
        _call("public", user_id="adm1")
    assert _status(ei) == 409
    assert "is_active=1" not in _sql(conn)


def test_restore_quarantined_blocked(monkeypatch):
    """退役→恢复 不是隔离复活通道：kb_restore 对隔离版本同样 409（共用 _assert_version_not_quarantined）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    conn = _install(monkeypatch, _Conn(meta_row=("marketing", "restricted", "retired", 1),
                                       version_row=("QUARANTINED", "quarantined")))
    with pytest.raises(Exception) as ei:
        api.kb_restore(req=api.KbRetireRequest(doc_id="DOC_Q"), request=None,
                       identity=api.Identity(user_id="adm1"))
    assert _status(ei) == 409
    assert "is_active=1" not in _sql(conn)


def test_kb_admin_any_dept_to_public(monkeypatch):
    """kb_admin 可把任意归属文档改 public。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(meta_row=("hr", "dept_internal", "active", 1)))
    resp = _call("public", user_id="dev1")
    assert resp.changed is True and resp.permission_level == "public"
    assert conn.committed is True
