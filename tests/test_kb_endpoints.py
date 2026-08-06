# -*- coding: utf-8 -*-
"""
test_kb_endpoints.py — Phase 0 知识库只读接口的【授权先行】行为（不依赖 DB）。

直接调用 api 的端点函数（request=None），验证：org-tree 在 kb_admin 下返回全量、
employee/匿名在任何 DB 查询【之前】被 401/403 拒绝。授权走 resolve_kb_identity（simulate
从 RAG_SIM_USER_ROLE 取），证明令牌 role 提示不是边界、DB 现查才是。
"""
import pytest


@pytest.fixture(autouse=True)
def _default_node_capability_absent(monkeypatch):
    """阶段 B：本文件全部端点测试默认 capability='absent'。

    两个原因：①本文件的桩游标（_stub_multi/_stub_capture）按 execute 次数弹结果，
    真探针的额外一次 information_schema 查询会让整个序列错位；②absent 生成与旧
    _kb_owner_scope_sql 逐字节同构的 SQL（tests/test_kb_doc_scope.py 的回归锚钉死），
    故本文件既有 legacy 断言语义完全不变。node 路径的测试**显式**改打 'present'
    并在桩行里自带 acl_mode/owner_dept_id 列。
    ⚠️ kb_console/kb_access 都是 from-import 绑定——三个命名空间都要打。"""
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_access, kb_console
    monkeypatch.setattr(api, "_kb_node_capability", lambda cur: "absent")
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "absent")
    monkeypatch.setattr(kb_access, "_kb_node_capability", lambda cur: "absent")
    # C8（2026-08-04）：schema/064 的能力探测同理默认 absent —— 同一个理由（真探针会多发一次
    # information_schema 查询，把按次数弹结果的桩游标整体错位）。absent ⇒ 生成与改动前
    # **逐字节相同**的 SQL/INSERT 列清单，本文件既有断言语义不变。
    # 需要覆盖绑定分支的用例请**显式**打成 True 并在桩行末尾自带 raw_version_id/content_binding_mode。
    monkeypatch.setattr(kb_console, "_kb_content_binding_columns", lambda cur: False)


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
        ("D1", "营销规范", "a.pdf", "marketing", "dept_internal", 1, "active", "2026-06-26", "DONE", "SUCCESS", None, "DONE", None),
        ("D2", "HR 手册", "b.pdf", "hr", "dept_internal", 2, "active", "2026-06-25", "DONE", "SUCCESS", None, "DONE", None),
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
    rows = [("D1", "x", "a.pdf", "hr", "dept_internal", 1, "active", "t", "DONE", "SUCCESS", None, "DONE", None)]
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
        ("D1", "t1", "a.pdf", "hr", "dept_internal", 1, "active", "ts", "DONE", "SUCCESS", None, "DONE", None, None),
        ("D2", "t2", "b.pdf", "hr", "dept_internal", 1, "active", "ts", "DONE", "SUCCESS", None, "DONE", None, None),
    ]
    _stub_multi(monkeypatch, [[], docrows, [("D1", 5, "2026-07-01 10:00:00")]])   # 首个 []=faceted 计数查询
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
    docrows = [("D1", "t1", "a.pdf", "hr", "dept_internal", 1, "active", "ts", "DONE", "SUCCESS", None, "DONE", None, None)]
    _stub_multi(monkeypatch, [[], docrows])   # 首个 []=faceted 计数查询
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
        ("D1", "t1", "a.pdf", "hr", "dept_internal", 1, "active", "ts", "REJECTED", None, None, "DONE", "内容过期，已被 v3 取代", None),
        ("D2", "t2", "b.pdf", "hr", "dept_internal", 1, "active", "ts", "FAILED", "FAILED", None, "DONE", "OCR timeout traceback…", None),
    ]
    _stub_multi(monkeypatch, [[], docrows])   # 首个 []=faceted 计数查询
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
    # 占位符个数**跟随** _KB_BAD_BADGES，不写死 4 个 —— 写死等于每加一个合法异常徽章
    # （如 C8 的「内容不符」）就红一次，而本条要守的是「IN 集合与该常量同口径」。
    assert f"IN ({','.join(['%s'] * len(_KB_BAD_BADGES))})" in sql
    assert list(params) == list(_KB_BAD_BADGES)
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


def test_feedback_review_sql_columns_exist_in_authoritative_ddl(monkeypatch):
    """SQL 列名契约（2026-07-11 staging P1 回归）：桩游标不校验列名——本端点曾 SELECT
    qa_session_log 不存在的 q.question（实列 query_text），单测全绿、staging/prod 真库
    pymysql 1054 必 500 且前端区块静默隐藏。这里把端点在两种 JOIN 形态下实际拼出的
    SQL 里 q./f./m./jt. 引用的每一列，钉死在对应表的权威 DDL（schema/ 跨文件联合集）上。"""
    _skip_if_not_sim()
    import re as _re
    from tests.test_schema_ddl_parity import _table_columns_across_schema
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    # 缓存旁路：本测试的断言对象是「真拼出的 SQL」，不能被同 key 的兄弟测试缓存喂饱
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    from opensearch_pipeline import api
    for fact_on in (False, True):   # JSON_TABLE 回退 / qa_retrieved_doc 事实表 两种形态都锁
        monkeypatch.setattr("opensearch_pipeline.qa_facts.fact_join_enabled",
                            lambda v=fact_on: v)
        sink = _stub_multi(monkeypatch, [[]])
        api.kb_feedback_review(request=None, limit=20, identity=api.Identity(user_id="adm1"))
        sql = sink["sql"]
        checks = {"q": "qa_session_log", "f": "user_feedback", "m": "document_meta"}
        if fact_on:
            checks["jt"] = "qa_retrieved_doc"   # 回退形态的 jt 是 JSON_TABLE 派生列，不查
        for alias, table in checks.items():
            refs = set(_re.findall(rf"\b{alias}\.([a-z_][a-z0-9_]*)", sql))
            assert refs, f"SQL 里没有 {alias}. 引用——查询形状变了，请同步本契约测试"
            missing = refs - _table_columns_across_schema(table)
            assert not missing, (f"{table}（别名 {alias}）引用了权威 DDL 不存在的列："
                                 f"{sorted(missing)}——真库必 1054，先对齐 schema/ 再改代码")
        assert "q.query_text" in sql   # 问题文本必须取自 query_text（本 bug 的直接钉子）


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


def test_admin_grant_audit_message_masks_staff_id(monkeypatch):
    """审计 message 里 staffId 首4…尾4掩码：完整 16-19 位数字会撞展示侧 bank_card 规则,
    approval-history 整段变「[银行卡号已脱敏]」（2026-08-04 现网 26 条 grant 全中）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [None])   # 目标用户当前 role 查询 → 无行
    from opensearch_pipeline import api
    resp = api.kb_admin_grant(
        api.KbAdminGrantRequest(user_id="999900001111222233", owner_depts=["marketing"]),
        request=None, identity=api.Identity(user_id="kbadmin"))
    assert resp.ok
    audit_sql, audit_params = next(c for c in sink["calls"] if "kb_audit_log" in c[0])
    msg = audit_params[8]   # _audit_params 元组序：message 恒在末位
    assert "9999…2233" in msg and "999900001111222233" not in msg
    assert msg.startswith("[acl_policy=")   # 掩码不影响 ACL 盖戳


def test_parse_admin_target_stamped_and_legacy():
    """_parse_admin_target 各代格式：盖戳+掩码（新端点写）/legacy 未盖戳短 id（测试桩）/
    revoke/存量未掩码行（盖戳与 seed 括注两形态）——完整 staffId 一律兜底成首4…尾4。"""
    from opensearch_pipeline.routes.kb_access import _parse_admin_target
    assert _parse_admin_target(
        "[acl_policy=ab12cd34] grant dept_admin 9999…2233 → depts=marketing nodes=unchanged") == "9999…2233"
    assert _parse_admin_target("grant dept_admin mgr002 → quality,production") == "mgr002"
    assert _parse_admin_target(
        "[acl_policy=ab12cd34] revoke 9999…2233 owner=- node=5 demoted=False") == "9999…2233"
    # 存量未掩码行（写侧掩码上线前）：完整 staffId 不得进 title/subject
    assert _parse_admin_target(
        "[acl_policy=ab12cd34] grant dept_admin 999900001111222233 → depts=marketing "
        "nodes=unchanged") == "9999…2233"
    assert _parse_admin_target(
        "grant dept_admin 888800001111222277(张三) → nodes=149975081(国内营销部) "
        "via seed_20260804") == "8888…2277(张三)"
    assert _parse_admin_target("") == ""


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
        (2, "SUCCESS", "", "SUCCESS", "", "", 1, "", "2026-06-20"),   # 退役前是「已上线」，doc 退役后应显已退役
        (1, "SUCCESS", "", "SUCCESS", "", "", 1, "", "2026-06-10"),
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
    _stub_multi(monkeypatch, [("marketing", "active"), [(1, "SUCCESS", "", "SUCCESS", "", "", 1, "", "2026-06-10")]])
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
    # P2-1（盲区审计）：退役必须喂 PENDING_DELETE outbox（全版本），否则 HA3 向量永久可检索
    pd = [c for c in sink["calls"] if "SET index_status='PENDING_DELETE'" in c[0]]
    assert len(pd) == 1 and pd[0][1] == ("D1",)
    assert "NOT IN ('DELETED', 'PENDING_DELETE')" in pd[0][0]


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
    # P2-1 对称撤销：退役挂上的 PENDING_DELETE/DELETED 拨回 NOT_INDEXED（仅当前版本），
    # 否则下轮 reconcile 删 HA3 恰好撤销这次恢复
    rv = [c for c in sink["calls"] if "document_version" in c[0] and "SET index_status='NOT_INDEXED'" in c[0]]
    assert len(rv) == 1 and rv[0][1] == ("D1", 2)
    assert "IN ('PENDING_DELETE', 'DELETED')" in rv[0][0]


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


def test_insights_top_docs_owner_axis_stable_keys(monkeypatch):
    """★ top_docs 归属轴与 dept_coverage 同稳定键（看板重设计 2026-08-03）：
    absent=组码 + 'unknown' 兜底（不得引用 owner_dept_id——旧 schema 无该列）；
    present=node:<id>，且 SELECT/GROUP BY 同一表达式；半迁移空串/双 NULL 归 'unknown'。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    # 同测内两次调用：绕开 60s 看板响应缓存（否则第二次不打 SQL）
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)

    sink = _stub_multi(monkeypatch, [])            # absent（本文件 autouse 默认）
    api.kb_insights(request=None, identity=api.Identity(user_id="dev1"))
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "COALESCE(NULLIF(m.owner_dept, ''), 'unknown')" in sqls
    assert "owner_dept_id" not in sqls

    for ns in (api, kb_console):
        monkeypatch.setattr(ns, "_kb_node_capability", lambda cur: "present")
    sink2 = _stub_multi(monkeypatch, [])
    api.kb_insights(request=None, identity=api.Identity(user_id="dev1"))
    joined = " || ".join(s for s, _ in sink2["calls"])
    expr = "COALESCE(NULLIF(m.owner_dept, ''), CONCAT('node:', m.owner_dept_id), 'unknown')"
    assert joined.count(expr) >= 2                 # SELECT 与 GROUP BY 同源


# ── /api/kb/feedback-stats 按部门筛选（2026-08-03，codex 两轮共识）───────────────
def _fb_stats_bypass_cache(monkeypatch):
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)


def test_feedback_stats_legacy_key_absent_schema(monkeypatch):
    """absent：legacy 走旧式 owner_dept IN（不引用 acl_mode/owner_dept_id）；伞形子线随入；
    分母 answer_total 从 qa_session_log 起查（绝不从反馈表来）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _fb_stats_bypass_cache(monkeypatch)
    from opensearch_pipeline import api
    sink = _stub_multi(monkeypatch, [(5,), (2, 1, 3, 1), [], []])
    resp = api.kb_feedback_stats(owner_key="legacy:production", request=None,
                                 identity=api.Identity(user_id="dev1"))
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "m.owner_dept IN" in sqls
    assert "owner_dept_id" not in sqls and "acl_mode" not in sqls
    assert "FROM" in sqls and "qa_session_log q" in sqls          # 分母独立起查
    assert any(p and "production_straw" in p for _, p in sink["calls"])   # 伞形展开
    assert resp.answer_total == 5 and resp.up == 2 and resp.down == 1 and resp.last7 == 1


def test_feedback_stats_node_key_absent_400_and_bad_key_400(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _fb_stats_bypass_cache(monkeypatch)
    from opensearch_pipeline import api
    _stub_multi(monkeypatch, [])
    for bad in ("node:3", "car:1", "node:0", ""):
        with pytest.raises(Exception) as ei:
            api.kb_feedback_stats(owner_key=bad, request=None, identity=api.Identity(user_id="dev1"))
        assert getattr(ei.value, "status_code", None) == 400, bad


def test_feedback_stats_node_key_present_descendants(monkeypatch):
    """present：node key → acl_mode='node' + 后代集 IN（与 _kb_managed_descendants 同源）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _fb_stats_bypass_cache(monkeypatch)
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    import opensearch_pipeline.dept_ancestry as _da
    import opensearch_pipeline.org_sync as _os
    for ns in (api, kb_console):
        monkeypatch.setattr(ns, "_kb_node_capability", lambda cur: "present")
    monkeypatch.setattr(_os, "load_children_index", lambda: (1, True, {3: [4], 4: []}))
    monkeypatch.setattr(_da, "resolve_descendant_ids", lambda ch, roots: ({3, 4}, True))
    sink = _stub_multi(monkeypatch, [(1,), (5,), (2, 1, 3, 1), [], []])   # 首查=dept_dim 在册
    resp = api.kb_feedback_stats(owner_key="node:3", request=None,
                                 identity=api.Identity(user_id="dev1"))
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "m.acl_mode='node' AND m.owner_dept_id IN" in sqls
    assert any(p and 3 in p and 4 in p for _, p in sink["calls"] if isinstance(p, tuple))
    assert resp.answer_total == 5


def test_feedback_stats_node_key_stale_snapshot_409(monkeypatch):
    """快照 stale ⇒ 409 org_snapshot_stale（服务器故障不扮 400，绝不拿 stale 后代集硬算）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _fb_stats_bypass_cache(monkeypatch)
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    import opensearch_pipeline.org_sync as _os
    for ns in (api, kb_console):
        monkeypatch.setattr(ns, "_kb_node_capability", lambda cur: "present")
    monkeypatch.setattr(_os, "load_children_index", lambda: (1, False, {}))
    _stub_multi(monkeypatch, [(1,)])
    with pytest.raises(Exception) as ei:
        api.kb_feedback_stats(owner_key="node:3", request=None, identity=api.Identity(user_id="dev1"))
    assert getattr(ei.value, "status_code", None) == 409
    assert "org_snapshot_stale" in str(getattr(ei.value, "detail", ""))


def test_feedback_review_owner_key_intersects_scope(monkeypatch):
    """dept_admin + owner_key：filter 与 scope AND 交集——只收窄不放宽（两个 IN 子句并存）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _fb_stats_bypass_cache(monkeypatch)
    from opensearch_pipeline import api
    sink = _stub_multi(monkeypatch, [[]])
    api.kb_feedback_review(request=None, owner_key="legacy:production",
                           identity=api.Identity(user_id="da1"))
    sql = sink["sql"]
    assert sql.count("owner_dept IN") >= 2          # scope 一处 + filter 一处
    params = sink["params"]
    assert "marketing" in params and "production" in params


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
    # perf 2026-07-16：主查询改服务端 GROUP BY，4 列：status, badge(SQL CASE), owner_dept, n。
    # 徽章判定（含 chunk_status→未入索引）由 _KB_BADGE_CASE_SQL 计算——其与 Python 版的
    # 同义性另由 test_kb_badge_case_sql_parity 钉死，此处只验分桶聚合与出参。
    rows = [
        ("active", "已上线", "marketing", 1),
        ("active", "未入索引", "production", 1),   # 0-chunk 经 CASE 归「未入索引」
        ("active", "已上线", "hr", 1),
    ]
    sink = _stub_multi(monkeypatch, [rows, (7,), (2,)])   # 主 fetchall + chunks fetchone + new_month fetchone
    from opensearch_pipeline import api
    resp = api.kb_stats(request=None, identity=api.Identity(user_id="dev1"))
    assert resp.owner_depts == ["hr", "marketing", "production"]      # 去重 + 排序
    assert resp.by_badge.get("未入索引") == 1                          # chunk_status 生效（此前漏传会误记「处理中」）
    assert resp.by_badge.get("已上线") == 2
    assert resp.chunks == 7 and resp.new_this_month == 2
    # kb_admin（无作用域 clause）分块计数保持无 JOIN 原查询：JOIN 会把孤儿分块悄悄减掉
    chunk_sql = [s for s, _ in sink["calls"] if "chunk_meta" in s][0]
    assert "document_meta" not in chunk_sql


def test_stats_chunk_count_scopes_via_document_meta(monkeypatch):
    """node-ACL：dept_admin 的分块计数按 document_meta.owner_dept 作用域（JOIN），
    **不得**按 chunk_meta.owner_dept —— 那是检索投影轴，node 文档在该列上是哨兵
    `__acl_node_mode_v1__`，按它过滤会让整篇 node 文档的分块从部门统计里消失。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    sink = _stub_multi(monkeypatch, [[("active", "已上线", "marketing", 1)], (7,), (2,)])
    from opensearch_pipeline import api
    resp = api.kb_stats(request=None, identity=api.Identity(user_id="da1"))
    assert resp.chunks == 7
    chunk_sql = [s for s, _ in sink["calls"] if "chunk_meta" in s][0]
    assert "JOIN" in chunk_sql and "document_meta m" in chunk_sql
    assert "m.owner_dept IN" in chunk_sql
    # 哨兵所在的列绝不可再出现在作用域条件里（回归钉死）
    assert "c.owner_dept" not in chunk_sql


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
    _stub_multi(monkeypatch, [("marketing", "报价单.pdf", "raw/marketing/D1/v2/报价单.pdf", 2, "", None)])
    monkeypatch.setattr("opensearch_pipeline.oss_url.generate_signed_url",
                        lambda key, expires=None, method="GET", **kw: f"https://oss.example/{key}?sig=x")
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D1", version=2, identity=api.Identity(user_id="adm1"))
    assert resp.available is True and resp.url.startswith("https://oss.example/")
    assert resp.version_no == 2 and resp.filename == "报价单.pdf"
    assert resp.content_type and resp.expires_in == 300


def test_doc_preview_no_raw_key_unavailable(monkeypatch):
    """raw_key 缺失 → available=False，url 空（前端如实提示不可预览）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [("marketing", "老文档.pdf", None, 1, "", None)])
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D1", identity=api.Identity(user_id="adm1"))
    assert resp.available is False and resp.url == ""


def test_doc_preview_dept_admin_foreign_403(monkeypatch):
    """dept_admin：他部门文档原件不外露（授权先于任何 URL 生成）→ 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    _stub_multi(monkeypatch, [("hr", "考勤.pdf", "raw/hr/D9/v1/考勤.pdf", 1, "", None)])
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


# ── 历史版本下载（2026-08-02，codex 共识）：隔离软拒 / MIME 按实物 / node 列位移 ──
def _boom_signer(monkeypatch):
    """签名函数装成炸弹：断言「签名前拒绝」——隔离/缺件路径绝不能触发 URL 生成。"""
    def _boom(*a, **kw):
        raise AssertionError("generate_signed_url 不该被调用（隔离/缺件必须在签名前拒绝）")
    monkeypatch.setattr("opensearch_pipeline.oss_url.generate_signed_url", _boom)


def test_doc_preview_quarantined_blocked_no_sign(monkeypatch):
    """publish_status='QUARANTINED'：available=False + blocked='quarantined'，签名零调用。
    一视同仁（Sam 拍板 2026-08-02）：kb_admin 也不外发——旧版本原件可能是门内脱敏前实物。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _boom_signer(monkeypatch)
    _stub_multi(monkeypatch, [("hr", "工资表.xlsx", "raw/hr/D3/v1/工资表.xlsx", 1, "QUARANTINED", None)])
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D3", version=1, identity=api.Identity(user_id="adm1"))
    assert resp.available is False and resp.blocked == "quarantined" and resp.url == ""


def test_doc_preview_gate_only_quarantine_blocked(monkeypatch):
    """gate_status='quarantined' 而 publish_status 为空：OR 语义必须同样拒绝（双字段权威）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _boom_signer(monkeypatch)
    _stub_multi(monkeypatch, [("hr", "截图.docx", "raw/hr/D4/v2/截图.docx", 2, "", "quarantined")])
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D4", version=2, identity=api.Identity(user_id="adm1"))
    assert resp.available is False and resp.blocked == "quarantined"


def test_doc_preview_current_version_quarantined_blocked(monkeypatch):
    """version=0（current 入口，DocTable 下载按钮同路径）：隔离软拒同样生效——端点级统一。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _boom_signer(monkeypatch)
    _stub_multi(monkeypatch, [("hr", "名单.pdf", "raw/hr/D5/v3/名单.pdf", 3, "QUARANTINED", "quarantined")])
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D5", identity=api.Identity(user_id="adm1"))
    assert resp.available is False and resp.blocked == "quarantined"


def test_doc_preview_mime_from_raw_key_ext(monkeypatch):
    """MIME 按该版本实物扩展名（raw_key）推导：文档级 filename=.pdf、旧版实物=.docx → docx MIME。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_multi(monkeypatch, [("marketing", "合同.pdf", "raw/marketing/D6/v1/合同初稿.docx", 1, "", None)])
    monkeypatch.setattr("opensearch_pipeline.oss_url.generate_signed_url",
                        lambda key, expires=None, method="GET", **kw: f"https://oss.example/{key}?sig=x")
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D6", version=1, identity=api.Identity(user_id="adm1"))
    assert resp.available is True
    assert resp.content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _force_capability_present(monkeypatch):
    """覆盖本文件 autouse 的 absent 钉桩（kb_console 是 from-import 绑定，按其命名空间打）。"""
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "present")


def test_doc_preview_node_mode_kb_admin_ok(monkeypatch):
    """capability=present：8 列行 (owner,filename,raw_key,vno,pubs,gate,acl_mode,owner_dept_id)
    解包位置锁定——node 文档 kb_admin 正向可下载（列位移回归锚）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _force_capability_present(monkeypatch)
    _stub_multi(monkeypatch, [("", "SOP.pdf", "raw/node-42/D7/v1/SOP.pdf", 1, "", None, "node", 42)])
    monkeypatch.setattr("opensearch_pipeline.oss_url.generate_signed_url",
                        lambda key, expires=None, method="GET", **kw: f"https://oss.example/{key}?sig=x")
    from opensearch_pipeline import api
    resp = api.kb_doc_preview(request=None, doc_id="D7", version=1, identity=api.Identity(user_id="adm1"))
    assert resp.available is True and resp.version_no == 1


def test_doc_preview_node_mode_legacy_dept_admin_403(monkeypatch):
    """capability=present：node 文档对仅有 legacy 部门授权的 dept_admin fail-closed 403
    （owner_dept 残值绝不回落 legacy 轴——mode 隔离回归锚）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "hr")
    _force_capability_present(monkeypatch)
    _boom_signer(monkeypatch)
    _stub_multi(monkeypatch, [("hr", "SOP.pdf", "raw/node-42/D7/v1/SOP.pdf", 1, "", None, "node", 42)])
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_doc_preview(request=None, doc_id="D7", version=1, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_version_history_gate_only_quarantine_badge_and_flags(monkeypatch):
    """版本列表：gate-only 隔离行 → quarantined=True + 徽章「已隔离」（此前存量 bug 显「已上线」）；
    has_raw 按 COALESCE 非空语义回传（0 → False，按钮置灰依据）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    ver_rows = [
        (3, "SUCCESS", "", "SUCCESS", "", "quarantined", 1, "", "2026-07-01"),   # gate-only 隔离
        (2, "SUCCESS", "", "SUCCESS", "QUARANTINED", "", 1, "", "2026-06-20"),   # publish 路径隔离
        (1, "SUCCESS", "", "SUCCESS", "", "", 0, "", "2026-06-10"),              # 正常但无原件
    ]
    _stub_multi(monkeypatch, [("marketing", "active"), ver_rows])
    from opensearch_pipeline import api
    resp = api.kb_version_history(request=None, doc_id="D1", identity=api.Identity(user_id="kb1"))
    v3, v2, v1 = resp.versions
    assert v3.quarantined is True and v3.status_badge == "已隔离" and v3.has_raw is True
    assert v2.quarantined is True and v2.status_badge == "已隔离"
    assert v1.quarantined is False and v1.status_badge == "已上线" and v1.has_raw is False


# ── GET/POST /api/kb/review-tasks：入库复审任务队列（盲区审计 P2-33）────────────
def test_review_tasks_kb_admin_only_and_age_order(monkeypatch):
    """kb_admin 专属（dept_admin 403）；收件箱不设时间窗、按龄升序；标题现查 document_meta。"""
    _skip_if_not_sim()
    from fastapi import HTTPException
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "hr")
    from opensearch_pipeline import api
    with pytest.raises(HTTPException) as ei:
        api.kb_review_tasks(request=None, limit=20, identity=api.Identity(user_id="da1"))
    assert ei.value.status_code == 403

    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    # 12 列：task_id, doc_id, title, version_no, review_type, review_reason, owner_dept,
    #        suggested_permission_level, review_status, reviewer_name, created_at, age
    rows = [("RT1", "D1", "员工薪酬发放办法", 2, "spot_check_mismatch",
             "实时权限 public 比 LLM 建议 restricted 更宽松", "hr", "restricted",
             "PENDING", None, "2026-06-25 08:10:00", 9)]
    sink = _stub_multi(monkeypatch, [rows])
    resp = api.kb_review_tasks(request=None, limit=20, identity=api.Identity(user_id="adm1"))
    it = resp.items[0]
    assert it.task_id == "RT1" and it.age_days == 9 and it.closed is False
    assert it.title == "员工薪酬发放办法" and it.suggested_permission_level == "restricted"
    list_sql = sink["calls"][0][0]
    assert "ORDER BY t.created_at ASC" in list_sql and "INTERVAL" not in list_sql
    assert "LEFT JOIN" in list_sql and "document_meta" in list_sql


def test_review_task_resolve_writes_reviewer_fields(monkeypatch):
    """处置写 review_status/reviewer_*/reviewed_at；reopen 清 reviewed_at；不存在 → 404。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [])
    from opensearch_pipeline import api
    r = api.kb_review_task_resolve(
        api.KbReviewTaskResolveRequest(task_id="RT1", action="resolve", comment="已改回 restricted"),
        request=None, identity=api.Identity(user_id="adm1"))
    assert r["review_status"] == "RESOLVED"
    upd = [c for c in sink["calls"] if "UPDATE" in c[0]][0]
    assert "reviewer_user_id=%s" in upd[0] and "reviewed_at=NOW()" in upd[0]
    assert "reviewer_comment=%s" in upd[0] and "已改回 restricted" in upd[1]
    r2 = api.kb_review_task_resolve(
        api.KbReviewTaskResolveRequest(task_id="RT1", action="reopen"),
        request=None, identity=api.Identity(user_id="adm1"))
    assert r2["review_status"] == "PENDING"
    upd2 = [c for c in sink["calls"] if "UPDATE" in c[0]][-1]
    assert "reviewed_at=NULL" in upd2[0]


# ── /api/kb/ops-metrics 运营数据面（批次γ，仅 kb_admin）──────────────────────────
def test_ops_metrics_dept_admin_forbidden(monkeypatch):
    """运营数据面是 kb_admin 专属：dept_admin（含写授权）也 403。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_ops_metrics(request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_ops_metrics_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_ops_metrics(request=None, identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_ops_metrics_kb_admin_shape_and_queries(monkeypatch):
    """kb_admin + 空桩 → 三块 available=True 且列表空（表可读、无数据 ≠ 失败）；关键查询都在。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    sink = _stub_multi(monkeypatch, [])
    from opensearch_pipeline import api
    resp = api.kb_ops_metrics(request=None, identity=api.Identity(user_id="dev1"))
    assert resp.window_days == 30
    assert resp.llm_available is True and resp.llm_by_model == [] and resp.llm_total_calls == 0
    assert resp.llm_cost_estimate is None                 # 价表未配 → 诚实 NULL 绝不编造单价
    assert resp.slo_available is True and resp.slo_daily == []
    assert resp.admission_available is True and resp.admission_daily == []
    sqls = " || ".join(s for s, _ in sink["calls"])
    assert "llm_call_log" in sqls                         # D1 成本底座
    assert "qa_daily_metrics" in sqls                     # D2 SLO 日趋势
    assert "qa_admission_reject" in sqls                  # D3 限流准入
    assert "PERCENT_RANK()" in sqls                       # 延迟分位与 governance 同款
    assert "slo_breaches_json" in sqls                    # 违约明细列被读取


def test_ops_metrics_all_queries_fail_raises_500(monkeypatch):
    """全部子查询失败 = 连接级故障 → 诚实 500（绝不 all-zeros 伪装健康）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_all_fail(monkeypatch)
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_ops_metrics(request=None, identity=api.Identity(user_id="dev1"))
    assert getattr(ei.value, "status_code", None) == 500


def test_ops_metrics_slo_and_admission_aggregation(monkeypatch):
    """种子数据：SLO 违约名解析 + breach 计数；准入 __admitted__ 伪行与拒绝原因分账。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    seq = [
        (5, 1, 1200, 800, None),          # q1 llm 总量（5 次调用 1 错，token 1200+800，cost NULL）
        (900, 4200),                       # q2 llm p50/p95
        [("qwen3.7-plus", 5, 1, 1200, 800, 1100)],                    # q3 by_model
        [("default", 4, 1500), ("deep", 1, 500)],                     # q4 by_category
        [("marketing", 3, 1300), ("未归集", 2, 700)],                  # q5 by_dept
        [("2026-07-13", 5, 2000)],                                    # q6 daily
        [                                                              # q7 slo
            ("2026-07-12", 100, 0.90, 0.05, 0.01, 8000, 40, 1, None, 3),
            ("2026-07-13", 120, 0.80, 0.10, 0.02, 9000, 42, 0,
             '[{"slo":"answer_rate","threshold":0.85,"value":0.8}]', 5),
        ],
        [                                                              # q8 admission
            ("2026-07-12", "__admitted__", 450),
            ("2026-07-13", "__admitted__", 500),
            ("2026-07-13", "per_min", 20),
            ("2026-07-13", "global_cap", 3),
        ],
    ]
    _stub_multi(monkeypatch, seq)
    from opensearch_pipeline import api
    resp = api.kb_ops_metrics(request=None, identity=api.Identity(user_id="dev1"))
    # LLM 总量与分组
    assert resp.llm_total_calls == 5 and resp.llm_error_calls == 1
    assert resp.llm_p95_latency_ms == 4200
    assert resp.llm_by_model[0].model == "qwen3.7-plus"
    assert resp.llm_by_dept[0].key == "marketing"
    # SLO：违约名解析 + breach 天数
    assert resp.slo_available is True and len(resp.slo_daily) == 2
    assert resp.slo_daily[0].slo_ok is True and resp.slo_daily[0].breaches == []
    assert resp.slo_daily[1].slo_ok is False and resp.slo_daily[1].breaches == ["answer_rate"]
    assert resp.slo_daily[1].rejected_count == 5
    assert resp.slo_breach_days == 1
    # 准入：伪行分账（__admitted__ 不算拒绝），原因按量降序
    d = {r.d: r for r in resp.admission_daily}
    assert d["2026-07-12"].admitted == 450 and d["2026-07-12"].rejected == 0
    assert d["2026-07-13"].admitted == 500 and d["2026-07-13"].rejected == 23
    assert [(r.reason, r.count) for r in resp.admission_reasons] == [("per_min", 20), ("global_cap", 3)]


# ── dept_coverage.qa_hits_7d（批次δ-2：看板 7/30 窗口切换的数据面）─────────────────
def _stub_dept_usage(monkeypatch, wow_rows="ok"):
    """定向桩：只喂 dept_coverage 的 30 天使用量与 14 天 wow 两条子查询；其余 fetchone→None
    （governance 各子查询 `or (defaults)` 兜底）、fetchall→[]。wow_rows='raise' 模拟 wow 子查询失败。"""
    sink = {"calls": []}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            sink["calls"].append(sql)
            self._sql = sql
            if wow_rows == "raise" and "INTERVAL 14 DAY" in sql and "qa_session_log" in sql:
                raise RuntimeError("wow query down")

        def fetchone(self):
            return None

        def fetchall(self):
            sql = getattr(self, "_sql", "")
            if "qa_session_log" in sql and "INTERVAL 14 DAY" in sql:
                return [("marketing", 9, 5)]          # 近7天=9 · 前7天=5
            # 阶段 B：分桶键改稳定表达式（GROUP BY 1），30 天窗口查询以 INTERVAL %s DAY 区分
            if "qa_session_log" in sql and ("GROUP BY m.owner_dept" in sql
                                            or ("GROUP BY 1" in sql and "INTERVAL %s DAY" in sql)):
                return [("marketing", 30, 3)]         # 近30天=30 · REFUSAL=3
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _Conn())
    return sink


def test_governance_dept_qa_hits_7d_exposed(monkeypatch):
    """qa_hits_7d = wow 子查询现成的 qa7（零新增扫描）：随 30/7 双口径一起吐出。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_dept_usage(monkeypatch)
    from opensearch_pipeline import api
    resp = api.kb_governance(request=None, identity=api.Identity(user_id="dev1"))
    row = next(r for r in resp.dept_coverage if r.owner_dept == "marketing")
    assert row.qa_hits == 30
    assert row.qa_hits_7d == 9
    assert row.qa_wow_net == 4          # 9-5，与 qa_hits_7d 同源
    assert row.no_answer_rate == 0.1


def test_governance_dept_qa_hits_7d_none_on_wow_failure(monkeypatch):
    """wow 子查询失败 → qa_hits_7d 与 qa_wow_net 同生共死给 None（未知≠零使用）；qa_hits 不受累。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _stub_dept_usage(monkeypatch, wow_rows="raise")
    from opensearch_pipeline import api
    resp = api.kb_governance(request=None, identity=api.Identity(user_id="dev1"))
    row = next(r for r in resp.dept_coverage if r.owner_dept == "marketing")
    assert row.qa_hits == 30            # 主口径独立存活
    assert row.qa_hits_7d is None       # 绝不伪装成 0
    assert row.qa_wow_net is None and row.qa_wow is None


# ── 批次ε-5 R2：台账徽章词表封闭集锁（跨层 seam——前端按字面量映射，词表漂移=可见性静默回归）──
def test_kb_status_badge_closed_set():
    """枚举驱动全组合：_kb_status_badge 输出 ⊆ _KB_BADGE_VOCAB 且每个词都可达（双向收紧）。
    新增/改名一个分支而未登记进 _KB_BADGE_VOCAB → 本测试红，倒逼同步前端
    console-app/src/lib/kb.ts BADGE_TONE 与 MyContributions displayState 特判词
    （前端侧对应锁=contribute.spec.ts「台账词表 seam 锁」）。"""
    import itertools
    from opensearch_pipeline.api import _kb_status_badge, _KB_BADGE_VOCAB

    content_vals = ["", "NOT_STARTED", "DONE", "FAILED", "REJECTED",
                    "SKIPPED_DUPLICATE", "PENDING_APPROVAL", "RUNNING", "whatever",
                    "CONTENT_MISMATCH"]   # C8：内容身份不符的安全终态（拍板单 §4.2）
    index_vals = ["", "NOT_INDEXED", "SUCCESS", "INDEXED", "FAILED"]
    doc_vals = [None, "active", "retired"]
    publish_vals = [None, "PUBLISHED", "QUARANTINED", "SKIPPED_EXPLOSION"]
    chunk_status_vals = [None, "OK", "EMPTY"]
    chunk_active_vals = [None, 0, 3]

    seen = set()
    for cs, ix, ds, ps, cks, ca in itertools.product(
            content_vals, index_vals, doc_vals, publish_vals, chunk_status_vals, chunk_active_vals):
        out = _kb_status_badge(cs, ix, ds, ca, ps, cks)
        assert out in _KB_BADGE_VOCAB, f"未登记的新徽章词 {out!r}（inputs cs={cs} ix={ix} ds={ds} ps={ps} cks={cks} ca={ca}）"
        seen.add(out)
    assert seen == _KB_BADGE_VOCAB, f"词表不再全可达（死词该从封闭集摘除）：缺 {_KB_BADGE_VOCAB - seen}"


def test_my_docs_and_browse_gate_only_row_renders_quarantined(monkeypatch):
    """★ 渲染侧 gate 轴（2026-08-04 独立核验 B2）：gate-only 隔离行在**列表渲染**也必须显
    「已隔离」——此前筛选/计数走 SQL 镜像认 gate、渲染调用不传 gate_status，同一行会
    「按已隔离筛出、列表里显示已上线」自相矛盾。my-docs 与 browse 双端点各钉一枚。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.qa_facts.fact_join_enabled", lambda: False)
    from opensearch_pipeline import api
    # my-docs：14 列（…, cpe, gate_status）
    docrows = [("D1", "t1", "a.pdf", "hr", "dept_internal", 1, "active", "ts",
                "DONE", "SUCCESS", None, "DONE", None, "quarantined")]
    _stub_multi(monkeypatch, [[], docrows])
    resp = api.kb_my_docs(request=None, limit=20, offset=0, identity=api.Identity(user_id="adm1"))
    assert resp.items[0].status_badge == "已隔离"
    # browse：13 列（…, chunk_status, gate_status）
    rows = [("D2", "y", "b.pdf", "hr", "dept_internal", 1, "active", "t",
             "DONE", "SUCCESS", None, "DONE", "quarantined")]
    _stub_multi(monkeypatch, [[], rows])
    resp2 = api.kb_browse(request=None, identity=api.Identity(user_id="adm1"))
    assert resp2.items[0].status_badge == "已隔离"


def test_my_docs_badge_counts_faceted(monkeypatch):
    """faceted 计数（2026-07-16 Sam 反馈）：badge_counts 与主查询同筛选（除 badge 自身）——
    计数查询不含 badge 谓词参数、按徽章 GROUP BY；响应携带映射供 chips/标题总数跟随筛选。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.qa_facts.fact_join_enabled", lambda: False)
    counts_rows = [("已上线", 12), ("未入索引", 3), ("已退役", 5)]
    docrows = [("D1", "t1", "a.pdf", "production", "dept_internal", 1, "active", "ts",
                "DONE", "SUCCESS", None, "DONE", None, None)]
    sink = _stub_multi(monkeypatch, [counts_rows, docrows])
    from opensearch_pipeline import api
    resp = api.kb_my_docs(request=None, limit=20, offset=0, owner_dept="production",
                          badge="已上线", identity=api.Identity(user_id="adm1"))
    assert resp.badge_counts == {"已上线": 12, "未入索引": 3, "已退役": 5}
    counts_sql, counts_params = sink["calls"][0]
    main_sql, _ = sink["calls"][1]
    assert "GROUP BY b" in counts_sql
    assert "owner_dept = %s" in counts_sql and "production" in (counts_params or ())
    # 计数查询不含 badge 自身的筛选（faceted 语义：各状态的数在当前其他筛选下可见）
    assert counts_sql.count("CASE") <= main_sql.count("CASE") and "已上线" not in str(counts_params)


def test_org_tree_exposes_node_acl_grant_flag(monkeypatch):
    """★ org-tree 必须回读侧 `node_acl_grant` —— 上传表单据此决定写 legacy 组码还是 node 节点。

    ⚠️ 这不是"控件做没做"的开关，是**安全开关**：GRANT 关时 `can_read_doc` 对 node 文档
    无条件 DENY（acl_policy.py:281），此刻若把新上传文档写成 node 授权，它对所有人不可见
    ——连归属部门自己都看不到（投影轴换哨兵后 legacy owner 分支不再放行）。
    """
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline import api
    monkeypatch.setattr(api, "_load_org_tree_snapshot", lambda: {"nodes": [], "stale": True})

    for flag, expect in ((True, True), (False, False)):
        monkeypatch.setattr("opensearch_pipeline.routes.kb_console._node_acl_grant_enabled",
                            lambda f=flag: f)
        resp = api.kb_org_tree(request=None, identity=api.Identity(user_id="dev1"))
        assert resp.node_acl_grant is expect


def test_org_tree_exposes_my_managed_node_roots(monkeypatch):
    """★ org-tree 回调用者自己的 node 管辖根（kb.granted_node_roots）——前端归属
    自动预填/管辖子树过滤的数据源。后端契约：有授权行回排序 int 列表、无行回 []；
    「缺字段=unknown≠空授权」的三态判定在前端 useOrgSnapshot 做。"""
    _skip_if_not_sim()
    from opensearch_pipeline import api
    monkeypatch.setattr(api, "_load_org_tree_snapshot", lambda: {"nodes": [], "stale": True})
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_NODE_ROOTS", "7,3")
    resp = api.kb_org_tree(request=None, identity=api.Identity(user_id="mgr1"))
    assert resp.my_managed_node_roots == [3, 7]

    monkeypatch.delenv("RAG_SIM_MANAGED_NODE_ROOTS")
    resp2 = api.kb_org_tree(request=None, identity=api.Identity(user_id="mgr1"))
    assert resp2.my_managed_node_roots == []


def test_org_tree_flag_fails_safe_to_legacy(monkeypatch):
    """读不到 config ⇒ 回 False（继续走 legacy 组码），绝不因异常就把上传切到 node 口径。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.routes import kb_console

    def _boom():
        raise RuntimeError("config 不可用")

    monkeypatch.setattr("opensearch_pipeline.config.get_config", _boom)
    assert kb_console._node_acl_grant_enabled() is False


# ── 附录B：doc-status 漏传 publish_status/chunk_status（2026-08-03）──────────────
# doc-status 是前端**轮询**的端点（useKb.trackStatus 每 8s 一次，只在徽章 ∈
# TERMINAL_BADGES 时收手）。它此前是全仓唯一不传这两个字段的真实状态端点，于是
# _kb_status_badge 的三条终态分支全部不可达。后果两个方向都有：轮询等不到终态，
# 以及 index_status 残留 'SUCCESS' 的隔离/缺内容件被显示成「已上线」。

def _doc_status(monkeypatch, dv_row, *, doc_status="active", counts=(5, 5, 5), sink=None):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    s = _stub_multi(monkeypatch, [("hr", doc_status, 3), dv_row, counts])
    if sink is not None:
        sink["s"] = s
    from opensearch_pipeline import api
    return api.kb_doc_status(request=None, doc_id="D1", version=3,
                             identity=api.Identity(user_id="kb1"))


def test_doc_status_selects_the_columns_its_unpacking_needs(monkeypatch):
    """桩游标按位置喂行，不会因 SELECT 少列而报错——所以必须**单独**钉住列清单。

    少取 publish_status/gate_status 在生产上是 6-元组解包收到 4-元组 ⇒ ValueError ⇒ 500，
    而纯徽章断言对此完全无感（本条正是补上那个反证空洞）。
    """
    sink = {}
    _doc_status(monkeypatch, ("SUCCESS", "", "SUCCESS", "", "", ""), sink=sink)
    dv_sql = [c[0] for c in sink["s"]["calls"] if "document_version" in c[0]][0]
    for col in ("content_process_status", "chunk_status", "index_status",
                "error_message", "publish_status", "gate_status"):
        assert col in dv_sql, f"doc-status 的 document_version 查询漏取 {col}：{dv_sql}"


# dv_row = (content_process_status, chunk_status, index_status, error_message,
#           publish_status, gate_status)

def test_doc_status_quarantined_never_shows_online(monkeypatch):
    """最严重的一支：隔离件的 index_status 可能残留 'SUCCESS' ⇒ 漏传时先命中「已上线」，
    把已隔离（未脱敏）文档显示成可搜。badge helper 的注释明说绝不能这样。"""
    r = _doc_status(monkeypatch, ("SUCCESS", "", "SUCCESS", "", "QUARANTINED", ""))
    assert r.status_badge == "已隔离", f"隔离件显示成了 {r.status_badge}"


def test_doc_status_gate_only_quarantine_is_quarantined(monkeypatch):
    """gate-only 隔离（publish_status 空、gate_status='quarantined'）走
    _kb_version_quarantined 这个唯一权威 OR 语义——与版本历史同源。"""
    r = _doc_status(monkeypatch, ("SUCCESS", "", "SUCCESS", "", "", "quarantined"))
    assert r.status_badge == "已隔离"


def test_doc_status_spot_quarantine_is_not_stuck_processing(monkeypatch):
    """spot 隔离把 index_status 写成 'DELETED' ⇒ 漏传时一路落到默认「处理中」，
    前端轮询 22 次×8s 后放弃。"""
    r = _doc_status(monkeypatch, ("QUARANTINED", "", "DELETED", "", "QUARANTINED", ""),
                    counts=(5, 0, 0))
    assert r.status_badge == "已隔离"


def test_doc_status_empty_chunk_is_terminal(monkeypatch):
    """chunk_status='EMPTY'（低文本图纸等，78 篇那批）永远不会进索引 —— 必须给终态。"""
    r = _doc_status(monkeypatch, ("DONE", "EMPTY", "", "", "", ""), counts=(0, 0, 0))
    assert r.status_badge == "未入索引"


def test_doc_status_skipped_publish_is_terminal(monkeypatch):
    r = _doc_status(monkeypatch, ("DONE", "", "", "", "SKIPPED_PII", ""), counts=(0, 0, 0))
    assert r.status_badge == "未入索引"


def test_doc_status_needs_review_never_shows_online(monkeypatch):
    """stage-3 毒 chunk 死信**只改 chunk_status**，dv.index_status 仍是 SUCCESS ⇒
    漏传时该文档显示「已上线」，管理员看不出它缺内容。"""
    r = _doc_status(monkeypatch, ("SUCCESS", "NEEDS_REVIEW", "SUCCESS", "", "", ""))
    assert r.status_badge == "未入索引"


def test_doc_status_online_path_unchanged(monkeypatch):
    """对照：正常上线件行为不变。"""
    r = _doc_status(monkeypatch, ("SUCCESS", "", "SUCCESS", "", "PUBLISHED", ""))
    assert r.status_badge == "已上线"


def test_doc_status_badge_agrees_with_version_history(monkeypatch):
    """同一行底层数据，doc-status 与 version-history 必须给出**同一个**徽章。

    这条把两个端点锁在一起——它们此前正是因为各传各的字段而漂移。
    """
    from opensearch_pipeline import api
    cases = [
        ("SUCCESS", "", "SUCCESS", "", "QUARANTINED", ""),
        ("SUCCESS", "", "SUCCESS", "", "", "quarantined"),
        ("DONE", "EMPTY", "", "", "", ""),
        ("SUCCESS", "NEEDS_REVIEW", "SUCCESS", "", "", ""),
        ("SUCCESS", "", "SUCCESS", "", "PUBLISHED", ""),
    ]
    for cps, chs, ixs, err, pubs, gate in cases:
        ds = _doc_status(monkeypatch, (cps, chs, ixs, err, pubs, gate))
        _stub_multi(monkeypatch, [("hr", "active"),
                                  [(3, cps, chs, ixs, pubs, gate, 1, err, "2026-08-03")]])
        vh = api.kb_version_history(request=None, doc_id="D1",
                                    identity=api.Identity(user_id="kb1"))
        assert ds.status_badge == vh.versions[0].status_badge, (
            f"两端点对同一行漂移：doc-status={ds.status_badge} "
            f"version-history={vh.versions[0].status_badge}（{cps}/{chs}/{ixs}/{pubs}/{gate}）")


def test_new_terminal_badges_are_terminal_in_frontend_poller():
    """端到端闭合：后端新放出的这两个徽章必须在前端 TERMINAL_BADGES 里，
    否则轮询照样收不了手 —— 修了后端却没解决用户可见的症状。"""
    import pathlib
    src = pathlib.Path("console-app/src/lib/kb.ts").read_text(encoding="utf-8")
    line = [ln for ln in src.splitlines() if "TERMINAL_BADGES" in ln][0]
    for badge in ("未入索引", "已隔离"):
        assert badge in line, f"前端轮询未把「{badge}」当终态：{line}"


# ── B8（Sam 2026-08-04 选 c）：差评复核的两层截断必须如实暴露 ────────────────────
# 此前两层都静默：SQL 硬 LIMIT 300 扫原始行 + 凑满 limit 就不再收新 message_id。
# ⇒ 管理员看到的「差评就这些」可能只是全量的一小部分，**且无从知道**。

def _fb_row(mid, doc="D1"):
    # (message_id, created, question, doc_id, title, owner, reason, comment, handled_status)
    return (mid, "2026-08-01", "问题" + mid, doc, "标题", "hr", "inaccurate", "", "PENDING")


def _feedback_review(monkeypatch, rows, limit=20):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    _stub_multi(monkeypatch, [rows])
    from opensearch_pipeline import api
    return api.kb_feedback_review(request=None, limit=limit,
                                  identity=api.Identity(user_id="kb1"))


def test_feedback_review_flags_message_layer_truncation(monkeypatch):
    """★ 消息层：不同 message_id 数超过 limit ⇒ truncated_messages=True。"""
    out = _feedback_review(monkeypatch, [_fb_row(f"m{i}") for i in range(25)], limit=20)
    assert len(out.items) == 20
    assert out.truncated_messages is True, "凑满 limit 后仍静默丢弃新消息"


def test_feedback_review_flags_scan_layer_truncation(monkeypatch):
    """★ 扫描层：SQL 扫满 300 行 ⇒ truncated_scan=True（可能还有更早的差评没进聚合）。

    ⚠️ 这一层与消息层**正交**：即使去重后条目远少于 limit，扫描仍可能被 300 行截断。
    本例故意让 300 行全属同一 message ⇒ items 只有 1 条、truncated_messages 为 False，
    但 truncated_scan 必须为 True。
    """
    out = _feedback_review(monkeypatch, [_fb_row("same", f"D{i}") for i in range(300)])
    assert out.truncated_messages is False
    assert out.truncated_scan is True, "SQL 扫满 300 行却没留痕"


def test_feedback_review_no_truncation_when_within_limits(monkeypatch):
    """对照：未触顶时两个标志都为 False（不制造无谓告警）。"""
    out = _feedback_review(monkeypatch, [_fb_row(f"m{i}") for i in range(3)])
    assert out.truncated_messages is False and out.truncated_scan is False
    assert len(out.items) == 3


def test_appending_a_column_no_longer_corrupts_acl_mode(monkeypatch):
    """★★ 行为级证明：给 my-docs 的行**追加一列**后，`acl_mode` 不得被污染。

    这正是 2026-08-04 落 R1 时差点造成的形态：capability='absent'（行里**没有**
    acl_mode/owner_dept_id），但因为追加了 `m.acl_revision`，`len(r) > 13` 恒真
    ⇒ 把那个整数当成 `acl_mode` 读 ⇒ 文档被判成 node 模式（或落进 `_kb_node_names` 查询）。
    **ACL 判定轴错了不报错、只错权限**，所以必须有行为级守卫，不能只靠源码扫描。
    """
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "absent")
    seen_node_ids = []
    monkeypatch.setattr(kb_console, "_kb_node_names",
                        lambda cur, ids: seen_node_ids.extend(ids) or {})
    monkeypatch.setattr(kb_console, "_kb_usage_enrich", lambda cur, ids: {})
    monkeypatch.setattr(kb_console, "_kb_badge_counts", lambda *a, **k: {})
    # 13 个基础列 + **1 个追加列**（模拟 m.acl_revision），capability=absent ⇒ 无 _mc
    row = ("D1", "标题", "f.pdf", "hr", "dept_internal", 1, "active", "2026-08-04",
           "DONE", "SUCCESS", "PUBLISHED", "", "", 77)
    _stub_multi(monkeypatch, [[row]])
    resp = api.kb_my_docs(request=None, identity=api.Identity(user_id="kb1"))
    assert resp.items, "用例前提：应返回一行"
    assert seen_node_ids == [], (
        f"追加列被当成 owner_dept_id 送进了节点名查询：{seen_node_ids} —— ACL 判定轴已被污染")
    assert resp.items[0].owner_dept == "hr"


def test_appending_two_columns_does_not_silently_flip_acl_mode(monkeypatch):
    """★★ 比上一条更可怕的一格：**追加两列**时旧写法不会崩，会**静默**把它们读成
    `acl_mode` / `owner_dept_id`。

    追加一列 ⇒ `r[14]` 越界 ⇒ IndexError（至少会响）；
    追加**两列** ⇒ `r[13]`/`r[14]` 都在 ⇒ 无异常、文档被判成 node 模式、
    那个整数被当作 `owner_dept_id` 送进节点名查询 —— **不报错、只错权限**。
    """
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "absent")
    seen_node_ids = []
    monkeypatch.setattr(kb_console, "_kb_node_names",
                        lambda cur, ids: seen_node_ids.extend(ids) or {})
    monkeypatch.setattr(kb_console, "_kb_usage_enrich", lambda cur, ids: {})
    monkeypatch.setattr(kb_console, "_kb_badge_counts", lambda *a, **k: {})
    row = ("D1", "标题", "f.pdf", "hr", "dept_internal", 1, "active", "2026-08-04",
           "DONE", "SUCCESS", "PUBLISHED", "", "", 77, 88)      # 追加**两**列
    _stub_multi(monkeypatch, [[row]])
    resp = api.kb_my_docs(request=None, identity=api.Identity(user_id="kb1"))
    assert seen_node_ids == [], (
        f"追加的两列被静默当成 acl_mode/owner_dept_id：{seen_node_ids}")
    assert resp.items[0].owner_dept == "hr", "归属轴被污染"


# ── 待审批队列必须排除已退役文档（2026-08-06 现网 20 个僵尸条目）────────────────
def test_pending_approvals_excludes_retired_docs():
    """★ kb_retire 不动 content_process_status ⇒ 被退役的待审批公开件仍是 PENDING_APPROVAL。
    若队列不按 m.status 过滤,它们会永远挂着:kb_approve 对退役文档有意 no-op(200+approved:0),
    于是"点一次成功一次、刷新又回来"。修在列表侧——那一版**确实**从未被批准,改数据反而
    会让 kb_restore 后无法重进审批。
    """
    import pathlib
    import re
    src = pathlib.Path("opensearch_pipeline/routes/kb_console.py").read_text(encoding="utf-8")
    m = re.search(r"def kb_pending_approvals.*?LIMIT 101", src, re.S)
    assert m, "kb_pending_approvals 的查询形态变了,本守卫需同步"
    q = m.group(0)
    assert "content_process_status = 'PENDING_APPROVAL'" in q
    assert "LOWER(m.status) = 'active'" in q, (
        "待审批队列未排除已退役文档 ⇒ 会出现永远批不掉的僵尸条目")
