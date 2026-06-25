# -*- coding: utf-8 -*-
"""
test_console_next_serving.py — /console-next（Vite SPA）服务端托管测试

覆盖 P5 的两条收口约束：
  · 修正#3：SPA 回退【仅作用于 /console-next/*】，不存在的 /api/* 必须返回 JSON 404 而非 index.html。
  · 修正#9：hash 静态资源 immutable，index.html / SPA 回退 no-cache。
  · 路径穿越守卫：越界路径回落 index.html，绝不外泄仓库文件。
"""

import os

# 模拟模式 + 固定签名密钥（须在导入 api 之前设置）
os.environ.setdefault("RAG_SIMULATE", "true")
os.environ.setdefault("RAG_SESSION_SIGNING_KEY", "test-signing-key")

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from opensearch_pipeline.api import app, _serve_next_file, _NEXT_DIST

client = TestClient(app)

_HAS_BUILD = (_NEXT_DIST / "index.html").is_file()
_needs_build = pytest.mark.skipif(not _HAS_BUILD, reason="next-dist 未构建（先 npm run build）")


# ── 修正#3：作用域隔离，绝不吞 /api ──────────────────────────────

def test_unknown_api_returns_json_404_not_index():
    """不存在的 /api/* → FastAPI 默认 JSON 404，绝不被 SPA 回退成 index.html。"""
    r = client.get("/api/definitely-not-a-real-endpoint")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert "detail" in r.json()
    assert "<!doctype html" not in r.text.lower()
    assert "<html" not in r.text.lower()


def test_unknown_api_subpath_also_json_404():
    """更深的 /api/kb/* 未知子路径同样 JSON 404（catch-all 不匹配 /api 前缀）。"""
    r = client.get("/api/kb/nope/deeper")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_real_api_still_works():
    """既有 API 不受影响。"""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


# ── 修正#9：缓存头 + SPA 回退 ────────────────────────────────────

@_needs_build
def test_console_next_root_serves_index_nocache():
    r = client.get("/console-next/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers.get("cache-control") == "no-cache"


@_needs_build
def test_console_next_spa_fallback_serves_index():
    """前端路由 /console-next/manage 无对应文件 → 回退 index.html（同根文档）。"""
    root = client.get("/console-next/")
    deep = client.get("/console-next/manage")
    assert deep.status_code == 200
    assert "text/html" in deep.headers["content-type"]
    assert deep.headers.get("cache-control") == "no-cache"
    assert deep.text == root.text          # SPA 回退到同一 index.html


@_needs_build
def test_console_next_hashed_asset_immutable():
    asset = next((_NEXT_DIST / "assets").glob("*.js"), None)
    if asset is None:
        pytest.skip("无 assets/*.js")
    r = client.get(f"/console-next/assets/{asset.name}")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "public, max-age=31536000, immutable"


@_needs_build
def test_console_next_no_slash_redirects():
    r = client.get("/console-next", follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308)
    assert r.headers["location"] == "/console-next/"


# ── 路径穿越守卫（直测 helper，避免客户端 URL 归一化）─────────────

@_needs_build
def test_path_traversal_falls_back_to_index():
    """越界 rel 路径绝不外泄仓库文件，回落 index.html。"""
    resp = _serve_next_file("../../api.py")
    # FileResponse.path（PosixPath）指向实际文件；必须是 index.html 而非 api.py
    assert str(getattr(resp, "path", "")).endswith("index.html")
