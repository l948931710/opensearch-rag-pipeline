# -*- coding: utf-8 -*-
"""
test_prod_access_rw_routing.py — 验证 get_prod_rw_conn 加载 .env.production
而不是 fallback 到 .env.prod_ro（fuling_ro 切换后的 regression test）。

背景：2026-06-14 把 .env.prod_ro 从 fuling_admin 切到 fuling_ro 之后，
load_prod_env 的默认 fallback 链 [".env.prod_ro", ".env.test", ".env.production"]
会让 get_prod_rw_conn 拿到 fuling_ro 凭证，下次 cleanup/rebuild 会 ERROR 1142。

修复：get_prod_rw_conn 默认 overlay='.env.production' + sanity 守卫拒绝
加载 *_ro 账号。
"""

from __future__ import annotations

from datetime import date
from unittest import mock

import pytest

from opensearch_pipeline.prod_access import (
    ProdAccessError,
    get_prod_readonly_conn,
    get_prod_rw_conn,
)


def _today_token():
    return f"PROD-RW:{date.today().isoformat()}"


# ─── RW 默认走 .env.production ─────────────────────────────────────────

def test_get_prod_rw_conn_defaults_to_env_production(monkeypatch):
    """get_prod_rw_conn(ack=...) 不传 overlay 时应该加载 .env.production，
    不能 fallback 到 .env.prod_ro。
    """
    captured = {}

    def fake_load(overlay=None):
        captured['overlay'] = overlay
        return {
            'RAG_RDS_HOST': 'fake-host',
            'RAG_RDS_USER': 'fuling_admin',
            'RAG_RDS_PASSWORD': 'fake',
            '_source_file': '.env.production',
        }

    def fake_connect(env, **kw):
        captured['env_user'] = env['RAG_RDS_USER']
        captured['env_source'] = env['_source_file']
        return mock.MagicMock(name='conn')

    monkeypatch.setattr('opensearch_pipeline.prod_access.load_prod_env', fake_load)
    monkeypatch.setattr('opensearch_pipeline.prod_access._connect', fake_connect)

    conn = get_prod_rw_conn(ack=_today_token())
    assert conn is not None
    assert captured['overlay'] == '.env.production', \
        f"expected overlay='.env.production', got {captured['overlay']!r}"
    assert captured['env_user'] == 'fuling_admin'
    assert captured['env_source'] == '.env.production'


def test_get_prod_rw_conn_explicit_overlay_respected(monkeypatch):
    """如果显式传 overlay='.env.test' 等，应该尊重显式参数（不强制 .env.production）。"""
    captured = {}

    def fake_load(overlay=None):
        captured['overlay'] = overlay
        return {
            'RAG_RDS_HOST': 'fake',
            'RAG_RDS_USER': 'fuling_admin',
            'RAG_RDS_PASSWORD': 'fake',
            '_source_file': overlay or 'fallback',
        }

    monkeypatch.setattr('opensearch_pipeline.prod_access.load_prod_env', fake_load)
    monkeypatch.setattr('opensearch_pipeline.prod_access._connect',
                        lambda *a, **k: mock.MagicMock())

    get_prod_rw_conn(ack=_today_token(), overlay='.env.test')
    assert captured['overlay'] == '.env.test'


# ─── 只读账号 sanity 守卫 ────────────────────────────────────────────

def test_get_prod_rw_conn_refuses_ro_user(monkeypatch):
    """如果某人/某次配错把 RW path 误指到 .env.prod_ro（fuling_ro 只读账号），
    要立刻 ProdAccessError，不要让它跑到 MySQL 再 ERROR 1142。
    """
    def fake_load(overlay=None):
        return {
            'RAG_RDS_HOST': 'fake',
            'RAG_RDS_USER': 'fuling_ro',
            'RAG_RDS_PASSWORD': 'fake',
            '_source_file': '.env.prod_ro',
        }

    monkeypatch.setattr('opensearch_pipeline.prod_access.load_prod_env', fake_load)
    monkeypatch.setattr('opensearch_pipeline.prod_access._connect',
                        lambda *a, **k: mock.MagicMock())

    with pytest.raises(ProdAccessError, match=r"看起来是只读账号"):
        get_prod_rw_conn(ack=_today_token(), overlay='.env.prod_ro')


def test_get_prod_rw_conn_refuses_arbitrary_ro_suffix(monkeypatch):
    """守卫对其他 `_ro` 后缀账号也应该拦（防御性，未来可能建 dms_ro / app_ro 等）。"""
    def fake_load(overlay=None):
        return {
            'RAG_RDS_HOST': 'fake',
            'RAG_RDS_USER': 'app_ro',
            'RAG_RDS_PASSWORD': 'fake',
            '_source_file': '.env.fake',
        }

    monkeypatch.setattr('opensearch_pipeline.prod_access.load_prod_env', fake_load)
    monkeypatch.setattr('opensearch_pipeline.prod_access._connect',
                        lambda *a, **k: mock.MagicMock())

    with pytest.raises(ProdAccessError, match=r"看起来是只读账号"):
        get_prod_rw_conn(ack=_today_token(), overlay='.env.fake')


# ─── RO path 未受影响 ────────────────────────────────────────────────

def test_get_prod_readonly_conn_still_works(monkeypatch):
    """readonly 路径不应受 RW 改动影响——仍走 load_prod_env 默认 fallback chain
    (prod_ro 优先)。"""
    captured = {}

    def fake_load(overlay=None):
        captured['overlay'] = overlay
        return {
            'RAG_RDS_HOST': 'fake',
            'RAG_RDS_USER': 'fuling_ro',
            'RAG_RDS_PASSWORD': 'fake',
            '_source_file': '.env.prod_ro',
        }

    def fake_connect(env, **kw):
        captured['init_command'] = kw.get('init_command')
        return mock.MagicMock()

    monkeypatch.setattr('opensearch_pipeline.prod_access.load_prod_env', fake_load)
    monkeypatch.setattr('opensearch_pipeline.prod_access._connect', fake_connect)

    get_prod_readonly_conn()
    assert captured['overlay'] is None, \
        "readonly 不应强制 overlay='.env.production'，让 fallback chain 选 prod_ro"
    assert captured['init_command'] == 'SET SESSION TRANSACTION READ ONLY'


# ─── token 校验仍有效 ─────────────────────────────────────────────────

def test_get_prod_rw_conn_rejects_stale_token(monkeypatch):
    """跨日 token 必须拒（这条本来就有，这里 regression）。"""
    monkeypatch.setattr('opensearch_pipeline.prod_access.load_prod_env',
                        lambda overlay=None: {})

    with pytest.raises(ProdAccessError, match=r"生产读写令牌无效"):
        get_prod_rw_conn(ack='PROD-RW:2020-01-01')
