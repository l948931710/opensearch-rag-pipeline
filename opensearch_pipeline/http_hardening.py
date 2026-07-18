# -*- coding: utf-8 -*-
"""HTTPS 硬化中间件（域名接入收口，2026-07-14）。

背景：https://rag.fulingplastics.com.cn 由 CLB 在云侧终结 TLS（80/HTTP + 443/HTTPS 双监听，
后端统一回源 SAE:8000 明文 HTTP），应用自身感知不到请求原始 scheme。本中间件在应用层收口
80 明文面：命中配置域名且明确来自 HTTP 的请求 → 重定向到 https；明确来自 HTTPS 的响应 → 附
HSTS 头。

安全边界（load-bearing，改动前先读）：
  · scheme 只认 CLB 显式注入的 X-Forwarded-Proto 头。**头缺失时一律放行、绝不动作**——
    小程序/旧钉钉卡片回调直连 120.55.69.9:8000 没有该头，不能受影响；CLB 监听未勾选
    「通过X-Forwarded-Proto头字段获取SLB的监听协议」时，80/443 流量在后端不可区分，
    此时若猜 scheme 做跳转会造成 https 侧无限重定向环。
  · 因此生效有三个前提，缺一则本中间件保持惰性（无副作用）：
      1) SAE env 设 RAG_FORCE_HTTPS_HOSTS=rag.fulingplastics.com.cn（默认空 = 整体 off）；
      2) CLB 80 监听高级配置勾选 X-Forwarded-Proto（跳转生效）；
      3) CLB 443 监听同样勾选（HSTS 生效）。
  · 仅按 Host 白名单命中，按 IP/其他域名访问不跳转不加头。
"""

from __future__ import annotations

from typing import Iterable

# HSTS 一年；不带 includeSubDomains/preload——rag.* 子域下无其他站点，保守起步，
# 后续要预加载再显式升级。
_HSTS_VALUE = b"max-age=31536000"


def _header(scope: dict, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1", "ignore")
    return ""


class HttpsRedirectMiddleware:
    """纯 ASGI 中间件（同 RequestIdMiddleware 挂法）：
    Host ∈ hosts 且 X-Forwarded-Proto=http → 301（GET/HEAD）/ 308（其余，保方法保 body 语义）
    跳 https 同路径同 query；X-Forwarded-Proto=https → 响应附 Strict-Transport-Security。"""

    def __init__(self, app, hosts: Iterable[str] = ()):
        self.app = app
        self.hosts = {h.strip().lower() for h in hosts if h and h.strip()}

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.hosts:
            await self.app(scope, receive, send)
            return

        host = _header(scope, b"host").split(":", 1)[0].strip().lower()
        if host not in self.hosts:
            await self.app(scope, receive, send)
            return

        # 多级代理可能追加成 "https, http"——首段是最外层（客户端侧）协议
        proto = _header(scope, b"x-forwarded-proto").split(",", 1)[0].strip().lower()

        if proto == "http":
            path = scope.get("raw_path") or scope.get("path", "/").encode("latin-1", "ignore")
            query = scope.get("query_string") or b""
            location = b"https://" + host.encode("latin-1", "ignore") + path
            if query:
                location += b"?" + query
            status = 301 if scope.get("method", "GET").upper() in ("GET", "HEAD") else 308
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": [(b"location", location), (b"content-length", b"0")],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        if proto == "https":
            async def _send(message):
                if message["type"] == "http.response.start":
                    headers = message.setdefault("headers", [])
                    if not any(k.lower() == b"strict-transport-security" for k, _ in headers):
                        headers.append((b"strict-transport-security", _HSTS_VALUE))
                await send(message)

            await self.app(scope, receive, _send)
            return

        # 头缺失或非法值：无法信任 scheme，放行（直连 IP 的小程序/钉钉回调走这里）
        await self.app(scope, receive, send)


# ── B5（生产级外审 2026-07-17 P2-07）：统一安全响应头 ─────────────────────────
import os as _os   # noqa: E402 — 仅本段使用；顶部无 os 依赖，保持原模块极简


def _security_headers() -> list:
    """每请求惰性计算（读 env，monkeypatch 可测；字符串拼接开销可忽略）。

    设计要点：
    · **frame 控制走「强制 CSP 只含 frame-ancestors」**：console 被钉钉 PC 工作台
      内嵌，X-Frame-Options 表达不了 allowlist（DENY/SAMEORIGIN 都会打死现网入口）；
      而 frame-ancestors 在 Report-Only 头里会被浏览器忽略——所以必须拆两个头：
      强制头只带 frame-ancestors（self+钉钉域），其余策略全走 Report-Only 观察，
      观察期零破坏（G 排查后续再逐条转强制）。
    · RAG_FRAME_ANCESTORS_EXTRA=host1,host2 追加祖先（如未来别的门户内嵌）；
      RAG_SECURITY_HEADERS=false 整组关闭（排障逃生口）。"""
    if _os.environ.get("RAG_SECURITY_HEADERS", "true").strip().lower() in (
            "0", "false", "no", "off"):
        return []
    ancestors = ["'self'", "https://*.dingtalk.com", "https://*.dingtalkapps.com"]
    extra = (_os.environ.get("RAG_FRAME_ANCESTORS_EXTRA") or "").strip()
    for e in extra.split(","):
        e = e.strip()
        if e:
            ancestors.append(e)
    csp_enforced = "frame-ancestors " + " ".join(ancestors)
    csp_report_only = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; connect-src 'self' https:; "
        "font-src 'self' data:")
    return [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        (b"content-security-policy", csp_enforced.encode("latin-1", "ignore")),
        (b"content-security-policy-report-only",
         csp_report_only.encode("latin-1", "ignore")),
    ]


class SecurityHeadersMiddleware:
    """纯 ASGI（同 HttpsRedirectMiddleware 挂法）：给每个 HTTP 响应补安全头；
    端点已显式设置的同名头不覆盖（尊重更具体的策略）。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        pending = _security_headers()
        if not pending:
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {k.lower() for k, _ in headers}
                for k, v in pending:
                    if k not in existing:
                        headers.append((k, v))
            await send(message)

        await self.app(scope, receive, _send)
