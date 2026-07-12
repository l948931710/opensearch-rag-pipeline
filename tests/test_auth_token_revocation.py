# -*- coding: utf-8 -*-
"""会话令牌撤销杠杆（P0-05 报告1「停用用户旧 token 仍持组读到 exp」）。

两把杠杆都默认不设=零行为变化；设了即时生效（verify 现读 env）：
- RAG_SESSION_TOKEN_MIN_IAT：iat 早于 epoch 的令牌全拒（含无 iat 旧令牌）；
- RAG_REVOKED_USER_IDS：名单内用户令牌全拒。
"""
import time

import pytest

from opensearch_pipeline.auth_token import issue_session_token, verify_session_token


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("RAG_SESSION_TOKEN_MIN_IAT", raising=False)
    monkeypatch.delenv("RAG_REVOKED_USER_IDS", raising=False)


def test_default_no_revocation_env_token_still_valid():
    tok = issue_session_token("u1", dept=["marketing"])
    payload = verify_session_token(tok)
    assert payload is not None and payload["uid"] == "u1"
    assert payload.get("iat")   # 新令牌带签发时刻


def test_min_iat_rejects_older_tokens(monkeypatch):
    tok = issue_session_token("u1", dept=["marketing"])
    assert verify_session_token(tok) is not None
    # epoch 设到未来：刚签发的令牌也被作废（全局登出止血阀语义）
    monkeypatch.setenv("RAG_SESSION_TOKEN_MIN_IAT", str(int(time.time()) + 60))
    assert verify_session_token(tok) is None
    # epoch 设到过去：令牌照常有效
    monkeypatch.setenv("RAG_SESSION_TOKEN_MIN_IAT", str(int(time.time()) - 3600))
    assert verify_session_token(tok) is not None


def test_min_iat_rejects_legacy_tokens_without_iat(monkeypatch):
    """无 iat 的旧令牌：epoch 一旦设定即整体作废（epoch 的意义就是清场存量）。"""
    import opensearch_pipeline.auth_token as at
    # 手工构造无 iat 的旧格式令牌（同签名路径）
    import hashlib
    import hmac
    import json
    payload = {"typ": "session", "uid": "legacy", "acl_groups": ["hr"],
               "dept": "hr", "name": "", "exp": int(time.time()) + 600}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    b64 = at._b64url_encode(body)
    sig = hmac.new(at._get_signing_key(), b64.encode("ascii"), hashlib.sha256).digest()
    tok = f"{b64}.{at._b64url_encode(sig)}"
    assert verify_session_token(tok) is not None      # 未设 epoch：旧令牌照常
    monkeypatch.setenv("RAG_SESSION_TOKEN_MIN_IAT", str(int(time.time()) - 1))
    assert verify_session_token(tok) is None          # 设了 epoch：无 iat 一律拒


def test_min_iat_malformed_env_fails_closed(monkeypatch):
    tok = issue_session_token("u1")
    monkeypatch.setenv("RAG_SESSION_TOKEN_MIN_IAT", "not-a-number")
    assert verify_session_token(tok) is None


def test_revoked_user_ids_csv(monkeypatch):
    tok_a = issue_session_token("alice", dept=["marketing"])
    tok_b = issue_session_token("bob", dept=["hr"])
    monkeypatch.setenv("RAG_REVOKED_USER_IDS", "alice, carol")
    assert verify_session_token(tok_a) is None        # 名单内：拒
    payload = verify_session_token(tok_b)
    assert payload is not None and payload["uid"] == "bob"   # 名单外：不受影响
