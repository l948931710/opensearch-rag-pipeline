# -*- coding: utf-8 -*-
"""阶段 B T5：管辖根自动派生（org_sync.derive_admin_node_roots）+ 成员双轴端点。

五条规则的可测形态（设计稿 §3.5，codex 2026-08-01 共识）：
  1 仅 active dept_admin 派生；2 manual 替换 auto（并撤停存量 auto）；
  3 0/多直属 fail-closed（撤停 auto + supersede 候选）；
  4 中心级/超规模/换根 ⇒ 只写候选；已确认的根（auto==派生根）**零变化**不反复撤销；
  5 降级判定看两表（node-only 管理员不误降）。
"""
from typing import Dict, List

import pytest

from opensearch_pipeline.org_sync import derive_admin_node_roots


class _Cur:
    """模式匹配型游标（不按序列弹——派生函数的查询次数是实现细节）。"""

    def __init__(self, admins=(), grants=None):
        # grants: {user_id: [(dept_id, source, is_active), ...]}
        self.admins = list(admins)
        self.grants: Dict[str, List[tuple]] = dict(grants or {})
        self.writes: List[tuple] = []
        self._rows: List[tuple] = []
        self.rowcount = 0

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT user_id FROM user_role"):
            self._rows = [(u,) for u in self.admins]
        elif s.startswith("SELECT managed_dept_id, source, is_active"):
            self._rows = list(self.grants.get(args[0], []))
        elif s.startswith("UPDATE") or s.startswith("INSERT"):
            self.writes.append((s, args))
            self.rowcount = 1
            self._rows = []
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


_DEPTS = {
    100: {"parent_id": 1, "name": "生产中心", "depth": 1},
    110: {"parent_id": 100, "name": "注塑事业部", "depth": 2},
    111: {"parent_id": 110, "name": "注塑制程检", "depth": 3},
    120: {"parent_id": 100, "name": "吸塑事业部", "depth": 2},
}


def _writes_matching(cur, frag):
    return [w for w in cur.writes if frag in w[0]]


def test_safe_first_derive_goes_straight_to_authority():
    """恰 1 直属 + 非中心级 + 规模内 + 无既有行 ⇒ 直接 upsert 权威表 source='auto'。"""
    cur = _Cur(admins=["u1"], grants={})
    out = derive_admin_node_roots(cur, 9, _DEPTS, {"u1": {110}})
    assert out["auto_upserted"] == 1 and out["candidates"] == 0
    ins = _writes_matching(cur, "INSERT INTO dept_admin_node_grant")
    assert len(ins) == 1 and ins[0][1][:2] == ("u1", 110)


def test_manual_override_disables_and_revokes_auto():
    """规则 2：manual 存在 ⇒ 跳过派生 + 撤停存量 auto 行（不留双轨）。"""
    cur = _Cur(admins=["u1"], grants={"u1": [(120, "manual", 1), (110, "auto", 1)]})
    out = derive_admin_node_roots(cur, 9, _DEPTS, {"u1": {110}})
    assert out["skipped_manual"] == 1 and out["auto_upserted"] == 0
    assert _writes_matching(cur, "SET is_active=0") and out["auto_revoked"] == 1


def test_multi_dept_fails_closed_and_supersedes():
    """规则 3：多直属 ⇒ 撤停旧 auto + supersede 候选，不写任何新授权。"""
    cur = _Cur(admins=["u1"], grants={"u1": [(110, "auto", 1)]})
    out = derive_admin_node_roots(cur, 9, _DEPTS, {"u1": {110, 120}})
    assert out["skipped_multi_dept"] == 1 and out["auto_upserted"] == 0
    assert _writes_matching(cur, "dept_admin_node_candidate SET status='superseded'")
    assert not _writes_matching(cur, "INSERT INTO dept_admin_node_grant")


def test_center_level_root_writes_candidate_not_authority():
    """规则 4：直挂中心（depth==1）⇒ 只写候选（实测 97 人直挂一级，中心级根=整个中心管理权）。"""
    cur = _Cur(admins=["u1"], grants={})
    out = derive_admin_node_roots(cur, 9, _DEPTS, {"u1": {100}})
    assert out["candidates"] == 1 and out["auto_upserted"] == 0
    cand = _writes_matching(cur, "INSERT INTO dept_admin_node_candidate")
    assert cand and cand[0][1][3] == "center_level"
    assert not _writes_matching(cur, "INSERT INTO dept_admin_node_grant")


def test_oversized_subtree_writes_candidate(monkeypatch):
    monkeypatch.setenv("RAG_NODE_ADMIN_AUTO_MAX_SUBTREE", "1")
    cur = _Cur(admins=["u1"], grants={})
    out = derive_admin_node_roots(cur, 9, _DEPTS, {"u1": {110}})   # 子树 {110,111}=2 > 1
    assert out["candidates"] == 1
    cand = _writes_matching(cur, "INSERT INTO dept_admin_node_candidate")
    assert cand[0][1][3] == "oversized"


def test_root_changed_revokes_old_auto_and_writes_candidate():
    """规则 4：调岗换根 ⇒ 撤旧 auto + 候选（防调岗静默提权）。"""
    cur = _Cur(admins=["u1"], grants={"u1": [(120, "auto", 1)]})
    out = derive_admin_node_roots(cur, 9, _DEPTS, {"u1": {110}})
    assert out["auto_revoked"] == 1 and out["candidates"] == 1
    cand = _writes_matching(cur, "INSERT INTO dept_admin_node_candidate")
    assert cand[0][1][3] == "root_changed"


def test_confirmed_root_is_stable_across_syncs():
    """★ 关键次序：已生效 auto 根与派生一致 ⇒ 零变化——即使该根是中心级/超规模。
    否则确认过的根每晚被撤销+重开候选，确认动作等于白做。"""
    cur = _Cur(admins=["u1"], grants={"u1": [(100, "auto", 1)]})   # 100 是中心级
    out = derive_admin_node_roots(cur, 9, _DEPTS, {"u1": {100}})
    assert out == {"auto_upserted": 0, "auto_revoked": 0, "candidates": 0,
                   "skipped_multi_dept": 0, "skipped_manual": 0}
    assert cur.writes == []


# ── 成员端点双轴（KbIdentity/请求模型层可测面）────────────────────────────────
def test_grant_request_model_node_axis_semantics():
    """node_roots：None=不动 / []=清空 / [ids]=权威全集——三态必须可区分。"""
    from opensearch_pipeline.routes.kb_access import KbAdminGrantRequest
    assert KbAdminGrantRequest(user_id="u").node_roots is None
    assert KbAdminGrantRequest(user_id="u", node_roots=[]).node_roots == []
    assert KbAdminGrantRequest(user_id="u", node_roots=[110]).node_roots == [110]


def test_grant_endpoint_rejects_root_node(monkeypatch):
    """管辖根=钉钉根 1（全库语义）必须 400——那是 kb_admin 的语义。"""
    from opensearch_pipeline import api
    from opensearch_pipeline.routes.kb_access import KbAdminGrantRequest, kb_admin_grant
    from fastapi import HTTPException
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")
    with pytest.raises(HTTPException) as ei:
        kb_admin_grant(KbAdminGrantRequest(user_id="u2", node_roots=[1]),
                       request=None, identity=api.Identity(user_id="adm1"))
    assert ei.value.status_code == 400


def test_grant_endpoint_still_400_when_both_axes_empty(monkeypatch):
    """回归锚：owner_depts 空 + node_roots 未给 ⇒ 维持既有 400 语义。"""
    from opensearch_pipeline import api
    from opensearch_pipeline.routes.kb_access import KbAdminGrantRequest, kb_admin_grant
    from fastapi import HTTPException
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")
    with pytest.raises(HTTPException) as ei:
        kb_admin_grant(KbAdminGrantRequest(user_id="u2", owner_depts=[]),
                       request=None, identity=api.Identity(user_id="adm1"))
    assert ei.value.status_code == 400


# ── 候选裁决：陈旧 rev 拒确认（TOCTOU 关死）────────────────────────────────────
class _DecideCur:
    """decide 端点的模式匹配游标：候选行 + 当前快照 rev + staff 挂靠。"""

    def __init__(self, *, cand=("u1", 110, 5, "pending"), cur_rev=5, staff_csv="110"):
        self.cand, self.cur_rev, self.staff_csv = cand, cur_rev, staff_csv
        self._rows, self.sql = [], []
        self.rowcount = 1

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self.sql.append((s, args))
        if "dept_admin_node_candidate WHERE id" in s:
            self._rows = [self.cand]
        elif "MAX(snapshot_rev)" in s:
            self._rows = [(self.cur_rev,)]
        elif "FROM" in s and "staff_dim" in s:
            self._rows = [(self.staff_csv,)]
        elif "user_role" in s and s.startswith("SELECT"):
            self._rows = [("dept_admin",)]
        elif "source='manual'" in s and s.startswith("SELECT"):
            self._rows = []                      # 无 manual override
        elif "dept_dim WHERE dept_id" in s:
            self._rows = [(1,)]
        elif "dept_dim WHERE is_active=1" in s:
            self._rows = [(110, 100), (100, 1)]  # depth 判定用（_candidate_risk）
        elif "SELECT depth FROM" in s:
            self._rows = [(2,)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _DecideConn:
    def __init__(self, cur):
        self._c, self.commits = cur, 0

    def cursor(self):
        return self._c

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _decide(monkeypatch, cur, action="confirm", cid=1):
    from fastapi.testclient import TestClient
    from opensearch_pipeline import api, db
    from opensearch_pipeline.routes import kb_access

    class _Kb:
        user_id, name, role = "adm", "管理员", "kb_admin"

    monkeypatch.setattr(kb_access, "_require_kb_admin", lambda i: _Kb())
    monkeypatch.setattr(kb_access, "_enforce_rate_limit", lambda *a, **k: None)
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_metadata_write_allowed", lambda *a, **k: None)
    import opensearch_pipeline.audit_log as al
    monkeypatch.setattr(al, "write_audit", lambda **k: None)
    monkeypatch.setattr(db, "_get_db_conn", lambda: _DecideConn(cur))
    return TestClient(api.app).post("/api/kb/admin-node-candidates/decide",
                                    json={"candidate_id": cid, "action": action})


def test_candidate_confirm_writes_authority(monkeypatch):
    cur = _DecideCur()
    r = _decide(monkeypatch, cur)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    assert any("INSERT INTO" in s and "dept_admin_node_grant" in s for s, _ in cur.sql)
    assert any("SET status='confirmed'" in s for s, _ in cur.sql)


def test_candidate_stale_rev_409_rederives_not_confirms(monkeypatch):
    """★ TOCTOU：快照已翻（rev 5→9）⇒ 409 + 按当前快照重派生刷新本行——
    **绝不确认陈旧候选**、绝不写权威表。"""
    cur = _DecideCur(cand=("u1", 110, 5, "pending"), cur_rev=9)
    r = _decide(monkeypatch, cur)
    assert r.status_code == 409
    assert any("derived_snapshot_rev=%s" in s for s, _ in cur.sql)     # 重派生刷新
    assert not any("INSERT INTO" in s and "dept_admin_node_grant" in s for s, _ in cur.sql)


def test_candidate_stale_rev_and_moved_dept_superseded(monkeypatch):
    """快照翻了且挂靠也变了 ⇒ superseded（旧候选不可再确认）。"""
    cur = _DecideCur(cand=("u1", 110, 5, "pending"), cur_rev=9, staff_csv="120")
    r = _decide(monkeypatch, cur)
    assert r.status_code == 409 and "失效" in str(r.json()["detail"])
    assert any("status='superseded'" in s for s, _ in cur.sql)
