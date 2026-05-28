# -*- coding: utf-8 -*-
"""
llm_generator.py — LLM 回答生成模块

支持普通模式和 SSE 流式输出。使用 DashScope Qwen（OpenAI compatible-mode）。
"""

import json
import logging
from typing import Any, Dict, Generator, List, Optional

import requests

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# System Prompt 模板
# ═══════════════════════════════════════════════════════════════

DEFAULT_SYSTEM_PROMPT = """你是浙江富岭塑胶有限公司的智能知识库助手。请根据以下检索到的文档内容回答用户问题。

规则：
1. 只基于提供的参考文档内容回答，不要编造信息
2. 如果文档中没有相关信息，明确告知用户"抱歉，当前知识库中未找到相关信息"
3. 回答末尾注明参考来源（文档标题和章节）
4. 保持简洁专业的语气
5. 如果用户问的是操作流程类问题，请用分步骤的方式回答
6. 如果多个文档内容有冲突，请同时说明并注明各自来源，由用户判断
7. 不要引用与问题明显无关的文档内容，忽略相关度为"低"的文档
8. 回答用中文"""


# ═══════════════════════════════════════════════════════════════
# Context 组装
# ═══════════════════════════════════════════════════════════════

def _format_context(chunks: List[Dict[str, Any]], max_chars: int = 6000) -> str:
    """将检索到的 chunks 组装为 prompt context。"""
    parts = []
    total_chars = 0

    for i, chunk in enumerate(chunks):
        title = chunk.get("title", "未知文档")
        section = chunk.get("section_title", "")
        text = chunk.get("chunk_text", "")
        score = chunk.get("score", 0)

        header = f"[文档{i+1}] {title}"
        if section:
            header += f" > {section}"
        if isinstance(score, (int, float)):
            # 混合检索融合分（weighted/RRF）：越大越相关，DESC 排序
            level = "高" if score >= 6.0 else "中" if score >= 4.5 else "低"
            header += f" (相关度: {level} {score:.2f})"

        entry = f"{header}\n{text}\n"

        if total_chars + len(entry) > max_chars:
            # 截断过长的 context
            remaining = max_chars - total_chars
            if remaining > 100:
                parts.append(entry[:remaining] + "...(截断)")
            break

        parts.append(entry)
        total_chars += len(entry)

    return "\n---\n".join(parts)


def _extract_sources(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 chunks 中提取来源信息。"""
    sources = []
    seen = set()
    for chunk in chunks:
        doc_id = chunk.get("doc_id", "")
        title = chunk.get("title", "")
        key = (doc_id, title)
        if key not in seen:
            seen.add(key)
            sources.append({
                "doc_id": doc_id,
                "title": title,
                "section": chunk.get("section_title", ""),
                "score": chunk.get("score", 0),
            })
    return sources


# ═══════════════════════════════════════════════════════════════
# Messages 构建（支持多轮对话）
# ═══════════════════════════════════════════════════════════════

def _build_messages(
    query: str,
    context: str,
    history: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    构建 chat messages 数组。

    Args:
        query: 用户当前问题
        context: 检索到的文档上下文
        history: 对话历史, [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
        system_prompt: 自定义 system prompt
    """
    _system = system_prompt or DEFAULT_SYSTEM_PROMPT

    messages = [
        {"role": "system", "content": _system},
    ]

    # 插入对话历史
    if history:
        messages.extend(history)

    # 当前轮：将 context 和 query 合并为 user message
    user_content = f"=== 参考文档 ===\n{context}\n\n=== 用户问题 ===\n{query}"
    messages.append({"role": "user", "content": user_content})

    return messages


# ═══════════════════════════════════════════════════════════════
# 非流式生成
# ═══════════════════════════════════════════════════════════════

def generate_answer(
    query: str,
    context_chunks: List[Dict[str, Any]],
    *,
    history: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
    max_context_chars: int = 6000,
    max_tokens: int = 2048,
    temperature: float = 0.1,
) -> Dict[str, Any]:
    """
    根据检索结果生成 LLM 回答（非流式）。

    Returns:
        {
            "answer": str,
            "sources": [{"doc_id", "title", "section", "score"}],
            "model": str,
            "usage": {"prompt_tokens", "completion_tokens", "total_tokens"},
        }
    """
    config = get_config()
    llm = config.llm

    if not llm.api_key:
        raise RuntimeError("LLM API Key 未配置")

    # 组装 context
    context = _format_context(context_chunks, max_chars=max_context_chars)
    messages = _build_messages(query, context, history=history, system_prompt=system_prompt)

    # 调用 DashScope (OpenAI compatible-mode)
    url = f"{llm.api_base_url.rstrip('/')}/chat/completions"

    payload = {
        "model": llm.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }

    resp = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {llm.api_key}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()

    answer = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    sources = _extract_sources(context_chunks)

    logger.info("Answer generated: model=%s, tokens=%s", llm.model, usage)
    return {
        "answer": answer,
        "sources": sources,
        "model": llm.model,
        "usage": usage,
    }


# ═══════════════════════════════════════════════════════════════
# SSE 流式生成
# ═══════════════════════════════════════════════════════════════

def generate_answer_stream(
    query: str,
    context_chunks: List[Dict[str, Any]],
    *,
    history: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
    max_context_chars: int = 6000,
    max_tokens: int = 2048,
    temperature: float = 0.1,
) -> Generator[str, None, None]:
    """
    根据检索结果生成 LLM 回答（SSE 流式）。

    Yields SSE-formatted strings:
        data: {"type": "chunk", "content": "..."}
        data: {"type": "sources", "sources": [...]}
        data: {"type": "done", "usage": {...}}
        data: [DONE]
    """
    config = get_config()
    llm = config.llm

    if not llm.api_key:
        raise RuntimeError("LLM API Key 未配置")

    context = _format_context(context_chunks, max_chars=max_context_chars)
    messages = _build_messages(query, context, history=history, system_prompt=system_prompt)

    url = f"{llm.api_base_url.rstrip('/')}/chat/completions"

    payload = {
        "model": llm.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    # 先 yield sources 信息
    sources = _extract_sources(context_chunks)
    yield f"data: {json.dumps({'type': 'sources', 'sources': sources}, ensure_ascii=False)}\n\n"

    # 流式请求
    with requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {llm.api_key}",
            "Content-Type": "application/json",
        },
        timeout=120,
        stream=True,
    ) as resp:
        resp.raise_for_status()

        usage_info = {}
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if not line.startswith("data: "):
                continue

            payload_str = line[6:]  # strip "data: "

            if payload_str.strip() == "[DONE]":
                break

            try:
                chunk_data = json.loads(payload_str)
            except json.JSONDecodeError:
                continue

            # 提取 usage（通常在最后一个 chunk）
            if chunk_data.get("usage"):
                usage_info = chunk_data["usage"]

            # 提取 delta content
            choices = chunk_data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield f"data: {json.dumps({'type': 'chunk', 'content': content}, ensure_ascii=False)}\n\n"

    # 结束
    yield f"data: {json.dumps({'type': 'done', 'model': llm.model, 'usage': usage_info}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
