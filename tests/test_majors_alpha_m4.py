# -*- coding: utf-8 -*-
"""Majors α3(M4)ask 业务域幂等(2026-07-21 codex 共识)。

锁死的不变量:
- flag off(默认)→ client_request_id 字段被忽略,行为 byte-identical(SSE 照常);
- flag on:回放查询位于 admission **之前**(命中不计 admission、不建 run);
  非终态回放 202 / 终态 200(JSON,replayed=true);同键异题 → 409;
- 首问:client_request_id+question_digest 随 ctx 进 create_run(原子落库);
- ThreadBusy 撞出时带键先回读——同键同题=自己已建的 run,回放而非 409;
- run_store:create_run 落两列;1062(限定 uk_run_client_req)→ ClientRequestBound;
  1054(点名列族)→ warn-once 无键回退;find_run_by_client_request 行映射。
真库并发双 POST 单赢家见 test_majors_alpha_m4_db.py。
"""
import hashlib

import pytest
from fastapi.testclient import TestClient

import opensearch_pipeline.routes.agent as agent_route
from opensearch_pipeline import api
from opensearch_pipeline.agent_runtime import (
    ChatResponse,
    ModelGateway,
    ThreadedRunExecutor,
    Usage,
    default_policy_engine,
    make_adjudicator,
)
from opensearch_pipeline.agent_runtime import run_store as run_store_mod
from opensearch_pipeline.agent_runtime.run_store import (
    ClientRequestBound,
    RDSRunStore,
    ThreadBusy,
)
from opensearch_pipeline.agent_tools import build_default_registry
from opensearch_pipeline.api import Identity, current_identity


def _digest(q: str) -> str:
    return hashlib.sha256(q.encode("utf-8")).hexdigest()


class _Provider:
    name = "dashscope"

    def capabilities(self, m):
        return None

    def chat(self, model, req):
        return ChatResponse(text="答案", tool_calls=[],
                            usage=Usage(tokens_prompt=1, tokens_completion=1), model=model)


class _Store:
    def __init__(self):
        self._n = 0
        self._step = 0
        self.created_ctx = []
        self.find_calls = []
        self.prior = None                 # find_run_by_client_request 的桩返回
        self.create_exc = None

    def create_run(self, ctx, profile):
        if self.create_exc is not None:
            raise self.create_exc
        self.created_ctx.append(ctx)
        self._n += 1
        return f"run{self._n}"

    def find_run_by_client_request(self, user_id, client_request_id):
        self.find_calls.append((user_id, client_request_id))
        return self.prior

    def transition(self, run_id, frm, to):
        return True

    def consume_budget(self, run_id, **k):
        return {}

    def append_step(self, run_id, step):
        self._step += 1
        return self._step

    def record_invocation(self, run_id, step_no, **kw):
        return f"inv{step_no}"

    def finish_invocation(self, invocation_id, **kw):
        pass

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return None

    def get_run(self, run_id):
        return None


def _identity():
    return Identity(user_id="u1", acl_groups=["production"], role="employee")


@pytest.fixture
def wired(monkeypatch):
    registry = build_default_registry()
    store = _Store()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _Provider()},
                           routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (registry, gateway, executor, store))
    yield store
    executor.shutdown()
    api.app.dependency_overrides.clear()


def _client(identity):
    api.app.dependency_overrides[current_identity] = lambda: identity
    return TestClient(api.app)


Q = "富岭包装规范?"


def test_flag_off_field_ignored(monkeypatch, wired):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.delenv("RAG_AGENT_ASK_IDEM_ENABLE", raising=False)
    r = _client(_identity()).post("/api/agent/ask",
                                  json={"question": Q, "client_request_id": "k1"})
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
    assert wired.find_calls == []                      # 无回放查询
    assert getattr(wired.created_ctx[0], "client_request_id", None) is None   # ctx 不带键


def test_flag_on_first_ask_stamps_ctx(monkeypatch, wired):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setenv("RAG_AGENT_ASK_IDEM_ENABLE", "true")
    r = _client(_identity()).post("/api/agent/ask",
                                  json={"question": Q, "client_request_id": "k1"})
    assert r.status_code == 200
    assert wired.find_calls == [("u1", "k1")]
    ctx = wired.created_ctx[0]
    assert getattr(ctx, "client_request_id") == "k1"
    assert getattr(ctx, "question_digest") == _digest(Q)


def test_replay_running_202_and_skips_admission(monkeypatch, wired):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setenv("RAG_AGENT_ASK_IDEM_ENABLE", "true")
    wired.prior = {"run_id": "r-prior", "status": "running",
                   "question_digest": _digest(Q), "message_id": "m1", "thread_id": "t1"}
    admits = []
    monkeypatch.setattr(agent_route, "_enforce_rate_limit",
                        lambda *a, **k: admits.append(1))
    r = _client(_identity()).post("/api/agent/ask",
                                  json={"question": Q, "client_request_id": "k1"})
    assert r.status_code == 202
    j = r.json()
    assert j["replayed"] is True and j["run_id"] == "r-prior" and j["status"] == "running"
    assert admits == []                                # 回放不计 admission
    assert wired.created_ctx == []                     # 不建第二个 run


def test_replay_terminal_200(monkeypatch, wired):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setenv("RAG_AGENT_ASK_IDEM_ENABLE", "true")
    wired.prior = {"run_id": "r-prior", "status": "succeeded",
                   "question_digest": _digest(Q), "message_id": "m1", "thread_id": "t1"}
    r = _client(_identity()).post("/api/agent/ask",
                                  json={"question": Q, "client_request_id": "k1"})
    assert r.status_code == 200 and r.json()["status"] == "succeeded"


def test_replay_digest_mismatch_409(monkeypatch, wired):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setenv("RAG_AGENT_ASK_IDEM_ENABLE", "true")
    wired.prior = {"run_id": "r-prior", "status": "running",
                   "question_digest": _digest("另一个问题"), "message_id": "m1",
                   "thread_id": "t1"}
    r = _client(_identity()).post("/api/agent/ask",
                                  json={"question": Q, "client_request_id": "k1"})
    assert r.status_code == 409 and "不同问题" in r.json()["detail"]


def test_thread_busy_with_key_replays(monkeypatch, wired):
    """uk_thread_active 先报的重试形态:同键同题 → 回放,非 409。"""
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setenv("RAG_AGENT_ASK_IDEM_ENABLE", "true")
    wired.create_exc = ThreadBusy("busy")

    calls = {"n": 0}
    _orig = wired.find_run_by_client_request

    def _find_second_time(user_id, key):
        # 首次(回放预检)未命中;create_run 撞 ThreadBusy 后第二次命中自己的 run
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return {"run_id": "r-mine", "status": "running",
                "question_digest": _digest(Q), "message_id": "m1", "thread_id": "t1"}

    wired.find_run_by_client_request = _find_second_time
    r = _client(_identity()).post("/api/agent/ask",
                                  json={"question": Q, "client_request_id": "k1"})
    assert r.status_code == 202 and r.json()["run_id"] == "r-mine"
    wired.find_run_by_client_request = _orig


def test_thread_busy_without_key_keeps_409(monkeypatch, wired):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setenv("RAG_AGENT_ASK_IDEM_ENABLE", "true")
    wired.create_exc = ThreadBusy("busy")
    r = _client(_identity()).post("/api/agent/ask", json={"question": Q})
    assert r.status_code == 409 and "回答在进行中" in r.json()["detail"]


# ══ run_store 单元(fake conn)═══════════════════════════════════════════════


class _Cur:
    def __init__(self, exc_when=None, fetch=None):
        self.calls = []
        self._exc_when = exc_when or (lambda sql, n: None)
        self._fetch = list(fetch or [])

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        exc = self._exc_when(sql, len(self.calls))
        if exc is not None:
            raise exc

    def fetchone(self):
        return self._fetch.pop(0) if self._fetch else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def begin(self):
        pass

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class _Ctx:
    thread_id = "t1"
    user_id = "u1"
    channel = "console"
    acl_groups = ["production"]


def _mk_store(monkeypatch, conns):
    st = RDSRunStore()
    pool = list(conns)
    monkeypatch.setattr(st, "_conn", lambda: pool.pop(0))
    monkeypatch.setattr(run_store_mod, "_op_db", lambda: "op")
    monkeypatch.setattr(run_store_mod, "_WARNED_054_MISSING", False)
    return st


def _ctx_with_key():
    ctx = _Ctx()
    ctx.client_request_id = "k1"
    ctx.question_digest = _digest(Q)
    return ctx


def test_create_run_includes_client_columns(monkeypatch):
    cur = _Cur()
    st = _mk_store(monkeypatch, [_Conn(cur)])
    st.create_run(_ctx_with_key(), "default")
    ins = [(s, a) for s, a in cur.calls if "INSERT" in s]
    assert len(ins) == 1
    assert "client_request_id" in ins[0][0] and "question_digest" in ins[0][0]
    assert "k1" in ins[0][1] and _digest(Q) in ins[0][1]


def test_create_run_client_dup_raises_client_request_bound(monkeypatch):
    dup = Exception(1062, "Duplicate entry 'u1-k1' for key 'agent_run.uk_run_client_req'")
    cur = _Cur(exc_when=lambda sql, n: dup if "INSERT" in sql else None)
    st = _mk_store(monkeypatch, [_Conn(cur)])
    with pytest.raises(ClientRequestBound):
        st.create_run(_ctx_with_key(), "default")


def test_create_run_1054_falls_back_keyless(monkeypatch):
    miss = Exception(1054, "Unknown column 'client_request_id' in 'field list'")
    cur = _Cur(exc_when=lambda sql, n: miss
               if ("INSERT" in sql and "client_request_id" in sql) else None)
    st = _mk_store(monkeypatch, [_Conn(cur)])
    st.create_run(_ctx_with_key(), "default")
    assert run_store_mod._WARNED_054_MISSING is True
    ins = [s for s, _ in cur.calls if "INSERT" in s]
    assert len(ins) == 2 and "client_request_id" not in ins[1]


def test_find_run_by_client_request_maps_row(monkeypatch):
    cur = _Cur(fetch=[("r1", "running", _digest(Q), "m1", "t1")])
    st = _mk_store(monkeypatch, [_Conn(cur)])
    row = st.find_run_by_client_request("u1", "k1")
    assert row == {"run_id": "r1", "status": "running", "question_digest": _digest(Q),
                   "message_id": "m1", "thread_id": "t1"}
