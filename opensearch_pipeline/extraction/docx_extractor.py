# -*- coding: utf-8 -*-
"""
docx_extractor.py — Word/DOCX 文本提取器

使用 python-docx 提取段落和表格，生成结构化 blocks。
逻辑 refactored from scan_pending_clean.py:L248-L265。

标题检测策略（用户决策：用 style + regex fallback）：
  1. 优先用 paragraph.style.name 判断 heading（Heading 1 / Heading 2）
  2. 如果 style 是 Normal 但文本匹配中文标题模式 → 也标记为 heading
  3. 工厂文档可能不使用标准 Word 样式，regex fallback 确保兼容

生产依赖：python-docx
模拟模式：不需要真实 DOCX 文件
"""

import re
from typing import List, Optional, Tuple

from opensearch_pipeline.extraction.schema import ExtractedBlock

# Word 标题样式名 → heading level 映射
_STYLE_LEVEL_MAP = {
    "Heading 1": 1, "Heading 2": 2, "Heading 3": 3, "Heading 4": 4,
    "heading 1": 1, "heading 2": 2, "heading 3": 3, "heading 4": 4,
    "标题 1": 1, "标题 2": 2, "标题 3": 3, "标题 4": 4,
    "Title": 1, "Subtitle": 2,
}

# 中文标题正则 fallback
_CN_HEADING_RE = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十\d]+[章节条款部分]\s*.+|"
    r"[一二三四五六七八九十]+[、\.]\s*.+|"
    r"（[一二三四五六七八九十\d]+）\s*.+"
    r")$"
)
_SUB_HEADING_RE = re.compile(r"^\d+\.\d+\s+.+$")


def _detect_heading_level(style_name: str, text: str) -> Optional[int]:
    """
    检测标题级别。

    策略：style 优先，regex fallback。
    """
    # 1. Word 样式检测
    if style_name in _STYLE_LEVEL_MAP:
        return _STYLE_LEVEL_MAP[style_name]

    # 2. 样式名包含 "Heading" 或 "标题"
    if "heading" in style_name.lower():
        # 尝试提取数字
        nums = re.findall(r"\d+", style_name)
        if nums:
            return min(int(nums[0]), 4)
        return 2

    # 3. Regex fallback：中文标题模式 (限制最大长度以防长正文段落被误判为标题而遗漏)
    stripped = text.strip()
    if len(stripped) <= 30:
        if _CN_HEADING_RE.match(stripped):
            if stripped.startswith("第"):
                return 1
            return 2

        if _SUB_HEADING_RE.match(stripped):
            return 3

    return None


def extract_docx(
    local_path: str,
) -> Tuple[List[ExtractedBlock], List[str]]:
    """
    从 DOCX 文件提取 blocks。

    使用递归的顺序块提取器来保留段落与表格的自然物理阅读顺序，
    并自动解包作为边框装饰的单单元格（1x1）外表层表格。

    Returns:
        (blocks, warnings)
    """
    try:
        import docx
        from docx.text.paragraph import Paragraph
        from docx.table import Table
    except ImportError:
        return [], ["python-docx not installed, cannot extract DOCX"]

    warnings: List[str] = []
    blocks: List[ExtractedBlock] = []
    current_section_ref = [None]  # 用列表传递可变引用，跟踪当前所属 section_path

    try:
        document = docx.Document(local_path)
    except Exception as e:
        return [], [f"Failed to open DOCX: {e}"]

    def _extract_recursive(parent) -> List[ExtractedBlock]:
        extracted = []
        if hasattr(parent, 'element') and hasattr(parent.element, 'body'):
            parent_elm = parent.element.body
        elif hasattr(parent, '_tc'):
            parent_elm = parent._tc
        elif hasattr(parent, 'element'):
            parent_elm = parent.element
        else:
            parent_elm = parent

        table_idx = 0
        for child in parent_elm.iterchildren():
            if child.tag.endswith('p'):
                para = Paragraph(child, parent)
                text = para.text.strip()
                if not text:
                    continue

                style_name = para.style.name if para.style else "Normal"
                heading_level = _detect_heading_level(style_name, text)

                if heading_level is not None:
                    current_section_ref[0] = text
                    extracted.append(ExtractedBlock(
                        block_type="heading",
                        text=text,
                        level=heading_level,
                        section_path=current_section_ref[0],
                        source="native",
                        extra={"word_style": style_name},
                    ))
                else:
                    extracted.append(ExtractedBlock(
                        block_type="paragraph",
                        text=text,
                        section_path=current_section_ref[0],
                        source="native",
                        extra={"word_style": style_name},
                    ))
            elif child.tag.endswith('tbl'):
                table = Table(child, parent)
                # 识别单单元格 (1x1) 装饰性外表格并递归解包其内部子段落/子表格
                if len(table.rows) == 1 and len(table.rows[0].cells) == 1:
                    cell = table.rows[0].cells[0]
                    extracted.extend(_extract_recursive(cell))
                else:
                    rows_text = []
                    for row in table.rows:
                        cells = []
                        for cell in row.cells:
                            cell_text = cell.text.strip()
                            if cell_text:
                                cells.append(cell_text)
                        if cells:
                            rows_text.append(" | ".join(cells))

                    if rows_text:
                        table_md = "\n".join(f"| {row} |" for row in rows_text)
                        extracted.append(ExtractedBlock(
                            block_type="table",
                            text=table_md,
                            section_path=current_section_ref[0],
                            source="native",
                            extra={"table_index": table_idx, "row_count": len(rows_text)},
                        ))
                    table_idx += 1
        return extracted

    try:
        blocks = _extract_recursive(document)
    except Exception as e:
        warnings.append(f"Recursive extraction encountered an issue: {e}")
        # fallback to basic non-recursive paragraphs extraction to be safe
        blocks = []
        for para in document.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style_name = para.style.name if para.style else "Normal"
            blocks.append(ExtractedBlock(
                block_type="paragraph",
                text=text,
                section_path=None,
                source="native",
                extra={"word_style": style_name},
            ))

    return blocks, warnings
