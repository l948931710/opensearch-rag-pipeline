# -*- coding: utf-8 -*-
"""组织同步 job 的不变量测试(不打网络、不打 DB)。

要锁死的:
  · 半截快照必须整轮失败(否则真实在册部门被判"消失"→ is_active=0 → 误判孤儿授权);
  · 消失的部门/人**置 0 不删行**(删了,授权给该节点的文档会静默不可见且查不出原因);
  · dry-run 绝不写;
  · 孤儿授权只告警不自动删。
"""
import pytest

from opensearch_pipeline import org_sync


class Cur:
    def __init__(self, old_depts=(), old_staff=(), grants=(), max_rev=3):
        self.old_depts, self.old_staff, self.grants, self.max_rev = old_depts, old_staff, grants, max_rev
        self._rows, self.writes, self.rowcount = [], [], 0

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if "MAX(snapshot_rev)" in s:
            self._rows = [(self.max_rev + 1,)]
        elif "SELECT dept_id FROM dept_dim" in s:
            self._rows = [(d,) for d in self.old_depts]
        elif "SELECT staff_id FROM staff_dim" in s:
            self._rows = [(s2,) for s2 in self.old_staff]
        elif "kb_doc_node_grant" in s:
            self._rows = list(self.grants)
        elif s.startswith("UPDATE"):
            self.writes.append(("UPDATE", s, args))
            self.rowcount = 1
        else:
            self._rows = []

    def executemany(self, sql, rows):
        self.writes.append(("MANY", " ".join(sql.split()), list(rows)))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Conn:
    def __init__(self, cur):
        self._cur, self.committed = cur, False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _patch(monkeypatch, depts, staff, cur):
    monkeypatch.setattr(org_sync, "fetch_org", lambda tok=None: (depts, staff))
    import opensearch_pipeline.db as db
    monkeypatch.setattr(db, "_get_db_conn", lambda: Conn(cur))


DEPTS = {10: {"parent_id": 1, "name": "生产中心", "depth": 1},
         11: {"parent_id": 10, "name": "模具车间", "depth": 2}}
STAFF = {"u1": {11}, "u2": {10, 11}}


def test_dry_run_writes_nothing(monkeypatch):
    cur = Cur(old_depts=[10, 11], old_staff=["u1"])
    _patch(monkeypatch, DEPTS, STAFF, cur)
    st = org_sync.sync(commit=False)
    assert cur.writes == [], "dry-run 写库了"
    assert st["dept_total"] == 2 and st["staff_total"] == 2
    assert st["staff_added"] == 1


def test_commit_upserts_and_deactivates_not_deletes(monkeypatch):
    """★ 消失的部门必须 is_active=0,**绝不 DELETE** —— 删行会让授权给它的文档
    静默对所有人不可见且无从解释。"""
    cur = Cur(old_depts=[10, 11, 99], old_staff=["u1", "u9"])
    _patch(monkeypatch, DEPTS, STAFF, cur)
    st = org_sync.sync(commit=True)
    kinds = [w[0] for w in cur.writes]
    assert kinds.count("MANY") == 2, "dept/staff 各一次 upsert"
    updates = [w[1] for w in cur.writes if w[0] == "UPDATE"]
    assert any("dept_dim SET is_active=0" in u for u in updates)
    assert any("staff_dim SET is_active=0" in u for u in updates)
    assert not any("DELETE" in w[1] for w in cur.writes), "绝不 DELETE"
    assert st["dept_gone"] == [99]


def test_orphan_grants_reported_not_deleted(monkeypatch):
    """授权指向已消失节点 ⇒ 只告警(返回码 2),绝不自动撤销管理员的意图。"""
    cur = Cur(old_depts=[10, 11, 99], grants=[("DOC_X", 99)])
    _patch(monkeypatch, DEPTS, STAFF, cur)
    st = org_sync.sync(commit=False)
    assert st["orphan_grants"] == [("DOC_X", 99)]
    assert not any("kb_doc_node_grant" in str(w) and "UPDATE" in w[0] for w in cur.writes)


def test_partial_snapshot_aborts_whole_round(monkeypatch):
    """★ 任一接口报错必须整轮失败 —— 半截快照会把在册部门判成'消失'。"""
    monkeypatch.setattr(org_sync, "_token", lambda: "t")
    monkeypatch.setattr(org_sync, "_listsub", lambda t, d: (_ for _ in ()).throw(
        RuntimeError("errcode=88 限流")))
    with pytest.raises(RuntimeError):
        org_sync.fetch_org()


def test_cycle_detected(monkeypatch):
    monkeypatch.setattr(org_sync, "_token", lambda: "t")
    seen = {"n": 0}

    def _sub(tok, dept_id):
        seen["n"] += 1
        return [{"dept_id": 10, "name": "环"}] if seen["n"] < 5 else []

    monkeypatch.setattr(org_sync, "_listsub", _sub)
    monkeypatch.setattr(org_sync, "_member_ids", lambda t, d: [])
    with pytest.raises(RuntimeError, match="成环"):
        org_sync.fetch_org()


def test_main_returns_2_on_orphans(monkeypatch, capsys):
    cur = Cur(old_depts=[10, 11, 99], grants=[("DOC_X", 99)])
    _patch(monkeypatch, DEPTS, STAFF, cur)
    assert org_sync.main([]) == 2
    assert "孤儿授权" in capsys.readouterr().out


def test_main_returns_3_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(org_sync, "sync", lambda commit: (_ for _ in ()).throw(RuntimeError("boom")))
    assert org_sync.main([]) == 3


def test_staff_dept_ids_serialized_sorted(monkeypatch):
    cur = Cur()
    _patch(monkeypatch, DEPTS, STAFF, cur)
    org_sync.sync(commit=True)
    staff_rows = [w[2] for w in cur.writes if w[0] == "MANY" and "staff_dim" in w[1]][0]
    by_id = {r[0]: r for r in staff_rows}
    assert by_id["u2"][1] == "10,11"          # 有序、去重、CSV
    assert by_id["u2"][2] == 10               # primary_dept


# ── load_org_tree：两种人数各对应一种授权语义（2026-08-01）────────────────────
class _TreeCur:
    """load_org_tree 的最小游标替身：dept_dim 元数据 + staff_dim 的 dept_ids CSV。"""

    def __init__(self, dept_rows, staff_csv):
        self._dept, self._staff, self._rows = dept_rows, staff_csv, []

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if "FROM dept_dim" in s:
            self._rows = self._dept
        elif "FROM staff_dim" in s:
            self._rows = [(c,) for c in self._staff]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _tree(monkeypatch, dept_rows, staff_csv):
    from opensearch_pipeline import db, org_sync as m
    m._TREE_CACHE.clear()                       # 进程内 TTL 缓存，逐测清
    monkeypatch.setattr(db, "_get_db_conn", lambda: Conn(_TreeCur(dept_rows, staff_csv)))
    return m.load_org_tree()


# (dept_id, parent_id, name, depth, snapshot_rev, age_hours, synced_at)
_TREE_DEPTS = [
    (100, 1, "生产中心", 1, 7, 0, "2026-08-01 00:00:00"),
    (110, 100, "注塑事业部", 2, 7, 0, "2026-08-01 00:00:00"),
    (111, 110, "注塑制程检", 3, 7, 0, "2026-08-01 00:00:00"),
    (120, 100, "吸塑事业部", 2, 7, 0, "2026-08-01 00:00:00"),
]
# 4 人：1 个直挂生产中心、1 个直挂注塑、1 个在注塑制程检、1 个在吸塑
_TREE_STAFF = ["100", "110", "111", "120"]


def test_org_tree_reports_direct_and_subtree_counts(monkeypatch):
    """★ 两个人数必须都回、且语义不同：
    子树数(含下级 d:) vs 直挂数(仅本级 dx:)。此前只回子树数 ⇒ 前端把「仅本级」
    一律按 0 计，勾了中心仅本级人数纹丝不动、看着像白勾。"""
    out = _tree(monkeypatch, _TREE_DEPTS, _TREE_STAFF)
    by = {n["dept_id"]: n for n in out["nodes"]}
    assert by[100]["staff_count"] == 4 and by[100]["direct_staff_count"] == 1   # 生产中心
    assert by[110]["staff_count"] == 2 and by[110]["direct_staff_count"] == 1   # 注塑事业部
    assert by[111]["staff_count"] == 1 and by[111]["direct_staff_count"] == 1   # 叶子：两者相等
    assert by[120]["staff_count"] == 1 and by[120]["direct_staff_count"] == 1
    assert out["staff_total"] == 4


def test_org_tree_direct_count_zero_for_pure_container(monkeypatch):
    """纯容器节点（有子部门但无人直挂）⇒ direct=0 而 subtree>0。
    这正是「勾了子部门却漏掉父节点直挂人员」提示的判据来源 —— direct=0 时不该报警。"""
    out = _tree(monkeypatch, _TREE_DEPTS, ["111", "120"])   # 没人直挂 100/110
    by = {n["dept_id"]: n for n in out["nodes"]}
    assert by[100]["direct_staff_count"] == 0 and by[100]["staff_count"] == 2
    assert by[110]["direct_staff_count"] == 0 and by[110]["staff_count"] == 1


# ── 阶段 B WP0：load_children_index 原子三元组（2026-08-01）───────────────────
class _ChildrenCur:
    """load_children_index 的最小游标替身：(dept_id, parent_id, snapshot_rev, age_hours)。"""

    def __init__(self, rows):
        self._src, self._rows = rows, []

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self._rows = self._src if "FROM dept_dim" in s else []

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _children(monkeypatch, rows):
    from opensearch_pipeline import db, org_sync as m
    m._children_cache_clear()
    monkeypatch.setattr(db, "_get_db_conn", lambda: Conn(_ChildrenCur(rows)))
    return m.load_children_index()


def test_children_index_atomic_triple(monkeypatch):
    """一次查询产出 (rev, fresh, children) 整组值——rev 与索引永远同源。"""
    rev, fresh, idx = _children(monkeypatch, [
        (100, 1, 7, 0), (110, 100, 7, 0), (111, 110, 7, 0), (120, 100, 7, 0)])
    assert rev == 7 and fresh is True
    assert sorted(idx[100]) == [110, 120] and idx[110] == [111]


def test_children_index_stale_after_48h(monkeypatch):
    """>48h ⇒ fresh=False（调用方视同不可用：自动根与后代展开同时失效）。"""
    _, fresh, _ = _children(monkeypatch, [(100, 1, 7, 49)])
    assert fresh is False


def test_children_index_empty_raises(monkeypatch):
    """表空 = 快照未灌 ⇒ 抛 OrgSnapshotUnavailable，绝不返回半截数据。"""
    from opensearch_pipeline.org_sync import OrgSnapshotUnavailable
    with pytest.raises(OrgSnapshotUnavailable):
        _children(monkeypatch, [])


def test_children_index_db_failure_raises(monkeypatch):
    from opensearch_pipeline import db, org_sync as m
    m._children_cache_clear()

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "_get_db_conn", _boom)
    with pytest.raises(m.OrgSnapshotUnavailable):
        m.load_children_index()


def test_children_index_cached_within_ttl(monkeypatch):
    """TTL 内第二次调用不再触发 DB（与 load_org_tree 同旋钮）。"""
    from opensearch_pipeline import db, org_sync as m
    m._children_cache_clear()
    calls = []

    def _conn():
        calls.append(1)
        return Conn(_ChildrenCur([(100, 1, 7, 0)]))

    monkeypatch.setattr(db, "_get_db_conn", _conn)
    a = m.load_children_index()
    b = m.load_children_index()
    assert a == b and len(calls) == 1
