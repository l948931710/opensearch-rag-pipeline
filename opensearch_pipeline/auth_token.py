# -*- coding: utf-8 -*-
"""
auth_token.py — 轻量级签名会话令牌（仅用 Python 标准库 HMAC-SHA256，无第三方依赖）

钉钉小程序免登后，后端用 issue_session_token() 颁发一个短期签名令牌返回给客户端；
后续 /api/ask、/api/feedback 等请求携带 `Authorization: Bearer <token>`，由
verify_session_token() 校验。令牌内嵌 {uid, acl_groups, dept, name, exp}，**ACL 权限组由服务端
解析后写入令牌，绝不信任客户端传入的部门**，从而堵住跨部门越权读取 dept_internal 文档的漏洞。

- `acl_groups`（权威）：用户所属 ACL 权限组数组，如 ["marketing","production"]。
- `dept`（旧·兼容）：同一组列表的 CSV，保留给旧消费端；新读取应优先 acl_groups。
  注意承载的是"权限组"而非组织部门——见 dingtalk_identity._DEPT_NAME_TO_GROUPS。

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
from typing import List, Optional, Union

logger = logging.getLogger(__name__)

def _default_session_ttl_seconds() -> int:
    """会话令牌默认 TTL（秒）。RAG_SESSION_TOKEN_TTL_HOURS 配置，默认 2h（原 8h）——缩短读组
    撤销窗口；current_identity 现已读时实时重查 acl，TTL 仅作兜底上界。下限 5 分钟（防误配 0）。"""
    try:
        hours = float(os.environ.get("RAG_SESSION_TOKEN_TTL_HOURS", "2"))
    except (TypeError, ValueError):
        hours = 2.0
    return max(300, int(hours * 3600))


_DEFAULT_TTL_SECONDS = _default_session_ttl_seconds()  # import 期解析；issue_session_token 调用期重解析

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


def _coerce_acl_groups(dept: Union[str, List[str], None]) -> List[str]:
    """把 dept 入参（单串 / CSV / 列表）归一为干净去重的 ACL 组列表。

    不做合法组白名单——白名单在检索安全边界 retriever._normalize_acl_groups 强制；
    此处只保证存进 token 的格式干净。放在本模块内（而非 import retriever）以保持
    auth_token 的零第三方依赖与无 import 环。
    """
    if not dept:
        return []
    items = dept.split(",") if isinstance(dept, str) else dept
    out: List[str] = []
    seen = set()
    for d in items:
        s = (d or "").strip() if isinstance(d, str) else str(d).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def issue_session_token(
    user_id: str,
    dept: Union[str, List[str], None] = None,
    name: Optional[str] = None,
    role: Optional[str] = None,
    ttl: Optional[int] = None,
) -> str:
    """签发会话令牌。ACL 权限组由服务端解析后写入，客户端不可篡改。

    `dept` 入参接受单组字符串、CSV 或组列表（历史参数名，承载的是 ACL 组）。
    令牌同时写权威 `acl_groups`（数组）与旧 `dept`（CSV，向后兼容）。

    `role`（知识库写授权角色，可选）：employee / dept_admin / kb_admin。仅作【入口可见性 UI 提示】，
    **非授权边界**——每个特权写接口必须用 DB 现查的 role + dept_admin_grant 重新裁决，
    以便撤销管理员后即时生效（不等令牌过期）。缺省/未知不写该键（消费端按 employee 兜底）。
    """
    if ttl is None:
        ttl = _default_session_ttl_seconds()   # 调用期重解析，随 RAG_SESSION_TOKEN_TTL_HOURS
    groups = _coerce_acl_groups(dept)
    payload = {
        "uid": user_id,
        "acl_groups": groups,        # 权威：权限组数组
        "dept": ",".join(groups),    # 旧·兼容：CSV（单值时与历史标量一致）
        "name": name or "",
        "exp": int(time.time()) + int(ttl),
    }
    if role and isinstance(role, str) and role.strip():
        payload["role"] = role.strip()
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


def sign_payload(payload: dict, ttl: int = _DEFAULT_TTL_SECONDS) -> str:
    """通用签名载荷（HMAC-SHA256，复用会话密钥）：供 upload token 等短期带签名凭证使用。

    自动写入 `exp`（现在 + ttl）。与 issue_session_token 同密钥、同紧凑格式，但不要求 uid，
    校验走 verify_payload（只验签名 + exp，不强制 uid，区别于 verify_session_token）。
    """
    body = dict(payload or {})
    body["exp"] = int(time.time()) + int(ttl)
    payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(_get_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_payload(token: str) -> Optional[dict]:
    """校验 sign_payload 颁发的载荷；有效返回 dict，否则 None（签名不符 / 格式错 / 过期）。"""
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
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload
