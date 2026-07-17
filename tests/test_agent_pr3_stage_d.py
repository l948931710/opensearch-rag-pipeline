# -*- coding: utf-8 -*-
"""
test_agent_pr3_stage_d.py — PR-3 Stage D（多副本形态：durable cancel + distributed
admission + 独立 worker + 拓扑守卫合流）

验收门（设计 docs/pr3_durable_worker_design_2026-07-17_DRAFT.md §3 Stage D）：
- durable cancel（schema/047）：request_cancel CAS（仅 running/resuming、仅首标）；
  心跳 ticker 的 _poll_durable_cancel 拾取→进程内协作旗标（fail-open 三态）；
  cancel 端点：本地句柄=202+标记同落；跨实例=标记+202；store 无标记方法=501 兜底；
  真库：标记往返/二次请求幂等 False/终态不可标；
- distributed admission：_global_admission_full 软上限语义（off/满/未满/旧 store/
  读失败 fail-open）；恢复重驱在闸满时 DispatchRetryLater（留 claimed 自然退避）；
- 独立 worker：flag 前置 fail-fast；装配抑制 web daemon 双跑；
- 拓扑守卫（P1-10 合流）：多副本 + agent 开 ⇒ RAG_AGENT_DURABLE_DISPATCH 必开。
"""
import threading
import uuid
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.executor import RunHandle, ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
from tests.test_config_loading import _fresh_load


# ── run_store SQL 面（伪连接，形态对齐 test_agent_pr3_operation_ledger）──────────


class _FakeCursor:
    def __init__(self, log, *, rowcount=1, fetchone=None):
        self._log = log
        self.rowcount = rowcount
        self._fetchone = fetchone

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetchone

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log, **cur_kw):
        self._log = log
        self._cur_kw = cur_kw

    def cursor(self):
        return _FakeCursor(self._log, **self._cur_kw)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _patched_store(monkeypatch, log, **cur_kw):
    store = RDSRunStore()
    monkeypatch.setattr(store, "_conn", lambda: _FakeConn(log, **cur_kw))
    return store


def test_request_cancel_cas_sql_shape(monkeypatch):
    log = []
    store = _patched_store(monkeypatch, log, rowcount=1)
    assert store.request_cancel("r1", requested_by="u1") is True
    sql, params = log[0]
    # CAS 三谓词：活动态、仅首标；发起人入列
    assert "status IN ('running','resuming')" in sql
    assert "cancel_requested_at IS NULL" in sql
    assert "cancel_requested_by" in sql and params == ("u1", "r1")


def test_request_cancel_cas_miss_returns_false(monkeypatch):
    store = _patched_store(monkeypatch, [], rowcount=0)
    assert store.request_cancel("r1", requested_by="u1") is False


def test_cancel_requested_read(monkeypatch):
    assert _patched_store(monkeypatch, [], fetchone=("2026-07-17",)).cancel_requested("r1")
    assert not _patched_store(monkeypatch, [], fetchone=(None,)).cancel_requested("r1")
    assert not _patched_store(monkeypatch, [], fetchone=None).cancel_requested("r1")


def test_count_active_runs(monkeypatch):
    log = []
    store = _patched_store(monkeypatch, log, fetchone=(7,))
    assert store.count_active_runs() == 7
    sql, _ = log[0]
    assert "status IN ('running','resuming')" in sql and "COUNT(*)" in sql


# ── ticker 拾取（_poll_durable_cancel 三态）──────────────────────────────────────


def _executor_with_store(store):
    ex = ThreadedRunExecutor.__new__(ThreadedRunExecutor)   # 只测 _poll_durable_cancel
    ex._store = store
    return ex


def test_poll_durable_cancel_hits_flag():
    ex = _executor_with_store(SimpleNamespace(cancel_requested=lambda rid: True))
    h, seen = RunHandle("r1"), threading.Event()
    ex._poll_durable_cancel("r1", h, seen)
    assert h.cancelled() is True and seen.is_set()


def test_poll_durable_cancel_no_flag_noop():
    ex = _executor_with_store(SimpleNamespace(cancel_requested=lambda rid: False))
    h, seen = RunHandle("r1"), threading.Event()
    ex._poll_durable_cancel("r1", h, seen)
    assert h.cancelled() is False and not seen.is_set()


def test_poll_durable_cancel_fails_open():
    def _boom(rid):
        raise RuntimeError("db down")

    ex = _executor_with_store(SimpleNamespace(cancel_requested=_boom))
    h, seen = RunHandle("r1"), threading.Event()
    ex._poll_durable_cancel("r1", h, seen)              # 不抛
    assert h.cancelled() is False and not seen.is_set()
    # 旧桩无方法：同样 no-op
    ex2 = _executor_with_store(SimpleNamespace())
    ex2._poll_durable_cancel("r1", h, seen)
    assert h.cancelled() is False


# ── cancel 端点（Stage D 分支；501 兜底已在 test_agent_cancel_and_relay_continuity）──


class _DurableCancelStore:
    def __init__(self, status="running", user_id="u1", mark_ok=True):
        self.status, self.user_id, self.mark_ok = status, user_id, mark_ok
        self.marked = []

    def get_run(self, run_id):
        if run_id != "r1":
            return None
        return {"run_id": "r1", "status": self.status, "user_id": self.user_id,
                "thread_id": "t1"}

    def request_cancel(self, run_id, *, requested_by=""):
        self.marked.append((run_id, requested_by))
        return self.mark_ok


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

    def _wire(store, handle=None):
        monkeypatch.setattr(
            agent_route, "_get_runtime",
            lambda: (None, None, _CancelExecutor(handle), store))
        return store

    yield _wire


def _post_cancel(user_id="u1"):
    from fastapi.testclient import TestClient

    from opensearch_pipeline import api
    from opensearch_pipeline.api import Identity, current_identity
    api.app.dependency_overrides[current_identity] = lambda: Identity(
        user_id=user_id, acl_groups=["production"], role="employee")
    try:
        return TestClient(api.app).post("/api/agent/runs/r1/cancel")
    finally:
        api.app.dependency_overrides.clear()


def test_cancel_cross_instance_marks_durable_202(cancel_wired):
    store = cancel_wired(_DurableCancelStore(status="running"), handle=None)
    resp = _post_cancel()
    assert resp.status_code == 202
    assert store.marked == [("r1", "u1")]
    assert "心跳" in resp.json()["note"]


def test_cancel_resuming_marks_durable_202(cancel_wired):
    """Stage D 前 resuming=409 稍后重试；现在 durable 标记覆盖恢复认领窗口。"""
    store = cancel_wired(_DurableCancelStore(status="resuming"), handle=None)
    assert _post_cancel().status_code == 202
    assert store.marked == [("r1", "u1")]


def test_cancel_cas_miss_terminal_409(cancel_wired):
    """标记 CAS 未中且 run 已进终态（并发窗口）→ 409 如实。"""
    store = _DurableCancelStore(status="running", mark_ok=False)

    def _flip_terminal(run_id):
        row = {"run_id": "r1", "status": "succeeded", "user_id": "u1", "thread_id": "t1"}
        return row if store.marked else {**row, "status": "running"}

    store.get_run = _flip_terminal                      # 标记后再读=终态
    cancel_wired(store, handle=None)
    assert _post_cancel().status_code == 409


def test_cancel_cas_miss_already_requested_202(cancel_wired):
    """标记 CAS 未中但 run 仍活（=已有在途取消请求）→ 幂等 202。"""
    cancel_wired(_DurableCancelStore(status="running", mark_ok=False), handle=None)
    assert _post_cancel().status_code == 202


def test_cancel_local_handle_also_marks_durable(cancel_wired):
    h = RunHandle("r1")
    store = cancel_wired(_DurableCancelStore(status="running"), handle=h)
    resp = _post_cancel()
    assert resp.status_code == 202 and h.cancelled() is True
    assert store.marked == [("r1", "u1")]               # 202 与轮边界之间重启，意愿不丢


# ── distributed admission ───────────────────────────────────────────────────────


def test_global_admission_off_by_default(monkeypatch):
    import opensearch_pipeline.routes.agent as agent_route
    monkeypatch.delenv("RAG_AGENT_GLOBAL_MAX_RUNNING", raising=False)
    assert agent_route._global_admission_full(
        SimpleNamespace(count_active_runs=lambda: 999)) is False


def test_global_admission_full_and_not_full(monkeypatch):
    import opensearch_pipeline.routes.agent as agent_route
    monkeypatch.setenv("RAG_AGENT_GLOBAL_MAX_RUNNING", "2")
    assert agent_route._global_admission_full(
        SimpleNamespace(count_active_runs=lambda: 2)) is True
    assert agent_route._global_admission_full(
        SimpleNamespace(count_active_runs=lambda: 1)) is False


def test_global_admission_fails_open(monkeypatch):
    import opensearch_pipeline.routes.agent as agent_route
    monkeypatch.setenv("RAG_AGENT_GLOBAL_MAX_RUNNING", "1")

    def _boom():
        raise RuntimeError("db down")

    assert agent_route._global_admission_full(
        SimpleNamespace(count_active_runs=_boom)) is False
    assert agent_route._global_admission_full(SimpleNamespace()) is False   # 旧 store 无计数


def test_recovered_submit_backs_off_when_global_full(monkeypatch):
    """恢复重驱过闸：全局满 → DispatchRetryLater（留 claimed 等租约到期，不收 failed）。"""
    import opensearch_pipeline.routes.agent as agent_route
    from opensearch_pipeline.agent_runtime.durable_dispatcher import DispatchRetryLater
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (None, None, None,
                                 SimpleNamespace(count_active_runs=lambda: 10)))
    monkeypatch.setenv("RAG_AGENT_GLOBAL_MAX_RUNNING", "1")
    cmd = {"command_id": "c1", "user_id": "u1", "thread_id": "t1",
           "payload": {"question": "问题"}}
    with pytest.raises(DispatchRetryLater, match="全局并发"):
        agent_route._dispatch_recovered_submit(cmd)


# ── 独立 worker ─────────────────────────────────────────────────────────────────


def test_worker_requires_flags(monkeypatch):
    from opensearch_pipeline import agent_worker
    monkeypatch.delenv("RAG_AGENT_ENABLE", raising=False)
    with pytest.raises(RuntimeError, match="RAG_AGENT_ENABLE"):
        agent_worker.build_worker()
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.delenv("RAG_AGENT_DURABLE_DISPATCH", raising=False)
    with pytest.raises(RuntimeError, match="RAG_AGENT_DURABLE_DISPATCH"):
        agent_worker.build_worker()


def test_worker_assembles_and_suppresses_web_daemon(monkeypatch):
    import opensearch_pipeline.routes.agent as agent_route
    from opensearch_pipeline import agent_worker
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")
    monkeypatch.setattr(agent_route, "_DISPATCHER_STARTED", False)
    monkeypatch.setattr(agent_route, "_get_runtime", lambda: (None, None, None, None))
    sentinel = SimpleNamespace(holder="h1", lease_s=120)
    monkeypatch.setattr(agent_route, "_get_dispatcher", lambda: sentinel)
    dispatcher, stop = agent_worker.build_worker()
    assert dispatcher is sentinel and not stop.is_set()
    assert agent_route._DISPATCHER_STARTED is True      # 抑制 web daemon 双跑


def test_web_dispatch_loop_kill_switch(monkeypatch):
    """RAG_AGENT_DISPATCH_LOOP=off：web 副本不起恢复线程（让路独立 worker）。"""
    import opensearch_pipeline.routes.agent as agent_route
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")
    monkeypatch.setenv("RAG_AGENT_DISPATCH_LOOP", "off")
    monkeypatch.setattr(agent_route, "_DISPATCHER_STARTED", False)
    agent_route._start_dispatcher()
    assert agent_route._DISPATCHER_STARTED is False
    monkeypatch.setenv("RAG_AGENT_DISPATCH_LOOP", "on")
    monkeypatch.setattr(agent_route, "_get_dispatcher",
                        lambda: SimpleNamespace(holder="h", lease_s=1,
                                                run_forever=lambda stop: None))
    agent_route._start_dispatcher()
    assert agent_route._DISPATCHER_STARTED is True


# ── 拓扑守卫（P1-10 合流增量）────────────────────────────────────────────────────


class TestTopologyGuardStageD:
    _BASE = dict(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                 RAG_DASHSCOPE_API_KEY="x", RAG_REQUIRE_AUTH="true",
                 RAG_ACL_FAIL_CLOSED="true", RAG_ALLOW_LEGACY_OPEN_PROD="",
                 RAG_SESSION_BACKEND="redis", RAG_RATE_LIMIT_BACKEND="redis",
                 RAG_MSG_DEDUP_BACKEND="redis", RAG_TOKEN_CACHE_BACKEND="redis")

    def test_multireplica_agent_without_durable_dispatch_raises(self):
        with pytest.raises(ValueError, match="RAG_AGENT_DURABLE_DISPATCH"):
            _fresh_load(RAG_EXPECTED_REPLICAS="2", RAG_AGENT_ENABLE="true",
                        RAG_AGENT_EVENT_RELAY="redis", **self._BASE)

    def test_multireplica_agent_with_durable_dispatch_passes(self):
        cfg = _fresh_load(RAG_EXPECTED_REPLICAS="2", RAG_AGENT_ENABLE="true",
                          RAG_AGENT_EVENT_RELAY="redis",
                          RAG_AGENT_DURABLE_DISPATCH="true", **self._BASE)
        assert cfg.environment == "production"

    def test_multireplica_agent_off_not_required(self):
        cfg = _fresh_load(RAG_EXPECTED_REPLICAS="2", **self._BASE)
        assert cfg.environment == "production"


# ── 真库契约（本地 MySQL；无库/无列整体 skip，host-pin 绝不写 staging/prod）────────


def _local_operation_conn():
    import pymysql

    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；解析到 host {cfg.rds.host!r}，拒绝连接。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.operation_database,
                           autocommit=False, connect_timeout=5)


def _cancel_db_ready():
    try:
        conn = _local_operation_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema=DATABASE() AND table_name='agent_run' "
                        "AND column_name='cancel_requested_at'")
            ok = cur.fetchone()[0] == 1
        conn.close()
        return ok
    except Exception:   # noqa: BLE001
        return False


skipif_no_cancel_db = pytest.mark.skipif(
    not _cancel_db_ready(), reason="本地 MySQL 无 agent_run.cancel_requested_at（先 apply schema/047）")


@skipif_no_cancel_db
def test_request_cancel_roundtrip_real_db():
    """真 MySQL：running 行标记成功→读回 True→二次标记 False（首标不覆写）；
    终态行不可标；count_active_runs 计入本行。"""
    run_id = uuid.uuid4().hex
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_run (run_id, thread_id, user_id, channel, status, "
                "agent_profile, acl_groups_snapshot, started_at) "
                "VALUES (%s,%s,'u_测试','console','running','default','[]',NOW(3))",
                (run_id, f"t_{run_id[:12]}"))
        conn.commit()
        store = RDSRunStore()
        assert store.count_active_runs() >= 1
        assert store.request_cancel(run_id, requested_by="tester") is True
        assert store.cancel_requested(run_id) is True
        assert store.request_cancel(run_id, requested_by="别人") is False   # 首标不覆写
        with conn.cursor() as cur:
            cur.execute("SELECT cancel_requested_by FROM agent_run WHERE run_id=%s",
                        (run_id,))
            assert cur.fetchone()[0] == "tester"
            cur.execute("UPDATE agent_run SET status='succeeded' WHERE run_id=%s", (run_id,))
        conn.commit()
        run2 = uuid.uuid4().hex
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_run (run_id, thread_id, user_id, channel, status, "
                "agent_profile, acl_groups_snapshot, started_at) "
                "VALUES (%s,%s,'u_测试','console','succeeded','default','[]',NOW(3))",
                (run2, f"t_{run2[:12]}"))
        conn.commit()
        assert store.request_cancel(run2) is False                          # 终态不可标
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_run WHERE user_id='u_测试'")
            conn.commit()
        finally:
            conn.close()