# -*- coding: utf-8 -*-
"""报告1 P0 加固（2026-07-10 外审）——全 flag 门控，默认 off 时现网零变化。

- P0-03 RAG_REQUIRE_AUTH：4 端点匿名 401 / 带 token 200；hot-questions 保持公开。
- P0-04 RAG_ACL_FAIL_CLOSED：current_identity DB 失败降 public（无记录保留令牌组）；
  主命中 RDS 复核不可用只留 public。
- P0-02 RDSConfig.pymysql_ssl_args：默认空 {} / 配 CA 后传 ssl。
默认 off 的行为不变由既有 test_api_rate_limit / test_miniapp_serving 覆盖（匿名仍 200）。
"""

import pytest
from fastapi.testclient import TestClient

import opensearch_pipeline.api as api
from opensearch_pipeline import rate_limiter as rl
from opensearch_pipeline.auth_token import issue_session_token

FAKE_CHUNKS = [{"chunk_id": "c1", "chunk_text": "内容", "doc_id": "d1",
                "permission_level": "public", "owner_dept": "行政部"}]
FAKE_ANSWER = {"answer": "答", "sources": [], "content_blocks": []}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "false")   # 隔离认证语义，不掺限流
    rl.LIMITER.reset_for_tests()
    monkeypatch.setattr(api, "retrieve_and_enrich", lambda *a, **kw: list(FAKE_CHUNKS))
    monkeypatch.setattr(api, "generate_answer", lambda *a, **kw: dict(FAKE_ANSWER))
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)
    monkeypatch.setattr(api, "_append_to_history", lambda *a, **kw: None)
    yield
    rl.LIMITER.reset_for_tests()


def _bearer(uid="U1", dept="行政部"):
    return {"Authorization": "Bearer " + issue_session_token(uid, dept=dept, name="测试")}


# ── P0-03 强制认证 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/search", {"query": "问"}),
    ("post", "/api/ask", {"question": "问"}),
    ("post", "/api/resign-images", {"oss_keys": ["k1"]}),
])
def test_require_auth_blocks_anon(monkeypatch, method, path, body):
    monkeypatch.setenv("RAG_REQUIRE_AUTH", "true")
    c = TestClient(api.app, raise_server_exceptions=False)
    # 匿名 → 401
    r = getattr(c, method)(path, json=body)
    assert r.status_code == 401
    # 带 token → 不是 401（放行到业务，可能 200 或业务码，但绝非认证拒绝）
    r2 = getattr(c, method)(path, json=body, headers=_bearer())
    assert r2.status_code != 401


def test_require_auth_stream_blocks_anon(monkeypatch):
    monkeypatch.setenv("RAG_REQUIRE_AUTH", "true")
    c = TestClient(api.app, raise_server_exceptions=False)
    assert c.post("/api/ask/stream", json={"question": "问"}).status_code == 401


def test_require_auth_off_allows_anon(monkeypatch):
    """默认 off：匿名照常放行（现网行为不变）。"""
    monkeypatch.delenv("RAG_REQUIRE_AUTH", raising=False)
    c = TestClient(api.app, raise_server_exceptions=False)
    assert c.post("/api/ask", json={"question": "问"}).status_code != 401


def test_hot_questions_stays_public_under_require_auth(monkeypatch):
    """hot-questions 不在强制认证名单——匿名已 fail-safe（dept_key=None 只给静态兜底）。"""
    monkeypatch.setenv("RAG_REQUIRE_AUTH", "true")
    c = TestClient(api.app, raise_server_exceptions=False)
    assert c.get("/api/hot-questions").status_code == 200


# ── P0-04 ACL fail-closed（current_identity）──────────────────────────────────


def _identity_groups(monkeypatch, *, db_ok, live_groups, strict):
    """构造 current_identity：令牌内嵌 行政部(admin)，_resolve_user_dept_cached 打桩。"""
    if strict:
        monkeypatch.setenv("RAG_ACL_FAIL_CLOSED", "true")
    else:
        monkeypatch.delenv("RAG_ACL_FAIL_CLOSED", raising=False)
    monkeypatch.setenv("RAG_LIVE_ACL_REREAD", "true")
    monkeypatch.setattr(
        "opensearch_pipeline.dingtalk_identity._resolve_user_dept_cached",
        lambda uid, with_status=False: (live_groups, db_ok) if with_status else live_groups)
    token = issue_session_token("U1", dept="行政部", name="测试")
    ident = api.current_identity(authorization="Bearer " + token)
    return ident.acl_groups


def test_failclosed_db_error_downgrades_to_public(monkeypatch):
    """strict + DB 失败 → 降级仅 public（丢令牌内嵌组）。"""
    groups = _identity_groups(monkeypatch, db_ok=False, live_groups=None, strict=True)
    assert groups == []


def test_failclosed_no_record_keeps_token_groups(monkeypatch):
    """strict + 无在册行（db_ok=True, live=None）→ 保留令牌组（不因未缓存降级）。"""
    groups = _identity_groups(monkeypatch, db_ok=True, live_groups=None, strict=True)
    assert "行政部" in groups          # 保留令牌内嵌组（原始 dept 名，未因未缓存降级）


def test_failopen_db_error_keeps_token_groups(monkeypatch):
    """默认 off + DB 失败 → 保留令牌组（历史 fail-open，现网不变）。"""
    groups = _identity_groups(monkeypatch, db_ok=False, live_groups=None, strict=False)
    assert "行政部" in groups


# ── P0-04 ACL fail-closed（主命中复核）────────────────────────────────────────


def test_revalidate_strict_keeps_public_only(monkeypatch):
    from opensearch_pipeline import retriever
    results = [{"chunk_id": "a", "permission_level": "public"},
               {"chunk_id": "b", "permission_level": "dept_internal", "owner_dept": "hr"}]
    monkeypatch.setenv("RAG_ACL_FAIL_CLOSED", "true")
    kept = retriever._keep_public_only_if_strict(results, "RDS 不可达")
    assert [r["chunk_id"] for r in kept] == ["a"]
    monkeypatch.delenv("RAG_ACL_FAIL_CLOSED", raising=False)
    assert retriever._keep_public_only_if_strict(results, "x") == results   # 默认保留全部


# ── P0-02 RDS SSL 字段 ────────────────────────────────────────────────────────


def test_rds_ssl_args_default_explicit_plaintext():
    """未配 CA = 显式明文（ssl_disabled=True），不能是空 kwargs：
    pymysql 2.x PREFERRED 模式会在服务端广播 SSL 能力位（RDS 2026-07-17 开通）后
    按客户端库/OpenSSL 版本自动尝试 TLS 且失败不回退——行为必须由配置决定。"""
    from opensearch_pipeline.config import RDSConfig
    assert RDSConfig().pymysql_ssl_args() == {"ssl_disabled": True}


def test_rds_ssl_args_with_ca(tmp_path):
    from opensearch_pipeline.config import RDSConfig
    ca = str(tmp_path / "ca.pem")
    args = RDSConfig(ssl_ca=ca, ssl_verify_cert=True).pymysql_ssl_args()
    assert args["ssl"]["ca"] == ca and args["ssl"]["check_hostname"] is True
    args2 = RDSConfig(ssl_ca=ca, ssl_verify_cert=False).pymysql_ssl_args()
    assert args2["ssl"]["check_hostname"] is False and args2["ssl_verify_cert"] is False
