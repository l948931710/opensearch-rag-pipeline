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
    """是否配置了 Redis 接入（供 /api/ready 决定是否纳入就绪判定）。
    B2（复核批次5）：Sentinel 模式无需 RAG_REDIS_URL（经哨兵发现 master），一并视为已配置。"""
    return bool(os.environ.get("RAG_REDIS_URL")
                or os.environ.get("RAG_REDIS_SENTINEL_NODES"))


def require_redis() -> bool:
    """B1（复核批次5）：多实例部署契约旗标。RAG_REQUIRE_REDIS=true 时，限流/会话的
    redis 后端**初始化失败必须拒绝起服**（raise），绝不静默回退 memory——多实例下
    memory 回退=各实例独立计数/独立会话，全局熔断实际放大 N 倍且 /ready 仍绿（假绿）。
    多实例部署清单三件同出：RAG_RATE_LIMIT_BACKEND=redis + RAG_SESSION_BACKEND=redis +
    RAG_REQUIRE_REDIS=true。"""
    return os.environ.get("RAG_REQUIRE_REDIS", "").strip().lower() in ("1", "true", "yes", "on")


def _parse_nodes(raw: str) -> list:
    """'h1:26379,h2:26379' → [('h1', 26379), ('h2', 26379)]（缺端口默认 26379）。"""
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        host, _, port = part.rpartition(":")
        out.append((host or part, int(port) if port.isdigit() else 26379))
    return out


def get_client() -> Any:
    """返回进程内单例 Redis 客户端（连接池内建）。惰性创建、线程安全。

    接入形态（B2 复核批次5，按优先级）：
    1. **Sentinel**：RAG_REDIS_SENTINEL_NODES="h1:26379,h2:26379"（+ 可选
       RAG_REDIS_SENTINEL_MASTER 默认 mymaster；RAG_REDIS_PASSWORD / RAG_REDIS_DB）
       —— redis-py 原生 Sentinel.master_for，failover 自动跟随新 master；
    2. **Cluster**：RAG_REDIS_CLUSTER=true + RAG_REDIS_URL —— RedisCluster.from_url
       （cluster 无 db 概念，URL 勿带 /n）；
    3. **单机**（默认）：RAG_REDIS_URL。
    未配置 → RuntimeError（调用方应在切 redis 后端前保证已配置）。
    未安装 redis 包 → RuntimeError（提示装 redis>=5.0）。
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        url = os.environ.get("RAG_REDIS_URL")
        sentinel_nodes = os.environ.get("RAG_REDIS_SENTINEL_NODES", "").strip()
        if not url and not sentinel_nodes:
            raise RuntimeError(
                "RAG_REDIS_URL / RAG_REDIS_SENTINEL_NODES 均未配置，无法创建 Redis 客户端")
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
        if cert_reqs and (url or "").startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = cert_reqs

        if sentinel_nodes:
            from redis.sentinel import Sentinel
            master = os.environ.get("RAG_REDIS_SENTINEL_MASTER", "").strip() or "mymaster"
            password = os.environ.get("RAG_REDIS_PASSWORD") or None
            db = int(os.environ.get("RAG_REDIS_DB", "0") or 0)
            sentinel = Sentinel(_parse_nodes(sentinel_nodes),
                                sentinel_kwargs=({"password": password} if password else None))
            _client = sentinel.master_for(master, password=password, db=db, **kwargs)
            logger.info("Redis 客户端已创建（sentinel master=%s nodes=%s）", master, sentinel_nodes)
        elif os.environ.get("RAG_REDIS_CLUSTER", "").strip().lower() in ("1", "true", "yes", "on"):
            from redis.cluster import RedisCluster
            _client = RedisCluster.from_url(url, **kwargs)
            logger.info("Redis 客户端已创建（cluster）：%s", _redact(url))
        else:
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
