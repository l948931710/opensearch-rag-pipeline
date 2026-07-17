# -*- coding: utf-8 -*-
"""
test_agent_batch4_fixes.py — 外审核查修复批次4（2026-07-17）回归

台账 docs/audits/enterprise_agent_review_verification_2026-07-16.md 批次4：
- P1-09 schema/MIGRATION_MANIFEST.tsv 单一来源 + readiness 逐库校验（迁移记错库不再被
  三库并集误判已应用）+ _AGENT_TABLES 补 llm_call_log/agent_audit_log + uk_thread_active
  索引探针；
- P1-13 治理审计同事务（approval_store.decide / resolve_uncertain_invocation /
  registry set_status 经 audit_writer(cur) 折入主事务；kill switch 原子失败先关闸回退）；
- P1-14 LEGACY ack 绑当日日期（batch5 文件就地更新）+ readiness 姿态自报；
- P1-10 增量：多副本守卫在 agent 开时要求 RAG_AGENT_EVENT_RELAY=redis；
- P1-11 console session_id 统一 sid:{user}:{sid} 命名空间（幂等、跨用户 403）；
- P1-12 message_id 随 create_run 同事务写入（ctx 携带）。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import opensearch_pipeline.routes.agent as agent_route
from opensearch_pipeline import api, readiness
from opensearch_pipeline.agent_runtime import (
    ChatResponse,
    ModelGateway,
    ThreadedRunExecutor,
    Usage,
    default_policy_engine,
    make_adjudicator,
)
from opensearch_pipeline.agent_tools import build_default_registry
from opensearch_pipeline.api import Identity, current_identity
from tests.test_config_loading import _fresh_load

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"


# ── P1-09：manifest 单一来源 + 逐库校验 ─────────────────────────────────────


def _load_manifest_dict():
    out = {}
    for line in (_SCHEMA_DIR / "MIGRATION_MANIFEST.tsv").read_text("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, target = line.split()[:2]
            out[name] = target
    return out


def test_p1_09_manifest_covers_all_schema_files():
    """双向完整：schema/ 每个编号迁移都在清单里（且目标合法），清单不含幽灵文件。"""
    manifest = _load_manifest_dict()
    local = {f.name for f in _SCHEMA_DIR.iterdir()
             if f.is_file() and readiness._MIG_RE.match(f.name)}
    assert local == set(manifest), (
        f"清单与 schema/ 不一致：缺 {local - set(manifest)}，多 {set(manifest) - local}")
    assert set(manifest.values()) <= {"knowledge", "operation", "ontology", "both", "split"}


class _LedgerCur:
    def __init__(self, ledgers):
        self._ledgers = ledgers            # dbname → [(filename, checksum)]
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        for dbname, rows in self._ledgers.items():
            if f"{dbname}.schema_migrations" in sql:
                self._rows = rows
                return
        raise RuntimeError(f"unexpected sql: {sql}")

    def fetchall(self):
        return self._rows


class _LedgerConn:
    def __init__(self, ledgers):
        self._ledgers = ledgers

    def cursor(self):
        return _LedgerCur(self._ledgers)

    def close(self):
        pass


def _wire_ledgers(monkeypatch, move_to_wrong_db=None):
    """按真实 manifest 构造三库台账（全部记在目标库）；move_to_wrong_db 把该文件的
    台账行从目标库挪到别的库（模拟迁移记错库）。"""
    manifest = _load_manifest_dict()
    dbs = {"knowledge": "k", "operation": "o", "ontology": "t"}
    ledgers = {"k": [], "o": [], "t": []}
    for name, target in manifest.items():
        rows = [(name, None)]
        if target == "both":
            for db in ledgers:
                ledgers[db] += rows
        elif target == "split":
            ledgers["k"] += rows            # split 回退全局并集：记任一库即可
        else:
            ledgers[dbs[target]] += rows
    if move_to_wrong_db:
        target = manifest[move_to_wrong_db]
        src = dbs[target]
        ledgers[src] = [r for r in ledgers[src] if r[0] != move_to_wrong_db]
        wrong = "k" if src != "k" else "o"
        ledgers[wrong].append((move_to_wrong_db, None))
    readiness._reset_cache()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _LedgerConn(ledgers))
    monkeypatch.setattr("opensearch_pipeline.config.get_config",
                        lambda: SimpleNamespace(rds=SimpleNamespace(
                            database="k", operation_database="o", ontology_database="t")))


def test_p1_09_per_db_ledger_ok_when_all_in_target(monkeypatch):
    _wire_ledgers(monkeypatch)
    assert readiness._schema_drift_once() == "ok"


def test_p1_09_misplaced_ledger_row_detected(monkeypatch):
    """迁移记错库（旧语义三库并集会误判已应用）→ unapplied（逐库判定）。"""
    _wire_ledgers(monkeypatch, move_to_wrong_db="022_agent_runtime.sql")
    assert readiness._schema_drift_once() == "unapplied:1"


def test_p1_09_agent_tables_expanded():
    assert "llm_call_log" in readiness._AGENT_TABLES
    assert "agent_audit_log" in readiness._AGENT_TABLES


def test_p1_09_schema_contract_missing_index(monkeypatch):
    """列全在、uk_thread_active 索引缺失 → contract missing（此前索引不在探测面，
    per-thread 串行化静默失效）。"""
    from tests.test_unknown_unknowns_batch5 import _wire
    r = _wire(monkeypatch, {"information_schema.columns": [(1,)],
                            "information_schema.statistics": [(0,)]})
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    monkeypatch.delenv("RAG_ONTOLOGY_ENABLE", raising=False)
    st = r.schema_contract_status()
    assert st.startswith("missing:") and "uk_thread_active" in st


# ── P1-13：治理审计同事务 ──────────────────────────────────────────────────


class _TxCur:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append(sql.strip().split()[0].upper())

    def fetchone(self):
        return self._conn.fetchone_value


class _TxConn:
    def __init__(self, fetchone_value=None):
        self.executed = []
        self.committed = 0
        self.rolled_back = 0
        self.fetchone_value = fetchone_value

    def begin(self):
        self.executed.append("BEGIN")

    def cursor(self):
        return _TxCur(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


def test_p1_13_set_status_audit_in_same_tx(monkeypatch):
    from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore

    conn = _TxConn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    calls = []
    store = RDSToolRegistryStore()
    n = store.set_status("t1", "disabled", audit_writer=lambda cur: calls.append(cur))
    assert n == 1 and calls and conn.committed == 1
    assert conn.executed[0] == "BEGIN"                      # 钉连接（多语句事务）
    # writer 在 commit 前被调（同事务）：executed 里 UPDATE 先于 commit，且只 commit 一次


def test_p1_13_set_status_audit_failure_rolls_back(monkeypatch):
    from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore

    conn = _TxConn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)

    def bad_writer(cur):
        raise RuntimeError("audit table down")

    with pytest.raises(RuntimeError, match="audit table down"):
        RDSToolRegistryStore().set_status("t1", "disabled", audit_writer=bad_writer)
    assert conn.committed == 0 and conn.rolled_back == 1    # 状态与审计要么都在要么都不在


def test_p1_13_resolve_uncertain_audit_in_same_tx(monkeypatch):
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    conn = _TxConn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    calls = []
    ok = RDSRunStore().resolve_uncertain_invocation(
        "inv1", to_status="succeeded", note="核实", resolved_by="admin",
        audit_writer=lambda cur: calls.append(cur))
    assert ok and calls and conn.committed == 1

    conn2 = _TxConn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn2)

    def bad_writer(cur):
        raise RuntimeError("audit down")

    with pytest.raises(RuntimeError, match="audit down"):
        RDSRunStore().resolve_uncertain_invocation(
            "inv1", to_status="succeeded", note="核实", resolved_by="admin",
            audit_writer=bad_writer)
    assert conn2.committed == 0 and conn2.rolled_back >= 1   # 状态未翻转


class _DecideCur(_TxCur):
    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.executed.append(s.split()[0].upper())
        if "FROM" in s and "approval_request" in s and "FOR UPDATE" in s:
            self._conn.fetchone_value = ("pending", "digest0", 0)
        self.rowcount = 1


class _DecideConn(_TxConn):
    def cursor(self):
        return _DecideCur(self)


def test_p1_13_decide_audit_writer_in_tx(monkeypatch):
    from opensearch_pipeline.agent_runtime.approval_store import (
        DECIDE_ACCEPTED, RDSApprovalStore)

    conn = _DecideConn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    calls = []
    res = RDSApprovalStore().decide(
        "req1", decision="approved", decided_by="admin",
        audit_writer=lambda cur: calls.append(cur))
    assert res == DECIDE_ACCEPTED and calls and conn.committed == 1
    assert conn.executed[0] == "BEGIN"

    conn2 = _DecideConn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn2)

    def bad_writer(cur):
        raise RuntimeError("audit down")

    with pytest.raises(RuntimeError, match="audit down"):
        RDSApprovalStore().decide("req1", decision="approved", decided_by="admin",
                                  audit_writer=bad_writer)
    assert conn2.committed == 0 and conn2.rolled_back >= 1   # 决定与审计同生共死


class _ToggleStoreAtomicFails:
    """原子（带 audit_writer）失败、先关闸（无 writer）成功的注册表桩。"""

    def __init__(self):
        self.status = "active"
        self.atomic_attempts = 0

    def set_status(self, name, status, audit_writer=None):
        if audit_writer is not None:
            self.atomic_attempts += 1
            raise RuntimeError("audit table down")
        self.status = status
        return 1


def test_p1_13_kill_switch_falls_back_to_disable_first(monkeypatch):
    """原子写失败 → 先关闸回退：禁用必须落地，响应体承认 audit_recorded=false。"""
    from tests.test_routes_agent import _kb_ident

    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    fake = _ToggleStoreAtomicFails()
    monkeypatch.setattr(agent_route, "_get_registry_store", lambda: fake)
    monkeypatch.setattr(agent_route, "_get_runtime", lambda: (None, None, None, None))
    import opensearch_pipeline.dingtalk_identity as di
    monkeypatch.setattr(di, "resolve_kb_identity", lambda uid: _kb_ident("kb_admin"))
    api.app.dependency_overrides[current_identity] = lambda: Identity(
        user_id="admin1", acl_groups=["g"], role="kb_admin")
    try:
        r = TestClient(api.app).post(
            "/api/agent/tools/toggle",
            json={"tool_name": "u8_writeback", "disabled": True, "reason": "事故排查"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "disabled" and body["audit_recorded"] is False
        assert fake.status == "disabled" and fake.atomic_attempts == 1
    finally:
        api.app.dependency_overrides.clear()


# ── P1-14：readiness 姿态自报 ──────────────────────────────────────────────


def test_p1_14_legacy_open_posture_status(monkeypatch):
    monkeypatch.delenv("RAG_ALLOW_LEGACY_OPEN_PROD", raising=False)
    assert readiness.legacy_open_posture_status() == "off"
    monkeypatch.setenv("RAG_ALLOW_LEGACY_OPEN_PROD", "ack:2026-07-17")
    assert readiness.legacy_open_posture_status() == "legacy_open(ack:2026-07-17)"


# ── P1-10 增量：多副本守卫补 event relay ────────────────────────────────────


_TOPO_BASE = dict(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                  RAG_DASHSCOPE_API_KEY="x", RAG_REQUIRE_AUTH="true",
                  RAG_ACL_FAIL_CLOSED="true", RAG_ALLOW_LEGACY_OPEN_PROD="",
                  RAG_SESSION_BACKEND="redis", RAG_RATE_LIMIT_BACKEND="redis",
                  RAG_MSG_DEDUP_BACKEND="redis", RAG_TOKEN_CACHE_BACKEND="redis")


def test_p1_10_multireplica_agent_on_requires_event_relay():
    with pytest.raises(ValueError, match="RAG_AGENT_EVENT_RELAY"):
        _fresh_load(RAG_EXPECTED_REPLICAS="2", RAG_AGENT_ENABLE="true",
                    RAG_AGENT_EVENT_RELAY="", **_TOPO_BASE)


def test_p1_10_multireplica_agent_on_relay_redis_passes():
    cfg = _fresh_load(RAG_EXPECTED_REPLICAS="2", RAG_AGENT_ENABLE="true",
                      RAG_AGENT_EVENT_RELAY="redis", **_TOPO_BASE)
    assert cfg.environment == "production"


def test_p1_10_multireplica_agent_off_no_relay_requirement():
    cfg = _fresh_load(RAG_EXPECTED_REPLICAS="2", RAG_AGENT_ENABLE="",
                      RAG_AGENT_EVENT_RELAY="", **_TOPO_BASE)
    assert cfg.environment == "production"


# ── P1-11：console session key 命名空间（路由级）────────────────────────────


class _CtxCaptureStore:
    def __init__(self):
        self._n = 0
        self.threads = []
        self.message_ids = []
        self.transitions = []

    def create_run(self, ctx, profile):
        self._n += 1
        self.threads.append(ctx.thread_id)
        self.message_ids.append(getattr(ctx, "message_id", None))
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
def sid_wired(monkeypatch):
    registry = build_default_registry()
    store = _CtxCaptureStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _NoToolProvider()},
                           routes={"light": [("dashscope", "m")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    monkeypatch.setattr(agent_route, "_get_runtime",
                        lambda: (registry, gateway, executor, store))
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    yield store
    executor.shutdown()


def _ask(identity, payload):
    api.app.dependency_overrides[current_identity] = lambda: identity
    return TestClient(api.app).post("/api/agent/ask", json=payload)


def test_p1_11_console_sid_namespaced_idempotent(sid_wired):
    ident = Identity(user_id="u1", acl_groups=["g"], role="employee")
    try:
        r = _ask(ident, {"question": "问题一", "session_id": "abc123"})
        assert r.status_code == 200
        assert sid_wired.threads[-1] == "sid:u1:abc123"     # 裸 sid → 用户命名空间
        r2 = _ask(ident, {"question": "问题二", "session_id": "sid:u1:abc123"})
        assert r2.status_code == 200
        assert sid_wired.threads[-1] == "sid:u1:abc123"     # 回传复合键不叠前缀
    finally:
        api.app.dependency_overrides.clear()


def test_p1_11_foreign_sid_namespace_403(sid_wired):
    ident = Identity(user_id="u1", acl_groups=["g"], role="employee")
    try:
        r = _ask(ident, {"question": "x", "session_id": "sid:u2:abc123"})
        assert r.status_code == 403                         # 他人命名空间键直接拒
    finally:
        api.app.dependency_overrides.clear()


def test_p1_11_missing_sid_gets_namespaced_key(sid_wired):
    ident = Identity(user_id="u1", acl_groups=["g"], role="employee")
    try:
        r = _ask(ident, {"question": "x"})
        assert r.status_code == 200
        assert sid_wired.threads[-1].startswith("sid:u1:")  # 新会话也在命名空间内
    finally:
        api.app.dependency_overrides.clear()


# ── P1-12：message_id 随 create_run 同事务 ──────────────────────────────────


def test_p1_12_ctx_carries_message_id_into_create_run(sid_wired):
    ident = Identity(user_id="u1", acl_groups=["g"], role="employee")
    try:
        r = _ask(ident, {"question": "x", "session_id": "abc"})
        assert r.status_code == 200
        assert sid_wired.message_ids[-1]                    # 建 run 时 message_id 已随 ctx 到位
    finally:
        api.app.dependency_overrides.clear()


def test_p1_12_rds_insert_includes_message_id(monkeypatch):
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    captured = {}

    class _Cur(_TxCur):
        def execute(self, sql, params=None):
            captured["sql"], captured["params"] = " ".join(sql.split()), params

    class _Conn(_TxConn):
        def cursor(self):
            return _Cur(self)

    conn = _Conn()
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    ctx = SimpleNamespace(thread_id="t1", conversation_id=None, user_id="u1",
                          channel="console", acl_groups=["g"], model_profile="light",
                          prompt_version=None, git_sha=None, message_id="msg-77")
    RDSRunStore().create_run(ctx, "default")
    assert "message_id" in captured["sql"]
    assert "msg-77" in captured["params"]                   # 同一 INSERT（同事务）写入
    ctx2 = SimpleNamespace(thread_id="t1", conversation_id=None, user_id="u1",
                           channel="console", acl_groups=["g"], model_profile=None,
                           prompt_version=None, git_sha=None)
    RDSRunStore().create_run(ctx2, "default")
    assert None in captured["params"]                       # 旧调用方无值 → NULL（兼容）