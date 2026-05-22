# -*- coding: utf-8 -*-
"""
test_chunker.py — DocumentChunker 单元测试
"""

import pytest
from opensearch_pipeline.chunker import DocumentChunker, Chunk
from opensearch_pipeline.extraction.schema import ExtractedBlock


class TestChunkFromBlocks:
    """chunk_from_blocks() 测试。"""

    def setup_method(self):
        self.chunker = DocumentChunker(
            max_chunk_chars=200,
            min_chunk_chars=10,
            overlap_chars=20,
        )

    def test_basic_paragraph_chunking(self):
        blocks = [
            ExtractedBlock(block_type="heading", text="标题", level=1, section_path="标题"),
            ExtractedBlock(block_type="paragraph", text="这是正文内容，需要足够长才能生成chunk。" * 3),
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_001", 1)
        assert len(chunks) >= 1
        assert all(c.chunk_type == "text_chunk" for c in chunks)

    def test_table_block_becomes_table_chunk(self):
        blocks = [
            ExtractedBlock(block_type="heading", text="表格测试", level=1),
            ExtractedBlock(
                block_type="table",
                text="| 列1 | 列2 |\n| A | B |\n| C | D |" * 3,
            ),
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_002", 1)
        table_chunks = [c for c in chunks if c.chunk_type == "table_chunk"]
        assert len(table_chunks) == 1

    def test_heading_sets_section_title(self):
        blocks = [
            ExtractedBlock(block_type="heading", text="审核流程", level=1, section_path="审核流程"),
            ExtractedBlock(block_type="paragraph", text="这是审核流程的详细内容描述。" * 3),
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_003", 1)
        assert len(chunks) >= 1
        assert chunks[0].section_title == "审核流程"

    def test_page_num_preserved(self):
        blocks = [
            ExtractedBlock(block_type="paragraph", text="第一页的内容" * 10, page_num=1),
            ExtractedBlock(block_type="paragraph", text="第二页的内容" * 10, page_num=2),
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_004", 1)
        page_nums = set(c.page_num for c in chunks if c.page_num is not None)
        assert 1 in page_nums
        assert 2 in page_nums

    def test_ocr_source_becomes_ocr_chunk(self):
        blocks = [
            ExtractedBlock(
                block_type="ocr_text",
                text="OCR识别出的文本内容" * 5,
                source="ocr",
                page_num=1,
            ),
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_005", 1)
        assert len(chunks) >= 1
        assert chunks[0].chunk_type == "ocr_chunk"

    def test_short_block_skipped(self):
        blocks = [
            ExtractedBlock(block_type="paragraph", text="短"),  # < min_chunk_chars
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_006", 1)
        assert len(chunks) == 0

    def test_metadata_propagation(self):
        blocks = [
            ExtractedBlock(block_type="paragraph", text="测试metadata传播" * 5),
        ]
        metadata = {
            "title": "测试文档",
            "owner_dept": "admin",
            "category_l1": "sop",
            "permission_level": "dept_internal",
        }
        chunks = self.chunker.chunk_from_blocks(
            blocks, "DOC_007", 1, metadata=metadata,
        )
        assert len(chunks) >= 1
        assert chunks[0].title == "测试文档"
        assert chunks[0].owner_dept == "admin"
        assert chunks[0].permission_level == "dept_internal"

    def test_dict_blocks_compatible(self):
        """兼容 dict 格式 blocks（从 canonical JSON 读取时）。"""
        blocks = [
            {"block_type": "heading", "text": "标题", "level": 1, "section_path": "标题"},
            {"block_type": "paragraph", "text": "内容文本" * 10, "page_num": 1},
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_008", 1)
        assert len(chunks) >= 1

    def test_empty_blocks(self):
        chunks = self.chunker.chunk_from_blocks([], "DOC_009", 1)
        assert chunks == []


class TestChunkDocument:
    """chunk_document() (legacy text-based) 测试。"""

    def setup_method(self):
        self.chunker = DocumentChunker(
            max_chunk_chars=200,
            min_chunk_chars=10,
        )

    def test_basic_text_chunking(self):
        text = "这是一段很长的文本。" * 50
        chunks = self.chunker.chunk_document(text, "DOC_LEGACY_001", 1)
        assert len(chunks) >= 1
        assert all(isinstance(c, Chunk) for c in chunks)

    def test_chunk_id_format(self):
        text = "文本内容" * 30
        chunks = self.chunker.chunk_document(text, "DOC_LEGACY_002", 1)
        assert len(chunks) >= 1
        assert chunks[0].chunk_id.startswith("DOC_LEGACY_002_v1_c")


class TestChunkFaq:
    """FAQ (split_mode='faq') 切分测试。"""

    def setup_method(self):
        self.chunker = DocumentChunker(
            max_chunk_chars=300,
            min_chunk_chars=5,
            overlap_chars=20,
            split_mode="faq",
        )

    def test_heuristic_faq_extraction_prefixes(self):
        blocks = [
            ExtractedBlock(block_type="paragraph", text="问：如何申请退款？"),
            ExtractedBlock(block_type="paragraph", text="答：请登录个人中心，点击申请退款。"),
            ExtractedBlock(block_type="paragraph", text="Q: What is the return policy?"),
            ExtractedBlock(block_type="paragraph", text="A: You can return within 30 days."),
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_FAQ_01", 1)
        # Expected two faq chunks
        faq_chunks = [c for c in chunks if c.chunk_type == "faq_chunk"]
        assert len(faq_chunks) == 2
        assert "如何申请退款" in faq_chunks[0].chunk_text
        assert "What is the return policy?" in faq_chunks[1].chunk_text

    def test_fallback_question_marks(self):
        blocks = [
            ExtractedBlock(block_type="paragraph", text="1. 宿舍有热水供应吗？"),
            ExtractedBlock(block_type="paragraph", text="宿舍全天24小时提供热水供应。"),
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_FAQ_02", 1)
        faq_chunks = [c for c in chunks if c.chunk_type == "faq_chunk"]
        assert len(faq_chunks) == 1
        assert "24小时提供热水" in faq_chunks[0].chunk_text

    def test_unmatched_text_falls_back_to_text_chunk(self):
        blocks = [
            ExtractedBlock(block_type="paragraph", text="本制度适用于公司所有员工，请大家务必遵守。"),
            ExtractedBlock(block_type="paragraph", text="问：违反制度怎么办？"),
            ExtractedBlock(block_type="paragraph", text="答：将按人事处罚条例处理。"),
        ]
        chunks = self.chunker.chunk_from_blocks(blocks, "DOC_FAQ_03", 1)
        text_chunks = [c for c in chunks if c.chunk_type == "text_chunk"]
        faq_chunks = [c for c in chunks if c.chunk_type == "faq_chunk"]
        assert len(text_chunks) == 1
        assert len(faq_chunks) == 1
        assert "本制度适用于公司所有员工" in text_chunks[0].chunk_text
        assert "违反制度怎么办" in faq_chunks[0].chunk_text


class TestChunkerDuplicateAvoidance:
    """测试段落合并逻辑，确保避免数据重复。"""

    def test_merge_adjacent_short_chunks_directly(self):
        chunker = DocumentChunker(max_chunk_chars=300)
        
        # Case 1: [Long, Short, Long]
        paras = ["A" * 200, "B" * 50, "C" * 200]
        merged = chunker._merge_adjacent_short_chunks(paras, min_chars=150)
        # Should merge "B"*50 into "A"*200
        assert len(merged) == 2
        assert merged[0] == "A" * 200 + "\n\n" + "B" * 50
        assert merged[1] == "C" * 200

        # Case 2: [Short, Long, Long]
        paras = ["A" * 50, "B" * 200, "C" * 200]
        merged = chunker._merge_adjacent_short_chunks(paras, min_chars=150)
        # Should merge "A"*50 into "B"*200
        assert len(merged) == 2
        assert merged[0] == "A" * 50 + "\n\n" + "B" * 200
        assert merged[1] == "C" * 200

        # Case 3: [Long, Short, Short]
        paras = ["A" * 200, "B" * 50, "C" * 50]
        merged = chunker._merge_adjacent_short_chunks(paras, min_chars=150)
        # Should merge both "B" and "C" into "A"
        assert len(merged) == 1
        assert merged[0] == "A" * 200 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50

    def test_short_chunk_contiguous_merging_from_blocks(self):
        chunker = DocumentChunker(
            max_chunk_chars=240,
            min_chunk_chars=10,
            overlap_chars=20,
        )
        p1_text = "A" * 200
        p2_text = "B" * 50
        p3_text = "C" * 200

        blocks = [
            ExtractedBlock(block_type="paragraph", text=p1_text),
            ExtractedBlock(block_type="paragraph", text=p2_text),
            ExtractedBlock(block_type="paragraph", text=p3_text),
        ]

        chunks = chunker.chunk_from_blocks(blocks, "DOC_DUP_001", 1)

        # Let's verify that p2_text is merged into the same chunk as p1_text, and NOT duplicated in others.
        # Let's count how many chunks contain p2_text:
        p2_count = sum(1 for c in chunks if p2_text in c.chunk_text)
        assert p2_count == 1
        
        # Verify that there is no chunk containing both p3_text and p1_text or p2_text
        for c in chunks:
            if p3_text in c.chunk_text:
                assert p1_text not in c.chunk_text
                assert p2_text not in c.chunk_text

    def test_short_chunk_contiguous_merging_from_document(self):
        chunker = DocumentChunker(
            max_chunk_chars=240,
            min_chunk_chars=10,
            overlap_chars=20,
        )
        p1_text = "A" * 200
        p2_text = "B" * 50
        p3_text = "C" * 200

        full_text = f"{p1_text}\n\n{p2_text}\n\n{p3_text}"
        chunks = chunker.chunk_document(full_text, "DOC_DUP_002", 1)

        # Verify that p2_text is merged with p1_text, and not duplicated in p3_text's chunks.
        p2_count = sum(1 for c in chunks if p2_text in c.chunk_text)
        assert p2_count == 1
        
        for c in chunks:
            if p3_text in c.chunk_text:
                assert p1_text not in c.chunk_text
                assert p2_text not in c.chunk_text



