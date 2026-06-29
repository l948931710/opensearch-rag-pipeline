# -*- coding: utf-8 -*-
"""
test_concurrency.py — Stage 2/3 并发互斥的真实 MySQL 测试

依赖本地 MySQL（Stage-2 认领用 FOR UPDATE SKIP LOCKED，需 MySQL 8）。通过多线程
模拟两个并发 DataWorks 实例，验证 P0-02 修复的「SELECT ... FOR UPDATE SKIP LOCKED
单步认领」真能让两实例认领到不相交的文档集（旧 UPDATE-then-SELECT 的第二次按状态
SELECT 会读到两实例的并集 → 重复处理）。
"""

import threading
import pytest


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
        """两个线程并发用 SELECT ... FOR UPDATE SKIP LOCKED 认领 LIMIT 5：各自【SELECT 到的】
        doc 集合必须不相交，合计恰好覆盖 10 条。

        这是 P0-02 的核心回归：旧实现 UPDATE 置 LOADING 后再 SELECT WHERE status='LOADING'，
        第二次 SELECT 读到两 worker 的并集 → 重复处理。新实现单步认领，认领到的就是要处理的。"""
        results = {"worker_a": set(), "worker_b": set()}
        barrier = threading.Barrier(2, timeout=10)

        def worker(name):
            conn = _get_real_db_conn()
            try:
                barrier.wait()  # 两个线程同时开始

                with conn.cursor() as cursor:
                    # 单步认领：锁定候选并跳过对方已锁的行（与 orchestrator 同一形态）。
                    cursor.execute("""
                        SELECT dv.doc_id, dv.id
                        FROM document_version dv
                        WHERE content_process_status = 'NOT_STARTED'
                          AND dv.status = 'active'
                          AND dv.canonical_json_key IS NOT NULL
                          AND dv.doc_id LIKE 'concurrency_test_%%'
                        ORDER BY dv.created_at ASC
                        LIMIT 5
                        FOR UPDATE OF dv SKIP LOCKED
                    """)
                    claimed = cursor.fetchall()
                    if claimed:
                        ids = [r[1] for r in claimed]
                        ph = ",".join(["%s"] * len(ids))
                        cursor.execute(
                            f"UPDATE document_version SET content_process_status='LOADING' "
                            f"WHERE id IN ({ph})",
                            ids,
                        )
                    conn.commit()
                    results[name] = {r[0] for r in claimed}   # 本 worker 真正认领到的 doc 集
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("worker_a",))
        t2 = threading.Thread(target=worker, args=("worker_b",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        a, b = results["worker_a"], results["worker_b"]
        # 核心不变量（P0-02）：两实例认领集合【不相交】——绝不重复处理同一文档。
        assert not (a & b), f"两 worker 认领集合重叠（= 会重复处理）: {sorted(a & b)}"
        claimed = a | b
        assert claimed, "至少应有一个 worker 认领到文档"

        # 守恒 + 真实性：认领到的都已置 LOADING，LOADING 行数恰等于并集大小（无双重认领），
        # LOADING + 剩余 NOT_STARTED == 10（SKIP LOCKED 单趟可能把对方锁住的行留给下一轮
        # drain——这是设计行为，drain-loop 会补齐；关键是绝不丢、绝不重复）。
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT content_process_status, COUNT(*) FROM document_version
                    WHERE doc_id LIKE 'concurrency_test_%%'
                    GROUP BY content_process_status
                """)
                counts = {row[0]: row[1] for row in cursor.fetchall()}
        finally:
            conn.close()
        assert counts.get("LOADING", 0) == len(claimed), (
            f"LOADING 行数应等于认领并集 {len(claimed)}, 实际 {counts}"
        )
        assert counts.get("LOADING", 0) + counts.get("NOT_STARTED", 0) == 10, (
            f"认领 + 剩余应守恒为 10（不丢不重复）, 实际 {counts}"
        )


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
