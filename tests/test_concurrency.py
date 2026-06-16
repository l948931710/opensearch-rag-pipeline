# -*- coding: utf-8 -*-
"""
test_concurrency.py — Stage 2/3 并发互斥的真实 MySQL 测试

依赖本地 MySQL。通过多线程模拟两个并发 DataWorks 实例，
验证 P0-3 修复的 UPDATE-then-SELECT 原子抢占是否真能防止重复处理。
"""

import os
import threading
import time
import pytest
from unittest.mock import patch


def _get_real_db_conn():
    """获取真实 MySQL 连接（非连接池），用于并发隔离测试。"""
    import pymysql
    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
    config = get_config()
    # 安全闸：本测试跑真实 DELETE/INSERT/UPDATE，绕过 _get_db_conn chokepoint 自建连接，
    # 故在此 host-pin——只允许本地 dev 栈，杜绝 RAG_ENV=prod_ro/staging/production 时把
    # 并发写打到生产 RDS。命中即 raise → _db_available() 捕获 → skipif_no_db 整体 skip。
    if config.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", config.rds.host):
        raise RuntimeError(
            f"[PROD-GUARD] test_concurrency 仅允许本地 MySQL；解析到 host "
            f"{config.rds.host!r}（非本地/命中生产指纹），拒绝连接。"
        )
    return pymysql.connect(
        host=config.rds.host,
        port=config.rds.port,
        user=config.rds.user,
        password=config.rds.password,
        database=config.rds.database,
        autocommit=False,
    )


def _db_available():
    """检查本地 MySQL 是否可用。"""
    try:
        conn = _get_real_db_conn()
        conn.close()
        return True
    except Exception:
        return False


skipif_no_db = pytest.mark.skipif(
    not _db_available(),
    reason="Local MySQL not available"
)


@skipif_no_db
class TestStage2ConcurrentPreemption:
    """
    验证 Stage 2 的 UPDATE ... SET content_process_status='LOADING' LIMIT N
    在两个并发线程中是否能互斥抢占（不会选到同一批文档）。
    """

    def setup_method(self):
        """插入 10 条 NOT_STARTED 测试记录。"""
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cursor:
                # 清除测试数据
                cursor.execute("DELETE FROM document_version WHERE doc_id LIKE 'concurrency_test_%'")
                cursor.execute("DELETE FROM document_meta WHERE doc_id LIKE 'concurrency_test_%'")

                for i in range(10):
                    doc_id = f"concurrency_test_{i:03d}"
                    cursor.execute("""
                        INSERT INTO document_meta (doc_id, title, owner_dept)
                        VALUES (%s, %s, 'test_dept')
                        ON DUPLICATE KEY UPDATE title = VALUES(title)
                    """, (doc_id, f"Test Doc {i}"))
                    cursor.execute("""
                        INSERT INTO document_version (
                            doc_id, version_no, status, content_process_status,
                            canonical_json_key, raw_key, file_ext
                        ) VALUES (%s, 1, 'active', 'NOT_STARTED', %s, %s, 'txt')
                        ON DUPLICATE KEY UPDATE content_process_status = 'NOT_STARTED'
                    """, (doc_id, f"canonical/{doc_id}.json", f"raw/{doc_id}.txt"))
                conn.commit()
        finally:
            conn.close()

    def teardown_method(self):
        """清理测试数据。"""
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM document_version WHERE doc_id LIKE 'concurrency_test_%'")
                cursor.execute("DELETE FROM document_meta WHERE doc_id LIKE 'concurrency_test_%'")
                conn.commit()
        finally:
            conn.close()

    def test_two_workers_never_preempt_same_documents(self):
        """
        两个线程同时执行 UPDATE ... LIMIT 5，结果不应重叠。
        合在一起应该恰好覆盖 10 条记录。
        """
        results = {"worker_a": [], "worker_b": []}
        barrier = threading.Barrier(2, timeout=10)

        def worker(name):
            conn = _get_real_db_conn()
            try:
                barrier.wait()  # 两个线程同时开始

                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE document_version
                        SET content_process_status = 'LOADING'
                        WHERE content_process_status = 'NOT_STARTED'
                          AND status = 'active'
                          AND canonical_json_key IS NOT NULL
                          AND doc_id LIKE 'concurrency_test_%%'
                        ORDER BY created_at ASC
                        LIMIT 5
                    """)
                    preempted = cursor.rowcount
                    conn.commit()

                    cursor.execute("""
                        SELECT doc_id FROM document_version
                        WHERE content_process_status = 'LOADING'
                          AND doc_id LIKE 'concurrency_test_%%'
                    """)
                    # 这里会返回所有 LOADING 的（两个 worker 的总和）
                    # 但 rowcount 告诉我们本 worker 抢了几个
                    results[name] = preempted
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("worker_a",))
        t2 = threading.Thread(target=worker, args=("worker_b",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        total = results["worker_a"] + results["worker_b"]
        assert total == 10, (
            f"两个 worker 应该合计抢占 10 条, 实际: "
            f"worker_a={results['worker_a']}, worker_b={results['worker_b']}"
        )

        # 验证 DB 中没有残留 NOT_STARTED
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT COUNT(*) FROM document_version
                    WHERE doc_id LIKE 'concurrency_test_%%'
                      AND content_process_status = 'NOT_STARTED'
                """)
                remaining = cursor.fetchone()[0]
                assert remaining == 0, f"应该没有 NOT_STARTED 残留, 实际: {remaining}"
        finally:
            conn.close()


@skipif_no_db
class TestStage3IndexLockConcurrency:
    """
    验证 Stage 3 的乐观锁 UPDATE ... WHERE index_status IN ('NOT_INDEXED', 'FAILED')
    在两个并发线程中是否能互斥（只有一个成功 rowcount > 0）。
    """

    def setup_method(self):
        """插入 1 条 NOT_INDEXED 测试记录。"""
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cursor:
                doc_id = "lock_test_doc"
                cursor.execute("DELETE FROM document_version WHERE doc_id = %s", (doc_id,))
                cursor.execute("DELETE FROM document_meta WHERE doc_id = %s", (doc_id,))
                cursor.execute("""
                    INSERT INTO document_meta (doc_id, title, owner_dept)
                    VALUES (%s, 'Lock Test', 'test_dept')
                    ON DUPLICATE KEY UPDATE title = 'Lock Test'
                """, (doc_id,))
                cursor.execute("""
                    INSERT INTO document_version (
                        doc_id, version_no, status, index_status,
                        content_process_status, canonical_json_key, raw_key, file_ext
                    ) VALUES (%s, 1, 'active', 'NOT_INDEXED', 'DONE', 'test.json', 'test.txt', 'txt')
                    ON DUPLICATE KEY UPDATE index_status = 'NOT_INDEXED'
                """, (doc_id,))
                conn.commit()
        finally:
            conn.close()

    def teardown_method(self):
        """清理。"""
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM document_version WHERE doc_id = 'lock_test_doc'")
                cursor.execute("DELETE FROM document_meta WHERE doc_id = 'lock_test_doc'")
                conn.commit()
        finally:
            conn.close()

    def test_only_one_worker_acquires_index_lock(self):
        """
        两个线程同时对同一条记录执行 UPDATE SET index_status='PROCESSING'
        WHERE index_status IN ('NOT_INDEXED', 'FAILED')。
        只有一个线程的 rowcount > 0。
        """
        results = {"worker_a": None, "worker_b": None}
        barrier = threading.Barrier(2, timeout=10)

        def worker(name):
            conn = _get_real_db_conn()
            try:
                barrier.wait()

                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE document_version
                        SET index_status = 'PROCESSING'
                        WHERE doc_id = 'lock_test_doc'
                          AND version_no = 1
                          AND index_status IN ('NOT_INDEXED', 'FAILED')
                    """)
                    results[name] = cursor.rowcount
                    conn.commit()
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("worker_a",))
        t2 = threading.Thread(target=worker, args=("worker_b",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        success_count = sum(1 for v in results.values() if v > 0)
        assert success_count == 1, (
            f"只有一个 worker 应该成功抢锁, 实际: "
            f"worker_a={results['worker_a']}, worker_b={results['worker_b']}"
        )

        # 验证 DB 状态
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT index_status FROM document_version
                    WHERE doc_id = 'lock_test_doc' AND version_no = 1
                """)
                status = cursor.fetchone()[0]
                assert status == "PROCESSING"
        finally:
            conn.close()
