# -*- coding: utf-8 -*-
"""
test_msg_dedup_backends.py — 钉钉 msgId 去重双后端（WS0-3 + 评审 C4 两阶段）

memory 后端（默认）：首见即登记，行为不变。
redis 后端：claim 领用（短 TTL）→ 处理成功 confirm 升级 done+完整窗 / 处理失败 release
  释放领用（不永久吞消息）。redis 故障 fail-open。
"""
import fakeredis
import pytest

from opensearch_pipeline import dingtalk_bot as db
from opensearch_pipeline import redis_client


@pytest.fixture(autouse=True)
def _clean():
    db._seen_msg_ids.clear()
    yield
    db._seen_msg_ids.clear()
    redis_client.set_client_for_test(None)


@pytest.fixture
def redis_backend(monkeypatch):
    monkeypatch.setenv("RAG_MSG_DEDUP_BACKEND", "redis")
    redis_client.set_client_for_test(fakeredis.FakeStrictRedis(decode_responses=True))


# ── memory 后端（默认，零行为变化）──────────────────────────────
def test_memory_set_first(monkeypatch):
    monkeypatch.delenv("RAG_MSG_DEDUP_BACKEND", raising=False)
    assert db._is_duplicate_msg("m1") is False   # 首见
    assert db._is_duplicate_msg("m1") is True     # 二次
    db._confirm_msg("m1")                          # memory 无操作
    db._release_msg("m1")                          # memory 无操作
    assert db._is_duplicate_msg("m1") is True     # 仍去重（memory release 不改变语义）


def test_empty_msgid_never_duplicate(monkeypatch):
    monkeypatch.delenv("RAG_MSG_DEDUP_BACKEND", raising=False)
    assert db._is_duplicate_msg("") is False
    assert db._is_duplicate_msg("?") is False


# ── redis 后端（claim / confirm / release）──────────────────────
def test_redis_claim_then_duplicate(redis_backend):
    assert db._is_duplicate_msg("m1") is False   # 领用成功 = 新消息
    assert db._is_duplicate_msg("m1") is True     # 已领用 = 重复


def test_redis_confirm_keeps_dedup(redis_backend):
    assert db._is_duplicate_msg("m1") is False
    db._confirm_msg("m1")                          # 处理成功 → done + 完整去重窗
    assert db._is_duplicate_msg("m1") is True     # 钉钉 300s 内重投仍被去重


def test_redis_release_allows_reprocess(redis_backend):
    """处理失败释放领用 → 钉钉重投可重新处理（评审 C4：先占位后崩溃不永久吞消息）。"""
    assert db._is_duplicate_msg("m1") is False    # 领用
    db._release_msg("m1")                          # 处理崩溃/失败 → 释放
    assert db._is_duplicate_msg("m1") is False    # 重投重新领用成功（消息未被吞）


def test_redis_shared_across_instances(monkeypatch):
    """两个实例共享 Redis → 一个领用后，另一个视为重复（跨实例去重）。"""
    monkeypatch.setenv("RAG_MSG_DEDUP_BACKEND", "redis")
    redis_client.set_client_for_test(fakeredis.FakeStrictRedis(decode_responses=True))
    # 同一进程内两次调用即等价于两实例共享同一 Redis（get_client 单例）
    assert db._is_duplicate_msg("shared") is False
    assert db._is_duplicate_msg("shared") is True


def test_redis_fail_open_on_error(monkeypatch):
    monkeypatch.setenv("RAG_MSG_DEDUP_BACKEND", "redis")

    class _Boom:
        def set(self, *a, **k):
            raise ConnectionError("redis down")

        def delete(self, *a, **k):
            raise ConnectionError("redis down")

    redis_client.set_client_for_test(_Boom())
    # 故障 → fail-open 放行（评审 C4「丢消息 > 重复处理」）
    assert db._is_duplicate_msg("m1") is False
    assert db._is_duplicate_msg("m1") is False
    db._confirm_msg("m1")   # 不抛
    db._release_msg("m1")   # 不抛


def test_redis_empty_msgid_skips_redis(redis_backend):
    assert db._is_duplicate_msg("") is False
    db._confirm_msg("")     # 无操作，不触 redis
    db._release_msg("?")    # 无操作，不触 redis
