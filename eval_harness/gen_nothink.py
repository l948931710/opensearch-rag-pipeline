"""Faithful answer generation with THINKING MODE OFF.

The production `generate_answer` does not set `enable_thinking`, so qwen3.6-plus may emit
reasoning tokens. The user requires answer-quality to be measured WITHOUT thinking mode.
This wrapper reuses the production prompt/context assembly verbatim (so the answer is
faithful to serving) but POSTs with `enable_thinking=false` and verifies it stayed off by
checking for any `reasoning_content` in the response.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from . import envboot  # noqa: F401
from opensearch_pipeline.config import get_config
from opensearch_pipeline import llm_generator as L


def generate_answer_nothink(
    query: str,
    context_chunks: List[Dict[str, Any]],
    *,
    history: Optional[List[Dict[str, str]]] = None,
    max_context_chars: int = 6000,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    pure_text: Optional[bool] = None,
) -> Dict[str, Any]:
    """Same contract as llm_generator.generate_answer, but with enable_thinking=False.

    Returns {answer, sources, model, usage, had_reasoning, latency_ms}.
    """
    config = get_config()
    llm = config.llm
    if not llm.api_key:
        raise RuntimeError("LLM API Key 未配置")

    _pure = config.rag.pure_text if pure_text is None else pure_text
    context, _included_chunks = L._format_context_ex(
        context_chunks, max_chars=max_context_chars, pure_text=_pure)
    # 与 llm_generator 逐字同款的算法：进了 context 的 chunk 的 1-based 文档号。
    # L4 用它作**权威可见集**（比正则扫 context 串可靠：不受文档正文自带 <<IMG:N>> 干扰）。
    _included_doc_indices = [i + 1 for i, c in enumerate(context_chunks)
                             if any(c is x for x in _included_chunks)]
    # ⚠️ 必须走生产的 `_select_system_prompt`，**不要**在这里重抄一份
    # `TEXT_ONLY if _pure else DEFAULT`。原先抄的那份在 #F-mm14 落地当天就让 A/B 变成
    # 假 ON：flag 只改了 llm_generator 的两条生产路径，评测走的是这里的复制品，
    # 于是 marker_validity 0.6458→0.6774 的"改善"其实全是温度噪声。
    # 评测与生产的 prompt 选择必须同源，否则量的不是被改的那个东西。
    _system = L._select_system_prompt(None, _pure, context)
    messages = L._build_messages(query, context, history=history, system_prompt=_system)

    url = f"{llm.api_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": llm.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        # DashScope compatible-mode: turn OFF Qwen3 thinking for fair, production-style answers.
        "enable_thinking": False,
    }
    t0 = time.time()
    resp = requests.post(
        url, json=payload,
        headers={"Authorization": f"Bearer {llm.api_key}", "Content-Type": "application/json"},
        timeout=120,
    )
    latency_ms = int((time.time() - t0) * 1000)
    resp.raise_for_status()
    data = resp.json()
    msg = data["choices"][0]["message"]
    answer = msg.get("content") or ""
    had_reasoning = bool(msg.get("reasoning_content"))
    return {
        "answer": answer,
        "sources": L._extract_sources(context_chunks),
        "model": llm.model,
        "usage": data.get("usage", {}),
        "had_reasoning": had_reasoning,
        "latency_ms": latency_ms,
        # 返回**实际喂给模型**的 context：L4-srv 的 orphan 分母要按它收窄到
        # "标记真的进了 context 的图"。让调用方重算会有参数漂移风险,故由生成方给出。
        "context": context,
        "included_doc_indices": _included_doc_indices,
    }
