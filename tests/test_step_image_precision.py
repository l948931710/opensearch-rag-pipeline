# -*- coding: utf-8 -*-
"""FAIR image-binding tests for step-card chunking.

Motivation: the GT eval's `image_accuracy` is *presence*-based ("does the matched
chunk have >=1 image"), which silently passed an over-attachment bug where every
sub-step of docx_sop got ALL 26 images. These tests measure PRECISION instead:
each image must bind to exactly the positionally-correct step, with no duplication
across steps. They reproduce the docx_sop structure (multi-level decimal sub-steps
4.2.1 / 4.2.3.2 written as paragraphs, with image_ref blocks interleaved) and would
FAIL on the pre-fix code (all sub-steps collapse into one 4.2 mega-step).
"""
from collections import Counter

from opensearch_pipeline.chunker import DocumentChunker


def _imgref(idx):
    return {"block_type": "image_ref", "text": "",
            "extra": {"image_index": idx, "oss_key": f"k{idx}",
                      "visual_summary": f"img{idx}", "filename": f"f{idx}.png"},
            "page_num": 1}


def _para(text):
    return {"block_type": "paragraph", "text": text, "page_num": 1}


# docx_sop-shaped blocks: heading 4.2, then decimal sub-steps as paragraphs,
# each followed by its own image(s).
BLOCKS = [
    {"block_type": "heading", "text": "4.2  计划单、客签样正确", "level": 3,
     "section_path": "4.2 计划单、客签样正确", "page_num": 1},
    _imgref(0),  # belongs to 4.2 (计划单/客签样)
    _para("4.2.1外观检验：查看印刷面的印刷是否与客签样的颜色相同，不许有明显的偏差。"),
    _imgref(1),  # belongs to 4.2.1 外观检验
    _para("4.2.3.2 印刷层干摩擦检验：测试方法如下，用白色棉布在印刷表面来回摩擦十次。"),
    _imgref(2),  # belongs to 4.2.3.2 干摩擦
    _imgref(3),  # belongs to 4.2.3.2 干摩擦
    _para("4.2.3.3 印刷层湿摩擦检验：测试方法如下，棉布蘸水后在印刷表面摩擦。"),
    _imgref(4),  # belongs to 4.2.3.3 湿摩擦
]


def _chunk():
    ch = DocumentChunker(split_mode="step", min_chunk_chars=5)
    return ch.chunk_from_blocks(blocks=BLOCKS, doc_id="d", version_no=1,
                                metadata={"title": "印刷检验"})


def test_substeps_become_separate_steps():
    """Decimal sub-steps (4.2.1 / 4.2.3.2 / 4.2.3.3) must each start a step_card —
    the pre-fix regex collapsed them into one 4.2 mega-step."""
    steps = [c for c in _chunk() if c.chunk_type == "step_card"]
    # 4.2, 4.2.1, 4.2.3.2, 4.2.3.3  → at least 4 distinct steps
    assert len(steps) >= 4, f"expected >=4 step_cards, got {len(steps)}"


def test_no_image_over_attachment():
    """Every image appears in exactly ONE chunk (no duplication across steps /
    continuation chunks). This is the metric the presence-based eval missed."""
    chunks = _chunk()
    counts = Counter()
    for c in chunks:
        for ref in c.extra.get("image_refs", []):
            counts[ref.get("image_index")] += 1
    assert counts, "no images bound at all"
    over = {k: v for k, v in counts.items() if v > 1}
    assert not over, f"image over-attachment (index -> #chunks): {dict(counts)}"
    assert set(counts) == {0, 1, 2, 3, 4}


def test_images_bind_to_positionally_correct_step():
    """Right image on the right step (precision), by adjacency of text and image."""
    chunks = _chunk()

    def step_with_img(idx):
        for c in chunks:
            if any(r.get("image_index") == idx for r in c.extra.get("image_refs", [])):
                return c
        return None

    # img1 sits right after "4.2.1外观检验" → that step's text must mention 外观
    assert "外观" in (step_with_img(1).chunk_text or "")
    # img2/img3 follow "4.2.3.2 ...干摩擦" →干摩擦 step
    assert "干摩擦" in (step_with_img(2).chunk_text or "")
    assert step_with_img(2) is step_with_img(3)  # both on the same step
    # img4 follows "4.2.3.3 ...湿摩擦"
    assert "湿摩擦" in (step_with_img(4).chunk_text or "")
    # the 干摩擦 and 湿摩擦 steps are DIFFERENT chunks (not collapsed)
    assert step_with_img(2) is not step_with_img(4)
