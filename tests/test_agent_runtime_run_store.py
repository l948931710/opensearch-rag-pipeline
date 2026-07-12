# -*- coding: utf-8 -*-
"""
test_agent_runtime_run_store.py — RunStore RDS 状态机（Task 6）

mock 打 DB 层（patch db._get_db_conn）做**状态机单测**：不碰真库，验证 create_run/
append_step/save_checkpoint/load_latest_checkpoint 的 SQL 编排，以及 transition 的
FOR UPDATE + CAS 单向状态机（合法迁移 / CAS 竞态返 False / 非法 pair 抛 / 终态 set ended_at）。
"""
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.run_store import (
    AgentStep,
    InvalidTransition,
    RDSRunStore,
    RunCheckpoint,
)


# ── mock DB 层（沿用 test_queue_monitor 的 _Conn/_Cur 范式）──────────────────
class _FakeCursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.state.setdefault("executed", []).append((s, params))
        up = s.upper()
        if "FOR UPDATE" in up:
            cur = self.state.get("current_status")
            self._fetch = (cur,) if cur is not None else None
        elif "COALESCE(MAX(STEP_NO)" in up:
            self._fetch = (self.state.get("next_step_no", 1),)
        elif up.startswith("SELECT") and "AGENT_CHECKPOINT" in up:
            self._fetch = self.state.get("checkpoint_row")
        elif up.startswith("SELECT") and "TURNS_USED" in up:
            self._fetch = self.state.get("budget_row", (0, 0, 0))
        elif up.startswith("SELECT") and "AGENT_RUN" in up:
            self._fetch = self.state.get("run_row")
        elif up.startswith("UPDATE"):
            self.rowcount = self.state.get("update_rowcount", 1)
        else:
            self._fetch = None      # INSERT

    def fetchone(self):
        return self._fetch


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        self.state["commits"] = self.state.get("commits", 0) + 1

    def rollback(self):
        self.state["rollbacks"] = self.state.get("rollbacks", 0) + 1

    def close(self):
        self.state["closes"] = self.state.get("closes", 0) + 1


def _wire(monkeypatch, state):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _FakeConn(state))


def _ctx(**over):
    base = dict(thread_id="conv:staff", user_id="u1", channel="console",
                acl_groups=("g1", "g2"), conversation_id="conv",
                model_profile=None, prompt_version=None, git_sha=None)
    base.update(over)
    return SimpleNamespace(**base)


def _executed(state):
    return [s for s, _ in state.get("executed", [])]


# ── create_run / append_step / checkpoint ───────────────────────────────────
def test_create_run_inserts_running(monkeypatch):
    state = {}
    _wire(monkeypatch, state)
    run_id = RDSRunStore().create_run(_ctx(), "default")
    assert len(run_id) == 32 and all(c in "0123456789abcdef" for c in run_id)
    ins = [s for s in _executed(state) if "INSERT" in s and "agent_run" in s]
    assert ins and "'running'" in ins[0]
    assert state.get("commits") == 1 and state.get("closes") == 1


def test_append_step_returns_next_no(monkeypatch):
    state = {"next_step_no": 3}
    _wire(monkeypatch, state)
    n = RDSRunStore().append_step("rid", AgentStep(kind="tool_call", payload={"x": 1}))
    assert n == 3
    assert any("INSERT" in s and "agent_step" in s for s in _executed(state))


def test_append_step_rejects_bad_kind(monkeypatch):
    with pytest.raises(ValueError):
        RDSRunStore().append_step("rid", AgentStep(kind="bogus", payload={}))


def test_save_checkpoint_returns_id(monkeypatch):
    state = {}
    _wire(monkeypatch, state)
    cp = RDSRunStore().save_checkpoint("rid", b"blob-bytes", "digest64")
    assert len(cp) == 32
    assert any("agent_checkpoint" in s and "INSERT" in s for s in _executed(state))


def test_load_latest_checkpoint_row(monkeypatch):
    state = {"checkpoint_row": ("cp1", b"blob", "dig", "2026-07-07 12:00:00")}
    _wire(monkeypatch, state)
    cp = RDSRunStore().load_latest_checkpoint("rid")
    assert isinstance(cp, RunCheckpoint)
    assert cp.checkpoint_id == "cp1" and cp.state_blob == b"blob" and cp.state_digest == "dig"


def test_load_latest_checkpoint_none(monkeypatch):
    state = {"checkpoint_row": None}
    _wire(monkeypatch, state)
    assert RDSRunStore().load_latest_checkpoint("rid") is None


# ── transition 状态机（核心）────────────────────────────────────────────────
def test_transition_legal_cas_match(monkeypatch):
    state = {"current_status": "running", "update_rowcount": 1}
    _wire(monkeypatch, state)
    assert RDSRunStore().transition("rid", "running", "suspended") is True
    assert any(s.upper().startswith("UPDATE") for s in _executed(state))
    assert state.get("commits") == 1


def test_transition_cas_miss_returns_false(monkeypatch):
    state = {"current_status": "suspended"}      # 当前不是 running → CAS 失败
    _wire(monkeypatch, state)
    assert RDSRunStore().transition("rid", "running", "suspended") is False
    assert not any(s.upper().startswith("UPDATE") for s in _executed(state))   # 未 UPDATE
    assert state.get("rollbacks") == 1


def test_transition_run_not_found_returns_false(monkeypatch):
    state = {"current_status": None}             # SELECT FOR UPDATE 返回 None
    _wire(monkeypatch, state)
    assert RDSRunStore().transition("rid", "running", "suspended") is False


def test_transition_illegal_pair_raises(monkeypatch):
    # running → expired 非法（expired 只能从 suspended）；抛在任何 DB 访问之前
    with pytest.raises(InvalidTransition):
        RDSRunStore().transition("rid", "running", "expired")


def test_transition_from_terminal_raises(monkeypatch):
    with pytest.raises(InvalidTransition):
        RDSRunStore().transition("rid", "succeeded", "running")


def test_transition_terminal_sets_ended_at(monkeypatch):
    state = {"current_status": "running", "update_rowcount": 1}
    _wire(monkeypatch, state)
    assert RDSRunStore().transition("rid", "running", "succeeded") is True
    upd = [s for s in _executed(state) if s.upper().startswith("UPDATE")][0]
    assert "ended_at" in upd


@pytest.mark.parametrize("frm,to,legal", [
    ("running", "suspended", True), ("running", "succeeded", True),
    ("running", "failed", True), ("running", "cancelled", True),
    ("running", "expired", False), ("running", "running", False),
    # resume 两步：suspended→resuming（认领）→running（执行器接手）；不再 suspended→running 直达
    ("suspended", "resuming", True), ("suspended", "running", False),
    ("suspended", "cancelled", True), ("suspended", "expired", True),
    ("suspended", "failed", True), ("suspended", "succeeded", False),
    ("resuming", "running", True), ("resuming", "failed", True),
    # 回边 resuming→suspended：认领后失败（池满/checkpoint 解码）回滚认领，
    # 否则 run 永久钉死 resuming（/approve 只认 suspended → 409）——深度审查 B 组 P1
    ("resuming", "cancelled", True), ("resuming", "suspended", True),
    ("succeeded", "running", False), ("failed", "running", False),
    ("cancelled", "running", False), ("expired", "running", False),
])
def test_transition_table_locked(frm, to, legal, monkeypatch):
    if legal:
        state = {"current_status": frm, "update_rowcount": 1}
        _wire(monkeypatch, state)
        assert RDSRunStore().transition("rid", frm, to) is True
    else:
        with pytest.raises(InvalidTransition):
            RDSRunStore().transition("rid", frm, to)


def test_heartbeat_updates_active(monkeypatch):
    state = {}
    _wire(monkeypatch, state)
    RDSRunStore().heartbeat("rid")
    upd = [s for s in _executed(state) if s.upper().startswith("UPDATE")]
    assert upd and "heartbeat_at" in upd[0]
    assert "IN ('running','suspended','resuming')" in upd[0]   # resuming 也是活动态


# ── B8：预算消耗跟踪（跨 suspend/resume 持久）────────────────────────────────
def test_consume_budget_accumulates(monkeypatch):
    state = {"budget_row": (5, 3, 1200)}          # UPDATE 后 SELECT 回读的累计值
    _wire(monkeypatch, state)
    out = RDSRunStore().consume_budget("rid", turns=1, tool_calls=1, tokens=200)
    assert out == {"turns_used": 5, "tool_calls_used": 3, "tokens_used": 1200}
    upd = [s for s in _executed(state) if s.upper().startswith("UPDATE")][0]
    assert "turns_used=turns_used+" in upd and "tokens_used=tokens_used+" in upd
    assert state.get("commits") == 1


def test_consume_budget_missing_run_returns_zeros(monkeypatch):
    state = {"budget_row": None}
    _wire(monkeypatch, state)
    assert RDSRunStore().consume_budget("gone", tokens=10) == {
        "turns_used": 0, "tool_calls_used": 0, "tokens_used": 0}
