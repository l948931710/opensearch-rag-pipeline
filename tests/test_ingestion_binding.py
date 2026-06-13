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


# ── _extract_step_no_from_label(D8 Phase 3 finding 3)──────────

@pytest.mark.parametrize("label,expected", [
    # 中文"步骤"前缀(主流形态:PDF / docx_water / xlsx_inspect)
    ("步骤3.1 U8扫码报检", (3, "3.1")),
    ("步骤 3.1 U8扫码报检", (3, "3.1")),
    ("步骤2 交货单分类", (2, None)),
    ("步骤 2", (2, None)),
    ("步骤4 报检填写(父)", (4, None)),
    ("步骤4.1 填写设备班次", (4, "4.1")),
    # 英文"Step"前缀
    ("Step 3.1", (3, "3.1")),
    ("step 3", (3, None)),
    # 纯 dotted(docx_sop / docx_manual 形态)
    ("4.2.1 外观检验", (4, "4.2.1")),
    ("4.2.3.1 3M胶带脱色测试", (4, "4.2.3.1")),
    ("1.1 采购发票录入 — 普通/专用发票选择与8步操作", (1, "1.1")),
    # 不命中(应返 None, None)
    ("前言(作业前提+说明)", (None, None)),
    ("目的-范围-职责", (None, None)),
    ("职责 — QC当班检验职责", (None, None)),
    ("流程5a — 纸箱标签盖章印刷", (None, None)),  # 流程N + 字母后缀 = clause,不抽
    ("", (None, None)),
])
def test_extract_step_no_from_label(label, expected):
    assert ib._extract_step_no_from_label(label) == expected


# ── matcher 次级 step_no/sec_no 过滤(D8 Phase 3 finding 3 修)──

def test_match_prefers_sec_no_match_within_typed_pool():
    """sec_no 完全匹配的 chunk 优先于其他 typed candidate(最特异)。"""
    chunks = [
        # density 短文本父(无 section_no)— 会被旧逻辑误选
        FakeChunk("步骤4：报检处填相应 U8 扫码 报检", chunk_type="step_card",
                  extra={"step_no": 4}),
        # 真 sec_no=4.1 子(中等长度)
        FakeChunk("4.1 按交货单填设备班次数量 U8 扫码 报检 处理 操作 完成 步骤",
                  chunk_type="step_card",
                  extra={"step_no": 4, "section_no": "4.1"}),
    ]
    gt = GtChunk(label="步骤4.1 填写设备班次", chunk_type="step_card",
                 keywords=["U8", "扫码", "报检"])
    matched = ib._match_gt_chunk_to_produced(gt, chunks)
    assert matched is chunks[1], "sec_no='4.1' 应锁回真子 chunk,而非短文本父"


def test_match_prefers_step_no_when_sec_no_no_match_recall_wins():
    """sec_no 无匹配但 step_no 匹配 → step_no 过滤池内 recall-max 胜(D8 Phase 3 实证场景)。

    pdf_sop GT 3.1 实证:step_no=4 父短文本偶然 d 高 → 撞掉真 step_no=3 main。
    extract step_no=3 过滤后,真 main(recall=1.0)胜过同 pool 内 sub=2 sec=3.2
    (recall<1.0 但 density 可能更高)。
    """
    chunks = [
        # 短文本父(step=4)偶然含 keyword,旧 density-max 会抢戏
        FakeChunk("步骤4：报检处填 U8 扫码 报检",
                  chunk_type="step_card", extra={"step_no": 4}),
        # step=3 sub=2 sec=3.2:短文本但 recall 低
        FakeChunk("3.2 扫码枪扫描条形码",
                  chunk_type="step_card",
                  extra={"step_no": 3, "section_no": "3.2"}),
        # 真 step=3 main:长文本,recall 高
        FakeChunk("步骤3：报检 U8 扫码 业务导航 质量管理 大量上下文充实长度 " * 10,
                  chunk_type="step_card", extra={"step_no": 3}),
    ]
    gt = GtChunk(label="步骤3.1 U8扫码报检", chunk_type="step_card",
                 keywords=["U8", "扫码", "报检", "业务导航", "质量管理"])
    matched = ib._match_gt_chunk_to_produced(gt, chunks)
    assert matched is chunks[2], "step_no=3 过滤池内 recall-max 应选真 main,而非 sub=2 sec=3.2"


def test_match_falls_back_density_when_step_no_not_in_any_chunk():
    """GT label 抽出 step_no=3 但无 chunk 含 step_no=3 → 回退当前 typed density-max(向后兼容)。"""
    chunks = [
        FakeChunk("扫码 报检 步骤", chunk_type="step_card",
                  extra={"step_no": 99}),  # step_no 不匹配
        FakeChunk("扫码 报检 步骤 " + "无关词 " * 50, chunk_type="step_card",
                  extra={"step_no": 88}),
    ]
    gt = GtChunk(label="步骤3.1", chunk_type="step_card",
                 keywords=["扫码", "报检"])
    matched = ib._match_gt_chunk_to_produced(gt, chunks)
    assert matched is chunks[0], "step_no 全无匹配 → 回退 density-max,短文本胜"


def test_match_falls_back_density_when_label_has_no_step_no():
    """GT label 抽不出步骤号("前言..." 形态)→ 走当前 typed density-max(零回归)。"""
    chunks = [
        FakeChunk("作业前提 交货单 标识卡 报检", chunk_type="text_chunk",
                  extra={"step_no": 0}),
        FakeChunk("作业前提 交货单 标识卡 报检 " + "无关 " * 30,
                  chunk_type="text_chunk", extra={"step_no": 0}),
    ]
    gt = GtChunk(label="前言(作业前提+说明)", chunk_type="text_chunk",
                 keywords=["作业前提", "交货单", "标识卡", "报检"])
    matched = ib._match_gt_chunk_to_produced(gt, chunks)
    assert matched is chunks[0], "无步骤号 → density-max,短文本胜"


def test_match_step_no_filter_only_within_typed_pool():
    """step_no 过滤只在 chunk_type 同类型 pool 内生效;不同类型不受影响。"""
    chunks = [
        # text_chunk(GT 类型不同)— 即使 step_no 匹配也不该被选
        FakeChunk("U8 扫码 报检 业务导航 质量管理",
                  chunk_type="text_chunk", extra={"step_no": 3}),
        # step_card 同类型 但 step_no 不匹配
        FakeChunk("U8 扫码 报检 大量充实文本 " * 5,
                  chunk_type="step_card", extra={"step_no": 99}),
    ]
    gt = GtChunk(label="步骤3.1", chunk_type="step_card",
                 keywords=["U8", "扫码", "报检"])
    matched = ib._match_gt_chunk_to_produced(gt, chunks)
    assert matched is chunks[1], "step_no 过滤限定在 typed pool 内;不同 type 不抢戏"


def test_match_sec_no_takes_precedence_over_step_no():
    """sec_no 匹配优先于仅 step_no 匹配(更特异)。"""
    chunks = [
        # 仅 step_no=4 匹配(无 section_no)
        FakeChunk("步骤4 报检 填写 U8 扫码", chunk_type="step_card",
                  extra={"step_no": 4}),
        # sec_no=4.1 匹配(更特异)
        FakeChunk("4.1 设备 班次 U8 扫码 报检", chunk_type="step_card",
                  extra={"step_no": 4, "section_no": "4.1"}),
    ]
    gt = GtChunk(label="步骤4.1 填写设备班次", chunk_type="step_card",
                 keywords=["U8", "扫码", "报检"])
    matched = ib._match_gt_chunk_to_produced(gt, chunks)
    assert matched is chunks[1], "sec_no='4.1' 完全匹配 > 仅 step_no=4 匹配"


# ── pred refs 提取 ────────────────────────────────────────────

def test_pred_refs_extract_docx():
    chunk = FakeChunk("...", extra={"image_refs": [
        {"image_index": 3}, {"image_index": 4}, {"image_index": 5},
    ]})
    refs = ib._pred_refs_from_chunk(chunk, "docx")
    assert len(refs) == 3
    assert {r.image_index for r in refs} == {3, 4, 5}


def test_pred_refs_extract_pdf():
    """PDF v2 坐标系:用 image_index 主键(extractor 全文 1-based),page 字段辅助。"""
    chunk = FakeChunk("...", extra={"image_refs": [
        {"page_num": 3, "image_index": 9},
        {"page": 3, "image_index": 10},
    ]})
    refs = ib._pred_refs_from_chunk(chunk, "pdf")
    assert len(refs) == 2
    assert all(r.page == 3 for r in refs)
    assert {r.image_index for r in refs} == {9, 10}


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
            # PDF 坐标系 v2:用 extractor 的全文 image_index(2026-06-12 修正)
            extra={"image_refs": [{"image_index": 10, "page_num": 3}]},
        )]
    monkeypatch.setattr(ib, "_extract_and_chunk", fake_extract)

    # 模拟 doc 文件存在
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "pdf_sop.pdf").write_bytes(b"")

    gt_path = _write_gt_json({
        "_meta": {"image_ref_scheme": "v2"},
        "pdf_sop": {
            "_doc_meta": {},
            "gt_chunks": [{
                "label": "x", "chunk_type": "step_card",
                "keywords": ["装配", "步骤"],
                "expected_image_refs": [{"image_index": 10, "page": 3}],
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


# ── D6 image_binding bundle 过滤(显式负例不送评)──────────────

def test_judge_bundle_skips_explicit_negative_cases(monkeypatch, tmp_path):
    """D6 改进:GT 标 []+pred 空 = 图无关 case,不入 judge_bundle_binding(避免
    Claude ib=3 中性把 image_binding 均值拖低,plan 抢救点)。

    GT 含图 OR pred 含图(cross-bind/over-attach 真 bug)仍送评。
    """
    # 3 个 chunks:含图+正确、显式负例+pred 空、显式负例+pred 含图(over-attach)
    chunks = [
        FakeChunk(chunk_text="扫码 步骤", chunk_type="step_card",
                  extra={"image_refs": [{"image_index": 3}]}),
        FakeChunk(chunk_text="前言 文字", chunk_type="text_chunk",
                  extra={"image_refs": []}),
        FakeChunk(chunk_text="补充 说明", chunk_type="step_card",
                  extra={"image_refs": [{"image_index": 99}]}),  # over-attach
    ]
    monkeypatch.setattr(ib, "_extract_and_chunk", lambda *a, **kw: chunks)
    gt_doc = GtDoc(label="docx_sop", fmt="docx", doc_sha256=None,
                   extractor_version=None, manifest_path=None, degraded=False,
                   gt_chunks=[
                       GtChunk(label="step1", chunk_type="step_card",
                               keywords=["扫码", "步骤"],
                               expected_image_refs=[ImageRef(fmt="docx", image_index=3)],
                               has_strong_refs=True),
                       GtChunk(label="前言", chunk_type="text_chunk",
                               keywords=["前言", "文字"],
                               expected_image_refs=[],
                               has_strong_refs=True),
                       GtChunk(label="补充", chunk_type="step_card",
                               keywords=["补充", "说明"],
                               expected_image_refs=[],
                               has_strong_refs=True),
                   ])
    result = ib.evaluate_doc("docx_sop", gt_doc, "/fake/p.docx")
    # 3 chunks 都入 mean_jaccard(strong path)— GT 显式 + matched
    assert result["n_strong_chunks"] == 3
    # 但 judge_items 只入 2 个:step1(GT 含图)+ 补充(over-attach pred 含图)
    qids = [it["qid"] for it in result["judge_items"]]
    assert "docx_sop::step1" in qids
    assert "docx_sop::补充" in qids
    assert "docx_sop::前言" not in qids   # 显式负例 + pred 空 → 不送评
    assert len(qids) == 2


# ── DOCX 独立 strict 路径(env-gated, production-faithful 复用 _extract_and_chunk)─
# 钉死:默认 OFF、env 开后才跑、strict 输出映射到 per_fmt['docx'] / binding_jaccard_docx、
# only_fmt 路由独立(PDF/XLSX 调用不触发 DOCX)、fixture 缺优雅返 None、PDF/XLSX 现
# 有路径完全不受影响、SOP 启发式正确把 admin_/hr_ 排除在主聚合外、has_gt_docx
# 最小门 >= 5 防 1 chunk 顶替。


def _make_fake_docx_strict_payload(mean=0.95, n_docs=3, n_strong_chunks=15,
                                   per_doc_extra=None):
    """模拟 _run_docx_strict_path 成功返回的 payload(完整 shape:含 img_dup_factor)。"""
    return {
        "by_fmt": {
            "n_docs": n_docs,
            "n_degraded_docs": 0,
            "n_strong_chunks": n_strong_chunks,
            "mean_jaccard": mean,
            "std_jaccard": 0.02,
            "_source": "strict_fixture",
        },
        "per_doc": [{
            "label": f"docx_strict::doc{i}.docx", "fmt": "docx",
            "degraded": False, "is_sop": True,
            "n_gt_chunks": 5, "n_strong_chunks": 5,
            "mean_jaccard": mean,
            "img_dup_factor": 1.0,
            "n_total_image_refs": 5,
            "n_step_cards": 5,
            "per_chunk": [], "judge_items": [], "_source": "strict_fixture",
            **(per_doc_extra or {}),
        } for i in range(n_docs)],
        "errors": [],
    }


def test_docx_sop_heuristic_classification():
    """_is_sop_docx:production_/oss_FL-*-WI-*/作业指导书/特定 it_ 操作手册 = SOP;
    admin_/hr_/eval_*_faq = 非 SOP。
    """
    sop_names = [
        "production_注塑事业部_FL-ZS-WI-002《奶茶杯测水试验》作业指导书.docx",
        "oss_FL-XS-WI-005《吸塑领料申请单打印》作业指导书-班组长.docx",
        "it_富岭U8+财务部操作手册.docx",
        "it_工资核算管理操作手册（2025年5月28日初版）.docx",
    ]
    non_sop_names = [
        "admin_A52吸烟管理制度.docx",
        "admin_食堂管理制度.docx",
        "hr_劳动合同模板.docx",
        "eval_company_faq.docx",
        "eval_it_support_faq.docx",
    ]
    for n in sop_names:
        assert ib._is_sop_docx(n), f"应识别为 SOP: {n}"
    for n in non_sop_names:
        assert not ib._is_sop_docx(n), f"应识别为非 SOP: {n}"


def test_docx_strict_disabled_by_default(monkeypatch, tmp_path):
    """env 未设 → _run_docx_strict_path 返 None,binding_jaccard_docx 保持 None。"""
    monkeypatch.delenv("EVAL_L4_DOCX_BINDING_ENABLE", raising=False)
    assert ib._docx_binding_enabled() is False
    assert ib._run_docx_strict_path() is None

    # run() 不喂 docx GT、env 未开 → binding_jaccard_docx 仍 None
    result = ib.run([], str(tmp_path))
    assert result["deterministic"]["binding_jaccard_docx"] is None


def test_docx_strict_enabled_via_env_writes_main_gate(monkeypatch, tmp_path):
    """env=true + strict 出数(n_strong_chunks >= 5)→ per_fmt['docx'] 用 strict 顶替。"""
    monkeypatch.setenv("EVAL_L4_DOCX_BINDING_ENABLE", "true")
    assert ib._docx_binding_enabled() is True

    monkeypatch.setattr(
        ib, "_run_docx_strict_path",
        lambda fixture_dir=None: _make_fake_docx_strict_payload(mean=0.986),
    )
    result = ib.run([], str(tmp_path))
    determ = result["deterministic"]
    assert determ["binding_jaccard_docx"] == 0.986
    assert determ["per_fmt"]["docx"]["n_docs"] == 3
    assert determ["per_fmt"]["docx"]["n_strong_chunks"] == 15
    assert determ["per_fmt"]["docx"]["mean_jaccard"] == 0.986
    assert determ["per_fmt"]["docx"]["_source"] == "strict_fixture"
    # per_doc trend 追加 3 行,每行含 img_dup_factor + n_total_image_refs(P1 修复)
    docx_trend = [d for d in result["per_doc"] if d.get("fmt") == "docx"]
    assert len(docx_trend) == 3
    assert all(d.get("_source") == "strict_fixture" for d in docx_trend)
    assert all("img_dup_factor" in d for d in docx_trend)
    assert all("n_total_image_refs" in d for d in docx_trend)


def test_docx_strict_yields_to_gt_jaccard_only_when_ge_5(monkeypatch, tmp_path):
    """P1 修复:GT 只 1 chunk(<5)→ strict 仍顶替主闸(防脆 GT 断崖)。"""
    monkeypatch.setenv("EVAL_L4_DOCX_BINDING_ENABLE", "true")
    monkeypatch.setattr(
        ib, "_run_docx_strict_path",
        lambda fixture_dir=None: _make_fake_docx_strict_payload(mean=0.50),
    )

    # 准备 docx GT,让主 Jaccard 路径仅出 1 chunk(<5 阈值)
    chunks = [FakeChunk(
        chunk_text="装配 步骤",
        chunk_type="step_card",
        extra={"image_refs": [{"image_index": 7}]},
    )]
    monkeypatch.setattr(ib, "_extract_and_chunk", lambda *a, **kw: chunks)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "docx_sop.docx").write_bytes(b"")
    gt_path = _write_gt_json({
        "_meta": {},
        "docx_sop": {
            "_doc_meta": {},
            "gt_chunks": [{
                "label": "x", "chunk_type": "step_card",
                "keywords": ["装配", "步骤"],
                "expected_image_refs": [{"image_index": 7}],
            }],
        },
    })
    try:
        result = ib.run([gt_path], str(docs_dir))
        determ = result["deterministic"]
        # GT 只 1 strong chunk(<5)→ strict 0.50 顶替主闸,防 1-chunk 脆 GT 断崖
        assert determ["binding_jaccard_docx"] == 0.50
        assert determ["per_fmt"]["docx"]["_source"] == "strict_fixture"
    finally:
        os.unlink(gt_path)


def test_docx_strict_yields_to_gt_jaccard_when_ge_5(monkeypatch, tmp_path):
    """GT n_strong_chunks >= 5 → strict 让位 GT 主闸(GT 量够才信)。"""
    monkeypatch.setenv("EVAL_L4_DOCX_BINDING_ENABLE", "true")
    monkeypatch.setattr(
        ib, "_run_docx_strict_path",
        lambda fixture_dir=None: _make_fake_docx_strict_payload(mean=0.50),
    )

    # 准备 5 个 chunk 的 GT,让主 Jaccard 路径出 mean=1.0
    chunks = [FakeChunk(
        chunk_text=f"装配 步骤 {i}",
        chunk_type="step_card",
        extra={"image_refs": [{"image_index": 7 + i}]},
    ) for i in range(5)]
    monkeypatch.setattr(ib, "_extract_and_chunk", lambda *a, **kw: chunks)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "docx_sop.docx").write_bytes(b"")
    gt_path = _write_gt_json({
        "_meta": {},
        "docx_sop": {
            "_doc_meta": {},
            "gt_chunks": [{
                "label": f"step{i}", "chunk_type": "step_card",
                "keywords": ["装配", "步骤", str(i)],
                "expected_image_refs": [{"image_index": 7 + i}],
            } for i in range(5)],
        },
    })
    try:
        result = ib.run([gt_path], str(docs_dir))
        determ = result["deterministic"]
        # GT 5 chunk 都 perfect match → mean=1.0;strict (0.50) 不顶替
        assert determ["binding_jaccard_docx"] == 1.0
        assert determ["per_fmt"]["docx"].get("_source") != "strict_fixture"
        # 但 strict per_doc 仍追加(trend 监控)
        sources = {d.get("_source") for d in result["per_doc"] if d.get("fmt") == "docx"}
        assert "strict_fixture" in sources
    finally:
        os.unlink(gt_path)


def test_docx_strict_not_triggered_when_only_fmt_pdf(monkeypatch, tmp_path):
    """only_fmt='pdf' → DOCX 路径不触发,PDF 现有行为完全不受影响。"""
    monkeypatch.setenv("EVAL_L4_DOCX_BINDING_ENABLE", "true")
    sentinel = {"called": False}

    def trap(fixture_dir=None):
        sentinel["called"] = True
        return _make_fake_docx_strict_payload()
    monkeypatch.setattr(ib, "_run_docx_strict_path", trap)
    monkeypatch.setattr(
        ib, "_extract_and_chunk",
        lambda *a, **kw: [FakeChunk(
            chunk_text="装配 步骤", chunk_type="step_card",
            extra={"image_refs": [{"image_index": 10, "page_num": 3}]},
        )],
    )

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "pdf_sop.pdf").write_bytes(b"")
    gt_path = _write_gt_json({
        "_meta": {},
        "pdf_sop": {
            "_doc_meta": {},
            "gt_chunks": [{
                "label": "x", "chunk_type": "step_card",
                "keywords": ["装配", "步骤"],
                "expected_image_refs": [{"image_index": 10, "page": 3}],
            }],
        },
    })
    try:
        result = ib.run([gt_path], str(docs_dir), only_fmt="pdf")
        assert sentinel["called"] is False, "only_fmt='pdf' 时不该触发 DOCX strict 路径"
        # PDF 主闸正常
        assert result["deterministic"]["binding_jaccard_pdf"] == 1.0
        assert result["deterministic"]["binding_jaccard_docx"] is None
    finally:
        os.unlink(gt_path)


def test_docx_strict_only_fmt_docx_triggers(monkeypatch, tmp_path):
    """only_fmt='docx' → DOCX strict 路径触发(独立脚本入口)。"""
    monkeypatch.setenv("EVAL_L4_DOCX_BINDING_ENABLE", "true")
    monkeypatch.setattr(
        ib, "_run_docx_strict_path",
        lambda fixture_dir=None: _make_fake_docx_strict_payload(mean=0.99),
    )
    result = ib.run([], str(tmp_path), only_fmt="docx")
    assert result["deterministic"]["binding_jaccard_docx"] == 0.99


def test_docx_strict_fixture_missing_returns_none(monkeypatch):
    """env 开但 fixture 目录不存在 → 返 None(fail-open)。"""
    monkeypatch.setenv("EVAL_L4_DOCX_BINDING_ENABLE", "true")
    out = ib._run_docx_strict_path(fixture_dir="/nonexistent/path/fuling_chunk_exp")
    assert out is None


def test_docx_strict_env_off_short_circuits(monkeypatch, tmp_path):
    """env=false → 即使 fixture 在也立刻返 None。"""
    monkeypatch.setenv("EVAL_L4_DOCX_BINDING_ENABLE", "false")
    # fixture_dir 指 tmp_path(存在)但 env OFF
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    assert ib._run_docx_strict_path(fixture_dir=str(fixture)) is None


def test_existing_pdf_xlsx_aggregation_unchanged_when_docx_disabled(monkeypatch, tmp_path):
    """回归保护:env OFF 时 PDF + XLSX 联合聚合与既有 test_run_aggregates 完全一致。"""
    monkeypatch.delenv("EVAL_L4_DOCX_BINDING_ENABLE", raising=False)

    def fake_extract(label, fmt, doc_path):
        if fmt == "pdf":
            return [FakeChunk(
                chunk_text="装配 步骤", chunk_type="step_card",
                extra={"image_refs": [{"image_index": 10, "page_num": 3}]},
            )]
        # xlsx
        return [FakeChunk(
            chunk_text="测试 数据", chunk_type="step_card",
            extra={"image_refs": [{"anchor_row": 5}]},
        )]
    monkeypatch.setattr(ib, "_extract_and_chunk", fake_extract)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "pdf_sop.pdf").write_bytes(b"")
    (docs_dir / "xlsx_sop.xlsx").write_bytes(b"")

    gt_path = _write_gt_json({
        "_meta": {"image_ref_scheme": "v2"},
        "pdf_sop": {
            "_doc_meta": {},
            "gt_chunks": [{"label": "p", "chunk_type": "step_card",
                           "keywords": ["装配", "步骤"],
                           "expected_image_refs": [{"image_index": 10, "page": 3}]}],
        },
        "xlsx_sop": {
            "_doc_meta": {},
            "gt_chunks": [{"label": "x", "chunk_type": "step_card",
                           "keywords": ["测试", "数据"],
                           "expected_image_refs": [{"block_index": 5}]}],
        },
    })
    try:
        result = ib.run([gt_path], str(docs_dir))
        determ = result["deterministic"]
        assert determ["binding_jaccard_pdf"] == 1.0
        assert determ["binding_jaccard_xlsx"] == 1.0
        assert determ["binding_jaccard_docx"] is None  # env OFF → 不写
        assert determ["binding_jaccard_pptx"] is None
        # per_doc 不该有 strict_fixture 痕迹
        sources = {d.get("_source") for d in result["per_doc"]}
        assert "strict_fixture" not in sources
        # 也不该有 docx_strict:: 痕迹的 errors(D7 重构后路径自己 raise 进 errors 时
        # 此断言会捕获回归)
        assert not any("docx_strict::" in e for e in determ["errors"])
    finally:
        os.unlink(gt_path)


# ── P1 必修:integration smoke test(真 fixture 跑完整路径)──────────
# 钉死 extractor 输出 blocks.extra.image_index + chunker step_card.extra.image_refs
# 字段不被 silent 改名。CI 没 fixture 或 python-docx 时优雅跳。

_SMOKE_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fuling_chunk_exp",
    "production_注塑事业部_FL-ZS-WI-002《奶茶杯测水试验》作业指导书.docx",
)


def _docx_runtime_available() -> bool:
    """smoke 真跑需 python-docx 才能 parse,缺则跳(CI 简包不装可选 extras)。"""
    try:
        import docx  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not os.path.exists(_SMOKE_FIXTURE) or not _docx_runtime_available(),
    reason=("smoke 跳:fixture 缺或 python-docx 未装(CI 简包)。"
            f"预期 fixture:{_SMOKE_FIXTURE}"),
)
def test_docx_strict_smoke_real_fixture(monkeypatch, tmp_path):
    """挑 1 个真 SOP docx 跑完整 _run_docx_strict_path,断 acc >= 0.9 且 errors=[]。

    钉死下列契约不被 silent 改名:
      - docx_extractor blocks.block_type ∈ {paragraph, heading, image_ref}
      - blocks.extra.image_index 是 int
      - production-faithful chunker(动态 split_mode)能识别该 SOP 为 step 路由
      - step_card chunk.extra.image_refs[*].image_index 可反查
    """
    monkeypatch.setenv("EVAL_L4_DOCX_BINDING_ENABLE", "true")

    # 临时 fixture 目录:只放 1 个真 SOP docx
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    import shutil as _sh
    _sh.copy2(_SMOKE_FIXTURE, fixture / os.path.basename(_SMOKE_FIXTURE))

    out = ib._run_docx_strict_path(fixture_dir=str(fixture))
    assert out is not None, "真 fixture 跑不出数 — extractor/chunker 契约已破"
    assert out.get("errors") == [], f"单 doc 异常被 fail-open 进 errors:{out.get('errors')}"

    by_fmt = out.get("by_fmt") or {}
    assert by_fmt.get("_source") == "strict_fixture"
    assert by_fmt.get("n_docs") == 1, "该 SOP 应入 SOP 主聚合"
    assert by_fmt.get("n_strong_chunks", 0) >= 1, "至少 1 张图能被 strict 对上"
    mj = by_fmt.get("mean_jaccard")
    assert mj is not None and mj >= 0.9, f"strict baseline 应 >= 0.9,实测 {mj}"

    # per_doc shape 完整(P1 修复:img_dup_factor + n_total_image_refs)
    doc = out["per_doc"][0]
    assert doc["is_sop"] is True
    assert doc["degraded"] is False  # SOP → 入主聚合
    assert "img_dup_factor" in doc
    assert "n_total_image_refs" in doc
    assert doc["n_step_cards"] >= 1, "production-faithful 路由应产 step_card"
