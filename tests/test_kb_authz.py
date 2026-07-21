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
    # 2026-07-04 拍板：共享=归属部门管理员职权 → 共享目标面=全量白名单（写权仍只 managed）
    assert set(ka.grantable_owner_depts(da)) == set(_VALID_ACL_GROUPS)
    assert ka.grantable_owner_depts(emp) == []                # employee 无共享权

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


# ── 共享目标（2026-07-04 拍板：跨组共享=归属部门管理员职权，不再转审批）──────
def test_cross_group_share_is_dept_admin_authority():
    da = KbIdentity.build(role="dept_admin", granted_owner_depts=["marketing"])
    # 共享给任意合法目标组（finance）→ 直接允许，不进 kb_admin 队列
    d = ka.authorize_upload(da, "marketing", "dept_internal", share_owner_depts=["finance"])
    assert d.allowed and not d.requires_kb_admin_approval
    # 共享给自身 managed（marketing）→ 同样免审批
    d2 = ka.authorize_upload(da, "marketing", "dept_internal", share_owner_depts=["marketing"])
    assert d2.allowed and not d2.requires_kb_admin_approval
    # 非法共享目标（净化后丢弃，数量减少）→ 仍转审批复核，不静默放行
    d3 = ka.authorize_upload(da, "marketing", "dept_internal", share_owner_depts=['x" OR 1=1'])
    assert d3.allowed and d3.requires_kb_admin_approval
    assert d3.reason == "invalid_share_targets_require_review"


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


# ── 管理作用域伞形展开（2026-07-16 子部门探索）─────────────────────
def test_expand_managed_production_umbrella_covers_family():
    from opensearch_pipeline.retriever import _PRODUCTION_UMBRELLA_OWNERS

    out = ka.expand_managed_owner_depts(["production"])
    assert set(out) == set(_PRODUCTION_UMBRELLA_OWNERS)
    assert "production_paper_cup" in out and "production_straw" in out


def test_expand_managed_marketing_stays_exact_read_ne_manage():
    # ⭐ 读≠管理：marketing 读侧共享 production 家族，但管理作用域绝不因此扩大
    assert ka.expand_managed_owner_depts(["marketing"]) == ["marketing"]


def test_expand_managed_mixed_and_empty():
    out = ka.expand_managed_owner_depts(["hr", "production"])
    assert "hr" in out and "production_mold" in out and "marketing" not in out
    assert ka.expand_managed_owner_depts([]) == []
    assert ka.expand_managed_owner_depts(None) == []


def test_kb_can_manage_subline_docs_via_umbrella():
    """production dept_admin 可管理子线 owner 的既有文档；marketing 不行（读≠管理）。"""
    from opensearch_pipeline.api import _kb_can_manage

    prod_da = KbIdentity.build(role="dept_admin", granted_owner_depts=["production"])
    mkt_da = KbIdentity.build(role="dept_admin", granted_owner_depts=["marketing"])
    kb = KbIdentity.build(role="kb_admin")
    for sub in ("production_paper_cup", "production_thermoforming",
                "production_injection", "production_straw", "production_mold"):
        assert _kb_can_manage(prod_da, sub), f"production 管理员应可管理 {sub}"
        assert _kb_can_manage(kb, sub)
        assert not _kb_can_manage(mkt_da, sub), f"marketing 管理员不得管理 {sub}"
    assert not _kb_can_manage(prod_da, "production_papercup")   # 未批准拼写仍 fail-closed


def test_kb_owner_scope_sql_expands_umbrella():
    from opensearch_pipeline.api import _kb_owner_scope_sql

    prod_da = KbIdentity.build(role="dept_admin", granted_owner_depts=["production"])
    clause, params = _kb_owner_scope_sql(prod_da)
    assert "owner_dept IN" in clause
    assert "production_paper_cup" in params and "production_straw" in params
    # 非 production 管理员保持精确集合
    hr_da = KbIdentity.build(role="dept_admin", granted_owner_depts=["hr"])
    _, hr_params = _kb_owner_scope_sql(hr_da)
    assert hr_params == ["hr"]


def test_upload_target_depts_production_sublines():
    """2026-07-17 拍板:生产子线开放为上传目标——只细化目标粒度,不扩 writer 受众。"""
    prod_admin = KbIdentity(user_id="p1", role=ka.ROLE_DEPT_ADMIN,
                            granted_owner_depts=("production",))
    hr_admin = KbIdentity(user_id="h1", role=ka.ROLE_DEPT_ADMIN, granted_owner_depts=("hr",))
    kb_admin = KbIdentity(user_id="k1", role=ka.ROLE_KB_ADMIN)
    employee = KbIdentity(user_id="e1", role=ka.ROLE_EMPLOYEE)

    # 下拉选项 = 伞值 + 8 个已批准子线(与 retriever 伞形白名单单一来源;
    # 2026-07-20 拍板 +吹膜/纸箱/纸浆模塑)
    opts = ka.upload_target_depts(prod_admin)
    assert opts[0] == "production"
    assert set(o for o in opts if o.startswith("production_")) == {
        "production_mold", "production_paper_cup", "production_thermoforming",
        "production_injection", "production_straw",
        "production_blown_film", "production_carton", "production_pulp_molding"}
    # 非 production 管理员:无子线
    assert all(not o.startswith("production_") for o in ka.upload_target_depts(hr_admin))
    # kb_admin:全量写白名单 + 子线
    assert "production_straw" in ka.upload_target_depts(kb_admin)

    # 裁决:管 production → 子线放行
    assert ka.authorize_upload(prod_admin, "production_straw", "dept_internal").allowed
    assert ka.authorize_upload(kb_admin, "production_injection", "dept_internal").allowed
    # 不管 production → 子线拒(受众未扩大)
    d = ka.authorize_upload(hr_admin, "production_straw", "dept_internal")
    assert not d.allowed and d.reason == "owner_dept_not_managed"
    # 员工恒拒
    assert not ka.authorize_upload(employee, "production_straw", "dept_internal").allowed
    # 双拼写/未批准值仍非法(白名单外 fail-closed)
    bad = ka.authorize_upload(prod_admin, "production_papercup", "dept_internal")
    assert not bad.allowed and bad.reason == "invalid_owner_dept"
