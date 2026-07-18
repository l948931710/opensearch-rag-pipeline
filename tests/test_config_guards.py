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
        """STAGING 形态：生产实例 + _stg 库/表（含运营库）= 合法，不需要 ack。"""
        cfg = _fresh_load(RAG_ENVIRONMENT="staging", RAG_SIMULATE="false",
                          RAG_RDS_HOST=PROD_RDS, RAG_RDS_DATABASE="fuling_knowledge_stg",
                          RAG_RDS_OPERATION_DATABASE="fuling_operation_stg",
                          RAG_HA3_ENDPOINT=PROD_HA3, RAG_HA3_TABLE_NAME="fuling_kb_chunks_stg",
                          RAG_DASHSCOPE_API_KEY="x")
        assert cfg.rds.database.endswith("_stg")

    def test_staging_prod_operation_db_needs_ack(self):
        """#F-staging-opdb：staging + _stg 主库/表，但运营库仍是生产 fuling_operation → 需 ack。"""
        kw = dict(RAG_ENVIRONMENT="staging", RAG_SIMULATE="false",
                  RAG_RDS_HOST=PROD_RDS, RAG_RDS_DATABASE="fuling_knowledge_stg",
                  RAG_RDS_OPERATION_DATABASE="fuling_operation",
                  RAG_HA3_ENDPOINT=PROD_HA3, RAG_HA3_TABLE_NAME="fuling_kb_chunks_stg",
                  RAG_DASHSCOPE_API_KEY="x")
        with pytest.raises(EnvironmentMismatchError, match=r"RDS_OPERATION_DATABASE"):
            _fresh_load(**kw)
        cfg = _fresh_load(**kw, RAG_ALLOW_REMOTE_DB="read_only_ack")
        assert cfg.rds.operation_database == "fuling_operation"


class TestVendorGuardFingerprintTrigger:
    """【P2-28/P2-6】供应商守卫（禁 Gemini + 必须 DashScope）触发条件 = 标签 OR 生产物理指纹。

    组合缺口：dev 标签 + read_only_ack 实连生产 RDS/HA3 + 只配 GEMINI key →
    模型解析全路由 Google，生产 chunk_text/查询内容被 POST 到 Google。现已闭合。
    """

    def test_dev_label_prod_rds_gemini_only_raises(self):
        """dev 标签 + ack 连生产 RDS + 仅 Gemini key → 供应商守卫按指纹触发，raise。"""
        with pytest.raises(ValueError, match="PRODUCTION SECURITY GUARD"):
            _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                        RAG_RDS_HOST=PROD_RDS, RAG_ALLOW_REMOTE_DB="read_only_ack",
                        RAG_GEMINI_API_KEY="AIza-test")

    def test_dev_label_prod_ha3_dashscope_passes(self):
        """日常合法用法不误伤：dev 标签 + ack 连生产 HA3 + DashScope key（解析到 Qwen）→ 通过。"""
        cfg = _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                          RAG_RDS_HOST="localhost", RAG_HA3_ENDPOINT=PROD_HA3,
                          RAG_ALLOW_REMOTE_SEARCH="read_only_ack",
                          RAG_DASHSCOPE_API_KEY="sk-test")
        assert "qwen" in cfg.llm.model.lower() or "plus" in cfg.llm.model.lower()

    def test_simulate_mode_never_triggers_vendor_guard(self):
        """模拟模式不碰真实目标：即使 env 里残留生产指纹 + 仅 Gemini key，守卫完全不触发。"""
        cfg = _fresh_load(RAG_SIMULATE="true", RAG_RDS_HOST=PROD_RDS,
                          RAG_HA3_ENDPOINT=PROD_HA3, RAG_GEMINI_API_KEY="AIza-test")
        assert cfg.simulate is True


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


class TestRrfThresholdWarning:
    """#9：rrf 融合 + 未开 rerank 时，7.7/5.8 阈值失配，load_config 必须 loud-warn。"""

    def test_rrf_without_rerank_warns(self, capsys):
        cfg = _fresh_load(RAG_HA3_HYBRID_FUSION="rrf", RAG_RERANK_ENABLE="false")
        out = capsys.readouterr().out
        assert cfg.alibaba_vector.hybrid_fusion == "rrf"
        assert "HA3_HYBRID_FUSION=rrf" in out and "误标" in out

    def test_weighted_no_warn(self, capsys):
        _fresh_load(RAG_HA3_HYBRID_FUSION="weighted")
        assert "HA3_HYBRID_FUSION=rrf" not in capsys.readouterr().out

    def test_rrf_with_rerank_no_warn(self, capsys):
        """rrf + rerank 开：档位改用 0.9/0.8 rerank 尺度，不属本告警场景。"""
        _fresh_load(RAG_HA3_HYBRID_FUSION="rrf", RAG_RERANK_ENABLE="true")
        assert "HA3_HYBRID_FUSION=rrf" not in capsys.readouterr().out


class TestEmbeddingRegimeGuard:
    """P3-8：非 DashScope 嵌入兜底与 dense+sparse 检索制度不兼容——真嵌入 + 检索后端时 fail-fast。"""

    def test_gemini_fallback_with_search_backend_raises(self):
        with pytest.raises(EnvironmentMismatchError, match="EMBEDDING REGIME"):
            _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                        RAG_RDS_HOST="localhost", RAG_GEMINI_API_KEY="g",
                        RAG_OPENSEARCH_HOST="localhost")

    def test_gemini_fallback_without_search_backend_passes(self):
        """无检索后端（纯抽取/离线脚本形态）不硬拦。"""
        cfg = _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                          RAG_RDS_HOST="localhost", RAG_GEMINI_API_KEY="g")
        assert "gemini" in cfg.embedding.model

    def test_dashscope_embedding_passes(self):
        cfg = _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                          RAG_RDS_HOST="localhost", RAG_DASHSCOPE_API_KEY="x",
                          RAG_OPENSEARCH_HOST="localhost")
        assert cfg.embedding.model == "text-embedding-v4"

    def test_simulate_mode_skips_guard(self):
        cfg = _fresh_load(RAG_SIMULATE="true", RAG_GEMINI_API_KEY="g",
                          RAG_OPENSEARCH_HOST="localhost")
        assert cfg.simulate is True

    def test_explicit_ack_allows_experiment(self):
        cfg = _fresh_load(RAG_ENVIRONMENT="development", RAG_SIMULATE="false",
                          RAG_RDS_HOST="localhost", RAG_GEMINI_API_KEY="g",
                          RAG_OPENSEARCH_HOST="localhost",
                          RAG_ALLOW_INCOMPATIBLE_EMBEDDING="ack")
        assert "gemini" in cfg.embedding.model

# ── 批次7（ultra config:632）：环境标签闭集 + 单点归一 ─────────────────────────


def test_environment_label_normalized_once():
    """'Production'/带空白 → 规范值——此前交叉校验自行 lower 而生产安全守卫用原值精确匹配，
    'Production' 能过 prod 交叉校验却静默跳过全部生产姿态守卫。"""
    from opensearch_pipeline.config import _normalize_environment
    assert _normalize_environment("Production") == "production"
    assert _normalize_environment("  STAGING ") == "staging"
    assert _normalize_environment("development") == "development"
    assert _normalize_environment("") == ""          # 空=development 语义（validator dev 分支）


def test_unknown_environment_label_fails_fast():
    """未知标签（'dev'/'prod'/拼写错误）此前不匹配任何交叉校验分支=静默跳过全部
    标签↔目标校验（'dev' 标签机器可静默读生产 RDS）→ 现在 fail-fast 拒绝启动。"""
    import pytest

    from opensearch_pipeline.config import EnvironmentMismatchError, _normalize_environment
    for bad in ("dev", "prod", "produciton", "stg"):
        with pytest.raises(EnvironmentMismatchError, match="未知环境标签"):
            _normalize_environment(bad)


def test_load_config_rejects_unknown_label(monkeypatch):
    """load_config 级：归一发生在任何守卫/连接之前，simulate 也拦（配置错误必须立刻可见）。"""
    import pytest

    from opensearch_pipeline.config import EnvironmentMismatchError, load_config
    monkeypatch.setenv("RAG_SIMULATE", "true")
    monkeypatch.setenv("RAG_ENVIRONMENT", "prod")
    with pytest.raises(EnvironmentMismatchError, match="未知环境标签"):
        load_config()
