# -*- coding: utf-8 -*-
"""
test_dingtalk_card_authz_empty_userid.py — 卡片回调归属校验回归测试

#F-dingtalk-empty-userid
覆盖 _card_callback_authorized 的跨用户归属门控：空 userId(caller) 不再绕过校验。

被修复缺陷：旧实现的跨用户判定形如 `if owner and user_id and owner != user_id`，
当回调 caller(userId) 为空时，`user_id` 短路使条件恒 False → 攻击者用空 userId
伪造 POST 即可绕过归属校验、灌他人消息的反馈/工单。修复后条件为
`if owner and owner != user_id`：空 caller 与非空 owner 不符 → 判为鉴权失败(False)。

本沙箱用 monkeypatch 伪造 DB：
  - opensearch_pipeline.db._get_db_conn 返回假 conn（cursor.fetchone 可控/或抛异常）
  - opensearch_pipeline.dingtalk_bot._op_db 固定库名，避免依赖真实 config/RDS
不触碰任何真实 DB/SDK/网络。
"""


import pytest

import opensearch_pipeline.db as db_mod
import opensearch_pipeline.dingtalk_bot as dbot
from opensearch_pipeline.dingtalk_bot import _card_callback_authorized, _QA_CTX_COLS


# ═══════════════════════════════════════════════════════════════
# 伪造 DB 基础设施（无真实连接）
# ═══════════════════════════════════════════════════════════════

class _FakeCursor:
    """支持 `with conn.cursor() as cur:` 上下文 + execute/fetchone。"""

    def __init__(self, row):
        self._row = row
        self.executed = []  # 记录 (sql, params)，便于附带断言绑定参数

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._cursor = _FakeCursor(row)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.fixture
def patch_db(monkeypatch):
    """返回一个 helper：给定 fetchone 的行(或 raise=异常)，装好 monkeypatch。

    _card_callback_authorized 内部是 `from opensearch_pipeline.db import _get_db_conn`
    （调用时按名从 db 模块取），故 patch db 模块属性即可命中。
    _op_db 固定为 testdb，绕开真实 config。
    """
    # 归属校验只做读；SQL 用到 _op_db()，固定掉即可（cursor.execute 是假的，SQL 内容不影响判定）
    monkeypatch.setattr(dbot, "_op_db", lambda: "testdb")

    created = {}

    def _install(row=None, raise_exc=None):
        if raise_exc is not None:
            def _boom(*a, **k):
                raise raise_exc
            monkeypatch.setattr(db_mod, "_get_db_conn", _boom)
            return None
        conn = _FakeConn(row)
        created["conn"] = conn
        monkeypatch.setattr(db_mod, "_get_db_conn", lambda *a, **k: conn)
        return conn

    _install.created = created
    return _install


def _row(user_id):
    """构造与 _QA_CTX_COLS 对齐的 dict 行（生产 DictCursor 形态）。"""
    d = {c: None for c in _QA_CTX_COLS}
    d["user_id"] = user_id
    d["session_id"] = "sess-1"
    d["query_text"] = "q"
    d["answer_text"] = "a"
    return d


# ═══════════════════════════════════════════════════════════════
# (a) 核心回归：有明确归属，空 caller → 拒绝（旧代码会误放行）
# ═══════════════════════════════════════════════════════════════

def test_empty_caller_with_owner_rejected(patch_db):
    """#F-dingtalk-empty-userid 核心：owner=alice、caller="" → False。

    旧代码 `if owner and user_id and owner != user_id` 因 user_id 为空短路 → 返回 True。
    修复后 `if owner and owner != user_id` → alice != "" → 返回 False。
    """
    patch_db(row=_row("alice"))
    assert _card_callback_authorized("mid-real", "") is False


def test_none_caller_with_owner_rejected(patch_db):
    """caller 为 None（body 里 userId 缺失落到 None）同样应被拒。"""
    patch_db(row=_row("alice"))
    assert _card_callback_authorized("mid-real", None) is False


# ═══════════════════════════════════════════════════════════════
# (b) 跨用户 caller → 拒绝
# ═══════════════════════════════════════════════════════════════

def test_cross_user_caller_rejected(patch_db):
    patch_db(row=_row("alice"))
    assert _card_callback_authorized("mid-real", "bob") is False


# ═══════════════════════════════════════════════════════════════
# (c) 本人 caller → 放行，且回填 prefetch 上下文
# ═══════════════════════════════════════════════════════════════

def test_owner_caller_allowed(patch_db):
    patch_db(row=_row("alice"))
    assert _card_callback_authorized("mid-real", "alice") is True


def test_owner_caller_prefills_prefetch(patch_db):
    patch_db(row=_row("alice"))
    prefetch = {}
    assert _card_callback_authorized("mid-real", "alice", prefetch=prefetch) is True
    assert prefetch["qa_context"]["user_id"] == "alice"
    assert prefetch["qa_context"]["session_id"] == "sess-1"


# ═══════════════════════════════════════════════════════════════
# (d) 群聊/无归属（user_id 空或 None）→ 仅存在性门控，放行
# ═══════════════════════════════════════════════════════════════

def test_group_chat_empty_owner_allows(patch_db):
    """owner 空字符串（群聊无归属）：存在性门控放行，即便 caller 也为空。"""
    patch_db(row=_row(""))
    assert _card_callback_authorized("mid-real", "") is True
    assert _card_callback_authorized("mid-real", "somebody") is True


def test_group_chat_none_owner_allows(patch_db):
    """owner 为 None → `or ""` 归一化为空 → 放行。"""
    patch_db(row=_row(None))
    assert _card_callback_authorized("mid-real", "somebody") is True


def test_tuple_row_owner_none_allows(patch_db):
    """行以 tuple(非 dict) 返回时，owner 取 row[0]；None → 放行。"""
    tuple_row = (None, "sess", "q", "a", None, None)
    patch_db(row=tuple_row)
    assert _card_callback_authorized("mid-real", "somebody") is True


# ═══════════════════════════════════════════════════════════════
# (e) 查库异常 → fail-open 放行（有意降级，逐字保留）
# ═══════════════════════════════════════════════════════════════

def test_db_exception_fail_open(patch_db):
    patch_db(raise_exc=RuntimeError("db down"))
    assert _card_callback_authorized("mid-real", "anyone") is True


# ═══════════════════════════════════════════════════════════════
# (f) message_id 不存在 → 拒绝；空 message_id → 直接拒绝
# ═══════════════════════════════════════════════════════════════

def test_unknown_message_id_rejected(patch_db):
    patch_db(row=None)  # fetchone 返回 None
    assert _card_callback_authorized("mid-forged", "alice") is False


def test_blank_message_id_rejected(patch_db):
    # 空 message_id：函数首行即 return False，不应触发任何 DB 调用
    patch_db(row=_row("alice"))
    assert _card_callback_authorized("", "alice") is False


# ═══════════════════════════════════════════════════════════════
# 附带：绑定参数以 %s + params 元组传入（防注入/绑定顺序回归）
# ═══════════════════════════════════════════════════════════════

def test_message_id_bound_as_param(patch_db):
    conn = patch_db(row=_row("alice"))
    _card_callback_authorized("mid-xyz", "alice")
    sql, params = conn.cursor().executed[0]
    assert params == ("mid-xyz",)
    assert "%s" in sql
