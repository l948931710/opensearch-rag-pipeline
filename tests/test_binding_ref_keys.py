# -*- coding: utf-8 -*-
"""
test_binding_ref_keys.py — eval_harness.binding.ref_keys 核心单测

钉死:四格式坐标系判等行为、strict vs primary 模式、empty-vs-empty=1.0 约定、
img_dup_factor 计算。
"""
import json
import os
import tempfile

import pytest

from eval_harness.binding.ref_keys import ImageRef, jaccard, parse_ref_dict, img_dup_factor
from eval_harness.binding.gt_loader import load_gt, validate_gt_against_manifest


# ── ImageRef 构造与判等 ──────────────────────────────────────

def test_docx_strict_equals_primary():
    a = ImageRef(fmt="docx", image_index=3)
    b = ImageRef(fmt="docx", image_index=3)
    assert a.strict_key() == b.strict_key()
    assert a.primary_key() == b.primary_key()


def test_pdf_weak_when_no_in_page_idx():
    weak = ImageRef(fmt="pdf", page=3)
    strong = ImageRef(fmt="pdf", page=3, in_page_idx=1)
    assert weak.is_weak() and not strong.is_weak()
    # primary 等价(都指页 3),strict 不等价
    assert weak.primary_key() == strong.primary_key()
    assert weak.strict_key() != strong.strict_key()


def test_pptx_weak_when_no_shape_idx():
    weak = ImageRef(fmt="pptx", slide_no=7)
    strong = ImageRef(fmt="pptx", slide_no=7, shape_idx=1)
    assert weak.is_weak() and not strong.is_weak()


def test_xlsx_block_index_only():
    a = ImageRef(fmt="xlsx", block_index=11)
    assert not a.is_weak()
    assert a.strict_key() == ("xlsx", 11)


# ── parse_ref_dict 容错 ──────────────────────────────────────

def test_parse_ref_dict_docx():
    ref = parse_ref_dict({"image_index": 5}, "docx")
    assert ref.image_index == 5 and ref.fmt == "docx"


def test_parse_ref_dict_pdf_aliases():
    # 接受 page 或 page_num,接受 in_page_idx 或 image_index_in_page
    a = parse_ref_dict({"page": 3, "in_page_idx": 1}, "pdf")
    b = parse_ref_dict({"page_num": 3, "image_index_in_page": 1}, "pdf")
    assert a.strict_key() == b.strict_key()


def test_parse_ref_dict_xlsx_anchor_row_alias():
    ref = parse_ref_dict({"anchor_row": 11}, "xlsx")
    assert ref.block_index == 11


def test_parse_ref_dict_unknown_fields_ignored():
    # 未知字段不报错
    ref = parse_ref_dict({"image_index": 3, "foo": "bar", "随便": 999}, "docx")
    assert ref.image_index == 3


# ── jaccard 核心约定 ────────────────────────────────────────

def test_jaccard_empty_vs_empty_is_one():
    """xlsx_spec 这类'该 step 不该有图'的负例必须显式入正确分子。"""
    assert jaccard([], []) == 1.0


def test_jaccard_empty_vs_nonempty_is_zero():
    pred = [ImageRef(fmt="docx", image_index=1)]
    assert jaccard([], pred) == 0.0
    assert jaccard(pred, []) == 0.0


def test_jaccard_perfect_match():
    gt = [ImageRef(fmt="docx", image_index=1), ImageRef(fmt="docx", image_index=2)]
    pred = [ImageRef(fmt="docx", image_index=2), ImageRef(fmt="docx", image_index=1)]
    assert jaccard(gt, pred) == 1.0


def test_jaccard_partial():
    gt = [ImageRef(fmt="docx", image_index=1), ImageRef(fmt="docx", image_index=2)]
    pred = [ImageRef(fmt="docx", image_index=1), ImageRef(fmt="docx", image_index=3)]
    # 交集 {1}=1, 并集 {1,2,3}=3 → 1/3
    assert abs(jaccard(gt, pred) - 1 / 3) < 1e-9


def test_jaccard_pdf_strict_vs_primary():
    """PDF 弱 GT 在 primary 模式可匹配 strong pred。"""
    gt_weak = [ImageRef(fmt="pdf", page=3)]              # 只标了 page
    pred_strong = [ImageRef(fmt="pdf", page=3, in_page_idx=1)]
    # strict 模式不匹配(in_page_idx None vs 1)
    assert jaccard(gt_weak, pred_strong, strict=True) == 0.0
    # primary 模式匹配(都指页 3)
    assert jaccard(gt_weak, pred_strong, strict=False) == 1.0


# ── img_dup_factor over-attach 检测 ─────────────────────────

def test_dup_factor_perfect():
    """每张图只绑一次 → 1.0"""
    refs = [
        ImageRef(fmt="docx", image_index=1),
        ImageRef(fmt="docx", image_index=2),
        ImageRef(fmt="docx", image_index=3),
    ]
    assert img_dup_factor(refs) == 1.0


def test_dup_factor_over_attach():
    """每子步骤被塞所有图(2 子步 × 3 图)= 2.0 (over-attach bug 信号)"""
    refs = [ImageRef(fmt="docx", image_index=i) for i in (1, 2, 3, 1, 2, 3)]
    assert img_dup_factor(refs) == 2.0


def test_dup_factor_empty():
    assert img_dup_factor([]) == 1.0  # 没有图 = no problem(分母防 0)


# ── gt_loader 集成 ─────────────────────────────────────────

def _write_tmp_json(payload, suffix=".json"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return path


def test_load_gt_basic():
    gt_path = _write_tmp_json({
        "_meta": {"image_ref_scheme": "v1", "extractor_version": "abc123"},
        "docx_sop": {
            "_doc_meta": {"doc_sha256": "deadbeef" * 8},
            "gt_chunks": [
                {"label": "step1", "chunk_type": "step_card",
                 "keywords": ["扫码"],
                 "expected_image_refs": [{"image_index": 3}, {"image_index": 4}]},
                {"label": "step2", "chunk_type": "step_card",
                 "keywords": [],
                 "expected_image_refs": []},  # 显式负例
            ],
        },
    })
    try:
        docs = load_gt(gt_path)
        assert "docx_sop" in docs
        d = docs["docx_sop"]
        assert d.fmt == "docx" and not d.degraded
        assert d.extractor_version == "abc123"
        assert len(d.gt_chunks) == 2
        assert d.gt_chunks[0].expected_image_refs[0].image_index == 3
        assert d.gt_chunks[0].has_strong_refs is True
        assert d.gt_chunks[1].expected_image_refs == []
    finally:
        os.unlink(gt_path)


def test_load_gt_skip_in_binding():
    gt_path = _write_tmp_json({
        "_meta": {"skip_in_binding": ["xlsx_inspect"]},
        "xlsx_inspect": {
            "_doc_meta": {},
            "gt_chunks": [{"label": "s", "chunk_type": "step_card"}],
        },
    })
    try:
        docs = load_gt(gt_path)
        assert docs["xlsx_inspect"].degraded is True
    finally:
        os.unlink(gt_path)


def test_load_gt_pdf_weak_refs():
    """PDF 只标 page = weak,has_strong_refs=False。"""
    gt_path = _write_tmp_json({
        "_meta": {},
        "pdf_sop": {
            "_doc_meta": {},
            "gt_chunks": [{
                "label": "3.1", "chunk_type": "step_card",
                "expected_image_refs": [{"page": 3}],  # 只标 page, 弱
            }],
        },
    })
    try:
        d = load_gt(gt_path)["pdf_sop"]
        assert d.gt_chunks[0].has_strong_refs is False
        assert d.gt_chunks[0].expected_image_refs[0].is_weak()
    finally:
        os.unlink(gt_path)


def test_validate_gt_against_manifest_ok():
    """manifest 含全部 GT ref_key → ok。"""
    manifest_path = _write_tmp_json({
        "_meta": {"extractor_version": "v1", "doc_sha256": "x"},
        "images": [
            {"ref_key": {"image_index": 3}},
            {"ref_key": {"image_index": 4}},
        ],
    })
    gt_path = _write_tmp_json({
        "_meta": {"extractor_version": "v1"},
        "docx_sop": {
            "_doc_meta": {"doc_sha256": "x"},
            "gt_chunks": [{"label": "s", "expected_image_refs": [{"image_index": 3}]}],
        },
    })
    try:
        gt = load_gt(gt_path)["docx_sop"]
        result = validate_gt_against_manifest(gt, manifest_path)
        assert result["ok"], result["reasons"]
    finally:
        os.unlink(manifest_path); os.unlink(gt_path)


def test_validate_gt_extractor_version_drift():
    """extractor_version 不匹配 → 报告漂移。"""
    manifest_path = _write_tmp_json({
        "_meta": {"extractor_version": "v2"},
        "images": [{"ref_key": {"image_index": 3}}],
    })
    gt_path = _write_tmp_json({
        "_meta": {"extractor_version": "v1"},
        "docx_sop": {
            "_doc_meta": {},
            "gt_chunks": [{"label": "s", "expected_image_refs": [{"image_index": 3}]}],
        },
    })
    try:
        gt = load_gt(gt_path)["docx_sop"]
        result = validate_gt_against_manifest(gt, manifest_path)
        assert not result["ok"]
        assert any("extractor_version" in r for r in result["reasons"])
    finally:
        os.unlink(manifest_path); os.unlink(gt_path)


def test_validate_gt_missing_ref():
    """GT 引用 manifest 里没有的 ref → 失败。"""
    manifest_path = _write_tmp_json({"_meta": {}, "images": [{"ref_key": {"image_index": 3}}]})
    gt_path = _write_tmp_json({
        "_meta": {},
        "docx_sop": {
            "_doc_meta": {},
            "gt_chunks": [{"label": "s", "expected_image_refs": [
                {"image_index": 3}, {"image_index": 99},  # 99 不在 manifest
            ]}],
        },
    })
    try:
        gt = load_gt(gt_path)["docx_sop"]
        result = validate_gt_against_manifest(gt, manifest_path)
        assert not result["ok"]
        assert len(result["missing_refs"]) == 1
        assert result["missing_refs"][0]["ref"] == {"image_index": 99}
    finally:
        os.unlink(manifest_path); os.unlink(gt_path)
