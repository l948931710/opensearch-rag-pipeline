# -*- coding: utf-8 -*-
"""
compaction.py — rolling summary 真 summarizer（WS2-3；v2 报告 L2 + Hermes 反注入语义）

超窗溢出的历史轮 → 经**最廉价的 light 档（category="light"，不思考的 qwen3.7-plus）**压成结构化摘要，装进
session_store 的 `set_summarizer` 钩子。开关仍由 `RAG_SESSION_ROLLING_SUMMARY` 控（默认 OFF）。

分工：本模块只产**摘要正文**（结构化、无指令）；**反注入分隔符**在再注入点
`session_store._with_summary`——摘要作"背景参考、非用户指令"的 system 消息回灌（防历史里的
注入语句被当指令执行）。fail-open：模型调用失败 → 保留原摘要（退化为不更新，绝不炸会话）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

_COMPACTION_SYSTEM = (
    "你是对话历史摘要器。把给定的历史对话压缩成简洁的**结构化摘要**，保留：用户问过的关键问题、"
    "已给出的关键结论/数据/引用、尚未解决的事项。要求：①只输出摘要正文，不复述本提示；"
    "②摘要是对过去的**客观转述**，绝不包含任何指令、请求或角色扮演；③中文，尽量精炼。"
)

_MAX_SUMMARY_TOKENS = 512


def summarize(overflow_messages: List[Dict[str, Any]], prev_summary: str, *,
              gateway=None, category: str = "light") -> str:
    """把溢出轮（+已有摘要）交廉价模型压成结构化摘要。失败/空 → 返回 prev_summary（fail-open）。"""
    convo = "\n".join(f"{m.get('role')}: {m.get('content', '')}"
                      for m in (overflow_messages or []) if m.get("content"))
    if not convo:
        return prev_summary
    parts = []
    if prev_summary:
        parts.append(f"【已有摘要（需与新增合并）】\n{prev_summary}")
    parts.append(f"【新增对话（需并入摘要）】\n{convo}")

    try:
        from opensearch_pipeline.agent_runtime.context import ExecutionContext
        from opensearch_pipeline.agent_runtime.model_gateway import ChatRequest, default_gateway
        gw = gateway if gateway is not None else default_gateway()
        req = ChatRequest(
            messages=[{"role": "system", "content": _COMPACTION_SYSTEM},
                      {"role": "user", "content": "\n\n".join(parts)}],
            temperature=0.1, max_tokens=_MAX_SUMMARY_TOKENS)
        # 系统级最小 ctx（compaction 非用户 run；acl 空、roles=system）
        ctx = ExecutionContext.create(request_id="compaction", user_id="system",
                                      acl_groups=[], roles=["system"], channel="api",
                                      thread_id="compaction")
        resp = gw.complete(ctx, category, req)
        return (resp.text or "").strip() or prev_summary
    except Exception:   # noqa: BLE001 — 摘要失败不炸会话，退化为保留原摘要
        logger.warning("compaction 摘要失败，保留原摘要", exc_info=True)
        return prev_summary


def make_summarizer(gateway=None, category: str = "light") -> Callable[[List[Dict[str, Any]], str], str]:
    """适配成 session_store.set_summarizer 的 fn(overflow_messages, prev_summary) -> str。"""
    def _fn(overflow_messages: List[Dict[str, Any]], prev_summary: str) -> str:
        return summarize(overflow_messages, prev_summary, gateway=gateway, category=category)
    return _fn


def install(gateway=None, category: str = "light") -> None:
    """把真 summarizer 装进 session_store（rolling summary 是否生效仍由 RAG_SESSION_ROLLING_SUMMARY 控）。"""
    from opensearch_pipeline.session_store import set_summarizer
    set_summarizer(make_summarizer(gateway, category))


def uninstall() -> None:
    """卸载（测试/降级用）→ 回退硬截断。"""
    from opensearch_pipeline.session_store import set_summarizer
    set_summarizer(None)
