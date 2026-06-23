# -*- coding: utf-8 -*-
"""
test_image_ocr_pii_gate.py — Stage-2 image-OCR PII gate (RAG_IMAGE_OCR_PII, default OFF).

Covers: detection over asset['ocr_text'] only (not visual_summary); flag-OFF no-op; high-sev
image hit → whole-doc QUARANTINE; material-code FP allow-list; graceful degradation on a bad
asset; the redaction gap-fix mutating the SAME in-memory asset object (with filename/anchor_row
preserved); and the DAG-2 redact-before-chunk ordering the same-object fix relies on.
"""

import opensearch_pipeline.pipeline_nodes as pn

_PHONE = "13800138000"
_IDCARD = "110101199003078515"  # matches cn_id_card pattern


def _doc(ocr_text="", visual_summary="", text="正文内容", status="ROUTE_TO_TEXT",
         filename="img1.png", anchor_row=None, llm_risk="low"):
    asset = {"status": status, "ocr_text": ocr_text, "visual_summary": visual_summary,
             "filename": filename}
    if anchor_row is not None:
        asset["anchor_row"] = anchor_row
    return {"doc_id": "D", "version_no": 1, "text": text, "assets": [asset],
            "llm_risk_level": llm_risk}


def _ctx(doc):
    return {"canonicals": [doc], "simulate_db": True}


def _img_hits(doc):
    return [h for h in doc.get("risk_hits", []) if h.get("source") == "image_ocr"]


# ── detection ────────────────────────────────────────────────────────────────
def test_flag_on_detects_image_phone(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"联系电话 {_PHONE}")
    pn.node_detect_sensitive(_ctx(doc))
    hits = _img_hits(doc)
    assert any(h["finding_type"] == "image_ocr:cn_mobile" for h in hits)
    assert doc["risk_level"] == "medium"


def test_flag_off_noop(monkeypatch):
    monkeypatch.delenv("RAG_IMAGE_OCR_PII", raising=False)
    doc = _doc(ocr_text=f"联系电话 {_PHONE}")
    pn.node_detect_sensitive(_ctx(doc))
    assert _img_hits(doc) == []
    assert doc["risk_level"] == "low"


def test_visual_summary_not_scanned(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text="干净文本", visual_summary=f"按钮上显示 {_PHONE}")
    pn.node_detect_sensitive(_ctx(doc))
    assert _img_hits(doc) == []
    assert doc["risk_level"] == "low"


def test_discarded_asset_skipped(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"电话 {_PHONE}", status="DISCARD_LOW_VALUE")
    pn.node_detect_sensitive(_ctx(doc))
    assert _img_hits(doc) == []


def test_high_sev_image_hit_quarantines_whole_doc(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"身份证 {_IDCARD}")
    ctx = _ctx(doc)
    pn.node_detect_sensitive(ctx)
    assert doc["risk_level"] == "high"
    pn.node_redact_or_quarantine(ctx)
    assert doc["redaction_action"] == "QUARANTINE"


def test_material_code_fp_ignored(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"物料编码 {_IDCARD}")          # id-card pattern but a material code
    pn.node_detect_sensitive(_ctx(doc))
    assert not any(h["finding_type"] == "image_ocr:cn_id_card" for h in _img_hits(doc))
    assert doc["risk_level"] == "low"
    # same number WITHOUT the anchor → real high-sev hit
    doc2 = _doc(ocr_text=f"客户身份证 {_IDCARD}")
    pn.node_detect_sensitive(_ctx(doc2))
    assert doc2["risk_level"] == "high"


def test_graceful_degradation_on_bad_asset(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    # base text carries a phone; the bad asset's ocr_text is a non-str → re.search raises → caught
    doc = _doc(ocr_text=f"电话 {_PHONE}", text=f"正文电话 {_PHONE}")
    doc["assets"].append({"status": "ROUTE_TO_TEXT", "ocr_text": 12345, "filename": "bad.png"})
    pn.node_detect_sensitive(_ctx(doc))            # must not raise
    assert doc["risk_level"] == "medium"           # base-text + good-asset PII still detected


# ── redaction gap-fix (same in-memory object) ────────────────────────────────
def test_redaction_masks_ocr_text_same_object(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"电话{_PHONE}")
    ctx = _ctx(doc)
    pn.node_detect_sensitive(ctx)                  # medium
    pn.node_redact_or_quarantine(ctx)              # same ctx/object
    assert _PHONE not in doc["assets"][0]["ocr_text"]
    assert "138****8000" in doc["assets"][0]["ocr_text"]


def test_flag_off_redaction_leaves_ocr_untouched(monkeypatch):
    monkeypatch.delenv("RAG_IMAGE_OCR_PII", raising=False)
    doc = _doc(ocr_text=f"电话{_PHONE}", text=f"正文{_PHONE}")  # base text → medium → redact runs
    ctx = _ctx(doc)
    pn.node_detect_sensitive(ctx)
    pn.node_redact_or_quarantine(ctx)
    assert doc["assets"][0]["ocr_text"] == f"电话{_PHONE}"      # ocr untouched when flag off


def test_filename_and_anchor_row_survive_redaction(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"电话{_PHONE}", filename="proc_step3.png", anchor_row=7)
    ctx = _ctx(doc)
    pn.node_detect_sensitive(ctx)
    pn.node_redact_or_quarantine(ctx)
    a = doc["assets"][0]
    assert a["filename"] == "proc_step3.png"
    assert a["anchor_row"] == 7
    assert _PHONE not in a["ocr_text"]


# ── structural: redact (node 03) runs before chunk (node 05) in DAG-2 ─────────
def test_dag2_redact_before_chunk():
    from opensearch_pipeline.dag_definitions import build_dag2_canonical_to_chunk
    dag = build_dag2_canonical_to_chunk()
    order = dag._topological_sort()
    funcs = [dag.nodes[nid].func for nid in order]
    assert funcs.index(pn.node_redact_or_quarantine) < funcs.index(pn.node_chunk_documents)
