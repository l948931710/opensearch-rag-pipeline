# -*- coding: utf-8 -*-
"""
test_config_loading.py — config.py 加载逻辑的单元测试

验证：
1. 环境变量优先级与覆盖关系
2. P0-1 修复：单例写回后 get_config() 返回新值
3. simulate 子开关的级联默认行为
4. Production Security Guard
5. DashScope/Gemini 动态路由
"""

import os
import pytest


def _fresh_load(**env_overrides):
    """在干净的环境变量中执行 load_config()，返回配置对象。"""
    # 清除所有 RAG_ 前缀环境变量
    rag_keys = [k for k in os.environ if k.startswith("RAG_")]
    saved = {k: os.environ.pop(k) for k in rag_keys}
    # 也清除非 RAG_ 前缀的 DASHSCOPE/GEMINI key
    for k in ["DASHSCOPE_API_KEY", "GEMINI_API_KEY"]:
        if k in os.environ:
            saved[k] = os.environ.pop(k)

    # 重置单例
    import opensearch_pipeline.config as cfg_module
    cfg_module._config = None

    try:
        os.environ.update(env_overrides)
        return cfg_module.load_config()
    finally:
        # 恢复环境变量
        for k in list(env_overrides.keys()):
            os.environ.pop(k, None)
        os.environ.update(saved)
        cfg_module._config = None


class TestSimulateCascade:
    """验证 RAG_SIMULATE 与子开关的级联默认行为。"""

    def test_simulate_true_cascades_to_all_sub_flags(self):
        """RAG_SIMULATE=true → 所有子开关默认 True。"""
        config = _fresh_load(RAG_SIMULATE="true")
        assert config.simulate is True
        assert config.simulate_db is True
        assert config.simulate_opensearch is True
        assert config.simulate_oss is True
        assert config.simulate_api is True

    def test_simulate_false_cascades_to_all_sub_flags(self):
        """RAG_SIMULATE=false → 所有子开关默认 False。"""
        config = _fresh_load(RAG_SIMULATE="false")
        assert config.simulate is False
        assert config.simulate_db is False
        assert config.simulate_opensearch is False
        assert config.simulate_oss is False
        assert config.simulate_api is False

    def test_sub_flag_overrides_parent(self):
        """子开关可以独立覆盖 RAG_SIMULATE 的默认值。"""
        config = _fresh_load(
            RAG_SIMULATE="false",
            RAG_SIMULATE_API="true",     # API 保持模拟
            RAG_SIMULATE_OSS="true",     # OSS 保持模拟
        )
        assert config.simulate is False
        assert config.simulate_db is False       # 跟随 SIMULATE=false
        assert config.simulate_api is True       # 独立覆盖
        assert config.simulate_oss is True       # 独立覆盖
        assert config.simulate_opensearch is False  # 跟随 SIMULATE=false

    def test_no_env_defaults_to_simulate_true(self):
        """无任何环境变量时，默认全模拟。"""
        config = _fresh_load()
        assert config.simulate is True


class TestDynamicAPIRouting:
    """验证 LLM/Embedding/OCR 的 DashScope vs Gemini 动态路由。"""

    def test_dashscope_key_routes_to_qwen(self):
        """有 DashScope key → LLM/OCR 使用 qwen 模型。"""
        config = _fresh_load(RAG_DASHSCOPE_API_KEY="sk-test-dashscope")
        assert "qwen" in config.llm.model.lower() or "plus" in config.llm.model.lower()
        assert "dashscope" in config.llm.api_base_url
        assert "dashscope" in config.ocr.api_base_url

    def test_gemini_key_only_routes_to_gemini(self):
        """仅有 Gemini key → LLM/OCR 使用 Gemini 模型。"""
        config = _fresh_load(RAG_GEMINI_API_KEY="AIza-test-gemini")
        assert "gemini" in config.llm.model.lower()
        assert "googleapis" in config.llm.api_base_url

    def test_embedding_model_routes_dashscope_for_text_embedding(self):
        """text-embedding-v4 模型 → 使用 DashScope Embedding 端点。"""
        config = _fresh_load(
            RAG_DASHSCOPE_API_KEY="sk-test",
            RAG_EMBEDDING_MODEL="text-embedding-v4",
        )
        assert "dashscope" in config.embedding.api_base_url

    def test_embedding_model_routes_gemini_for_gemini_model(self):
        """gemini-embedding-2 模型 → 使用 Gemini Embedding 端点。"""
        config = _fresh_load(
            RAG_GEMINI_API_KEY="AIza-test",
            RAG_EMBEDDING_MODEL="gemini-embedding-2",
        )
        assert "googleapis" in config.embedding.api_base_url


class TestProductionSecurityGuard:
    """验证 production/staging 环境的 Gemini 禁止机制。"""

    def test_production_without_dashscope_key_raises(self):
        """production 环境必须配置 DashScope key。"""
        with pytest.raises(ValueError, match="PRODUCTION SECURITY GUARD"):
            _fresh_load(
                RAG_ENVIRONMENT="production",
                RAG_GEMINI_API_KEY="AIza-test",
            )

    def test_production_with_dashscope_key_passes(self):
        """production 环境有 DashScope key → 正常通过。"""
        config = _fresh_load(
            RAG_ENVIRONMENT="production",
            RAG_DASHSCOPE_API_KEY="sk-test-prod",
        )
        assert config.environment == "production"

    def test_staging_gemini_model_override_raises(self):
        """staging 环境下手动将模型设为 Gemini → 应被 guard 拦截。"""
        with pytest.raises(ValueError, match="PRODUCTION SECURITY GUARD"):
            _fresh_load(
                RAG_ENVIRONMENT="staging",
                RAG_DASHSCOPE_API_KEY="sk-test",
                RAG_LLM_MODEL="gemini-3.1-flash-lite",
            )

    def test_development_allows_gemini(self):
        """development 环境允许使用 Gemini（不触发 guard）。"""
        config = _fresh_load(
            RAG_ENVIRONMENT="development",
            RAG_GEMINI_API_KEY="AIza-test-dev",
        )
        assert "gemini" in config.llm.model.lower()


class TestSingletonWriteback:
    """验证 P0-1 修复：load_config() 后写回 _config 单例。"""

    def test_get_config_returns_updated_after_writeback(self):
        """模拟 P0-1 修复：手动写回 _config 后 get_config() 返回新值。"""
        import opensearch_pipeline.config as cfg_module

        # 保存原始状态
        orig_config = cfg_module._config

        try:
            # 模拟 main() 中的修复逻辑
            new_config = _fresh_load(RAG_SIMULATE="false", RAG_ENVIRONMENT="development")
            cfg_module._config = new_config

            # 后续 get_config() 应该返回新配置
            got = cfg_module.get_config()
            assert got is new_config
            assert got.simulate is False
        finally:
            cfg_module._config = orig_config

    def test_get_config_without_writeback_returns_stale(self):
        """不写回 _config 时，get_config() 返回旧的缓存值。"""
        import opensearch_pipeline.config as cfg_module

        orig_config = cfg_module._config

        try:
            # 设置一个旧的缓存
            old_config = _fresh_load(RAG_SIMULATE="true")
            cfg_module._config = old_config

            # 直接调用 load_config()，但不赋值给 _config
            # (模拟忘记写回单例的场景)
            rag_keys = [k for k in os.environ if k.startswith("RAG_")]
            saved = {k: os.environ.pop(k) for k in rag_keys}
            for k in ["DASHSCOPE_API_KEY", "GEMINI_API_KEY"]:
                if k in os.environ:
                    saved[k] = os.environ.pop(k)
            try:
                os.environ["RAG_SIMULATE"] = "false"
                _new = cfg_module.load_config()
                # 不写回: cfg_module._config = _new
            finally:
                os.environ.pop("RAG_SIMULATE", None)
                os.environ.update(saved)

            # get_config() 仍然返回旧缓存
            got = cfg_module.get_config()
            assert got is old_config
            assert got.simulate is True
        finally:
            cfg_module._config = orig_config


class TestEdgeCases:
    """config 加载的边界情况。"""

    def test_env_bool_accepts_various_truthy_values(self):
        """_env_bool 接受 true/1/yes 作为 True。"""
        for val in ["true", "1", "yes", "True", "YES"]:
            config = _fresh_load(RAG_SIMULATE=val)
            assert config.simulate is True

    def test_env_bool_accepts_various_falsy_values(self):
        """_env_bool 接受 false/0/no 作为 False。"""
        for val in ["false", "0", "no", "False", "NO"]:
            config = _fresh_load(RAG_SIMULATE=val)
            assert config.simulate is False

    def test_empty_rds_password_does_not_crash(self):
        """RDS 密码为空时不应崩溃。"""
        config = _fresh_load(RAG_RDS_PASSWORD="")
        assert config.rds.password == ""

    def test_custom_embedding_dimension(self):
        """自定义 embedding 维度被正确解析。"""
        config = _fresh_load(RAG_EMBEDDING_DIMENSION="768")
        assert config.embedding.dimension == 768
