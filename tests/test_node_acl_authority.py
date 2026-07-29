# -*- coding: utf-8 -*-
"""node-ACL 权威解析(access_grants.resolve_doc_acl)测试。

核心不变量:
  · 两个权威源【分别】解析 —— 组码走白名单、节点值不经白名单(否则 d:/dx: 被静默丢光)
  · schema/060 未 apply ⇒ 全库恒 legacy,行为逐字节不变(node 分支彻底惰化)
  · 节点权威读失败 ⇒ fail-closed 空集,绝不放行
"""
from opensearch_pipeline.access_grants import resolve_doc_acl
from opensearch_pipeline.acl_policy import ACL_MODE_LEGACY, ACL_MODE_NODE


class FakeCursor:
    """按 SQL 关键字分派的最小游标替身(与 tests/test_access_grants.py 同型)。"""

    def __init__(self, *, has_node_col=True, meta=(), grants=(), nodes=()):
        self._has_node = has_node_col
        self._meta, self._grants, self._nodes = meta, grants, nodes
        self._rows = []
        self.node_query_raises = False

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if "information_schema.COLUMNS" in s:
            self._rows = [(1 if self._has_node else 0,)]
        elif "FROM " in s and "document_meta" in s:
            # 未 apply 时调用方不选 acl_mode 列 → 只回 3 列
            self._rows = [r if self._has_node else r[:3] for r in self._meta]
        elif "kb_access_request" in s:
            self._rows = list(self._grants)
        elif "kb_doc_node_grant" in s:
            if self.node_query_raises:
                raise RuntimeError("表不存在")
            self._rows = list(self._nodes)
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


META_NODE = [("D1", "dept_internal", "production", "node")]
META_LEGACY = [("D1", "dept_internal", "production", "legacy")]


def test_node_authority_parsed_separately_from_group_whitelist():
    """★ 节点值不经组码白名单 —— 混在一起过白名单会把 d:/dx: 静默丢光。"""
    cur = FakeCursor(meta=META_NODE,
                     grants=[("D1", "marketing")],
                     nodes=[("D1", 599318766, "subtree"), ("D1", 34274162, "exact")])
    acl = resolve_doc_acl(["D1"], cur)["D1"]
    assert acl.mode == ACL_MODE_NODE
    assert acl.node_ids == (599318766,)
    assert acl.exact_node_ids == (34274162,)
    assert acl.groups == ("marketing",)          # legacy 源仍照常解析(供回滚/审计)


def test_scope_defaults_to_subtree_when_unrecognized():
    cur = FakeCursor(meta=META_NODE, nodes=[("D1", 7, None), ("D1", 8, "SUBTREE"), ("D1", 9, "bogus")])
    acl = resolve_doc_acl(["D1"], cur)["D1"]
    assert acl.node_ids == (7, 8, 9) and acl.exact_node_ids == ()


def test_exact_scope_is_case_insensitive():
    cur = FakeCursor(meta=META_NODE, nodes=[("D1", 7, "EXACT"), ("D1", 8, " exact ")])
    acl = resolve_doc_acl(["D1"], cur)["D1"]
    assert acl.exact_node_ids == (7, 8) and acl.node_ids == ()


def test_migration_not_applied_yields_pure_legacy():
    """★ schema/060 未 apply ⇒ 恒 legacy、node 字段空 —— 代码可先部署,apply 是 user-gated。"""
    cur = FakeCursor(has_node_col=False, meta=META_LEGACY, grants=[("D1", "hr")])
    acl = resolve_doc_acl(["D1"], cur)["D1"]
    assert acl.mode == ACL_MODE_LEGACY
    assert acl.node_ids == () and acl.exact_node_ids == ()
    assert acl.groups == ("hr",)
    assert acl.owner_dept == "production" and acl.permission_level == "dept_internal"


def test_node_table_read_failure_fails_closed():
    """节点权威读失败 ⇒ 空集(不放行),绝不退化成"当作无限制"。"""
    cur = FakeCursor(meta=META_NODE, nodes=[("D1", 5, "subtree")])
    cur.node_query_raises = True
    acl = resolve_doc_acl(["D1"], cur)["D1"]
    assert acl.node_ids == () and acl.exact_node_ids == ()


def test_unknown_mode_falls_back_to_legacy():
    cur = FakeCursor(meta=[("D1", "dept_internal", "production", "bogus")])
    assert resolve_doc_acl(["D1"], cur)["D1"].mode == ACL_MODE_LEGACY


def test_missing_doc_still_returns_failclosed_entry():
    """文档不在 document_meta(已硬删/竞态)⇒ 返回空 permission_level ⇒ can_read_doc 判 False。"""
    cur = FakeCursor(meta=[])
    acl = resolve_doc_acl(["GONE"], cur)["GONE"]
    assert acl.permission_level == "" and acl.mode == ACL_MODE_LEGACY
    from opensearch_pipeline.acl_policy import AclContext, can_read_doc
    assert can_read_doc(AclContext(groups=("production",)), acl,
                        grant_enabled=True, enforce_enabled=True) is False


def test_empty_input_short_circuits():
    assert resolve_doc_acl([], FakeCursor()) == {}
