# -*- coding: utf-8 -*-
"""P1-2 checkpoint 真实性/机密性（重评审计 2026-07-11）。

- digest：密钥在场 → `hmac1:<hmac_sha256>`（真实性——重算 sha256 绕不过）；无密钥回退
  裸 sha256（SIM/本地零配置，历史行为）；
- 迁移窗口：legacy 裸 sha256 行在密钥在场时仍可核对；REQUIRE_HMAC=true 拒绝降级；
- 加密：RAG_AGENT_CHECKPOINT_ENCRYPT=true + 密钥 → blob=enc1:+base64(AES-GCM)，
  编解码往返；密钥缺失/密文损坏 → 解码抛错（executor 回滚认领）。
"""
import hashlib
import hmac as hmac_mod
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.approval import Approved
from opensearch_pipeline.agent_runtime.loop import (
    DefaultAgentLoop,
    ModelTurn,
    checkpoint_hmac_key,
    decode_checkpoint_state,
    encode_checkpoint,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_KEY", raising=False)
    monkeypatch.delenv("RAG_SESSION_SIGNING_KEY", raising=False)
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_ENCRYPT", raising=False)
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_REQUIRE_HMAC", raising=False)


def test_no_key_falls_back_to_plain_sha256():
    blob, digest = encode_checkpoint([{"role": "user", "content": "q"}])
    assert not digest.startswith("hmac1:")
    assert hashlib.sha256(blob).hexdigest() == digest


def test_key_present_emits_hmac_digest(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_KEY", "k1")
    blob, digest = encode_checkpoint([{"role": "user", "content": "q"}])
    assert digest.startswith("hmac1:")
    assert digest == "hmac1:" + hmac_mod.new(b"k1", blob, hashlib.sha256).hexdigest()
    # 明文（未开加密）仍可直接解码
    assert decode_checkpoint_state(blob)["messages"][0]["content"] == "q"


def test_key_derivation_from_session_signing_key(monkeypatch):
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "sess-key")
    derived = checkpoint_hmac_key()
    assert derived == hashlib.sha256(b"sess-key:agent-checkpoint").digest()
    # 专用键优先于派生键
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_KEY", "dedicated")
    assert checkpoint_hmac_key() == b"dedicated"


def _resume_store(blob, digest):
    class _Store:
        def __init__(self):
            self.transitions = []

        def transition(self, run_id, frm, to):
            self.transitions.append((frm, to))
            return True

        def load_latest_checkpoint(self, run_id):
            return SimpleNamespace(checkpoint_id="cp1", state_blob=blob, state_digest=digest)

        def consume_budget(self, run_id, **kw):
            return {}

    return _Store()


def _try_resume(store):
    from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
    from opensearch_pipeline.agent_runtime.tool import ToolResult
    ex = ThreadedRunExecutor(store, lambda ctx, ev: ToolResult.text_ok("x"), max_concurrent=2)
    try:
        from opensearch_pipeline.agent_runtime.context import ExecutionContext
        ctx = ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"],
                                      roles=["employee"], channel="console", thread_id="t")
        h = ex.resume("run1", ctx, Approved(),
                      DefaultAgentLoop(lambda m, t: ModelTurn(text="a")), [])
        list(h.events())
    finally:
        ex.shutdown(wait=True)


def test_hmac_tamper_rejected_and_rolls_back(monkeypatch):
    from opensearch_pipeline.agent_runtime.executor import RunRejected
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_KEY", "k1")
    blob, digest = encode_checkpoint([{"role": "user", "content": "q"}],
                                     pending_call=None, turn=0)
    tampered = blob.replace(b'"q"', b'"HACKED"')
    # 攻击者重算裸 sha256 也没用——digest 是 HMAC，密钥算不出
    store = _resume_store(tampered, digest)
    with pytest.raises(RunRejected, match="真实性"):
        _try_resume(store)
    assert ("resuming", "suspended") in store.transitions


def test_legacy_sha256_row_verifies_unless_require_hmac(monkeypatch):
    from opensearch_pipeline.agent_runtime.executor import RunRejected
    # 无密钥时代写下的 legacy 行
    blob, digest = encode_checkpoint([{"role": "user", "content": "q"}])
    assert not digest.startswith("hmac1:")
    # 密钥上线后（迁移窗口）：legacy 行仍可续跑
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_KEY", "k1")
    _try_resume(_resume_store(blob, digest))          # 不抛
    # REQUIRE_HMAC：拒绝降级（防「把摘要改回裸 sha256」绕过 HMAC）
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_REQUIRE_HMAC", "true")
    store = _resume_store(blob, digest)
    with pytest.raises(RunRejected, match="降级"):
        _try_resume(store)
    assert ("resuming", "suspended") in store.transitions


def test_encrypt_roundtrip_and_key_required(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_KEY", "k1")
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_ENCRYPT", "true")
    pytest.importorskip("cryptography")
    msgs = [{"role": "user", "content": "机密问题"}]
    blob, digest = encode_checkpoint(msgs, pending_call={"call_id": "c", "tool_name": "w",
                                                         "arguments": {"qty": 7}}, turn=1)
    assert blob.startswith(b"enc1:")
    assert "机密问题".encode("utf-8") not in blob     # 明文不落盘
    assert digest.startswith("hmac1:")                 # encrypt-then-MAC：HMAC 对密文算
    state = decode_checkpoint_state(blob)
    assert state["messages"] == msgs and state["pending_call"]["arguments"] == {"qty": 7}
    # 无密钥进程读密文 → 显式抛错（不是静默坏数据）
    monkeypatch.delenv("RAG_AGENT_CHECKPOINT_KEY", raising=False)
    with pytest.raises(ValueError, match="无密钥"):
        decode_checkpoint_state(blob)
    # 换密钥 → AES-GCM 认证失败抛错
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_KEY", "other-key")
    with pytest.raises(Exception):
        decode_checkpoint_state(blob)


def test_encrypt_flag_off_stays_plaintext(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_CHECKPOINT_KEY", "k1")
    blob, _ = encode_checkpoint([{"role": "user", "content": "q"}])
    assert not blob.startswith(b"enc1:")
    assert b'"q"' in blob
