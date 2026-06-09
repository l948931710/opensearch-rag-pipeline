# -*- coding: utf-8 -*-
"""
test_session_store.py — 会话存储线程安全回归

api.py 的 def 处理器跑在 FastAPI 线程池、dingtalk_bot 每条消息又另起线程，
多线程并发访问同一个 _LRUSessionStore。OrderedDict 的 check-then-act（in→del、
create→popitem 淘汰、move_to_end）不是线程安全的，必须靠锁串行化。
"""

import threading

from opensearch_pipeline import session_store
from opensearch_pipeline.session_store import _LRUSessionStore


def test_concurrent_get_create_evict_no_crash():
    """8 线程在持续淘汰（key 数 > maxsize）下并发 get/create/set_history 不得抛错或损坏结构。"""
    store = _LRUSessionStore(maxsize=10)
    errors = []

    def worker(n):
        try:
            for i in range(300):
                key = f"k{(i + n) % 20}"  # 20 个 key、容量 10 → 持续 LRU 淘汰
                entry = store.get(key)
                if entry is None:
                    entry = store.create(key)
                entry.history.append({"role": "user", "content": "x"})
                store.set_history(key, entry.history[-20:])
        except Exception as ex:  # noqa: BLE001 - 测试需捕获任意线程异常
            errors.append(ex)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发访问抛出异常: {errors[:3]}"
    assert len(store._store) <= 10, "LRU 容量上限被突破"


def test_concurrent_module_level_api_no_crash(monkeypatch):
    """模块级 get_or_create_session / append_to_history 的复合操作在并发下保持一致。"""
    monkeypatch.setattr(session_store, "_sessions", _LRUSessionStore(maxsize=50))
    errors = []

    def worker(n):
        try:
            for i in range(200):
                sid = f"s{(i + n) % 8}"
                got_sid, _ = session_store.get_or_create_session(sid)
                assert got_sid == sid
                session_store.append_to_history(sid, "q", "a")
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发访问抛出异常: {errors[:3]}"
    # 历史被裁剪到上限（MAX_HISTORY_TURNS*2），不会无限增长
    for sid in (f"s{i}" for i in range(8)):
        _, hist = session_store.get_or_create_session(sid)
        assert len(hist) <= session_store.MAX_HISTORY_TURNS * 2
