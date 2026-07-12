# -*- coding: utf-8 -*-
"""P1-3 DingTalk 三根因加固（重评审计 2026-07-11）。

① HTTP 面下线闸：RAG_DINGTALK_HTTP_ENDPOINTS_ENABLE=false → /dingtalk/webhook 与
   /dingtalk/card/callback 一律 404（prod=Stream，合法流量不经公网 HTTP；「签名不绑
   body 的窗口内伪造」「card 回调无签名」两个攻击面随端点消失）；
② card 回调验签：required 模式非 True 一律 403；shadow（默认）只记日志放行；
③ card 路径 replay 去重：同一回调体窗口内二投直接吞（claim→confirm/release 两阶段）。
（归属校验 fail-open→fail-closed 的翻转在 test_dingtalk_card_authz_empty_userid /
test_dingtalk_streaming 既有测试中更新断言。）
"""
import base64
import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient

from opensearch_pipeline import api, dingtalk_bot


@pytest.fixture()
def client():
    return TestClient(api.app)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("RAG_DINGTALK_HTTP_ENDPOINTS_ENABLE", raising=False)
    monkeypatch.delenv("RAG_DINGTALK_CARD_SIG_REQUIRED", raising=False)
    monkeypatch.delenv("DINGTALK_CARD_CALLBACK_API_SECRET", raising=False)
    # card 回调去重表清场（跨用例不串窗）
    with dingtalk_bot._seen_msg_lock:
        dingtalk_bot._seen_msg_ids.clear()


def _card_sign(secret: str, ts_ms: int) -> str:
    return base64.b64encode(hmac.new(secret.encode("utf-8"),
                                     f"{ts_ms}\n{secret}".encode("utf-8"),
                                     hashlib.sha256).digest()).decode("utf-8")


# ── ① HTTP 面下线闸 ─────────────────────────────────────────────────────────────


def test_http_endpoints_disabled_404(client, monkeypatch):
    monkeypatch.setenv("RAG_DINGTALK_HTTP_ENDPOINTS_ENABLE", "false")
    assert client.post("/dingtalk/webhook", json={}).status_code == 404
    assert client.post("/dingtalk/card/callback", json={}).status_code == 404


def test_http_endpoints_default_on(client, monkeypatch):
    """默认 on=历史行为：webhook 走到签名校验（403 而非 404）。"""
    monkeypatch.setenv("DINGTALK_APP_SECRET", "s3cret")
    r = client.post("/dingtalk/webhook", json={})
    assert r.status_code == 403                       # 无签名 → 签名校验拒，端点在位


# ── ② card 回调验签 ─────────────────────────────────────────────────────────────


def _cb_body():
    return {"outTrackId": "m-x", "userId": "u1",
            "content": "{\"cardPrivateData\":{\"params\":{\"action\":\"upvote\","
                       "\"message_id\":\"m-x\"}}}"}


@pytest.fixture()
def _authorized_noop(monkeypatch):
    """绕开归属校验与落库（本组只测签名/门控层）。"""
    monkeypatch.setattr(dingtalk_bot, "_card_callback_authorized",
                        lambda mid, uid, prefetch=None: False)   # 拒写但 200 ACK


def test_card_sig_required_rejects_unsigned_and_bad(client, monkeypatch, _authorized_noop):
    monkeypatch.setenv("RAG_DINGTALK_CARD_SIG_REQUIRED", "true")
    monkeypatch.setenv("DINGTALK_CARD_CALLBACK_API_SECRET", "cb-secret")
    assert client.post("/dingtalk/card/callback", json=_cb_body()).status_code == 403
    ts = int(time.time() * 1000)
    r = client.post("/dingtalk/card/callback", json=_cb_body(),
                    headers={"x-ddpaas-signature": "bad", "x-ddpaas-signature-timestamp": str(ts)})
    assert r.status_code == 403


def test_card_sig_required_accepts_valid(client, monkeypatch, _authorized_noop):
    monkeypatch.setenv("RAG_DINGTALK_CARD_SIG_REQUIRED", "true")
    monkeypatch.setenv("DINGTALK_CARD_CALLBACK_API_SECRET", "cb-secret")
    ts = int(time.time() * 1000)
    r = client.post("/dingtalk/card/callback", json=_cb_body(),
                    headers={"x-ddpaas-signature": _card_sign("cb-secret", ts),
                             "x-ddpaas-signature-timestamp": str(ts)})
    assert r.status_code == 200


def test_card_sig_required_rejects_stale_timestamp(client, monkeypatch, _authorized_noop):
    monkeypatch.setenv("RAG_DINGTALK_CARD_SIG_REQUIRED", "true")
    monkeypatch.setenv("DINGTALK_CARD_CALLBACK_API_SECRET", "cb-secret")
    stale = int(time.time() * 1000) - (dingtalk_bot._TIMESTAMP_TOLERANCE + 60) * 1000
    r = client.post("/dingtalk/card/callback", json=_cb_body(),
                    headers={"x-ddpaas-signature": _card_sign("cb-secret", stale),
                             "x-ddpaas-signature-timestamp": str(stale)})
    assert r.status_code == 403


def test_card_sig_shadow_mode_allows_unsigned(client, monkeypatch, _authorized_noop):
    """默认 shadow：未验签放行（记日志）——先核对真实头名/算法再开强制。"""
    monkeypatch.setenv("DINGTALK_CARD_CALLBACK_API_SECRET", "cb-secret")
    assert client.post("/dingtalk/card/callback", json=_cb_body()).status_code == 200


# ── ③ card 路径 replay 去重 ─────────────────────────────────────────────────────


def test_card_callback_replay_deduped(monkeypatch):
    calls = {"n": 0}

    def _inner(body):
        calls["n"] += 1
        return {"msgtype": "empty"}

    monkeypatch.setattr(dingtalk_bot, "_process_card_callback_inner", _inner)
    body = {"outTrackId": "m-r", "userId": "u1", "content": "{}"}
    assert dingtalk_bot._process_card_callback_body(dict(body)) == {"msgtype": "empty"}
    assert dingtalk_bot._process_card_callback_body(dict(body)) == {"msgtype": "duplicate"}
    assert calls["n"] == 1
    # 不同 body（换 action/用户）不受影响
    other = {"outTrackId": "m-r2", "userId": "u2", "content": "{}"}
    assert dingtalk_bot._process_card_callback_body(other) == {"msgtype": "empty"}
    assert calls["n"] == 2


def test_card_callback_failure_releases_claim(monkeypatch):
    """处理失败释放领用：重投可重试，不永久吞回调（两阶段与 webhook 同款）。"""
    box = {"fail": True}

    def _inner(body):
        if box["fail"]:
            raise RuntimeError("transient")
        return {"msgtype": "empty"}

    monkeypatch.setattr(dingtalk_bot, "_process_card_callback_inner", _inner)
    body = {"outTrackId": "m-f", "userId": "u1", "content": "{}"}
    with pytest.raises(RuntimeError):
        dingtalk_bot._process_card_callback_body(dict(body))
    box["fail"] = False
    assert dingtalk_bot._process_card_callback_body(dict(body)) == {"msgtype": "empty"}
