# -*- coding: utf-8 -*-
"""
Unified Extraction Layer

统一文档提取入口，消除 scan_pending_clean / faq_extract / batch_prepare_submit 三处重复。

用法：
    from opensearch_pipeline.extraction import UnifiedExtractor, ExtractionResult

    extractor = UnifiedExtractor(simulate=True)
    result = extractor.extract(task)
"""

from opensearch_pipeline.extraction.schema import ExtractionResult, ExtractedBlock
from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
from opensearch_pipeline.extraction.ocr_client import OCRClient, OCRResult

__all__ = [
    "UnifiedExtractor",
    "ExtractionResult",
    "ExtractedBlock",
    "OCRClient",
    "OCRResult",
]
