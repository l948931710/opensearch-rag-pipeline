# -*- coding: utf-8 -*-
"""
test_agent_pr3_dispatch.py — PR-3 Stage A（durable dispatch：outbox+lease+恢复扫描）

设计 docs/pr3_durable_worker_design_2026-07-17_DRAFT.md 的验收门：
- outbox 存储事务形态（enqueue 单条自成事务；claim_next=SKIP LOCKED+_begin 钉连接；
  holder CAS）；
- dispatcher 恢复语义：已绑 run 的过期命令收口 done **绝不重执行**（at-most-once）；
  未绑命令重驱（at-least-once）；DispatchRetryLater 留 claimed；payload 坏/不可重建
  收口 failed；attempts 封顶；
- 路由 flag on/off 等价：off 零 outbox 交互；on 时 SSE 契约不变 + 命令 enqueue→
  claimed→bind→done 全链；受理面故障回退直通（可用性优先）；
- 恢复重驱身份**现解**（铁律 5）：异常=DispatchRetryLater，绝不用提交时快照。
"""
import threading
import time
from types import SimpleNamespace

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
from opensearch_pipeline.agent_runtime.durable_dispatcher import (
    DispatchRetryLater,
    DurableDispatcher,
)
from opensearch_pipeline.agent_tools import build_default_registry
from opensearch_pipeline.api import Identity, current_identity


# ── in-memory outbox（与 RDSDispatchOutbox 同契约）───────────────────────────


class MemOutbox:
    def __init__(self):
        self.rows = {}
        self._n = 0

    def enqueue(self, *, thread_id, user_id, channel, payload, kind="submit"):
        self._n += 1
        cid = f"cmd{self._n}"
        self.rows[cid] = {"command_id": cid, "kind": kind, "status": "queued",
                          "thread_id": thread_id, "user_id": user_id, "channel": channel,
                          "payload": payload, "run_id": None, "attempts": 0,
                          "lease_holder": None, "lease_expires_at": None, "last_error": None}
        return cid

    def claim_specific(self, command_id, *, holder, lease_s):
        r = self.rows.get(command_id)
        if not r or r["status"] != "queued":
            return False
        r.update(status="claimed", lease_holder=holder,
                 lease_expires_at=time.time() + lease_s)
        r["attempts"] += 1
        return True

    def claim_next(self, *, holder, lease_s, max_attempts=3, min_age_s=0):
        # min_age_s（批次4 认领宽限）：内存桩不建模 created_at，签名兼容即可——
        # 宽限的 SQL 语义由 RDS 实现的专项测试覆盖。
        now = time.time()
        for r in self.rows.values():
            expired = (r["status"] == "claimed"
                       and (r["lease_expires_at"] or 0) < now)
            # kind-aware（Stage B）：已绑 run 的 submit 绝不重认领；resume 照常
            if r["run_id"] is not None and r["kind"] == "submit":
                continue
            if (r["status"] == "queued" or expired) and r["attempts"] < max_attempts:
                r.update(status="claimed", lease_holder=holder,
                         lease_expires_at=now + lease_s)
                r["attempts"] += 1
                return dict(r)
        return None

    def renew_lease(self, command_id, *, holder, lease_s):
        r = self.rows.get(command_id)
        if r and r["status"] == "claimed" and r["lease_holder"] == holder:
            r["lease_expires_at"] = time.time() + lease_s
            return True
        return False

    def bind_run(self, command_id, run_id, *, holder):
        r = self.rows.get(command_id)
        if r and r["status"] == "claimed" and r["lease_holder"] == holder:
            r["run_id"] = run_id
            return True
        return False

    def complete(self, command_id, *, status, holder=None, error=None):
        r = self.rows.get(command_id)
        if not r or r["status"] != "claimed":
            return False
        if holder is not None and r["lease_holder"] != holder:
            return False
        r.update(status=status, last_error=error)
        return True

    def sweep_exhausted(self, *, max_attempts=3):
        now = time.time()
        n = 0
        for r in self.rows.values():
            expired = (r["status"] == "claimed"
                       and (r["lease_expires_at"] or 0) < now)
            if (r["run_id"] is None and r["attempts"] >= max_attempts
                    and (r["status"] == "queued" or expired)):
                r.update(status="failed", last_error="重驱次数封顶（attempts 上限）")
                n += 1
        return n

    def list_bound_expired(self, limit=20):
        now = time.time()
        return [dict(r) for r in self.rows.values()
                if r["status"] == "claimed" and r["run_id"] is not None
                and r["kind"] == "submit"                  # Stage B：resume 命令排除
                and (r["lease_expires_at"] or 0) < now][:limit]

    def backlog_count(self):
        now = time.time()
        return sum(1 for r in self.rows.values()
                   if r["status"] == "queued"
                   or (r["status"] == "claimed" and (r["lease_expires_at"] or 0) < now))


# ── outbox RDS 存储：事务形态（fake conn）────────────────────────────────────


class _Cur:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 1
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.sqls.append(s)
        if s.upper().startswith("SELECT") and "agent_dispatch_command" in s:
            self._row = self._conn.select_row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class _Conn:
    def __init__(self, select_row=None):
        self.sqls = []
        self.begun = 0
        self.committed = 0
        self.rolled_back = 0
        self.select_row = select_row

    def begin(self):
        self.begun += 1

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


def test_p3_outbox_enqueue_single_insert(monkeypatch):
    from opensearch_pipeline.agent_runtime.dispatch_outbox import RDSDispatchOutbox

    conn = _Conn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    cid = RDSDispatchOutbox().enqueue(thread_id="t", user_id="u", channel="console",
                                      payload={"question": "q"})
    assert cid and conn.committed == 1
    assert any(s.upper().startswith("INSERT INTO") for s in conn.sqls)


def test_p3_outbox_claim_next_skip_locked_and_begin(monkeypatch):
    from opensearch_pipeline.agent_runtime.dispatch_outbox import RDSDispatchOutbox

    row = ("cmd1", "submit", "queued", "t", "u", "console", '{"question":"q"}',
           None, 0, None, None, None)
    conn = _Conn(select_row=row)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    cmd = RDSDispatchOutbox().claim_next(holder="h1", lease_s=60)
    assert cmd and cmd["command_id"] == "cmd1" and cmd["payload"] == {"question": "q"}
    assert conn.begun == 1                                  # _begin 钉连接（多语句事务）
    assert any("FOR UPDATE SKIP LOCKED" in s for s in conn.sqls)
    assert conn.committed == 1


def test_p3_outbox_complete_holder_cas(monkeypatch):
    from opensearch_pipeline.agent_runtime.dispatch_outbox import RDSDispatchOutbox

    conn = _Conn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    assert RDSDispatchOutbox().complete("cmd1", status="done", holder="h1") is True
    assert any("lease_holder=%s" in s for s in conn.sqls)   # CAS on holder
    with pytest.raises(ValueError):
        RDSDispatchOutbox().complete("cmd1", status="running")   # 非法收口词


# ── dispatcher 恢复语义（MemOutbox）─────────────────────────────────────────


def _mk(outbox, recover_fn, **kw):
    return DurableDispatcher(outbox, recover_fn, holder="h-test", lease_s=60, **kw)


def test_p3_fastpath_lifecycle():
    ob = MemOutbox()
    d = _mk(ob, lambda cmd: None)
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    assert d.claim_for_request(cid)
    d.bind_and_done(cid, "run1")
    r = ob.rows[cid]
    assert r["status"] == "done" and r["run_id"] == "run1"


def test_p3_bound_expired_closes_without_reexecution():
    """已绑 run 的过期命令 → 收口 done，recover_fn **绝不**被调（at-most-once）。"""
    ob = MemOutbox()
    calls = []
    d = _mk(ob, lambda cmd: calls.append(cmd) or "runX")
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    ob.claim_specific(cid, holder="dead-worker", lease_s=60)
    ob.rows[cid]["run_id"] = "run1"
    ob.rows[cid]["lease_expires_at"] = time.time() - 10     # 持有者已死
    rep = d.tick()
    assert ob.rows[cid]["status"] == "done"
    assert calls == []                                       # 绝不重执行
    assert rep["closed"] == 1 and rep["redriven"] == 0


def test_p3_unbound_queued_redriven():
    """未绑 run 的命令 → recover_fn 重建执行 → bind+done（at-least-once）。"""
    ob = MemOutbox()
    d = _mk(ob, lambda cmd: "run9")
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    rep = d.tick()
    r = ob.rows[cid]
    assert r["status"] == "done" and r["run_id"] == "run9" and rep["redriven"] == 1


def test_p3_retry_later_stays_claimed():
    ob = MemOutbox()

    def _busy(cmd):
        raise DispatchRetryLater("executor 满")

    d = _mk(ob, _busy)
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    d.tick()
    r = ob.rows[cid]
    assert r["status"] == "claimed" and r["run_id"] is None   # 留 claimed 等租约到期
    assert r["attempts"] == 1


def test_p3_unrecoverable_fails():
    ob = MemOutbox()

    def _boom(cmd):
        raise ValueError("payload 语义坏")

    d = _mk(ob, _boom)
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    d.tick()
    assert ob.rows[cid]["status"] == "failed"
    assert "重建执行失败" in ob.rows[cid]["last_error"]


def test_p3_broken_payload_fails_without_recover():
    ob = MemOutbox()
    calls = []
    d = _mk(ob, lambda cmd: calls.append(cmd) or "runX")
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    ob.rows[cid]["payload"] = None                           # 载荷坏行
    d.tick()
    assert ob.rows[cid]["status"] == "failed" and calls == []


def test_p3_attempts_capped_to_failed():
    ob = MemOutbox()
    d = _mk(ob, lambda cmd: (_ for _ in ()).throw(DispatchRetryLater("永远忙")),
            max_attempts=2)
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    for _ in range(3):                                       # 每轮租约立刻过期以便重认领
        d.tick()
        ob.rows[cid]["lease_expires_at"] = time.time() - 1
    d.tick()
    r = ob.rows[cid]
    assert r["status"] == "failed" and "封顶" in r["last_error"]
    assert r["attempts"] == 2                                # 恰好封顶，不再无限重驱


# ── 路由集成（flag on/off）──────────────────────────────────────────────────


class _CaptureStore:
    def __init__(self):
        self._n = 0
        self.transitions = []

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        return True

    def consume_budget(self, run_id, **k):
        return {}

    def append_step(self, run_id, step):
        return 1

    def record_invocation(self, run_id, step_no, **kw):
        return "inv1"

    def finish_invocation(self, invocation_id, **kw):
        pass

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return None


class _NoToolProvider:
    name = "dashscope"

    def capabilities(self, m):
        return None

    def chat(self, model, req):
        return ChatResponse(text="直答。", tool_calls=[],
                            usage=Usage(tokens_prompt=3, tokens_completion=2), model=model)


@pytest.fixture
def pr3_wired(monkeypatch):
    registry = build_default_registry()
    store = _CaptureStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _NoToolProvider()},
                           routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (registry, gateway, executor, store))
    ob = MemOutbox()
    monkeypatch.setattr(agent_route, "_get_dispatch_outbox", lambda: ob)
    monkeypatch.setattr(agent_route, "_DISPATCHER", None)    # 每测新建（绑定本 ob）
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    # 关投机检索：本组测命令生命周期/命名空间，投机预取的共享 _SPEC_POOL 背景线程
    # 会越过测试边界污染其它测的 _RETRIEVE 计数（与本组无关，显式关掉）
    monkeypatch.setenv("RAG_AGENT_SPEC_RETRIEVAL", "false")
    yield ob, store
    executor.shutdown()


def _ask(payload):
    api.app.dependency_overrides[current_identity] = lambda: Identity(
        user_id="u1", acl_groups=["g"], role="employee")
    return TestClient(api.app).post("/api/agent/ask", json=payload)


def test_p3_flag_off_zero_outbox_interaction(pr3_wired, monkeypatch):
    ob, _ = pr3_wired
    monkeypatch.delenv("RAG_AGENT_DURABLE_DISPATCH", raising=False)
    try:
        r = _ask({"question": "问题", "session_id": "s1"})
        assert r.status_code == 200 and '"type": "done"' in r.text
        assert ob.rows == {}                                 # off=字节级旧路径
    finally:
        api.app.dependency_overrides.clear()


def test_p3_flag_on_full_command_lifecycle(pr3_wired, monkeypatch):
    ob, store = pr3_wired
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")
    try:
        r = _ask({"question": "问题", "session_id": "s1"})
        assert r.status_code == 200 and '"type": "done"' in r.text   # SSE 契约不变
        assert len(ob.rows) == 1
        cmd = list(ob.rows.values())[0]
        assert cmd["status"] == "done" and cmd["run_id"] == "run1"
        assert cmd["payload"]["question"] == "问题"
        assert cmd["payload"]["message_id"]                  # 恢复锚点随命令 durable
        assert cmd["thread_id"] == "sid:u1:s1"               # P1-11 命名空间键
    finally:
        api.app.dependency_overrides.clear()


def test_b1_enqueue_failure_fail_closed_503(pr3_wired, monkeypatch):
    """B1 P1-01（生产级外审 2026-07-17，行为更替改判）：flag 开启时 enqueue 失败=受理
    失败——503+Retry-After，不再回退直通（旧行为=零 durable 痕迹，进程死亡问题蒸发；
    对齐 dispatch_outbox「enqueue 失败=受理失败（fail-closed）」模块契约）。"""
    ob, store = pr3_wired
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")
    monkeypatch.delenv("RAG_AGENT_DISPATCH_ACCEPT_FAILOPEN", raising=False)

    class _BrokenOutbox(MemOutbox):
        def enqueue(self, **kw):
            raise RuntimeError("043 未 apply")

    monkeypatch.setattr(agent_route, "_get_dispatch_outbox", lambda: _BrokenOutbox())
    monkeypatch.setattr(agent_route, "_DISPATCHER", None)
    try:
        r = _ask({"question": "问题", "session_id": "s1"})
        assert r.status_code == 503
        assert r.headers.get("retry-after") == "5"
        assert store.transitions == []                       # 未直通执行：零 run
    finally:
        api.app.dependency_overrides.clear()


def test_b1_enqueue_failure_failopen_escape_restores_direct(pr3_wired, monkeypatch):
    """B1 P1-01 逃生口：RAG_AGENT_DISPATCH_ACCEPT_FAILOPEN=true 还原旧「回退直通」
    行为（043 未 apply 的过渡窗口专用）。"""
    ob, store = pr3_wired
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")
    monkeypatch.setenv("RAG_AGENT_DISPATCH_ACCEPT_FAILOPEN", "true")

    class _BrokenOutbox(MemOutbox):
        def enqueue(self, **kw):
            raise RuntimeError("043 未 apply")

    monkeypatch.setattr(agent_route, "_get_dispatch_outbox", lambda: _BrokenOutbox())
    monkeypatch.setattr(agent_route, "_DISPATCHER", None)
    try:
        r = _ask({"question": "问题", "session_id": "s1"})
        assert r.status_code == 200 and '"type": "done"' in r.text   # 旧可用性优先行为
        assert ("run1", "running", "succeeded") in store.transitions
    finally:
        api.app.dependency_overrides.clear()


def test_b1_claim_exception_after_enqueue_returns_409(pr3_wired, monkeypatch):
    """B1 P1-01：enqueue 已 durable、claim 中途异常——命令在案（queued）交恢复扫描
    重驱，本方 409 不直通（直通=双执行双答案）。"""
    ob, store = pr3_wired
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")
    monkeypatch.delenv("RAG_AGENT_DISPATCH_ACCEPT_FAILOPEN", raising=False)

    class _ClaimBoomOutbox(MemOutbox):
        def claim_specific(self, command_id, *, holder, lease_s):
            raise RuntimeError("DB timeout during claim")

    boom = _ClaimBoomOutbox()
    monkeypatch.setattr(agent_route, "_get_dispatch_outbox", lambda: boom)
    monkeypatch.setattr(agent_route, "_DISPATCHER", None)
    try:
        r = _ask({"question": "问题", "session_id": "s1"})
        assert r.status_code == 409
        assert store.transitions == []                       # 未直通执行
        assert len(boom.rows) == 1                           # 命令已受理在案
        assert list(boom.rows.values())[0]["status"] == "queued"   # 恢复扫描可重驱
    finally:
        api.app.dependency_overrides.clear()


# ── 恢复重驱（_dispatch_recovered_submit）───────────────────────────────────


def _cmd(question="问题", thread_id="sid:u1:s9", user_id="u1", message_id="m-77"):
    return {"command_id": "cmd1", "kind": "submit", "thread_id": thread_id,
            "user_id": user_id, "channel": "console",
            "payload": {"question": question, "thinking": False,
                        "conversation_id": None, "message_id": message_id,
                        "request_id": "rid1"}}


class _MsgCaptureStore(_CaptureStore):
    def __init__(self):
        super().__init__()
        self.ctx_seen = []

    def create_run(self, ctx, profile):
        self.ctx_seen.append(ctx)
        return super().create_run(ctx, profile)


@pytest.fixture
def recover_wired(monkeypatch):
    registry = build_default_registry()
    store = _MsgCaptureStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _NoToolProvider()},
                           routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (registry, gateway, executor, store))
    import opensearch_pipeline.dingtalk_identity as di
    monkeypatch.setattr(di, "_resolve_user_identity", lambda uid: {"dept": ["production"]})
    monkeypatch.setattr(di, "resolve_kb_identity",
                        lambda uid: SimpleNamespace(role="employee"))
    yield store
    executor.shutdown()


def test_p3_recover_rebuilds_ctx_with_live_acl(recover_wired):
    run_id = agent_route._dispatch_recovered_submit(_cmd())
    assert run_id == "run1"
    ctx = recover_wired.ctx_seen[0]
    assert ctx.user_id == "u1" and "production" in list(ctx.acl_groups)   # 现解 ACL
    assert ctx.thread_id == "sid:u1:s9"
    assert getattr(ctx, "message_id", None) == "m-77"        # P1-12 锚点沿 payload
    deadline = time.monotonic() + 5                          # 等驱动收尾（无 SSE 消费者）
    while (time.monotonic() < deadline
           and ("run1", "running", "succeeded") not in recover_wired.transitions):
        time.sleep(0.02)
    assert ("run1", "running", "succeeded") in recover_wired.transitions


def test_p3_recover_identity_blip_retry_later(recover_wired, monkeypatch):
    import opensearch_pipeline.dingtalk_identity as di
    monkeypatch.setattr(di, "_resolve_user_identity",
                        lambda uid: (_ for _ in ()).throw(RuntimeError("dingtalk down")))
    with pytest.raises(DispatchRetryLater):
        agent_route._dispatch_recovered_submit(_cmd())


def test_p3_recover_capacity_retry_later(recover_wired, monkeypatch):
    registry, gateway, executor, store = agent_route._get_runtime()
    gate = threading.Event()
    ex_full = ThreadedRunExecutor(store, lambda c, e: gate.wait(2), max_concurrent=1)
    ex_full._active = 1                                      # 模拟满载（_acquire 即拒）
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (registry, gateway, ex_full, store))
    with pytest.raises(DispatchRetryLater, match="执行器暂不可用"):
        agent_route._dispatch_recovered_submit(_cmd())
    ex_full._active = 0
    ex_full.shutdown()


def test_p3_recover_missing_question_unrecoverable(recover_wired):
    cmd = _cmd()
    cmd["payload"]["question"] = ""
    with pytest.raises(ValueError, match="question"):
        agent_route._dispatch_recovered_submit(cmd)


# ══════════════════════════════════════════════════════════════════════════
# Stage B：resume 命令入 outbox
# ══════════════════════════════════════════════════════════════════════════


def test_p3b_claim_next_skips_bound_submit_takes_resume():
    """kind-aware 认领（顺修 Stage A 缺口）：已绑 run 的 submit 绝不重认领（at-most-once）；
    resume 命令 run_id 恒有值仍照常认领（重驱幂等）。"""
    ob = MemOutbox()
    # 已绑 run 的 submit（模拟 bound-expired 漏网）
    s = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    ob.claim_specific(s, holder="dead", lease_s=60)
    ob.rows[s]["run_id"] = "run-s"
    ob.rows[s]["lease_expires_at"] = time.time() - 10
    # resume 命令（run_id 恒有值）
    r = ob.enqueue(thread_id="t", user_id="u", channel="console",
                   payload={"decision": "approved", "request_id": "req1"})
    ob.rows[r]["run_id"] = "run-r"
    ob.rows[r]["kind"] = "resume"
    claimed = ob.claim_next(holder="h", lease_s=60)
    assert claimed and claimed["command_id"] == r          # 跳过已绑 submit，认领 resume


def test_p3b_recover_resume_suspended_redrives(monkeypatch):
    """恢复重驱 resume 命令：run 仍 suspended → 走 _redrive_resume_run 返回 run_id。"""
    called = {}
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (None, None, None, SimpleNamespace(
                            get_run=lambda rid: {"run_id": rid, "status": "suspended",
                                                 "thread_id": "t", "user_id": "u"})))
    monkeypatch.setattr(agent_route, "_get_approval_store", lambda: object())
    monkeypatch.setattr(agent_route, "_redrive_resume_run",
                        lambda *a, **k: called.setdefault("kind", a[1]) or "ok")
    cmd = {"command_id": "c1", "kind": "resume", "run_id": "run9",
           "payload": {"decision": "approved", "request_id": "req1"}}
    assert agent_route._dispatch_recovered_resume(cmd) == "run9"
    assert called["kind"] == "approved"


def test_p3b_recover_resume_terminal_closes_done(monkeypatch):
    """run 已终态 → 返回 run_id 让 dispatcher 收口 done（幂等，不重驱）。"""
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (None, None, None, SimpleNamespace(
                            get_run=lambda rid: {"run_id": rid, "status": "succeeded"})))
    cmd = {"command_id": "c1", "kind": "resume", "run_id": "run9",
           "payload": {"decision": "approved", "request_id": "req1"}}
    assert agent_route._dispatch_recovered_resume(cmd) == "run9"


def test_p3b_recover_resume_running_retry_later(monkeypatch):
    """run 已被认领在跑（running/resuming）→ DispatchRetryLater（等其收尾）。"""
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (None, None, None, SimpleNamespace(
                            get_run=lambda rid: {"run_id": rid, "status": "running"})))
    cmd = {"command_id": "c1", "kind": "resume", "run_id": "run9",
           "payload": {"decision": "approved", "request_id": "req1"}}
    with pytest.raises(DispatchRetryLater):
        agent_route._dispatch_recovered_resume(cmd)


def test_p3b_recover_resume_pool_full_retry_later(monkeypatch):
    """_redrive_resume_run 返回 retry（池满/身份瞬断）→ DispatchRetryLater。"""
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (None, None, None, SimpleNamespace(
                            get_run=lambda rid: {"run_id": rid, "status": "suspended",
                                                 "thread_id": "t", "user_id": "u"})))
    monkeypatch.setattr(agent_route, "_get_approval_store", lambda: object())
    monkeypatch.setattr(agent_route, "_redrive_resume_run", lambda *a, **k: "retry")
    cmd = {"command_id": "c1", "kind": "resume", "run_id": "run9",
           "payload": {"decision": "approved", "request_id": "req1"}}
    with pytest.raises(DispatchRetryLater):
        agent_route._dispatch_recovered_resume(cmd)


def test_p3b_dispatch_recover_routes_by_kind(monkeypatch):
    """_dispatch_recover 按 kind 分流：submit→submit 恢复，resume→resume 恢复。"""
    seen = []
    monkeypatch.setattr(agent_route, "_dispatch_recovered_submit",
                        lambda cmd: seen.append("submit") or "rs")
    monkeypatch.setattr(agent_route, "_dispatch_recovered_resume",
                        lambda cmd: seen.append("resume") or "rr")
    assert agent_route._dispatch_recover({"kind": "submit"}) == "rs"
    assert agent_route._dispatch_recover({"kind": "resume"}) == "rr"
    assert agent_route._dispatch_recover({}) == "rs"        # 缺 kind 默认 submit
    assert seen == ["submit", "resume", "submit"]


def test_p3b_insert_command_tx_shape(monkeypatch):
    """insert_command_tx：单条 INSERT，run_id 随命令写（resume 命令 enqueue 即绑 run）。"""
    from opensearch_pipeline.agent_runtime.dispatch_outbox import insert_command_tx

    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split())
            captured["params"] = params

    cid = insert_command_tx(_Cur(), command_id="c1", kind="resume", thread_id="t",
                            user_id="u", channel="console",
                            payload={"decision": "approved"}, run_id="run9")
    assert cid == "c1"
    assert "INSERT INTO" in captured["sql"] and "agent_dispatch_command" in captured["sql"]
    assert "c1" in captured["params"] and "run9" in captured["params"]
    assert "resume" in captured["params"]


def test_p3b_decide_writes_resume_command_in_tx(monkeypatch):
    """approval_store.decide 的 outbox_writer 在决定同事务被调（写 resume 命令）。"""
    from opensearch_pipeline.agent_runtime.approval_store import RDSApprovalStore

    class _DecCur:
        def __init__(self, conn):
            self._conn = conn
            self.rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            self._conn.sqls.append(s)
            if "FROM" in s and "approval_request" in s and "FOR UPDATE" in s:
                self._conn.row = ("pending", "d0", 0)

        def fetchone(self):
            return self._conn.row

    class _DecConn:
        def __init__(self):
            self.sqls = []
            self.row = None
            self.committed = 0

        def begin(self):
            pass

        def cursor(self):
            return _DecCur(self)

        def commit(self):
            self.committed += 1

        def rollback(self):
            pass

        def close(self):
            pass

    conn = _DecConn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    calls = []
    RDSApprovalStore().decide("req1", decision="approved", decided_by="admin",
                              outbox_writer=lambda cur: calls.append(cur))
    assert calls and conn.committed == 1                   # outbox_writer 在 commit 前被调


def test_p3b_close_done_claims_then_completes():
    """close_done（resume 命令 fast-path 收口）：queued → claimed → done。"""
    ob = MemOutbox()
    d = _mk(ob, lambda cmd: None)
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console",
                     payload={"decision": "approved"})
    ob.rows[cid]["run_id"] = "run9"                        # resume 命令 enqueue 即绑 run
    d.close_done(cid)
    assert ob.rows[cid]["status"] == "done"

# ── 批次4（ultra 评审）：认领竞态/CAS 静默/resume 僵尸 ─────────────────────────


def test_b4_bind_cas_miss_skips_complete_and_stays_loud(caplog):
    """批次4（ultra durable_dispatcher:73）：租约被他方偷走后 bind CAS 落空——响亮 ERROR
    + 跳过 complete（命令归窃取方收敛），绝不静默把他人持有的命令越权收口 done。"""
    import logging

    ob = MemOutbox()
    d = _mk(ob, lambda cmd: None)
    cid = ob.enqueue(thread_id="t", user_id="u", channel="console", payload={"question": "q"})
    assert d.claim_for_request(cid)
    ob.rows[cid]["lease_holder"] = "thief"          # 模拟他方偷租约（dispatch 超时未续）
    with caplog.at_level(logging.ERROR):
        d.bind_and_done(cid, "runX")                # 不抛
    assert ob.rows[cid]["status"] == "claimed"      # 未被越权收口 done
    assert ob.rows[cid]["run_id"] is None           # bind 未落
    assert any("bind CAS 落空" in r.message for r in caplog.records)


def test_b4_tick_closes_exhausted_resume_zombies():
    """批次4（ultra dispatch_outbox:242）：tick 接线 sweep_exhausted_resumes——
    attempts 封顶的 resume 命令按 run 现状收口，计数进返回值（观测面）。"""
    class _Outbox(MemOutbox):
        def __init__(self):
            super().__init__()
            self.swept_with = None

        def sweep_exhausted_resumes(self, *, max_attempts=3):
            self.swept_with = max_attempts
            return 2

    ob = _Outbox()
    d = _mk(ob, lambda cmd: None, max_attempts=5)
    rep = d.tick()
    assert ob.swept_with == 5
    assert rep["resume_closed"] == 2


def test_b4_claim_next_receives_min_age_grace(monkeypatch):
    """批次4（ultra dispatch_outbox:134）：扫描认领必须带最小年龄宽限——挡住
    「fast-path enqueue 与 claim_specific 之间毫秒窗被扫描抢单 → 双跑」的竞态。"""
    seen = {}

    class _Outbox(MemOutbox):
        def claim_next(self, *, holder, lease_s, max_attempts=3, min_age_s=0):
            seen["min_age_s"] = min_age_s
            return None

    monkeypatch.setenv("RAG_AGENT_DISPATCH_CLAIM_GRACE_S", "45")
    d = DurableDispatcher(_Outbox(), lambda cmd: None, holder="h-test", lease_s=60)
    d.tick()
    assert seen["min_age_s"] == 45


def test_b4_rds_claim_next_sql_carries_age_grace(monkeypatch):
    """RDS 实现的 SQL 面：min_age_s>0 时 queued 臂带 created_at 宽限谓词；=0 字节级旧 SQL。"""
    from opensearch_pipeline.agent_runtime.dispatch_outbox import RDSDispatchOutbox

    conn = _Conn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    RDSDispatchOutbox().claim_next(holder="h", lease_s=60, min_age_s=30)
    sel = [s for s in conn.sqls if s.strip().upper().startswith("SELECT")][0]
    assert "created_at < DATE_SUB(NOW(3), INTERVAL %s SECOND)" in sel
    conn2 = _Conn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn2)
    RDSDispatchOutbox().claim_next(holder="h", lease_s=60)
    sel2 = [s for s in conn2.sqls if s.strip().upper().startswith("SELECT")][0]
    assert "DATE_SUB" not in sel2 and "status='queued'" in sel2


def test_b4_rds_sweep_exhausted_resumes_sql_shape(monkeypatch):
    """RDS 实现的 SQL 面：resume 僵尸收口按 run 现状分派 failed/done，LEFT JOIN 兜住
    run 行已清除的孤儿。"""
    from opensearch_pipeline.agent_runtime.dispatch_outbox import RDSDispatchOutbox

    conn = _Conn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    RDSDispatchOutbox().sweep_exhausted_resumes(max_attempts=3)
    sql = " ".join(" ".join(s.split()) for s in conn.sqls)
    assert "kind='resume'" in sql and "attempts >= %s" in sql
    assert "LEFT JOIN" in sql and "r.status='suspended'" in sql


def test_b4_lost_claim_does_not_direct_dispatch(pr3_wired, monkeypatch):
    """批次4（ultra dispatch_outbox:134 后半）：认领被他方抢走后**不再直通执行**——
    此前直通 + 抢单方稍后重驱 = 同一问题双 run 双答案双计费；现在 409 受理提示
    （恰一次执行归命令持有方），零 run 创建。"""
    _ob, store = pr3_wired
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")

    class _StolenOutbox(MemOutbox):
        def claim_specific(self, command_id, *, holder, lease_s):
            return False        # 他方已抢先持有

    monkeypatch.setattr(agent_route, "_get_dispatch_outbox", lambda: _StolenOutbox())
    monkeypatch.setattr(agent_route, "_DISPATCHER", None)
    try:
        r = _ask({"question": "问题", "session_id": "s1"})
        assert r.status_code == 409
        assert store.transitions == []               # 未直通执行：零状态迁移零 run
    finally:
        api.app.dependency_overrides.clear()
