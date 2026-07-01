# -*- coding: utf-8 -*-
"""Regression tests for the dormant VLM-rebuild review fixes #8 and #9.

These paths are gated behind RAG_REBUILD_ENABLED (default off); the tests force
enabled=True / set the flags directly to exercise the logic.

#8  vlm_rebuilder must not lose a whole page rebuild because one VLM block has a
    non-integer `level` (e.g. "一", "1.", "H1") — safe level parse + per-block guard.
#9  a cost-gate-denied document must be skipped downstream (no chunks indexed),
    not left in a split state (RDS quarantined but chunks pushed to the index).
"""
import os

os.environ.setdefault("RAG_SIMULATE", "true")

from types import SimpleNamespace

from opensearch_pipeline.config import RebuildConfig
from opensearch_pipeline.extraction import vlm_rebuilder as vlmr
from opensearch_pipeline.extraction.cost_breaker import CostBreaker
from opensearch_pipeline.extraction.schema import ExtractionResult
import opensearch_pipeline.pipeline_nodes as pn


def _rebuild_cfg(**rb):
    defaults = dict(enabled=True, max_pages=50, doc_budget_rmb=5.0, run_budget_rmb=200.0,
                    ocr_page_rmb=0.06, vlm_image_rmb=0.04)
    defaults.update(rb)
    return SimpleNamespace(
        rebuild=RebuildConfig(**defaults),
        ocr=SimpleNamespace(max_ocr_pages=5, vlm_model="qwen-vl", model="qwen-vl",
                            api_key="k", api_base_url="https://dashscope.aliyuncs.com"),
        environment="test", simulate_db=True,
    )


def _pdf_result():
    return ExtractionResult(doc_id="D", version_no=1, source_key="raw/x.pdf", file_ext="pdf",
                            extract_method="pypdf", title="x", text="", text_length=0,
                            blocks=[], page_count=1)


# ─────────────────────────────────────────────────────────────────────
# #8  malformed VLM `level` must not discard the page rebuild
# ─────────────────────────────────────────────────────────────────────
class TestVlmRebuilderSafeLevel:
    def test_safe_int_level_parses_or_defaults(self):
        f = vlmr._safe_int_level
        assert f("2") == 2
        assert f("1.") == 1
        assert f(3) == 3
        assert f("一") == 0      # Chinese numeral → default, no crash
        assert f("H1") == 0
        assert f(None) == 0
        assert f("") == 0

    def test_malformed_level_block_does_not_lose_page(self, monkeypatch):
        cfg = _rebuild_cfg(enabled=True)
        monkeypatch.setattr(vlmr, "_page_char_counts", lambda p: [0])  # page 0 escalates
        monkeypatch.setattr(vlmr, "_render_page_image", lambda p, i, **k: (b"img", "image/png"))
        monkeypatch.setattr(vlmr, "_vlm_reconstruct_page", lambda *a, **k: [
            {"type": "heading", "text": "第一章", "level": "一"},   # malformed level
            {"type": "paragraph", "text": "正文内容描述"},
        ])
        task = {"local_path": "/tmp/x.pdf", "doc_id": "D", "version_no": 1}
        out = vlmr.maybe_rebuild_pdf(task, _pdf_result(), cfg, breaker=CostBreaker(cfg))
        texts = [b.text for b in out.blocks if getattr(b, "source", "") == "multimodal"]
        assert "第一章" in texts and "正文内容描述" in texts, "both blocks must survive; no crash"

    def test_rebuild_refunds_when_no_blocks_produced(self, monkeypatch):
        # gate allowed but VLM returns nothing → reservation must be refunded
        cfg = _rebuild_cfg(enabled=True)
        br = CostBreaker(cfg)
        monkeypatch.setattr(vlmr, "_page_char_counts", lambda p: [0, 0])
        monkeypatch.setattr(vlmr, "_render_page_image", lambda p, i, **k: (b"img", "image/png"))
        monkeypatch.setattr(vlmr, "_vlm_reconstruct_page", lambda *a, **k: [])  # nothing rebuilt
        task = {"local_path": "/tmp/x.pdf", "doc_id": "D", "version_no": 1}
        vlmr.maybe_rebuild_pdf(task, _pdf_result(), cfg, breaker=br)
        assert br.run_total_rmb == 0.0, "unspent reservation must be refunded when no blocks produced"


# ─────────────────────────────────────────────────────────────────────
# #9  cost-gate-denied document must be skipped downstream (no split state)
# ─────────────────────────────────────────────────────────────────────
class TestCostQuarantineSkip:
    def test_rebuild_deny_sets_cost_quarantined(self, monkeypatch):
        # doc too big for per-doc budget → gate denies → result flagged cost_quarantined
        cfg = _rebuild_cfg(enabled=True, doc_budget_rmb=0.01)  # any escalate page exceeds
        monkeypatch.setattr(vlmr, "_page_char_counts", lambda p: [0, 0, 0])
        task = {"local_path": "/tmp/x.pdf", "doc_id": "D", "version_no": 1, "owner_dept": "sales"}
        out = vlmr.maybe_rebuild_pdf(task, _pdf_result(), cfg, breaker=CostBreaker(cfg))
        assert out.cost_quarantined is True

    def test_redact_node_quarantines_cost_flagged_doc(self):
        ctx = {"canonicals": [{"doc_id": "D", "text": "内容", "risk_level": "low",
                               "cost_quarantined": True}]}
        pn.node_redact_or_quarantine(ctx)
        assert ctx["canonicals"][0]["redaction_action"] == "QUARANTINE"

    def test_redact_node_clean_when_not_flagged(self):
        ctx = {"canonicals": [{"doc_id": "D", "text": "干净内容", "risk_level": "low"}]}
        pn.node_redact_or_quarantine(ctx)
        assert ctx["canonicals"][0]["redaction_action"] == "CLEAN"

    def test_build_canonical_propagates_cost_quarantined(self):
        r = _pdf_result()
        r.text = "t"
        r.cost_quarantined = True
        ctx = {"extractions": [r]}
        pn.node_build_canonical(ctx)
        assert ctx["canonicals"][0]["cost_quarantined"] is True

    def test_cost_quarantined_doc_not_chunked(self):
        # the existing QUARANTINE skip means a cost-quarantined doc produces no chunks
        ctx = {"canonicals": [{"doc_id": "D", "version_no": 1, "file_ext": "pdf",
                               "text": "一些内容文本用于切块", "redaction_action": "QUARANTINE",
                               "title": "x", "category_l1": "", "category_l2": "", "blocks": []}]}
        pn.node_chunk_documents(ctx)
        assert all(c.doc_id != "D" for c in ctx["chunks"]), "quarantined doc must not be chunked"

    def test_optional_refine_cost_deny_does_not_quarantine(self):
        # optional table-refine cost-deny must keep the doc usable (NOT cost_quarantined):
        # a successfully-extracted doc must not be dropped just because optional polish was skipped.
        from opensearch_pipeline.extraction.schema import ExtractedBlock
        cfg = _rebuild_cfg(enabled=True, refine_tables=True, run_budget_rmb=0.0)  # any cost denied
        result = _pdf_result()
        result.text = "正文内容（原生表格可用）"
        result.blocks = [ExtractedBlock(block_type="table", text="| 单列 |", page_num=1)]  # mangled → target
        task = {"local_path": "/tmp/x.pdf", "doc_id": "D", "version_no": 1}
        out = vlmr.maybe_refine_tables(task, result, cfg, breaker=CostBreaker(cfg))
        assert out.cost_quarantined is False, "optional refine cost-deny must NOT quarantine a usable doc"

    def test_cost_quarantined_survives_canonical_json_roundtrip(self):
        # production stage-2 rebuilds the canonical from OSS JSON; the flag must survive
        # node_build_canonical's json.dumps and the stage-2 loader's content_json.get().
        import json as _json
        r = _pdf_result()
        r.text = "t"
        r.cost_quarantined = True
        ctx = {"extractions": [r]}
        pn.node_build_canonical(ctx)
        canonical = ctx["canonicals"][0]
        content_json = _json.loads(_json.dumps(canonical))  # OSS write → stage-2 read
        assert content_json.get("cost_quarantined", False) is True

    def test_xlsx_layout_type_survives_stage2_reload(self):
        """F-2：DAG1 把 xlsx_layout_type 写进 canonical JSON，且生产 Stage-2 loader 必须回读它——
        否则重载后为空 → DAG2 回退重分类 → procedure_image_guide 被误判 normal_spreadsheet、
        step_card/图片绑定结构静默丢失。"""
        import json as _json
        import inspect
        from opensearch_pipeline import dataworks_orchestrator as dwo
        # (1) DAG1 侧：xlsx_layout_type 经 json.dumps 落盘后仍可回读
        r = _pdf_result()
        r.file_ext = "xlsx"
        r.text = "一些单元格内容"
        r.xlsx_layout_type = "procedure_image_guide"
        ctx = {"extractions": [r]}
        pn.node_build_canonical(ctx)
        content_json = _json.loads(_json.dumps(ctx["canonicals"][0]))  # OSS write → stage-2 read
        assert content_json.get("xlsx_layout_type") == "procedure_image_guide"
        # (2) Stage-2 loader 侧：canonical_doc 白名单必须回读 xlsx_layout_type + filename（合同）
        src = inspect.getsource(dwo)
        assert '"xlsx_layout_type": content_json.get("xlsx_layout_type")' in src, \
            "orchestrator Stage-2 canonical_doc 未回读 xlsx_layout_type（F-2 回归）"
        assert '"filename": content_json.get("filename") or title' in src
