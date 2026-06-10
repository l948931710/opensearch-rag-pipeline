# -*- coding: utf-8 -*-
"""
session_store.py — 内存会话存储（LRU 淘汰 + 超时过期）

供 api.py 和 dingtalk_bot.py 共用，避免循环导入。
生产环境可替换为 Redis 实现。
"""

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)

# 保留最近 N 轮对话 = RAG_MAX_HISTORY_TURNS（此前写死 10，环境变量是哑的）
MAX_HISTORY_TURNS = get_config().rag.max_history_turns
MAX_SESSIONS = int(os.environ.get("RAG_MAX_SESSIONS", "500"))
SESSION_TIMEOUT_SECONDS = int(os.environ.get("RAG_SESSION_TIMEOUT", "1800"))  # 30 分钟


class _SessionEntry:
    """一个会话条目：包含对话历史和最后活动时间。"""
    __slots__ = ("history", "last_active")

    def __init__(self):
        self.history: List[Dict[str, str]] = []
        self.last_active: float = time.time()

    def touch(self):
        """更新最后活动时间。"""
        self.last_active = time.time()

    def is_expired(self) -> bool:
        """检查是否已超时。"""
        return (time.time() - self.last_active) > SESSION_TIMEOUT_SECONDS


class _LRUSessionStore:
    """LRU 会话缓存，支持超时过期和容量淘汰。"""

    def __init__(self, maxsize: int = MAX_SESSIONS):
        self._store: OrderedDict[str, _SessionEntry] = OrderedDict()
        self._maxsize = maxsize
        # OrderedDict 的 check-then-act（in→del、create→popitem 淘汰）不是线程安全的。
        # api.py 的 def 处理器跑在 FastAPI 线程池、dingtalk_bot 每条消息又另起线程，
        # 多线程并发访问同一 store 必须加锁。用 RLock 以便模块级复合操作可跨多次调用持锁。
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[_SessionEntry]:
        with self._lock:
            if key in self._store:
                entry = self._store[key]
                if entry.is_expired():
                    # 超时：删除旧 session，返回 None（调用方会创建新的）
                    del self._store[key]
                    logger.info("Session 超时过期 (%.0f分钟未活动): %s", SESSION_TIMEOUT_SECONDS / 60, key)
                    return None
                self._store.move_to_end(key)
                entry.touch()
                return entry
            return None

    def create(self, key: str) -> _SessionEntry:
        """创建新会话，必要时淘汰最旧的会话。"""
        with self._lock:
            entry = _SessionEntry()
            self._store[key] = entry
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                evicted_key, _ = self._store.popitem(last=False)
                logger.debug("Session evicted (LRU): %s", evicted_key)
            return entry

    def set_history(self, key: str, history: List[Dict[str, str]]):
        with self._lock:
            if key in self._store:
                self._store[key].history = history
                self._store[key].touch()
                self._store.move_to_end(key)

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False


_sessions = _LRUSessionStore()


def get_or_create_session(session_id: Optional[str]) -> Tuple[str, List[Dict[str, str]]]:
    """获取或创建会话，返回 (session_id, history)。

    如果 session 存在但已超时（30分钟无活动），自动创建新 session。
    """
    # 跨 get→create 持锁，消除 TOCTOU（两个线程同时为同一新 session 各建一个条目）
    with _sessions._lock:
        if session_id:
            entry = _sessions.get(session_id)
            if entry is not None:
                return session_id, entry.history

        sid = session_id or str(uuid.uuid4())
        entry = _sessions.create(sid)
        return sid, entry.history


def clear_session(session_id: str) -> bool:
    """删除会话历史（线程安全、幂等）。存在并删除返回 True；不存在/已过期返回 False。

    服务端记忆真正清除的唯一入口（小程序「清除会话」此前只清本地 UI，服务端
    旧上下文继续陪聊到 30 分钟 TTL）。
    """
    if not session_id:
        return False
    return _sessions.delete(session_id)


def append_to_history(session_id: str, user_msg: str, assistant_msg: str):
    """将当前轮对话追加到会话历史。"""
    # 整个读-改-写序列持锁，避免与并发 get/create/淘汰 交错破坏历史
    with _sessions._lock:
        entry = _sessions.get(session_id)
        if entry is None:
            entry = _sessions.create(session_id)

        entry.history.append({"role": "user", "content": user_msg})
        entry.history.append({"role": "assistant", "content": assistant_msg})

        # 裁剪超出的轮数（保留最近 N 轮 = 2N 条消息）
        max_messages = MAX_HISTORY_TURNS * 2
        if len(entry.history) > max_messages:
            _sessions.set_history(session_id, entry.history[-max_messages:])
