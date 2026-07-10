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
    monkeypatch.setattr(session_store, "_backend", _LRUSessionStore(maxsize=50))
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


def test_clear_session_removes_history(monkeypatch):
    """clear_session：清除后 get_or_create 返回全新空历史；幂等；空 id 安全。"""
    monkeypatch.setattr(session_store, "_backend", _LRUSessionStore(maxsize=50))

    sid, _ = session_store.get_or_create_session("sess-clear")
    session_store.append_to_history(sid, "问题", "回答")
    _, hist = session_store.get_or_create_session(sid)
    assert len(hist) == 2

    assert session_store.clear_session(sid) is True
    _, hist2 = session_store.get_or_create_session(sid)
    assert hist2 == [], "清除后必须是全新空历史"

    assert session_store.clear_session("nonexistent") is False  # 幂等
    assert session_store.clear_session("") is False
    assert session_store.clear_session(None) is False


def test_concurrent_clear_and_append_no_crash(monkeypatch):
    """clear 与 append 并发交错：不抛错、不破坏结构（清除点之后历史从零重新累积）。"""
    monkeypatch.setattr(session_store, "_backend", _LRUSessionStore(maxsize=50))
    errors = []

    def appender():
        try:
            for _ in range(200):
                session_store.append_to_history("race", "q", "a")
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)

    def clearer():
        try:
            for _ in range(100):
                session_store.clear_session("race")
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)

    threads = [threading.Thread(target=appender) for _ in range(4)] + \
              [threading.Thread(target=clearer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 clear/append 抛出异常: {errors[:3]}"
    _, hist = session_store.get_or_create_session("race")
    assert len(hist) <= session_store.MAX_HISTORY_TURNS * 2


# ── 会话归属绑定（盲区审计 P3-6）───────────────────────────────


def test_owner_binding_denies_other_identity(monkeypatch):
    """已绑定身份的条目：他人身份/匿名访问一律 SessionOwnershipError；本人放行。"""
    import pytest

    monkeypatch.setattr(session_store, "_backend", _LRUSessionStore(maxsize=50))

    sid, _ = session_store.get_or_create_session("cid1:VICTIM", owner="VICTIM")
    session_store.append_to_history(sid, "受限问题", "受限回答", owner="VICTIM")

    # 他人已认证身份伪造钉钉结构化 key → 拒绝
    with pytest.raises(session_store.SessionOwnershipError):
        session_store.get_or_create_session("cid1:VICTIM", owner="ATTACKER")
    # 匿名持有 key 也不放行（owner=None ≠ 已绑定身份）
    with pytest.raises(session_store.SessionOwnershipError):
        session_store.get_or_create_session("cid1:VICTIM")
    with pytest.raises(session_store.SessionOwnershipError):
        session_store.append_to_history("cid1:VICTIM", "q", "a", owner="ATTACKER")
    with pytest.raises(session_store.SessionOwnershipError):
        session_store.clear_session("cid1:VICTIM", owner="ATTACKER")

    # 本人照常
    _, hist = session_store.get_or_create_session("cid1:VICTIM", owner="VICTIM")
    assert len(hist) == 2


def test_anonymous_entry_binds_on_first_authenticated_touch(monkeypatch):
    """匿名创建（服务端 UUID）持有即所有；首个已认证访问者就地绑定，之后他人被拒。"""
    import pytest

    monkeypatch.setattr(session_store, "_backend", _LRUSessionStore(maxsize=50))

    sid, _ = session_store.get_or_create_session(None)  # 匿名创建
    _, _ = session_store.get_or_create_session(sid)     # 匿名再访问 OK
    _, _ = session_store.get_or_create_session(sid, owner="U1")  # 首个认证者绑定
    with pytest.raises(session_store.SessionOwnershipError):
        session_store.get_or_create_session(sid, owner="U2")


def test_trusted_caller_evicts_hijacked_entry(monkeypatch):
    """trusted（钉钉 bot）：key 被他人抢注时丢弃污染条目重建，真实用户不被 DoS，
    且抢注者留下的历史不会泄给真实用户。"""
    import pytest

    monkeypatch.setattr(session_store, "_backend", _LRUSessionStore(maxsize=50))

    # 攻击者以自己身份抢注受害者的钉钉会话 key 并塞入历史
    session_store.get_or_create_session("cidX:VICTIM", owner="ATTACKER")
    session_store.append_to_history("cidX:VICTIM", "毒问题", "毒回答", owner="ATTACKER")

    # bot 以权威身份进来：得到全新空历史（而非异常/污染历史）
    sid, hist = session_store.get_or_create_session(
        "cidX:VICTIM", owner="VICTIM", trusted=True)
    assert sid == "cidX:VICTIM"
    assert hist == []

    # 条目现归属真实用户，攻击者再访问被拒
    with pytest.raises(session_store.SessionOwnershipError):
        session_store.get_or_create_session("cidX:VICTIM", owner="ATTACKER")

    # trusted 清除对自己命名空间无条件可清
    assert session_store.clear_session("cidX:VICTIM", trusted=True) is True


def test_owner_verification_non_ascii_ids(monkeypatch):
    """现网回归守护（深度审查 F 组）：hmac.compare_digest 对含非 ASCII 的 str 抛
    TypeError——归属比较必须 encode 后进行。钉钉 staffId/中文 user_id 二次访问
    已绑定会话绝不能 500。"""
    import pytest

    monkeypatch.setattr(session_store, "_backend", _LRUSessionStore(maxsize=50))

    sid, _ = session_store.get_or_create_session("会话-中文", owner="员工-张三")
    assert sid == "会话-中文"
    # 同一非 ASCII owner 第二次访问：必须放行（修复前 TypeError → 500）
    sid2, _ = session_store.get_or_create_session("会话-中文", owner="员工-张三")
    assert sid2 == sid
    session_store.append_to_history(sid, "问", "答", owner="员工-张三")
    assert len(session_store.get_or_create_session(sid, owner="员工-张三")[1]) == 2
    # 非 ASCII 不匹配 owner：仍是干净的归属拒绝（而非 TypeError）
    with pytest.raises(session_store.SessionOwnershipError):
        session_store.get_or_create_session(sid, owner="员工-李四")
