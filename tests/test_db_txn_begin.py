# -*- coding: utf-8 -*-
"""tests/test_db_txn_begin.py — ultra P1（2026-07-17）：_get_db_conn 取连接即 begin()。

池以 autocommit=False 建立却从不 begin() 时，DBUtils SteadyDB 视每条语句可独立重放：多语句
事务中途断连被 tough cursor 透明重连 + 只重执行失败那一句，前置写全丢却 rowcount=1 照常 commit
（账本撕裂）。begin() 关闭该重放。这里测防御式 helper 与 _get_db_conn 的接线（无真实 DB）。"""
from types import SimpleNamespace

import opensearch_pipeline.db as db


class _ConnWithBegin:
    def __init__(self):
        self.begins = 0

    def begin(self):
        self.begins += 1


def test_begin_txn_calls_begin_by_default():
    c = _ConnWithBegin()
    db._begin_txn(c)
    assert c.begins == 1


def test_begin_txn_killswitch_skips(monkeypatch):
    monkeypatch.setenv("RAG_DB_TXN_BEGIN", "false")
    c = _ConnWithBegin()
    db._begin_txn(c)
    assert c.begins == 0, "RAG_DB_TXN_BEGIN=false 恢复旧无-begin 语义"


def test_begin_txn_stub_without_begin_is_skipped():
    class _NoBegin:
        pass
    db._begin_txn(_NoBegin())   # 桩连接无 begin 则跳过，绝不抛


def test_begin_txn_begin_error_not_fatal():
    """begin 本身抛错不阻断取连接（退化为无-begin 语义，保读路径可用性）。"""
    class _Boom:
        def begin(self):
            raise RuntimeError("transient")
    db._begin_txn(_Boom())   # 不抛


def test_get_db_conn_begins_transaction(monkeypatch):
    """集成：_get_db_conn 对返回的连接调 begin()（事务开头，保 FOR UPDATE 锁语义）。"""
    guarded = _ConnWithBegin()
    monkeypatch.setattr(db, "_db_pool", object())   # 跳过懒初始化
    monkeypatch.setattr(db, "_acquire_pool_connection", lambda: object())
    monkeypatch.setattr(db, "get_config", lambda: SimpleNamespace(rds=SimpleNamespace(host="x")))
    monkeypatch.setattr("opensearch_pipeline.env_guard.GuardedDBConnection",
                        lambda conn, host: guarded)
    out = db._get_db_conn()
    assert out is guarded
    assert guarded.begins == 1, "_get_db_conn 必须对连接 begin()（关闭 SteadyDB 单句重放）"


def test_get_db_conn_killswitch_no_begin(monkeypatch):
    guarded = _ConnWithBegin()
    monkeypatch.setenv("RAG_DB_TXN_BEGIN", "off")
    monkeypatch.setattr(db, "_db_pool", object())
    monkeypatch.setattr(db, "_acquire_pool_connection", lambda: object())
    monkeypatch.setattr(db, "get_config", lambda: SimpleNamespace(rds=SimpleNamespace(host="x")))
    monkeypatch.setattr("opensearch_pipeline.env_guard.GuardedDBConnection",
                        lambda conn, host: guarded)
    db._get_db_conn()
    assert guarded.begins == 0
