# -*- coding: utf-8 -*-
"""
pdf_extractor.py — PDF 文本提取器

提取策略（按优先级）：
  1. pdfplumber（Python 3.7 兼容，提取效果好）
  2. pypdf / PyPDF2（fallback）

如果两者都提取 0 chars，则返回空 blocks，由 OCR fallback 接管。

生产依赖：pdfplumber 或 pypdf
模拟模式：不需要真实 PDF 文件
"""

import re
from typing import List, Optional, Tuple

from opensearch_pipeline.extraction.schema import ExtractedBlock
from opensearch_pipeline.extraction.text_extractor import (
    extract_text_file,
    extract_title_from_blocks,
)


def _extract_with_pdfplumber(
    local_path: str, max_pages: int
) -> Tuple[List[ExtractedBlock], int, List[str]]:
    """使用 pdfplumber 提取 PDF 文本。"""
    import pdfplumber

    warnings = []
    all_blocks = []

    try:
        pdf = pdfplumber.open(local_path)
    except Exception as e:
        return [], 0, [f"pdfplumber failed to open PDF: {e}"]

    page_count = len(pdf.pages)

    for page_idx, page in enumerate(pdf.pages[:max_pages]):
        page_num = page_idx + 1
        try:
            page_text = page.extract_text() or ""
        except Exception as e:
            warnings.append(f"Page {page_num}: pdfplumber extract failed: {e}")
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

    pdf.close()
    return all_blocks, page_count, warnings


def _extract_with_pypdf(
    local_path: str, max_pages: int
) -> Tuple[List[ExtractedBlock], int, List[str]]:
    """使用 pypdf/PyPDF2 提取 PDF 文本。"""
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

    优先用 pdfplumber，失败或 0 chars 则用 pypdf。

    Returns:
        (blocks, page_count, warnings)
    """
    all_warnings = []

    # 策略 1: pdfplumber
    try:
        blocks, page_count, warnings = _extract_with_pdfplumber(local_path, max_pages)
        total_chars = sum(len(b.text) for b in blocks)
        if total_chars > 0:
            print(f"      [pdf] pdfplumber extracted {total_chars} chars from {page_count} pages")
            return blocks, page_count, warnings
        all_warnings.extend(warnings)
        all_warnings.append(f"pdfplumber returned 0 chars, trying pypdf")
    except ImportError:
        all_warnings.append("pdfplumber not installed, trying pypdf")
    except Exception as e:
        all_warnings.append(f"pdfplumber error: {e}, trying pypdf")

    # 策略 2: pypdf / PyPDF2
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
