# -*- coding: utf-8 -*-
"""
test_destructive_guard.py — 运行时破坏性操作守卫（env_guard.py）

覆盖：production 放行 / 非指纹目标放行 / 当日 ack 放行 / 过期 ack 拒绝 /
RAG_READONLY 一律拒绝 / STAGING _stg 资源放行 / GuardedBucket 写拦读传。
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import opensearch_pipeline.env_guard as eg
from opensearch_pipeline.env_guard import (DestructiveOpBlocked, GuardedBucket,
                                           assert_destructive_write_allowed)

PROD_HA3 = "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com"


def _cfg(environment="development", readonly=False, rds_db="fuling_knowledge",
         ha3_table="", oss_bucket="fuling-knowledge-base"):
    return SimpleNamespace(
        environment=environment, readonly=readonly,
        rds=SimpleNamespace(host="x", database=rds_db),
        alibaba_vector=SimpleNamespace(table_name=ha3_table),
        oss=SimpleNamespace(bucket_name=oss_bucket),
    )


@pytest.fixture
def patch_cfg(monkeypatch):
    def _apply(cfg):
        monkeypatch.setattr(eg, "get_config", lambda: cfg)
    return _apply


def test_production_always_allowed(patch_cfg):
    patch_cfg(_cfg(environment="production"))
    assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_non_prod_target_allowed(patch_cfg):
    patch_cfg(_cfg())
    assert_destructive_write_allowed("search_delete", "localhost:9200", kind="search")


def test_dev_to_prod_target_blocked(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_same_day_ack_allows(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK",
                       f"search_delete:{date.today().isoformat()}")
    assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_wildcard_same_day_ack_allows(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"*:{date.today().isoformat()}")
    assert_destructive_write_allowed("push_index", PROD_HA3, kind="search")


def test_stale_ack_rejected(patch_cfg, monkeypatch):
    """陈年 export 残留不得长期放行——ack 按日过期。"""
    patch_cfg(_cfg())
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"search_delete:{yesterday}")
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_wrong_op_ack_rejected(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK",
                       f"some_other_op:{date.today().isoformat()}")
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_readonly_blocks_everything(patch_cfg, monkeypatch):
    """RAG_READONLY=true（PROD-RO）：连非生产目标也拒绝，且 ack 无效。"""
    patch_cfg(_cfg(readonly=True))
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"*:{date.today().isoformat()}")
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("write_chunk_meta", "localhost", kind="rds")


def test_staging_stg_resources_allowed(patch_cfg):
    """STAGING 共享生产实例但写 _stg 资源 = 合法（后缀已被加载期强校验）。"""
    patch_cfg(_cfg(environment="staging", rds_db="fuling_knowledge_stg",
                   ha3_table="fuling_kb_chunks_stg"))
    assert_destructive_write_allowed("write_chunk_meta",
                                     "rm-bp15j7wekd5738f093o.mysql.rds.aliyuncs.com", kind="rds")
    assert_destructive_write_allowed("push_index", PROD_HA3, kind="search")


def test_staging_non_stg_table_still_blocked(patch_cfg, monkeypatch):
    """staging 标签但表名不带 _stg（=直指生产活表）仍要拦。"""
    patch_cfg(_cfg(environment="staging", ha3_table="fuling_kb_chunks"))
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("push_index", PROD_HA3, kind="search")


class _FakeBucket:
    def __init__(self):
        self.calls = []

    def put_object(self, key, data):
        self.calls.append(("put", key))
        return "put-ok"

    def get_object(self, key):
        self.calls.append(("get", key))
        return "get-ok"

    def sign_url(self, method, key, expires):
        return f"https://signed/{key}"


def test_guarded_bucket_blocks_prod_writes(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    gb = GuardedBucket(_FakeBucket(), "fuling-knowledge-base")
    with pytest.raises(DestructiveOpBlocked):
        gb.put_object("rag-ready/x/content.md", b"data")


def test_guarded_bucket_reads_and_signing_pass_through(patch_cfg):
    patch_cfg(_cfg())
    gb = GuardedBucket(_FakeBucket(), "fuling-knowledge-base")
    assert gb.get_object("raw/a.pdf") == "get-ok"
    assert gb.sign_url("GET", "processing/assets/i.png", 600).startswith("https://signed/")


def test_guarded_bucket_non_prod_bucket_writes_allowed(patch_cfg):
    """staging/其他桶不命中精确指纹——写放行。"""
    patch_cfg(_cfg(environment="staging", oss_bucket="fuling-knowledge-base-staging"))
    gb = GuardedBucket(_FakeBucket(), "fuling-knowledge-base-staging")
    assert gb.put_object("rag-ready/x/content.md", b"data") == "put-ok"
