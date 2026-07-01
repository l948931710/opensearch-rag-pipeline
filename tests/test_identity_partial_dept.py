# -*- coding: utf-8 -*-
"""F-22：多部门用户首解析时某部门 department/get 瞬时失败 → 不完整 CSV 绝不落缓存（否则永久
少授权）；缓存命中的自动 employee 行加行级 TTL 复核自愈残缺，seeded 行（role≠employee）永远
缓存优先（H3）。全程 monkeypatch DB 与钉钉 API，与 simulate 无关。"""
from unittest.mock import MagicMock, patch

import opensearch_pipeline.dingtalk_identity as di


# ── 桩 DB：SELECT 返回可配 cache_row=(dept_code, role, age_seconds)；记录所有 execute ──
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
        self.cache_row = cache_row     # (dept_code, role, age_seconds) 或 None=cache-miss
        self.calls = []

    def cursor(self):
        return _FakeCur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _inserted(conn):
    return any("INSERT INTO" in sql and "user_role" in sql for sql, _ in conn.calls)


# ══ 第一部分：_fetch_dingtalk_user_info 的 is_partial 判定 ══
def test_fetch_user_info_marks_partial_when_a_dept_name_empty(monkeypatch):
    """dept_id_list=[1,2]，dept 2 的 department/get 返回空名（超时）→ is_partial=True。"""
    monkeypatch.setattr("opensearch_pipeline.dingtalk_card._get_access_token", lambda: "tok")
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"errcode": 0, "result": {"name": "张三", "dept_id_list": [1, 2]}}
    monkeypatch.setattr(di.requests, "post", lambda *a, **k: resp)
    monkeypatch.setattr(di, "_fetch_dept_name",
                        lambda token, did: "国际贸易部" if did == 1 else "")  # dept 2 超时
    info = di._fetch_dingtalk_user_info("U1")
    assert info["is_partial"] is True
    assert info["dept_name"] == "国际贸易部"   # 只收到成功解析的那个


def test_fetch_user_info_complete_when_all_depts_resolve(monkeypatch):
    monkeypatch.setattr("opensearch_pipeline.dingtalk_card._get_access_token", lambda: "tok")
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"errcode": 0, "result": {"name": "张三", "dept_id_list": [1, 2]}}
    monkeypatch.setattr(di.requests, "post", lambda *a, **k: resp)
    monkeypatch.setattr(di, "_fetch_dept_name",
                        lambda token, did: {1: "国际贸易部", 2: "行政部"}[did])
    info = di._fetch_dingtalk_user_info("U1")
    assert info["is_partial"] is False
    assert set(info["dept_name"].split(",")) == {"国际贸易部", "行政部"}


def test_fetch_user_info_no_dept_is_not_partial(monkeypatch):
    """dept_id_list 为空是合法的『无部门』，不算不完整（别误判成瞬时失败）。"""
    monkeypatch.setattr("opensearch_pipeline.dingtalk_card._get_access_token", lambda: "tok")
    resp = MagicMock()
    resp.status_code = 200
    resp.json = lambda: {"errcode": 0, "result": {"name": "张三", "dept_id_list": []}}
    monkeypatch.setattr(di.requests, "post", lambda *a, **k: resp)
    info = di._fetch_dingtalk_user_info("U1")
    assert info["is_partial"] is False


# ══ 第二部分：_resolve_user_dept 的缓存策略 ══
def test_resolve_partial_result_not_cached(monkeypatch):
    """cache-miss + API 返回 is_partial=True → 绝不 INSERT（避免残缺 CSV 永久少授权），
    best-effort 返回本次已解析组。"""
    conn = _FakeConn(cache_row=None)   # cache-miss
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda *a, **k: conn)
    monkeypatch.setattr(di, "_fetch_dingtalk_user_info",
                        lambda sid: {"user_name": "u", "dept_name": "行政部", "is_partial": True})
    out = di._resolve_user_dept("U1")
    assert not _inserted(conn), "不完整解析绝不落缓存"
    assert isinstance(out, list)   # best-effort（真实子集，fail-closed 方向）


def test_resolve_complete_result_is_cached(monkeypatch):
    """cache-miss + API 完整 → 正常 INSERT 落缓存。"""
    conn = _FakeConn(cache_row=None)
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda *a, **k: conn)
    monkeypatch.setattr(di, "_fetch_dingtalk_user_info",
                        lambda sid: {"user_name": "u", "dept_name": "行政部", "is_partial": False})
    di._resolve_user_dept("U1")
    assert _inserted(conn), "完整解析应落缓存"


def test_resolve_employee_row_stale_passes_through_to_api(monkeypatch):
    """自动 employee 行过期（age > TTL）→ 穿透重查 API 并刷新缓存。"""
    conn = _FakeConn(cache_row=("行政部", "employee", 999999))   # 远超默认 6h TTL
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda *a, **k: conn)
    called = {"n": 0}

    def _fake_api(sid):
        called["n"] += 1
        return {"user_name": "u", "dept_name": "行政部", "is_partial": False}
    monkeypatch.setattr(di, "_fetch_dingtalk_user_info", _fake_api)
    di._resolve_user_dept("U1")
    assert called["n"] == 1, "过期 employee 行应穿透重查 API"
    assert _inserted(conn), "重查完整 → 刷新缓存"


def test_resolve_seeded_row_stale_never_refetched(monkeypatch):
    """seeded 行（role≠employee）即使过期也永远缓存优先，绝不穿透重查（H3 不被破坏）。"""
    conn = _FakeConn(cache_row=("行政部", "admin", 999999))   # 过期但 role=admin=seeded
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda *a, **k: conn)
    called = {"n": 0}
    monkeypatch.setattr(di, "_fetch_dingtalk_user_info",
                        lambda sid: called.__setitem__("n", called["n"] + 1))
    di._resolve_user_dept("U1")
    assert called["n"] == 0, "seeded 行绝不因 TTL 穿透重查（H3）"
    assert not _inserted(conn), "seeded 行缓存命中即返回，不写库"


def test_resolve_employee_row_fresh_returns_cache(monkeypatch):
    """自动 employee 行未过期（age < TTL）→ 直接返回缓存，不打 API。"""
    conn = _FakeConn(cache_row=("行政部", "employee", 100))   # 100s < 默认 TTL
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda *a, **k: conn)
    called = {"n": 0}
    monkeypatch.setattr(di, "_fetch_dingtalk_user_info",
                        lambda sid: called.__setitem__("n", called["n"] + 1))
    di._resolve_user_dept("U1")
    assert called["n"] == 0, "未过期缓存命中即返回，不打 API"


def test_resolve_stale_employee_api_failure_falls_back_to_cache(monkeypatch):
    """过期 employee 行穿透重查但 API 失败 → 退回旧缓存，绝不 fail-closed 掉已知部门到 public。"""
    conn = _FakeConn(cache_row=("行政部", "employee", 999999))
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn", lambda *a, **k: conn)
    monkeypatch.setattr(di, "_fetch_dingtalk_user_info", lambda sid: None)   # API 失败
    expected = di._normalize_dept_to_codes("行政部")
    out = di._resolve_user_dept("U1")
    assert out == expected, "API 失败应退回旧缓存组，不丢已知部门"
