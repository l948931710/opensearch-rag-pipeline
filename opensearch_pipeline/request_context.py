# -*- coding: utf-8 -*-
"""request_context.py — 请求级 correlation id（统一 trace，OBS-trace）。

一个请求入口（API 中间件）生成或透传一个 request_id 并存入 ContextVar；同一请求内的任意代码
（retriever / llm_generator / 各 except 块）经 get_request_id() 拿到【同一个】id，使错误响应、
日志、qa_session_log.error_message 可互相关联（用户上报响应头里的 X-Request-Id，运维即可在日志
里 grep 到整条链路）。

ContextVar 在 FastAPI 的线程池（sync def handler 经 run_in_executor 复制上下文）与同步生成器中
随上下文自动传播，无需改任何函数签名。**故意用纯 ASGI 中间件而非 BaseHTTPMiddleware**：后者把
端点放到另一个 anyio 任务里运行，dispatch 内 set 的 ContextVar 不会传到端点（Starlette 已知坑）；
纯 ASGI 中间件与下游 app 同任务，id 对端点与嵌套调用均可见。

仅标准库，无第三方依赖、无 import 环。"""
from __future__ import annotations

import contextvars
import logging
import uuid

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def new_request_id() -> str:
    """生成 8 位十六进制 id（与历史 trace_id 同形，便于平滑替换）。"""
    return uuid.uuid4().hex[:8]


def set_request_id(value: str) -> str:
    """设置当前上下文 request_id（空/无效 → 生成新的）；返回最终生效值。"""
    rid = (value or "").strip() or new_request_id()
    _request_id.set(rid)
    return rid


def get_request_id() -> str:
    """取当前上下文 request_id；未设置 → '-'。"""
    return _request_id.get()


class RequestIdLogFilter(logging.Filter):
    """把当前 request_id 注入每条日志（record.request_id），供 Formatter 的 %(request_id)s 使用。
    部署侧把本 filter 挂到 handler 并在 format 串加 %(request_id)s，即可让所有日志带 trace。
    （不在此处强制安装全局 logging 配置——避免与 uvicorn 既有 logger 冲突；按需在部署侧接入。）"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = get_request_id()
        return True


class RequestIdMiddleware:
    """纯 ASGI 中间件：入站读 X-Request-Id（跨服务透传，缺失则新生成），存入 ContextVar，
    并在响应头回写 X-Request-Id。挂法：app.add_middleware(RequestIdMiddleware)。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for k, v in scope.get("headers") or []:
            if k == b"x-request-id":
                incoming = v.decode("latin-1", "ignore")
                break
        rid = set_request_id(incoming)

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", rid.encode("latin-1", "ignore")))
            await send(message)

        await self.app(scope, receive, _send)
