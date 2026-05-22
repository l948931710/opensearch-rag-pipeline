# -*- coding: utf-8 -*-
"""
unified_extractor.py — 统一文档提取入口

单一入口 UnifiedExtractor.extract()，根据 file_ext 分发到对应的提取器。
支持 mock 模式（接收 mock_text）和生产模式（读取真实文件）。

DAG 层不需要知道文件类型，只调用 extract() 即可。
"""

import os
from typing import Optional

from opensearch_pipeline.extraction.schema import ExtractionResult, ExtractedBlock
from opensearch_pipeline.extraction.text_extractor import (
    extract_text_file,
    blocks_to_text,
    extract_title_from_blocks,
)
from opensearch_pipeline.extraction.ocr_client import OCRClient


class UnifiedExtractor:
    """
    统一文档提取器。

    用法：
        extractor = UnifiedExtractor(simulate=True)
        result = extractor.extract(task)

    task dict 需包含：
        doc_id, version_no, file_ext, raw_key
        mock_text (可选，模拟模式)
        local_path (可选，生产模式)
    """

    # 原生提取文本低于此阈值 → OCR fallback
    OCR_THRESHOLD_CHARS = 100

    def __init__(
        self,
        oss_client=None,
        ocr_client: Optional[OCRClient] = None,
        simulate: bool = True,
    ):
        self.oss_client = oss_client
        if not ocr_client:
            from opensearch_pipeline.config import get_config
            cfg = get_config()
            self.ocr_client = OCRClient(
                api_key=cfg.ocr.api_key,
                api_base_url=cfg.ocr.api_base_url,
                ocr_model=cfg.ocr.model,
                max_ocr_pages=cfg.ocr.max_ocr_pages,
                simulate=simulate,
            )
        else:
            self.ocr_client = ocr_client
        self.simulate = simulate

    def extract(self, task: dict) -> ExtractionResult:
        """
        统一提取入口。

        根据 file_ext 分发，自动处理 OCR fallback。
        """
        file_ext = task.get("file_ext", "txt").lower().strip().lstrip(".")
        doc_id = task["doc_id"]
        version_no = task["version_no"]
        source_key = task.get("raw_key", "")

        # ── Mock 模式：直接解析注入的文本 ──
        if "mock_text" in task:
            return self._extract_mock(task, file_ext)

        # ── 生产模式：按文件类型分发 ──
        if file_ext == "pdf":
            return self._extract_pdf(task)
        elif file_ext == "docx":
            return self._extract_docx(task)
        elif file_ext in ("xlsx", "xls"):
            return self._extract_xlsx(task)
        elif file_ext in ("txt", "md", "csv", "html"):
            return self._extract_text(task)
        elif file_ext in ("png", "jpg", "jpeg", "webp"):
            return self._extract_image(task)
        else:
            return self._unsupported(task, file_ext)

    # ── Mock 模式 ──

    def _extract_mock(self, task: dict, file_ext: str) -> ExtractionResult:
        """解析 mock_text 为结构化 blocks。"""
        text = task["mock_text"]
        blocks = extract_text_file(text, source="mock")
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))
        flat_text = blocks_to_text(blocks)

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=file_ext,
            extract_method="mock_injection",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            page_count=None,
            ocr_required=False,
            ocr_status="NOT_REQUIRED",
        )

    # ── PDF ──

    def _extract_pdf(self, task: dict) -> ExtractionResult:
        """PDF 提取 + OCR fallback。"""
        from opensearch_pipeline.extraction.pdf_extractor import extract_pdf

        local_path = task.get("local_path", "")
        blocks, page_count, warnings = extract_pdf(local_path)
        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        result = ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext="pdf",
            extract_method="pypdf",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            page_count=page_count,
            warnings=warnings,
        )

        # OCR fallback 判断
        if self._needs_ocr(result):
            result = self._apply_ocr_fallback(task, result)

        return result

    # ── DOCX ──

    def _extract_docx(self, task: dict) -> ExtractionResult:
        """DOCX 提取。"""
        from opensearch_pipeline.extraction.docx_extractor import extract_docx

        local_path = task.get("local_path", "")
        blocks, warnings = extract_docx(local_path)
        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext="docx",
            extract_method="python_docx",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            warnings=warnings,
        )

    # ── XLSX / XLS ──

    def _extract_xlsx(self, task: dict) -> ExtractionResult:
        """Excel 提取：逐 sheet 逐行读取单元格文本。"""
        local_path = task.get("local_path", "")
        file_ext = task.get("file_ext", "xlsx").lower()
        blocks = []
        warnings = []

        try:
            import openpyxl
            wb = openpyxl.load_workbook(local_path, read_only=True, data_only=True)
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows_text = []
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    line = "\t".join(cells).strip()
                    if line:
                        rows_text.append(line)
                if rows_text:
                    sheet_text = f"## {sheet_name}\n" + "\n".join(rows_text)
                    blocks.append(ExtractedBlock(
                        block_type="table",
                        text=sheet_text,
                        page_num=None,
                        source="openpyxl"
                    ))
            wb.close()
        except Exception as e:
            warnings.append(f"Failed to extract Excel file: {e}")

        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=file_ext,
            extract_method="openpyxl",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            warnings=warnings,
        )

    # ── Plain text / Markdown ──

    def _extract_text(self, task: dict) -> ExtractionResult:
        """纯文本/Markdown 提取。"""
        local_path = task.get("local_path", "")
        file_ext = task.get("file_ext", "txt").lower()

        try:
            with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
                raw_text = f.read()
        except Exception as e:
            return ExtractionResult(
                doc_id=task["doc_id"],
                version_no=task["version_no"],
                source_key=task.get("raw_key", ""),
                file_ext=file_ext,
                extract_method="plain_text",
                title=task.get("filename", ""),
                text="",
                text_length=0,
                warnings=[f"Failed to read file: {e}"],
            )

        blocks = extract_text_file(raw_text, source="native")
        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=file_ext,
            extract_method="plain_text",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
        )

    # ── Image (direct OCR) ──

    def _extract_image(self, task: dict) -> ExtractionResult:
        """图片直接调用过滤漏斗进行三阶段分析。"""
        from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor
        
        local_path = task.get("local_path", "")
        is_public = "_quarantine/" not in task.get("raw_key", "")
        
        processor = ImageFunnelProcessor(simulate=self.simulate)
        funnel_res = processor.process_image(local_path, task["doc_id"], is_public=is_public)
        
        status = funnel_res["status"]
        assets = [{
            "filename": os.path.basename(local_path),
            "local_path": local_path,
            "status": status,
            "width": funnel_res.get("width", 0),
            "height": funnel_res.get("height", 0),
            "file_size_kb": funnel_res.get("file_size_kb", 0.0),
            "ocr_text": funnel_res.get("ocr_text", ""),
            "visual_summary": funnel_res.get("visual_summary", ""),
            "reason": funnel_res.get("reason", "")
        }]

        # 根据漏斗决策构建块和全文
        blocks = []
        warnings = []
        
        if status == "ROUTE_TO_TEXT" and funnel_res.get("ocr_text"):
            blocks.append(ExtractedBlock(
                block_type="ocr_text",
                text=funnel_res["ocr_text"],
                page_num=1,
                source="ocr"
            ))
        elif status == "QUARANTINE_SENSITIVE":
            warnings.append(f"🚨 Sensitive content detected in non-public image asset: {funnel_res.get('reason')}")
        
        flat_text = blocks_to_text(blocks)

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=task.get("file_ext", "png"),
            extract_method="image_funnel",
            title=task.get("filename", ""),
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            ocr_required=(status == "ROUTE_TO_TEXT"),
            ocr_status="DONE" if status == "ROUTE_TO_TEXT" else "NOT_REQUIRED",
            warnings=warnings,
            assets=assets
        )


    # ── Unsupported ──

    def _unsupported(self, task: dict, file_ext: str) -> ExtractionResult:
        """不支持的文件类型。"""
        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=file_ext,
            extract_method=f"unsupported:{file_ext}",
            title=task.get("filename", ""),
            text="",
            text_length=0,
            warnings=[f"Unsupported file type: {file_ext}"],
        )

    # ── OCR fallback logic ──

    def _needs_ocr(self, result: ExtractionResult) -> bool:
        """判断是否需要 OCR fallback。"""
        if result.text_length >= self.OCR_THRESHOLD_CHARS:
            return False

        # 只有 PDF 和图片类型走 OCR
        if result.file_ext not in ("pdf", "png", "jpg", "jpeg", "webp"):
            return False

        return True

    def _apply_ocr_fallback(self, task: dict, result: ExtractionResult) -> ExtractionResult:
        """应用 OCR fallback，按页添加 OCR blocks。"""
        local_path = task.get("local_path", "")

        if result.file_ext == "pdf":
            ocr_result = self.ocr_client.ocr_pdf(
                local_path, task["doc_id"], self.oss_client,
            )
        else:
            ocr_result = self.ocr_client.ocr_image(
                local_path, task["doc_id"], self.oss_client,
            )

        # 添加 OCR blocks
        ocr_blocks = ocr_result.to_blocks()
        result.blocks.extend(ocr_blocks)

        # 更新 text
        if ocr_result.combined_text:
            if result.text:
                result.text = result.text + "\n\n" + ocr_result.combined_text
            else:
                result.text = ocr_result.combined_text
            result.text_length = len(result.text)

        result.ocr_required = True
        result.ocr_status = ocr_result.status
        result.extract_method = f"{result.extract_method}+ocr_fallback"

        return result
