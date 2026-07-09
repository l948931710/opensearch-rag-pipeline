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
