# -*- coding: utf-8 -*-
"""
dingtalk_card.py — 钉钉互动卡片发送模块

通过钉钉开放平台 API 发送带反馈按钮的互动卡片。
使用 HTTP 回调模式接收用户点击事件。

环境变量：
  DINGTALK_CLIENT_ID              — 应用 AppKey
  DINGTALK_CLIENT_SECRET          — 应用 AppSecret
  DINGTALK_CARD_TEMPLATE_ID       — 卡片模板 ID（在 card.dingtalk.com 创建）
  DINGTALK_CARD_CALLBACK_ROUTE_KEY — 回调路由键（注册回调时自定义）
  DINGTALK_CARD_CALLBACK_URL       — 卡片回调公网地址（启动时自动注册到上面的 route key；
                                     不配则反馈按钮点击无法回调，但不影响答案/打字机）
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Access Token 管理（缓存 + 过期刷新）
# ═══════════════════════════════════════════════════════════════

_token_lock = threading.Lock()
_cached_token: Optional[str] = None
_token_expires_at: float = 0


def _get_access_token() -> Optional[str]:
    """
    获取钉钉 access_token（带缓存，过期前 5 分钟自动刷新）。

    POST https://api.dingtalk.com/v1.0/oauth2/accessToken
    """
    global _cached_token, _token_expires_at

    with _token_lock:
        # 缓存有效（提前 5 分钟刷新）
        if _cached_token and time.time() < _token_expires_at - 300:
            return _cached_token

        client_id = os.environ.get("DINGTALK_CLIENT_ID", "")
        client_secret = os.environ.get("DINGTALK_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            logger.warning("DINGTALK_CLIENT_ID 或 DINGTALK_CLIENT_SECRET 未配置")
            return None

        try:
            resp = requests.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": client_id, "appSecret": client_secret},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error("获取 access_token 失败: status=%s, body=%s", resp.status_code, resp.text)
                return None

            data = resp.json()
            _cached_token = data.get("accessToken")
            expire_in = data.get("expireIn", 7200)
            _token_expires_at = time.time() + expire_in

            logger.info("access_token 刷新成功, expires_in=%ds", expire_in)
            return _cached_token

        except Exception as e:
            logger.error("获取 access_token 异常: %s", e, exc_info=True)
            return None


# ═══════════════════════════════════════════════════════════════
# 互动卡片 HTTP 回调地址注册（启动时调用）
# ═══════════════════════════════════════════════════════════════

def register_card_callback(*, force_update: Optional[bool] = None) -> bool:
    """注册互动卡片 HTTP 回调地址（callbackRouteKey → callbackUrl），供服务启动时调用。

    钉钉互动卡片按钮回调需要先把服务的回调 URL 注册到 callbackRouteKey 上，否则用户点击
    反馈按钮（有帮助/没帮助/其他）时钉钉不知道往哪发，点击无人接收。本函数把
    DINGTALK_CARD_CALLBACK_URL 注册到 DINGTALK_CARD_CALLBACK_ROUTE_KEY。

    POST https://api.dingtalk.com/v1.0/card/callbacks/register

    幂等：首次插入用 forceUpdate=false；改地址时设 DINGTALK_CARD_CALLBACK_FORCE_UPDATE=true
    （或传 force_update=True）覆盖。未配置回调 URL / 无 access_token 时跳过；失败只记日志、
    返回 False，绝不抛出（注册失败不应阻断服务启动，仅反馈按钮回调暂不可用，答案/打字机不受影响）。

    Returns:
        True=注册成功；False=跳过或失败（非致命）。
    """
    callback_url = os.environ.get("DINGTALK_CARD_CALLBACK_URL", "").strip()
    route_key = os.environ.get("DINGTALK_CARD_CALLBACK_ROUTE_KEY", "").strip()
    if not callback_url or not route_key:
        logger.info(
            "未配置 DINGTALK_CARD_CALLBACK_URL/ROUTE_KEY，跳过卡片回调注册"
            "（答案/打字机不受影响，仅反馈按钮点击暂无法回调）"
        )
        return False

    token = _get_access_token()
    if not token:
        logger.warning("无 access_token，卡片回调注册跳过")
        return False

    if force_update is None:
        force_update = os.environ.get("DINGTALK_CARD_CALLBACK_FORCE_UPDATE", "").lower() in ("true", "1", "yes")
    # apiSecret：钉钉用它对回调请求签名；当前 /card/callback 处理器未校验来源，可自定义任意值。
    api_secret = os.environ.get("DINGTALK_CARD_CALLBACK_API_SECRET", "fuling_card_cb")

    payload = {
        "apiSecret": api_secret,
        "callbackUrl": callback_url,
        "callbackRouteKey": route_key,
        "forceUpdate": force_update,
    }
    try:
        resp = requests.post(
            "https://api.dingtalk.com/v1.0/card/callbacks/register",
            json=payload,
            headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("卡片回调注册成功: routeKey=%s -> %s", route_key, callback_url)
            return True
        # 已存在且未 forceUpdate 时钉钉会返回非 200，属正常（无需每次都改地址）。
        logger.warning(
            "卡片回调注册未成功（若提示已存在可忽略；改地址请设 "
            "DINGTALK_CARD_CALLBACK_FORCE_UPDATE=true 后重启）: status=%s, body=%s",
            resp.status_code, resp.text[:300],
        )
        return False
    except Exception as e:
        logger.error("卡片回调注册异常: %s", e, exc_info=True)
        return False


# ═══════════════════════════════════════════════════════════════
# 互动卡片发送
# ═══════════════════════════════════════════════════════════════

def _format_sources_text(sources: List[Dict[str, Any]]) -> str:
    """将 sources 列表格式化为纯文本。"""
    if not sources:
        return ""
    lines = []
    seen_titles = set()
    for i, src in enumerate(sources, 1):
        title = src.get("title", "未知文档")
        if title in seen_titles:
            continue
        seen_titles.add(title)
        section = src.get("section", "")
        score = src.get("score", 0)
        line = f"{i}. {title}"
        if section:
            line += f" > {section}"
        if isinstance(score, (int, float)):
            line += f"（相关度 {score:.2f}）"
        lines.append(line)
    return "\n".join(lines)


import re

# 匹配 LLM 在 answer 末尾追加的参考来源段落标题
_TRAILING_SOURCES_PATTERN = re.compile(
    r'\n\s*[-—─]{2,}\s*\n'              # --- 分隔线 + 后续内容
    r'|'
    r'\n\s*\*{0,2}参考(?:来源|文档|资料)\s*[:：]\s*\*{0,2}\s*\n'  # **参考来源：** 或 参考文档：
    r'|'
    r'\n\s*(?:参考(?:来源|文档|资料)|引用来源|来源信息)\s*[:：]\s*\n'  # 参考来源：
    r'|'
    r'\n\s*(?:---+|===+)\s*$'            # 文末的分隔线
    ,
    re.MULTILINE
)


def _strip_trailing_sources(answer: str) -> str:
    """去除 LLM answer 末尾的参考来源段落（卡片有独立 sources 区域）。

    LLM 有时会在回答末尾输出类似：
      ---
      **参考来源：**
      - 《文档A》 > 章节1
      - 《文档B》 > 章节2

    这些内容与卡片 sources 区域重复，且占用大量空白，需要剥除。
    """
    if not answer:
        return answer

    match = _TRAILING_SOURCES_PATTERN.search(answer)
    if match:
        # 只截取匹配位置之前的内容
        cleaned = answer[:match.start()].rstrip()
        # 安全检查：确保不会把 answer 截没了（至少保留 5 字符）
        if len(cleaned) >= 5:
            return cleaned

    return answer.rstrip()


def _inline_images_to_markdown(
    answer: str,
    content_blocks: Optional[List[Dict[str, str]]],
) -> str:
    """将 content_blocks 中的图片以 Markdown 语法内联到 answer 中。

    钉钉互动卡片的 Markdown 组件支持 ![alt](url) 图片语法。
    将图片直接嵌入 answer 文本，可以不依赖卡片模板的 Loop/Image 组件。

    策略：
    - 如果 content_blocks 包含图文穿插数据（type=markdown + type=image），
      直接拼接成一段完整的 Markdown 文本
    - 如果没有 content_blocks 或没有图片，返回原始 answer
    """
    if not content_blocks:
        return answer

    # 检查是否有图片块
    has_images = any(b.get("type") == "image" for b in content_blocks)
    if not has_images:
        return answer

    # 将 content_blocks 拼接为 Markdown 文本
    parts = []
    for block in content_blocks:
        block_type = block.get("type", "")
        if block_type == "markdown":
            parts.append(block.get("content", ""))
        elif block_type == "image":
            url = block.get("url", "")
            caption = block.get("caption", "图片")
            title = block.get("title", "")
            if url:
                # 钉钉 Markdown 图片语法
                parts.append(f"\n\n![{title or caption}]({url})\n")
                if caption and caption != title:
                    parts.append(f"*{caption}*\n")

    return "\n".join(parts).strip()


def _assemble_delivery_payload(
    *,
    template_id: str,
    out_track_id: str,
    card_param_map: Dict[str, Any],
    conversation_id: str,
    conversation_type: str,
    sender_staff_id: str,
    client_id: str,
    callback_route_key: str = "",
) -> Optional[Dict[str, Any]]:
    """组装 createAndDeliver 投放载荷（openSpaceId + 私有数据 message_id + 单聊/群聊投放模型）。

    成品卡片与流式卡片共用此投放逻辑。返回 None 表示无法投放（如单聊缺少真实 staffId），
    调用方应降级。注意：无真实 staffId 的调试场景会就地把 message_id 写入 card_param_map。
    """
    # 检测是否有真正的 staffId（加密的 senderId 以 $:LWCP 开头，不能用于卡片 API）
    has_real_staff_id = bool(sender_staff_id) and not sender_staff_id.startswith("$:")

    # 构建 openSpaceId（区分单聊/群聊，SDK 要求小写）
    if conversation_type == "2":
        open_space_id = f"dtv1.card//im_group.{conversation_id}"
    else:
        if not has_real_staff_id:
            logger.warning("单聊模式下无真实 staffId，卡片投放跳过")
            return None
        open_space_id = f"dtv1.card//im_robot.{sender_staff_id}"

    # message_id：有真实 staffId 放私有数据，否则放公有数据（调试模式）
    if has_real_staff_id:
        private_data = {sender_staff_id: {"cardParamMap": {"message_id": out_track_id}}}
    else:
        card_param_map["message_id"] = out_track_id
        private_data = {}

    payload: Dict[str, Any] = {
        "cardTemplateId": template_id,
        "outTrackId": out_track_id,
        "callbackType": "HTTP",
        "userIdType": 1,
        "cardData": {"cardParamMap": card_param_map},
        "openSpaceId": open_space_id,
    }

    if private_data:
        payload["privateData"] = private_data
    if callback_route_key:
        payload["callbackRouteKey"] = callback_route_key

    # 投放模型（区分单聊/群聊）
    if conversation_type == "2":
        payload["imGroupOpenDeliverModel"] = {"robotCode": client_id}
        payload["imGroupOpenSpaceModel"] = {"supportForward": True}
    else:
        payload["userId"] = sender_staff_id
        payload["imRobotOpenDeliverModel"] = {"spaceType": "IM_ROBOT", "robotCode": client_id}
        payload["imRobotOpenSpaceModel"] = {"supportForward": True}

    return payload


def _post_card_deliver(token: str, payload: Dict[str, Any], message_id: str) -> bool:
    """POST createAndDeliver 并校验投递结果（200 不代表投递成功）。"""
    try:
        print(f"[CARD DEBUG] openSpaceId={payload.get('openSpaceId')}", flush=True)
        print(f"[CARD DEBUG] cardParamMap={json.dumps(payload.get('cardData', {}).get('cardParamMap', {}), ensure_ascii=False)[:800]}", flush=True)
        resp = requests.post(
            "https://api.dingtalk.com/v1.0/card/instances/createAndDeliver",
            json=payload,
            headers={
                "x-acs-dingtalk-access-token": token,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        print(f"[CARD DEBUG] response status={resp.status_code}, body={resp.text[:500]}", flush=True)
        if resp.status_code == 200:
            resp_data = resp.json()
            deliver_results = resp_data.get("result", {}).get("deliverResults", [])
            all_delivered = all(d.get("success", False) for d in deliver_results) if deliver_results else False
            if all_delivered:
                logger.info("互动卡片发送成功: message_id=%s", message_id)
                return True
            error_msgs = [d.get("errorMsg", "") for d in deliver_results if not d.get("success")]
            logger.warning("互动卡片投递失败: %s", error_msgs)
            print(f"[CARD DEBUG] 投递失败: {error_msgs}", flush=True)
            return False
        logger.error("互动卡片发送失败: status=%s, body=%s", resp.status_code, resp.text)
        return False
    except Exception as e:
        logger.error("互动卡片发送异常: %s", e, exc_info=True)
        return False


def send_interactive_card(
    *,
    conversation_id: str,
    conversation_type: str,
    sender_staff_id: str,
    message_id: str,
    question: str,
    answer: str,
    sources: List[Dict[str, Any]],
    latency_ms: int,
    model: str,
    content_blocks: Optional[List[Dict[str, str]]] = None,
) -> bool:
    """
    发送互动卡片（带反馈按钮）。

    Args:
        conversation_id: 钉钉会话 ID
        conversation_type: '1'=单聊, '2'=群聊
        sender_staff_id: 用户 staffId
        message_id: 本次回答的唯一 ID（作为 outTrackId）
        question: 用户原始问题
        answer: RAG 回答正文
        sources: 引用来源列表
        latency_ms: 总耗时(ms)
        model: 模型名称

    Returns:
        True=发送成功, False=发送失败（调用方应降级为 Markdown）
    """
    token = _get_access_token()
    if not token:
        logger.warning("无 access_token，互动卡片发送跳过")
        return False

    template_id = os.environ.get("DINGTALK_CARD_TEMPLATE_ID", "")
    callback_route_key = os.environ.get("DINGTALK_CARD_CALLBACK_ROUTE_KEY", "")
    client_id = os.environ.get("DINGTALK_CLIENT_ID", "")

    if not template_id:
        logger.warning("DINGTALK_CARD_TEMPLATE_ID 未配置，互动卡片发送跳过")
        return False

    # 卡片公有数据（所有值必须是 string 类型）
    sources_text = _format_sources_text(sources)
    meta = f"模型: {model} | 耗时: {latency_ms / 1000:.1f}s"

    # 防御性清理：LLM 可能仍在 answer 末尾输出参考来源，
    # 卡片已有独立的 sources 区域，需要去重避免大片空白
    clean_answer = _strip_trailing_sources(answer)
    # 清理 <<IMG:N>> / <IMG:N> 占位符
    clean_answer = re.sub(r'<{1,2}IMG:\d+>{1,2}', '', clean_answer).strip()

    # 当有 content_blocks 图文穿插时，answer 置空避免内容重复显示
    # （模板条件可见性不可靠，从代码端强制互斥）
    display_answer = "" if content_blocks else clean_answer

    card_param_map = {
        "title": question[:50],
        "question": question,
        "answer": display_answer,
        "content_blocks": json.dumps(content_blocks, ensure_ascii=False) if content_blocks else "",
        "sources": sources_text,
        "sources_text": sources_text,
        "meta": meta,
        "feedback_status": "",
    }

    payload = _assemble_delivery_payload(
        template_id=template_id,
        out_track_id=message_id,
        card_param_map=card_param_map,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        sender_staff_id=sender_staff_id,
        client_id=client_id,
        callback_route_key=callback_route_key,
    )
    if payload is None:
        return False
    return _post_card_deliver(token, payload, message_id)


# ═══════════════════════════════════════════════════════════════
# 卡片更新（反馈后替换按钮）
# ═══════════════════════════════════════════════════════════════

def update_card_data(message_id: str, card_param_map: Dict[str, str]) -> bool:
    """通用：更新卡片公有数据变量（PUT https://api.dingtalk.com/v1.0/card/instances）。

    用于反馈状态切换、流式完成后置位 is_answer_done（触发反馈按钮显示）等。
    失败只记日志、返回 False，绝不抛出。

    Args:
        message_id: outTrackId（发送卡片时设定的）
        card_param_map: 要覆盖写入的公有变量，如 {"feedback_status": "..."} 或 {"is_answer_done": "true"}
    """
    token = _get_access_token()
    if not token:
        return False

    payload = {
        "outTrackId": message_id,
        "cardData": {"cardParamMap": card_param_map},
    }

    try:
        resp = requests.put(
            "https://api.dingtalk.com/v1.0/card/instances",
            json=payload,
            headers={
                "x-acs-dingtalk-access-token": token,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("卡片数据更新成功: message_id=%s, keys=%s", message_id, list(card_param_map.keys()))
            return True
        logger.error("卡片数据更新失败: status=%s, body=%s", resp.status_code, resp.text)
        return False
    except Exception as e:
        logger.error("卡片数据更新异常: %s", e, exc_info=True)
        return False


def update_card_feedback_status(
    message_id: str,
    feedback_status: str,
) -> bool:
    """更新卡片的 feedback_status 变量（将按钮替换为反馈确认文本）。

    PUT https://api.dingtalk.com/v1.0/card/instances（委托给 update_card_data）。
    """
    return update_card_data(message_id, {"feedback_status": feedback_status})


# ═══════════════════════════════════════════════════════════════
# 流式 AI 卡片（打字机效果）
# ═══════════════════════════════════════════════════════════════

def create_streaming_card(
    *,
    conversation_id: str,
    conversation_type: str,
    sender_staff_id: str,
    message_id: str,
    question: str,
    sources: List[Dict[str, Any]],
    model: str,
    stream_key: str = "content",
) -> bool:
    """投放一张流式 AI 卡片占位（流式变量初始为空，随后由 streaming_update_card 逐步填充）。

    使用 DINGTALK_STREAM_CARD_TEMPLATE_ID 指向的流式卡片模板（需在钉钉卡片平台预先注册，
    含一个绑定 stream_key 的流式文本组件，以及与成品卡片一致的反馈按钮 / sources / meta 变量）。
    sources / meta / question 在生成前即已知，故一并设置。

    Returns:
        True=投放成功（可开始流式更新）, False=失败（调用方应降级为非流式成品卡片路径）。
    """
    token = _get_access_token()
    if not token:
        logger.warning("无 access_token，流式卡片发送跳过")
        return False

    template_id = os.environ.get("DINGTALK_STREAM_CARD_TEMPLATE_ID", "")
    callback_route_key = os.environ.get("DINGTALK_CARD_CALLBACK_ROUTE_KEY", "")
    client_id = os.environ.get("DINGTALK_CLIENT_ID", "")

    if not template_id:
        logger.warning("DINGTALK_STREAM_CARD_TEMPLATE_ID 未配置，流式卡片发送跳过")
        return False

    card_param_map = {
        "title": question[:50],
        "question": question,
        stream_key: "",
        # B2 版式：参考来源 + "模型 ｜ 耗时" 均由定稿帧拼进 content（答案→来源→耗时，耗时落最底下、
        # 紧挨按钮）。故 sources/meta 页脚此处置空，避免与正文里的来源/耗时重复（用 update_card_data
        # 写页脚会触发重渲染闪烁，故一律改走 content 流式写）。
        "sources": "",
        "sources_text": "",
        "meta": "",
        "feedback_status": "",
        # 反馈按钮以 is_answer_done=="true" 为可见性门控：初始为空 → 流式期间隐藏；
        # _stream_answer_to_card 完成后置 "true" 显示。（需模板已声明 is_answer_done 变量）
        "is_answer_done": "",
    }

    payload = _assemble_delivery_payload(
        template_id=template_id,
        out_track_id=message_id,
        card_param_map=card_param_map,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        sender_staff_id=sender_staff_id,
        client_id=client_id,
        callback_route_key=callback_route_key,
    )
    if payload is None:
        return False
    return _post_card_deliver(token, payload, message_id)


def streaming_update_card(
    out_track_id: str,
    content: str,
    *,
    key: str = "content",
    is_full: bool = True,
    is_finalize: bool = False,
    is_error: bool = False,
) -> bool:
    """流式更新 AI 卡片内容（打字机效果）。

    PUT https://api.dingtalk.com/v1.0/card/streaming

    guid 每帧自动生成（对齐官方 dingtalk-stream SDK 的 `AICardReplier.streaming`：流式会话由
    outTrackId 关联，guid 只是每次请求的唯一标识，须是带连字符的标准 UUID 串、且每帧不同；
    早先版本复用同一个无连字符 uuid4().hex 会触发钉钉 500 unknownError）。

    Args:
        out_track_id: 卡片 outTrackId（= message_id）
        content: 本次写入的内容
        key: 流式卡片模板中绑定的流式变量名
        is_full: True=content 为累计全文（覆盖式，对拆分标记更稳健）；False=增量追加
        is_finalize: True=最后一帧，结束流式
        is_error: True=以错误态结束

    Returns:
        True=更新成功, False=更新失败（非致命，调用方可继续或降级）。
    """
    token = _get_access_token()
    if not token:
        return False

    payload = {
        "outTrackId": out_track_id,
        "guid": str(uuid.uuid4()),  # 每帧新 guid、标准 UUID 串（对齐官方 SDK；复用/无连字符会 500）
        "key": key,
        "content": content,
        "isFull": is_full,
        "isFinalize": is_finalize,
        "isError": is_error,
    }

    try:
        resp = requests.put(
            "https://api.dingtalk.com/v1.0/card/streaming",
            json=payload,
            headers={
                "x-acs-dingtalk-access-token": token,
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.error("流式卡片更新失败: status=%s, body=%s", resp.status_code, resp.text)
        return False
    except Exception as e:
        logger.error("流式卡片更新异常: %s", e, exc_info=True)
        return False


def send_text_to_user(staff_id: str, text: str) -> bool:
    """机器人主动给某个用户发一条纯文本（1 对 1）。

    用于卡片【回调】里给用户发提示（回调请求里没有 sessionWebhook，无法走 _send_text_reply）。
    例如：点「转人工」后回「已为你转人工」、点「补充原因」后回「请直接回复本条消息」。
    需应用具备「机器人发送单聊消息」权限（Robot.Message.Send / 单聊消息）；失败 fail open，不抛。
    """
    token = _get_access_token()
    if not token or not staff_id or not text:
        return False
    robot_code = os.environ.get("DINGTALK_CLIENT_ID", "")
    try:
        resp = requests.post(
            "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
            json={
                "robotCode": robot_code,
                "userIds": [staff_id],
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": text}, ensure_ascii=False),
            },
            headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning("send_text_to_user 失败: status=%s, body=%s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.warning("send_text_to_user 异常: %s", e)
        return False
