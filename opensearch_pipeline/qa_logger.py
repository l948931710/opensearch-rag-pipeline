# -*- coding: utf-8 -*-
"""
qa_logger.py — RAG 问答日志写入模块

每次 RAG 问答完成后，将完整的问答上下文写入 qa_session_log 表。
所有写入操作均用 try/except 包裹，失败只记日志不阻断回复。

供 dingtalk_bot.py 和 api.py 共用。
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def generate_message_id() -> str:
    """生成唯一的 message_id，作为反馈系统的核心关联键。"""
    return str(uuid.uuid4())


def log_qa_session(
    *,
    session_id: str,
    message_id: str,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    user_dept: Optional[str] = None,
    query_text: str,
    answer_text: Optional[str] = None,
    intent_type: Optional[str] = None,
    risk_level: Optional[str] = None,
    risk_blocked: bool = False,
    retrieved_docs: Optional[List[Dict[str, Any]]] = None,
    cited_docs: Optional[List[Dict[str, Any]]] = None,
    latency_ms: int = 0,
    retrieval_latency_ms: Optional[int] = None,
    llm_latency_ms: Optional[int] = None,
    answer_status: str = "SUCCESS",
    model_name: Optional[str] = None,
    error_message: Optional[str] = None,
    opensearch_hit_count: Optional[int] = None,
    top_score: Optional[float] = None,
    conversation_type: Optional[str] = None,
    content_blocks_json: Optional[str] = None,
) -> None:
    """
    写入一条 qa_session_log 记录。

    所有异常均被捕获并记录日志，绝不向调用方抛出异常。
    问答回复是核心功能，落库是辅助功能。

    Args:
        session_id: 会话 ID（钉钉 conversationId:staffId 或 API session）
        message_id: 本次回答的唯一 ID，后续反馈通过此 ID 关联
        user_id: 钉钉 staffId 或 API 调用方 ID
        user_name: 用户昵称
        user_dept: 用户部门代码
        query_text: 用户原始问题
        answer_text: 机器人回答快照
        retrieved_docs: OpenSearch 原始召回结果（topK chunks）
        cited_docs: 最终引用的来源文档
        latency_ms: 总耗时(ms)
        retrieval_latency_ms: 检索阶段耗时(ms)，第一版暂不填
        llm_latency_ms: LLM 生成阶段耗时(ms)，第一版暂不填
        answer_status: SUCCESS / NO_RESULT / LLM_ERROR / RETRIEVAL_ERROR / BLOCKED
        model_name: 使用的 LLM 模型名称
        error_message: 失败时的错误信息
        opensearch_hit_count: 检索命中数
        top_score: 最高检索得分
        conversation_type: '1'=单聊, '2'=群聊
    """
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn

        # 序列化 JSON 字段
        retrieved_json = None
        if retrieved_docs:
            # 只保留关键字段，避免存储过大
            retrieved_json = json.dumps(
                [
                    {
                        "doc_id": d.get("doc_id", ""),
                        "title": d.get("title", ""),
                        "section_title": d.get("section_title", ""),
                        "score": d.get("score", 0),
                        "chunk_index": d.get("chunk_index", 0),
                    }
                    for d in retrieved_docs
                ],
                ensure_ascii=False,
            )

        cited_json = None
        if cited_docs:
            cited_json = json.dumps(cited_docs, ensure_ascii=False)

        conn = _get_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO fuling_operation.qa_session_log (
                        session_id, message_id, user_id, user_name, user_dept,
                        query_text, answer_text, intent_type, risk_level, risk_blocked,
                        retrieved_docs_json, cited_docs_json,
                        latency_ms, retrieval_latency_ms, llm_latency_ms,
                        answer_status, model_name, error_message,
                        opensearch_hit_count, top_score, conversation_type,
                        content_blocks_json
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s
                    )
                    """,
                    (
                        session_id, message_id, user_id or "", user_name, user_dept,
                        query_text, answer_text, intent_type, risk_level,
                        1 if risk_blocked else 0,
                        retrieved_json, cited_json,
                        latency_ms, retrieval_latency_ms, llm_latency_ms,
                        answer_status, model_name, error_message,
                        opensearch_hit_count, top_score, conversation_type,
                        content_blocks_json,
                    ),
                )
            conn.commit()
            logger.info(
                "qa_session_log 写入成功: message_id=%s, status=%s",
                message_id, answer_status,
            )
        finally:
            conn.close()

    except Exception as e:
        # 绝不阻断主流程
        logger.error(
            "qa_session_log 写入失败 (non-fatal): message_id=%s, error=%s",
            message_id, e, exc_info=True,
        )
