# -*- coding: utf-8 -*-
"""
text_extractor.py — txt / md / csv / html 文本提取器

将纯文本/markdown 解析为结构化 blocks：
  - Markdown 标题 → heading block
  - Pipe 表格 → table block
  - 列表项 → list block (保持段落合并)
  - 其余 → paragraph block
"""

import re
from typing import List, Optional

from opensearch_pipeline.extraction.schema import ExtractedBlock


# Markdown 标题匹配
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)$")

# 中文标题匹配：第X章 / 一、 / （一）/ 1. 2.
_CN_HEADING_RE = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十\d]+[章节条款部分]\s*.+|"
    r"[一二三四五六七八九十]+[、\.]\s*.+|"
    r"（[一二三四五六七八九十\d]+）\s*.+"
    r")$"
)

# 次级序号标题：3.1 xxx / 3.2 xxx
_SUB_HEADING_RE = re.compile(r"^\d+\.\d+\s+.+$")


def extract_text_file(
    text: str,
    source: str = "native",
) -> List[ExtractedBlock]:
    """
    将文本/markdown 内容解析为 blocks。

    Args:
        text: 文件内容
        source: 来源标记 ("native" / "mock")

    Returns:
        List[ExtractedBlock]
    """
    lines = text.split("\n")
    blocks: List[ExtractedBlock] = []
    current_section: Optional[str] = None
    buffer: List[str] = []
    table_buffer: List[str] = []

    def _flush_text():
        nonlocal buffer
        if buffer:
            joined = "\n".join(buffer).strip()
            if joined:
                blocks.append(ExtractedBlock(
                    block_type="paragraph",
                    text=joined,
                    section_path=current_section,
                    source=source,
                ))
            buffer = []

    def _flush_table():
        nonlocal table_buffer
        if table_buffer:
            joined = "\n".join(table_buffer).strip()
            if joined:
                blocks.append(ExtractedBlock(
                    block_type="table",
                    text=joined,
                    section_path=current_section,
                    source=source,
                ))
            table_buffer = []

    for line in lines:
        stripped = line.strip()

        # ── 空行：flush 当前 buffer ──
        if not stripped:
            if table_buffer:
                _flush_table()
            elif buffer:
                _flush_text()
            continue

        # ── 表格行：至少 2 个 pipe ──
        if stripped.count("|") >= 2:
            if buffer:
                _flush_text()
            # 跳过 markdown 分隔行 (| --- | --- |)
            if re.match(r"^\|[\s\-:]+\|", stripped):
                table_buffer.append(stripped)
                continue
            table_buffer.append(stripped)
            continue
        else:
            if table_buffer:
                _flush_table()

        # ── Markdown 标题 ──
        md_match = _HEADING_RE.match(stripped)
        if md_match:
            _flush_text()
            level = len(md_match.group(1))
            title = md_match.group(2).strip()
            current_section = title
            blocks.append(ExtractedBlock(
                block_type="heading",
                text=title,
                level=level,
                section_path=current_section,
                source=source,
            ))
            continue

        # ── 中文标题 ──
        if _CN_HEADING_RE.match(stripped):
            _flush_text()
            # 推断 level：第X章=1, 一、=2, （一）=2
            if stripped.startswith("第"):
                level = 1
            else:
                level = 2
            current_section = stripped
            blocks.append(ExtractedBlock(
                block_type="heading",
                text=stripped,
                level=level,
                section_path=current_section,
                source=source,
            ))
            continue

        # ── 次级标题 3.1 / 3.2 ──
        if _SUB_HEADING_RE.match(stripped):
            _flush_text()
            parent = current_section or ""
            current_section = f"{parent} > {stripped}" if parent else stripped
            blocks.append(ExtractedBlock(
                block_type="heading",
                text=stripped,
                level=3,
                section_path=current_section,
                source=source,
            ))
            continue

        # ── 普通文本 ──
        buffer.append(stripped)

    # flush 剩余
    _flush_table()
    _flush_text()

    return blocks


def blocks_to_text(blocks: List[ExtractedBlock]) -> str:
    """将 blocks 拼接为 flat text（向后兼容）。"""
    parts = []
    for block in blocks:
        if block.block_type == "heading":
            prefix = "#" * block.level + " " if block.level > 0 else ""
            parts.append(f"{prefix}{block.text}")
        else:
            parts.append(block.text)
    return "\n\n".join(parts)


def extract_title_from_blocks(blocks: List[ExtractedBlock], fallback: str = "") -> str:
    """从 blocks 中提取标题（第一个 heading）。"""
    for block in blocks:
        if block.block_type == "heading" and block.text.strip():
            return block.text.strip()
    return fallback
