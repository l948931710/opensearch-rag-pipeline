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


def _make_conn(fetchall_results):
    """MagicMock 连接：cursor.fetchall 依次返回 fetchall_results 中的列表。"""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.side_effect = list(fetchall_results)
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


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_detects_and_heals_strandee(mock_get_db_conn, mock_get_client):
    """命中一条搁浅文档：HA3 按整型 chunk_meta.id 删除 → 停用旧 chunk → dv 修 SUCCESS → commit。"""
    conn, cursor = _make_conn([
        [("doc1", 2)],        # 检测查询：一条搁浅 (doc1, v2)
        [(101,), (102,)],     # 旧版本 active chunk 的 RDS 主键
    ])
    mock_get_db_conn.return_value = conn
    client = _ha3_client()
    mock_get_client.return_value = client

    result = reconcile_stranded_versions()

    assert result["total"] == 1
    assert result["success"] == 1
    assert result["failed"] == 0

    # HA3 删除必须用 chunk_meta.id（整型主键），cmd=delete
    push_body = client.push_documents.call_args[0][2].body
    assert {"cmd": "delete", "fields": {"id": 101}} in push_body
    assert {"cmd": "delete", "fields": {"id": 102}} in push_body

    sqls = [c[0][0] for c in cursor.execute.call_args_list]
    assert any("is_active = FALSE" in s and "version_no < %s" in s for s in sqls), (
        "旧版本 chunk 必须停用（is_active=FALSE）"
    )
    assert any("SET index_status = 'SUCCESS'" in s for s in sqls), (
        "搁浅文档的 document_version 必须修回 SUCCESS"
    )
    conn.commit.assert_called_once()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_search_delete_failure_leaves_rds_untouched(mock_get_db_conn, mock_get_client):
    """索引删除失败 → RDS 不动（保持可检测、下次重试）、rollback、不向外抛异常。"""
    conn, cursor = _make_conn([
        [("doc1", 2)],
        [(101,)],
    ])
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
    conn, cursor = _make_conn([
        [("doc1", 2)],
        [(101,)],
    ])
    mock_get_db_conn.return_value = conn
    mock_get_client.return_value = "MOCK_HA3_CLIENT"

    result = reconcile_stranded_versions()

    assert result["failed"] == 1
    assert any("MOCK_HA3_CLIENT" in e for e in result["errors"])
    for call in cursor.execute.call_args_list:
        assert "is_active = FALSE" not in call[0][0]
    conn.commit.assert_not_called()


@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_no_strandees_is_noop(mock_get_db_conn):
    """没有搁浅文档：只跑检测查询，不碰客户端、不提交。"""
    conn, cursor = _make_conn([[]])
    mock_get_db_conn.return_value = conn

    result = reconcile_stranded_versions()

    assert result == {"total": 0, "success": 0, "failed": 0, "errors": []}
    conn.commit.assert_not_called()


def test_detection_sql_guards():
    """检测 SQL 的关键护栏（同 test_g/test_h 的源码巡检风格）。"""
    source = inspect.getsource(reconcile_stranded_versions)
    assert "QUARANTINED" in source, "被隔离的文档不得参与对账"
    assert "INTERVAL 2 HOUR" in source, "必须避让 2h 内在跑的 stage-3（PROCESSING）"
    assert "index_status != 'INDEXED'" in source, (
        "「全量 INDEXED」必须用 NOT EXISTS 非 INDEXED 来证明（部分成功不算）"
    )
    assert "version_no <" in source, "搁浅特征 = 旧版本仍有 active chunk"
    assert "is_active = 1" in source


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
