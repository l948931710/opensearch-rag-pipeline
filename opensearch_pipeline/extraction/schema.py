# -*- coding: utf-8 -*-
"""
schema.py — Unified Extraction Layer 输出类型

ExtractionResult: 文档级输出
ExtractedBlock:   块级输出（heading / paragraph / table / list / ocr_text）

设计原则：
  - text 字段保持向后兼容（分类、敏感检测仍然用 flat text）
  - blocks 字段为 chunker 提供结构化元信息（page_num, section_path, block_type）
  - 支持 mock 模式（mock_text → 自动解析出 blocks）
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExtractedBlock:
    """
    文档中的一个语义块。

    block_type 取值：
      heading    — 标题（level 1-4）
      paragraph  — 正文段落
      table      — 表格（markdown pipe format）
      list       — 列表项
      ocr_text   — OCR 识别出的文本
    """
    block_type: str
    text: str
    level: int = 0                          # heading level: 1-4; 0 for non-headings
    page_num: Optional[int] = None
    section_path: Optional[str] = None      # e.g. "三、审核流程 > 3.2 核对发票信息"
    source: str = "native"                  # "native" | "ocr" | "mock"
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "block_type": self.block_type,
            "text": self.text,
            "level": self.level,
            "page_num": self.page_num,
            "section_path": self.section_path,
            "source": self.source,
            "extra": self.extra,
        }


@dataclass
class ExtractionResult:
    """
    统一提取结果。

    text:   拼接后的全文（向后兼容）
    blocks: 结构化块列表（新能力，支持 page_num / section_path）
    """
    doc_id: str
    version_no: int
    source_key: str                         # raw OSS key
    file_ext: str                           # "pdf", "docx", "txt", etc.
    extract_method: str                     # "pypdf", "python_docx", "markdown", "ocr_fallback"
    title: str                              # extracted from first heading or filename

    # 向后兼容：flat text
    text: str
    text_length: int

    # 结构化输出
    blocks: List[ExtractedBlock] = field(default_factory=list)
    page_count: Optional[int] = None

    # OCR
    ocr_required: bool = False
    ocr_status: str = "NOT_REQUIRED"        # NOT_REQUIRED / DONE / SIMULATED / FAILED

    # 附件和警告
    warnings: List[str] = field(default_factory=list)
    assets: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "version_no": self.version_no,
            "source_key": self.source_key,
            "file_ext": self.file_ext,
            "extract_method": self.extract_method,
            "title": self.title,
            "text": self.text,
            "text_length": self.text_length,
            "blocks": [b.to_dict() for b in self.blocks],
            "page_count": self.page_count,
            "ocr_required": self.ocr_required,
            "ocr_status": self.ocr_status,
            "warnings": self.warnings,
            "assets": self.assets,
        }
