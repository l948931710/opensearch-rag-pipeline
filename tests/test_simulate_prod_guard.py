# -*- coding: utf-8 -*-
"""
test_simulate_prod_guard.py — 防 sim→prod 泄露的 3 层 guard 回归测试

2026-06-13 事故：run_simulation / test fixture 的 autouse + `_get_db_conn` 路径
绕过了 simulate_db 守卫，直接对生产 RDS DELETE FROM chunk_meta。
本文件确认 3 层防护都能拒绝生产 host：
  Layer 1: db._init_db_pool — simulate_db=True + 生产 host → 拒绝建池
  Layer 2: tests/test_classification + test_pipeline 的 autouse fixture
           远程 host 直接 skip（在 ensure_local_db_wired 里已有，本测试只确认调用点）
  Layer 3: run_simulation.main() — 入口 host 命中生产指纹 → 直接 raise
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from opensearch_pipeline.config import PROD_FINGERPRINTS, get_config


# 生产 RDS 实例标识子串（与 config.PROD_FINGERPRINTS["rds"] 同源）
_PROD_RDS_FP = PROD_FINGERPRINTS["rds"][0]
_FAKE_PROD_HOST = f"{_PROD_RDS_FP}.rwlb.rds.aliyuncs.com"


# ─── Layer 1: _init_db_pool guard ────────────────────────────────────────

def test_layer1_init_db_pool_refuses_prod_under_simulate(monkeypatch):
    """simulate_db=True 时连接生产 host 必须 raise，不建池。"""
    from opensearch_pipeline import db

    # 重置池单例（避免之前 test 留下的 pool）
    db._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", True)
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)

    with pytest.raises(RuntimeError, match=r"DB POOL GUARD.*simulate_db=True.*生产指纹"):
        db._init_db_pool()

    # 池仍是 None（没建起来）
    assert db._db_pool is None


def test_layer1_local_host_under_simulate_ok(monkeypatch):
    """simulate_db=True + localhost：guard 不应触发（不实际建池，只过 guard 检查）。

    用 mock 拦截 PooledDB 构造，避免实际去连 localhost MySQL。
    """
    from opensearch_pipeline import db

    db._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", True)
    monkeypatch.setattr(cfg.rds, "host", "localhost")

    pool_called = {"n": 0}

    class _FakePool:
        def __init__(self, *_a, **_kw):
            pool_called["n"] += 1

        def close(self):
            pass

    import dbutils.pooled_db
    monkeypatch.setattr(dbutils.pooled_db, "PooledDB", _FakePool)

    db._init_db_pool()
    assert pool_called["n"] == 1, "guard 不应拦截 localhost"

    db._reset_db_pool()


def _install_fake_pool(monkeypatch):
    """把 PooledDB 换成记录构造 kwargs 的 no-op，返回记录列表（用于断言 init_command）。"""
    calls = []

    class _FakePool:
        def __init__(self, *_a, **kw):
            calls.append(kw)

        def close(self):
            pass

    import dbutils.pooled_db
    monkeypatch.setattr(dbutils.pooled_db, "PooledDB", _FakePool)
    return calls


def test_layer1_prod_host_production_env_is_writable(monkeypatch):
    """environment=production + 生产 host：合法写入流程——建池且**可写**（无 SESSION READ ONLY）。"""
    from opensearch_pipeline import db

    db._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "environment", "production")
    monkeypatch.setattr(cfg, "readonly", False)
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)

    calls = _install_fake_pool(monkeypatch)
    db._init_db_pool()
    assert len(calls) == 1, "simulate_db=False + production 是合法 prod 流程，应建池"
    assert "init_command" not in calls[0], "生产写流程不应被设成只读"

    db._reset_db_pool()


def test_layer1_nonprod_label_prod_host_is_readonly_pool(monkeypatch):
    """P2: 非生产标签(development)指向生产 RDS 且无 ack——建池但 SESSION READ ONLY（读放行、写在 MySQL 拦）。"""
    from opensearch_pipeline import db

    db._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "environment", "development")
    monkeypatch.setattr(cfg, "readonly", False)
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)

    calls = _install_fake_pool(monkeypatch)
    db._init_db_pool()
    assert len(calls) == 1
    assert calls[0].get("init_command") == "SET SESSION TRANSACTION READ ONLY"

    db._reset_db_pool()


def test_layer1_nonprod_label_prod_host_with_ack_is_writable(monkeypatch):
    """P2 cohesion: 非生产标签指向生产 RDS **但带当日 destructive ack**——可写池（与守卫层一致，
    保留 RAG_DESTRUCTIVE_PROD_ACK 逃生口）。"""
    from datetime import date

    from opensearch_pipeline import db

    db._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "environment", "development")
    monkeypatch.setattr(cfg, "readonly", False)
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"*:{date.today().isoformat()}")

    calls = _install_fake_pool(monkeypatch)
    db._init_db_pool()
    assert len(calls) == 1
    assert "init_command" not in calls[0], "带当日 ack 的非生产→生产应可写，与守卫层一致"

    db._reset_db_pool()


def test_layer1_readonly_flag_overrides_ack(monkeypatch):
    """PROD-RO 硬边界：RAG_READONLY=true 即便带当日 ack 仍只读（ack 不豁免 readonly）。"""
    from datetime import date

    from opensearch_pipeline import db

    db._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "environment", "development")
    monkeypatch.setattr(cfg, "readonly", True)
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"*:{date.today().isoformat()}")

    calls = _install_fake_pool(monkeypatch)
    db._init_db_pool()
    assert len(calls) == 1
    assert calls[0].get("init_command") == "SET SESSION TRANSACTION READ ONLY"

    db._reset_db_pool()


def test_layer1_readonly_flag_forces_readonly_pool_even_local(monkeypatch):
    """P2: RAG_READONLY=true——即便本地 host 也建只读池（声明式只读的物理 MySQL 边界）。"""
    from opensearch_pipeline import db

    db._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "environment", "development")
    monkeypatch.setattr(cfg, "readonly", True)
    monkeypatch.setattr(cfg.rds, "host", "localhost")

    calls = _install_fake_pool(monkeypatch)
    db._init_db_pool()
    assert len(calls) == 1
    assert calls[0].get("init_command") == "SET SESSION TRANSACTION READ ONLY"

    db._reset_db_pool()


def test_layer1_staging_stg_db_is_writable(monkeypatch):
    """staging 标签写 _stg 库（共享生产实例但合法写）：建池可写，不设只读。"""
    from opensearch_pipeline import db

    db._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "environment", "staging")
    monkeypatch.setattr(cfg, "readonly", False)
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)
    monkeypatch.setattr(cfg.rds, "database", "fuling_knowledge_stg")

    calls = _install_fake_pool(monkeypatch)
    db._init_db_pool()
    assert len(calls) == 1
    assert "init_command" not in calls[0], "staging 写 _stg 是合法写，不应被设成只读"

    db._reset_db_pool()


# ─── Layer 2: test fixture skip 验证 ─────────────────────────────────────

def test_layer2_ensure_local_db_wired_refuses_prod(monkeypatch):
    """`ensure_local_db_wired()` 必须对生产 host 返回 False（fixture 由此 skip）。"""
    from tests import local_stack

    # 重置 cache
    local_stack._db_available = None
    local_stack._db_reason = ""

    cfg = get_config()
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)

    assert local_stack.ensure_local_db_wired() is False
    assert "not local" in local_stack.local_db_unavailable_reason().lower() \
        or "not local" in local_stack._db_reason.lower()

    # 还原 cache 给后续测试
    local_stack._db_available = None
    local_stack._db_reason = ""


# ─── Layer 3: run_simulation.main 入口断言 ───────────────────────────────

def test_layer3_run_simulation_main_refuses_prod_rds(monkeypatch):
    """run_simulation.main() 在 RDS host 命中生产指纹时必须直接 raise，不进 argparse。"""
    from opensearch_pipeline import run_simulation

    cfg = get_config()
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)
    # 清空 endpoint 避免 layer3 第二条提前触发
    monkeypatch.setattr(cfg.alibaba_vector, "endpoint", "")
    monkeypatch.setattr(cfg.opensearch, "host", "")

    # sys.argv 不重要——guard 在 argparse 前
    monkeypatch.setattr("sys.argv", ["run_simulation"])
    with pytest.raises(RuntimeError, match=r"RUN_SIMULATION GUARD.*RDS host.*生产指纹"):
        run_simulation.main()


def test_layer3_run_simulation_main_refuses_prod_search(monkeypatch):
    """run_simulation.main() 在检索 endpoint 命中生产指纹时也要 raise。"""
    from opensearch_pipeline import run_simulation

    cfg = get_config()
    monkeypatch.setattr(cfg.rds, "host", "localhost")  # rds 走开
    monkeypatch.setattr(cfg.alibaba_vector, "endpoint",
                        f"{PROD_FINGERPRINTS['search'][0]}.public.ha.aliyuncs.com")

    monkeypatch.setattr("sys.argv", ["run_simulation"])
    with pytest.raises(RuntimeError, match=r"RUN_SIMULATION GUARD.*检索 endpoint.*生产指纹"):
        run_simulation.main()


# ─── Cross-layer cohesion: pool read-only verdict ↔ guard block verdict ───────

def test_pool_verdict_agrees_with_guard_verdict(monkeypatch):
    """跨层一致性：_pool_readonly_declared 的"只读"判定必须与守卫层"拦写"判定一一对应——
    任一 session 三层同进同出，绝不出现"守卫放行但池只读把写憋死在 MySQL ERROR 1792"的矛盾。
    （此用例若在 _pool_readonly_declared honor-ack 修复前跑，dev→prod+ack 一格会 fail。）"""
    import opensearch_pipeline.env_guard as eg
    from opensearch_pipeline.env_guard import (DestructiveOpBlocked,
                                               assert_destructive_write_allowed)
    from opensearch_pipeline.db import _pool_readonly_declared

    today = date.today().isoformat()
    PROD = _FAKE_PROD_HOST

    # (readonly, env, db, host, ack) -> 期望"写被拦 / 池只读"
    cases = [
        (True,  "development", "fuling_knowledge",     PROD,        None,         True),
        (True,  "development", "fuling_knowledge",     PROD,        f"*:{today}", True),
        (False, "production",  "fuling_knowledge",     PROD,        None,         False),
        (False, "staging",     "fuling_knowledge_stg", PROD,        None,         False),
        (False, "development", "fuling_knowledge",     "localhost", None,         False),
        (False, "development", "fuling_knowledge",     PROD,        None,         True),
        (False, "development", "fuling_knowledge",     PROD,        f"*:{today}", False),
    ]

    for readonly, env, db, host, ack, expect_blocked in cases:
        cfg = SimpleNamespace(
            environment=env, readonly=readonly,
            rds=SimpleNamespace(host=host, database=db),
            alibaba_vector=SimpleNamespace(table_name=""),
            oss=SimpleNamespace(bucket_name="fuling-knowledge-base"),
        )
        monkeypatch.setattr(eg, "get_config", lambda c=cfg: c)
        if ack is None:
            monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
        else:
            monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", ack)

        pool_ro = _pool_readonly_declared(cfg)
        try:
            assert_destructive_write_allowed("rds_write", host, kind="rds", any_ack=True)
            guard_blocked = False
        except DestructiveOpBlocked:
            guard_blocked = True

        assert pool_ro == guard_blocked == expect_blocked, (
            f"cell readonly={readonly} env={env} db={db} host={host} ack={ack}: "
            f"pool_ro={pool_ro} guard_blocked={guard_blocked} expected={expect_blocked}")
