# -*- coding: utf-8 -*-
"""
test_agent_reaudit_fixes.py — 2026-07-11 重审计逐项修复的回归锁（§1 可靠性 / §2 数据
完整性 / §4 性能 / §5 UX / console 307）。

R1 完成侧 fencing · R2 后台心跳 · R3 drain 排水 · R4 运行中 checkpoint · R5 Redis 中继 ·
U1/U2 message_id 锚定与答案读回 · U3 /console 307 token→fragment · P1 title 批量 embedding。
"""
import json
import math
import threading
import time

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
from opensearch_pipeline.agent_runtime.events import (
    ModelDelta,
    RunCompleted,
    RunFailed,
)
from opensearch_pipeline.agent_runtime.executor import RunHandle, RunRejected, ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.loop import DefaultAgentLoop, ModelTurn, ProposedCall
from opensearch_pipeline.agent_runtime.tool import ToolResult


class _FakeStore:
    def __init__(self, deny_success=False):
        self.transitions = []
        self.budget = {"turns_used": 0, "tool_calls_used": 0, "tokens_used": 0}
        self.steps = []
        self.checkpoints = []            # (run_id, blob, digest)
        self.hb_count = 0
        self.deny_success = deny_success
        self._n = 0
        self._step_no = 0

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def append_step(self, run_id, step):
        self._step_no += 1
        self.steps.append((run_id, step.kind, step.payload))
        return self._step_no

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        if self.deny_success and (frm, to) == ("running", "succeeded"):
            return False                 # 模拟被 reaper 收尸/取消后完成迟到
        return True

    def consume_budget(self, run_id, *, turns=0, tool_calls=0, tokens=0):
        self.budget["turns_used"] += turns
        self.budget["tool_calls_used"] += tool_calls
        self.budget["tokens_used"] += tokens
        return dict(self.budget)

    def heartbeat(self, run_id):
        self.hb_count += 1

    def save_checkpoint(self, run_id, blob, digest):
        self.checkpoints.append((run_id, blob, digest))
        return f"cp{len(self.checkpoints)}"


def _ctx():
    return ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"],
                                   roles=["employee"], channel="console", thread_id="t",
                                   budget=RunBudget(max_turns=8))


def _scripted(turns):
    box = {"i": 0}

    def _fn(msgs, tools):
        t = turns[box["i"]]
        box["i"] += 1
        return t

    return _fn


def _tool_then_final():
    return DefaultAgentLoop(_scripted([
        ModelTurn(tool_calls=[ProposedCall(call_id="c1", tool_name="knowledge_search",
                                           arguments={"query": "x"})]),
        ModelTurn(text="答案"),
    ]))


# ── R1：完成侧 fencing（CAS 失败 → 结果作废，不再静默吞）─────────────────────────


def test_completion_cas_failure_suppresses_side_effects():
    """running→succeeded CAS 失败（被收尸/取消抢先）→ on_complete 不跑、不发 done、
    改发 RunFailed——修「答案落 qa_log 而 durable=failed」的分叉（重审计 §1 怀疑者 bug）。"""
    store = _FakeStore(deny_success=True)
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    completed = []
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [],
                       on_complete=lambda text, retrieved=None: completed.append(text))
    events = list(handle.events())
    ex.shutdown()
    assert completed == []                                   # 完成侧副作用被抑制
    assert not any(isinstance(e, RunCompleted) for e in events)
    assert any(isinstance(e, RunFailed) and "作废" in e.error for e in events)


def test_completion_cas_success_keeps_normal_path():
    store = _FakeStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    completed = []
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [],
                       on_complete=lambda text, retrieved=None: completed.append(text))
    events = list(handle.events())
    ex.shutdown()
    assert completed == ["答案"]
    assert any(isinstance(e, RunCompleted) for e in events)
    assert ("run1", "running", "succeeded") in store.transitions


# ── R2：后台心跳 ticker（长工具调用不再被 reaper 误杀）───────────────────────────


def test_background_heartbeat_ticks_during_long_tool_call(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_HEARTBEAT_INTERVAL_S", "0.05")
    store = _FakeStore()

    def slow_adjudicate(ctx, ev):
        time.sleep(0.4)                  # 模拟长工具调用（轮边界心跳打不进来的窗口）
        return ToolResult.text_ok("r")

    ex = ThreadedRunExecutor(store, slow_adjudicate, max_concurrent=2)
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [])
    list(handle.events())
    ex.shutdown()
    # 轮边界心跳只有 1 次（tool 轮）；ticker 0.05s 间隔在 0.4s 内至少再打 3 次
    assert store.hb_count >= 4


def test_heartbeat_ticker_disabled_by_nonpositive_interval(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_HEARTBEAT_INTERVAL_S", "0")
    store = _FakeStore()

    def slow_adjudicate(ctx, ev):
        time.sleep(0.2)
        return ToolResult.text_ok("r")

    ex = ThreadedRunExecutor(store, slow_adjudicate, max_concurrent=2)
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [])
    list(handle.events())
    ex.shutdown()
    assert store.hb_count == 1           # 只剩轮边界心跳（历史行为）


# ── R3：drain 排水（拒新 + 限时等 + 超时诚实标 failed）──────────────────────────


def test_drain_rejects_new_and_fails_stuck_runs():
    store = _FakeStore()
    gate = threading.Event()

    def blocking_adjudicate(ctx, ev):
        gate.wait(3)
        return ToolResult.text_ok("r")

    ex = ThreadedRunExecutor(store, blocking_adjudicate, max_concurrent=2)
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [])
    rep = ex.drain(timeout=0.15)
    assert rep["force_failed"] == 1                          # 超时 run 诚实标 failed
    assert ("run1", "running", "failed") in store.transitions
    with pytest.raises(RunRejected):                         # 排水中拒新
        ex.submit(_ctx(), _tool_then_final(), [], [])
    gate.set()
    handle.wait(2)                       # 线程收尾；完成侧 CAS 与 failed 标记的竞态由 R1 兜底


def test_drain_waits_for_fast_runs():
    store = _FakeStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [])
    assert handle.wait(2)
    rep = ex.drain(timeout=1.0)
    assert rep["force_failed"] == 0


# ── R4：运行中 checkpoint（flag 默认 off；开 → 轮边界持久化，不外发内部事件）──────


def test_midrun_checkpoint_persists_at_round_boundary(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_MIDRUN_CHECKPOINT", "true")
    store = _FakeStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [])
    events = list(handle.events())
    ex.shutdown()
    assert len(store.checkpoints) == 1
    _rid, blob, _digest = store.checkpoints[0]
    from opensearch_pipeline.agent_runtime.loop import decode_checkpoint_state
    state = decode_checkpoint_state(blob)
    assert state["pending_call"] is None                     # 非挂起快照
    assert any(m.get("role") == "tool" for m in state["messages"])   # 已含上一轮工具结果
    assert state["turn"] == 0                                # resume 语义 start_turn=turn+1
    assert not any(getattr(e, "type", "") == "run_checkpoint_ready" for e in events)


def test_midrun_checkpoint_off_by_default(monkeypatch):
    monkeypatch.delenv("RAG_AGENT_MIDRUN_CHECKPOINT", raising=False)
    store = _FakeStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [])
    list(handle.events())
    ex.shutdown()
    assert store.checkpoints == []


# ── R5：Redis Stream 事件中继（flag 默认 off）────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.streams = {}
        self.ttls = {}

    def xadd(self, key, fields, maxlen=None, approximate=None):
        entries = self.streams.setdefault(key, [])
        eid = f"{len(entries) + 1}-0"
        entries.append((eid, dict(fields)))
        return eid

    def expire(self, key, ttl):
        self.ttls[key] = ttl

    def exists(self, key):
        return 1 if key in self.streams else 0

    def xread(self, streams, count=None, block=None):
        out = []
        for key, last_id in streams.items():
            last = int(str(last_id).split("-")[0] or 0)
            fresh = [(eid, f) for eid, f in self.streams.get(key, [])
                     if int(eid.split("-")[0]) > last]
            if fresh:
                out.append((key, fresh))
        return out


@pytest.fixture
def relay_env(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setenv("RAG_AGENT_EVENT_RELAY", "redis")
    monkeypatch.setenv("RAG_REDIS_URL", "redis://unit-test:6379/0")
    # R3-P1 后 publish 默认异步（writer 线程）——本组断言流内容/顺序语义，钉回同步
    # 保确定性（异步性能/丢帧语义在 test_agent_r3_relay_reconcile 专测）
    monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "false")
    from opensearch_pipeline import redis_client
    monkeypatch.setattr(redis_client, "get_client", lambda: fake)
    return fake


def test_relay_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RAG_AGENT_EVENT_RELAY", raising=False)
    from opensearch_pipeline.agent_runtime.event_relay import attach_relay, relay_enabled
    assert relay_enabled() is False
    h = RunHandle("rid1")
    attach_relay(h)
    assert h._relay is None              # 关 = 进程内历史行为，零回归面


def test_relay_publishes_events_and_end_sentinel(relay_env):
    from opensearch_pipeline.agent_runtime.event_relay import attach_relay
    h = RunHandle("rid2")
    attach_relay(h)
    assert h._relay is not None
    h._emit(ModelDelta(text="你好"))
    h._emit(RunCompleted(final_text="答案", streamed=True))
    h._finish()
    key = [k for k in relay_env.streams if "rid2" in k][0]
    payloads = [json.loads(f["data"]) for _eid, f in relay_env.streams[key]]
    assert [p["type"] for p in payloads] == ["model_delta", "run_completed", "__end__"]
    assert payloads[1]["final_text"] == "答案"
    assert relay_env.ttls[key] > 0


def test_relay_stream_replay_stops_at_terminal(relay_env):
    from opensearch_pipeline.agent_runtime.event_relay import attach_relay, stream_run_events
    h = RunHandle("rid3")
    attach_relay(h)
    h._emit(ModelDelta(text="a"))
    h._emit(ModelDelta(text="b"))
    h._emit(RunFailed(error="boom"))
    h._finish()
    got = list(stream_run_events("rid3", block_ms=10, overall_timeout_s=2))
    assert [d["type"] for d in got] == ["model_delta", "model_delta", "run_failed"]


def test_relay_wired_through_executor_submit(relay_env):
    store = _FakeStore()
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("r"), max_concurrent=2)
    handle = ex.submit(_ctx(), _tool_then_final(), [{"role": "user", "content": "q"}], [])
    list(handle.events())
    ex.shutdown()
    key = [k for k in relay_env.streams if "run1" in k][0]
    types = [json.loads(f["data"])["type"] for _eid, f in relay_env.streams[key]]
    assert "tool_call_proposed" in types and "run_completed" in types
    assert types[-1] == "__end__"
    # artifacts（exclude=True）绝不进线协议
    for _eid, f in relay_env.streams[key]:
        assert "artifacts" not in json.loads(f["data"])


# ── U2：resume 复用原 message_id（反馈投票不悬空）────────────────────────────────


def test_resume_callbacks_reuse_run_message_id():
    import opensearch_pipeline.routes.agent as agent_route

    class _RS:
        def load_latest_checkpoint(self, run_id):
            return None

    run = {"run_id": "r1", "message_id": "mid-original", "conversation_id": None}
    mid, _remember, _fail, _persist = agent_route._resume_callbacks(_RS(), run, "t1", "u1", ["g"])
    assert mid == "mid-original"


def test_resume_callbacks_fallback_for_legacy_rows():
    import opensearch_pipeline.routes.agent as agent_route

    class _RS:
        def load_latest_checkpoint(self, run_id):
            return None

    run = {"run_id": "r1", "message_id": None, "conversation_id": None}
    mid, _remember, _fail, _persist = agent_route._resume_callbacks(_RS(), run, "t1", "u1", ["g"])
    assert mid                                              # 036 前历史行：回退新生成


# ── U1：run 详情带回最终答案（qa_session_log 经 agent_run.message_id 锚定）────────


class _DetailStore:
    def get_run(self, run_id):
        if run_id != "r1":
            return None
        return {"run_id": "r1", "status": "succeeded", "user_id": "u1", "channel": "console",
                "agent_profile": "default", "started_at": None, "ended_at": None,
                "thread_id": "t1", "conversation_id": None, "model_profile": "light",
                "message_id": "mid-1"}

    def list_steps(self, run_id, **kw):
        return []

    def list_invocations(self, **kw):
        return []


class _NoApprovalStore:
    def get_latest_by_run(self, run_id):
        return None


@pytest.fixture
def detail_wired(monkeypatch):
    import opensearch_pipeline.routes.agent as agent_route
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setattr(agent_route, "_enforce_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (None, None, None, _DetailStore()))
    monkeypatch.setattr(agent_route, "_get_approval_store", lambda: _NoApprovalStore())
    import opensearch_pipeline.qa_logger as ql
    monkeypatch.setattr(ql, "fetch_answer_by_message_id",
                        lambda mid: {"message_id": mid, "answer_text": "最终答案全文",
                                     "answered_at": "2026-07-11 10:00:00"}
                        if mid == "mid-1" else None)
    yield


def _detail_client():
    from fastapi.testclient import TestClient

    from opensearch_pipeline import api
    from opensearch_pipeline.api import Identity, current_identity
    api.app.dependency_overrides[current_identity] = lambda: Identity(
        user_id="u1", acl_groups=["production"], role="employee")
    return TestClient(api.app)


def test_run_detail_returns_final_answer(detail_wired):
    from opensearch_pipeline import api
    try:
        body = _detail_client().get("/api/agent/runs/r1").json()
        assert body["final"] == {"message_id": "mid-1", "answer_text": "最终答案全文",
                                 "answered_at": "2026-07-11 10:00:00"}
    finally:
        api.app.dependency_overrides.clear()


def test_run_detail_final_none_when_qa_row_missing(detail_wired, monkeypatch):
    """留存期外/历史行：读回 None（fail-open），详情其余字段照常。"""
    import opensearch_pipeline.qa_logger as ql
    monkeypatch.setattr(ql, "fetch_answer_by_message_id", lambda mid: None)
    from opensearch_pipeline import api
    try:
        body = _detail_client().get("/api/agent/runs/r1").json()
        assert body["final"] is None
        assert body["run"]["run_id"] == "r1"
    finally:
        api.app.dependency_overrides.clear()


# ── U3：/console 307 token 不再进 Location query ─────────────────────────────────


def test_console_redirect_moves_token_to_fragment():
    from fastapi.testclient import TestClient

    from opensearch_pipeline import api
    c = TestClient(api.app)
    r = c.get("/console?token=SECRET123&doc_id=D1", follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc == "/console/?doc_id=D1#token=SECRET123"
    assert "token=SECRET123" not in loc.split("#")[0]        # query 段零 token


def test_console_redirect_without_token_unchanged():
    from fastapi.testclient import TestClient

    from opensearch_pipeline import api
    c = TestClient(api.app)
    r = c.get("/console?doc_id=D2", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/console/?doc_id=D2"
    r2 = c.get("/console", follow_redirects=False)
    assert r2.headers["location"] == "/console/"


# ── P1（重审计 §4）：title 向量批量 warmup + 持久缓存 + 单飞 ─────────────────────


class _VecStore:
    """最小 ontology store 桩：N 个 product 对象，无 active 别名。"""

    def __init__(self, n):
        self._objs = [{"object_id": f"o{i}", "object_type": "product",
                       "canonical_ref": f"P-{i:03d}", "title": f"测试品名{i}",
                       "owner_dept": "pmc", "data_classification": "internal",
                       "status": "active"} for i in range(n)]

    def get_active_identifier(self, ns, norm):
        return None

    def get_object(self, oid):
        return None

    def find_objects(self, otype, *, title_like=None, status="active", limit=50):
        return list(self._objs[:limit]) if otype == "product" else []


def _hash_vec(text):
    import hashlib as _h
    return [b / 255.0 for b in _h.sha256(text.encode("utf-8")).digest()[:16]]


@pytest.fixture
def batch_embed_env(monkeypatch):
    """默认 embedder + 计数版 embed_texts_native + 关持久层（隔离测内存/批量行为）。"""
    from opensearch_pipeline.ontology.resolve import OntologyResolver
    calls = {"batch": [], "single": 0}

    def fake_query_embed(text, **kw):
        calls["single"] += 1
        return _hash_vec(text), [], []

    def fake_batch(texts, **kw):
        calls["batch"].append(list(texts))
        return [(_hash_vec(t), [], []) for t in texts]

    import opensearch_pipeline.embedding_client as ec
    import opensearch_pipeline.retriever as rt
    monkeypatch.setattr(rt, "get_query_embedding", fake_query_embed)
    monkeypatch.setattr(ec, "embed_texts_native", fake_batch)
    monkeypatch.setattr(OntologyResolver, "_persistent_cache", staticmethod(lambda: None))
    return calls


def test_title_vectors_batched_not_per_object(batch_embed_env):
    """重审计 §4：25 对象 → 批量 API ceil(25/batch_size) 次，而非 25 次单发。"""
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.ontology.resolve import OntologyResolver
    resolver = OntologyResolver(_VecStore(25))
    resolver.resolve("u8", "龙虾杯询码", object_type_hint="product")
    bs = max(1, int(get_config().embedding.batch_size or 10))
    assert len(batch_embed_env["batch"]) == math.ceil(25 / bs)
    assert sum(len(b) for b in batch_embed_env["batch"]) == 25
    assert batch_embed_env["single"] == 1                    # 仅 query 本身单发一次


def test_title_vectors_memory_cache_hits_second_resolve(batch_embed_env):
    from opensearch_pipeline.ontology.resolve import OntologyResolver
    resolver = OntologyResolver(_VecStore(12))
    resolver.resolve("u8", "第一问", object_type_hint="product")
    n_after_first = len(batch_embed_env["batch"])
    resolver.resolve("u8", "第二问", object_type_hint="product")
    assert len(batch_embed_env["batch"]) == n_after_first    # 标题向量零新批次


def test_title_vectors_persistent_cache_cross_instance(monkeypatch):
    """持久层命中：新 resolver 实例（冷内存）从 KV 取回向量，零 embedding 调用。"""
    from opensearch_pipeline.ontology.resolve import OntologyResolver
    calls = {"batch": 0}
    kv = {}

    class _FakeKV:
        def get_many(self, keys):
            return {k: kv[k] for k in keys if k in kv}

        def put_many(self, mapping):
            kv.update(mapping)

    def fake_query_embed(text, **kw):
        return _hash_vec(text), [], []

    def fake_batch(texts, **kw):
        calls["batch"] += 1
        return [(_hash_vec(t), [], []) for t in texts]

    import opensearch_pipeline.embedding_client as ec
    import opensearch_pipeline.retriever as rt
    monkeypatch.setattr(rt, "get_query_embedding", fake_query_embed)
    monkeypatch.setattr(ec, "embed_texts_native", fake_batch)
    monkeypatch.setattr(OntologyResolver, "_persistent_cache", staticmethod(lambda: _FakeKV()))

    OntologyResolver(_VecStore(8)).resolve("u8", "问一", object_type_hint="product")
    assert calls["batch"] >= 1 and kv                        # 首访批量算 + 写持久层
    n = calls["batch"]
    OntologyResolver(_VecStore(8)).resolve("u8", "问二", object_type_hint="product")
    assert calls["batch"] == n                               # 冷实例全部持久层命中


def test_injected_embedder_keeps_per_title_contract(monkeypatch):
    """注入单文本 embedder（非默认）→ 不走批量/持久层，逐条契约不变。"""
    import opensearch_pipeline.embedding_client as ec
    from opensearch_pipeline.ontology.resolve import OntologyResolver
    boom = {"n": 0}

    def _no_batch(*a, **k):
        boom["n"] += 1
        raise AssertionError("注入 embedder 不应触发批量路径")

    monkeypatch.setattr(ec, "embed_texts_native", _no_batch)
    counted = {"n": 0}

    def _embed(text):
        counted["n"] += 1
        return _hash_vec(text)

    resolver = OntologyResolver(_VecStore(5), embedder=_embed)
    res = resolver.resolve("u8", "询码X", object_type_hint="product")
    assert boom["n"] == 0
    assert counted["n"] == 6                                 # query + 5 标题（历史行为）
    assert res.status in ("candidate", "unresolved")
