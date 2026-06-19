# -*- coding: utf-8 -*-
"""Increment 2 — VLM PDF table refinement (flag-off default + number-fidelity gate).

Strict no-regression: with the flag off (default) it is a byte-identical no-op and
makes ZERO VLM calls. With the flag on, the number-fidelity gate rejects any VLM
table that drops a native digit (keeps the rule output), so a refinement can never
lose a number.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import opensearch_pipeline.extraction.vlm_rebuilder as VR
from opensearch_pipeline.extraction.schema import ExtractedBlock, ExtractionResult


def _cfg(enabled=True, refine=True):
    rebuild = types.SimpleNamespace(enabled=enabled, refine_tables=refine, max_pages=50,
                                    doc_budget_rmb=5.0, run_budget_rmb=200.0,
                                    ocr_page_rmb=0.06, vlm_image_rmb=0.04)
    ocr = types.SimpleNamespace(vlm_model="qwen3-vl-plus", model="qwen-vl-ocr",
                                api_key="k", api_base_url="https://dashscope.aliyuncs.com")
    return types.SimpleNamespace(rebuild=rebuild, ocr=ocr, environment="test", simulate_db=True)


def _result(blocks):
    return ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="pdf",
                            extract_method="pdfplumber_layout", title="t",
                            text="\n".join(b.text for b in blocks),
                            text_length=0, blocks=blocks, page_count=1)


def _table(text, page=1):
    return ExtractedBlock(block_type="table", text=text, page_num=page, source="native",
                          extra={"table_index": 0, "row_count": 2, "detected_by": "pdfplumber_lines"})


def _patch_vlm(monkeypatch, vlm_tables):
    """Patch render + reconstruct so no real API is hit; VLM returns given table markdowns."""
    monkeypatch.setattr(VR, "_render_page_image", lambda *a, **k: (b"img", "image/jpeg"))
    monkeypatch.setattr(VR, "_vlm_reconstruct_page",
                        lambda *a, **k: [{"type": "table", "text": t} for t in vlm_tables])


# ── number-fidelity helpers ─────────────────────────────────────────────────

def test_number_multiset_is_a_multiset():
    c = VR._number_multiset("杯口外径 89.3 高度 89.3 数量 12,345")
    assert c["89.3"] == 2 and c["12345"] == 1   # repeated digit counted twice; comma normalized


def test_gate_rejects_dropped_digit_and_dropped_duplicate():
    assert VR._native_numbers_preserved("89.3 135.7", "89.3 135.7 50") is True   # extra ok
    assert VR._native_numbers_preserved("89.3 135.7", "89.3") is False           # dropped a number
    assert VR._native_numbers_preserved("89.3 89.3", "89.3") is False            # dropped a duplicate


def test_mangle_detection():
    assert VR._table_is_mangled("| a | 89.3 | 135.7 |\n| b |") is True           # ragged
    assert VR._table_is_mangled("| onecol |\n| onecol2 |") is True               # degenerate single column
    assert VR._table_is_mangled("| 项目 | 值 |\n| 杯口外径 | 89.3 |\n| 高度 | 135.7 |") is False  # clean


# ── flag gating (the regression guard) ──────────────────────────────────────

def test_flag_off_is_byte_identical_noop(monkeypatch):
    monkeypatch.setattr(VR, "_render_page_image", lambda *a, **k: (_ for _ in ()).throw(AssertionError("VLM must NOT be called when flag off")))
    blk = _table("| a | 89.3 | 135.7 |\n| b |")
    res = _result([blk])
    before_text, before_extra = blk.text, dict(blk.extra)
    out = VR.maybe_refine_tables({"local_path": "/x.pdf"}, res, _cfg(refine=False))
    assert out is res and blk.text == before_text and blk.extra == before_extra   # untouched


def test_requires_enabled(monkeypatch):
    monkeypatch.setattr(VR, "_render_page_image", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    blk = _table("| a | 89.3 | 135.7 |\n| b |")
    res = _result([blk])
    VR.maybe_refine_tables({"local_path": "/x.pdf"}, res, _cfg(enabled=False, refine=True))
    assert blk.extra.get("refined_by") is None


def test_non_pdf_noop(monkeypatch):
    monkeypatch.setattr(VR, "_render_page_image", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    blk = _table("| a | 89.3 |\n| b |")
    res = _result([blk])
    res.file_ext = "docx"
    VR.maybe_refine_tables({"local_path": "/x.docx"}, res, _cfg())
    assert blk.extra.get("refined_by") is None


# ── gate behavior on a live (mocked-VLM) refine ─────────────────────────────

def test_accepts_recovery_and_keeps_fallback(monkeypatch):
    vlm = "| 项目 | 值 | 单位 |\n| 杯口外径 | 89.3 | mm |\n| 高度 | 135.7 | mm |"   # keeps 89.3, recovers 135.7
    _patch_vlm(monkeypatch, [vlm])
    native = "| 项目 | 值 | 单位 |\n| 杯口外径 89.3 mm |"                            # ragged (3 cells then 1), missing 135.7
    blk = _table(native)
    res = _result([blk])
    VR.maybe_refine_tables({"local_path": "/x.pdf"}, res, _cfg())
    assert blk.extra["refined_by"] == "vlm"
    assert "135.7" in blk.text                                   # number recovered
    assert blk.extra["fallback_text"] == native                 # native preserved for audit
    assert blk.source == "native"                               # source unchanged
    assert "+vlm_table_refine" in res.extract_method


def test_rejects_when_vlm_drops_a_native_number(monkeypatch):
    _patch_vlm(monkeypatch, ["| 项目 | 值 |\n| 杯口外径 | 89.3 |\n| 高度 |"])        # DROPS 135.7
    native = "| a | 89.3 | 135.7 |\n| b |"                                          # ragged native, has 135.7
    blk = _table(native)
    res = _result([blk])
    VR.maybe_refine_tables({"local_path": "/x.pdf"}, res, _cfg())
    assert blk.extra.get("refined_by") is None and blk.text == native              # kept native (fail-safe)


def test_rejects_mismatched_table_even_when_it_has_numbers(monkeypatch):
    # Real-VLM smoke test caught this: a 0-number native header table + a same-page
    # DIFFERENT table (with numbers) → digit-only gate trivially passed and SWAPPED content.
    # The content-correspondence gate must reject it and keep the native table.
    _patch_vlm(monkeypatch, ["| 富岭科技 | 文件编号 | FL-ZS-WI-005 | A/0 | 2024 |"])  # unrelated doc-header table
    native = "| 时间 | 机台 | 数量 | 货号 |\n| a |"                                   # ragged, different table, 0 numbers
    blk = _table(native)
    res = _result([blk])
    VR.maybe_refine_tables({"local_path": "/x.pdf"}, res, _cfg())
    assert blk.extra.get("refined_by") is None and blk.text == native               # not swapped


def test_clean_table_not_targeted(monkeypatch):
    monkeypatch.setattr(VR, "_render_page_image", lambda *a, **k: (_ for _ in ()).throw(AssertionError("clean table must not be sent to VLM")))
    blk = _table("| 项目 | 值 |\n| 杯口外径 | 89.3 |\n| 高度 | 135.7 |")            # well-formed
    res = _result([blk])
    VR.maybe_refine_tables({"local_path": "/x.pdf"}, res, _cfg())
    assert blk.extra.get("refined_by") is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
