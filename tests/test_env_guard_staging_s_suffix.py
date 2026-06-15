# -*- coding: utf-8 -*-
"""
test_env_guard_staging_s_suffix.py — env_guard 接受 staging HA3 `_s` 后缀

配套 config.py 的 _STAGING_HA3_SUFFIXES 扩展(commit a951bb5)。env_guard.py 有
自己 hardcode 的 `_stg` 检查,如果不一起改,stage 3 push HA3 到 fuling_kb_chunks_s
会被 assert_destructive_write_allowed 误拦(_s 不是 _stg)。

覆盖:
  - assert_destructive_write_allowed(staging, search, _s 表) → 放行
  - assert_destructive_write_allowed(staging, search, _stg 表) → 放行(原有)
  - assert_destructive_write_allowed(staging, search, 生产表名) → 拦截
  - RDS _stg / OSS -staging 不受影响
"""
from __future__ import annotations

from unittest import mock

import pytest

from opensearch_pipeline import env_guard
from opensearch_pipeline.env_guard import DestructiveOpBlocked, assert_destructive_write_allowed


def _fake_cfg(*, environment="staging", readonly=False,
              rds_db="fuling_knowledge_stg", ha3_table="fuling_kb_chunks_s",
              oss_bucket="fuling-knowledge-base-staging"):
    cfg = mock.MagicMock()
    cfg.environment = environment
    cfg.readonly = readonly
    cfg.rds.database = rds_db
    cfg.alibaba_vector.table_name = ha3_table
    cfg.oss.bucket_name = oss_bucket
    return cfg


def test_staging_search_s_suffix_allowed(monkeypatch):
    """staging + HA3 表 _s 后缀 → 写放行(不 raise)。"""
    monkeypatch.setattr(env_guard, "get_config", lambda: _fake_cfg(ha3_table="fuling_kb_chunks_s"))
    assert_destructive_write_allowed(
        "push_documents", "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com", kind="search")


def test_staging_search_stg_suffix_allowed(monkeypatch):
    """staging + HA3 表 _stg 后缀 → 写放行(原有兼容)。"""
    monkeypatch.setattr(env_guard, "get_config", lambda: _fake_cfg(ha3_table="fuling_kb_chunks_stg"))
    assert_destructive_write_allowed(
        "push_documents", "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com", kind="search")


def test_staging_search_production_table_blocked(monkeypatch):
    """staging label 但 HA3 表是生产表名(无 _stg/_s) → 拦截(需 ACK)。"""
    monkeypatch.setattr(env_guard, "get_config", lambda: _fake_cfg(ha3_table="fuling_kb_chunks"))
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed(
            "push_documents", "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com", kind="search")


def test_staging_rds_stg_still_allowed(monkeypatch):
    """staging + RDS _stg → 放行(没被改动影响)。"""
    monkeypatch.setattr(env_guard, "get_config", lambda: _fake_cfg())
    assert_destructive_write_allowed(
        "write_chunk_meta", "rm-bp15j7wekd5738f093o.rwlb.rds.aliyuncs.com", kind="rds")


def test_staging_oss_staging_still_allowed(monkeypatch):
    """staging + OSS -staging → 放行(没被改动影响)。"""
    monkeypatch.setattr(env_guard, "get_config", lambda: _fake_cfg())
    assert_destructive_write_allowed(
        "put_canonical", "fuling-knowledge-base-staging", kind="oss")


def test_readonly_still_blocks_everything(monkeypatch):
    """RAG_READONLY=true 优先级最高,即便 staging _s 表也拦。"""
    monkeypatch.setattr(env_guard, "get_config",
                        lambda: _fake_cfg(readonly=True, ha3_table="fuling_kb_chunks_s"))
    with pytest.raises(DestructiveOpBlocked, match=r"READONLY"):
        assert_destructive_write_allowed(
            "push_documents", "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com", kind="search")
