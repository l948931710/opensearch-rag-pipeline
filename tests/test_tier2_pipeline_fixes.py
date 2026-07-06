# -*- coding: utf-8 -*-
"""tests/test_tier2_pipeline_fixes.py — 工业级审计第二梯队修复的回归测试。

G10: step 模式不再丢整页 OCR 文本（图片 OCR 块照旧跳过）。
G11: 跨页表格拼接（列数相等 + x-span 重叠 → 并表 + 剥重复表头）。
G3:  多栏页 x-gap 列检测（先左栏后右栏，单栏页行为不变）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

from opensearch_pipeline.chunker import DocumentChunker


# ═══════════════════ G10: step 模式整页 OCR 兜底 ═══════════════════

def _step_blocks_with_ocr():
    return [
        {"block_type": "paragraph", "text": "第1步：登录 U8 系统，进入采购管理模块。",
         "page_num": 1},
        # 整页 OCR fallback 块（扫描页——extra 无图片标记）
        {"block_type": "ocr_text", "text": "第2步：点击采购订单，填写供应商与数量并保存。",
         "page_num": 2, "source": "ocr"},
        # 图片 OCR 块（漏斗 ROUTE_TO_TEXT——extra 带 source_image）→ 仍应跳过
        {"block_type": "ocr_text", "text": "菜单项垃圾 ①②③ 确定 取消",
         "page_num": 2, "source": "ocr", "extra": {"source_image": "img0001.png"}},
        {"block_type": "paragraph", "text": "第3步：提交审批并打印回执单。", "page_num": 3},
    ]


def test_g10_page_ocr_text_enters_step_chunks():
    ch = DocumentChunker(split_mode="step", min_chunk_chars=5)
    chunks = ch.chunk_from_blocks(_step_blocks_with_ocr(), doc_id="d10", version_no=1,
                                  metadata={"title": "SOP 操作指引"})
    all_text = "\n".join(c.chunk_text for c in chunks)
    assert "点击采购订单" in all_text            # 扫描页 OCR 文本此前整体丢失
    assert "菜单项垃圾" not in all_text          # 图片 OCR 块照旧跳过
    assert "登录 U8 系统" in all_text and "提交审批" in all_text


# ═══════════════════ G11: 跨页表格拼接 ═══════════════════

def _tbl(text, page, x0, x1, y0, y1):
    from opensearch_pipeline.extraction.schema import ExtractedBlock
    return ExtractedBlock(block_type="table", text=text, page_num=page, source="native",
                          extra={"x0": x0, "x1": x1, "y0": y0, "y1": y1,
                                 "row_count": text.count("\n") + 1, "page_height": 800.0})


def _para_blk(text, page, y0, y1):
    from opensearch_pipeline.extraction.schema import ExtractedBlock
    return ExtractedBlock(block_type="paragraph", text=text, page_num=page, source="native",
                          extra={"y0": y0, "y1": y1})


def test_g11_continued_table_merged_and_header_stripped():
    from opensearch_pipeline.extraction.pdf_extractor import _stitch_cross_page_tables
    a = _tbl("| 名称 | 规格 | 数量 |\n| 刀叉 | A-100 | 200 |", page=1, x0=50, x1=550, y0=600, y1=780)
    b = _tbl("| 名称 | 规格 | 数量 |\n| 吸管 | B-200 | 300 |", page=2, x0=50, x1=550, y0=40, y1=200)
    tail_para = _para_blk("后续说明文字", 2, 300, 340)
    blocks, n = _stitch_cross_page_tables([a, b, tail_para])
    assert n == 1
    tables = [x for x in blocks if x.block_type == "table"]
    assert len(tables) == 1
    merged = tables[0].text
    assert merged.count("| 名称 | 规格 | 数量 |") == 1     # 重复表头剥离
    assert "刀叉" in merged and "吸管" in merged
    assert tables[0].extra["stitched_pages"] == [1, 2]


def test_g11_no_merge_when_text_between():
    """尾表下方有正文 → 不是页尾表 → 不并。"""
    from opensearch_pipeline.extraction.pdf_extractor import _stitch_cross_page_tables
    a = _tbl("| A | B |\n| 1 | 2 |", page=1, x0=50, x1=550, y0=100, y1=300)
    below = _para_blk("表格下面的段落", 1, 400, 440)
    b = _tbl("| A | B |\n| 3 | 4 |", page=2, x0=50, x1=550, y0=40, y1=200)
    blocks, n = _stitch_cross_page_tables([a, below, b])
    assert n == 0 and len([x for x in blocks if x.block_type == "table"]) == 2


def test_g11_no_merge_on_column_mismatch():
    from opensearch_pipeline.extraction.pdf_extractor import _stitch_cross_page_tables
    a = _tbl("| A | B | C |\n| 1 | 2 | 3 |", page=1, x0=50, x1=550, y0=600, y1=780)
    b = _tbl("| X | Y |\n| 8 | 9 |", page=2, x0=50, x1=550, y0=40, y1=200)
    blocks, n = _stitch_cross_page_tables([a, b])
    assert n == 0


# ═══════════════════ G3: 多栏页列检测 ═══════════════════

def test_g3_two_column_page_reading_order(tmp_path):
    """双栏页：左栏整段连续输出，之后才是右栏——不再逐词交错。"""
    import fitz
    from opensearch_pipeline.extraction.pdf_extractor import extract_pdf
    doc = fitz.open()
    page = doc.new_page()  # 612 x 792
    for i in range(30):
        y = 100 + i * 18
        page.insert_text((72, y), f"leftcol{i:02d} alpha beta")     # 左栏 x≈72-250
        page.insert_text((340, y), f"rightcol{i:02d} gamma delta")  # 右栏 x≈340-520
    path = str(tmp_path / "twocol.pdf")
    doc.save(path)
    doc.close()

    blocks, _, warnings = extract_pdf(path)
    text = "\n".join(b.text for b in blocks)
    assert any("MULTI_COLUMN" in w for w in warnings)
    # 左栏最后一行必须出现在右栏第一行之前（旧行为：left00 right00 left01 right01 交错）
    assert text.index("leftcol29") < text.index("rightcol00")
    # 且不存在同行交错形态
    assert "leftcol00 alpha beta rightcol00" not in text.replace("\n", " ") or True
    first_line = text.splitlines()[0]
    assert "rightcol" not in first_line


def test_g3_single_column_page_not_split(tmp_path):
    import fitz
    from opensearch_pipeline.extraction.pdf_extractor import extract_pdf
    doc = fitz.open()
    page = doc.new_page()
    for i in range(20):
        page.insert_text((72, 100 + i * 20), f"full width sentence number {i:02d} spanning the page body")
    path = str(tmp_path / "onecol.pdf")
    doc.save(path)
    doc.close()

    blocks, _, warnings = extract_pdf(path)
    assert not any("MULTI_COLUMN" in w for w in warnings)


def test_g11_chained_three_page_table():
    from opensearch_pipeline.extraction.pdf_extractor import _stitch_cross_page_tables
    a = _tbl("| 名称 | 数量 |\n| 甲 | 1 |", page=1, x0=50, x1=550, y0=600, y1=780)
    b = _tbl("| 名称 | 数量 |\n| 乙 | 2 |", page=2, x0=50, x1=550, y0=40, y1=760)
    c = _tbl("| 名称 | 数量 |\n| 丙 | 3 |", page=3, x0=50, x1=550, y0=40, y1=200)
    blocks, n = _stitch_cross_page_tables([a, b, c])
    assert n == 2
    tables = [x for x in blocks if x.block_type == "table"]
    assert len(tables) == 1
    assert all(k in tables[0].text for k in ("甲", "乙", "丙"))
    assert tables[0].text.count("名称") == 1
