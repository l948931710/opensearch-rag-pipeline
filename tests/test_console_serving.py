# -*- coding: utf-8 -*-
"""
test_console_serving.py — 控制台托管测试（P7 切换后）

布局：
  · /console            = 新 Vite SPA（默认入口）；无尾斜杠 → 307 到 /console/（保留 query）
  · /console/{path}     = SPA 静态 + 作用域回退（构建 base /console/）
  · /console-legacy     = 旧·H5 控制台（保留至 P8）
  · /console-next[/...] = 307 → /console/...（并行阶段路径向后兼容，保留 query）

收口约束：
  · 修正#3：回退仅作用于 /console*，不存在的 /api/* 返回 JSON 404 而非 index.html。
  · 修正#9：hash 资源 immutable，index.html / SPA 回退 no-cache。
  · 小程序兼容：/console?token=&doc_id=... 重定向【保留 query】，深链不丢。
"""

import os

os.environ.setdefault("RAG_SIMULATE", "true")
os.environ.setdefault("RAG_SESSION_SIGNING_KEY", "test-signing-key")

import pytest
from starlette.testclient import TestClient

from opensearch_pipeline.api import app, _serve_console_spa, _NEXT_DIST

client = TestClient(app)

_HAS_BUILD = (_NEXT_DIST / "index.html").is_file()
_needs_build = pytest.mark.skipif(not _HAS_BUILD, reason="next-dist 未构建（先 npm run build）")


# ── 修正#3：作用域隔离，绝不吞 /api ──────────────────────────────

def test_unknown_api_returns_json_404_not_index():
    r = client.get("/api/definitely-not-a-real-endpoint")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert "detail" in r.json()
    assert "<html" not in r.text.lower()


def test_unknown_api_subpath_also_json_404():
    r = client.get("/api/kb/nope/deeper")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_real_api_still_works():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


# ── /console 切换：默认入口 = SPA，无尾斜杠 + 小程序深链 query 保留 ──

def test_console_no_slash_redirects_preserving_query():
    """小程序 /console?token=&doc_id=... → 307 到 /console/?...（query 不可丢）。"""
    r = client.get("/console", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/console/"

    r2 = client.get("/console?token=ABC&doc_id=DOC_9&owner=hr", follow_redirects=False)
    assert r2.status_code == 307
    assert r2.headers["location"] == "/console/?token=ABC&doc_id=DOC_9&owner=hr"


@_needs_build
def test_console_root_serves_index_nocache():
    r = client.get("/console/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers.get("cache-control") == "no-cache"


@_needs_build
def test_console_spa_fallback_serves_index():
    root = client.get("/console/")
    deep = client.get("/console/manage")
    assert deep.status_code == 200
    assert deep.headers.get("cache-control") == "no-cache"
    assert deep.text == root.text          # SPA 回退到同一 index.html


@_needs_build
def test_console_hashed_asset_immutable():
    asset = next((_NEXT_DIST / "assets").glob("*.js"), None)
    if asset is None:
        pytest.skip("无 assets/*.js")
    r = client.get(f"/console/assets/{asset.name}")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "public, max-age=31536000, immutable"


# ── 旧 legacy 保留 ──────────────────────────────────────────────

def test_console_legacy_still_served():
    r = client.get("/console-legacy")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ── /console-next 并行路径 → 重定向（保留子路径 + query）───────────

def test_console_next_redirects_to_console():
    r = client.get("/console-next/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/console/"

    r2 = client.get("/console-next/manage?token=T", follow_redirects=False)
    assert r2.status_code == 307
    assert r2.headers["location"] == "/console/manage?token=T"


# ── 路径穿越守卫 ─────────────────────────────────────────────────

@_needs_build
def test_path_traversal_falls_back_to_index():
    resp = _serve_console_spa("../../api.py")
    assert str(getattr(resp, "path", "")).endswith("index.html")
