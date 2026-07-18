# -*- coding: utf-8 -*-
"""P2-04b（评审 closure 5）：dingtalk_msg_dedup RDS 认领的真实 MySQL 并发测试。

20 线程并发认领同一 msgId，断言**严格 1 个赢家**——覆盖单测 fake cursor 覆盖不了的
真实竞态面（InnoDB 行锁 + ODKU rowcount 语义 + 条件接管 WHERE 重评估）。
依赖本地 MySQL 8（host-pin 安全闸同 test_concurrency：非本地栈一律 skip）。
"""
import threading

import pytest


def _local_db_ok():
    """本地 MySQL 可用性 + host-pin（拒绝把并发写打到远端）。"""
    try:
        import pymysql
        from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
        cfg = get_config()
        if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
            return False
        conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                               password=cfg.rds.password, connect_timeout=3)
        conn.close()
        return True
    except Exception:   # noqa: BLE001
        return False


skipif_no_db = pytest.mark.skipif(not _local_db_ok(), reason="Local MySQL not available")


@pytest.fixture()
def _rds_wired(monkeypatch):
    """接通真库：兜底层强制开 + 建表（051 DDL 幂等）+ 用后清理。"""
    from opensearch_pipeline import dingtalk_bot as bot
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.db import _get_db_conn

    monkeypatch.setattr(bot, "_rds_dedup_on", lambda: True)
    monkeypatch.setattr(bot, "_RDS_DEDUP_MISSING_UNTIL", 0.0)
    db = get_config().rds.operation_database
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {db}.dingtalk_msg_dedup ("
                "  msg_id VARCHAR(128) NOT NULL PRIMARY KEY,"
                "  state VARCHAR(24) NOT NULL DEFAULT 'processing',"
                "  attempts INT NOT NULL DEFAULT 1,"
                "  message_id VARCHAR(64) DEFAULT NULL,"
                "  created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),"
                "  updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)"
                "    ON UPDATE CURRENT_TIMESTAMP(3),"
                "  KEY idx_msg_dedup_updated (updated_at)"
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
            cur.execute(f"DELETE FROM {db}.dingtalk_msg_dedup "
                        f"WHERE msg_id LIKE 'it-p204b-%%'")
        conn.commit()
    finally:
        conn.close()
    yield bot, db
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {db}.dingtalk_msg_dedup "
                        f"WHERE msg_id LIKE 'it-p204b-%%'")
        conn.commit()
    finally:
        conn.close()


def _race(bot, msg_id, n=20):
    """n 线程同时认领同一 msgId，返回 claimed=True 的数量。"""
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def _one():
        barrier.wait()
        claimed, _meta = bot._rds_dedup_claim(msg_id)
        with lock:
            results.append(claimed)

    ts = [threading.Thread(target=_one) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    return sum(1 for r in results if r), len(results)


@skipif_no_db
def test_20_concurrent_claims_exactly_one_winner(_rds_wired):
    bot, _db = _rds_wired
    winners, total = _race(bot, "it-p204b-fresh")
    assert total == 20
    assert winners == 1, f"并发认领必须唯一赢家，实得 {winners}"


@skipif_no_db
def test_20_concurrent_stale_takeovers_exactly_one_winner(_rds_wired):
    """陈旧行接管同样唯一赢家（条件 UPDATE 的 WHERE 在行锁后重评估）。"""
    from opensearch_pipeline.db import _get_db_conn
    bot, db = _rds_wired
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {db}.dingtalk_msg_dedup "
                f"(msg_id, state, attempts, updated_at) "
                f"VALUES ('it-p204b-stale', 'processing', 1, "
                f"NOW(3) - INTERVAL %s SECOND)",
                (bot._TIMESTAMP_TOLERANCE + 300,))
        conn.commit()
    finally:
        conn.close()
    winners, total = _race(bot, "it-p204b-stale")
    assert total == 20
    assert winners == 1, f"陈旧接管必须唯一赢家，实得 {winners}"


@skipif_no_db
def test_winner_then_all_losers_see_fresh_meta(_rds_wired):
    bot, _db = _rds_wired
    assert bot._rds_dedup_claim("it-p204b-meta") == (True, None)
    bot._rds_dedup_mark("it-p204b-meta", "retryable_failed", att=2, mid="MID-9")
    claimed, meta = bot._rds_dedup_claim("it-p204b-meta")
    assert claimed is False
    assert meta["st"] == "retryable_failed" and meta["mid"] == "MID-9"
