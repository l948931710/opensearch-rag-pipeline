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


def _op_db() -> str:
    """问答运营库名（qa_session_log/user_feedback/escalation_ticket 所在库）。
    经 RAG_RDS_OPERATION_DATABASE 配置（STAGING 用 fuling_operation_stg）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.operation_database


def generate_message_id() -> str:
    """生成唯一的 message_id，作为反馈系统的核心关联键。"""
    return str(uuid.uuid4())


def _conversation_history_on() -> bool:
    """RAG_CONVERSATION_HISTORY 开关（懒读 config；异常退回 False）。"""
    try:
        from opensearch_pipeline.config import get_config
        return bool(get_config().rag.conversation_history)
    except Exception:
        return False


def _upsert_conversation(conn, user_id: str, conversation_id: str, title) -> None:
    """会话元数据幂等 upsert（标题仅首次落、后续只更新时间）。

    独立小事务，失败仅 warning、绝不回滚已落库的审计行。隐藏状态由删除接口单独管理，
    本 upsert 不触碰 hidden_at —— 故对已隐藏会话继续写入不会令其自动重现。
    """
    try:
        with conn.cursor() as c2:
            c2.execute(
                f"""
                INSERT INTO {_op_db()}.qa_conversation
                    (user_id, conversation_id, title, created_at, updated_at, last_message_at)
                VALUES (%s, %s, %s, NOW(3), NOW(3), NOW(3))
                ON DUPLICATE KEY UPDATE updated_at = NOW(3), last_message_at = NOW(3)
                """,
                (user_id, conversation_id, (title or "")[:255]),
            )
        conn.commit()
    except Exception as ce:
        logger.warning(
            "qa_conversation upsert 失败 (non-fatal): conversation_id=%s, %s",
            conversation_id, ce,
        )


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
    conversation_id: Optional[str] = None,
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
                        # 答案血缘：chunk_id(内嵌 version) + version_no,使一条回答可溯源到精确 chunk/版本。
                        # 不带它们时,re-chunk 后 chunk_index 漂移 → 历史答案无法定位到原始来源(L7-01/INC-6)。
                        "chunk_id": d.get("chunk_id", ""),
                        "version_no": d.get("version_no"),
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
            base_cols = [
                "session_id", "message_id", "user_id", "user_name", "user_dept",
                "query_text", "answer_text", "intent_type", "risk_level", "risk_blocked",
                "retrieved_docs_json", "cited_docs_json",
                "latency_ms", "retrieval_latency_ms", "llm_latency_ms",
                "answer_status", "model_name", "error_message",
                "opensearch_hit_count", "top_score", "conversation_type",
                "content_blocks_json",
            ]
            base_vals = [
                session_id, message_id, user_id or "", user_name, user_dept,
                query_text, answer_text, intent_type, risk_level,
                1 if risk_blocked else 0,
                retrieved_json, cited_json,
                latency_ms, retrieval_latency_ms, llm_latency_ms,
                answer_status, model_name, error_message,
                opensearch_hit_count, top_score, conversation_type,
                content_blocks_json,
            ]

            def _insert(cols, vals):
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO {_op_db()}.qa_session_log ({', '.join(cols)}) "
                        f"VALUES ({', '.join(['%s'] * len(cols))})",
                        tuple(vals),
                    )
                conn.commit()

            # 正常路径：开关开 + 有 conversation_id → conversation_id 直接进主 INSERT（原子，无 post-commit 空窗）。
            # 兼容降级：库未迁移（unknown column 1054）→ 回滚后改 legacy INSERT，核心审计行恒落库、绝不丢。
            enrich = bool(conversation_id) and _conversation_history_on()
            try:
                if enrich:
                    _insert(base_cols + ["conversation_id"], base_vals + [conversation_id])
                else:
                    _insert(base_cols, base_vals)
            except Exception as ie:
                ierr = ie.args[0] if getattr(ie, "args", None) and isinstance(ie.args[0], int) else None
                if enrich and ierr == 1054:
                    logger.warning(
                        "conversation_id 列缺失，降级 legacy INSERT（请应用 schema/006）: message_id=%s, %s",
                        message_id, ie,
                    )
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    _insert(base_cols, base_vals)
                else:
                    raise
            logger.info(
                "qa_session_log 写入成功: message_id=%s, status=%s",
                message_id, answer_status,
            )
            # 审计行已落库；再 best-effort 幂等 upsert 会话元数据（独立小事务，失败仅 warning）。
            if enrich:
                _upsert_conversation(conn, user_id or "", conversation_id, query_text)
        finally:
            conn.close()

    except Exception as e:
        # 绝不阻断主流程；但表结构漂移（列/表不存在）意味着**每一条**问答日志都在静默丢失、
        # 反馈再也找不到 message_id —— 必须比普通写入失败喊得更响。pymysql 错误的 args[0] 是 errno。
        errno = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
        if errno in (1054, 1146):  # 1054=Unknown column / 1146=表不存在
            logger.critical(
                "qa_session_log 表结构落后于代码 (errno=%s)：请在 RDS 重跑 "
                "schema/002_feedback_system.sql（幂等）。修复前所有问答日志静默丢失、"
                "反馈无法按 message_id 关联。message_id=%s, error=%s",
                errno, message_id, e,
            )
        else:
            logger.error(
                "qa_session_log 写入失败 (non-fatal): message_id=%s, error=%s",
                message_id, e, exc_info=True,
            )
