# -*- coding: utf-8 -*-
"""node-ACL 阶段 A 核心判定/投影的单元测试。

覆盖 codex-review 达成共识的四条硬约束:
  ① 投影模式互斥(node 绝不含组码 / legacy 绝不写哨兵)——堵"升版复活 owner"
  ② 读判定真值表(GRANT=false ⇒ node 非 public **无条件** DENY,org_wide_reader 亦不例外)
  ③ 严格数字解析 + 冒号不可被净化(前缀/去冒号/整串三类负例)
  ④ 物理祖先链**不被锚点 `[]` 截断**,超限丢整条通道且不回退 legacy
"""
import pytest

from opensearch_pipeline.acl_policy import (
    ACL_MODE_LEGACY,
    ACL_MODE_NODE,
    MAX_NODE_OR_TERMS,
    NODE_OWNER_SENTINEL,
    AclContext,
    DocAcl,
    InvalidFlagPosture,
    assert_flag_posture,
    can_read_doc,
    format_node_value,
    node_filter_terms,
    normalize_node_ids,
    parse_node_value,
    project_doc_acl,
)
from opensearch_pipeline.dept_ancestry import (
    ANCHOR_GROUPS_BY_DEPT_ID,
    parent_getter_from_index,
    resolve_ancestor_chains,
)

# 生产中心(599318766)→ 模具车间;「其他」(68112184)是显式 [] 锚,用于验证不截断物理链
TREE = {599318766: 1, 728779788: 599318766, 900001: 728779788, 68112184: 1, 900002: 68112184}
GETP = parent_getter_from_index(TREE)


# ── ③ 值域编解码 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "d:59931876x", "d:-1", "d:0", "d:", "d: 123", "599318766",     # 去冒号负例
    "d:599318766,d:1", "production", "d:1.5", None, "", "D:123",
])
def test_parse_node_value_rejects_bad(bad):
    assert parse_node_value(bad) is None


def test_parse_node_value_roundtrip():
    assert parse_node_value(format_node_value(599318766)) == 599318766


def test_format_node_value_rejects_nonpositive():
    for bad in (0, -3):
        with pytest.raises(ValueError):
            format_node_value(bad)


def test_node_value_survives_ha3_sanitizer_check():
    """⚠️ 净化器会删掉冒号 —— 证明节点值绝不能流经它(故本模块自行拼接)。"""
    from opensearch_pipeline.retriever import _sanitize_ha3_filter_value
    assert _sanitize_ha3_filter_value("d:123") != "d:123"


def test_normalize_dedups_and_flags_overflow():
    ids, ov = normalize_node_ids(["d:5", 5, "d:7", "garbage"], limit=10)
    assert ids == [5, 7] and ov is False
    _, ov2 = normalize_node_ids([f"d:{i}" for i in range(1, 12)], limit=10)
    assert ov2 is True


# ── ① 投影模式互斥 ────────────────────────────────────────────────────────────
def test_projection_legacy_keeps_real_owner_and_groups():
    owner, allowed = project_doc_acl(ACL_MODE_LEGACY, "production", ["hr", "hr", "quality"], [9])
    assert owner == "production"
    assert allowed == ["hr", "quality"]          # 去重保序;节点被忽略


def test_projection_node_is_sentinel_and_never_carries_group_codes():
    owner, allowed = project_doc_acl(ACL_MODE_NODE, "production", ["hr", "quality"], [599318766, 1])
    assert owner == NODE_OWNER_SENTINEL          # 真实 owner 绝不进检索投影轴
    assert allowed == ["d:599318766", "d:1"]
    assert not any(":" not in v for v in allowed), "node 投影混入组码 ⇒ 组码 OR 分支会照样放行"


def test_projection_unknown_mode_falls_back_to_legacy_not_node():
    owner, allowed = project_doc_acl("bogus", "production", ["hr"], [5])
    assert owner == "production" and allowed == ["hr"]


def test_sentinel_is_not_expandable_from_any_group():
    """哨兵之所以成立:没有任何用户组会展开出它 ⇒ owner 分支对 node 文档永不命中。"""
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS, _expand_groups_to_owners
    for g in _VALID_ACL_GROUPS:
        assert NODE_OWNER_SENTINEL not in _expand_groups_to_owners([g])


# ── ② 读判定真值表 ────────────────────────────────────────────────────────────
NODE_DOC = DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal",
                  owner_dept="production", node_ids=(599318766,))
CTX_IN = AclContext(groups=("production",), ancestor_dept_ids=(900001, 728779788, 599318766))
CTX_OUT = AclContext(groups=("hr",), ancestor_dept_ids=(900002, 68112184))


def test_node_grant_on_intersection_allows():
    assert can_read_doc(CTX_IN, NODE_DOC, grant_enabled=True, enforce_enabled=True) is True


def test_node_grant_on_no_intersection_denies():
    assert can_read_doc(CTX_OUT, NODE_DOC, grant_enabled=True, enforce_enabled=True) is False


def test_node_grant_off_denies_unconditionally_even_for_authorized_user():
    """★ 核心:'按权威复核'推不出'全部拒绝' —— GRANT=false 必须是无条件 DENY。"""
    assert can_read_doc(CTX_IN, NODE_DOC, grant_enabled=False, enforce_enabled=True) is False


def test_node_grant_off_denies_org_wide_reader_too():
    """总经办全库读**不豁免** GRANT 闸门(否则回滚期仍能看到 node 文档)。"""
    ctx = AclContext(groups=("admin",), org_wide_reader=True)
    assert can_read_doc(ctx, NODE_DOC, grant_enabled=False, enforce_enabled=True) is False
    assert can_read_doc(ctx, NODE_DOC, grant_enabled=True, enforce_enabled=True) is True


def test_node_enforce_off_is_illegal_posture():
    with pytest.raises(InvalidFlagPosture):
        can_read_doc(CTX_IN, NODE_DOC, grant_enabled=True, enforce_enabled=False)
    with pytest.raises(InvalidFlagPosture):
        assert_flag_posture(grant_enabled=True, enforce_enabled=False)
    assert_flag_posture(grant_enabled=False, enforce_enabled=True)      # 合法回滚姿态


def test_untrusted_chain_fails_closed():
    ctx = AclContext(groups=("production",), ancestor_dept_ids=(599318766,), node_channel_ok=False)
    assert can_read_doc(ctx, NODE_DOC, grant_enabled=True, enforce_enabled=True) is False


@pytest.mark.parametrize("perm,want", [("public", True), ("restricted", False), ("", False), ("bogus", False)])
def test_permission_level_axis_is_orthogonal(perm, want):
    doc = DocAcl(mode=ACL_MODE_NODE, permission_level=perm, node_ids=(599318766,))
    assert can_read_doc(CTX_IN, doc, grant_enabled=True, enforce_enabled=True) is want


def test_unknown_mode_yields_public_only():
    doc = DocAcl(mode="bogus", permission_level="dept_internal", node_ids=(599318766,))
    assert can_read_doc(CTX_IN, doc, grant_enabled=True, enforce_enabled=True) is False


def test_legacy_doc_unaffected_by_node_flags():
    """存量文档行为逐字节不变 —— 两个 flag 任意组合都不改变 legacy 判定。"""
    doc = DocAcl(mode=ACL_MODE_LEGACY, permission_level="dept_internal", owner_dept="production")
    ctx_ok = AclContext(groups=("production",))
    ctx_no = AclContext(groups=("hr",))
    for grant in (False, True):
        assert can_read_doc(ctx_ok, doc, grant_enabled=grant, enforce_enabled=True) is True
        assert can_read_doc(ctx_no, doc, grant_enabled=grant, enforce_enabled=True) is False


def test_legacy_cross_dept_group_grant_allows():
    doc = DocAcl(mode=ACL_MODE_LEGACY, permission_level="dept_internal",
                 owner_dept="quality", groups=("marketing",))
    assert can_read_doc(AclContext(groups=("marketing",)), doc,
                        grant_enabled=True, enforce_enabled=True) is True


# ── ④ 物理祖先链 ──────────────────────────────────────────────────────────────
def test_chain_includes_self_excludes_virtual_root():
    ids, ok = resolve_ancestor_chains([900001], GETP)
    assert ok and ids == [900001, 728779788, 599318766]


def test_chain_not_truncated_by_explicit_empty_anchor():
    """★「其他」(68112184) 在锚点表是显式 [] = 组码解析停止;但**物理链不得被截断**,
    否则授权其父节点将覆盖不到它,违反纯子树语义。"""
    assert ANCHOR_GROUPS_BY_DEPT_ID.get(68112184) == []
    ids, ok = resolve_ancestor_chains([900002], GETP)
    assert ok and 68112184 in ids


def test_chain_union_dedups_shared_ancestors():
    ids, ok = resolve_ancestor_chains([900001, 728779788], GETP)
    assert ok and ids == [900001, 728779788, 599318766]


def test_chain_fails_closed_on_broken_parent_lookup():
    ids, ok = resolve_ancestor_chains([424242], GETP)     # 不在快照 ⇒ 查询失败
    assert ok is False and ids == []


def test_chain_fails_closed_on_cycle():
    ids, ok = resolve_ancestor_chains([1001], parent_getter_from_index({1001: 1002, 1002: 1001}))
    assert ok is False and ids == []


def test_chain_fails_closed_on_too_many_direct_depts():
    ids, ok = resolve_ancestor_chains(list(range(2, 20)), GETP, max_direct_depts=8)
    assert ok is False and ids == []


def test_filter_terms_drop_whole_channel_on_overflow():
    """超限丢**整条通道**(不截断取前 N,顺序不稳定;也绝不回退 legacy 更宽的口径)。"""
    ctx = AclContext(ancestor_dept_ids=tuple(range(1, MAX_NODE_OR_TERMS + 5)))
    assert node_filter_terms(ctx) == []
    ok_ctx = AclContext(ancestor_dept_ids=(599318766, 728779788))
    assert node_filter_terms(ok_ctx) == ["d:599318766", "d:728779788"]


def test_filter_terms_empty_when_channel_untrusted():
    assert node_filter_terms(AclContext(ancestor_dept_ids=(5,), node_channel_ok=False)) == []
