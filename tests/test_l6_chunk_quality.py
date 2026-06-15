# -*- coding: utf-8 -*-
"""Unit tests for the L6 chunk-quality layer — pure metric functions, no I/O.

envboot forces simulate OFF, so the eval harness cannot be run in sim mode; these tests
ARE the offline validation path. Every test feeds synthetic in-memory chunks to the pure
family_* / analyze_corpus / merge_chunk_panel functions and asserts the detector + gate.
"""
from __future__ import annotations

import os as _os

# Importing the L6 layer triggers eval_harness.envboot.boot() at import time, which mutates
# global os.environ (forces RAG_SIMULATE=false + prod endpoints) for the LIVE harness. That
# leaks into other test files collected after this one. Snapshot + restore so the test session
# env is unchanged — the pure metric functions exercised here never read os.environ.
_SAVED_ENV = dict(_os.environ)
from eval_harness.layers import l6_chunk_quality as L6  # noqa: E402  (boots envboot)
from eval_harness import judge  # noqa: E402
_os.environ.clear()
_os.environ.update(_SAVED_ENV)


def _chunk(cid, doc, ctype="text_chunk", text="正常的一段文字内容，足够长以通过校验。",
           section="概述", img=None, fn="a.docx", token=20):
    return {"chunk_id": cid, "doc_id": doc, "chunk_type": ctype, "section_title": section,
            "chunk_text": text, "token_count": token, "image_refs_json": img,
            "original_filename": fn, "category_l1": "manual"}


_GOOD_D7 = {"D1": {"sym_diff": 0, "rds_active": 3078}, "D4": {"orphan_count": 0},
            "D7": {"missing": 0, "duplicate": 0},
            "D6": {"chunk_compliant_all": 712, "total_chunks": 724},
            "D3": {"routed_total": 306, "routed_with_step": 201}}
_CLEAN_H = {"rds_active": 2, "ha3_unique": 2, "truncated": False,
            "missing_in_ha3": 0, "extra_in_ha3": 0, "idset_jaccard": 1.0}


# ── Family B — boundary / size ────────────────────────────────────────────

def test_boundary_oversize_structural_detected():
    chunks = [_chunk("c1", "D1"),
              _chunk("c2", "D1", ctype="procedure_parent", text="步骤" * 3000, token=2400)]
    b = L6.family_boundary(chunks)
    assert b["oversize_count"] == 1
    assert b["oversize_structural_count"] == 1


def test_boundary_midsentence_doc_clustered():
    # D1: one clean + one mid-sentence -> doc mean 0.5; D2 clean -> 0 ; cluster mean 0.25
    chunks = [_chunk("c1", "D1", text="这句话正常结束。"),
              _chunk("c2", "D1", text="这句话没有结束符号被切断"),
              _chunk("c3", "D2", text="完整的句子。")]
    b = L6.family_boundary(chunks)
    assert 0.0 < b["midsentence_cut_rate"] <= 0.5


def test_boundary_token_drift():
    chunks = [_chunk("c1", "D1", text="一二三四五六七八九十", token=999)]
    b = L6.family_boundary(chunks)
    assert b["token_drift_count"] == 1


# ── Family C — self-containedness heuristic ───────────────────────────────

def test_self_containedness_dangling_anaphor():
    chunks = [_chunk("c1", "D1", text="该步骤需要先确认设备状态再继续操作。", section=None),
              _chunk("c2", "D1", text="完整独立的一句话内容。", section="安全")]
    c = L6.family_self_containedness_heuristic(chunks)
    assert c["dangling_anaphor_rate"] is not None and c["dangling_anaphor_rate"] > 0
    assert len(c["sample"]) >= 1


def test_self_containedness_with_section_title_not_flagged():
    # dangling opener but has a section_title -> antecedent resolvable -> not flagged
    chunks = [_chunk("c1", "D1", text="该流程分三步执行完成。", section="发货流程")]
    c = L6.family_self_containedness_heuristic(chunks)
    assert c["dangling_anaphor_rate"] == 0.0


# ── Family E — dedup ──────────────────────────────────────────────────────

def test_dedup_template_vs_twin():
    base = "消防器材应定期检查并保持完好有效随时可用确保安全无隐患，各班组每月需填写检查记录表归档。"
    chunks = [
        _chunk("x1", "DA", text=base + "。"),
        _chunk("x2", "DB", text=base + "！"),   # cross-doc near-dup -> twin contamination
        _chunk("x3", "DA", text=base + "。"),   # same-doc exact -> legit template
    ]
    d = L6.family_dedup(chunks)
    assert d["exact_dup_same_doc"] >= 1
    assert d["near_dup_pairs_cross_doc"] >= 1
    assert d["near_dup_cross_sample"][0]["jaccard"] >= 0.9


# ── Family F — image over-attach ──────────────────────────────────────────

def test_image_binding_overattach():
    chunks = [_chunk("c1", "D1", ctype="step_card",
                     img='[{"image_index":1},{"image_index":1},{"image_index":1}]')]
    f = L6.family_image_binding(chunks)
    assert f["img_dup_factor_max"] == 3.0
    assert f["n_chunks_with_images"] == 1


def test_image_binding_malformed_json():
    chunks = [_chunk("c1", "D1", ctype="step_card", img="{not valid json")]
    f = L6.family_image_binding(chunks)
    assert f["malformed_json"] == 1


# ── Family D — routing (metadata) ─────────────────────────────────────────

def test_routing_policy_doc_producing_faq_is_mismatch():
    # a policy doc (制度) should route clause; producing only faq_chunk is a family mismatch
    chunks = [{"chunk_id": "c1", "doc_id": "D1", "chunk_type": "faq_chunk",
               "section_title": None, "chunk_text": "问：x 答：y" * 5, "token_count": 20,
               "image_refs_json": None, "original_filename": "p.docx",
               "category_l1": "policy", "title": "员工管理制度"}]
    d = L6.family_routing(chunks, {"routed_total": 10, "routed_with_step": 4})
    assert d["mismatch_count"] == 1
    assert d["d3_under_chunk_candidates"] == 6


# ── verdict three-state ───────────────────────────────────────────────────

def test_verdict_go_on_clean_corpus():
    chunks = [_chunk("c1", "D1", text="完整的一句话内容结束。"),
              _chunk("c2", "D2", text="另一段完整内容结束。")]
    out = L6.analyze_corpus(chunks, _GOOD_D7, _CLEAN_H, code_commit="t")
    assert out["state"] == "GO"
    assert out["go_no_go"] is True


def test_verdict_no_go_defect_on_oversize_structural():
    chunks = [_chunk("c1", "D1", text="完整句子。"),
              _chunk("c2", "D2", ctype="procedure_parent", text="步骤" * 3000, token=2400)]
    out = L6.analyze_corpus(chunks, _GOOD_D7, _CLEAN_H, code_commit="t")
    assert out["state"] == "NO_GO_DEFECT"


def test_verdict_incomplete_evidence_without_d7():
    # clean measured gates but D1-D7 JSON absent ⇒ INCOMPLETE, never GO (no fail-open)
    chunks = [_chunk("c1", "D1", text="完整的一句话内容已经结束并且足够长以通过校验。")]
    out = L6.analyze_corpus(chunks, None, _CLEAN_H, code_commit="t")
    assert out["state"] == "NO_GO_INCOMPLETE_EVIDENCE"


def test_verdict_incomplete_on_truncated_idset():
    chunks = [_chunk("c1", "D1", text="完整的一句话内容已经结束并且足够长以通过校验。")]
    h = {"truncated": True}
    out = L6.analyze_corpus(chunks, _GOOD_D7, h, code_commit="t")
    assert out["state"] == "NO_GO_INCOMPLETE_EVIDENCE"


def test_verdict_no_go_defect_on_extra_in_ha3():
    # extra_in_ha3 > 0 ⇒ incomplete purge ⇒ hard fail (measured) ⇒ DEFECT
    chunks = [_chunk("c1", "D1", text="完整的一句话内容已经结束并且足够长以通过校验。")]
    h = {"truncated": False, "missing_in_ha3": 0, "extra_in_ha3": 7, "idset_jaccard": 0.9}
    out = L6.analyze_corpus(chunks, _GOOD_D7, h, code_commit="t")
    assert out["state"] == "NO_GO_DEFECT"


# ── fingerprint ───────────────────────────────────────────────────────────

def test_fingerprint_stable_and_sensitive():
    chunks = [_chunk("c1", "D1"), _chunk("c2", "D1")]
    fp1 = L6.compute_fingerprint(chunks, _GOOD_D7, "abc")
    fp2 = L6.compute_fingerprint(list(reversed(chunks)), _GOOD_D7, "abc")
    assert fp1["chunk_id_set_hash"] == fp2["chunk_id_set_hash"]  # order-independent
    fp3 = L6.compute_fingerprint(chunks + [_chunk("c3", "D2")], _GOOD_D7, "abc")
    assert fp3["chunk_id_set_hash"] != fp1["chunk_id_set_hash"]  # input drift detected
    assert fp1["rubric_version"] == L6.RUBRIC_VERSION


# ── judge bundle bucketing ────────────────────────────────────────────────

def test_judge_bundle_separates_buckets():
    chunks = [_chunk(f"c{i}", f"D{i % 5}") for i in range(60)]
    risk = {"c1", "c2", "c3"}
    bundle = L6.build_chunk_judge_bundle(chunks, risk, n_repr=20, n_risk=3, n_rare=5)
    buckets = {b["bucket"] for b in bundle}
    assert buckets == {"representative", "risk"}
    assert all(b["kind"] == "chunk" for b in bundle)
    assert all(b["rubric_version"] == L6.RUBRIC_VERSION for b in bundle)
    # risk items must not also appear as representative (no double counting)
    repr_ids = {b["item_id"] for b in bundle if b["bucket"] == "representative"}
    risk_ids = {b["item_id"] for b in bundle if b["bucket"] == "risk"}
    assert repr_ids.isdisjoint(risk_ids)


def test_merge_chunk_panel_keeps_buckets_separate():
    bundle = [{"item_id": "a", "bucket": "representative", "chunk_type": "text_chunk",
               "rubric_version": "chunk_rubric_v1"},
              {"item_id": "b", "bucket": "risk", "chunk_type": "step_card",
               "rubric_version": "chunk_rubric_v1"}]
    panels = [{"judge": "j1", "verdicts": [
                {"item_id": "a", "self_containedness": 5, "coherence": 5, "type_fidelity": 5,
                 "truncation": 5, "overall": 5, "verdict": "pass", "rationale": "ok"},
                {"item_id": "b", "self_containedness": 2, "coherence": 2, "type_fidelity": 2,
                 "truncation": 2, "overall": 2, "verdict": "fail", "rationale": "cut"}]}]
    m = judge.merge_chunk_panel(bundle, panels)
    assert m["representative"]["pass_rate_overall_ge4"] == 1.0
    assert m["risk_enriched"]["pass_rate_overall_ge4"] == 0.0
    assert set(m["by_chunk_type"]) == {"text_chunk", "step_card"}
