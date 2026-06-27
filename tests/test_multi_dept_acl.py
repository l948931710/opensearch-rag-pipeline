# -*- coding: utf-8 -*-
"""
test_multi_dept_acl.py — 多部门 ACL 边界

覆盖：
  - _normalize_acl_groups：净化 + 白名单 + 去重 + fail-closed（H2）
  - _same_permission：二次取回（邻居/step 扩展）的权限一致性守卫（H4 防御纵深）
  - build_qa_log_kwargs：list user_dept → CSV 扁平化（A5，VARCHAR 无迁移）
  - _search_chunks_opensearch：本地回退用 terms 多值（A1）
"""

import sys
import types


# ── _normalize_acl_groups：白名单 + fail-closed ──────────────────

def test_normalize_acl_groups_basic():
    from opensearch_pipeline.retriever import _normalize_acl_groups
    assert _normalize_acl_groups("marketing,production") == ["marketing", "production"]
    assert _normalize_acl_groups(["marketing", "production"]) == ["marketing", "production"]
    assert _normalize_acl_groups(["marketing", "marketing"]) == ["marketing"]  # 去重
    assert _normalize_acl_groups(["marketing,production"]) == ["marketing", "production"]


def test_normalize_acl_groups_fail_closed():
    from opensearch_pipeline.retriever import _normalize_acl_groups
    assert _normalize_acl_groups(None) == []
    assert _normalize_acl_groups("") == []
    assert _normalize_acl_groups("   ") == []
    assert _normalize_acl_groups(["", "  "]) == []
    assert _normalize_acl_groups("营销中心") == []          # 中文名非组代码
    assert _normalize_acl_groups("production_injection") == []  # OSS 子线代码非权限组（不在白名单）


def test_normalize_acl_groups_drops_injection_elements():
    from opensearch_pipeline.retriever import _normalize_acl_groups
    # 合法组保留，注入元素净化后非白名单 → 丢弃
    assert _normalize_acl_groups(['marketing', 'x" OR 1=1 OR owner_dept="y']) == ["marketing"]
    assert _normalize_acl_groups(['x" OR permission_level="restricted']) == []


# ── _same_permission：二次取回权限一致性守卫 ───────────────────

def test_same_permission_guard():
    from opensearch_pipeline.retriever import _same_permission
    center = {"permission_level": "dept_internal", "owner_dept": "finance"}
    assert _same_permission({"permission_level": "dept_internal", "owner_dept": "finance"}, center)
    # 权限等级不同 → 丢弃
    assert not _same_permission({"permission_level": "public", "owner_dept": "finance"}, center)
    # 部门不同 → 丢弃（绝不把他部门 dept_internal 拼入）
    assert not _same_permission({"permission_level": "dept_internal", "owner_dept": "hr"}, center)


def test_same_permission_defaults_public():
    from opensearch_pipeline.retriever import _same_permission
    # 缺字段默认 public/""，两个 public 行视为一致
    assert _same_permission({}, {})
    assert _same_permission({"permission_level": "public", "owner_dept": ""}, {})


# ── build_qa_log_kwargs：list → CSV ─────────────────────────────

def test_qa_log_user_dept_csv_flatten():
    from opensearch_pipeline.answer_flow import build_qa_log_kwargs
    kw = build_qa_log_kwargs(session_id="s", message_id="m", question="q",
                             user_dept=["marketing", "production"])
    assert kw["user_dept"] == "marketing,production"
    kw2 = build_qa_log_kwargs(session_id="s", message_id="m", question="q", user_dept="marketing")
    assert kw2["user_dept"] == "marketing"
    kw3 = build_qa_log_kwargs(session_id="s", message_id="m", question="q", user_dept=None)
    assert kw3["user_dept"] is None


# ── 本地回退：terms 多值 ────────────────────────────────────────

def test_opensearch_fallback_uses_terms(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def search(self, index=None, body=None):
            captured["body"] = body
            return {"hits": {"hits": []}}

    fake_mod = types.ModuleType("opensearchpy")
    fake_mod.OpenSearch = _FakeClient
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_mod)

    from opensearch_pipeline import retriever

    class _OS:
        host = "h"
        port = 9200
        auth_user = None
        auth_password = None
        use_ssl = False
        verify_certs = False
        index_name = "idx"

    class _Rag:
        allowed_depts_acl = False        # Phase D flag 默认关

    class _Cfg:
        opensearch = _OS()
        rag = _Rag()

    monkeypatch.setattr(retriever, "get_config", lambda: _Cfg())

    retriever._search_chunks_opensearch("q", [0.1] * 8, top_k=5,
                                        user_dept=["marketing", "production"])

    perm_should = captured["body"]["query"]["bool"]["filter"][0]["bool"]["should"]
    dept_clauses = [c for c in perm_should if "bool" in c]
    assert dept_clauses, perm_should
    terms = [m for m in dept_clauses[0]["bool"]["must"] if "terms" in m]
    # 'production' 伞组展开为各 production* 子线 owner（与 HA3 _build_permission_filter 同源）；
    # marketing 仍精确。期望值由 _expand_groups_to_owners 派生，新增子线时自动跟随。
    assert terms and terms[0]["terms"]["owner_dept"] == \
        retriever._expand_groups_to_owners(["marketing", "production"])
    assert "production_mold" in terms[0]["terms"]["owner_dept"]
    # flag 关 → 不追加 allowed_depts 分支
    assert all("allowed_depts" not in m.get("terms", {}) for c in dept_clauses for m in c["bool"]["must"])


# ── Phase D：allowed_depts ACL OR 项（RAG_ALLOWED_DEPTS_ACL，默认关）─────────────

def _stub_flag(monkeypatch, on):
    from opensearch_pipeline import retriever

    class _Rag:
        allowed_depts_acl = on

    class _Cfg:
        rag = _Rag()

    monkeypatch.setattr(retriever, "get_config", lambda: _Cfg())
    return retriever


def test_permission_filter_flag_off_byte_identical(monkeypatch):
    """约束 5：flag 关时过滤串与历史逐字节一致（无 allowed_depts）。"""
    r = _stub_flag(monkeypatch, False)
    f = r._build_permission_filter("finance")
    assert f == '(permission_level="public") OR (permission_level="dept_internal" AND (owner_dept="finance"))'
    assert "allowed_depts" not in f


def test_permission_filter_flag_on_adds_allowed_depts(monkeypatch):
    """flag 开：base 不变 + 追加 allowed_depts OR 分支；restricted 永不出现；public 子句不重复。"""
    r = _stub_flag(monkeypatch, True)
    f = r._build_permission_filter("finance")
    assert '(permission_level="dept_internal" AND (owner_dept="finance"))' in f      # 原 owner 分支不变
    assert '(permission_level="dept_internal" AND (allowed_depts="finance"))' in f   # 追加 allowed 分支
    assert "restricted" not in f
    assert f.count('permission_level="public"') == 1


def test_permission_filter_allowed_uses_groups_not_owner_expansion(monkeypatch):
    """allowed_depts 分支用【组码】(marketing)，不做 owner 伞组展开（与 owner 分支正交）。"""
    r = _stub_flag(monkeypatch, True)
    f = r._build_permission_filter("marketing")
    assert 'owner_dept="production_mold"' in f          # owner 分支：marketing 伞组展开含 production 家族
    assert 'allowed_depts="marketing"' in f             # allowed 分支：只用组码
    assert 'allowed_depts="production_mold"' not in f   # allowed 不展开 owner


def test_permission_filter_empty_groups_public_only_regardless_of_flag(monkeypatch):
    """无合法组 → 仅 public（早返回先于 flag），flag 开也不放行 dept_internal。"""
    r = _stub_flag(monkeypatch, True)
    assert r._build_permission_filter(None) == '(permission_level="public")'
    assert r._build_permission_filter("营销中心") == '(permission_level="public")'  # 中文名非组码 → fail-closed


def test_opensearch_fallback_flag_on_adds_allowed_depts_terms(monkeypatch):
    """本地回退：flag 开 → 追加 allowed_depts terms（= 组码，非 owner 展开）。"""
    captured = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def search(self, index=None, body=None):
            captured["body"] = body
            return {"hits": {"hits": []}}

    fake_mod = types.ModuleType("opensearchpy")
    fake_mod.OpenSearch = _FakeClient
    monkeypatch.setitem(sys.modules, "opensearchpy", fake_mod)

    from opensearch_pipeline import retriever

    class _OS:
        host = "h"
        port = 9200
        auth_user = None
        auth_password = None
        use_ssl = False
        verify_certs = False
        index_name = "idx"

    class _Rag:
        allowed_depts_acl = True

    class _Cfg:
        opensearch = _OS()
        rag = _Rag()

    monkeypatch.setattr(retriever, "get_config", lambda: _Cfg())
    retriever._search_chunks_opensearch("q", [0.1] * 8, top_k=5, user_dept=["marketing", "production"])

    perm_should = captured["body"]["query"]["bool"]["filter"][0]["bool"]["should"]
    dept_clauses = [c for c in perm_should if "bool" in c]
    assert len(dept_clauses) == 2          # owner_dept 分支 + allowed_depts 分支
    allowed_terms = [m for dc in dept_clauses for m in dc["bool"]["must"]
                     if "terms" in m and "allowed_depts" in m["terms"]]
    assert allowed_terms and allowed_terms[0]["terms"]["allowed_depts"] == ["marketing", "production"]
