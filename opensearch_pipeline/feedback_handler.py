# -*- coding: utf-8 -*-
"""
feedback_handler.py — RAG 反馈处理模块

处理用户对 RAG 回答的反馈：
  - upvote / downvote → 写入 user_feedback（ON DUPLICATE KEY UPDATE 覆盖）
  - handoff → 写入 escalation_ticket

供钉钉卡片回调和 REST API 端点共用。
"""

import json
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 反馈状态文本映射
# ═══════════════════════════════════════════════════════════════

_FEEDBACK_STATUS_MAP = {
    "upvote": "✅ 已反馈：有帮助",
    "downvote": "📝 已反馈：没帮助",
    "handoff": "🙋 已转人工处理",
}


def _op_db() -> str:
    """问答运营库名（qa_session_log/user_feedback/escalation_ticket 所在库）。
    经 RAG_RDS_OPERATION_DATABASE 配置（STAGING 用 fuling_operation_stg）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.operation_database


def get_feedback_status_text(action: str) -> str:
    """根据 action 返回卡片显示的反馈状态文本。"""
    return _FEEDBACK_STATUS_MAP.get(action, "✅ 已反馈")


# ═══════════════════════════════════════════════════════════════
# 核心反馈处理
# ═══════════════════════════════════════════════════════════════

def handle_feedback(
    *,
    message_id: str,
    user_id: str,
    user_name: Optional[str] = None,
    action: str,
    reason: Optional[str] = None,
    comment: Optional[str] = None,
) -> bool:
    """
    处理用户反馈。

    Args:
        message_id: 关联的 qa_session_log.message_id
        user_id: 反馈用户 ID
        user_name: 反馈用户昵称
        action: 'upvote' / 'downvote' / 'handoff'
        reason: 反馈原因代码（可选）
        comment: 反馈备注（可选）

    Returns:
        True=处理成功, False=处理失败
    """
    if not message_id or not user_id:
        logger.error("handle_feedback: message_id 或 user_id 为空")
        return False

    if action not in ("upvote", "downvote", "handoff"):
        logger.error("handle_feedback: 未知 action=%s", action)
        return False

    try:
        if action in ("upvote", "downvote"):
            return _save_feedback(
                message_id=message_id,
                user_id=user_id,
                user_name=user_name,
                feedback_type=action,
                reason=reason,
                comment=comment,
            )
        elif action == "handoff":
            return _create_escalation(
                message_id=message_id,
                user_id=user_id,
                user_name=user_name,
            )
    except Exception as e:
        logger.error(
            "handle_feedback 异常: message_id=%s, action=%s, error=%s",
            message_id, action, e, exc_info=True,
        )
        return False

    return False


# ═══════════════════════════════════════════════════════════════
# 反馈写入（upvote / downvote）
# ═══════════════════════════════════════════════════════════════

def _save_feedback(
    *,
    message_id: str,
    user_id: str,
    user_name: Optional[str],
    feedback_type: str,
    reason: Optional[str],
    comment: Optional[str],
) -> bool:
    """
    写入 user_feedback 表，重复反馈覆盖更新。

    使用 ON DUPLICATE KEY UPDATE (基于 uk_message_user 唯一约束)。
    从 qa_session_log 获取原始问答上下文冗余存储。
    """
    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    conn = _get_db_conn()
    try:
        # 查询原始问答上下文
        session_id = ""
        query_text = ""
        ai_answer = ""
        cited_json = None
        user_dept = None

        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT session_id, query_text, answer_text, cited_docs_json, user_dept
                FROM {_op_db()}.qa_session_log
                WHERE message_id = %s
                LIMIT 1
                """,
                (message_id,),
            )
            row = cursor.fetchone()
            if row:
                session_id = row[0] or ""
                query_text = row[1] or ""
                ai_answer = row[2] or ""
                cited_json = row[3]
                user_dept = row[4]

        # 写入 user_feedback（覆盖更新）。注意 handled_status 的 IF：用户点过「补充原因」
        # （AWAITING_COMMENT）后又通过卡片内联表单/小程序提交了明确反馈，挂起状态必须取消，
        # 否则他的下一条私聊问题仍会被 take_awaiting_comment 误吞成补充原因。
        feedback_id = str(uuid.uuid4())
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {_op_db()}.user_feedback (
                    feedback_id, session_id, message_id, user_id, user_name, user_dept,
                    query_text, ai_answer, cited_doc_ids_json,
                    feedback_type, feedback_reason, feedback_comment,
                    handled_status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s
                )
                ON DUPLICATE KEY UPDATE
                    feedback_type = VALUES(feedback_type),
                    feedback_reason = VALUES(feedback_reason),
                    feedback_comment = VALUES(feedback_comment),
                    handled_status = IF(handled_status = 'AWAITING_COMMENT',
                                        'PENDING', handled_status),
                    updated_at = NOW()
                """,
                (
                    feedback_id, session_id, message_id, user_id, user_name, user_dept,
                    query_text, ai_answer, cited_json,
                    feedback_type, reason, comment,
                    "PENDING",
                ),
            )
        conn.commit()

        logger.info(
            "user_feedback 写入成功: message_id=%s, user_id=%s, type=%s",
            message_id, user_id, feedback_type,
        )
        return True

    except Exception as e:
        conn.rollback()
        logger.error(
            "user_feedback 写入失败: message_id=%s, error=%s",
            message_id, e, exc_info=True,
        )
        return False
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 转人工（handoff）
# ═══════════════════════════════════════════════════════════════

def _create_escalation(
    *,
    message_id: str,
    user_id: str,
    user_name: Optional[str],
) -> bool:
    """
    写入 escalation_ticket 表。

    转人工不写 user_feedback 表（它们是不同的业务语义）。
    """
    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    conn = _get_db_conn()
    try:
        # 查询原始问答上下文
        session_id = ""
        query_text = ""
        ai_answer = ""
        user_dept = None

        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT session_id, query_text, answer_text, user_dept
                FROM {_op_db()}.qa_session_log
                WHERE message_id = %s
                LIMIT 1
                """,
                (message_id,),
            )
            row = cursor.fetchone()
            if row:
                session_id = row[0] or ""
                query_text = row[1] or ""
                ai_answer = row[2] or ""
                user_dept = row[3]

        # 写入 escalation_ticket
        ticket_id = str(uuid.uuid4())
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {_op_db()}.escalation_ticket (
                    ticket_id, session_id, message_id, user_id, user_name, user_dept,
                    query_text, ai_answer, trigger_reason, ticket_status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    ticket_id, session_id, message_id, user_id, user_name, user_dept,
                    query_text, ai_answer, "USER_HANDOFF", "PENDING",
                ),
            )
        conn.commit()

        logger.info(
            "escalation_ticket 创建成功: ticket_id=%s, message_id=%s, user_id=%s",
            ticket_id, message_id, user_id,
        )
        return True

    except Exception as e:
        conn.rollback()
        logger.error(
            "escalation_ticket 创建失败: message_id=%s, error=%s",
            message_id, e, exc_info=True,
        )
        return False
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# 「其他原因」自由文本：标记待补充 + 回收用户回复
# 流式卡不能弹内联表单（会冲掉流式正文→白屏），故改为：点「补充原因」→ 标记 AWAITING_COMMENT
# + 机器人提示用户直接回复 → 用户回复的下一条消息被 take_awaiting_comment 接住，写进 feedback_comment。
# 状态存 RDS（handled_status='AWAITING_COMMENT'），多 worker 安全。
# ═══════════════════════════════════════════════════════════════

def mark_awaiting_comment(
    *, message_id: str, user_id: str, user_name: Optional[str] = None
) -> bool:
    """标记用户对某条回答「待补充文字原因」。写/覆盖一条 user_feedback
    (downvote / reason=other / handled_status=AWAITING_COMMENT)。"""
    if not message_id or not user_id:
        return False
    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    conn = _get_db_conn()
    try:
        session_id = query_text = ai_answer = ""
        cited_json = None
        user_dept = None
        with conn.cursor() as cursor:
            cursor.execute(
                f"""SELECT session_id, query_text, answer_text, cited_docs_json, user_dept
                   FROM {_op_db()}.qa_session_log WHERE message_id = %s LIMIT 1""",
                (message_id,),
            )
            row = cursor.fetchone()
            if row:
                session_id = row[0] or ""
                query_text = row[1] or ""
                ai_answer = row[2] or ""
                cited_json = row[3]
                user_dept = row[4]

        feedback_id = str(uuid.uuid4())
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {_op_db()}.user_feedback (
                    feedback_id, session_id, message_id, user_id, user_name, user_dept,
                    query_text, ai_answer, cited_doc_ids_json,
                    feedback_type, feedback_reason, handled_status
                ) VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s)
                ON DUPLICATE KEY UPDATE
                    feedback_type = VALUES(feedback_type),
                    feedback_reason = VALUES(feedback_reason),
                    handled_status = VALUES(handled_status),
                    updated_at = NOW()
                """,
                (feedback_id, session_id, message_id, user_id, user_name, user_dept,
                 query_text, ai_answer, cited_json,
                 "downvote", "other", "AWAITING_COMMENT"),
            )
        conn.commit()
        logger.info("已标记待补充原因: message_id=%s, user_id=%s", message_id, user_id)
        return True
    except Exception as e:
        conn.rollback()
        logger.error("mark_awaiting_comment 失败: message_id=%s, error=%s", message_id, e, exc_info=True)
        return False
    finally:
        conn.close()


def take_awaiting_comment(*, user_id: str, comment: str, within_seconds: int = 600) -> bool:
    """若该用户最近 within_seconds 内有 handled_status='AWAITING_COMMENT' 的反馈，把 comment
    写进其 feedback_comment 并置回 'PENDING'；命中返回 True（调用方据此判定「这条消息是补充原因、
    不是新问题」）。未命中返回 False（按普通问答处理）。"""
    if not user_id or not comment or not comment.strip():
        return False
    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    try:
        conn = _get_db_conn()
    except Exception as e:
        # 取连接失败（RDS 故障/连接池初始化失败）也按「未命中」返回：调用方把这条消息
        # 当普通问题继续处理 —— 本函数在私聊主路径上，绝不能让 DB 故障 500 掉每条私聊
        logger.error(
            "take_awaiting_comment 取连接失败（按普通问题处理）: user_id=%s, error=%s",
            user_id, e,
        )
        return False
    try:
        with conn.cursor() as cursor:
            # 兜底回收：超窗仍滞留 AWAITING_COMMENT 的行归位 PENDING —— 没有这步，
            # 该用户多日后的任意私聊消息仍可能被误吞成「补充原因」，行也永远卡在挂起态
            cursor.execute(
                f"""UPDATE {_op_db()}.user_feedback
                   SET handled_status = 'PENDING'
                   WHERE user_id = %s AND handled_status = 'AWAITING_COMMENT'
                     AND updated_at < (NOW() - INTERVAL %s SECOND)""",
                (user_id, int(within_seconds)),
            )
            # 窗口按 updated_at 计：mark_awaiting_comment 的 upsert 只刷新 updated_at，
            # 旧投票行的 created_at 可能远早于点击「补充原因」的时刻，按 created_at 会误判超窗
            cursor.execute(
                f"""
                SELECT id FROM {_op_db()}.user_feedback
                WHERE user_id = %s AND handled_status = 'AWAITING_COMMENT'
                  AND updated_at >= (NOW() - INTERVAL %s SECOND)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (user_id, int(within_seconds)),
            )
            row = cursor.fetchone()
            if not row:
                conn.commit()  # 过期回收也要落地
                return False
            fid = row[0]
        with conn.cursor() as cursor:
            cursor.execute(
                f"""UPDATE {_op_db()}.user_feedback
                   SET feedback_comment = %s, handled_status = 'PENDING', updated_at = NOW()
                   WHERE id = %s""",
                (comment.strip()[:1000], fid),
            )
        conn.commit()
        logger.info("已收下补充原因: user_id=%s, len=%d", user_id, len(comment.strip()))
        return True
    except Exception as e:
        conn.rollback()
        logger.error("take_awaiting_comment 失败: user_id=%s, error=%s", user_id, e, exc_info=True)
        return False
    finally:
        conn.close()
