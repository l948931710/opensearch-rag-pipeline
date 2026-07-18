# -*- coding: utf-8 -*-
"""query_rewriter.py — 多轮追问的检索前改写（RAG_FOLLOWUP_REWRITE，默认关）。

问题背景：检索 / 意图路由 / 落库三处都只用当前轮原始 query，历史只注入生成端 LLM。
「那第二步呢」「然后呢」这类指代性追问单看检索必然空手 → 沉淀进知识缺口队列的也是
无上下文、无法认领的短问题。本模块在检索前把这类追问改写成独立完整问题：

  1. **启发式门控 looks_followup**（零成本，宁可漏触发不误触发）：必须命中指代词/
     接续词/序数指代（词表单一来源 = contribution.REFERENTIAL_TOKENS）或「短且缺
     主体」（contribution.is_incomplete_question）；**长度只作辅助上限**——仅
     "history 非空且问题短" 绝不触发，「怎么请假」「如何报销」这类独立短问题不调 LLM。
  2. **LLM 改写**（仅门控命中后调用）：小模型 temperature=0 把追问改写为自包含问题；
     "已独立则原样返回"。输出先做确定性 cleanup（剥模型前缀/引号）再做护栏校验。

失败语义（graceful degradation）：无 key / 超时 / 异常 / 输出不合格 → 返回 None，
调用方回退原始 query，绝不影响回答主流程。

历史裁剪纪律：build_rewrite_history 只取最近 ≤3 轮的 user 消息与 assistant 最终可见
回答（role 白名单），**绝不包含** system prompt / tool trace / retrieval chunks /
隐藏消息——改写模型只看用户实际看到的对话。

测试 patch 接缝：本模块 `_http_post`（http_session 惯例，勿 patch requests.post）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

from opensearch_pipeline.http_session import http_post as _http_post

logger = logging.getLogger(__name__)

_MAX_HISTORY_TURNS = 3          # 改写只看最近 3 轮（追问指代极少跨更远）
_MAX_CONTENT_CHARS = 500        # 单条历史消息截断（防超长回答撑爆小模型输入）
_MAX_REWRITE_CHARS = 200        # 改写输出上限（超长 = 模型跑偏，弃用）

_SYSTEM_PROMPT = (
    "你是企业知识库检索系统的查询改写器。用户在多轮对话中提出了一个依赖上文的追问，"
    "请结合对话历史把它改写成一个不依赖上文、语义完整的独立问题：补全省略的主体与对象、"
    "消解“那/这/它/上面/第X步”等指代，保留原追问的关键词与意图，不要扩写或回答。\n"
    "若当前问题本身已经独立完整，原样输出它。\n"
    "只输出改写后的问题本身，不要任何前缀、引号或解释。"
)

# 模型常见口癖前缀（确定性 cleanup；顺序无关，循环剥到不再变化）
_PREFIX_RES = [
    re.compile(r"^(改写后的问题|改写后|完整问题|独立问题|用户想问的是|改写|问题)\s*[:：]\s*"),
    re.compile(r"^[「『\"'“]"),
]
_SUFFIX_RES = [re.compile(r"[」』\"'”]$")]


def rewrite_enabled() -> bool:
    """总闸 RAG_FOLLOWUP_REWRITE（默认关 —— 关闭时整条链路零行为变化）。"""
    return os.environ.get("RAG_FOLLOWUP_REWRITE", "").strip().lower() in ("1", "true", "yes", "on")


def _rewrite_timeout() -> float:
    try:
        v = float(os.environ.get("RAG_FOLLOWUP_REWRITE_TIMEOUT", "4"))
    except ValueError:
        return 4.0
    return v if 0.5 <= v <= 30 else 4.0


def _rewrite_model() -> str:
    return (os.environ.get("RAG_FOLLOWUP_REWRITE_MODEL") or "qwen-flash").strip() or "qwen-flash"


def looks_followup(query: str, history: Optional[List[Dict[str, Any]]]) -> bool:
    """启发式门控：history 非空 且（命中指代/接续词表【且不超长】或 短且缺主体）。

    词表与「短缺主体」判定都来自 contribution（单一来源）；长度只作辅助——
    命中词表但归一化超过 2×RAG_FOLLOWUP_MAX_LEN 视为内容自足，不触发。
    """
    if not query or not history:
        return False
    from opensearch_pipeline.contribution import (
        _incomplete_max_len, has_referential_token, is_incomplete_question,
        is_junk_question, normalize_question)
    if is_junk_question(query):
        return False               # 确定性垃圾不值得一次 LLM 调用
    if is_incomplete_question(query):
        return True
    if has_referential_token(query):
        return len(normalize_question(query)) <= _incomplete_max_len() * 2
    return False


def build_rewrite_history(merged_history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    """给改写模型的历史切片：最近 ≤3 轮的 user/assistant 消息（role 白名单），
    assistant 内容经 history_answer_text 剥内部图片标记。system/tool 等一律丢弃。"""
    out: List[Dict[str, str]] = []
    for m in (merged_history or []):
        role = str((m or {}).get("role") or "").strip()
        content = str((m or {}).get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if role == "assistant":
            try:
                from opensearch_pipeline.answer_flow import history_answer_text
                content = history_answer_text(content)
            except Exception:   # noqa: BLE001 — 剥标记失败原样用（仅影响提示质量）
                pass
        out.append({"role": role, "content": content[:_MAX_CONTENT_CHARS]})
    return out[-_MAX_HISTORY_TURNS * 2:]


def _cleanup_output(text: str, original: str) -> Optional[str]:
    """确定性 cleanup + 护栏：剥前缀/引号 → 非空、≤200 字、与原文相同=未改写（None）。"""
    s = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    changed = True
    while changed and s:
        changed = False
        for rex in _PREFIX_RES + _SUFFIX_RES:
            ns = rex.sub("", s).strip()
            if ns != s:
                s, changed = ns, True
    if not s or len(s) > _MAX_REWRITE_CHARS:
        return None
    from opensearch_pipeline.contribution import normalize_question
    if normalize_question(s) == normalize_question(original):
        return None   # 模型认为已独立 → 视为未改写
    return s


def rewrite_followup(query: str, merged_history: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """把追问改写成独立问题；任何失败返回 None（调用方回退原 query）。

    调用方负责 flag 与 looks_followup 门控——本函数只做改写本身（便于单测）。
    """
    from opensearch_pipeline.config import get_config
    llm = get_config().llm
    if not llm.api_key:
        return None
    hist = build_rewrite_history(merged_history)
    if not hist:
        return None
    url = f"{llm.api_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": _rewrite_model(),
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT}]
                    + hist
                    + [{"role": "user", "content": f"当前追问：{query}"}],
        "max_tokens": 120,
        "temperature": 0,
        "stream": False,
        "enable_thinking": False,   # 小改写任务，思考只添延迟
    }
    try:
        resp = _http_post(
            url, json=payload,
            headers={"Authorization": f"Bearer {llm.api_key}",
                     "Content-Type": "application/json"},
            timeout=_rewrite_timeout())
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:   # noqa: BLE001 — 改写失败恒回退原问题（fail-open）
        logger.warning("追问改写调用失败（回退原问题）: %s", e)
        return None
    out = _cleanup_output(content, query)
    if out:
        logger.info("追问改写: %r → %r", query[:40], out[:60])
    return out


def maybe_rewrite(query: str, merged_history: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """flag + 门控 + 改写的一站式入口；返回改写后的问题或 None（不改写）。"""
    if not rewrite_enabled():
        return None
    if not looks_followup(query, merged_history):
        return None
    return rewrite_followup(query, merged_history)
