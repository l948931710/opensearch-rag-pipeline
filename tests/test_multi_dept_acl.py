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
    # field-drift 回归：public 子句必须用 permission_level（与 dept/allowed_depts 分支及 HA3
    # _build_permission_filter 同字段），不得回退到 kb_type。
    assert perm_should[0] == {"term": {"permission_level": "public"}}, perm_should
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


# ── Phase D：查询侧拒绝（read-side deny）—— 撤销跨部门授权后即时生效，不等 HA3 投影收回 ────

class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCur()

    def close(self):
        pass


def _stub_deny(monkeypatch, *, flag, authorized=None, db_raises=False):
    """桩：retriever.get_config(flag) + _get_db_conn + resolve_allowed_depts（受控权威）。"""
    from opensearch_pipeline import retriever

    class _Rag:
        allowed_depts_acl = flag

    class _Cfg:
        rag = _Rag()

    monkeypatch.setattr(retriever, "get_config", lambda: _Cfg())

    def _conn():
        if db_raises:
            raise RuntimeError("DB down")
        return _FakeConn()

    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", _conn)
    monkeypatch.setattr(
        "opensearch_pipeline.access_grants.resolve_allowed_depts",
        lambda ids, cur: dict(authorized or {}),
    )
    return retriever


def _rows():
    # finance 用户：own=finance(dept_internal) 同部门；mkt_x=marketing(dept_internal) 跨部门；pub=public。
    return [
        {"doc_id": "own", "owner_dept": "finance", "permission_level": "dept_internal"},
        {"doc_id": "mkt_x", "owner_dept": "marketing", "permission_level": "dept_internal"},
        {"doc_id": "pub", "owner_dept": "marketing", "permission_level": "public"},
    ]


def test_deny_flag_off_passthrough(monkeypatch):
    r = _stub_deny(monkeypatch, flag=False)
    rows = _rows()
    assert r._deny_revoked_cross_dept(rows, "finance") == rows   # flag 关：原样返回（不建连）


def test_deny_keeps_cross_dept_with_live_grant(monkeypatch):
    # 权威仍有 mkt_x→finance 的 approved 授权 → 保留。
    r = _stub_deny(monkeypatch, flag=True, authorized={"mkt_x": ["finance"]})
    out = {x["doc_id"] for x in r._deny_revoked_cross_dept(_rows(), "finance")}
    assert out == {"own", "mkt_x", "pub"}


def test_deny_drops_revoked_cross_dept(monkeypatch):
    # 权威无 mkt_x 的 approved 授权（已撤销/投影未收回）→ 丢弃跨部门命中，同部门/public 保留。
    r = _stub_deny(monkeypatch, flag=True, authorized={})
    out = {x["doc_id"] for x in r._deny_revoked_cross_dept(_rows(), "finance")}
    assert out == {"own", "pub"}


def test_deny_other_group_grant_does_not_match(monkeypatch):
    # mkt_x 仅授权给 hr（非 finance）→ finance 用户仍被丢弃（按组码交集判定）。
    r = _stub_deny(monkeypatch, flag=True, authorized={"mkt_x": ["hr"]})
    out = {x["doc_id"] for x in r._deny_revoked_cross_dept(_rows(), "finance")}
    assert out == {"own", "pub"}


def test_deny_fail_closed_on_db_error(monkeypatch):
    # 权威不可达 → fail-closed：丢弃全部跨部门命中，保留同部门/public。
    r = _stub_deny(monkeypatch, flag=True, db_raises=True)
    out = {x["doc_id"] for x in r._deny_revoked_cross_dept(_rows(), "finance")}
    assert out == {"own", "pub"}


def test_deny_no_cross_dept_skips_db(monkeypatch):
    # 全为同部门/public（无跨部门命中）→ 不建连即原样返回（即便 DB 会抛错也不触发）。
    r = _stub_deny(monkeypatch, flag=True, db_raises=True)
    rows = [
        {"doc_id": "own", "owner_dept": "finance", "permission_level": "dept_internal"},
        {"doc_id": "pub", "owner_dept": "marketing", "permission_level": "public"},
    ]
    assert {x["doc_id"] for x in r._deny_revoked_cross_dept(rows, "finance")} == {"own", "pub"}
