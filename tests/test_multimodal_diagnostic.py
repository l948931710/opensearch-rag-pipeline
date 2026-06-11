# -*- coding: utf-8 -*-
"""
test_multimodal_diagnostic.py — 多模态管线行为契约测试

最初是多模态修复前的"现状诊断"快照；多模态管线落地后升级为当前契约的回归测试：
  1. 现有 test data (normal/sensitive/multi) 确实不含图片
  2. 含图片 asset 的 mock task 可以正确创建 image chunk（[图片描述] + 文档标题前缀）
  3. to_ha3_doc() 携带 source_image / visual_summary 元数据（多模态修复后的契约）
  4. 独立图像向量（One-Peace source_image_vector）已废弃：图片 chunk 经 chunk_text
     走统一 text-embedding-v4 路径，不再依赖 local_path 本地文件
"""

import os
import tempfile
import pytest
from PIL import Image

from opensearch_pipeline.dag_definitions import (
    build_dag1_raw_to_canonical,
    build_dag2_canonical_to_chunk,
    build_dag3_chunk_to_opensearch,
)
from opensearch_pipeline.run_simulation import get_test_data
from opensearch_pipeline.chunker import Chunk


# ═══════════════════════════════════════════════════════════════
# 测试组 1：验证现有数据全部是纯文本
# ═══════════════════════════════════════════════════════════════

class TestCurrentPipelineTextOnly:
    """验证现有管线数据中不包含任何图像 chunk。"""

    @pytest.mark.parametrize("scenario", ["normal", "sensitive", "multi"])
    def test_existing_scenarios_produce_zero_image_chunks(self, scenario):
        """各 scenario 的 mock_text 全是 Markdown，不含 image asset。"""
        ctx = get_test_data(scenario)
        ctx = build_dag1_raw_to_canonical().run(ctx)
        ctx = build_dag2_canonical_to_chunk().run(ctx)

        chunks = ctx.get("valid_chunks", ctx.get("chunks", []))
        image_chunks = [c for c in chunks if c.chunk_type == "image"]
        assert len(image_chunks) == 0, (
            f"Scenario '{scenario}' unexpectedly produced {len(image_chunks)} image chunks"
        )
        print(f"\n  ✅ Scenario '{scenario}': {len(chunks)} chunks, 0 image chunks — confirmed text-only")


# ═══════════════════════════════════════════════════════════════
# 测试组 2：带图片 asset 的 mock data 验证 image chunk 创建
# ═══════════════════════════════════════════════════════════════

MOCK_TEXT_WITH_IMAGE_REF = """
# 注塑车间工艺流程图

## 一、概述

本文档展示了注塑车间的完整工艺流程。

## 二、流程

1. 原料准备 → 烘干 → 注塑 → 冷却 → 检验

具体工艺参数请参考随附流程图。
"""


class TestImageChunkCreation:
    """验证 node_chunk_documents 对 ROUTE_TO_VECTOR asset 的 image chunk 创建逻辑。"""

    def test_route_to_vector_asset_creates_image_chunk(self):
        """当 canonical 中有 status=ROUTE_TO_VECTOR 的 asset 时，应创建 chunk_type='image' 的 chunk。"""
        from opensearch_pipeline.pipeline_nodes import node_chunk_documents

        canonical = {
            "doc_id": "DOC_DIAG_IMG_001",
            "version_no": 1,
            "text": MOCK_TEXT_WITH_IMAGE_REF,
            "title": "注塑车间工艺流程图.docx",
            "owner_dept": "production",
            "source_key": "raw/production/注塑车间工艺流程图.docx",
            "category_l1": "sop",
            "category_l2": "equipment_sop",
            "permission_level": "public",
            "kb_type": "public",
            "risk_level": "low",
            "blocks": [
                {"text": "本文档展示了注塑车间的完整工艺流程。", "page_num": 1, "block_type": "paragraph"},
                {"text": "1. 原料准备 → 烘干 → 注塑 → 冷却 → 检验", "page_num": 1, "block_type": "paragraph"},
            ],
            "assets": [
                {
                    "filename": "workflow_diagram.png",
                    "status": "ROUTE_TO_VECTOR",
                    "visual_summary": "注塑车间工艺流程图，展示从原料到成品的完整流程",
                    "ocr_text": "原料 → 注塑 → 冷却",
                    "width": 800,
                    "height": 600,
                    "file_size_kb": 120.5,
                    "local_path": "/tmp/nonexistent/DOC_DIAG_IMG_001_workflow_diagram.png"
                }
            ],
        }

        ctx = {
            "canonicals": [canonical],
            "min_chunk_chars": 5,
        }

        node_chunk_documents(ctx)

        chunks = ctx["chunks"]
        image_chunks = [c for c in chunks if c.chunk_type == "image"]
        text_chunks = [c for c in chunks if c.chunk_type != "image"]

        # 应有至少 1 个 image chunk
        assert len(image_chunks) == 1, f"Expected 1 image chunk, got {len(image_chunks)}"
        img = image_chunks[0]

        # 验证 image chunk 的结构：标题前缀 + [图片描述] + visual_summary
        assert "[图片描述]" in img.chunk_text
        assert "【文档:注塑车间工艺流程图.docx】" in img.chunk_text
        assert "注塑车间工艺流程图" in img.chunk_text
        assert img.extra.get("source_image") is not None
        assert "workflow_diagram.png" in img.extra["source_image"]
        assert img.extra.get("visual_summary") == "注塑车间工艺流程图，展示从原料到成品的完整流程"
        # One-Peace 图像向量已废弃：extra 不再依赖 local_path / source_image_vector
        assert img.extra.get("source_image_vector") is None
        assert "local_path" not in img.extra

        print(f"\n  ✅ Image chunk created successfully:")
        print(f"     chunk_type = {img.chunk_type}")
        print(f"     chunk_text = {img.chunk_text[:80]}...")
        print(f"     source_image = {img.extra['source_image']}")
        print(f"     Text chunks: {len(text_chunks)}")


# ═══════════════════════════════════════════════════════════════
# 测试组 3：to_ha3_doc() 图像元数据字段验证（多模态修复后的契约）
# ═══════════════════════════════════════════════════════════════

class TestHA3DocFieldCoverage:
    """验证 to_ha3_doc() 携带图像元数据、且不携带已废弃的图像向量。"""

    def test_to_ha3_doc_carries_image_metadata_fields(self):
        """to_ha3_doc() 包含 source_image / visual_summary；source_image_vector（One-Peace）已废弃。"""
        img_chunk = Chunk(
            chunk_id="DIAG_IMG_CHUNK_001",
            doc_id="DOC_DIAG_001",
            version_no=1,
            chunk_index=5,
            chunk_type="image",
            chunk_text="[Image Schematic] 注塑车间工艺流程图",
            token_count=10,
            page_num=1,
            permission_level="public",
            extra={
                "source_image": "processing/assets/production/DOC_DIAG_001/v1/workflow.png",
                "visual_summary": "注塑车间工艺流程图",
                "source_image_vector": [0.1] * 768,
                "local_path": "/tmp/fake/workflow.png",
            }
        )
        # 给它加上文本 embedding 用于测试
        img_chunk.embedding_vector = [0.01] * 1024

        ha3_doc = img_chunk.to_ha3_doc(pk_field="id")
        os_doc = img_chunk.to_opensearch_doc()

        # ─── HA3 doc: 图像元数据透传（serving 真图渲染依赖 source_image）───
        ha3_fields = set(ha3_doc.keys())
        assert ha3_doc.get("source_image") == \
            "processing/assets/production/DOC_DIAG_001/v1/workflow.png", \
            "to_ha3_doc() 必须透传 extra.source_image（DingTalk 卡片/小程序真图渲染依赖它）"
        assert "visual_summary" in ha3_fields, "to_ha3_doc() 应透传 visual_summary"
        # One-Peace 独立图像向量已废弃，不应再进索引
        assert "source_image_vector" not in ha3_fields, \
            "source_image_vector（One-Peace）已废弃，不应出现在 HA3 doc"

        # chunk_type 字段应该存在
        assert "chunk_type" in ha3_fields, "to_ha3_doc() 应包含 chunk_type"
        assert ha3_doc["chunk_type"] == "image"

        print(f"\n  ✅ to_ha3_doc() 字段验证 — 图像元数据透传:")
        print(f"     HA3 doc fields: {sorted(ha3_fields)}")
        print(f"     source_image: ✅ 透传")
        print(f"     visual_summary: ✅ 透传")
        print(f"     source_image_vector: ❌ 已废弃（One-Peace）")
        print(f"     chunk_type: ✅ 已有 (value='{ha3_doc['chunk_type']}')")

        # ─── OpenSearch doc: 验证有条件输出 ───
        os_fields = set(os_doc.keys())
        has_image_vector = "source_image_vector" in os_fields
        print(f"\n  📋 to_opensearch_doc() 对比:")
        print(f"     source_image_vector: {'✅ 存在' if has_image_vector else '❌ 缺失'}")

    def test_to_ha3_doc_vs_to_opensearch_doc_field_diff(self):
        """对比两个序列化方法的字段差异，明确列出 gap。"""
        chunk = Chunk(
            chunk_id="DIAG_DIFF_001",
            doc_id="DOC_DIFF",
            version_no=1,
            chunk_index=0,
            chunk_type="image",
            chunk_text="[Image Schematic] test",
            token_count=3,
            page_num=1,
            permission_level="public",
            extra={
                "source_image": "assets/test.png",
                "source_image_vector": [0.5] * 768,
            }
        )
        chunk.embedding_vector = [0.1] * 1024
        chunk.sparse_vector_indices = [1, 5, 10]
        chunk.sparse_vector_values = [0.8, 0.3, 0.1]

        ha3 = chunk.to_ha3_doc()
        os_doc = chunk.to_opensearch_doc()

        ha3_keys = set(ha3.keys())
        os_keys = set(os_doc.keys())

        only_in_os = os_keys - ha3_keys
        only_in_ha3 = ha3_keys - os_keys

        print(f"\n  📊 字段差异分析:")
        print(f"     HA3 独有字段: {sorted(only_in_ha3)}")
        print(f"     OpenSearch 独有字段: {sorted(only_in_os)}")
        print(f"     共同字段: {sorted(ha3_keys & os_keys)}")

        # 关键断言：source_image_vector 只在 OpenSearch doc 中
        assert "source_image_vector" in only_in_os or "source_image_vector" not in os_keys


# ═══════════════════════════════════════════════════════════════
# 测试组 4：local_path 生命周期问题验证
# ═══════════════════════════════════════════════════════════════

class TestLocalPathLifecycle:
    """验证 local_path 在 embedding 阶段指向已删除文件的行为。"""

    def test_embedding_silently_skips_missing_local_path(self):
        """
        模拟 DAG 3 的 node_generate_embeddings 处理一个 local_path 指向不存在文件的 image chunk：
        不报错，文本 embedding 照常生成；One-Peace 图像向量已废弃，
        source_image_vector 保持 None（图片检索完全依赖 chunk_text 的统一文本向量）。
        """
        from opensearch_pipeline.pipeline_nodes import node_generate_embeddings

        img_chunk = Chunk(
            chunk_id="DIAG_LIFECYCLE_001",
            doc_id="DOC_LIFECYCLE",
            version_no=1,
            chunk_index=0,
            chunk_type="image",
            chunk_text="[Image Schematic] 测试流程图",
            token_count=5,
            page_num=1,
            permission_level="public",
            extra={
                "source_image": "assets/test.png",
                "source_image_vector": None,
                "local_path": "/tmp/rag_extract_DELETED_12345/DOC_LIFECYCLE_test.png"  # 不存在的路径
            }
        )

        ctx = {
            "valid_chunks": [img_chunk],
            "simulate_api": True,  # 使用模拟模式
        }

        node_generate_embeddings(ctx)

        # 模拟模式下文本 embedding 照常生成（统一 text-embedding 路径，维度跟随配置）
        from opensearch_pipeline.config import get_config
        assert img_chunk.embedding_vector is not None, "文本 embedding 应该已生成"
        assert len(img_chunk.embedding_vector) == get_config().embedding.dimension
        assert img_chunk.embedding_status == "DONE"

        # 关键检查：One-Peace 已废弃，source_image_vector 不再被填充（即使 local_path 失效也不报错）
        assert img_chunk.extra.get("source_image_vector") is None, \
            "One-Peace 图像向量已废弃，embedding 阶段不应再填充 source_image_vector"

        print(f"\n  ✅ 模拟模式: 统一文本向量已生成 ({len(img_chunk.embedding_vector)} 维)")
        print(f"     source_image_vector: None（One-Peace 已废弃）")
        print(f"     local_path: {img_chunk.extra['local_path']}")
        print(f"     local_path exists: {os.path.exists(img_chunk.extra['local_path'])}")

    def test_real_mode_skips_missing_image_file(self):
        """
        验证：如果 simulate_api=False 但 local_path 指向不存在的文件，
        os.path.exists 检查会导致整个 One-Peace 调用被静默跳过。
        这里我们不真正调用 API，而是验证逻辑走向。
        """
        # 只验证 os.path.exists 检查的行为
        fake_path = "/tmp/rag_extract_ALREADY_DELETED/test_image.png"
        assert not os.path.exists(fake_path), "Test setup: path should not exist"

        # 模拟 pipeline_nodes.py L2429-2430 的逻辑
        local_img_path = fake_path
        would_call_one_peace = os.path.exists(local_img_path)

        assert not would_call_one_peace, (
            "With deleted tmp_dir, os.path.exists returns False → One-Peace API call is silently skipped"
        )
        print(f"\n  ✅ 确认：local_path '{fake_path}' 不存在")
        print(f"     os.path.exists() = False → One-Peace 调用被静默跳过")
        print(f"     source_image_vector 将永远为 None（在生产模式下）")

    def test_tmp_dir_cleanup_timing(self):
        """验证 tempfile + shutil.rmtree 的时序：文件在 finally 块中立即被删除。"""
        import shutil

        # 模拟 node_extract_text_with_ocr 的 try/finally 模式
        tmp_dir = tempfile.mkdtemp(prefix="rag_diagnostic_")
        test_file = os.path.join(tmp_dir, "test_image.png")
        
        # 创建测试文件
        img = Image.new("RGB", (100, 100), color="red")
        img.save(test_file)
        assert os.path.exists(test_file), "File should exist after creation"

        # 保存路径引用（模拟 asset["local_path"]）
        saved_path = test_file

        # 模拟 finally 清理
        try:
            pass  # 正常处理
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # 验证文件已被删除，但路径字符串仍然存在
        assert not os.path.exists(saved_path), "File should be deleted after rmtree"
        assert saved_path != "", "Path string still exists as a dangling reference"
        
        print(f"\n  ✅ 时序验证:")
        print(f"     tmp_dir created: {tmp_dir}")
        print(f"     file created: {test_file}")
        print(f"     rmtree executed in finally block")
        print(f"     saved_path = '{saved_path}' (dangling reference)")
        print(f"     os.path.exists(saved_path) = False")


# ═══════════════════════════════════════════════════════════════
# 测试组 5：完整 pipeline 带图片的端到端模拟
# ═══════════════════════════════════════════════════════════════

class TestFullPipelineWithImageAsset:
    """端到端验证带图片 asset 的完整管线（模拟模式）。"""

    def test_full_pipeline_with_image_asset(self):
        """
        构造一个带 ROUTE_TO_VECTOR 图片 asset 的文档，
        跑完 DAG 1-3 的模拟模式，追踪 image chunk 的完整生命周期。
        """
        from opensearch_pipeline.pipeline_nodes import (
            node_chunk_documents,
            node_validate_chunks,
            node_generate_embeddings,
            node_build_opensearch_payload,
        )

        # ── Step 1: 构造带图片的 canonical ──
        canonical = {
            "doc_id": "DOC_E2E_IMG_001",
            "version_no": 1,
            "text": MOCK_TEXT_WITH_IMAGE_REF,
            "title": "注塑车间工艺流程图.docx",
            "owner_dept": "production",
            "source_key": "raw/production/注塑车间工艺流程图.docx",
            "category_l1": "sop",
            "category_l2": "equipment_sop",
            "permission_level": "public",
            "kb_type": "public",
            "risk_level": "low",
            "summary": "注塑车间工艺流程图",
            "blocks": [
                {"text": MOCK_TEXT_WITH_IMAGE_REF, "page_num": 1, "block_type": "paragraph"},
            ],
            "assets": [
                {
                    "filename": "workflow_diagram.png",
                    "status": "ROUTE_TO_VECTOR",
                    "visual_summary": "注塑车间工艺流程图，展示从原料到成品的完整生产流程",
                    "ocr_text": "原料 → 烘干 → 注塑 → 冷却 → 检验",
                    "width": 800,
                    "height": 600,
                    "file_size_kb": 120.5,
                    "local_path": "/tmp/rag_extract_FAKE/DOC_E2E_IMG_001_workflow_diagram.png"
                }
            ],
        }

        ctx = {
            "canonicals": [canonical],
            "min_chunk_chars": 5,
        }

        # ── Step 2: DAG 2 — Chunk Documents ──
        node_chunk_documents(ctx)
        
        all_chunks = ctx["chunks"]
        text_chunks = [c for c in all_chunks if c.chunk_type != "image"]
        image_chunks = [c for c in all_chunks if c.chunk_type == "image"]
        
        print(f"\n  📋 DAG 2 Chunk 结果:")
        print(f"     Total: {len(all_chunks)} | Text: {len(text_chunks)} | Image: {len(image_chunks)}")
        
        assert len(image_chunks) == 1, f"Expected 1 image chunk, got {len(image_chunks)}"

        # ── Step 3: DAG 2 — Validate Chunks ──
        node_validate_chunks(ctx)
        valid_chunks = ctx["valid_chunks"]
        valid_image_chunks = [c for c in valid_chunks if c.chunk_type == "image"]
        
        print(f"     After validation: {len(valid_chunks)} valid | {len(valid_image_chunks)} image")
        assert len(valid_image_chunks) == 1, "Image chunk should pass validation"

        # ── Step 4: DAG 3 — Generate Embeddings (模拟模式) ──
        ctx["simulate_api"] = True
        node_generate_embeddings(ctx)
        
        embedded = ctx["embedded_chunks"]
        embedded_img = [c for c in embedded if c.chunk_type == "image"]
        
        print(f"\n  📋 DAG 3 Embedding 结果:")
        for c in embedded_img:
            has_text_emb = c.embedding_vector is not None
            has_img_emb = c.extra.get("source_image_vector") is not None
            img_dim = len(c.extra["source_image_vector"]) if has_img_emb else 0
            text_dim = len(c.embedding_vector) if has_text_emb else 0
            print(f"     chunk_type={c.chunk_type}")
            print(f"     text embedding: {'✅' if has_text_emb else '❌'} ({text_dim}维)")
            print(f"     image embedding: {'✅' if has_img_emb else '❌'} ({img_dim}维)")
            print(f"     local_path: {c.extra.get('local_path', 'N/A')}")
            print(f"     local_path exists: {os.path.exists(c.extra.get('local_path', ''))}")

        # ── Step 5: 验证 to_ha3_doc() 输出 ──
        for c in embedded_img:
            ha3 = c.to_ha3_doc()
            os_doc = c.to_opensearch_doc()
            
            print(f"\n  📋 序列化对比:")
            print(f"     to_ha3_doc() fields: {sorted(ha3.keys())}")
            print(f"     to_opensearch_doc() fields: {sorted(os_doc.keys())}")
            
            ha3_has_source = "source_image" in ha3
            ha3_has_vector = "source_image_vector" in ha3 or "mm_dense_vector" in ha3
            os_has_vector = "source_image_vector" in os_doc
            
            print(f"     HA3 source_image: {'✅' if ha3_has_source else '❌ 缺失'}")
            print(f"     HA3 image vector: {'✅' if ha3_has_vector else '❌ 缺失'}")
            print(f"     OS source_image_vector: {'✅' if os_has_vector else '❌ 缺失'}")

            # 断言验证（多模态修复后的契约）
            assert ha3_has_source, "to_ha3_doc() 必须透传 source_image（serving 真图渲染依赖）"
            assert not ha3_has_vector, "One-Peace 图像向量已废弃，不应出现在 HA3 doc"

        print(f"\n  🔍 契约结论:")
        print(f"     1. 图片 chunk 创建: ✅ 正常")
        print(f"     2. 模拟 embedding 生成: ✅ 正常（统一文本向量路径）")
        print(f"     3. to_ha3_doc() 输出: ✅ 透传 source_image / visual_summary")
        print(f"     4. 独立图像向量: ❌ One-Peace 已废弃（检索靠 chunk_text 文本向量）")
