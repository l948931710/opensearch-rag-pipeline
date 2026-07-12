# -*- coding: utf-8 -*-
"""
tests/test_version_supersede.py — version-bump 重灌后 document_version 单 active 不变量

被修的缺口（2026-07-12 双 active 审计）：stage-3 只做 chunk 级停用（chunk_meta.is_active），
document_version 旧 active 行从来没有任何管道路径降级——version-bump 重灌后每 doc 留下
双 status='active'（7-06 重灌批 485/485 + 窗口外存量 104；纯台账脏，无双服务）。

修复语义（本文件锁死）：
  - node_deactivate_old_chunks 收尾事务内、且仅对 SUCCESS 组文档，把
    version_no < 当前版本 且 status='active' 的旧版本行 CAS 置 'superseded'；
  - 顺序不变量：supersede 在旧 chunk 停用（is_active=FALSE）与 index_status 收尾 CAS
    之后（新版本 INDEXED 成功后才 supersede 旧版）；
  - FAILED / LIMIT-推迟（NOT_INDEXED）文档旧版本仍在服务，绝不 supersede；
  - CAS on status='active' 幂等（重跑无副作用），不碰 retired 行；
  - reconcile_stranded_versions 兜底路径同语义。
"""

from unittest.mock import MagicMock, patch

from opensearch_pipeline.chunker import Chunk
from opensearch_pipeline.pipeline_nodes import node_deactivate_old_chunks
from opensearch_pipeline.spot_checker import reconcile_stranded_versions


def _mk_chunk(cid, doc, status, ver=2):
    c = Chunk(chunk_id=cid, doc_id=doc, version_no=ver, chunk_index=0,
              chunk_type="text_chunk", chunk_text="t", token_count=1)
    c.index_status = status
    c.embedding_status = "DONE" if status == "INDEXED" else "FAILED"  # INDEXED ⇒ DONE（非僵尸）
    return c


def _ha3_client(status_code=200):
    client = MagicMock(spec=["push_documents"])
    resp = MagicMock()
    resp.status_code = status_code
    resp.body = ""
    resp.text = ""
    client.push_documents.return_value = resp
    return client


# ── 1) SUCCESS 文档 supersede 旧版本；FAILED 文档绝不 ──────────────────────────


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
@patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
def test_success_doc_supersedes_older_versions_failed_doc_excluded(
        mock_get_client, mock_get_db_conn):
    """docA 全量 INDEXED（SUCCESS 收尾）→ 恰好一次版本级 supersede，且只含 docA；
    docB embedding-FAILED → 不在 supersede 参数内（旧版本仍在服务）。"""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    # 1-tuple：完整性闸（需 len>=2）与旧 id SELECT（需 len>=3）均读作空 → 健康直通路径
    mock_cursor.fetchall.return_value = [(101,)]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn
    mock_get_client.return_value = _ha3_client()

    ok_chunk = _mk_chunk("ca", "docA", "INDEXED")
    bad_chunk = _mk_chunk("cb", "docB", "NOT_INDEXED")  # embedding-FAILED：内存状态看不出失败
    ctx = {
        "valid_chunks": [ok_chunk, bad_chunk],
        "embedding_failed_chunks": [bad_chunk],
        "preempted_doc_versions": {("docA", 2), ("docB", 2)},
        "simulate_db": False,
        "simulate_opensearch": False,
        "dag3_no_work": False,
    }

    node_deactivate_old_chunks(ctx)

    supersede_calls = [
        c for c in mock_cursor.execute.call_args_list
        if "SET status = 'superseded'" in c[0][0]
    ]
    assert len(supersede_calls) == 1, (
        f"SUCCESS 组应恰好发出一次版本级 supersede，got {len(supersede_calls)}"
    )
    sql = supersede_calls[0][0][0]
    params = supersede_calls[0][0][1]
    assert "version_no < %s" in sql, "只允许降级严格更旧的版本行"
    assert "status = 'active'" in sql, "必须 CAS on status='active'（幂等 + 不碰 retired）"
    assert params == ("docA", 2), (
        f"supersede 只能覆盖 SUCCESS 文档（docA v2），FAILED 的 docB 不得入内，got {params}"
    )

    # 顺序不变量：旧 chunk 停用 → index_status 收尾 CAS → 版本级 supersede
    sqls = [c[0][0] for c in mock_cursor.execute.call_args_list]
    i_deact = next(i for i, s in enumerate(sqls) if "is_active = FALSE" in s)
    i_cas = max(i for i, s in enumerate(sqls)
                if "UPDATE document_version" in s and "SET index_status = %s" in s)
    i_sup = next(i for i, s in enumerate(sqls) if "SET status = 'superseded'" in s)
    assert i_deact < i_sup and i_cas < i_sup, (
        "版本级 supersede 必须在旧 chunk 停用与 index_status 收尾之后（同事务尾部）"
    )
    mock_conn.commit.assert_called_once()


# ── 2) LIMIT 边界推迟的文档绝不 supersede ─────────────────────────────────────


class _DeferCursor:
    """按 SQL 内容分发 fetchall：完整性闸返回残留尾部 → 该版本被推迟（NOT_INDEXED）。"""

    def __init__(self, residual_rows):
        self.residual_rows = residual_rows
        self.executed = []
        self._last_sql = ""
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._last_sql = sql
        self.executed.append((sql, params))

    def fetchall(self):
        if "index_status != 'INDEXED'" in self._last_sql:
            return self.residual_rows
        return []

    def close(self):
        pass


class _PlainConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        outer = self

        class _Ctx:
            def __enter__(self):
                return outer._cursor

            def __exit__(self, *a):
                return False

        return _Ctx()

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
@patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
def test_limit_deferred_doc_never_superseded(mock_get_client, mock_get_db_conn):
    """本批 chunk 全 INDEXED 但 RDS 仍有同版本残留尾部（LIMIT 切批）→ 收尾 NOT_INDEXED，
    旧版本仍在服务：绝不允许发出版本级 supersede。"""
    cur = _DeferCursor(residual_rows=[("docL", 2)])
    mock_get_db_conn.return_value = _PlainConn(cur)
    mock_get_client.return_value = _ha3_client()

    ctx = {
        "valid_chunks": [_mk_chunk("cl", "docL", "INDEXED")],
        "preempted_doc_versions": {("docL", 2)},
        "simulate_db": False,
        "simulate_opensearch": False,
        "dag3_no_work": False,
    }

    node_deactivate_old_chunks(ctx)

    assert not any("SET status = 'superseded'" in sql for sql, _ in cur.executed), (
        "LIMIT 边界推迟（NOT_INDEXED 收尾）的文档禁止版本级 supersede"
    )
    # 佐证推迟真的发生：状态收尾写的是 NOT_INDEXED 而非 SUCCESS
    status_writes = [(sql, p) for sql, p in cur.executed
                     if "UPDATE document_version" in sql and "SET index_status = %s" in sql]
    assert status_writes and status_writes[0][1][0] == "NOT_INDEXED"


# ── 3) 台账重放：version-bump 重灌后恰余一个 active（且幂等） ──────────────────


class _LedgerCursor:
    """微型 document_version 台账：只实现本测试涉及语句的行级语义。"""

    def __init__(self, ledger):
        self.ledger = ledger  # list[dict]: doc_id / version_no / status / index_status
        self._last_sql = ""
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._last_sql = sql
        params = params or ()
        if "SET status = 'superseded'" in sql:
            pairs = [(params[i], params[i + 1]) for i in range(0, len(params), 2)]
            n = 0
            for row in self.ledger:
                if row["status"] != "active":
                    continue
                if any(row["doc_id"] == d and row["version_no"] < v for d, v in pairs):
                    row["status"] = "superseded"
                    n += 1
            self.rowcount = n
        elif "UPDATE document_version" in sql and "SET index_status = %s" in sql:
            st = params[0]
            pairs = [(params[i], params[i + 1]) for i in range(1, len(params), 2)]
            n = 0
            for row in self.ledger:
                if row["index_status"] != "PROCESSING":
                    continue  # CAS：只允许 PROCESSING→终态
                if any(row["doc_id"] == d and row["version_no"] == v for d, v in pairs):
                    row["index_status"] = st
                    n += 1
            self.rowcount = n
        else:
            self.rowcount = 0

    def fetchall(self):
        return []  # 完整性闸无残留；旧 id SELECT 为空（chunk 行不在本台账建模范围）

    def close(self):
        pass


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
@patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
def test_version_bump_reingest_leaves_single_active(mock_get_client, mock_get_db_conn):
    """重灌场景重放：v1 active（上代）+ v2 active/PROCESSING（version-bump 新代）。
    收尾后台账恰余一个 active 且为 v2；重跑一遍（SUCCESS-relock/重入）不变——幂等。"""
    ledger = [
        {"doc_id": "docA", "version_no": 1, "status": "active", "index_status": "SUCCESS"},
        {"doc_id": "docA", "version_no": 2, "status": "active", "index_status": "PROCESSING"},
    ]
    mock_get_db_conn.return_value = _PlainConn(_LedgerCursor(ledger))
    mock_get_client.return_value = _ha3_client()

    def _run():
        node_deactivate_old_chunks({
            "valid_chunks": [_mk_chunk("ca", "docA", "INDEXED")],
            "preempted_doc_versions": {("docA", 2)},
            "simulate_db": False,
            "simulate_opensearch": False,
            "dag3_no_work": False,
        })

    _run()
    actives = [r for r in ledger if r["status"] == "active"]
    assert len(actives) == 1 and actives[0]["version_no"] == 2, (
        f"version-bump 重灌收尾后必须恰余一个 active 版本行（新版本），got {ledger}"
    )
    assert ledger[0]["status"] == "superseded"
    assert ledger[1]["index_status"] == "SUCCESS"

    _run()  # 幂等：CAS on status='active' / index_status='PROCESSING' 均不再命中
    actives = [r for r in ledger if r["status"] == "active"]
    assert len(actives) == 1 and actives[0]["version_no"] == 2, (
        f"重跑收尾必须保持单 active 不变量，got {ledger}"
    )


# ── 4) reconcile_stranded_versions 兜底同语义 ────────────────────────────────


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_reconcile_stranded_supersedes_old_versions(mock_get_db_conn, mock_get_client):
    """搁浅治愈路径：停用旧 chunk + dv 修 SUCCESS 之后，同事务降级旧 active 版本行。"""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.side_effect = [
        [("doc1", 2)],        # 检测查询：一条搁浅 (doc1, v2)
        [(101,), (102,)],     # 旧版本 active chunk 的 RDS 主键
    ]
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_get_db_conn.return_value = conn
    mock_get_client.return_value = _ha3_client()

    result = reconcile_stranded_versions()

    assert result["success"] == 1 and result["failed"] == 0

    supersede_calls = [
        c for c in cursor.execute.call_args_list
        if "SET status = 'superseded'" in c[0][0]
    ]
    assert len(supersede_calls) == 1, "兜底治愈必须附带一次版本级 supersede"
    sql = supersede_calls[0][0][0]
    params = supersede_calls[0][0][1]
    assert "version_no < %s" in sql and "status = 'active'" in sql
    assert params == ("doc1", 2)

    sqls = [c[0][0] for c in cursor.execute.call_args_list]
    i_success = next(i for i, s in enumerate(sqls) if "SET index_status = 'SUCCESS'" in s)
    i_sup = next(i for i, s in enumerate(sqls) if "SET status = 'superseded'" in s)
    assert i_success < i_sup, "supersede 必须在新版本 SUCCESS 确认之后（同事务尾部）"
    conn.commit.assert_called_once()


@patch("opensearch_pipeline.spot_checker._get_opensearch_client")
@patch("opensearch_pipeline.spot_checker._get_db_conn")
def test_reconcile_failure_path_no_supersede(mock_get_db_conn, mock_get_client):
    """索引删除失败 → 回滚路径：不得发出版本级 supersede（旧版本保持可检测）。"""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.side_effect = [
        [("doc1", 2)],
        [(101,)],
    ]
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_get_db_conn.return_value = conn
    mock_get_client.return_value = _ha3_client(status_code=500)  # HA3 删除失败

    result = reconcile_stranded_versions()

    assert result["failed"] == 1
    assert not any(
        "SET status = 'superseded'" in c[0][0] for c in cursor.execute.call_args_list
    ), "索引删除失败时旧版本行必须保持 active（可检测、待重试）"
    conn.rollback.assert_called()


# ── 5) simulate 路径零 DB 写 ─────────────────────────────────────────────────


@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_simulate_path_touches_no_db(mock_get_db_conn):
    """simulate 分支只打印（含 supersede 演练行），绝不建立 DB 连接。"""
    node_deactivate_old_chunks({
        "valid_chunks": [_mk_chunk("ca", "docA", "INDEXED")],
        "preempted_doc_versions": {("docA", 2)},
        "simulate_db": True,
        "simulate_opensearch": True,
        "dag3_no_work": False,
    })
    mock_get_db_conn.assert_not_called()
