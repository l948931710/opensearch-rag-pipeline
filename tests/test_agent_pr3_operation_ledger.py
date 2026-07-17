# -*- coding: utf-8 -*-
"""
test_agent_pr3_operation_ledger.py — PR-3 Stage C（HIGH_WRITE 操作台账 + 自动对账 + 隔离池）

设计 docs/pr3_durable_worker_design_2026-07-17_DRAFT.md §3 Stage C 的验收门：
- executor 透传：副作用工具执行前 ctx.operation_id = invocation_id（reclaim 重试同 id）；
  READ_ONLY 不注入；ctx=None / 简化 ctx 静默跳过（行为与 Stage C 之前一致）；
- per-tool worker 隔离：副作用工具跑 tool-iso-<name> 专属池、读工具留共享池；
  kill switch（RAG_AGENT_TOOL_ISOLATED_POOL=false）回退共享池；舱壁语义不变；
- 台账语义：同 operation_id 二次 insert → OperationAlreadyApplied（PK fencing）；
  check 三态（applied / not_applied / unknown-错配）；
- 工具接线（MemoryOntologyStore × InMemoryOperationLedger 配对）：铸别名随座缝写台账
  （回执与返回值同构）；fencing 输家读台账回执按幂等成功、零新副作用；未注入
  operation_id 不写台账；check_operation 走台账；
- 自动对账：applied→succeeded（回执+同事务审计随 CAS）；not_applied→failed；
  unknown / 无 check_operation / registry 失踪 → 留置；CAS 输家计 lost 不报错；
- run_store SQL 面（伪连接）：resolve 的 receipt_json 只在 succeeded 方向进 SET；
  失败方向绝不覆写回执；扫描 SQL 带静置期谓词。
"""
import threading
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.operation_ledger import (
    InMemoryOperationLedger,
    OperationAlreadyApplied,
    RDSOperationLedger,
)
from opensearch_pipeline.agent_runtime.operation_reconciler import (
    reconcile_uncertain_invocations,
)
from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec
from opensearch_pipeline.agent_runtime.tool_executor import ToolExecutor
from opensearch_pipeline.agent_tools.ontology_identity_resolve import (
    OntologyIdentityResolveTool,
)
from opensearch_pipeline.ontology import stewardship
from opensearch_pipeline.ontology.store import MemoryOntologyStore


# ── 共用桩（形态对齐 test_agent_runtime_tool_executor._Store）────────────────────


class _Store:
    def __init__(self):
        self.invocations = []
        self.stamped = []          # mark_invocation_operation 资格章记录

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

    def find_invocation_by_key(self, tool_name, idempotency_key):
        return None

    def mark_invocation_operation(self, invocation_id):
        self.stamped.append(invocation_id)


class _CaptureTool:
    """记录 run 时的 ctx.operation_id 与执行线程名。"""

    def __init__(self, spec):
        self.spec = spec
        self.seen_operation_ids = []
        self.thread_names = []

    def run(self, ctx, args, idempotency_key=None):
        self.seen_operation_ids.append(getattr(ctx, "operation_id", None))
        self.thread_names.append(threading.current_thread().name)
        return ToolResult.text_ok("ok")


def _spec(risk=RiskLevel.LOW_WRITE, idem="key_required", name="wtool", side_effects=True):
    return ToolSpec(name=name, version="1.0.0", description="d",
                    input_schema={"type": "object"}, output_schema={"type": "object"},
                    risk_level=risk, permission_scope="kb.search",
                    data_classification="internal", idempotency=idem,
                    side_effects=side_effects)


def _real_ctx(run_id="r1"):
    return ExecutionContext.create(request_id="req", user_id="u1", acl_groups=["pmc"],
                                   roles=("employee",), channel="console",
                                   thread_id="t1", run_id=run_id)


def _execute(executor, tool, ctx, key="k1"):
    return executor.execute(ctx, tool, {}, run_id="r1", step_no=1,
                            policy_decision="allow", policy_id="p1",
                            idempotency_key=key)


# ── executor 透传 ────────────────────────────────────────────────────────────────


def test_executor_injects_operation_id_for_side_effect_tool():
    store = _Store()
    tool = _CaptureTool(_spec(name="w_inject"))
    r = _execute(ToolExecutor(store), tool, _real_ctx())
    assert r.status == "succeeded"
    # operation_id = 本次 invocation 行 id（fencing 键与 trace 行同源）
    assert tool.seen_operation_ids == [store.invocations[0]["id"]]
    # 注入成功 ⇒ 对账资格章回填（schema/046）
    assert store.stamped == [store.invocations[0]["id"]]


def test_executor_skips_injection_for_read_only_tool():
    tool = _CaptureTool(_spec(risk=RiskLevel.READ_ONLY, idem="none",
                              name="r_noinject", side_effects=False))
    r = _execute(ToolExecutor(_Store()), tool, _real_ctx(), key=None)
    assert r.status == "succeeded"
    assert tool.seen_operation_ids == [None]


def test_executor_injection_fails_open_on_simplified_ctx():
    """SimpleNamespace / None ctx（既有测试形态）：注入静默跳过，执行照常；
    注入没成 ⇒ 绝不盖资格章（没章的行永留人工对账通道）。"""
    store = _Store()
    tool = _CaptureTool(_spec(name="w_simplectx"))
    assert _execute(ToolExecutor(store), tool, SimpleNamespace()).status == "succeeded"
    assert tool.seen_operation_ids == [None] and store.stamped == []
    store2 = _Store()
    tool2 = _CaptureTool(_spec(name="w_nonectx"))
    assert _execute(ToolExecutor(store2), tool2, None).status == "succeeded"
    assert tool2.seen_operation_ids == [None] and store2.stamped == []


def test_reclaimed_retry_reuses_same_operation_id():
    """failed 回收重试复用原 invocation 行 ⇒ 同 operation_id（台账 PK fencing 的前提）。"""
    store = _Store()

    class _StoreWithPrior(_Store):
        def __init__(self):
            super().__init__()
            self.reclaimed = []

        def find_invocation_by_key(self, tool_name, idempotency_key):
            return {"invocation_id": "invPRIOR", "status": "failed", "age_s": 10}

        def reclaim_failed_invocation(self, invocation_id):
            self.reclaimed.append(invocation_id)
            return True

    store = _StoreWithPrior()
    tool = _CaptureTool(_spec(name="w_reclaim"))
    r = _execute(ToolExecutor(store), tool, _real_ctx())
    assert r.status == "succeeded" and store.reclaimed == ["invPRIOR"]
    assert tool.seen_operation_ids == ["invPRIOR"]


# ── per-tool worker 隔离 ─────────────────────────────────────────────────────────


def test_side_effect_tool_runs_on_isolated_pool():
    tool = _CaptureTool(_spec(name="w_isopool"))
    assert _execute(ToolExecutor(_Store()), tool, _real_ctx()).status == "succeeded"
    assert tool.thread_names[0].startswith("tool-iso-w_isopool")


def test_read_only_tool_stays_on_shared_pool():
    tool = _CaptureTool(_spec(risk=RiskLevel.READ_ONLY, idem="none",
                              name="r_shared", side_effects=False))
    assert _execute(ToolExecutor(_Store()), tool, _real_ctx(), key=None).status == "succeeded"
    assert tool.thread_names[0].startswith("tool-timeout")


def test_isolated_pool_kill_switch(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_TOOL_ISOLATED_POOL", "false")
    tool = _CaptureTool(_spec(name="w_killswitch"))
    assert _execute(ToolExecutor(_Store()), tool, _real_ctx()).status == "succeeded"
    assert tool.thread_names[0].startswith("tool-timeout")


# ── 台账语义 ────────────────────────────────────────────────────────────────────


def test_memory_ledger_fencing_and_check():
    led = InMemoryOperationLedger()
    led.insert_tx(None, operation_id="op1", tool_name="toolA", run_id="r1",
                  receipt={"identifier_id": "i1"})
    with pytest.raises(OperationAlreadyApplied):
        led.insert_tx(None, operation_id="op1", tool_name="toolA")
    assert led.check("toolA", "op1")["outcome"] == "applied"
    assert led.check("toolA", "op1")["receipt"] == {"identifier_id": "i1"}
    assert led.check("toolA", "op404")["outcome"] == "not_applied"
    # 账实错配（台账行归属另一工具）→ unknown，绝不自动处置
    assert led.check("toolB", "op1")["outcome"] == "unknown"


def test_rds_ledger_translates_dup_and_tolerates_no_cursor():
    class _DupCur:
        def execute(self, sql, params=None):
            raise Exception(1062, "Duplicate entry 'op1' for key 'PRIMARY'")

    with pytest.raises(OperationAlreadyApplied) as ei:
        RDSOperationLedger().insert_tx(_DupCur(), operation_id="op1", tool_name="t")
    # 异常消息不得携带上游各层用来翻译 dup 的记号（原样穿透，不被误译）
    assert "Duplicate entry" not in str(ei.value) and "1062" not in str(ei.value)
    # cur=None（内存主存储配对）→ 诚实 no-op，不炸
    assert RDSOperationLedger().insert_tx(None, operation_id="op2", tool_name="t") == "op2"


# ── 工具接线（MemoryOntologyStore × InMemoryOperationLedger）──────────────────────


@pytest.fixture(autouse=True)
def _fresh_resolver_registry(monkeypatch):
    import opensearch_pipeline.agent_runtime.approval_store as approval_store
    monkeypatch.setattr(approval_store, "_SCOPE_RESOLVERS", {})


@pytest.fixture()
def ont_store():
    mem = MemoryOntologyStore()
    stewardship.ensure_seeds(mem)
    return mem


def _ont_ctx(operation_id=None, run_id="r1"):
    ctx = ExecutionContext.create(request_id="", user_id="u1", acl_groups=["pmc"],
                                  roles=("employee",), channel="console",
                                  thread_id="t1", run_id=run_id)
    if operation_id is not None:
        from dataclasses import replace
        ctx = replace(ctx, operation_id=operation_id)
    return ctx


def _mint_target(store):
    return store.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc",
                             _caller="test")["object_id"]


def test_tool_writes_ledger_with_mint(ont_store):
    led = InMemoryOperationLedger()
    tool = OntologyIdentityResolveTool(store=ont_store, operation_ledger=led)
    target = _mint_target(ont_store)
    r = tool.run(_ont_ctx(operation_id="opX"),
                 {"namespace": "u8", "value": "CP-062", "target_object_id": target})
    assert r.status == "succeeded" and r.receipt and not r.receipt["idempotent"]
    row = led.fetch("opX")
    assert row is not None and row["tool_name"] == tool.spec.name and row["run_id"] == "r1"
    # 台账回执与返回回执同构（同一构造点）
    assert row["receipt"] == r.receipt
    assert tool.check_operation("opX")["outcome"] == "applied"


def test_tool_without_operation_id_writes_no_ledger(ont_store):
    led = InMemoryOperationLedger()
    tool = OntologyIdentityResolveTool(store=ont_store, operation_ledger=led)
    target = _mint_target(ont_store)
    r = tool.run(_ont_ctx(), {"namespace": "u8", "value": "CP-063",
                              "target_object_id": target})
    assert r.status == "succeeded"
    assert led.fetch("opX") is None and led._rows == {}


def test_tool_fencing_loser_returns_ledger_receipt(ont_store):
    """僵尸已提交同 operation → 重试撞 fence：读台账回执按幂等成功，零新副作用。"""
    led = InMemoryOperationLedger()
    tool = OntologyIdentityResolveTool(store=ont_store, operation_ledger=led)
    target = _mint_target(ont_store)
    ghost_receipt = {"identifier_id": "ghost", "idempotent": False}
    led.insert_tx(None, operation_id="opY", tool_name=tool.spec.name, receipt=ghost_receipt)
    r = tool.run(_ont_ctx(operation_id="opY"),
                 {"namespace": "u8", "value": "CP-064", "target_object_id": target})
    assert r.status == "succeeded"
    assert r.receipt == ghost_receipt
    assert "已由先前执行提交" in r.content[0].text
    # 输家事务未落任何别名（fence 先于 mutation）
    assert ont_store.get_active_identifier("u8", "CP-064") is None


def test_tool_check_operation_not_applied(ont_store):
    tool = OntologyIdentityResolveTool(store=ont_store,
                                       operation_ledger=InMemoryOperationLedger())
    assert tool.check_operation("nope")["outcome"] == "not_applied"


# ── 自动对账 ────────────────────────────────────────────────────────────────────


class _ReconStore:
    def __init__(self, rows, resolve_ok=True):
        self.rows = rows
        self.resolve_ok = resolve_ok
        self.resolved = []

    def list_uncertain_invocations_for_reconcile(self, *, min_age_s, limit):
        self.seen_min_age = min_age_s
        return list(self.rows)

    def resolve_uncertain_invocation(self, invocation_id, *, to_status, note, resolved_by,
                                     audit_writer=None, receipt_json=None):
        calls = []
        if audit_writer is not None:
            audit_writer(_AuditCur(calls))       # 模拟同事务审计写
        self.resolved.append({"invocation_id": invocation_id, "to_status": to_status,
                              "note": note, "resolved_by": resolved_by,
                              "receipt_json": receipt_json, "audit_sqls": calls})
        return self.resolve_ok


class _AuditCur:
    def __init__(self, sink):
        self._sink = sink

    def execute(self, sql, params=None):
        self._sink.append(sql)


class _ProbeTool:
    def __init__(self, probe):
        self._probe = probe

    def check_operation(self, operation_id):
        return self._probe


class _Registry:
    def __init__(self, tools):
        self._tools = tools

    def get(self, name, version=None):
        return self._tools.get(name)


def test_reconcile_applied_resolves_succeeded_with_receipt_and_audit():
    store = _ReconStore([{"invocation_id": "i1", "tool_name": "t1", "run_id": "r1",
                          "operation_id": "i1"}])
    reg = _Registry({"t1": _ProbeTool({"outcome": "applied",
                                       "receipt": {"identifier_id": "x"}, "detail": "有行"})})
    counts = reconcile_uncertain_invocations(store, reg, min_age_s=1, limit=10)
    assert counts["succeeded"] == 1 and store.seen_min_age == 1
    done = store.resolved[0]
    assert done["to_status"] == "succeeded" and done["resolved_by"] == "auto-reconciler"
    assert done["receipt_json"] == '{"identifier_id": "x"}'
    assert any("agent_audit_log" in s for s in done["audit_sqls"])   # P1-13 同事务审计


def test_reconcile_not_applied_resolves_failed_without_receipt():
    store = _ReconStore([{"invocation_id": "i2", "tool_name": "t1", "run_id": None,
                          "operation_id": "i2"}])
    reg = _Registry({"t1": _ProbeTool({"outcome": "not_applied", "receipt": None,
                                       "detail": "无行"})})
    counts = reconcile_uncertain_invocations(store, reg, min_age_s=1, limit=10)
    assert counts["failed"] == 1
    assert store.resolved[0]["to_status"] == "failed"
    assert store.resolved[0]["receipt_json"] is None


def test_reconcile_leaves_unknown_protocol_less_and_unstamped_untouched():
    rows = [{"invocation_id": "i3", "tool_name": "t_unknown", "run_id": None,
             "operation_id": "i3"},
            {"invocation_id": "i4", "tool_name": "t_noproto", "run_id": None,
             "operation_id": "i4"},
            {"invocation_id": "i5", "tool_name": "t_gone", "run_id": None,
             "operation_id": "i5"},
            # 无资格章（Stage C 前遗留行形态）：即便工具支持协议也不许自动处置——
            # 「台账无行」对未注入的执行证明不了未提交
            {"invocation_id": "i6", "tool_name": "t_unknown", "run_id": None,
             "operation_id": None}]
    store = _ReconStore(rows)
    reg = _Registry({"t_unknown": _ProbeTool({"outcome": "unknown", "detail": "不可达"}),
                     "t_noproto": object()})       # 无 check_operation
    counts = reconcile_uncertain_invocations(store, reg, min_age_s=1, limit=10)
    assert store.resolved == []                    # 一个都没动
    assert counts["unknown"] == 1 and counts["skipped"] == 3


def test_reconcile_cas_loser_counts_lost_not_error():
    store = _ReconStore([{"invocation_id": "i6", "tool_name": "t1", "run_id": None,
                          "operation_id": "i6"}],
                        resolve_ok=False)
    reg = _Registry({"t1": _ProbeTool({"outcome": "applied", "receipt": None, "detail": "-"})})
    counts = reconcile_uncertain_invocations(store, reg, min_age_s=1, limit=10)
    assert counts["lost"] == 1 and counts["succeeded"] == 0


def test_reconcile_probe_exception_is_contained():
    class _Boom:
        def check_operation(self, operation_id):
            raise RuntimeError("探针炸了")

    rows = [{"invocation_id": "i7", "tool_name": "tb", "run_id": None,
             "operation_id": "i7"},
            {"invocation_id": "i8", "tool_name": "t1", "run_id": None,
             "operation_id": "i8"}]
    store = _ReconStore(rows)
    reg = _Registry({"tb": _Boom(),
                     "t1": _ProbeTool({"outcome": "applied", "receipt": None, "detail": "-"})})
    counts = reconcile_uncertain_invocations(store, reg, min_age_s=1, limit=10)
    # 单行故障不拖垮整轮：i7 计 unknown，i8 照常处置
    assert counts["unknown"] == 1 and counts["succeeded"] == 1


# ── run_store SQL 面（伪连接）───────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, log):
        self._log = log
        self.rowcount = 1

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log):
        self._log = log

    def cursor(self):
        return _FakeCursor(self._log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _patched_store(monkeypatch, log):
    store = RDSRunStore()
    monkeypatch.setattr(store, "_conn", lambda: _FakeConn(log))
    return store


def test_resolve_receipt_json_only_on_succeeded(monkeypatch):
    log = []
    store = _patched_store(monkeypatch, log)
    assert store.resolve_uncertain_invocation(
        "i1", to_status="succeeded", note="n", resolved_by="auto-reconciler",
        receipt_json='{"a":1}')
    sql, params = log[0]
    assert "receipt_json=%s" in sql and '{"a":1}' in params
    assert "[对账 by auto-reconciler]" in params[1]

    log.clear()
    # failed 方向：即便带了 receipt_json 也绝不覆写（失败不该留"成功回执"）
    assert store.resolve_uncertain_invocation(
        "i2", to_status="failed", note="n", resolved_by="admin", receipt_json='{"a":1}')
    sql, params = log[0]
    assert "receipt_json" not in sql


def test_list_uncertain_for_reconcile_sql_shape(monkeypatch):
    log = []
    store = _patched_store(monkeypatch, log)
    assert store.list_uncertain_invocations_for_reconcile(min_age_s=120, limit=7) == []
    sql, params = log[0]
    assert "status='uncertain'" in sql and "INTERVAL %s SECOND" in sql
    assert params == (120, 7)


# ── 真库契约（本地 MySQL；无库/无表整体 skip，host-pin 绝不写 staging/prod）────────


def _local_operation_conn():
    import pymysql

    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；解析到 host {cfg.rds.host!r}，拒绝连接。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.operation_database,
                           autocommit=False, connect_timeout=5)


def _ledger_db_ready():
    try:
        conn = _local_operation_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema=DATABASE() AND table_name IN "
                        "('agent_tool_operation','tool_invocation')")
            ok = cur.fetchone()[0] == 2
        conn.close()
        return ok
    except Exception:   # noqa: BLE001
        return False


_LEDGER_DB_OK = _ledger_db_ready()

skipif_no_ledger_db = pytest.mark.skipif(
    not _LEDGER_DB_OK, reason="本地 MySQL 无 agent_tool_operation（先 apply schema/045）")


@skipif_no_ledger_db
def test_rds_ledger_roundtrip_and_fencing_real_db():
    """真 MySQL：insert→fetch 回执 JSON 往返；同 id 二次 insert=真 1062→
    OperationAlreadyApplied（fencing 输家事务回滚）。"""
    import uuid as _uuid

    from opensearch_pipeline.agent_runtime.operation_ledger import (
        fetch_operation,
        insert_operation_tx,
    )
    op_id = _uuid.uuid4().hex
    receipt = {"identifier_id": "id1", "requested_by": "u测试"}
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            insert_operation_tx(cur, operation_id=op_id, tool_name="t_real",
                                run_id=None, receipt=receipt)
        conn.commit()
        row = fetch_operation(op_id)
        assert row is not None and row["receipt"] == receipt
        assert RDSOperationLedger().check("t_real", op_id)["outcome"] == "applied"
        assert RDSOperationLedger().check("t_other", op_id)["outcome"] == "unknown"
        with conn.cursor() as cur:
            with pytest.raises(OperationAlreadyApplied):
                insert_operation_tx(cur, operation_id=op_id, tool_name="t_real")
        conn.rollback()
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_tool_operation WHERE operation_id=%s", (op_id,))
            conn.commit()
        finally:
            conn.close()


@skipif_no_ledger_db
def test_resolve_uncertain_with_receipt_real_db():
    """真 MySQL：uncertain 行 → 扫描面拾取（静置期谓词）→ resolve succeeded 带回执落库。"""
    import uuid as _uuid
    inv_id = _uuid.uuid4().hex
    key = f"k_{inv_id[:12]}"
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tool_invocation (invocation_id, run_id, step_no, tool_name, "
                "tool_version, args_json, args_digest, idempotency_key, status, "
                "policy_decision, policy_id, operation_id, started_at, ended_at) "
                "VALUES (%s,%s,1,%s,'1.0.0','{}','d',%s,'uncertain','allow','p',%s,"
                "DATE_SUB(NOW(3), INTERVAL 2000 SECOND), "
                "DATE_SUB(NOW(3), INTERVAL 1000 SECOND))",
                (inv_id, _uuid.uuid4().hex, "t_real_resolve", key, inv_id))
        conn.commit()
        store = RDSRunStore()
        rows = store.list_uncertain_invocations_for_reconcile(min_age_s=300, limit=200)
        assert any(r["invocation_id"] == inv_id for r in rows)
        assert store.resolve_uncertain_invocation(
            inv_id, to_status="succeeded", note="真库对账验证",
            resolved_by="auto-reconciler", receipt_json='{"identifier_id": "x9"}')
        with conn.cursor() as cur:
            cur.execute("SELECT status, receipt_json, error_text FROM tool_invocation "
                        "WHERE invocation_id=%s", (inv_id,))
            status, receipt_json, err = cur.fetchone()
        assert status == "succeeded" and "x9" in (receipt_json or "")
        assert "[对账 by auto-reconciler]" in (err or "")
        # CAS 单向：已处置行不可再翻
        assert not store.resolve_uncertain_invocation(
            inv_id, to_status="failed", note="二翻", resolved_by="x")
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tool_invocation WHERE invocation_id=%s", (inv_id,))
            conn.commit()
        finally:
            conn.close()
