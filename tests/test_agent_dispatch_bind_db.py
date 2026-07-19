# -*- coding: utf-8 -*-
"""R1（RB-RT-01）：agent_run.dispatch_command_id 反向锚的真实 MySQL 并发测试。

20 线程并发为**同一 dispatch 命令**建 run（各自独立 thread_id，排除 uk_thread_active
干扰）——断言恰 1 个赢家、其余 19 个 DispatchCommandBound（uk_run_dispatch_cmd 裁决）。
本地无 MySQL / 无 agent_run 表则 skip；052 列以幂等 ALTER 就地补齐（本地 dev 栈）。
"""
import threading
import uuid

import pytest


def _local_agent_db_ready():
    try:
        import pymysql
        from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
        cfg = get_config()
        if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
            return False
        conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                               password=cfg.rds.password,
                               database=cfg.rds.operation_database, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES LIKE 'agent_run'")
                if cur.fetchone() is None:
                    return False
                # 052 幂等补列（本地 dev 栈；1060/1061=已存在）
                try:
                    cur.execute(
                        "ALTER TABLE agent_run "
                        "ADD COLUMN dispatch_command_id CHAR(32) DEFAULT NULL, "
                        "ADD UNIQUE KEY uk_run_dispatch_cmd (dispatch_command_id)")
                except Exception as e:   # noqa: BLE001
                    if not (getattr(e, "args", ()) and e.args[0] in (1060, 1061)):
                        raise
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:   # noqa: BLE001
        return False


skipif_no_db = pytest.mark.skipif(not _local_agent_db_ready(),
                                  reason="Local MySQL with agent_run not available")


@pytest.fixture()
def _store():
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
    from opensearch_pipeline.db import _get_db_conn
    from opensearch_pipeline.config import get_config
    db = get_config().rds.operation_database

    def _cleanup():
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {db}.agent_run "
                            f"WHERE dispatch_command_id LIKE 'it-r1-%%' "
                            f"OR thread_id LIKE 'it-r1-%%'")
            conn.commit()
        finally:
            conn.close()

    _cleanup()
    yield RDSRunStore()
    _cleanup()


def _mk_ctx(anchor, thread_id):
    from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
    ctx = ExecutionContext.create(
        request_id="r", user_id="it-r1", acl_groups=["g"], roles=["employee"],
        channel="console", thread_id=thread_id, budget=RunBudget(max_turns=2))
    object.__setattr__(ctx, "dispatch_command_id", anchor)
    return ctx


@skipif_no_db
def test_20_concurrent_create_run_same_command_single_winner(_store):
    from opensearch_pipeline.agent_runtime.run_store import DispatchCommandBound
    anchor = "it-r1-" + uuid.uuid4().hex[:20]
    n = 20
    barrier = threading.Barrier(n)
    wins, losses, others = [], [], []
    lock = threading.Lock()

    def _one(i):
        ctx = _mk_ctx(anchor, f"it-r1-t{i}-{uuid.uuid4().hex[:8]}")
        barrier.wait()
        try:
            rid = _store.create_run(ctx, "default")
            with lock:
                wins.append(rid)
        except DispatchCommandBound:
            with lock:
                losses.append(i)
        except Exception as e:   # noqa: BLE001
            with lock:
                others.append(repr(e))

    ts = [threading.Thread(target=_one, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not others, f"意外异常: {others[:3]}"
    assert len(wins) == 1, f"必须唯一赢家，实得 {len(wins)}"
    assert len(losses) == n - 1
    # 赢家可按锚反查（恢复扫描 rebind 的依据）
    found = _store.find_run_by_dispatch_command(anchor)
    assert found and found["run_id"] == wins[0]


@skipif_no_db
def test_anchor_null_runs_unlimited(_store):
    """非 durable-dispatch 的 run（锚 NULL）不受 uk 限制——MySQL UNIQUE 多 NULL 语义。"""
    r1 = _store.create_run(_mk_ctx(None, f"it-r1-n1-{uuid.uuid4().hex[:8]}"), "default")
    r2 = _store.create_run(_mk_ctx(None, f"it-r1-n2-{uuid.uuid4().hex[:8]}"), "default")
    assert r1 and r2 and r1 != r2
