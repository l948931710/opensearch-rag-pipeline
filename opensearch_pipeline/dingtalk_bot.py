# -*- coding: utf-8 -*-
"""
dingtalk_bot.py — 钉钉企业内部机器人回调适配层

流程：
  1. 钉钉用户 @机器人 或私聊发送消息
  2. 钉钉服务器 POST 到 /dingtalk/webhook
  3. 验证签名 → 解析消息 → 立即回复 "正在查询…"
  4. 后台线程调用 RAG 检索 + LLM 生成
  5. 通过 sessionWebhook 回复 Markdown 格式答案

环境变量：
  DINGTALK_APP_SECRET   — 机器人 AppSecret，用于签名验证（必须）
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import uuid
import time
from typing import Any, Dict, List, Optional

import requests as http_requests
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from opensearch_pipeline.retriever import retrieve_and_enrich
from opensearch_pipeline.llm_generator import generate_answer
from opensearch_pipeline.config import get_config
from opensearch_pipeline.session_store import get_or_create_session, append_to_history
from opensearch_pipeline.qa_logger import generate_message_id, log_qa_session
from opensearch_pipeline.dingtalk_card import send_interactive_card, update_card_feedback_status
from opensearch_pipeline.feedback_handler import handle_feedback, get_feedback_status_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dingtalk", tags=["DingTalk"])


# ═══════════════════════════════════════════════════════════════
# 用户部门解析
# ═══════════════════════════════════════════════════════════════

def _resolve_user_dept(staff_id: str) -> Optional[str]:
    """
    从 RDS user_role 表查询用户所属部门。
    如果 user_role 中不存在，自动通过钉钉 API 获取并缓存。

    查询失败或用户不存在时返回 None，调用方会降级为只返回 public + internal 文档。
    """
    if not staff_id or staff_id.startswith("$:"):
        return None

    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn

        conn = _get_db_conn()
        try:
            # 1. 先查本地缓存
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dept_code FROM fuling_knowledge.user_role "
                    "WHERE user_id = %s AND is_active = 1 LIMIT 1",
                    (staff_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    logger.info("用户部门解析成功（缓存）: staff_id=%s → dept=%s", staff_id, row[0])
                    return row[0]

            # 2. 本地没有，调钉钉 API 获取
            user_info = _fetch_dingtalk_user_info(staff_id)
            if user_info:
                dept_name = user_info.get("dept_name", "")
                user_name = user_info.get("user_name", "")
                # 3. 缓存到 user_role 表
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO fuling_knowledge.user_role (user_id, user_name, dept_code, role, is_active)
                            VALUES (%s, %s, %s, %s, 1)
                            ON DUPLICATE KEY UPDATE
                                user_name = VALUES(user_name),
                                dept_code = VALUES(dept_code),
                                updated_at = NOW()
                            """,
                            (staff_id, user_name, dept_name, "employee"),
                        )
                    conn.commit()
                    logger.info("用户信息已缓存: staff_id=%s, name=%s, dept=%s", staff_id, user_name, dept_name)
                except Exception as cache_err:
                    logger.warning("缓存用户信息失败: %s", cache_err)
                return dept_name or None
            else:
                logger.warning("用户未在 user_role 表中注册且 API 查询失败: staff_id=%s", staff_id)
                return None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询用户部门失败 staff_id=%s: %s", staff_id, e)
        return None


def _fetch_dingtalk_user_info(user_id: str) -> Optional[dict]:
    """
    通过钉钉 API 获取用户信息（姓名、部门等）。

    Returns:
        {"user_name": "张三", "dept_name": "行政部"} 或 None
    """
    from opensearch_pipeline.dingtalk_card import _get_access_token

    token = _get_access_token()
    if not token:
        return None

    try:
        import requests as _requests

        # 使用旧版 API（更兼容）: /topapi/v2/user/get
        resp = _requests.post(
            f"https://oapi.dingtalk.com/topapi/v2/user/get?access_token={token}",
            json={"userid": user_id},
            timeout=5,
        )
        print(f"[USER DEBUG] 钉钉用户查询: userId={user_id}, status={resp.status_code}", flush=True)

        if resp.status_code == 200:
            data = resp.json()
            print(f"[USER DEBUG] API 响应: errcode={data.get('errcode')}, errmsg={data.get('errmsg')}", flush=True)
            if data.get("errcode") == 0:
                result = data.get("result", {})
                user_name = result.get("name", "")
                dept_name = ""
                # 获取部门 ID 列表，取第一个部门名称
                dept_id_list = result.get("dept_id_list", [])
                if dept_id_list:
                    dept_name = _fetch_dept_name(token, dept_id_list[0])
                print(f"[USER DEBUG] 用户信息: name={user_name}, dept={dept_name}", flush=True)
                return {"user_name": user_name, "dept_name": dept_name}
            else:
                print(f"[USER DEBUG] 用户查询业务失败: {data}", flush=True)
                return None
        else:
            print(f"[USER DEBUG] 用户查询HTTP失败: {resp.text[:300]}", flush=True)
            return None
    except Exception as e:
        print(f"[USER DEBUG] 用户查询异常: {e}", flush=True)
        return None


def _fetch_dept_name(token: str, dept_id: int) -> str:
    """通过部门 ID 获取部门名称。"""
    try:
        import requests as _requests
        resp = _requests.post(
            f"https://oapi.dingtalk.com/topapi/v2/department/get?access_token={token}",
            json={"dept_id": dept_id},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                return data.get("result", {}).get("name", "")
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

def _get_app_secret() -> str:
    return os.environ.get("DINGTALK_APP_SECRET", "")


# 签名验证的时间窗口（秒）
_TIMESTAMP_TOLERANCE = 3600


# ═══════════════════════════════════════════════════════════════
# 签名验证
# ═══════════════════════════════════════════════════════════════

def _verify_signature(timestamp: str, sign: str) -> bool:
    """
    验证钉钉回调签名。

    钉钉在 HTTP Header 中发送 timestamp 和 sign，
    签名算法：Base64(HmacSHA256(timestamp + "\\n" + appSecret))
    """
    app_secret = _get_app_secret()
    if not app_secret:
        logger.error("DINGTALK_APP_SECRET 未配置，拒绝请求（生产环境必须配置此密钥）")
        return False

    # 校验时间戳防重放
    try:
        ts_ms = int(timestamp)
        now_ms = int(time.time() * 1000)
        if abs(now_ms - ts_ms) > _TIMESTAMP_TOLERANCE * 1000:
            logger.warning("签名时间戳过期: ts=%s, now=%s", ts_ms, now_ms)
            return False
    except (ValueError, TypeError):
        return False

    string_to_sign = f"{timestamp}\n{app_secret}"
    hmac_code = hmac.new(
        app_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    expected_sign = base64.b64encode(hmac_code).decode("utf-8")
    return hmac.compare_digest(sign, expected_sign)


# ═══════════════════════════════════════════════════════════════
# 消息解析
# ═══════════════════════════════════════════════════════════════

def _extract_question(body: Dict[str, Any]) -> str:
    """
    从钉钉回调 body 中提取用户问题文本。

    钉钉消息格式因客户端和版本不同而异：
      - msgtype="text"     → body["text"]["content"] 为纯文本字符串
      - msgtype="richText" → body["content"]["richText"] 为富文本片段数组
                             每个片段形如 {"text": "..."} 或 {"pictureUrl": "..."}
    群聊中会包含 @机器人 标记，需要去掉。
    """
    msgtype = body.get("msgtype", "")
    content = ""

    if msgtype == "text":
        # 标准文本消息：body["text"]["content"]
        text_obj = body.get("text", {})
        if isinstance(text_obj, dict):
            content = text_obj.get("content", "") or ""
        elif isinstance(text_obj, str):
            content = text_obj

    elif msgtype == "richText":
        # 富文本消息（iOS/部分 Android 客户端）：body["content"]["richText"]
        content_obj = body.get("content", {})
        if isinstance(content_obj, dict):
            rich_parts = content_obj.get("richText", [])
            if isinstance(rich_parts, list):
                # 拼接所有文本片段，忽略图片等非文本片段
                text_segments = []
                for part in rich_parts:
                    if isinstance(part, dict) and "text" in part:
                        text_segments.append(str(part["text"]))
                content = "".join(text_segments)

    else:
        # 未知 msgtype：尝试通用提取
        text_obj = body.get("text", {})
        if isinstance(text_obj, dict):
            content = text_obj.get("content", "") or ""
        elif isinstance(text_obj, str):
            content = text_obj
        # fallback: 顶层 content 如果是字符串
        if not content:
            top_content = body.get("content", "")
            if isinstance(top_content, str):
                content = top_content

    # 确保 content 是字符串
    if not isinstance(content, str):
        logger.warning("[消息提取] content 类型异常: type=%s, value=%r", type(content).__name__, content)
        content = str(content) if content else ""

    content = content.strip()

    logger.info("[消息提取] msgtype=%s, raw_content=%r", msgtype, content)
    print(f"[DINGTALK] 消息提取: msgtype={msgtype}, raw_content={content!r}", flush=True)

    if not content:
        return ""

    # 去除 @机器人 标记（群聊场景，可能出现在任意位置）
    cleaned = re.sub(r"@\S+", "", content).strip()

    logger.info("[消息提取] 去除@后: %r", cleaned)
    print(f"[DINGTALK] 去除@后: {cleaned!r}", flush=True)

    return cleaned


def _get_conversation_type(body: Dict[str, Any]) -> str:
    """返回 '1'=单聊, '2'=群聊"""
    return body.get("conversationType", "1")


# ═══════════════════════════════════════════════════════════════
# 回复发送
# ═══════════════════════════════════════════════════════════════

def _send_reply(session_webhook: str, markdown_title: str, markdown_text: str):
    """通过 sessionWebhook 发送 Markdown 格式回复。"""
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": markdown_title,
            "text": markdown_text,
        },
    }
    try:
        resp = http_requests.post(
            session_webhook,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error("钉钉回复失败: status=%s, body=%s", resp.status_code, resp.text)
        else:
            logger.info("钉钉回复成功")
    except Exception as e:
        logger.error("钉钉回复异常: %s", e, exc_info=True)


def _send_text_reply(session_webhook: str, text: str):
    """通过 sessionWebhook 发送纯文本回复。"""
    payload = {
        "msgtype": "text",
        "text": {"content": text},
    }
    try:
        http_requests.post(
            session_webhook,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except Exception as e:
        logger.error("钉钉纯文本回复异常: %s", e, exc_info=True)


# ═══════════════════════════════════════════════════════════════
# 回答格式化
# ═══════════════════════════════════════════════════════════════

def _format_answer_markdown(
    question: str,
    answer: str,
    sources: List[Dict[str, Any]],
    latency_ms: int,
    model: str,
) -> str:
    """将 RAG 回答格式化为钉钉 Markdown。"""

    lines = []
    lines.append(f"#### 💡 {question}")
    lines.append("")
    lines.append(answer)
    lines.append("")

    # 参考来源
    if sources:
        lines.append("---")
        lines.append("**📚 参考来源**")
        seen_titles = set()
        for src in sources:
            title = src.get("title", "未知文档")
            if title in seen_titles:
                continue
            seen_titles.add(title)
            section = src.get("section", "")
            score = src.get("score", 0)
            source_line = f"- {title}"
            if section:
                source_line += f" > {section}"
            source_line += f"（相关度 {score:.2f}）"
            lines.append(source_line)
        lines.append("")

    # 元信息
    lines.append(f"> 模型: {model} | 耗时: {latency_ms/1000:.1f}s")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 后台 RAG 处理
# ═══════════════════════════════════════════════════════════════

def _process_rag_query(
    question: str,
    session_webhook: str,
    sender_nick: str,
    conversation_id: str,
    sender_staff_id: str = "",
    conversation_type: str = "1",
):
    """
    后台线程：执行 RAG 检索 + LLM 生成，通过 sessionWebhook 回复。

    独立线程运行，不阻塞钉钉 webhook 回调的 HTTP 响应。
    支持多轮对话：使用 conversationId:senderStaffId 作为 session key，
    群聊中每个用户拥有独立的对话上下文。
    """
    t0 = time.time()
    message_id = generate_message_id()

    # 构建 session key：群聊中按用户隔离，单聊中按会话隔离
    session_key = f"{conversation_id}:{sender_staff_id}" if sender_staff_id else conversation_id
    _, history = get_or_create_session(session_key)

    try:
        # 0. 解析用户部门（用于权限过滤）
        user_dept = _resolve_user_dept(sender_staff_id) if sender_staff_id else None

        # 1. 统一检索 + 邻居拼接（top_k=7, stitch window=±1）
        chunks = retrieve_and_enrich(question, user_dept=user_dept)

        if not chunks:
            # 无结果也要落库
            latency_ms = int((time.time() - t0) * 1000)
            log_qa_session(
                session_id=session_key,
                message_id=message_id,
                user_id=sender_staff_id,
                user_name=sender_nick,
                user_dept=user_dept,
                query_text=question,
                answer_text=None,
                latency_ms=latency_ms,
                answer_status="NO_RESULT",
                opensearch_hit_count=0,
                conversation_type=conversation_type,
            )
            _send_text_reply(
                session_webhook,
                "🤷 抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述。",
            )
            return

        # 2. LLM 生成（传入多轮对话历史）
        result = generate_answer(
            question,
            chunks,
            history=list(history),
            max_tokens=2048,
            temperature=0.1,
        )

        latency_ms = int((time.time() - t0) * 1000)

        # 3. 追加到会话历史（供下轮使用）
        append_to_history(session_key, question, result["answer"])

        # 4. 落库
        top_score = max((c.get("score", 0) for c in chunks), default=None)
        log_qa_session(
            session_id=session_key,
            message_id=message_id,
            user_id=sender_staff_id,
            user_name=sender_nick,
            user_dept=user_dept,
            query_text=question,
            answer_text=result["answer"],
            retrieved_docs=chunks,
            cited_docs=result.get("sources"),
            latency_ms=latency_ms,
            answer_status="SUCCESS",
            model_name=result.get("model"),
            opensearch_hit_count=len(chunks),
            top_score=top_score,
            conversation_type=conversation_type,
        )

        # 5. 发送互动卡片（失败降级为 Markdown）
        print(f"[DEBUG] 准备发送互动卡片: message_id={message_id}, conv_type={conversation_type}", flush=True)
        try:
            card_sent = send_interactive_card(
                conversation_id=conversation_id,
                conversation_type=conversation_type,
                sender_staff_id=sender_staff_id,
                message_id=message_id,
                question=question,
                answer=result["answer"],
                sources=result["sources"],
                latency_ms=latency_ms,
                model=result["model"],
            )
            print(f"[DEBUG] 互动卡片结果: card_sent={card_sent}", flush=True)
        except Exception as card_err:
            print(f"[DEBUG] 互动卡片异常: {card_err}", flush=True)
            card_sent = False

        if not card_sent:
            # 降级：使用 Markdown 回复（无反馈按钮）
            print("[DEBUG] 降级为 Markdown 回复", flush=True)
            md_text = _format_answer_markdown(
                question=question,
                answer=result["answer"],
                sources=result["sources"],
                latency_ms=latency_ms,
                model=result["model"],
            )
            _send_reply(session_webhook, f"回答：{question[:20]}", md_text)
            print("[DEBUG] Markdown 回复已发送", flush=True)

    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        latency_ms = int((time.time() - t0) * 1000)
        logger.error(
            "RAG 处理失败 [trace=%s]: question=%s, error=%s",
            trace_id, question, e, exc_info=True,
        )
        # 失败也要落库
        log_qa_session(
            session_id=session_key,
            message_id=message_id,
            user_id=sender_staff_id,
            user_name=sender_nick,
            query_text=question,
            latency_ms=latency_ms,
            answer_status="LLM_ERROR",
            error_message=f"[trace={trace_id}] {str(e)[:500]}",
            conversation_type=conversation_type,
        )
        _send_text_reply(
            session_webhook,
            f"❌ 处理您的问题时出错，请稍后重试。(trace: {trace_id})",
        )


# ═══════════════════════════════════════════════════════════════
# Webhook 端点
# ═══════════════════════════════════════════════════════════════

@router.post("/webhook")
async def dingtalk_webhook(request: Request):
    """
    钉钉机器人消息回调端点。

    钉钉在用户 @机器人 或私聊时，POST 消息到此端点。
    立即返回 200（发送 "查询中" 提示），后台线程处理 RAG 问答。
    """
    # 用 try/except 包裹整体逻辑，确保任何异常都有回复
    try:
        return await _handle_webhook(request)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("webhook 处理未捕获异常: %s", e, exc_info=True)
        print(f"[DINGTALK ERROR] webhook 未捕获异常: {e}", flush=True)
        raise HTTPException(status_code=500, detail="内部错误")


async def _handle_webhook(request: Request):
    """实际的 webhook 处理逻辑。"""
    # 1. 签名验证
    timestamp = request.headers.get("timestamp", "")
    sign = request.headers.get("sign", "")

    if not _verify_signature(timestamp, sign):
        logger.warning("钉钉签名验证失败: timestamp=%s", timestamp)
        raise HTTPException(status_code=403, detail="签名验证失败")

    # 2. 解析消息体
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析请求体")

    body_json = json.dumps(body, ensure_ascii=False, default=str)
    logger.info(
        "收到钉钉消息: sender=%s, conversationType=%s, msgId=%s",
        body.get("senderNick", "unknown"),
        body.get("conversationType", "?"),
        body.get("msgId", "?"),
    )
    logger.info("钉钉完整消息体: %s", body_json)
    print(f"[DINGTALK] 完整消息体: {body_json}", flush=True)

    # 3. 提取问题文本
    question = _extract_question(body)
    session_webhook = body.get("sessionWebhook", "")
    sender_nick = body.get("senderNick", "用户")
    sender_staff_id = body.get("senderStaffId", "") or body.get("senderId", "")
    conversation_id = body.get("conversationId", "")

    print(f"[DINGTALK] 提取结果: question={question!r}, has_webhook={bool(session_webhook)}", flush=True)

    if not question:
        if session_webhook:
            _send_text_reply(session_webhook, "👋 您好！请输入您想查询的问题，我会为您从知识库中检索答案。")
        return {"msgtype": "empty"}

    if not session_webhook:
        logger.error("钉钉回调缺少 sessionWebhook，无法回复")
        raise HTTPException(status_code=400, detail="缺少 sessionWebhook")

    # 4. 立即回复 "查询中" 提示
    _send_text_reply(session_webhook, f"🔍 正在为您查询「{question[:50]}」，请稍候...")

    # 5. 后台线程处理 RAG 问答
    conv_type = str(body.get("conversationType", "1"))
    thread = threading.Thread(
        target=_process_rag_query,
        args=(question, session_webhook, sender_nick, conversation_id, sender_staff_id, conv_type),
        daemon=True,
    )
    thread.start()

    # 6. 立即返回 200
    return {"msgtype": "empty"}


# ═══════════════════════════════════════════════════════════════
# 互动卡片回调端点
# ═══════════════════════════════════════════════════════════════

@router.post("/card/callback")
async def card_callback(request: Request):
    """
    钉钉互动卡片 HTTP 回调端点。

    当用户点击卡片上的按钮（有帮助/没帮助/转人工）时，
    钉钉 POST 到此端点。处理反馈后更新卡片状态。

    回调请求体格式：
    {
        "type": "actionCallback",
        "outTrackId": "message_id",
        "userId": "user123",
        "content": "{\"cardPrivateData\":{\"actionIds\":[...],\"params\":{\"action\":\"downvote\",\"message_id\":\"xxx\"}}}"
    }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析请求体")

    logger.info(
        "收到卡片回调: outTrackId=%s, userId=%s",
        body.get("outTrackId", "?"),
        body.get("userId", "?"),
    )

    # 解析回调数据
    out_track_id = body.get("outTrackId", "")
    user_id = body.get("userId", "")

    # content 是 JSON 字符串，需要二次解析
    content_str = body.get("content", "{}")
    try:
        content = json.loads(content_str) if isinstance(content_str, str) else content_str
    except (json.JSONDecodeError, TypeError):
        content = {}

    params = content.get("cardPrivateData", {}).get("params", {})
    action = params.get("action", "")
    message_id = params.get("message_id", "") or out_track_id

    if not message_id or not action:
        logger.warning("卡片回调缺少 message_id 或 action: body=%s", body)
        return {"cardData": {"cardParamMap": {}}}

    # 处理反馈
    success = handle_feedback(
        message_id=message_id,
        user_id=user_id,
        action=action,
    )

    # 从数据库重建完整卡片数据（回调响应会覆盖整个 cardParamMap）
    feedback_text = get_feedback_status_text(action) if success else "⚠️ 反馈处理失败"
    print(f"[CALLBACK DEBUG] message_id={message_id}, action={action}, success={success}, text={feedback_text}", flush=True)

    card_param_map = {"feedback_status": feedback_text}
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT query_text, answer_text, cited_docs_json, model_name, latency_ms
                    FROM fuling_operation.qa_session_log
                    WHERE message_id = %s LIMIT 1
                    """,
                    (message_id,),
                )
                row = cursor.fetchone()
                if row:
                    card_param_map["question"] = row[0] or ""
                    card_param_map["title"] = (row[0] or "")[:50]
                    card_param_map["answer"] = row[1] or ""
                    # 重建 sources_text
                    sources_json = row[2]
                    if sources_json:
                        import json as _json
                        try:
                            sources_list = _json.loads(sources_json) if isinstance(sources_json, str) else sources_json
                            sources_lines = []
                            for i, s in enumerate(sources_list, 1):
                                if isinstance(s, dict):
                                    sources_lines.append(f"{i}. {s.get('title', s.get('doc_name', '未知文档'))}")
                                else:
                                    sources_lines.append(f"{i}. {s}")
                            card_param_map["sources_text"] = "\n".join(sources_lines)
                            card_param_map["sources"] = card_param_map["sources_text"]
                        except Exception:
                            card_param_map["sources_text"] = ""
                            card_param_map["sources"] = ""
                    else:
                        card_param_map["sources_text"] = ""
                        card_param_map["sources"] = ""
                    model = row[3] or "unknown"
                    latency = row[4] or 0
                    card_param_map["meta"] = f"模型: {model} | 耗时: {latency / 1000:.1f}s"
                    card_param_map["message_id"] = message_id
        finally:
            conn.close()
    except Exception as e:
        print(f"[CALLBACK DEBUG] 重建卡片数据失败: {e}", flush=True)

    return {
        "cardData": {
            "cardParamMap": card_param_map,
        },
    }
