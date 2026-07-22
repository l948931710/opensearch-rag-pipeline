# -*- coding: utf-8 -*-
"""Majors α5(M6)真库双连接穿透契约(2026-07-21 codex 共识;MISSING EVIDENCE 补件)。

cap=1 时两个独立连接同时走「哨兵 FOR UPDATE → COUNT → INSERT」:哨兵锁串行化保证
恰一赢家,输家在锁到手后看到已提交的计数 → ApprovalQuotaExceeded——不靠隐式
gap-lock,不受隔离级别/优化器路径影响。

无本地 MySQL / 缺 058(agent_quota_lock)→ 整体 skip(CI db-integration 从 schema/
全量建库含 058,恒实跑)。host-pin 本地;行清理在 finally。
"""
import os
import threading
import uuid

import pytest

from opensearch_pipeline.config import get_config
from opensearch_pipeline.env_guard import is_prod_target

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _conn():
    import pymysql
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；host {cfg.rds.host!r} 拒绝。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.operation_database,
                           autocommit=False, connect_timeout=5)


def _ready():
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agent_quota_lock "
                        "WHERE lock_name='approval_admission'")
            ok = cur.fetchone()[0] == 1
        c.close()
        return ok
    except Exception:
        return False


_DB_OK = _ready()

if not _DB_OK and os.environ.get("RAG_AGENT_TESTS_REQUIRE_RDS", "").strip() == "1":
    raise RuntimeError("RAG_AGENT_TESTS_REQUIRE_RDS=1 但 agent_quota_lock/哨兵不可用——"
                       "检查 ci_load_schema 是否含 058")

skipif_no_db = pytest.mark.skipif(
    not _DB_OK, reason="本地 MySQL 无 agent_quota_lock 哨兵（先 apply schema/058）")


def _mk_ctx(user):
    from opensearch_pipeline.agent_runtime import ExecutionContext
    return ExecutionContext.create(request_id="m6db", user_id=user,
                                   acl_groups=["production"], roles=["employee"],
                                   channel="console", thread_id="m6t-" + uuid.uuid4().hex[:8])


@skipif_no_db
def test_two_connections_cap_one_exactly_one_winner(monkeypatch):
    from opensearch_pipeline.agent_runtime.approval_store import (
        ApprovalQuotaExceeded, RDSApprovalStore)
    user = "m6-" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("RAG_AGENT_APPROVAL_PENDING_CAP", "1")
    store = RDSApprovalStore()
    results, req_ids = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    call = {"tool_name": "m6_tool", "call_id": "c1", "arguments": {}}

    def _worker(i):
        barrier.wait()
        conn = _conn()
        try:
            conn.begin()
            with conn.cursor() as cur:
                rid = store.insert_request(cur, "m6run-" + uuid.uuid4().hex[:8],
                                           _mk_ctx(user), call)
            conn.commit()
            with lock:
                results.append("ok")
                req_ids.append(rid)
        except ApprovalQuotaExceeded:
            conn.rollback()
            with lock:
                results.append("quota")
        except Exception as e:   # noqa: BLE001
            conn.rollback()
            with lock:
                results.append(f"err:{e!r}")
        finally:
            conn.close()

    ts = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    try:
        assert sorted(results) == ["ok", "quota"], results   # 恰一赢家,零穿透
    finally:
        if req_ids:
            c = _conn()
            with c.cursor() as cur:
                ph = ",".join(["%s"] * len(req_ids))
                cur.execute(f"DELETE FROM approval_request WHERE request_id IN ({ph})",
                            req_ids)
            c.commit()
            c.close()
