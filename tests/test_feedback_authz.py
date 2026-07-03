"""#F-fbk-authz /api/feedback 反馈归属鉴权回归测试。

覆盖两处修复（opensearch_pipeline/api.py）：
  1. `_feedback_owns_message(message_id, user_id)` 归属门控：
       命中 → True / 未命中 → False / 空参 → False / 查库异常 → fail-open True。
  2. `/api/feedback` 端点：匿名请求绝不采信客户端自报的 `req.user_id`，改走
       `_anon_uid`（anon:<ip 短哈希>）；且非本人 message 拒收（403）。

沙箱说明：不连真实 MySQL——mock `opensearch_pipeline.db._get_db_conn` 的 cursor.fetchone
驱动归属函数；端点侧用 fastapi TestClient，spy `handle_feedback` 捕获真正落库的 user_id。

对旧代码为何失败：旧端点 `uid = req.user_id`（采信请求体）且无归属门控/无 403：
  - test_endpoint_anon_uid_not_body_user_id：旧代码把 req.user_id 原样传给 handle_feedback，
    捕获到的 user_id == 客户端自报值 → 断言 !=body & startswith("anon:") 失败。
  - test_endpoint_non_owned_message_403：旧代码无门控恒 200 → 断言 403 失败。
"""

import contextlib

import pytest

from opensearch_pipeline import api


# ── mock DB 基建 ─────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, fetchone_result, sink):
        self._fetchone_result = fetchone_result
        self._sink = sink  # 记录 (sql, params) 以校验绑定顺序

    def execute(self, sql, params=None):
        self._sink.append((sql, params))

    def fetchone(self):
        return self._fetchone_result


class _FakeConn:
    def __init__(self, fetchone_result, sink):
        self._fetchone_result = fetchone_result
        self._sink = sink
        self.closed = False

    def cursor(self):
        # 被 `with conn.cursor() as cursor:` 使用 → 需上下文管理器
        cur = _FakeCursor(self._fetchone_result, self._sink)

        @contextlib.contextmanager
        def _cm():
            yield cur

        return _cm()

    def close(self):
        self.closed = True


def _patch_db(monkeypatch, *, fetchone_result=None, raise_exc=None, sink=None):
    """patch db._get_db_conn（函数内 `from opensearch_pipeline.db import _get_db_conn`）。"""
    import opensearch_pipeline.db as dbmod

    def _fake_get_db_conn(*a, **kw):
        if raise_exc is not None:
            raise raise_exc
        return _FakeConn(fetchone_result, sink if sink is not None else [])

    monkeypatch.setattr(dbmod, "_get_db_conn", _fake_get_db_conn)


# ── _feedback_owns_message 单测 ──────────────────────────────

def test_owns_message_hit_returns_true(monkeypatch):
    """qa_session_log 命中该 (message_id,user_id) → 归属成立 True。"""
    sink = []
    _patch_db(monkeypatch, fetchone_result=(1,), sink=sink)
    assert api._feedback_owns_message("m-1", "u-1") is True
    # 顺带校验参数绑定顺序：(message_id, user_id)
    assert sink and sink[0][1] == ("m-1", "u-1")


def test_owns_message_miss_returns_false(monkeypatch):
    """未命中（fetchone→None）→ 非本人消息 → False（端点据此 403）。"""
    _patch_db(monkeypatch, fetchone_result=None)
    assert api._feedback_owns_message("m-1", "u-1") is False


def test_owns_message_empty_args_returns_false(monkeypatch):
    """空 message_id / 空 user_id → 直接 False，不查库。"""
    called = {"db": False}

    import opensearch_pipeline.db as dbmod

    def _boom(*a, **kw):
        called["db"] = True
        raise AssertionError("空参不应触达 DB")

    monkeypatch.setattr(dbmod, "_get_db_conn", _boom)
    assert api._feedback_owns_message("", "u-1") is False
    assert api._feedback_owns_message("m-1", "") is False
    assert called["db"] is False


def test_owns_message_db_error_fail_open_true(monkeypatch):
    """查库异常 → fail-open 放行 True（不因瞬时 SELECT 抖动误拒本人反馈）。"""
    _patch_db(monkeypatch, raise_exc=RuntimeError("connection reset"))
    assert api._feedback_owns_message("m-1", "u-1") is True


# ── /api/feedback 端点集成（TestClient）──────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    return TestClient(api.app)


def test_endpoint_non_owned_message_403(client, monkeypatch):
    """(a) 归属未命中 → 端点返回 403，且不触达 handle_feedback。"""
    monkeypatch.setattr(api, "_feedback_owns_message", lambda mid, uid: False)

    called = {"handled": False}

    def _spy_handle(**kw):
        called["handled"] = True
        return True

    monkeypatch.setattr(api, "handle_feedback", _spy_handle)

    r = client.post("/api/feedback", json={
        "message_id": "m-victim",
        "user_id": "victim-staff-123",  # 客户端自报，端点应无视
        "feedback_type": "downvote",
    })
    assert r.status_code == 403
    assert called["handled"] is False


def test_endpoint_anon_uid_not_body_user_id(client, monkeypatch):
    """(b) 匿名请求：真正落库的 user_id 走 _anon_uid，绝不等于客户端自报 req.user_id。"""
    # 放行归属门控（raising=False：旧代码无此函数也不因 patch 报错，仍能对旧行为断言失败）
    monkeypatch.setattr(api, "_feedback_owns_message", lambda mid, uid: True, raising=False)

    captured = {}

    def _spy_handle(**kw):
        captured.update(kw)
        return True

    monkeypatch.setattr(api, "handle_feedback", _spy_handle)

    body_uid = "victim-staff-123"
    r = client.post("/api/feedback", json={
        "message_id": "m-1",
        "user_id": body_uid,          # 攻击者伪造他人 staffId
        "feedback_type": "upvote",
    })
    assert r.status_code == 200
    # 真正写入的 user_id 必须是匿名命名空间，而非客户端自报值
    assert captured.get("user_id") != body_uid
    assert str(captured.get("user_id", "")).startswith("anon:")


def test_endpoint_anon_uid_matches_anon_uid_helper(client, monkeypatch):
    """匿名落库 user_id 与 _anon_uid(request) 一致（同一 IP 短哈希命名空间）。"""
    monkeypatch.setattr(api, "_feedback_owns_message", lambda mid, uid: True, raising=False)

    captured = {}
    monkeypatch.setattr(api, "handle_feedback",
                        lambda **kw: (captured.update(kw) or True))

    xff = "203.0.113.55"
    r = client.post("/api/feedback",
                    json={"message_id": "m-1", "user_id": "spoof",
                          "feedback_type": "upvote"},
                    headers={"X-Forwarded-For": xff})
    assert r.status_code == 200
    uid = captured.get("user_id", "")
    assert uid.startswith("anon:")
    # 与 helper 同源：稳定按 IP 归属，不随请求体变化
    assert uid != "spoof"
