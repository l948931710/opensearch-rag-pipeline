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
    _system = L.TEXT_ONLY_SYSTEM_PROMPT if _pure else L.DEFAULT_SYSTEM_PROMPT
    context = L._format_context(context_chunks, max_chars=max_context_chars, pure_text=_pure)
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
    }
