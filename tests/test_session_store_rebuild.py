# -*- coding: utf-8 -*-
"""
test_session_store_rebuild.py — WS0-2 回读重建（Redis/进程重启后从 qa_session_log 恢复会话）

mock db._get_db_conn 验 _rebuild_from_qa_log 的排序/fail-safe/跳过；monkeypatch 验
get_or_create_session 的 rebuild-on-miss + 回种 + flag/session_id 门控。不碰真库。
"""
from opensearch_pipeline import session_store as ss


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sql = sql

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        pass


# ── _rebuild_from_qa_log ─────────────────────────────────────────────────────
def test_rebuild_orders_desc_rows_to_chronological(monkeypatch):
    import opensearch_pipeline.db as db
    monkeypatch.setattr(db, "_get_db_conn",
                        lambda: _Conn([("Q3", "A3"), ("Q2", "A2"), ("Q1", "A1")]))  # created_at DESC
    out = ss._rebuild_from_qa_log("sess-x", "u1", 10)
    assert [m["content"] for m in out] == ["Q1", "A1", "Q2", "A2", "Q3", "A3"]      # 回时间正序
    assert [m["role"] for m in out] == ["user", "assistant"] * 3


def test_rebuild_null_answer_skipped(monkeypatch):
    """卫生层对齐在线入史策略（should_append_history）：空答案的轮不回种——
    否则拒答/半截轮以空 assistant 消息复活进上下文（深度审查 D 组）。"""
    import opensearch_pipeline.db as db
    monkeypatch.setattr(db, "_get_db_conn", lambda: _Conn([("Q1", None)]))
    assert ss._rebuild_from_qa_log("s", "u", 10) == []


def test_clear_tombstone_suppresses_rebuild(monkeypatch):
    """主动清除墓碑：clear_session 之后（会话超时窗内）不回读复活刚清掉的上下文——
    「新会话」/ /api/session/clear 不能被 qa_log 回读静默失效（深度审查 D 组 P1）。"""
    monkeypatch.setattr(ss, "_backend", ss.MemorySessionStore())
    monkeypatch.setattr(ss, "_conversation_history_on", lambda: True)
    monkeypatch.setattr(ss, "_rebuild_from_qa_log",
                        lambda *a: [{"role": "user", "content": "旧问"},
                                    {"role": "assistant", "content": "旧答"}])
    ss.append_to_history("tomb-1", "q", "a", owner="u")
    assert ss.clear_session("tomb-1", owner="u") is True
    sid, hist = ss.get_or_create_session("tomb-1", owner="u")
    assert sid == "tomb-1"
    assert hist == []          # 墓碑期内不回读复活


def test_rebuild_fail_safe_returns_empty_on_db_error(monkeypatch):
    import opensearch_pipeline.db as db

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "_get_db_conn", _boom)
    assert ss._rebuild_from_qa_log("s", "u", 10) == []          # fail-safe，不抛


def test_rebuild_skips_without_session_or_owner():
    assert ss._rebuild_from_qa_log(None, "u", 10) == []
    assert ss._rebuild_from_qa_log("s", None, 10) == []


# ── get_or_create_session rebuild-on-miss ────────────────────────────────────
def test_get_or_create_rebuilds_on_miss_and_seeds(monkeypatch):
    monkeypatch.setattr(ss, "_backend", ss.MemorySessionStore())
    monkeypatch.setattr(ss, "_conversation_history_on", lambda: True)
    monkeypatch.setattr(ss, "_rebuild_from_qa_log",
                        lambda sid, owner, n: [{"role": "user", "content": "Q1"},
                                               {"role": "assistant", "content": "A1"}])
    _, hist = ss.get_or_create_session("sess-restart", owner="u1")
    assert [m["content"] for m in hist] == ["Q1", "A1"]         # 回读重建
    # 已回种 → 二次取用回种值（改 rebuild 返回值证明其不再被调）
    monkeypatch.setattr(ss, "_rebuild_from_qa_log", lambda *a: [{"role": "user", "content": "X"}])
    _, hist2 = ss.get_or_create_session("sess-restart", owner="u1")
    assert [m["content"] for m in hist2] == ["Q1", "A1"]


def test_get_or_create_no_rebuild_when_flag_off(monkeypatch):
    monkeypatch.setattr(ss, "_backend", ss.MemorySessionStore())
    monkeypatch.setattr(ss, "_conversation_history_on", lambda: False)
    monkeypatch.setattr(ss, "_rebuild_from_qa_log", lambda *a: [{"role": "user", "content": "NO"}])
    _, hist = ss.get_or_create_session("s-off", owner="u1")
    assert hist == []                                          # flag off → 不重建


def test_get_or_create_no_rebuild_when_session_id_none(monkeypatch):
    monkeypatch.setattr(ss, "_backend", ss.MemorySessionStore())
    monkeypatch.setattr(ss, "_conversation_history_on", lambda: True)
    called = {"n": 0}

    def _rb(*a):
        called["n"] += 1
        return []
    monkeypatch.setattr(ss, "_rebuild_from_qa_log", _rb)
    _, hist = ss.get_or_create_session(None, owner="u1")       # 新建（无 session_id）→ 不重建
    assert hist == [] and called["n"] == 0
