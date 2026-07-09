# -*- coding: utf-8 -*-
"""
test_agent_runtime_context.py — ExecutionContext / RunBudget（Task 8 / 报告 §3）

覆盖：服务端构造（acl_groups/roles 归一为不可变 tuple）· frozen 不可写（模型/请求体伪造无效）·
needs_reauth 阈值（resume 重解析，铁律 5）· RunBudget caps 与 deadline · with_run_id 返回新实例 ·
与 run_store.create_run 组合冒烟（ExecutionContext 满足 create_run 所读字段）。
"""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from opensearch_pipeline.agent_runtime.context import (
    DEFAULT_REAUTH_THRESHOLD_S,
    ExecutionContext,
    RunBudget,
)


def _ctx(**over):
    base = dict(request_id="req1", user_id="u1", acl_groups=["g1", "g2"],
                roles=["employee"], channel="console", thread_id="conv:staff")
    base.update(over)
    return ExecutionContext.create(**base)


# ── 服务端构造 + 不可变 ─────────────────────────────────────────
def test_create_normalizes_to_tuples():
    ctx = _ctx(acl_groups=["a", "b"], roles=["dept_admin"])
    assert ctx.acl_groups == ("a", "b") and isinstance(ctx.acl_groups, tuple)
    assert isinstance(ctx.roles, tuple)


def test_frozen_cannot_reassign_identity():
    ctx = _ctx()
    with pytest.raises(FrozenInstanceError):
        ctx.user_id = "attacker"
    with pytest.raises(FrozenInstanceError):
        ctx.acl_groups = ("forged_finance",)


def test_acl_groups_tuple_is_immutable():
    ctx = _ctx()
    assert not hasattr(ctx.acl_groups, "append")   # tuple 无 append，模型无法追加权限组


def test_run_id_defaults_none_and_backfill_returns_new():
    ctx = _ctx()
    assert ctx.run_id is None
    ctx2 = ctx.with_run_id("abc123")
    assert ctx2.run_id == "abc123"
    assert ctx.run_id is None                        # 原实例不变（frozen）
    assert ctx2 is not ctx


def test_has_role():
    ctx = _ctx(roles=["employee", "agent_admin"])
    assert ctx.has_role("agent_admin") and not ctx.has_role("kb_admin")


# ── needs_reauth（铁律 5）────────────────────────────────────────
def test_needs_reauth_threshold():
    t0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    ctx = _ctx(auth_resolved_at=t0)
    within = t0 + timedelta(seconds=DEFAULT_REAUTH_THRESHOLD_S - 1)
    beyond = t0 + timedelta(seconds=DEFAULT_REAUTH_THRESHOLD_S + 1)
    assert ctx.needs_reauth(within) is False
    assert ctx.needs_reauth(beyond) is True


# ── RunBudget ───────────────────────────────────────────────────
def test_budget_defaults_and_deadline():
    ctx = _ctx()
    assert ctx.budget.max_turns > 0 and ctx.budget.token_budget > 0
    assert ctx.budget.deadline is not None            # create 默认给 TTL deadline


def test_budget_is_past_deadline():
    now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
    b = RunBudget.with_ttl(now, ttl_s=60)
    assert b.is_past_deadline(now + timedelta(seconds=30)) is False
    assert b.is_past_deadline(now + timedelta(seconds=61)) is True


def test_budget_frozen():
    with pytest.raises(FrozenInstanceError):
        RunBudget().max_turns = 999


# ── 与 run_store 组合冒烟（ExecutionContext 满足 create_run 契约）──
def test_composes_with_run_store(monkeypatch):
    from tests.test_agent_runtime_run_store import _FakeConn
    state = {}
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _FakeConn(state))
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    ctx = _ctx(conversation_id="conv")
    run_id = RDSRunStore().create_run(ctx, "default")
    assert len(run_id) == 32
    ins = [s for s, _ in state["executed"] if "INSERT" in s and "agent_run" in s]
    assert ins and "'running'" in ins[0]
