# -*- coding: utf-8 -*-
"""tests/test_tombstone_f4d.py — 评审F4d（2026-07-21）：user_role.is_active=0 墓碑 = 权威空组。

修复前：所有身份查询带 AND is_active=1 过滤 → 墓碑行等同「从未见过」→ 回钉钉 API
刷新出全组（撤销完全失效），且 RAG_ACL_FAIL_CLOSED 开着也拦不住（无在册行=保留令牌组）。
修复后：is_active=0 = 显式撤读权 → live/cached 双路径返回权威空组 []（≠None），
不回 API、不经 upsert 复活；api.py 零改动即在默认与 strict 两模式下同时收紧。

时延契约（有意保留，测试钉死）：bot 层 90s / live-ACL 层 45s 暖缓存窗内旧组仍可能
被复用；RAG_LIVE_ACL_REREAD=false 时收敛点=下次发令牌（≈TTL 2h）。应急即时撤权
走 RAG_REVOKED_USER_IDS / RAG_SESSION_TOKEN_MIN_IAT（见 test_auth_token_revocation）。
"""
import time

import opensearch_pipeline.api as api
import opensearch_pipeline.dingtalk_identity as di
from opensearch_pipeline.auth_token import issue_session_token


# ── 桩 DB（同 test_identity_partial_dept 形态；live SELECT 返回 4 元组含 is_active）──
class _FakeCur:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        self._last = sql

    def fetchone(self):
        if "SELECT dept_code" in self._last:
            return self.conn.cache_row
        return None

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cache_row=None):
        self.cache_row = cache_row
        self.calls = []

    def cursor(self):
        return _FakeCur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# ── live 路径 ────────────────────────────────────────────────────────────────


def test_live_tombstone_authoritative_empty_no_api_no_resurrection(monkeypatch):
    """墓碑行 → ([], True)：零钉钉 API 调用、零 INSERT（不复活）、不落回名字口径。"""
    conn = _FakeConn(cache_row=("行政部", "employee", 100, 0))   # is_active=0
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    called = {"n": 0}
    monkeypatch.setattr(di, "_fetch_dingtalk_user_info",
                        lambda sid: called.__setitem__("n", called["n"] + 1) or
                        {"user_name": "u", "dept_name": "行政部", "is_partial": False})

    groups, cacheable = di._resolve_user_dept_live("U_TOMB")

    assert groups == [] and cacheable is True
    assert called["n"] == 0, "墓碑绝不回钉钉 API 刷新（那正是撤销失效的洞）"
    assert not any("INSERT" in c[0] for c in conn.calls), "墓碑绝不经 upsert 复活"


def test_live_active_row_unaffected(monkeypatch):
    """在册活跃行（is_active=1）行为逐字不变：命中缓存即返回组。"""
    conn = _FakeConn(cache_row=("行政部", "employee", 100, 1))
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    groups, cacheable = di._resolve_user_dept_live("U1")
    assert groups == di._normalize_dept_to_codes("行政部") and cacheable is True


# ── cached（读时复核）路径 ───────────────────────────────────────────────────


def _wire_cached(monkeypatch, row):
    di._live_acl_cache_clear()

    class _Cur2(_FakeCur):
        def fetchone(self):
            if "SELECT dept_code" in self._last:
                return row
            return None

    class _Conn2(_FakeConn):
        def cursor(self):
            return _Cur2(self)

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Conn2())


def test_cached_tombstone_returns_authoritative_empty(monkeypatch):
    """cached 路径：墓碑 → []（权威空，≠None）；with_status → ([], db_ok=True)。"""
    _wire_cached(monkeypatch, ("行政部", 0))
    assert di._resolve_user_dept_cached("U_TOMB") == []
    di._live_acl_cache_clear()
    assert di._resolve_user_dept_cached("U_TOMB", with_status=True) == ([], True)


def test_cached_absent_row_still_none(monkeypatch):
    """无在册行仍是 None（保留令牌组语义不变——F4d 只动墓碑腿）。"""
    _wire_cached(monkeypatch, None)
    assert di._resolve_user_dept_cached("U_NOROW") is None


# ── api.py current_identity 集成（零改动收紧）────────────────────────────────


def _identity_groups(monkeypatch, *, live, strict, db_ok=True, reread=True):
    if strict:
        monkeypatch.setenv("RAG_ACL_FAIL_CLOSED", "true")
    else:
        monkeypatch.delenv("RAG_ACL_FAIL_CLOSED", raising=False)
    monkeypatch.setenv("RAG_LIVE_ACL_REREAD", "true" if reread else "false")
    monkeypatch.setattr(
        "opensearch_pipeline.dingtalk_identity._resolve_user_dept_cached",
        lambda uid, with_status=False: (live, db_ok) if with_status else live)
    token = issue_session_token("U1", dept="行政部", name="测试")
    return api.current_identity(authorization="Bearer " + token).acl_groups


def test_identity_tombstone_denies_default_mode(monkeypatch):
    """默认（fail-open）模式：live=[]（墓碑权威空）→ 令牌内嵌组被清空。"""
    assert _identity_groups(monkeypatch, live=[], strict=False) == []


def test_identity_tombstone_denies_strict_mode(monkeypatch):
    """strict 模式同样清空——修复前 strict 也拦不住墓碑（无在册行腿保留令牌组）。"""
    assert _identity_groups(monkeypatch, live=[], strict=True) == []


def test_identity_absent_row_keeps_token_groups(monkeypatch):
    """无在册行（None）在两模式下都保留令牌组——既有语义不受 F4d 影响
    （与 test_main_p0_hardening::test_failclosed_no_record_keeps_token_groups 同锚）。"""
    assert "行政部" in _identity_groups(monkeypatch, live=None, strict=True)
    assert "行政部" in _identity_groups(monkeypatch, live=None, strict=False)


def test_identity_reread_disabled_keeps_token_groups(monkeypatch):
    """RAG_LIVE_ACL_REREAD=false：复核整体跳过 → 墓碑在令牌有效期内不生效
    （时延契约的条件性——收敛点=下次发令牌 ≈TTL 2h；文档同口径）。"""
    assert "行政部" in _identity_groups(monkeypatch, live=[], strict=False, reread=False)


# ── 暖缓存窗口（时延契约钉死）────────────────────────────────────────────────


def test_warm_bot_cache_serves_stale_groups_within_ttl(monkeypatch):
    """bot 层 90s 暖缓存窗内墓碑不生效（有意的有界撤销时延）；缓存过期后立即空组。"""
    conn = _FakeConn(cache_row=("行政部", "employee", 100, 0))   # DB 已墓碑
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    di._bot_dept_cache_clear()
    stale = di._normalize_dept_to_codes("行政部")
    with di._bot_dept_cache_lock:
        di._bot_dept_cache["U_TOMB"] = (time.time() + 90, list(stale))

    assert di._resolve_user_dept("U_TOMB") == stale, "暖缓存窗内旧组仍被复用（≤90s 契约）"

    di._bot_dept_cache_clear()
    assert di._resolve_user_dept("U_TOMB") == [], "缓存失效后墓碑立即生效（权威空组）"
