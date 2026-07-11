# -*- coding: utf-8 -*-
"""kb console 聚合端点 × 真实 MySQL 的回归（requires_local_db 族，CI db-integration job 串行跑）。

2026-07-11 staging P1 的教训：/api/kb/feedback-review 的 SELECT 写了 qa_session_log
不存在的 q.question（实列 query_text）——桩游标不校验列名，单测全绿，staging 真库
pymysql 1054 必 500，且前端把 500 静默成区块消失，差评复核在治理面完全不可见。
mock 能证明分组/脱敏/作用域逻辑，「列存在于真表」只有真库能证明，本模块补上这一层：
按权威 schema/ 建出的库上 seed → 调端点 → 断言行回流。

跑本地 docker-compose dev 栈（fuling_knowledge + fuling_operation，tests/local_stack.py
接线）；CI db-integration job 先跑 scripts/ci_load_schema.sh 从零建双库。真实 DML：
固定主键 seed → finally 按同一组主键精确清理，开跑先预清一次故可重入。
"""
import json

import pytest

from tests.local_stack import requires_local_db

# 本模块专属固定主键：seed 与清理都按它们定位，绝不触碰库里其他行
_MSG_ID = "ITEST-FBR-M1"
_SESSION_ID = "ITEST-FBR-S1"
_FEEDBACK_ID = "ITEST-FBR-F1"
_DOC_ID = "ITEST-FBR-D1"
_USER_ID = "ITEST-FBR-user"


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


def _cleanup(conn):
    from opensearch_pipeline.api import _kb_db
    from opensearch_pipeline.qa_logger import _op_db
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_op_db()}.user_feedback WHERE feedback_id = %s", (_FEEDBACK_ID,))
        cur.execute(f"DELETE FROM {_op_db()}.qa_session_log WHERE message_id = %s", (_MSG_ID,))
        cur.execute(f"DELETE FROM {_kb_db()}.document_meta WHERE doc_id = %s", (_DOC_ID,))
    conn.commit()


@requires_local_db
def test_feedback_review_executes_on_real_schema(monkeypatch):
    """真库执行 + 行回流：downvote × 引用文档 seed 后端点必须出行且逐字段成型。
    老代码（q.question）在这里直接 pymysql 1054 → HTTPException 500——本测试
    即该 bug 的最小真库复现，列名契约的「活体」版。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    # 缓存旁路：断言对象是 SQL 真跑，不是 TTL 缓存行为
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    # 钉死 JSON_TABLE 回退形态（生产默认 RAG_QA_FACT_JOIN=off）：不随环境 flag 漂移，
    # 且该形态直接消费 qa_session_log 行本身，不依赖事实表回填
    monkeypatch.setattr("opensearch_pipeline.qa_facts.fact_join_enabled", lambda: False)

    from opensearch_pipeline.api import _kb_db
    from opensearch_pipeline.db import _get_db_conn
    from opensearch_pipeline.qa_logger import _op_db

    conn = _get_db_conn()
    try:
        _cleanup(conn)   # 上次中断的残留先清，保证可重入
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_op_db()}.qa_session_log"
                " (session_id, message_id, user_id, query_text, answer_status, cited_docs_json)"
                " VALUES (%s, %s, %s, %s, 'SUCCESS', %s)",
                (_SESSION_ID, _MSG_ID, _USER_ID, "清洗流程有哪些注意事项？",
                 json.dumps([{"doc_id": _DOC_ID}])))
            cur.execute(
                f"INSERT INTO {_op_db()}.user_feedback"
                " (feedback_id, message_id, user_id, feedback_type,"
                "  feedback_reason, feedback_comment)"
                " VALUES (%s, %s, %s, 'downvote', 'inaccurate', %s)",
                (_FEEDBACK_ID, _MSG_ID, _USER_ID, "步骤三和现场设备对不上"))
            cur.execute(
                f"INSERT INTO {_kb_db()}.document_meta (doc_id, title, owner_dept, status)"
                " VALUES (%s, %s, 'production', 'active')",
                (_DOC_ID, "设备清洗 SOP"))
        conn.commit()

        from opensearch_pipeline import api
        resp = api.kb_feedback_review(request=None, limit=20,
                                      identity=api.Identity(user_id="ITEST-FBR-adm"))
        items = {i.message_id: i for i in resp.items}
        assert _MSG_ID in items, "seed 的差评没有回流——列名/JOIN/时间窗有一个在真库上坏了"
        it = items[_MSG_ID]
        assert "清洗流程" in it.question            # 问题文本必须来自 qa_session_log.query_text
        assert it.reasons == ["不准确"]
        assert it.comment and it.handled is False   # PENDING（DDL 默认）= 未处置，进收件箱
        assert [d.doc_id for d in it.docs] == [_DOC_ID]
        assert it.docs[0].owner_dept == "production"
    finally:
        try:
            _cleanup(conn)
        finally:
            conn.close()
