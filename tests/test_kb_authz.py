# -*- coding: utf-8 -*-
"""
test_kb_authz.py — 知识库写授权层边界（读 ≠ 管理 ≠ 授权 三分原则）

核心回归：marketing 管理员的【读】组含 production（_DEPT_NAME_TO_GROUPS["国际贸易部"]
= [marketing, production]），但【写】范围绝不含 production —— 即写授权不得从读组推导。
其余覆盖：fail-closed 角色/净化、公开需 kb_admin、跨组共享需审批。
（退役授权不在本层：用 managed_owner_depts 作用域 + 端点内「公开需 kb_admin」规则，见 api.py::kb_retire。）
"""

from opensearch_pipeline import kb_authz as ka
from opensearch_pipeline.kb_authz import (
    KbIdentity,
    ROLE_EMPLOYEE,
    ROLE_DEPT_ADMIN,
    ROLE_KB_ADMIN,
)


# ── 角色归一 fail-closed ──────────────────────────────────────────
def test_normalize_role_fail_closed():
    assert ka.normalize_role(None) == ROLE_EMPLOYEE
    assert ka.normalize_role("") == ROLE_EMPLOYEE
    assert ka.normalize_role("ADMIN") == ROLE_EMPLOYEE        # 未知 → employee
    assert ka.normalize_role("Dept_Admin") == ROLE_DEPT_ADMIN
    assert ka.normalize_role("kb_admin") == ROLE_KB_ADMIN


# ── 身份构造净化 ──────────────────────────────────────────────────
def test_identity_build_sanitizes_grants():
    ident = KbIdentity.build(
        user_id="u1", role="dept_admin",
        acl_groups="marketing,production",
        granted_owner_depts=["marketing", "nonsense_dept", 'x" OR 1=1'],
    )
    assert ident.role == ROLE_DEPT_ADMIN
    assert ident.acl_groups == ("marketing", "production")   # 读组仅作展示参考
    assert list(ident.granted_owner_depts) == ["marketing"]  # 非法/未知 owner 丢弃


def test_sanitize_owner_depts_forms():
    assert ka.sanitize_owner_depts("marketing,production") == ["marketing", "production"]
    assert ka.sanitize_owner_depts(["finance", "finance"]) == ["finance"]          # 去重
    assert ka.sanitize_owner_depts(None) == []
    assert ka.sanitize_owner_depts("营销中心") == []                                # 中文名非组代码
    assert ka.sanitize_owner_depts(['x" OR owner_dept="finance']) == []            # 注入净化后非白名单
    assert ka.sanitize_owner_depts("production_mold") == []                        # 历史子线非写白名单


# ── 入口可见性 ────────────────────────────────────────────────────
def test_console_access_by_role():
    assert not ka.can_access_console(KbIdentity.build(role="employee"))
    assert ka.can_access_console(KbIdentity.build(role="dept_admin", granted_owner_depts=["hr"]))
    assert ka.can_access_console(KbIdentity.build(role="kb_admin"))


# ── managed / grantable 范围 ──────────────────────────────────────
def test_managed_owner_depts_by_role():
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS

    emp = KbIdentity.build(role="employee", granted_owner_depts=["hr"])
    assert ka.managed_owner_depts(emp) == []                  # employee 无写权（即便误 seed）

    da = KbIdentity.build(role="dept_admin", granted_owner_depts=["marketing"])
    assert ka.managed_owner_depts(da) == ["marketing"]
    assert ka.grantable_owner_depts(da) == ["marketing"]      # 免审批共享面 == managed

    kb = KbIdentity.build(role="kb_admin")
    assert set(ka.managed_owner_depts(kb)) == set(_VALID_ACL_GROUPS)  # kb_admin 全量


# ── ⭐ 核心回归：读组含 production，但写权不含 ──────────────────────
def test_read_groups_do_not_grant_write_authority():
    """国际贸易部管理员：读组 [marketing, production]，managed 仅 {marketing}。

    必须证明：能写 marketing，但【不能】写 production —— 写授权不从读组推导。
    """
    trade_admin = KbIdentity.build(
        user_id="trade1", role="dept_admin",
        acl_groups=["marketing", "production"],   # 读：含 production
        granted_owner_depts=["marketing"],        # 写：仅 marketing（显式 seed）
    )
    # 能写自己的 owner_dept，dept_internal 直接发布
    d_ok = ka.authorize_upload(trade_admin, "marketing", "dept_internal")
    assert d_ok.allowed and not d_ok.requires_kb_admin_approval

    # 关键：尽管读组含 production，写 production 必须被拒
    d_block = ka.authorize_upload(trade_admin, "production", "dept_internal")
    assert not d_block.allowed
    assert d_block.reason == "owner_dept_not_managed"


# ── 公开需 kb_admin 审批 ──────────────────────────────────────────
def test_public_requires_kb_admin_approval():
    da = KbIdentity.build(role="dept_admin", granted_owner_depts=["finance"])
    d = ka.authorize_upload(da, "finance", "public")
    assert d.allowed and d.requires_kb_admin_approval
    assert d.reason == "public_requires_kb_admin"

    # kb_admin 自身即审批人 → 公开免审批
    kb = KbIdentity.build(role="kb_admin")
    d2 = ka.authorize_upload(kb, "finance", "public")
    assert d2.allowed and not d2.requires_kb_admin_approval


# ── 跨组共享需审批；同组共享免审批 ───────────────────────────────
def test_cross_group_share_requires_approval():
    da = KbIdentity.build(role="dept_admin", granted_owner_depts=["marketing"])
    # 共享给非 managed 的 finance → 需审批
    d = ka.authorize_upload(da, "marketing", "dept_internal", share_owner_depts=["finance"])
    assert d.allowed and d.requires_kb_admin_approval
    assert d.reason == "cross_group_share_requires_kb_admin"
    # 共享给自身 managed（marketing）→ 免审批
    d2 = ka.authorize_upload(da, "marketing", "dept_internal", share_owner_depts=["marketing"])
    assert d2.allowed and not d2.requires_kb_admin_approval
    # 非法共享目标（净化后丢弃，数量减少）→ 也转审批，不静默放行
    d3 = ka.authorize_upload(da, "marketing", "dept_internal", share_owner_depts=['x" OR 1=1'])
    assert d3.allowed and d3.requires_kb_admin_approval


# ── 硬拒绝路径 ────────────────────────────────────────────────────
def test_hard_denials():
    emp = KbIdentity.build(role="employee")
    assert ka.authorize_upload(emp, "hr", "dept_internal").reason == "not_admin"

    da = KbIdentity.build(role="dept_admin", granted_owner_depts=["hr"])
    assert not ka.authorize_upload(da, "definitely_not_a_group", "dept_internal").allowed
    assert ka.authorize_upload(da, "definitely_not_a_group", "dept_internal").reason == "invalid_owner_dept"
    assert ka.authorize_upload(da, "hr", "bogus_level").reason == "invalid_permission_level"
    # owner 合法但不在 managed
    assert ka.authorize_upload(da, "finance", "dept_internal").reason == "owner_dept_not_managed"


# ── grant 审计 ────────────────────────────────────────────────────
def test_audit_managed_grants_surfaces_bad():
    bad = ka.audit_managed_grants(["marketing", "typo_dept", "production_mold"])
    assert "typo_dept" in bad and "production_mold" in bad
    assert "marketing" not in bad           # 合法项不报
    assert ka.audit_managed_grants(["finance"]) == []


def test_normalize_permission_level_fail_closed():
    """未知/空 → restricted（最严，fail-closed，G8）；合法值与别名仍正确。"""
    assert ka.normalize_permission_level("") == ka.PERM_RESTRICTED
    assert ka.normalize_permission_level(None) == ka.PERM_RESTRICTED
    assert ka.normalize_permission_level("garbage") == ka.PERM_RESTRICTED
    assert ka.normalize_permission_level("internal") == ka.PERM_DEPT_INTERNAL   # 别名仍生效
    assert ka.normalize_permission_level("public") == ka.PERM_PUBLIC
    assert ka.normalize_permission_level("RESTRICTED") == ka.PERM_RESTRICTED
