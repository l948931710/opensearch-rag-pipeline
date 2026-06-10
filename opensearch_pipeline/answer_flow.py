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

from typing import Any, Dict, List, Optional

# 统一 NO_RESULT 文案（= 原 /api/ask 措辞；钉钉端回复时自行加 "🤷 " 渠道前缀）
NO_RESULT_MESSAGE = "抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述您的问题。"

# LLM 生成参数缺省值（API 请求可覆盖；钉钉端固定用缺省）
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.1


def top_score_of(chunks: Optional[List[Dict[str, Any]]]) -> Optional[float]:
    """检索结果最高分；空/None → None。"""
    if not chunks:
        return None
    return max((c.get("score", 0) for c in chunks), default=None)


def should_append_history(answer_text: Optional[str], answer_status: str) -> bool:
    """统一写历史策略：仅非空且 SUCCESS 的回答进入会话历史。

    出错时的部分回答不入史 —— 残句进上下文会污染后续轮次（钉钉流式一直如此，
    API 流式原先会把出错前的半截写进去，已统一）。
    """
    return bool(answer_text) and answer_status == "SUCCESS"


def build_qa_log_kwargs(
    *,
    session_id: str,
    message_id: str,
    question: str,
    user_id: str = "",
    user_name: Optional[str] = None,
    user_dept: Optional[str] = None,
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
) -> Dict[str, Any]:
    """qa_session_log 载荷的单一组装点。永远返回【全字段】（未知处显式 None，
    与 qa_logger.log_qa_session 的参数缺省一致）。

    chunks 语义：None = 检索未完成（命中数/分数全 None）；[] = NO_RESULT（命中数 0）。
    """
    return dict(
        session_id=session_id,
        message_id=message_id,
        user_id=user_id,
        user_name=user_name,
        user_dept=user_dept,
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
    )
