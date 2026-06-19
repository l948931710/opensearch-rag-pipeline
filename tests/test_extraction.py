# -*- coding: utf-8 -*-
"""
test_extraction.py — Unified Extraction Layer 单元测试
"""

from opensearch_pipeline.extraction.schema import ExtractionResult, ExtractedBlock
from opensearch_pipeline.extraction.text_extractor import (
    extract_text_file,
    blocks_to_text,
    extract_title_from_blocks,
)
from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor


class TestExtractedBlock:
    """ExtractedBlock 基础测试。"""

    def test_to_dict(self):
        block = ExtractedBlock(
            block_type="heading",
            text="一、目的",
            level=1,
            page_num=1,
            section_path="一、目的",
            source="native",
        )
        d = block.to_dict()
        assert d["block_type"] == "heading"
        assert d["level"] == 1
        assert d["page_num"] == 1
        assert d["source"] == "native"

    def test_defaults(self):
        block = ExtractedBlock(block_type="paragraph", text="hello")
        assert block.level == 0
        assert block.page_num is None
        assert block.source == "native"
        assert block.extra == {}


class TestTextExtractor:
    """文本/Markdown 解析器测试。"""

    def test_markdown_headings(self):
        text = "# 标题一\n\n正文段落\n\n## 标题二\n\n更多内容"
        blocks = extract_text_file(text)
        headings = [b for b in blocks if b.block_type == "heading"]
        paragraphs = [b for b in blocks if b.block_type == "paragraph"]
        assert len(headings) == 2
        assert headings[0].level == 1
        assert headings[1].level == 2
        assert len(paragraphs) == 2

    def test_chinese_headings(self):
        text = "第一章 总则\n\n内容描述\n\n一、适用范围\n\n适用内容"
        blocks = extract_text_file(text)
        headings = [b for b in blocks if b.block_type == "heading"]
        assert len(headings) == 2
        assert headings[0].level == 1  # 第X章
        assert headings[1].level == 2  # 一、

    def test_sub_headings(self):
        text = "3.1 登录系统\n\n操作步骤\n\n3.2 提交审核\n\n审核步骤"
        blocks = extract_text_file(text)
        headings = [b for b in blocks if b.block_type == "heading"]
        assert len(headings) == 2
        assert all(h.level == 3 for h in headings)

    def test_table_detection(self):
        text = "| 名称 | 说明 |\n| --- | --- |\n| A | B |"
        blocks = extract_text_file(text)
        tables = [b for b in blocks if b.block_type == "table"]
        assert len(tables) == 1
        assert "|" in tables[0].text

    def test_section_path_tracking(self):
        text = "# 审核流程\n\n步骤一\n\n## 准备工作\n\n准备内容"
        blocks = extract_text_file(text)
        paragraphs = [b for b in blocks if b.block_type == "paragraph"]
        assert paragraphs[0].section_path == "审核流程"
        assert paragraphs[1].section_path == "准备工作"

    def test_empty_text(self):
        blocks = extract_text_file("")
        assert blocks == []

    def test_blocks_to_text_roundtrip(self):
        original = "# 标题\n\n内容段落"
        blocks = extract_text_file(original)
        reassembled = blocks_to_text(blocks)
        assert "标题" in reassembled
        assert "内容段落" in reassembled

    def test_extract_title(self):
        blocks = [
            ExtractedBlock(block_type="heading", text="文档标题", level=1),
            ExtractedBlock(block_type="paragraph", text="内容"),
        ]
        assert extract_title_from_blocks(blocks) == "文档标题"

    def test_extract_title_fallback(self):
        blocks = [ExtractedBlock(block_type="paragraph", text="内容")]
        assert extract_title_from_blocks(blocks, fallback="默认标题") == "默认标题"


class TestUnifiedExtractor:
    """UnifiedExtractor mock 模式测试。"""

    def setup_method(self):
        self.extractor = UnifiedExtractor(simulate=True)

    def test_mock_extraction(self):
        task = {
            "doc_id": "DOC_TEST_001",
            "version_no": 1,
            "file_ext": "md",
            "raw_key": "raw/admin/test.md",
            "mock_text": "# 测试文档\n\n这是内容\n\n## 第二节\n\n更多内容",
        }
        result = self.extractor.extract(task)

        assert isinstance(result, ExtractionResult)
        assert result.doc_id == "DOC_TEST_001"
        assert result.extract_method == "mock_injection"
        assert result.text_length > 0
        assert len(result.blocks) > 0

    def test_mock_blocks_have_headings(self):
        task = {
            "doc_id": "DOC_TEST_002",
            "version_no": 1,
            "file_ext": "txt",
            "raw_key": "raw/admin/test.txt",
            "mock_text": "# 标题\n\n段落文本",
        }
        result = self.extractor.extract(task)
        headings = [b for b in result.blocks if b.block_type == "heading"]
        assert len(headings) >= 1

    def test_mock_table_block(self):
        task = {
            "doc_id": "DOC_TEST_003",
            "version_no": 1,
            "file_ext": "md",
            "raw_key": "raw/admin/table.md",
            "mock_text": "# 表格测试\n\n| 列1 | 列2 |\n| --- | --- |\n| A | B |",
        }
        result = self.extractor.extract(task)
        tables = [b for b in result.blocks if b.block_type == "table"]
        assert len(tables) == 1

    def test_unsupported_file_type(self):
        # pptx 已有真实 extractor；.xls（旧版二进制 Excel）是按决策显式不支持的类型，
        # 必须走 _unsupported 给出可见警告，而非静默路由进 _extract_xlsx。
        task = {
            "doc_id": "DOC_TEST_004",
            "version_no": 1,
            "file_ext": "xls",
            "raw_key": "raw/admin/test.xls",
        }
        result = self.extractor.extract(task)
        assert "unsupported" in result.extract_method
        assert len(result.warnings) > 0

    def test_extraction_result_to_dict(self):
        task = {
            "doc_id": "DOC_TEST_005",
            "version_no": 1,
            "file_ext": "txt",
            "raw_key": "raw/admin/test.txt",
            "mock_text": "# Title\n\nContent",
        }
        result = self.extractor.extract(task)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert isinstance(d["blocks"], list)
        assert d["doc_id"] == "DOC_TEST_005"


class TestOCRFallback:
    """OCR fallback 逻辑测试。"""

    def test_short_text_triggers_ocr(self):
        extractor = UnifiedExtractor(simulate=True)
        task = {
            "doc_id": "DOC_SCAN_001",
            "version_no": 1,
            "file_ext": "pdf",
            "raw_key": "raw/admin/scanned.pdf",
            "mock_text": "短文本",  # < 100 chars
        }
        result = extractor.extract(task)
        # mock 模式下 mock_text 不触发 OCR（因为走 _extract_mock）
        # 真实 OCR fallback 在 _extract_pdf 中
        assert result.text_length > 0

    def test_sufficient_text_no_ocr(self):
        extractor = UnifiedExtractor(simulate=True)
        long_text = "这是一段足够长的文本。" * 20  # > 100 chars
        task = {
            "doc_id": "DOC_LONG_001",
            "version_no": 1,
            "file_ext": "txt",
            "raw_key": "raw/admin/long.txt",
            "mock_text": long_text,
        }
        result = extractor.extract(task)
        assert result.ocr_required is False
        assert result.ocr_status == "NOT_REQUIRED"
