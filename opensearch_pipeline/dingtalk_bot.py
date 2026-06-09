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
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from opensearch_pipeline.retriever import retrieve_and_enrich
from opensearch_pipeline.llm_generator import generate_answer, generate_answer_stream, _extract_sources
from opensearch_pipeline.config import get_config
from opensearch_pipeline.session_store import get_or_create_session, append_to_history
from opensearch_pipeline.qa_logger import generate_message_id, log_qa_session
from opensearch_pipeline.dingtalk_card import (
    send_interactive_card,
    update_card_feedback_status,
    update_card_data,
    create_streaming_card,
    streaming_update_card,
    send_text_to_user,
    _strip_trailing_sources,
    _format_sources_text,
)
from opensearch_pipeline.feedback_handler import (
    handle_feedback,
    mark_awaiting_comment,
    take_awaiting_comment,
)
from opensearch_pipeline.dingtalk_identity import _resolve_user_dept

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dingtalk", tags=["DingTalk"])


# 用户身份/部门解析（机器人 + 小程序共用）已抽离到 dingtalk_identity.py


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
    images: Optional[List[Dict[str, str]]] = None,
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

    # 相关图片（Markdown 降级时追加到末尾）
    if images:
        lines.append("---")
        lines.append("**🖼️ 相关图片**")
        for img in images[:3]:
            desc = img.get("title", "图片")[:30]
            url = img.get("url", "")
            if url:
                lines.append(f"![{desc}]({url})")
        lines.append("")

    # 元信息
    lines.append(f"> 模型: {model} | 耗时: {latency_ms/1000:.1f}s")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 后台 RAG 处理
# ═══════════════════════════════════════════════════════════════

def _stream_answer_to_card(
    *,
    question: str,
    chunks: List[Dict[str, Any]],
    history: List[Dict[str, str]],
    session_key: str,
    message_id: str,
    conversation_id: str,
    conversation_type: str,
    sender_staff_id: str,
    sender_nick: str,
    user_dept: Optional[str],
    t0: float,
    t_retrieval: float,
    retrieval_latency_ms: int,
) -> bool:
    """以流式 AI 卡片（打字机效果）输出纯文本回答。

    流程：投放流式卡片占位 → 逐 token 累计并按节流间隔覆盖式更新 → 定稿 → 写历史 + 落库。
    与非流式路径保持一致的落库/反馈语义（钉钉端纯文本，不含图文 content_blocks）。

    Returns:
        True  —— 已完整处理（投放+流式+落库），调用方应直接 return；
        False —— 流式卡片投放失败（未配置/无 token/单聊缺 staffId 等），调用方应降级到
                 非流式成品卡片路径（不会重复落库）。
    """
    cfg = get_config()
    # 默认 "content"：钉钉 AI 流式卡片的约定流式变量（AICardContent + 流式 MarkdownBlock 默认绑定
    # content、varType=markdown）。推流 key 必须 == 模板流式组件绑定的变量，否则 500 unknownError。
    # 可经 DINGTALK_STREAM_CARD_KEY 覆盖（须与模板里流式组件绑定的变量名一致）。
    stream_key = os.environ.get("DINGTALK_STREAM_CARD_KEY", "content")
    model_name = cfg.llm.model
    sources = _extract_sources(chunks)

    # 1. 先投放流式卡片占位（sources/meta/question 此时已知）
    created = create_streaming_card(
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        sender_staff_id=sender_staff_id,
        message_id=message_id,
        question=question,
        sources=sources,
        model=model_name,
        stream_key=stream_key,
    )
    if not created:
        logger.warning("流式卡片投放失败，降级为非流式路径: message_id=%s", message_id)
        return False

    interval_s = max(cfg.rag.dingtalk_stream_interval_ms, 0) / 1000.0
    collected: List[str] = []
    answer_status = "SUCCESS"
    error_message: Optional[str] = None

    def _clean(text: str) -> str:
        # 与成品卡片一致的清理：去除末尾参考来源段 + <<IMG:N>> 占位符（钉钉端纯文本）
        return re.sub(r'<{1,2}IMG:\d+>{1,2}', '', _strip_trailing_sources(text)).strip()

    # 非阻塞推流：后台单线程每 interval 推一次"最新累计正文"，主循环只消费 LLM token、不被
    # PUT /card/streaming 的网络往返阻塞。同步推流在推流往返慢时会把 ~6s 的生成拖成数十秒
    # （每帧阻塞累加）。关键不变量：finalize 前必须 stop+join 推流线程，杜绝"推流帧覆盖定稿帧"
    # → 空白/掉页脚（曾踩过的坑）。单线程顺序推流→帧不乱序；推流失败 fail open。
    _push_interval = interval_s if interval_s > 0 else 0.3
    _latest = {"text": ""}
    _plock = threading.Lock()
    _pstop = threading.Event()

    def _pusher() -> None:
        last = ""
        while not _pstop.wait(_push_interval):
            with _plock:
                txt = _latest["text"]
            if txt and txt != last:
                try:
                    streaming_update_card(message_id, txt, key=stream_key, is_full=True)
                    last = txt
                except Exception:
                    pass  # 推流失败不影响生成/定稿

    _pthread = threading.Thread(target=_pusher, name="dt-stream-push", daemon=True)

    def _stop_pusher() -> None:
        _pstop.set()
        if _pthread.is_alive():
            _pthread.join(timeout=5)

    try:
        _pthread.start()
        for event in generate_answer_stream(
            question,
            chunks,
            history=history if history else None,
            max_tokens=2048,
            temperature=0.1,
            pure_text=True,
        ):
            if '"type": "chunk"' not in event:
                continue
            try:
                d = json.loads(event[6:].strip())
            except (json.JSONDecodeError, KeyError):
                continue
            if d.get("type") != "chunk" or not d.get("content"):
                continue
            collected.append(d["content"])
            _txt = _clean("".join(collected))
            with _plock:
                _latest["text"] = _txt  # 后台线程按节流推送，主循环不阻塞

        # 生成结束 → 先停推流线程（确保不与定稿竞争），再写定稿帧
        _stop_pusher()
        full_answer = _clean("".join(collected))
        # B2 版式：定稿帧把【参考来源 + "模型 ｜ 耗时"】按序拼进正文末尾 → 顺序 答案→来源→耗时，
        # 耗时落到最底下（紧挨按钮）、渲染成灰色缩进，且【不闪不空白】。改走 content 而非 meta 页脚：
        # 定稿前/后用 update_card_data 写页脚都会触发重渲染闪烁。create 时 sources/meta 页脚已置空，
        # 避免重复。full_answer 保持干净（落库/写历史不含来源/耗时页脚）。
        # 显示「检索/生成」分段耗时而非总耗时：生成(LLM 输出)是主要成本(占 ~50-75%)，检索通常 <1s，
        # 让用户看清耗时归属（27s 是模型在写、不是系统慢）。生成 = time.time()-t_retrieval（= llm_latency_ms）。
        _ret_s = (retrieval_latency_ms or 0) / 1000.0
        _gen_s = time.time() - t_retrieval
        _src_md = _format_sources_text(sources)
        _footer = f"> 模型: {model_name} ｜ 检索 {_ret_s:.1f}s · 生成 {_gen_s:.1f}s"
        _final = (
            f"{full_answer}\n\n📚 **参考来源**\n{_src_md}\n\n{_footer}"
            if _src_md else f"{full_answer}\n\n{_footer}"
        )
        streaming_update_card(
            message_id, _final,
            key=stream_key, is_full=True, is_finalize=True,
        )
        # 可选「白屏保险」：把定稿全文也写进 cardData[stream_key]，让【完成态卡片在 cardData 里自洽】。
        # 背景：自定义「回传请求」按钮(👍/👎/转人工)被点击时，钉钉客户端可能从 cardData 重渲染卡片；
        # 而 create 时 stream_key 置空、流式正文只在「流式通道」→ 重渲染读 cardData 就是空 → 白屏。
        # 本模板(原生2)完成态正文绑定的恰是 stream_key(content)，故把全文持久化进 cardData 后，
        # 重渲染即自洽、不白屏。默认【关闭】：保持"纯流式定稿、不调 update_card_data"的现状，避免对
        # 原生反馈/无回调模板回归（历史 A/B：盲调 update_card_data 会白屏）。仅当改用自定义回传按钮
        # 且【真机实测点击会白屏】时，置环境变量 DINGTALK_FINALIZE_PERSIST_CONTENT=true 开启。
        if os.environ.get("DINGTALK_FINALIZE_PERSIST_CONTENT", "").strip().lower() in ("1", "true", "yes"):
            try:
                update_card_data(message_id, {stream_key: _final, "is_answer_done": "true"})
            except Exception as _e:
                logger.warning("定稿持久化 content 到 cardData 失败(忽略，不影响流式正文): %s", _e)
    except Exception as e:
        _stop_pusher()  # 异常路径同样先停推流线程，再写错误定稿帧（避免推流帧覆盖）
        trace_id = uuid.uuid4().hex[:8]
        answer_status = "LLM_ERROR"
        error_message = f"[trace={trace_id}] {str(e)[:500]}"
        logger.error("流式生成失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        full_answer = _clean("".join(collected))
        streaming_update_card(
            message_id,
            full_answer or f"❌ 回答生成失败，请稍后重试。(trace: {trace_id})",
            key=stream_key, is_full=True, is_finalize=True, is_error=True,
        )

    llm_latency_ms = int((time.time() - t_retrieval) * 1000)
    latency_ms = int((time.time() - t0) * 1000)
    top_score = max((c.get("score", 0) for c in chunks), default=None)

    # ⚠️ 不在定稿后调 update_card_data：PUT /card/instances 会用部分 cardParamMap 覆盖流式写入的
    # content（A/B 实测：调了→定稿后卡片空白；不调→全文保留）。完成态按钮已在模板里硬化
    # （只看 feedback_status==""，不依赖 is_answer_done），定稿帧已含全文、create 已设
    # question/sources/meta，故完成态可直接显示，无需任何收尾更新。
    # 代价：meta 页脚不带"耗时"（保持 create 时的"模型: X"），换取不丢正文——值得。

    # 写历史（仅成功且有内容时）+ 落库（与非流式路径一致的反馈语义）
    if full_answer and answer_status == "SUCCESS":
        append_to_history(session_key, question, full_answer)

    log_qa_session(
        session_id=session_key,
        message_id=message_id,
        user_id=sender_staff_id,
        user_name=sender_nick,
        user_dept=user_dept,
        query_text=question,
        answer_text=full_answer or None,
        retrieved_docs=chunks,
        cited_docs=sources,
        latency_ms=latency_ms,
        retrieval_latency_ms=retrieval_latency_ms,
        llm_latency_ms=llm_latency_ms,
        answer_status=answer_status,
        model_name=model_name,
        opensearch_hit_count=len(chunks),
        top_score=top_score,
        conversation_type=conversation_type,
        error_message=error_message,
    )
    return True


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
        t_retrieval = time.time()
        retrieval_latency_ms = int((t_retrieval - t0) * 1000)

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
                retrieval_latency_ms=retrieval_latency_ms,
                answer_status="NO_RESULT",
                opensearch_hit_count=0,
                conversation_type=conversation_type,
            )
            _send_text_reply(
                session_webhook,
                "🤷 抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述。",
            )
            return

        # 2a. 流式 AI 卡片路径（打字机效果）：开关开启且配置了流式模板时启用；
        #     投放失败/未配置时自动降级到下方非流式成品卡片路径（不重复落库）。
        if get_config().rag.dingtalk_streaming and os.environ.get("DINGTALK_STREAM_CARD_TEMPLATE_ID"):
            if _stream_answer_to_card(
                question=question,
                chunks=chunks,
                history=list(history),
                session_key=session_key,
                message_id=message_id,
                conversation_id=conversation_id,
                conversation_type=conversation_type,
                sender_staff_id=sender_staff_id,
                sender_nick=sender_nick,
                user_dept=user_dept,
                t0=t0,
                t_retrieval=t_retrieval,
                retrieval_latency_ms=retrieval_latency_ms,
            ):
                return

        # 2. LLM 生成（传入多轮对话历史）
        #    纯文本模式（RAG_PURE_TEXT）下生成纯文字回答，下游跳过图文穿插
        pure_text = get_config().rag.pure_text
        result = generate_answer(
            question,
            chunks,
            history=list(history),
            max_tokens=2048,
            temperature=0.1,
            pure_text=pure_text,
        )

        t_llm = time.time()
        llm_latency_ms = int((t_llm - t_retrieval) * 1000)
        latency_ms = int((t_llm - t0) * 1000)

        # 3. 追加到会话历史（供下轮使用）
        append_to_history(session_key, question, result["answer"])

        # 4. 构建 content_blocks（图文穿插）；纯文本模式下不展示图片
        from opensearch_pipeline.content_blocks_builder import build_content_blocks, content_blocks_to_json
        content_blocks = [] if pure_text else build_content_blocks(result["answer"], chunks)
        content_blocks_json_str = content_blocks_to_json(content_blocks)

        # 5. 落库（包含 content_blocks_json 供回调重建）
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
            retrieval_latency_ms=retrieval_latency_ms,
            llm_latency_ms=llm_latency_ms,
            answer_status="SUCCESS",
            model_name=result.get("model"),
            opensearch_hit_count=len(chunks),
            top_score=top_score,
            conversation_type=conversation_type,
            content_blocks_json=content_blocks_json_str or None,
        )

        # 6. 发送互动卡片（失败降级为 Markdown）
        img_blocks = [b for b in content_blocks if b.get("type") == "image"] if content_blocks else []
        print(f"[DEBUG] 准备发送互动卡片: message_id={message_id}, conv_type={conversation_type}, "
              f"content_blocks={len(content_blocks)}, image_blocks={len(img_blocks)}", flush=True)
        for ib in img_blocks:
            url_preview = ib.get("url", "")[:100]
            print(f"[DEBUG]   📸 image: title={ib.get('title','')[:40]}, url={url_preview}...", flush=True)
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
                content_blocks=content_blocks if content_blocks else None,
            )
            print(f"[DEBUG] 互动卡片结果: card_sent={card_sent}", flush=True)
        except Exception as card_err:
            print(f"[DEBUG] 互动卡片异常: {card_err}", flush=True)
            card_sent = False

        if not card_sent:
            # 降级：使用 Markdown 回复（无反馈按钮）
            print("[DEBUG] 降级为 Markdown 回复", flush=True)
            # 提取图片信息用于 markdown 降级显示
            md_images = []
            if content_blocks:
                md_images = [b for b in content_blocks if b.get("type") == "image"]
            md_text = _format_answer_markdown(
                question=question,
                answer=result["answer"],
                sources=result["sources"],
                latency_ms=latency_ms,
                model=result["model"],
                images=md_images,
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
    """实际的 webhook 处理逻辑。

    签名校验与 body 读取走 async；其后的 ack 文本回复（HTTP POST）与 take_awaiting_comment（DB）
    都是阻塞 I/O，放进线程池执行，避免阻塞共享事件循环（同一进程还服务 /api/ask 等接口）。
    """
    # 1. 签名验证（仅用 headers）
    timestamp = request.headers.get("timestamp", "")
    sign = request.headers.get("sign", "")

    if not _verify_signature(timestamp, sign):
        logger.warning("钉钉签名验证失败: timestamp=%s", timestamp)
        raise HTTPException(status_code=403, detail="签名验证失败")

    # 2. 解析消息体（async）
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无法解析请求体")

    # 3. 其余逻辑全是阻塞 I/O → 线程池执行
    return await run_in_threadpool(_process_webhook_body, body)


def _process_webhook_body(body: dict):
    """webhook 同步处理：日志、问题提取、「补充原因」回收、ack 回复、起后台 RAG 线程。"""
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

    # ── 「补充原因」回收（仅单聊 conversationType=='1'）──
    #    若该用户刚点过卡片上的「补充原因」(handled_status=AWAITING_COMMENT)，把这条【单聊】消息当作
    #    对上一条回答的补充原因收下，写进 user_feedback.feedback_comment，不走问答。
    #    为何仅单聊：「补充原因」提示本就是机器人【私信(单聊)】发的，用户在单聊里回复即可；而群聊里
    #    @机器人 的消息一律按【新问题】处理，避免把群里的提问误判成"补充原因"。
    if str(body.get("conversationType", "1")) == "1" and sender_staff_id \
            and take_awaiting_comment(user_id=sender_staff_id, comment=question):
        if session_webhook:
            _send_text_reply(session_webhook, "✅ 已记录你补充的原因，谢谢反馈！")
        return {"msgtype": "feedback_comment"}

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

    # 落库/文本回复均为阻塞 I/O → 线程池执行，避免阻塞事件循环
    return await run_in_threadpool(_process_card_callback_body, body)


def _process_card_callback_body(body: dict):
    """卡片回调同步处理：解析 action/feedback → 落库 → 文本提示。一律 ACK-only（不含 cardData）。"""
    logger.info(
        "收到卡片回调: outTrackId=%s, userId=%s",
        body.get("outTrackId", "?"),
        body.get("userId", "?"),
    )
    # 全量回调体日志：用于抓取钉钉【原生赞踩 Feedback 组件】点击时的真实 payload（action/字段名），
    # 以便把原生赞踩精确落库（自定义按钮已从模板移除，避免 cardParamMap 更新冲掉流式正文→白屏）。
    try:
        print(f"[CALLBACK RAW] {json.dumps(body, ensure_ascii=False)[:1500]}", flush=True)
    except Exception:
        pass

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
    reason = params.get("reason")  # ActionSheet 菜单传入的踩原因
    comment = params.get("comment")  # 官方赞踩模版的"踩→内联输入→提交"带的自由文本原因

    # 兼容钉钉【官方赞踩模版】：它用 feedback=good/bad（不是 action），且不传 message_id（用 outTrackId 兜底）。
    # 该模版的"踩"用本地态 setLocalState 弹【内联输入框】（纯客户端、不发回调 → 不白屏！），只有"提交"
    # 才 request 回调，带 feedback=bad + comment。"赞"则直接 request 带 feedback=good。
    feedback = params.get("feedback")
    if not action and feedback:
        action = "upvote" if feedback in ("good", "like", "up") else "downvote"

    user_name = body.get("userName") or None

    if not message_id or not action:
        logger.warning("卡片回调缺少 message_id 或 action: body=%s", body)
        return {}  # ACK-only（不带 cardData）→ 不更新卡片

    # ⚠️ 回调一律 ACK-only：响应里【绝不放 cardData】→ 钉钉不重渲染卡片 → 不会冲掉流式写入的正文
    # （白屏根因，已三次实证）。赞踩的视觉由钉钉【原生 Feedback 组件】自己呈现；转人工/补充原因的
    # 提示走机器人 1 对 1 文本消息（回调请求里没有 sessionWebhook）。落库失败 fail open，不影响 ACK。
    _ACK: dict = {}

    # ── 转人工 ──
    if action == "handoff":
        try:
            handle_feedback(message_id=message_id, user_id=user_id, user_name=user_name, action="handoff")
            if user_id:
                send_text_to_user(user_id, "🙋 已为你转人工，相关同事会尽快跟进～")
        except Exception as e:
            logger.error("handoff 处理失败: %s", e, exc_info=True)
        return _ACK

    # ── 「补充原因」自由文本：标记待补充 + 提示用户直接回复 ──
    #    用户回复的下一条消息由 _handle_webhook 的 take_awaiting_comment 接住，写进 feedback_comment。
    if action in ("add_reason", "downvote_other", "downvote_other_start", "downvote_other_submit"):
        try:
            mark_awaiting_comment(message_id=message_id, user_id=user_id, user_name=user_name)
            if user_id:
                send_text_to_user(user_id, "📝 想补充具体原因？直接【回复本条消息】发给我就行，我会记录下来～")
        except Exception as e:
            logger.error("add_reason 处理失败: %s", e, exc_info=True)
        return _ACK

    # ── 赞 / 踩（自定义按钮 或 钉钉原生赞踩回调）──
    #    原生赞踩的 action/reason 字段名以 [CALLBACK RAW] 实测为准；届时把别名补进下面集合即可。
    _UP = ("upvote", "like", "thumbs_up", "good", "helpful")
    _DOWN = ("downvote", "dislike", "thumbs_down", "bad", "unhelpful")
    if action in _UP or action in _DOWN:
        norm = "upvote" if action in _UP else "downvote"
        # 官方赞踩模版"踩+提交"带 comment（自由文本原因）；有 comment 但没显式 reason 时记为 other。
        _reason = reason or ("other" if (norm == "downvote" and comment) else None)
        try:
            handle_feedback(message_id=message_id, user_id=user_id, user_name=user_name,
                            action=norm, reason=_reason, comment=comment)
            print(f"[CALLBACK DEBUG] 赞踩落库: message_id={message_id}, action={action}->{norm}, "
                  f"reason={_reason}, has_comment={bool(comment)}", flush=True)
        except Exception as e:
            logger.error("赞踩落库失败: %s", e, exc_info=True)
        return _ACK

    # 其它/未识别动作：已 ACK，不更新卡片（[CALLBACK RAW] 已记录原始 body 供排查 + 补别名）
    logger.info("卡片回调未识别 action=%s（已 ACK，不更新卡片）", action)
    return _ACK


def _rebuild_card_param_map(card_param_map: dict, message_id: str, context: str = "") -> None:
    """从 qa_session_log 重建卡片回调所需的完整字段。

    钉钉互动卡片的回调响应会覆盖整个 cardParamMap，因此每次回调都必须
    重新填充 question/answer/sources/meta/content_blocks 等字段。

    Args:
        card_param_map: 待填充的字典（就地修改）。
        message_id: 对应 qa_session_log 的 message_id。
        context: 日志上下文描述（用于区分调用来源）。
    """
    # 流式卡片的 sources / meta / 反馈按钮均以 is_answer_done=="true" 为可见性门控；钉钉回调
    # 响应会覆盖整个 cardParamMap，必须在此显式恢复，否则流式卡片在用户点击反馈后这些区域折叠。
    # 对成品卡片无害（其模板不引用该变量）。回调只在答案已生成完毕后触发，故恒为 "true"。
    card_param_map["is_answer_done"] = "true"
    _row_found = False
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT query_text, answer_text, cited_docs_json, model_name, latency_ms,
                           retrieval_latency_ms, llm_latency_ms, content_blocks_json
                    FROM fuling_operation.qa_session_log
                    WHERE message_id = %s LIMIT 1
                    """,
                    (message_id,),
                )
                row = cursor.fetchone()
                if row:
                    _row_found = True
                    card_param_map["question"] = row[0] or ""
                    card_param_map["title"] = (row[0] or "")[:50]
                    _answer = row[1] or ""
                    card_param_map["answer"] = _answer
                    # 重建参考来源行（cited_docs_json → "1. 标题"）
                    _src_lines = []
                    sources_json = row[2]
                    if sources_json:
                        try:
                            sources_list = json.loads(sources_json) if isinstance(sources_json, str) else sources_json
                            for i, s in enumerate(sources_list, 1):
                                if isinstance(s, dict):
                                    _src_lines.append(f"{i}. {s.get('title', s.get('doc_name', '未知文档'))}")
                                else:
                                    _src_lines.append(f"{i}. {s}")
                        except Exception:
                            _src_lines = []
                    model = row[3] or "unknown"
                    latency = row[4] or 0
                    ret_ms = row[5] or 0       # 检索阶段耗时
                    gen_ms = row[6] or 0       # LLM 生成阶段耗时（= 模型输出延迟）
                    # 流式卡(B2)：正文绑 content，版式 答案→📚来源→"模型 ｜ 检索·生成"灰色引用块（页脚落最底下、
                    # 紧挨按钮）；sources/meta 页脚置空，与 _stream_answer_to_card 定稿帧版式一致，避免反馈点击
                    # 后版式跳变（来源回到页脚、耗时挪位）。成品卡：正文绑 answer + 页脚 sources_text/meta（原逻辑）。
                    _streaming = bool(
                        get_config().rag.dingtalk_streaming
                        and os.environ.get("DINGTALK_STREAM_CARD_TEMPLATE_ID")
                    )
                    if _streaming:
                        _parts = [_answer]
                        if _src_lines:
                            _parts.append("📚 **参考来源**\n" + "\n".join(_src_lines))
                        _parts.append(f"> 模型: {model} ｜ 检索 {ret_ms / 1000:.1f}s · 生成 {gen_ms / 1000:.1f}s")
                        card_param_map["content"] = "\n\n".join(_parts)
                        card_param_map["sources_text"] = ""
                        card_param_map["sources"] = ""
                        card_param_map["meta"] = ""
                    else:
                        # 成品卡正文绑 answer；content 兜底写回避免被回调清空
                        card_param_map["content"] = _answer
                        _src_text = "\n".join(_src_lines)
                        card_param_map["sources_text"] = _src_text
                        card_param_map["sources"] = _src_text
                        card_param_map["meta"] = f"模型: {model} | 耗时: {latency / 1000:.1f}s"
                    card_param_map["message_id"] = message_id
                    # 重建 content_blocks（图文穿插数据）
                    content_blocks_json = row[7]
                    if content_blocks_json:
                        card_param_map["content_blocks"] = content_blocks_json if isinstance(content_blocks_json, str) else json.dumps(content_blocks_json, ensure_ascii=False)
                    else:
                        card_param_map["content_blocks"] = ""
        finally:
            conn.close()
    except Exception as e:
        debug_ctx = f"{context}重建卡片数据失败" if context else "重建卡片数据失败"
        print(f"[CALLBACK DEBUG] {debug_ctx}: {e}", flush=True)

    if not _row_found:
        # 兜底：qa_session_log 查无此 message_id（演示卡未落库 / RDS 异常 / message_id 不匹配）。
        # 钉钉回调响应会【覆盖整卡】cardParamMap → content/answer 留空就会整卡白屏。这里写入占位，
        # 保证至少不白屏，反馈按钮与"其他原因"表单仍可用。生产中每条回答都会 log_qa_session 落库，
        # 正常走不到这里；走到这里说明该卡未落库或 RDS 不可用。
        _placeholder = "✅ 已收到你的反馈。（原回答内容暂时无法重新载入，不影响本次反馈记录）"
        card_param_map.setdefault("content", _placeholder)
        card_param_map.setdefault("answer", _placeholder)
        card_param_map.setdefault("question", "")
        card_param_map.setdefault("title", "")
        card_param_map.setdefault("sources_text", "")
        card_param_map.setdefault("sources", "")
        card_param_map.setdefault("meta", "")
        card_param_map.setdefault("content_blocks", "")

