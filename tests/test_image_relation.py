# -*- coding: utf-8 -*-
"""
test_image_relation.py — 图片-步骤关系分类器单元测试
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opensearch_pipeline.extraction.image_relation_classifier import (
    classify_image_relation,
    keyword_overlap,
    ImageRelation,
)


def test_annotation_always_primary():
    """有 annotation 的图片应始终为 primary"""
    rel = classify_image_relation(
        step_text="步骤1：泡水2小时",
        caption="奶茶杯泡水",
        has_annotation=True,
    )
    assert rel.relation == "primary"
    assert rel.confidence == 1.0
    assert not rel.audit_flag


def test_no_caption_inline_default_primary():
    """无 caption 的 inline 图片默认 primary"""
    rel = classify_image_relation(
        step_text="步骤1：每个机台取10根吸管",
        caption="",
        position="inline",
    )
    assert rel.relation == "primary"
    assert rel.confidence == 0.5


def test_no_caption_between_steps_audit_flag():
    """无 caption 的 between_steps 图片默认 primary 但标记 audit"""
    rel = classify_image_relation(
        step_text="步骤1：取吸管",
        caption="",
        position="between_steps",
    )
    assert rel.relation == "primary"
    assert rel.audit_flag  # 低置信度需要审核


def test_ui_screenshot_primary():
    """UI 截图类 caption → primary"""
    rel = classify_image_relation(
        step_text="点击保存按钮",
        caption="U8系统生产订单列表界面截图，显示保存按钮和查询窗口",
    )
    assert rel.relation == "primary"
    assert rel.confidence >= 0.9


def test_high_keyword_overlap_primary():
    """关键词高度重叠 → primary"""
    rel = classify_image_relation(
        step_text="将十根吸管开胶头朝下插入到热水中",
        caption="测试人员将纸吸管逐根插入热水容器中进行耐热测试",
    )
    assert rel.relation == "primary"
    assert rel.confidence >= 0.7


def test_reference_keywords_supporting():
    """参考说明类关键词 → supporting 或 visual_knowledge"""
    rel = classify_image_relation(
        step_text="检查温度是否达标",
        caption="温度标准参考对照表，列出不同产品的温度要求",
    )
    # "对照表"是 visual_knowledge 强信号，步骤本身不涉及对照表 → visual_knowledge
    # 若 caption 只有"参考"类关键词则为 supporting
    assert rel.relation in ("supporting", "visual_knowledge")


def test_flowchart_visual_knowledge():
    """明确的流程图 → visual_knowledge（且步骤本身不涉及流程图时）"""
    rel = classify_image_relation(
        step_text="点击确认按钮完成提交",
        caption="注塑生产全流程图，从原料准备到质量检验的完整工艺流程",
    )
    assert rel.relation == "visual_knowledge"


def test_flowchart_in_context_primary():
    """步骤本身讨论流程图时 → primary（不升级为 visual_knowledge）"""
    rel = classify_image_relation(
        step_text="参照以下工艺流程图进行操作",
        caption="注塑生产工艺流程图",
    )
    assert rel.relation == "primary"
    assert "步骤本身涉及" in rel.reason


def test_comparison_table_visual_knowledge():
    """缺陷对照表 → visual_knowledge"""
    rel = classify_image_relation(
        step_text="检查产品表面是否翘边",
        caption="产品缺陷对照表，展示合格与不合格品的外观差异",
    )
    assert rel.relation == "visual_knowledge"


def test_keyword_overlap_calculation():
    """关键词重叠度计算"""
    # 完全不相关
    o1 = keyword_overlap("打印交货单", "注塑设备维护保养")
    assert o1 < 0.1

    # 高度相关
    o2 = keyword_overlap(
        "在U8系统中点击查询按钮搜索商检号",
        "U8生产订单列表界面，输入商检号查询"
    )
    assert o2 > 0.2

    # 空文本
    assert keyword_overlap("", "test") == 0.0
    assert keyword_overlap("test", "") == 0.0


def test_medium_overlap_with_reference():
    """中等重叠 + 参考类关键词 → supporting"""
    rel = classify_image_relation(
        step_text="将数据填入领料单",
        caption="领料单填写示例模板",
    )
    assert rel.relation in ("supporting", "primary")


def test_conservative_default():
    """保守策略：有 caption 但低重叠时，inline 位置仍然 primary"""
    rel = classify_image_relation(
        step_text="步骤3：将十根吸管开胶头朝下插入",
        caption="某个完全无关的图片描述",
        position="inline",
    )
    # 保守策略：即使语义不太匹配，inline 位置也应该是 primary 或 supporting
    assert rel.relation in ("primary", "supporting")


def test_audit_flag_on_low_confidence():
    """低置信度应设置 audit_flag"""
    rel = classify_image_relation(
        step_text="步骤1：准备材料",
        caption="一张模糊的照片",
        position="inline",
    )
    # 低重叠 + inline → 应该有 audit_flag
    if rel.confidence < 0.5:
        assert rel.audit_flag


# ── 运行 ──
if __name__ == "__main__":
    import traceback
    tests = [
        test_annotation_always_primary,
        test_no_caption_inline_default_primary,
        test_no_caption_between_steps_audit_flag,
        test_ui_screenshot_primary,
        test_high_keyword_overlap_primary,
        test_reference_keywords_supporting,
        test_flowchart_visual_knowledge,
        test_flowchart_in_context_primary,
        test_comparison_table_visual_knowledge,
        test_keyword_overlap_calculation,
        test_medium_overlap_with_reference,
        test_conservative_default,
        test_audit_flag_on_low_confidence,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed}/{passed+failed} passed")
