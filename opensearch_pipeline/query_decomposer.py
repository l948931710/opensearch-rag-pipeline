# -*- coding: utf-8 -*-
"""query_decomposer.py — 多意图查询分解（multi-doc retrieval）。

跨文档综合问题（如"新员工从入职到入住宿舍需要经过哪些流程？"）在单查询检索下
R@1 仅 ~8%（tests/eval/topk_window_sweep_report.md；251 题 gold 的跨文档子集同样
确认）：top-k 被单一最相似文档占满，第二个目标文档挤不进上下文。

两级设计（遵循本仓库 graceful-degradation 约定，任何失败都回退单查询路径）：
  1. **启发式触发**（零成本，宁可多触发）：连词并列 / 对比 / "从…到…" / 多问号
     等多意图信号。触发的代价只是一次小 LLM 调用，由 LLM 做精确判别。
  2. **LLM 分解**（仅触发后调用）：Qwen 判别是否真需多路检索，并拆成 2~N 个
     自包含子查询；严格 JSON 数组输出，temperature=0，超时/解析失败 → 不分解。

模式（config.rag.multi_query_mode，RAG_MULTI_QUERY_MODE）：
  off（默认） → 不分解；auto → 启发式触发后才调 LLM；llm → 每个查询都判别（评测对照用）。
"""

import json
import logging
import re
from typing import List

import requests

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)

# 多意图信号（启发式，召回优先）：
#   并列连词 / 对比 / 范围跨越（从…到…）/ 并列顿号 / 多个问号
_MULTI_INTENT_PATTERNS = [
    re.compile(r"[和与跟]|以及|还有|或者|及其?"),
    re.compile(r"区别|对比|比较|不同|分别|各自"),
    re.compile(r"从.+到"),
    re.compile(r"、"),
    re.compile(r"[？?].*[？?]"),
]

_DECOMPOSE_SYSTEM = """你是企业知识库检索系统的查询分析器。判断用户问题是否需要分别检索多个不同主题/文档才能完整回答（例如：同时问多个流程或制度、对比两类事物、覆盖多个环节的综合问题）。
- 若需要：把它拆成 {max_sub} 个以内的独立子查询，每个子查询自包含（补全主语、不用代词、保留原问题关键词），分别对应一个待检索主题。
- 若单一主题即可回答：输出 []
只输出 JSON 数组（如 ["员工入职办理流程", "新员工宿舍入住安排"]），不要任何解释。"""


def looks_multi_intent(query: str) -> bool:
    """启发式多意图信号：召回优先（误触发的代价只是一次小 LLM 判别调用）。"""
    if len(query) < 8:  # 过短查询拆不出两个自包含主题
        return False
    return any(p.search(query) for p in _MULTI_INTENT_PATTERNS)


def _parse_subqueries(content: str, *, max_sub: int, original: str) -> List[str]:
    """从 LLM 输出中解析子查询数组；非法/不足 2 条 → []。"""
    m = re.search(r"\[.*\]", content or "", re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    subs, seen = [], set()
    for item in arr:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or s == original.strip() or s in seen:
            continue
        seen.add(s)
        subs.append(s)
    if len(subs) < 2:  # 拆不出两个不同主题 = 无需分解
        return []
    return subs[:max_sub]


def _llm_decompose(query: str) -> List[str]:
    """调 Qwen 判别 + 分解。任何失败（无 key/超时/解析失败）→ []（回退单查询）。"""
    config = get_config()
    llm = config.llm
    if not llm.api_key:
        return []
    max_sub = max(2, config.rag.multi_query_max)
    url = f"{llm.api_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": llm.model,
        "messages": [
            {"role": "system", "content": _DECOMPOSE_SYSTEM.format(max_sub=max_sub)},
            {"role": "user", "content": query},
        ],
        "max_tokens": 200,
        "temperature": 0,
        "stream": False,
        "enable_thinking": False,  # 分解是小判别任务，思考只添延迟
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {llm.api_key}",
                     "Content-Type": "application/json"},
            timeout=config.rag.decompose_timeout,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("查询分解调用失败（回退单查询）: %s", e)
        return []
    return _parse_subqueries(content, max_sub=max_sub, original=query)


def maybe_decompose(query: str) -> List[str]:
    """按 multi_query_mode 决定是否分解。

    Returns:
        子查询列表（≥2 条，不含原查询本身）；不分解时返回 []。
    """
    mode = get_config().rag.multi_query_mode
    if mode not in ("auto", "llm"):
        return []
    if mode == "auto" and not looks_multi_intent(query):
        return []
    subs = _llm_decompose(query)
    if subs:
        logger.info("查询分解: %r → %d 路子查询 %s", query[:40], len(subs),
                    [s[:30] for s in subs])
    return subs
