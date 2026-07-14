# -*- coding: utf-8 -*-
"""
test_multi_instance_batch5.py — 复核批次5（多实例配套）B1 + B2 回归锁

B1 强制 Redis fail-loud：RAG_REQUIRE_REDIS=true 时限流/会话 redis 后端初始化失败
    **raise 拒绝起服**（多实例下 memory 回退=各实例独立计数/各存会话的假绿）；
    后端未切 redis 同样 raise（配置自相矛盾）。未置位保持单实例安全默认（回退+ERROR）。
B2 ready 与 Redis 解耦：Redis 故障默认不摘实例（防 30s failover 摘空整个集群），
    报告 error + CRITICAL 告警；RAG_READY_REDIS_STRICT=true 恢复摘除语义。
    Sentinel/Cluster 接入（redis-py 原生）。
"""
from unittest.mock import MagicMock

import pytest

from opensearch_pipeline import redis_client


@pytest.fixture(autouse=True)
def _clean_redis_env(monkeypatch):
    """每测隔离：清 Redis 相关 env + 复位单例客户端。"""
    for k in ("RAG_REQUIRE_REDIS", "RAG_RATE_LIMIT_BACKEND", "RAG_SESSION_BACKEND",
              "RAG_REDIS_URL", "RAG_REDIS_SENTINEL_NODES", "RAG_REDIS_SENTINEL_MASTER",
              "RAG_REDIS_CLUSTER", "RAG_READY_REDIS_STRICT", "RAG_REDIS_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    redis_client.set_client_for_test(None)
    yield
    redis_client.set_client_for_test(None)


def _healthy_client():
    cli = MagicMock()
    cli.register_script.return_value = MagicMock()
    return cli


# ═══════════════════════════════════════════════════════════════
# B1：限流器工厂
# ═══════════════════════════════════════════════════════════════


def test_limiter_fallback_without_require(monkeypatch):
    """未置 REQUIRE：redis 初始化失败回退 memory（单实例安全默认，现状保持）。"""
    from opensearch_pipeline.rate_limiter import ServingRateLimiter, _make_limiter
    monkeypatch.setenv("RAG_RATE_LIMIT_BACKEND", "redis")   # URL 未配 → get_client raise
    lim = _make_limiter()
    assert isinstance(lim, ServingRateLimiter)


def test_limiter_require_redis_init_failure_raises(monkeypatch):
    from opensearch_pipeline.rate_limiter import _make_limiter
    monkeypatch.setenv("RAG_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("RAG_REQUIRE_REDIS", "true")
    with pytest.raises(RuntimeError, match="拒绝以 memory 降级起服"):
        _make_limiter()


def test_limiter_require_redis_backend_mismatch_raises(monkeypatch):
    from opensearch_pipeline.rate_limiter import _make_limiter
    monkeypatch.setenv("RAG_REQUIRE_REDIS", "true")          # 后端仍默认 memory
    with pytest.raises(RuntimeError, match="未切 redis"):
        _make_limiter()


def test_limiter_require_redis_healthy_builds_redis_backend(monkeypatch):
    from opensearch_pipeline.rate_limiter import RedisRateLimiter, _make_limiter
    monkeypatch.setenv("RAG_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("RAG_REQUIRE_REDIS", "true")
    redis_client.set_client_for_test(_healthy_client())
    assert isinstance(_make_limiter(), RedisRateLimiter)


# ═══════════════════════════════════════════════════════════════
# B1：会话存储工厂
# ═══════════════════════════════════════════════════════════════


def test_session_fallback_without_require(monkeypatch):
    """未置 REQUIRE：redis 初始化失败回退 memory + ERROR（原先 import 期直接崩）。"""
    from opensearch_pipeline.session_store import MemorySessionStore, _make_backend
    monkeypatch.setenv("RAG_SESSION_BACKEND", "redis")
    assert isinstance(_make_backend(), MemorySessionStore)


def test_session_require_redis_init_failure_raises(monkeypatch):
    from opensearch_pipeline.session_store import _make_backend
    monkeypatch.setenv("RAG_SESSION_BACKEND", "redis")
    monkeypatch.setenv("RAG_REQUIRE_REDIS", "true")
    with pytest.raises(RuntimeError, match="拒绝以 memory 降级起服"):
        _make_backend()


def test_session_require_redis_backend_mismatch_raises(monkeypatch):
    from opensearch_pipeline.session_store import _make_backend
    monkeypatch.setenv("RAG_REQUIRE_REDIS", "true")
    with pytest.raises(RuntimeError, match="未切 redis"):
        _make_backend()


def test_session_require_redis_healthy_builds_redis_backend(monkeypatch):
    from opensearch_pipeline.session_store import RedisSessionStore, _make_backend
    monkeypatch.setenv("RAG_SESSION_BACKEND", "redis")
    monkeypatch.setenv("RAG_REQUIRE_REDIS", "true")
    redis_client.set_client_for_test(_healthy_client())
    assert isinstance(_make_backend(), RedisSessionStore)


# ═══════════════════════════════════════════════════════════════
# B2：ready 与 Redis 解耦
# ═══════════════════════════════════════════════════════════════


def _ready(monkeypatch):
    """走 live 探针分支（simulate 会在端点头部 early-return、不含 redis 键）；
    RDS 桩 ok、HA3 桩 skipped（与 P2-05 ready 测试同法）。"""
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config(), "simulate", False)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *_a):
            pass

        def fetchone(self):
            return (1,)

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    from opensearch_pipeline import db as dbmod
    from opensearch_pipeline import retriever
    monkeypatch.setattr(dbmod, "_get_db_conn", lambda *a, **k: _Conn())
    monkeypatch.setattr(retriever, "_get_ha3_client", lambda: "MOCK_HA3_CLIENT")
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    return TestClient(app).get("/api/ready")


def test_ready_redis_error_lenient_by_default(monkeypatch):
    """默认宽松：Redis error 报告但不摘实例（防 failover 摘空集群）。"""
    monkeypatch.setenv("RAG_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setattr(redis_client, "ping", lambda: False)
    r = _ready(monkeypatch)
    body = r.json()
    assert body["redis"] == "error"          # 照实报告
    assert r.status_code == 200              # 但不摘流量
    assert body["status"] == "ok"


def test_ready_redis_error_strict_degrades(monkeypatch):
    """RAG_READY_REDIS_STRICT=true：恢复旧语义（Redis error 即摘除本实例）。"""
    monkeypatch.setenv("RAG_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("RAG_READY_REDIS_STRICT", "true")
    monkeypatch.setattr(redis_client, "ping", lambda: False)
    r = _ready(monkeypatch)
    assert r.status_code == 503
    assert r.json()["redis"] == "error"


def test_ready_redis_ok_unchanged(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setattr(redis_client, "ping", lambda: True)
    r = _ready(monkeypatch)
    assert r.status_code == 200 and r.json()["redis"] == "ok"


# ═══════════════════════════════════════════════════════════════
# B2：Sentinel / Cluster 接入
# ═══════════════════════════════════════════════════════════════


def test_parse_nodes():
    assert redis_client._parse_nodes("h1:26379, h2:26380") == [("h1", 26379), ("h2", 26380)]
    assert redis_client._parse_nodes("solo") == [("solo", 26379)]     # 缺端口默认哨兵口
    assert redis_client._parse_nodes("") == []


def test_get_client_sentinel_mode(monkeypatch):
    """RAG_REDIS_SENTINEL_NODES → Sentinel.master_for（failover 自动跟随）。"""
    import sys
    import types
    captured: dict = {}

    class _FakeSentinel:
        def __init__(self, nodes, sentinel_kwargs=None):
            captured["nodes"] = nodes
            captured["sentinel_kwargs"] = sentinel_kwargs

        def master_for(self, master, **kw):
            captured["master"] = master
            captured["kw"] = kw
            return "MASTER_CLIENT"

    fake_redis = types.ModuleType("redis")
    fake_sentinel_mod = types.ModuleType("redis.sentinel")
    fake_sentinel_mod.Sentinel = _FakeSentinel
    fake_redis.sentinel = fake_sentinel_mod
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "redis.sentinel", fake_sentinel_mod)

    monkeypatch.setenv("RAG_REDIS_SENTINEL_NODES", "s1:26379,s2:26379")
    monkeypatch.setenv("RAG_REDIS_SENTINEL_MASTER", "fuling-master")
    assert redis_client.is_configured() is True          # 无 URL 也算已配置
    cli = redis_client.get_client()
    assert cli == "MASTER_CLIENT"
    assert captured["nodes"] == [("s1", 26379), ("s2", 26379)]
    assert captured["master"] == "fuling-master"
    assert captured["kw"]["decode_responses"] is True    # 通用 kwargs 透传


def test_get_client_cluster_mode(monkeypatch):
    import sys
    import types
    captured: dict = {}

    class _FakeCluster:
        @classmethod
        def from_url(cls, url, **kw):
            captured["url"] = url
            captured["kw"] = kw
            return "CLUSTER_CLIENT"

    fake_redis = types.ModuleType("redis")
    fake_cluster_mod = types.ModuleType("redis.cluster")
    fake_cluster_mod.RedisCluster = _FakeCluster
    fake_redis.cluster = fake_cluster_mod
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "redis.cluster", fake_cluster_mod)

    monkeypatch.setenv("RAG_REDIS_URL", "redis://c1:6379")
    monkeypatch.setenv("RAG_REDIS_CLUSTER", "true")
    assert redis_client.get_client() == "CLUSTER_CLIENT"
    assert captured["url"] == "redis://c1:6379"
