# -*- coding: utf-8 -*-
"""保存端点 /api/kb/doc-node-grants 的不变量测试。

要锁死的:
  · **整体替换而非追加** —— 未勾选的必须 soft-revoke(否则"取消勾选"是谎言);
  · `acl_revision` CAS —— 并发两个整体替换必须有一个 409,不能静默丢勾选;
  · 节点数超上限 **422 拒绝**,绝不静默截断;
  · dept_id 必须在册(授权给已消失节点 = 文档对所有人不可见且无从解释);
  · restricted/public/离线/越权 一律拒;
  · 撤销走 soft-revoke,**不 DELETE**(审计留痕)。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_access

    class _Kb:
        user_id, name, role = "adm", "管理员", "kb_admin"

    monkeypatch.setattr(kb_access, "_require_kb_console", lambda i: _Kb())
    monkeypatch.setattr(kb_access, "_kb_can_manage", lambda kb, owner: owner != "other")
    monkeypatch.setattr(kb_access, "_enforce_rate_limit", lambda *a, **k: None)
    return TestClient(api.app)


class Cur:
    def __init__(self, *, owner="production", perm="dept_internal", status="active",
                 mode="legacy", rev=3, live=(10, 11, 12), before=()):
        self.owner, self.perm, self.status, self.mode, self.rev = owner, perm, status, mode, rev
        self.live, self.before = live, before
        self._rows, self.sql = [], []
        self.rowcount = 1

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self.sql.append((s, args))
        if "FOR UPDATE" in s:
            self._rows = [(self.owner, self.perm, self.status, self.mode, self.rev)]
        elif "dept_dim" in s:
            self._rows = [(d,) for d in self.live]
        elif "SELECT dept_id, scope FROM" in s:
            self._rows = list(self.before)
        elif "information_schema" in s:
            self._rows = [(1,)]
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


class Conn:
    def __init__(self, cur):
        self._c, self.committed = cur, False

    def cursor(self):
        return self._c

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _wire(monkeypatch, cur):
    import opensearch_pipeline.db as db
    from opensearch_pipeline import access_grants
    monkeypatch.setattr(db, "_get_db_conn", lambda: Conn(cur))
    monkeypatch.setattr(access_grants, "enqueue_acl_projection", lambda c, d, reason="": None)
    monkeypatch.setattr(access_grants, "materialize_doc_allowed_depts", lambda c, d, **k: {})
    import opensearch_pipeline.audit_log as al
    monkeypatch.setattr(al, "write_audit", lambda **k: None)
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_metadata_write_allowed", lambda *a, **k: None)


def _post(client, **body):
    body.setdefault("doc_id", "D1")
    return client.post("/api/kb/doc-node-grants", json=body)


def test_replace_semantics_revokes_unselected(client, monkeypatch):
    """★ 整体替换:before 有 11,本次只勾 10 ⇒ 11 必须被 soft-revoke。"""
    cur = Cur(before=[(11, "subtree")])
    _wire(monkeypatch, cur)
    r = _post(client, nodes=[{"dept_id": 10, "subtree": True}], acl_revision=3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["granted"] == ["d:10"] and body["revoked"] == ["d:11"]
    assert body["acl_mode"] == "node" and body["acl_revision"] == 4
    revokes = [s for s, _ in cur.sql if "SET revoked_at=NOW()" in s]
    assert revokes, "未勾选的节点没有被撤销 ⇒「取消勾选」是谎言"
    assert not any(s.startswith("DELETE") for s, _ in cur.sql), "撤销必须 soft-revoke 留痕"


def test_exact_scope_projects_dx(client, monkeypatch):
    cur = Cur()
    _wire(monkeypatch, cur)
    r = _post(client, nodes=[{"dept_id": 10, "subtree": False}], acl_revision=3)
    assert r.json()["granted"] == ["dx:10"]


def test_stale_revision_conflicts(client, monkeypatch):
    """并发两个整体替换 ⇒ 后到者 409,绝不静默覆盖先到者的勾选。"""
    _wire(monkeypatch, Cur(rev=5))
    assert _post(client, nodes=[{"dept_id": 10}], acl_revision=3).status_code == 409


def test_missing_revision_is_allowed(client, monkeypatch):
    """不传 revision = 不做 CAS(首次保存/脚本场景),仍照常推进版本。"""
    _wire(monkeypatch, Cur(rev=7))
    assert _post(client, nodes=[{"dept_id": 10}]).json()["acl_revision"] == 8


def test_too_many_nodes_rejected_not_truncated(client, monkeypatch):
    """★ 超上限必须 422 拒绝 —— 静默截断会让 UI 以为已完整保存。"""
    from opensearch_pipeline.acl_policy import MAX_DOC_NODES
    _wire(monkeypatch, Cur(live=tuple(range(1, MAX_DOC_NODES + 10))))
    r = _post(client, nodes=[{"dept_id": i} for i in range(1, MAX_DOC_NODES + 3)])
    assert r.status_code == 422 and "上限" in r.json()["detail"]


def test_unknown_dept_rejected(client, monkeypatch):
    """授权给不在册节点 = 文档对所有人不可见且无从解释 ⇒ 必须当场拒。"""
    _wire(monkeypatch, Cur(live=(10,)))
    r = _post(client, nodes=[{"dept_id": 10}, {"dept_id": 999}])
    assert r.status_code == 400 and "999" in r.json()["detail"]


@pytest.mark.parametrize("perm,code", [("restricted", 403), ("public", 400)])
def test_permission_level_gate(client, monkeypatch, perm, code):
    _wire(monkeypatch, Cur(perm=perm))
    assert _post(client, nodes=[{"dept_id": 10}]).status_code == code


def test_retired_doc_rejected(client, monkeypatch):
    _wire(monkeypatch, Cur(status="retired"))
    assert _post(client, nodes=[{"dept_id": 10}]).status_code == 400


def test_non_manager_rejected(client, monkeypatch):
    """管理轴仍是真实 owner(node 模式只改检索投影轴)⇒ 授权判定无需等 T5。"""
    _wire(monkeypatch, Cur(owner="other"))
    assert _post(client, nodes=[{"dept_id": 10}]).status_code == 403


def test_negative_dept_id_rejected(client, monkeypatch):
    _wire(monkeypatch, Cur())
    assert _post(client, nodes=[{"dept_id": -5}]).status_code == 400


def test_empty_selection_revokes_all_and_still_switches_mode(client, monkeypatch):
    """全取消 ⇒ 授权清空但文档**仍是 node 模式**(绝不回落 legacy 让属主组重新可见)。"""
    cur = Cur(before=[(10, "subtree"), (11, "exact")])
    _wire(monkeypatch, cur)
    body = _post(client, nodes=[], acl_revision=3).json()
    assert body["granted"] == [] and set(body["revoked"]) == {"d:10", "dx:11"}
    assert body["acl_mode"] == "node"
