# -*- coding: utf-8 -*-
"""test_qa_gap_groups.py — 缺口相似问法语义归组（schema/039+040）纯函数层。

锁定三件事：贪心归组的阈值/确定性（可复算）、展示层归并的口径（asks 求和/days min/
refusal 优先/代表恒为真实成员）、以及「绝不错并」的保守边界。
"""
from opensearch_pipeline.qa_gap_groups import (
    cosine,
    greedy_group,
    merge_semantic_gaps,
    semantic_threshold,
)


def _gap(h, asks=1, days=0, kind="no_result", dept="", pending=False, msg="m"):
    return {"hash": h, "raw": f"q-{h[:6]}", "asks": asks, "days": days,
            "dept": dept, "kind": kind, "msg": msg, "pending": pending}


def test_cosine_edges():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0                 # 空/长度不一致 → 不相似
    assert cosine([1.0], [1.0, 0.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0    # 零向量绝不归并


def test_greedy_group_threshold_and_determinism():
    members = [
        {"hash": "a" * 64, "text": "怎么开发票", "vector": [1.0, 0.0], "weight": 5},
        {"hash": "b" * 64, "text": "发票怎么开", "vector": [0.96, 0.28], "weight": 2},
        {"hash": "c" * 64, "text": "食堂几点开门", "vector": [0.0, 1.0], "weight": 1},
    ]
    rows1 = greedy_group(members, 0.90)
    rows2 = greedy_group(list(reversed(members)), 0.90)   # 输入顺序无关（内部按权重排序）
    assert rows1 == rows2
    by_hash = {r["question_hash"]: r for r in rows1}
    assert by_hash["a" * 64]["group_id"] == "a" * 64      # 权重最高者为代表
    assert by_hash["a" * 64]["similarity"] == 1.0
    assert by_hash["b" * 64]["group_id"] == "a" * 64      # cos≈0.96 ≥ 0.90 入组
    assert by_hash["c" * 64]["group_id"] == "c" * 64      # 正交 → 自成一组


def test_greedy_group_below_threshold_stays_apart():
    members = [
        {"hash": "a" * 64, "text": "t1", "vector": [1.0, 0.0], "weight": 3},
        {"hash": "b" * 64, "text": "t2", "vector": [0.8, 0.6], "weight": 1},   # cos=0.8 < 0.9
    ]
    rows = {r["question_hash"]: r for r in greedy_group(members, 0.90)}
    assert rows["b" * 64]["group_id"] == "b" * 64   # 宁漏并不错并


def test_greedy_group_weight_tie_breaks_by_hash():
    members = [
        {"hash": "b" * 64, "text": "t", "vector": [1.0, 0.0], "weight": 1},
        {"hash": "a" * 64, "text": "t", "vector": [1.0, 0.0], "weight": 1},
    ]
    rows = {r["question_hash"]: r for r in greedy_group(members, 0.90)}
    assert rows["a" * 64]["group_id"] == "a" * 64   # 并列按 hash 字典序，a 为代表
    assert rows["b" * 64]["group_id"] == "a" * 64


def test_merge_semantic_gaps_semantics():
    h1, h2, h3 = "1" * 64, "2" * 64, "3" * 64
    gaps = [_gap(h1, asks=3, days=5, kind="no_result"),
            _gap(h2, asks=2, days=2, kind="refusal", dept="marketing", pending=True),
            _gap(h3, asks=1, days=9)]
    merged = merge_semantic_gaps(gaps, {h1: h1, h2: h1})
    assert len(merged) == 2
    card = next(m for m in merged if m["hash"] == h1)
    assert card["asks"] == 5 and card["days"] == 2          # 求和 / 取 min
    assert card["kind"] == "refusal" and card["pending"] is True
    assert card["dept"] == "marketing"                      # 首个非空
    assert card["phrasings"] == 2
    solo = next(m for m in merged if m["hash"] == h3)
    assert solo["phrasings"] == 1


def test_merge_rep_absent_falls_back_to_top_member():
    """组代表已关闭/出窗（不在开放成员中）→ 主成员取 asks 最高者——卡片 hash 恒为
    真实开放成员（贡献关闭语义不变），绝不携带幽灵 hash。"""
    h1, h2, h9 = "1" * 64, "2" * 64, "9" * 64
    gaps = [_gap(h1, asks=1, days=5), _gap(h2, asks=4, days=7)]
    merged = merge_semantic_gaps(gaps, {h1: h9, h2: h9})
    assert len(merged) == 1
    assert merged[0]["hash"] == h2 and merged[0]["asks"] == 5


def test_merge_empty_map_passthrough():
    gaps = [_gap("1" * 64), _gap("2" * 64)]
    assert merge_semantic_gaps(gaps, {}) is gaps


def test_threshold_env_guardrails(monkeypatch):
    monkeypatch.setenv("RAG_QA_GAP_SEMANTIC_THRESHOLD", "0.85")
    assert semantic_threshold() == 0.85
    monkeypatch.setenv("RAG_QA_GAP_SEMANTIC_THRESHOLD", "1.7")    # 越界回默认
    assert semantic_threshold() == 0.90
    monkeypatch.setenv("RAG_QA_GAP_SEMANTIC_THRESHOLD", "abc")
    assert semantic_threshold() == 0.90
