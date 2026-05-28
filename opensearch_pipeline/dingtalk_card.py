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
"""

import json
import logging
import os
import threading
import time
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

    # 检测是否有真正的 staffId（加密的 senderId 以 $:LWCP 开头，不能用于卡片 API）
    has_real_staff_id = bool(sender_staff_id) and not sender_staff_id.startswith("$:")

    # 构建 openSpaceId（区分单聊/群聊，SDK 要求小写）
    if conversation_type == "2":
        # 群聊
        open_space_id = f"dtv1.card//im_group.{conversation_id}"
    else:
        # 单聊（需要真实 staffId）
        if not has_real_staff_id:
            logger.warning("单聊模式下无真实 staffId，互动卡片发送跳过")
            return False
        open_space_id = f"dtv1.card//im_robot.{sender_staff_id}"

    # 卡片公有数据（所有值必须是 string 类型）
    sources_text = _format_sources_text(sources)
    meta = f"模型: {model} | 耗时: {latency_ms / 1000:.1f}s"

    card_param_map = {
        "title": question[:50],
        "question": question,
        "answer": answer,
        "sources": sources_text,
        "sources_text": sources_text,
        "meta": meta,
        "feedback_status": "",
    }

    # 私有数据和 userId 处理
    if has_real_staff_id:
        # 有真实 staffId：message_id 放私有数据
        private_data = {
            sender_staff_id: {
                "cardParamMap": {
                    "message_id": message_id,
                },
            },
        }
    else:
        # 无真实 staffId（调试模式）：message_id 放公有数据
        card_param_map["message_id"] = message_id
        private_data = {}

    payload: Dict[str, Any] = {
        "cardTemplateId": template_id,
        "outTrackId": message_id,
        "callbackType": "HTTP",
        "userIdType": 1,
        "cardData": {
            "cardParamMap": card_param_map,
        },
        "openSpaceId": open_space_id,
    }

    # 有私有数据时才加入
    if private_data:
        payload["privateData"] = private_data

    # 添加回调路由键
    if callback_route_key:
        payload["callbackRouteKey"] = callback_route_key

    # 投放模型（区分单聊/群聊）
    if conversation_type == "2":
        payload["imGroupOpenDeliverModel"] = {
            "robotCode": client_id,
        }
        payload["imGroupOpenSpaceModel"] = {
            "supportForward": True,
        }
    else:
        payload["userId"] = sender_staff_id
        payload["imRobotOpenDeliverModel"] = {
            "spaceType": "IM_ROBOT",
            "robotCode": client_id,
        }
        payload["imRobotOpenSpaceModel"] = {
            "supportForward": True,
        }

    try:
        print(f"[CARD DEBUG] openSpaceId={payload.get('openSpaceId')}", flush=True)
        print(f"[CARD DEBUG] cardParamMap={json.dumps(payload.get('cardData',{}).get('cardParamMap',{}), ensure_ascii=False)[:800]}", flush=True)
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
            # 检查投递结果（API 返回 200 不代表投递成功）
            resp_data = resp.json()
            deliver_results = resp_data.get("result", {}).get("deliverResults", [])
            all_delivered = all(d.get("success", False) for d in deliver_results) if deliver_results else False
            if all_delivered:
                logger.info("互动卡片发送成功: message_id=%s", message_id)
                return True
            else:
                error_msgs = [d.get("errorMsg", "") for d in deliver_results if not d.get("success")]
                logger.warning("互动卡片投递失败: %s", error_msgs)
                print(f"[CARD DEBUG] 投递失败: {error_msgs}", flush=True)
                return False
        else:
            logger.error(
                "互动卡片发送失败: status=%s, body=%s",
                resp.status_code, resp.text,
            )
            return False

    except Exception as e:
        logger.error("互动卡片发送异常: %s", e, exc_info=True)
        return False


# ═══════════════════════════════════════════════════════════════
# 卡片更新（反馈后替换按钮）
# ═══════════════════════════════════════════════════════════════

def update_card_feedback_status(
    message_id: str,
    feedback_status: str,
) -> bool:
    """
    更新卡片的 feedback_status 变量（将按钮替换为反馈确认文本）。

    PUT https://api.dingtalk.com/v1.0/card/instances

    Args:
        message_id: outTrackId（发送卡片时设定的）
        feedback_status: 例如 "✅ 已反馈：有帮助"

    Returns:
        True=更新成功, False=更新失败
    """
    token = _get_access_token()
    if not token:
        return False

    payload = {
        "outTrackId": message_id,
        "cardData": {
            "cardParamMap": {
                "feedback_status": feedback_status,
            },
        },
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
            logger.info("卡片状态更新成功: message_id=%s, status=%s", message_id, feedback_status)
            return True
        else:
            logger.error(
                "卡片状态更新失败: status=%s, body=%s",
                resp.status_code, resp.text,
            )
            return False

    except Exception as e:
        logger.error("卡片状态更新异常: %s", e, exc_info=True)
        return False
