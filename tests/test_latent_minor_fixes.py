# -*- coding: utf-8 -*-
"""
test_latent_minor_fixes.py — 2026-06 系统评审「latent/minor」批次修复的回归测试。

覆盖：
1. HA3 主键兜底必须确定性（不能用 PYTHONHASHSEED 加盐的内建 hash()）
2. 生产安全守卫必须覆盖 VLM 模型（RAG_VLM_MODEL）
"""

import hashlib
import os

import pytest


# ═══════════════════════════════════════════════════════════════
# 1. HA3 pk 兜底确定性
# ═══════════════════════════════════════════════════════════════

class TestStablePkFallback:
    def test_fallback_pk_is_md5_derived(self):
        """无 rds_id 时主键 = chunk_id 的 md5 前 8 字节（63 位），与进程无关。"""
        from opensearch_pipeline.chunker import Chunk, _stable_pk_from_chunk_id

        chunk = Chunk(
            chunk_id="doc-1_v1_0003",
            doc_id="doc-1",
            version_no=1,
            chunk_index=3,
            chunk_type="text_chunk",
            chunk_text="正文",
            token_count=2,
        )
        doc = chunk.to_ha3_doc("id")

        expected = int.from_bytes(
            hashlib.md5(b"doc-1_v1_0003").digest()[:8], "big"
        ) & 0x7FFFFFFFFFFFFFFF
        assert doc["id"] == expected
        assert doc["id"] == _stable_pk_from_chunk_id("doc-1_v1_0003")
        # 63 位非负（HA3 INT64 主键）
        assert 0 <= doc["id"] <= 0x7FFFFFFFFFFFFFFF

    def test_rds_id_wins_over_fallback(self):
        """带 rds_id 时主键必须是 chunk_meta.id（生产路径），兜底不得介入。"""
        from opensearch_pipeline.chunker import Chunk

        chunk = Chunk(
            chunk_id="doc-1_v1_0003",
            doc_id="doc-1",
            version_no=1,
            chunk_index=3,
            chunk_type="text_chunk",
            chunk_text="正文",
            token_count=2,
            rds_id=42,
        )
        assert chunk.to_ha3_doc("id")["id"] == 42


# ═══════════════════════════════════════════════════════════════
# 2. 生产守卫覆盖 VLM 模型
# ═══════════════════════════════════════════════════════════════

def _fresh_load(**env_overrides):
    """在干净环境变量中执行 load_config()（与 test_config_loading.py 同款模式）。"""
    rag_keys = [k for k in os.environ if k.startswith("RAG_")]
    saved = {k: os.environ.pop(k) for k in rag_keys}
    for k in ["DASHSCOPE_API_KEY", "GEMINI_API_KEY"]:
        if k in os.environ:
            saved[k] = os.environ.pop(k)

    import opensearch_pipeline.config as cfg_module
    cfg_module._config = None

    try:
        os.environ.update(env_overrides)
        return cfg_module.load_config()
    finally:
        for k in list(env_overrides.keys()):
            os.environ.pop(k, None)
        os.environ.update(saved)
        cfg_module._config = None


class TestProductionGuardCoversVlmModel:
    def test_gemini_vlm_model_rejected_in_production(self):
        """RAG_VLM_MODEL=gemini-* 在生产环境必须被守卫拦截（此前被跳过）。"""
        with pytest.raises(ValueError, match=r"PRODUCTION SECURITY GUARD.*VLM"):
            _fresh_load(
                RAG_ENVIRONMENT="production",
                RAG_DASHSCOPE_API_KEY="sk-test",
                RAG_VLM_MODEL="gemini-3.1-flash-lite",
            )

    def test_qwen_vlm_model_passes_in_production(self):
        """默认 qwen3-vl-plus 通过守卫（不误伤）。"""
        config = _fresh_load(
            RAG_ENVIRONMENT="production",
            RAG_DASHSCOPE_API_KEY="sk-test",
        )
        assert config.ocr.vlm_model == "qwen3-vl-plus"
