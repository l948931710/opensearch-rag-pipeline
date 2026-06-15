# -*- coding: utf-8 -*-
"""
test_simulate_prod_guard.py — 防 sim→prod 泄露的 3 层 guard 回归测试

2026-06-13 事故：run_simulation / test fixture 的 autouse + `_get_db_conn` 路径
绕过了 simulate_db 守卫，直接对生产 RDS DELETE FROM chunk_meta。
本文件确认 3 层防护都能拒绝生产 host：
  Layer 1: _init_db_pool — simulate_db=True + 生产 host → 拒绝建池
  Layer 2: tests/test_classification + test_pipeline 的 autouse fixture
           远程 host 直接 skip（在 ensure_local_db_wired 里已有，本测试只确认调用点）
  Layer 3: run_simulation.main() — 入口 host 命中生产指纹 → 直接 raise
"""

from __future__ import annotations

import pytest

from opensearch_pipeline.config import PROD_FINGERPRINTS, get_config


# 生产 RDS 实例标识子串（与 config.PROD_FINGERPRINTS["rds"] 同源）
_PROD_RDS_FP = PROD_FINGERPRINTS["rds"][0]
_FAKE_PROD_HOST = f"{_PROD_RDS_FP}.rwlb.rds.aliyuncs.com"


# ─── Layer 1: _init_db_pool guard ────────────────────────────────────────

def test_layer1_init_db_pool_refuses_prod_under_simulate(monkeypatch):
    """simulate_db=True 时连接生产 host 必须 raise，不建池。"""
    from opensearch_pipeline import pipeline_nodes

    # 重置池单例（避免之前 test 留下的 pool）
    pipeline_nodes._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", True)
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)

    with pytest.raises(RuntimeError, match=r"DB POOL GUARD.*simulate_db=True.*生产指纹"):
        pipeline_nodes._init_db_pool()

    # 池仍是 None（没建起来）
    assert pipeline_nodes._db_pool is None


def test_layer1_local_host_under_simulate_ok(monkeypatch):
    """simulate_db=True + localhost：guard 不应触发（不实际建池，只过 guard 检查）。

    用 mock 拦截 PooledDB 构造，避免实际去连 localhost MySQL。
    """
    from opensearch_pipeline import pipeline_nodes

    pipeline_nodes._reset_db_pool()

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

    pipeline_nodes._init_db_pool()
    assert pool_called["n"] == 1, "guard 不应拦截 localhost"

    pipeline_nodes._reset_db_pool()


def test_layer1_prod_host_with_simulate_false_passes(monkeypatch):
    """simulate_db=False（合法 prod 写入流程）+ 生产 host：guard 不应触发。"""
    from opensearch_pipeline import pipeline_nodes

    pipeline_nodes._reset_db_pool()

    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg.rds, "host", _FAKE_PROD_HOST)

    pool_called = {"n": 0}

    class _FakePool:
        def __init__(self, *_a, **_kw):
            pool_called["n"] += 1

        def close(self):
            pass

    import dbutils.pooled_db
    monkeypatch.setattr(dbutils.pooled_db, "PooledDB", _FakePool)

    pipeline_nodes._init_db_pool()
    assert pool_called["n"] == 1, "simulate_db=False 是合法 prod 流程，不应拦"

    pipeline_nodes._reset_db_pool()


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
