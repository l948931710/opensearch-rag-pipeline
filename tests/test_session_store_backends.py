# -*- coding: utf-8 -*-
"""
test_session_store_backends.py — session_store 双后端契约测试（WS0-2）

memory 与 redis 两后端跑**同一套断言**，保证行为一致（含 owner 归属、2N 裁剪、
trusted 重建、幂等 clear）。redis 后端用 fakeredis 高保真模拟（含 WATCH/MULTI 事务）。

参照 plan WS0-2 + WS0-Pre re-baseline A2（owner 归属不能在 redis 后端丢失）。
"""
import fakeredis
import pytest

from opensearch_pipeline import session_store as ss
from opensearch_pipeline.session_store import (
    MAX_HISTORY_TURNS,
    MemorySessionStore,
    RedisSessionStore,
    SessionOwnershipError,
)


@pytest.fixture(params=["memory", "redis"])
def store(request):
    if request.param == "memory":
        return MemorySessionStore()
    return RedisSessionStore(fakeredis.FakeStrictRedis(decode_responses=True))


# ── 基础生命周期 ────────────────────────────────────────────────────────────
def test_anonymous_create_returns_uuid_and_empty(store):
    sid, history = store.get_or_create(None, owner=None)
    assert sid and len(sid) >= 8
    assert history == []


def test_get_or_create_existing_same_owner_returns_history(store):
    sid, _ = store.get_or_create("conv:staff", owner="staff")
    store.append(sid, "Q1", "A1", owner="staff")
    sid2, history = store.get_or_create(sid, owner="staff")
    assert sid2 == sid
    assert history == [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
    ]


def test_append_then_read_back(store):
    sid, _ = store.get_or_create("s1", owner="u1")
    store.append(sid, "hello", "hi there", owner="u1")
    hist = store.get_history(sid, owner="u1")
    assert hist == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_clear_returns_true_then_fresh(store):
    sid, _ = store.get_or_create("s2", owner="u1")
    store.append(sid, "q", "a", owner="u1")
    assert store.clear(sid, owner="u1") is True
    # clear 后再取应为空历史
    _, hist = store.get_or_create(sid, owner="u1")
    assert hist == []


def test_clear_nonexistent_returns_false(store):
    assert store.clear("never-existed", owner="u1") is False


# ── owner 归属校验（re-baseline A2 的核心） ─────────────────────────────────
def test_owner_mismatch_not_trusted_raises(store):
    store.get_or_create("conv:alice", owner="alice")
    with pytest.raises(SessionOwnershipError):
        store.get_or_create("conv:alice", owner="mallory")


def test_owner_mismatch_trusted_rebuilds(store):
    sid, _ = store.get_or_create("conv:alice", owner="alice")
    store.append(sid, "secret", "confidential", owner="alice")
    # trusted 来源（已验签钉钉回调）遇归属不符 → 丢弃重建，不泄露旧上下文
    sid2, hist = store.get_or_create("conv:alice", owner="mallory", trusted=True)
    assert sid2 == "conv:alice"
    assert hist == []


def test_anonymous_binds_on_first_identified_access(store):
    # 匿名创建（无 owner），随后带身份访问 → 就地绑定；此后他人访问被拒
    sid, _ = store.get_or_create(None, owner=None)
    store.get_or_create(sid, owner="u1")  # 绑定到 u1
    with pytest.raises(SessionOwnershipError):
        store.get_or_create(sid, owner="u2")


def test_append_owner_mismatch_raises(store):
    sid, _ = store.get_or_create("conv:bob", owner="bob")
    with pytest.raises(SessionOwnershipError):
        store.append(sid, "q", "a", owner="attacker")


def test_clear_owner_mismatch_raises(store):
    store.get_or_create("conv:carol", owner="carol")
    with pytest.raises(SessionOwnershipError):
        store.clear("conv:carol", owner="intruder")


def test_get_history_owner_mismatch_raises(store):
    sid, _ = store.get_or_create("conv:dave", owner="dave")
    store.append(sid, "q", "a", owner="dave")
    with pytest.raises(SessionOwnershipError):
        store.get_history(sid, owner="eve")


# ── 2N 裁剪 ─────────────────────────────────────────────────────────────────
def test_history_trimmed_to_2n(store):
    sid, _ = store.get_or_create("s3", owner="u1")
    turns = MAX_HISTORY_TURNS + 5
    for i in range(turns):
        store.append(sid, f"q{i}", f"a{i}", owner="u1")
    hist = store.get_history(sid, owner="u1")
    assert len(hist) == MAX_HISTORY_TURNS * 2  # 只保留最近 N 轮
    # 最后一条应是最新一轮的 assistant
    assert hist[-1] == {"role": "assistant", "content": f"a{turns - 1}"}
    assert hist[-2] == {"role": "user", "content": f"q{turns - 1}"}


# ── 模块级门面 + 工厂 ───────────────────────────────────────────────────────
def test_module_facade_delegates_to_backend():
    original = ss._backend
    try:
        ss._set_backend_for_test(RedisSessionStore(fakeredis.FakeStrictRedis(decode_responses=True)))
        sid, hist = ss.get_or_create_session(None, owner="u1")
        assert hist == []
        ss.append_to_history(sid, "q", "a", owner="u1")
        _, hist2 = ss.get_or_create_session(sid, owner="u1")
        assert hist2 == [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        assert ss.clear_session(sid, owner="u1") is True
    finally:
        ss._set_backend_for_test(original)


def test_default_backend_is_memory(monkeypatch):
    monkeypatch.delenv("RAG_SESSION_BACKEND", raising=False)
    assert isinstance(ss._make_backend(), MemorySessionStore)


def test_unknown_backend_falls_back_to_memory(monkeypatch):
    monkeypatch.setenv("RAG_SESSION_BACKEND", "cassandra")
    assert isinstance(ss._make_backend(), MemorySessionStore)


def test_redis_backend_selected(monkeypatch):
    monkeypatch.setenv("RAG_SESSION_BACKEND", "redis")
    from opensearch_pipeline import redis_client

    redis_client.set_client_for_test(fakeredis.FakeStrictRedis(decode_responses=True))
    try:
        backend = ss._make_backend()
        assert isinstance(backend, RedisSessionStore)
    finally:
        redis_client.set_client_for_test(None)
