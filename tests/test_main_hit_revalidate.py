# -*- coding: utf-8 -*-
"""P3-1（盲区审计）主命中 RDS 复核：retriever._revalidate_main_hits。

权限执行不对称的补齐——邻居/扩展路径一直有 is_active=1 + 同权限复核，主 HA3 命中
此前直接投放。本复核丢弃 is_active=0 / permission_level·owner_dept 与权威表不一致 /
RDS 已无此 chunk 的命中；权威不可达或整体空集则保留（HA3 服务端过滤是第一道边界）。
"""
import pytest

from opensearch_pipeline import retriever as rt


class _Cur:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur

    def close(self):
        pass


@pytest.fixture
def real_db_mode(monkeypatch):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config(), "simulate_db", False)
    monkeypatch.setattr(get_config().rag, "main_hit_revalidate", True)


def _hits():
    return [
        {"chunk_id": "c1", "permission_level": "public", "owner_dept": ""},
        {"chunk_id": "c2", "permission_level": "dept_internal", "owner_dept": "hr"},
        {"chunk_id": "c3", "permission_level": "public", "owner_dept": ""},
        {"chunk_id": "c4", "permission_level": "public", "owner_dept": ""},
    ]


def test_revalidate_drops_inactive_drifted_and_missing(real_db_mode, monkeypatch):
    """c1 一致保留；c2 权限收紧（public→dept_internal 投影未收敛的反方向）丢弃；
    c3 已停用丢弃；c4 RDS 已无此行（purge 后 HA3 残留）丢弃。"""
    rows = [
        ("c1", 1, "public", ""),
        ("c2", 1, "restricted", "hr"),   # 权威已收紧，HA3 投影还是 dept_internal
        ("c3", 0, "public", ""),         # 已停用（retire/旧版本）
        # c4 无行
    ]
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Conn(rows))
    kept = rt._revalidate_main_hits(_hits())
    assert [c["chunk_id"] for c in kept] == ["c1"]


def test_revalidate_owner_dept_drift_dropped(real_db_mode, monkeypatch):
    rows = [("c1", 1, "public", ""), ("c2", 1, "dept_internal", "finance")]
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Conn(rows))
    kept = rt._revalidate_main_hits(_hits()[:2])
    assert [c["chunk_id"] for c in kept] == ["c1"]


def test_revalidate_null_owner_equals_empty_string(real_db_mode, monkeypatch):
    """RDS NULL owner_dept 与 HA3 空串视为一致（历史行不误杀）。"""
    rows = [("c1", 1, "public", None)]
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Conn(rows))
    kept = rt._revalidate_main_hits([_hits()[0]])
    assert len(kept) == 1


def test_revalidate_fail_open_on_db_error(real_db_mode, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("rds down")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    hits = _hits()
    assert rt._revalidate_main_hits(hits) == hits


def test_revalidate_fail_open_on_empty_result(real_db_mode, monkeypatch):
    """整体空集 = 几乎必然连错库/桩连接，按权威不可用处理保留结果，绝不全灭答案。"""
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: _Conn([]))
    hits = _hits()
    assert rt._revalidate_main_hits(hits) == hits


def test_revalidate_flag_off_untouched(real_db_mode, monkeypatch):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config().rag, "main_hit_revalidate", False)

    def _boom(*a, **k):
        raise AssertionError("flag off 不得建连")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    hits = _hits()
    assert rt._revalidate_main_hits(hits) == hits


def test_revalidate_simulate_mode_skipped(monkeypatch):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config(), "simulate_db", True)

    def _boom(*a, **k):
        raise AssertionError("simulate 模式不得建连")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    hits = _hits()
    assert rt._revalidate_main_hits(hits) == hits
