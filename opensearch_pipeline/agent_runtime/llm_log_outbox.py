# -*- coding: utf-8 -*-
"""
llm_log_outbox.py — llm_call_log 有界 outbox（perf 批次 C §4.5）

默认 **off**（`RAG_AGENT_LLM_LOG_ASYNC`，默认 false）：`wrap_call_logger` 原样返回
同步 `record_llm_call`，行为逐字节不变。开 → 每次模型调用只入有界内存队列（热路径
零 DB 往返），单 worker 后台攒批 `record_llm_calls`（executemany 单事务）落库。

边界（评审 §10 红线逐条对齐）：
· **HIGH_WRITE 审计不经此路**：agent_audit_log 的 write-ahead 在 tool_executor /
  adjudicator，同步保证原样；本模块只承接 llm_call_log（成本对账账本）。
· **有界不静默丢**（no-silent-caps）：队列满（RAG_AGENT_LLM_LOG_QUEUE，默认 1000）
  → 该条**回退同步直写**并记 WARNING——宁可热路径偶发慢一拍，不丢账。
· 冲刷失败整批记 WARNING 后丢弃（与 gateway._record_call 对单条同步失败的 fail-open
  同语义；账本缺行可由 usage 终帧/qa 对账兜底发现）。
· `drain_llm_log()`：测试断言/优雅退出前冲干净（镜像 tool_executor.drain_read_trace）。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_FLUSH_BATCH = 50          # 单次冲刷攒批上限（executemany 行数）
_FLUSH_IDLE_S = 0.5        # 队列空时的等待步长（也是最大冲刷延迟）

_q: Optional["queue.Queue[Dict[str, Any]]"] = None
_worker: Optional[threading.Thread] = None
_store = None
_lock = threading.Lock()
_idle = threading.Event()  # 队列已清空信号（drain 用）
_idle.set()


def async_enabled() -> bool:
    return os.environ.get("RAG_AGENT_LLM_LOG_ASYNC", "").strip().lower() \
        in ("1", "true", "yes", "on")


def _queue_cap() -> int:
    try:
        return max(1, int(os.environ.get("RAG_AGENT_LLM_LOG_QUEUE", "") or 1000))
    except ValueError:
        return 1000


def _run_worker() -> None:
    assert _q is not None
    while True:
        try:
            first = _q.get(timeout=_FLUSH_IDLE_S)
        except queue.Empty:
            _idle.set()
            continue
        _idle.clear()
        batch = [first]
        while len(batch) < _FLUSH_BATCH:
            try:
                batch.append(_q.get_nowait())
            except queue.Empty:
                break
        try:
            _store.record_llm_calls(batch)
        except Exception:   # noqa: BLE001 — 冲刷失败整批 fail-open（见模块头）
            logger.warning("llm_call_log outbox 冲刷失败（丢弃 %d 行）", len(batch),
                           exc_info=True)
        finally:
            for _ in batch:
                _q.task_done()
        if _q.empty():
            _idle.set()


def _ensure_worker(store) -> None:
    global _q, _worker, _store
    with _lock:
        _store = store
        if _worker is not None and _worker.is_alive():
            return
        if _q is None:
            _q = queue.Queue(maxsize=_queue_cap())
        _worker = threading.Thread(target=_run_worker, name="llm-log-outbox", daemon=True)
        _worker.start()


def wrap_call_logger(store) -> Callable:
    """gateway `call_logger` 的接线点：flag off → 原同步 `record_llm_call`（零变化）；
    on → 入队回调（满则同步直写兜底）。"""
    if not async_enabled():
        return store.record_llm_call
    if not hasattr(store, "record_llm_calls"):   # 桩/旧实现无批量法 → 保持同步
        return store.record_llm_call
    _ensure_worker(store)

    def _enqueue(**kw) -> None:
        try:
            _q.put_nowait(dict(kw))
            _idle.clear()
        except queue.Full:
            logger.warning("llm_call_log outbox 队列满（%d）——本条回退同步直写", _queue_cap())
            store.record_llm_call(**kw)

    return _enqueue


def drain_llm_log(timeout: float = 5.0) -> bool:
    """等待已入队账本行全部落库（测试断言/优雅退出前用）。返回是否清空。
    R3（P2-RT-17）：改按 Queue 的 task_done 账本（unfinished_tasks）轮询——旧 _idle
    事件在「producer put 后迟到 clear」与「worker 写入中」两窗口有竞态（假阳=还有
    在途行就放行退出，假阴=已清空却报 False）。账本口径：put +1 / task_done -1，
    归零=队列空且在途批已写完，无竞态窗。"""
    if _q is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if getattr(_q, "unfinished_tasks", 0) == 0:
            return True
        time.sleep(0.02)
    return getattr(_q, "unfinished_tasks", 0) == 0
