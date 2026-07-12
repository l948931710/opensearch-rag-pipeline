# -*- coding: utf-8 -*-
"""tests/test_gen_meta_contract.py — 盲区审计 P2-20/21/22 + P2-8（schema/018）。

覆盖两条闭环：
  * gen_meta：prompt 身份（sha/flags）+ 采样参数 + 检索制度快照 → qa_session_log.gen_meta_json。
    prompt_sha 稳定性 / 1054 回退（未 apply 018 时审计行绝不丢）/ 四条链路调用点透传。
  * embedding 契约行（rag_runtime_contract）：match / mismatch / 表缺失静默跳过 +
    /api/ready 失配降级 503。
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# 1. build_gen_meta — prompt_sha 稳定性 + flags 判定（P2-20/21）
# ═══════════════════════════════════════════════════════════════

def test_build_gen_meta_prompt_sha_stable_and_flags():
    from opensearch_pipeline import llm_generator as G

    m1 = G.build_gen_meta(G.DEFAULT_SYSTEM_PROMPT, temperature=0.1, model="qwen-test")
    m2 = G.build_gen_meta(G.DEFAULT_SYSTEM_PROMPT, temperature=0.1, model="qwen-test")
    # 同一 prompt → sha 逐次稳定（16 位十六进制）
    assert m1["prompt_sha"] == m2["prompt_sha"]
    assert len(m1["prompt_sha"]) == 16
    # flags 与拼接实现同源：默认图文 prompt 含规则 10、不含条件规则
    assert m1["prompt_flags"]["img_interleave"] is True
    assert m1["prompt_flags"]["low_confidence"] is False
    assert m1["prompt_flags"]["injection_guard"] is False
    assert m1["prompt_flags"]["custom_base"] is False
    # P2-21：temperature 如实记录；top_p 从不发送 → 恒 null
    assert m1["temperature"] == 0.1
    assert m1["top_p"] is None
    assert m1["model"] == "qwen-test"
    # 检索制度快照（P2-22）：simulate config 是真实 dataclass，关键键必须齐
    r = m1["retrieval"]
    assert set(r) == {"fusion", "knn_weight", "th_high", "th_med", "rerank",
                      "rerank_model", "top_k", "embedding_model", "embedding_dim"}
    assert isinstance(r["rerank"], bool)

    # 条件规则改变 prompt → sha 必变、对应 flag 翻转
    m3 = G.build_gen_meta(G.DEFAULT_SYSTEM_PROMPT + G.LOW_CONFIDENCE_RULE,
                          temperature=0.1, model="qwen-test")
    assert m3["prompt_sha"] != m1["prompt_sha"]
    assert m3["prompt_flags"]["low_confidence"] is True
    # 纯文本 prompt：规则 10 缺席
    m4 = G.build_gen_meta(G.TEXT_ONLY_SYSTEM_PROMPT, temperature=0.1, model="m")
    assert m4["prompt_flags"]["img_interleave"] is False
    # 自定义 system prompt → custom_base
    m5 = G.build_gen_meta("你是别的助手", temperature=0.1, model="m")
    assert m5["prompt_flags"]["custom_base"] is True


def test_build_gen_meta_config_missing_degrades_retrieval(monkeypatch):
    """config 不全（测试 mock 场景）→ retrieval 如实缺席为 None，prompt 部分照常。"""
    from opensearch_pipeline import llm_generator as G

    monkeypatch.setattr(G, "get_config",
                        lambda: SimpleNamespace())  # 无 alibaba_vector/rag/embedding
    m = G.build_gen_meta(G.DEFAULT_SYSTEM_PROMPT, temperature=0.3, model="m")
    assert m["retrieval"] is None
    assert len(m["prompt_sha"]) == 16


@patch("opensearch_pipeline.llm_generator._http_post")
@patch("opensearch_pipeline.llm_generator.get_config")
def test_generate_answer_returns_gen_meta(mock_config, mock_post):
    """非流式返回 dict 携带 gen_meta（temperature/model 取实际请求值）。"""
    from opensearch_pipeline.llm_generator import generate_answer

    mock_cfg = MagicMock()
    mock_cfg.llm.api_key = "test-key"
    mock_cfg.llm.api_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    mock_cfg.llm.model = "qwen3.6-plus"
    mock_cfg.rag.score_threshold_high = 8.0
    mock_cfg.rag.score_threshold_medium = 5.0
    mock_cfg.rag.serving_model_gateway = False
    mock_config.return_value = mock_cfg

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "答案"}}],
        "usage": {"total_tokens": 10},
    }
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    chunks = [{"title": "员工手册", "chunk_text": "住宿规定...", "doc_id": "D1", "score": 9.0}]
    result = generate_answer("怎么申请住宿", chunks, temperature=0.42)
    gm = result["gen_meta"]
    assert gm is not None
    assert gm["temperature"] == 0.42
    assert gm["model"] == "qwen3.6-plus"
    assert len(gm["prompt_sha"]) == 16


def test_generate_answer_via_stream_passes_gen_meta(monkeypatch):
    """攒流收集器经 meta_out 出参截留 gen_meta；mock 生成器不回填 → None 不炸。"""
    from opensearch_pipeline import llm_generator as lg

    def fake_stream_with_meta(q, chunks, **kw):
        mo = kw.get("meta_out")
        if mo is not None:
            mo["gen_meta"] = {"prompt_sha": "abcd1234abcd1234"}
        yield 'data: {"type": "chunk", "content": "x"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(lg, "generate_answer_stream", fake_stream_with_meta)
    res = lg.generate_answer_via_stream("q", [])
    assert res["gen_meta"] == {"prompt_sha": "abcd1234abcd1234"}

    def fake_stream_no_meta(q, chunks, **kw):
        yield 'data: {"type": "chunk", "content": "x"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(lg, "generate_answer_stream", fake_stream_no_meta)
    res2 = lg.generate_answer_via_stream("q", [])
    assert res2["gen_meta"] is None


# ═══════════════════════════════════════════════════════════════
# 2. build_qa_log_kwargs — gen_meta → gen_meta_json（answer_flow 保持纯函数）
# ═══════════════════════════════════════════════════════════════

def test_build_qa_log_kwargs_serializes_gen_meta():
    from opensearch_pipeline.answer_flow import build_qa_log_kwargs

    kw = build_qa_log_kwargs(
        session_id="s", message_id="m", question="q",
        gen_meta={"prompt_sha": "abc", "retrieval": {"fusion": "weighted"}},
    )
    d = json.loads(kw["gen_meta_json"])
    assert d["prompt_sha"] == "abc"
    assert d["retrieval"]["fusion"] == "weighted"
    # 缺省（NO_RESULT/错误路径）→ None
    kw2 = build_qa_log_kwargs(session_id="s", message_id="m", question="q")
    assert kw2["gen_meta_json"] is None
    # 不可序列化载荷 → default=str 兜底成串，绝不抛
    kw3 = build_qa_log_kwargs(session_id="s", message_id="m", question="q",
                              gen_meta={"model": MagicMock()})
    assert kw3["gen_meta_json"] is None or isinstance(kw3["gen_meta_json"], str)


# ═══════════════════════════════════════════════════════════════
# 3. qa_logger — gen_meta_json 写入 + 1054 回退（schema/018 未 apply 审计行不丢）
# ═══════════════════════════════════════════════════════════════

def _mock_conn():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


@patch("opensearch_pipeline.db._get_db_conn")
def test_gen_meta_json_written_when_column_exists(mock_get_conn, monkeypatch):
    from opensearch_pipeline import qa_logger
    monkeypatch.setattr(qa_logger, "_GEN_META_COL_MISSING", False)
    conn, cur = _mock_conn()
    mock_get_conn.return_value = conn

    qa_logger.log_qa_session(session_id="s1", message_id="m1", query_text="q",
                             gen_meta_json='{"prompt_sha":"abc"}')
    sql, params = cur.execute.call_args[0]
    assert "gen_meta_json" in sql
    assert '{"prompt_sha":"abc"}' in params
    conn.commit.assert_called_once()


@patch("opensearch_pipeline.db._get_db_conn")
def test_gen_meta_json_1054_falls_back_and_caches(mock_get_conn, monkeypatch, caplog):
    """未 apply 018：首写 1054 → 回退旧列集重试成功（行不丢）+ 负缓存置位；
    第二条日志直接不携带该列（不再重试）。"""
    from opensearch_pipeline import qa_logger
    monkeypatch.setattr(qa_logger, "_GEN_META_COL_MISSING", False)
    conn, cur = _mock_conn()
    mock_get_conn.return_value = conn

    def _exec(sql, params):
        if "gen_meta_json" in sql:
            raise Exception(1054, "Unknown column 'gen_meta_json' in 'field list'")

    cur.execute.side_effect = _exec
    with caplog.at_level(logging.DEBUG, logger="opensearch_pipeline.qa_logger"):
        qa_logger.log_qa_session(session_id="s1", message_id="m1", query_text="q",
                                 gen_meta_json='{"prompt_sha":"abc"}')  # 必须不 raise

    sqls = [c.args[0] for c in cur.execute.call_args_list]
    assert any("gen_meta_json" in s for s in sqls), "应先尝试携带新列"
    assert any("gen_meta_json" not in s for s in sqls), "1054 后必须回退旧列集重写"
    assert qa_logger._GEN_META_COL_MISSING is True
    # 回退成功 = 行没丢：不应有 CRITICAL（结构漂移丢行才 CRITICAL）
    assert not any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert any("schema/018" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)

    # 负缓存生效：后续调用一次都不再携带
    cur.execute.reset_mock()
    cur.execute.side_effect = None
    qa_logger.log_qa_session(session_id="s1", message_id="m2", query_text="q",
                             gen_meta_json='{"prompt_sha":"abc"}')
    assert all("gen_meta_json" not in c.args[0] for c in cur.execute.call_args_list)


# ═══════════════════════════════════════════════════════════════
# 4. 调用点透传（/api/ask 非流式 + /api/ask/stream）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    return TestClient(app)


@patch("opensearch_pipeline.api.build_mini_program_blocks", return_value=[])
@patch("opensearch_pipeline.api.log_qa_session")
@patch("opensearch_pipeline.api._append_to_history")
@patch("opensearch_pipeline.api.generate_answer")
@patch("opensearch_pipeline.api.retrieve_and_enrich")
def test_api_ask_passes_gen_meta_to_log(mock_retrieve, mock_gen, mock_append,
                                        mock_log, mock_blocks, client):
    mock_retrieve.return_value = [
        {"chunk_text": "内容", "title": "手册", "doc_id": "D1", "score": 9.0}]
    mock_gen.return_value = {
        "answer": "答", "sources": [], "model": "m", "usage": {},
        "gen_meta": {"prompt_sha": "cafe0000cafe0000"},
    }
    resp = client.post("/api/ask", json={"question": "怎么申请住宿"})
    assert resp.status_code == 200
    kw = mock_log.call_args.kwargs
    assert kw["gen_meta_json"] and "cafe0000cafe0000" in kw["gen_meta_json"]

    # mock 不带 gen_meta 键（既有测试形态）→ None，调用点不炸
    mock_log.reset_mock()
    mock_gen.return_value = {"answer": "答", "sources": [], "model": "m", "usage": {}}
    resp = client.post("/api/ask", json={"question": "怎么申请住宿"})
    assert resp.status_code == 200
    assert mock_log.call_args.kwargs["gen_meta_json"] is None


def _fake_stream_with_meta(*args, meta_out=None, **kwargs):
    if meta_out is not None:
        meta_out["gen_meta"] = {"prompt_sha": "deadbeefdeadbeef"}
    yield 'data: {"type": "sources", "sources": []}\n\n'
    yield 'data: {"type": "chunk", "content": "答"}\n\n'
    yield 'data: {"type": "done", "model": "m", "usage": {}}\n\n'
    yield "data: [DONE]\n\n"


@patch("opensearch_pipeline.api.build_content_blocks", return_value=[])
@patch("opensearch_pipeline.api.log_qa_session")
@patch("opensearch_pipeline.api._append_to_history")
@patch("opensearch_pipeline.api.generate_answer_stream", side_effect=_fake_stream_with_meta)
@patch("opensearch_pipeline.api.retrieve_and_enrich")
def test_api_stream_passes_gen_meta_to_log(mock_retrieve, mock_stream, mock_append,
                                           mock_log, mock_blocks, client):
    mock_retrieve.return_value = [
        {"chunk_text": "内容", "title": "手册", "doc_id": "D1", "score": 9.0}]
    resp = client.post("/api/ask/stream", json={"question": "测试"})
    assert resp.status_code == 200
    kw = mock_log.call_args.kwargs
    assert kw["gen_meta_json"] and "deadbeefdeadbeef" in kw["gen_meta_json"]
    # gen_meta 只落库、绝不进 SSE 线协议（内部阈值/模型配置不外泄）
    assert "deadbeefdeadbeef" not in resp.text


# ═══════════════════════════════════════════════════════════════
# 5. embedding 契约（P2-8）：match / mismatch / 表缺失 + /api/ready 降级
# ═══════════════════════════════════════════════════════════════

def _contract_cfg(model="text-embedding-v4", dim=1024):
    return SimpleNamespace(
        simulate=False, simulate_db=False,
        rds=SimpleNamespace(operation_database="fuling_operation"),
        embedding=SimpleNamespace(model=model, dimension=dim),
    )


def _conn_with_rows(rows):
    conn, cur = _mock_conn()
    cur.fetchall.return_value = rows
    return conn


@patch("opensearch_pipeline.db._get_db_conn")
def test_contract_match_returns_none(mock_get_conn):
    from opensearch_pipeline.runtime_contract import check_embedding_contract
    mock_get_conn.return_value = _conn_with_rows(
        [("embedding_model", "text-embedding-v4"), ("embedding_dimension", "1024")])
    assert check_embedding_contract(cfg=_contract_cfg()) is None


@patch("opensearch_pipeline.db._get_db_conn")
def test_contract_mismatch_reports_description(mock_get_conn):
    from opensearch_pipeline.runtime_contract import check_embedding_contract
    # v4→v5 同维升级：模型漂移必须被点名（这正是 P2-8 的静默垃圾相似度场景）
    mock_get_conn.return_value = _conn_with_rows(
        [("embedding_model", "text-embedding-v5"), ("embedding_dimension", "1024")])
    desc = check_embedding_contract(cfg=_contract_cfg())
    assert desc and "text-embedding-v5" in desc and "text-embedding-v4" in desc
    # 维度失配同样点名
    mock_get_conn.return_value = _conn_with_rows(
        [("embedding_model", "text-embedding-v4"), ("embedding_dimension", "2048")])
    desc2 = check_embedding_contract(cfg=_contract_cfg())
    assert desc2 and "2048" in desc2


@patch("opensearch_pipeline.db._get_db_conn")
def test_contract_table_missing_or_empty_silent(mock_get_conn):
    """表未 apply（1146）/无契约行（从未真实摄取）→ None，绝不误报、绝不抛。"""
    from opensearch_pipeline.runtime_contract import check_embedding_contract
    conn, cur = _mock_conn()
    cur.execute.side_effect = Exception(
        1146, "Table 'fuling_operation.rag_runtime_contract' doesn't exist")
    mock_get_conn.return_value = conn
    assert check_embedding_contract(cfg=_contract_cfg()) is None
    # 空表（无契约行）
    mock_get_conn.return_value = _conn_with_rows([])
    assert check_embedding_contract(cfg=_contract_cfg()) is None
    # dict-cursor 行形态（prod_access 形态）也能比对
    mock_get_conn.return_value = _conn_with_rows(
        [{"contract_key": "embedding_model", "contract_value": "text-embedding-v4"},
         {"contract_key": "embedding_dimension", "contract_value": "1024"}])
    assert check_embedding_contract(cfg=_contract_cfg()) is None


@patch("opensearch_pipeline.db._get_db_conn")
def test_upsert_embedding_contract_writes_and_fails_open(mock_get_conn):
    from opensearch_pipeline.runtime_contract import upsert_embedding_contract
    conn, cur = _mock_conn()
    mock_get_conn.return_value = conn
    assert upsert_embedding_contract("text-embedding-v4", 1024) is True
    sql, params = cur.execute.call_args[0]
    assert "rag_runtime_contract" in sql and "ON DUPLICATE KEY UPDATE" in sql
    assert "text-embedding-v4" in params and "1024" in params
    conn.commit.assert_called_once()
    # 表缺失/连接失败 → fail-open False，绝不抛（摄取主流程不受影响）
    mock_get_conn.side_effect = RuntimeError("db down")
    assert upsert_embedding_contract("m", 1) is False


def test_api_ready_degrades_on_contract_mismatch(monkeypatch):
    """失配标志置位 → /api/ready 503 + embedding_contract=mismatch；复位 → 状态词 ok。"""
    from fastapi.testclient import TestClient
    import opensearch_pipeline.retriever as rt
    from opensearch_pipeline import api
    from opensearch_pipeline.config import get_config

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)   # 走真探针分支
    conn, _cur = _mock_conn()
    _cur.fetchone.return_value = (1,)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda **kw: conn)
    monkeypatch.setattr(rt, "_get_ha3_client", lambda: "MOCK_HA3_CLIENT")

    monkeypatch.setattr(api, "_EMBEDDING_CONTRACT_MISMATCH", True)
    r = TestClient(api.app).get("/api/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded" and body["embedding_contract"] == "mismatch"

    monkeypatch.setattr(api, "_EMBEDDING_CONTRACT_MISMATCH", False)
    r2 = TestClient(api.app).get("/api/ready")
    assert r2.json()["embedding_contract"] == "ok"
