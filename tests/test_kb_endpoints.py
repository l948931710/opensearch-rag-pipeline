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
    assert b("DONE", "SUCCESS", "active", 0) == "处理中"     # SUCCESS 但 0 活跃 chunk → 不算已上线


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
