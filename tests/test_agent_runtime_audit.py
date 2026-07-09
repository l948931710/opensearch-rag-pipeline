# -*- coding: utf-8 -*-
"""
test_agent_runtime_audit.py — 合规审计（agent_audit_log / audit.py，WS1 收尾③）

覆盖：
- _actor 抽取（真 ctx / None ctx 兜底）
- RDSAuditLog.record 成功（假 conn 断言 INSERT + commit）
- **fail-closed**：DB 不可用 + fail_closed=True → 抛 AuditWriteError（高风险阻断）
- **fail-open**：DB 不可用 + fail_closed=False → 返回 None、不抛（不阻断）
- NULL_AUDIT 无操作
- ToolExecutor 集成：HIGH_WRITE 审计失败 → 阻断执行（tool 不跑、invocation=failed）；
  READ_ONLY 审计失败 → fail-open 继续执行；审计入参字段正确
"""
from datetime import datetime, timezone

import pytest

from opensearch_pipeline.agent_runtime.audit import (
    NULL_AUDIT, AuditWriteError, RDSAuditLog, _actor)
from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec
from opensearch_pipeline.agent_runtime.tool_executor import ToolExecutor


# ── 假 DB conn ────────────────────────────────────────────────────
class _Cur:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append((sql, params))

    def close(self):
        pass


class _Conn:
    def __init__(self, sink):
        self.sink = sink
        self.committed = self.rolled_back = self.closed = False

    def cursor(self):
        return _Cur(self.sink)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _ctx():
    return ExecutionContext.create(
        request_id="req-1", user_id="u42", acl_groups=["production_mold", "public"],
        roles=["employee", "dept_admin"], channel="console", thread_id="t1", run_id="run-9",
        auth_resolved_at=datetime(2026, 7, 8, tzinfo=timezone.utc))


# ── _actor ────────────────────────────────────────────────────────
def test_actor_from_ctx():
    user_id, roles, acl, channel, rid = _actor(_ctx())
    assert user_id == "u42"
    assert roles == "employee,dept_admin"
    assert '"production_mold"' in acl and '"public"' in acl
    assert channel == "console" and rid == "req-1"


def test_actor_none_ctx_placeholder():
    assert _actor(None) == ("-", None, None, None, None)


# ── RDSAuditLog.record ────────────────────────────────────────────
def test_record_success_inserts_and_commits():
    sink = []
    conn = _Conn(sink)
    audit = RDSAuditLog()
    audit._conn = lambda: conn                    # 注入假 conn
    aid = audit.record(_ctx(), event_type="tool_call", action="u8.create@1.0.0",
                       decision="authorized", risk_level="high_write", policy_id="p1",
                       args_digest="d" * 64, detail={"scope": "u8.writeback"},
                       run_id="run-9", step_no=3, fail_closed=True)
    assert isinstance(aid, str) and len(aid) == 32
    assert conn.committed and conn.closed and not conn.rolled_back
    assert len(sink) == 1
    sql, params = sink[0]
    assert "INSERT INTO" in sql and "agent_audit_log" in sql
    assert params[0] == aid and "u8.create@1.0.0" in params and "high_write" in params
    assert params[1] == "run-9" and params[2] == 3          # run_id, step_no


def test_record_run_id_falls_back_to_ctx():
    sink = []
    audit = RDSAuditLog()
    audit._conn = lambda: _Conn(sink)
    audit.record(_ctx(), event_type="tool_call", action="a", decision="authorized")
    # run_id 未显式传 → 取 ctx.run_id
    assert sink[0][1][1] == "run-9"


def test_record_fail_closed_raises():
    def boom():
        raise RuntimeError("db unreachable")

    audit = RDSAuditLog()
    audit._conn = boom
    with pytest.raises(AuditWriteError):
        audit.record(_ctx(), event_type="tool_call", action="u8", decision="authorized",
                     fail_closed=True)


def test_record_fail_open_swallows():
    def boom():
        raise RuntimeError("db unreachable")

    audit = RDSAuditLog()
    audit._conn = boom
    # fail_closed 默认 False → 吞掉返回 None，不抛
    assert audit.record(_ctx(), event_type="tool_call", action="kb.search",
                        decision="authorized") is None


def test_null_audit_noop():
    assert NULL_AUDIT.record(_ctx(), event_type="x", action="y", decision="z") is None


# ── ToolExecutor 集成 ─────────────────────────────────────────────
class _Store:
    def __init__(self):
        self.invocations = []

    def record_invocation(self, run_id, step_no, **kw):
        iid = f"inv{len(self.invocations) + 1}"
        self.invocations.append({"id": iid, "status": kw["status"]})
        return iid

    def finish_invocation(self, invocation_id, **kw):
        for inv in self.invocations:
            if inv["id"] == invocation_id:
                inv.update(kw)

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return None


class _Tool:
    def __init__(self, spec):
        self.spec = spec
        self.calls = 0

    def run(self, ctx, args, idempotency_key=None):
        self.calls += 1
        return ToolResult.text_ok("ok")


class _FakeAudit:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def record(self, ctx, **kw):
        self.calls.append(kw)
        if self.fail:
            if kw.get("fail_closed"):
                raise AuditWriteError("audit down")
            return None
        return "aud1"


def _spec(risk=RiskLevel.READ_ONLY, idem="none", name="t"):
    return ToolSpec(name=name, version="1.0.0", description="d", input_schema={"type": "object"},
                    output_schema={"type": "object"}, risk_level=risk, permission_scope="kb.search",
                    data_classification="internal", idempotency=idem)


def _call(ex, tool, **kw):
    base = dict(run_id="r", step_no=1, policy_decision="allow", policy_id="p")
    base.update(kw)
    return ex.execute(None, tool, {"q": "x"}, **base)


def test_high_write_audit_failure_blocks_execution():
    store, audit = _Store(), _FakeAudit(fail=True)
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="u8"))
    r = _call(ToolExecutor(store, audit=audit), tool, idempotency_key="k1")
    assert r.status == "failed" and "审计" in r.error
    assert tool.calls == 0                                   # 未执行（无审计不产生副作用）
    assert store.invocations[0]["status"] == "failed"        # invocation 收尾为 failed
    assert audit.calls[0]["fail_closed"] is True             # 高风险=fail-closed


def test_read_only_audit_failure_is_fail_open():
    store, audit = _Store(), _FakeAudit(fail=True)
    tool = _Tool(_spec())                                    # READ_ONLY
    r = _call(ToolExecutor(store, audit=audit), tool)
    assert r.status == "succeeded" and tool.calls == 1       # fail-open：照常执行
    assert audit.calls[0]["fail_closed"] is False


def test_audit_record_fields():
    store, audit = _Store(), _FakeAudit()
    tool = _Tool(_spec(name="kb"))
    _call(ToolExecutor(store, audit=audit), tool)
    kw = audit.calls[0]
    assert kw["event_type"] == "tool_call" and kw["action"] == "kb@1.0.0"
    assert kw["decision"] == "authorized" and kw["risk_level"] == "read_only"
    assert kw["run_id"] == "r" and kw["step_no"] == 1 and kw["policy_id"] == "p"
    assert kw["args_digest"] and kw["detail"]["policy_decision"] == "allow"


def test_no_audit_defaults_null_no_crash():
    # 不注入 audit（默认 NULL_AUDIT）→ 既有行为零变化
    store = _Store()
    tool = _Tool(_spec())
    r = _call(ToolExecutor(store), tool)
    assert r.status == "succeeded" and tool.calls == 1
