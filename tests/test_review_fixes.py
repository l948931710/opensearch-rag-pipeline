# -*- coding: utf-8 -*-
"""Regression tests for code-review fixes #2, #4, #1 (multimodal / serving arc).

#2  chunker._dedup_table_chunks must NOT drop distinct data tables that share a
    header row; it may only collapse repeated page-header/footer tables.
#4  content_blocks_builder must surface step_card image captions (stored under the
    `caption` key by the chunker), not read the always-empty `ocr_text`.
#1  PPTX `visual_knowledge` slide images must be indexable (top-level source_image)
    and recognized by the retriever (cover-demotion + neighbor-stitch allowlists).
"""
import os

os.environ.setdefault("RAG_SIMULATE", "true")

from opensearch_pipeline.chunker import DocumentChunker, Chunk
from opensearch_pipeline.extraction.schema import ExtractedBlock
from opensearch_pipeline.content_blocks_builder import _extract_image_chunks
from opensearch_pipeline import retriever
import opensearch_pipeline.pipeline_nodes as pn


def _table(text: str) -> Chunk:
    return Chunk(chunk_id="t", doc_id="d", version_no=1, chunk_index=0,
                 chunk_type="table_chunk", chunk_text=text, token_count=1)


# ─────────────────────────────────────────────────────────────────────
# #2  _dedup_table_chunks — data-loss regression
# ─────────────────────────────────────────────────────────────────────
class TestDedupTableChunks:
    def test_distinct_data_tables_sharing_header_are_kept(self):
        t1 = _table("| 项目 | 标准 | 结果 |\n| 外观 | 无毛刺 | 合格 |\n| 颜色 | 乳白 | 合格 |")
        t2 = _table("| 项目 | 标准 | 结果 |\n| 尺寸 | 200±1mm | 合格 |\n| 重量 | 5±0.2g | 合格 |")
        out = DocumentChunker._dedup_table_chunks([t1, t2])
        assert len(out) == 2, "distinct data tables sharing a header must NOT be deduped"
        joined = "".join(c.chunk_text for c in out)
        assert "尺寸" in joined and "外观" in joined

    def test_distinct_tables_via_chunk_from_blocks(self):
        ch = DocumentChunker(split_mode="text", min_chunk_chars=5)
        blocks = [
            ExtractedBlock(block_type="heading", text="检验记录", level=1, section_path="检验记录"),
            ExtractedBlock(block_type="table",
                           text="| 项目 | 标准 | 结果 |\n| 外观 | 无毛刺 | 合格 |\n| 颜色 | 乳白 | 合格 |"),
            ExtractedBlock(block_type="paragraph", text="第二批次检验数据如下所示。" * 2),
            ExtractedBlock(block_type="table",
                           text="| 项目 | 标准 | 结果 |\n| 尺寸 | 200±1mm | 合格 |\n| 重量 | 5±0.2g | 合格 |"),
        ]
        tables = [c for c in ch.chunk_from_blocks(blocks, "DOC", 1) if c.chunk_type == "table_chunk"]
        assert len(tables) == 2
        assert "尺寸" in "".join(c.chunk_text for c in tables)

    def test_repeated_page_header_tables_collapse(self):
        # short + pagination marker, differ only by page number → collapse to 1
        def mk(p):
            return _table(f"| 文件编号 | 版本 | 页码 |\n| FL-001 | V2 | 第 {p} 页 共 5 页 |")
        out = DocumentChunker._dedup_table_chunks([mk(1), mk(2), mk(3)])
        assert len(out) == 1, "repeated page-header tables differing only by page number must collapse"

    def test_single_table_kept(self):
        assert len(DocumentChunker._dedup_table_chunks([_table("| 项目 | 标准 |\n| 外观 | 无毛刺 |")])) == 1

    def test_short_tables_without_pagination_marker_kept(self):
        # short tables with a 编号 column but NO pagination phrase must never collapse
        a = _table("| 编号 | 数量 |\n| A1 | 100 |")
        b = _table("| 编号 | 数量 |\n| A2 | 200 |")
        assert len(DocumentChunker._dedup_table_chunks([a, b])) == 2

    def test_data_tables_with_page_marker_differing_only_in_numbers_kept(self):
        # adversarial gap: two per-page summaries that DO carry a page marker but whose
        # data numbers differ (1200 vs 3400) must NOT collapse — page phrase is normalized,
        # but other digits are preserved.
        t1 = _table("| 日产量 | 共 5 页 |\n| 1200 | 第 1 页 |")
        t2 = _table("| 日产量 | 共 5 页 |\n| 3400 | 第 2 页 |")
        out = DocumentChunker._dedup_table_chunks([t1, t2])
        assert len(out) == 2, "tables differing only in data numbers must be kept"
        assert "3400" in "".join(c.chunk_text for c in out)

    def test_header_tables_with_different_doc_numbers_kept(self):
        # adversarial gap: same page number, different doc numbers (FL-001 vs FL-002)
        # must NOT collapse (digit-strip used to erase 001/002).
        t1 = _table("| 文件编号 | 页码 |\n| FL-001 | 第 1 页 |")
        t2 = _table("| 文件编号 | 页码 |\n| FL-002 | 第 1 页 |")
        assert len(DocumentChunker._dedup_table_chunks([t1, t2])) == 2

    def test_image_bearing_table_never_deduped(self):
        # adversarial gap: a caption-less image-bearing header table must never be dropped,
        # even if its text matches an earlier plain header (protects image_refs contract).
        plain = _table("| 文件编号 | 页码 |\n| FL-001 | 第 1 页 |")
        with_img = Chunk(chunk_id="img", doc_id="d", version_no=1, chunk_index=1,
                         chunk_type="table_chunk",
                         chunk_text="| 文件编号 | 页码 |\n| FL-001 | 第 2 页 |", token_count=1,
                         extra={"image_refs": [{"oss_key": "diagram.png"}]})
        out = DocumentChunker._dedup_table_chunks([plain, with_img])
        oss = [r.get("oss_key") for c in out for r in (c.extra.get("image_refs") or [])]
        assert "diagram.png" in oss, "image-bearing table must survive dedup"

    def test_non_table_chunks_untouched(self):
        c1 = Chunk(chunk_id="x", doc_id="d", version_no=1, chunk_index=0,
                   chunk_type="text_chunk", chunk_text="同样的首行", token_count=1)
        c2 = Chunk(chunk_id="y", doc_id="d", version_no=1, chunk_index=1,
                   chunk_type="text_chunk", chunk_text="同样的首行", token_count=1)
        assert len(DocumentChunker._dedup_table_chunks([c1, c2])) == 2


# ─────────────────────────────────────────────────────────────────────
# #4  step_card caption surfacing
# ─────────────────────────────────────────────────────────────────────
class TestStepCardCaption:
    def test_caption_surfaces_from_caption_key(self):
        chunk = {"chunk_type": "step_card", "title": "步骤1",
                 "image_refs": [{"oss_key": "k.png", "caption": "电子天平归零示意"}]}
        imap = _extract_image_chunks([chunk])
        assert imap[1][0]["visual_summary"] == "电子天平归零示意"
        assert imap[1][0]["source_image"] == "k.png"

    def test_caption_surfaces_from_retriever_rebuilt_shape(self):
        # retriever rebuild yields {oss_key, ocr_text:"", caption, order}
        chunk = {"chunk_type": "step_card", "title": "步骤2",
                 "image_refs": [{"oss_key": "k2.png", "ocr_text": "", "caption": "称量盘清洁", "order": 0}]}
        imap = _extract_image_chunks([chunk])
        assert imap[1][0]["visual_summary"] == "称量盘清洁"

    def test_falls_back_to_ocr_text_when_no_caption(self):
        chunk = {"chunk_type": "step_card", "title": "步骤3",
                 "image_refs": [{"oss_key": "k3.png", "ocr_text": "OCR文本"}]}
        imap = _extract_image_chunks([chunk])
        assert imap[1][0]["visual_summary"] == "OCR文本"


# ─────────────────────────────────────────────────────────────────────
# #1  PPTX visual_knowledge serving
# ─────────────────────────────────────────────────────────────────────
class TestVisualKnowledgeServing:
    def test_pptx_slide_image_hoisted_to_source_image(self):
        """Drive the real node_chunk_documents PPTX path → visual_knowledge chunk
        must carry a top-level source_image so to_ha3_doc indexes it."""
        doc = {
            "doc_id": "PPTX_001", "version_no": 1, "file_ext": "pptx",
            "title": "产品介绍", "category_l1": "", "category_l2": "",
            "text": "幻灯片", "owner_dept": "sales",
            "source_key": "raw/sales/PPTX_001.pptx", "canonical_key": "canonical/PPTX_001.json",
            "blocks": [ExtractedBlock(block_type="paragraph",
                                      text="第一页产品介绍内容描述说明。" * 2, page_num=1)],
            "assets": [{"status": "ROUTE_TO_VECTOR", "page_num": 1, "filename": "slide1.png",
                        "image_category": "product", "visual_summary": "产品外观图", "ocr_text": ""}],
        }
        ctx = {"canonicals": [doc], "split_mode": "dynamic"}
        pn.node_chunk_documents(ctx)
        vk = [c for c in ctx["chunks"] if c.chunk_type == "visual_knowledge"]
        assert vk, "PPTX slide with a ROUTE_TO_VECTOR image should produce a visual_knowledge chunk"
        c = vk[0]
        assert c.extra.get("source_image"), "visual_knowledge chunk must carry top-level source_image"
        assert c.extra.get("image_refs"), "image_refs should still be present"
        ha3 = c.to_ha3_doc()
        assert ha3.get("source_image"), "to_ha3_doc must index source_image so the image is retrievable"

    def test_visual_knowledge_renders_from_source_image(self):
        chunk = {"chunk_type": "visual_knowledge", "title": "幻灯片",
                 "source_image": "processing/assets/slide1.png", "visual_summary": "产品外观图"}
        imap = _extract_image_chunks([chunk])
        assert imap[1][0]["source_image"] == "processing/assets/slide1.png"

    def test_stitch_skips_visual_knowledge(self, monkeypatch):
        """visual_knowledge is a complete unit → must be skipped by neighbor stitching
        (otherwise its image-bearing text gets mangled with ±1 neighbor prose)."""
        class _Cur:
            def execute(self, *a, **k):
                pass

            def fetchall(self):
                # 批量化后查询会 SELECT doc_id 并按 doc_id 分组；chunk_index 落在中心 5 的 ±1 内
                return [{"doc_id": "D", "chunk_index": 5, "chunk_text": "邻居文本", "section_title": ""}]

            def close(self):
                pass

        class _Conn:
            def cursor(self, *a, **k):
                return _Cur()

            def close(self):
                pass

        monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: _Conn())

        chunks = [
            {"chunk_type": "text_chunk", "doc_id": "D", "chunk_index": 5, "chunk_text": "原文"},
            {"chunk_type": "visual_knowledge", "doc_id": "D", "chunk_index": 9,
             "chunk_text": "图片说明", "source_image": "x.png"},
        ]
        out = retriever.stitch_neighbor_chunks(chunks, window=1)
        by_type = {c["chunk_type"]: c for c in out}
        assert by_type["text_chunk"].get("_stitched") is True, "normal chunk should be stitched"
        assert "_stitched" not in by_type["visual_knowledge"], "visual_knowledge must be skipped"
        assert by_type["visual_knowledge"]["chunk_text"] == "图片说明", "skipped chunk text untouched"

    def test_visual_knowledge_in_cover_demotion_allowlist(self):
        """A short, section-less visual_knowledge chunk must not be demoted to the tail
        like a cover page (it carries a load-bearing image). Cover-demotion lives inline
        in search_chunks and needs a live HA3 client to exercise end-to-end, so guard the
        allowlist membership at the source level."""
        import inspect
        src = inspect.getsource(retriever.search_chunks)
        assert '"visual_knowledge"' in src and "封面降权" in src, \
            "visual_knowledge must be in the cover-demotion allowlist inside search_chunks"

    @staticmethod
    def _mock_vk_db(monkeypatch, refs_by_id):
        """Mock _get_db_conn so the visual_knowledge branch returns image_refs_json per chunk_id."""
        import json as _json

        class _Cur:
            def __init__(self):
                self._cid = None

            def execute(self, sql, params=None):
                self._cid = (params or [None])[0]

            def fetchone(self):
                refs = refs_by_id.get(self._cid)
                return {"image_refs_json": _json.dumps(refs)} if refs is not None else None

            def fetchall(self):
                return []

            def close(self):
                pass

        class _Conn:
            def cursor(self, *a, **k):
                return _Cur()

            def close(self):
                pass

        monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: _Conn())

    def test_ha3_parser_exposes_chunk_id(self):
        """Root fix: _parse_ha3_response must surface chunk_id (RDS-rebuild key + dedup key)."""
        from types import SimpleNamespace
        resp = SimpleNamespace(body={"result": [
            {"fields": {"chunk_id": "DOC1_0", "chunk_type": "step_card",
                        "chunk_text_store": "步骤文本", "doc_id": "DOC1"}, "score": 0.9}
        ]})
        out = retriever._parse_ha3_response(resp)
        assert out[0]["chunk_id"] == "DOC1_0"

    def test_visual_knowledge_multi_image_rehydrated(self, monkeypatch):
        """Multi-image slide: HA3 returns only the primary source_image; expand_step_context
        re-hydrates ALL image_refs from RDS by chunk_id so images 2..N are servable."""
        self._mock_vk_db(monkeypatch, {"VK1": [
            {"oss_key": "img1.png", "visual_summary": "图1"},
            {"oss_key": "img2.png", "visual_summary": "图2"},
        ]})
        chunks = [{"chunk_type": "visual_knowledge", "chunk_id": "VK1",
                   "source_image": "img1.png", "chunk_text": "幻灯片", "score": 1.0}]
        out = retriever.expand_step_context(chunks, query="产品图")
        vk = [c for c in out if c.get("chunk_type") == "visual_knowledge"][0]
        assert {r["oss_key"] for r in vk.get("image_refs", [])} == {"img1.png", "img2.png"}

    def test_type1_visual_knowledge_keeps_source_image_when_no_rds_refs(self, monkeypatch):
        """Chunker step-mode visual_knowledge (single image via source_image, NULL
        image_refs_json in RDS) must survive the re-hydration branch unchanged and still
        render via its source_image fallback."""
        self._mock_vk_db(monkeypatch, {})  # RDS returns no image_refs_json row
        chunks = [{"chunk_type": "visual_knowledge", "chunk_id": "VK_T1",
                   "source_image": "ref.png", "chunk_text": "[参考图] 示意", "score": 0.9}]
        out = retriever.expand_step_context(chunks, query="图")
        vk = [c for c in out if c.get("chunk_type") == "visual_knowledge"][0]
        assert vk["source_image"] == "ref.png", "single-image VK must keep its source_image fallback"
        assert not vk.get("image_refs"), "no spurious image_refs when RDS has none"
        # and it still renders:
        assert _extract_image_chunks([vk])[1][0]["source_image"] == "ref.png"

    def test_distinct_visual_knowledge_chunks_not_collapsed(self, monkeypatch):
        """Root-fix regression: with chunk_id present, the expand_step_context dedup must
        keep distinct chunks (previously chunk_id-less chunks all collapsed to one)."""
        self._mock_vk_db(monkeypatch, {"VK1": [{"oss_key": "a.png"}], "VK2": [{"oss_key": "b.png"}]})
        chunks = [
            {"chunk_type": "visual_knowledge", "chunk_id": "VK1", "source_image": "a.png",
             "chunk_text": "slide A", "score": 0.9},
            {"chunk_type": "visual_knowledge", "chunk_id": "VK2", "source_image": "b.png",
             "chunk_text": "slide B", "score": 0.8},
        ]
        out = retriever.expand_step_context(chunks, query="图")
        ids = {c.get("chunk_id") for c in out if c.get("chunk_type") == "visual_knowledge"}
        assert ids == {"VK1", "VK2"}, "distinct visual_knowledge chunks must not collapse in dedup"


# ─────────────────────────────────────────────────────────────────────
# #6  numbered structural section headers must not become step cards
# ─────────────────────────────────────────────────────────────────────
class TestStepSectionHeaders:
    def setup_method(self):
        self.ch = DocumentChunker(split_mode="step", min_chunk_chars=5)

    def test_numbered_section_headers_not_steps(self):
        blocks = [
            ExtractedBlock(block_type="heading", text="1 目的", level=1, section_path="1 目的"),
            ExtractedBlock(block_type="paragraph", text="本文件规定了相关操作的目的与意义。" * 2),
            ExtractedBlock(block_type="heading", text="2 范围", level=1, section_path="2 范围"),
            ExtractedBlock(block_type="paragraph", text="适用于所有相关工序的操作过程。" * 2),
            ExtractedBlock(block_type="paragraph", text="步骤1：打开设备电源开关并等待自检完成。"),
            ExtractedBlock(block_type="paragraph", text="步骤2：核对仪表读数是否在范围内。"),
        ]
        step_cards = [c for c in self.ch.chunk_from_blocks(blocks, "D", 1)
                      if c.chunk_type == "step_card"]
        # section headers must NOT become step cards
        assert not any(c.chunk_text.lstrip().startswith(("1 目的", "2 范围")) for c in step_cards)
        # and none rendered with the spurious step_no=0 from a section heading
        assert all(c.extra.get("step_no") for c in step_cards), "no step_no=0 section-header cards"
        # real steps still detected with real numbers
        nos = sorted(c.extra.get("step_no") for c in step_cards if c.extra.get("step_no"))
        assert 1 in nos and 2 in nos

    def test_multilevel_numbered_heading_still_a_step(self):
        blocks = [
            ExtractedBlock(block_type="heading", text="3.2.4 正常单据记账", level=2, section_path="3.2.4"),
            ExtractedBlock(block_type="paragraph", text="在系统中录入单据并保存凭证信息。" * 2),
        ]
        step_cards = [c for c in self.ch.chunk_from_blocks(blocks, "D", 1)
                      if c.chunk_type == "step_card"]
        assert step_cards, "multi-level numbered operational heading must still be a step"
        assert any("正常单据记账" in c.chunk_text for c in step_cards)


# ─────────────────────────────────────────────────────────────────────
# #5  trailing orphan image after the last step must be flushed, not dropped
# ─────────────────────────────────────────────────────────────────────
class TestStepOrphanImageFlush:
    def test_trailing_image_after_crosspage_table_is_bound(self):
        ch = DocumentChunker(split_mode="step", min_chunk_chars=5)
        blocks = [
            ExtractedBlock(block_type="heading", text="1 准备操作", level=1, page_num=1,
                           section_path="1 准备操作"),
            ExtractedBlock(block_type="paragraph", text="先完成准备工作的详细描述内容。" * 2, page_num=1),
            ExtractedBlock(block_type="table", text="| 列 |\n| 值 |", page_num=2),  # cross-page → closes step
            ExtractedBlock(block_type="image_ref", text="", page_num=2,
                           extra={"oss_key": "trailing.png", "source_image": "trailing.png"}),
        ]
        chunks = ch.chunk_from_blocks(blocks, "D", 1)
        bound = [r.get("oss_key") or r.get("source_image")
                 for c in chunks for r in (c.extra.get("image_refs") or [])]
        assert "trailing.png" in bound, "trailing orphan image must be flushed into the last step"


# ─────────────────────────────────────────────────────────────────────
# #3  image-only PPTX slide must still produce a chunk carrying its image
# ─────────────────────────────────────────────────────────────────────
class TestImageOnlySlide:
    def test_image_only_slide_gets_visual_knowledge_chunk(self):
        doc = {
            "doc_id": "PPTX_IMG", "version_no": 1, "file_ext": "pptx",
            "title": "产品手册", "category_l1": "", "category_l2": "",
            "text": "幻灯片", "owner_dept": "sales",
            "source_key": "raw/sales/PPTX_IMG.pptx", "canonical_key": "canonical/PPTX_IMG.json",
            "blocks": [ExtractedBlock(block_type="paragraph",
                                      text="第一页文字内容描述说明。" * 2, page_num=1)],
            "assets": [
                {"status": "ROUTE_TO_VECTOR", "page_num": 1, "filename": "s1.png",
                 "image_category": "product", "visual_summary": "封面图", "ocr_text": ""},
                {"status": "ROUTE_TO_VECTOR", "page_num": 2, "filename": "s2.png",
                 "image_category": "product", "visual_summary": "产品外观图", "ocr_text": ""},
            ],
        }
        ctx = {"canonicals": [doc], "split_mode": "dynamic"}
        pn.node_chunk_documents(ctx)
        vk = [c for c in ctx["chunks"] if c.chunk_type == "visual_knowledge"]
        pages = {c.page_num for c in vk}
        assert 2 in pages, "image-only slide (page 2) must produce a visual_knowledge chunk"
        s2 = [c for c in vk if c.page_num == 2][0]
        assert "s2.png" in (s2.extra.get("source_image") or ""), "image-only slide must carry source_image"
