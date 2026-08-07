# -*- coding: utf-8 -*-
"""serving_pools.py — serving 侧子线程池的**进程级**归口（D3 第一步：旋钮 + 指标 + 上限）。

## 为什么有这个文件

`retriever.py` 此前在四处**按请求新建** `ThreadPoolExecutor`，其中融合三臂那处
（`retriever.py:_client_fusion_search`，`config.py` 的 `client_fusion_enable` 默认 True）
**每次查询必起**。`api.py` 的 AnyIO 令牌默认 120 ⇒ 理论上 120×3=360 个短生命周期子线程，
**没有任何进程级总量闸**，也**没有任何指标**能说出实际在飞多少。

本模块把这四处归口到进程级池，并给出可观测与可选的上限。

## 🔴 默认值刻意维持现状（Sam 2026-08-06 拍板：先落旋钮+指标+上限，默认不改行为）

- `max_workers` 默认按 **AnyIO 令牌数 × 每查询臂数** 算出，即"当前请求上限下不可能排队"的值
  ⇒ HTTP 路径的并发形态**与改动前等价**，只是线程被**复用**而不是每请求新建；
- **准入上限（真正的限流）默认关**（`RAG_POOL_ADMISSION=false`）——开它之前需要
  staging 的配额/P95/降级率数据，拍脑袋定值等于把限流做成事故源。

⚠️ 一处**如实说明的行为差异**：`ThreadPoolExecutor` 的 worker 一旦创建就不再回收。
所以一次流量尖峰后，常驻线程数会停在峰值，而不是像以前那样随请求结束消散。
换来的是不再有每请求的线程创建/销毁开销。**这条要在压测里量，不要假装没有。**

## 🔴 三个池，不是一个——依赖必须无环

| 池 | 使用者 | 它的任务会不会再等池 |
|---|---|---|
| `fusion`（叶） | 融合 D/S/B 三臂 | 不会（臂内直接 `client.search()`） |
| `prefetch` | 主路预取、cosurface 预取 | 会（主路预取 → `search_chunks` → `fusion`） |
| `fanout` | multi-query 各子查询 | 会（→ `fusion`；且 `_one(0)` 会等 `prefetch` 的 primary future） |

合并成一个池就死锁：fanout 任务占满 worker 后在等 prefetch/fusion 的任务，而后者拿不到 worker。
拆成三个后依赖是单向的 `fanout → {prefetch, fusion}`、`prefetch → fusion`，**无环**
（codex 2026-08-06 抓出「`_one(0)` 等同属一池的 primary future」这条自等待，故 prefetch 必须独立）。
"""
from __future__ import annotations

import contextvars
import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

FUSION = "fusion"
PREFETCH = "prefetch"
FANOUT = "fanout"
_KINDS = (FUSION, PREFETCH, FANOUT)

_pools: Dict[str, ThreadPoolExecutor] = {}
_lock = threading.Lock()

# 指标：只用整型计数 + 一把锁，够用且不引依赖。inflight 用 +1/-1 而不是 len(池)——
# 后者拿不到"排队中"与"在飞"的区分。
_stats_lock = threading.Lock()
_stats: Dict[str, Dict[str, int]] = {
    k: {"submitted": 0, "inflight": 0, "queued": 0, "rejected": 0, "completed": 0}
    for k in _KINDS
}


def _anyio_tokens() -> int:
    """AnyIO 请求线程上限（api.py:139 同源）。默认值按它推，保证 HTTP 路径不排队。"""
    try:
        return max(1, int(os.environ.get("RAG_THREADPOOL_TOKENS", "120")))
    except ValueError:
        return 120


def _default_workers(kind: str) -> int:
    # fusion：每查询最多 3 臂 ⇒ 令牌数 × 3。
    # prefetch：每查询最多 1 主路 + 1 cosurface ⇒ ×2。
    # fanout：每查询最多 4 路（`min(4, len(queries))`）⇒ ×4，但默认 multi_query_mode=off，
    #         池是惰性建的，off 时这个数根本不产生线程。
    return _anyio_tokens() * {FUSION: 3, PREFETCH: 2, FANOUT: 4}[kind]


def _workers(kind: str) -> int:
    env = os.environ.get(f"RAG_POOL_{kind.upper()}_WORKERS", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            logger.warning("RAG_POOL_%s_WORKERS=%r 非整数，回落默认", kind.upper(), env)
    return _default_workers(kind)


def _admission_on() -> bool:
    """真正的限流闸。**默认关** —— 开之前要有 staging 数据（见模块 docstring）。"""
    return os.environ.get("RAG_POOL_ADMISSION", "false").strip().lower() in ("1", "true", "yes")


def _admission_cap(kind: str) -> int:
    env = os.environ.get(f"RAG_POOL_{kind.upper()}_CAP", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return _workers(kind) * 2      # 在飞 + 排队 的总上限


def get_pool(kind: str) -> ThreadPoolExecutor:
    """取进程级池（惰性建，双检锁——同 `http_session.py:23-31` 的既有范式）。"""
    if kind not in _KINDS:
        raise ValueError(f"未知池类型 {kind!r}，只允许 {_KINDS}")
    pool = _pools.get(kind)
    if pool is not None:
        return pool
    with _lock:
        pool = _pools.get(kind)
        if pool is None:
            n = _workers(kind)
            pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"rag-{kind}")
            _pools[kind] = pool
            logger.info("serving pool 建立: kind=%s max_workers=%d admission=%s",
                        kind, n, _admission_on())
    return pool


def _wrap(kind: str, fn: Callable, ctx) -> Callable:
    """任务包装：ContextVar 传播 + 计数 + **TLS 清理**。

    ⚠️ TLS 清理不是形式主义（codex 2026-08-06 点名）：worker 线程在长活池里**跨请求复用**，
    若某个任务在自己线程上开了 `retriever._conn_scope`（今天没有，但没东西拦着后来人），
    残留的 `conn` 会被下一个任务当成"共享连接"复用——那是一条早已 close 或永不归还的连接。
    这里**关掉**它而不只是置空引用。
    """
    def _runner():
        with _stats_lock:
            _stats[kind]["queued"] -= 1
            _stats[kind]["inflight"] += 1
        try:
            return ctx.run(fn)
        finally:
            _clear_conn_scope()
            with _stats_lock:
                _stats[kind]["inflight"] -= 1
                _stats[kind]["completed"] += 1
    return _runner


def _clear_conn_scope() -> None:
    try:
        from opensearch_pipeline import retriever as _r
        scope = getattr(_r, "_conn_scope", None)
        if scope is None:
            return
        conn = getattr(scope, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:   # noqa: BLE001 — 清理不得反过来打断任务
                pass
        scope.conn = None
        scope.active = False
    except Exception:   # noqa: BLE001
        pass


class PoolSaturated(RuntimeError):
    """准入闸拒绝。**调用方必须显式处理**——绝不让它冒泡成整条查询失败。"""


def submit(kind: str, fn: Callable, *args, **kwargs) -> Future:
    """提交任务；准入闸开且已满时抛 `PoolSaturated`（默认关 ⇒ 永不抛）。

    ⚠️ permit 生命周期：`queued` 在**提交成功**后才 +1；submit 本身失败要立刻回退计数，
    否则"有界"会随失败次数**永久缩小**（codex 2026-08-06 BLOCKER）。
    """
    if kind not in _KINDS:
        raise ValueError(f"未知池类型 {kind!r}")
    bound = None
    if args or kwargs:
        def bound():        # noqa: E306 — 绑参，避免 ctx.run 传参
            return fn(*args, **kwargs)
    target = bound or fn

    with _stats_lock:
        if _admission_on():
            st = _stats[kind]
            if st["inflight"] + st["queued"] >= _admission_cap(kind):
                st["rejected"] += 1
                raise PoolSaturated(f"{kind} 池已满（inflight={st['inflight']} queued={st['queued']}）")
        _stats[kind]["queued"] += 1
        _stats[kind]["submitted"] += 1
    try:
        return get_pool(kind).submit(_wrap(kind, target, contextvars.copy_context()))
    except BaseException:
        with _stats_lock:            # 提交失败 ⇒ 立刻退还，绝不泄漏
            _stats[kind]["queued"] -= 1
        raise


def submit_or_none(kind: str, fn: Callable, *args, **kwargs) -> Optional[Future]:
    """提交，饱和则回 `None` —— 让调用方走**自己既有的**同步/降级路径。

    比"抛异常指望落进某个 except"可靠：`_client_fusion_search` 的 fail-open 只包
    `fut.result()`、**不包 submit**（codex BLOCKER 2），靠异常会直接崩掉整条查询。
    """
    try:
        return submit(kind, fn, *args, **kwargs)
    except PoolSaturated as e:
        logger.warning("serving pool 饱和，调用方降级: %s", e)
        return None


def pool_stats() -> Dict[str, Dict[str, int]]:
    """快照（供监控与测试）。`rejected` 必须接告警，否则饱和降级就是静默的。"""
    with _stats_lock:
        out = {k: dict(v) for k, v in _stats.items()}
    for k in _KINDS:
        out[k]["max_workers"] = _workers(k)
        out[k]["admission"] = 1 if _admission_on() else 0
        out[k]["alive"] = 1 if k in _pools else 0
    return out


def shutdown_pools(wait: bool = False) -> None:
    """进程退出时统一关闭（由 `api.py` 的 lifespan 退出分支调用）。

    ⚠️ **只允许在这里关**。请求路径上对共享池调用 `shutdown` 会把整个进程的池毒化——
    这正是 `retriever.py` 原先两处 `shutdown(wait=False)` 在改用共享池后必须删掉的原因。
    """
    with _lock:
        pools, _pools_snapshot = list(_pools.items()), None
        _pools.clear()
    for kind, pool in pools:
        try:
            pool.shutdown(wait=wait)
            logger.info("serving pool 关闭: %s", kind)
        except Exception as e:   # noqa: BLE001
            logger.warning("serving pool 关闭失败 %s: %s", kind, e)


def _reset_for_tests() -> None:
    """测试专用：关池 + 清计数，让每个用例从干净状态开始。"""
    shutdown_pools(wait=True)
    with _stats_lock:
        for k in _KINDS:
            _stats[k] = {"submitted": 0, "inflight": 0, "queued": 0,
                         "rejected": 0, "completed": 0}
