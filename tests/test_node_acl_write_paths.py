# -*- coding: utf-8 -*-
"""写路径的 node-ACL 投影不变量(codex 评审 BLOCKER-1)。

要锁死的核心事实:chunk.owner_dept 继承自 document_meta 的**真实 owner**,而 stage-2 /
stage-3 / materialize 历史上都**只重算 allowed_depts**。node 文档一旦升版 / re-chunk /
重推,真实 owner 会被写回**检索投影轴**,legacy owner 分支静默复活 = **权限重开**。
"""
import pytest

from opensearch_pipeline.access_grants import materialize_doc_allowed_depts, resolve_acl_modes
from opensearch_pipeline.acl_policy import (
    ACL_MODE_LEGACY, ACL_MODE_NODE, NODE_OWNER_SENTINEL, project_doc_acl,
)


class Cur:
    """按 SQL 关键字分派的游标替身(tuple 行,与 access_grants 契约一致)。"""

    def __init__(self, *, has_col=True, mode="legacy", ver=1, perm="dept_internal",
                 nodes=(), cur_allowed=(), cur_owner="production", grants=()):
        self.has_col, self.mode, self.ver, self.perm = has_col, mode, ver, perm
        self.nodes, self.cur_allowed, self.cur_owner, self.grants = nodes, cur_allowed, cur_owner, grants
        self._rows, self.updates, self.rowcount = [], [], 3

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if "information_schema.COLUMNS" in s:
            self._rows = [(1 if self.has_col else 0,)]
        elif "SELECT doc_id, permission_level, owner_dept" in s:
            # resolve_doc_acl 的四列查询(未 apply 时只有三列)
            self._rows = [("D1", self.perm, "production", self.mode)[:4 if self.has_col else 3]]
        elif "SELECT doc_id, acl_mode" in s:
            # resolve_acl_modes 的两列轻量查询
            self._rows = [("D1", self.mode)]
        elif "current_version_no" in s:
            self._rows = [(self.ver,)]
        elif "kb_doc_node_grant" in s:
            self._rows = list(self.nodes)
        elif "kb_access_request" in s:
            self._rows = list(self.grants)
        elif "GROUP_CONCAT" in s:
            self._rows = [(self.perm,)]
        elif "DISTINCT allowed_depts" in s:
            self._rows = [(a,) for a in self.cur_allowed]
        elif "DISTINCT owner_dept" in s:
            self._rows = [(self.cur_owner,)]
        elif s.startswith("UPDATE"):
            self.updates.append((s, args))
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# ── 投影纯函数:node 绝不带真实 owner / 绝不带组码 ────────────────────────────
def test_node_projection_replaces_real_owner_with_sentinel():
    owner, allowed = project_doc_acl(ACL_MODE_NODE, "production", ["hr"], [7], [9])
    assert owner == NODE_OWNER_SENTINEL, "真实 owner 泄进检索投影轴 = legacy owner 分支复活"
    assert allowed == ["d:7", "dx:9"]
    assert "production" not in allowed and "hr" not in allowed


def test_legacy_projection_unchanged():
    assert project_doc_acl(ACL_MODE_LEGACY, "production", ["hr"], [7]) == ("production", ["hr"])


# ── 模式批查:060 未 apply / 读失败 ⇒ 一律 legacy(绝不误升 node)───────────────
def test_modes_default_legacy_when_migration_absent():
    assert resolve_acl_modes(["D1"], Cur(has_col=False, mode="node")) == {"D1": ACL_MODE_LEGACY}


def test_modes_unknown_value_falls_back_to_legacy():
    assert resolve_acl_modes(["D1"], Cur(mode="bogus")) == {"D1": ACL_MODE_LEGACY}


def test_modes_reads_node():
    assert resolve_acl_modes(["D1"], Cur(mode="node")) == {"D1": ACL_MODE_NODE}


# ── materialize:node 模式必须【同时】改 owner 与 allowed_depts ────────────────
def test_materialize_node_rewrites_owner_to_sentinel():
    """★ 只改 allowed_depts 而留真实 owner ⇒ 等于没收权。"""
    cur = Cur(mode="node", nodes=[("D1", 599318766, "subtree")], cur_owner="production")
    out = materialize_doc_allowed_depts(cur, "D1")
    assert out["status"] == "materialized"
    sql, args = cur.updates[-1]
    assert "owner_dept=%s" in sql, "materialize 未改写 owner ⇒ legacy owner 分支仍放行"
    assert NODE_OWNER_SENTINEL in args
    assert '"d:599318766"' in args[0] or "d:599318766" in str(args)


def test_materialize_node_unchanged_only_when_owner_also_matches():
    """owner 已是哨兵 + 授权集一致 → unchanged;owner 还是真实值 → 必须触发写。"""
    same = dict(mode="node", nodes=[("D1", 7, "subtree")], cur_allowed=['["d:7"]'])
    assert materialize_doc_allowed_depts(
        Cur(cur_owner=NODE_OWNER_SENTINEL, **same), "D1")["status"] == "unchanged"
    cur = Cur(cur_owner="production", **same)
    assert materialize_doc_allowed_depts(cur, "D1")["status"] != "unchanged"
    assert cur.updates, "owner 漂移未触发重投影"


def test_materialize_legacy_does_not_touch_owner():
    """legacy 路径行为逐字节不变:UPDATE 只碰 allowed_depts,不含 owner_dept。"""
    cur = Cur(mode="legacy", grants=[("D1", "marketing")], perm="dept_internal")
    materialize_doc_allowed_depts(cur, "D1")
    assert cur.updates, "legacy 应有写入"
    assert "owner_dept=%s" not in cur.updates[-1][0]


def test_materialize_node_revoked_retracts_and_keeps_sentinel():
    """节点授权全撤 → allowed_depts 清空,但 owner **仍是哨兵**(文档仍是 node 模式,
    绝不因为没授权就把真实 owner 放回去 —— 那会让全组重新可见)。"""
    cur = Cur(mode="node", nodes=[], cur_allowed=['["d:7"]'], cur_owner=NODE_OWNER_SENTINEL)
    out = materialize_doc_allowed_depts(cur, "D1")
    assert out["status"] == "retracted"
    sql, args = cur.updates[-1]
    assert NODE_OWNER_SENTINEL in args and args[0] is None


@pytest.mark.parametrize("apply_flag", [False, True])
def test_materialize_node_preview_does_not_write(apply_flag):
    cur = Cur(mode="node", nodes=[("D1", 7, "subtree")], cur_owner="production")
    materialize_doc_allowed_depts(cur, "D1", apply=apply_flag)
    assert bool(cur.updates) is apply_flag
