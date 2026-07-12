# -*- coding: utf-8 -*-
"""
sanitize.py — agent 持久化前的参数脱敏（深度审查 2026-07-09 治理组）

022 DDL 对 tool_invocation.args_json 的契约是「脱敏后入 JSON，原文只留 digest」，但实现
一直存原文；agent_step 审批 payload、approval_request.proposed_args_json 同理。本模块是
这三个写入点共享的脱敏层：深拷贝遍历 dict/list，对每个字符串值过 `pii_patterns.scrub_image_text`
（身份证/手机号/邮箱/银行卡/密钥全表就地掩码，自带 FP 护栏、幂等）。

逐值脱敏而非整串 JSON 脱敏：保证输出恒为合法 JSON（正则替换绝不可能破坏结构）。
失败方向 fail-closed：脱敏本身异常时**绝不回退存原文**，落占位符 + digest 可回放。

⚠️ checkpoint state_blob 刻意不在此脱敏——resume 需要 messages 原文往返，掩码会破坏续跑。
checkpoint 的治理走 retention 短留存 + purge_subject 主体擦除（retention.py agent 组）。
"""
from __future__ import annotations

import json
from typing import Any, Dict


def _scrub_value(v: Any) -> Any:
    if isinstance(v, str):
        from opensearch_pipeline.pii_patterns import scrub_image_text
        return scrub_image_text(v)
    if isinstance(v, dict):
        return {k: _scrub_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub_value(x) for x in v]
    return v


def sanitize_args(args: Any) -> Dict[str, Any]:
    """脱敏一份工具参数（dict）供持久化。异常 → 占位符（绝不存原文）。"""
    try:
        out = _scrub_value(args if isinstance(args, dict) else {"_raw": args})
        return out if isinstance(out, dict) else {"_raw": out}
    except Exception as e:   # noqa: BLE001 — fail-closed：脱敏失败宁存占位不存原文
        return {"_sanitize_error": str(e)[:200]}


def sanitize_args_json(args: Any) -> str:
    """脱敏 + JSON 序列化（tool_invocation.args_json / approval_request.proposed_args_json）。"""
    return json.dumps(sanitize_args(args), ensure_ascii=False)
