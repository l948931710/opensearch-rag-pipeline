# -*- coding: utf-8 -*-
"""Majors α 批 M3(2026-07-21 codex 共识,docs/majors_remediation_plan_2026-07-21_DRAFT.md)。

α1(M3.1)commit-ACK 消歧——锁死的不变量:
- store 层:commit 段异常 → CommitAmbiguous(携原异常),rollback 仅 best-effort;
  commit **前**异常维持原分类(dup→DuplicateActiveIdentifier / 其余原样,确定失败);
- 工具层:CommitAmbiguous + 台账 applied → 幂等成功收口(绝不重复铸号/谎报失败);
  not_applied / unknown / 查证异常 / 无 operation_id → 原样上抛(→ executor 按
  has_side_effects 归 uncertain,该路径由既有 test_agent_fault_model 族钉住);
- 预边界失败(commit 前)仍以 ToolResult.fail 表达——catch-all 的 fail 保持诚实。

α2(M3.2)记账幂等 bookkeeping_id——锁死的不变量见下方第二段。
"""
import pytest

from opensearch_pipeline.agent_runtime import run_store as run_store_mod
from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.operation_ledger import InMemoryOperationLedger
from opensearch_pipeline.agent_runtime.run_store import RDSRunStore, is_bookkeeping_dup
from opensearch_pipeline.agent_tools.ontology_identity_resolve import (
    OntologyIdentityResolveTool,
)
from opensearch_pipeline.ontology import stewardship
from opensearch_pipeline.ontology.store import (
    CommitAmbiguous,
    MemoryOntologyStore,
    RDSOntologyStore,
)


def _ctx(user="u1", operation_id=None):
    ctx = ExecutionContext.create(request_id="", user_id=user, acl_groups=["pmc"],
                                  roles=("employee",), channel="console", thread_id="t1")
    if operation_id is not None:
        object.__setattr__(ctx, "operation_id", operation_id)
    return ctx


@pytest.fixture()
def store():
    mem = MemoryOntologyStore()
    stewardship.ensure_seeds(mem)
    return mem


def _target(store):
    return store.mint_object("product", "6.2口径龙虾杯", owner_dept="pmc", _caller="test")


def _args(obj):
    return {"namespace": "u8", "value": "abc123", "target_object_id": obj["object_id"]}


# ══ α1 工具层:CommitAmbiguous × 台账三态 ═══════════════════════════════════════


def _tool_with_ambiguous_commit(store, monkeypatch, ledger):
    tool = OntologyIdentityResolveTool(store=store, operation_ledger=ledger)

    def _boom(*a, **k):
        raise CommitAmbiguous("commit 结果不可知(注入)")

    monkeypatch.setattr(store, "insert_identifier_closing_case", _boom)
    return tool


def test_commit_ambiguous_ledger_applied_idempotent_ok(store, monkeypatch):
    """applied → 幂等成功收口,回执取台账,绝不二次铸号。"""
    obj = _target(store)
    led = InMemoryOperationLedger()
    led.insert_tx(None, operation_id="op-1", tool_name="ontology_identity_resolve",
                  receipt={"identifier_id": "id-1", "namespace": "u8"})
    tool = _tool_with_ambiguous_commit(store, monkeypatch, led)
    res = tool.run(_ctx(operation_id="op-1"), _args(obj))
    assert res.status == "succeeded"
    assert res.receipt == {"identifier_id": "id-1", "namespace": "u8"}
    assert "台账确认" in res.content[0].text


def test_commit_ambiguous_ledger_not_applied_reraises(store, monkeypatch):
    """not_applied ≠ 未提交(慢提交窗口)——原样上抛进 uncertain,绝不谎报确定失败。"""
    obj = _target(store)
    tool = _tool_with_ambiguous_commit(store, monkeypatch, InMemoryOperationLedger())
    with pytest.raises(CommitAmbiguous):
        tool.run(_ctx(operation_id="op-1"), _args(obj))


def test_commit_ambiguous_ledger_check_error_reraises(store, monkeypatch):
    """台账查证自身失败=仍不可知 → 上抛(fail-safe,不把基础设施故障当结论)。"""
    obj = _target(store)

    class _BrokenLedger:
        def check(self, *a, **k):
            raise RuntimeError("ledger down")

    tool = _tool_with_ambiguous_commit(store, monkeypatch, _BrokenLedger())
    with pytest.raises(CommitAmbiguous):
        tool.run(_ctx(operation_id="op-1"), _args(obj))


def test_commit_ambiguous_without_operation_id_reraises(store, monkeypatch):
    """flag-off/直调形态(无台账可查)→ 上抛;绝不在无凭据时收成功或失败。"""
    obj = _target(store)
    tool = _tool_with_ambiguous_commit(store, monkeypatch, InMemoryOperationLedger())
    with pytest.raises(CommitAmbiguous):
        tool.run(_ctx(operation_id=None), _args(obj))


def test_precommit_storage_error_still_tool_fail(store, monkeypatch):
    """commit 前异常(store 已整体回滚)→ 维持 ToolResult.fail(确定失败,可安全重试)。"""
    obj = _target(store)
    tool = OntologyIdentityResolveTool(store=store,
                                       operation_ledger=InMemoryOperationLedger())
    monkeypatch.setattr(store, "insert_identifier_closing_case",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db err")))
    res = tool.run(_ctx(operation_id="op-1"), _args(obj))
    assert res.status == "failed"
    assert "存储异常" in (res.error or "")


# ══ α1 store 层:commit 段与 commit 前的分类边界 ═══════════════════════════════


class _ScriptCursor:
    """pymysql 形状替身:fetchone 按脚本出队;可指定第 N 次 execute 抛错。"""

    def __init__(self, fetch_script, execute_exc_at=None, execute_exc=None):
        self._fetch = list(fetch_script)
        self._n = 0
        self._exc_at = execute_exc_at
        self._exc = execute_exc

    def execute(self, sql, params=None):
        self._n += 1
        if self._exc_at is not None and self._n == self._exc_at:
            raise self._exc

    def fetchone(self):
        return self._fetch.pop(0) if self._fetch else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor, commit_exc=None, rollback_exc=None):
        self._cursor = cursor
        self.commit_exc = commit_exc
        self.rollback_exc = rollback_exc
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        if self.commit_exc is not None:
            raise self.commit_exc

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_exc is not None:
            raise self.rollback_exc

    def close(self):
        pass


def _rds_store_with(conn, monkeypatch):
    st = RDSOntologyStore.__new__(RDSOntologyStore)   # 不跑 __init__(免 config/连接池)
    monkeypatch.setattr(st, "_conn", lambda: conn, raising=False)
    monkeypatch.setattr(RDSOntologyStore, "_db", staticmethod(lambda: "db"),
                        raising=False)
    return st


def test_store_commit_phase_error_raises_commit_ambiguous(monkeypatch):
    """commit 抛错 → CommitAmbiguous(__cause__=原异常);rollback best-effort 也失败不掩盖。"""
    cur = _ScriptCursor(fetch_script=[None, ("active",)])
    boom = RuntimeError("lost connection during commit")
    conn = _FakeConn(cur, commit_exc=boom, rollback_exc=RuntimeError("conn dead"))
    st = _rds_store_with(conn, monkeypatch)
    with pytest.raises(CommitAmbiguous) as ei:
        st.insert_identifier_closing_case("u8", "abc123", "ABC123", "obj-1")
    assert ei.value.__cause__ is boom
    assert conn.rollbacks == 1


def test_store_precommit_error_keeps_original_classification(monkeypatch):
    """commit 前异常(body 内)→ 原分类(此处 ValueError:目标非 active),非 CommitAmbiguous。"""
    cur = _ScriptCursor(fetch_script=[None, ("merged",)])
    conn = _FakeConn(cur)
    st = _rds_store_with(conn, monkeypatch)
    with pytest.raises(ValueError, match="非 active"):
        st.insert_identifier_closing_case("u8", "abc123", "ABC123", "obj-1")
    assert conn.rollbacks == 1


# ══ α2(M3.2)记账幂等 bookkeeping_id ══════════════════════════════════════════
#
# 锁死的不变量:
# - record_turn/record_tool_call 带 bookkeeping_id → step INSERT 落该列;
# - 1054 且点名 bookkeeping_id(055 未 apply)→ warn-once 负缓存 + legacy 无列重试;
# - is_bookkeeping_dup 限定索引名(非目标 1062 绝不误判成功);
# - executor:首/重放撞幂等键=已落(单次调用收口);持续失败=零 durable 回退写
#   (executor 侧断言在 tests/test_agent_runtime_executor.py α2 契约更新两例)。

@pytest.fixture(autouse=True)
def _reset_bookkeeping_negcache(monkeypatch):
    monkeypatch.setattr(run_store_mod, "_BOOKKEEPING_COL_MISSING", False)


class _RecCur:
    def __init__(self, exc_when=None):
        self.calls = []          # [(sql, args)]
        self._exc_when = exc_when or (lambda sql, n: None)

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        exc = self._exc_when(sql, len(self.calls))
        if exc is not None:
            raise exc

    def fetchone(self):
        return (1,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecConn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def begin(self):
        pass

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _run_store_with_conns(monkeypatch, conns):
    st = RDSRunStore()
    pool = list(conns)
    monkeypatch.setattr(st, "_conn", lambda: pool.pop(0))
    monkeypatch.setattr(run_store_mod, "_op_db", lambda: "op")
    return st


def _unknown_col_exc():
    return Exception(1054, "Unknown column 'bookkeeping_id' in 'field list'")


def test_record_turn_writes_bookkeeping_column(monkeypatch):
    cur = _RecCur()
    st = _run_store_with_conns(monkeypatch, [_RecConn(cur)])
    st.record_turn("r1", turn_index=0, tokens_total=5, bookkeeping_id="bk-1")
    step_inserts = [(s, a) for s, a in cur.calls if "agent_step" in s and "INSERT" in s]
    assert len(step_inserts) == 1
    sql, args = step_inserts[0]
    assert "bookkeeping_id" in sql and "bk-1" in args


def test_record_turn_unknown_col_falls_back_legacy_warn_once(monkeypatch):
    """1054 点名 bookkeeping_id → 负缓存置位 + 同参 legacy 无列重试(新连接新事务)。"""
    bad_cur = _RecCur(exc_when=lambda sql, n: _unknown_col_exc()
                      if ("INSERT" in sql and "bookkeeping_id" in sql) else None)
    good_cur = _RecCur()
    st = _run_store_with_conns(monkeypatch, [_RecConn(bad_cur), _RecConn(good_cur)])
    st.record_turn("r1", turn_index=0, tokens_total=5,
                   llm_rows=[{"provider": "p", "model": "m", "category": "light",
                              "status": "ok", "latency_ms": 1}],
                   bookkeeping_id="bk-1")
    assert run_store_mod._BOOKKEEPING_COL_MISSING is True
    retry_inserts = [s for s, _ in good_cur.calls if "agent_step" in s and "INSERT" in s]
    assert len(retry_inserts) == 1 and "bookkeeping_id" not in retry_inserts[0]
    # llm 行随重试事务落齐(原事务已回滚,零双计)
    assert any("llm_call_log" in s for s, _ in good_cur.calls)
    # 负缓存后续直接走无列路径(不再尝试带列)
    after_cur = _RecCur()
    st2 = _run_store_with_conns(monkeypatch, [_RecConn(after_cur)])
    st2.record_turn("r1", turn_index=1, tokens_total=1, bookkeeping_id="bk-2")
    assert all("bookkeeping_id" not in s for s, _ in after_cur.calls)


def test_record_tool_call_bookkeeping_and_unknown_col(monkeypatch):
    cur = _RecCur()
    st = _run_store_with_conns(monkeypatch, [_RecConn(cur)])
    st.record_tool_call("r1", payload={"tool_name": "t"}, bookkeeping_id="bk-9")
    ins = [(s, a) for s, a in cur.calls if "INSERT" in s]
    assert len(ins) == 1 and "bookkeeping_id" in ins[0][0] and "bk-9" in ins[0][1]
    # 1054 → legacy 重试
    bad = _RecCur(exc_when=lambda sql, n: _unknown_col_exc()
                  if ("INSERT" in sql and "bookkeeping_id" in sql) else None)
    good = _RecCur()
    monkeypatch.setattr(run_store_mod, "_BOOKKEEPING_COL_MISSING", False)
    st2 = _run_store_with_conns(monkeypatch, [_RecConn(bad), _RecConn(good)])
    st2.record_tool_call("r1", payload={"tool_name": "t"}, bookkeeping_id="bk-9")
    assert any("INSERT" in s and "bookkeeping_id" not in s for s, _ in good.calls)


def test_is_bookkeeping_dup_discriminates_target_key():
    target = Exception(1062, "Duplicate entry 'bk-1' for key 'agent_step.uk_step_bookkeeping'")
    pk = Exception(1062, "Duplicate entry 'r1-3' for key 'agent_step.PRIMARY'")
    other = Exception(1054, "Unknown column 'bookkeeping_id' in 'field list'")
    assert is_bookkeeping_dup(target) is True
    assert is_bookkeeping_dup(pk) is False
    assert is_bookkeeping_dup(other) is False


def test_executor_treats_bookkeeping_dup_as_landed():
    """首调即撞幂等键(重放场景的收口形态)→ 单次调用返回,零重试零回退写。"""
    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
    from opensearch_pipeline.agent_runtime.tool import ToolResult

    calls = []

    class _S:
        def create_run(self, ctx, p):
            return "r1"

        def record_turn(self, run_id, **kw):
            calls.append(kw)
            raise Exception(1062,
                            "Duplicate entry 'x' for key 'agent_step.uk_step_bookkeeping'")

        def append_step(self, *a, **k):
            raise AssertionError("已落判定后不得再有 durable 回退写")

        def increment_budget(self, *a, **k):
            raise AssertionError("已落判定后不得再有 durable 回退写")

    ex = ThreadedRunExecutor(_S(), lambda c, e: ToolResult.text_ok("x"), max_concurrent=1)
    ex._record_turn("r1", 0, usage=None, final=True, ctx=None)
    assert len(calls) == 1 and calls[0].get("bookkeeping_id")


def test_executor_retry_then_success_records_once():
    """瞬态失败 → 同键重放成功:恰两次调用同 bookkeeping_id,零分段回退。"""
    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
    from opensearch_pipeline.agent_runtime.tool import ToolResult

    calls = []

    class _S:
        def create_run(self, ctx, p):
            return "r1"

        def record_turn(self, run_id, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise RuntimeError("commit ack lost")

        def append_step(self, *a, **k):
            raise AssertionError("重放成功后不得分段回退")

    ex = ThreadedRunExecutor(_S(), lambda c, e: ToolResult.text_ok("x"), max_concurrent=1)
    ex._record_turn("r1", 0, usage=None, final=True, ctx=None)
    assert len(calls) == 2
    assert calls[0]["bookkeeping_id"] == calls[1]["bookkeeping_id"]
