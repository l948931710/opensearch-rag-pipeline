# -*- coding: utf-8 -*-
"""test_kb_approval.py — /api/kb/approve 与 /api/kb/reject 的授权与状态行为（全程 sim）。

评审 P2-14「封住权限提升零测试缺口」：这两个端点此前**零路由测试**——仓内仅有的
`kb_approve` 字样在 test_destructive_guard.py 里只是把它当作守卫的 op 名字符串，
与端点本身无关。审批放行意味着把 PENDING_APPROVAL 版本推进入库队列（下一批就进检索），
是纯粹的提权动作，其「仅 kb_admin」这道闸门此前没有任何测试看守。

同时钉住 F-37 纵深防御：文档已退役时不得放行任何 pending 版本——kb_retire 只把 current
版本置 retired，更早的 pending 版本可能仍 status=active，放行后会被 stage-1 认领**复活**。
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
        self.conn.calls.append((" ".join(sql.split()), params))
        self._last = sql
        return self.conn.rowcount

    def fetchone(self):
        if "document_meta" in self._last and "FOR UPDATE" in self._last:
            return self.conn.meta_row
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, meta_row=("active",), rowcount=2):
        self.meta_row = meta_row
        self.rowcount = rowcount
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


@pytest.fixture()
def audit(monkeypatch):
    """审计是旁路：捕获而非落库（write_audit 在端点内惰性 import）。"""
    seen = []
    monkeypatch.setattr("opensearch_pipeline.audit_log.write_audit",
                        lambda **kw: seen.append(kw))
    return seen


def _install(monkeypatch, conn):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    return conn


def _approve(doc_id="DOC_X", version_no=None, user_id="u1"):
    from opensearch_pipeline import api
    return api.kb_approve(api.KbApprovalRequest(doc_id=doc_id, version_no=version_no),
                          request=None, identity=api.Identity(user_id=user_id))


def _reject(doc_id="DOC_X", version_no=None, reason=None, user_id="u1"):
    from opensearch_pipeline import api
    return api.kb_reject(api.KbApprovalRequest(doc_id=doc_id, version_no=version_no,
                                               reason=reason),
                         request=None, identity=api.Identity(user_id=user_id))


def _status(ei):
    return getattr(ei.value, "status_code", None)


def _updates(conn):
    return [(s, p) for s, p in conn.calls if s.startswith("UPDATE")]


# ── 提权闸门：仅 kb_admin（此前零覆盖）────────────────────────────────────────

@pytest.mark.parametrize("role", ["employee", "dept_admin"])
@pytest.mark.parametrize("fn", ["approve", "reject"])
def test_non_kb_admin_cannot_approve_or_reject(monkeypatch, audit, role, fn):
    """dept_admin 也不行——审批放行影响的是全库入库队列，不是本部门内部事务。

    ⚠️ 关键在于：**403 必须发生在任何 DB 写之前**。只断言状态码不够——若哪天有人
    把角色判定挪到 UPDATE 之后，状态码仍是 403 而版本已经被放行了。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", role)
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    conn = _install(monkeypatch, _Conn())
    with pytest.raises(Exception) as ei:
        (_approve if fn == "approve" else _reject)()
    assert _status(ei) == 403
    assert conn.calls == [], f"403 之前不得触碰 DB，实际执行了 {conn.calls}"
    assert not conn.committed
    assert audit == [], "被拒的请求不该写审计"


@pytest.mark.parametrize("fn", ["approve", "reject"])
def test_kb_admin_missing_doc_id_is_400_not_a_blind_update(monkeypatch, audit, fn):
    """缺 doc_id → 400，且不得下发任何 UPDATE（谓词只剩状态列 = 全表放行）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn())
    with pytest.raises(Exception) as ei:
        (_approve if fn == "approve" else _reject)(doc_id="")
    assert _status(ei) == 400
    assert conn.calls == []


# ── approve 行为 ────────────────────────────────────────────────────────────

def test_approve_only_touches_pending_versions(monkeypatch, audit):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(rowcount=2))
    out = _approve()
    assert out == {"status": "ok", "approved": 2}
    ups = _updates(conn)
    assert len(ups) == 1
    sql, params = ups[0]
    assert "content_process_status='PENDING_APPROVAL'" in sql, (
        "必须只放行待审版本——丢了这个谓词会把 FAILED/REJECTED 版本一并复活")
    assert "SET content_process_status='NOT_STARTED'" in sql
    assert "approval_status='APPROVED'" in sql
    assert params == ("DOC_X",)
    assert conn.committed
    assert audit and audit[0]["action_type"] == "APPROVE"


def test_approve_locks_document_meta_before_deciding(monkeypatch, audit):
    """F-37 与 kb_retire 的串行化靠 document_meta FOR UPDATE——且必须**先于** UPDATE。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn())
    _approve()
    kinds = ["lock" if ("document_meta" in s and "FOR UPDATE" in s)
             else "update" if s.startswith("UPDATE") else "other" for s, _ in conn.calls]
    assert "lock" in kinds and "update" in kinds
    assert kinds.index("lock") < kinds.index("update")


def test_approve_refuses_to_revive_a_retired_doc(monkeypatch, audit):
    """F-37：文档已退役 ⇒ 一个 pending 版本都不放行（否则被 stage-1 认领复活）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(meta_row=("retired",)))
    out = _approve()
    assert out["approved"] == 0 and "退役" in out.get("note", "")
    assert _updates(conn) == [], "退役文档下不得下发任何放行 UPDATE"


def test_approve_version_scoped_vs_all_pending(monkeypatch, audit):
    """带 version_no → 谓词收窄到该版本；不带 → 放行该文档全部 pending 版本。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn())
    _approve(version_no=3)
    sql, params = _updates(conn)[0]
    assert "AND version_no=%s" in sql and params == ("DOC_X", 3)

    conn2 = _install(monkeypatch, _Conn())
    _approve()
    sql2, params2 = _updates(conn2)[0]
    assert "version_no=%s" not in sql2 and params2 == ("DOC_X",)


# ── reject 行为 ─────────────────────────────────────────────────────────────

def test_reject_marks_rejected_and_truncates_reason(monkeypatch, audit):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn(rowcount=1))
    out = _reject(reason="x" * 900)
    assert out == {"status": "ok", "rejected": 1}
    sql, params = _updates(conn)[0]
    assert "content_process_status='PENDING_APPROVAL'" in sql
    assert "SET content_process_status='REJECTED'" in sql and "approval_status='REJECTED'" in sql
    assert len(params[0]) == 500, "content_process_error 列为 VARCHAR(500)，必须截断"
    assert audit and audit[0]["action_type"] == "REJECT"


def test_reject_defaults_reason_when_absent(monkeypatch, audit):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install(monkeypatch, _Conn())
    _reject(reason=None)
    assert _updates(conn)[0][1][0] == "rejected"




# ── P2-11：review-tasks 分页（此前只回 items，limit=20 静默截断）──────────────

def _review_tasks(monkeypatch, n_rows, *, limit=20, offset=0, include_closed=False):
    """桩：SELECT review_task 回 n_rows 行（端点多取一条判 has_more）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.routes import kb_console

    seen = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def execute(self, sql, params=None):
            seen["sql"] = " ".join(sql.split())
            seen["params"] = params

        def fetchall(self):
            return [(f"T{i}", "D1", "标题", 1, "spot_check_mismatch", "r", "hr",
                     "restricted", "PENDING", "", "2026-08-03", 3) for i in range(n_rows)]

        def fetchone(self): return None

    class _Cn:
        def cursor(self): return _C()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Cn())
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    from opensearch_pipeline import api
    out = api.kb_review_tasks(request=None, limit=limit, offset=offset,
                              include_closed=include_closed,
                              identity=api.Identity(user_id="kb1"))
    return out, seen


def test_review_tasks_reports_has_more_and_trims_the_probe_row(monkeypatch):
    """多取一条判 has_more，但**不得**把那条渲染出去（否则每页多一条、翻页错位）。"""
    out, seen = _review_tasks(monkeypatch, 21, limit=20)
    assert out.has_more is True
    assert len(out.items) == 20, "探测行必须被裁掉"
    assert "LIMIT %s OFFSET %s" in seen["sql"]
    assert seen["params"] == (21, 0), "取 limit+1 条"


def test_review_tasks_last_page_has_no_more(monkeypatch):
    out, _ = _review_tasks(monkeypatch, 20, limit=20)
    assert out.has_more is False and len(out.items) == 20


def test_review_tasks_order_by_has_unique_tiebreaker(monkeypatch):
    """新增 OFFSET 分页不得重演 d2c8e12：created_at 是秒精度，必须带 task_id。"""
    _, seen_open = _review_tasks(monkeypatch, 1, include_closed=False)
    _, seen_closed = _review_tasks(monkeypatch, 1, include_closed=True)
    for tag, seen in (("open", seen_open), ("closed", seen_closed)):
        order = seen["sql"].split("ORDER BY")[1].split("LIMIT")[0]
        assert "t.task_id" in order, f"{tag} 分支 ORDER BY 缺唯一 tiebreaker：{order}"


def test_review_tasks_cache_key_includes_offset(monkeypatch):
    """offset 必须进 cache key —— 漏了它第 2 页会命中第 1 页缓存（加载更多变成重复追加）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.routes import kb_console
    keys = []
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: keys.append(k) or None)

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): pass
        def fetchall(self): return []
        def fetchone(self): return None

    class _Cn:
        def cursor(self): return _C()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Cn())
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    from opensearch_pipeline import api
    api.kb_review_tasks(request=None, offset=0, identity=api.Identity(user_id="kb1"))
    api.kb_review_tasks(request=None, offset=20, identity=api.Identity(user_id="kb1"))
    assert keys[0] != keys[1], f"两页 cache key 相同 ⇒ 第 2 页会吃第 1 页缓存：{keys}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
