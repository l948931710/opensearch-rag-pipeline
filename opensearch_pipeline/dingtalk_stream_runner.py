# -*- coding: utf-8 -*-
"""
dingtalk_stream_runner.py — 钉钉 Stream 模式接入（出站 WSS，免公网回调端点）

为什么有这个模块：服务端无 HTTPS 时，HTTP 回调模式下员工问答内容明文过公网，
且必须维持一个公网入站端点。Stream 模式由本服务主动向钉钉网关建立出站 WSS
（TLS 加密），机器人消息与互动卡片回调都从这条连接推下来——公网入站端点可以
收紧甚至关闭（小程序上线前）。

复用既有同步核心（与 HTTP 回调路由共用，双模可并存、零断档切换）：
  - 机器人消息  → dingtalk_bot._process_webhook_body(callback.data)
                  Stream 推送的消息体与 webhook body 同构（含 sessionWebhook），
                  回复/卡片/落库链路零改动。
  - 卡片回调    → dingtalk_bot._process_card_callback_body(callback.data)
                  payload 同含 outTrackId/userId/content。配合 dingtalk_card
                  侧 callbackType=STREAM（见 _assemble_delivery_payload）。

开关：DINGTALK_STREAM_MODE=true 才启动（默认关）。

⚠️ 不要在本地开发时对生产应用开启此开关：Stream 是"连接分担"模型——同一
clientId 的所有在线连接共同分担消息推送。本地连上后会把生产用户的提问
"吃走"并用本地配置作答（HTTP 回调模式不存在此问题，钉钉只推注册的 URL）。
本地调试请使用独立的测试应用 clientId/Secret。

ack 语义（B7-P2-04，Sam 拍板 2026-07-18 二维分类=可重试性×发送确定性）：
  · 同步窗瞬态失败（TransientMessageError，attempts 未封顶）→ 返回
    STATUS_SYSTEM_EXCEPTION 请求钉钉重投——幂等由 dedup 四态机兜住
    （processing/sent/retryable_failed/final_failed + delivery_unknown）；
  · 永久错误/重试耗尽/发送后内部错误 → ACK OK（终态已收口，重投=空转或重复回答）；
  · 其余未知异常保守 ACK OK（旧哲学兜底：宁可静默有告警，不重复回答）。
答案计算在后台线程（ACK 早已返回）：发送层失败的主重试引擎=进程内自重试；
钉钉重投只在 ack 丢包时机会性出现，届时按 retryable_failed 复用已算答案重发。

环境变量：
  DINGTALK_STREAM_MODE     — true/1/yes 启用（默认关闭）
  DINGTALK_CLIENT_ID       — 应用 AppKey（与卡片 API 共用）
  DINGTALK_CLIENT_SECRET   — 应用 AppSecret（与卡片 API 共用）
"""

import asyncio
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 运行态：守护线程 + "已进入运行循环"标记 + 客户端引用（真实 WSS 连通探针用）。
# is_stream_active() 被 dingtalk_card 用来决定卡片 callbackType（STREAM/HTTP）——
# 客户端没起来时卡片必须回退 HTTP 回调，否则反馈按钮点击丢失。
_thread: Optional[threading.Thread] = None
_running = threading.Event()
_client = None   # dingtalk_stream.DingTalkStreamClient（start_stream_client 内赋值）


def stream_mode_enabled() -> bool:
    """DINGTALK_STREAM_MODE 开关（默认关闭）。"""
    return os.environ.get("DINGTALK_STREAM_MODE", "").strip().lower() in ("1", "true", "yes")


def _retry_ack_status(sdk_mod):
    """B7-P2-04：请求钉钉重投的 ack 状态。SDK 无专用 LATER——用 STATUS_SYSTEM_EXCEPTION
    （服务端瞬态语义，非 OK 即触发重投）；SDK 形态变化取不到时保守退回 STATUS_OK
    （宁可放弃这次重投也不 AttributeError 打断事件循环）。"""
    return getattr(sdk_mod.AckMessage, "STATUS_SYSTEM_EXCEPTION",
                   sdk_mod.AckMessage.STATUS_OK)


def is_stream_active() -> bool:
    """Stream 通道是否**真实连通**（供卡片 callbackType 路由判断）。

    批次7（ultra dingtalk_stream_runner:140）：此前只看线程活性——_running 在
    start_forever 建立 WSS **之前**就已置位，且 start_forever 内部 3s 退避重连的
    断线窗里线程照样存活：这些窗口里按 STREAM 发出的卡片，按钮点击会静默丢失
    （_assemble_delivery_payload 自己的注释警告过）。SDK 的 client.websocket 在
    连接建立后才赋值（断线后残留已关闭对象）——据此探真实连通：
    websocket 为 None（首连未成）或已关闭（重连窗）都判不活 → 卡片回退 HTTP 回调
    （保守方向：宁走 HTTP 也不丢点击）。SDK 内部形态变化时探不出属性 → 按不活处理。"""
    if not (_running.is_set() and _thread is not None and _thread.is_alive()):
        return False
    ws = getattr(_client, "websocket", None) if _client is not None else None
    if ws is None:
        return False                       # 尚未完成首次 WSS 建连（或 SDK 形态变化）
    closed = getattr(ws, "closed", None)   # websockets 旧版属性
    if closed is not None:
        return not bool(closed)
    return getattr(ws, "close_code", None) is None   # 新版：close_code 非 None=已关


def start_stream_client() -> bool:
    """启动钉钉 Stream 客户端守护线程（幂等；服务启动时调用）。

    Returns:
        True  —— 客户端线程已在运行（本次启动或先前已启动）；
        False —— 未启动（开关关闭 / 凭证缺失 / SDK 未安装），服务继续以
                 HTTP 回调模式工作，不影响其余功能。
    """
    global _thread, _client

    if not stream_mode_enabled():
        logger.debug("DINGTALK_STREAM_MODE 未开启，跳过 Stream 客户端启动")
        return False

    if _thread is not None and _thread.is_alive():
        return True

    client_id = os.environ.get("DINGTALK_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DINGTALK_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        logger.error(
            "DINGTALK_STREAM_MODE 已开启但 DINGTALK_CLIENT_ID/SECRET 未配置，"
            "Stream 客户端不启动（回退 HTTP 回调模式）"
        )
        return False

    try:
        import dingtalk_stream
    except ImportError:
        logger.error(
            "DINGTALK_STREAM_MODE 已开启但 dingtalk-stream 未安装"
            "（pip install dingtalk-stream），回退 HTTP 回调模式"
        )
        return False

    # 延迟导入：避免 runner ↔ dingtalk_card 的环（card 按 is_stream_active 选 callbackType）
    from opensearch_pipeline.dingtalk_bot import (
        TransientMessageError,
        _process_card_callback_body,
        _process_webhook_body,
    )

    class _BotMessageHandler(dingtalk_stream.CallbackHandler):
        """机器人消息：payload 与 webhook body 同构，直接喂给共享同步核心。

        同步核心含阻塞 I/O（ack 文本回复 / DB），丢线程池执行，
        不能阻塞 Stream 客户端的事件循环（心跳会断）。
        """

        async def process(self, callback):
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, _process_webhook_body, callback.data
                )
            except TransientMessageError as e:
                # B7-P2-04：同步窗瞬态（attempts 未封顶）→ 非 OK ack 请求钉钉重投；
                # 幂等由 dedup 四态机兜住（重投按 retryable_failed 路由，不重复回答）
                logger.warning("Stream 消息瞬态失败——请求钉钉重投: %s", e)
                return _retry_ack_status(dingtalk_stream), "RETRY"
            except Exception as e:
                # 永久/耗尽/未知兜底：终态已在 dedup 收口，ACK OK 不空转重投
                logger.error("Stream 机器人消息处理异常: %s", e, exc_info=True)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    class _CardCallbackHandler(dingtalk_stream.CallbackHandler):
        """互动卡片回调：ACK-only 语义不变（响应不带 cardData → 不触发重渲染白屏）。"""

        async def process(self, callback):
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, _process_card_callback_body, callback.data
                )
            except Exception as e:
                logger.error("Stream 卡片回调处理异常: %s", e, exc_info=True)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    credential = dingtalk_stream.Credential(client_id, client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    _client = client   # 批次7：暴露给 is_stream_active 做真实 WSS 连通探针
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC, _BotMessageHandler()
    )
    client.register_callback_handler(
        dingtalk_stream.Card_Callback_Router_Topic, _CardCallbackHandler()
    )

    def _run() -> None:
        _running.set()
        try:
            # start_forever 内置断线重连（3s 退避循环），正常情况下永不返回
            client.start_forever()
        except Exception as e:
            logger.error("钉钉 Stream 客户端退出: %s", e, exc_info=True)
        finally:
            _running.clear()

    _thread = threading.Thread(target=_run, name="dingtalk-stream", daemon=True)
    _thread.start()
    logger.info(
        "钉钉 Stream 客户端已启动（topics: 机器人消息 + 卡片回调）；"
        "控制台切换推送模式前请先确认本日志出现在新版本实例上"
    )
    return True
