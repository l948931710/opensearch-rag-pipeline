# -*- coding: utf-8 -*-
"""
tests/test_reconcile_races.py — F3（2026-07-21 评审共识）对账竞态根治回归测试

覆盖三层：
  1. reconcile_pending_deletes 新流程单元测试（meta 锁、锁内重验、chunk→dv 写序、
     CAS 异常回滚、bounded 客户端 fail-closed、deadline 钳位）；
  2. HA3 SDK 语义钉死（max_attempts=0 = 恰一次尝试；bounded 客户端生效值断言）；
  3. 真库双连接锁序测试（reconciler/quarantine/stage-3 三两配对不死锁）——
     本模块因此进 conftest 的 local-db-stack 串行组。
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from opensearch_pipeline.spot_checker import (
    _delete_chunks_from_index,
    _reconcile_ha3_deadline_s,
    reconcile_pending_deletes,
)


def _make_conn(fetchall_results, fetchone_results=None, rowcount=1):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.side_effect = list(fetchall_results)
    if fetchone_results is not None:
        cursor.fetchone.side_effect = list(fetchone_results)
    cursor.rowcount = rowcount
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


def _ha3_client(status_code=200):
    client = MagicMock(spec=["push_documents"])
    resp = MagicMock()
    resp.status_code = status_code
    resp.body = ""
    resp.text = ""
    client.push_documents.return_value = resp
    return client


# ── 1) reconcile_pending_deletes 新流程 ──────────────────────────────────────


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_pending_delete_lock_reverify_skips_restored(mock_get_db_conn, mock_get_client):
    """锁内重验发现已被并发恢复（非 PENDING_DELETE）→ 零 HA3 删除、零 RDS 写、计 skipped_stale。"""
    conn, cursor = _make_conn(
        [[("doc1", 1)]],
        fetchone_results=[("doc1",), ("NOT_INDEXED",)],
    )
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_pending_deletes()

    assert result["skipped_stale"] == 1
    assert result["success"] == 0 and result["failed"] == 0
    client.push_documents.assert_not_called()
    assert not any(c[0][0].lstrip().startswith("UPDATE")
                   for c in cursor.execute.call_args_list), "跳过路径绝无 RDS 写"
    conn.commit.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_pending_delete_success_flow_lock_and_write_order(mock_get_db_conn, mock_get_client):
    """成功路径写序（F3 锁序纪律）：meta FOR UPDATE → 锁内重验 → HA3 删除 →
    chunk 停用【先于】dv CAS → commit。"""
    conn, cursor = _make_conn(
        [[("doc1", 1)], [(11,), (12,)]],
        fetchone_results=[("doc1",), ("PENDING_DELETE",)],
    )
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_pending_deletes()

    assert result["success"] == 1
    assert result["failed"] == 0 and result["skipped_stale"] == 0
    push_body = client.push_documents.call_args[0][2].body
    assert {"cmd": "delete", "fields": {"id": 11}} in push_body

    sqls = [c[0][0] for c in cursor.execute.call_args_list]
    i_meta = next(i for i, s in enumerate(sqls) if "document_meta" in s and "FOR UPDATE" in s)
    i_chunk = next(i for i, s in enumerate(sqls) if "UPDATE chunk_meta" in s)
    i_dv = next(i for i, s in enumerate(sqls) if "UPDATE document_version" in s)
    assert i_meta < i_chunk < i_dv, "锁序必须 meta → chunk → dv（与 stage-3 的 chunk→dv 兼容）"
    # dv CAS 必须仍然带 PENDING_DELETE 前置态（belt）
    assert "index_status = 'PENDING_DELETE'" in sqls[i_dv]
    conn.commit.assert_called_once()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_pending_delete_cas_anomaly_rolls_back_chunk_writes(mock_get_db_conn, mock_get_client):
    """meta 锁下版本 CAS 落空 = 未建模写方（异常）：整笔回滚（chunk 停用一并撤销）、
    按 failed+errors 上报、绝不 commit。"""
    conn, cursor = _make_conn(
        [[("doc1", 1)], [(11,)]],
        fetchone_results=[("doc1",), ("PENDING_DELETE",), ("NOT_INDEXED",)],
        rowcount=0,   # CAS 落空
    )
    mock_get_db_conn.return_value = conn
    mock_get_client.return_value = _ha3_client()

    result = reconcile_pending_deletes()

    assert result["failed"] == 1
    assert result["success"] == 0 and result["skipped_stale"] == 0
    assert any("UNDER document_meta lock" in e for e in result["errors"])
    conn.rollback.assert_called()
    conn.commit.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_pending_delete_capability_failure_fail_closed(mock_get_db_conn, monkeypatch):
    """bounded 客户端能力缺失 → 整批 fail-closed：全部按 failed 计、零 HA3/RDS 写、
    行保持 PENDING_DELETE 可重试（绝不回退无界老路径）。"""
    from opensearch_pipeline import spot_checker
    conn, cursor = _make_conn([[("doc1", 1), ("doc2", 1)]])
    mock_get_db_conn.return_value = conn
    monkeypatch.setattr(
        spot_checker, "_get_bounded_search_client",
        lambda: (_ for _ in ()).throw(RuntimeError("runtime options not effective")))

    result = reconcile_pending_deletes()

    assert result["failed"] == 2 and result["total"] == 2
    assert result["success"] == 0
    assert any("fail-closed" in e for e in result["errors"])
    assert not any("UPDATE" in c[0][0] for c in cursor.execute.call_args_list)
    conn.commit.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_stranded_capability_failure_fail_closed(mock_get_db_conn, monkeypatch):
    """A3 同款 fail-closed：需要删除的候选停留原搁浅态，零写。

    B8（2026-07-25）：bounded 客户端改为**按需惰性获取**（RDS-only 分支根本不删 HA3，
    不该被整批拒跑），所以本用例的夹具必须真正走到删除分支 —— 候选要有更旧的 active chunk。
    """
    from opensearch_pipeline import spot_checker
    # fetchall 脚本：① 候选行 ② 该 doc 全部 active chunk（当前版本 INDEXED + 一条更旧的）
    conn, cursor = _make_conn(
        [[("doc1", 2)], [(11, 2, "INDEXED"), (7, 1, "INDEXED")]],
        # fetchone 脚本：_lock_doc 的 doc_id 行 + 版本行观测（index_status/updated_at/
        # status/publish_status/proc_stale_ok）
        fetchone_results=[("doc1",), ("PROCESSING", "2026-07-25 00:00:00", "active", None, 1)],
    )
    mock_get_db_conn.return_value = conn
    monkeypatch.setattr(
        spot_checker, "_get_bounded_search_client",
        lambda: (_ for _ in ()).throw(RuntimeError("runtime options not effective")))

    result = spot_checker.reconcile_stranded_versions()

    assert result["failed"] == 1 and result["total"] == 1
    assert any("fail-closed" in e for e in result["errors"])
    # 零写入。判据收紧为「语句以 UPDATE 开头」——惰性取客户端后，发现不可用之前会先取
    # 行锁（`SELECT ... FOR UPDATE`），那是读锁不是写；真正的零写证据是 commit 未被调用。
    assert not any(" ".join(str(c[0][0]).split()).upper().startswith("UPDATE")
                   for c in cursor.execute.call_args_list)
    conn.commit.assert_not_called()


# ── 2) deadline 钳位 + HA3 SDK 语义钉死 ──────────────────────────────────────


def test_deadline_clamp(monkeypatch):
    """RAG_RECONCILE_HA3_DEADLINE_S 钳位 [5,25] 默认 20——不允许配置出破坏 50s 锁上界的值。"""
    monkeypatch.delenv("RAG_RECONCILE_HA3_DEADLINE_S", raising=False)
    assert _reconcile_ha3_deadline_s() == 20.0
    monkeypatch.setenv("RAG_RECONCILE_HA3_DEADLINE_S", "120")
    assert _reconcile_ha3_deadline_s() == 25.0
    monkeypatch.setenv("RAG_RECONCILE_HA3_DEADLINE_S", "1")
    assert _reconcile_ha3_deadline_s() == 5.0
    monkeypatch.setenv("RAG_RECONCILE_HA3_DEADLINE_S", "garbage")
    assert _reconcile_ha3_deadline_s() == 20.0


def test_delete_chunks_deadline_exceeded_before_any_push():
    """deadline 已过 → 批间检查在第一批之前就抛，零 HA3 调用（调用方回滚重试）。"""
    conn, cursor = _make_conn([[(11,), (12,)]])
    client = _ha3_client()
    from opensearch_pipeline.config import get_config
    with pytest.raises(RuntimeError, match="deadline exceeded"):
        _delete_chunks_from_index("doc1", 1, conn, get_config(),
                                  client=client, deadline_ts=time.monotonic() - 1)
    client.push_documents.assert_not_called()


def test_search_delete_old_chunks_deadline_exceeded():
    """_search_delete_old_chunks 的 deadline 前置检查（stranded healer 路径）。"""
    from opensearch_pipeline.pipeline_nodes import _search_delete_old_chunks
    from opensearch_pipeline.config import get_config
    client = _ha3_client()
    with pytest.raises(RuntimeError, match="deadline exceeded"):
        _search_delete_old_chunks(client, get_config(), "idx", "doc1", 2, [11],
                                  deadline_ts=time.monotonic() - 1)
    client.push_documents.assert_not_called()


def test_tea_max_attempts_zero_means_single_attempt():
    """钉死 SDK 语义（Codex r6 实证）：Tea allow_retry 在 maxAttempts=0 时拒绝第二次
    尝试（retry_times=1 → False），而 SDK 默认 maxAttempts=2 允许重试——bounded 客户端
    必须用 0。SDK 升级破坏此语义时本测试红灯。"""
    from Tea.core import TeaCore
    # 首次尝试（retry_times=0）恒放行
    assert TeaCore.allow_retry({"retryable": True, "maxAttempts": 0}, 0) is True
    # maxAttempts=0 → 不允许任何重试
    assert TeaCore.allow_retry({"retryable": True, "maxAttempts": 0}, 1) is False
    # 对照：默认 maxAttempts=2 会放行第二次尝试（这正是要关掉的行为）
    assert TeaCore.allow_retry({"retryable": True, "maxAttempts": 2}, 1) is True


def test_bounded_client_effective_options_asserted(monkeypatch):
    """clients.py bounded=True：构造后核验客户端【生效】runtime options；SDK 形态变化
    吞掉选项时必须 RuntimeError（fail-closed），绝不静默无界。"""
    import sys
    from types import SimpleNamespace
    from opensearch_pipeline import clients as clients_mod
    import opensearch_pipeline.config as config_mod

    # xdist 同 worker 前序模块（test_ha3_engine/test_reconcile）可能向 sys.modules 注入
    # 无 __file__ 的 SDK 桩——桩上跑"真 SDK 存储生效值"正例无意义，跳过（targeted/
    # 干净 worker 照常验证；生产侧的能力断言本身就是运行时守卫）。
    _ha3_client_mod = sys.modules.get("alibabacloud_ha3engine_vector.client")
    if _ha3_client_mod is not None and not getattr(_ha3_client_mod, "__file__", None):
        pytest.skip("HA3 SDK 已被其他测试模块 stub（无 __file__）——正例断言无意义")

    fake_cfg = SimpleNamespace(
        simulate_opensearch=False,
        alibaba_vector=SimpleNamespace(
            endpoint="http://fake-ha3.local", instance_id="inst",
            access_user_name="u", access_pass_word="p"),
        opensearch=SimpleNamespace(host="localhost", port=9200, auth_user="",
                                   auth_password="", use_ssl=False, verify_certs=False),
    )
    monkeypatch.setattr(config_mod, "get_config", lambda: fake_cfg)

    # 正例：真 SDK Client 构造（无网络 I/O），生效值必须 max_attempts=0/5s/10s
    client = clients_mod._get_opensearch_client(bounded=True)
    ro = client._runtime_options
    assert ro.max_attempts == 0 and ro.connect_timeout == 5000 and ro.read_timeout == 10000

    # 反例：SDK 形态变化（_runtime_options 丢失）→ RuntimeError fail-closed
    import alibabacloud_ha3engine_vector.client as ha3_client_mod

    class _BrokenClient:
        def __init__(self, config):
            self._runtime_options = None

    monkeypatch.setattr(ha3_client_mod, "Client", _BrokenClient)
    with pytest.raises(RuntimeError, match="bounded runtime options"):
        clients_mod._get_opensearch_client(bounded=True)


# ── 3) 真库双连接锁序测试（local-db-stack 串行组）────────────────────────────


def _get_real_db_conn():
    """真实本地 MySQL 连接（同 test_concurrency 的 host-pin 安全闸）。"""
    import pymysql
    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
    config = get_config()
    if config.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", config.rds.host):
        raise RuntimeError("[PROD-GUARD] 锁序测试仅允许本地 MySQL")
    return pymysql.connect(
        host=config.rds.host, port=config.rds.port, user=config.rds.user,
        password=config.rds.password, database=config.rds.database, autocommit=False,
    )


def _db_available():
    try:
        conn = _get_real_db_conn()
        conn.close()
        return True
    except Exception:
        return False


skipif_no_db = pytest.mark.skipif(not _db_available(), reason="Local MySQL not available")

_DOC = "f3_lockorder_testdoc"

# 各写方的锁获取序（真实 SQL 骨架；同值 UPDATE 也照常取 X 锁）
_ORDER_RECONCILER = [
    ("SELECT doc_id FROM document_meta WHERE doc_id = %s FOR UPDATE", ()),
    ("UPDATE chunk_meta SET is_active = is_active WHERE doc_id = %s AND version_no = 1", ()),
    ("UPDATE document_version SET updated_at = updated_at WHERE doc_id = %s AND version_no = 1", ()),
]
_ORDER_QUARANTINE = [
    ("UPDATE document_meta SET kb_type = kb_type WHERE doc_id = %s", ()),
    ("UPDATE chunk_meta SET is_active = is_active WHERE doc_id = %s AND version_no = 1", ()),
    ("UPDATE document_version SET updated_at = updated_at WHERE doc_id = %s AND version_no = 1", ()),
]
_ORDER_STAGE3 = [   # stage-3 终态化：chunk（旧版本 bulk）→ dv（supersede/终态）；不取 meta
    ("UPDATE chunk_meta SET is_active = is_active WHERE doc_id = %s AND version_no = 1", ()),
    ("UPDATE document_version SET updated_at = updated_at WHERE doc_id = %s AND version_no = 1", ()),
]
_ORDER_COST_BREAKER = [   # 归一后：meta → dv
    ("UPDATE document_meta SET kb_type = kb_type WHERE doc_id = %s", ()),
    ("UPDATE document_version SET updated_at = updated_at WHERE doc_id = %s AND version_no = 1", ()),
]


@skipif_no_db
class TestLockOrderNoDeadlock:
    """双连接并发跑两写方的锁获取序，断言无 ERROR 1213（死锁）。

    每对写方循环多轮、彼此错峰 sleep，给交错留窗口；1205（锁等待超时）在 3s 窗口内
    同样按失败计——归一后的锁序应当只产生【短暂串行等待】。"""

    @pytest.fixture(autouse=True)
    def _seed_doc(self):
        conn = _get_real_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chunk_meta WHERE doc_id = %s", (_DOC,))
                cur.execute("DELETE FROM document_version WHERE doc_id = %s", (_DOC,))
                cur.execute("DELETE FROM document_meta WHERE doc_id = %s", (_DOC,))
                cur.execute(
                    "INSERT INTO document_meta (doc_id, title, status, current_version_no) "
                    "VALUES (%s, 'F3 锁序测试', 'active', 1)", (_DOC,))
                cur.execute(
                    "INSERT INTO document_version (doc_id, version_no, status, index_status) "
                    "VALUES (%s, 1, 'active', 'SUCCESS')", (_DOC,))
                cur.execute(
                    "INSERT INTO chunk_meta (chunk_id, doc_id, version_no, chunk_index, "
                    "chunk_text, is_active, index_status) "
                    "VALUES (%s, %s, 1, 0, 'F3 锁序测试块', 1, 'INDEXED')",
                    (f"{_DOC}_c0", _DOC))
            conn.commit()
            yield
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM chunk_meta WHERE doc_id = %s", (_DOC,))
                    cur.execute("DELETE FROM document_version WHERE doc_id = %s", (_DOC,))
                    cur.execute("DELETE FROM document_meta WHERE doc_id = %s", (_DOC,))
                conn.commit()
            finally:
                conn.close()

    def _run_pair(self, order_a, order_b, rounds=3):
        errors = []

        def worker(order, start_delay):
            conn = _get_real_db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SET SESSION innodb_lock_wait_timeout = 3")
                for _ in range(rounds):
                    time.sleep(start_delay)
                    try:
                        with conn.cursor() as cur:
                            for sql, _extra in order:
                                cur.execute(sql, (_DOC,))
                                time.sleep(0.15)   # 持锁窗口，给对方交错机会
                        conn.commit()
                    except Exception as e:  # noqa: BLE001
                        conn.rollback()
                        errors.append(e)
            finally:
                conn.close()

        ta = threading.Thread(target=worker, args=(order_a, 0.0))
        tb = threading.Thread(target=worker, args=(order_b, 0.07))
        ta.start(); tb.start()
        ta.join(timeout=30); tb.join(timeout=30)
        deadlocks = [e for e in errors if getattr(e, "args", [None])[0] == 1213]
        assert not deadlocks, f"死锁（ERROR 1213）：{deadlocks}"
        assert not errors, f"锁等待超时/其他错误（归一后应只有短暂串行等待）：{errors}"

    def test_reconciler_vs_stage3(self):
        self._run_pair(_ORDER_RECONCILER, _ORDER_STAGE3)

    def test_reconciler_vs_quarantine(self):
        self._run_pair(_ORDER_RECONCILER, _ORDER_QUARANTINE)

    def test_quarantine_vs_stage3(self):
        """回归：旧 quarantine（dv→meta→chunk）对 stage-3（chunk→dv）存在潜在环——
        归一到 meta→chunk→dv 后不复存在。"""
        self._run_pair(_ORDER_QUARANTINE, _ORDER_STAGE3)

    def test_cost_breaker_vs_reconciler(self):
        self._run_pair(_ORDER_COST_BREAKER, _ORDER_RECONCILER)


# ── 4) 写方源码锁序护栏（无 DB 也跑）────────────────────────────────────────


def test_quarantine_write_order_meta_first():
    """spot_checker 隔离 Phase-2 事务：meta → chunk → dv（源码序巡检）。"""
    import inspect
    from opensearch_pipeline import spot_checker
    src = inspect.getsource(spot_checker.run_spot_check_pipeline)
    i_meta = src.index("UPDATE document_meta")
    i_chunk = src.index("UPDATE chunk_meta")
    i_dv = src.index("SET risk_level = 'high'")
    assert i_meta < i_chunk < i_dv, "隔离事务写序必须 meta → chunk → dv"


def test_cost_breaker_write_order_meta_first():
    import inspect
    from opensearch_pipeline.extraction import cost_breaker
    src = inspect.getsource(cost_breaker.quarantine_for_cost)
    assert src.index("UPDATE document_meta") < src.index("UPDATE document_version"), (
        "cost_breaker 隔离事务写序必须 meta → dv")


def test_duplicate_skip_write_order_meta_first():
    import inspect
    from opensearch_pipeline import pipeline_nodes
    src = inspect.getsource(pipeline_nodes.node_build_canonical)
    i_meta = src.index("UPDATE document_meta SET current_version_no")
    i_dv = src.index("UPDATE document_version SET content_process_status='SKIPPED_DUPLICATE'")
    assert i_meta < i_dv, "duplicate-skip 写序必须 meta → dv"
