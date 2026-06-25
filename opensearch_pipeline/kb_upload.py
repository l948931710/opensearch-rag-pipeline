# -*- coding: utf-8 -*-
"""
kb_upload.py — 知识库自助上传的纯helper（doc_id/ULID、raw_key、文件名校验、签名 upload token）。

设计：
  - doc_id = "DOC_" + ULID（时间可排序、无碰撞、**与文件名解耦**——不再用文件名 MD5，
    杜绝改名/同名导致的错版）。
  - 上传走"两段式"：upload-url 颁发【后端钦定的 raw_key】+ 短期签名 upload token；客户端直传 OSS；
    register 校验 token（HMAC，客户端不可伪造 raw_key/owner_dept/doc_id）+ OSS-HEAD 实物校验。
  - raw_key = raw/<owner_dept>/<doc_id>/<upload_id>/<filename>。owner_dept 仍是第 2 段
    （_dept_from_raw_key 正确）；**version_no 不进路径**——它在 register 时【事务+行锁】内分配，
    避免并发升版撞号的鸡生蛋问题。
"""

from __future__ import annotations

import os
import time
from typing import Optional, Tuple

# 上传约束
UPLOAD_TOKEN_TTL = 30 * 60          # upload token / 签名 PUT 有效期：30 分钟
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 单文件上限 50MB（可后续配置化）
_UPLOAD_TOKEN_TYP = "kb_upload"

# Phase 1 直传支持的扩展名（office + 图片）。遗留 doc/xls/ppt 单独提示走 Phase 1.5 转换。
# 合法准入集对齐 ingest_policy.INGESTABLE_EXTS（避免注册后以 0-chunk 空文档静默走完生命周期）。
_PHASE1_EXTS = {"pdf", "docx", "xlsx", "pptx", "jpg", "jpeg", "png"}

_EXT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
}

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """26 位 Crockford-base32 ULID（48bit 毫秒时间 + 80bit 随机）。时间前缀 → 可排序。"""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    val = (ts << 80) | rand
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[val & 0x1F])
        val >>= 5
    return "".join(reversed(out))


def new_doc_id() -> str:
    """新文档 ID：DOC_<ULID>。与文件名/部门无关，永不碰撞。"""
    return "DOC_" + new_ulid()


def file_ext(filename: str) -> str:
    """小写扩展名（无点）。无扩展名 → ""。"""
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].strip().lower()


def safe_filename(filename: str) -> str:
    """取 basename 并剥离路径分隔符/控制字符（防目录穿越与 key 注入）。"""
    base = (filename or "").replace("\\", "/").split("/")[-1].strip()
    # 仅保留可见字符；去掉可能破坏 OSS key 的字符（保留中文、字母数字、常见标点）
    cleaned = "".join(c for c in base if c.isprintable() and c not in '"\r\n\t')
    return cleaned[:200] or "upload.bin"


def validate_upload_filename(filename: str) -> Tuple[bool, str, str]:
    """校验上传文件名。返回 (ok, ext, reason)。

    - 合法 Phase1 扩展名 → (True, ext, "ok")
    - 遗留 doc/xls/ppt → (False, ext, "legacy_format")（前端提示：另存为新格式后重传）
    - 其他 → (False, ext, "unsupported_format")
    """
    ext = file_ext(filename)
    if not ext:
        return False, "", "no_extension"
    if ext in {"doc", "xls", "ppt"}:
        return False, ext, "legacy_format"
    if ext not in _PHASE1_EXTS:
        return False, ext, "unsupported_format"
    return True, ext, "ok"


def expected_mime(ext: str) -> str:
    return _EXT_MIME.get((ext or "").lower(), "application/octet-stream")


def build_raw_key(owner_dept: str, doc_id: str, upload_id: str, filename: str) -> str:
    """raw/<owner_dept>/<doc_id>/<upload_id>/<filename>。owner_dept 第 2 段（_dept_from_raw_key 依赖）。"""
    return f"raw/{owner_dept}/{doc_id}/{upload_id}/{safe_filename(filename)}"


def sign_upload_token(payload: dict, ttl: int = UPLOAD_TOKEN_TTL) -> str:
    """签发 upload token（HMAC，复用 auth_token 的签名密钥）。内嵌后端钦定字段，客户端不可改。"""
    from opensearch_pipeline.auth_token import sign_payload

    body = dict(payload)
    body["typ"] = _UPLOAD_TOKEN_TYP
    return sign_payload(body, ttl=ttl)


def verify_upload_token(token: str) -> Optional[dict]:
    """校验 upload token；有效且 typ 正确 → payload dict，否则 None。"""
    from opensearch_pipeline.auth_token import verify_payload

    payload = verify_payload(token)
    if not payload or payload.get("typ") != _UPLOAD_TOKEN_TYP:
        return None
    return payload
