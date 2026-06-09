# -*- coding: utf-8 -*-
"""
auth_token.py — 轻量级签名会话令牌（仅用 Python 标准库 HMAC-SHA256，无第三方依赖）

钉钉小程序免登后，后端用 issue_session_token() 颁发一个短期签名令牌返回给客户端；
后续 /api/ask、/api/feedback 等请求携带 `Authorization: Bearer <token>`，由
verify_session_token() 校验。令牌内嵌 {uid, dept, name, exp}，**部门由服务端解析后写入令牌，
绝不信任客户端传入的部门**，从而堵住跨部门越权读取 dept_internal 文档的漏洞。

紧凑、自包含格式（精简版 JWT 思路）：
    base64url(json_payload) + "." + base64url(hmac_sha256(payload_b64, key))

签名密钥来自环境变量 RAG_SESSION_SIGNING_KEY：
  - production / staging 下缺失则直接抛错（与 config.py 的生产安全护栏一致）
  - 开发环境缺失则生成进程级临时密钥并告警（重启后旧令牌失效，仅影响本地联调）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 8 * 3600  # 8 小时

# 进程级临时密钥：仅在开发环境且未配置 RAG_SESSION_SIGNING_KEY 时使用
_ephemeral_key: Optional[str] = None


def _get_signing_key() -> bytes:
    key = os.environ.get("RAG_SESSION_SIGNING_KEY", "").strip()
    if key:
        return key.encode("utf-8")

    # 未配置：生产/预发严格报错；开发环境降级为进程级临时密钥
    try:
        from opensearch_pipeline.config import get_config
        env = get_config().environment
    except Exception:
        env = "development"

    if env in ("production", "staging"):
        raise RuntimeError(
            "🚨 [SECURITY] RAG_SESSION_SIGNING_KEY 未配置，无法在 "
            f"'{env}' 环境签发/校验会话令牌。请注入一个高熵随机密钥后再启动服务。"
        )

    global _ephemeral_key
    if _ephemeral_key is None:
        _ephemeral_key = secrets.token_urlsafe(32)
        logger.warning(
            "RAG_SESSION_SIGNING_KEY 未配置，已生成进程级临时签名密钥（仅限开发；"
            "服务重启后已签发的令牌全部失效）。"
        )
    return _ephemeral_key.encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_session_token(
    user_id: str,
    dept: Optional[str] = None,
    name: Optional[str] = None,
    ttl: int = _DEFAULT_TTL_SECONDS,
) -> str:
    """签发会话令牌。部门由服务端解析后写入，客户端不可篡改。"""
    payload = {
        "uid": user_id,
        "dept": dept or "",
        "name": name or "",
        "exp": int(time.time()) + int(ttl),
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(_get_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_session_token(token: str) -> Optional[dict]:
    """校验令牌；有效返回 payload dict，否则返回 None（格式错误 / 签名不符 / 已过期）。"""
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected = hmac.new(_get_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
        actual = _b64url_decode(sig_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected, actual):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None

    if not isinstance(payload, dict) or not payload.get("uid"):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload
