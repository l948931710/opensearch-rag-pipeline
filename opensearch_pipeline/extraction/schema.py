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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 圈数字开头的标注式 callout 行（"⑤双击图标"）。覆盖 ①-⑳(2460-2473)、
# ⓪⓫-⓾(24EA-24FF)、❶-❿(2776-277F)；annotation_parser.CIRCLED_DIGITS(①-⑩)
# 是其子集（用于标注映射）。允许行首空白/常见列表符。
_PSEUDO_HEADING_PREFIX_RE = re.compile(
    r'^[\s　>·•\-–—]*[①-⑳⓪-⓿❶-❿]'
)


def is_pseudo_heading(text: str) -> bool:
    """标注式 callout 行被 PDF 字号/加粗启发误判为 heading 时，会把
    section_title 污染成 "⑤双击图标"（回答里显示 章节：⑤双击图标）。
    只 veto 圈数字开头的行 —— 编号节标题（"4.1 检查模具"）是合法 heading、
    驱动切块边界，绝不在此列（known-open 修复 2026-06-10）。"""
    if not text:
        return False
    return bool(_PSEUDO_HEADING_PREFIX_RE.match(text))


# SOP 步骤边界模式（字符串，单一来源）：chunker._STEP_BOUNDARY_RE 与 PDF 提取的
# 段落切分（pdf_extractor）共用，保证"提取按步骤切块"与"chunker 按步骤切卡"边界一致。
STEP_BOUNDARY_PATTERN = (
    r'^[ \t　]*(?:'                                  # 容忍行首缩进（空格/制表/全角空格）
    r'步骤\s*([一二三四五六七八九十\d]+)|'             # 步骤1 / 步骤三 / 步骤 2
    r'Step\s*(\d+)|'                                  # Step 1 / Step2
    r'第\s*([一二三四五六七八九十\d]+)\s*步|'          # 第一步 / 第1步
    r'(\d+)\s*[\.．、]\s*(?![\d])|'                   # 1. / 1．/ 2、（排除 1.1 条款编号）
    r'(\d+)\s*[)）]\s*|'                              # 1) / 2）
    r'(\d+\.\d+)\s+\S|'                               # 1.2 / 3.2 条款编号步骤（后跟空格+文字）
    r'(\d+\.\d+(?:\.\d+)+)\s*(?=[一-鿿A-Za-z0-9])'    # 4.2.1 多级子步骤
    r')'
)


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

    # PDF 原生抽取页上限截断（P1-09）：page_count（真实总页数）> PDF_NATIVE_MAX_PAGES 时，
    # 超限页既无原生文本也不进 OCR 判定 → 静默丢知识。此标志让下游/治理看板可见，不再把
    # 「仅前 N 页上线」当作完整「已上线」。extracted_pages = 实际被原生覆盖的页数（截断时=上限）。
    extract_truncated: bool = False
    extracted_pages: Optional[int] = None

    # OCR
    ocr_required: bool = False
    ocr_status: str = "NOT_REQUIRED"        # NOT_REQUIRED / DONE / SIMULATED / FAILED

    # 附件和警告
    warnings: List[str] = field(default_factory=list)
    assets: List[Dict[str, Any]] = field(default_factory=list)

    # 成本封存：VLM-rebuild 成本闸拒绝本文档 → 下游应跳过切块/索引 (避免裂脑状态)
    cost_quarantined: bool = False

    # 成本顺延（ultra P1 纠偏 2026-07-17）：瞬态共享预算（RUN/DAILY）耗尽被拒 → 本 run 不定稿：
    # node_build_canonical 扣住 canonical（不写 keys），下一 run/次日预算滚动后 stage-1 按既有
    # 谓词（NOT_STARTED 且 keys IS NULL）重捡、重新过闸。与 cost_quarantined（doc-intrinsic，
    # 终态封存）互斥——瞬态拒绝若烙 quarantined，canonical 定稿后文档在 stage-2 落 EMPTY/DONE
    # 静默终态，「可重认领」名存实亡。
    cost_deferred: bool = False

    # XLSX 版面分类结果：DAG1 用真实 filename 分类一次后持久化到 canonical，供 DAG2 直接消费，
    # 避免 DAG2 用（重载后丢失的）空 filename 重新分类 → layout 漂移 → step_card 结构静默丢失 (P0-3)。
    xlsx_layout_type: Optional[str] = None

    # P2-32（VLM degraded 传播）：本文档有多少张图片拿到的是 VLM 降级兜底结论
    # （超时/解析失败 → 占位 caption 或保守隔离，见 image_funnel_processor ~430）。>0 时
    # stage-2 收尾把 document_version.content_process_status 落 NEEDS_REVIEW 而非 DONE——
    # 文档留在可复查集合，VLM 恢复后重灌自愈；文本抽取/切块/索引照常（graceful degradation）。
    vlm_degraded_count: int = 0

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
            "extract_truncated": self.extract_truncated,
            "extracted_pages": self.extracted_pages,
            "ocr_required": self.ocr_required,
            "ocr_status": self.ocr_status,
            "warnings": self.warnings,
            "assets": self.assets,
            "cost_quarantined": self.cost_quarantined,
            "cost_deferred": self.cost_deferred,
            "xlsx_layout_type": self.xlsx_layout_type,
            "vlm_degraded_count": self.vlm_degraded_count,
        }
