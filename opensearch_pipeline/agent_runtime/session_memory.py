# -*- coding: utf-8 -*-
"""
session_memory.py — SessionMemory（agent 多轮会话记忆门面；v2 报告 §6 L2 Session Memory）

plan WS1-1：SessionMemory Protocol + 复用 WS0-2 会话存储（session_store 双后端）。agent 入口
用它取「最近 N 轮 + rolling summary」喂进 loop、答完 append，实现多轮上下文连续。

分工：**存储/裁剪/归属/rolling summary 全在 session_store**（memory/redis 双后端已验），本模块
只是 agent-facing 门面——把 thread_id 作会话键，get_snapshot 取历史快照、append 落一轮。
键结构与钉钉一致：thread_id = f"{conversation_id}:{user_id}"（无 conversation_id 时用 session_id）。

⚠️ 归属：owner=已验证 user_id，透传 session_store 的 owner 校验（防伪造 thread 键窃取他人上下文）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

Msg = Dict[str, Any]


@dataclass
class SessionSnapshot:
    """一次会话快照：thread_id（回填新建的 sid）+ 最近 N 轮消息（含 rolling summary 前置 system 消息，若开）。"""

    thread_id: str
    messages: List[Msg] = field(default_factory=list)


class SessionMemory(Protocol):
    """agent 多轮记忆契约。SessionStoreMemory 是默认实现（复用 session_store）。"""

    def get_snapshot(self, thread_id: Optional[str], *, owner: Optional[str] = None) -> SessionSnapshot:
        ...

    def append(self, thread_id: str, user_msg: str, assistant_msg: str, *,
               owner: Optional[str] = None) -> None:
        ...

    def clear(self, thread_id: str, *, owner: Optional[str] = None) -> bool:
        ...


class SessionStoreMemory:
    """复用 session_store（memory/redis 双后端 + owner 归属 + 2N 裁剪 + rolling summary 全继承）。

    thread_id 作会话键；None → session_store 新建 UUID（get_snapshot 的 thread_id 回填该 sid，
    供客户端下一轮回传）。取历史与落一轮均透传 owner（归属校验）。
    """

    def get_snapshot(self, thread_id: Optional[str], *, owner: Optional[str] = None) -> SessionSnapshot:
        from opensearch_pipeline.session_store import get_or_create_session
        sid, history = get_or_create_session(thread_id, owner=owner)
        return SessionSnapshot(thread_id=sid, messages=list(history))

    def append(self, thread_id: str, user_msg: str, assistant_msg: str, *,
               owner: Optional[str] = None) -> None:
        from opensearch_pipeline.session_store import append_to_history
        append_to_history(thread_id, user_msg, assistant_msg, owner=owner)

    def clear(self, thread_id: str, *, owner: Optional[str] = None) -> bool:
        from opensearch_pipeline.session_store import clear_session
        return clear_session(thread_id, owner=owner)


def default_session_memory() -> SessionMemory:
    """生产工厂：SessionStoreMemory（承接 RAG_SESSION_BACKEND=memory|redis 后端选择）。"""
    return SessionStoreMemory()
