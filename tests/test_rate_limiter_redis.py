# -*- coding: utf-8 -*-
"""
test_rate_limiter_redis.py — 限流器 Redis 后端（WS0-3）

两部分：
1. 双后端共享契约：memory 与 redis 在**冻结时钟**下（固定窗≡滑动窗的单窗行为）跑同一套
   四层准入断言，保证行为一致。
2. Redis 专项：跨实例共享计数（全局熔断真正全局）、fail-closed、固定窗恢复、触顶告警一次/日。

redis 后端用 fakeredis + lupa 高保真模拟（真 Redis 原生跑 Lua）。memory 版一字未动，
其既有测试见 test_rate_limiter.py。
"""
import threading

import fakeredis
import pytest

from opensearch_pipeline import rate_limiter as rl


class FakeClock:
    def __init__(self, t0: float = 1_700_000_000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _redis_limiter(clock=None):
    lim = rl.RedisRateLimiter(fakeredis.FakeStrictRedis(decode_responses=True))
    if clock is not None:
        lim._now = clock
    return lim


# ─────────────────────────────────────────────────────────────────────────────
# 1. 双后端共享契约（冻结时钟）
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(params=["memory", "redis"])
def make_backend(request, monkeypatch):
    def _make(clock: FakeClock = None, **env):
        monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        if request.param == "memory":
            lim = rl.ServingRateLimiter()
        else:
            lim = rl.RedisRateLimiter(fakeredis.FakeStrictRedis(decode_responses=True))
        if clock is not None:
            lim._now = clock
        return lim

    return _make


def test_per_min_admit_then_deny(make_backend):
    lim = make_backend(FakeClock(), RAG_RATE_USER_PER_MIN=2, RAG_RATE_USER_PER_DAY=0)
    assert lim.admit_ask("u:U1", is_user=True) is None
    assert lim.admit_ask("u:U1", is_user=True) is None
    d = lim.admit_ask("u:U1", is_user=True)
    assert d is not None and d.status_code == 429 and d.reason == "per_min"
    assert 1 <= d.retry_after <= 61


def test_per_day_admit_then_deny(make_backend):
    lim = make_backend(FakeClock(), RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=3)
    for _ in range(3):
        assert lim.admit_ask("u:U1", is_user=True) is None
    d = lim.admit_ask("u:U1", is_user=True)
    assert d is not None and d.status_code == 429 and d.reason == "per_day"


def test_daily_rollover_resets(make_backend):
    clock = FakeClock()
    lim = make_backend(clock, RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=2)
    assert lim.admit_ask("u:U1", is_user=True) is None
    assert lim.admit_ask("u:U1", is_user=True) is None
    assert lim.admit_ask("u:U1", is_user=True).reason == "per_day"
    clock.advance(86400)  # 北京次日
    assert lim.admit_ask("u:U1", is_user=True) is None


def test_anon_stricter_and_keys_isolated(make_backend):
    lim = make_backend(FakeClock(), RAG_RATE_USER_PER_MIN=5, RAG_RATE_ANON_PER_MIN=1,
                       RAG_RATE_USER_PER_DAY=0, RAG_RATE_ANON_PER_DAY=0)
    assert lim.admit_ask("ip:1.2.3.4", is_user=False) is None
    assert lim.admit_ask("ip:1.2.3.4", is_user=False).reason == "per_min"
    assert lim.admit_ask("ip:5.6.7.8", is_user=False) is None      # 不同 IP 独立
    for _ in range(5):
        assert lim.admit_ask("u:U1", is_user=True) is None          # 登录用户独立且更宽


def test_zero_disables_layer(make_backend):
    lim = make_backend(FakeClock(), RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=0,
                       RAG_GLOBAL_DAILY_LLM_CAP=0)
    for _ in range(30):
        assert lim.admit_ask("u:U1", is_user=True) is None


def test_thinking_anon_and_off_and_quota(make_backend):
    # 匿名深思拒绝
    lim = make_backend(FakeClock())
    d = lim.admit_ask("ip:1.2.3.4", is_user=False, thinking=True)
    assert d is not None and d.status_code == 403 and d.reason == "thinking_anon"
    # 深思整体关闭
    lim2 = make_backend(FakeClock(), RAG_THINKING_DAILY_QUOTA=0)
    d2 = lim2.admit_ask("u:U1", is_user=True, thinking=True)
    assert d2 is not None and d2.reason == "thinking_off"
    # 深思日配额 + 拒绝不消耗常规预算
    lim3 = make_backend(FakeClock(), RAG_THINKING_DAILY_QUOTA=2,
                        RAG_RATE_USER_PER_MIN=3, RAG_RATE_USER_PER_DAY=0)
    assert lim3.admit_ask("u:U1", is_user=True, thinking=True) is None
    assert lim3.admit_ask("u:U1", is_user=True, thinking=True) is None
    assert lim3.admit_ask("u:U1", is_user=True, thinking=True).reason == "thinking_quota"
    assert lim3.admit_ask("u:U1", is_user=True, thinking=False) is None  # 关掉深思立即可问


def test_global_cap_trips_for_everyone_but_not_search(make_backend):
    lim = make_backend(FakeClock(), RAG_GLOBAL_DAILY_LLM_CAP=2,
                       RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=0,
                       RAG_RATE_ANON_PER_MIN=0, RAG_RATE_ANON_PER_DAY=0)
    assert lim.admit_ask("u:U1", is_user=True) is None
    assert lim.admit_ask("ip:1.2.3.4", is_user=False) is None
    d = lim.admit_ask("u:U2", is_user=True)
    assert d is not None and d.status_code == 503 and d.reason == "global_cap"
    assert lim.admit_ask("u:U2", is_user=True, count_llm=False) is None  # search 不受熔断


def test_aux_window(make_backend):
    lim = make_backend(FakeClock(), RAG_RATE_AUX_PER_MIN=2)
    assert lim.admit_aux("ip:1.2.3.4") is None
    assert lim.admit_aux("ip:1.2.3.4") is None
    d = lim.admit_aux("ip:1.2.3.4")
    assert d is not None and d.status_code == 429 and d.reason == "aux_per_min"


def test_master_switch_off(make_backend):
    lim = make_backend(FakeClock(), RAG_RATE_LIMIT_ENABLE="false", RAG_RATE_USER_PER_MIN=1)
    for _ in range(10):
        assert lim.admit_ask("u:U1", is_user=True) is None
    assert "禁用" in lim.describe()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Redis 专项
# ─────────────────────────────────────────────────────────────────────────────
def test_redis_global_cap_shared_across_instances(monkeypatch):
    """两个 RedisRateLimiter 实例共享一个 Redis（= 两个 SAE 实例）→ 全局熔断跨实例生效。
    这是 memory 后端做不到的（各实例独立计数 → cap 实际放大 N 倍）。"""
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
    monkeypatch.setenv("RAG_GLOBAL_DAILY_LLM_CAP", "2")
    monkeypatch.setenv("RAG_RATE_USER_PER_MIN", "0")
    monkeypatch.setenv("RAG_RATE_USER_PER_DAY", "0")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    clock = FakeClock()
    a = rl.RedisRateLimiter(client); a._now = clock
    b = rl.RedisRateLimiter(client); b._now = clock
    assert a.admit_ask("u:1", is_user=True) is None   # 全局=1
    assert b.admit_ask("u:2", is_user=True) is None   # 全局=2（共享计数）
    d = a.admit_ask("u:3", is_user=True)              # 越过共享上限
    assert d is not None and d.reason == "global_cap"


def test_redis_fail_closed_on_ask_open_on_aux(monkeypatch):
    """Redis 故障：ask 成本路径 fail-closed 503（评审 C3）；aux 非成本路径 fail-open。"""
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")

    class _BoomClient:
        def register_script(self, script):
            def _run(keys=None, args=None):
                raise ConnectionError("redis down")
            return _run

        def set(self, *a, **k):
            raise ConnectionError("redis down")

    lim = rl.RedisRateLimiter(_BoomClient())
    lim._now = FakeClock()
    d = lim.admit_ask("u:1", is_user=True, count_llm=True)
    assert d is not None and d.status_code == 503 and d.reason == "ratelimit_backend_down"
    assert lim.admit_ask("u:1", is_user=True, count_llm=False) is None  # 非成本路径不拖垮
    assert lim.admit_aux("u:1") is None                                 # aux fail-open


def test_redis_minute_fixed_window_recovers(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
    monkeypatch.setenv("RAG_RATE_USER_PER_MIN", "2")
    monkeypatch.setenv("RAG_RATE_USER_PER_DAY", "0")
    monkeypatch.setenv("RAG_GLOBAL_DAILY_LLM_CAP", "0")
    clock = FakeClock()
    lim = _redis_limiter(clock)
    assert lim.admit_ask("u:U1", is_user=True) is None
    assert lim.admit_ask("u:U1", is_user=True) is None
    assert lim.admit_ask("u:U1", is_user=True).reason == "per_min"
    clock.advance(60)  # 下一个固定窗桶
    assert lim.admit_ask("u:U1", is_user=True) is None


def test_redis_cap_alert_once_per_day(monkeypatch):
    """触顶（第 cap 个准入的边沿 + 之后的 global_cap 拒绝）每北京日只告警一次；
    日界翻转后再次触顶 → 再告警。Redis 用 SETNX 跨实例去重。"""
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
    monkeypatch.setenv("RAG_GLOBAL_DAILY_LLM_CAP", "2")
    monkeypatch.setenv("RAG_RATE_USER_PER_MIN", "0")
    monkeypatch.setenv("RAG_RATE_USER_PER_DAY", "0")
    monkeypatch.setattr(rl, "_DISPATCH_ASYNC", False)
    alerts = []
    monkeypatch.setattr(rl, "_dispatch_cap_alert", lambda cap: alerts.append(cap))
    monkeypatch.setattr(rl, "_persist_rejected", lambda snap: True)
    clock = FakeClock()
    lim = _redis_limiter(clock)
    assert lim.admit_ask("u:a", is_user=True) is None
    assert alerts == []
    assert lim.admit_ask("u:b", is_user=True) is None   # 触顶边沿（第 2 个）
    assert alerts == [2]
    assert lim.admit_ask("u:c", is_user=True).reason == "global_cap"
    assert alerts == [2]                                # 每日至多一次
    clock.advance(86400)                                # 北京次日
    assert lim.admit_ask("u:a", is_user=True) is None
    assert lim.admit_ask("u:b", is_user=True) is None
    assert alerts == [2, 2]


def test_redis_thinking_weighted_toward_global_cap(monkeypatch):
    """深思按 RAG_GLOBAL_CAP_THINKING_WEIGHT（默认 8）计入全局熔断。"""
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
    monkeypatch.setenv("RAG_GLOBAL_DAILY_LLM_CAP", "10")
    monkeypatch.setenv("RAG_RATE_USER_PER_MIN", "0")
    monkeypatch.setenv("RAG_RATE_USER_PER_DAY", "0")
    monkeypatch.setenv("RAG_THINKING_DAILY_QUOTA", "10")
    clock = FakeClock()
    lim = _redis_limiter(clock)
    assert lim.admit_ask("u:a", is_user=True, thinking=True) is None   # +8 → 8
    assert lim.admit_ask("u:b", is_user=True) is None                  # +1 → 9
    assert lim.admit_ask("u:c", is_user=True) is None                  # +1 → 10 触顶
    assert lim.admit_ask("u:d", is_user=True).reason == "global_cap"


def test_redis_factory_selects_backend(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_BACKEND", "redis")
    redis_mod = __import__("opensearch_pipeline.redis_client", fromlist=["set_client_for_test"])
    redis_mod.set_client_for_test(fakeredis.FakeStrictRedis(decode_responses=True))
    try:
        assert isinstance(rl._make_limiter(), rl.RedisRateLimiter)
    finally:
        redis_mod.set_client_for_test(None)
    monkeypatch.setenv("RAG_RATE_LIMIT_BACKEND", "memory")
    assert isinstance(rl._make_limiter(), rl.ServingRateLimiter)


def test_redis_concurrent_admission_exact(monkeypatch):
    """8 线程 × 20 次并发抢 50 名额：Lua 服务端原子 → 恰好放行 50（无超额）。"""
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
    monkeypatch.setenv("RAG_RATE_USER_PER_MIN", "50")
    monkeypatch.setenv("RAG_RATE_USER_PER_DAY", "0")
    monkeypatch.setenv("RAG_GLOBAL_DAILY_LLM_CAP", "0")
    lim = _redis_limiter(FakeClock())
    admitted = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(20):
            if lim.admit_ask("u:U1", is_user=True) is None:
                admitted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 50
