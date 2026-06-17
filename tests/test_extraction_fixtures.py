# -*- coding: utf-8 -*-
"""tests/test_extraction_fixtures.py — DC-1: run the real extractors against COMMITTED binary fixtures.

Unlike the synthesized-at-test-time fixtures, these are version-stable binaries committed under
tests/fixtures/ (regenerate via tests/fixtures/make_fixtures.py). The docx fixture carries merged
table cells (gridSpan + vMerge) as a permanent regression guard for the DC-3 dedup fix.
"""
import os

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def test_fixture_files_committed():
    for f in ("merged_cells.docx", "merged_cells.xlsx", "sample.pdf"):
        p = os.path.join(FIX, f)
        assert os.path.exists(p) and os.path.getsize(p) > 0, f"missing committed fixture: {f}"


def test_docx_fixture_merged_cells_deduped():
    """DC-3 permanent guard on a fixed binary: merged cells must not duplicate."""
    from opensearch_pipeline.extraction.docx_extractor import extract_docx
    blocks, _ = extract_docx(os.path.join(FIX, "merged_cells.docx"))
    tbl = "\n".join(b.text for b in blocks if getattr(b, "block_type", "") == "table")
    assert "合并表头 | 列C" in tbl            # horizontal gridSpan deduped (not 合并表头 | 合并表头)
    assert "合并表头 | 合并表头" not in tbl
    assert tbl.count("纵向合并") == 1          # vertical vMerge emitted once
    assert "甲 | 乙 | 丙" in tbl               # non-merged row intact


def test_pdf_fixture_text_extracted():
    from opensearch_pipeline.extraction.pdf_extractor import extract_pdf
    blocks, page_count, _ = extract_pdf(os.path.join(FIX, "sample.pdf"))
    text = "\n".join(getattr(b, "text", "") for b in blocks)
    assert page_count >= 1
    assert "PDF fixture line one" in text and "PDF fixture line two" in text


def test_xlsx_fixture_merged_cell_value_surfaced():
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    res = UnifiedExtractor(simulate=True).extract({
        "doc_id": "FIXTURE_XLSX", "version_no": 1, "file_ext": "xlsx", "raw_key": "",
        "local_path": os.path.join(FIX, "merged_cells.xlsx"),
    })
    text = "\n".join(getattr(b, "text", "") for b in res.blocks)
    assert "合并行说明" in text   # merged-cell value surfaced (read_only=False expansion)
    assert "温度" in text and "25" in text
