# -*- coding: utf-8 -*-
"""
test_token_cache_backends.py — 钉钉 access token 缓存双后端（WS0-3 收口）

memory 后端（默认）：进程内 L1，行为不变。
redis 后端：L2 多实例共享刷新（一个实例刷新，其余读共享 token）+ SETNX 防惊群；L1 保留。
所有测试 stub 掉 _refresh_token_from_api，隔离真实钉钉 API。
"""
import fakeredis
import pytest

from opensearch_pipeline import dingtalk_card as dc
from opensearch_pipeline import redis_client


@pytest.fixture(autouse=True)
def _reset():
    dc._cached_token = None
    dc._token_expires_at = 0
    yield
    dc._cached_token = None
    dc._token_expires_at = 0
    redis_client.set_client_for_test(None)


def _stub_api(monkeypatch, token="tok", expire_in=7200, counter=None):
    def _fake():
        if counter is not None:
            counter.append(1)
        return token, expire_in
    monkeypatch.setattr(dc, "_refresh_token_from_api", _fake)


# ── memory 后端（默认，零行为变化）──────────────────────────────
def test_memory_l1_caches(monkeypatch):
    monkeypatch.delenv("RAG_TOKEN_CACHE_BACKEND", raising=False)
    calls = []
    _stub_api(monkeypatch, "tok-A", 7200, calls)
    assert dc._get_access_token() == "tok-A"
    assert dc._get_access_token() == "tok-A"
    assert len(calls) == 1   # 第二次走 L1，不再调 API


def test_memory_none_when_refresh_fails(monkeypatch):
    monkeypatch.delenv("RAG_TOKEN_CACHE_BACKEND", raising=False)
    monkeypatch.setattr(dc, "_refresh_token_from_api", lambda: (None, 0))
    assert dc._get_access_token() is None


# ── redis 后端（L2 共享刷新）────────────────────────────────────
def test_redis_shared_refresh_once(monkeypatch):
    monkeypatch.setenv("RAG_TOKEN_CACHE_BACKEND", "redis")
    redis_client.set_client_for_test(fakeredis.FakeStrictRedis(decode_responses=True))
    calls = []
    _stub_api(monkeypatch, "tok-R", 7200, calls)
    # 实例1：L1 空 → L2 空 → 刷新 → 写 Redis+L1
    assert dc._get_access_token() == "tok-R"
    assert len(calls) == 1
    # 模拟"实例2"：清 L1，Redis 仍有 → 读共享 token，不再调 API
    dc._cached_token = None
    dc._token_expires_at = 0
    assert dc._get_access_token() == "tok-R"
    assert len(calls) == 1   # 共享刷新：全实例仅刷一次


def test_redis_l1_still_fast_path(monkeypatch):
    monkeypatch.setenv("RAG_TOKEN_CACHE_BACKEND", "redis")
    redis_client.set_client_for_test(fakeredis.FakeStrictRedis(decode_responses=True))
    calls = []
    _stub_api(monkeypatch, "tok-R", 7200, calls)
    assert dc._get_access_token() == "tok-R"
    assert dc._get_access_token() == "tok-R"   # L1 命中，连 Redis 都不碰
    assert len(calls) == 1


def test_redis_fail_open_falls_back(monkeypatch):
    monkeypatch.setenv("RAG_TOKEN_CACHE_BACKEND", "redis")

    class _Boom:
        def get(self, *a, **k):
            raise ConnectionError("down")

        def set(self, *a, **k):
            raise ConnectionError("down")

        def pttl(self, *a, **k):
            raise ConnectionError("down")

        def delete(self, *a, **k):
            raise ConnectionError("down")

    redis_client.set_client_for_test(_Boom())
    calls = []
    _stub_api(monkeypatch, "tok-fb", 7200, calls)
    assert dc._get_access_token() == "tok-fb"   # redis 故障 → 直连兜底（fail-open）
    assert len(calls) == 1


# ── SETNX 防惊群 ────────────────────────────────────────────────
def test_redis_herd_lock_waits_then_adopts(monkeypatch):
    """未抢到刷新锁：等待期间持锁实例把 token 写进 Redis → 采纳，不自刷。"""
    monkeypatch.setenv("RAG_TOKEN_CACHE_BACKEND", "redis")
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    redis_client.set_client_for_test(fake)
    fake.set(redis_client.key(dc._TOKEN_LOCK_KEY), "1", ex=15)   # 锁被别人持有
    tok_key = redis_client.key(dc._TOKEN_REDIS_KEY)

    def _fake_sleep(_s):   # 模拟持锁实例在我等待期间写入 token
        fake.set(tok_key, "tok-herd", ex=6900)

    monkeypatch.setattr(dc.time, "sleep", _fake_sleep)
    monkeypatch.setattr(dc, "_TOKEN_HERD_WAIT_TRIES", 3)
    calls = []
    _stub_api(monkeypatch, "should-not-refresh", 7200, calls)
    assert dc._get_access_token() == "tok-herd"
    assert len(calls) == 0   # 采纳共享 token，未自刷（防惊群生效）


def test_redis_herd_lock_timeout_self_refreshes(monkeypatch):
    """等不到（持锁实例迟迟不写）→ 自刷兜底，不死等。"""
    monkeypatch.setenv("RAG_TOKEN_CACHE_BACKEND", "redis")
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    redis_client.set_client_for_test(fake)
    fake.set(redis_client.key(dc._TOKEN_LOCK_KEY), "1", ex=15)
    monkeypatch.setattr(dc.time, "sleep", lambda _s: None)   # 不真睡；token 始终不出现
    monkeypatch.setattr(dc, "_TOKEN_HERD_WAIT_TRIES", 2)
    calls = []
    _stub_api(monkeypatch, "tok-self", 7200, calls)
    assert dc._get_access_token() == "tok-self"
    assert len(calls) == 1   # 等待超时 → 自刷
