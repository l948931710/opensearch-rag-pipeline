# -*- coding: utf-8 -*-
"""Unit tests for the VLM PDF rebuilder (extraction/vlm_rebuilder.py).

No real API: render + VLM calls are monkeypatched. Validates the load-bearing
guarantees — flag-off no-op, no escalation of extractable PDFs (number-fidelity),
cost-gate denial → rule fallback, and in-place splice of typed blocks.
"""
from types import SimpleNamespace

import pytest

from opensearch_pipeline.config import RebuildConfig
from opensearch_pipeline.extraction.schema import ExtractedBlock, ExtractionResult
from opensearch_pipeline.extraction import vlm_rebuilder as VR


def make_cfg(**rb):
    defaults = dict(enabled=True, max_pages=50, doc_budget_rmb=5.0,
                    run_budget_rmb=200.0, ocr_page_rmb=0.06, vlm_image_rmb=0.04)
    defaults.update(rb)
    return SimpleNamespace(rebuild=RebuildConfig(**defaults),
                           ocr=SimpleNamespace(max_ocr_pages=5, vlm_model="qwen3-vl-plus",
                                               api_key="k", api_base_url="https://x"),
                           environment="development", simulate_db=True)


def make_result(ext="pdf", text="", blocks=None):
    return ExtractionResult(
        doc_id="d", version_no=1, source_key="raw/x/f.pdf", file_ext=ext,
        extract_method="pypdf", title="T", text=text, text_length=len(text),
        blocks=blocks or [],
    )


TASK = {"doc_id": "d", "version_no": 1, "file_ext": "pdf", "local_path": "/tmp/f.pdf",
        "owner_dept": "x", "doc_title": "T", "filename": "f.pdf"}


def test_noop_when_disabled(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(VR, "_page_char_counts", lambda p: called.__setitem__("n", called["n"] + 1) or [0])
    cfg = make_cfg(enabled=False)
    r = make_result(text="orig")
    out = VR.maybe_rebuild_pdf(TASK, r, cfg)
    assert out.text == "orig" and "vlm_rebuild" not in (out.extract_method or "")
    assert called["n"] == 0  # didn't even look at pages


def test_skips_extractable_pdf(monkeypatch):
    """All pages have text → no escalation → number-fidelity preserved."""
    monkeypatch.setattr(VR, "_page_char_counts", lambda p: [500, 500, 500])
    monkeypatch.setattr(VR, "_render_page_image",
                        lambda *a, **k: pytest.fail("must not render an extractable PDF"))
    cfg = make_cfg()
    r = make_result(text="real digits 89.3")
    out = VR.maybe_rebuild_pdf(TASK, r, cfg)
    assert out.text == "real digits 89.3"
    assert "vlm_rebuild" not in (out.extract_method or "")


def test_cost_gate_denies(monkeypatch):
    """Escalation needed but over the per-doc page cap → DENY → rule fallback (no render)."""
    monkeypatch.setattr(VR, "_page_char_counts", lambda p: [0, 0, 0])
    monkeypatch.setattr(VR, "_render_page_image",
                        lambda *a, **k: pytest.fail("denied gate must not render"))
    cfg = make_cfg(max_pages=1)  # 3 escalate-pages > cap 1
    r = make_result(text="")
    out = VR.maybe_rebuild_pdf(TASK, r, cfg)
    assert "vlm_rebuild" not in (out.extract_method or "")
    assert out.blocks == []


def test_splices_rebuilt_blocks(monkeypatch):
    """Unextractable page → VLM blocks spliced in place, source=multimodal, text+method updated."""
    monkeypatch.setattr(VR, "_page_char_counts", lambda p: [0])
    monkeypatch.setattr(VR, "_render_page_image", lambda *a, **k: (b"PNGBYTES", "image/png"))
    monkeypatch.setattr(VR, "_vlm_reconstruct_page",
                        lambda *a, **k: [{"type": "heading", "text": "BRC 食品安全认证证书", "level": 1},
                                         {"type": "paragraph", "text": "证书编号 12345，有效期至 2026-01-01。"}])
    cfg = make_cfg()
    r = make_result(text="", blocks=[])
    out = VR.maybe_rebuild_pdf(TASK, r, cfg)
    assert "vlm_rebuild" in out.extract_method
    assert len(out.blocks) == 2
    assert all(b.source == "multimodal" and b.extra.get("rebuilt_by") == "vlm" for b in out.blocks)
    assert "12345" in out.text and "BRC" in out.text   # digits preserved verbatim
    assert out.text_length == len(out.text)


def test_splice_keeps_existing_blocks(monkeypatch):
    """Mixed doc: page1 extractable (rule block), page2 unextractable → rule block kept,
    rebuilt block appended after it (reading order by page)."""
    monkeypatch.setattr(VR, "_page_char_counts", lambda p: [400, 0])
    monkeypatch.setattr(VR, "_render_page_image", lambda *a, **k: (b"X", "image/png"))
    monkeypatch.setattr(VR, "_vlm_reconstruct_page",
                        lambda *a, **k: [{"type": "paragraph", "text": "page2 recovered"}])
    cfg = make_cfg()
    p1 = ExtractedBlock(block_type="paragraph", text="page1 rule text", page_num=1, source="native")
    r = make_result(text="page1 rule text", blocks=[p1])
    out = VR.maybe_rebuild_pdf(TASK, r, cfg)
    assert out.blocks[0] is p1                                   # rule block preserved
    assert out.blocks[-1].source == "multimodal"                # rebuilt appended
    assert out.blocks[-1].page_num == 2
