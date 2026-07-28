# -*- coding: utf-8 -*-
"""B4 / B5 / B17（2026-07-25）：只留痕与观测，**不改任何行为**。

三条的共同性质：现网都存在"内容悄悄少了、状态却照常成功"的缺口，但改行为都需要先有
现网真实计数（B4 的行级切分会改 chunk 家族、B5 的提高上限要先有成本熔断器、B17 的收紧
判据是家族迁移）。所以本批只把信号接出来。

B4：五处 table_chunk 创建点无长度上限，validator 按 token_count>2000 整块丢弃 →
    「问规格参数查不到」。**拆两种语义**：还有其它有效 chunk → partial-loss/NEEDS_REVIEW；
    全丢导致 0 chunk → 保留既有 FAILED+retry 语义，只补原因（不为了留痕改掉失败语义）。
B5：三处页级上限（原生抽取 / OCR / 图片提取）此前都没进 partial_loss_notes。
B17：`m_mode in ("text","clause") and _detect_step_patterns(doc)` 无条件覆盖，判据很宽；
    先记初始/最终模式 + detector 命中，让"多少篇因为哪个词走了 step"可查。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.chunker import Chunk


def _chunk(cid, doc_id="D1", ctype="table_chunk", tokens=3000, text="x"):
    c = Chunk(chunk_id=cid, doc_id=doc_id, version_no=1, chunk_index=0,
              chunk_type=ctype, chunk_text=text, token_count=tokens)
    return c


# ═══════════════════ B4：超长表格丢弃留痕，拆两语义 ═══════════════════

def test_b4_note_when_other_chunks_survive():
    """还有其它有效 chunk → 走 partial-loss/NEEDS_REVIEW（文档可服务）。"""
    ctx = {"chunks": [_chunk("t1"), _chunk("ok1", ctype="text_chunk", tokens=50, text="正常内容")]}
    pn.node_validate_chunks(ctx)
    notes = ctx.get("table_drop_notes") or {}
    assert notes, "超长 table 丢弃必须留痕"
    key = ("D1", 1)
    assert any("[TABLE_DROPPED]" in n for n in notes[key])
    assert [c.chunk_id for c in ctx["valid_chunks"]] == ["ok1"]


def test_b4_zero_chunk_keeps_existing_failed_semantics():
    """全丢导致 0 chunk：**不碰**既有 0-chunk 疑似失败分支的定级，只补原因。"""
    ctx = {"chunks": [_chunk("t1"), _chunk("t2")]}
    pn.node_validate_chunks(ctx)
    assert ctx["valid_chunks"] == []
    assert ctx.get("table_drop_notes"), "仍要留原因"
    # validator 不得自己改 document_version 状态（那是 closure 的职责）
    src = inspect.getsource(pn.node_validate_chunks)
    assert "UPDATE document_version" not in src


def test_b4_only_counts_oversize_tables_not_other_drops():
    ctx = {"chunks": [_chunk("e1", ctype="table_chunk", tokens=50, text="  "),   # empty_text
                      _chunk("ok", ctype="text_chunk", tokens=50, text="正文")]}
    pn.node_validate_chunks(ctx)
    assert not (ctx.get("table_drop_notes") or {}), "只统计 too_many_tokens 的表格"


def test_b4_notes_merge_into_partial_loss_channel_without_overwriting():
    """B4 的 note 必须**追加**进同一 closure（那里还有 A11 unreadable / 批次6 ocr_partial）。"""
    src = inspect.getsource(pn.node_write_chunk_meta)
    assert '_partial_notes.extend((ctx.get("table_drop_notes")' in src


# ═══════════════════ B5：三处页级上限统一留痕 ═══════════════════

def test_b5_ocr_result_carries_skipped_pages():
    from opensearch_pipeline.extraction.ocr_client import OCRResult
    assert OCRResult().skipped_pages == 0
    assert OCRResult(skipped_pages=7).skipped_pages == 7


def test_b5_ocr_cap_is_recorded_and_propagated():
    from opensearch_pipeline.extraction import ocr_client, unified_extractor
    src = inspect.getsource(ocr_client.OCRClient)
    assert "_skipped_by_cap" in src and "skipped_pages=_skipped_by_cap" in src
    ue = inspect.getsource(unified_extractor.UnifiedExtractor._apply_ocr_fallback)
    assert "ocr_page_cap" in ue and "partial_loss_notes.append" in ue


def test_b5_native_truncation_now_enters_partial_loss():
    """此前只有 warnings + 结构化字段 → 照常定稿 DONE，不走复查通道。"""
    from opensearch_pipeline.extraction import unified_extractor
    src = inspect.getsource(unified_extractor.UnifiedExtractor._extract_pdf)
    assert "native_page_cap" in src
    i_flag = src.index("result.extract_truncated = True")
    i_note = src.index("native_page_cap")
    assert i_flag < i_note, "留痕必须跟在既有截断判定之后（不改判定本身）"


def test_b5_image_page_cap_recorded_in_stats():
    from opensearch_pipeline.extraction import image_extraction_utils, unified_extractor
    src = inspect.getsource(image_extraction_utils.extract_images_from_pdf)
    assert 'stats["skipped_pages_beyond_cap"]' in src
    assert "def _warn_page_cap_skips(" in inspect.getsource(unified_extractor)


def test_b5_does_not_change_any_cap_value():
    """只留痕：三个页上限是当前唯一的摄取侧成本闸（cost_breaker 因 RebuildConfig.enabled=False
    恒 no-op），拆闸之前必须先有 breaker。"""
    from opensearch_pipeline.extraction import image_extraction_utils
    src = inspect.getsource(image_extraction_utils.extract_images_from_pdf)
    assert "range(min(len(pdf), max_pages))" in src, "上限本身不得被改动"


def test_b5_notes_are_appended_not_overwritten():
    from opensearch_pipeline.extraction import unified_extractor
    src = inspect.getsource(unified_extractor.UnifiedExtractor._extract_pdf)
    assert "result.partial_loss_notes.extend(_page_cap_notes)" in src


# ═══════════════════ B17：路由观测 ═══════════════════

def test_b17_records_initial_final_and_detector_signal():
    src = inspect.getsource(pn.node_chunk_documents)
    for key in ("route_initial_mode", "route_final_mode", "step_detector_matched"):
        assert key in src, f"缺 {key}"
    # 三个量都要记：PPTX/XLSX 会在 detector 之后再次覆盖 m_mode，只记最终值无法归因
    assert "_initial_mode = m_mode" in src
    assert "_step_detected = _detect_step_patterns(doc)" in src


def test_b17_does_not_change_the_routing_predicate():
    """只观测：收紧判据 = 改 chunk 家族，需冻结重灌 + manifest 门，不在本批。"""
    src = inspect.getsource(pn.node_chunk_documents)
    assert 'if m_mode in ("text", "clause") and _step_detected:' in src


def test_b17_step_card_not_added_to_fix_b_types():
    """`step_card → _FIX_B_TYPES` 是独立的行为变化，已从本项拆走。"""
    from opensearch_pipeline.chunker import _FIX_B_TYPES
    assert "step_card" not in _FIX_B_TYPES
