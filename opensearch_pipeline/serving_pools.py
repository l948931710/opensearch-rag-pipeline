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


# tests 置 False：告警同步直调便于断言；生产恒 True（热路径零阻塞）。同 rate_limiter:72。
_DISPATCH_ASYNC = True


def _dispatch_saturation_alert(kind: str, inflight: int, queued: int, cap: int) -> None:
    """池饱和 → OBS-4 运维告警。**这条不是锦上添花，是防静默事故的必需品。**

    为什么：饱和降级会让延迟指标**变好看**——离线扫描实测 60 并发 / cap=24 时
    89% 的臂被拒、多数查询拿到零臂回落服务端混合，p50 从 0.642s "降到" 0.058s。
    **快是因为没干活。** 只盯 p50/p95 会把召回塌方读成性能改善，
    所以拒绝必须自己喊出来，而不是等人去翻 `pool_stats()`。

    ⚠️ 为什么不接 `ops_monitor`（那个 launchd 作业）：它的每个检查都是**对 RDS 发 SQL**
    （见 `queue_monitor.py:run_queue_aging_check`），而本计数在 **serving 进程内存**里，
    跨进程读不到。serving 进程内直接告警是本仓既有范式——`rate_limiter.py:75-95`
    的全局熔断触顶就是这么做的。

    fail-open：告警发不出去绝不影响限流主路径。`alerting.py` 自带 60s per-dedup_key 限流。
    """
    def _send() -> None:
        try:
            from opensearch_pipeline.alerting import send_ops_alert
            send_ops_alert(
                f"检索子线程池饱和：{kind}",
                f"`{kind}` 池准入闸拒绝了新任务（inflight={inflight} queued={queued} cap={cap}）。\n\n"
                "**后果是静默的**：被拒的臂不参与融合 ⇒ 该查询回落少臂/服务端混合，"
                "延迟指标反而变好，召回却降级。\n\n"
                f"- 若是正常增长 → 调高 `RAG_POOL_{kind.upper()}_WORKERS` / `_CAP`；\n"
                "- 若是突发风暴 → 查上游（HA3/DashScope）是否变慢导致在飞堆积；\n"
                "- 临时止血 → `RAG_POOL_ADMISSION=false` 关闸（回到不限流、只排队）。",
                severity="critical", dedup_key=f"pool-saturated:{kind}")
        except Exception:   # noqa: BLE001
            logger.warning("池饱和告警发送失败（fail-open）", exc_info=True)
    if _DISPATCH_ASYNC:
        threading.Thread(target=_send, daemon=True, name=f"pool-alert-{kind}").start()
    else:
        _send()


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

    # ⚠️ 锁内只取快照，告警在**锁外**发 —— 发告警要建线程/走网络，在 _stats_lock 里做
    # 会让所有并发提交跟着阻塞（这把锁在热路径上）。
    snapshot = None
    with _stats_lock:
        st = _stats[kind]
        if _admission_on():
            cap = _admission_cap(kind)
            if st["inflight"] + st["queued"] >= cap:
                st["rejected"] += 1
                snapshot = (st["inflight"], st["queued"], cap)
        if snapshot is None:
            st["queued"] += 1
            st["submitted"] += 1
    if snapshot is not None:
        _dispatch_saturation_alert(kind, *snapshot)
        raise PoolSaturated(
            f"{kind} 池已满（inflight={snapshot[0]} queued={snapshot[1]} cap={snapshot[2]}）")
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
