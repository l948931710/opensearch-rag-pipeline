# -*- coding: utf-8 -*-
"""
test_session_store_rolling_summary.py — 会话滚动摘要（WS1 收尾；flag OFF + 注入式 summarizer）

两后端跑同一套断言：
- 默认 OFF（flag 未开 或 summarizer 未注入）→ 完全退化为原硬截断，零系统消息（零回归）。
- ON + summarizer → 溢出轮滚进摘要，读取时摘要作前置 system 消息回灌；摘要跨多次滚动累加。
- summarizer 抛错 → fail-open：按截断处理、不炸会话、不surface 摘要。

redis 用 fakeredis 高保真（含 WATCH/MULTI）；memory 的 cap 是模块全局 MAX_HISTORY_TURNS，
故 monkeypatch 到 2 轮（=4 条）与 redis 的 max_turns=2 对齐。
"""
import fakeredis
import pytest

from opensearch_pipeline import session_store as ss
from opensearch_pipeline.session_store import MemorySessionStore, RedisSessionStore

_FLAG = "RAG_SESSION_ROLLING_SUMMARY"


@pytest.fixture(autouse=True)
def _reset_summarizer():
    """每例后清除注入的 summarizer，避免泄漏到其它测试。"""
    yield
    ss.set_summarizer(None)


@pytest.fixture(params=["memory", "redis"])
def small_store(request, monkeypatch):
    """cap=2 轮（4 条）的 store。memory 走模块全局、redis 走 max_turns 参数。"""
    monkeypatch.setattr(ss, "MAX_HISTORY_TURNS", 2)
    if request.param == "memory":
        return MemorySessionStore()
    return RedisSessionStore(fakeredis.FakeStrictRedis(decode_responses=True), max_turns=2)


def _fake_summarizer(overflow, prev_summary):
    """把溢出消息内容拼进摘要（累加式），供断言可见其滚动了哪些轮。"""
    joined = ";".join(m["content"] for m in overflow)
    return (prev_summary + "|" if prev_summary else "") + f"S[{joined}]"


def _append_turns(store, sid, owner, n, start=1):
    for i in range(start, start + n):
        store.append(sid, f"Q{i}", f"A{i}", owner=owner)


# ── 默认 OFF：零回归 ──────────────────────────────────────────────────────────
def test_off_by_default_truncates_no_summary(small_store, monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)         # flag 关
    ss.set_summarizer(None)                          # 无 summarizer
    sid, _ = small_store.get_or_create("s", owner="u")
    _append_turns(small_store, sid, "u", 3)          # 6 条 > cap 4
    hist = small_store.get_history(sid, owner="u")
    assert hist == [                                 # 只剩最近 2 轮，无 system 摘要
        {"role": "user", "content": "Q2"}, {"role": "assistant", "content": "A2"},
        {"role": "user", "content": "Q3"}, {"role": "assistant", "content": "A3"},
    ]
    assert all(m["role"] != "system" for m in hist)


def test_flag_on_but_no_summarizer_still_truncates(small_store, monkeypatch):
    monkeypatch.setenv(_FLAG, "true")                # flag 开
    ss.set_summarizer(None)                          # 但未注入 summarizer → 仍退化截断
    sid, _ = small_store.get_or_create("s", owner="u")
    _append_turns(small_store, sid, "u", 3)
    hist = small_store.get_history(sid, owner="u")
    assert len(hist) == 4 and all(m["role"] != "system" for m in hist)


# ── ON + summarizer：滚动摘要 ────────────────────────────────────────────────
def test_on_summarizes_overflow_and_surfaces(small_store, monkeypatch):
    monkeypatch.setenv(_FLAG, "true")
    ss.set_summarizer(_fake_summarizer)
    sid, _ = small_store.get_or_create("s", owner="u")
    _append_turns(small_store, sid, "u", 3)          # 溢出 = 轮1（Q1/A1）
    hist = small_store.get_history(sid, owner="u")
    assert hist[0]["role"] == "system"
    assert hist[0]["content"].startswith(ss._SUMMARY_PREFIX)
    assert "Q1" in hist[0]["content"] and "A1" in hist[0]["content"]   # 轮1 被滚进摘要
    assert hist[1:] == [                             # 摘要后接最近 2 轮
        {"role": "user", "content": "Q2"}, {"role": "assistant", "content": "A2"},
        {"role": "user", "content": "Q3"}, {"role": "assistant", "content": "A3"},
    ]


def test_get_or_create_also_surfaces_summary(small_store, monkeypatch):
    monkeypatch.setenv(_FLAG, "true")
    ss.set_summarizer(_fake_summarizer)
    sid, _ = small_store.get_or_create("s", owner="u")
    _append_turns(small_store, sid, "u", 3)
    _, hist = small_store.get_or_create(sid, owner="u")   # get_or_create 也回灌摘要
    assert hist[0]["role"] == "system" and "Q1" in hist[0]["content"]


def test_summary_accumulates_across_rolls(small_store, monkeypatch):
    monkeypatch.setenv(_FLAG, "true")
    ss.set_summarizer(_fake_summarizer)
    sid, _ = small_store.get_or_create("s", owner="u")
    _append_turns(small_store, sid, "u", 4)          # 两次溢出：轮1、轮2 先后滚进
    hist = small_store.get_history(sid, owner="u")
    summary = hist[0]["content"]
    assert "Q1" in summary and "Q2" in summary       # 两轮都进了摘要
    assert summary.count("S[") == 2                  # 累加式（prev_summary 被传入）
    assert hist[1:] == [
        {"role": "user", "content": "Q3"}, {"role": "assistant", "content": "A3"},
        {"role": "user", "content": "Q4"}, {"role": "assistant", "content": "A4"},
    ]


# ── summarizer 抛错 → fail-open ──────────────────────────────────────────────
def test_summarizer_failure_fail_open_truncates(small_store, monkeypatch):
    monkeypatch.setenv(_FLAG, "true")

    def _boom(overflow, prev):
        raise RuntimeError("summarizer down")

    ss.set_summarizer(_boom)
    sid, _ = small_store.get_or_create("s", owner="u")
    _append_turns(small_store, sid, "u", 3)          # summarizer 抛错→按截断处理
    hist = small_store.get_history(sid, owner="u")
    assert len(hist) == 4 and all(m["role"] != "system" for m in hist)   # 无摘要、未崩


# ── 评审 2026-07-24：摘要预算段的 fail-open 护栏（redis 专属：事务外裸调用） ──────
class TestSummaryPrecomputeFailOpen:
    """摘要是增强，绝不能把 /api/ask 主答案链路炸掉。

    被修的形态：滚动摘要的「读现有→算溢出→摘要」整段在写事务的 try 之外，
    一次 hget 抖动或一条坏行就直接抛出 append_to_history——而此时答案已经生成，
    用户拿到 500。类 docstring 承诺的是相反的事（写丢弃本轮，不破主答案）。
    """

    @staticmethod
    def _store(client):
        return RedisSessionStore(client, max_turns=2)

    def _wire_on(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "true")
        ss.set_summarizer(_fake_summarizer)

    def test_hget_failure_degrades_to_truncation(self, monkeypatch, caplog):
        """摘要 hget 抛错 → 本轮按截断处理，append 正常返回、历史照常写入。"""
        import logging

        client = fakeredis.FakeStrictRedis(decode_responses=True)
        store = self._store(client)
        self._wire_on(monkeypatch)
        sid, _ = store.get_or_create("s", owner="u")
        _append_turns(store, sid, "u", 2)                 # 填到 cap（4 条）

        def _boom(*_a, **_k):
            raise RuntimeError("redis down mid-flight")

        monkeypatch.setattr(client, "hget", _boom)
        with caplog.at_level(logging.DEBUG, logger="opensearch_pipeline.session_store"):
            store.append(sid, "Q3", "A3", owner="u")      # 必须不 raise
        assert any("滚动摘要" in r.getMessage() for r in caplog.records)

        monkeypatch.undo()                                # 放开 hget 再读回
        ss.set_summarizer(None)
        hist = store.get_history(sid, owner="u")
        assert [m["content"] for m in hist] == ["Q2", "A2", "Q3", "A3"]   # 截断语义完好
        assert all(m["role"] != "system" for m in hist)                   # 没有半截摘要

    def test_corrupt_history_row_skipped_not_fatal(self, monkeypatch):
        """历史里混进非 JSON 行 → 该条跳过，摘要照常滚，append 不抛。"""
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        store = self._store(client)
        self._wire_on(monkeypatch)
        sid, _ = store.get_or_create("s", owner="u")
        _append_turns(store, sid, "u", 2)
        client.lset(store._mkey(sid), 0, "}{ not json")   # 污染最老一条

        store.append(sid, "Q3", "A3", owner="u")          # 必须不 raise

        ss.set_summarizer(None)
        hist = store.get_history(sid, owner="u")
        # 溢出的是最老两条（被污染的 Q1 + 好行 A1）：坏行跳过，好行仍进摘要
        assert hist and hist[0]["role"] == "system"
        assert "S[A1]" in hist[0]["content"]
        assert [m["content"] for m in hist[1:]] == ["Q2", "A2", "Q3", "A3"]

    def test_append_never_raises_when_lrange_dies(self, monkeypatch):
        """lrange 抛错（原内层 try 的形态）同样只降级——护栏收口在同一处。"""
        client = fakeredis.FakeStrictRedis(decode_responses=True)
        store = self._store(client)
        self._wire_on(monkeypatch)
        sid, _ = store.get_or_create("s", owner="u")

        def _boom(*_a, **_k):
            raise RuntimeError("lrange down")

        monkeypatch.setattr(client, "lrange", _boom)
        store.append(sid, "Q1", "A1", owner="u")          # 必须不 raise
