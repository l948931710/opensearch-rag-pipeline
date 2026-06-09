# -*- coding: utf-8 -*-
"""
test_sources_format.py — _format_sources_text（卡片/Markdown/回调三处共用，B5）
"""

from opensearch_pipeline.dingtalk_card import _format_sources_text


def test_numbered_style_with_section_and_score():
    out = _format_sources_text([
        {"title": "员工手册", "section": "请假", "score": 9.12},
        {"title": "考勤制度", "score": 7.0},
    ])
    assert out == "1. 员工手册 > 请假（相关度 9.12）\n2. 考勤制度（相关度 7.00）"


def test_bullet_style():
    out = _format_sources_text([{"title": "A", "score": 1.0}], style="bullet")
    assert out == "- A（相关度 1.00）"


def test_dedup_by_title_keeps_original_numbering():
    # 去重后编号取原列表位置（去重项被跳过，可有间隔）——沿用原卡片行为
    out = _format_sources_text([
        {"title": "A"}, {"title": "A"}, {"title": "B"},
    ])
    assert out == "1. A\n3. B"


def test_doc_name_fallback_and_non_dict():
    assert _format_sources_text([{"doc_name": "外部文档"}]) == "1. 外部文档"
    assert _format_sources_text(["纯字符串来源"]) == "1. 纯字符串来源"
    assert _format_sources_text([{}]) == "1. 未知文档"


def test_empty_and_missing_score_no_noise():
    assert _format_sources_text([]) == ""
    # score 缺失 → 不显示 "（相关度 0.00）" 噪声（回调路径修复点）
    assert _format_sources_text([{"title": "X"}]) == "1. X"
