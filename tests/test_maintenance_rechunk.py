"""Maintenance re-chunk: freeze existing classification/routing, never call the LLM classifier.
Regression for the 2026-06-15 batch where re-classifying flipped chunk families run-to-run."""
import pytest
from unittest.mock import patch

from opensearch_pipeline.pipeline_nodes import (
    node_classify_and_risk_assess,
    node_chunk_documents,
)

LLM_PATH = "opensearch_pipeline.pipeline_nodes.run_gemini_classification"


def _doc(doc_id, cat1="sop", cat2="safety_sop", text="第一条 内容。\n第二条 内容。"):
    return {"doc_id": doc_id, "version_no": 1, "text": text, "source_key": f"public/{doc_id}.docx"}


# (b) maintenance mode → 0 classifier calls, category comes from the frozen manifest
@patch(LLM_PATH)
def test_maintenance_makes_zero_classifier_calls(mock_llm):
    docs = [_doc("d1")]
    ctx = {"canonicals": docs, "simulate_db": True, "simulate_api": False,
           "frozen_routing": {"d1": {"category_l1": "sop", "category_l2": "inspection_sop", "split_mode": "step"}}}
    node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0, "maintenance must NOT call the LLM classifier"
    assert docs[0]["category_l1"] == "sop" and docs[0]["category_l2"] == "inspection_sop"
    assert docs[0]["classification_status"] == "FROZEN_MAINTENANCE"


# (c) missing frozen routing → fail closed BEFORE any DB touch
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
@patch(LLM_PATH)
def test_maintenance_missing_routing_fails_closed_no_write(mock_llm, mock_db):
    ctx = {"canonicals": [_doc("d1"), _doc("d2")], "simulate_db": False,
           "frozen_routing": {"d1": {"category_l1": "sop", "category_l2": "others"}}}  # d2 missing
    with pytest.raises(RuntimeError, match="fail closed"):
        node_classify_and_risk_assess(ctx)
    assert mock_db.call_count == 0, "must not open a DB connection on a missing-routing abort"
    assert mock_llm.call_count == 0, "must not reclassify on abort"


# (d) normal ingestion (no frozen_routing) still uses the LLM classifier
@patch(LLM_PATH)
def test_normal_ingestion_still_uses_classifier(mock_llm):
    mock_llm.return_value = {"category_l1": "sop", "category_l2": "safety_sop", "faq_eligible": False,
                             "confidence": 0.9, "llm_risk_level": "low", "summary": "s"}
    docs = [_doc("d1")]
    ctx = {"canonicals": docs, "simulate_db": True, "simulate_api": False}  # NO frozen_routing
    node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 1, "normal path must call the classifier"
    assert docs[0]["category_l1"] == "sop"


# (e) the 3 docs that drifted in the batch get their FROZEN (rebuild) category, not a re-roll
@patch(LLM_PATH)
def test_three_drift_docs_use_frozen_category(mock_llm):
    frozen = {
        "DOC_PRODUCTION_20260513120642_14DFDF": {"category_l1": "sop", "category_l2": "inspection_sop", "split_mode": "step"},
        "DOC_QUALITY_20260611201419_7FA6C7": {"category_l1": "sop", "category_l2": "others", "split_mode": "step"},
        "DOC_HR_20260514123016_8BD7C3": {"category_l1": "policy", "category_l2": "hr_policy", "split_mode": "clause"},
    }
    docs = [_doc(d) for d in frozen]
    ctx = {"canonicals": docs, "simulate_db": True, "simulate_api": False, "frozen_routing": frozen}
    node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0
    for d in docs:
        assert (d["category_l1"], d["category_l2"]) == (
            frozen[d["doc_id"]]["category_l1"], frozen[d["doc_id"]]["category_l2"])


# (a) same canonical + frozen category re-chunked twice → identical count / type mix / mode
def test_rechunk_is_deterministic_with_frozen_category():
    text = ("第一条 公司安全规定如下。员工必须遵守。\n第二条 违规处理办法。\n"
            "第三条 奖励制度说明。\n") * 25

    def run_once():
        doc = {"doc_id": "d_det", "version_no": 1, "text": text,
               "category_l1": "policy", "category_l2": "hr_policy", "file_ext": "txt"}
        ctx = {"canonicals": [doc], "split_mode": "dynamic"}
        node_chunk_documents(ctx)
        chunks = ctx["chunks"]
        mix = {}
        for c in chunks:
            mix[c.chunk_type] = mix.get(c.chunk_type, 0) + 1
        return len(chunks), tuple(sorted(mix.items()))

    n1, mix1 = run_once()
    n2, mix2 = run_once()
    assert n1 == n2 and mix1 == mix2, f"re-chunk must be deterministic: {n1}/{mix1} vs {n2}/{mix2}"
    assert n1 > 0
