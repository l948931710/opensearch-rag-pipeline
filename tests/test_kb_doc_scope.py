# -*- coding: utf-8 -*-
"""阶段 B WP1：文档域 mode 隔离管理判定（kb_authz.can_manage_doc + api 三件套）。

要锁死的（codex 2026-08-01 评审共识）：
  · 严格 mode 隔离——node 文档的 owner_dept 残值绝不命中 legacy 管理员（越权洞）；
  · capability 三态——absent 生成与现行逐字节同构的 SQL，unknown 一律 1=0（仅 kb_admin）；
  · 后代集不可得（None）⇒ node 判定/腿 fail-closed，绝不回退 legacy 残值；
  · 全 legacy 存量语义恒等（回归锚）。
"""
from opensearch_pipeline.kb_authz import KbIdentity, can_manage_doc


def _kb(role="dept_admin", owners=(), roots=()):
    return KbIdentity.build(user_id="u1", role=role,
                            granted_owner_depts=list(owners),
                            granted_node_roots=list(roots))


# ── can_manage_doc 真值表 ─────────────────────────────────────────────────────
def test_kb_admin_manages_everything():
    kb = _kb(role="kb_admin")
    assert can_manage_doc(kb, "legacy", "hr", None, None)
    assert can_manage_doc(kb, "node", None, 599318766, None)
    assert can_manage_doc(kb, "weird", None, None, None)


def test_employee_never_manages():
    kb = _kb(role="employee", owners=["hr"], roots=[100])
    assert not can_manage_doc(kb, "legacy", "hr", None, {100})


def test_legacy_hit_and_miss_with_umbrella():
    kb = _kb(owners=["production"])
    assert can_manage_doc(kb, "legacy", "production", None, None)
    # 伞形展开：子线文档归 production 管理员（与既有 _kb_can_manage 同语义）
    assert can_manage_doc(kb, "legacy", "production_paper_cup", None, None)
    assert not can_manage_doc(kb, "legacy", "hr", None, None)


def test_node_doc_never_matches_legacy_residual_owner():
    """★ 核心隔离：node 文档 owner_dept 残值 = 该管理员的组码，也**不得**放行。"""
    kb = _kb(owners=["production"], roots=())
    assert not can_manage_doc(kb, "node", "production", 599318766, set())


def test_node_hit_and_miss_by_descendants():
    kb = _kb(roots=[100])
    assert can_manage_doc(kb, "node", None, 110, {100, 110, 111})
    assert not can_manage_doc(kb, "node", None, 999, {100, 110, 111})


def test_node_descendants_unavailable_fails_closed():
    """后代集 None（快照过期/读失败）⇒ False，绝不回退 legacy 残值判定。"""
    kb = _kb(owners=["production"], roots=[100])
    assert not can_manage_doc(kb, "node", "production", 110, None)


def test_unknown_mode_and_missing_owner_id_fail_closed():
    kb = _kb(owners=["hr"], roots=[100])
    assert not can_manage_doc(kb, "weird", "hr", 100, {100})
    assert not can_manage_doc(kb, "node", None, None, {100})


def test_legacy_doc_ignores_node_axis():
    """两轴独立：只有 node 管辖根的管理员管不了 legacy 文档。"""
    kb = _kb(owners=(), roots=[100])
    assert not can_manage_doc(kb, "legacy", "hr", None, {100})


# ── _kb_doc_owner_scope_sql ──────────────────────────────────────────────────
def _scope(kb, cap, monkeypatch=None, descendants="unset"):
    from opensearch_pipeline import api
    if descendants != "unset":
        monkeypatch.setattr(api, "_kb_managed_descendants", lambda _kb: descendants)
    return api._kb_doc_owner_scope_sql(kb, cap)


def test_scope_kb_admin_unrestricted():
    assert _scope(_kb(role="kb_admin"), "present") == ("", [], False)


def test_scope_absent_bytes_identical_to_legacy_helper():
    """★ 回归锚：cap=absent 生成的 SQL 与现行 _kb_owner_scope_sql(kb, "m.owner_dept")
    逐字节同构——060 未 apply 的环境行为零变化。"""
    from opensearch_pipeline import api
    kb = _kb(owners=["hr", "production"])
    clause, params, degraded = api._kb_doc_owner_scope_sql(kb, "absent")
    old_clause, old_params = api._kb_owner_scope_sql(kb, "m.owner_dept")
    assert clause == old_clause and params == old_params and degraded is False


def test_scope_absent_no_owners_matches_empty():
    clause, params, degraded = _scope(_kb(owners=()), "absent")
    assert clause == "AND 1=0" and params == [] and not degraded


def test_scope_unknown_fails_closed_and_degraded():
    """探测失败 ≠ 未 apply：unknown 一律 1=0（把它当 absent 会违反「未知仅 kb_admin」裁决）。"""
    clause, params, degraded = _scope(_kb(owners=["hr"]), "unknown")
    assert clause == "AND 1=0" and degraded is True


def test_scope_present_mode_isolated_two_legs(monkeypatch):
    kb = _kb(owners=["hr"], roots=[100])
    clause, params, degraded = _scope(kb, "present", monkeypatch, {100, 110})
    assert "m.acl_mode='legacy' AND m.owner_dept IN" in clause
    assert "m.acl_mode='node' AND m.owner_dept_id IN" in clause
    assert " OR " in clause and params == ["hr", 100, 110] and not degraded


def test_scope_present_node_leg_dropped_when_descendants_unavailable(monkeypatch):
    """后代集 None ⇒ 去 node 腿 + degraded；legacy 腿保留（legacy 文档不受波及）。"""
    kb = _kb(owners=["hr"], roots=[100])
    clause, params, degraded = _scope(kb, "present", monkeypatch, None)
    assert "acl_mode='node'" not in clause and "m.acl_mode='legacy'" in clause
    assert params == ["hr"] and degraded is True


def test_scope_present_no_axis_matches_empty(monkeypatch):
    clause, params, degraded = _scope(_kb(), "present", monkeypatch, set())
    assert clause == "AND 1=0" and params == []


# ── _kb_node_capability 三态 ─────────────────────────────────────────────────
def test_capability_tri_state(monkeypatch):
    from opensearch_pipeline import access_grants, api
    calls = {}

    def fake(cursor, strict=False):
        calls["strict"] = strict
        r = calls["ret"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(access_grants, "_node_acl_columns_present", fake)
    calls["ret"] = True
    assert api._kb_node_capability(None) == "present" and calls["strict"] is True
    calls["ret"] = False
    assert api._kb_node_capability(None) == "absent"
    calls["ret"] = RuntimeError("probe boom")
    assert api._kb_node_capability(None) == "unknown"


# ── _kb_read_doc_triplet 双路 ────────────────────────────────────────────────
class _Cur:
    def __init__(self, row):
        self._row, self.sqls = row, []

    def execute(self, sql, args=()):
        self.sqls.append(" ".join(sql.split()))

    def fetchone(self):
        return self._row


def test_read_triplet_present(monkeypatch):
    from opensearch_pipeline import api
    monkeypatch.setattr(api, "_kb_node_capability", lambda cur: "present")
    cur = _Cur(("", "node", 110, 3, "dept_internal", "active"))
    t = api._kb_read_doc_triplet(cur, "d1")
    assert t["acl_mode"] == "node" and t["owner_dept_id"] == 110 and t["acl_revision"] == 3
    assert "acl_mode" in cur.sqls[0]


def test_read_triplet_absent_falls_back_legacy_columns(monkeypatch):
    from opensearch_pipeline import api
    monkeypatch.setattr(api, "_kb_node_capability", lambda cur: "absent")
    cur = _Cur(("hr", "dept_internal", "active"))
    t = api._kb_read_doc_triplet(cur, "d1")
    assert t["acl_mode"] == "legacy" and t["owner_dept_id"] is None and t["owner_dept"] == "hr"
    assert "acl_mode" not in cur.sqls[0]


def test_read_triplet_missing_doc_none(monkeypatch):
    from opensearch_pipeline import api
    monkeypatch.setattr(api, "_kb_node_capability", lambda cur: "present")
    assert api._kb_read_doc_triplet(_Cur(None), "nope") is None


# ── 条件列存在性一律按 capability 判定，不用 len(row)（2026-08-04）───────────────

def test_no_length_heuristic_for_conditional_acl_columns():
    """★ 禁止用 `len(r) > N` 探测 `_mc`（acl_mode / owner_dept_id）这两列是否存在。

    那是**长度启发式**：只要给同一条 SELECT **追加任何新列**（哪怕加在末位），判据就恒真
    ⇒ 新列被当成 `acl_mode` 读、`owner_dept_id` 读到错值 —— 而这是 **ACL 判定轴**，
    错了**不报错、只错权限**。
    2026-08-04 落 R1 时差点踩上：想给 my-docs 加 `m.acl_revision`，`len(r) > 13` 会立刻恒真。
    """
    import pathlib
    import re
    src = pathlib.Path("opensearch_pipeline/routes/kb_console.py").read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"len\(r\)\s*>\s*\d+", src):
        line_no = src[:m.start()].count("\n") + 1
        line = src.splitlines()[line_no - 1].strip()
        bad.append(f"{line_no}: {line[:100]}")
    assert not bad, (
        "条件列存在性又用回了长度启发式（加尾列即静默错位）：\n  " + "\n  ".join(bad))


def test_conditional_acl_columns_are_read_by_capability():
    """正向：三处 `_mode, _oid` 解包都必须按 capability 分派。"""
    import pathlib
    src = pathlib.Path("opensearch_pipeline/routes/kb_console.py").read_text(encoding="utf-8")
    # 2026-08-05：审批队列补 owner DTO 后由 3 处增至 4 处。数字是**有意**钉死的——新增解包
    # 点必须同步改这里，从而强制作者确认新点也按 capability 分派（而不是抄了个 len(r) 启发式）。
    sites = [ln for ln in src.splitlines() if "_mode, _oid = ((r[" in ln]
    assert len(sites) == 4, f"预期 4 处行级解包，实得 {len(sites)}"
    for ln in sites:
        assert '== "present"' in ln, f"未按 capability 判定：{ln.strip()[:100]}"



# ── 归属 facet 筛选键闭环（2026-08-05）────────────────────────────────────────
# stats.owner_facets 回的 legacy 键带 `legacy:` 前缀，而筛选入参此前只认**裸组码**。
# 键不闭环 = 调用方必须自己记得剥前缀，漏剥则清洗成 'legacyhr' 匹配不到任何行、
# 静默返回空台账（fail-closed 得毫无线索）。这一组把两种形态都钉住。
def _facet(v):
    from opensearch_pipeline.routes.kb_console import _kb_owner_facet_sql
    return _kb_owner_facet_sql(v)


def test_facet_accepts_legacy_prefixed_key_same_as_bare_code():
    """`legacy:hr` 与裸 `hr` 必须产出**同一条**谓词——facet 键可原样回传。"""
    assert _facet("legacy:hr") == _facet("hr") == ("AND m.owner_dept = %s", ["hr"])


def test_facet_node_key_targets_node_axis_only():
    sql, params = _facet("node:34265162")
    assert sql == "AND m.acl_mode = 'node' AND m.owner_dept_id = %s"
    assert params == [34265162]


def test_facet_bare_legacy_prefix_is_rejected_not_silently_unfiltered():
    """裸 `legacy:` 是非法值，必须 fail-closed（None）——绝不能退化成「不筛选」而泄露全作用域。"""
    assert _facet("legacy:") == (None, None)
    assert _facet("legacy:   ") == (None, None)


def test_facet_empty_means_no_filter_and_injection_still_stripped():
    assert _facet("") == ("", [])
    # 剥前缀后仍走同一套清洗：引号/分号被剥掉（连字符按 _SANITIZE_RE 设计保留——
    # 组码里合法），且恒参数化，不拼串。
    assert _facet("legacy:';DROP--") == ("AND m.owner_dept = %s", ["DROP--"])
    # 清洗后为空 = 传了但清不出东西 → fail-closed，不是"不筛选"
    assert _facet("legacy:!!!") == (None, None)
