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
import time
from typing import Any, Dict, List, Optional

import requests as http_requests
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from opensearch_pipeline.retriever import search_chunks
from opensearch_pipeline.llm_generator import generate_answer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dingtalk", tags=["DingTalk"])


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
        # 未配置 AppSecret 时跳过验证（仅用于开发调试）
        logger.warning("DINGTALK_APP_SECRET 未配置，跳过签名验证")
        return True

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
    群聊中会包含 @机器人 的前缀，需要去掉。
    """
    text_obj = body.get("text", {})
    content = text_obj.get("content", "").strip()

    # 去除 @机器人 标记（群聊场景）
    # 钉钉会在 content 开头添加 @xxx 的文本
    content = re.sub(r"^@\S+\s*", "", content).strip()

    return content


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
            source_line += f"（相关度 {score:.0%}）"
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
):
    """
    后台线程：执行 RAG 检索 + LLM 生成，通过 sessionWebhook 回复。

    独立线程运行，不阻塞钉钉 webhook 回调的 HTTP 响应。
    """
    t0 = time.time()

    try:
        # 1. 检索
        chunks = search_chunks(question, top_k=5)

        if not chunks:
            _send_text_reply(
                session_webhook,
                "🤷 抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述。",
            )
            return

        # 2. LLM 生成
        result = generate_answer(
            question,
            chunks,
            max_tokens=2048,
            temperature=0.1,
        )

        latency_ms = int((time.time() - t0) * 1000)

        # 3. 格式化并回复
        md_text = _format_answer_markdown(
            question=question,
            answer=result["answer"],
            sources=result["sources"],
            latency_ms=latency_ms,
            model=result["model"],
        )

        _send_reply(session_webhook, f"回答：{question[:20]}", md_text)

    except Exception as e:
        logger.error(
            "RAG 处理失败: question=%s, error=%s",
            question, e, exc_info=True,
        )
        _send_text_reply(
            session_webhook,
            f"❌ 处理您的问题时出错，请稍后重试。\n错误信息：{str(e)[:200]}",
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
    # 1. 签名验证
    timestamp = request.headers.get("timestamp", "")
    sign = request.headers.get("sign", "")

    if _get_app_secret() and not _verify_signature(timestamp, sign):
        logger.warning("钉钉签名验证失败: timestamp=%s", timestamp)
        raise HTTPException(status_code=403, detail="签名验证失败")

    # 2. 解析消息体
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析请求体")

    logger.info(
        "收到钉钉消息: sender=%s, conversationType=%s, msgId=%s",
        body.get("senderNick", "unknown"),
        body.get("conversationType", "?"),
        body.get("msgId", "?"),
    )

    # 3. 提取问题文本
    question = _extract_question(body)
    session_webhook = body.get("sessionWebhook", "")
    sender_nick = body.get("senderNick", "用户")
    conversation_id = body.get("conversationId", "")

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
    thread = threading.Thread(
        target=_process_rag_query,
        args=(question, session_webhook, sender_nick, conversation_id),
        daemon=True,
    )
    thread.start()

    # 6. 立即返回 200
    return {"msgtype": "empty"}
