# -*- coding: utf-8 -*-
"""
redis_client.py — 统一 Redis 接入（连接池 + 健康探针 + key 前缀）

WS0 状态外置的基建：session / 限流 / 去重 / token 缓存共用一个连接池。
默认不启用——仅当 RAG_REDIS_URL 配置且对应 backend flag 切到 redis 时才连接。

设计约束：
- redis-py 为**惰性 import**：未安装 redis 包时本模块仍可导入（memory 后端不受影响）。
- 单例客户端（连接池内建），线程安全惰性创建。
- 统一 key 前缀 `fl:`，避免与同实例其它用途撞键。
- 可选 TLS：URL 用 rediss:// 走 TLS；RAG_REDIS_TLS_CERT_REQS 控制证书校验级别。
- 健康探针 ping() 供 /api/ready 使用（仅当 RAG_REDIS_URL 配置时参与就绪判定）。

参照 plan WS0-1。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "fl:"

_client: Optional[Any] = None
_client_lock = threading.Lock()


def key(*parts: Any) -> str:
    """构造带统一前缀的 key：key('sess', sid, 'msgs') -> 'fl:sess:{sid}:msgs'。"""
    return KEY_PREFIX + ":".join(str(p) for p in parts)


def is_configured() -> bool:
    """是否配置了 Redis 接入（供 /api/ready 决定是否纳入就绪判定）。"""
    return bool(os.environ.get("RAG_REDIS_URL"))


def get_client() -> Any:
    """返回进程内单例 Redis 客户端（连接池内建）。惰性创建、线程安全。

    未配置 RAG_REDIS_URL → RuntimeError（调用方应在切 redis 后端前保证已配置）。
    未安装 redis 包 → RuntimeError（提示装 redis>=5.0）。
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        url = os.environ.get("RAG_REDIS_URL")
        if not url:
            raise RuntimeError("RAG_REDIS_URL 未配置，无法创建 Redis 客户端")
        try:
            import redis  # 惰性 import：memory 后端不需要装 redis
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError("redis 包未安装（pip install 'redis>=5.0'）") from exc

        kwargs: dict = dict(
            decode_responses=True,
            socket_timeout=float(os.environ.get("RAG_REDIS_SOCKET_TIMEOUT", "3")),
            socket_connect_timeout=float(os.environ.get("RAG_REDIS_CONNECT_TIMEOUT", "3")),
            health_check_interval=30,
        )
        # 可选 TLS 证书校验级别（none/optional/required），仅 rediss:// 生效
        cert_reqs = os.environ.get("RAG_REDIS_TLS_CERT_REQS")
        if cert_reqs and url.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = cert_reqs
        _client = redis.Redis.from_url(url, **kwargs)
        logger.info("Redis 客户端已创建：%s", _redact(url))
        return _client


def ping() -> bool:
    """健康探针：连通且 PING 成功返回 True；任何异常返回 False。"""
    try:
        return bool(get_client().ping())
    except Exception as exc:  # noqa: BLE001 - 探针对所有故障一律判 False
        logger.warning("Redis PING 失败：%s", exc)
        return False


def set_client_for_test(client: Optional[Any]) -> None:
    """测试注入（fakeredis 实例）或复位（传 None）。仅供测试使用。"""
    global _client
    with _client_lock:
        _client = client


def _redact(url: str) -> str:
    """隐去 URL 中的密码，供日志。"""
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
        if parts.password:
            netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:  # pragma: no cover
        pass
    return url
