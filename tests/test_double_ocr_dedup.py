# -*- coding: utf-8 -*-
"""G6 双重 OCR 去重（docs/tier3_decision_2026-07-06.md 抓获的集成 bug）。

整页扫描 PDF 走两条 OCR 路径：页面作为嵌入图片被 funnel ROUTE_TO_TEXT 转写一次，
per-page OCR 兜底又整页转写一次——两份文本只差换行/标点（hash 不同，精确去重不吃），
canonical 内容翻倍（生产实锤 6B37DC：1 页 600 字 → 11 chunk/1501 字，"第七条"×4）。
修复 = _apply_ocr_fallback 合并点以页级 OCR 为权威，剔除同页被覆盖的 funnel 文本块；
守卫基线 = eval_harness/fidelity baseline.json real 口径（scan_rd_training hard）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

from opensearch_pipeline.extraction.ocr_client import OCRPageResult, OCRResult
from opensearch_pipeline.extraction.schema import ExtractedBlock, ExtractionResult
from opensearch_pipeline.extraction.unified_extractor import (
    UnifiedExtractor,
    _drop_double_ocr_dups,
)
from opensearch_pipeline.text_similarity import gram_containment

# 真实复现对的缩影：同一页两次 OCR，只差换行与全半角标点
_FUNNEL_TEXT = ("人才进修培训制度\n第一条目的\n建立和完善人才培训机制,通过有计划有目的地"
                "对人员进行培训，使各岗位人员\n熟练掌握岗位技能知识。更好的在各岗位发挥作用。")
_PAGE_OCR_TEXT = ("人才进修培训制度\n第一条目的\n建立和完善人才培训机制,通过有计划有目的地"
                  "对人员进行培训，使各岗位人员熟练掌握岗位技能知识。更好的在各岗位发挥作用。")
_UNRELATED_TEXT = "设备清扫作业指导书\n第一步 关闭电源并挂牌上锁\n第二步 清理传送带表面残料"


def _funnel_block(text, page=1):
    return ExtractedBlock(block_type="ocr_text", text=text, page_num=page, source="ocr",
                          extra={"source_image": "scan_p1_img0007.png"})


def _page_ocr_block(text, page=1):
    return ExtractedBlock(block_type="ocr_text", text=text, page_num=page, source="ocr")


# ── gram_containment 原语 ──

def test_gram_containment_identical_and_whitespace_variant():
    assert gram_containment(_FUNNEL_TEXT, _FUNNEL_TEXT) == 1.0
    # 换行差异被空白归一吸收 → 接近 1
    assert gram_containment(_FUNNEL_TEXT, _PAGE_OCR_TEXT) > 0.95


def test_gram_containment_unrelated_text_low():
    assert gram_containment(_UNRELATED_TEXT, _PAGE_OCR_TEXT) < 0.2


def test_gram_containment_short_or_empty_needle_is_no_fingerprint():
    # 无指纹返回 0.0（调用方 fail-open 保留块），与 simhash64 空文本语义一致
    assert gram_containment("", _PAGE_OCR_TEXT) == 0.0
    assert gram_containment("ab", _PAGE_OCR_TEXT) == 0.0
    assert gram_containment(_FUNNEL_TEXT, "") == 0.0


# ── _drop_double_ocr_dups 判定 ──

def test_covered_funnel_block_dropped():
    blocks = [_funnel_block(_FUNNEL_TEXT)]
    kept, dropped = _drop_double_ocr_dups(blocks, [_page_ocr_block(_PAGE_OCR_TEXT)])
    assert kept == []
    assert dropped == [1]


def test_distinct_funnel_block_kept():
    """页级 OCR 没覆盖到的图内文本（内容确有差异）必须保留——宁重勿丢。"""
    blocks = [_funnel_block(_UNRELATED_TEXT)]
    kept, dropped = _drop_double_ocr_dups(blocks, [_page_ocr_block(_PAGE_OCR_TEXT)])
    assert len(kept) == 1 and dropped == []


def test_funnel_block_on_page_without_page_ocr_kept():
    """非 needy 页（原生文本充足、无页级 OCR）的 funnel 块照旧共存——cosurface 行为不变。"""
    blocks = [_funnel_block(_FUNNEL_TEXT, page=2)]
    kept, dropped = _drop_double_ocr_dups(blocks, [_page_ocr_block(_PAGE_OCR_TEXT, page=1)])
    assert len(kept) == 1 and dropped == []


def test_native_and_pageless_blocks_untouched():
    native = ExtractedBlock(block_type="paragraph", text=_FUNNEL_TEXT, page_num=1,
                            source="native")
    pageless_funnel = _funnel_block(_FUNNEL_TEXT, page=None)
    kept, dropped = _drop_double_ocr_dups([native, pageless_funnel],
                                          [_page_ocr_block(_PAGE_OCR_TEXT)])
    assert kept == [native, pageless_funnel] and dropped == []


def test_short_funnel_text_fail_open():
    """过短文本无指纹（containment=0）→ 保留，绝不因误判丢内容。"""
    blocks = [_funnel_block("ab")]
    kept, dropped = _drop_double_ocr_dups(blocks, [_page_ocr_block(_PAGE_OCR_TEXT)])
    assert len(kept) == 1 and dropped == []


def test_multi_page_only_matching_page_deduped():
    blocks = [_funnel_block(_FUNNEL_TEXT, page=1), _funnel_block(_FUNNEL_TEXT, page=3)]
    kept, dropped = _drop_double_ocr_dups(
        blocks, [_page_ocr_block(_PAGE_OCR_TEXT, page=1),
                 _page_ocr_block(_UNRELATED_TEXT, page=3)])
    assert dropped == [1]
    assert [b.page_num for b in kept] == [3]


# ── _apply_ocr_fallback 集成（mock OCR client，零 API）──

class _StubOCRClient:
    def __init__(self, pages):
        self._pages = pages

    def ocr_pdf(self, local_path, doc_id, oss_client, page_nums=None):
        return OCRResult(pages=[OCRPageResult(page_num=p, text=t) for p, t in self._pages],
                         combined_text="\n".join(t for _, t in self._pages), status="DONE")


def _scanned_pdf_result(funnel_text):
    """纯扫描页的 ExtractionResult：0 原生文本 + funnel ROUTE_TO_TEXT 块。"""
    return ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="pdf",
                            extract_method="pypdf", title="t", text=funnel_text,
                            text_length=len(funnel_text),
                            blocks=[_funnel_block(funnel_text)], page_count=1)


def test_apply_ocr_fallback_drops_double_ocr_and_warns():
    ux = UnifiedExtractor.__new__(UnifiedExtractor)
    ux.oss_client = None
    ux.ocr_client = _StubOCRClient([(1, _PAGE_OCR_TEXT)])
    res = ux._apply_ocr_fallback({"doc_id": "d", "local_path": "/nonexistent.pdf"},
                                 _scanned_pdf_result(_FUNNEL_TEXT), needy=[1])
    ocr_texts = [b.text for b in res.blocks if b.block_type == "ocr_text"]
    assert ocr_texts == [_PAGE_OCR_TEXT], "同页应只剩权威页级 OCR 块"
    assert any("DOUBLE_OCR_DEDUP" in w for w in res.warnings)
    # 内容不再翻倍：合并后文本 ≈ 单份页级转写
    assert len(res.text) <= len(_PAGE_OCR_TEXT) + 10


def test_apply_ocr_fallback_keeps_distinct_funnel_text():
    ux = UnifiedExtractor.__new__(UnifiedExtractor)
    ux.oss_client = None
    ux.ocr_client = _StubOCRClient([(1, _UNRELATED_TEXT)])
    res = ux._apply_ocr_fallback({"doc_id": "d", "local_path": "/nonexistent.pdf"},
                                 _scanned_pdf_result(_FUNNEL_TEXT), needy=[1])
    ocr_texts = [b.text for b in res.blocks if b.block_type == "ocr_text"]
    assert sorted(ocr_texts) == sorted([_FUNNEL_TEXT, _UNRELATED_TEXT])
    assert not any("DOUBLE_OCR_DEDUP" in w for w in res.warnings)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn(); print(f"  ✓ {fn.__name__}")
    print("all double-OCR dedup tests passed")
