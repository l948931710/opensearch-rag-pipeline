# -*- coding: utf-8 -*-
"""阶段 B WP6：doc-meta 端点（四件事同事务）+ meta 投影 pre-drain 的证据补全。

codex MISSING EVIDENCE 对应项：
  · CAS 必填 + title-only 变更也推进 revision（两个持同 revision 的并发编辑第二个必 409）；
  · legacy→node 迁移必须 owner+可见集一起给（原子，不产生半迁移态）+ 清理存量申请行；
  · node→legacy 回滚 = kb_admin-only + 必填白名单 legacy_owner（撤 grants + owner 写回 + outbox）；
  · title/category 变更 enqueue meta 投影（061 未 apply 抛 → 整笔回滚诚实失败）；
  · pre-drain：category 同步 + NOT_INDEXED 标脏 + attempts 推进，claims 带 generation。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console

    class _Kb:
        user_id, name, role = "adm", "管理员", "kb_admin"

    monkeypatch.setattr(kb_console, "_require_kb_console", lambda i: _Kb())
    monkeypatch.setattr(kb_console, "_kb_can_manage_doc", lambda kb, m, o, oid: True)
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "present")
    monkeypatch.setattr(kb_console, "_node_acl_grant_enabled", lambda: True)
    monkeypatch.setattr(kb_console, "_enforce_rate_limit", lambda *a, **k: None)
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_metadata_write_allowed", lambda *a, **k: None)
    import opensearch_pipeline.audit_log as al
    monkeypatch.setattr(al, "write_audit", lambda **k: None)
    return TestClient(api.app)


class _Cur:
    """模式匹配游标：document_meta 行 + dept_dim + 授权表 + outbox 全应答。"""

    def __init__(self, *, mode="node", owner=None, oid=110, rev=3, perm="dept_internal",
                 status="active", title="旧标题", c1="sop", c2="mold",
                 live=(110, 120, 130), grants=()):
        self.mode, self.owner, self.oid, self.rev = mode, owner, oid, rev
        self.perm, self.status, self.title, self.c1, self.c2 = perm, status, title, c1, c2
        self.live, self.grants = live, grants
        self._rows, self.sql = [], []
        self.rowcount = 1

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self.sql.append((s, args))
        if "FOR UPDATE" in s and "document_meta" in s:
            self._rows = [(self.owner, self.perm, self.status, self.rev, self.title,
                           self.c1, self.c2, self.mode, self.oid)]
        elif "dept_dim" in s and s.startswith("SELECT"):
            self._rows = [(d,) for d in self.live]
        elif "kb_doc_node_grant" in s and s.startswith("SELECT"):
            self._rows = list(self.grants)
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


class _Conn:
    def __init__(self, cur):
        self._c, self.committed, self.rolled = cur, False, False

    def cursor(self):
        return self._c

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled = True

    def close(self):
        pass


def _wire(monkeypatch, cur, *, enqueue_meta_boom=False):
    import opensearch_pipeline.db as db
    from opensearch_pipeline import access_grants
    conn = _Conn(cur)
    monkeypatch.setattr(db, "_get_db_conn", lambda: conn)
    monkeypatch.setattr(access_grants, "enqueue_acl_projection", lambda c, d, reason="": c.execute(
        "INSERT INTO kb_acl_projection_outbox (doc_id) VALUES (%s)", (d,)))
    if enqueue_meta_boom:
        def _boom(c, d, reason=""):
            raise RuntimeError("Table 'kb_doc_meta_projection_outbox' doesn't exist (1146)")
        monkeypatch.setattr(access_grants, "enqueue_meta_projection", _boom)
    else:
        monkeypatch.setattr(access_grants, "enqueue_meta_projection", lambda c, d, reason="": c.execute(
            "INSERT INTO kb_doc_meta_projection_outbox (doc_id) VALUES (%s)", (d,)))
    monkeypatch.setattr(access_grants, "materialize_doc_allowed_depts", lambda c, d, **k: {})
    return conn


def _post(client, **body):
    body.setdefault("doc_id", "D1")
    return client.post("/api/kb/doc-meta", json=body)


# ── CAS 语义 ─────────────────────────────────────────────────────────────────
def test_missing_expected_revision_rejected(client, monkeypatch):
    _wire(monkeypatch, _Cur())
    r = _post(client, title="新标题")
    assert r.status_code == 400 and "expected_acl_revision" in r.json()["detail"]


def test_stale_revision_conflicts(client, monkeypatch):
    _wire(monkeypatch, _Cur(rev=5))
    assert _post(client, expected_acl_revision=3, title="新标题").status_code == 409


def test_title_only_change_bumps_revision(client, monkeypatch):
    """★ M3：title-only 变更也推进 acl_revision——否则两个持同 revision 的并发元数据
    编辑第二个仍会通过，CAS 对元数据并发无效。"""
    cur = _Cur(rev=3)
    conn = _wire(monkeypatch, cur)
    r = _post(client, expected_acl_revision=3, title="新标题")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == ["title"] and body["acl_revision"] == 4
    assert any("SET acl_revision=%s" in s for s, _ in cur.sql) and conn.committed
    # meta 投影 outbox 同事务入队（title 进 HA3 走 stage-3 pre-drain 重推）
    assert any("kb_doc_meta_projection_outbox" in s for s, _ in cur.sql)


def test_no_change_no_bump(client, monkeypatch):
    """无实际变更 → 不推进 revision、不入队、rollback。"""
    cur = _Cur(rev=3, title="旧标题")
    conn = _wire(monkeypatch, cur)
    r = _post(client, expected_acl_revision=3, title="旧标题")
    assert r.status_code == 200 and r.json()["changed"] == []
    assert not any("SET acl_revision" in s for s, _ in cur.sql)
    assert conn.rolled and not conn.committed


def test_meta_outbox_enqueue_failure_rolls_back_whole_edit(client, monkeypatch):
    """061 未 apply（enqueue 抛 1146）⇒ 整笔 500——诚实失败，绝不半提交
    （元数据改了、投影意图丢了 = HA3 永远陈旧）。"""
    cur = _Cur(rev=3)
    conn = _wire(monkeypatch, cur, enqueue_meta_boom=True)
    r = _post(client, expected_acl_revision=3, title="新标题")
    assert r.status_code == 500 and not conn.committed


# ── legacy→node 迁移 ────────────────────────────────────────────────────────
def test_migration_requires_owner_and_visible_together(client, monkeypatch):
    """迁移必须 owner+可见集一起给——只给 owner 会产生半迁移态（缺可见集）。"""
    _wire(monkeypatch, _Cur(mode="legacy", owner="production", oid=None))
    r = _post(client, expected_acl_revision=3, owner_dept_id=110)
    assert r.status_code == 400 and "同时提供" in r.json()["detail"]


def test_migration_atomic_and_cleans_legacy_requests(client, monkeypatch):
    """迁移同事务：切 mode + owner_dept=NULL + 授权全集 + M6 清理存量申请行
    （approved→revoked / pending→rejected——隐形组码回滚复活面）。"""
    cur = _Cur(mode="legacy", owner="production", oid=None, rev=3)
    conn = _wire(monkeypatch, cur)
    r = _post(client, expected_acl_revision=3, owner_dept_id=110,
              visible_nodes=[{"dept_id": 120, "subtree": True}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["acl_mode"] == "node" and set(body["changed"]) >= {"mode", "owner"}
    sqls = [s for s, _ in cur.sql]
    assert any("SET acl_mode='node'" in s and "owner_dept=NULL" in s for s in sqls)
    assert any("status='revoked'" in s for s in sqls) and any("status='rejected'" in s for s in sqls)
    assert any("kb_acl_projection_outbox" in s for s in sqls) and conn.committed


def test_migration_blocked_when_grant_flag_off(client, monkeypatch):
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_node_acl_grant_enabled", lambda: False)
    _wire(monkeypatch, _Cur(mode="legacy", owner="production", oid=None))
    r = _post(client, expected_acl_revision=3, owner_dept_id=110,
              visible_nodes=[{"dept_id": 120, "subtree": True}])
    assert r.status_code == 400 and "RAG_NODE_ACL_GRANT" in r.json()["detail"]


def test_dead_target_node_rejected(client, monkeypatch):
    _wire(monkeypatch, _Cur(mode="legacy", owner="production", oid=None, live=(120,)))
    r = _post(client, expected_acl_revision=3, owner_dept_id=999,
              visible_nodes=[{"dept_id": 120, "subtree": True}])
    assert r.status_code == 400 and "999" in r.json()["detail"]


# ── node→legacy 回滚（kb_admin-only 显式契约）────────────────────────────────
def test_rollback_requires_legacy_owner(client, monkeypatch):
    _wire(monkeypatch, _Cur(mode="node", oid=110))
    r = _post(client, expected_acl_revision=3, target_acl_mode="legacy")
    assert r.status_code == 400 and "legacy_owner_dept" in r.json()["detail"]


def test_rollback_writes_owner_revokes_grants_and_enqueues(client, monkeypatch):
    """回滚同事务：撤全部 node grants + owner 写回 + owner_dept_id=NULL + ACL outbox
    （mode 对称 materializer 据此把 chunk 哨兵洗回真实组码——全链路的授权端）。"""
    cur = _Cur(mode="node", oid=110, rev=3)
    conn = _wire(monkeypatch, cur)
    r = _post(client, expected_acl_revision=3, target_acl_mode="legacy",
              legacy_owner_dept="production")
    assert r.status_code == 200, r.text
    assert r.json()["acl_mode"] == "legacy"
    sqls = [s for s, _ in cur.sql]
    assert any("kb_doc_node_grant SET revoked_at=NOW()" in s for s in sqls)
    assert any("SET acl_mode='legacy'" in s and "owner_dept_id=NULL" in s for s in sqls)
    assert any("kb_acl_projection_outbox" in s for s in sqls) and conn.committed


def test_rollback_kb_admin_only(client, monkeypatch):
    from opensearch_pipeline.routes import kb_console

    class _DeptAdmin:
        user_id, name, role = "da1", "部管", "dept_admin"

    monkeypatch.setattr(kb_console, "_require_kb_console", lambda i: _DeptAdmin())
    _wire(monkeypatch, _Cur(mode="node", oid=110))
    r = _post(client, expected_acl_revision=3, target_acl_mode="legacy",
              legacy_owner_dept="production")
    assert r.status_code == 403


# ── meta 投影 pre-drain（stage-3 loader 窗口的另一半）────────────────────────
class _DrainCur:
    def __init__(self):
        self.sql = []
        self._rows = []
        self.rowcount = 2

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self.sql.append((s, args))
        if "kb_doc_meta_projection_outbox" in s and s.startswith("SELECT"):
            self._rows = [(1, "D1", 5), (2, "D2", 7)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pre_drain_syncs_category_marks_dirty_and_claims(monkeypatch):
    from opensearch_pipeline import db
    from opensearch_pipeline.access_grants import pre_drain_meta_projection
    cur = _DrainCur()

    class _C:
        def cursor(self):
            return cur

        def commit(self):
            self.committed = True

        def close(self):
            pass

    monkeypatch.setattr(db, "_get_db_conn", lambda: _C())
    out = pre_drain_meta_projection()
    assert out["claims"] == [(1, "D1", 5), (2, "D2", 7)]      # 带 generation（成功后 CAS 收尾用）
    sqls = [s for s, _ in cur.sql]
    # category 从 document_meta 同步进 chunk_meta（stage-3 载荷读 cm 列）+ NOT_INDEXED 标脏
    assert sum(1 for s in sqls if "cm.category_l1 = dm.category_l1" in s and "NOT_INDEXED" in s) == 2
    assert sum(1 for s in sqls if "attempts = attempts + 1" in s) == 2
