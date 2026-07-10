# -*- coding: utf-8 -*-
"""ontology/normalize.py —— 别名归一的表驱动单测。

重点锁死三条铁律的反例（可行性红队 critical）：
① 不剥改模后缀（ABC123 ≠ ABC123-M）② customer 保中文/括号/大小写（A(高钙) ≠ A(低钙)）
③ lab_sample 内容原样。归一是 uk_ns_norm_active 唯一键的第一道闸——这里错一个字符，
库里就是一次事实合并。
"""
import pytest

from opensearch_pipeline.ontology.normalize import dispatch_key, normalize

# ── 表驱动正例：(namespace, raw, expected_norm) ──────────────────────────────────
CASES = [
    # u8/internal/lot_code：去首尾空白 + 全半角 + 大写化，保留全部后缀
    ("u8", " abc123-m ", "ABC123-M"),
    ("u8", "ABC123", "ABC123"),
    ("u8", "ａｂｃ１２３－Ｍ", "ABC123-M"),          # 全角字母/数字/连字符
    ("u8", "　abc123　", "ABC123"),           # 全角空格修剪
    ("u8", "abc123-n", "ABC123-N"),
    ("u8", "abc123-w", "ABC123-W"),
    ("internal", " flp-x01 ", "FLP-X01"),
    ("lot_code", "lt20260701 ", "LT20260701"),
    # customer:*：仅去首尾空白 + 全半角统一；保中文/括号/大小写
    ("customer:KFC", "A（高钙）", "A(高钙)"),
    ("customer:KFC", "A(高钙)", "A(高钙)"),
    ("customer:KFC", "a(高钙)", "a(高钙)"),           # 大小写保留，不抹平
    ("customer:KFC", " KFC-大杯 ", "KFC-大杯"),
    # supplier/material_grade：内部空白皆噪声，大写化
    ("supplier:金发", " pp-1100 ", "PP-1100"),
    ("supplier:金发", "pp 1100", "PP1100"),
    ("supplier:金发", "ＰＰ－１１００", "PP-1100"),
    ("material_grade", "pp\t1100", "PP1100"),
    # lab_sample：去全部空白，内容原样（大小写/中文保留）
    ("lab_sample", "黑色注塑叉子", "黑色注塑叉子"),
    ("lab_sample", "黑色 注塑叉子", "黑色注塑叉子"),
    ("lab_sample", "pp1100+浅蓝", "pp1100+浅蓝"),
    # 未登记命名空间：保守默认（去首尾空白 + 全半角，不改大小写）
    ("us_wh:excel", " Ｘ1 ", "X1"),
    ("dingtalk_uid", "manager4402", "manager4402"),
    ("hr:emp_no", " 00123 ", "00123"),
]


@pytest.mark.parametrize("namespace,raw,expected", CASES)
def test_normalize_table(namespace, raw, expected):
    assert normalize(namespace, raw) == expected


@pytest.mark.parametrize("namespace,raw,expected", CASES)
def test_normalize_idempotent(namespace, raw, expected):
    """归一后再归一必须不变——回填 worker 会对已归一值重复过闸。"""
    assert normalize(namespace, expected) == expected


# ── 铁律反例：错一个就是唯一键上的事实合并 ────────────────────────────────────────
@pytest.mark.parametrize(
    "namespace",
    ["u8", "internal", "customer:KFC", "supplier:X", "material_grade",
     "lab_sample", "lot_code", "us_wh:excel"],
)
@pytest.mark.parametrize("suffix", ["-M", "-N", "-W"])
def test_never_strips_revision_suffix(namespace, suffix):
    base = normalize(namespace, "ABC123")
    variant = normalize(namespace, f"ABC123{suffix}")
    assert base != variant, f"{namespace} 剥掉了改模后缀 {suffix}——基础品与变体被合并"


def test_customer_parenthetical_semantics_distinct():
    assert normalize("customer:KFC", "A(高钙)") != normalize("customer:KFC", "A(低钙)")
    # 全半角括号统一后仍相等（同一含义两种输入法）
    assert normalize("customer:KFC", "A（高钙）") == normalize("customer:KFC", "A(高钙)")


def test_customer_scope_lives_in_namespace_not_value():
    """业务 scope 唯一性由 namespace 编码（S4）：两个客户的同名编号互不相干。"""
    assert dispatch_key("customer:KFC") == dispatch_key("customer:麦当劳") == "customer"
    # 归一值相同没关系——唯一键是 (namespace, norm)，namespace 不同即不同行
    assert normalize("customer:KFC", "A1") == normalize("customer:麦当劳", "A1")


# ── dispatch 与防御 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "namespace,key",
    [("customer:KFC", "customer"), ("u8", "u8"), ("supplier:金发", "supplier"),
     (" U8 ", "u8"), ("customer:KFC:嵌套", "customer")],
)
def test_dispatch_key(namespace, key):
    assert dispatch_key(namespace) == key


@pytest.mark.parametrize("raw", ["", "   ", "　", None, 123])
def test_normalize_rejects_empty_or_nonstr(raw):
    with pytest.raises((ValueError, TypeError)):
        normalize("u8", raw)  # type: ignore[arg-type]


@pytest.mark.parametrize("namespace", ["", "  "])
def test_normalize_rejects_empty_namespace(namespace):
    with pytest.raises(ValueError, match="namespace"):
        normalize(namespace, "ABC")
