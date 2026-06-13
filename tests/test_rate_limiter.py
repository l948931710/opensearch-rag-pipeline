# -*- coding: utf-8 -*-
"""rate_limiter 纯逻辑单测（不依赖 FastAPI/外部服务）。

覆盖：客户端 IP 自适应解析（EIP 直连忽略伪造 XFF / SLB 后取最右跳）、
每分钟滑动窗、日配额与北京日界翻转、深思配额（匿名拒绝/拒绝不消耗常规预算）、
全局日熔断（search 不计入）、0=关闭该层语义、总开关、并发精确性。
"""

import threading

import pytest

from opensearch_pipeline import rate_limiter as rl


# ── 测试装置 ─────────────────────────────────────────────────

class FakeClock:
    """可推进的假时钟（注入 limiter._now，避免猴补丁全局 time.time）。"""

    def __init__(self, t0: float = 1_700_000_000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def make_limiter(monkeypatch):
    """构造启用态的独立限流器实例（不碰模块单例），env 覆盖限额。"""

    def _make(clock: FakeClock = None, **env):
        monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        lim = rl.ServingRateLimiter()
        if clock is not None:
            lim._now = clock
        return lim

    return _make


# ── resolve_client_ip ────────────────────────────────────────

def test_ip_public_host_ignores_spoofed_xff():
    # EIP 直连形态：socket 对端是公网客户端，XFF 是攻击者可伪造头，必须忽略。
    # 注意夹具须用真·公网 IP：RFC 5737 文档段（203.0.113.x）is_global=False，会走 XFF 分支
    assert rl.resolve_client_ip("120.55.69.9", "1.2.3.4") == "120.55.69.9"


def test_ip_private_host_takes_rightmost_xff():
    # SLB 后形态：对端是网关私网地址，取最右一跳（最近可信代理追加的真实客户端）
    assert rl.resolve_client_ip("10.0.0.5", "9.9.9.9, 203.0.113.7") == "203.0.113.7"
    assert rl.resolve_client_ip("100.64.0.1", "203.0.113.7") == "203.0.113.7"


def test_ip_private_host_garbage_xff_falls_back():
    assert rl.resolve_client_ip("10.0.0.5", "not-an-ip, also-bad") == "10.0.0.5"
    assert rl.resolve_client_ip("10.0.0.5", None) == "10.0.0.5"


def test_ip_unparseable_host_uses_xff_then_literal():
    # TestClient 的 host 是字面量 "testclient"：带 XFF 用 XFF，否则用字面量兜底
    assert rl.resolve_client_ip("testclient", "203.0.113.7") == "203.0.113.7"
    assert rl.resolve_client_ip("testclient", None) == "testclient"
    assert rl.resolve_client_ip(None, None) == "unknown"
    assert rl.resolve_client_ip("", "") == "unknown"


def test_ip_ipv6():
    # 公网 v6 直取（忽略 XFF）；2001:db8::/32 是文档保留段（非 global）→ 走 XFF 分支
    assert rl.resolve_client_ip("2400:3200::1", "1.2.3.4") == "2400:3200::1"
    assert rl.resolve_client_ip("2001:db8::1", "203.0.113.7") == "203.0.113.7"


# ── 每分钟滑动窗 ─────────────────────────────────────────────

def test_per_minute_window_and_recovery(make_limiter):
    clock = FakeClock()
    lim = make_limiter(clock, RAG_RATE_USER_PER_MIN=2, RAG_RATE_USER_PER_DAY=0)
    assert lim.admit_ask("u:U1", is_user=True) is None
    clock.advance(10)
    assert lim.admit_ask("u:U1", is_user=True) is None
    clock.advance(10)
    d = lim.admit_ask("u:U1", is_user=True)
    assert d is not None and d.status_code == 429 and d.reason == "per_min"
    assert 1 <= d.retry_after <= 61
    # 窗口滑过最早一次请求后恢复
    clock.advance(41)  # 距第一次请求 61s
    assert lim.admit_ask("u:U1", is_user=True) is None


def test_anon_stricter_than_user_and_keys_isolated(make_limiter):
    clock = FakeClock()
    lim = make_limiter(clock, RAG_RATE_USER_PER_MIN=5, RAG_RATE_ANON_PER_MIN=1,
                       RAG_RATE_USER_PER_DAY=0, RAG_RATE_ANON_PER_DAY=0)
    assert lim.admit_ask("ip:1.2.3.4", is_user=False) is None
    d = lim.admit_ask("ip:1.2.3.4", is_user=False)
    assert d is not None and d.reason == "per_min"
    # 不同 IP / 登录用户互不影响
    assert lim.admit_ask("ip:5.6.7.8", is_user=False) is None
    for _ in range(5):
        assert lim.admit_ask("u:U1", is_user=True) is None


def test_zero_means_layer_disabled(make_limiter):
    clock = FakeClock()
    lim = make_limiter(clock, RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=0,
                       RAG_GLOBAL_DAILY_LLM_CAP=0)
    for _ in range(50):
        assert lim.admit_ask("u:U1", is_user=True) is None


# ── 日配额与日界 ─────────────────────────────────────────────

def test_daily_quota_and_beijing_rollover(make_limiter):
    clock = FakeClock()
    lim = make_limiter(clock, RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=3)
    for _ in range(3):
        assert lim.admit_ask("u:U1", is_user=True) is None
    d = lim.admit_ask("u:U1", is_user=True)
    assert d is not None and d.status_code == 429 and d.reason == "per_day"
    assert 1 <= d.retry_after <= 86401
    # 推进到北京次日，配额重置
    clock.advance(86400)
    assert lim.admit_ask("u:U1", is_user=True) is None


def test_anon_daily_message_nudges_login(make_limiter):
    clock = FakeClock()
    lim = make_limiter(clock, RAG_RATE_ANON_PER_MIN=0, RAG_RATE_ANON_PER_DAY=1)
    assert lim.admit_ask("ip:1.2.3.4", is_user=False) is None
    d = lim.admit_ask("ip:1.2.3.4", is_user=False)
    assert d is not None and "登录" in d.message


# ── 深思配额 ─────────────────────────────────────────────────

def test_thinking_denied_for_anonymous(make_limiter):
    lim = make_limiter(FakeClock())
    d = lim.admit_ask("ip:1.2.3.4", is_user=False, thinking=True)
    assert d is not None and d.status_code == 403 and d.reason == "thinking_anon"


def test_thinking_quota_and_denial_consumes_nothing(make_limiter):
    clock = FakeClock()
    lim = make_limiter(clock, RAG_THINKING_DAILY_QUOTA=2,
                       RAG_RATE_USER_PER_MIN=3, RAG_RATE_USER_PER_DAY=0)
    assert lim.admit_ask("u:U1", is_user=True, thinking=True) is None
    assert lim.admit_ask("u:U1", is_user=True, thinking=True) is None
    d = lim.admit_ask("u:U1", is_user=True, thinking=True)
    assert d is not None and d.status_code == 429 and d.reason == "thinking_quota"
    assert "深度思考" in d.message
    # 深思拒绝不消耗常规预算：每分钟窗只记了 2 次，关掉深思立即可问
    assert lim.admit_ask("u:U1", is_user=True, thinking=False) is None
    # 次日深思配额重置
    clock.advance(86400)
    assert lim.admit_ask("u:U1", is_user=True, thinking=True) is None


def test_thinking_quota_zero_means_feature_off(make_limiter):
    lim = make_limiter(FakeClock(), RAG_THINKING_DAILY_QUOTA=0)
    d = lim.admit_ask("u:U1", is_user=True, thinking=True)
    assert d is not None and d.status_code == 403 and d.reason == "thinking_off"


# ── 全局日熔断 ───────────────────────────────────────────────

def test_global_cap_trips_for_everyone_but_not_search(make_limiter):
    clock = FakeClock()
    lim = make_limiter(clock, RAG_GLOBAL_DAILY_LLM_CAP=2,
                       RAG_RATE_USER_PER_MIN=0, RAG_RATE_USER_PER_DAY=0,
                       RAG_RATE_ANON_PER_MIN=0, RAG_RATE_ANON_PER_DAY=0)
    assert lim.admit_ask("u:U1", is_user=True) is None
    assert lim.admit_ask("ip:1.2.3.4", is_user=False) is None
    # 触顶后：任何主体的问答都 503
    d = lim.admit_ask("u:U2", is_user=True)
    assert d is not None and d.status_code == 503 and d.reason == "global_cap"
    # /api/search（count_llm=False）不受 LLM 熔断影响
    assert lim.admit_ask("u:U2", is_user=True, count_llm=False) is None
    # 次日恢复
    clock.advance(86400)
    assert lim.admit_ask("u:U3", is_user=True) is None


# ── 辅助端点窗口 ─────────────────────────────────────────────

def test_aux_window_independent_from_ask(make_limiter):
    clock = FakeClock()
    lim = make_limiter(clock, RAG_RATE_AUX_PER_MIN=2, RAG_RATE_ANON_PER_MIN=1,
                       RAG_RATE_ANON_PER_DAY=0)
    assert lim.admit_aux("ip:1.2.3.4") is None
    assert lim.admit_aux("ip:1.2.3.4") is None
    d = lim.admit_aux("ip:1.2.3.4")
    assert d is not None and d.status_code == 429 and d.reason == "aux_per_min"
    # ask 窗与 aux 窗相互独立
    assert lim.admit_ask("ip:1.2.3.4", is_user=False) is None


# ── 总开关与默认值 ───────────────────────────────────────────

def test_master_switch_off(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "false")
    monkeypatch.setenv("RAG_RATE_USER_PER_MIN", "1")
    lim = rl.ServingRateLimiter()
    for _ in range(10):
        assert lim.admit_ask("u:U1", is_user=True) is None
    assert "禁用" in lim.describe()


def test_default_enabled_tracks_simulate_api(monkeypatch):
    # 未显式设置总开关时：enabled == not simulate_api（模拟模式无账单可保护）
    monkeypatch.delenv("RAG_RATE_LIMIT_ENABLE", raising=False)
    from opensearch_pipeline.config import get_config
    lim = rl.ServingRateLimiter()
    assert lim.limits().enabled == (not get_config().simulate_api)


def test_invalid_env_int_falls_back(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
    monkeypatch.setenv("RAG_RATE_USER_PER_MIN", "not-a-number")
    lim = rl.ServingRateLimiter()
    assert lim.limits().user_per_min == 6  # 回落默认值


def test_reload_limits_picks_up_env_change(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
    lim = rl.ServingRateLimiter()
    assert lim.limits().user_per_min == 6
    monkeypatch.setenv("RAG_RATE_USER_PER_MIN", "9")
    assert lim.limits().user_per_min == 6      # 旧快照仍生效
    assert lim.reload_limits().user_per_min == 9


# ── 并发精确性 ───────────────────────────────────────────────

def test_concurrent_admission_exact(make_limiter):
    lim = make_limiter(FakeClock(), RAG_RATE_USER_PER_MIN=50, RAG_RATE_USER_PER_DAY=0)
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
    # 8 线程 × 20 次 = 160 次尝试，恰好放行 50（锁内检查+计入原子）
    assert len(admitted) == 50
