# -*- coding: utf-8 -*-
"""tests/test_audit_log.py — Phase-1 L5: append-only kb_audit_log writer + deactivation wiring.

Revives the previously-dead kb_audit_log (zero writers) into an append-only lineage trail.
Invariants: fail-open (never raises), no-op in simulate, own connection (doesn't poison the
caller's txn), and wired at the irreversible DEACTIVATE transition.
"""
import inspect


def test_audit_trace_id_from_run_provenance():
    from opensearch_pipeline.audit_log import audit_trace_id
    assert audit_trace_id({"run_provenance": {"git_commit": "abc123", "bizdate": "20260616"}}) == "abc123:20260616"
    assert audit_trace_id({"run_provenance": {"git_commit": "abc123"}}) == "abc123"
    assert audit_trace_id({"bizdate": "20260616"}) == "20260616"
    assert audit_trace_id({}) is None
    assert audit_trace_id(None) is None


def test_write_audit_noop_in_simulate(monkeypatch):
    """simulate=True must not touch the DB at all."""

    def _boom(**kw):
        raise AssertionError("_get_db_conn must NOT be called in simulate mode")

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    # must not raise
    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id="d", version_no=1, action_type="DEACTIVATE", simulate=True)


def test_write_audit_fail_open_on_db_error(monkeypatch, caplog):
    """A DB failure must be swallowed (audit is auxiliary; never break ingest)."""
    import logging

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    from opensearch_pipeline.audit_log import write_audit
    with caplog.at_level(logging.WARNING, logger="opensearch_pipeline.audit_log"):
        write_audit(doc_id="d", version_no=1, action_type="INDEX", action_result="SUCCESS")  # must not raise
    assert any("kb_audit_log write failed" in r.getMessage() for r in caplog.records)


def test_write_audit_inserts_expected_row(monkeypatch):

    captured = {}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            captured["committed"] = True

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: _Conn())
    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id="DOC_X", version_no=3, action_type="DEACTIVATE", action_result="SUCCESS",
                trace_id="abc:20260616", message="retired", simulate=False)

    assert "INSERT INTO fuling_knowledge.kb_audit_log" in captured["sql"]
    p = captured["params"]
    assert p[0] == "abc:20260616" and p[1] == "DOC_X" and p[2] == 3
    assert p[3] == "DEACTIVATE" and p[4] == "SUCCESS"
    assert captured.get("committed") and captured.get("closed")


def test_write_audit_cursor_path_same_txn_no_new_conn(monkeypatch):
    """cursor 路径（serving）：用调用方游标写、【不】开新连接（同事务原子审计，B1）。"""

    def _boom(**kw):
        raise AssertionError("cursor 路径不得自开连接（应复用调用方事务）")

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id="D1", version_no=2, action_type="RETIRE_REQUEST",
                operator_type="user", operator_id="u1", trace_id="t", cursor=_Cur())
    assert "INSERT INTO fuling_knowledge.kb_audit_log" in captured["sql"]
    assert captured["params"][1] == "D1" and captured["params"][3] == "RETIRE_REQUEST"


def test_write_audit_cursor_path_propagates_error():
    """cursor 路径【不吞】异常（区别于 ingestion fail-open）：游标抛错 → 向上传播 → 调用方事务回滚。"""
    import pytest

    class _BadCur:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

    from opensearch_pipeline.audit_log import write_audit
    with pytest.raises(RuntimeError):
        write_audit(doc_id="D1", version_no=1, action_type="KB_ADMIN_REVOKE", cursor=_BadCur())


# ── ACL 策略版本（acl_policy_version 盖进 ACL 审计行）──
def test_acl_policy_version_stable_and_content_addressed():
    from opensearch_pipeline.versions import acl_policy_version
    v = acl_policy_version()
    assert v and v != "unknown" and len(v) == 12   # 真实 12-hex 内容指纹（test 环境映射可 import）
    assert acl_policy_version() == v                # 同进程稳定


def test_acl_policy_version_changes_when_mapping_changes(monkeypatch):
    import opensearch_pipeline.dingtalk_identity as di
    from opensearch_pipeline.versions import acl_policy_version
    before = acl_policy_version()
    patched = dict(di._DEPT_NAME_TO_GROUPS)
    patched["新部门X"] = ["finance"]
    monkeypatch.setattr(di, "_DEPT_NAME_TO_GROUPS", patched)
    assert acl_policy_version() != before           # 映射改动 → 版本自动变（无需手动 bump）


def test_acl_policy_version_changes_when_ancestry_anchors_change(monkeypatch):
    """锚表也是活策略（RAG_ACL_ANCESTRY 开时它就是【唯一】的活策略）——改锚必须 bump。
    没有这条，把 ancestry_anchors 从 payload 里删掉也没有测试会红。"""
    import opensearch_pipeline.dept_ancestry as da
    from opensearch_pipeline.versions import acl_policy_version
    before = acl_policy_version()
    patched = dict(da.ANCHOR_GROUPS_BY_DEPT_ID)
    patched[999000111] = ["finance"]
    monkeypatch.setattr(da, "ANCHOR_GROUPS_BY_DEPT_ID", patched)
    assert acl_policy_version() != before


def test_acl_policy_version_distinguishes_ancestry_flag_state(monkeypatch):
    """flag ON/OFF 是【两套活策略】（对上百名员工给出不同的组）——指纹必须能分辨，
    否则审计员看到版本号无从判断当时哪张表在活。"""
    from opensearch_pipeline.versions import acl_policy_version
    monkeypatch.delenv("RAG_ACL_ANCESTRY", raising=False)
    off = acl_policy_version()
    monkeypatch.setenv("RAG_ACL_ANCESTRY", "true")
    assert acl_policy_version() != off


def _capture_conn(monkeypatch, captured):

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): captured["params"] = params

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: _Conn())


def test_write_audit_stamps_acl_policy_on_acl_action(monkeypatch):
    """ACL 授权动作（KB_ADMIN_GRANT 等）→ 消息盖上 [acl_policy=<ver>]，原文保留。"""
    captured = {}
    _capture_conn(monkeypatch, captured)
    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id=None, version_no=None, action_type="KB_ADMIN_GRANT",
                operator_id="admin1", trace_id="t", message="granted kb_admin to u9")
    msg = captured["params"][8]
    assert msg.startswith("[acl_policy=") and "granted kb_admin to u9" in msg


def test_write_audit_no_acl_stamp_on_lifecycle_action(monkeypatch):
    """文档生命周期动作（DEACTIVATE 等）不盖 ACL 策略版本（避免噪声）。"""
    captured = {}
    _capture_conn(monkeypatch, captured)
    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id="D", version_no=1, action_type="DEACTIVATE", message="retired")
    assert captured["params"][8] == "retired"


# ── staffId 掩码（2026-08-04：approval-history bank_card 误伤修复）──────────

def test_mask_staff_id_long_numeric():
    """16+ 位钉钉 staffId → 首4…尾4（不再落入展示侧 bank_card 规则区间）。"""
    from opensearch_pipeline.audit_log import mask_staff_id
    assert mask_staff_id("999900001111222233") == "9999…2233"
    assert mask_staff_id("99990000111122223333") == "9999…3333"   # 20 位也掩（格式无关）


def test_mask_staff_id_short_passthrough():
    """≤8 字符（测试桩/短 id）撞不到 16 位规则 → 原样保留审计可读性。"""
    from opensearch_pipeline.audit_log import mask_staff_id
    assert mask_staff_id("u9") == "u9"
    assert mask_staff_id("mgr002") == "mgr002"
    assert mask_staff_id("") == ""
    assert mask_staff_id(None) == ""


def test_masked_grant_message_survives_display_redaction():
    """掩码后的 KB_ADMIN_GRANT 模板消息过展示侧 redact_text 零替换（本 bug 的回归锚）。

    对照组钉住两个设计依据：完整 18 位 staffId 被 bank_card 吞成占位符（原 bug 复现）；
    '****' 分隔的掩码被 masked_id 吞（分隔符必须 '…' 的原因）。
    """
    from opensearch_pipeline.audit_log import mask_staff_id
    from opensearch_pipeline.redaction import redact_text
    msg = (f"[acl_policy=ab12cd34] grant dept_admin {mask_staff_id('999900001111222233')} "
           "→ depts=- nodes=34265162")
    out, counts = redact_text(msg)
    assert out == msg and counts == {}
    assert "[银行卡号已脱敏]" in redact_text("grant dept_admin 999900001111222233 → depts=-")[0]
    assert "[标识已脱敏]" in redact_text("grant dept_admin 1013****1320 → depts=-")[0]


def test_deactivate_wires_audit_write():
    """node_deactivate_old_chunks must emit a DEACTIVATE audit on the irreversible retirement.

    A7（2026-07-25）：改走 write_audit_many（按旧 chunk 条数逐条开连接+提交的 N+1 合并），
    **行粒度不变**——每条退役旧行仍写一行；行数断言在
    test_production_hardening.test_t_deactivate_defers_on_limit_boundary_cut。"""
    from opensearch_pipeline.pipeline_nodes import node_deactivate_old_chunks
    src = inspect.getsource(node_deactivate_old_chunks)
    assert "from opensearch_pipeline.audit_log import write_audit_many" in src
    assert '"action_type": "DEACTIVATE"' in src
    assert "simulate=simulate_db" in src, "audit must no-op in simulate (pass simulate=simulate_db)"


# ═══════════════ A7（2026-07-25）：write_audit_many 的失败语义 ═══════════════
# 批量化只合并 DB 往返，不得削弱 fail-open 与逐行隔离；其中 commit 异常有特殊约定：
# 提交结果未知 + kb_audit_log 无业务唯一键（只有自增主键）⇒ **绝不回放**，否则可能写重。

class _ManyCursor:
    def __init__(self, owner, fail_executemany=False):
        self.owner = owner
        self.fail_executemany = fail_executemany

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        self.owner.executemany_calls.append(len(rows))
        if self.fail_executemany:
            raise RuntimeError("executemany boom")

    def execute(self, sql, params=None):
        self.owner.execute_calls.append(params)


class _ManyConn:
    def __init__(self, fail_executemany=False, fail_commit=False, fail_rollback=False):
        self.executemany_calls = []
        self.execute_calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self._fe, self._fc, self._fr = fail_executemany, fail_commit, fail_rollback

    def cursor(self):
        return _ManyCursor(self, self._fe)

    def commit(self):
        self.commits += 1
        if self._fc:
            raise RuntimeError("commit boom")

    def rollback(self):
        self.rollbacks += 1
        if self._fr:
            raise RuntimeError("rollback boom")

    def close(self):
        self.closed += 1


def _rows(n):
    return [{"doc_id": f"d{i}", "version_no": 1, "action_type": "DEACTIVATE",
             "message": f"m{i}"} for i in range(n)]


def test_write_audit_many_noop_in_simulate(monkeypatch):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("simulate 下不得连库")))
    from opensearch_pipeline.audit_log import write_audit_many
    write_audit_many(_rows(3), simulate=True)
    write_audit_many([], simulate=False)      # 空输入同样零连接


def test_write_audit_many_single_roundtrip(monkeypatch):
    conn = _ManyConn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: conn)
    from opensearch_pipeline.audit_log import write_audit_many
    write_audit_many(_rows(1200))
    assert conn.executemany_calls == [500, 500, 200]   # 分块
    assert conn.commits == 3 and conn.closed == 1


def test_write_audit_many_falls_back_row_by_row_on_executemany_error(monkeypatch):
    """普通执行失败 → rollback + 逐行新事务重试，恢复旧的逐行隔离。"""
    conn = _ManyConn(fail_executemany=True)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: conn)
    seen = []
    import opensearch_pipeline.audit_log as al
    monkeypatch.setattr(al, "write_audit", lambda **kw: seen.append(kw["doc_id"]))
    al.write_audit_many(_rows(3))
    assert conn.rollbacks == 1
    assert seen == ["d0", "d1", "d2"], "每行都要用独立事务重试"


def test_write_audit_many_does_not_replay_after_commit_failure(monkeypatch):
    """commit 结果未知 → 绝不回放（kb_audit_log 无业务唯一键，回放必然可能写重）。"""
    conn = _ManyConn(fail_commit=True)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: conn)
    replayed = []
    import opensearch_pipeline.audit_log as al
    monkeypatch.setattr(al, "write_audit", lambda **kw: replayed.append(kw))
    al.write_audit_many(_rows(3))
    assert conn.executemany_calls == [3]
    assert replayed == [], "提交结果未知时不得回放"


def test_write_audit_many_discards_connection_when_rollback_fails(monkeypatch):
    """回滚都失败 → 连接状态不可信，必须丢弃后逐行各自开新连接。"""
    conns = [_ManyConn(fail_executemany=True, fail_rollback=True), _ManyConn()]
    made = []

    def _mk(**kw):
        c = conns[len(made)] if len(made) < len(conns) else _ManyConn()
        made.append(c)
        return c

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _mk)
    import opensearch_pipeline.audit_log as al
    monkeypatch.setattr(al, "write_audit", lambda **kw: None)
    al.write_audit_many(_rows(2))
    assert conns[0].closed >= 1, "坏连接必须被丢弃"


def test_write_audit_many_fail_open_on_connect_error(monkeypatch, caplog):
    import logging
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    from opensearch_pipeline.audit_log import write_audit_many
    with caplog.at_level(logging.WARNING, logger="opensearch_pipeline.audit_log"):
        write_audit_many(_rows(2))     # must not raise
    assert any("batch write failed" in r.getMessage() for r in caplog.records)


def test_write_audit_many_rows_are_field_identical_to_write_audit():
    """两条路径共用 _audit_params → 批量化不得悄悄改变审计行内容。"""
    from opensearch_pipeline.audit_log import _audit_params
    one = _audit_params(doc_id="d", version_no=2, action_type="DEACTIVATE",
                        trace_id="t", message="m")
    assert one == ("t", "d", 2, "DEACTIVATE", "SUCCESS", "pipeline", None, None, "m")
    # ACL 动作仍盖策略版本戳
    acl = _audit_params(doc_id="d", version_no=1, action_type="APPROVE", message="x")
    assert acl[8].startswith("[acl_policy=")


def test_register_wires_audit_write():
    """node_register_metadata must emit a REGISTER audit (doc/version lifecycle start)."""
    from opensearch_pipeline.pipeline_nodes import node_register_metadata
    src = inspect.getsource(node_register_metadata)
    assert 'action_type="REGISTER"' in src and "write_audit(" in src
    assert "simulate=simulate_db" in src


def test_chunk_status_closure_wires_audit_write():
    """node_write_chunk_meta status closure must emit a CHUNK audit (DONE/EMPTY) per (doc,version)."""
    from opensearch_pipeline.pipeline_nodes import node_write_chunk_meta
    src = inspect.getsource(node_write_chunk_meta)
    assert 'action_type="CHUNK"' in src and "write_audit(" in src
    assert "simulate=simulate_db" in src


def test_index_status_wires_audit_write():
    """node_update_index_status must emit an INDEX audit (SUCCESS/FAILED) per (doc,version)."""
    from opensearch_pipeline.pipeline_nodes import node_update_index_status
    src = inspect.getsource(node_update_index_status)
    assert 'action_type="INDEX"' in src and "write_audit(" in src
    assert '"FAILED" if (_d, _v) in failed_doc_versions else "SUCCESS"' in src
