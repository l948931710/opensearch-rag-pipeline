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
    assert len(resp.my_managed_owner_depts) == 15            # kb_admin 管理全部 owner_dept（2026-07-03 扩容至 15 组）
    assert len(resp.acl_groups) == 15
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
    # 2026-07-04 拍板：共享=归属部门管理员职权 → 共享目标面广告=全量白名单（写权面不变）
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS
    assert set(resp.my_grantable_owner_depts) == set(_VALID_ACL_GROUPS)


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

    class _Conn:
        def cursor(self):
            return _CaptureCur(sink)

        def close(self):
            pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
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
    # 0-chunk / 版本被跳过终态 → 未入索引（此前落到"处理中"，管理员看不出永远搜不到）
    assert b("DONE", None, "active", None, None, "EMPTY") == "未入索引"
    assert b("DONE", None, "active", None, "SKIPPED_EMPTY") == "未入索引"
    assert b("QUARANTINED", None, "active", None, "SKIPPED_EXPLOSION", "QUARANTINED_EXPLOSION") == "未入索引"
    assert b("DONE", None, "active", None, "QUARANTINED", "EMPTY") == "已隔离"   # PII 隔离优先
    assert b("DONE", "SUCCESS", "active", None, None, "DONE") == "已上线"        # 正常件不受影响


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

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
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
        ("D1", "营销规范", "a.pdf", "marketing", "dept_internal", 1, "active", "2026-06-26", "DONE", "SUCCESS", None, "DONE"),
        ("D2", "HR 手册", "b.pdf", "hr", "dept_internal", 2, "active", "2026-06-25", "DONE", "SUCCESS", None, "DONE"),
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
    rows = [("D1", "x", "a.pdf", "hr", "dept_internal", 1, "active", "t", "DONE", "SUCCESS", None, "DONE")]
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

        def rollback(self):
            sink["rolledback"] = True

        def close(self):
            pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
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


# ── POST /api/kb/access-grants：owner 侧主动共享（多部门可见度）──────────────
def test_grant_create_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_grant_create(api.KbAccessGrantCreate(doc_id="D1", target_depts=["hr"]),
                                   request=None, identity=api.Identity(user_id="e1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_grant_create_no_valid_targets_rejected(monkeypatch):
    """目标组码全非白名单 → 400（不查库）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_grant_create(api.KbAccessGrantCreate(doc_id="D1", target_depts=["typo_dept"]),
                                   request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_grant_create_requires_owner_manage(monkeypatch):
    """共享权 = 文档所属部门管理员：doc 归属 hr、调用者只管 marketing → 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "dept_internal", "active")])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_grant_create(api.KbAccessGrantCreate(doc_id="D1", target_depts=["rd"]),
                                   request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_grant_create_public_rejected(monkeypatch):
    """公开文档全公司可读，无需共享 → 400。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("marketing", "public", "active")])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_grant_create(api.KbAccessGrantCreate(doc_id="D1", target_depts=["hr"]),
                                   request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_grant_create_restricted_rejected(monkeypatch):
    """受限文档绝不外露 → 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("marketing", "restricted", "active")])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_grant_create(api.KbAccessGrantCreate(doc_id="D1", target_depts=["hr"]),
                                   request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_grant_create_inactive_rejected(monkeypatch):
    """非在线文档（退役等）不可共享 → 400。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("marketing", "dept_internal", "retired")])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_access_grant_create(api.KbAccessGrantCreate(doc_id="D1", target_depts=["hr"]),
                                   request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_grant_create_happy_inserts_approved(monkeypatch):
    """本部门 dept_internal + 合法目标 → 逐目标 INSERT status='approved'（decided_by=授权人）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [("marketing", "dept_internal", "active"), []])   # 文档 + 无既有 approved
    from opensearch_pipeline import api
    resp = api.kb_access_grant_create(api.KbAccessGrantCreate(doc_id="D1", target_depts=["hr", "rd"], reason="巡检需要"),
                                      request=None, identity=api.Identity(user_id="da1"))
    assert resp.ok is True and sorted(resp.granted) == ["hr", "rd"] and resp.skipped == []
    inserts = [c for c in sink["calls"] if "INSERT INTO fuling_knowledge.kb_access_request" in c[0]]
    assert len(inserts) == 2
    assert all("'approved'" in c[0] for c in inserts)          # 直插 approved（复用同一状态机）
    assert all("da1" in c[1] for c in inserts)                 # requester=decided_by=授权人
    assert {c[1][4] for c in inserts} == {"hr", "rd"}          # 每目标一行（撤销粒度）
    assert sink.get("committed") is True


def test_grant_create_idempotent_covered_and_self_skipped(monkeypatch):
    """已覆盖目标（含被动流 CSV 行）与归属部门自身 → skipped；其余照常放行。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [
        ("marketing", "dept_internal", "active"),
        [("hr,quality",)],                                     # 既有 approved（CSV 覆盖 hr + quality）
    ])
    from opensearch_pipeline import api
    resp = api.kb_access_grant_create(
        api.KbAccessGrantCreate(doc_id="D1", target_depts=["hr", "marketing", "rd", "quality"]),
        request=None, identity=api.Identity(user_id="da1"))
    assert resp.granted == ["rd"]                              # 只有 rd 是新放行
    assert sorted(resp.skipped) == ["hr", "marketing", "quality"]    # 覆盖×2 + 归属自身×1
    inserts = [c for c in sink["calls"] if "INSERT INTO fuling_knowledge.kb_access_request" in c[0]]
    assert len(inserts) == 1 and inserts[0][1][4] == "rd"


def test_grant_create_umbrella_of_subline_owner_skipped(monkeypatch):
    """归属为生产子线（production_mold）时，伞组 production 目标冗余 → skipped（伞用户本就可读）。

    子线 owner 不在 dept_admin 写白名单（sim 会被 sanitize 掉）→ 用 kb_admin（全权）覆盖此分支。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [("production_mold", "dept_internal", "active"), []])
    from opensearch_pipeline import api
    resp = api.kb_access_grant_create(
        api.KbAccessGrantCreate(doc_id="D1", target_depts=["production", "hr"]),
        request=None, identity=api.Identity(user_id="dev1"))
    assert resp.granted == ["hr"] and resp.skipped == ["production"]
    inserts = [c for c in sink["calls"] if "INSERT INTO fuling_knowledge.kb_access_request" in c[0]]
    assert len(inserts) == 1 and inserts[0][1][4] == "hr"


def test_grant_create_out_of_taxonomy_subline_not_skipped(monkeypatch):
    """闭集外 production_*（papercup 双拼）：production 用户检索 fail-closed 读不到 → 共享给
    production 是唯一放行通道，必须真写 grant 行（此前 startswith 前缀判定会误吞成冗余）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.retriever import _PRODUCTION_UMBRELLA_OWNERS
    assert "production_papercup" not in _PRODUCTION_UMBRELLA_OWNERS   # 前提自证：闭集外
    sink = _stub_multi(monkeypatch, [("production_papercup", "dept_internal", "active"), []])
    from opensearch_pipeline import api
    resp = api.kb_access_grant_create(
        api.KbAccessGrantCreate(doc_id="D1", target_depts=["production"]),
        request=None, identity=api.Identity(user_id="dev1"))
    assert resp.granted == ["production"] and resp.skipped == []
    inserts = [c for c in sink["calls"] if "INSERT INTO fuling_knowledge.kb_access_request" in c[0]]
    assert len(inserts) == 1 and inserts[0][1][4] == "production"


def test_grant_create_marketing_on_production_family_skipped(monkeypatch):
    """marketing 读者经共享面本就覆盖 production 家族 → 对 production_mold 授 marketing 是冗余，
    skipped（此前前缀判定漏掉这个非前缀形覆盖，写了冗余行）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [("production_mold", "dept_internal", "active"), []])
    from opensearch_pipeline import api
    resp = api.kb_access_grant_create(
        api.KbAccessGrantCreate(doc_id="D1", target_depts=["marketing", "hr"]),
        request=None, identity=api.Identity(user_id="dev1"))
    assert resp.granted == ["hr"] and resp.skipped == ["marketing"]
    inserts = [c for c in sink["calls"] if "INSERT INTO fuling_knowledge.kb_access_request" in c[0]]
    assert len(inserts) == 1 and inserts[0][1][4] == "hr"


# ── 利用度 enrich（qa_retrieved_doc 事实表，RAG_QA_FACT_JOIN 门控）──────────────
def test_my_docs_usage_enrich_when_fact_join_on(monkeypatch):
    """fact join 可用：页内 doc 一次聚合；命中=次数+时间，未命中=0（真·从未被引用）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.qa_facts.fact_join_enabled", lambda: True)
    docrows = [
        ("D1", "t1", "a.pdf", "hr", "dept_internal", 1, "active", "ts", "DONE", "SUCCESS", None, "DONE", None),
        ("D2", "t2", "b.pdf", "hr", "dept_internal", 1, "active", "ts", "DONE", "SUCCESS", None, "DONE", None),
    ]
    _stub_multi(monkeypatch, [docrows, [("D1", 5, "2026-07-01 10:00:00")]])
    from opensearch_pipeline import api
    resp = api.kb_my_docs(request=None, limit=20, offset=0, identity=api.Identity(user_id="adm1"))
    by = {i.doc_id: i for i in resp.items}
    assert by["D1"].cited_count == 5 and by["D1"].last_cited_at.startswith("2026-07-01")
    assert by["D2"].cited_count == 0 and by["D2"].last_cited_at == ""


def test_my_docs_usage_none_when_fact_join_off(monkeypatch):
    """flag 关（默认）：cited_count=None（不可用），绝不显示成 0——0 与「不知道」必须可区分。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.qa_facts.fact_join_enabled", lambda: False)
    docrows = [("D1", "t1", "a.pdf", "hr", "dept_internal", 1, "active", "ts", "DONE", "SUCCESS", None, "DONE", None)]
    _stub_multi(monkeypatch, [docrows])
    from opensearch_pipeline import api
    resp = api.kb_my_docs(request=None, limit=20, offset=0, identity=api.Identity(user_id="adm1"))
    assert resp.items[0].cited_count is None


def test_my_docs_reject_reason_only_when_rejected(monkeypatch):
    """驳回原因外露口径：仅 content_process_status=REJECTED 时填 reject_reason（反馈闭环）；
    其他失败态的 content_process_error 是内部诊断文案，不外发。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.qa_facts.fact_join_enabled", lambda: False)
    docrows = [
        ("D1", "t1", "a.pdf", "hr", "dept_internal", 1, "active", "ts", "REJECTED", None, None, "DONE", "内容过期，已被 v3 取代"),
        ("D2", "t2", "b.pdf", "hr", "dept_internal", 1, "active", "ts", "FAILED", "FAILED", None, "DONE", "OCR timeout traceback…"),
    ]
    _stub_multi(monkeypatch, [docrows])
    from opensearch_pipeline import api
    resp = api.kb_my_docs(request=None, limit=20, offset=0, identity=api.Identity(user_id="adm1"))
    by = {i.doc_id: i for i in resp.items}
    assert by["D1"].status_badge == "已驳回" and by["D1"].reject_reason == "内容过期，已被 v3 取代"
    assert by["D2"].reject_reason == ""   # 处理失败 ≠ 驳回：内部错误文案不外露


def test_ledger_filter_anomaly_badge_in_clause():
    """「异常」聚合筛选：badge=异常 → 坏徽章 IN 集合（与前端 BAD_BADGES/待办条同口径）；
    普通徽章仍走单值等号。"""
    from opensearch_pipeline.routes.kb_console import _kb_ledger_filter_sql
    from opensearch_pipeline.api import _KB_BAD_BADGES
    sql, params = _kb_ledger_filter_sql("", "异常", "")
    assert "IN (%s,%s,%s,%s)" in sql and list(params) == list(_KB_BAD_BADGES)
    sql1, params1 = _kb_ledger_filter_sql("", "已上线", "")
    assert sql1.rstrip().endswith("= %s") and params1 == ["已上线"]


# ── GET /api/kb/feedback-review：差评联动复核队列（部门作用域，只读）────────────
def test_feedback_review_groups_and_scopes(monkeypatch):
    """按 message 分组保序 + 文档去重；dept_admin 作用域进 SQL（owner IN）；问题/补充说明过 PII 脱敏；
    点踩原因映射中文；默认只收未处置（handled 子句）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    # 9 列：message_id, created_at, question, doc_id, title, owner_dept, feedback_reason, feedback_comment, handled_status
    rows = [
        ("M1", "2026-07-03 10:00:00", "物料标准是多少？手机 13812345678", "D1", "营销物料规范", "marketing", "inaccurate,outdated", "手机 13812345678 那段过期了", None),
        ("M1", "2026-07-03 10:00:00", "物料标准是多少？手机 13812345678", "D2", "品牌 VI 手册", "marketing", "inaccurate,outdated", "手机 13812345678 那段过期了", None),
        ("M1", "2026-07-03 10:00:00", "物料标准是多少？手机 13812345678", "D1", "营销物料规范", "marketing", "inaccurate,outdated", "手机 13812345678 那段过期了", None),  # 重复 doc
        ("M2", "2026-07-02 09:00:00", "退货流程？", "D3", "售后 SOP", "marketing", None, "", "PENDING"),
    ]
    sink = _stub_multi(monkeypatch, [rows])
    from opensearch_pipeline import api
    resp = api.kb_feedback_review(request=None, limit=20, identity=api.Identity(user_id="da1"))
    assert resp.scope == "dept"
    assert [i.message_id for i in resp.items] == ["M1", "M2"]          # 保序（差评时间倒序）
    assert [d.doc_id for d in resp.items[0].docs] == ["D1", "D2"]      # 文档去重
    assert "13812345678" not in resp.items[0].question                 # 他人提问必须脱敏
    assert resp.items[0].reasons == ["不准确", "已过时"]                 # 原因码 → 中文标签（多选）
    assert "13812345678" not in resp.items[0].comment                  # 补充说明同样脱敏
    assert resp.items[0].comment and resp.items[0].handled is False
    assert resp.items[1].reasons == []                                 # 无原因 → 空
    assert "owner_dept IN" in sink["sql"] and "downvote" in sink["sql"]
    assert "handled_status NOT IN ('RESOLVED','DISMISSED')" in sink["sql"]   # 默认只收未处置


def test_feedback_review_include_resolved_drops_handled_filter(monkeypatch):
    """include_resolved=True → 不加 handled 过滤（连已处置一并返回）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [[]])
    from opensearch_pipeline import api
    api.kb_feedback_review(request=None, limit=20, include_resolved=True, identity=api.Identity(user_id="adm1"))
    assert "handled_status NOT IN" not in sink["sql"]


def test_feedback_review_kb_admin_global_empty_ok(monkeypatch):
    """kb_admin 全库（无 owner 过滤）；近窗口无差评 → 诚实空。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [[]])
    from opensearch_pipeline import api
    resp = api.kb_feedback_review(request=None, limit=20, identity=api.Identity(user_id="adm1"))
    assert resp.scope == "global" and resp.items == []
    assert "owner_dept IN" not in sink["sql"]


# ── GET /api/kb/visibility-explain：「谁能看到这篇文档」解释器（只读）──────────
def test_visibility_explain_dept_internal_with_grants(monkeypatch):
    """dept_internal：owner 组 + 授权部门；与检索同源（marketing 无伞/共享 → 只有自身）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [
        ("marketing", "dept_internal", "active", 1),      # document_meta
        (None, None),                                     # document_version（未隔离）
        [("hr,rd",), ("quality",)],                       # approved 授权行（含 CSV）
    ])
    from opensearch_pipeline import api
    resp = api.kb_visibility_explain(request=None, doc_id="D1", identity=api.Identity(user_id="da1"))
    assert resp.everyone is False and resp.nobody is False
    got = {(r.dept, r.via) for r in resp.readers}
    assert ("marketing", "owner") in got
    assert {("hr", "grant"), ("rd", "grant"), ("quality", "grant")} <= got
    assert resp.readers[0].dept == "marketing"            # 归属组排最前


def test_visibility_explain_production_subline_semantics(monkeypatch):
    """production_mold：读者 = production（伞组）+ marketing（共享面）——与
    retriever._expand_groups_to_owners 同源反查，绝无第二份规则。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [
        ("production_mold", "dept_internal", "active", 2),
        (None, None),
        [],
    ])
    from opensearch_pipeline import api
    resp = api.kb_visibility_explain(request=None, doc_id="D2", identity=api.Identity(user_id="adm1"))
    got = {(r.dept, r.via) for r in resp.readers}
    assert got == {("production", "umbrella"), ("marketing", "shared_policy")}


def test_visibility_explain_public_and_restricted(monkeypatch):
    """public → everyone；restricted → nobody（授权行即便存在也不外露）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [("hr", "public", "active", 1), (None, None)])
    from opensearch_pipeline import api
    r1 = api.kb_visibility_explain(request=None, doc_id="D3", identity=api.Identity(user_id="adm1"))
    assert r1.everyone is True and r1.readers == []
    _stub_multi(monkeypatch, [("hr", "restricted", "active", 1), (None, None)])
    r2 = api.kb_visibility_explain(request=None, doc_id="D4", identity=api.Identity(user_id="adm1"))
    assert r2.nobody is True and r2.readers == []


def test_visibility_explain_quarantined_shows_nobody(monkeypatch):
    """隔离件：nobody + quarantined 旗标（不在检索中，覆盖一切授权）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [
        ("hr", "dept_internal", "active", 1),
        ("QUARANTINED", "quarantined"),
    ])
    from opensearch_pipeline import api
    resp = api.kb_visibility_explain(request=None, doc_id="D5", identity=api.Identity(user_id="adm1"))
    assert resp.nobody is True and resp.quarantined is True and resp.readers == []


def test_visibility_explain_foreign_dept_forbidden(monkeypatch):
    """作用域：非归属部门管理员 403（授权清单不对只读浏览者外露）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "dept_internal", "active", 1)])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_visibility_explain(request=None, doc_id="D6", identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_grant_create_kb_admin_allowed(monkeypatch):
    """kb_admin 可对任意归属文档主动共享（_kb_can_manage 全权）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [("hr", "dept_internal", "active"), []])
    from opensearch_pipeline import api
    resp = api.kb_access_grant_create(api.KbAccessGrantCreate(doc_id="D1", target_depts=["marketing"]),
                                      request=None, identity=api.Identity(user_id="dev1"))
    assert resp.granted == ["marketing"]
    assert any("INSERT INTO fuling_knowledge.kb_access_request" in c[0] for c in sink["calls"])


# ── Phase F：成员/角色管理（kb_admin 专属）──
def test_admin_grants_list_kb_admin(monkeypatch):
    """kb_admin 列管理员名单 + 各自 managed_owner_depts + grantable 白名单。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [
        [("u1", "张三", "marketing", "dept_admin"), ("u2", "李四", "", "kb_admin")],  # user_role
        [("u1", "marketing"), ("u1", "finance")],                                     # dept_admin_grant
    ])
    from opensearch_pipeline import api
    resp = api.kb_admin_grants_list(request=None, identity=api.Identity(user_id="kbadmin"))
    by = {it.user_id: it for it in resp.items}
    assert by["u1"].role == "dept_admin" and by["u1"].managed_owner_depts == ["finance", "marketing"]
    assert by["u2"].role == "kb_admin" and by["u2"].managed_owner_depts == []
    assert "marketing" in resp.grantable_owner_depts and "finance" in resp.grantable_owner_depts


def test_admin_grants_dept_admin_forbidden(monkeypatch):
    """成员管理 = kb_admin 专属：dept_admin 调 → 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_admin_grants_list(request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_admin_grant_creates_dept_admin(monkeypatch):
    """kb_admin 授予 → upsert user_role=dept_admin + dept_admin_grant 行（净化后的组）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [None])   # 目标用户当前 role 查询 → 无行（新用户）
    from opensearch_pipeline import api
    resp = api.kb_admin_grant(
        api.KbAdminGrantRequest(user_id="newuser", user_name="王五", owner_depts=["marketing", "typo_dept", "finance"], note="营销+财务"),
        request=None, identity=api.Identity(user_id="kbadmin"))
    assert resp.ok and resp.role == "dept_admin"
    assert resp.managed_owner_depts == ["finance", "marketing"]            # typo_dept fail-closed 丢弃
    sqls = " ".join(c[0] for c in sink["calls"])
    assert "INSERT INTO fuling_knowledge.user_role" in sqls
    assert sqls.count("INSERT INTO fuling_knowledge.dept_admin_grant") == 2  # 2 个净化后的组


def test_admin_grant_invalid_depts_400(monkeypatch):
    """owner_depts 全非白名单 → 400（不写库）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_admin_grant(api.KbAdminGrantRequest(user_id="u9", owner_depts=["nope", "bad"]),
                           request=None, identity=api.Identity(user_id="kbadmin"))
    assert getattr(ei.value, "status_code", None) == 400


def test_admin_grant_self_forbidden(monkeypatch):
    """不能改自己的角色 → 400。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_admin_grant(api.KbAdminGrantRequest(user_id="kbadmin", owner_depts=["marketing"]),
                           request=None, identity=api.Identity(user_id="kbadmin"))
    assert getattr(ei.value, "status_code", None) == 400


def test_admin_grant_kb_admin_target_forbidden(monkeypatch):
    """目标已是 kb_admin → 拒绝（防误降级）400。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [("kb_admin",)])   # 目标当前 role=kb_admin
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_admin_grant(api.KbAdminGrantRequest(user_id="otherkb", owner_depts=["marketing"]),
                           request=None, identity=api.Identity(user_id="kbadmin"))
    assert getattr(ei.value, "status_code", None) == 400


def test_admin_revoke_all_demotes(monkeypatch):
    """撤销全部授权 → 软删 grant + 无剩余 → 降级 employee。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [("dept_admin",), (0,)])   # 目标 role；撤后剩余 0
    from opensearch_pipeline import api
    resp = api.kb_admin_grant_revoke(api.KbAdminRevokeRequest(user_id="u1"),
                                     request=None, identity=api.Identity(user_id="kbadmin"))
    assert resp.ok and resp.role == "employee"
    sqls = " ".join(c[0] for c in sink["calls"])
    assert "UPDATE fuling_knowledge.dept_admin_grant SET is_active=0" in sqls
    assert "SET role=%s" in sqls   # 降级 user_role


# ── 恢复上线 /api/kb/restore（退役逆操作）──
def test_version_history_retired_doc_badges(monkeypatch):
    """退役文档的版本历史：传入 doc 级状态后各版本徽章如实显「已退役」（B4），不再误显流水线态。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    ver_rows = [
        (2, "SUCCESS", "", "SUCCESS", "", "", "2026-06-20"),   # 退役前是「已上线」，doc 退役后应显已退役
        (1, "SUCCESS", "", "SUCCESS", "", "", "2026-06-10"),
    ]
    _stub_multi(monkeypatch, [("marketing", "retired"), ver_rows])   # meta(fetchone) + versions(fetchall)
    from opensearch_pipeline import api
    resp = api.kb_version_history(request=None, doc_id="D1", identity=api.Identity(user_id="kb1"))
    assert len(resp.versions) == 2
    assert all(v.status_badge == "已退役" for v in resp.versions)


def test_version_history_active_doc_pipeline_badge(monkeypatch):
    """对照：active 文档版本仍显流水线态（已上线），doc_status 传入不误伤。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [("marketing", "active"), [(1, "SUCCESS", "", "SUCCESS", "", "", "2026-06-10")]])
    from opensearch_pipeline import api
    resp = api.kb_version_history(request=None, doc_id="D1", identity=api.Identity(user_id="kb1"))
    assert resp.versions[0].status_badge == "已上线"


def test_retire_deactivates_all_versions(monkeypatch):
    """退役停用该文档【全部活跃版本】chunk（WHERE doc_id 不限 version_no）——闭合旧版本残留 is_active=1。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [("marketing", "dept_internal", "active", 3)])
    from opensearch_pipeline import api
    resp = api.kb_retire(api.KbRetireRequest(doc_id="D1"), request=None, identity=api.Identity(user_id="kb1"))
    assert resp.retired is True and resp.already is False
    chunk_upd = [c for c in sink["calls"] if "chunk_meta SET is_active=0" in c[0]]
    assert len(chunk_upd) == 1
    assert "version_no" not in chunk_upd[0][0]          # 不再限当前版本
    assert chunk_upd[0][1] == ("D1",)                    # 仅按 doc_id（全部活跃版本）
    # document_version 仍只退役当前版本（版本表语义保留）
    ver_upd = [c for c in sink["calls"] if "document_version SET status='retired'" in c[0]]
    assert ver_upd and ver_upd[0][1] == ("D1", 3)


def test_restore_reactivates_retired(monkeypatch):
    """退役文档恢复：status retired→active（meta+version）+ chunk is_active=1 + NOT_INDEXED。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [("marketing", "dept_internal", "retired", 2)])
    from opensearch_pipeline import api
    resp = api.kb_restore(api.KbRetireRequest(doc_id="D1"), request=None, identity=api.Identity(user_id="kb1"))
    assert resp.restored is True and resp.already is False
    sqls = " ".join(c[0] for c in sink["calls"])
    assert sqls.count("SET status='active'") == 2                      # document_meta + document_version
    assert "is_active=1, index_status='NOT_INDEXED'" in sqls           # chunk 重激活 + 标脏


def test_restore_already_active_idempotent(monkeypatch):
    """已在线 → 幂等（restored=False, already=True），不写。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [("marketing", "dept_internal", "active", 1)])
    from opensearch_pipeline import api
    resp = api.kb_restore(api.KbRetireRequest(doc_id="D1"), request=None, identity=api.Identity(user_id="kb1"))
    assert resp.already is True and resp.restored is False
    assert not [c for c in sink["calls"] if "SET status='active'" in c[0]]   # 幂等不写


def test_restore_public_needs_kb_admin(monkeypatch):
    """公开文档 dept_admin 不可恢复（与退役同款不对称）→ 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("marketing", "public", "retired", 1)])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_restore(api.KbRetireRequest(doc_id="D1"), request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_restore_scope_forbidden(monkeypatch):
    """dept_admin 非管理部门 → 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "dept_internal", "retired", 1)])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_restore(api.KbRetireRequest(doc_id="D1"), request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def _stub_myreq(monkeypatch, request_rows, doc_state):
    """桩游标（按 SQL 片段分支）：主列表 fetchall 返回 request_rows；per-doc count(fetchone) +
    allowed_depts(fetchall) 由 doc_state 提供。用于验 /api/kb/my-access-requests 派生同步态。"""
    import json

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
                if al == "__BAD_JSON__":
                    return [("{not valid json",)]   # 触发 current_allowed_for_doc 的 json.loads 抛错（#7）
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

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())


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


def test_my_access_requests_bad_row_degrades_not_500(monkeypatch):
    """#7 防御：单行脏 allowed_depts JSON → 绝不 500 整张列表（两行都在）。

    注：access_grants.current_allowed_for_doc 现在对单行坏 JSON 是【跳过坏行+告警、不再 raise】
    （fail-closed-trio P2 修复）——所以坏 doc 的 current 派生不含被跳过的 chunk → projected=False
    → 显 'pending_sync'（对账会重投影自愈），而非旧的 raise→端点 except→'n/a'。端点的 n/a 降级
    路径仍服务于真正的 DB 异常。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    rows = [
        ("1", "DBAD", "T坏", "marketing", "finance", "approved", "r", "2026-06-01", "2026-06-02", 1),
        ("2", "DOK", "T好", "marketing", "quality", "approved", "r", "2026-06-01", "2026-06-02", 1),
    ]
    doc_state = {
        "DBAD": {"cnt": 1, "indexed": 1, "allowed": "__BAD_JSON__"},   # 脏 JSON → 坏 chunk 被跳过
        "DOK": {"cnt": 1, "indexed": 1, "allowed": ["quality"]},       # 正常 → projected
    }
    _stub_myreq(monkeypatch, rows, doc_state)
    from opensearch_pipeline import api
    resp = api.kb_my_access_requests(request=None, identity=api.Identity(user_id="da1"))  # 不抛 500
    by_id = {it.id: it.sync_state for it in resp.items}
    assert len(resp.items) == 2                  # 坏行未吞掉整张表，两行都在（#7 核心：不 500）
    assert by_id["1"] == "pending_sync"          # 坏 chunk 被跳过 → 未投影 → 待对账重投影自愈
    assert by_id["2"] == "projected"             # 好行不受影响


# ── /api/kb/insights 使用成效 + 知识缺口（Phase E）──────────────────────────────
def test_insights_employee_forbidden_before_db(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:   # 管理员特权：employee 在任何 DB 查询前 403
        api.kb_insights(request=None, identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_insights_anonymous_unauthorized(monkeypatch):
    _skip_if_not_sim()
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_insights(request=None, identity=None)
    assert getattr(ei.value, "status_code", None) == 401


def test_insights_dept_admin_scope_and_collation(monkeypatch):
    """dept_admin：作用域收窄到本部门 + JSON→doc_id collation-cast（1267 防御）+ 参数化。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [])   # 空桩：各子查询取数为空 → 安全默认，不 500
    from opensearch_pipeline import api
    resp = api.kb_insights(request=None, identity=api.Identity(user_id="da1"))
    assert resp.scope == "dept"
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "m.owner_dept IN" in sqls                              # 作用域收窄
    assert "COLLATE utf8mb4_unicode_ci" in sqls                   # 1267 防御：collation-cast
    assert "JSON_TABLE" in sqls                                   # 经 retrieved_docs_json 归属
    assert any(p and "marketing" in p for _, p in sink["calls"])  # 部门参数化
    assert "AND 1=0" not in sqls                                  # 有授权 → 非空集


def test_insights_kb_admin_unscoped_global(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [])
    from opensearch_pipeline import api
    resp = api.kb_insights(request=None, identity=api.Identity(user_id="dev1"))
    assert resp.scope == "global"
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "m.owner_dept IN" not in sqls                          # kb_admin 不限作用域
    assert "AND 1=0" not in sqls


def test_insights_dept_admin_no_managed_fail_closed(monkeypatch):
    """无授权 dept_admin → 作用域 1=0 空集，绝不静默当全量。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "")
    sink = _stub_multi(monkeypatch, [])
    from opensearch_pipeline import api
    api.kb_insights(request=None, identity=api.Identity(user_id="da0"))
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "AND 1=0" in sqls


def test_insights_gap_queries_pii_redacted(monkeypatch):
    """知识缺口跨用户展示：gap_queries 是【他人】原始提问，必须 PII 脱敏（与 /api/kb/gaps 一致）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    raw = "我的手机号13800138000怎么报销"
    # fetch 顺序：usage(fetchone) → cited(fetchone: 提问数, 被帮到的用户数) → top_docs(fetchall) → gap_queries(fetchall)
    _stub_multi(monkeypatch, [(10, 5, 8, 2), (3, 2), [], [(raw, 2, 7.5)]])
    from opensearch_pipeline import api
    resp = api.kb_insights(request=None, identity=api.Identity(user_id="dev1"))
    assert resp.cited == 3 and resp.helped_users == 2   # 同一 cited JOIN：提问数 vs 去重用户数
    assert resp.gap_queries, "应有一条缺口"
    q = resp.gap_queries[0].query
    assert "13800138000" not in q     # 他人手机号不外泄
    assert q != raw                   # 已脱敏


# ── /api/kb/governance 全库治理（Phase E，仅 kb_admin）──────────────────────────
def test_governance_dept_admin_forbidden(monkeypatch):
    """治理看板是 kb_admin 专属：dept_admin（含写授权）也 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_governance(request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_governance_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_governance(request=None, identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_governance_kb_admin_shape_and_queries(monkeypatch):
    """kb_admin：空桩 → 安全默认不 500；关键治理查询都在（PII/反馈/延迟分位）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [])
    from opensearch_pipeline import api
    resp = api.kb_governance(request=None, identity=api.Identity(user_id="dev1"))
    assert resp.window_days == 30
    assert resp.docs_active == 0 and resp.dept_coverage == []     # 空桩降级，不 500
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "document_sensitive_finding" in sqls                   # PII 风险
    assert "user_feedback" in sqls                                # 反馈好评率
    assert "PERCENT_RANK()" in sqls                               # 延迟 p50/p95
    assert "escalation_ticket" in sqls                            # 转人工
    # 嵌入失败率两列都判非空（NULL 失败数绝不当 0% 完美率）
    assert "embedding_failed_chunks IS NOT NULL" in sqls


def _stub_all_fail(monkeypatch):
    """桩游标：每条 execute 都抛 → 验「全部子查询失败 → 诚实 500」而非 all-zeros 200。"""

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            raise RuntimeError("simulated DB fault")

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())


def test_insights_all_queries_fail_raises_500(monkeypatch):
    """连接成功但所有子查询失败（DB mid-batch gone-away）→ 诚实 500，不伪装 all-zeros 无数据。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_all_fail(monkeypatch)
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_insights(request=None, identity=api.Identity(user_id="dev1"))
    assert getattr(ei.value, "status_code", None) == 500


def test_governance_all_queries_fail_raises_500(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_all_fail(monkeypatch)
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_governance(request=None, identity=api.Identity(user_id="dev1"))
    assert getattr(ei.value, "status_code", None) == 500


def test_insights_partial_failure_degrades_not_500(monkeypatch):
    """部分子查询失败（首条成功、其余抛）→ 不 500：已取到的指标照常，未取到的诚实空。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")

    class _Cur:
        def __init__(self):
            self.n = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.n += 1
            if self.n > 1:                       # 仅第一条（使用聚合）成功，其余抛
                raise RuntimeError("simulated partial fault")

        def fetchone(self):
            return (12, 5, 9, 3)                  # questions/askers/success/refusal

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
    from opensearch_pipeline import api
    resp = api.kb_insights(request=None, identity=api.Identity(user_id="dev1"))   # 不抛 500
    assert resp.questions == 12 and resp.success == 9    # 成功子查询的真实指标保留
    assert resp.top_docs == [] and resp.gap_queries == []  # 失败子查询诚实空


# ── /api/kb/approval-history 审批历史（只读聚合，四流合并时间线）───────────────────
def test_approval_history_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:   # 管理员特权：employee 在任何 DB 查询前 403
        api.kb_approval_history(request=None, identity=api.Identity(user_id="e1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_approval_history_anonymous_unauthorized(monkeypatch):
    _skip_if_not_sim()
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_approval_history(request=None, identity=None)
    assert getattr(ei.value, "status_code", None) == 401


def test_approval_history_dept_admin_scope(monkeypatch):
    """dept_admin：只跑 access+contribution，两者均 owner/category 作用域收窄；不碰 kb_audit_log。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    # fetch 顺序（全 fetchall）：access → contribution → user_role(操作者名)
    access = [("D1", "销售SOP", "marketing", "production", "王伟", "approved", "引用", "", "da1", "2026-06-28 14:00:00")]
    contrib = [("2ozpp杯速度", "marketing", "孙工", "accepted", "", "searchable", "mgr2", "2026-06-27 09:00:00")]
    names = [("da1", "李娜"), ("mgr2", "陈立")]
    sink = _stub_multi(monkeypatch, [access, contrib, names])
    from opensearch_pipeline import api
    resp = api.kb_approval_history(request=None, identity=api.Identity(user_id="da1"))
    assert len(resp.items) == 2
    assert {it.kind for it in resp.items} == {"access", "contribution"}
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "owner_dept IN" in sqls and "category_dept IN" in sqls   # 双作用域收窄
    assert "kb_audit_log" not in sqls                               # dept_admin 不查上传/成员授权
    assert "status IN" in sqls and "review_status IN" in sqls       # 只取已决行
    acc = next(it for it in resp.items if it.kind == "access")
    assert acc.action == "approved" and acc.subject == "王伟" and acc.decided_by_name == "李娜"


def test_approval_history_kb_admin_all_sources(monkeypatch):
    """kb_admin：四源都跑（含 kb_audit_log 上传 APPROVE + 成员 KB_ADMIN_GRANT），无作用域收窄。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    access = [("D1", "销售SOP", "marketing", "production", "王伟", "revoked", "", "到期收回", "kb1", "2026-06-28 14:00:00")]
    contrib = [("请假加急", "hr", "周敏", "rejected", "与制度冲突", "", "mgr3", "2026-06-26 16:00:00")]
    upload = [("D9", "安全规程v4", "production", "APPROVE", "kb1", "2026-06-28 11:00:00", "")]
    admin = [("KB_ADMIN_GRANT", "kb1", "2026-06-25 09:00:00", "grant dept_admin mgr002 → quality,production")]
    names = [("kb1", "系统管理员"), ("mgr3", "陈立")]
    sink = _stub_multi(monkeypatch, [access, contrib, upload, admin, names])
    from opensearch_pipeline import api
    resp = api.kb_approval_history(request=None, identity=api.Identity(user_id="dev1"))
    assert {it.kind for it in resp.items} == {"access", "contribution", "upload", "admin_grant"}
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "kb_audit_log" in sqls and "APPROVE" in sqls and "KB_ADMIN_GRANT" in sqls
    assert "owner_dept IN (" not in sqls                            # kb_admin 不收窄
    ag = next(it for it in resp.items if it.kind == "admin_grant")
    assert ag.title == "mgr002" and ag.action == "granted"          # message 解析出目标 uid
    assert next(it for it in resp.items if it.kind == "upload").action == "approved"
    # 合并按 decided_at 倒序：最新 access(28 14:00) 在首
    assert resp.items[0].kind == "access"


def test_approval_history_pii_redacted(monkeypatch):
    """跨用户自由文本（申请理由 / 贡献问题）必须脱敏，不泄露他人手机号。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    access = [("D1", "报销SOP", "finance", "hr", "王伟", "approved", "我手机13800138000报销用", "", "kb1", "2026-06-28 10:00:00")]
    contrib = [("我的手机号13900139000怎么改", "hr", "周敏", "rejected", "", "", "kb1", "2026-06-27 10:00:00")]
    names = [("kb1", "系统管理员")]
    _stub_multi(monkeypatch, [access, contrib, [], [], names])   # upload/admin 空
    from opensearch_pipeline import api
    resp = api.kb_approval_history(request=None, identity=api.Identity(user_id="dev1"))
    blob = " ".join((it.title + " " + it.detail) for it in resp.items)
    assert "13800138000" not in blob and "13900139000" not in blob


def test_approval_history_all_queries_fail_raises_500(monkeypatch):
    """连接成功但所有子查询失败 → 诚实 500，不伪装 all-empty 无历史。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_all_fail(monkeypatch)
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_approval_history(request=None, identity=api.Identity(user_id="dev1"))
    assert getattr(ei.value, "status_code", None) == 500


def test_approval_history_partial_degrades_not_500(monkeypatch):
    """部分子查询失败（access 成功、其余抛）→ 不 500：已取到的照常，操作者名回退 uid。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    access = [("D1", "销售SOP", "marketing", "production", "王伟", "approved", "引用", "", "kb1", "2026-06-28 14:00:00")]

    class _Cur:
        def __init__(self):
            self.n = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.n += 1
            if self.n > 1:                       # 仅第一条（access）成功，其余抛
                raise RuntimeError("simulated partial fault")

        def fetchall(self):
            return access                        # 仅 access execute 后被调用一次

        def fetchone(self):
            return None

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
    from opensearch_pipeline import api
    resp = api.kb_approval_history(request=None, identity=api.Identity(user_id="dev1"))   # 不抛 500
    assert [it.kind for it in resp.items] == ["access"]
    assert resp.items[0].decided_by_name == "kb1"    # names 查询也失败 → 回退 uid


# ═══════════════════════════════════════════════════════════════════════════════
# #7 台账筛选/计数下推服务端 —— 徽章 CASE 奇偶校验 + my-docs 结构化筛选 + stats facet
# ═══════════════════════════════════════════════════════════════════════════════
def test_kb_badge_case_sql_parity():
    """_KB_BADGE_CASE_SQL 必须与 _kb_status_badge 同序同义（结构 + 优先级）。
    守卫：改 _kb_status_badge 的判定顺序/取值时忘同步 SQL 镜像 → 这里红。"""
    from opensearch_pipeline import api
    sql = api._KB_BADGE_CASE_SQL
    b = api._kb_status_badge
    # 1) list 路径（chunk_active=None）代表性输入 → Python 徽章必出现在 CASE 里
    samples = [
        b("DONE", "SUCCESS", "retired"),                       # 已退役
        b("DONE", "SUCCESS", "active", None, "QUARANTINED"),   # 已隔离
        b("DONE", None, "active", None, None, "EMPTY"),        # 未入索引
        b("DONE", None, "active", None, "SKIPPED_EMPTY"),      # 未入索引（SKIPPED 前缀）
        b("DONE", "SUCCESS", "active"),                        # 已上线
        b("FAILED", None, "active"),                           # 处理失败
        b("REJECTED", None, "active"),                         # 已驳回
        b("SKIPPED_DUPLICATE", None, "active"),                # 内容未变
        b("PENDING_APPROVAL", None, "active"),                 # 待审核
        b("NOT_STARTED", None, "active"),                      # 排队中
        b("PROCESSING", None, "active"),                       # 处理中（默认）
    ]
    for badge in samples:
        assert f"'{badge}'" in sql, f"徽章 {badge} 未出现在 CASE 里"
    # 2) 优先级顺序：CASE 里各徽章首次出现的位置必须与 Python if 阶梯同序
    order = ["已退役", "已隔离", "未入索引", "已上线", "处理失败", "已驳回", "内容未变", "待审核", "排队中", "处理中"]
    positions = [sql.index(f"'{x}'") for x in order]
    assert positions == sorted(positions), "CASE 徽章顺序与 _kb_status_badge 优先级阶梯不一致"
    # 3) SKIPPED 前缀不得用带字面 % 的 LIKE（pymysql 参数化坑）；用 LEFT(...)='SKIPPED'
    assert "LIKE 'SKIPPED%'" not in sql and "LEFT(" in sql


def test_my_docs_badge_filter_uses_case(monkeypatch):
    """badge 参数 → WHERE 拼 CASE 徽章判定 + 该徽章作为参数（服务端筛选，覆盖全库）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    api.kb_my_docs(request=None, limit=20, offset=0, badge="未入索引", identity=api.Identity(user_id="dev1"))
    assert "CASE" in sink["sql"] and "END) = %s" in sink["sql"]
    assert "未入索引" in sink["params"]          # 徽章值作为参数
    assert sink["params"][-2:] == (21, 0)       # limit+1, offset 仍在末尾


def test_my_docs_owner_perm_cited_filters(monkeypatch):
    """owner_dept + perm + cited 全服务端；参数顺序 = 归属 → 可见范围 →（cited 无参）→ limit/offset。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    api.kb_my_docs(request=None, limit=20, offset=0, owner_dept="production_mold", perm="public",
                   identity=api.Identity(user_id="dev1"))
    assert "m.owner_dept = %s" in sink["sql"] and "m.permission_level = %s" in sink["sql"]
    assert sink["params"][0] == "production_mold" and sink["params"][1] == "public"
    assert sink["params"][-2:] == (21, 0)


def test_my_docs_no_filters_params_unchanged(monkeypatch):
    """无任何筛选 → 参数仍是 (21, 0)（不因新增筛选形参回归）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    api.kb_my_docs(request=None, limit=20, offset=0, identity=api.Identity(user_id="dev1"))
    assert sink["params"] == (21, 0)
    assert "CASE" not in sink["sql"] and "permission_level = %s" not in sink["sql"]


def test_my_docs_invalid_owner_facet_fail_closed(monkeypatch):
    """归属 facet 清洗后为空（纯注入字符）→ fail-closed 空，不落库。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_capture(monkeypatch)
    from opensearch_pipeline import api
    resp = api.kb_my_docs(request=None, limit=20, offset=0, owner_dept="'; DROP--", identity=api.Identity(user_id="dev1"))
    assert resp.items == [] and resp.has_more is False


def test_stats_owner_depts_facet_and_chunk_status_badge(monkeypatch):
    """stats 返回全作用域 owner_depts（去重排序）；0-chunk 文档经 chunk_status 归『未入索引』。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    # 主查询 6 列：status, cps, ixs, pubs, chunk_status, owner_dept
    rows = [
        ("active", "DONE", "SUCCESS", None, "DONE", "marketing"),      # 已上线
        ("active", "DONE", None, "active", "EMPTY", "production"),     # 0-chunk → 未入索引
        ("active", "DONE", "SUCCESS", None, "DONE", "hr"),            # 已上线
    ]
    _stub_multi(monkeypatch, [rows, (7,), (2,)])   # 主 fetchall + chunks fetchone + new_month fetchone
    from opensearch_pipeline import api
    resp = api.kb_stats(request=None, identity=api.Identity(user_id="dev1"))
    assert resp.owner_depts == ["hr", "marketing", "production"]      # 去重 + 排序
    assert resp.by_badge.get("未入索引") == 1                          # chunk_status 生效（此前漏传会误记「处理中」）
    assert resp.by_badge.get("已上线") == 2
    assert resp.chunks == 7 and resp.new_this_month == 2


# ═══════════════════════════════════════════════════════════════════════════════
# #1 差评处置：POST /api/kb/feedback-review/resolve
# ═══════════════════════════════════════════════════════════════════════════════
def test_feedback_resolve_kb_admin_updates(monkeypatch):
    """kb_admin resolve → UPDATE handled_status='RESOLVED' + handled_by=uid，按 message_id 覆盖。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [])   # kb_admin 跳过作用域校验；UPDATE execute 返回 None 无妨
    from opensearch_pipeline import api
    r = api.kb_feedback_resolve(api.KbFeedbackResolveRequest(message_id="M9", action="resolve"),
                                request=None, identity=api.Identity(user_id="adm1"))
    assert r["handled_status"] == "RESOLVED" and sink.get("committed")
    assert "SET handled_status=%s" in sink["sql"]
    assert sink["params"][0] == "RESOLVED" and sink["params"][1] == "adm1" and sink["params"][2] == "M9"


def test_feedback_resolve_reopen_sets_pending(monkeypatch):
    """reopen → handled_status='PENDING'（撤销处置）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [])
    from opensearch_pipeline import api
    r = api.kb_feedback_resolve(api.KbFeedbackResolveRequest(message_id="M9", action="reopen"),
                                request=None, identity=api.Identity(user_id="adm1"))
    assert r["handled_status"] == "PENDING"


def test_feedback_resolve_dept_admin_scope_guard(monkeypatch):
    """dept_admin：目标差评不涉本部门文档（作用域 SELECT 无命中）→ 403，不 UPDATE。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [None])   # 作用域 SELECT 1 → 无命中
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_feedback_resolve(api.KbFeedbackResolveRequest(message_id="M9", action="resolve"),
                                request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403
    assert sink.get("rolledback") and not sink.get("committed")
    assert "owner_dept IN" in sink["sql"]   # 作用域进了校验 SQL


def test_feedback_resolve_dept_admin_in_scope_ok(monkeypatch):
    """dept_admin：目标差评涉本部门文档（作用域命中）→ 放行 UPDATE。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [(1,)])   # 作用域 SELECT 1 → 命中
    from opensearch_pipeline import api
    r = api.kb_feedback_resolve(api.KbFeedbackResolveRequest(message_id="M9", action="dismiss"),
                                request=None, identity=api.Identity(user_id="da1"))
    assert r["handled_status"] == "DISMISSED" and sink.get("committed")


def test_feedback_resolve_missing_message_id_400(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_feedback_resolve(api.KbFeedbackResolveRequest(message_id="", action="resolve"),
                                request=None, identity=api.Identity(user_id="adm1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_feedback_resolve_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_feedback_resolve(api.KbFeedbackResolveRequest(message_id="M9", action="resolve"),
                                request=None, identity=api.Identity(user_id="e1"))
    assert getattr(ei.value, "status_code", None) == 403


# ═══════════════════════════════════════════════════════════════════════════════
# #5 审批内容预览：GET /api/kb/doc-preview
# ═══════════════════════════════════════════════════════════════════════════════
def test_doc_preview_signs_raw_key(monkeypatch):
    """kb_admin：有 raw_key → 返回签名 GET URL（available=True）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [("marketing", "报价单.pdf", "raw/marketing/D1/v2/报价单.pdf", 2)])
    monkeypatch.setattr("opensearch_pipeline.oss_url.generate_signed_url",
                        lambda key, expires=None, method="GET": f"https://oss.example/{key}?sig=x")
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D1", version=2, identity=api.Identity(user_id="adm1"))
    assert resp.available is True and resp.url.startswith("https://oss.example/")
    assert resp.version_no == 2 and resp.filename == "报价单.pdf"
    assert resp.content_type and resp.expires_in == 300


def test_doc_preview_no_raw_key_unavailable(monkeypatch):
    """raw_key 缺失 → available=False，url 空（前端如实提示不可预览）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [("marketing", "老文档.pdf", None, 1)])
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D1", identity=api.Identity(user_id="adm1"))
    assert resp.available is False and resp.url == ""


def test_doc_preview_dept_admin_foreign_403(monkeypatch):
    """dept_admin：他部门文档原件不外露（授权先于任何 URL 生成）→ 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "考勤.pdf", "raw/hr/D9/v1/考勤.pdf", 1)])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_doc_preview(request=None, doc_id="D9", identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_doc_preview_missing_404(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [None])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_doc_preview(request=None, doc_id="ZZZ", identity=api.Identity(user_id="adm1"))
    assert getattr(ei.value, "status_code", None) == 404


def test_doc_preview_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_doc_preview(request=None, doc_id="D1", identity=api.Identity(user_id="e1"))
    assert getattr(ei.value, "status_code", None) == 403
