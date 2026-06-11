# -*- coding: utf-8 -*-
"""
pdf_extractor.py — PDF 文本提取器（Layout-Aware）

提取策略（按优先级）：
  1. pdfplumber layout-aware（字号推断标题、表格提取、页眉页脚过滤）
  2. pypdf / PyPDF2（fallback，flat text）

如果两者都提取 0 chars，则返回空 blocks，由 OCR fallback 接管。

Layout-Aware 能力（pdfplumber 路径）：
  - Pass 1: 文档级统计（字号直方图 → body_size + heading levels，页眉页脚 y 检测）
  - Pass 2: 逐页结构化提取（表格优先 → 排除表格区域 → 文本行分组 → heading 检测）
  - section_path 追踪：与 docx_extractor 一致

生产依赖：pdfplumber 或 pypdf
模拟模式：不需要真实 PDF 文件
"""

import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

from opensearch_pipeline.extraction.schema import (
    STEP_BOUNDARY_PATTERN, ExtractedBlock, is_pseudo_heading,
)
from opensearch_pipeline.extraction.text_extractor import (
    extract_text_file,
    extract_title_from_blocks,
)

# 步骤边界（行锚定）：PDF 页面常把多个步骤合并为一个视觉段落；按步骤行切开段落，
# 每个步骤块才能携带自己的 y 区间，图片注入才能按版面位置（y 序）锚定到正确步骤。
_STEP_LINE_RE = re.compile(STEP_BOUNDARY_PATTERN, re.IGNORECASE)

# ── 中文标题正则（与 docx_extractor / text_extractor 保持一致）──
_CN_HEADING_RE = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十\d]+[章节条款部分]\s*.+|"
    r"[一二三四五六七八九十]+[、\.]\s*.+|"
    r"（[一二三四五六七八九十\d]+）\s*.+"
    r")$"
)
_SUB_HEADING_RE = re.compile(r"^\d+\.\d+\s+.+$")

# ── 字号分桶精度 ──
_SIZE_BUCKET_PRECISION = 0.5  # 0.5pt 粒度


def _bucket_size(size: float) -> float:
    """将字号按 0.5pt 精度分桶。"""
    return round(size / _SIZE_BUCKET_PRECISION) * _SIZE_BUCKET_PRECISION


def _detect_heading_by_regex(text: str) -> Optional[int]:
    """用中文正则检测标题级别（与 docx_extractor 一致的 fallback）。"""
    stripped = text.strip()
    if len(stripped) > 50 or len(stripped) < 2:
        return None
    if _CN_HEADING_RE.match(stripped):
        return 1 if stripped.startswith("第") else 2
    if _SUB_HEADING_RE.match(stripped):
        return 3
    return None


# ═══════════════════════════════════════════════════════════════
# Layout-Aware 提取（主路径）
# ═══════════════════════════════════════════════════════════════

class _LayoutAnalysis:
    """Pass 1 分析结果容器。"""
    __slots__ = (
        "body_size", "heading_size_to_level",
        "header_y_max", "footer_y_min",
        "header_texts", "footer_texts",
    )

    def __init__(self):
        self.body_size: float = 0.0
        self.heading_size_to_level: Dict[float, int] = {}
        self.header_y_max: float = 0.0
        self.footer_y_min: float = float("inf")
        self.header_texts: Set[str] = set()
        self.footer_texts: Set[str] = set()


def _pass1_analyze(pdf, max_pages: int) -> Tuple[_LayoutAnalysis, List[str]]:
    """
    Pass 1: 文档级统计。

    扫描所有页的 words，构建：
      1. 字号直方图 → body_size + heading size-to-level 映射
      2. 页眉页脚检测 → header/footer y 边界 + 文本集合

    Returns:
        (_LayoutAnalysis, warnings)
    """
    analysis = _LayoutAnalysis()
    warnings: List[str] = []
    pages = pdf.pages[:max_pages]
    num_pages = len(pages)

    if num_pages == 0:
        return analysis, warnings

    # ── 字号统计（按字符加权）──
    size_char_counts: Counter = Counter()
    # ── 页眉页脚候选：(rounded_y, text) → 出现的页面集合 ──
    top_candidates: Dict[Tuple[int, str], Set[int]] = defaultdict(set)
    bottom_candidates: Dict[Tuple[int, str], Set[int]] = defaultdict(set)

    for page_idx, page in enumerate(pages):
        try:
            words = page.extract_words(
                extra_attrs=["fontname", "size"],
                x_tolerance=3, y_tolerance=3,
            )
        except Exception as e:
            warnings.append(f"Page {page_idx+1}: extract_words failed: {e}")
            continue

        if not words:
            continue

        page_height = page.height
        header_zone = page_height * 0.10   # 前 10%
        footer_zone = page_height * 0.90   # 后 10%

        for w in words:
            text = w.get("text", "").strip()
            if not text:
                continue

            # 字号统计
            size = float(w.get("size", 0))
            if size > 0:
                bucketed = _bucket_size(size)
                size_char_counts[bucketed] += len(text)

            # 页眉/页脚候选
            top_val = float(w.get("top", 0))
            if top_val < header_zone and len(text) > 1:
                rounded_y = round(top_val / 5) * 5
                top_candidates[(rounded_y, text)].add(page_idx)
            elif top_val > footer_zone and len(text) > 1:
                rounded_y = round(top_val / 5) * 5
                bottom_candidates[(rounded_y, text)].add(page_idx)

    # ── 计算 body_size ──
    if not size_char_counts:
        return analysis, warnings

    analysis.body_size = size_char_counts.most_common(1)[0][0]

    # ── 构建 heading size → level 映射 ──
    # 条件：size > body_size + 1.5 （要有显著差距）
    # 且该 size 的总字符数不超过 body 字符数的 15%（heading 文本量远少于正文）
    body_chars = size_char_counts.get(analysis.body_size, 1)
    max_heading_chars = max(body_chars * 0.15, 200)  # 至少允许 200 字符

    heading_sizes = sorted(
        [s for s in size_char_counts
         if s > analysis.body_size + 1.5
         and size_char_counts[s] <= max_heading_chars],
        reverse=True,
    )
    for i, hs in enumerate(heading_sizes[:3]):
        analysis.heading_size_to_level[hs] = i + 1  # 1, 2, 3

    # ── 页眉/页脚判定 ──
    # 在 ≥60% 的页面的同一 y 位置出现的文本 → 页眉/页脚
    min_pages_threshold = max(2, int(num_pages * 0.6))

    header_y_values: List[float] = []
    for (y, text), page_set in top_candidates.items():
        if len(page_set) >= min_pages_threshold:
            analysis.header_texts.add(text)
            header_y_values.append(y)

    footer_y_values: List[float] = []
    for (y, text), page_set in bottom_candidates.items():
        if len(page_set) >= min_pages_threshold:
            analysis.footer_texts.add(text)
            footer_y_values.append(y)

    # header_y_max: 页眉区域的下界（所有 header word 的最大 y + 余量）
    if header_y_values:
        analysis.header_y_max = max(header_y_values) + 15  # 15pt 余量
    # footer_y_min: 页脚区域的上界
    if footer_y_values:
        analysis.footer_y_min = min(footer_y_values) - 5   # 5pt 余量

    return analysis, warnings


def _pass2_extract_page(
    page,
    page_num: int,
    analysis: _LayoutAnalysis,
    current_section: List[Optional[str]],
) -> Tuple[List[ExtractedBlock], List[str]]:
    """
    Pass 2: 单页结构化提取。

    流程：
      1. 裁剪页眉/页脚
      2. 提取表格 → table blocks
      3. 排除表格区域 → 提取剩余文本
      4. 文本行分组 → heading 检测（字号 + regex fallback）
      5. 生成 blocks + section_path 追踪

    Args:
        page: pdfplumber Page 对象
        page_num: 页码（1-indexed）
        analysis: Pass 1 分析结果
        current_section: 可变引用，追踪当前 section_path

    Returns:
        (blocks, warnings)
    """
    blocks: List[ExtractedBlock] = []
    warnings: List[str] = []

    page_width = page.width
    page_height = page.height

    # ── Step 1: 裁剪页眉/页脚 ──
    crop_top = analysis.header_y_max if analysis.header_y_max > 0 else 0
    crop_bottom = analysis.footer_y_min if analysis.footer_y_min < page_height else page_height

    # 安全检查：裁剪区域必须有效
    if crop_top >= crop_bottom or (crop_bottom - crop_top) < 50:
        crop_top = 0
        crop_bottom = page_height

    try:
        cropped = page.crop((0, crop_top, page_width, crop_bottom))
    except Exception:
        cropped = page  # fallback: 不裁剪

    # ── Step 2: 表格提取 ──
    table_bboxes: List[Tuple[float, float, float, float]] = []

    try:
        tables = cropped.find_tables(table_settings={
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
        })
    except Exception:
        tables = []

    for table_idx, table in enumerate(tables):
        try:
            rows_data = table.extract()
        except Exception:
            continue

        if not rows_data:
            continue

        # 记录表格 bbox 用于后续排除
        table_bboxes.append(table.bbox)

        # 渲染为 markdown pipe format
        rows_text = []
        for row in rows_data:
            cells = [str(c).strip() if c else "" for c in row]
            if any(cells):
                rows_text.append(" | ".join(cells))

        if rows_text:
            table_md = "\n".join(f"| {row} |" for row in rows_text)
            blocks.append(ExtractedBlock(
                block_type="table",
                text=table_md,
                page_num=page_num,
                section_path=current_section[0],
                source="native",
                extra={
                    "table_index": table_idx,
                    "row_count": len(rows_text),
                    "detected_by": "pdfplumber_lines",
                    "y0": float(table.bbox[1]),
                    "y1": float(table.bbox[3]),
                },
            ))

    # ── Step 3: 排除表格区域后提取文本 ──
    try:
        words = cropped.extract_words(
            extra_attrs=["fontname", "size"],
            x_tolerance=3, y_tolerance=3,
        )
    except Exception:
        words = []

    if not words and not blocks:
        return blocks, warnings

    # 过滤掉落在表格 bbox 内的 words
    non_table_words = []
    for w in words:
        wx0, wtop, wx1, wbottom = (
            float(w["x0"]), float(w["top"]),
            float(w["x1"]), float(w["bottom"]),
        )
        in_table = False
        for (tx0, ttop, tx1, tbottom) in table_bboxes:
            # word 中心点在表格内 → 属于表格
            w_center_x = (wx0 + wx1) / 2
            w_center_y = (wtop + wbottom) / 2
            if tx0 <= w_center_x <= tx1 and ttop <= w_center_y <= tbottom:
                in_table = True
                break
        if not in_table:
            non_table_words.append(w)

    # ── Step 4: 按 y 坐标分组成文本行 ──
    if not non_table_words:
        return blocks, warnings

    lines = _group_words_into_lines(non_table_words, y_tolerance=4)

    # ── Step 5: 对每行做 heading 检测 + 生成 blocks ──
    # buffer 记录 (text, top, bottom)：段落块携带 y 区间（extra.y0/y1），
    # 供 _insert_image_refs_heuristic 按版面位置锚定图片
    text_buffer: List[Tuple[str, float, float]] = []

    def _flush_paragraph():
        nonlocal text_buffer
        if text_buffer:
            para_text = "\n".join(t for t, _, _ in text_buffer).strip()
            if para_text:
                blocks.append(ExtractedBlock(
                    block_type="paragraph",
                    text=para_text,
                    page_num=page_num,
                    section_path=current_section[0],
                    source="native",
                    extra={
                        "detected_by": "layout",
                        "y0": min(top for _, top, _ in text_buffer),
                        "y1": max(bottom for _, _, bottom in text_buffer),
                    },
                ))
            text_buffer = []

    for line_info in lines:
        line_text = line_info["text"].strip()
        if not line_text:
            if text_buffer:
                _flush_paragraph()
            continue

        # 步骤边界行：先冲掉已缓冲段落，让每个步骤独立成块（携带自己的 y 区间）。
        # 不影响全文 text（仍按行拼接），只改变块粒度 —— chunker 会按尺寸再合并。
        if text_buffer and _STEP_LINE_RE.match(line_text):
            _flush_paragraph()
        # 大纵向间隙（>40pt ≈ 嵌入图片/图表占位）也切段：否则环绕图片的文字会被
        # 合并成一个跨越整版的巨型段落（y0..y1 罩住所有图），图片按 y 锚定时
        # 全部塌到同一块上（2026-06-10 pdf_sop p3 实证：1 段 y183-693 吞 3 图）
        elif text_buffer and (line_info["top"] - text_buffer[-1][2]) > 40:
            _flush_paragraph()

        line_size = line_info["dominant_size"]
        line_fontname = line_info["dominant_fontname"]

        # ── Heading 检测 ──
        heading_level = None
        detected_by = None

        # 标注式 callout veto："⑤双击图标"常以标题字号/加粗排版，字号/加粗
        # 启发会把它当 heading → section_title 污染（章节：⑤双击图标）。
        # veto 后圈数字行成为普通段落，归入所属步骤文本。
        looks_callout = is_pseudo_heading(line_text)

        # 策略 1: 字号推断（需要文本长度 ≤50 防止长段落误判）
        if (not looks_callout and analysis.heading_size_to_level
                and line_size is not None and len(line_text) <= 50):
            # 查找匹配的 heading size（±0.5pt 容差）
            for hs, level in analysis.heading_size_to_level.items():
                if abs(line_size - hs) < 0.6:
                    heading_level = level
                    detected_by = "font_size"
                    break

        # 策略 2: Bold 字体 + 正文字号 → 可能是次级标题
        if (heading_level is None and not looks_callout
                and line_fontname and line_size is not None):
            is_bold = "bold" in line_fontname.lower() or "黑体" in line_fontname
            is_body_size = abs(line_size - analysis.body_size) < 0.6
            if is_bold and is_body_size and len(line_text) <= 40:
                heading_level = 3
                detected_by = "bold_font"

        # 策略 3: 中文正则 fallback
        if heading_level is None:
            regex_level = _detect_heading_by_regex(line_text)
            if regex_level is not None:
                heading_level = regex_level
                detected_by = "regex"

        # ── 生成 block ──
        if heading_level is not None:
            _flush_paragraph()
            current_section[0] = line_text
            blocks.append(ExtractedBlock(
                block_type="heading",
                text=line_text,
                level=heading_level,
                page_num=page_num,
                section_path=current_section[0],
                source="native",
                extra={
                    "font_size": line_size,
                    "fontname": line_fontname,
                    "detected_by": detected_by,
                    "y0": line_info["top"],
                    "y1": line_info.get("bottom", line_info["top"]),
                },
            ))
        else:
            text_buffer.append((
                line_text,
                line_info["top"],
                line_info.get("bottom", line_info["top"]),
            ))

    _flush_paragraph()
    return blocks, warnings


def _group_words_into_lines(
    words: list,
    y_tolerance: float = 4,
) -> List[dict]:
    """
    将 words 按 y 坐标分组成文本行。

    Returns:
        List of dicts: {
            "text": str,
            "top": float,
            "dominant_size": float,
            "dominant_fontname": str,
        }
    """
    if not words:
        return []

    # 按 top 排序
    sorted_words = sorted(words, key=lambda w: (float(w["top"]), float(w["x0"])))

    lines: List[dict] = []
    current_line_words = [sorted_words[0]]
    current_top = float(sorted_words[0]["top"])

    for w in sorted_words[1:]:
        w_top = float(w["top"])
        if abs(w_top - current_top) <= y_tolerance:
            current_line_words.append(w)
        else:
            lines.append(_build_line(current_line_words))
            current_line_words = [w]
            current_top = w_top

    if current_line_words:
        lines.append(_build_line(current_line_words))

    return lines


def _build_line(words: list) -> dict:
    """从一组同行 words 构建行信息。"""
    # 按 x0 排序确保正确的阅读顺序
    words_sorted = sorted(words, key=lambda w: float(w["x0"]))

    # 拼接文本（用空格连接，但中文字符间不加空格）
    parts = []
    for i, w in enumerate(words_sorted):
        text = w.get("text", "")
        if i > 0 and parts:
            prev_x1 = float(words_sorted[i-1].get("x1", 0))
            curr_x0 = float(w.get("x0", 0))
            gap = curr_x0 - prev_x1
            # 如果间距 > 15pt，插入空格（处理表头中的列间距）
            if gap > 15:
                parts.append("  ")
            elif gap > 3:
                parts.append(" ")
        parts.append(text)

    line_text = "".join(parts)

    # 确定主导字号和字体（按字符数加权）
    size_counts: Counter = Counter()
    font_counts: Counter = Counter()
    for w in words_sorted:
        text = w.get("text", "")
        size = float(w.get("size", 0))
        fontname = w.get("fontname", "")
        n = len(text)
        if size > 0:
            size_counts[_bucket_size(size)] += n
        if fontname:
            font_counts[fontname] += n

    dominant_size = size_counts.most_common(1)[0][0] if size_counts else None
    dominant_fontname = font_counts.most_common(1)[0][0] if font_counts else ""

    return {
        "text": line_text,
        "top": min(float(w["top"]) for w in words_sorted),
        "bottom": max(float(w.get("bottom", w["top"])) for w in words_sorted),
        "dominant_size": dominant_size,
        "dominant_fontname": dominant_fontname,
    }


def _extract_with_pdfplumber(
    local_path: str, max_pages: int
) -> Tuple[List[ExtractedBlock], int, List[str]]:
    """
    使用 pdfplumber Layout-Aware 提取 PDF 文本。

    Two-Pass 架构：
      Pass 1: 文档级统计（字号 + 页眉页脚）
      Pass 2: 逐页结构化提取（表格 + heading + section_path）
    """
    import pdfplumber

    warnings = []

    try:
        pdf = pdfplumber.open(local_path)
    except Exception as e:
        return [], 0, [f"pdfplumber failed to open PDF: {e}"]

    page_count = len(pdf.pages)

    if page_count == 0:
        pdf.close()
        return [], 0, []

    # ── Pass 1: 文档级分析 ──
    analysis, p1_warnings = _pass1_analyze(pdf, max_pages)
    warnings.extend(p1_warnings)

    # 如果字号数据不足，说明可能是扫描件或无法提取
    if analysis.body_size == 0:
        pdf.close()
        return [], page_count, warnings + ["Pass 1: no font size data (scanned PDF?)"]

    # ── Pass 2: 逐页提取 ──
    all_blocks: List[ExtractedBlock] = []
    current_section: List[Optional[str]] = [None]  # 可变引用

    for page_idx, page in enumerate(pdf.pages[:max_pages]):
        page_num = page_idx + 1
        try:
            page_blocks, p2_warnings = _pass2_extract_page(
                page, page_num, analysis, current_section,
            )
            all_blocks.extend(page_blocks)
            warnings.extend(p2_warnings)
        except Exception as e:
            warnings.append(f"Page {page_num}: layout extraction failed: {e}")
            # Per-page fallback: 用 flat text
            try:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    fallback_blocks = extract_text_file(page_text, source="native")
                    for b in fallback_blocks:
                        b.page_num = page_num
                    all_blocks.extend(fallback_blocks)
            except Exception:
                pass

    if page_count > max_pages:
        warnings.append(
            f"PDF has {page_count} pages, only first {max_pages} extracted"
        )

    # 记录 layout analysis 结果到 warnings（供调试）
    if analysis.heading_size_to_level:
        size_map = ", ".join(
            f"{s}pt→H{l}" for s, l in sorted(analysis.heading_size_to_level.items())
        )
        warnings.append(f"Layout: body={analysis.body_size}pt, headings=[{size_map}]")
    if analysis.header_texts:
        warnings.append(
            f"Header/footer filtered: {len(analysis.header_texts)} header texts, "
            f"header_y_max={analysis.header_y_max:.0f}, footer_y_min={analysis.footer_y_min:.0f}"
        )

    pdf.close()

    total_chars = sum(len(b.text) for b in all_blocks)
    if total_chars > 0:
        print(f"      [pdf] pdfplumber layout-aware extracted {total_chars} chars, "
              f"{len(all_blocks)} blocks from {min(page_count, max_pages)} pages")

    return all_blocks, page_count, warnings


def _extract_with_pypdf(
    local_path: str, max_pages: int
) -> Tuple[List[ExtractedBlock], int, List[str]]:
    """使用 pypdf/PyPDF2 提取 PDF 文本（fallback，无 layout 能力）。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return [], 0, ["pypdf/PyPDF2 not installed"]

    warnings = []
    all_blocks = []

    try:
        reader = PdfReader(local_path)
    except Exception as e:
        return [], 0, [f"Failed to open PDF: {e}"]

    page_count = len(reader.pages)

    for page_idx, page in enumerate(reader.pages[:max_pages]):
        page_num = page_idx + 1
        try:
            page_text = page.extract_text() or ""
        except Exception as e:
            warnings.append(f"Page {page_num}: extract_text failed: {e}")
            page_text = ""

        if not page_text.strip():
            continue

        page_blocks = extract_text_file(page_text, source="native")
        for block in page_blocks:
            block.page_num = page_num
        all_blocks.extend(page_blocks)

    if page_count > max_pages:
        warnings.append(
            f"PDF has {page_count} pages, only first {max_pages} extracted"
        )

    return all_blocks, page_count, warnings


def extract_pdf(
    local_path: str,
    max_pages: int = 20,
) -> Tuple[List[ExtractedBlock], int, List[str]]:
    """
    从 PDF 文件提取 blocks（带 page_num）。

    优先用 pdfplumber layout-aware，失败或 0 chars 则用 pypdf flat fallback。

    Returns:
        (blocks, page_count, warnings)
    """
    all_warnings = []

    # 策略 1: pdfplumber layout-aware
    try:
        blocks, page_count, warnings = _extract_with_pdfplumber(local_path, max_pages)
        total_chars = sum(len(b.text) for b in blocks)
        if total_chars > 0:
            return blocks, page_count, warnings
        all_warnings.extend(warnings)
        all_warnings.append("pdfplumber layout-aware returned 0 chars, trying pypdf")
    except ImportError:
        all_warnings.append("pdfplumber not installed, trying pypdf")
    except Exception as e:
        all_warnings.append(f"pdfplumber error: {e}, trying pypdf")

    # 策略 2: pypdf / PyPDF2 (flat text fallback)
    try:
        blocks, page_count, warnings = _extract_with_pypdf(local_path, max_pages)
        total_chars = sum(len(b.text) for b in blocks)
        if total_chars > 0:
            print(f"      [pdf] pypdf extracted {total_chars} chars from {page_count} pages")
        else:
            print(f"      [pdf] pypdf also returned 0 chars (scanned PDF?)")
        all_warnings.extend(warnings)
        return blocks, page_count, all_warnings
    except Exception as e:
        all_warnings.append(f"pypdf error: {e}")

    return [], 0, all_warnings


def get_pdf_text_length(blocks: List[ExtractedBlock]) -> int:
    """计算 PDF 提取的总文本长度。"""
    return sum(len(b.text) for b in blocks)
