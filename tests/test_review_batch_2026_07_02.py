# -*- coding: utf-8 -*-
"""test_review_batch_2026_07_02.py — 外审第一轮修复批的行为锁（全程 simulate）。

覆盖：
  P1-07  连接池获取有上限超时 → DBPoolExhausted；serving 端映射 503 DB_POOL_EXHAUSTED。
  P2-05  /api/ready 不回灌异常原文（仅 up/down + trace_id）；/api/version 不外泄 environment/simulate。
  P2-09  钉钉回调 msgId 事件级去重（首见放行 / 窗口内重放拦截 / 过期后重放视为新 / 空 id 不去重）。
  P1-09  >上限页数的 PDF 被标 extract_truncated + 回写真实总页数 + TRUNCATED 警告。
"""
import asyncio

import pytest


# ───────────────────────── P1-07：连接池超时 → 503 ─────────────────────────

def test_acquire_pool_exhausted_raises_dbpoolexhausted(monkeypatch):
    from opensearch_pipeline import db
    from dbutils.pooled_db import TooManyConnections

    class _FullPool:
        def connection(self):
            raise TooManyConnections()

    monkeypatch.setattr(db, "_db_pool", _FullPool())
    monkeypatch.setenv("RAG_DB_ACQUIRE_TIMEOUT", "0.1")   # 快速超时
    with pytest.raises(db.DBPoolExhausted):
        db._acquire_pool_connection()


def test_acquire_pool_no_timeout_delegates_to_blocking(monkeypatch):
    """RAG_DB_ACQUIRE_TIMEOUT≤0 → 恢复旧的 blocking 取连接（直接透传 pool.connection()）。"""
    from opensearch_pipeline import db
    sentinel = object()

    class _Pool:
        def connection(self):
            return sentinel

    monkeypatch.setattr(db, "_db_pool", _Pool())
    monkeypatch.setenv("RAG_DB_ACQUIRE_TIMEOUT", "0")
    assert db._acquire_pool_connection() is sentinel


def test_dbpoolexhausted_maps_to_503():
    from opensearch_pipeline.api import _db_pool_exhausted_handler
    from opensearch_pipeline.db import DBPoolExhausted

    resp = asyncio.run(_db_pool_exhausted_handler(None, DBPoolExhausted("pool full")))
    assert resp.status_code == 503
    import json
    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert body["code"] == "DB_POOL_EXHAUSTED"
    assert resp.headers.get("Retry-After") == "2"


# ───────────────────────── P2-05：探针脱敏 ─────────────────────────

def test_readiness_does_not_leak_exception_text(monkeypatch):
    """RDS 探针失败时只回 rds='error' + trace_id，绝不回灌异常原文（host/驱动错误）。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)   # 强制走 live 探针分支

    from opensearch_pipeline import db as dbmod
    from opensearch_pipeline import retriever

    secret = "rm-bp15j7wekd5738f093o.rwlb.rds.aliyuncs.com pymysql OperationalError"

    def _boom(*a, **k):
        raise RuntimeError(secret)

    monkeypatch.setattr(dbmod, "_get_db_conn", _boom)
    monkeypatch.setattr(retriever, "_get_ha3_client", lambda: "MOCK_HA3_CLIENT")  # HA3 跳过

    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    r = TestClient(app).get("/api/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["rds"] == "error"          # 组件 up/down 保留
    assert body.get("trace_id")            # 对账用 trace_id
    assert secret not in r.text            # 异常原文绝不外泄
    assert "aliyuncs" not in r.text


def test_version_endpoint_hides_deployment_shape():
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    body = TestClient(app).get("/api/version").json()
    assert body.get("git_commit") and "embedding_model_version" in body
    assert "environment" not in body and "simulate" not in body


# ───────────────────────── P2-09：钉钉 msgId 去重 ─────────────────────────

def test_msgid_dedup_window(monkeypatch):
    from opensearch_pipeline import dingtalk_bot as dbot
    dbot._seen_msg_ids.clear()
    clock = {"t": 1000.0}
    monkeypatch.setattr(dbot.time, "time", lambda: clock["t"])

    assert dbot._is_duplicate_msg("MSG_A") is False   # 首见放行
    assert dbot._is_duplicate_msg("MSG_A") is True    # 窗口内重放拦截
    assert dbot._is_duplicate_msg("MSG_B") is False   # 不同 id 放行
    assert dbot._is_duplicate_msg("") is False        # 空 id 无法去重
    assert dbot._is_duplicate_msg("?") is False       # 缺失占位不去重

    clock["t"] = 1000.0 + dbot._TIMESTAMP_TOLERANCE + 1   # 过期
    assert dbot._is_duplicate_msg("MSG_A") is False   # 过期后重放视为新消息


def test_webhook_body_replay_returns_duplicate(monkeypatch):
    from opensearch_pipeline import dingtalk_bot as dbot
    dbot._seen_msg_ids.clear()
    body = {"msgId": "REPLAY_1", "conversationType": "2",
            "text": {"content": ""}, "sessionWebhook": ""}
    # 首次：空问题分支正常返回（非 duplicate）
    first = dbot._process_webhook_body(dict(body))
    assert first.get("msgtype") != "duplicate"
    # 重放同 msgId：命中去重，直接 duplicate，不再进入问答/回复
    second = dbot._process_webhook_body(dict(body))
    assert second == {"msgtype": "duplicate"}


# ───────────────────────── P1-09：PDF 页上限截断标注 ─────────────────────────

def test_pdf_truncation_flag_set_for_oversize(monkeypatch):
    from opensearch_pipeline.extraction import unified_extractor as ue
    from opensearch_pipeline.extraction import pdf_extractor
    from opensearch_pipeline.extraction import image_extraction_utils
    from opensearch_pipeline.extraction.schema import ExtractedBlock

    ex = ue.UnifiedExtractor(simulate=True)
    # G2 后上限是实例属性（RAG_PDF_NATIVE_MAX_PAGES，默认 200）；固定为 20 使测试与默认值解耦。
    ex.pdf_native_max_pages = 20
    # 桩掉重子步骤：extract_pdf 返回 37 页；无嵌入图；跳过 OCR。
    monkeypatch.setattr(pdf_extractor, "extract_pdf",
                        lambda path, **k: ([ExtractedBlock("paragraph", "前 20 页正文")], 37, []))
    monkeypatch.setattr(image_extraction_utils, "extract_images_from_pdf",
                        lambda *a, **k: [])
    monkeypatch.setattr(ex, "_pages_needing_ocr", lambda r: [])

    res = ex._extract_pdf({
        "doc_id": "D", "version_no": 1, "raw_key": "raw/x.pdf",
        "filename": "x.pdf", "local_path": "/tmp/x.pdf",
    })
    assert res.extract_truncated is True
    assert res.extracted_pages == 20
    assert res.page_count == 37                                             # 真实总页数回写
    assert any("TRUNCATED" in w for w in res.warnings)


def test_pdf_not_truncated_when_within_cap(monkeypatch):
    from opensearch_pipeline.extraction import unified_extractor as ue
    from opensearch_pipeline.extraction import pdf_extractor
    from opensearch_pipeline.extraction import image_extraction_utils
    from opensearch_pipeline.extraction.schema import ExtractedBlock

    ex = ue.UnifiedExtractor(simulate=True)
    monkeypatch.setattr(pdf_extractor, "extract_pdf",
                        lambda path, **k: ([ExtractedBlock("paragraph", "短文档")], 5, []))
    monkeypatch.setattr(image_extraction_utils, "extract_images_from_pdf",
                        lambda *a, **k: [])
    monkeypatch.setattr(ex, "_pages_needing_ocr", lambda r: [])

    res = ex._extract_pdf({
        "doc_id": "D", "version_no": 1, "raw_key": "raw/y.pdf",
        "filename": "y.pdf", "local_path": "/tmp/y.pdf",
    })
    assert res.extract_truncated is False
    assert res.extracted_pages is None


# ───────────────────────── P2-04：上传/会话签名密钥拆分 ─────────────────────────

def test_upload_key_falls_back_to_session_when_unset(monkeypatch):
    """未配置 RAG_UPLOAD_SIGNING_KEY → 上传令牌回退会话密钥（向后兼容，行为同拆分前）。"""
    from opensearch_pipeline import auth_token as at
    monkeypatch.delenv("RAG_UPLOAD_SIGNING_KEY", raising=False)
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "s" * 40)
    at._reset_signing_key_cache()
    tok = at.sign_payload({"foo": "bar"}, ttl=300)
    assert at.verify_payload(tok) is not None


def test_upload_token_uses_upload_key_isolated_from_session(monkeypatch):
    """配置独立上传密钥后：上传令牌验签走上传密钥（旋转它即失效），会话令牌不受影响（双向隔离）。"""
    from opensearch_pipeline import auth_token as at
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "s" * 40)
    monkeypatch.setenv("RAG_UPLOAD_SIGNING_KEY", "u" * 40)
    at._reset_signing_key_cache()
    tok = at.sign_payload({"foo": "bar"}, ttl=300)
    assert at.verify_payload(tok) is not None

    monkeypatch.setenv("RAG_UPLOAD_SIGNING_KEY", "u2" * 20)   # 旋转上传密钥
    at._reset_signing_key_cache()
    assert at.verify_payload(tok) is None                     # 旧上传令牌失效 → 证明验签用上传密钥

    session_tok = at.issue_session_token("U1", dept=["marketing"])   # 会话令牌不受上传密钥旋转影响
    assert at.verify_session_token(session_tok) is not None
    at._reset_signing_key_cache()


# ───────────────────────── P1-03：成本路径限流 fail-closed ─────────────────────────

def test_ratelimit_ask_fail_closed_on_limiter_error(monkeypatch):
    """限流器抛异常时：问答/检索（成本路径）fail-CLOSED → 503；辅助端点 fail-open 放行。"""
    from opensearch_pipeline import api
    from fastapi import HTTPException

    class _BoomLimiter:
        def admit_ask(self, *a, **k):
            raise RuntimeError("limiter broken")

        def admit_aux(self, *a, **k):
            raise RuntimeError("limiter broken")

    monkeypatch.setattr(api, "LIMITER", _BoomLimiter())

    with pytest.raises(HTTPException) as ei:
        api._enforce_rate_limit(None, None, scope="ask")
    assert ei.value.status_code == 503

    # 辅助端点（无成本）→ fail-open：不抛，正常放行
    assert api._enforce_rate_limit(None, None, scope="aux") is None


# ───────────────────────── P2-01：间接 prompt injection 防护 ─────────────────────────

def test_injection_rule_off_by_default_byte_identical():
    """默认 OFF（与全项目 prompt-flag 约定一致）→ system prompt 逐字节不变，不动 eval 基线。"""
    from opensearch_pipeline import llm_generator as G
    msgs = G._build_messages("q", "ctx")
    assert msgs[0]["content"] == G.DEFAULT_SYSTEM_PROMPT
    assert G._PROMPT_INJECTION_RULE not in msgs[0]["content"]


def test_injection_rule_appended_when_enabled(monkeypatch):
    """开启后：把「参考文档=不可信数据」边界追加到一切规则之后。"""
    from opensearch_pipeline import llm_generator as G
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config().rag, "prompt_injection_guard", True)
    msgs = G._build_messages("q", "ctx")
    sysmsg = msgs[0]["content"]
    assert G._PROMPT_INJECTION_RULE in sysmsg
    assert "不可信数据" in sysmsg
    assert G._PROMPT_INJECTION_RULE not in G.DEFAULT_SYSTEM_PROMPT   # 模块常量未被污染


# ───────────────────────── P2-02：权限字段缺失 fail-closed ─────────────────────────

def test_ha3_parse_missing_permission_defaults_restricted():
    """HA3 命中缺 permission_level（投影漂移）→ 解析默认 restricted，绝不当 public。"""
    from opensearch_pipeline import retriever as R
    resp = type("R", (), {"body": {"result": [
        {"fields": {"chunk_id": "c1", "doc_id": "d1", "chunk_text_store": "x"}},   # 无 permission_level
    ]}})()
    parsed = R._parse_ha3_response(resp)
    assert parsed[0]["permission_level"] == "restricted"


def test_same_permission_fail_closed_on_missing():
    """邻居拼接：权限缺失按 restricted → 与已通过权限的 public 中心不同 → 不拼接（fail-closed）。"""
    from opensearch_pipeline import retriever as R
    assert R._same_permission({"owner_dept": "hr"}, {"permission_level": "public", "owner_dept": "hr"}) is False
    assert R._same_permission({"owner_dept": "hr"}, {"owner_dept": "hr"}) is True   # 两侧都缺→都 restricted→一致


def test_cross_dept_widened_to_nonpublic_fail_closed(monkeypatch):
    """Phase-D 授权开时：非 public 的 owner-不匹配命中（含缺失→restricted）走 fail-closed 授权复核。"""
    from opensearch_pipeline import retriever as R
    from opensearch_pipeline.config import get_config
    import opensearch_pipeline.db as db
    import opensearch_pipeline.access_grants as ag

    monkeypatch.setattr(get_config().rag, "allowed_depts_acl", True)

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass

    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass

    monkeypatch.setattr(db, "_get_db_conn", lambda *a, **k: _Conn())
    monkeypatch.setattr(ag, "resolve_allowed_depts", lambda missing, cur: {})   # 无任何授权

    results = [
        {"doc_id": "d1", "permission_level": "public", "owner_dept": "finance"},        # public → 保留
        {"doc_id": "d2", "permission_level": "restricted", "owner_dept": "finance"},    # 非 public + 跨部门 → 丢
        {"doc_id": "d3", "permission_level": "dept_internal", "owner_dept": "marketing"},  # owner 命中 → 保留
    ]
    out = R._deny_revoked_cross_dept(results, ["marketing"])
    assert {r["doc_id"] for r in out} == {"d1", "d3"}
