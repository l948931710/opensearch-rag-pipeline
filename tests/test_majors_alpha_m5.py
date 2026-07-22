# -*- coding: utf-8 -*-
"""Majors α4(M5)stewardship 轮换收敛(2026-07-21 codex 共识)。

锁死的不变量:
- registered 工具的可见性=live scope **唯一权威**:轮换后新 steward 在列表**立即**
  可见(无需任何刷新动作),旧 steward 立即不可见;unregistered → 快照回退;
- kb_admin 恒可见(零 resolve 开销);授权路径零缓存;
- 游标:版本化+HMAC 签名,伪造/篡改 → ValueError;next_cursor=最后**已扫描**候选;
- refresh_scope:CAS(status='pending' AND approver_scope=<old>),rowcount=1 才写
  old→new 审计(同事务);竞态输家 False 零审计;
- _authorize_approver:检出漂移即刷新存量列(裁决 403 也刷),403 文案带轮换提示;
- refresh_stale_scopes:仅漂移行刷新,单行失败不拖垮整批。
"""
import json
from datetime import datetime, timedelta

import pytest

import opensearch_pipeline.routes.agent as agent_route
from opensearch_pipeline.agent_runtime import approval_store as ap_mod
from opensearch_pipeline.agent_runtime.approval_store import (
    RDSApprovalStore,
    _decode_cursor,
    _encode_cursor,
    register_approver_scope_resolver,
)
from opensearch_pipeline.api import Identity


@pytest.fixture(autouse=True)
def _fresh_resolvers(monkeypatch):
    monkeypatch.setattr(ap_mod, "_SCOPE_RESOLVERS", {})
    monkeypatch.setattr(ap_mod, "_op_db", lambda: "op")


# ── 游标 ──────────────────────────────────────────────────────────────────────


def test_cursor_roundtrip_and_tamper():
    ca = datetime(2026, 7, 21, 10, 30, 0, 123000)
    tok = _encode_cursor(ca, "rq-1")
    got_ca, got_id = _decode_cursor(tok)
    assert got_ca == ca and got_id == "rq-1"
    with pytest.raises(ValueError):
        _decode_cursor(tok[:-4] + "beef")            # 签名篡改
    with pytest.raises(ValueError):
        _decode_cursor("v9." + tok.split(".", 1)[1])  # 版本不符
    with pytest.raises(ValueError):
        _decode_cursor("garbage")


# ── list_pending_page(fake conn)────────────────────────────────────────────


def _row(request_id, tool_name, scope, created_at, args=None):
    """按 _COLS 顺序构造行。"""
    return (request_id, "r-" + request_id, "c1", tool_name, "",
            json.dumps(args or {"target_object_id": "obj-1"}), "dg", "sum",
            "requester", "pmc", scope, "pending",
            created_at + timedelta(days=3), created_at, None)


class _PageCur:
    def __init__(self, batches):
        self._batches = list(batches)
        self.sqls = []

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def fetchall(self):
        return self._batches.pop(0) if self._batches else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _PageConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def begin(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _store_with(monkeypatch, cur):
    st = RDSApprovalStore()
    monkeypatch.setattr(st, "_conn", lambda: _PageConn(cur))
    return st


_T0 = datetime(2026, 7, 21, 9, 0, 0)


def test_rotation_new_steward_sees_old_steward_does_not(monkeypatch):
    """存量 scope='sales',live 解析='supply' → supply 立即可见,sales 不可见。"""
    register_approver_scope_resolver("t_live", lambda ctx, args: "supply")
    rows = [_row("rq1", "t_live", "sales", _T0)]
    st = _store_with(monkeypatch, _PageCur([rows]))
    page = st.list_pending_page(["supply"], limit=10)
    assert [d["request_id"] for d in page["items"]] == ["rq1"]
    st2 = _store_with(monkeypatch, _PageCur([rows]))
    page2 = st2.list_pending_page(["sales"], limit=10)
    assert page2["items"] == []                      # 旧 steward 立即不可见(live 唯一权威)


def test_unregistered_tool_snapshot_fallback(monkeypatch):
    rows = [_row("rq1", "t_plain", "sales", _T0)]
    st = _store_with(monkeypatch, _PageCur([rows]))
    assert [d["request_id"] for d in st.list_pending_page(["sales"], limit=10)["items"]] == ["rq1"]
    st2 = _store_with(monkeypatch, _PageCur([rows]))
    assert st2.list_pending_page(["supply"], limit=10)["items"] == []


def test_kb_admin_sees_all_without_resolve(monkeypatch):
    calls = []
    register_approver_scope_resolver("t_live", lambda ctx, args: calls.append(1) or "supply")
    rows = [_row("rq1", "t_live", "sales", _T0), _row("rq2", "t_plain", "hr", _T0)]
    st = _store_with(monkeypatch, _PageCur([rows]))
    page = st.list_pending_page(None, limit=10)
    assert len(page["items"]) == 2 and calls == []   # kb_admin 零 resolve


def test_csv_component_match(monkeypatch):
    register_approver_scope_resolver("t_live", lambda ctx, args: "steward,backup")
    rows = [_row("rq1", "t_live", "old", _T0)]
    st = _store_with(monkeypatch, _PageCur([rows]))
    assert len(st.list_pending_page(["backup"], limit=10)["items"]) == 1


def test_next_cursor_points_at_last_scanned(monkeypatch):
    """满批未扫尽 → next_cursor=最后已扫描候选;续页从其后继续。"""
    register_approver_scope_resolver("t_live", lambda ctx, args: "supply")
    batch = [_row(f"rq{i:03d}", "t_live", "x", _T0 + timedelta(seconds=i))
             for i in range(200)]
    st = _store_with(monkeypatch, _PageCur([batch]))
    page = st.list_pending_page(["supply"], limit=1)
    assert [d["request_id"] for d in page["items"]] == ["rq000"]
    assert page["next_cursor"] is not None
    ca, rid = _decode_cursor(page["next_cursor"])
    assert rid == "rq000"                            # 最后已扫描=第一行(填满即停)
    # 尾批不满 → 扫尽 → 无 next_cursor
    st2 = _store_with(monkeypatch, _PageCur([batch[:3]]))
    page2 = st2.list_pending_page(["supply"], limit=10)
    assert len(page2["items"]) == 3 and page2["next_cursor"] is None


def test_hidden_rows_are_skipped_not_leaked(monkeypatch):
    register_approver_scope_resolver("t_live", lambda ctx, args: "supply")
    rows = [_row("rq1", "t_live", "x", _T0),
            _row("rq2", "t_plain", "sales", _T0 + timedelta(seconds=1)),
            _row("rq3", "t_live", "x", _T0 + timedelta(seconds=2))]
    st = _store_with(monkeypatch, _PageCur([rows]))
    page = st.list_pending_page(["supply"], limit=10)
    assert [d["request_id"] for d in page["items"]] == ["rq1", "rq3"]   # rq2 不可见不泄露


# ── refresh_scope CAS ────────────────────────────────────────────────────────


class _CasCur:
    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.sqls = []

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _CasConn(_PageConn):
    def __init__(self, cur):
        super().__init__(cur)
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_refresh_scope_cas_updates_and_audits(monkeypatch):
    cur = _CasCur(rowcount=1)
    st = RDSApprovalStore()
    monkeypatch.setattr(st, "_conn", lambda: _CasConn(cur))
    assert st.refresh_scope("rq1", "sales", "supply", run_id="r1", actor="u9") is True
    joined = "\n".join(s for s, _ in cur.sqls)
    assert "approver_scope=%s" in joined and "status='pending'" in joined
    assert "agent_audit_log" in joined               # 同事务审计
    upd_args = cur.sqls[0][1]
    assert upd_args[0] == "supply" and upd_args[2] == "sales"   # CAS 带 old 谓词


def test_refresh_scope_cas_loser_no_audit(monkeypatch):
    cur = _CasCur(rowcount=0)
    conn = _CasConn(cur)
    st = RDSApprovalStore()
    monkeypatch.setattr(st, "_conn", lambda: conn)
    assert st.refresh_scope("rq1", "sales", "supply") is False
    assert not any("agent_audit_log" in s for s, _ in cur.sqls)
    assert conn.rollbacks == 1 and conn.commits == 0


def test_refresh_stale_scopes_only_drifted(monkeypatch):
    register_approver_scope_resolver("t_live", lambda ctx, args: "supply")
    rows = [("rq1", "r1", "t_live", json.dumps({}), "sales"),    # 漂移 → 刷
            ("rq2", "r2", "t_live", json.dumps({}), "supply"),   # 一致 → 跳
            ("rq3", "r3", "t_plain", json.dumps({}), "sales")]   # 未注册 → 跳
    cur = _PageCur([rows])
    st = RDSApprovalStore()
    monkeypatch.setattr(st, "_conn", lambda: _PageConn(cur))
    refreshed = []
    monkeypatch.setattr(st, "refresh_scope",
                        lambda rid, old, new, **k: refreshed.append((rid, old, new)) or True)
    assert st.refresh_stale_scopes() == 1
    assert refreshed == [("rq1", "sales", "supply")]


# ── _authorize_approver 漂移刷新 + 403 提示 ──────────────────────────────────


def _authz(monkeypatch, *, role, managed=(), refreshes=None):
    register_approver_scope_resolver("t_live", lambda ctx, args: "supply")

    class _Kb:
        pass

    kb = _Kb()
    kb.role = role
    import opensearch_pipeline.dingtalk_identity as di
    import opensearch_pipeline.kb_authz as ka
    monkeypatch.setattr(di, "resolve_kb_identity", lambda uid: kb)
    monkeypatch.setattr(ka, "managed_owner_depts", lambda k: set(managed))

    class _St:
        def refresh_scope(self, rid, old, new, **k):
            (refreshes if refreshes is not None else []).append((rid, old, new))
            return True

    monkeypatch.setattr(agent_route, "_get_approval_store", lambda: _St())
    identity = Identity(user_id="approver", acl_groups=["pmc"], role="employee")
    run = {"user_id": "requester"}
    areq = {"request_id": "rq1", "run_id": "r1", "tool_name": "t_live",
            "proposed_args": {}, "approver_scope": "sales"}
    return agent_route._authorize_approver(identity, run, areq, "approved")


def test_authorize_rotation_kb_admin_passes_and_refreshes(monkeypatch):
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    refreshes = []
    scope = _authz(monkeypatch, role=ROLE_KB_ADMIN, refreshes=refreshes)
    assert scope == "supply"                          # 裁决用 live
    assert refreshes == [("rq1", "sales", "supply")]  # 存量列追上事实


def test_authorize_rotation_old_steward_403_with_hint(monkeypatch):
    from fastapi import HTTPException

    from opensearch_pipeline.kb_authz import ROLE_DEPT_ADMIN
    refreshes = []
    with pytest.raises(HTTPException) as ei:
        _authz(monkeypatch, role=ROLE_DEPT_ADMIN, managed=("sales",), refreshes=refreshes)
    assert ei.value.status_code == 403
    assert "轮换" in ei.value.detail                  # 明示轮换
    assert refreshes == [("rq1", "sales", "supply")]  # 403 也刷新(正确 steward 立即受益)


def test_authorize_new_steward_passes(monkeypatch):
    from opensearch_pipeline.kb_authz import ROLE_DEPT_ADMIN
    scope = _authz(monkeypatch, role=ROLE_DEPT_ADMIN, managed=("supply",))
    assert scope == "supply"
