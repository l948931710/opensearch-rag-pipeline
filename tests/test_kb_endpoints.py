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
