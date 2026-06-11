# -*- coding: utf-8 -*-
"""
test_prod_access.py — 生产连接统一入口（prod_access.py）

覆盖：只读连接强制 READ ONLY init_command / RW 令牌当日校验 / env 解析不污染进程 /
OSS 只读代理拦写。
"""

from datetime import date, timedelta

import pytest

import opensearch_pipeline.prod_access as pa
from opensearch_pipeline.prod_access import ProdAccessError


@pytest.fixture
def fake_env(monkeypatch):
    env = {"RAG_RDS_HOST": "prod-host", "RAG_RDS_PORT": "3306",
           "RAG_RDS_USER": "u", "RAG_RDS_PASSWORD": "p",
           "RAG_RDS_DATABASE": "fuling_knowledge", "_source_file": ".env.prod_ro"}
    monkeypatch.setattr(pa, "load_prod_env", lambda overlay=None: dict(env))
    return env


@pytest.fixture
def capture_connect(monkeypatch):
    calls = {}

    def _fake_connect(env, **kwargs):
        calls.update(kwargs)
        calls["host"] = env["RAG_RDS_HOST"]
        return "CONN"
    monkeypatch.setattr(pa, "_connect", _fake_connect)
    return calls


def test_readonly_conn_sets_read_only_session(fake_env, capture_connect):
    assert pa.get_prod_readonly_conn() == "CONN"
    assert capture_connect["init_command"] == "SET SESSION TRANSACTION READ ONLY"


def test_rw_conn_requires_today_token(fake_env, capture_connect):
    today = date.today().isoformat()
    assert pa.get_prod_rw_conn(ack=f"PROD-RW:{today}") == "CONN"
    assert capture_connect.get("init_command") is None


@pytest.mark.parametrize("bad", [
    "", "PROD-RW", "prod-rw:2026-06-10", "PROD-RW:1999-01-01",
])
def test_rw_conn_rejects_bad_tokens(fake_env, capture_connect, bad):
    with pytest.raises(ProdAccessError):
        pa.get_prod_rw_conn(ack=bad)


def test_rw_conn_rejects_yesterday(fake_env, capture_connect):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with pytest.raises(ProdAccessError):
        pa.get_prod_rw_conn(ack=f"PROD-RW:{yesterday}")


def test_load_prod_env_does_not_pollute_process(tmp_path, monkeypatch):
    """load_prod_env 返回 dict，不写 os.environ。"""
    import os
    (tmp_path / ".env").write_text("RAG_SHARED=base\n", encoding="utf-8")
    (tmp_path / ".env.prod_ro").write_text(
        "RAG_RDS_HOST=prodhost\nRAG_PROBE_VAR=secret\n", encoding="utf-8")
    monkeypatch.setattr(pa, "_REPO_ROOT", tmp_path)
    env = pa.load_prod_env()
    assert env["RAG_PROBE_VAR"] == "secret"
    assert env["RAG_SHARED"] == "base"
    assert env["_source_file"] == ".env.prod_ro"
    assert "RAG_PROBE_VAR" not in os.environ


def test_load_prod_env_overlay_priority(tmp_path, monkeypatch):
    """显式 overlay > prod_ro > test > production。"""
    (tmp_path / ".env.production").write_text("RAG_RDS_HOST=via-production\n", encoding="utf-8")
    monkeypatch.setattr(pa, "_REPO_ROOT", tmp_path)
    assert pa.load_prod_env()["RAG_RDS_HOST"] == "via-production"
    (tmp_path / ".env.prod_ro").write_text("RAG_RDS_HOST=via-prod-ro\n", encoding="utf-8")
    assert pa.load_prod_env()["RAG_RDS_HOST"] == "via-prod-ro"
    assert pa.load_prod_env(".env.production")["RAG_RDS_HOST"] == "via-production"


def test_readonly_oss_bucket_blocks_writes():
    class _B:
        def get_object(self, k):
            return "ok"

        def put_object(self, k, d):  # pragma: no cover — 必须被代理拦截
            raise AssertionError("should never reach real put")
    ro = pa._ReadOnlyBucket(_B())
    assert ro.get_object("raw/a.pdf") == "ok"
    with pytest.raises(ProdAccessError):
        ro.put_object("raw/a.pdf", b"x")
