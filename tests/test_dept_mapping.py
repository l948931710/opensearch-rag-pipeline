# -*- coding: utf-8 -*-
"""
test_dept_mapping.py — 钉钉中文部门名 → ACL 权限组代码【列表】 归一化

用户部门来自钉钉（中文名），chunk.owner_dept 来自 OSS 目录（组代码），HA3 权限过滤
要求两边完全相等。_normalize_dept_to_codes 把中文叶子部门名归一化为权限组列表（一名可多组），
并过 _VALID_ACL_GROUPS 白名单（H2）；未知/非法 → []（fail-closed）。
"""

from opensearch_pipeline.dingtalk_identity import (
    _PRODUCTION_WORKSHOP_DEPTS,
    _normalize_dept_to_codes,
)


def test_leaf_depts_map_to_group_lists():
    # 叶子部门（权限单口径）→ 一名可映射多组
    assert _normalize_dept_to_codes("财务部") == ["finance"]
    assert _normalize_dept_to_codes("自动化信息部") == ["it"]
    assert _normalize_dept_to_codes("国际贸易部") == ["marketing", "production"]
    assert _normalize_dept_to_codes("国内营销部") == ["marketing", "production"]
    assert _normalize_dept_to_codes("电子商务部") == ["marketing", "production"]
    assert _normalize_dept_to_codes("计划部") == ["marketing", "pmc"]
    assert _normalize_dept_to_codes("资材部") == ["supply", "pmc"]
    assert _normalize_dept_to_codes("行政部") == ["admin"]
    assert _normalize_dept_to_codes("人力资源部") == ["hr"]
    assert _normalize_dept_to_codes("技术部") == ["quality"]
    assert _normalize_dept_to_codes("研发部") == ["rd"]


def test_center_names_single_group_fallback():
    # 单组无歧义的中心名保留为兜底
    assert _normalize_dept_to_codes("营销中心") == ["marketing"]
    assert _normalize_dept_to_codes("生产中心") == ["production"]
    assert _normalize_dept_to_codes("PMC部") == ["pmc"]


def test_codes_pass_through_whitelisted():
    # 已是合法组代码 → 原样返回（幂等，单元素列表）
    for code in ("marketing", "hr", "production", "admin", "it", "finance", "supply", "rd", "quality", "pmc"):
        assert _normalize_dept_to_codes(code) == [code]


def test_csv_and_list_inputs():
    assert _normalize_dept_to_codes("marketing,production") == ["marketing", "production"]
    assert _normalize_dept_to_codes(["marketing", "production"]) == ["marketing", "production"]
    assert _normalize_dept_to_codes(["国际贸易部"]) == ["marketing", "production"]
    # 多部门用户的部门名 CSV（_fetch_dingtalk_user_info 产出形态）
    assert _normalize_dept_to_codes("国际贸易部,行政部") == ["marketing", "production", "admin"]


def test_dedupe():
    assert _normalize_dept_to_codes(["marketing", "marketing"]) == ["marketing"]
    # 计划部(marketing,pmc) + 国际贸易部(marketing,production) → marketing 去重
    assert _normalize_dept_to_codes("计划部,国际贸易部") == ["marketing", "pmc", "production"]


def test_unknown_and_illegal_are_fail_closed_empty():
    # 未在映射表 + 不在白名单 → 丢弃为 [] （仅 public，绝不误授权）
    for name in ("综合管理中心", "办公室", "杭州分公司", "不存在的部门"):
        assert _normalize_dept_to_codes(name) == []
    # 形似但非法的组代码（OSS 子线代码不在权限组白名单）→ 丢弃
    assert _normalize_dept_to_codes("production_injection") == []
    assert _normalize_dept_to_codes(["marketing", "production_injection"]) == ["marketing"]


def test_edge_cases():
    assert _normalize_dept_to_codes(None) == []
    assert _normalize_dept_to_codes("") == []
    assert _normalize_dept_to_codes("   ") == []
    assert _normalize_dept_to_codes(["", "  "]) == []
    assert _normalize_dept_to_codes("  人力资源部  ") == ["hr"]  # strip 后命中


# ── 生产中心子树 → production 伞组（H4：subline 用户实际拿到 production，闭合 risk ④） ──

def test_production_workshop_leaves_map_to_production():
    # 钉钉把一线员工挂在叶子部门上；这些名字都必须归一化为 'production' 伞组。
    # 涵盖：中间节点(事业部/车间)、叶子(机修/料房/班组长/仓管)、含特殊字符(— 、)的名字。
    for name in (
        "模具车间",      # 线上实有 1 名用户(陈仁国)被误判 —— 本次修复的确证 bug
        "模具A",
        "模具B",
        "注塑事业部",
        "注塑车间—料房",
        "三车间A区机修",
        "纸杯—淋膜",
        "原辅料、五金仓库",
        "吹膜车间",
        "纸箱车间",
        "纸浆模塑事业部",
        "精益部",
    ):
        assert _normalize_dept_to_codes(name) == ["production"], name


def test_zicaibu_under_production_keeps_supply_pmc():
    # 资材部 结构上挂在生产中心下，但权限单口径属 [supply, pmc]——_DEPT_NAME_TO_GROUPS
    # 精确映射优先，不得被生产子树伞组覆盖；且 资材部 不应出现在生产子树集合里。
    assert _normalize_dept_to_codes("资材部") == ["supply", "pmc"]
    assert "资材部" not in _PRODUCTION_WORKSHOP_DEPTS


def test_unknown_workshop_name_fail_closed():
    # 形似生产但不在快照里的名字 → fail-closed（仅 public，绝不误授予 production）
    for name in ("注塑五车间", "模具C", "不存在的车间", "生产某部"):
        assert name not in _PRODUCTION_WORKSHOP_DEPTS
        assert _normalize_dept_to_codes(name) == []


def test_production_workshop_csv_dedupes_with_other_groups():
    # 多部门 CSV：模具车间(production) + 国际贸易部(marketing,production) → production 去重
    assert _normalize_dept_to_codes("模具车间,国际贸易部") == ["production", "marketing"]
    # 生产叶子 + 资材部：production 与 supply/pmc 并存（资材部精确映射优先）
    assert _normalize_dept_to_codes("注塑事业部,资材部") == ["production", "supply", "pmc"]
