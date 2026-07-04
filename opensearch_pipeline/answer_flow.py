# -*- coding: utf-8 -*-
"""
answer_flow.py — 四条回答链路（/api/ask、/api/ask/stream、钉钉同步、钉钉流式）共用的
收尾簿记：qa_session_log 载荷组装 + 写历史策略 + NO_RESULT 文案。

设计约束（务必保持）：本模块只提供【纯函数与常量】，不做任何副作用调用。
log_qa_session / append_to_history 的实际调用留在各调用方模块内、经模块全局名解析 ——
现有 4 个测试文件约 50 处 patch("opensearch_pipeline.api.log_qa_session") /
patch("opensearch_pipeline.dingtalk_bot.…") 的 mock 接缝全部依赖这一点。
把副作用挪进本模块会让这些测试静默失去拦截对象。

背景：2026-06 评审确认 4 条链路的簿记尾部各自手写、已发生漂移
（/api/ask 落请求体 user_id 而非 uid、API 路径从不落 user_dept、
content_blocks_json 三缺一、NO_RESULT 文案三份两样、写历史条件四种）。
本模块是这些字段的单一事实来源。
"""

import re
from typing import Any, Dict, List, Optional, Union

# 统一 NO_RESULT 文案（= 原 /api/ask 措辞；钉钉端回复时自行加 "🤷 " 渠道前缀）
NO_RESULT_MESSAGE = "抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述您的问题。"

# 强拒答句式（serving 侧 canonical 判定）。与 eval_harness/matching.py 的
# _REFUSAL_STRONG 故意保持两份独立拷贝：serving 不得 import eval_harness，
# eval 侧度量口径也不随 serving 演进漂移。如修改请同步评审两处。
_REFUSAL_STRONG_PATTERN = re.compile(
    r"(抱歉[，,。]?\s*(当前|知识库|未|没有)|知识库中(未|没有)|未找到相关信息|"
    r"无法回答|没有找到相关|未能找到相关|未提供相关信息)"
)


def is_refusal_answer(text: Optional[str], max_chars: int = 110) -> bool:
    """回答是否为「知识库未命中」类拒答（纯函数）。

    两种命中：拒答句式开篇（前 30 字符内出现，长答也算拒答开场）；
    或全文很短（≤ max_chars）且含强拒答句式。语义对齐 eval_harness/matching.py
    的 hard_refusal。NO_RESULT_MESSAGE 与 LLM 规则 2 指示的
    「抱歉，当前知识库中未找到相关信息」都命中 —— /api/ask 用它统一两种
    "未找到" 形态（检索空 vs LLM 带弱来源拒答）置 no_result 标志。
    """
    if not text:
        return False
    a = text.strip()
    m = _REFUSAL_STRONG_PATTERN.search(a)
    if not m:
        return False
    return m.start() <= 30 or len(a) <= max_chars

# LLM 生成参数缺省值（API 请求可覆盖；钉钉端固定用缺省）
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.1


def top_score_of(chunks: Optional[List[Dict[str, Any]]]) -> Optional[float]:
    """检索结果最高分；空/None → None。"""
    if not chunks:
        return None
    return max((c.get("score", 0) for c in chunks), default=None)


def should_append_history(answer_text: Optional[str], answer_status: str) -> bool:
    """统一写历史策略：非空且 SUCCESS / CLIENT_DISCONNECTED 的回答进入会话历史。

    出错时的部分回答不入史 —— 残句进上下文会污染后续轮次（钉钉流式一直如此，
    API 流式原先会把出错前的半截写进去，已统一）。

    CLIENT_DISCONNECTED（SSE 客户端中途断开）例外入史：collected frames = 用户
    实际看到的内容，follow-up 轮的语境应与用户所见一致（区别于出错残句 ——
    用户看到的是错误提示、不是半截回答）。该状态只改落库分桶，不改历史语义。
    """
    return bool(answer_text) and answer_status in ("SUCCESS", "CLIENT_DISCONNECTED")


def history_answer_text(answer_text: str) -> str:
    """统一入史文本策略（#F-mm5, RAG_HISTORY_STRIP_IMG_MARKERS 默认 OFF）。

    ON 时入史前剥掉 <<IMG:N>> 图片标记：历史里的标记会诱导 follow-up 轮 LLM 模仿，
    而 N 按【当前轮】image_map 解析——旧轮的 N 恰落在新轮任一带图 chunk 上即穿透
    referenced-only 的全部防线，附上无关噪声图（与 strip_doc_citations 防「文档N」
    编号模仿是同一根因的同款处理）。四个入史调用点（/api/ask、/api/ask/stream、
    钉钉流式/非流式）统一过本函数；qa_session_log 的 answer_text **不经此函数**
    （日志保真/溯源用原文）。回放侧兜底在 llm_generator._build_messages（覆盖存量
    已污染历史与客户端显式 req.history）。

    ⚠️ 调用方约束：只包裹入史实参，绝不提前改写 result["answer"] —— content_blocks
    构建依赖原始标记（/api/ask 的 blocks 在入史之后才构建）。
    """
    if not answer_text:
        return answer_text
    try:
        from opensearch_pipeline.config import get_config
        if not get_config().rag.history_strip_img_markers:
            return answer_text
        from opensearch_pipeline.content_blocks_builder import strip_image_markers
        return strip_image_markers(answer_text)
    except Exception:
        return answer_text   # fail-open：清洗失败不阻塞入史


def build_qa_log_kwargs(
    *,
    session_id: str,
    message_id: str,
    question: str,
    user_id: str = "",
    user_name: Optional[str] = None,
    user_dept: Union[str, List[str], None] = None,
    answer_text: Optional[str] = None,
    chunks: Optional[List[Dict[str, Any]]] = None,
    cited_docs: Optional[List[Dict[str, Any]]] = None,
    latency_ms: int = 0,
    retrieval_latency_ms: Optional[int] = None,
    llm_latency_ms: Optional[int] = None,
    answer_status: str = "SUCCESS",
    model_name: Optional[str] = None,
    error_message: Optional[str] = None,
    conversation_type: Optional[str] = None,
    content_blocks_json: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """qa_session_log 载荷的单一组装点。永远返回【全字段】（未知处显式 None，
    与 qa_logger.log_qa_session 的参数缺省一致）。

    chunks 语义：None = 检索未完成（命中数/分数全 None）；[] = NO_RESULT（命中数 0）。

    answer_status 词表（语料排查按此分桶）：
      SUCCESS   — 正常回答
      REFUSAL   — 拒答型（检索有候选但 LLM 按护栏拒答，is_refusal_answer 判定；
                  客户端同样显示"未找到"）→ 语料弱 / 检索未召回
      NO_RESULT — 检索为空 → 语料缺，补文档的直接信号
      LLM_ERROR — 生成异常（error_message 带 trace_id）
      CLIENT_DISCONNECTED — SSE 客户端中途断开，回答被截断（仅 /api/ask/stream）；
                  不参与 REFUSAL 翻转（截断恰止于拒答句式会假命中），照常入史
    注意：入史策略（should_append_history）按翻转前的原状态判定 —— REFUSAL 只改
    落库标签，不改变"拒答照旧入史"的既有行为。
    """
    return dict(
        session_id=session_id,
        message_id=message_id,
        user_id=user_id,
        user_name=user_name,
        # 多组：list → CSV（qa_session_log.user_dept 仍 VARCHAR，无迁移）；str/None 原样
        user_dept=",".join(user_dept) if isinstance(user_dept, list) else user_dept,
        query_text=question,
        answer_text=answer_text or None,
        retrieved_docs=chunks or None,
        cited_docs=cited_docs or None,
        latency_ms=latency_ms,
        retrieval_latency_ms=retrieval_latency_ms,
        llm_latency_ms=llm_latency_ms,
        answer_status=answer_status,
        model_name=model_name,
        error_message=error_message,
        opensearch_hit_count=(len(chunks) if chunks is not None else None),
        top_score=top_score_of(chunks),
        conversation_type=conversation_type,
        content_blocks_json=content_blocks_json or None,
        conversation_id=conversation_id,
    )
