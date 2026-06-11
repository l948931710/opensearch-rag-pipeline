# -*- coding: utf-8 -*-
"""
test_config_guards.py — 环境标签↔物理目标交叉校验（config._validate_environment_target_consistency）

规则表与豁免变量语义见 docs/environment_design.md §7。
复用 test_config_loading._fresh_load 的干净加载模式。
"""

import pytest

from tests.test_config_loading import _fresh_load
from opensearch_pipeline.config import EnvironmentMismatchError, is_prod_target

PROD_RDS = "rm-bp15j7wekd5738f093o.mysql.rds.aliyuncs.com"
PROD_HA3 = "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com"


class TestDevLabelGuards:
    def test_dev_label_remote_rds_raises(self):
        """R1：dev 标签 + 远程 RDS（非 simulate）→ fail-fast。"""
        with pytest.raises(EnvironmentMismatchError):
            _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                        RAG_RDS_HOST=PROD_RDS, RAG_DASHSCOPE_API_KEY="x")

    def test_dev_label_remote_rds_with_ack_passes(self):
        cfg = _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                          RAG_RDS_HOST=PROD_RDS, RAG_DASHSCOPE_API_KEY="x",
                          RAG_ALLOW_REMOTE_DB="read_only_ack")
        assert cfg.rds.host == PROD_RDS

    def test_ack_typo_raises(self):
        """R7：豁免变量值拼写错误不得静默放行。"""
        with pytest.raises(EnvironmentMismatchError):
            _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                        RAG_RDS_HOST=PROD_RDS, RAG_DASHSCOPE_API_KEY="x",
                        RAG_ALLOW_REMOTE_DB="yes")

    def test_dev_label_prod_search_raises(self):
        """R2：dev 标签 + 生产检索指纹 → fail-fast；ack 放行。"""
        with pytest.raises(EnvironmentMismatchError):
            _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                        RAG_RDS_HOST="localhost", RAG_DASHSCOPE_API_KEY="x",
                        RAG_HA3_ENDPOINT=PROD_HA3)
        cfg = _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                          RAG_RDS_HOST="localhost", RAG_DASHSCOPE_API_KEY="x",
                          RAG_HA3_ENDPOINT=PROD_HA3,
                          RAG_ALLOW_REMOTE_SEARCH="read_only_ack")
        assert PROD_HA3 in cfg.alibaba_vector.endpoint

    def test_simulate_placeholder_hosts_pass(self):
        """simulate=true 时占位 host 不触发任何规则（make sim 兼容）。"""
        cfg = _fresh_load(RAG_SIMULATE="true", RAG_RDS_HOST="some-garbage-host")
        assert cfg.simulate_db is True


class TestStagingTestLabelGuards:
    def test_test_label_prod_targets_need_double_ack(self):
        """R3（.env.prod_ro / envboot 形态）：staging/test 标签指生产 → 需双 ack。"""
        kw = dict(RAG_ENVIRONMENT="staging", RAG_SIMULATE="false",
                  RAG_RDS_HOST=PROD_RDS, RAG_HA3_ENDPOINT=PROD_HA3,
                  RAG_HA3_TABLE_NAME="fuling_kb_chunks", RAG_DASHSCOPE_API_KEY="x")
        with pytest.raises(EnvironmentMismatchError):
            _fresh_load(**kw)
        cfg = _fresh_load(**kw, RAG_ALLOW_REMOTE_DB="read_only_ack",
                          RAG_ALLOW_REMOTE_SEARCH="read_only_ack")
        assert cfg.environment == "staging"

    def test_staging_stg_suffixed_resources_pass_without_ack(self):
        """STAGING 形态：生产实例 + _stg 库/表 = 合法，不需要 ack。"""
        cfg = _fresh_load(RAG_ENVIRONMENT="staging", RAG_SIMULATE="false",
                          RAG_RDS_HOST=PROD_RDS, RAG_RDS_DATABASE="fuling_knowledge_stg",
                          RAG_HA3_ENDPOINT=PROD_HA3, RAG_HA3_TABLE_NAME="fuling_kb_chunks_stg",
                          RAG_DASHSCOPE_API_KEY="x")
        assert cfg.rds.database.endswith("_stg")


class TestProductionLabelGuards:
    def test_production_localhost_rds_raises_no_exemption(self):
        """R4：production 标签 + localhost RDS 必为配错。"""
        with pytest.raises(EnvironmentMismatchError):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="false",
                        RAG_RDS_HOST="localhost", RAG_DASHSCOPE_API_KEY="x",
                        RAG_HA3_ENDPOINT=PROD_HA3, RAG_HA3_TABLE_NAME="fuling_kb_chunks")

    def test_production_no_search_backend_raises(self):
        """R5：production 无任何检索后端。"""
        with pytest.raises(EnvironmentMismatchError):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="false",
                        RAG_RDS_HOST=PROD_RDS, RAG_DASHSCOPE_API_KEY="x")

    def test_production_normal_shape_passes(self):
        cfg = _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="false",
                          RAG_RDS_HOST=PROD_RDS, RAG_DASHSCOPE_API_KEY="x",
                          RAG_HA3_ENDPOINT=PROD_HA3, RAG_HA3_TABLE_NAME="fuling_kb_chunks")
        assert cfg.environment == "production"

    def test_production_simulate_smoke_passes(self):
        """DataWorks 冒烟形态：production 标签 + simulate=true 必须合法。"""
        cfg = _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                          RAG_DASHSCOPE_API_KEY="x")
        assert cfg.simulate is True

    def test_d7_ha3_endpoint_without_table_raises(self):
        """D7：production 启用 HA3 但表名为空（历史双标默认已移除）→ fail-fast。"""
        with pytest.raises(EnvironmentMismatchError):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="false",
                        RAG_RDS_HOST=PROD_RDS, RAG_DASHSCOPE_API_KEY="x",
                        RAG_HA3_ENDPOINT=PROD_HA3)


class TestStagingOverlayConstraints:
    def test_rag_env_staging_requires_stg_suffixes(self, monkeypatch):
        """RAG_ENV=staging 的资源后缀强约束（无豁免）。"""
        monkeypatch.setenv("RAG_ENV", "staging")
        with pytest.raises(EnvironmentMismatchError):
            _fresh_load(RAG_ENVIRONMENT="staging", RAG_SIMULATE="false",
                        RAG_RDS_HOST=PROD_RDS, RAG_RDS_DATABASE="fuling_knowledge",
                        RAG_DASHSCOPE_API_KEY="x",
                        RAG_HA3_ENDPOINT=PROD_HA3, RAG_HA3_TABLE_NAME="fuling_kb_chunks_stg")


class TestFingerprints:
    def test_oss_fingerprint_exact_match_excludes_staging_bucket(self):
        """staging 桶名以生产桶名为前缀——oss 指纹必须精确匹配。"""
        assert is_prod_target("oss", "fuling-knowledge-base")
        assert not is_prod_target("oss", "fuling-knowledge-base-staging")

    def test_search_fingerprint_substring(self):
        assert is_prod_target("search", PROD_HA3)
        assert not is_prod_target("search", "localhost")
