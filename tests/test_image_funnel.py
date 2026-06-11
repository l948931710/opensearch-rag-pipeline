# -*- coding: utf-8 -*-
"""
test_image_funnel.py — Comprehensive unit and integration tests for the Three-Stage Image Filtering Funnel,
plain raw bypass rule, parent risk propagation, and strict Gemini fallback blocking.
"""

import os
import pytest
import tempfile
import unittest.mock as mock
from PIL import Image

import opensearch_pipeline.config
from opensearch_pipeline.config import get_config
from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor
from opensearch_pipeline.pipeline_nodes import (
    node_classify_and_risk_assess,
    node_detect_sensitive,
    node_redact_or_quarantine,
    _get_db_conn
)
from opensearch_pipeline.extraction.ocr_client import OCRResult
from tests.local_stack import requires_local_db


@pytest.fixture
def temp_image():
    """创建一个临时图像的辅助 fixture。"""
    temp_files = []

    def _create(width: int, height: int, filename: str = "temp_img.png", fill_bytes: int = 0):
        # 建立临时目录
        tmpdir = tempfile.gettempdir()
        path = os.path.join(tmpdir, filename)
        
        # 使用 PIL 创建图像
        img = Image.new("RGB", (width, height), color="white")
        img.save(path)
        
        # 若需要模拟特定 byte size，可以额外追加垃圾数据
        if fill_bytes > 0:
            with open(path, "ab") as f:
                f.write(b"\0" * fill_bytes)
                
        temp_files.append(path)
        return path

    yield _create

    # 清理所有创建的临时文件
    for p in temp_files:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


class TestImageFunnelThreeStages:
    """第一至三阶段图像过滤漏斗测试。"""

    def test_funnel1_heuristics_low_resolution(self, temp_image):
        """测试漏斗 1：低分辨率 (width/height < 50px) 过滤。"""
        # 创建 40x40 像素的小图
        img_path = temp_image(40, 40, "icon_small.png")
        processor = ImageFunnelProcessor(simulate=True)
        
        result = processor.process_image(img_path, doc_id="doc1", is_public=True)
        assert result["status"] == "DISCARD_DECORATIVE"
        assert "Funnel 1" in result["reason"]

    def test_funnel1_heuristics_extreme_aspect_ratio(self, temp_image):
        """测试漏斗 1：极端长宽比 (aspect_ratio > 8.0) 过滤。"""
        # 宽 200，高 10 (aspect ratio = 20) — 模拟装饰性分割线
        img_path = temp_image(200, 10, "line_spacer.png")
        processor = ImageFunnelProcessor(simulate=True)
        
        result = processor.process_image(img_path, doc_id="doc1", is_public=True)
        assert result["status"] == "DISCARD_DECORATIVE"
        assert "Funnel 1" in result["reason"]

    def test_funnel1_heuristics_low_size(self, temp_image):
        """测试漏斗 1：极小文件大小 (< 3.0 KB) 过滤。"""
        # 创建 60x60 像素但极小的文件（默认 PNG 压缩可能小于 3KB）
        img_path = temp_image(60, 60, "mini_empty.png")
        # 确保文件大小确实小于 3KB
        size_kb = os.path.getsize(img_path) / 1024.0
        
        if size_kb < 3.0:
            processor = ImageFunnelProcessor(simulate=True)
            result = processor.process_image(img_path, doc_id="doc1", is_public=True)
            assert result["status"] == "DISCARD_DECORATIVE"
            assert "Funnel 1" in result["reason"]

    def test_funnel2_text_density_high(self, temp_image):
        """测试漏斗 2：高文本密度 (OCR 字符数 > 120) 路由到文本正文。"""
        # 创建一个 100x100 的合格图像
        img_path = temp_image(100, 100, "text_block.png", fill_bytes=4096)
        processor = ImageFunnelProcessor(simulate=True)
        
        # Mock OCR 提取长文本
        mock_ocr_result = OCRResult(
            status="SUCCESS",
            combined_text="这是一段非常长的 OCR 提取出的中文文本，用于测试漏斗 2 的高文本密度过滤规则。当文字长度超过 120 个字时，图片应当被直接路由为文本块，并被当作普通的 text 段落加入索引通道，而不要把它送去多模态向量索引队列。为了确保这个字符串的长度能够绝对稳定地超过一百二十个字符的过滤阈值，我们在这里多拼接一段充满描述细节的文字。"
        )
        
        with mock.patch.object(processor.ocr_client, "ocr_image", return_value=mock_ocr_result):
            result = processor.process_image(img_path, doc_id="doc1", is_public=True)
            
        assert result["status"] == "ROUTE_TO_TEXT"
        assert len(result["ocr_text"]) > 120
        assert "Funnel 2" in result["reason"]

    def test_funnel3_vlm_sensitive_quarantine(self, temp_image):
        """测试漏斗 3：隔离文件中的敏感印章/签名会被 Qwen-VL (模拟) 审计拦截并隔离。"""
        # is_public = False 表示原始文档处于隔离目录（_quarantine/ 路径）
        # 文件名中含有 "seal" 触发模拟敏感判定
        img_path = temp_image(100, 100, "red_seal_stamp.png", fill_bytes=5120)
        processor = ImageFunnelProcessor(simulate=True)
        
        result = processor.process_image(img_path, doc_id="doc_quar", is_public=False)
        assert result["status"] == "QUARANTINE_SENSITIVE"
        assert "Funnel 3" in result["reason"]

    def test_funnel3_vlm_sensitive_bypassed_for_public(self, temp_image):
        """测试漏斗 3：如果是普通 raw 下的内部公开文档 (is_public = True)，会跳过敏感印章拦截，路由至向量化。"""
        # is_public = True 时，虽然含有 "seal"，但会跳过安全敏感报警
        img_path = temp_image(100, 100, "red_seal_stamp.png", fill_bytes=5120)
        processor = ImageFunnelProcessor(simulate=True)
        
        result = processor.process_image(img_path, doc_id="doc_pub", is_public=True)
        assert result["status"] == "ROUTE_TO_VECTOR"
        assert "visual_summary" in result

    def test_funnel3_vlm_low_relevance(self, temp_image):
        """测试漏斗 3：低业务相关性图片（例如 banner、decoration、spacer）会被判定为 LOW_RELEVANCE 并丢弃。"""
        # 文件名包含 "logo" 或 "banner"
        img_path = temp_image(100, 100, "company_logo_banner.png", fill_bytes=5120)
        processor = ImageFunnelProcessor(simulate=True)
        
        result = processor.process_image(img_path, doc_id="doc1", is_public=True)
        assert result["status"] == "DISCARD_DECORATIVE"
        assert "Funnel 3" in result["reason"]

    def test_funnel3_vlm_clean_technical_diagram(self, temp_image):
        """测试漏斗 3：高价值商业技术图表通过 VLM 语义判定，路由至向量化队列并生成语义摘要。"""
        img_path = temp_image(100, 100, "architecture_workflow.png", fill_bytes=5120)
        processor = ImageFunnelProcessor(simulate=True)
        
        result = processor.process_image(img_path, doc_id="doc1", is_public=True)
        assert result["status"] == "ROUTE_TO_VECTOR"
        # 模拟 VLM caption（883605c 起的文案）：必须带 [Simulated] 标记且回填 doc_id
        assert result["visual_summary"].startswith("[Simulated]")
        assert "doc1" in result["visual_summary"]


class TestDegradedVlmNotCached:
    """降级的 VLM 兜底结论必须带 degraded 标记，避免被写入跨文档持久缓存。"""

    def _short_ocr(self, processor):
        return mock.patch.object(
            processor.ocr_client, "ocr_image",
            return_value=OCRResult(status="SUCCESS", combined_text="短文本"),
        )

    def test_degraded_clean_propagates_to_vector(self, temp_image):
        """VLM 降级 CLEAN（如超时兜底）→ ROUTE_TO_VECTOR 仍带 degraded=True。"""
        img_path = temp_image(100, 100, "diagram_clean.png", fill_bytes=5120)
        processor = ImageFunnelProcessor(simulate=True)
        with self._short_ocr(processor), mock.patch.object(
            processor, "_vlm_audit_and_summary",
            return_value={"status": "CLEAN", "caption": "c", "image_category": "step_screenshot",
                          "annotation_map": {}, "degraded": True},
        ):
            result = processor.process_image(img_path, doc_id="d", is_public=True)
        assert result["status"] == "ROUTE_TO_VECTOR"
        assert result.get("degraded") is True

    def test_degraded_sensitive_propagates_to_quarantine(self, temp_image):
        """VLM 降级 SENSITIVE（隔离文档兜底）→ QUARANTINE_SENSITIVE 仍带 degraded=True。"""
        img_path = temp_image(100, 100, "audit_fail.png", fill_bytes=5120)
        processor = ImageFunnelProcessor(simulate=True)
        with self._short_ocr(processor), mock.patch.object(
            processor, "_vlm_audit_and_summary",
            return_value={"status": "SENSITIVE", "caption": "", "image_category": "unknown",
                          "annotation_map": {}, "degraded": True},
        ):
            result = processor.process_image(img_path, doc_id="d", is_public=False)
        assert result["status"] == "QUARANTINE_SENSITIVE"
        assert result.get("degraded") is True

    def test_healthy_verdict_has_no_degraded_flag(self, temp_image):
        """正常（非降级）VLM 结论 degraded 为假值，可被缓存。"""
        img_path = temp_image(100, 100, "architecture_workflow.png", fill_bytes=5120)
        processor = ImageFunnelProcessor(simulate=True)
        with self._short_ocr(processor):
            result = processor.process_image(img_path, doc_id="d", is_public=True)
        assert result["status"] == "ROUTE_TO_VECTOR"
        assert not result.get("degraded")


class TestPlainRawBypassRule:
    """非隔离公开 raw 文档免 LLM 敏感审计与脱敏旁路测试。"""

    def test_plain_raw_bypasses_heavy_llm_classification(self):
        """测试非隔离目录（如 raw/dept1/test.pdf）下的文档，在 node_classify_and_risk_assess 中完全绕过大模型分类评估。"""
        doc = {
            "doc_id": "DOC_BYPASS_001",
            "version_no": 1,
            "text": "这是一份普通发货指导说明书，公开分享。",
            "source_key": "raw/admin/shipping_instruction.pdf"
        }
        ctx = {
            "canonicals": [doc],
            "simulate": True
        }

        # 临时清空 pytest 的环境变量，使其模拟生产线运行的 `is_public` 判定规则
        orig_pytest_env = os.environ.get("PYTEST_CURRENT_TEST")
        if "PYTEST_CURRENT_TEST" in os.environ:
            del os.environ["PYTEST_CURRENT_TEST"]

        try:
            # 执行分类与风险判定节点序列
            node_classify_and_risk_assess(ctx)
            node_detect_sensitive(ctx)
            node_redact_or_quarantine(ctx)
            
            # 校验直接被默认设置为 low 风险，且未引发任何 API 错误
            assert doc["llm_risk_level"] == "low"
            assert doc["permission_level"] == "public"
            assert doc["classification_status"] == "CONTENT_CLASSIFIED"
            assert "[Bypassed Audit Summary]" in doc["summary"] or doc["summary"] == doc["text"]
        finally:
            # 恢复测试状态
            if orig_pytest_env is not None:
                os.environ["PYTEST_CURRENT_TEST"] = orig_pytest_env

    def test_quarantined_raw_undergoes_llm_classification(self):
        """测试隔离目录（含有 _quarantine/ 路径）下的文档，不会被旁路，正常进行 LLM classification 调用（这里用 mock 测试）。"""
        doc = {
            "doc_id": "DOC_QUAR_001",
            "version_no": 1,
            "text": "限制级高度机密工资单。",
            "source_key": "raw/_quarantine/payroll_details.xlsx"
        }
        ctx = {
            "canonicals": [doc],
            "simulate": True,
            "mock_classification": {
                "category_l1": "record",
                "category_l2": "finance",
                "faq_eligible": False,
                "confidence": 0.98,
                "risk_level": "high",
                "summary": "机密工资单明细"
            }
        }

        node_classify_and_risk_assess(ctx)
        node_detect_sensitive(ctx)
        node_redact_or_quarantine(ctx)
        
        # 验证没有被默认低风险旁路，正确应用了 mock_classification 中的高风险
        assert doc["llm_risk_level"] == "high"
        assert doc["redaction_action"] == "QUARANTINE"


class TestParentRiskPropagation:
    """测试子资产敏感风险自动向上传递给父文档的联动机制。"""

    @requires_local_db
    def test_sensitive_asset_propagates_high_risk_to_parent(self):
        """测试如果子图片被审计为 QUARANTINE_SENSITIVE，父文档的 risk_level 自动被判定为 high 并标记 sensitive_detected=True。"""
        # 1. 插入临时父文档到 RDS
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO document_meta (doc_id, title) VALUES ('doc_parent_risk', 'Parent Doc Title')")
            cursor.execute("INSERT INTO document_version (doc_id, version_no, content_process_status) VALUES ('doc_parent_risk', 1, 'NOT_STARTED')")
        conn.commit()
        conn.close()

        try:
            doc = {
                "doc_id": "doc_parent_risk",
                "version_no": 1,
                "text": "这是一份普通文档的正文，但其关联的图片包含了敏感印章。",
                "source_key": "raw/_quarantine/parent_doc.docx",
                "assets": [
                    {
                        "filename": "image_clean.png",
                        "status": "ROUTE_TO_VECTOR"
                    },
                    {
                        "filename": "image_stamp.png",
                        "status": "QUARANTINE_SENSITIVE", # 🚨 发现敏感图片资产！
                        "reason": "VLM Audit Found Seal"
                    }
                ]
            }

            ctx = {
                "canonicals": [doc],
                "simulate": False
            }

            # 执行完整安全判定节点序列
            node_detect_sensitive(ctx)
            node_redact_or_quarantine(ctx)

            # 验证父文档的敏感判定和高风险已成功向上传递
            assert doc["sensitive_detected"] is True
            assert doc["risk_level"] == "high"
            assert doc.get("redaction_action") == "QUARANTINE"

            # 验证 RDS 中的状态已经被更新，同时发现被保存到了 document_sensitive_finding
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT finding_type, matched_text_preview FROM document_sensitive_finding WHERE doc_id='doc_parent_risk'"
                )
                finding = cursor.fetchone()
                assert finding is not None
                assert finding[0] == "IMAGE_SENSITIVE_AUDIT"
                assert "image_stamp.png" in finding[1]
            conn.close()

        finally:
            # 清理数据库
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM document_sensitive_finding WHERE doc_id='doc_parent_risk'")
                cursor.execute("DELETE FROM document_version WHERE doc_id='doc_parent_risk'")
                cursor.execute("DELETE FROM document_meta WHERE doc_id='doc_parent_risk'")
            conn.commit()
            conn.close()


class TestGeminiFallbackSafeguard:
    """生产/测试环境中杜绝 Google Gemini 模型 Fallback 逃逸的严苛校验测试。"""

    def test_staging_production_gemini_fallback_crashes_on_startup(self):
        """测试在 production 或 staging 环境下，若使用 Google Gemini 模型或 API Endpoint，启动过程直接抛出 ValueError 强行崩溃。"""
        # 保存原有的环境变量，以便后续恢复
        orig_env_vars = {}
        for key in ["RAG_ENVIRONMENT", "RAG_DASHSCOPE_API_KEY", "GEMINI_API_KEY", "LLM_API_KEY", "OCR_API_KEY", "EMBEDDING_API_KEY"]:
            orig_env_vars[key] = os.environ.get(key)

        try:
            # 1. 模拟 staging 环境下没配 DashScope KEY，直接报错
            os.environ["RAG_ENVIRONMENT"] = "staging"
            if "RAG_DASHSCOPE_API_KEY" in os.environ:
                del os.environ["RAG_DASHSCOPE_API_KEY"]
            
            # 清空缓存
            opensearch_pipeline.config._config = None
            
            with pytest.raises(ValueError, match="🚨 \\[PRODUCTION SECURITY GUARD\\] DashScope API Key is not configured"):
                get_config()

            # 2. 模拟 production 环境下虽有 DashScope KEY，但是模型配成了 Google Gemini 相关的模型
            os.environ["RAG_ENVIRONMENT"] = "production"
            os.environ["RAG_DASHSCOPE_API_KEY"] = "sk-mock-dashscope-key"
            # 故意把 LLM 配置成 gemini-3.1-flash-lite
            os.environ["RAG_LLM_MODEL"] = "gemini-3.1-flash-lite"
            
            opensearch_pipeline.config._config = None
            
            with pytest.raises(ValueError, match="🚨 \\[PRODUCTION SECURITY GUARD\\] LLM config resolved to Google Gemini"):
                get_config()

            # 3. 模拟配置了 Google APIs 接口 base_url，崩溃
            os.environ["RAG_LLM_MODEL"] = "qwen-max"
            # 把 OCR Base URL 改为 Google Api 路径
            os.environ["RAG_OCR_API_BASE_URL"] = "https://generativelanguage.googleapis.com"
            
            opensearch_pipeline.config._config = None
            
            with pytest.raises(ValueError, match="🚨 \\[PRODUCTION SECURITY GUARD\\] OCR config resolved to Google Gemini"):
                get_config()

        finally:
            # 彻底恢复原有的环境变量和配置缓存
            for key, val in orig_env_vars.items():
                if val is None:
                    if key in os.environ:
                        del os.environ[key]
                else:
                    os.environ[key] = val
            
            if "RAG_OCR_API_BASE_URL" in os.environ:
                del os.environ["RAG_OCR_API_BASE_URL"]

            opensearch_pipeline.config._config = None
            get_config()  # 重新加载正常配置
