# -*- coding: utf-8 -*-
"""
OCR-孤儿修复回归测试 (2026-06-15)。

bug: image-heavy SOP (如 6B0EAA 辅料赠送入库手册.docx) 所有正文段落 <min_chunk_chars
被丢弃 → chunks 为空 → ROUTE_TO_TEXT 图片的 OCR 进 pending_image_refs 却因
`if pending_image_refs and chunks:` 为假被静默丢弃 → 0 chunk → 文档从检索消失。

fix: chunker.py chunk_from_blocks 末尾加 `elif pending_image_refs and not chunks:`
合成一个 ocr_chunk 承载 OCR。
"""
from opensearch_pipeline.chunker import DocumentChunker


def _img_block(image_index, ocr_text, visual_summary):
    return {
        "block_type": "image_ref",
        "text": "",
        "extra": {
            "image_index": image_index,
            "oss_key": f"processing/assets/doc/v2/image{image_index}.png",
            "source_image": f"image{image_index}.png",
            "ocr_text": ocr_text,
            "visual_summary": visual_summary,
        },
    }


# 模拟 6B0EAA: 2 个 <50 字正文段 + 3 个 ROUTE_TO_TEXT 富 OCR 图
ORPHAN_BLOCKS = [
    {"block_type": "paragraph", "text": "1 进入库存管理-其他入库-其他入库单"},
    _img_block(0, "其他入库单 单据编号 制单人 审核 仓库 辅料仓 数量 金额", "U8 其他入库单录入界面"),
    {"block_type": "paragraph", "text": "2 点击增加-选择辅料赠送入库"},
    _img_block(1, "辅料赠送入库单 单号0000017944 供应商 物料编码 规格 批号", "辅料赠送入库单填写界面"),
    _img_block(2, "保存 提交审核 单据状态 已审核 制单日期", "单据提交审核界面"),
]


def test_ocr_orphan_synthesizes_chunk():
    """fix 后: 全短文+图片文档产出 1 个 ocr_chunk, 带 image_refs + OCR 文本。"""
    chunker = DocumentChunker(split_mode="text", min_chunk_chars=50)
    chunks = chunker.chunk_from_blocks(
        ORPHAN_BLOCKS, doc_id="DOC_TEST_OCR_ORPHAN", version_no=2,
        metadata={"title": "辅料赠送入库操手册.docx"},
    )
    assert len(chunks) >= 1, "fix 后不应再 0 chunk"
    # 找到承载 OCR 的 chunk
    ocr_chunks = [c for c in chunks if c.chunk_type == "ocr_chunk"]
    assert ocr_chunks, f"应有 ocr_chunk, 实际类型: {[c.chunk_type for c in chunks]}"
    c = ocr_chunks[0]
    refs = c.extra.get("image_refs", [])
    assert len(refs) == 3, f"应承载 3 张图的 ref, 实际 {len(refs)}"
    # image_refs 契约字段保留
    for r in refs:
        for k in ("oss_key", "source_image", "visual_summary", "ocr_text", "image_index"):
            assert k in r, f"image_ref 丢了契约字段 {k}"
    # OCR 关键词进入 chunk_text (可检索)
    assert "辅料赠送入库" in c.chunk_text
    assert "[图片OCR]" in c.chunk_text
    assert c.token_count > 5


def test_normal_multipara_with_image_unchanged():
    """回归: 正常多段(>=50字) + 尾图 → 图挂到最后真 chunk, 不触发 fallback 分支。"""
    blocks = [
        {"block_type": "paragraph", "text": "这是一段足够长的正文内容" * 5},
        {"block_type": "paragraph", "text": "第二段同样足够长的正文描述内容" * 5},
        _img_block(0, "某界面 OCR 文本", "界面截图"),
    ]
    chunker = DocumentChunker(split_mode="text", min_chunk_chars=50)
    chunks = chunker.chunk_from_blocks(blocks, doc_id="DOC_TEST_NORMAL", version_no=1)
    assert len(chunks) >= 1
    # 图应挂到 text_chunk, 不应出现额外 ocr_chunk fallback
    assert not any(c.chunk_type == "ocr_chunk" for c in chunks), \
        "正常文档不应触发 ocr_chunk fallback 分支"
    # 图挂到了某 chunk
    assert any(c.extra.get("image_refs") for c in chunks)


def test_short_paragraphs_no_image_stays_empty():
    """回归: 纯短文无图 → 仍 0 chunk (fallback 分支只在有 pending image 时触发)。"""
    blocks = [
        {"block_type": "paragraph", "text": "短句一"},
        {"block_type": "paragraph", "text": "短句二"},
    ]
    chunker = DocumentChunker(split_mode="text", min_chunk_chars=50)
    chunks = chunker.chunk_from_blocks(blocks, doc_id="DOC_TEST_SHORT", version_no=1)
    assert len(chunks) == 0, f"纯短文无图应 0 chunk, 实际 {len(chunks)}"
