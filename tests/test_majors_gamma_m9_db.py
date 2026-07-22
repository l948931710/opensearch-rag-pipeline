# -*- coding: utf-8 -*-
"""Majors γ1(M9.1)真库累计契约(2026-07-21 codex 共识)。

- complete_run_atomic(active_ms)：CAS 同事务 COALESCE+delta 落 agent_run.active_latency_ms，
  extra_writer 收锁内算出的 stored+delta(与 durable 同值)；
- suspend_run_atomic(active_ms) → 复走 → complete：两腿累计相加；
- transition(active_ms) 仅终态累计；read_active_latency 回读。

无本地 MySQL / agent_run 缺 054 列 → 整体 skip(CI db-integration 从 schema/ 全量
建库含 054，恒实跑)。host-pin 本地，绝不写 staging/prod。
"""
import os
import uuid

import pytest

from opensearch_pipeline.config import get_config
from opensearch_pipeline.env_guard import is_prod_target

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _local_operation_conn():
    import pymysql
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；解析到 host {cfg.rds.host!r}，拒绝连接。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.operation_database,
                           autocommit=True, connect_timeout=5)


def _db_ready_with_054():
    try:
        conn = _local_operation_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema=DATABASE() AND table_name='agent_run' "
                        "AND column_name='active_latency_ms'")
            ok = cur.fetchone()[0] == 1
        conn.close()
        return ok
    except Exception:
        return False


_DB_OK = _db_ready_with_054()

if not _DB_OK and os.environ.get("RAG_AGENT_TESTS_REQUIRE_RDS", "").strip() == "1":
    raise RuntimeError(
        "RAG_AGENT_TESTS_REQUIRE_RDS=1 但本地 MySQL 不可用或 agent_run 缺 active_latency_ms"
        "——真库契约族不许静默 skip；检查 ci_load_schema 是否含 054")

skipif_no_db = pytest.mark.skipif(
    not _DB_OK, reason="本地 MySQL 无 agent_run.active_latency_ms（先 apply schema/054）")


def _store():
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
    return RDSRunStore()


def _ctx(thread_id: str):
    from opensearch_pipeline.agent_runtime import ExecutionContext
    return ExecutionContext.create(request_id="m9db", user_id="m9-tester",
                                   acl_groups=["production"], roles=["employee"],
                                   channel="console", thread_id=thread_id)


def _read_al(run_id):
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT active_latency_ms, status FROM agent_run WHERE run_id=%s",
                        (run_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _cleanup(run_ids):
    if not run_ids:
        return
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            ph = ",".join(["%s"] * len(run_ids))
            cur.execute(f"DELETE FROM agent_checkpoint WHERE run_id IN ({ph})", list(run_ids))
            cur.execute(f"DELETE FROM agent_step WHERE run_id IN ({ph})", list(run_ids))
            cur.execute(f"DELETE FROM agent_run WHERE run_id IN ({ph})", list(run_ids))
    finally:
        conn.close()


@skipif_no_db
def test_complete_accumulates_and_writer_sees_total():
    store, runs = _store(), []
    try:
        run_id = store.create_run(_ctx(f"m9a-{uuid.uuid4().hex[:8]}"), "default")
        runs.append(run_id)
        seen = {}

        def writer(cur, active_total_ms=None):
            seen["total"] = active_total_ms

        assert store.complete_run_atomic(run_id, extra_writer=writer, active_ms=1500)
        al, status = _read_al(run_id)
        assert status == "succeeded" and int(al) == 1500
        assert seen["total"] == 1500        # 锁内 stored(0)+delta(1500)，与 durable 同值
    finally:
        _cleanup(runs)


@skipif_no_db
def test_suspend_then_complete_accumulates_both_legs():
    store, runs = _store(), []
    try:
        run_id = store.create_run(_ctx(f"m9b-{uuid.uuid4().hex[:8]}"), "default")
        runs.append(run_id)
        _cp, ok = store.suspend_run_atomic(run_id, b"blob", "digest", active_ms=700)
        assert ok
        assert store.read_active_latency(run_id) == 700
        assert store.transition(run_id, "suspended", "resuming")
        assert store.transition(run_id, "resuming", "running")
        assert store.read_active_latency(run_id) == 700   # 非终态迁移不累计
        seen = {}

        def writer(cur, active_total_ms=None):
            seen["total"] = active_total_ms

        assert store.complete_run_atomic(run_id, extra_writer=writer, active_ms=300)
        al, _ = _read_al(run_id)
        assert int(al) == 1000 and seen["total"] == 1000  # 两腿 700+300
    finally:
        _cleanup(runs)


@skipif_no_db
def test_terminal_transition_accumulates_delta():
    store, runs = _store(), []
    try:
        run_id = store.create_run(_ctx(f"m9c-{uuid.uuid4().hex[:8]}"), "default")
        runs.append(run_id)
        assert store.transition(run_id, "running", "failed", active_ms=250)
        al, status = _read_al(run_id)
        assert status == "failed" and int(al) == 250
    finally:
        _cleanup(runs)


@skipif_no_db
def test_no_active_ms_leaves_null_and_zero_is_noop():
    """不传 active_ms（既有调用形态）→ 列保持 NULL（byte-identical）；0 视同不传。"""
    store, runs = _store(), []
    try:
        r1 = store.create_run(_ctx(f"m9d-{uuid.uuid4().hex[:8]}"), "default")
        runs.append(r1)
        assert store.complete_run_atomic(r1)
        al, _ = _read_al(r1)
        assert al is None
        r2 = store.create_run(_ctx(f"m9e-{uuid.uuid4().hex[:8]}"), "default")
        runs.append(r2)
        assert store.transition(r2, "running", "cancelled", active_ms=0)
        al2, _ = _read_al(r2)
        assert al2 is None
    finally:
        _cleanup(runs)


# ── γ4（M15 价表）真库回读——γ 批真库契约共用本串行模块（agent 真库族同表）─────


@skipif_no_db
def test_g4_record_llm_call_roundtrips_price_version():
    """057 已 apply（本地）：record_llm_call 带 price_table_version 落库可回读；
    不带=NULL（byte-identical）。"""
    from decimal import Decimal

    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
    store, ids = RDSRunStore(), []
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema=DATABASE() AND table_name='llm_call_log' "
                        "AND column_name='price_table_version'")
            if cur.fetchone()[0] != 1:
                pytest.skip("llm_call_log 缺 price_table_version（先 apply schema/057）")
        c1 = store.record_llm_call(
            run_id=None, request_id="g4", provider="dashscope", model="qwen3.6-plus",
            category="agent", prompt_version=None, tokens_prompt=1000, tokens_completion=500,
            cost_estimate=Decimal("0.0100"), latency_ms=5, status="ok",
            user_id="g4-tester", dept_group="production", price_table_version="2026-07-v1")
        c2 = store.record_llm_call(
            run_id=None, request_id="g4", provider="dashscope", model="qwen3.6-plus",
            category="agent", prompt_version=None, tokens_prompt=1, tokens_completion=1,
            cost_estimate=None, latency_ms=5, status="ok",
            user_id="g4-tester", dept_group="production")
        ids = [c1, c2]
        with conn.cursor() as cur:
            cur.execute("SELECT call_id, cost_estimate, price_table_version "
                        "FROM llm_call_log WHERE call_id IN (%s,%s)", (c1, c2))
            rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        assert rows[c1][1] == "2026-07-v1" and rows[c1][0] is not None
        assert rows[c2] == (None, None)
    finally:
        if ids:
            with conn.cursor() as cur:
                ph = ",".join(["%s"] * len(ids))
                cur.execute(f"DELETE FROM llm_call_log WHERE call_id IN ({ph})", ids)
        conn.close()
