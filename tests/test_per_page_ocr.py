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


def test_pages_beyond_native_cap_not_flagged_for_ocr():
    """原生抽取只覆盖前 PDF_NATIVE_MAX_PAGES(=20) 页；page_count 更大时，越界页（未被抽取、
    per_page 缺失）不能因"0 原生字符"被误判需 OCR——否则浪费 OCR 预算并挤出真扫描页。"""
    ux = _gate()
    # 50 页 PDF，前 20 页都有充足原生文本（21-50 未抽取）→ 不应有任何页需 OCR
    res = _pdf({pg: 200 for pg in range(1, 21)}, page_count=50)
    assert ux._pages_needing_ocr(res) == []
    # 对照：前 20 页内仍有真扫描页（page 5 空）→ 照常被 OCR
    pages = {pg: 200 for pg in range(1, 21)}
    pages[5] = 0
    res2 = _pdf(pages, page_count=50)
    assert ux._pages_needing_ocr(res2) == [5]


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


def test_zero_page_count_no_local_path_returns_placeholder_page1():
    """RD 61D861: page_count<=0 + no local path → conservative [1] placeholder (was [])."""
    ux = _gate()
    res = ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="pdf",
                           extract_method="m", title="t", text="", text_length=0,
                           blocks=[], page_count=0)
    pages = ux._pages_needing_ocr(res)
    assert pages == [1], f"expected [1] placeholder, got {pages}"
    assert ux._needs_ocr(res) is True
    assert any("conservative OCR fallback" in w for w in res.warnings)


def test_negative_page_count_returns_placeholder_page1():
    """page_count<0 (defensive) also triggers conservative fallback."""
    ux = _gate()
    res = ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="pdf",
                           extract_method="m", title="t", text="", text_length=0,
                           blocks=[], page_count=-1)
    pages = ux._pages_needing_ocr(res)
    assert pages == [1]
    assert any("conservative OCR fallback" in w for w in res.warnings)


def test_none_page_count_returns_placeholder_page1():
    """page_count=None (extraction never set it) also triggers conservative fallback."""
    ux = _gate()
    res = ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="pdf",
                           extract_method="m", title="t", text="", text_length=0,
                           blocks=[], page_count=None)
    pages = ux._pages_needing_ocr(res)
    assert pages == [1]


def test_zero_page_count_with_recoverable_local_path_uses_recovered():
    """page_count=0 but local PDF readable → recover real page_count, then OCR per-page."""
    import fitz
    doc = fitz.open()
    for _ in range(3):
        doc.new_page()  # 3 blank pages
    path = tempfile.mktemp(suffix=".pdf")
    doc.save(path)
    doc.close()

    ux = _gate()
    res = ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="pdf",
                           extract_method="m", title="t", text="", text_length=0,
                           blocks=[], page_count=0)
    res._local_path = path  # simulate stash by _extract_pdf
    pages = ux._pages_needing_ocr(res)
    # All 3 pages blank → all 3 below threshold → all OCR
    assert pages == [1, 2, 3], f"expected [1,2,3], got {pages}"
    assert res.page_count == 3  # recovered + persisted
    assert any("recovered_page_count=3" in w for w in res.warnings)


def test_non_pdf_with_zero_page_count_still_returns_empty():
    """Non-PDF (e.g. docx) must not be affected by the PDF conservative fallback."""
    ux = _gate()
    res = ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="docx",
                           extract_method="m", title="t", text="x", text_length=1,
                           blocks=[], page_count=0)
    assert ux._pages_needing_ocr(res) == []


if __name__ == "__main__":
    for fn in [test_scanned_body_with_text_cover_triggers_per_page_ocr,
               test_image_only_page_counts_as_needing_ocr, test_all_text_pdf_skips_ocr,
               test_image_file_still_uses_whole_doc_threshold,
               test_end_to_end_merges_ocr_pages_in_order,
               test_zero_page_count_no_local_path_returns_placeholder_page1,
               test_negative_page_count_returns_placeholder_page1,
               test_none_page_count_returns_placeholder_page1,
               test_zero_page_count_with_recoverable_local_path_uses_recovered,
               test_non_pdf_with_zero_page_count_still_returns_empty]:
        fn(); print(f"  ✓ {fn.__name__}")
    print("all per-page-OCR tests passed")
