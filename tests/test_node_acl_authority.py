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


# ── strict 姿态（serving ENFORCE 专用；写路径的宽松语义必须原样保留）─────────
import pytest                                                       # noqa: E402

from opensearch_pipeline import access_grants                       # noqa: E402
from opensearch_pipeline.access_grants import NodeAclAuthorityUnavailable  # noqa: E402


# capability positive cache 的每测清空由 conftest 的 `_reset_node_acl_capability_cache`
# 统一负责（它是进程内全局状态，污染会跨【文件】发生 —— 只在本文件清不够）。


class _ProbeRaisingCursor(FakeCursor):
    def execute(self, sql, args=()):
        if "information_schema.COLUMNS" in " ".join(sql.split()):
            raise RuntimeError("information_schema 不可达")
        super().execute(sql, args)


def test_strict_capability_probe_failure_raises():
    """strict：capability 探测异常 ⇒ 抛（调用方整体 fail-closed），不再伪装成『未 apply』。"""
    with pytest.raises(NodeAclAuthorityUnavailable):
        resolve_doc_acl(["D1"], _ProbeRaisingCursor(meta=META_NODE), strict=True)


def test_lenient_capability_probe_failure_still_degrades_to_legacy():
    """宽松（写路径）：保持原行为不变 —— 探测失败按未 apply 处理。"""
    acl = resolve_doc_acl(["D1"], _ProbeRaisingCursor(meta=META_NODE))["D1"]
    assert acl.mode == ACL_MODE_LEGACY


def test_strict_schema_not_applied_is_not_a_failure():
    """探测成功且 060 确未 apply ⇒ 合法部署态，strict 下同样返回全 legacy（不抛）。"""
    cur = FakeCursor(has_node_col=False, meta=META_LEGACY)
    assert resolve_doc_acl(["D1"], cur, strict=True)["D1"].mode == ACL_MODE_LEGACY


def test_strict_node_grant_read_failure_raises():
    """strict：『读不到授权表』≠『这篇零授权』⇒ 抛，交调用方整体 fail-closed。"""
    cur = FakeCursor(meta=META_NODE, nodes=[("D1", 5, "subtree")])
    cur.node_query_raises = True
    with pytest.raises(NodeAclAuthorityUnavailable):
        resolve_doc_acl(["D1"], cur, strict=True)


def test_strict_node_grant_empty_is_not_a_failure():
    """节点授权正常返回空集 ⇒ 零授权 DocAcl，不得误判为故障。"""
    cur = FakeCursor(meta=META_NODE, nodes=[])
    acl = resolve_doc_acl(["D1"], cur, strict=True)["D1"]
    assert acl.mode == ACL_MODE_NODE and acl.node_ids == () and acl.exact_node_ids == ()


def test_strict_missing_meta_row_is_omitted():
    """strict：缺 document_meta 行 ⇒ **不返回该 doc**（绝不合成可被 owner 放行的 legacy ACL）。"""
    assert resolve_doc_acl(["GONE"], FakeCursor(meta=[]), strict=True) == {}


def test_strict_unknown_mode_is_omitted():
    """strict：未知 acl_mode ⇒ 不返回该 doc（宽松路径仍改写为 legacy）。"""
    cur = FakeCursor(meta=[("D1", "dept_internal", "production", "bogus")])
    assert resolve_doc_acl(["D1"], cur, strict=True) == {}


# ── capability positive-only cache ──────────────────────────────────────────
class _CountingCursor(FakeCursor):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.probe_calls = 0
        self.execute_calls = 0

    def execute(self, sql, args=()):
        self.execute_calls += 1
        if "information_schema.COLUMNS" in " ".join(sql.split()):
            self.probe_calls += 1
        super().execute(sql, args)


def test_capability_true_is_cached_and_query_count_drops():
    """cold=4 次 execute（probe+meta+grants+nodes）；warm=3 次（probe 被缓存吃掉）。"""
    cur = _CountingCursor(meta=META_NODE, nodes=[])
    resolve_doc_acl(["D1"], cur)
    assert (cur.probe_calls, cur.execute_calls) == (1, 4)
    cur2 = _CountingCursor(meta=META_NODE, nodes=[])
    resolve_doc_acl(["D1"], cur2)
    assert (cur2.probe_calls, cur2.execute_calls) == (0, 3)


def test_capability_false_is_never_cached():
    """★ False 不缓存 —— 否则 apply 之后、缓存失效之前 serving 仍按 legacy 解析，
    恰好重新制造本批要消灭的 stale-owner 模式混淆。apply 后下一次调用必须立即识别。"""
    cold = _CountingCursor(has_node_col=False, meta=META_LEGACY)
    resolve_doc_acl(["D1"], cold)
    assert cold.probe_calls == 1
    again = _CountingCursor(has_node_col=False, meta=META_LEGACY)
    resolve_doc_acl(["D1"], again)
    assert again.probe_calls == 1, "False 被缓存了"
    applied = _CountingCursor(meta=META_NODE, nodes=[("D1", 7, "subtree")])
    assert resolve_doc_acl(["D1"], applied)["D1"].mode == ACL_MODE_NODE   # 立即生效


def test_capability_exception_is_never_cached():
    """异常不缓存：下一次仍重试（strict 抛、宽松退回 legacy）。"""
    with pytest.raises(NodeAclAuthorityUnavailable):
        resolve_doc_acl(["D1"], _ProbeRaisingCursor(meta=META_NODE), strict=True)
    assert not access_grants._NODE_SCHEMA_PRESENT
    ok = _CountingCursor(meta=META_NODE, nodes=[])
    assert resolve_doc_acl(["D1"], ok, strict=True)["D1"].mode == ACL_MODE_NODE
    assert ok.probe_calls == 1


def test_capability_cache_is_keyed_by_host_and_database(monkeypatch):
    """不同 host/database 的缓存互不共享（测试/staging/多目标进程绝不串值）。"""
    keys = iter([("h1", "db1"), ("h2", "db2")])
    monkeypatch.setattr(access_grants, "_schema_cache_key", lambda: next(keys))
    first = _CountingCursor(meta=META_NODE, nodes=[])
    resolve_doc_acl(["D1"], first)
    second = _CountingCursor(meta=META_NODE, nodes=[])
    resolve_doc_acl(["D1"], second)
    assert second.probe_calls == 1, "换了 host/database 却复用了缓存"
