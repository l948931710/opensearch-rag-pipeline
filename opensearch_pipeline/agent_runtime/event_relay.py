# -*- coding: utf-8 -*-
"""
event_relay.py — 跨实例事件中继（Redis Stream；R5，2026-07-11 重审计 §1）

executor 的事件流此前只存在于进程内 queue.Queue：SSE 消费者断连重连落在另一副本、
或 /approve 续跑无消费者时，事件对外彻底不可达（executor.py 模块头自认「跨实例中继
(Redis Stream) 待补」）。本模块把每个 run 的**对外**事件镜像 XADD 进
`fl:agent:run:{run_id}:ev`（MAXLEN 近似截断 + TTL），任何副本可经
GET /api/agent/runs/{id}/events 从头回放到终态。

默认 **off**：RAG_AGENT_EVENT_RELAY=redis 且 RAG_REDIS_URL 已配置才生效——与
session/限流的 redis 后端同一「回滚开关」语义：关掉即回进程内历史行为，零回归面。
全程 fail-open：中继任何故障只降级为单副本语义，绝不影响进程内 SSE 主路径（单 run
首错后本 run 静默停发，避免每帧刷日志）。

载荷边界：publish 走 events.dump_event——ToolResultEmitted.artifacts（exclude=True）
不进线协议；RunSuspended 由 driver 剥离 state_messages 后才 _emit；RunCheckpointReady
是内部事件、driver 持久化后即消费，永不到达本模块。终态后由 RunHandle._finish 追加
`__end__` 哨兵帧，消费侧据此收流。

局限（v1，有意为之）：sources / content_blocks 帧依赖 artifacts 进程内旁路，不进中继
——跨实例回放只有文本/工具状态/审批/完成帧；答案依据走 durable（qa_session_log.
retrieved_docs + 运行中心 invocations）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

_END_TYPE = "__end__"
_TERMINAL_TYPES = ("run_completed", "run_failed")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def relay_enabled() -> bool:
    """flag（默认 off）+ Redis 配置双条件。"""
    if os.environ.get("RAG_AGENT_EVENT_RELAY", "").strip().lower() != "redis":
        return False
    try:
        from opensearch_pipeline import redis_client
        return redis_client.is_configured()
    except Exception:   # noqa: BLE001
        return False


def _stream_key(run_id: str) -> str:
    from opensearch_pipeline import redis_client
    return redis_client.key("agent", "run", run_id, "ev")


class _RedisRelay:
    """单 run 的发布器（挂在 RunHandle._relay 上）。首错后本 run 静默停发（fail-open）。"""

    def __init__(self, run_id: str):
        self._run_id = run_id
        self._key = _stream_key(run_id)
        self._maxlen = _int_env("RAG_AGENT_EVENT_RELAY_MAXLEN", 4096)
        self._ttl_s = _int_env("RAG_AGENT_EVENT_RELAY_TTL_S", 7200)
        self._dead = False

    def _xadd(self, payload: Dict[str, Any]) -> None:
        if self._dead:
            return
        try:
            from opensearch_pipeline import redis_client
            cli = redis_client.get_client()
            cli.xadd(self._key, {"data": json.dumps(payload, ensure_ascii=False)},
                     maxlen=self._maxlen, approximate=True)
            cli.expire(self._key, self._ttl_s)
        except Exception:   # noqa: BLE001
            self._dead = True
            logger.warning("run %s 事件中继发布失败（本 run 降级单副本，进程内 SSE 不受影响）",
                           self._run_id, exc_info=True)

    def publish(self, ev: Any) -> None:
        try:
            from opensearch_pipeline.agent_runtime.events import dump_event
            self._xadd(dump_event(ev))
        except Exception:   # noqa: BLE001 — dump 失败同样只降级
            self._dead = True
            logger.warning("run %s 事件序列化失败（中继停发）", self._run_id, exc_info=True)

    def end(self) -> None:
        self._xadd({"type": _END_TYPE})


def attach_relay(handle) -> None:
    """给 RunHandle 装发布器（flag off / Redis 未配置 → no-op，保持 _relay=None）。"""
    if not relay_enabled():
        return
    handle._relay = _RedisRelay(handle.run_id)


def has_stream(run_id: str) -> bool:
    """回放前探测：终态 run 的流可能已过 TTL——缺流时端点直接给终结提示，不空等。"""
    try:
        from opensearch_pipeline import redis_client
        return bool(redis_client.get_client().exists(_stream_key(run_id)))
    except Exception:   # noqa: BLE001
        return False


def stream_run_events(run_id: str, *, block_ms: int = 15000,
                      overall_timeout_s: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """从头（0-0）回放 + XREAD 阻塞尾随，直到 __end__ / 终态帧 / 总超时。
    Redis 客户端 decode_responses=True → fields 均为 str。"""
    from opensearch_pipeline import redis_client
    cli = redis_client.get_client()
    key = _stream_key(run_id)
    last_id = "0-0"
    timeout_s = overall_timeout_s if overall_timeout_s is not None else \
        _int_env("RAG_AGENT_EVENT_RELAY_READ_TIMEOUT_S", 1800)
    deadline = time.monotonic() + max(1, timeout_s)
    while time.monotonic() < deadline:
        resp = cli.xread({key: last_id}, count=256, block=block_ms)
        if not resp:
            continue
        for _k, entries in resp:
            for eid, fields in entries:
                last_id = eid
                try:
                    d = json.loads(fields.get("data") or "{}")
                except Exception:   # noqa: BLE001 — 脏帧跳过
                    continue
                t = d.get("type")
                if t == _END_TYPE:
                    return
                yield d
                if t in _TERMINAL_TYPES:
                    return          # 终态帧即收流（__end__ 只是兜底哨兵）
