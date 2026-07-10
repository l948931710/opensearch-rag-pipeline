# -*- coding: utf-8 -*-
"""test_https_redirect.py — CLB 80 明文监听应用层跳 HTTPS（https_redirect.py）。

TestClient 不带 X-Forwarded-Proto（等价本地/EIP 直连），既有测试全部不受
中间件影响；本文件专测带头场景的跳转语义。"""

import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import opensearch_pipeline.api as api
    return TestClient(api.app)


def test_no_header_no_redirect(client):
    """无 X-Forwarded-Proto（本地开发 / EIP 直连 :8000）→ 正常应答不跳转。"""
    r = client.get("/api/health")
    assert r.status_code == 200


def test_https_proto_no_redirect(client):
    """来自 443 监听（X-Forwarded-Proto: https）→ 放行。"""
    r = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200


def test_http_proto_get_301_preserves_host_path_query(client):
    """来自 80 明文监听的 GET → 301，Location 保 host/路径/查询串。"""
    r = client.get("/api/version?x=1&y=%E4%B8%AD",
                   headers={"X-Forwarded-Proto": "http"},
                   follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://testserver/api/version?x=1&y=%E4%B8%AD"
    # 跳转响应由外层 RequestIdMiddleware 回写 correlation id（挂载顺序回归锚点）
    assert r.headers.get("x-request-id")


def test_http_proto_post_308_keeps_method(client):
    """非 GET/HEAD 用 308（保方法保 body），避免误配 http:// 的客户端 POST 变 GET。"""
    r = client.post("/api/ask", json={"question": "q"},
                    headers={"X-Forwarded-Proto": "http"},
                    follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "https://testserver/api/ask"


def test_explicit_port_80_stripped(client):
    """Host 带显式 :80 → 目标 https 不应残留 80 端口。"""
    r = client.get("/api/version",
                   headers={"X-Forwarded-Proto": "http", "Host": "rag.example.com:80"},
                   follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://rag.example.com/api/version"


def test_multi_hop_proto_uses_first(client):
    """多级代理逗号链取第一跳：'http, https' 视为明文入站 → 跳转。"""
    r = client.get("/api/version",
                   headers={"X-Forwarded-Proto": "http, https"},
                   follow_redirects=False)
    assert r.status_code == 301


def test_probe_paths_exempt(client):
    """探活路径豁免：CLB/SAE 健康检查打在 80 上也绝不能收到 3xx 被判异常。"""
    for path in ("/api/health", "/api/ready"):
        r = client.get(path, headers={"X-Forwarded-Proto": "http"},
                       follow_redirects=False)
        assert r.status_code not in (301, 308), path
