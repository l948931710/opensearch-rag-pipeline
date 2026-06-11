# -*- coding: utf-8 -*-
"""
llm_generator.py — LLM 回答生成模块

支持普通模式和 SSE 流式输出。使用 DashScope Qwen（OpenAI compatible-mode）。
"""

import json
import logging
import re
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
4. 仅当用户问的是**操作流程/步骤类**问题（答案是一串要依次执行的动作，如系统操作、办理手续）时，用分步骤方式回答：完整列出该流程的所有步骤与关键参数、不遗漏后续步骤，每步单独成段、以「**第N步**」开头（N 用阿拉伯数字，如 **第1步**），不要使用"1."、"①"等其他编号格式。若答案是**条款、分档规则、清单、禁令、处罚/奖惩标准**等非动作序列内容（如"如何划分/有哪些规定/罚多少"），不要套用「第N步」，改用条目符号或文档原有编号呈现——但完整性要求同样适用：文档中与该问题直接相关的全部条件、分档、例外、折算/豁免细则都要覆盖，不要因格式紧凑而省略要点
5. 如果多个文档内容有冲突，请同时说明并注明各自来源（用文档标题描述，不要用「文档N」编号），由用户判断
6. 不要引用与问题明显无关的文档内容，忽略相关度为"低"的文档
7. 回答用中文
8. 不要在回答正文或末尾列出参考来源、文档名称或来源清单；也不要以「[文档N]」「文档N」等编号引用参考文档，不要在步骤或段落后附「来源：…」标注 ——「文档N」是系统内部编号，用户看不到参考文档列表（系统会自动在回答下方展示来源，这是硬性要求）
9. 回答中的数字、型号、参数、按钮名称、菜单路径、步骤顺序必须严格来自参考文档原文，不得编造或自行推断；文档未提供的具体细节请直接说明"文档未提供"，不要凭常识补全"""

# 图文穿插规则（规则 10）：仅在图文（multimodal）模式下追加到 system prompt。
_IMG_INTERLEAVE_RULE = """
10. 如果参考文档中包含图片（标记为 [📷 图片]），请阅读图片内容描述，在回答中与该图片内容相关的段落后插入 <<IMG:N>> 标记（N 为文档编号）。对操作步骤类回答：若步骤正文来自带 [📷 图片] 标记的文档，默认应在该步骤后插入对应 <<IMG:N>>（界面截图/实物图对执行者有直接帮助），仅当图片与该步骤内容明显无关时才省略。严禁插入与回答内容无关的其他文档的图片标记。不要重复描述图片内容本身，用户将直接看到图片"""

# 默认（图文穿插）system prompt
# 2026-06-10：规则 4 增加「**第N步**」步骤编号 house style（小程序/原型步骤样式
# 与机器人格式统一）——此后与历史 prompt 不再逐字节一致。
DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE + _IMG_INTERLEAVE_RULE

# 纯文本 system prompt — 去掉图片插入规则（规则 9）
TEXT_ONLY_SYSTEM_PROMPT = _SYSTEM_PROMPT_BASE

# 低置信度护栏指令（soft answerability guard，RAG_LOW_CONFIDENCE_GUARD）。
# 不做按分数硬拒答：离线标定（eval_harness/gate_calibration.json）显示 top-1 重排分
# 的正/负分布重叠严重，硬闸门必然大量误拒可答问题；这里只在低置信带追加强化指令，
# 由读得到文档内容的 LLM 做第二级判别。
LOW_CONFIDENCE_RULE = """

注意：本次检索结果与用户问题的相关度偏低。请先逐条核对参考文档是否真正覆盖用户问题的要点：只有文档内容能直接支撑答案时才作答；若所有文档都只是主题相近但并未回答该问题（例如讲的是另一种产品、另一个流程、另一份制度，或文档编号/名称与所问不符），必须回复"抱歉，当前知识库中未找到相关信息"，不得从相近内容推断或拼凑作答。"""


def is_low_confidence_band(chunks: List[Dict[str, Any]]) -> bool:
    """top 检索分是否落入低置信带（重排分优先，缺失时用融合分；阈值=对应 medium）。

    与 RAG_LOW_CONFIDENCE_GUARD 开关解耦：/api/ask 响应的 guard 字段直接复用本判定
    （客户端据此渲染低匹配提示条），护栏指令是否注入 prompt 仍由开关决定。
    """
    config = get_config()
    if not chunks:
        return False
    rerank_scores = [c["rerank_score"] for c in chunks
                     if isinstance(c.get("rerank_score"), (int, float))]
    if rerank_scores:
        return max(rerank_scores) < config.rag.rerank_score_threshold_medium
    fused = [c["score"] for c in chunks if isinstance(c.get("score"), (int, float))]
    if not fused:
        return False
    return max(fused) < config.rag.score_threshold_medium


def _is_low_confidence(chunks: List[Dict[str, Any]]) -> bool:
    """护栏指令是否生效 = RAG_LOW_CONFIDENCE_GUARD 开启 且 落入低置信带。"""
    return get_config().rag.low_confidence_guard and is_low_confidence_band(chunks)


# 档位 → prompt 中文标签（与 API sources[].level 同源，见 score_level）
_LEVEL_ZH = {"high": "高", "mid": "中", "low": "低"}


def score_level(chunk: Dict[str, Any]) -> str:
    """按 chunk 分数标定相关度档位：'high'/'mid'/'low'；score 非数值返回 ''。

    单一事实来源：_format_context 的 prompt 中文标签与 API sources[].level 都由
    本函数判定。量纲选择沿用历史逻辑（有 rerank_score 键 → rerank 阈值 0.9/0.8，
    否则融合阈值 7.7/5.8）—— rerank 开启后分数是 0-1 量纲，客户端不可自行重算。
    """
    score = chunk.get("score", 0)
    if not isinstance(score, (int, float)):
        return ""
    config = get_config()
    if "rerank_score" in chunk:
        hi, md = config.rag.rerank_score_threshold_high, config.rag.rerank_score_threshold_medium
    else:
        hi, md = config.rag.score_threshold_high, config.rag.score_threshold_medium
    return "high" if score >= hi else "mid" if score >= md else "low"


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
            # 条款编号步骤（4.1 / 3.2.4）优先显示原文编号：step_no 是文档内
            # ordinal（排序键），照搬会让回答说"步骤5"而文档写"4.1"
            section_no = chunk.get("section_no", "")
            if section_no:
                step_label = f"步骤{section_no}"
            elif step_no:
                step_label = f"步骤{step_no}"
                if total_steps:
                    step_label = f"步骤{step_no}/{total_steps}"
            else:
                step_label = "步骤"
            header += f" [{step_label}]"
            image_refs = chunk.get("image_refs") or []
            if image_refs and not pure_text:
                # [📷 图片] 标签与 image/text_chunk 分支对齐：缺标签时 LLM 引用倾向明显
                # 偏低（2026-06-11 生产复测 J-water_soak/QA-24 带图步骤卡 0 引用实证）。
                header += f" [📷 图片] <<IMG:{i+1}>>"
        elif chunk_type == "procedure_parent":
            header += " [流程概览]"
        elif chunk_type in ("text_chunk", "clause_chunk", "ocr_chunk", "visual_knowledge"):
            # 与 content_blocks_builder._extract_image_chunks 对齐：这些类型若携带图片，
            # 也要给 LLM 一个 <<IMG:N>> 提示；否则 referenced-only 渲染（只展示被引用图）会漏图。
            # 纯文本模式下不注入标记（也不展示图片）。
            if not pure_text and ((chunk.get("image_refs") or []) or chunk.get("source_image")):
                header += f" [📷 图片] <<IMG:{i+1}>>"
        if isinstance(score, (int, float)):
            # 档位判定与 API sources[].level 同源（score_level），中文标签仅作 prompt 展示
            header += f" (相关度: {_LEVEL_ZH[score_level(chunk)]} {score:.2f})"

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


# 来源去重时从标题剥掉的文件扩展名（docx+pdf 双格式 double-ingest 视为同一来源）
_TITLE_EXT_PATTERN = re.compile(r'\.(docx?|pdf|xlsx?|csv|html?|txt)\s*$', re.IGNORECASE)


def _section_of(chunk: Dict[str, Any]) -> str:
    """chunk 的定位信息：section_title，为空时回退页码（PDF 作业指导书等无标题结构文档）。

    RDS 重建的 chunk 可能带显式 None（列为 NULL）—— 必须归一为 ""，
    SourceInfo.section 是非可空 str。
    """
    section = chunk.get("section_title") or ""
    if not section:
        try:
            page_num = int(chunk.get("page_num") or 0)
        except (TypeError, ValueError):
            page_num = 0   # 页码异常不影响回答（优雅降级）
        if page_num > 0:
            section = f"第{page_num}页"
    return section


def _extract_sources(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 chunks 中提取来源信息。

    去重按【标题去扩展名】而非 doc_id：语料中同一文件被重复注册成多个 doc_id
    （跨部门重复上传，如 A1员工行为管理标准 4 次注册）或 docx+pdf 双格式 double-ingest
    时，用户不应看到同一文档出现两行。chunks 已按检索排序，保留首次出现（排名最高）；
    被折叠行仅用于回填定位信息（docx 首位无页码、pdf 孪生有 第N页 时不丢失定位）。
    无标题文档退回 doc_id 区分，避免互不相关的空标题文档被折叠。
    """
    sources: List[Dict[str, Any]] = []
    key_to_idx: Dict[str, int] = {}
    for chunk in chunks:
        doc_id = chunk.get("doc_id", "")
        title = chunk.get("title", "")
        key = _TITLE_EXT_PATTERN.sub('', title).strip() or doc_id
        if key in key_to_idx:
            # 折叠重复行，但若保留行缺定位而本行有（docx 无原生页码、pdf 孪生有），回填
            kept = sources[key_to_idx[key]]
            if not kept["section"]:
                kept["section"] = _section_of(chunk)
            continue
        key_to_idx[key] = len(sources)
        sources.append({
            "doc_id": doc_id,
            "title": title,
            "section": _section_of(chunk),
            "score": chunk.get("score", 0),
            "level": score_level(chunk),
            "chunk_type": chunk.get("chunk_type", ""),
            "source_image": chunk.get("source_image", ""),
            "visual_summary": chunk.get("visual_summary", ""),
        })
    return sources


# ═══════════════════════════════════════════════════════════════
# 内部编号引用清洗（[文档N] 泄漏）
# ═══════════════════════════════════════════════════════════════

# _format_context 给 chunk 编的「[文档N]」标签是 prompt 内部协议（<<IMG:N>> 的 N 也指它），
# 但 LLM 偶发把编号写进正文当引用（2026-06-10 受控盲评 J-r120_21：步骤后附
# 「来源：[文档5] 提供了…」「来源：[文档3]」，2/3 评审点名）。规则 8 已明令禁止，
# 这里做确定性兜底清洗。⚠️ 不动 <<IMG:N>> —— 图文 blocks 构建依赖它。

# 行级：整行都是「来源/出处/依据：…文档N…」的归因行（含长句变体），整行删除。
_DOC_ATTRIBUTION_LINE_PATTERN = re.compile(
    r'^[ \t>*\-•·]*[（(]?\s*(?:信息|资料|参考)?(?:来源|出处|依据)\s*[）)]?\*{0,2}\s*[:：]'
    r'[^\n]*文档\s*\d+[^\n]*$',
    re.MULTILINE,
)
# 词级：[文档5] / 【文档3】/ （文档2）及聚合形态 [文档3、文档5]；连带吞掉紧邻的
# 「见/参见/根据…」引导词与 markdown 链接目标 [文档5](url)，避免残留语法碎片。
_DOC_CITATION_TOKEN_PATTERN = re.compile(
    r'(?:见|参见|详见|引自|出自|来自|根据|依据)?\s*'
    r'[\[【（(]\s*文档\s*\d+(?:\s*[、,，/和与]\s*(?:文档\s*)?\d+)*\s*[\]】）)]'
    r'(?:\([^)\n]*\))?'
)
_EMPTY_BRACKET_PATTERN = re.compile(r'[（(]\s*[）)]')
_EXCESS_BLANK_LINE_PATTERN = re.compile(r'\n{3,}')

# 标题式来源段：不含「文档N」编号、纯用《标题》/文件名列清单的形态（2026-06-11 钉钉
# 截图实证：「来源依据：」+《员工手册202108月.docx》bullets 同时穿透 strip_doc_citations
# （无 文档N）与 dingtalk_card._strip_trailing_sources（词表无「来源依据」），与卡片/小程序
# 的结构化来源面板形成双重引用）。两种形态：
#   段式：标题行独占一行 + 后续 ≥1 行「列表项且像文档引用」（《》/文档扩展名/章节）——
#         列表项必须像文档引用才删，正文里合法的《标题》叙述不受影响；
#   行式：强标题词 + 同行《标题》/文件名（弱词「来源/依据」不入行式，防误杀
#         「处罚依据：《员工手册》第3条」这类正当答案）。
_SOURCE_HEAD_STRONG = r'(?:参考(?:来源|文档|资料)|引用来源|来源信息|来源依据|来源清单|资料来源)'
_DOC_REF_HINT = r'(?:《[^》\n]{1,80}》|\.(?:docx|pdf|xlsx|pptx|txt|md)\b|章节)'
_SOURCE_SECTION_PATTERN = re.compile(
    r'^[ \t>*#\-•·]*\*{0,2}(?:' + _SOURCE_HEAD_STRONG + r'|来源)\*{0,2}\s*[:：]?\s*\*{0,2}\s*$\n?'
    r'(?:^[ \t]*(?:[-*•·]|\d+[.、）)])\s*[^\n]*' + _DOC_REF_HINT + r'[^\n]*$\n?)+',
    re.MULTILINE,
)
_SOURCE_LINE_PATTERN = re.compile(
    r'^[ \t>*\-•·]*\*{0,2}' + _SOURCE_HEAD_STRONG + r'\*{0,2}\s*[:：][^\n]*'
    + _DOC_REF_HINT + r'[^\n]*$',
    re.MULTILINE,
)


def strip_doc_citations(text: Optional[str]) -> str:
    """去除正文中泄漏的「[文档N]」上下文编号引用与「来源：[文档N]…」归因行。

    与 strip_image_markers（content_blocks_builder）分工：那边清 <<IMG:N>> 且必须在
    blocks 构建【之后】；本函数与占位符无关，可以（也应该）在 blocks/落库/写历史
    【之前】尽早调用 —— 编号引用进会话历史会诱导后续轮继续模仿。
    """
    if not text:
        return ""
    cleaned = _DOC_ATTRIBUTION_LINE_PATTERN.sub('', text)
    cleaned = _SOURCE_SECTION_PATTERN.sub('', cleaned)  # 标题式来源段（无 文档N 编号）
    cleaned = _SOURCE_LINE_PATTERN.sub('', cleaned)
    cleaned = _DOC_CITATION_TOKEN_PATTERN.sub('', cleaned)
    cleaned = _EMPTY_BRACKET_PATTERN.sub('', cleaned)   # 「（见[文档3]）」→「（）」残壳
    cleaned = _EXCESS_BLANK_LINE_PATTERN.sub('\n\n', cleaned)
    return cleaned.strip()


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
    if _is_low_confidence(context_chunks):
        logger.info("低置信度护栏触发（top 分低于 medium 阈值），追加强化拒答指令")
        _system = _system + LOW_CONFIDENCE_RULE

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

    # 源头清洗 [文档N] 编号引用（非流式四条消费链路一次覆盖；流式在收集端清理）
    answer = strip_doc_citations(data["choices"][0]["message"]["content"])
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
    if _is_low_confidence(context_chunks):
        logger.info("低置信度护栏触发（top 分低于 medium 阈值），追加强化拒答指令")
        _system = _system + LOW_CONFIDENCE_RULE

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
