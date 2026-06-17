# -*- coding: utf-8 -*-
"""
tests/test_qa_logger.py — qa_session_log 写入与表结构漂移告警

被修的事故：schema 文件从未在 fuling_operation 建出带 content_blocks_json 的
qa_session_log，全新部署上每条 INSERT 都报 Unknown column/table，被 catch-all
按非致命吞掉 → 问答日志整行静默丢失、反馈找不到 message_id、监控全盲。
"""

import logging
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from opensearch_pipeline.qa_logger import log_qa_session

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"


def _conn_raising(exc):
    conn = MagicMock()
    cur = MagicMock()
    cur.execute.side_effect = exc
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_unknown_column_logs_critical_with_schema_hint(mock_get_conn, caplog):
    """errno 1054（列不存在）→ CRITICAL + 指向 schema/002 的修复提示；绝不向外抛。"""
    mock_get_conn.return_value = _conn_raising(
        Exception(1054, "Unknown column 'content_blocks_json' in 'field list'")
    )
    with caplog.at_level(logging.DEBUG, logger="opensearch_pipeline.qa_logger"):
        log_qa_session(session_id="s1", message_id="m1", query_text="q")  # 必须不 raise

    crit = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert crit, "表结构漂移必须按 CRITICAL 告警（普通 ERROR 会淹没在噪音里）"
    assert "schema/002_feedback_system.sql" in crit[0].getMessage()


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_unknown_table_logs_critical(mock_get_conn, caplog):
    """errno 1146（表不存在，全新 fuling_operation 库）同样按 CRITICAL 告警。"""
    mock_get_conn.return_value = _conn_raising(
        Exception(1146, "Table 'fuling_operation.qa_session_log' doesn't exist")
    )
    with caplog.at_level(logging.DEBUG, logger="opensearch_pipeline.qa_logger"):
        log_qa_session(session_id="s1", message_id="m1", query_text="q")

    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_generic_error_stays_error_level(mock_get_conn, caplog):
    """非结构漂移的写入失败保持原有 ERROR 级别（non-fatal），不升 CRITICAL。"""
    mock_get_conn.return_value = _conn_raising(Exception("connection reset"))
    with caplog.at_level(logging.DEBUG, logger="opensearch_pipeline.qa_logger"):
        log_qa_session(session_id="s1", message_id="m1", query_text="q")

    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_success_path_commits_and_closes(mock_get_conn):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    mock_get_conn.return_value = conn

    log_qa_session(session_id="s1", message_id="m1", query_text="q",
                   content_blocks_json='[{"type":"text"}]')

    conn.commit.assert_called_once()
    conn.close.assert_called_once()
    # content_blocks_json 必须真的进了 INSERT 参数
    params = cur.execute.call_args[0][1]
    assert '[{"type":"text"}]' in params


def test_insert_columns_all_exist_in_schema_files():
    """结构漂移防回归：log_qa_session 的 INSERT 列必须每一列都出现在 schema/ DDL 里
    （正是这条护栏缺失让 content_blocks_json 静默丢了所有问答日志）。"""
    import inspect
    from opensearch_pipeline import qa_logger

    source = inspect.getsource(qa_logger.log_qa_session)
    # 库名已配置化（RAG_RDS_OPERATION_DATABASE → f-string 插值 {_op_db()}），表名不变
    m = re.search(r"INSERT INTO \{_op_db\(\)\}\.qa_session_log\s*\((.*?)\)\s*VALUES",
                  source, re.S)
    assert m, "找不到 INSERT 列清单"
    columns = [c.strip() for c in m.group(1).split(",") if c.strip()]
    assert "content_blocks_json" in columns  # sanity

    ddl_text = "".join(
        (SCHEMA_DIR / f).read_text(encoding="utf-8")
        for f in ("001_opensearch_pipeline.sql", "002_feedback_system.sql")
    )
    missing = [c for c in columns if c not in ddl_text]
    assert not missing, f"INSERT 用到了 schema DDL 里不存在的列: {missing}"


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_retrieved_docs_json_carries_chunk_id_and_version_no(mock_get_conn):
    """答案血缘：retrieved_docs_json 必须带 chunk_id + version_no，使一条已落库回答能
    溯源到精确的 chunk 与文档版本（L7-01 / INC-6）。re-chunk 后 chunk_index 会漂移，
    仅靠 doc_id/chunk_index 无法复现原始来源。"""
    import json as _json

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    mock_get_conn.return_value = conn

    log_qa_session(
        session_id="s1", message_id="m1", query_text="q",
        retrieved_docs=[{
            "doc_id": "DOC_HR_x", "chunk_id": "DOC_HR_x_v3_c0007_ABCD1234",
            "version_no": 3, "title": "t", "section_title": "s",
            "score": 9.1, "chunk_index": 7,
        }],
    )
    params = cur.execute.call_args[0][1]
    # 找到 retrieved_docs_json 参数（含 chunk_id 的 JSON 串）
    rj = next(p for p in params if isinstance(p, str) and "chunk_id" in p)
    docs = _json.loads(rj)
    assert docs[0]["chunk_id"] == "DOC_HR_x_v3_c0007_ABCD1234"
    assert docs[0]["version_no"] == 3
