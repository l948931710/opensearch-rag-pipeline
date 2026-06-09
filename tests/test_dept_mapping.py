# -*- coding: utf-8 -*-
"""
test_dept_mapping.py — 钉钉中文部门名 → owner_dept 英文代码 归一化

用户部门来自钉钉（中文名），chunk.owner_dept 来自 OSS 目录（英文代码），HA3 权限过滤
要求两边完全相等。_normalize_dept_to_code 负责把中文名归一化为代码；未知部门 fail-closed。
"""

from opensearch_pipeline.dingtalk_identity import _normalize_dept_to_code


def test_known_chinese_names_map_to_codes():
    assert _normalize_dept_to_code("营销中心") == "marketing"
    assert _normalize_dept_to_code("人力资源部") == "hr"
    assert _normalize_dept_to_code("PMC部") == "pmc"
    assert _normalize_dept_to_code("生产中心") == "production"


def test_codes_pass_through_idempotent():
    # 已是英文代码 → 原样返回（再归一化一次结果不变）
    for code in ("marketing", "hr", "production", "admin", "it"):
        assert _normalize_dept_to_code(code) == code


def test_unmapped_dept_is_fail_closed_passthrough():
    # 未在表中的部门原样返回 → 匹配不到任何 chunk（仅 public 可见），绝不误授权
    for name in ("综合管理中心", "办公室", "电子商务部", "杭州分公司", "不存在的部门"):
        assert _normalize_dept_to_code(name) == name


def test_edge_cases():
    assert _normalize_dept_to_code(None) is None
    assert _normalize_dept_to_code("") == ""
    assert _normalize_dept_to_code("  人力资源部  ") == "hr"  # strip 后命中
