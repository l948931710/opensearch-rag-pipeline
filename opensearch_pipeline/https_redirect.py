# -*- coding: utf-8 -*-
"""https_redirect.py — CLB 80 明文监听的应用层 HTTPS 强制跳转。

背景：SAE 绑定的公网 CLB 上，443（HTTPS，证书 rag.fulingplastics.com.cn）与 80（HTTP）
两条监听都转发到容器 8000。SAE 的绑定弹窗只能建「明文转发」监听，且 SAE 创建的 CLB
禁止在 CLB 控制台手改（会被自动同步逻辑无预警覆盖），监听级 301 无法配置——所以跳转
只能在应用层做。

判据是 CLB 注入的 X-Forwarded-Proto 请求头（多级代理取第一跳）：
  · 值为 http  → 该请求来自 80 明文监听 → 301/308 跳转到 https:// 同 host 同路径；
  · 值为 https → 来自 443 监听，放行；
  · 头缺失     → 未经代理（本地开发、EIP 直连 :8000、单测 TestClient），放行——
    这保证了迁移期小程序旧版走 EIP 明文直连的行为不变。

豁免路径：/api/health 与 /api/ready 不跳转（CLB/SAE 探活按 200 判定，跳转会把
健康检查打成异常摘除后端）。

方法语义：GET/HEAD 用 301（浏览器手输裸域名的主场景，可被缓存）；其余方法用 308
（保方法保 body，误配成 http:// 的客户端 POST 不至于被降级成 GET）。

开关：RAG_FORCE_HTTPS，默认 true（该中间件只对带 X-Forwarded-Proto: http 的流量
生效，eval/单测/本地均不受影响，故不沿用「默认 OFF」惯例——默认 OFF 一旦忘配
环境变量，80 监听就是敞开的明文服务）。设 false/0/no 可整体停用。

仅标准库，纯 ASGI 中间件（与 RequestIdMiddleware 同款形态，避开 BaseHTTPMiddleware
的 ContextVar 已知坑）。"""
from __future__ import annotations

# 探活豁免：CLB/SAE 健康检查路径，绝不跳转
_EXEMPT_PATHS = ("/api/health", "/api/ready")


class HttpsRedirectMiddleware:
    """纯 ASGI 中间件：X-Forwarded-Proto: http 的请求 301/308 跳转到 https 同址。
    挂法：app.add_middleware(HttpsRedirectMiddleware)（应在最外层，先于业务逻辑）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        proto = ""
        host = ""
        for k, v in scope.get("headers") or []:
            if k == b"x-forwarded-proto" and not proto:
                # 多级代理为逗号分隔链，第一跳才是客户端侧真实协议
                proto = v.decode("latin-1", "ignore").split(",")[0].strip().lower()
            elif k == b"host" and not host:
                host = v.decode("latin-1", "ignore").strip()

        path = scope.get("path") or "/"
        # host 缺失（HTTP/1.0 裸探测等）拼不出跳转目标，放行由后端正常应答
        if proto != "http" or not host or path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # 明文监听在 80，https 默认 443：去掉显式 :80 端口，其余端口原样保留
        if host.endswith(":80"):
            host = host[: -len(":80")]
        location = "https://" + host + path
        query = scope.get("query_string") or b""
        if query:
            location += "?" + query.decode("latin-1", "ignore")

        method = (scope.get("method") or "GET").upper()
        status = 301 if method in ("GET", "HEAD") else 308
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"location", location.encode("latin-1", "ignore")),
                (b"content-length", b"0"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})
