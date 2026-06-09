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

# 文字回答通用规则 1-9；图文穿插规则（规则 10）单独拆出，
# 以便纯文本模式（pure_text）复用同一套基础规则但去掉图片插入指令。
# 规则 2/4/8/9 经文本质量 A/B 评测优化（51 query × 3-judge panel + 32 changed-answer judge）：
#   规则 2 修复"过度拒答"（检索命中即作答，不以"未找到"开头）；规则 8 强化"正文不列来源清单"；
#   规则 9 新增"数字/步骤须出自原文，缺失则说明文档未提供"（评测：fabrication 3→0, 正向过度拒答 9→3）。
_SYSTEM_PROMPT_BASE = """你是浙江富岭塑胶有限公司的智能知识库助手。请根据以下检索到的文档内容回答用户问题。

规则：
1. 只基于提供的参考文档内容回答，不要编造信息
2. 只有当参考文档确实没有任何相关或间接相关内容时，才回复"抱歉，当前知识库中未找到相关信息"。只要文档中有相关内容（包括界面截图说明、功能描述、操作步骤、相近条款、表格数据等），就必须直接基于这些内容作答，不要以"未找到"开头再补充答案
3. 保持简洁专业的语气
4. 如果用户问的是操作流程/步骤类问题，请用分步骤的方式回答，并完整列出文档中该流程涉及的所有步骤与关键参数，不要遗漏后续步骤
5. 如果多个文档内容有冲突，请同时说明并注明各自来源，由用户判断
6. 不要引用与问题明显无关的文档内容，忽略相关度为"低"的文档
7. 回答用中文
8. 不要在回答正文或末尾列出参考来源、文档名称或来源清单（系统会自动在回答下方展示来源，这是硬性要求）
9. 回答中的数字、型号、参数、按钮名称、菜单路径、步骤顺序必须严格来自参考文档原文，不得编造或自行推断；文档未提供的具体细节请直接说明"文档未提供"，不要凭常识补全"""

# 图文穿插规则（规则 10）：仅在图文（multimodal）模式下追加到 system prompt。
_IMG_INTERLEAVE_RULE = """
10. 如果参考文档中包含图片（标记为 [📷 图片]），请阅读图片内容描述，在回答中与该图片内容相关的段落后插入 <<IMG:N>> 标记（N 为文档编号）。如果某张图片与回答内容完全无关则不要插入。不要重复描述图片内容本身，用户将直接看到图片"""

# 默认（图文穿插）system prompt — 与历史逐字节一致
DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE + _IMG_INTERLEAVE_RULE

# 纯文本 system prompt — 去掉图片插入规则（规则 9）
TEXT_ONLY_SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE


# ═══════════════════════════════════════════════════════════════
# Context 组装
# ═══════════════════════════════════════════════════════════════

def _format_context(
    chunks: List[Dict[str, Any]],
    max_chars: int = 6000,
    pure_text: bool = False,
) -> str:
    """将检索到的 chunks 组装为 prompt context。

    pure_text=True（纯文本模式）：不再注入 <<IMG:N>> 图片插入标记，但仍保留
    [📷 图片] 标签与 visual_summary 文本，确保图片的语义内容不丢失（LLM 仍可
    据此用文字作答），只是不会触发图片穿插渲染。
    pure_text=False（默认）：行为与历史完全一致（图文穿插）。
    """
    config = get_config()
    threshold_high = config.rag.score_threshold_high
    threshold_medium = config.rag.score_threshold_medium
    # 重排开启时 chunk["score"] 为 rerank 分（0~1），需用 rerank 阈值标定相关度标签。
    rr_high = config.rag.rerank_score_threshold_high
    rr_medium = config.rag.rerank_score_threshold_medium

    parts = []
    total_chars = 0

    for i, chunk in enumerate(chunks):
        title = chunk.get("title", "未知文档")
        section = chunk.get("section_title", "")
        text = chunk.get("chunk_text", "")
        score = chunk.get("score", 0)
        chunk_type = chunk.get("chunk_type", "")

        header = f"[文档{i+1}] {title}"
        if section:
            header += f" > {section}"
        if chunk_type == "image":
            visual_summary = chunk.get("visual_summary", "")
            # 纯文本模式只保留 [📷 图片] 标签 + 图片内容描述，不注入 <<IMG:N>> 标记
            header += " [📷 图片]" if pure_text else f" [📷 图片] <<IMG:{i+1}>>"
            if visual_summary:
                header += f"\n图片内容：{visual_summary[:120]}"
        elif chunk_type == "step_card":
            step_no = chunk.get("step_no") or chunk.get("_step_no", "")
            total_steps = chunk.get("_total_steps", "")
            step_label = f"步骤{step_no}" if step_no else "步骤"
            if total_steps:
                step_label = f"步骤{step_no}/{total_steps}"
            header += f" [{step_label}]"
            image_refs = chunk.get("image_refs") or []
            if image_refs and not pure_text:
                header += f" <<IMG:{i+1}>>"
        elif chunk_type == "procedure_parent":
            header += " [流程概览]"
        elif chunk_type in ("text_chunk", "clause_chunk", "ocr_chunk", "visual_knowledge"):
            # 与 content_blocks_builder._extract_image_chunks 对齐：这些类型若携带图片，
            # 也要给 LLM 一个 <<IMG:N>> 提示；否则 referenced-only 渲染（只展示被引用图）会漏图。
            # 纯文本模式下不注入标记（也不展示图片）。
            if not pure_text and ((chunk.get("image_refs") or []) or chunk.get("source_image")):
                header += f" [📷 图片] <<IMG:{i+1}>>"
        if isinstance(score, (int, float)):
            # 越大越相关：融合分用 fused 阈值；rerank 分（0~1）用 rerank 阈值。
            if "rerank_score" in chunk:
                _hi, _md = rr_high, rr_medium
            else:
                _hi, _md = threshold_high, threshold_medium
            level = "高" if score >= _hi else "中" if score >= _md else "低"
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
                "chunk_type": chunk.get("chunk_type", ""),
                "source_image": chunk.get("source_image", ""),
                "visual_summary": chunk.get("visual_summary", ""),
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
    pure_text: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    根据检索结果生成 LLM 回答（非流式）。

    Args:
        pure_text: 纯文本模式开关。None → 取 config.rag.pure_text（全局开关）；
                   True → 去掉图文穿插（system prompt 不含规则 9，context 不注入
                   <<IMG:N>> 标记）；False → 图文穿插（默认）。

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

    # 解析纯文本开关：显式参数优先，否则取全局 config
    _pure = config.rag.pure_text if pure_text is None else pure_text
    _system = system_prompt or (TEXT_ONLY_SYSTEM_PROMPT if _pure else DEFAULT_SYSTEM_PROMPT)

    # 组装 context
    context = _format_context(context_chunks, max_chars=max_context_chars, pure_text=_pure)
    messages = _build_messages(query, context, history=history, system_prompt=_system)

    # 调用 DashScope (OpenAI compatible-mode)
    url = f"{llm.api_base_url.rstrip('/')}/chat/completions"

    payload = {
        "model": llm.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        # 非流式同样关闭思考（默认 False）：思考拖慢且 DashScope 对 qwen3 非流式+思考支持受限。
        "enable_thinking": llm.enable_thinking,
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

def parse_sse_data_frame(event: str) -> Optional[dict]:
    """解析一行 SSE 帧 ``data: {json}`` → dict；非数据帧 / [DONE] / 解析失败返回 None。

    生产者 generate_answer_stream 与消费者（api.py / dingtalk_bot.py）共用，替代原先三处
    "子串嗅探 + json.loads(event[6:])" 的脆弱手写解析（答案正文里出现 `"type": "chunk"`
    字面量也会被误判）。
    """
    if not event:
        return None
    s = event.strip()
    if not s.startswith("data:"):
        return None
    payload = s[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        d = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    return d if isinstance(d, dict) else None


def generate_answer_stream(
    query: str,
    context_chunks: List[Dict[str, Any]],
    *,
    history: Optional[List[Dict[str, str]]] = None,
    system_prompt: Optional[str] = None,
    max_context_chars: int = 6000,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    pure_text: Optional[bool] = None,
) -> Generator[str, None, None]:
    """
    根据检索结果生成 LLM 回答（SSE 流式）。

    pure_text: 见 generate_answer。None → 取 config.rag.pure_text。

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

    _pure = config.rag.pure_text if pure_text is None else pure_text
    _system = system_prompt or (TEXT_ONLY_SYSTEM_PROMPT if _pure else DEFAULT_SYSTEM_PROMPT)

    context = _format_context(context_chunks, max_chars=max_context_chars, pure_text=_pure)
    messages = _build_messages(query, context, history=history, system_prompt=_system)

    url = f"{llm.api_base_url.rstrip('/')}/chat/completions"

    payload = {
        "model": llm.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
        # 关闭 Qwen3 思考模式（默认 False）：思考会先生成大量 reasoning_content（本函数只读 content、
        # 直接丢弃），实测拖慢 ~4.5x（38.5s→8.6s）且首字 34s→1.3s，并挤占 max_tokens 致答案截断。
        # RAG 有检索上下文兜底，无需思考。可经 RAG_LLM_ENABLE_THINKING=true 开启对照。
        "enable_thinking": llm.enable_thinking,
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
