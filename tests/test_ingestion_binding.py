# -*- coding: utf-8 -*-
"""
test_ingestion_binding.py — eval_harness.binding.ingestion_binding 核心单测

钉死:matcher 行为(keyword recall + density)、pred_refs 提取、聚合契约、
fail-open(单 doc 失败不阻断)、only_fmt 过滤、img_dup_factor 聚合。
"""
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import pytest

from eval_harness.binding.ref_keys import ImageRef
from eval_harness.binding.gt_loader import GtChunk, GtDoc
from eval_harness.binding import ingestion_binding as ib


# ── 假 chunk 形状(Chunk dataclass 子集)──────────────────────

@dataclass
class FakeChunk:
    chunk_text: str
    chunk_type: str = "text_chunk"
    page_num: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# ── matcher 行为 ─────────────────────────────────────────────

def test_match_finds_density_winner():
    """覆盖集中(recall ≥ 0.3)→ density 最大者(短文本 + 同 hits 偏好)。"""
    chunks = [
        FakeChunk("扫码 报检 步骤", chunk_type="step_card"),                 # short, dense
        FakeChunk("扫码 报检 步骤 " + "无关词 " * 50, chunk_type="procedure_parent"),  # long, low density
    ]
    gt = GtChunk(label="3.1", chunk_type="step_card",
                 keywords=["扫码", "报检"])
    matched = ib._match_gt_chunk_to_produced(gt, chunks)
    assert matched is chunks[0], "应优先 density 高的 step_card,不被 procedure_parent 抢"


def test_match_returns_best_recall_fallback_when_no_cover():
    """没有 chunk 覆盖率 ≥ 0.3 时,回退最高 recall。"""
    chunks = [
        FakeChunk("含关键词A"),  # 1/3 = 33% > 0.3, 覆盖
        FakeChunk("空"),
    ]
    gt = GtChunk(label="x", chunk_type="step_card",
                 keywords=["关键词A", "关键词B", "关键词C"])
    matched = ib._match_gt_chunk_to_produced(gt, chunks)
    assert matched is chunks[0]


def test_match_returns_none_for_no_keywords():
    """GT 无 keywords → 返 None(不该参与匹配)。"""
    gt = GtChunk(label="x", chunk_type="step_card", keywords=[])
    assert ib._match_gt_chunk_to_produced(gt, [FakeChunk("abc")]) is None


# ── pred refs 提取 ────────────────────────────────────────────

def test_pred_refs_extract_docx():
    chunk = FakeChunk("...", extra={"image_refs": [
        {"image_index": 3}, {"image_index": 4}, {"image_index": 5},
    ]})
    refs = ib._pred_refs_from_chunk(chunk, "docx")
    assert len(refs) == 3
    assert {r.image_index for r in refs} == {3, 4, 5}


def test_pred_refs_extract_pdf():
    chunk = FakeChunk("...", extra={"image_refs": [
        {"page_num": 3, "image_index_in_page": 1},  # alias 路径
        {"page": 3, "in_page_idx": 2},               # 标准路径
    ]})
    refs = ib._pred_refs_from_chunk(chunk, "pdf")
    assert len(refs) == 2
    assert all(r.page == 3 for r in refs)
    assert {r.in_page_idx for r in refs} == {1, 2}


def test_pred_refs_extract_xlsx_anchor_row_alias():
    chunk = FakeChunk("...", extra={"image_refs": [
        {"anchor_row": 11}, {"anchor_row": 12},
    ]})
    refs = ib._pred_refs_from_chunk(chunk, "xlsx")
    assert {r.block_index for r in refs} == {11, 12}


def test_pred_refs_empty_for_no_extra():
    assert ib._pred_refs_from_chunk(FakeChunk("..."), "docx") == []


# ── all_step_card_refs(全文档平铺,for dup_factor)──────────

def test_all_step_card_refs_skips_non_step():
    chunks = [
        FakeChunk("a", chunk_type="step_card", extra={"image_refs": [{"image_index": 1}]}),
        FakeChunk("b", chunk_type="text_chunk", extra={"image_refs": [{"image_index": 99}]}),
        FakeChunk("c", chunk_type="step_card", extra={"image_refs": [{"image_index": 2}, {"image_index": 1}]}),
    ]
    refs = ib._all_step_card_refs(chunks, "docx")
    # 只取 step_card 的:1 + 2,1
    assert len(refs) == 3
    assert sorted(r.image_index for r in refs) == [1, 1, 2]


# ── evaluate_doc 端到端(monkeypatch extract+chunk)──────────

def test_evaluate_doc_perfect_match(monkeypatch):
    """GT step_card 期望 image 3,produced step_card 含 image 3 → Jaccard 1.0。"""
    chunks = [
        FakeChunk(
            chunk_text="扫码报检的核心步骤是把货号扫上",
            chunk_type="step_card",
            extra={"image_refs": [{"image_index": 3}]},
        ),
    ]
    monkeypatch.setattr(ib, "_extract_and_chunk", lambda *a, **kw: chunks)

    gt_doc = GtDoc(label="docx_sop", fmt="docx", doc_sha256=None,
                   extractor_version=None, manifest_path=None, degraded=False,
                   gt_chunks=[GtChunk(
                       label="3.1 扫码报检", chunk_type="step_card",
                       keywords=["扫码", "报检", "货号"],
                       expected_image_refs=[ImageRef(fmt="docx", image_index=3)],
                       has_strong_refs=True,
                   )])
    result = ib.evaluate_doc("docx_sop", gt_doc, "/fake/path.docx")
    assert result["fmt"] == "docx"
    assert result["mean_jaccard"] == 1.0
    assert result["n_strong_chunks"] == 1
    assert result["per_chunk"][0]["jaccard"] == 1.0


def test_evaluate_doc_partial_match(monkeypatch):
    """期望 {3, 4},实际 {3, 5} → Jaccard = 1/3。"""
    chunks = [FakeChunk(
        chunk_text="扫码 报检",
        chunk_type="step_card",
        extra={"image_refs": [{"image_index": 3}, {"image_index": 5}]},
    )]
    monkeypatch.setattr(ib, "_extract_and_chunk", lambda *a, **kw: chunks)
    gt_doc = GtDoc(label="docx_sop", fmt="docx", doc_sha256=None,
                   extractor_version=None, manifest_path=None, degraded=False,
                   gt_chunks=[GtChunk(
                       label="x", chunk_type="step_card",
                       keywords=["扫码", "报检"],
                       expected_image_refs=[
                           ImageRef(fmt="docx", image_index=3),
                           ImageRef(fmt="docx", image_index=4),
                       ],
                       has_strong_refs=True,
                   )])
    result = ib.evaluate_doc("docx_sop", gt_doc, "/fake/path.docx")
    assert abs(result["mean_jaccard"] - 1 / 3) < 1e-9


def test_evaluate_doc_weak_gt_recorded_but_not_scored(monkeypatch):
    """弱 GT(has_strong_refs=False)入 per_chunk 但不算入 mean_jaccard。"""
    chunks = [FakeChunk(
        chunk_text="装配步骤",
        chunk_type="step_card",
        extra={"image_refs": [{"page_num": 3, "image_index_in_page": 1}]},
    )]
    monkeypatch.setattr(ib, "_extract_and_chunk", lambda *a, **kw: chunks)
    gt_doc = GtDoc(label="pdf_sop", fmt="pdf", doc_sha256=None,
                   extractor_version=None, manifest_path=None, degraded=False,
                   gt_chunks=[GtChunk(
                       label="x", chunk_type="step_card",
                       keywords=["装配", "步骤"],
                       expected_image_refs=[ImageRef(fmt="pdf", page=3)],  # weak
                       has_strong_refs=False,
                   )])
    result = ib.evaluate_doc("pdf_sop", gt_doc, "/fake/path.pdf")
    assert result["mean_jaccard"] is None  # 没 strong 分数
    assert result["per_chunk"][0]["weak"] is True
    assert result["per_chunk"][0]["n_pred_refs"] == 1


def test_evaluate_doc_fail_open_on_extract_error(monkeypatch):
    """extract/chunk 抛异常 → 单 doc 失败入 error,不抛出。"""
    def boom(*a, **kw):
        raise RuntimeError("simulated extract failure")
    monkeypatch.setattr(ib, "_extract_and_chunk", boom)
    gt_doc = GtDoc(label="x", fmt="docx", doc_sha256=None,
                   extractor_version=None, manifest_path=None, degraded=False,
                   gt_chunks=[GtChunk(label="a", chunk_type="step_card", keywords=["a"])])
    result = ib.evaluate_doc("x", gt_doc, "/fake/p.docx")
    assert "error" in result
    assert "simulated extract" in result["error"]


# ── run() 全套聚合 + only_fmt 过滤 ───────────────────────────

def _write_gt_json(payload):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return path


def test_run_aggregates_by_fmt_and_emits_main_gates(monkeypatch, tmp_path):
    """run() 出 binding_jaccard_<fmt> + per_fmt + img_dup_factor_p95 主闸键。"""
    # Mock extract+chunk:让 PDF doc 出完美匹配
    def fake_extract(label, fmt, doc_path):
        return [FakeChunk(
            chunk_text="装配 步骤",
            chunk_type="step_card",
            extra={"image_refs": [{"page_num": 3, "image_index_in_page": 1}]},
        )]
    monkeypatch.setattr(ib, "_extract_and_chunk", fake_extract)

    # 模拟 doc 文件存在
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "pdf_sop.pdf").write_bytes(b"")

    gt_path = _write_gt_json({
        "_meta": {"image_ref_scheme": "v1"},
        "pdf_sop": {
            "_doc_meta": {},
            "gt_chunks": [{
                "label": "x", "chunk_type": "step_card",
                "keywords": ["装配", "步骤"],
                "expected_image_refs": [{"page": 3, "in_page_idx": 1}],
            }],
        },
    })

    try:
        result = ib.run([gt_path], str(docs_dir))
        determ = result["deterministic"]
        # 主闸键存在
        assert determ["binding_jaccard_pdf"] == 1.0
        assert determ["binding_jaccard_docx"] is None  # 未跑该格式 = None
        assert determ["binding_jaccard_xlsx"] is None
        assert determ["binding_jaccard_pptx"] is None
        assert determ["img_dup_factor_p95"] == 1.0
        # per_fmt 包含 pdf 详情
        assert determ["per_fmt"]["pdf"]["n_docs"] == 1
        assert determ["per_fmt"]["pdf"]["n_strong_chunks"] == 1
        assert determ["per_fmt"]["pdf"]["mean_jaccard"] == 1.0
        # judge_bundle_binding 出条目
        assert len(result["judge_bundle_binding"]) == 1
        assert result["judge_bundle_binding"][0]["qid"] == "pdf_sop::x"
    finally:
        os.unlink(gt_path)


def test_run_only_fmt_filters_other_formats(monkeypatch, tmp_path):
    monkeypatch.setattr(ib, "_extract_and_chunk",
                        lambda *a, **kw: [FakeChunk("test", chunk_type="step_card")])
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "pdf_sop.pdf").write_bytes(b"")
    (docs_dir / "docx_sop.docx").write_bytes(b"")

    gt_path = _write_gt_json({
        "_meta": {},
        "pdf_sop": {"_doc_meta": {}, "gt_chunks": [{"label": "x", "keywords": ["test"]}]},
        "docx_sop": {"_doc_meta": {}, "gt_chunks": [{"label": "y", "keywords": ["test"]}]},
    })
    try:
        result = ib.run([gt_path], str(docs_dir), only_fmt="pdf")
        labels = {d["label"] for d in result["per_doc"]}
        assert labels == {"pdf_sop"}  # docx_sop 被过滤掉
    finally:
        os.unlink(gt_path)


def test_run_degraded_doc_excluded_from_main_gate(monkeypatch, tmp_path):
    """degraded GT doc 仍跑 per_doc 但不算入 per_fmt 的 mean_jaccard 分子。"""
    monkeypatch.setattr(ib, "_extract_and_chunk",
                        lambda *a, **kw: [FakeChunk(
                            "test 数据", chunk_type="step_card",
                            extra={"image_refs": [{"image_index": 99}]}
                        )])
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "xlsx_inspect.xlsx").write_bytes(b"")

    gt_path = _write_gt_json({
        "_meta": {"skip_in_binding": ["xlsx_inspect"]},
        "xlsx_inspect": {
            "_doc_meta": {},
            "gt_chunks": [{"label": "x", "chunk_type": "step_card",
                           "keywords": ["test", "数据"],
                           "expected_image_refs": [{"block_index": 1}]}],
        },
    })
    try:
        result = ib.run([gt_path], str(docs_dir))
        # xlsx_inspect 仍在 per_doc(供趋势监控)
        assert any(d["label"] == "xlsx_inspect" for d in result["per_doc"])
        # 但 per_fmt.xlsx.mean_jaccard 是 None(degraded 不计入)
        assert result["deterministic"]["per_fmt"]["xlsx"]["n_degraded_docs"] == 1
        assert result["deterministic"]["per_fmt"]["xlsx"]["mean_jaccard"] is None
    finally:
        os.unlink(gt_path)


def test_run_missing_doc_records_error(monkeypatch, tmp_path):
    """源文档不存在 → errors 列表里有记录,不阻断。"""
    gt_path = _write_gt_json({
        "_meta": {},
        "pdf_missing": {"_doc_meta": {}, "gt_chunks": [{"label": "x", "keywords": ["a"]}]},
    })
    try:
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        result = ib.run([gt_path], str(docs_dir))
        assert any("源文档不存在" in e for e in result["deterministic"]["errors"])
    finally:
        os.unlink(gt_path)


def test_run_empty_gt_files():
    """空 gt_files 列表 → 全空 + 全部主闸 None。"""
    result = ib.run([], "/nonexistent")
    determ = result["deterministic"]
    assert result["per_doc"] == []
    for fmt in ("docx", "pdf", "xlsx", "pptx"):
        assert determ[f"binding_jaccard_{fmt}"] is None
    assert determ["img_dup_factor_p95"] is None


# ── _percentile 工具 ────────────────────────────────────────

def test_percentile_basic():
    assert ib._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) == pytest.approx(4.8)


def test_percentile_empty_returns_none():
    assert ib._percentile([], 0.5) is None


def test_percentile_single():
    assert ib._percentile([3.14], 0.5) == 3.14
