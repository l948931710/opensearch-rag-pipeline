# -*- coding: utf-8 -*-
"""
tests/test_reconcile_stranded.py — 搁浅版本对账（reconcile_stranded_versions）回归测试

被修的事故：DAG 3 部分失败时 node_update_index_status raise → 停用节点被跳过，
orchestrator 回滚把同跑内全量 INDEXED 的文档也标 FAILED；其 chunk_meta 已是 INDEXED，
stage-3 loader 永远不会再选中它们 → 新旧两个版本同时被检索，且无任何任务能自愈。
"""

import inspect
from unittest.mock import MagicMock, patch

from opensearch_pipeline.spot_checker import reconcile_stranded_versions


def _make_conn(fetchall_results, fetchone_results=None, rowcount=1):
    """MagicMock 连接：cursor.fetchall / fetchone 依次返回给定序列。

    F3 后的调用序（healthy heal 一条 (doc1, v2)，观测态 FAILED）：
      fetchall: ①提示扫描 ②doc 全量 active chunk FOR UPDATE 锁读
      fetchone: ①_lock_doc（document_meta 行） ②版本行重验五元组
      rowcount: 版本收尾 CAS 的 changed-rows（1=胜出）
    """
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.side_effect = list(fetchall_results)
    if fetchone_results is not None:
        cursor.fetchone.side_effect = list(fetchone_results)
    cursor.rowcount = rowcount
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


# 版本行重验五元组：(index_status, updated_at, status, publish_status, proc_stale_ok)
_VROW_FAILED = ("FAILED", "2026-07-21 00:00:00", "active", None, 1)
# chunk 锁读三元组：(id, version_no, index_status)——旧 v1 两条 + 新 v2 全量 INDEXED
_CHUNKS_OK = [(101, 1, "INDEXED"), (102, 1, "INDEXED"), (201, 2, "INDEXED")]


def _ha3_client(status_code=200):
    client = MagicMock(spec=["push_documents"])
    resp = MagicMock()
    resp.status_code = status_code
    resp.body = ""
    resp.text = ""
    client.push_documents.return_value = resp
    return client


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_detects_and_heals_strandee(mock_get_db_conn, mock_get_client):
    """命中一条搁浅文档：meta 锁 + 锁内重验 → HA3 按整型 chunk_meta.id 删除 →
    停用旧 chunk → dv 精确态 CAS 修 SUCCESS → supersede → commit。"""
    conn, cursor = _make_conn(
        [[("doc1", 2)], _CHUNKS_OK],
        fetchone_results=[("doc1",), _VROW_FAILED],
    )
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_stranded_versions()

    assert result["total"] == 1
    assert result["success"] == 1
    assert result["failed"] == 0
    assert result["skipped_stale"] == 0

    # HA3 删除必须用 chunk_meta.id（整型主键），cmd=delete，且只删旧版本行
    push_body = client.push_documents.call_args[0][2].body
    assert {"cmd": "delete", "fields": {"id": 101}} in push_body
    assert {"cmd": "delete", "fields": {"id": 102}} in push_body
    assert not any(d["fields"]["id"] == 201 for d in push_body), "当前版本 chunk 绝不能删"

    sqls = [c[0][0] for c in cursor.execute.call_args_list]
    assert any("FOR UPDATE" in s and "document_meta" in s for s in sqls), (
        "必须先取 document_meta 行锁（F3 串行化原语）"
    )
    assert any("is_active = FALSE" in s and "version_no < %s" in s for s in sqls), (
        "旧版本 chunk 必须停用（is_active=FALSE）"
    )
    assert any("SET index_status = 'SUCCESS'" in s and "index_status = %s" in s for s in sqls), (
        "版本收尾必须是精确观测态 CAS（非无条件覆写）"
    )
    conn.commit.assert_called_once()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_search_delete_failure_leaves_rds_untouched(mock_get_db_conn, mock_get_client):
    """索引删除失败 → RDS 不动（保持可检测、下次重试）、rollback、不向外抛异常。"""
    conn, cursor = _make_conn(
        [[("doc1", 2)], _CHUNKS_OK],
        fetchone_results=[("doc1",), _VROW_FAILED],
    )
    mock_get_db_conn.return_value = conn
    client = MagicMock(spec=["push_documents"])
    client.push_documents.side_effect = Exception("HA3 unreachable")
    mock_get_client.return_value = client

    result = reconcile_stranded_versions()  # 必须不 raise

    assert result["total"] == 1
    assert result["success"] == 0
    assert result["failed"] == 1
    for call in cursor.execute.call_args_list:
        sql = call[0][0]
        assert "is_active = FALSE" not in sql, "索引删除失败时绝不能停用 RDS 旧版本"
        assert "SET index_status = 'SUCCESS'" not in sql
    conn.rollback.assert_called()
    conn.commit.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_mock_client_rejected(mock_get_db_conn, mock_get_client):
    """simulate 错配返回 mock 字符串时：该文档按失败计，绝无 RDS 写入。"""
    conn, cursor = _make_conn(
        [[("doc1", 2)], _CHUNKS_OK],
        fetchone_results=[("doc1",), _VROW_FAILED],
    )
    mock_get_db_conn.return_value = conn
    mock_get_client.return_value = "MOCK_HA3_CLIENT"

    result = reconcile_stranded_versions()

    assert result["failed"] == 1
    assert any("MOCK_HA3_CLIENT" in e for e in result["errors"])
    for call in cursor.execute.call_args_list:
        assert "is_active = FALSE" not in call[0][0]
    conn.commit.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_lock_reverify_fresh_processing_skips_before_ha3(mock_get_db_conn, mock_get_client):
    """F3：等 meta 锁期间 stage-3 装入了【新鲜】PROCESSING → 锁内重验（proc_stale_ok=0）
    必须在任何 HA3 删除之前放弃该候选，计 skipped_stale。"""
    vrow_fresh_processing = ("PROCESSING", "2026-07-21 08:00:00", "active", None, 0)
    conn, cursor = _make_conn(
        [[("doc1", 2)]],
        fetchone_results=[("doc1",), vrow_fresh_processing],
    )
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_stranded_versions()

    assert result["skipped_stale"] == 1
    assert result["success"] == 0 and result["failed"] == 0
    client.push_documents.assert_not_called()
    conn.commit.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_lock_reverify_chunk_redirty_skips_before_ha3(mock_get_db_conn, mock_get_client):
    """F3：kb_set_visibility 在锁前把当前版本 chunk 标脏 NOT_INDEXED → chunk 完整性
    重验不成立 → 放弃候选、零 HA3 删除（原「全量 INDEXED」证明失效）。"""
    chunks_redirtied = [(101, 1, "INDEXED"), (201, 2, "NOT_INDEXED"), (202, 2, "INDEXED")]
    conn, cursor = _make_conn(
        [[("doc1", 2)], chunks_redirtied],
        fetchone_results=[("doc1",), _VROW_FAILED],
    )
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_stranded_versions()

    assert result["skipped_stale"] == 1
    client.push_documents.assert_not_called()
    conn.commit.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_lock_reverify_empty_current_chunks_skips(mock_get_db_conn, mock_get_client):
    """F3（Codex r6）：当前版本 active chunk 变空集 → NOT-EXISTS 空真不算数，
    必须因「≥1 INDEXED」不成立而放弃（防 vacuous fence）。"""
    only_old_chunks = [(101, 1, "INDEXED")]
    conn, cursor = _make_conn(
        [[("doc1", 2)], only_old_chunks],
        fetchone_results=[("doc1",), _VROW_FAILED],
    )
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_stranded_versions()

    assert result["skipped_stale"] == 1
    client.push_documents.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_finalize_cas_loss_skips_supersede(mock_get_db_conn, mock_get_client):
    """F3：版本收尾 CAS 落空（stage-3 并发接管）→ 旧 chunk 停用保留、supersede 跳过、
    计 skipped_stale（success-before-supersede 不变量）。"""
    conn, cursor = _make_conn(
        [[("doc1", 2)], _CHUNKS_OK],
        fetchone_results=[("doc1",), _VROW_FAILED],
        rowcount=0,   # CAS 输
    )
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_stranded_versions()

    assert result["skipped_stale"] == 1
    assert result["success"] == 0
    sqls = [c[0][0] for c in cursor.execute.call_args_list]
    assert not any("SET status = 'superseded'" in s for s in sqls), (
        "收尾 CAS 落空时绝不能 supersede（交胜者自己的收尾路径）"
    )
    assert any("is_active = FALSE" in s for s in sqls), "旧 chunk 停用保留（与胜者语义一致）"
    conn.commit.assert_called_once()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_stale_processing_uses_timestamp_bound_cas(mock_get_db_conn, mock_get_client):
    """F3：陈旧 PROCESSING 候选 → 收尾必须是 (index_status='PROCESSING' AND updated_at=观测值)
    时间戳绑定 CAS——takeover 刷新 updated_at 后本 CAS 落空、healer 认输。"""
    observed_ts = "2026-07-21 05:00:00"
    vrow_stale_processing = ("PROCESSING", observed_ts, "active", None, 1)
    conn, cursor = _make_conn(
        [[("doc1", 2)], _CHUNKS_OK],
        fetchone_results=[("doc1",), vrow_stale_processing],
    )
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_stranded_versions()

    assert result["success"] == 1
    cas_calls = [c for c in cursor.execute.call_args_list
                 if "SET index_status = 'SUCCESS'" in c[0][0]]
    assert len(cas_calls) == 1
    sql, params = cas_calls[0][0][0], cas_calls[0][0][1]
    assert "index_status = 'PROCESSING'" in sql and "updated_at = %s" in sql
    assert observed_ts in params, "CAS 必须绑定锁内观测到的 updated_at"


@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_no_strandees_is_noop(mock_get_db_conn):
    """没有搁浅文档：只跑检测查询，不碰客户端、不提交。"""
    conn, cursor = _make_conn([[]])
    mock_get_db_conn.return_value = conn

    result = reconcile_stranded_versions()

    assert result == {"total": 0, "success": 0, "failed": 0, "skipped_stale": 0, "errors": []}
    conn.commit.assert_not_called()


def test_detection_sql_guards():
    """检测 SQL 的关键护栏（同 test_g/test_h 的源码巡检风格）。"""
    source = inspect.getsource(reconcile_stranded_versions)
    assert "QUARANTINED" in source, "被隔离的文档不得参与对账"
    assert "INTERVAL 2 HOUR" in source, "必须避让 2h 内在跑的 stage-3（PROCESSING）"
    assert "index_status != '{ChunkIndexStatus.INDEXED}'" in source, (
        "「全量 INDEXED」必须用 NOT EXISTS 非 INDEXED 来证明（部分成功不算；词表常量渲染）"
    )
    assert "version_no <" in source, "搁浅特征 = 旧版本仍有 active chunk"
    assert "is_active = 1" in source
    # ── F3（2026-07-21 评审共识）新增护栏 ──
    assert "_lock_doc" in source, "必须先取 document_meta 行锁（串行化 console 写方族）"
    assert "PENDING_DELETE" in source, "提示扫描与锁内重验都必须排除删除队列中的版本"
    assert "updated_at = %s" in source, (
        "陈旧 PROCESSING 收尾必须时间戳绑定 CAS（takeover 刷新即认输）"
    )
    assert "FOR UPDATE" in source, "chunk 完整性事实必须锁读（封同 doc 幻影插入）"
    assert "_get_bounded_search_client" in source, "持锁跨 HA3 必须走 bounded 客户端"


def test_orchestrator_calls_reconciler_for_stage3_only():
    """run_stage_drained：stage-3 生产跑在 drain 计数之前对账一次；stage-2/模拟不跑；
    对账抛异常也不阻断当日入库。"""
    import opensearch_pipeline.dataworks_orchestrator as orch

    order = []
    with patch("opensearch_pipeline.spot_checker.reconcile_stranded_versions",
               side_effect=lambda: order.append("reconcile")
               or {"total": 0, "success": 0, "failed": 0, "errors": []}), \
         patch.object(orch, "_count_pending_rows",
                      side_effect=lambda stage: order.append("count") or 0), \
         patch.object(orch, "run_stage", MagicMock()):
        orch.run_stage_drained(stage=3, bizdate="20260610", simulate=False)
    assert order[0] == "reconcile", "对账必须先于 pending 计数（搁浅文档计数为 0）"
    assert order.count("reconcile") == 1

    with patch("opensearch_pipeline.spot_checker.reconcile_stranded_versions") as mock_rec, \
         patch.object(orch, "_count_pending_rows", side_effect=lambda stage: 0), \
         patch.object(orch, "_reset_stale_stage2_locks", MagicMock()), \
         patch.object(orch, "run_stage", MagicMock()):
        orch.run_stage_drained(stage=2, bizdate="20260610", simulate=False)
    mock_rec.assert_not_called()

    with patch("opensearch_pipeline.spot_checker.reconcile_stranded_versions") as mock_rec2, \
         patch.object(orch, "run_stage", MagicMock()):
        orch.run_stage_drained(stage=3, bizdate="20260610", simulate=True)
    mock_rec2.assert_not_called()

    # 对账整体抛异常 → drain 照常进行（优雅降级）
    counted = []
    with patch("opensearch_pipeline.spot_checker.reconcile_stranded_versions",
               side_effect=Exception("reconcile blew up")), \
         patch.object(orch, "_count_pending_rows",
                      side_effect=lambda stage: counted.append(1) or 0), \
         patch.object(orch, "run_stage", MagicMock()):
        orch.run_stage_drained(stage=3, bizdate="20260610", simulate=False)
    assert counted, "对账失败不得阻断 drain"


def test_spot_check_pipeline_wires_stranded_reconcile():
    """run_spot_check_pipeline 生产路径里必须挂上搁浅对账（独立于 orchestrator 的兜底）。"""
    from opensearch_pipeline.spot_checker import run_spot_check_pipeline
    source = inspect.getsource(run_spot_check_pipeline)
    assert "reconcile_stranded_versions()" in source
