# -*- coding: utf-8 -*-
"""
test_kb_endpoints.py — Phase 0 知识库只读接口的【授权先行】行为（不依赖 DB）。

直接调用 api 的端点函数（request=None），验证：org-tree 在 kb_admin 下返回全量、
employee/匿名在任何 DB 查询【之前】被 401/403 拒绝。授权走 resolve_kb_identity（simulate
从 RAG_SIM_USER_ROLE 取），证明令牌 role 提示不是边界、DB 现查才是。
"""
import pytest


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


def test_org_tree_kb_admin_sees_all(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    resp = api.kb_org_tree(request=None, identity=api.Identity(user_id="dev1"))
    assert resp.my_role == "kb_admin"
    assert len(resp.my_managed_owner_depts) == 10            # kb_admin 管理全部 owner_dept
    assert len(resp.acl_groups) == 10
    # 部门→组映射包含已知条目
    assert resp.dept_name_to_groups.get("财务部") == ["finance"]


def test_org_tree_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_org_tree(request=None, identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_org_tree_anonymous_unauthorized(monkeypatch):
    _skip_if_not_sim()
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_org_tree(request=None, identity=None)
    assert getattr(ei.value, "status_code", None) == 401


def test_my_docs_employee_forbidden_before_db(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    # employee 在任何 DB 查询前就 403（若先查库会是 500/连接错误）
    with pytest.raises(Exception) as ei:
        api.kb_my_docs(request=None, limit=20, offset=0, identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_dept_admin_org_tree_scope(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_USER_DEPT", "国际贸易部")          # 读组 [marketing, production]
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")  # 写授权仅 marketing
    from opensearch_pipeline import api
    resp = api.kb_org_tree(request=None, identity=api.Identity(user_id="trade1"))
    assert resp.my_role == "dept_admin"
    assert resp.my_managed_owner_depts == ["marketing"]      # 读≠写：managed 不含 production
    assert resp.my_grantable_owner_depts == ["marketing"]


# ── my-docs 文档名搜索：子句 + LIKE 通配符转义（防"输入 % 匹配全部"）──────────────
class _CaptureCur:
    """桩游标：捕获 execute(sql, params)，fetchall 返回空。"""
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink["sql"] = sql
        self._sink["params"] = params

    def fetchall(self):
        return []


def _stub_capture(monkeypatch):
    sink = {}
    import opensearch_pipeline.pipeline_nodes as pn

    class _Conn:
        def cursor(self):
            return _CaptureCur(sink)

        def close(self):
            pass

    monkeypatch.setattr(pn, "_get_db_conn", lambda: _Conn())
    return sink


def test_my_docs_search_filters_and_escapes(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    resp = api.kb_my_docs(request=None, limit=20, offset=0, q="报告%_x",
                          identity=api.Identity(user_id="dev1"))
    assert resp.items == []
    assert "LIKE %s ESCAPE '!'" in sink["sql"]          # 显式 '!' 转义符（不依赖 sql_mode）
    # % → !% , _ → !_ 被转义（否则用户输入 % 会匹配全部、_ 匹配任意单字符）
    like = sink["params"][0]
    assert like == "%报告!%!_x%"
    assert sink["params"][1] == like


def test_my_docs_no_query_adds_no_search_clause(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    api.kb_my_docs(request=None, limit=20, offset=0, q="", identity=api.Identity(user_id="dev1"))
    assert "LIKE" not in sink["sql"]
    assert sink["params"] == (21, 0)   # kb_admin 无 owner 参数 → 仅 limit+1, offset


def test_pending_approvals_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_pending_approvals(request=None, identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_pending_approvals_dept_admin_forbidden(monkeypatch):
    """部门管理员能进控制台，但审批队列仅 kb_admin（读≠审批）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_pending_approvals(request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_pending_approvals_kb_admin_ok(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    resp = api.kb_pending_approvals(request=None, identity=api.Identity(user_id="dev1"))
    assert resp.items == []
    assert "PENDING_APPROVAL" in sink["sql"]


def test_kb_status_badge_recognizes_success():
    """管线 index_status='SUCCESS' 必须映射为已上线（曾错认 'INDEXED' → 1478 活跃文档全显示处理中）。"""
    from opensearch_pipeline import api
    b = api._kb_status_badge
    assert b("DONE", "SUCCESS", "active") == "已上线"        # 管线真实上线值
    assert b("DONE", "INDEXED", "active") == "已上线"        # 兼容旧/别名词
    assert b("DONE", "NOT_INDEXED", "active") == "处理中"    # 内容处理完但没进索引
    assert b("DONE", "SUCCESS", "superseded") == "已退役"    # 退役判定优先于上线
    assert b("FAILED", "NOT_INDEXED", "active") == "处理失败"
    assert b("NOT_STARTED", "NOT_INDEXED", "active") == "排队中"
    assert b("PENDING_APPROVAL", "NOT_INDEXED", "active") == "待审核"   # 公开/跨组上传待审批
    assert b("REJECTED", "NOT_INDEXED", "active") == "已驳回"   # 升版被驳回：不得落到默认"处理中"
    assert b("DONE", "SUCCESS", "active", 0) == "处理中"     # SUCCESS 但 0 活跃 chunk → 不算已上线
    # PII 隔离：即便 index_status 残留 SUCCESS 也必须显示已隔离（绝不能误显示已上线）
    assert b("DONE", "SUCCESS", "active", None, "QUARANTINED") == "已隔离"
    assert b("DONE", "NOT_INDEXED", "active", None, "QUARANTINED") == "已隔离"
    assert b("DONE", "SUCCESS", "superseded", None, "QUARANTINED") == "已退役"   # 退役判定仍优先


def test_my_docs_dept_admin_search_keeps_owner_scope(monkeypatch):
    """搜索不绕过 owner 作用域：dept_admin 搜索时 owner_dept 过滤仍在，参数顺序正确。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    api.kb_my_docs(request=None, limit=20, offset=0, q="杯", identity=api.Identity(user_id="da1"))
    assert "m.owner_dept IN" in sink["sql"]              # 作用域子句仍在
    assert sink["sql"].index("owner_dept IN") < sink["sql"].index("LIKE")  # 作用域在搜索之前
    # 参数顺序：owner(marketing) → 2×LIKE → limit+1, offset（错位会破坏过滤）
    assert sink["params"][0] == "marketing"
    assert sink["params"][1] == "%杯%" and sink["params"][2] == "%杯%"
    assert sink["params"][-2:] == (21, 0)


# ── /api/kb/browse 全部门只读浏览（Phase B）──────────────────────────────────
def _stub_rows(monkeypatch, rows):
    """桩游标：execute 捕获 SQL/params，fetchall 返回给定行（用于验 can_manage 映射）。"""
    sink = {}
    import opensearch_pipeline.pipeline_nodes as pn

    class _RowsCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            sink["sql"] = sql
            sink["params"] = params

        def fetchall(self):
            return rows

    class _Conn:
        def cursor(self):
            return _RowsCur()

        def close(self):
            pass

    monkeypatch.setattr(pn, "_get_db_conn", lambda: _Conn())
    return sink


def test_browse_employee_forbidden_before_db(monkeypatch):
    """全部门浏览仍是管理员特权：employee 在任何 DB 查询【之前】403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_browse(request=None, scope="all", identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_browse_excludes_restricted_and_no_write_scope(monkeypatch):
    """安全核心：只允许 public/dept_internal（排除 restricted）+ 只在线 + 绝不复用写作用域过滤。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    api.kb_browse(request=None, scope="all", identity=api.Identity(user_id="da1"))
    sql = sink["sql"]
    assert "permission_level IN ('public','dept_internal')" in sql   # 允许清单：restricted 一律排除
    assert "restricted" not in sql                                   # 连词都不出现
    assert "m.status='active'" in sql                                # 只列在线（退役件不可申请）
    assert "owner_dept IN" not in sql                                # 绝不复用 _kb_owner_scope_sql 写作用域


def test_browse_can_manage_flags_dept_admin(monkeypatch):
    """可见=全部门、可操作=写作用域：本部门行 can_manage=True，其他部门行 False。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    rows = [
        ("D1", "营销规范", "a.pdf", "marketing", "dept_internal", 1, "active", "2026-06-26", "DONE", "SUCCESS", None),
        ("D2", "HR 手册", "b.pdf", "hr", "dept_internal", 2, "active", "2026-06-25", "DONE", "SUCCESS", None),
    ]
    _stub_rows(monkeypatch, rows)
    from opensearch_pipeline import api
    resp = api.kb_browse(request=None, scope="all", identity=api.Identity(user_id="da1"))
    by = {i.doc_id: i for i in resp.items}
    assert by["D1"].can_manage is True      # 本部门 marketing → 可管
    assert by["D2"].can_manage is False     # 其他部门 hr → 只读


def test_browse_kb_admin_all_manageable(monkeypatch):
    """kb_admin 全部门皆可管：can_manage 恒 True。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    rows = [("D1", "x", "a.pdf", "hr", "dept_internal", 1, "active", "t", "DONE", "SUCCESS", None)]
    _stub_rows(monkeypatch, rows)
    from opensearch_pipeline import api
    resp = api.kb_browse(request=None, scope="all", identity=api.Identity(user_id="dev1"))
    assert resp.items[0].can_manage is True


def test_browse_invalid_scope_fail_closed_empty(monkeypatch):
    """非法 scope（非 all）→ fail-closed 空，绝不静默当全量。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    resp = api.kb_browse(request=None, scope="managed", identity=api.Identity(user_id="dev1"))
    assert resp.items == [] and resp.has_more is False


def test_browse_owner_facet_param(monkeypatch):
    """owner_dept facet：参数化 = %s，作为查询参数传入。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    api.kb_browse(request=None, scope="all", owner_dept="hr", identity=api.Identity(user_id="dev1"))
    assert "m.owner_dept = %s" in sink["sql"]
    assert sink["params"][0] == "hr"


# ── /api/kb/access-requests 跨部门检索授权申请（Phase C 记录层）──────────────
def _stub_multi(monkeypatch, fetch_seq):
    """桩游标：execute 累积 calls；fetchone 依次弹 fetch_seq，fetchall 弹一个列表元素。"""
    sink = {"calls": []}
    seq = list(fetch_seq)
    import opensearch_pipeline.pipeline_nodes as pn

    class _Cur:
        lastrowid = 123

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            sink["calls"].append((sql, params))
            sink["sql"] = sql
            sink["params"] = params

        def fetchone(self):
            return seq.pop(0) if seq else None

        def fetchall(self):
            return seq.pop(0) if seq else []

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            sink["committed"] = True

        def close(self):
            pass

    monkeypatch.setattr(pn, "_get_db_conn", lambda: _Conn())
    return sink


def test_access_submit_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_request_submit(api.KbAccessRequestSubmit(doc_id="D1"), request=None, identity=api.Identity(user_id="e1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_access_submit_kb_admin_rejected(monkeypatch):
    """kb_admin 直接管理全部，无需申请 → 400（不查库）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_request_submit(api.KbAccessRequestSubmit(doc_id="D1"), request=None, identity=api.Identity(user_id="dev1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_access_submit_own_dept_rejected(monkeypatch):
    """本部门文档无需申请 → 400。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("marketing", "dept_internal", "active")])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_request_submit(api.KbAccessRequestSubmit(doc_id="D1"), request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_access_submit_public_rejected(monkeypatch):
    """公开文档全公司可读 → 400。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "public", "active")])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_request_submit(api.KbAccessRequestSubmit(doc_id="D1"), request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_access_submit_restricted_rejected(monkeypatch):
    """受限文档不可申请授权 → 403（绝不开放）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "restricted", "active")])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_request_submit(api.KbAccessRequestSubmit(doc_id="D1"), request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_access_submit_foreign_dept_internal_inserts(monkeypatch):
    """其他部门 dept_internal → 入队 pending；requester_depts = 申请人 managed。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [("hr", "dept_internal", "active"), None])   # 文档 + 无既有 pending
    from opensearch_pipeline import api
    resp = api.kb_access_request_submit(api.KbAccessRequestSubmit(doc_id="D1", reason="需引用"),
                                        request=None, identity=api.Identity(user_id="da1"))
    assert resp.status == "pending" and resp.already is False and resp.id == "123"
    inserts = [c for c in sink["calls"] if "INSERT INTO fuling_knowledge.kb_access_request" in c[0]]
    assert len(inserts) == 1
    assert "marketing" in inserts[0][1]      # requester_depts = managed
    assert "hr" in inserts[0][1]             # owner_dept = 文档归属


def test_access_submit_idempotent_existing_pending(monkeypatch):
    """同 (doc, 申请人) 已有 pending → 幂等返回既有，不重复入队。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "dept_internal", "active"), (77,)])
    from opensearch_pipeline import api
    resp = api.kb_access_request_submit(api.KbAccessRequestSubmit(doc_id="D1"), request=None, identity=api.Identity(user_id="da1"))
    assert resp.already is True and resp.id == "77"


def test_access_list_dept_admin_scoped(monkeypatch):
    """审批方作用域：dept_admin 仅见 owner_dept ∈ managed 的 pending。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [[]])
    from opensearch_pipeline import api
    resp = api.kb_access_requests_list(request=None, identity=api.Identity(user_id="da1"))
    assert resp.items == []
    assert "r.owner_dept IN" in sink["sql"]
    assert "r.status='pending'" in sink["sql"]
    assert sink["params"] == ("marketing",)


def test_access_list_kb_admin_all(monkeypatch):
    """kb_admin 见全部 pending（不限作用域）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [[]])
    from opensearch_pipeline import api
    api.kb_access_requests_list(request=None, identity=api.Identity(user_id="dev1"))
    assert "owner_dept IN" not in sink["sql"]


def test_access_list_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_requests_list(request=None, identity=api.Identity(user_id="e1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_access_approve_requires_owner_manage(monkeypatch):
    """审批权 = 文档所属部门管理员：非 owner_dept 管理者 → 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "pending", "D1")])      # 申请归属 hr，调用者只管 marketing
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_request_approve(api.KbAccessDecisionRequest(id="5"), request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_access_approve_updates(monkeypatch):
    """owner_dept 管理者通过 → UPDATE status='approved'。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [("marketing", "pending", "D1")])
    from opensearch_pipeline import api
    resp = api.kb_access_request_approve(api.KbAccessDecisionRequest(id="5"), request=None, identity=api.Identity(user_id="da1"))
    assert resp.decided is True and resp.status == "approved"
    updates = [c for c in sink["calls"] if "UPDATE fuling_knowledge.kb_access_request" in c[0]]
    assert len(updates) == 1 and "approved" in updates[0][1]


def test_access_reject_non_pending_idempotent(monkeypatch):
    """已决申请再审 → 幂等（decided=False, already=True），不重复改。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [("marketing", "approved", "D1")])
    from opensearch_pipeline import api
    resp = api.kb_access_request_reject(api.KbAccessDecisionRequest(id="5", reason="x"), request=None, identity=api.Identity(user_id="dev1"))
    assert resp.already is True and resp.decided is False


def test_access_revoke_approved_updates(monkeypatch):
    """owner_dept 管理者撤销【已批准】授权 → UPDATE status='revoked'（approved→revoked）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [("marketing", "approved", "D1")])
    from opensearch_pipeline import api
    resp = api.kb_access_request_revoke(
        api.KbAccessDecisionRequest(id="5", reason="申请人离职收回"), request=None, identity=api.Identity(user_id="da1"))
    assert resp.decided is True and resp.status == "revoked"
    updates = [c for c in sink["calls"] if "UPDATE fuling_knowledge.kb_access_request" in c[0]]
    assert len(updates) == 1 and "revoked" in updates[0][1]


def test_access_revoke_non_approved_idempotent(monkeypatch):
    """撤销作用于非 approved（pending/rejected）→ 幂等（already=True, decided=False），绝不误转、不写。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [("marketing", "pending", "D1")])
    from opensearch_pipeline import api
    resp = api.kb_access_request_revoke(api.KbAccessDecisionRequest(id="5"), request=None, identity=api.Identity(user_id="dev1"))
    assert resp.already is True and resp.decided is False and resp.status == "pending"
    assert not [c for c in sink["calls"] if "UPDATE fuling_knowledge.kb_access_request" in c[0]]   # 非 approved → 不写


def test_access_revoke_requires_owner_manage(monkeypatch):
    """撤销权 = 文档所属部门管理员（与审批同授权）：非 owner_dept 管理者 → 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "approved", "D1")])     # 授权归 hr，调用者只管 marketing
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_request_revoke(api.KbAccessDecisionRequest(id="5"), request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


# ── 已授权清单 /api/kb/access-grants（approved 存量，供撤销）──
def test_access_grants_list_dept_admin_scoped(monkeypatch):
    """已授权清单作用域：dept_admin 仅见 owner_dept ∈ managed 的 approved；映射 requester_depts / decided_at。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [[
        ("7", "D1", "营销规范", "marketing", "production", "王伟", "dept_internal", "引用", "2026-06-26"),
    ]])
    from opensearch_pipeline import api
    resp = api.kb_access_grants_list(request=None, identity=api.Identity(user_id="da1"))
    assert "r.status='approved'" in sink["sql"] and "r.owner_dept IN" in sink["sql"]
    assert sink["params"] == ("marketing",)
    assert len(resp.items) == 1
    assert resp.items[0].requester_dept == "production" and resp.items[0].decided_at == "2026-06-26"


def test_access_grants_list_kb_admin_all(monkeypatch):
    """kb_admin 见全部 approved（不限作用域）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [[]])
    from opensearch_pipeline import api
    api.kb_access_grants_list(request=None, identity=api.Identity(user_id="dev1"))
    assert "owner_dept IN" not in sink["sql"] and "r.status='approved'" in sink["sql"]


def test_access_grants_list_employee_forbidden(monkeypatch):
    """员工无管理台访问 → 403（先于任何 DB）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_grants_list(request=None, identity=api.Identity(user_id="e1"))
    assert getattr(ei.value, "status_code", None) == 403


def _stub_myreq(monkeypatch, request_rows, doc_state):
    """桩游标（按 SQL 片段分支）：主列表 fetchall 返回 request_rows；per-doc count(fetchone) +
    allowed_depts(fetchall) 由 doc_state 提供。用于验 /api/kb/my-access-requests 派生同步态。"""
    import json
    import opensearch_pipeline.pipeline_nodes as pn

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self._s = " ".join(sql.lower().split())
            self._p = tuple(params or ())

        def fetchall(self):
            if "from fuling_knowledge.kb_access_request r" in self._s:
                return request_rows
            if "distinct allowed_depts" in self._s:
                al = doc_state.get(self._p[0], {}).get("allowed", [])
                return [(json.dumps(al),)] if al else []
            return []

        def fetchone(self):
            if "sum(index_status='indexed')" in self._s:
                st = doc_state.get(self._p[0], {})
                return (st.get("cnt", 0), st.get("indexed", 0))
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(pn, "_get_db_conn", lambda: _Conn())


def test_my_access_requests_sync_state(monkeypatch):
    """申请人侧派生态：approved 且全 INDEXED 且 allowed_depts⊇授予组 → projected；
    否则 pending_sync；rejected → n/a。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    rows = [
        ("1", "DA", "TA", "marketing", "finance", "approved", "r", "2026-06-01", "2026-06-02", 1),
        ("2", "DB", "TB", "marketing", "quality", "approved", "r", "2026-06-01", "2026-06-02", 1),
        ("3", "DC", "TC", "marketing", "hr", "approved", "r", "2026-06-01", "2026-06-02", 1),
        ("4", "DD", "TD", "marketing", "supply", "rejected", "r", "2026-06-01", "2026-06-02", 1),
    ]
    doc_state = {
        "DA": {"cnt": 3, "indexed": 3, "allowed": ["finance"]},   # 全 INDEXED + finance⊆ → projected
        "DB": {"cnt": 2, "indexed": 1, "allowed": ["quality"]},   # 未全 INDEXED → pending_sync
        "DC": {"cnt": 2, "indexed": 2, "allowed": []},            # 全 INDEXED 但 hr⊄[] → pending_sync
    }
    _stub_myreq(monkeypatch, rows, doc_state)
    from opensearch_pipeline import api
    resp = api.kb_my_access_requests(request=None, identity=api.Identity(user_id="da1"))
    by_id = {it.id: it.sync_state for it in resp.items}
    assert by_id["1"] == "projected"
    assert by_id["2"] == "pending_sync"
    assert by_id["3"] == "pending_sync"
    assert by_id["4"] == "n/a"                                    # rejected → 不派生
    assert len(resp.items) == 4
