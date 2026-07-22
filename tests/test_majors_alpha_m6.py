# -*- coding: utf-8 -*-
"""Majors α5(M6)审批入场配额(2026-07-21 codex 共识)。

锁死的不变量:
- caps 全 0(默认)→ insert_request 零 058 接触(byte-identical);
- 任一 cap>0:哨兵 FOR UPDATE 串行化 → global→user→user-tool 三 COUNT → 超限
  ApprovalQuotaExceeded(与 suspend 同事务,回滚零副作用);
- 058 表缺(1146)/哨兵行缺 → ApprovalQuotaUnavailable **fail-closed**;
- readiness:caps 全 0 → skipped;cap>0 且表缺 → missing(红)。
真库双连接并发穿透见 test_majors_alpha_m6_db.py。
"""
import pytest

from opensearch_pipeline.agent_runtime import approval_store as ap_mod
from opensearch_pipeline.agent_runtime.approval_store import (
    ApprovalQuotaExceeded,
    ApprovalQuotaUnavailable,
    RDSApprovalStore,
)
from opensearch_pipeline.agent_runtime.context import ExecutionContext


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(ap_mod, "_SCOPE_RESOLVERS", {})
    monkeypatch.setattr(ap_mod, "_op_db", lambda: "op")
    for n in ("RAG_AGENT_APPROVAL_GLOBAL_CAP", "RAG_AGENT_APPROVAL_PENDING_CAP",
              "RAG_AGENT_APPROVAL_PER_TOOL_CAP"):
        monkeypatch.delenv(n, raising=False)


class _Cur:
    """脚本游标:sentinel_row 控哨兵;counts 队列按序回填 COUNT;table_missing 时
    哨兵 SELECT 抛 1146。"""

    def __init__(self, *, sentinel_row=("approval_admission",), counts=(),
                 table_missing=False):
        self.sqls = []
        self._sentinel = sentinel_row
        self._counts = list(counts)
        self._missing = table_missing
        self._last = None

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        self._last = sql
        if "agent_quota_lock" in sql and self._missing:
            raise Exception(1146, "Table 'op.agent_quota_lock' doesn't exist")

    def fetchone(self):
        if "agent_quota_lock" in (self._last or ""):
            return self._sentinel
        if "COUNT(*)" in (self._last or ""):
            return (self._counts.pop(0),) if self._counts else (0,)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ctx(user="u1"):
    return ExecutionContext.create(request_id="", user_id=user, acl_groups=["pmc"],
                                   roles=("employee",), channel="console", thread_id="t1")


_CALL = {"tool_name": "ontology_identity_resolve", "call_id": "c1", "arguments": {}}


def _insert(cur, store=None):
    st = store or RDSApprovalStore()
    return st.insert_request(cur, "r1", _ctx(), _CALL)


def test_caps_all_zero_no_quota_sql():
    cur = _Cur()
    _insert(cur)
    assert not any("agent_quota_lock" in s for s, _ in cur.sqls)   # 零接触
    assert any("INSERT" in s for s, _ in cur.sqls)


def test_under_cap_passes_and_locks_sentinel(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_APPROVAL_PENDING_CAP", "10")
    cur = _Cur(counts=[3])
    _insert(cur)
    joined = "\n".join(s for s, _ in cur.sqls)
    assert "agent_quota_lock" in joined and "FOR UPDATE" in joined
    assert any("INSERT" in s for s, _ in cur.sqls)


def test_over_cap_raises_exceeded(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_APPROVAL_PENDING_CAP", "3")
    cur = _Cur(counts=[3])
    with pytest.raises(ApprovalQuotaExceeded, match="待审批请求已达上限"):
        _insert(cur)
    assert not any("INSERT INTO" in s and "approval_request" in s for s, _ in cur.sqls)


def test_global_and_per_tool_layers(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_APPROVAL_GLOBAL_CAP", "100")
    monkeypatch.setenv("RAG_AGENT_APPROVAL_PER_TOOL_CAP", "2")
    cur = _Cur(counts=[50, 2])      # global 过(50<100),per-tool 触顶(2>=2)
    with pytest.raises(ApprovalQuotaExceeded, match="工具"):
        _insert(cur)
    # global 层单独触顶
    monkeypatch.setenv("RAG_AGENT_APPROVAL_GLOBAL_CAP", "50")
    cur2 = _Cur(counts=[50])
    with pytest.raises(ApprovalQuotaExceeded, match="全局上限"):
        _insert(cur2)


def test_table_missing_fail_closed(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_APPROVAL_PENDING_CAP", "10")
    cur = _Cur(table_missing=True)
    with pytest.raises(ApprovalQuotaUnavailable, match="058"):
        _insert(cur)


def test_sentinel_row_missing_fail_closed(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_APPROVAL_PENDING_CAP", "10")
    cur = _Cur(sentinel_row=None)
    with pytest.raises(ApprovalQuotaUnavailable, match="哨兵"):
        _insert(cur)


def test_non_quota_db_error_propagates(monkeypatch):
    """非 1146 的哨兵读故障原样上抛——绝不把基础设施故障吞成配额语义。"""
    monkeypatch.setenv("RAG_AGENT_APPROVAL_PENDING_CAP", "10")

    class _BoomCur(_Cur):
        def execute(self, sql, args=None):
            self.sqls.append((sql, args))
            if "agent_quota_lock" in sql:
                raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        _insert(_BoomCur())


# ── readiness 契约 ───────────────────────────────────────────────────────────


def test_readiness_skipped_when_caps_zero(monkeypatch):
    from opensearch_pipeline import readiness
    for n in ("RAG_AGENT_APPROVAL_GLOBAL_CAP", "RAG_AGENT_APPROVAL_PENDING_CAP",
              "RAG_AGENT_APPROVAL_PER_TOOL_CAP"):
        monkeypatch.delenv(n, raising=False)
    assert readiness.approval_quota_contract_status() == "skipped"


def test_readiness_missing_table_red(monkeypatch):
    from opensearch_pipeline import readiness
    monkeypatch.setenv("RAG_AGENT_APPROVAL_PENDING_CAP", "5")
    monkeypatch.setattr(readiness, "_tables_exist", lambda db, tables: "missing")
    monkeypatch.setattr(readiness, "_cached", lambda key, ttl, compute: compute())
    assert "missing" in readiness.approval_quota_contract_status()
