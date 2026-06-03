# -*- coding: utf-8 -*-
"""
image_relation_classifier.py — 图片-步骤关系分类器（规则引擎）

对 step_card 中绑定的图片，根据 VLM caption / OCR 文本与步骤文本的语义关系，
分类为三种关系类型：

  - primary:           操作证据/步骤截图，与步骤强相关
  - supporting:        补充说明/参考示意，辅助理解
  - visual_knowledge:  独立参考知识（流程图/对照表/规格表等），不从属于单一步骤

设计原则：
  1. 保守策略：位置紧邻时默认 primary/supporting，不轻易升级为 visual_knowledge
  2. 只有明确识别为"字段说明/关系图/缺陷对照/流程图"等独立知识类图片才标记 visual_knowledge
  3. 零 API 调用，纯规则 + jieba 关键词重叠
  4. 低置信度结果记录 audit_flag，不影响主流程

依赖：jieba, re (标准库)
"""

import re
from dataclasses import dataclass, field
from typing import Set

import jieba

# ────────────────────────────────────────────────────────────────
# 数据类
# ────────────────────────────────────────────────────────────────


@dataclass
class ImageRelation:
    """图片-步骤关系分类结果。"""
    relation: str           # "primary" | "supporting" | "visual_knowledge"
    confidence: float       # 0.0 - 1.0
    reason: str             # 分类理由（调试/审计用）
    audit_flag: bool = False  # True = 低置信度，需人工审核


# ────────────────────────────────────────────────────────────────
# 关键词集合
# ────────────────────────────────────────────────────────────────

# UI 截图类关键词 → 强 primary 信号
_UI_KEYWORDS: Set[str] = {
    "界面", "截图", "系统", "窗口", "对话框", "菜单", "按钮", "弹出",
    "下拉", "勾选", "输入框", "选项卡", "工具栏", "导航", "登录",
    "操作界面", "设置页面", "模块", "点击", "双击", "右键",
    "U8", "ERP", "有度", "钉钉",
}

# 参考说明类关键词 → supporting 信号
_REFERENCE_KEYWORDS: Set[str] = {
    "标准", "规格", "参考", "示意", "示例", "样例", "模板",
    "注意事项", "要求", "规范", "说明", "备注", "提示",
    "样品", "效果图", "实物", "成品", "外观",
}

# 独立知识类关键词 → visual_knowledge 信号（保守，需要强信号才升级）
_KNOWLEDGE_KEYWORDS: Set[str] = {
    "流程图", "架构图", "组织架构", "拓扑图", "关系图",
    "对照表", "对比表", "规格表", "参数表", "汇总表",
    "缺陷对照", "不良品对照", "合格标准对照",
    "字段说明", "字段含义", "数据字典",
    "工艺流程", "生产流程", "审批流程",
    "整体概览", "全景图", "总览",
}

# 停用词（jieba 分词后过滤）
_STOPWORDS: Set[str] = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
    "它", "们", "那", "什么", "为", "以", "及", "或", "与", "等",
    "如", "从", "中", "对", "把", "被", "将", "可以", "应该", "需要",
    "进行", "进入", "通过", "使用", "操作", "然后", "之后", "如下",
    "按照", "根据", "其中", "以下", "下面", "上面",
}


# ────────────────────────────────────────────────────────────────
# 公共接口
# ────────────────────────────────────────────────────────────────

def classify_image_relation(
    step_text: str,
    caption: str,
    ocr_keywords: str = "",
    has_annotation: bool = False,
    position: str = "inline",
) -> ImageRelation:
    """
    分类图片与步骤的关系。

    Args:
        step_text:       当前步骤文本
        caption:         VLM visual_summary 或 OCR 摘要
        ocr_keywords:    OCR 提取的关键词
        has_annotation:  是否有 annotation_map (①②③ 标注)
        position:        图片位置 — "inline"(紧跟步骤) | "trailing"(步骤尾部)
                         | "between_steps"(步骤间)

    Returns:
        ImageRelation 分类结果
    """
    # ── 规则 1：有 annotation 显式绑定 → 直接 primary ──
    if has_annotation:
        return ImageRelation("primary", 1.0, "annotation 显式绑定")

    # 无 caption 且无 OCR → 纯位置绑定，保守默认
    if not caption and not ocr_keywords:
        if position == "between_steps":
            return ImageRelation("primary", 0.4, "无语义信息，步骤间位置默认绑定",
                                 audit_flag=True)
        return ImageRelation("primary", 0.5, "无语义信息，位置紧邻默认绑定")

    # 合并语义信号
    combined_caption = " ".join(filter(None, [caption, ocr_keywords]))

    # ── 规则 2：检测 visual_knowledge 强信号（保守）──
    # 只有明确包含独立知识类关键词时才升级
    vk_hits = _keyword_hits(combined_caption, _KNOWLEDGE_KEYWORDS)
    if len(vk_hits) >= 1:
        # 额外检查：如果步骤文本也包含相关内容，说明图片是步骤的一部分
        step_vk_hits = _keyword_hits(step_text, _KNOWLEDGE_KEYWORDS)
        if step_vk_hits:
            # 步骤本身就在讨论流程图/对照表，图片是步骤的配图
            return ImageRelation("primary", 0.85,
                                 f"步骤本身涉及 {step_vk_hits}，图片为步骤配图")
        # 步骤不涉及 → 这是独立知识图片
        return ImageRelation("visual_knowledge", 0.8,
                             f"独立知识类图片: {vk_hits}")

    # ── 规则 3：UI 截图关键词 → 强 primary ──
    ui_hits = _keyword_hits(combined_caption, _UI_KEYWORDS)
    if ui_hits:
        return ImageRelation("primary", 0.9,
                             f"UI 截图类: {ui_hits}")

    # ── 规则 4：关键词重叠度 ──
    overlap = keyword_overlap(step_text, combined_caption)

    if overlap > 0.3:
        return ImageRelation("primary", min(0.95, 0.7 + overlap),
                             f"关键词高度重叠 ({overlap:.2f})")

    if overlap > 0.15:
        # 中等重叠 → 检查是否有参考类信号
        ref_hits = _keyword_hits(combined_caption, _REFERENCE_KEYWORDS)
        if ref_hits:
            return ImageRelation("supporting", 0.75,
                                 f"参考说明 + 中等重叠 ({overlap:.2f}): {ref_hits}")
        return ImageRelation("primary", 0.7,
                             f"关键词中等重叠 ({overlap:.2f})")

    # ── 规则 5：参考说明类关键词 ──
    ref_hits = _keyword_hits(combined_caption, _REFERENCE_KEYWORDS)
    if ref_hits:
        return ImageRelation("supporting", 0.7,
                             f"参考说明类图片: {ref_hits}")

    # ── 规则 6：低重叠的位置默认 ──
    if overlap > 0.05:
        return ImageRelation("supporting", 0.55,
                             f"关键词低重叠 ({overlap:.2f})，标记为补充",
                             audit_flag=True)

    # ── 规则 7：兜底 — 保守策略，紧邻默认 primary ──
    if position in ("inline", "trailing"):
        return ImageRelation("primary", 0.4,
                             "关键词无重叠但位置紧邻，保守绑定",
                             audit_flag=True)

    # between_steps 且无任何语义信号
    return ImageRelation("supporting", 0.35,
                         "步骤间图片，低语义关联，标记为补充说明",
                         audit_flag=True)


def keyword_overlap(text_a: str, text_b: str) -> float:
    """
    计算两段文本的关键词 Jaccard 相似度。

    使用 jieba 分词，过滤停用词和短词（<2 字符），
    计算 |A ∩ B| / |A ∪ B|。

    Args:
        text_a: 文本 A（通常是步骤文本）
        text_b: 文本 B（通常是图片 caption）

    Returns:
        Jaccard 相似度 [0.0, 1.0]
    """
    words_a = _segment(text_a)
    words_b = _segment(text_b)
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) if union else 0.0


# ────────────────────────────────────────────────────────────────
# 内部辅助
# ────────────────────────────────────────────────────────────────

def _segment(text: str) -> Set[str]:
    """jieba 分词 + 过滤停用词 + 过滤短词。"""
    if not text:
        return set()
    words = set()
    for w in jieba.cut(text):
        w = w.strip()
        if len(w) < 2:
            continue
        if w in _STOPWORDS:
            continue
        words.add(w)
    return words


def _keyword_hits(text: str, keyword_set: Set[str]) -> Set[str]:
    """检查文本中包含哪些关键词。直接子串匹配。"""
    if not text:
        return set()
    hits = set()
    for kw in keyword_set:
        if kw in text:
            hits.add(kw)
    return hits
