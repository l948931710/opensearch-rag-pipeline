# -*- coding: utf-8 -*-
"""Majors α3(M4)真库并发契约(2026-07-21 codex 共识;MISSING EVIDENCE 补件)。

- 同 (user_id, client_request_id) 双 create_run → 恰一赢家,输家 ClientRequestBound;
- 并发 8 线程同键异 thread → 恰一 run 落库(uk_run_client_req DB 裁决);
- find_run_by_client_request 回读赢家。

无本地 MySQL / agent_run 缺 054 列 → 整体 skip(本地未 apply 054 属预期;CI
db-integration 从 schema/ 全量建库含 054,恒实跑)。host-pin 本地,绝不写 staging/prod。
"""
import os
import threading
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
                        "AND column_name IN ('client_request_id','question_digest')")
            ok = cur.fetchone()[0] == 2
        conn.close()
        return ok
    except Exception:
        return False


_DB_OK = _db_ready_with_054()

if not _DB_OK and os.environ.get("RAG_AGENT_TESTS_REQUIRE_RDS", "").strip() == "1":
    raise RuntimeError(
        "RAG_AGENT_TESTS_REQUIRE_RDS=1 但本地 MySQL 不可用或 agent_run 缺 054 列——"
        "真库契约族不许静默 skip；检查 ci_load_schema 是否含 054")

skipif_no_db = pytest.mark.skipif(
    not _DB_OK, reason="本地 MySQL 无 agent_run.client_request_id（先 apply schema/054）")


def _ctx(thread_id: str, client_key: str):
    from opensearch_pipeline.agent_runtime import ExecutionContext
    ctx = ExecutionContext.create(request_id="m4db", user_id="m4-tester",
                                  acl_groups=["production"], roles=["employee"],
                                  channel="console", thread_id=thread_id)
    object.__setattr__(ctx, "client_request_id", client_key)
    object.__setattr__(ctx, "question_digest", "d" * 64)
    return ctx


def _cleanup(run_ids):
    if not run_ids:
        return
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            ph = ",".join(["%s"] * len(run_ids))
            cur.execute(f"DELETE FROM agent_run WHERE run_id IN ({ph})", list(run_ids))
    finally:
        conn.close()


@skipif_no_db
def test_same_key_second_create_raises_and_find_returns_winner():
    from opensearch_pipeline.agent_runtime.run_store import ClientRequestBound, RDSRunStore
    store = RDSRunStore()
    key = "m4-" + uuid.uuid4().hex[:16]
    created = []
    try:
        r1 = store.create_run(_ctx(f"m4t-{uuid.uuid4().hex[:8]}", key), "default")
        created.append(r1)
        with pytest.raises(ClientRequestBound):
            r2 = store.create_run(_ctx(f"m4t-{uuid.uuid4().hex[:8]}", key), "default")
            created.append(r2)   # pragma: no cover — 不应到达
        found = store.find_run_by_client_request("m4-tester", key)
        assert found is not None and found["run_id"] == r1
        assert found["question_digest"] == "d" * 64
    finally:
        _cleanup(created)


@skipif_no_db
def test_concurrent_same_key_exactly_one_winner():
    from opensearch_pipeline.agent_runtime.run_store import ClientRequestBound, RDSRunStore
    store = RDSRunStore()
    key = "m4c-" + uuid.uuid4().hex[:16]
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def _worker(i):
        barrier.wait()
        try:
            rid = store.create_run(_ctx(f"m4c-{key}-{i}", key), "default")
            with lock:
                results.append(("ok", rid))
        except ClientRequestBound:
            with lock:
                results.append(("bound", None))
        except Exception as e:   # noqa: BLE001 — 其它异常=测试失败面
            with lock:
                results.append(("err", repr(e)))

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    winners = [r for r in results if r[0] == "ok"]
    try:
        assert len(results) == 8, results
        assert len(winners) == 1, results
        assert all(r[0] in ("ok", "bound") for r in results), results
        found = store.find_run_by_client_request("m4-tester", key)
        assert found is not None and found["run_id"] == winners[0][1]
    finally:
        _cleanup([r[1] for r in winners])
