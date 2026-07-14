# -*- coding: utf-8 -*-
"""
test_agent_cancel_and_relay_continuity.py — 复核批次2（B3 挂起-续跑中继断流 + A5 服务端 cancel）

B3：RunHandle._finish 只在真终态写中继 __end__；挂起段与续跑段共用同一 run_id 流，
    suspend→approve→resume→replay 全帧必须经 /events 可达（此前挂起点写 __end__，
    回放在审批处永久收流，续跑段含最终答案帧不可达——复核新发现）。
A5：POST /api/agent/runs/{id}/cancel 协作式取消——轮边界收口 cancelled 终态、槽位释放、
    中继收到终态帧；门禁同 run 详情（本人/kb_admin，他人 404）；suspended/终态 409；
    句柄不在本实例 501。SSE 断连不自动 cancel，只记 _client_disconnected_at。

复用 test_agent_reaudit_fixes 的 _FakeRedis/relay_env/_FakeStore/_ctx 桩（同一 mock 语义）。
"""
import json
import threading
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.approval import Approved, RejectedTerminate
from opensearch_pipeline.agent_runtime.events import (
    RunCompleted,
    RunFailed,
    RunSuspended,
)
from opensearch_pipeline.agent_runtime.executor import RunHandle, ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop, ModelTurn, ProposedCall
from opensearch_pipeline.agent_runtime.tool import ToolResult
from tests.test_agent_reaudit_fixes import _FakeRedis, _FakeStore, _ctx  # 桩复用（同一 mock 语义）


@pytest.fixture
def relay_env(monkeypatch):
    """与 test_agent_reaudit_fixes.relay_env 同构（本地定义避免导入 fixture 的 F811 遮蔽）。"""
    fake = _FakeRedis()
    monkeypatch.setenv("RAG_AGENT_EVENT_RELAY", "redis")
    monkeypatch.setenv("RAG_REDIS_URL", "redis://unit-test:6379/0")
    from opensearch_pipeline import redis_client
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)
    return fake


class _CheckpointStore(_FakeStore):
    """_FakeStore + resume 所需的 checkpoint 读回。"""

    def load_latest_checkpoint(self, run_id):
        if not self.checkpoints:
            return None
        _rid, blob, digest = self.checkpoints[-1]
        return SimpleNamespace(state_blob=blob, state_digest=digest,
                               checkpoint_id=f"cp{len(self.checkpoints)}")


class _StubLoop:
    """直接产喂事件序列的 loop 桩（绕开 adjudicate/model_fn，聚焦 driver 收尾语义）。"""

    def __init__(self, run_events=None, resume_events=None):
        self._run = list(run_events or [])
        self._resume = list(resume_events or [])

    def run(self, ctx, messages, tools):
        yield from self._run

    def resume(self, ctx, state, outcome, tools):
        yield from self._resume


def _suspend_ev():
    return RunSuspended(
        pending_call={"call_id": "c1", "tool_name": "danger_tool", "arguments": {"a": 1}},
        turn_index=0,
        state_messages=[{"role": "user", "content": "q"}],
    )


def _relay_types(fake_redis, run_id):
    key = [k for k in fake_redis.streams if run_id in k]
    if not key:
        return []
    return [json.loads(f["data"])["type"] for _eid, f in fake_redis.streams[key[0]]]


# ═══════════════════════════════════════════════════════════════
# B3：挂起不写 __end__，续跑段经同一流可达
# ═══════════════════════════════════════════════════════════════


def test_suspend_does_not_write_end_sentinel(relay_env, monkeypatch):
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_HMAC_KEY", raising=False)
    store = _CheckpointStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.submit(_ctx(), _StubLoop(run_events=[_suspend_ev()]),
                       [{"role": "user", "content": "q"}], [])
    evs = list(handle.events())          # 本地队列照常收流（原 /ask SSE 在挂起帧后结束）
    assert any(isinstance(e, RunSuspended) for e in evs)
    types = _relay_types(relay_env, handle.run_id)
    assert "run_suspended" in types
    assert "__end__" not in types        # B3 核心：挂起不是终态，不封流
    ex.shutdown()


def test_suspend_resume_replay_reaches_all_frames(relay_env, monkeypatch):
    """台账验收用例：FakeRedis 下 suspend→approve→resume→replay 全帧可达。"""
    from opensearch_pipeline.agent_runtime.event_relay import stream_run_events
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_HMAC_KEY", raising=False)
    store = _CheckpointStore()
    loop = _StubLoop(run_events=[_suspend_ev()],
                     resume_events=[RunCompleted(final_text="最终答案", streamed=True)])
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.submit(_ctx(), loop, [{"role": "user", "content": "q"}], [])
    list(handle.events())
    run_id = handle.run_id

    h2 = ex.resume(run_id, _ctx(), Approved(), loop, [])
    list(h2.events())
    ex.shutdown()

    raw = _relay_types(relay_env, run_id)
    assert raw == ["run_suspended", "run_completed", "__end__"]   # 单流全帧 + 终态才封流
    replay = [d["type"] for d in stream_run_events(run_id, block_ms=10, overall_timeout_s=2)]
    assert replay == ["run_suspended", "run_completed"]           # 回放穿过挂起帧直达答案帧


def test_rejected_terminate_terminal_frame_reaches_relay(relay_env, monkeypatch):
    """顺手修：拒绝终止的裸 handle 也挂中继——跨实例回放能看到「已被拒绝」终态。"""
    from opensearch_pipeline.agent_runtime.event_relay import stream_run_events
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_HMAC_KEY", raising=False)
    store = _CheckpointStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.submit(_ctx(), _StubLoop(run_events=[_suspend_ev()]),
                       [{"role": "user", "content": "q"}], [])
    list(handle.events())
    run_id = handle.run_id

    h2 = ex.resume(run_id, _ctx(), RejectedTerminate(), _StubLoop(), [])
    evs = list(h2.events())
    ex.shutdown()
    assert any(isinstance(e, RunFailed) for e in evs)
    assert ("run1", "resuming", "cancelled") in store.transitions
    raw = _relay_types(relay_env, run_id)
    assert raw == ["run_suspended", "run_failed", "__end__"]
    replay = [d["type"] for d in stream_run_events(run_id, block_ms=10, overall_timeout_s=2)]
    assert replay == ["run_suspended", "run_failed"]


# ═══════════════════════════════════════════════════════════════
# A5：协作式 cancel（executor 层语义）
# ═══════════════════════════════════════════════════════════════


def test_cancel_collapses_run_at_turn_boundary(relay_env):
    """cancel 后：run 落 cancelled 终态、槽位释放、中继收到终态帧（台账验收）。"""
    gate = threading.Event()

    def _fn(msgs, tools):
        gate.wait(5)                      # 卡住首轮，窗口内置 cancel 旗标 → 确定性命中检查点
        return ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                                  arguments={"query": "x"})])

    store = _FakeStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.submit(_ctx(), DefaultAgentLoop(_fn), [{"role": "user", "content": "q"}], [])
    assert ex.get_live_handle(handle.run_id) is handle
    handle.request_cancel()
    gate.set()
    assert handle.wait(5)
    evs = list(handle.events())
    assert any(isinstance(e, RunFailed) and "取消" in e.error for e in evs)
    assert ("run1", "running", "cancelled") in store.transitions
    assert ex.get_live_handle(handle.run_id) is None      # 句柄已摘除
    assert ex.active_count() == 0                          # 槽位释放
    types = _relay_types(relay_env, handle.run_id)
    assert types[-2:] == ["run_failed", "__end__"]         # 终态帧到中继 + 正常封流
    ex.shutdown()


# ═══════════════════════════════════════════════════════════════
# A5：cancel 路由（门禁 + 语义分支）
# ═══════════════════════════════════════════════════════════════


class _CancelRunStore:
    def __init__(self, status="running", user_id="u1"):
        self.status, self.user_id = status, user_id

    def get_run(self, run_id):
        if run_id != "r1":
            return None
        return {"run_id": "r1", "status": self.status, "user_id": self.user_id,
                "thread_id": "t1"}


class _CancelExecutor:
    def __init__(self, handle=None):
        self._h = handle

    def get_live_handle(self, run_id):
        return self._h


@pytest.fixture
def cancel_wired(monkeypatch):
    import opensearch_pipeline.routes.agent as agent_route
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setattr(agent_route, "_enforce_rate_limit", lambda *a, **k: None)

    def _wire(status="running", user_id="u1", handle=None):
        monkeypatch.setattr(
            agent_route, "_get_runtime",
            lambda: (None, None, _CancelExecutor(handle), _CancelRunStore(status, user_id)))

    yield _wire


def _client(user_id="u1", role="employee"):
    from fastapi.testclient import TestClient

    from opensearch_pipeline import api
    from opensearch_pipeline.api import Identity, current_identity
    api.app.dependency_overrides[current_identity] = lambda: Identity(
        user_id=user_id, acl_groups=["production"], role=role)
    return TestClient(api.app)


def _post_cancel(**client_kw):
    from opensearch_pipeline import api
    try:
        return _client(**client_kw).post("/api/agent/runs/r1/cancel")
    finally:
        api.app.dependency_overrides.clear()


def test_cancel_route_owner_running_202(cancel_wired):
    h = RunHandle("r1")
    cancel_wired(status="running", handle=h)
    resp = _post_cancel()
    assert resp.status_code == 202
    assert resp.json()["status"] == "cancel_requested"
    assert h.cancelled() is True                     # 旗标已置，轮边界生效


def test_cancel_route_other_user_404(cancel_wired, monkeypatch):
    from opensearch_pipeline import dingtalk_identity
    monkeypatch.setattr(dingtalk_identity, "resolve_kb_identity",
                        lambda uid: SimpleNamespace(role="employee"))
    h = RunHandle("r1")
    cancel_wired(status="running", user_id="别人", handle=h)
    resp = _post_cancel()
    assert resp.status_code == 404                   # 不可见==不存在
    assert h.cancelled() is False


def test_cancel_route_suspended_409(cancel_wired):
    cancel_wired(status="suspended", handle=RunHandle("r1"))
    resp = _post_cancel()
    assert resp.status_code == 409
    assert "审批" in resp.json()["detail"]


def test_cancel_route_terminal_409(cancel_wired):
    cancel_wired(status="succeeded", handle=RunHandle("r1"))
    assert _post_cancel().status_code == 409


def test_cancel_route_no_local_handle_501(cancel_wired):
    cancel_wired(status="running", handle=None)
    resp = _post_cancel()
    assert resp.status_code == 501                   # 跨实例取消留 v2


def test_sse_disconnect_records_time_not_cancel():
    """断连策略：GeneratorExit 只记 _client_disconnected_at，不置 cancel 旗标。"""
    import opensearch_pipeline.routes.agent as agent_route
    h = RunHandle("r9")
    gen = agent_route._stream_events(h, "s1", "m1")
    next(gen)                                        # 吐出 session 帧后挂在 events() 上
    gen.close()                                      # 模拟客户端断连（GeneratorExit）
    assert h.cancelled() is False
    assert getattr(h, "_client_disconnected_at", None) is not None
