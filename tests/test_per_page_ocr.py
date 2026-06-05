# -*- coding: utf-8 -*-
"""Increment 0b — per-page OCR gate.

Guards the fix to the all-or-nothing OCR gate: a multi-page PDF where the cover
page has text but the body is scanned must OCR the scanned pages (the old gate
skipped OCR whenever whole-document text_length >= 100).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
from opensearch_pipeline.extraction.schema import ExtractedBlock, ExtractionResult


def _gate():
    return UnifiedExtractor.__new__(UnifiedExtractor)  # gate methods need no clients


def _pdf(text_pages_chars, page_count):
    """ExtractionResult with given per-page native-text sizes."""
    blocks = []
    for pg, n in text_pages_chars.items():
        if n:
            blocks.append(ExtractedBlock(block_type="paragraph", text="x" * n, page_num=pg))
    return ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="pdf",
                            extract_method="m", title="t", text="x" * sum(text_pages_chars.values()),
                            text_length=sum(text_pages_chars.values()), blocks=blocks,
                            page_count=page_count)


def test_scanned_body_with_text_cover_triggers_per_page_ocr():
    ux = _gate()
    # page 1 rich (200), pages 2-4 empty -> old whole-doc gate (200>=100) would skip OCR
    res = _pdf({1: 200, 2: 0, 3: 0, 4: 0}, page_count=4)
    assert ux._pages_needing_ocr(res) == [2, 3, 4]
    assert ux._needs_ocr(res) is True


def test_image_only_page_counts_as_needing_ocr():
    ux = _gate()
    res = _pdf({1: 200}, page_count=2)
    res.blocks.append(ExtractedBlock(block_type="image_ref", text="", page_num=2))  # image, no native text
    assert ux._pages_needing_ocr(res) == [2]


def test_all_text_pdf_skips_ocr():
    ux = _gate()
    res = _pdf({1: 300, 2: 300, 3: 300}, page_count=3)
    assert ux._pages_needing_ocr(res) == []
    assert ux._needs_ocr(res) is False


def test_image_file_still_uses_whole_doc_threshold():
    ux = _gate()
    res = ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="jpg",
                           extract_method="m", title="t", text="ab", text_length=2,
                           blocks=[], page_count=1)
    assert ux._needs_ocr(res) is True
    res.text_length = 500
    assert ux._needs_ocr(res) is False


def test_end_to_end_merges_ocr_pages_in_order():
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Fuling cover page with substantial native text " * 6)
    doc.new_page(); doc.new_page()  # blank pages 2 & 3
    path = tempfile.mktemp(suffix=".pdf"); doc.save(path); doc.close()

    ux = UnifiedExtractor()
    ux.ocr_client.simulate = True  # deterministic OCR text
    res = ux.extract({"doc_id": "synth", "version_no": 1, "local_path": path, "file_ext": "pdf",
                      "filename": "synth.pdf", "raw_key": "raw/x/synth.pdf", "_tmp_dir": tempfile.mkdtemp()})
    ocr_pages = sorted({b.page_num for b in res.blocks if getattr(b, "block_type", "") == "ocr_text"})
    assert ocr_pages == [2, 3]
    assert "ocr_fallback" in res.extract_method
    order = [getattr(b, "page_num", 0) or 0 for b in res.blocks]
    assert order == sorted(order)  # merged in page order


if __name__ == "__main__":
    for fn in [test_scanned_body_with_text_cover_triggers_per_page_ocr,
               test_image_only_page_counts_as_needing_ocr, test_all_text_pdf_skips_ocr,
               test_image_file_still_uses_whole_doc_threshold,
               test_end_to_end_merges_ocr_pages_in_order]:
        fn(); print(f"  ✓ {fn.__name__}")
    print("all per-page-OCR tests passed")
