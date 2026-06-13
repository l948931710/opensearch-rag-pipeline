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


def test_pdf_weak_when_no_image_index():
    """PDF v2 坐标系:image_index 是主键,缺失=weak(只标 page=presence-only)。"""
    weak = ImageRef(fmt="pdf", page=3)
    strong = ImageRef(fmt="pdf", page=3, image_index=10)
    assert weak.is_weak() and not strong.is_weak()
    # weak 走 page 命名空间,strong 走 image_index — 不应误等
    assert weak.primary_key() != strong.primary_key()
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
    """PDF v2:主键 image_index;page/page_num 别名仍认作辅助字段,不参与 strict。"""
    a = parse_ref_dict({"image_index": 10, "page": 3}, "pdf")
    b = parse_ref_dict({"image_index": 10, "page_num": 3}, "pdf")
    assert a.strict_key() == b.strict_key() == ("pdf", 10)


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


def test_jaccard_pdf_weak_gt_does_not_match_strong_pred():
    """PDF v2:weak GT(只标 page)的 primary_key 走 'pdf:page' 命名空间,
    strong pred 走 'pdf' 命名空间 — 不应误匹配。

    设计意图:weak GT 是 GT 半成品状态,strict 模式不计入(has_strong_refs=False
    会让它走 presence-only path 不入 mean_jaccard)。primary 模式即使被显式调用
    也不该 false-positive 跨命名空间相等。
    """
    gt_weak = [ImageRef(fmt="pdf", page=3)]
    pred_strong = [ImageRef(fmt="pdf", image_index=10, page=3)]
    assert jaccard(gt_weak, pred_strong, strict=True) == 0.0
    assert jaccard(gt_weak, pred_strong, strict=False) == 0.0  # 命名空间分离


def test_jaccard_pdf_strict_match():
    """v2:PDF strict 用 image_index 主键判等。"""
    gt = [ImageRef(fmt="pdf", image_index=10, page=3),
          ImageRef(fmt="pdf", image_index=11, page=3)]
    pred = [ImageRef(fmt="pdf", image_index=11, page=3),
            ImageRef(fmt="pdf", image_index=10, page=3)]  # 顺序不重要
    assert jaccard(gt, pred, strict=True) == 1.0


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
        # 显式负例语义:空 expected_image_refs 应当入主闸(empty-vs-empty=1.0),
        # 不应被错判 weak 而排除 — 2026-06-12 修复
        assert d.gt_chunks[1].has_strong_refs is True
    finally:
        os.unlink(gt_path)


def test_load_gt_field_missing_vs_explicit_empty():
    """区分'GT 未标该字段(不入主闸)'vs'显式 []`(入主闸 empty-vs-empty=1.0)。

    回归测试:2026-06-12 D5 baseline 跑发现 DOCX 76 chunks 全部没标
    expected_image_refs 字段,但被错当显式负例入主闸,假阴性把 DOCX mean
    拖到 0.7763(实际 --strict 跑过 98.6%)。修后字段缺失走 degraded 路径。
    """
    gt_path = _write_tmp_json({
        "_meta": {},
        "docx_sop": {
            "_doc_meta": {},
            "gt_chunks": [
                # 字段缺失:GT 未标 → degraded chunk,不入主闸
                {"label": "未标", "keywords": ["a"]},
                # 显式空集:显式负例 → 入主闸
                {"label": "显式空", "keywords": ["b"], "expected_image_refs": []},
            ],
        },
    })
    try:
        d = load_gt(gt_path)["docx_sop"]
        assert d.gt_chunks[0].has_strong_refs is False  # 字段缺失 → degraded
        assert d.gt_chunks[1].has_strong_refs is True   # 显式空 → 入主闸
    finally:
        os.unlink(gt_path)


def test_load_gt_explicit_negative_is_strong():
    """显式 `expected_image_refs: []` = 该 step 不该有图,GT 钉死不绑图也算对。

    回归测试:2026-06-12 之前 `bool(refs) and ...` 把空 list 判 weak,
    导致 PDF 首测 mean_jaccard=0(空 refs 全被踢出分母)。修后 jaccard 会算
    empty-vs-empty=1.0,作为正确不绑图的奖励。
    """
    gt_path = _write_tmp_json({
        "_meta": {},
        "pdf_sop": {
            "_doc_meta": {},
            "gt_chunks": [
                # 显式负例:前言段无图
                {"label": "前言", "keywords": ["前言"], "expected_image_refs": []},
                # weak ref(只标 page,无 image_index)
                {"label": "弱", "keywords": ["弱"], "expected_image_refs": [{"page": 3}]},
                # strong ref(v2:image_index 主键)
                {"label": "强", "keywords": ["强"],
                 "expected_image_refs": [{"image_index": 10, "page": 3}]},
            ],
        },
    })
    try:
        d = load_gt(gt_path)["pdf_sop"]
        assert d.gt_chunks[0].has_strong_refs is True   # 显式负例 = 可入主闸
        assert d.gt_chunks[1].has_strong_refs is False  # weak page-only = 仅 trend
        assert d.gt_chunks[2].has_strong_refs is True
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
