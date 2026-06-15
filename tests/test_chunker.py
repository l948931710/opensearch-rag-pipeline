# -*- coding: utf-8 -*-
"""
test_chunker.py — DocumentChunker 单元测试
"""

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


class TestStepCardLengthLimit:
    """Fix 1: step_card 超长拆分测试。"""

    def test_oversized_step_card_splits(self):
        """当步骤文字 + 图片 OCR 超过 max_chunk_chars 时应拆分。"""
        chunker = DocumentChunker(
            max_chunk_chars=200,
            min_chunk_chars=10,
            overlap_chars=0,
            split_mode="step",
        )
        blocks = [
            ExtractedBlock(block_type="paragraph", text="步骤1. 打开阀门并检查压力表读数"),
            ExtractedBlock(block_type="paragraph", text="确认系统正常后继续操作" * 3),
            # image_ref with long OCR text
            {
                "block_type": "image_ref",
                "text": "",
                "page_num": 1,
                "section_path": None,
                "source": "multimodal",
                "extra": {
                    "image_index": 0,
                    "source_image": "img.png",
                    "oss_key": "key",
                    "ocr_text": "阀门操作注意事项" * 10,
                    "visual_summary": "操作面板显示压力表和控制按钮的详细视图" * 3,
                },
            },
            ExtractedBlock(block_type="paragraph", text="步骤2. 关闭阀门"),
        ]
        chunks = chunker.chunk_from_blocks(blocks, "DOC_STEP_SPLIT", 1)
        step_cards = [c for c in chunks if c.chunk_type == "step_card"]
        
        # 步骤1 应该被拆分为至少 2 个 step_card
        step1_cards = [c for c in step_cards if c.extra.get("step_no") == 1]
        assert len(step1_cards) >= 2, f"Expected step_card split, got {len(step1_cards)} chunks"
        
        # 补充 chunk 应标记为 is_step_continuation
        continuation_cards = [c for c in step1_cards if c.extra.get("is_step_continuation")]
        assert len(continuation_cards) >= 1
        
        # 所有 step_card 都不应超过 max_chunk_chars（含 context_prefix）
        for c in step_cards:
            assert len(c.chunk_text) <= 300, f"step_card too long: {len(c.chunk_text)} chars"

    def test_short_step_card_not_split(self):
        """短步骤不应拆分。"""
        chunker = DocumentChunker(
            max_chunk_chars=800,
            min_chunk_chars=10,
            overlap_chars=0,
            split_mode="step",
        )
        blocks = [
            ExtractedBlock(block_type="paragraph", text="步骤1. 打开系统"),
            ExtractedBlock(block_type="paragraph", text="步骤2. 输入密码"),
        ]
        chunks = chunker.chunk_from_blocks(blocks, "DOC_STEP_SHORT", 1)
        step_cards = [c for c in chunks if c.chunk_type == "step_card"]
        continuations = [c for c in step_cards if c.extra.get("is_step_continuation")]
        assert len(continuations) == 0


class TestRowCardContextEnrichment:
    """Fix 2: row_card 极短 chunk 上下文丰富测试。"""

    def test_row_card_prepends_section_context(self):
        """row_card 模式下，极短行应追加设备名称前缀。"""
        chunker = DocumentChunker(
            max_chunk_chars=300,
            min_chunk_chars=5,
            overlap_chars=0,
            row_card=True,
        )
        blocks = [
            ExtractedBlock(
                block_type="heading", text="三辊研磨机 每班清扫",
                level=1, section_path="三辊研磨机 每班清扫"
            ),
            ExtractedBlock(block_type="paragraph", text="传动部位\t目视检查\t每班"),
            ExtractedBlock(block_type="paragraph", text="研磨辊\t清洁擦拭\t每班"),
        ]
        chunks = chunker.chunk_from_blocks(blocks, "DOC_ROW_CTX", 1)
        text_chunks = [c for c in chunks if c.chunk_type == "text_chunk"]
        
        assert len(text_chunks) == 2
        # 每个短行应包含设备名称前缀
        for c in text_chunks:
            assert "三辊研磨机" in c.chunk_text, f"Missing equipment context: {c.chunk_text}"

    def test_row_card_long_text_no_prefix(self):
        """超过 200 字的行不应追加前缀（避免冗余）。"""
        chunker = DocumentChunker(
            max_chunk_chars=500,
            min_chunk_chars=5,
            overlap_chars=0,
            row_card=True,
        )
        long_text = "详细说明" * 60  # > 200 chars
        blocks = [
            ExtractedBlock(
                block_type="heading", text="设备A",
                level=1, section_path="设备A"
            ),
            ExtractedBlock(block_type="paragraph", text=long_text),
        ]
        chunks = chunker.chunk_from_blocks(blocks, "DOC_ROW_LONG", 1)
        text_chunks = [c for c in chunks if c.chunk_type == "text_chunk"]
        assert len(text_chunks) >= 1
        # 长文本不应有 【设备A】 前缀
        assert not text_chunks[0].chunk_text.startswith("【设备A】")


class TestClauseInterClauseOverlap:
    """Fix A (2026-06-15 L6 A/B): clause 模式不再生成 `[上文]` 跨条款面包屑。

    历史上每个条款 chunk 会前置 `[上文] {上一条款标题}`；离线 A/B 证实其对可读性净负、
    对 recall/rerank 无贡献，故整段移除。本类改为验证"无 `[上文]`、条款正文完整保留"。
    """

    def test_clause_chunks_have_no_shangwen_prefix(self):
        """所有条款 chunk 都不应含 `[上文]` 前缀，且条款正文完整保留。"""
        chunker = DocumentChunker(
            max_chunk_chars=500,
            min_chunk_chars=10,
            overlap_chars=100,
            split_mode="clause",
        )
        blocks = [
            ExtractedBlock(
                block_type="paragraph",
                text="第一条 本制度适用于公司全体员工的考勤管理。" * 3
            ),
            ExtractedBlock(
                block_type="paragraph",
                text="第二条 员工应当按时打卡上下班，不得迟到早退。" * 3
            ),
            ExtractedBlock(
                block_type="paragraph",
                text="第三条 请假需提前一天提交申请，特殊情况除外。" * 3
            ),
        ]
        chunks = chunker.chunk_from_blocks(blocks, "DOC_CLAUSE_OVL", 1)
        clause_chunks = [c for c in chunks if c.chunk_type == "clause_chunk"]

        assert len(clause_chunks) == 3
        # Fix A: 没有任何 [上文] 面包屑
        assert all("[上文]" not in c.chunk_text for c in clause_chunks)
        # 条款正文仍完整保留（各自的条款内容在对应 chunk 内）
        assert "第二条" in clause_chunks[1].chunk_text
        assert "第三条" in clause_chunks[2].chunk_text

    def test_clause_first_chunk_no_context(self):
        """只有一个条款时不应有 [上文] 标记。"""
        chunker = DocumentChunker(
            max_chunk_chars=2000,
            min_chunk_chars=10,
            overlap_chars=100,
            split_mode="clause",
        )
        blocks = [
            ExtractedBlock(
                block_type="paragraph",
                text="第一条 本制度适用于公司全体员工。" * 5
            ),
        ]
        chunks = chunker.chunk_from_blocks(blocks, "DOC_CLAUSE_SINGLE", 1)
        clause_chunks = [c for c in chunks if c.chunk_type == "clause_chunk"]
        assert len(clause_chunks) == 1
        assert "[上文]" not in clause_chunks[0].chunk_text


class TestL6ContentFixes:
    """Fix A (`[上文]` 移除) + Fix B (section_title 失稳治理) 回归 (2026-06-15 L6 A/B)。

    依据 docs/audits/L6_ab_prefix_section_findings_2026-06-15.md（recall A/B 通过）。
    """

    # ── Fix B 解析器单测 (req 3/4) ──
    def test_fixb_own_heading_overrides_stale_inherited(self):
        from opensearch_pipeline.chunker import _resolve_clause_section_title as R
        # 自身明确编号标题应覆盖被卡住的 inherited current_section
        assert R("1.2 打卡规定\n员工应按时打卡上下班。", "七、安全奖惩制度") == "1.2 打卡规定"
        assert R("第三章 安全管理\n本章规定。", "第一章 总则") == "第三章 安全管理"

    def test_fixb_blank_when_inherited_not_corroborated(self):
        from opensearch_pipeline.chunker import _resolve_clause_section_title as R
        # 正文讲合同谈判、inherited 是"生产车间职责"→无任何印证→留空（绝不保留错误标签）
        assert R("负责除销售采购合同以外的合同谈判工作。", "3.2 生产车间职责") is None

    def test_fixb_keeps_corroborated_inherited(self):
        from opensearch_pipeline.chunker import _resolve_clause_section_title as R
        # 无 own heading 但 inherited 被正文印证 → 沿用（不过度留空）
        assert R("急救药箱应配置常用药品并定期检查更换。", "5.2 急救药箱配置") == "5.2 急救药箱配置"

    def test_fixb_number_only_inherited_blanked(self):
        from opensearch_pipeline.chunker import _resolve_clause_section_title as R
        assert R("一些没有编号的正文内容描述。", "3.2.1") is None  # 标题无实义词，无法印证

    def test_fixb_long_numbered_clause_is_not_a_heading(self):
        from opensearch_pipeline.chunker import _leading_section_heading as H
        # 以编号开头的长条款正文不应被当成 own heading
        assert H("3.2.1 负责除销售、采购合同以外其他各类合同的谈判工作并据法务意见办理。") is None
        assert H("1.2 打卡规定\n正文") == "1.2 打卡规定"

    def test_fixb_long_chapter_line_not_heading_and_fits_column(self):
        # 长的 "一、…/第X章…" run-on 不当 heading，且 section_title 永不超 varchar(255)
        from opensearch_pipeline.chunker import (
            _leading_section_heading as H, _resolve_clause_section_title as R)
        long_line = "一、" + "甲方应按合同约定履行义务并承担相应责任" * 5  # >60 字
        assert H(long_line + "\n正文") is None
        out = R(long_line, "5.2 急救药箱配置")
        assert out is None or len(out) <= 60

    # ── _create_chunk type-guard: 仅 clause/text 被治理；其他类型 byte-equal (req 1/5) ──
    def test_fixb_only_affects_clause_text_types(self):
        ch = DocumentChunker(prepend_section=True, prepend_title=False)
        stale, body = "七、安全奖惩制度", "负责合同谈判与对外采购的协调工作安排。"
        for t in ("clause_chunk", "text_chunk", "section_chunk"):
            c = ch._create_chunk("D", 1, 0, t, body, section_title=stale, metadata={})
            assert c.section_title is None, f"{t} 失稳标签应被治理"
            assert "章节:" not in c.chunk_text
        # 未受影响类型：section_title 原样、prefix 仍含原章节（byte-equal 保证）
        for t in ("step_card", "table_chunk", "faq_chunk", "procedure_parent", "visual_knowledge"):
            c = ch._create_chunk("D", 1, 0, t, body, section_title=stale, metadata={})
            assert c.section_title == stale, f"{t} section_title 不应被改动"

    # ── Fix A end-to-end: clause 无 [上文] (req 2) ──
    def test_fixa_clause_no_shangwen_endtoend(self):
        ch = DocumentChunker(max_chunk_chars=500, min_chunk_chars=10,
                             overlap_chars=100, split_mode="clause")
        blocks = [ExtractedBlock(block_type="paragraph", text=f"第{n}条 内容内容内容内容。" * 3)
                  for n in ("一", "二", "三")]
        chunks = ch.chunk_from_blocks(blocks, "DOC_CLAUSE_FIXA", 1)
        clause = [c for c in chunks if c.chunk_type == "clause_chunk"]
        assert clause and all("[上文]" not in c.chunk_text for c in clause)

    # ── step doc 结构不变: step_no / image_refs / parent 链接 / chunk_index (req 5) ──
    def test_step_doc_structure_unchanged(self):
        ch = DocumentChunker(max_chunk_chars=2000, min_chunk_chars=10,
                             overlap_chars=0, split_mode="step")
        blocks = [
            ExtractedBlock(block_type="paragraph", text="步骤1. 打开阀门并检查压力表读数是否正常"),
            {"block_type": "image_ref", "text": "", "page_num": 1, "section_path": None,
             "source": "multimodal", "extra": {"image_index": 0, "source_image": "img.png",
             "oss_key": "key", "ocr_text": "阀门操作说明", "visual_summary": "操作面板视图"}},
            ExtractedBlock(block_type="paragraph", text="步骤2. 确认读数后关闭阀门并记录数据"),
            ExtractedBlock(block_type="paragraph", text="步骤3. 清理现场并签字确认完成操作"),
        ]
        chunks = ch.chunk_from_blocks(blocks, "DOC_STEP_FIX", 1)
        step_cards = [c for c in chunks if c.chunk_type == "step_card"]
        assert step_cards, "step doc 应产出 step_card"
        # step_no 仍存在
        assert all(c.extra.get("step_no") is not None for c in step_cards)
        # image_refs 仍绑定到带图的 step_card（契约键不变）
        assert any(c.extra.get("image_refs") for c in step_cards)
        # chunk_index 连续且无重复（Fix A/B 不触碰；parent 取尾号，列表顺序≠索引序）
        idxs = [c.chunk_index for c in chunks]
        assert len(idxs) == len(set(idxs))               # 无重复
        assert sorted(idxs) == list(range(len(idxs)))    # 连续 0..n-1
        # 父子链接完好
        parents = [c for c in chunks if c.chunk_type == "procedure_parent"]
        if parents:
            pids = {p.chunk_id for p in parents}
            linked = [c for c in step_cards if c.extra.get("parent_chunk_id")]
            assert linked and all(c.extra["parent_chunk_id"] in pids for c in linked)


class TestProcedureParentEnrichment:
    """Fix 4: procedure_parent embedding 质量提升测试。"""

    def test_parent_includes_preamble_summary(self):
        """procedure_parent 应包含前导文本中的目的/范围描述。"""
        chunker = DocumentChunker(
            max_chunk_chars=800,
            min_chunk_chars=10,
            overlap_chars=0,
            split_mode="step",
        )
        blocks = [
            ExtractedBlock(
                block_type="paragraph",
                text="本文档的目的是规范涂布工序的标准操作流程，适用于所有涂布车间。"
            ),
            ExtractedBlock(block_type="paragraph", text="步骤1. 准备涂布材料"),
            ExtractedBlock(block_type="paragraph", text="检查涂布液浓度是否达标。"),
            ExtractedBlock(block_type="paragraph", text="步骤2. 启动涂布机"),
            ExtractedBlock(block_type="paragraph", text="按下启动按钮，等待机器预热。"),
        ]
        chunks = chunker.chunk_from_blocks(blocks, "DOC_PARENT_ENR", 1)
        parent_chunks = [c for c in chunks if c.chunk_type == "procedure_parent"]
        
        assert len(parent_chunks) == 1
        parent = parent_chunks[0]
        # 应包含目的/范围的前导文本
        assert "目的" in parent.chunk_text or "涂布工序" in parent.chunk_text
        # 仍应包含步骤列表
        assert "步骤1" in parent.chunk_text
        assert "步骤2" in parent.chunk_text

    def test_parent_fallback_to_first_preamble(self):
        """无关键词命中时，应取第一段前导文本。"""
        chunker = DocumentChunker(
            max_chunk_chars=800,
            min_chunk_chars=10,
            overlap_chars=0,
            split_mode="step",
        )
        blocks = [
            ExtractedBlock(
                block_type="paragraph",
                text="涂布工序操作手册，版本号V2.3，编制日期2024年1月。"
            ),
            ExtractedBlock(block_type="paragraph", text="步骤1. 准备材料"),
            ExtractedBlock(block_type="paragraph", text="步骤2. 开始涂布"),
        ]
        chunks = chunker.chunk_from_blocks(blocks, "DOC_PARENT_FB", 1)
        parent_chunks = [c for c in chunks if c.chunk_type == "procedure_parent"]
        
        assert len(parent_chunks) == 1
        parent = parent_chunks[0]
        # 应包含第一段前导文本作为 fallback
        assert "涂布工序操作手册" in parent.chunk_text



class TestDottedStepNumbers:
    """X.Y 条款编号步骤（known-open 修复 2026-06-10）：int("4.1") 必然失败，
    旧 fallback 乱给 step_no；现在 step_no=文档内 ordinal（检索端排序/±1 扩展
    窗依赖紧凑稳定），原文编号存 section_no（heading 派生步骤的既有契约键）。"""

    def _chunker(self):
        return DocumentChunker(max_chunk_chars=800, min_chunk_chars=10,
                               overlap_chars=0, split_mode="step")

    def test_dotted_steps_get_ordinal_and_section_no(self):
        blocks = [
            ExtractedBlock(block_type="paragraph",
                           text="4.1 检查模具表面是否清洁，确认无残留物后方可进入下一步。"),
            ExtractedBlock(block_type="paragraph",
                           text="4.2 将原料倒入进料口，注意控制送料速度避免溢出堵塞。"),
            ExtractedBlock(block_type="paragraph",
                           text="4.3 启动设备并观察运行状态，记录首件检验结果数据。"),
        ]
        chunks = self._chunker().chunk_from_blocks(blocks, "DOTTED01", 1)
        cards = [c for c in chunks if c.chunk_type == "step_card"]
        assert len(cards) == 3, f"应产生 3 张步骤卡，实际 {len(cards)}"
        assert [c.extra["step_no"] for c in cards] == [1, 2, 3], (
            "X.Y 步骤的 step_no 应为文档内 ordinal"
            f"，实际 {[c.extra['step_no'] for c in cards]}")
        assert [c.extra.get("section_no") for c in cards] == ["4.1", "4.2", "4.3"], \
            "原文编号必须存 section_no 供展示层还原"

    def test_sub_step_nests_under_dotted_main(self):
        """X.Y 主步骤之后的 N) 子项：沿用该卡的 ordinal step_no + sub_step_no。"""
        blocks = [
            ExtractedBlock(block_type="paragraph",
                           text="4.1 检查模具表面是否清洁，确认无残留物后方可进入下一步。"),
            ExtractedBlock(block_type="paragraph",
                           text="4.2 完成进料操作前的各项准备工作，依次执行以下子项。"),
            ExtractedBlock(block_type="paragraph",
                           text="1) 打开进料阀门并确认压力表读数在正常范围之内。"),
        ]
        chunks = self._chunker().chunk_from_blocks(blocks, "DOTTED02", 1)
        cards = [c for c in chunks if c.chunk_type == "step_card"]
        subs = [c for c in cards if c.extra.get("sub_step_no") is not None]
        assert len(subs) == 1, "1) 子项应产生一张子步骤卡"
        main_42 = [c for c in cards if c.extra.get("section_no") == "4.2"][0]
        assert subs[0].extra["step_no"] == main_42.extra["step_no"], \
            "子项必须沿用 4.2 卡的 ordinal step_no（否则与主步骤碰撞）"
        assert subs[0].extra["sub_step_no"] == 1

    def test_plain_steps_unaffected(self):
        """显式 步骤N 文档：行为与修复前完全一致（无 section_no）。"""
        blocks = [
            ExtractedBlock(block_type="paragraph", text="步骤1. 准备涂布材料并核对清单。"),
            ExtractedBlock(block_type="paragraph", text="步骤2. 启动涂布机并等待预热完成。"),
        ]
        chunks = self._chunker().chunk_from_blocks(blocks, "PLAIN01", 1)
        cards = [c for c in chunks if c.chunk_type == "step_card"]
        assert [c.extra["step_no"] for c in cards] == [1, 2]
        assert all(not c.extra.get("section_no") for c in cards)

    def test_format_context_prefers_section_no(self):
        """serving 展示：section_no 存在时显示 步骤4.1 而非 ordinal 步骤5。"""
        from opensearch_pipeline import llm_generator as G
        ctx = G._format_context([{
            "title": "注塑成型作业指导书", "chunk_text": "检查模具表面是否清洁。",
            "chunk_type": "step_card", "step_no": 5, "section_no": "4.1",
            "score": 8.0,
        }])
        assert "步骤4.1" in ctx and "步骤5" not in ctx
        ctx2 = G._format_context([{
            "title": "注塑成型作业指导书", "chunk_text": "检查模具表面是否清洁。",
            "chunk_type": "step_card", "step_no": 5, "score": 8.0,
        }])
        assert "步骤5" in ctx2, "无 section_no 时行为不变"


class TestOpenSearchDocServingContract:
    """to_opensearch_doc 必须携带本地检索 _source 请求的 serving 契约字段。

    retriever._search_chunks_opensearch 的 _source 列表请求 chunk_id/chunk_index/
    source_image/visual_summary；缺失会导致邻居拼接失效、图片不可达
    （local_e2e_20260610 报告 artifact-3 的根因）。
    """

    def _chunk(self, **extra):
        c = Chunk(
            chunk_id="DOC_X_v1_c003", doc_id="DOC_X", version_no=1,
            chunk_index=3, chunk_type="step_card",
            chunk_text="步骤内容", token_count=4, page_num=2,
            permission_level="public", extra=extra or None,
        )
        return c

    def test_chunk_id_and_chunk_index_present(self):
        doc = self._chunk().to_opensearch_doc()
        assert doc["chunk_id"] == "DOC_X_v1_c003"
        assert doc["chunk_index"] == 3
        assert doc["id"] == "DOC_X_v1_c003", "保留 id 字段（bulk _id 兼容）"

    def test_multimodal_fields_passthrough(self):
        doc = self._chunk(
            source_image="processing/assets/d/DOC_X/v1/p2_img1.png",
            visual_summary="U8 登录界面截图",
        ).to_opensearch_doc()
        assert doc["source_image"] == "processing/assets/d/DOC_X/v1/p2_img1.png"
        assert doc["visual_summary"] == "U8 登录界面截图"

    def test_parity_with_ha3_doc_on_serving_fields(self):
        """除引擎特有字段外，serving 消费的字段两种序列化都要有。"""
        c = self._chunk(source_image="a.png", visual_summary="vs")
        os_doc, ha3_doc = c.to_opensearch_doc(), c.to_ha3_doc()
        for f in ("chunk_id", "doc_id", "chunk_index", "chunk_type",
                  "source_image", "visual_summary", "page_num", "section_title"):
            assert f in os_doc and f in ha3_doc, f"serving 字段 {f} 缺失"
