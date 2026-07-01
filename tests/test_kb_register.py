# -*- coding: utf-8 -*-
"""test_kb_register.py — kb register（自助上传登记）端到端行为，全程 simulate（#9）。

此前 register 在 sim 下不可跑：oss_url.head_object 恒返 None → register 永远 400，违反
CLAUDE.md「改动先在 simulate 验证」。#9 给 head_object 加了 sim 合成 HEAD，本套据此直接
驱动 kb_register（request=None，桩 DB），覆盖：
  happy-path 新建 / 公开待审批 / 幂等重登记 / 升版号自增 / 0 字节拒 / 超限拒 / uid 不符 / 坏 token，
  外加并发 1062 唯一键 → 幂等返回（#2 的修复路径，在 sim 下验证）。

桩 DB 按 SQL 关键字回放 fetchone，可注入 document_version INSERT 抛 1062 以模拟并发竞态。
"""
import hashlib

import pytest


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


# ── 桩 DB ────────────────────────────────────────────────────────────────
class _FakeCur:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        self._last = sql
        # 注入：document_version INSERT 抛 1062（并发双提交撞 uk_doc_version），只抛一次。
        if ("INSERT INTO" in sql and "document_version" in sql
                and self.conn.raise_dupkey_on_version_insert):
            self.conn.raise_dupkey_on_version_insert = False
            raise Exception(1062, "Duplicate entry for key 'uk_doc_version'")
        # 注入：内容查重 SELECT 抛错（验证 fail-open）
        if "m.owner_dept" in sql and "v.etag" in sql and self.conn.raise_on_dedup:
            raise Exception("simulated dedup query failure")
        return 1

    def fetchone(self):
        s = self._last
        if "document_version" in s and "WHERE raw_key=" in s:
            self.conn._rawkey_query_count += 1
            if self.conn.rolled_back:
                return self.conn.winner_row
            # F-38：模拟"锁前 raw_key 读空、持锁后复查命中赢家"的升版竞态——
            # 第 1 次(锁前)返回 idempotent_row(None)，第 2 次(锁内复查)返回 lock_recheck_row。
            if self.conn.lock_recheck_row is not None and self.conn._rawkey_query_count >= 2:
                return self.conn.lock_recheck_row
            return self.conn.idempotent_row
        if "current_version_no, permission_level" in s:
            return self.conn.meta_row
        return None

    def fetchall(self):
        # 内容查重 SELECT（按 etag 找其它 active 文档）
        if "m.owner_dept" in self._last and "v.etag" in self._last:
            return self.conn.dup_rows
        return []


class _FakeConn:
    def __init__(self, *, idempotent_row=None, meta_row=None, winner_row=None, raise_dupkey=False,
                 dup_rows=None, raise_on_dedup=False, lock_recheck_row=None):
        self.idempotent_row = idempotent_row
        self.meta_row = meta_row
        self.winner_row = winner_row
        self.lock_recheck_row = lock_recheck_row   # F-38 升版锁内复查命中行（None=不命中）
        self._rawkey_query_count = 0
        self.raise_dupkey_on_version_insert = raise_dupkey
        self.dup_rows = dup_rows or []        # 内容查重 SELECT 返回的 (doc_id, title, owner_dept) 行
        self.raise_on_dedup = raise_on_dedup
        self.calls = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCur(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _install_conn(monkeypatch, conn):
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: conn)
    return conn


def _dept_admin(monkeypatch, managed="marketing"):
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", managed)


def _mint(**over):
    from opensearch_pipeline import kb_upload
    p = {
        "uid": "da1", "action": "new", "doc_id": "DOC_TEST", "owner_dept": "marketing",
        "raw_key": "raw/marketing/DOC_TEST/UP1/方案.pdf", "filename": "方案.pdf", "ext": "pdf",
        "title": "营销方案", "category_l1": "", "category_l2": "",
        "permission_level": "dept_internal", "share_owner_depts": [],
        "max_size": kb_upload.MAX_UPLOAD_BYTES, "requires_approval": False, "owner_name": "张三",
    }
    p.update(over)
    return kb_upload.sign_upload_token(p)


def _call(monkeypatch, token, user_id="da1"):
    from opensearch_pipeline import api
    return api.kb_register(req=api.KbRegisterRequest(upload_token=token),
                           request=None, identity=api.Identity(user_id=user_id))


def _version_insert_call(conn):
    for sql, params in conn.calls:
        if "INSERT INTO" in sql and "document_version" in sql:
            return sql, params
    return None, None


# ── 测试 ─────────────────────────────────────────────────────────────────
def test_register_new_happy_path(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(idempotent_row=None))
    resp = _call(monkeypatch, _mint())
    assert resp.version_no == 1
    assert resp.content_process_status == "NOT_STARTED"
    assert resp.requires_kb_admin_approval is False
    assert resp.status_badge == "排队中"
    assert resp.idempotent is False
    assert resp.title == "营销方案"
    assert conn.committed is True
    # raw_key_hash 写入：document_version INSERT 的 params 含 sha256(raw_key)
    sql, params = _version_insert_call(conn)
    assert sql is not None and "raw_key_hash" in sql
    expect = hashlib.sha256("raw/marketing/DOC_TEST/UP1/方案.pdf".encode("utf-8")).hexdigest()
    assert expect in params


def test_register_public_requires_approval(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(idempotent_row=None))
    resp = _call(monkeypatch, _mint(permission_level="public"))
    assert resp.requires_kb_admin_approval is True
    assert resp.content_process_status == "PENDING_APPROVAL"
    assert resp.status_badge == "待审核"


def test_register_idempotent_reregister(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(idempotent_row=("DOC_TEST", 1, "NOT_STARTED")))
    resp = _call(monkeypatch, _mint())
    assert resp.idempotent is True
    assert resp.doc_id == "DOC_TEST"
    assert resp.version_no == 1


def test_register_version_up_increments_version_no(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(idempotent_row=None, meta_row=(3, "dept_internal", "active")))
    resp = _call(monkeypatch, _mint(action="version", permission_level="dept_internal"))
    assert resp.version_no == 4               # current 3 → +1
    assert resp.idempotent is False
    # 升版应 UPDATE document_meta.current_version_no（匹配语句体，别误命中 SELECT ... FOR UPDATE）
    assert any("SET current_version_no" in sql for sql, _ in conn.calls)


def test_register_version_up_on_retired_doc_409(monkeypatch):
    """F-37：退役文档在 register 写库入口也必须拦（upload-url 与 register 间存在 token TTL 窗口）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(idempotent_row=None, meta_row=(3, "dept_internal", "retired")))
    with pytest.raises(Exception) as ei:
        _call(monkeypatch, _mint(action="version", permission_level="dept_internal"))
    assert getattr(ei.value, "status_code", None) == 409


def test_register_version_lock_recheck_idempotent(monkeypatch):
    """F-38：升版并发双击——锁前 raw_key 读空，持锁后复查命中赢家行 → 幂等返回，不推高版本号。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(
        idempotent_row=None, meta_row=(3, "dept_internal", "active"),
        lock_recheck_row=("DOC_X", 4, "NOT_STARTED"),
    ))
    resp = _call(monkeypatch, _mint(action="version", permission_level="dept_internal"))
    assert resp.idempotent is True and resp.version_no == 4
    # 幂等命中即返回：绝不 UPDATE document_meta.current_version_no（避免版本空洞）。
    # 注意匹配真正的 UPDATE 语句体（"SET current_version_no"），别误命中 "SELECT ... FOR UPDATE"。
    assert not any("SET current_version_no" in sql for sql, _ in conn.calls)
    assert conn.committed is True


def test_register_zero_byte_rejected(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    monkeypatch.setenv("RAG_SIM_OSS_HEAD_SIZE", "0")
    _install_conn(monkeypatch, _FakeConn())
    with pytest.raises(Exception) as ei:
        _call(monkeypatch, _mint())
    assert getattr(ei.value, "status_code", None) == 400


def test_register_oversize_rejected(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    from opensearch_pipeline import kb_upload
    monkeypatch.setenv("RAG_SIM_OSS_HEAD_SIZE", str(kb_upload.MAX_UPLOAD_BYTES + 1))
    _install_conn(monkeypatch, _FakeConn())
    with pytest.raises(Exception) as ei:
        _call(monkeypatch, _mint())
    assert getattr(ei.value, "status_code", None) == 413


def test_register_token_uid_mismatch_forbidden(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    _install_conn(monkeypatch, _FakeConn())
    with pytest.raises(Exception) as ei:
        _call(monkeypatch, _mint(uid="someone_else"))   # token uid ≠ 调用者
    assert getattr(ei.value, "status_code", None) == 403


def test_register_bad_token_rejected(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    _install_conn(monkeypatch, _FakeConn())
    with pytest.raises(Exception) as ei:
        _call(monkeypatch, "not-a-valid-token")
    assert getattr(ei.value, "status_code", None) == 400


def test_content_dup_visible_to_kb_admin(monkeypatch):
    """内容查重：kb_admin 可管理全部 → 同 etag 的跨部门文档以详情形式出现在 content_dups。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _install_conn(monkeypatch, _FakeConn(
        idempotent_row=None,
        dup_rows=[("DOC_OTHER", "同款安全手册", "finance")],   # 别的部门已有同内容
    ))
    resp = _call(monkeypatch, _mint())
    assert resp.version_no == 1
    assert len(resp.content_dups) == 1
    assert resp.content_dups[0].doc_id == "DOC_OTHER"
    assert resp.content_dups[0].owner_dept == "finance"
    assert resp.content_dups_other == 0


def test_content_dup_redacted_for_dept_admin_other_scope(monkeypatch):
    """隐私：dept_admin 对【管理范围外】部门的同内容命中只计数，不泄露标题/部门。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _install_conn(monkeypatch, _FakeConn(
        idempotent_row=None,
        dup_rows=[("DOC_FIN", "财务密件", "finance"),       # 范围外 → 仅计数
                  ("DOC_MKT", "营销同款", "marketing")],     # 范围内 → 给详情
    ))
    resp = _call(monkeypatch, _mint(), user_id="da1")
    assert [d.doc_id for d in resp.content_dups] == ["DOC_MKT"]   # 仅可见的
    assert resp.content_dups_other == 1                          # finance 那篇只计数
    assert all(d.owner_dept != "finance" for d in resp.content_dups)


def test_content_dup_none(monkeypatch):
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _install_conn(monkeypatch, _FakeConn(idempotent_row=None, dup_rows=[]))
    resp = _call(monkeypatch, _mint())
    assert resp.content_dups == [] and resp.content_dups_other == 0


def test_version_up_skips_content_dedup(monkeypatch):
    """升版（同 doc_id 换文件）天然不算重复 → 不查内容、不报警。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    conn = _install_conn(monkeypatch, _FakeConn(
        idempotent_row=None, meta_row=(2, "dept_internal", "active"),
        dup_rows=[("DOC_X", "本不该出现", "finance")],
    ))
    resp = _call(monkeypatch, _mint(action="version", permission_level="dept_internal"))
    assert resp.version_no == 3
    assert resp.content_dups == [] and resp.content_dups_other == 0
    # 没有发出内容查重 SELECT
    assert not any("m.owner_dept" in s and "v.etag" in s for s, _ in conn.calls)


def test_content_dedup_fail_open(monkeypatch):
    """内容查重出错 → fail-open：register 仍成功，只是不报警。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    _install_conn(monkeypatch, _FakeConn(idempotent_row=None, raise_on_dedup=True))
    resp = _call(monkeypatch, _mint())
    assert resp.version_no == 1                       # 登记成功
    assert resp.content_dups == [] and resp.content_dups_other == 0


def test_register_stores_etag(monkeypatch):
    """register 把 OSS ETag 写入 document_version（内容查重的指纹来源）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setenv("RAG_SIM_OSS_HEAD_ETAG", "ABC123ETAG")
    conn = _install_conn(monkeypatch, _FakeConn(idempotent_row=None))
    _call(monkeypatch, _mint())
    sql, params = _version_insert_call(conn)
    assert "etag" in sql and "ABC123ETAG" in params


def test_head_object_sim_returns_synthetic_meta(monkeypatch):
    """#9：head_object 在 sim 下返回合成 HEAD（此前恒 None → register 永远 400）。"""
    _skip_if_not_sim()
    from opensearch_pipeline.oss_url import head_object
    monkeypatch.delenv("RAG_SIM_OSS_HEAD_SIZE", raising=False)
    m = head_object("raw/marketing/DOC_X/UP1/x.pdf")
    assert m is not None and m["size"] == 1024            # 默认非空
    assert m["content_type"] == "application/pdf"          # 按扩展名推断
    monkeypatch.setenv("RAG_SIM_OSS_HEAD_SIZE", "0")       # 可覆盖（0 字节/超限分支钩子）
    assert head_object("raw/x/y.pdf")["size"] == 0
    assert head_object("") is None                         # 空 key → None


def test_register_concurrent_dupkey_returns_idempotent(monkeypatch):
    """#2：并发双提交撞 uk_doc_version(1062) → 回滚 + 按 raw_key 重查赢家行 → 幂等成功，不抛 500。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(
        idempotent_row=None,                           # 初查：本事务没看到（竞态前提）
        winner_row=("DOC_TEST", 1, "NOT_STARTED"),     # 回滚后重查：赢家已提交
        raise_dupkey=True,
    ))
    resp = _call(monkeypatch, _mint())
    assert resp.idempotent is True
    assert resp.doc_id == "DOC_TEST"
    assert resp.version_no == 1
    assert conn.rolled_back is True                     # 走了回滚分支而非 500
