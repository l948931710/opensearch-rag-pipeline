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

            # G12：非正立词（旋转/斜置——典型为水印、骑缝章文字）不参与字号统计
            # 与页眉页脚候选：其字号会污染 body_size 直方图，且水印不该被当成页眉。
            if not w.get("upright", True):
                continue

            # 字号统计
            size = float(w.get("size", 0))
            if size > 0:
                bucketed = _bucket_size(size)
                size_char_counts[bucketed] += len(text)

            # 页眉/页脚候选。G12：候选键做数字归一——"第3页/第4页"这类逐页变化的
            # 页码文本，精确文本键下每页都是新候选、永远达不到 60% 频次阈值，页脚
            # 裁剪对其失效；归一成模式（"第#页"）后按模式聚合，y 照常参与裁剪边界。
            top_val = float(w.get("top", 0))
            if top_val < header_zone and len(text) > 1:
                rounded_y = round(top_val / 5) * 5
                top_candidates[(rounded_y, re.sub(r"\d+", "#", text))].add(page_idx)
            elif top_val > footer_zone and len(text) > 1:
                rounded_y = round(top_val / 5) * 5
                bottom_candidates[(rounded_y, re.sub(r"\d+", "#", text))].add(page_idx)

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

    # 无框/少框表格：lines 策略检不出 → 这些表格此前降级成普通段落（丢失行列结构）。仅当 lines
    # 完全无结果时，用 text 策略（按文字对齐推断行列）再试一次——只在 lines 为空时启用，绝不改动既有
    # 有框表格的行为；text 策略更易误判，故加 2×2 + 单元格填充率(≥0.3) 合理性闸，过闸才接受。fail-open。
    _detect_label = "pdfplumber_lines"
    if not tables:
        # G3 前置守卫：双栏正文页的左右栏词网格会被 text 策略误判成"表格"
        # （整页并成一张假表、两栏内容按行交错进单元格）。检出列分隔即跳过
        # text 策略，让正文走 Step 3/4 的分栏合行路径。
        _col_guard = None
        try:
            _fw = [w for w in cropped.extract_words(x_tolerance=3, y_tolerance=3)
                   if w.get("upright", True)]
            _col_guard = _detect_column_split(_fw, page_width)
        except Exception:
            _col_guard = None
        if _col_guard is not None:
            _cand = []
        else:
            try:
                _cand = cropped.find_tables(table_settings={
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                    "snap_tolerance": 4,
                })
            except Exception:
                _cand = []
        _accepted = []
        for _t in _cand:
            try:
                _rd = _t.extract()
            except Exception:
                continue
            if not _rd or len(_rd) < 2:
                continue
            if max((len(r) for r in _rd), default=0) < 2:
                continue
            _cells = sum(len(r) for r in _rd) or 1
            _filled = sum(1 for r in _rd for c in r if c and str(c).strip())
            if _filled / _cells < 0.3:
                continue
            _accepted.append(_t)
        if _accepted:
            tables = _accepted
            _detect_label = "pdfplumber_text"

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
                    "detected_by": _detect_label,
                    "y0": float(table.bbox[1]),
                    "y1": float(table.bbox[3]),
                    # G11：跨页表格拼接需要 x-span 做同表判定（additive，勿删）
                    "x0": float(table.bbox[0]),
                    "x1": float(table.bbox[2]),
                    "page_height": float(page_height),
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

    # G12：丢弃非正立词——斜置水印（"内部资料""绝密"对角线盖章文字）此前混入正文行分组，
    # 在行内按 x 排序时把水印字符插进正常句子中间。竖排正文在本语料（横排中文办公文档）中
    # 不存在，误伤面可忽略；如遇竖排文档该行为可再按页级竖排占比放行。
    words = [w for w in words if w.get("upright", True)]

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

    # G3：双栏页先分栏再合行（先左栏后右栏），否则同 y 的左右栏文字会被逐词交错。
    _split_x = _detect_column_split(non_table_words, page_width)
    if _split_x is not None:
        _left = [w for w in non_table_words
                 if (float(w["x0"]) + float(w["x1"])) / 2 < _split_x]
        _right = [w for w in non_table_words
                  if (float(w["x0"]) + float(w["x1"])) / 2 >= _split_x]
        lines = (_group_words_into_lines(_left, y_tolerance=4)
                 + _group_words_into_lines(_right, y_tolerance=4))
        warnings.append(f"[MULTI_COLUMN] page {page_num}: two-column layout split at x={_split_x:.0f}")
    else:
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

        # 页面叠加圈号标注（独立"⑧"文本元素，排版上贴在所属图片 bbox 内）：
        # 独立成块并携带 x/y 几何，供 _insert_image_refs_heuristic 做
        # 标注→图片的包含判定（图号引用绑定的第 0 优先级证据）。
        # 该文档版式常见"图在引用文字之前"，仅靠阅读序/y 锚定会把图吞进前一步骤
        # （2026-06-11 FL-ZS-WI-005 枪图实证）。
        if re.fullmatch(r'[①-⑳]', line_text):
            _flush_paragraph()
            blocks.append(ExtractedBlock(
                block_type="paragraph",
                text=line_text,
                page_num=page_num,
                section_path=current_section[0],
                source="native",
                extra={
                    "detected_by": "circled_label",
                    "circled_label": line_text,
                    "x0": line_info.get("x0"),
                    "x1": line_info.get("x1"),
                    "y0": line_info["top"],
                    "y1": line_info.get("bottom", line_info["top"]),
                },
            ))
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


def _detect_column_split(words: list, page_width: float) -> Optional[float]:
    """G3：双栏页 x-gap 列检测。返回分栏线 x 坐标，单栏页返回 None。

    此前全页按 (top, x0) 全局排序合行——双栏页左右两栏同一 y 的文字被逐词交错，
    正文语序完全破坏后静默入索引。判定（全部满足，保守方向，误判成本高）：
      1. 页面中部 30%-70% 区间存在宽 ≥20pt 的纵向空隙，无任何词横跨；
      2. 空隙两侧各承载 ≥25% 的词量（排除"窄栏批注/行首缩进"形态）；
      3. 两侧文字的纵向范围重叠 ≥60%（真正并排，而非上下两段错位）。
    检出后先左栏后右栏分别合行。三栏及以上不处理（本语料不存在）。
    """
    if len(words) < 30 or page_width <= 0:
        return None
    spans = [(float(w["x0"]), float(w["x1"]), float(w["top"]), float(w["bottom"]))
             for w in words]
    step = 2.0
    lo, hi = page_width * 0.30, page_width * 0.70
    # 逐候选位置检测"无词横跨"，聚合成连续空隙区间
    gap_runs: List[Tuple[float, float]] = []
    run_start = None
    x = lo
    while x <= hi:
        straddled = any(s0 < x < s1 for s0, s1, _, _ in spans)
        if not straddled:
            if run_start is None:
                run_start = x
        elif run_start is not None:
            gap_runs.append((run_start, x - step))
            run_start = None
        x += step
    if run_start is not None:
        gap_runs.append((run_start, hi))

    for g0, g1 in sorted(gap_runs, key=lambda r: r[1] - r[0], reverse=True):
        if g1 - g0 < 20:
            continue
        split = (g0 + g1) / 2
        left = [s for s in spans if (s[0] + s[1]) / 2 < split]
        right = [s for s in spans if (s[0] + s[1]) / 2 >= split]
        n = len(spans)
        if len(left) / n < 0.25 or len(right) / n < 0.25:
            continue
        l_top, l_bot = min(s[2] for s in left), max(s[3] for s in left)
        r_top, r_bot = min(s[2] for s in right), max(s[3] for s in right)
        overlap = min(l_bot, r_bot) - max(l_top, r_top)
        min_extent = min(l_bot - l_top, r_bot - r_top)
        if min_extent <= 0 or overlap / min_extent < 0.6:
            continue
        return split
    return None


def _md_table_cols(table_md: str) -> int:
    """markdown pipe 表首行列数（"| a |  | c |" → 3）。"""
    first = table_md.split("\n", 1)[0]
    parts = first.split("|")
    return max(0, len(parts) - 2)


def _norm_row(row: str) -> str:
    return re.sub(r"\s+", "", row)


def _stitch_cross_page_tables(blocks: List[ExtractedBlock]) -> Tuple[List[ExtractedBlock], int]:
    """G11：跨页表格规则拼接。

    此前严格逐页出表，续表丢表头且行列结构断裂（检索侧 ±1 邻块拼接不恢复结构）。
    拼接条件（全部满足才并，保守方向；Azure DI 公开的启发式同款思路）：
      1. 页 N 的最后一张表是"页尾表"：其下方无正文块（y0 > 表 y1+5pt 视为下方）；
      2. 页 N+1 的第一张表是"页首表"：其上方无正文块；
      3. 两表列数相等；
      4. x-span 重叠 ≥80%（同一张表在版面上水平对齐）。
    并表时若续表首行与主表首行文本一致（重复打印的表头）则剥离。链式处理支持
    3 页以上长表。任何页缺表/条件不满足 → 完全不动（byte-equal 回归安全）。
    """
    stitched = 0
    # 按页聚块（blocks 页间有序；页内表先文后，用 y 判位置）
    pages = sorted({b.page_num for b in blocks if b.page_num})
    by_page: Dict[int, List[ExtractedBlock]] = {p: [] for p in pages}
    for b in blocks:
        if b.page_num:
            by_page[b.page_num].append(b)

    def _tables(p):
        return [b for b in by_page.get(p, []) if b.block_type == "table"
                and isinstance(b.extra, dict) and "x0" in b.extra]

    def _texts(p):
        return [b for b in by_page.get(p, [])
                if b.block_type in ("paragraph", "heading", "list")]

    to_drop: set = set()
    for p in pages:
        if p + 1 not in by_page:
            continue
        tails = _tables(p)
        heads = _tables(p + 1)
        if not tails or not heads:
            continue
        tail = max(tails, key=lambda b: b.extra.get("y1", 0))
        head = min(heads, key=lambda b: b.extra.get("y0", float("inf")))
        if id(tail) in to_drop or id(head) in to_drop:
            continue
        # 条件 1/2：尾表下方、首表上方不得有正文
        if any(t.extra and t.extra.get("y0", 0) > tail.extra["y1"] + 5 for t in _texts(p)):
            continue
        if any(t.extra and t.extra.get("y1", 0) < head.extra["y0"] - 5 for t in _texts(p + 1)):
            continue
        # 条件 3：列数相等
        cols_a, cols_b = _md_table_cols(tail.text), _md_table_cols(head.text)
        if cols_a < 2 or cols_a != cols_b:
            continue
        # 条件 4：x-span 重叠 ≥80%（以较窄表为基）
        ax0, ax1 = tail.extra["x0"], tail.extra["x1"]
        bx0, bx1 = head.extra["x0"], head.extra["x1"]
        overlap = min(ax1, bx1) - max(ax0, bx0)
        narrower = min(ax1 - ax0, bx1 - bx0)
        if narrower <= 0 or overlap / narrower < 0.8:
            continue
        # 并表：剥离续表重复表头
        b_rows = head.text.split("\n")
        if b_rows and _norm_row(b_rows[0]) == _norm_row(tail.text.split("\n", 1)[0]):
            b_rows = b_rows[1:]
        if not b_rows:
            to_drop.add(id(head))
            continue
        tail.text = tail.text + "\n" + "\n".join(b_rows)
        tail.extra["row_count"] = tail.text.count("\n") + 1
        tail.extra.setdefault("stitched_pages", [p]).append(p + 1)
        # 链式：让 p+2 的首表能继续并进来——把并入后的 tail 顶替 head 在 p+1 的位置
        by_page[p + 1] = [tail if b is head else b for b in by_page[p + 1]]
        tail.extra["y1"] = head.extra.get("y1", tail.extra["y1"])
        to_drop.add(id(head))
        stitched += 1

    if not stitched:
        return blocks, 0
    return [b for b in blocks if id(b) not in to_drop], stitched


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
        "x0": min(float(w["x0"]) for w in words_sorted),
        "x1": max(float(w.get("x1", w["x0"])) for w in words_sorted),
        "dominant_size": dominant_size,
        "dominant_fontname": dominant_fontname,
    }


def _extract_with_pdfplumber(
    local_path: str, max_pages: int, meta_out: Optional[dict] = None
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
    # G12-OCR：把 Pass-1 检出的页眉/页脚词集回传调用方（meta_out 可选出参，kwargs 安全）。
    # OCR 兜底文本无坐标、y 裁剪不适用——词集是唯一可迁移的页眉知识
    # （词级 + 数字归一键，见 top_candidates 收集处）。
    if meta_out is not None:
        meta_out["header_texts"] = set(analysis.header_texts)
        meta_out["footer_texts"] = set(analysis.footer_texts)

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

    # ── G11：跨页表格拼接（列数相等 + x-span 对齐 + 页尾/页首位置证据）──
    try:
        all_blocks, _n_stitched = _stitch_cross_page_tables(all_blocks)
        if _n_stitched:
            warnings.append(f"[TABLE_STITCH] merged {_n_stitched} cross-page table continuation(s)")
    except Exception as _st_err:  # fail-open：拼接失败绝不影响既有抽取
        warnings.append(f"cross-page table stitch skipped: {_st_err}")

    # 记录 layout analysis 结果到 warnings（供调试）
    if analysis.heading_size_to_level:
        size_map = ", ".join(
            f"{s}pt→H{lvl}" for s, lvl in sorted(analysis.heading_size_to_level.items())
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
    meta_out: Optional[dict] = None,
) -> Tuple[List[ExtractedBlock], int, List[str]]:
    """
    从 PDF 文件提取 blocks（带 page_num）。

    优先用 pdfplumber layout-aware，失败或 0 chars 则用 pypdf flat fallback。
    meta_out（可选出参）：pdfplumber 路径回填 Pass-1 页眉/页脚词集
    （header_texts/footer_texts，G12-OCR 供 OCR 兜底文本裁剪）；pypdf 路径不填。

    Returns:
        (blocks, page_count, warnings)
    """
    all_warnings = []

    # 策略 1: pdfplumber layout-aware
    try:
        blocks, page_count, warnings = _extract_with_pdfplumber(
            local_path, max_pages, meta_out=meta_out)
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
            print("      [pdf] pypdf also returned 0 chars (scanned PDF?)")
        all_warnings.extend(warnings)
        return blocks, page_count, all_warnings
    except Exception as e:
        all_warnings.append(f"pypdf error: {e}")

    return [], 0, all_warnings


def get_pdf_text_length(blocks: List[ExtractedBlock]) -> int:
    """计算 PDF 提取的总文本长度。"""
    return sum(len(b.text) for b in blocks)
