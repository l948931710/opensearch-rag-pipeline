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

# perf I#73：签名密钥 bytes 的进程级缓存——每个带 Bearer 的请求（verify/issue 各调一次）此前
# 都重读 os.environ + str.encode。env 进程内不变，首次成功解析后缓存即可（ephemeral 分支本就
# 缓存 _ephemeral_key，此处对齐 env 分支）。仅缓存【非空 env key】：未配置时仍每次走原有解析
# （生产/预发硬报错、开发 ephemeral），语义零变化。
_signing_key_cache: Optional[bytes] = None
_upload_signing_key_cache: Optional[bytes] = None


def _reset_signing_key_cache() -> None:
    """测试钩子：清空签名密钥缓存（测试中途换 RAG_SESSION_SIGNING_KEY / RAG_UPLOAD_SIGNING_KEY
    时调用；生产无需）。"""
    global _signing_key_cache, _upload_signing_key_cache
    _signing_key_cache = None
    _upload_signing_key_cache = None


def _get_signing_key() -> bytes:
    global _signing_key_cache
    if _signing_key_cache is not None:
        return _signing_key_cache
    key = os.environ.get("RAG_SESSION_SIGNING_KEY", "").strip()
    if key:
        _signing_key_cache = key.encode("utf-8")
        return _signing_key_cache

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


def _get_upload_signing_key() -> bytes:
    """上传令牌（sign_payload/verify_payload）的签名密钥（P2-04）。

    RAG_UPLOAD_SIGNING_KEY 配置则用它，让上传令牌与会话令牌【密钥隔离】——一方泄露不牵连另一方。
    未配置 → **回退会话密钥**（_get_signing_key，与拆分前行为完全一致，向后兼容：既有部署只设了
    RAG_SESSION_SIGNING_KEY 也照常工作；生产/预发缺会话密钥仍由 _get_signing_key 硬报错兜底）。
    仅缓存显式配置的独立上传密钥；回退时直接返回会话密钥（其自身已缓存）。"""
    global _upload_signing_key_cache
    if _upload_signing_key_cache is not None:
        return _upload_signing_key_cache
    key = os.environ.get("RAG_UPLOAD_SIGNING_KEY", "").strip()
    if key:
        _upload_signing_key_cache = key.encode("utf-8")
        return _upload_signing_key_cache
    return _get_signing_key()


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
        "typ": "session",            # 令牌类型判别：堵住 upload token（typ=kb_upload）被当会话令牌复用
        "uid": user_id,
        "acl_groups": groups,        # 权威：权限组数组
        "dept": ",".join(groups),    # 旧·兼容：CSV（单值时与历史标量一致）
        "name": name or "",
        "iat": int(time.time()),     # 签发时刻——撤销杠杆 RAG_SESSION_TOKEN_MIN_IAT 的判据
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
    # 令牌类型判别：拒绝携带【非 session】typ 的令牌（如 sign_payload 颁发的 upload token
    # typ=kb_upload），否则它带 uid+exp+合法签名即可冒充会话令牌建立身份。向后兼容——
    # 旧版会话令牌无 typ 键，在 TTL 抽干窗口内仍放行（payload.get("typ") is None）。
    typ = payload.get("typ")
    if typ is not None and typ != "session":
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    if not _passes_revocation(payload):
        return None
    return payload


def _passes_revocation(payload: dict) -> bool:
    """撤销杠杆（P0-05 报告1「停用用户旧 token 仍持组读到 exp」）。两把都默认不设=零行为变化：

    - ``RAG_SESSION_TOKEN_MIN_IAT``（unix 秒）：iat 早于该时刻的令牌一律拒（含无 iat 的
      旧令牌——设了 epoch 就是要把存量令牌全部作废，全局登出/密钥疑似泄露时的止血阀）；
    - ``RAG_REVOKED_USER_IDS``（CSV）：名单内用户的会话令牌一律拒（离职/停用个体，
      不必等 2h TTL 抽干）。每次校验现读 env——测试/热更新（重启注入）即时生效。
    """
    min_iat = os.environ.get("RAG_SESSION_TOKEN_MIN_IAT", "").strip()
    if min_iat:
        try:
            if int(payload.get("iat") or 0) < int(min_iat):
                return False
        except (TypeError, ValueError):
            return False               # epoch 配错宁可全拒（fail-closed），配置错误必须当场暴露
    revoked = os.environ.get("RAG_REVOKED_USER_IDS", "").strip()
    if revoked:
        ids = {s.strip() for s in revoked.split(",") if s.strip()}
        if str(payload.get("uid") or "") in ids:
            return False
    return True


def sign_payload(payload: dict, ttl: int = _DEFAULT_TTL_SECONDS) -> str:
    """通用签名载荷（HMAC-SHA256）：供 upload token 等短期带签名凭证使用。

    P2-04：用【上传密钥】签名（RAG_UPLOAD_SIGNING_KEY，未配置回退会话密钥），与会话令牌隔离。
    自动写入 `exp`（现在 + ttl）。与 issue_session_token 同紧凑格式，但不要求 uid，
    校验走 verify_payload（只验签名 + exp，不强制 uid，区别于 verify_session_token）。
    """
    body = dict(payload or {})
    body["exp"] = int(time.time()) + int(ttl)
    payload_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(_get_upload_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_payload(token: str, expected_typ: Optional[str] = None) -> Optional[dict]:
    """校验 sign_payload 颁发的载荷；有效返回 dict，否则 None（签名不符 / 格式错 / 过期）。

    P2-04：用【上传密钥】验签（RAG_UPLOAD_SIGNING_KEY，未配置回退会话密钥）——与 sign_payload 同源。

    批次9（ultra P3 auth_token:113）：typ 判别在验签层强制。默认密钥共享（上传密钥未配置
    时回退会话密钥）下，会话令牌与任何 sign_payload 凭证互为合法签名——跨型伪造此前只被
    「调用方记得查 typ」挡住（与 perm_from_raw_key fail-open 同类的调用方依赖）。现在：
    ① typ=session 的载荷一律拒绝（会话令牌绝不能当通用签名凭证消费）；
    ② expected_typ 给定时精确匹配（调用点显式声明预期类型，新调用点忘查也不开洞）。"""
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected = hmac.new(_get_upload_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
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
    typ = payload.get("typ")
    if typ == "session":
        return None
    if expected_typ is not None and typ != expected_typ:
        return None
    return payload
