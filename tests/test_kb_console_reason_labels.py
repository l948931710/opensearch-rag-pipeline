# -*- coding: utf-8 -*-
"""
tests/test_kb_console_reason_labels.py — 点踩原因码翻译字典/助手单测。

背景（2026-07-22）：小程序 feedback-bar 自上线起就发送 wrong_image（「图片不对」），
console 点踩面板对齐小程序后同样发送，但 _KB_DOWNVOTE_REASON_LABELS 缺该条，
差评复核页与治理聚合只能回退显示英文原码。本文件锁死：多码翻译、未知码
回退、wrong_image 中文标签，以及小程序全部原因码与字典的覆盖对齐。
纯函数单测，无 DB/网络依赖。
"""

from opensearch_pipeline.routes.kb_console import (
    _KB_DOWNVOTE_REASON_LABELS,
    _kb_reason_labels,
)

# 与 fuling-rag-miniapp/components/feedback-bar/feedback-bar.js 的 REASONS 保持同步；
# 客户端新增原因码时此清单与字典须一并更新（本测试即为漂移哨兵）。
_MINIAPP_REASON_CODES = [
    "inaccurate",
    "irrelevant",
    "incomplete",
    "not_found",
    "wrong_image",
    "outdated",
]


def test_multi_code_comma_joined_translation():
    """逗号拼接多选码逐个翻译，保序。"""
    assert _kb_reason_labels("inaccurate,not_found,wrong_image") == [
        "不准确",
        "未找到",
        "图片不对",
    ]


def test_unknown_code_falls_back_to_raw():
    """未知码安全回退显示原始 code，且不影响相邻已知码。"""
    assert _kb_reason_labels("mystery_code") == ["mystery_code"]
    assert _kb_reason_labels("inaccurate,mystery_code") == ["不准确", "mystery_code"]


def test_wrong_image_chinese_label():
    """wrong_image 映射「图片不对」（与小程序 REASONS label 同文案）。"""
    assert _KB_DOWNVOTE_REASON_LABELS["wrong_image"] == "图片不对"
    assert _kb_reason_labels("wrong_image") == ["图片不对"]


def test_all_miniapp_reason_codes_covered():
    """小程序发送的全部原因码都必须有中文标签（防再现本次缺条类缺口）。"""
    missing = [c for c in _MINIAPP_REASON_CODES if c not in _KB_DOWNVOTE_REASON_LABELS]
    assert missing == []
