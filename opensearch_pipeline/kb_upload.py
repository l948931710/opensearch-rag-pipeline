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
import re
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


# permission_level → 部门后的路径段（dept_internal/restricted 编码;public/None→无段=扁平）。
# 系统设计：可见范围【由路径定】(resolve_permission_level 路径启发式,非 LLM)。自助上传/贡献的扁平
# raw/<dept>/<doc>/... 无段 → 管线 stage-2 默认 public 覆盖回写 → dept_internal/restricted 被静默升公开
# (staging 实测)。故把可见范围编码进路径,让管线解析回登记值。
_PERM_PATH_SEG = {"dept_internal": "internal", "internal": "internal", "restricted": "restricted"}


def _require_clean_seg(name: str, value: str) -> str:
    """key 段硬校验：非空且不含 '/'。含分隔符的段会让后续所有段错位,
    perm_from_raw_key 读错可见范围段——尾斜杠 category_dept 越权发 public 的机制根(2026-07-17 P0)。"""
    v = (value or "").strip()
    if not v or "/" in v:
        raise ValueError(f"raw_key 段非法({name}={value!r})：不得为空或含 '/'")
    return v


def build_raw_key(owner_dept: str, doc_id: str, upload_id: str, filename: str,
                  permission_level: Optional[str] = None) -> str:
    """raw/<owner_dept>[/<perm_seg>]/<doc_id>/<upload_id>/<filename>。

    owner_dept 始终第 2 段（_dept_from_raw_key 依赖）；可见范围段（internal/restricted）是第 3 段,
    不影响部门解析。permission_level 省略/public → 扁平（= 旧行为,向后兼容）。
    各段拒绝空值/内嵌 '/'（调用方须先过 kb_authz.sanitize_owner_dept 等净化）。
    阶段 B：node 文档的第 2 段 = ``node-<dept_id>``（node_storage_segment 构造），
    经 parse_raw_owner 自描述解析；perm 段语义不变（路径启发式照常工作）。
    """
    owner_dept = _require_clean_seg("owner_dept", owner_dept)
    doc_id = _require_clean_seg("doc_id", doc_id)
    upload_id = _require_clean_seg("upload_id", upload_id)
    seg = _PERM_PATH_SEG.get((permission_level or "").strip().lower())
    head = f"{owner_dept}/{seg}" if seg else owner_dept
    return f"raw/{head}/{doc_id}/{upload_id}/{safe_filename(filename)}"


# ── 阶段 B：node 归属的 raw-key 命名空间 ─────────────────────────────────────
# 组码白名单全部 [a-z_]，与 node-<数字> 值域不相交 ⇒ 第 2 段自描述、零歧义。
# 严格正整数规范形（禁前导零/符号/空白）——路径段是安全输入面，宽松解析 = 伪造面。
_NODE_SEG_RE = re.compile(r"^node-([1-9][0-9]{0,18})$")


def node_storage_segment(owner_dept_id: int) -> str:
    """归属节点 → raw-key 第 2 段。非法 id 直接抛（调用方须先过 dept_dim active 校验）。"""
    v = int(owner_dept_id)
    if v <= 1:
        raise ValueError(f"非法归属节点 id：{owner_dept_id!r}（不得 ≤1/根节点）")
    return f"node-{v}"


def parse_raw_owner(raw_key: str) -> dict:
    """raw key → 结构化归属（codex 阶段 B major：**不改 _dept_from_raw_key 的返回语义**——
    图片对象路径等 8 处消费方继续拿第 2 段原文当 storage_segment 用，路径布局不变）。

    返回 {"mode": "legacy"|"node", "storage_segment": <第2段原文>,
          "legacy_owner": <组码或 None>, "owner_dept_id": <int 或 None>}。
    非 raw/ 前缀 → mode='legacy'、全空值（与 _dept_from_raw_key 的 default 腿同语义）。
    """
    seg = ""
    if raw_key and raw_key.startswith("raw/"):
        parts = raw_key.split("/")
        if len(parts) > 1:
            seg = parts[1]
    m = _NODE_SEG_RE.match(seg)
    if m:
        return {"mode": "node", "storage_segment": seg,
                "legacy_owner": None, "owner_dept_id": int(m.group(1))}
    return {"mode": "legacy", "storage_segment": seg,
            "legacy_owner": seg or None, "owner_dept_id": None}


def perm_from_raw_key(raw_key: str) -> str:
    """从已固定的 raw_key 反推可见范围（路径即权威,retry/对账/续跑用）：
    扁平 5 段→public · 第 3 段 internal/dept_internal→dept_internal · restricted→restricted。

    结构畸形（段数不对/空段/未知第 3 段）一律返 restricted——此处曾失效开放 return "public",
    是尾斜杠越权 P0 的机制根；与 retriever/normalize_permission_level 的 fail-closed 约定对齐。"""
    parts = (raw_key or "").split("/")
    if parts[:1] == ["raw"] and len(parts) in (5, 6) and all(p.strip() for p in parts[1:]):
        if len(parts) == 5:
            return "public"
        seg = parts[2].strip().lower()
        if seg in ("internal", "dept_internal"):
            return "dept_internal"
        if seg == "restricted":
            return "restricted"
    return "restricted"


def sign_upload_token(payload: dict, ttl: int = UPLOAD_TOKEN_TTL) -> str:
    """签发 upload token（HMAC，复用 auth_token 的签名密钥）。内嵌后端钦定字段，客户端不可改。"""
    from opensearch_pipeline.auth_token import sign_payload

    body = dict(payload)
    body["typ"] = _UPLOAD_TOKEN_TYP
    return sign_payload(body, ttl=ttl)


def verify_upload_token(token: str) -> Optional[dict]:
    """校验 upload token；有效且 typ 正确 → payload dict，否则 None。"""
    from opensearch_pipeline.auth_token import verify_payload

    # 批次9：expected_typ 下推到验签层强制（本地判别保留为带）
    payload = verify_payload(token, expected_typ=_UPLOAD_TOKEN_TYP)
    if not payload or payload.get("typ") != _UPLOAD_TOKEN_TYP:
        return None
    return payload
