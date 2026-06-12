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

ack 语义：一律 ACK OK（即使处理异常）。非 OK ack 会触发钉钉重投，而问答
处理重复执行 = 用户收到两份回答；处理内部本就 fail-open（落库失败不阻断
回复），与 webhook 路径"绝不把 500 回给钉钉"的哲学一致。

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

# 运行态：守护线程 + "已进入运行循环"标记。
# is_stream_active() 被 dingtalk_card 用来决定卡片 callbackType（STREAM/HTTP）——
# 客户端没起来时卡片必须回退 HTTP 回调，否则反馈按钮点击丢失。
_thread: Optional[threading.Thread] = None
_running = threading.Event()


def stream_mode_enabled() -> bool:
    """DINGTALK_STREAM_MODE 开关（默认关闭）。"""
    return os.environ.get("DINGTALK_STREAM_MODE", "").strip().lower() in ("1", "true", "yes")


def is_stream_active() -> bool:
    """Stream 客户端线程是否在运行（供卡片 callbackType 路由判断）。"""
    return _running.is_set() and _thread is not None and _thread.is_alive()


def start_stream_client() -> bool:
    """启动钉钉 Stream 客户端守护线程（幂等；服务启动时调用）。

    Returns:
        True  —— 客户端线程已在运行（本次启动或先前已启动）；
        False —— 未启动（开关关闭 / 凭证缺失 / SDK 未安装），服务继续以
                 HTTP 回调模式工作，不影响其余功能。
    """
    global _thread

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
            except Exception as e:
                # fail-open：处理失败也 ACK OK，杜绝重投导致的重复回答
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
