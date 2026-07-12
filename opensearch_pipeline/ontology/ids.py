# -*- coding: utf-8 -*-
"""
ids.py — 本体标识：ULID 铸造 + canonical_ref 展示号。

canonical 主键由本体铸造（ULID，26 位 Crockford base32），**永不等于任何源系统 ID**——
会分裂的 U8 货号绝不抬为主键，所有源系统编号一律作别名（ontology_identifier）。
人读展示号 canonical_ref = FLP-<类型码>-<序号>（发号走 ontology_ref_seq 原子取号，见 store.py）。

纯 stdlib 零新依赖（不为 26 个字符引第三方包）；48-bit 毫秒时间戳 + 80-bit 随机，
时间有序（跨毫秒）+ 碰撞概率可忽略。
"""
from __future__ import annotations

import os
import time

# Crockford base32：无 I/L/O/U，防人读混淆
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_CROCKFORD)}

ULID_LEN = 26
_REF_PREFIX = "FLP"


def new_ulid() -> str:
    """铸一个 ULID：26 字符 Crockford base32（130 位容量装 128 位值，首字符 0–7）。"""
    ts_ms = time.time_ns() // 1_000_000
    if ts_ms >= 1 << 48:  # pragma: no cover — 公元 10889 年
        raise OverflowError("ULID 时间戳溢出 48 位")
    value = (ts_ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> shift) & 0x1F] for shift in range(125, -1, -5))


def ulid_timestamp_ms(ulid: str) -> int:
    """解出 ULID 的毫秒时间戳（审计/排障用；也是编码正确性的测试锚点）。"""
    if len(ulid) != ULID_LEN:
        raise ValueError(f"ULID 长度应为 {ULID_LEN}，得到 {len(ulid)}")
    value = 0
    for ch in ulid:
        try:
            value = (value << 5) | _DECODE[ch]
        except KeyError:
            raise ValueError(f"非法 ULID 字符 {ch!r}（Crockford base32 无 I/L/O/U）")
    return value >> 80


def format_ref(type_code: str, seq_no: int) -> str:
    """展示号：FLP-<类型码>-<序号>（6 位零填充，超出自然增长；序号从 1 起）。"""
    if not type_code or not type_code.isascii() or not type_code.isalnum():
        raise ValueError(f"非法类型码 {type_code!r}（ASCII 字母数字）")
    if seq_no <= 0:
        raise ValueError(f"序号须为正整数，得到 {seq_no}")
    return f"{_REF_PREFIX}-{type_code.upper()}-{seq_no:06d}"
