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
