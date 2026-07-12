# -*- coding: utf-8 -*-
"""
test_agent_runtime_session_memory.py — SessionMemory（agent 多轮记忆门面；WS1-1 / 报告 L2）

复用 session_store memory 后端（默认）：get_snapshot/append 往返、多轮累积、None→新建 sid、
owner 归属隔离、clear。用独立 thread_id 避免与其它测试串味。
"""
import pytest

from opensearch_pipeline import session_store as ss
from opensearch_pipeline.agent_runtime.session_memory import (
    SessionStoreMemory, default_session_memory)


def test_default_factory_is_session_store_memory():
    assert isinstance(default_session_memory(), SessionStoreMemory)


def test_snapshot_empty_for_new_thread():
    snap = SessionStoreMemory().get_snapshot("sm-new-1", owner="u1")
    assert snap.thread_id == "sm-new-1"
    assert snap.messages == []


def test_append_then_snapshot_returns_turns_in_order():
    mem = SessionStoreMemory()
    tid = "sm-multi-1"
    ss.clear_session(tid, owner="u1", trusted=True)
    mem.append(tid, "Q1", "A1", owner="u1")
    mem.append(tid, "Q2", "A2", owner="u1")
    snap = mem.get_snapshot(tid, owner="u1")
    assert [m["role"] for m in snap.messages] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in snap.messages] == ["Q1", "A1", "Q2", "A2"]


def test_none_thread_mints_id():
    snap = SessionStoreMemory().get_snapshot(None, owner="u1")
    assert snap.thread_id and len(snap.thread_id) >= 8      # 新建 UUID


def test_owner_isolation_blocks_other_user():
    mem = SessionStoreMemory()
    tid = "sm-own-1"
    ss.clear_session(tid, owner="alice", trusted=True)
    mem.append(tid, "机密问题", "机密答案", owner="alice")
    with pytest.raises(ss.SessionOwnershipError):
        mem.get_snapshot(tid, owner="mallory")             # 他人不能读会话


def test_clear_removes_history():
    mem = SessionStoreMemory()
    tid = "sm-clr-1"
    mem.append(tid, "Q", "A", owner="u1")
    assert mem.clear(tid, owner="u1") is True
    assert mem.get_snapshot(tid, owner="u1").messages == []
