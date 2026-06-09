# -*- coding: utf-8 -*-
"""
test_procedure_parent_xlsx.py — XLSX procedure_image_guide 现在生成 procedure_parent
并回填 parent_chunk_id (C1)，使 step_card 在检索期可做兄弟步骤扩展（与 DOCX/PDF 一致）。
"""

from opensearch_pipeline.chunker import DocumentChunker


def _chunker():
    return DocumentChunker(
        max_chunk_chars=2000, min_chunk_chars=1, overlap_chars=0,
        xlsx_layout_type="procedure_image_guide",
    )


def test_xlsx_steps_get_procedure_parent_and_parent_chunk_id():
    blocks = [
        {"block_type": "paragraph", "text": "目的：钉钉审批操作", "extra": {}},
        {"block_type": "paragraph", "text": "打开钉钉工作台", "extra": {"step_no": 1}, "page_num": 1},
        {"block_type": "paragraph", "text": "点击审批进入", "extra": {"step_no": 2}, "page_num": 1},
        {"block_type": "paragraph", "text": "提交申请", "extra": {"step_no": 3}, "page_num": 1},
    ]
    chunks = _chunker()._chunk_procedure_steps(blocks, "XLSX_DOC", 1, {"title": "审批指引"})

    step_cards = [c for c in chunks if c.chunk_type == "step_card"]
    parents = [c for c in chunks if c.chunk_type == "procedure_parent"]

    assert len(step_cards) == 3
    assert len(parents) == 1, "应生成唯一 procedure_parent"
    parent = parents[0]

    # parent 关联全部 step_card；每个 step_card 回填了 parent_chunk_id
    sc_ids = {c.chunk_id for c in step_cards}
    assert set(parent.extra.get("child_chunk_ids", [])) == sc_ids
    assert parent.extra.get("step_count") == 3
    for sc in step_cards:
        assert sc.extra.get("parent_chunk_id") == parent.chunk_id


def test_xlsx_no_steps_produces_no_parent():
    blocks = [{"block_type": "paragraph", "text": "仅说明，无步骤", "extra": {}}]
    chunks = _chunker()._chunk_procedure_steps(blocks, "XLSX_DOC2", 1, {"title": "无步骤"})
    assert not [c for c in chunks if c.chunk_type == "procedure_parent"]
