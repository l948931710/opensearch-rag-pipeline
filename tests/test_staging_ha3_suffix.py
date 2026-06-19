# -*- coding: utf-8 -*-
"""
test_staging_ha3_suffix.py — verify config 守卫接受 `_s` 和 `_stg` 两种 staging HA3 后缀

背景: 2026-06-15 用户最初按 docs/environment_design.md 想建 `fuling_kb_chunks_stg`,
但阿里云 OpenSearch 向量版控制台那次 `_stg` 表建失败,改用 `fuling_kb_chunks_s`
建表成功。config.py 接 `_STAGING_HA3_SUFFIXES = ("_stg", "_s")`,守卫扩展为两个都允许。

测试覆盖:
  - RAG_ENV=staging + `_s` 后缀  → 通过 (新支持)
  - RAG_ENV=staging + `_stg` 后缀 → 通过 (原有)
  - RAG_ENV=staging + 无后缀 / 其他 → 仍 raise
  - R3 (env=staging,生产 RDS+HA3 实例) 下 `_s` 也 OK
  - RDS 库名守卫仍只接受 `_stg` (没被错误放宽)
"""

from __future__ import annotations


import pytest

from opensearch_pipeline.config import (
    EnvironmentMismatchError,
    _STAGING_HA3_SUFFIXES,
    _validate_environment_target_consistency,
)


def _build_cfg(*, ha3_table: str, rds_db: str = "fuling_knowledge_stg",
               oss_bucket: str = "fuling-knowledge-base-staging",
               environment: str = "staging") -> object:
    """构造一个最小但完整的 PipelineConfig-shaped 对象用于 validation。"""
    from opensearch_pipeline.config import (
        AlibabaVectorSearchConfig, OpenSearchConfig, OSSConfig, PipelineConfig, RDSConfig,
    )
    cfg = PipelineConfig()
    cfg.environment = environment
    cfg.simulate_db = False
    cfg.simulate_opensearch = False
    cfg.simulate_oss = False
    cfg.rds = RDSConfig(
        host="rm-bp15j7wekd5738f093o.rwlb.rds.aliyuncs.com",
        port=3306, user="fuling_stg", password="x",
        database=rds_db, charset="utf8mb4",
    )
    cfg.alibaba_vector = AlibabaVectorSearchConfig(
        endpoint="ha-cn-kgl4slr1n01.public.ha.aliyuncs.com",
        instance_id="ha-cn-kgl4slr1n01",
        access_user_name="x", access_pass_word="x",
        table_name=ha3_table,
    )
    cfg.opensearch = OpenSearchConfig()
    cfg.oss = OSSConfig(
        endpoint="oss-cn-hangzhou.aliyuncs.com",
        access_key_id="x", access_key_secret="x",
        bucket_name=oss_bucket,
    )
    return cfg


# ─── _STAGING_HA3_SUFFIXES 常量本身 ─────────────────────────────────

def test_suffix_constant_includes_both():
    assert "_stg" in _STAGING_HA3_SUFFIXES
    assert "_s" in _STAGING_HA3_SUFFIXES


# ─── RAG_ENV=staging overlay 强校验 (config.py:489-501) ────────────

def test_staging_overlay_accepts_underscore_s(monkeypatch):
    """新支持: `fuling_kb_chunks_s` 在 RAG_ENV=staging 下应当通过。"""
    monkeypatch.setenv("RAG_ENV", "staging")
    cfg = _build_cfg(ha3_table="fuling_kb_chunks_s")
    _validate_environment_target_consistency(cfg)  # 不应 raise


def test_staging_overlay_accepts_underscore_stg(monkeypatch):
    """原有: `fuling_kb_chunks_stg` 仍然通过 (backwards compat)。"""
    monkeypatch.setenv("RAG_ENV", "staging")
    cfg = _build_cfg(ha3_table="fuling_kb_chunks_stg")
    _validate_environment_target_consistency(cfg)


def test_staging_overlay_rejects_production_table(monkeypatch):
    """生产表名 `fuling_kb_chunks` (无 _stg/_s) 必须 raise。
    注:实际上 R3 (env=staging,生产实例) 也会先 raise '非 _stg/_s 表'——任一守卫
    触发即可,这条 test 只确认 'production 表名一定会被拒'。"""
    monkeypatch.setenv("RAG_ENV", "staging")
    cfg = _build_cfg(ha3_table="fuling_kb_chunks")
    with pytest.raises(EnvironmentMismatchError, match=r"_stg|_s"):
        _validate_environment_target_consistency(cfg)


def test_staging_overlay_rejects_arbitrary_suffix(monkeypatch):
    """随便起的后缀 (e.g., `_test`) 也要 raise。"""
    monkeypatch.setenv("RAG_ENV", "staging")
    cfg = _build_cfg(ha3_table="fuling_kb_chunks_test")
    with pytest.raises(EnvironmentMismatchError, match=r"_stg|_s"):
        _validate_environment_target_consistency(cfg)


# ─── R3 (staging/test label + prod 实例) 校验 (config.py:463-469) ────

def test_r3_accepts_underscore_s_for_staging_on_prod_instance(monkeypatch):
    """staging label 指向生产 HA3 实例时,`_s` 后缀也应通过 (不需要 ACK)。"""
    # 移除任何 RAG_ALLOW_REMOTE_SEARCH 干扰
    monkeypatch.delenv("RAG_ALLOW_REMOTE_SEARCH", raising=False)
    monkeypatch.delenv("RAG_ENV", raising=False)
    cfg = _build_cfg(ha3_table="fuling_kb_chunks_s", environment="staging")
    _validate_environment_target_consistency(cfg)  # 不应 raise


def test_r3_rejects_production_table_for_staging_label(monkeypatch):
    """staging label 但 HA3 表是生产表 (无 _stg/_s),没 ACK 必须 raise。"""
    monkeypatch.delenv("RAG_ALLOW_REMOTE_SEARCH", raising=False)
    monkeypatch.delenv("RAG_ENV", raising=False)
    cfg = _build_cfg(ha3_table="fuling_kb_chunks", environment="staging")
    with pytest.raises(EnvironmentMismatchError, match=r"非 _stg/_s 表"):
        _validate_environment_target_consistency(cfg)


# ─── RDS 库名守卫仍只接受 _stg (确认没被误放宽) ─────────────────────

def test_rds_database_still_requires_stg_suffix(monkeypatch):
    """RDS 库名守卫不应受 HA3 表名改动影响:`_s` 不是合法 RDS 后缀。"""
    monkeypatch.setenv("RAG_ENV", "staging")
    cfg = _build_cfg(
        ha3_table="fuling_kb_chunks_s",
        rds_db="fuling_knowledge_s",  # 错的, RDS 应该是 _stg
    )
    # R3 (env=staging 生产实例) 先撞: '非 _stg 库'
    # 不用 'STAGING overlay 强约束' 的 '必须以 _stg 结尾' 那条措辞,因为 R3 先 raise
    with pytest.raises(EnvironmentMismatchError, match=r"_stg"):
        _validate_environment_target_consistency(cfg)


# ─── OSS 桶名守卫不变 ───────────────────────────────────────────────

def test_oss_bucket_still_requires_staging_suffix(monkeypatch):
    """OSS 桶名守卫不应受 HA3 表名改动影响:必须以 -staging 结尾。"""
    monkeypatch.setenv("RAG_ENV", "staging")
    cfg = _build_cfg(
        ha3_table="fuling_kb_chunks_s",
        oss_bucket="fuling-knowledge-base-prod",  # 错的, 应该是 -staging
    )
    with pytest.raises(EnvironmentMismatchError, match=r"-staging 结尾"):
        _validate_environment_target_consistency(cfg)
