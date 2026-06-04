"""XLSX Layout Classifier — 纯关键词评分，零 LLM 调用。

将 XLSX 文档分为 4 类 layout，用于选择不同的入库策略：
  - equipment_cleaning_standard  (设备清扫/点检基准书)
  - procedure_image_guide        (流程图文作业指导书/SOP)
  - product_spec_instruction     (产品规格书/生产作业指导书)
  - normal_spreadsheet           (普通表格)
"""
from __future__ import annotations

import re
from typing import Sequence


# ── 评分阈值 ──
_MIN_SCORE = 5  # 最高分 ≥ 此值才选非 normal_spreadsheet（文件名命中即可达标）

# ── Type A: 设备清扫/点检基准书 ──
_TYPE_A_FILENAME_KW = [
    "清扫基准", "点检基准", "设备清扫", "设备点检", "设备保养",
]
_TYPE_A_HEADER_KW = {
    "清扫部位名称", "清扫基准", "清扫方法", "清扫工具", "清扫周期",
    "点检部位", "点检方法", "判定标准",
}
_TYPE_A_CONTENT_KW = [
    "清扫时要点检", "点检项目", "清扫时点检",
]

# ── Type B: 流程图文作业指导书 ──
_TYPE_B_FILENAME_KW = [
    "作业指导书", "SOP", "工艺指导书",
]
_TYPE_B_SCOPE_KW = ["目的和范围", "作业程序"]
_TYPE_B_HEADER_KW = ["步骤", "作业说明"]
_RE_FIGURE_REF = re.compile(r"(?:如图|见.*?图)\s*\d+")
_RE_FIGURE_LABEL = re.compile(r"图\s*\d+")

# ── Type C: 产品规格书/生产作业指导书 ──
_TYPE_C_FILENAME_KW = [
    "产品规格书", "生产作业指导书",
]
_TYPE_C_SPEC_KW = ["产品编码", "产品描述", "主要规格"]
_TYPE_C_PKG_KW = ["包装要求", "外箱要求", "内托要求", "合格证"]


def _contains_any(text: str, keywords: Sequence[str]) -> bool:
    return any(kw in text for kw in keywords)


def _count_header_hits(text: str, keywords: set[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


def classify_xlsx_layout(
    filename: str,
    sheet_names: list[str],
    flat_text: str,
) -> tuple[str, dict]:
    """对一个 XLSX 文档进行 layout 分类。

    Args:
        filename: 文件名（含扩展名）
        sheet_names: 所有 sheet 名称列表
        flat_text: 从 openpyxl 提取的全文（blocks 拼接）

    Returns:
        (layout_type, debug_info)
        debug_info 包含 scores、matched_signals、confidence
    """
    combined = filename + " " + flat_text
    sheet_text = " ".join(sheet_names)

    scores = {
        "equipment_cleaning_standard": 0,
        "procedure_image_guide": 0,
        "product_spec_instruction": 0,
        "normal_spreadsheet": 0,
    }
    signals: dict[str, list[str]] = {k: [] for k in scores}

    # ── Type A: 设备清扫/点检基准书 ──
    if _contains_any(filename, _TYPE_A_FILENAME_KW):
        scores["equipment_cleaning_standard"] += 5
        signals["equipment_cleaning_standard"].append(
            f"文件名命中: {[kw for kw in _TYPE_A_FILENAME_KW if kw in filename]}"
        )

    header_hits = _count_header_hits(combined, _TYPE_A_HEADER_KW)
    if header_hits >= 2:
        scores["equipment_cleaning_standard"] += 5
        signals["equipment_cleaning_standard"].append(
            f"表头关键词命中 {header_hits} 个"
        )

    if _contains_any(combined, _TYPE_A_CONTENT_KW):
        scores["equipment_cleaning_standard"] += 3
        signals["equipment_cleaning_standard"].append("含清扫时要点检/点检项目")

    # Sheet 名包含 "每班清扫" / "每周清扫" 等
    cleaning_sheet_kw = ["每班清扫", "每周清扫", "每月清扫"]
    if _contains_any(sheet_text, cleaning_sheet_kw):
        scores["equipment_cleaning_standard"] += 2
        signals["equipment_cleaning_standard"].append(
            f"Sheet 名命中: {[kw for kw in cleaning_sheet_kw if kw in sheet_text]}"
        )

    # ── Type B: 流程图文作业指导书 ──
    # "作业指导书" / "工艺指导书" 是强信号（+5），"SOP" 较弱（+3）
    if _contains_any(filename, ["作业指导书", "工艺指导书"]):
        scores["procedure_image_guide"] += 5
        signals["procedure_image_guide"].append("文件名含作业指导书/工艺指导书")
    elif "SOP" in filename.upper():
        scores["procedure_image_guide"] += 3
        signals["procedure_image_guide"].append("文件名含 SOP")

    if _contains_any(combined, _TYPE_B_SCOPE_KW):
        scores["procedure_image_guide"] += 3
        signals["procedure_image_guide"].append("含目的和范围/作业程序")

    b_header_hits = sum(1 for kw in _TYPE_B_HEADER_KW if kw in combined)
    if b_header_hits >= 2:
        scores["procedure_image_guide"] += 4
        signals["procedure_image_guide"].append("含步骤+作业说明表头")
    elif b_header_hits == 1:
        scores["procedure_image_guide"] += 2
        signals["procedure_image_guide"].append(
            f"含部分表头: {[kw for kw in _TYPE_B_HEADER_KW if kw in combined]}"
        )

    fig_ref_count = len(_RE_FIGURE_REF.findall(combined))
    if fig_ref_count >= 2:
        scores["procedure_image_guide"] += 4
        signals["procedure_image_guide"].append(f"图号引用 {fig_ref_count} 处")

    fig_label_count = len(_RE_FIGURE_LABEL.findall(combined))
    if fig_label_count >= 3:
        scores["procedure_image_guide"] += 2
        signals["procedure_image_guide"].append(f"图号标签 {fig_label_count} 处")

    # ── Type C: 产品规格书/生产作业指导书 ──
    if _contains_any(filename, _TYPE_C_FILENAME_KW):
        scores["product_spec_instruction"] += 5
        signals["product_spec_instruction"].append("文件名含产品规格书/生产作业指导书")

    spec_hits = sum(1 for kw in _TYPE_C_SPEC_KW if kw in combined)
    if spec_hits >= 2:
        scores["product_spec_instruction"] += 4
        signals["product_spec_instruction"].append(
            f"规格关键词命中 {spec_hits} 个"
        )

    pkg_hits = sum(1 for kw in _TYPE_C_PKG_KW if kw in combined)
    if pkg_hits >= 1:
        scores["product_spec_instruction"] += 3
        signals["product_spec_instruction"].append(
            f"包装关键词命中 {pkg_hits} 个"
        )

    # ── 选择最高分 ──
    # 排除 normal_spreadsheet（它始终为 0）
    typed_scores = {
        k: v for k, v in scores.items() if k != "normal_spreadsheet"
    }
    best_type = max(typed_scores, key=typed_scores.get)
    best_score = typed_scores[best_type]

    if best_score < _MIN_SCORE:
        best_type = "normal_spreadsheet"
        signals["normal_spreadsheet"].append(
            f"最高分 {best_score} < 阈值 {_MIN_SCORE}，回退普通表格"
        )

    # 冲突检测
    sorted_types = sorted(typed_scores.items(), key=lambda x: -x[1])
    layout_conflict = (
        len(sorted_types) >= 2
        and sorted_types[0][1] >= _MIN_SCORE
        and sorted_types[1][1] >= _MIN_SCORE
        and abs(sorted_types[0][1] - sorted_types[1][1]) <= 2
    )

    # A 和 B 同分时，A 优先（文件名信号更强）
    if layout_conflict:
        if (sorted_types[0][0] == "procedure_image_guide"
                and sorted_types[1][0] == "equipment_cleaning_standard"):
            if scores["equipment_cleaning_standard"] >= _MIN_SCORE:
                best_type = "equipment_cleaning_standard"

    # 置信度
    total = sum(typed_scores.values()) or 1
    confidence = round(scores[best_type] / total, 2) if best_type != "normal_spreadsheet" else 0.0

    debug_info = {
        "xlsx_layout_type": best_type,
        "confidence": confidence,
        "scores": scores,
        "matched_signals": signals.get(best_type, []),
        "all_signals": signals,
        "layout_conflict": layout_conflict,
    }

    return best_type, debug_info
"""Module providing XLSX Layout Classifier."""
