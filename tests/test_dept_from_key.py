# -*- coding: utf-8 -*-
"""
test_dept_from_key.py — _dept_from_raw_key（OSS raw/ key → 部门代码，8 处拷贝合并）

owner_dept 驱动 HA3 dept_internal 权限过滤，必须只认 raw/ 前缀；原 line 573 缺此 guard。
"""

from opensearch_pipeline.pipeline_nodes import _dept_from_raw_key


def test_raw_key_returns_dept():
    assert _dept_from_raw_key("raw/marketing/SOP/x.docx") == "marketing"
    assert _dept_from_raw_key("raw/hr/a.pdf") == "hr"


def test_non_raw_key_returns_default():
    # 非 raw/ 路径绝不把第二段误当部门（修复 line 573 漂移）
    assert _dept_from_raw_key("processing/assets/admin/x.png") == "unknown"
    assert _dept_from_raw_key("s3://bucket/finance/y") == "unknown"
    assert _dept_from_raw_key("processing/assets/admin/x.png", default="kept") == "kept"


def test_edge_cases():
    assert _dept_from_raw_key("", "d") == "d"
    assert _dept_from_raw_key(None, "d") == "d"        # type: ignore[arg-type]
    # "raw/".split("/") == ["raw", ""] → parts[1] == ""（与原 8 处内联实现逐字一致：
    # 它们也是 if len(parts) > 1: dept = parts[1]，对 "raw/" 同样得到空串。不在 dedup 中改行为）
    assert _dept_from_raw_key("raw/", "d") == ""
    assert _dept_from_raw_key("rawx/marketing/y", "d") == "d"  # 不是 raw/ 前缀
