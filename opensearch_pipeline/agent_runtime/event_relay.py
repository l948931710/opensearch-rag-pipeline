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
import queue
import threading
import time
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

_END_TYPE = "__end__"
_RESET_TYPE = "__reset__"
_TERMINAL_TYPES = ("run_completed", "run_failed")


def _async_enabled() -> bool:
    """R3（P1-RT-10）：publish 异步化开关（默认开；=false 回退同步旧路径）。"""
    return os.environ.get("RAG_AGENT_EVENT_RELAY_ASYNC",
                          "true").strip().lower() in ("1", "true", "yes", "on")


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
        self._ttl_set = False
        # R3（P1-RT-10）：有界异步发布——模型每吐一段字都同步等一次 XADD 时，Redis
        # 慢 3s 会反向把 provider 流消费拖 3s。delta 帧队满即丢（计数留痕），控制帧
        # （tool/审批/终态/__end__）短等后必进队；单 writer 线程保序。
        self._async = _async_enabled()
        self._q: Optional[queue.Queue] = None
        self._writer: Optional[threading.Thread] = None
        self._dropped = 0

    def _ensure_writer(self) -> None:
        if self._q is not None:
            return
        self._q = queue.Queue(maxsize=_int_env("RAG_AGENT_EVENT_RELAY_QUEUE", 1024))
        self._writer = threading.Thread(target=self._drain, daemon=True,
                                        name=f"relay-{self._run_id[:8]}")
        self._writer.start()

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is None:                    # 关闭哨兵
                    return
                self._xadd_now(item)
            finally:
                self._q.task_done()

    def _enqueue(self, payload: Dict[str, Any]) -> None:
        self._ensure_writer()
        is_delta = payload.get("type") == "model_delta"
        try:
            if is_delta:
                self._q.put_nowait(payload)         # delta：队满即丢（可再生内容）
            else:
                self._q.put(payload, timeout=2.0)   # 控制帧：短等必达；仍满=Redis 长瘫
        except queue.Full:
            self._dropped += 1
            if self._dropped in (1, 100) or self._dropped % 1000 == 0:
                logger.warning("run %s 中继队列满，累计丢弃 %d 帧（delta 优先丢；"
                               "Redis 延迟异常）", self._run_id, self._dropped)

    def _xadd(self, payload: Dict[str, Any]) -> None:
        if self._dead:
            return
        if self._async:
            self._enqueue(payload)
            return
        self._xadd_now(payload)

    def _xadd_now(self, payload: Dict[str, Any]) -> None:
        if self._dead:
            return
        try:
            from opensearch_pipeline import redis_client
            cli = redis_client.get_client()
            cli.xadd(self._key, {"data": json.dumps(payload, ensure_ascii=False)},
                     maxlen=self._maxlen, approximate=True)
            # perf 批次 B §4.4：EXPIRE 不再逐帧发——model_delta 是帧量大头（长答案数百帧），
            # 逐帧 EXPIRE 纯属重复续期。首帧 + 一切非 delta 帧（tool/审批/终态/__end__，低频）
            # 仍续期，TTL 语义≈「自最后一个控制帧起 _ttl_s」，与旧行为在所有真实 run 形态下
            # 等效（任何 run 的 delta 之后必有控制帧收尾）。
            if not self._ttl_set or payload.get("type") != "model_delta":
                cli.expire(self._key, self._ttl_s)
                self._ttl_set = True
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
        if self._async and self._q is not None:
            try:                                     # 尽力排空（≤3s），进程退出不悬挂
                _deadline = time.monotonic() + 3.0
                while not self._q.empty() and time.monotonic() < _deadline:
                    time.sleep(0.02)
                self._q.put_nowait(None)             # 关闭 writer
            except Exception:   # noqa: BLE001
                pass


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


def _id_tuple(sid: str):
    try:
        ms, _, seq = str(sid).partition("-")
        return int(ms), int(seq or 0)
    except Exception:   # noqa: BLE001
        return (0, 0)


def resolve_start_id(run_id: str, last_event_id: Optional[str]):
    """R3（P1-RT-05）：把客户端 Last-Event-ID 解析为续读游标。
    返回 (start_id, reset)：
    - 无 last_event_id → ("0-0", False) 全量回放（旧行为）；
    - last_event_id 仍在流窗口内（≥ 首帧 id）→ (它, False) 从其后续读；
    - 已被 MAXLEN 截掉（< 首帧 id）/ 流不存在 / 探测失败 → ("0-0", True)：
      **reset 协议**——先发 __reset__ 帧再全量重放，客户端收到 reset 即清屏按 id 去重。"""
    if not last_event_id:
        return "0-0", False
    try:
        from opensearch_pipeline import redis_client
        cli = redis_client.get_client()
        first = cli.xrange(_stream_key(run_id), min="-", max="+", count=1)
        if not first:
            return "0-0", True
        first_id = first[0][0]
        if _id_tuple(last_event_id) < _id_tuple(first_id):
            return "0-0", True                    # 游标已被 trim——重放需声明 reset
        return str(last_event_id), False
    except Exception:   # noqa: BLE001
        return "0-0", True


def stream_run_events(run_id: str, *, block_ms: int = 15000,
                      overall_timeout_s: Optional[int] = None,
                      is_terminal_fn=None,
                      start_id: str = "0-0",
                      announce_reset: bool = False) -> Iterator[Dict[str, Any]]:
    """回放 + XREAD 阻塞尾随，直到 __end__ / 终态帧 / 总超时。
    R3（P1-RT-05）：start_id 支持断线续读（配 resolve_start_id 消费 Last-Event-ID），
    每帧携 `_relay_id`（Redis stream id）供 SSE 层写 `id:` 行；announce_reset=True
    时先发 __reset__ 帧（游标被 trim 的全量重放，客户端据此清屏防「既重复又缺字」）。
    Redis 客户端 decode_responses=True → fields 均为 str。

    is_terminal_fn（批次4，ultra event_relay:132）：可选的「durable run 已终态」探针
    （routes 层注入 run_store 读，本模块不 import serving）。中继存在恰为其兜底的两类
    崩溃场景里**终态帧永远不会出现在流上**：(a) 执行方被 SIGKILL → reaper 只做 DB 侧
    running→failed，不碰 Redis；(b) 中途 publish 失败 _dead=True 后一切后续帧（含终态帧
    与 __end__）静默丢弃。此前消费者回放完残帧后 XREAD 空等到 1800s。现在：每次 XREAD
    空转（安静 block_ms 无新帧）时查一次终态——已终态则再做一次 500ms 短读 drain 赛跑帧
    后收流；活跃流帧不触发探针（零 DB 额外读）。不传 = 旧行为。"""
    from opensearch_pipeline import redis_client
    cli = redis_client.get_client()
    key = _stream_key(run_id)
    last_id = start_id or "0-0"
    if announce_reset:
        yield {"type": _RESET_TYPE}
    timeout_s = overall_timeout_s if overall_timeout_s is not None else \
        _int_env("RAG_AGENT_EVENT_RELAY_READ_TIMEOUT_S", 1800)
    deadline = time.monotonic() + max(1, timeout_s)
    draining = False    # 终态已观测：本轮短读后无新帧即收流
    while time.monotonic() < deadline:
        resp = cli.xread({key: last_id}, count=256,
                         block=500 if draining else block_ms)
        if not resp:
            if draining:
                return
            if is_terminal_fn is not None:
                try:
                    _term = bool(is_terminal_fn())
                except Exception:   # noqa: BLE001 — 探针失败按未终态处理（保守继续尾随）
                    _term = False
                if _term:
                    draining = True   # 短宽限 drain（终态帧可能正在赛跑写入）
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
                d["_relay_id"] = eid                # R3：SSE 层写 id: 行的游标
                yield d
                if t in _TERMINAL_TYPES:
                    return          # 终态帧即收流（__end__ 只是兜底哨兵）
