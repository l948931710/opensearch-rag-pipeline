# -*- coding: utf-8 -*-
"""阶段 B WP3：三个 legacy-only 缺口的 mode 感知修复 + resign node 分支。

codex 点名（设计稿 :289 + M6 细化）：
  · 直接共享/申请/审批的组码通道对 node 文档关死（隐形组码回滚复活面）——**新增禁止、
    收权放行**（decide 的 reject/revoke 对 node 文档照常）；
  · decide 按当前文档三元组现裁，不再只信申请行 owner_dept 快照；
  · explain 对 node 文档解释节点授权（LEFT JOIN 含失活节点并标注，不悄悄隐藏授权意图）；
  · resign-images 的 node 文档走 can_read_doc（此前手写 legacy 判定恒拒——图全挂）。
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
    monkeypatch.setattr(kb_access, "_kb_can_manage", lambda kb, owner: True)
    monkeypatch.setattr(kb_access, "_kb_can_manage_doc", lambda kb, m, o, oid: True)
    monkeypatch.setattr(kb_access, "_kb_node_capability", lambda cur: "present")
    monkeypatch.setattr(kb_access, "_enforce_rate_limit", lambda *a, **k: None)
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_metadata_write_allowed", lambda *a, **k: None)
    import opensearch_pipeline.audit_log as al
    monkeypatch.setattr(al, "write_audit", lambda **k: None)
    return TestClient(api.app)


class _Cur:
    """模式匹配游标：document_meta / kb_access_request / kb_doc_node_grant 三源应答。"""

    def __init__(self, *, doc_mode="node", doc_owner=None, doc_oid=110, perm="dept_internal",
                 status="active", req_row=None, node_grants=()):
        self.doc_mode, self.doc_owner, self.doc_oid = doc_mode, doc_owner, doc_oid
        self.perm, self.status = perm, status
        self.req_row = req_row              # kb_access_request 行 (owner_dept, status, doc_id)
        self.node_grants = list(node_grants)
        self._rows, self.sql = [], []
        self.rowcount, self.lastrowid = 1, 7

    def execute(self, sql, args=()):
        s = " ".join(sql.split())
        self.sql.append((s, args))
        if "FROM" in s and "document_meta" in s and s.startswith("SELECT"):
            if "current_version_no" in s:
                self._rows = [(self.doc_owner, self.perm, self.status, 1,
                               self.doc_mode, self.doc_oid)]
            elif "owner_dept, permission_level, status" in s:
                self._rows = [(self.doc_owner, self.perm, self.status,
                               self.doc_mode, self.doc_oid)][:1]
            elif "owner_dept, status" in s:
                self._rows = [(self.doc_owner, self.status, self.doc_mode, self.doc_oid)]
            else:
                self._rows = []
        elif "kb_access_request" in s and s.startswith("SELECT") and "WHERE id=" in s:
            self._rows = [self.req_row] if self.req_row else []
        elif "kb_doc_node_grant" in s and s.startswith("SELECT"):
            self._rows = list(self.node_grants)
        elif "document_version" in s and s.startswith("SELECT"):
            self._rows = [("PUBLISHED", "ok")]
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
        self._c, self.committed = cur, False

    def cursor(self):
        return self._c

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def _wire(monkeypatch, cur):
    import opensearch_pipeline.db as db
    monkeypatch.setattr(db, "_get_db_conn", lambda: _Conn(cur))


# ── 缺口①：直接共享对 node 文档关死 ──────────────────────────────────────────
def test_direct_share_rejected_on_node_doc(client, monkeypatch):
    _wire(monkeypatch, _Cur(doc_mode="node"))
    r = client.post("/api/kb/access-grants",
                    json={"doc_id": "D1", "target_depts": ["hr"]})
    assert r.status_code == 400 and "组织树" in r.json()["detail"]


# ── 缺口③：decide 按当前文档三元组现裁 + M6 收权放行 ─────────────────────────
def test_decide_approve_blocked_on_node_doc(client, monkeypatch):
    """node 文档上的 pending 组码申请不可 approve（隐形组码复活面）。"""
    _wire(monkeypatch, _Cur(doc_mode="node", req_row=("production", "pending", "D1")))
    r = client.post("/api/kb/access-requests/approve", json={"id": "9"})
    assert r.status_code == 400 and "不可批准" in r.json()["detail"]


def test_decide_reject_allowed_on_node_doc(client, monkeypatch):
    """收权动作（reject pending）对 node 文档放行——M6：只禁新增/扩大。"""
    _wire(monkeypatch, _Cur(doc_mode="node", req_row=("production", "pending", "D1")))
    r = client.post("/api/kb/access-requests/reject", json={"id": "9"})
    assert r.status_code == 200 and r.json()["decided"] is True


def test_decide_revoke_allowed_on_node_doc(client, monkeypatch):
    _wire(monkeypatch, _Cur(doc_mode="node", req_row=("production", "approved", "D1")))
    r = client.post("/api/kb/access-requests/revoke", json={"id": "9"})
    assert r.status_code == 200 and r.json()["decided"] is True


def test_decide_authorizes_on_current_doc_not_snapshot(client, monkeypatch):
    """快照 owner 命中但当前文档三元组不在管辖 ⇒ 403（快照列降级为展示/审计）。"""
    from opensearch_pipeline.routes import kb_access
    monkeypatch.setattr(kb_access, "_kb_can_manage_doc", lambda kb, m, o, oid: False)
    _wire(monkeypatch, _Cur(doc_mode="legacy", doc_owner="hr",
                            req_row=("production", "pending", "D1")))
    r = client.post("/api/kb/access-requests/approve", json={"id": "9"})
    assert r.status_code == 403


# ── 缺口②：explain 对 node 文档解释节点授权 ──────────────────────────────────
def test_explain_node_doc_lists_node_grants_with_inactive_marker(client, monkeypatch):
    _wire(monkeypatch, _Cur(doc_mode="node", node_grants=[
        (110, "subtree", "注塑事业部", 1),
        (999, "exact", None, 0),          # 失活节点：名字缺、is_active=0
    ]))
    r = client.get("/api/kb/visibility-explain", params={"doc_id": "D1"})
    assert r.status_code == 200
    body = r.json()
    assert body["acl_mode"] == "node"
    vias = {x["via"] for x in body["readers"]}
    assert vias == {"node_subtree", "node_exact"}
    names = [x["dept"] for x in body["readers"]]
    assert "注塑事业部" in names and any("已失效" in n for n in names)


def test_explain_node_doc_empty_grants_is_nobody(client, monkeypatch):
    """node 文档授权空集 = 无人可见（如实），不回落组码解释。"""
    _wire(monkeypatch, _Cur(doc_mode="node", node_grants=[]))
    body = client.get("/api/kb/visibility-explain", params={"doc_id": "D1"}).json()
    assert body["nobody"] is True and body["readers"] == []


# ── 申请 submit 对 node 文档关死 ─────────────────────────────────────────────
def test_access_submit_rejected_on_node_doc(client, monkeypatch):
    from opensearch_pipeline.routes import kb_access

    class _DeptAdmin:
        user_id, name, role = "da1", "部管", "dept_admin"

    monkeypatch.setattr(kb_access, "_require_kb_console", lambda i: _DeptAdmin())
    import opensearch_pipeline.kb_authz as kz
    monkeypatch.setattr(kz, "managed_owner_depts", lambda kb: ["hr"])
    _wire(monkeypatch, _Cur(doc_mode="node", doc_owner=None))
    r = client.post("/api/kb/access-requests", json={"doc_id": "D1", "reason": "x"})
    assert r.status_code == 400 and "组织树" in r.json()["detail"]


# ── resign-images:node 文档走真 can_read_doc(调用签名回归钉,2026-08-04 红队)──
# 刻意不 mock can_read_doc:被钉的缝隙正是 api ↔ acl_policy 的调用契约
# (keyword-only grant/enforce 少传 ⇒ 恒 TypeError 被 except 兜住 ⇒ node 批全拒)。
# 脚手架独立于上方 _Cur/_Conn(其 document_meta 分支匹配不上 resign 的三列查询)。

class _MetaCur:
    """只应答 resign 的 document_meta 三列查询(doc_id, permission_level, owner_dept)。"""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows


class _MetaConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _MetaCur(self._rows)

    def close(self):
        pass


def _wire_resign(monkeypatch, *, flags):
    """resign node 分支的全部缝隙:DB 连接 / 模式批查 / 权威解析 / 身份 / 姿态旗。"""
    from opensearch_pipeline import access_grants, api, retriever
    from opensearch_pipeline.acl_policy import AclContext, DocAcl

    rows = [("DOC_NODE_OK", "dept_internal", None),
            ("DOC_NODE_OTHER", "dept_internal", None)]
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _MetaConn(rows))
    monkeypatch.setattr(access_grants, "resolve_acl_modes",
                        lambda ids, cur: {d: "node" for d in ids})
    acls = {
        "DOC_NODE_OK": DocAcl(mode="node", permission_level="dept_internal",
                              node_ids=(110,)),
        "DOC_NODE_OTHER": DocAcl(mode="node", permission_level="dept_internal",
                                 node_ids=(999,)),
    }
    monkeypatch.setattr(access_grants, "resolve_doc_acl",
                        lambda ids, cur, strict=False: {d: acls[d] for d in ids})
    ctx = AclContext(groups=("hr",), ancestor_dept_ids=(1, 110),
                     direct_dept_ids=(110,), node_channel_ok=True)
    monkeypatch.setattr(api, "_build_acl_ctx", lambda identity, user_id=None: ctx)
    monkeypatch.setattr(retriever, "_node_acl_flags", lambda: flags)
    return api


def test_resign_node_doc_grant_hit_not_all_denied(monkeypatch):
    """授权节点命中的 node 文档必须可签——缺陷态(TypeError 兜底)会全拒,本测试即红。"""
    api = _wire_resign(monkeypatch, flags=(True, True))
    ident = api.Identity(user_id="u1", acl_groups=["hr"])
    visible = api._resign_visible_doc_ids({"DOC_NODE_OK", "DOC_NODE_OTHER"}, ident)
    assert visible == {"DOC_NODE_OK"}   # 未授权篇不可见:防过修成 fail-open


def test_resign_node_doc_denied_when_grant_off(monkeypatch, caplog):
    """GRANT 关 ⇒ 真值表无条件 DENY,且必须是裁决而非异常兜底(无兜底告警)——
    钉住姿态旗真从 config 通到判定点(防硬编码 True/True 或实参对调)。"""
    import logging

    api = _wire_resign(monkeypatch, flags=(False, True))
    ident = api.Identity(user_id="u1", acl_groups=["hr"])
    with caplog.at_level(logging.WARNING):
        visible = api._resign_visible_doc_ids({"DOC_NODE_OK", "DOC_NODE_OTHER"}, ident)
    assert visible == set()
    assert "resign-images node 判定失败" not in caplog.text
