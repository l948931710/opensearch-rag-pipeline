# -*- coding: utf-8 -*-
"""
test_dingtalk_stream_runner.py — 钉钉 Stream 模式接入测试

覆盖：
  1. 开关/凭证门控（默认关闭、缺凭证不启动、幂等）
  2. SDK 缺失时优雅回退（不抛、返回 False）
  3. 客户端装配（两个 topic 注册、守护线程、is_stream_active）
  4. 消息处理器语义（callback.data 直通共享核心；异常仍 ACK OK——杜绝重投重答）
  5. 卡片投放 callbackType 路由（Stream 活跃→STREAM 且不带 routeKey；否则 HTTP+routeKey）
"""

import asyncio
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

from opensearch_pipeline import dingtalk_stream_runner as runner


# ═══════════════════════════════════════════════════════════════
# 辅助：伪 dingtalk_stream SDK
# ═══════════════════════════════════════════════════════════════

class _FakeClient:
    """记录注册与启动调用；start_forever 阻塞直到 release 被 set（模拟常驻连接）。"""

    instances = []

    def __init__(self, credential):
        self.credential = credential
        self.handlers = {}
        # started/release 的 .wait() 与线程 .join() 在下面测试里用 30s 上限：正常瞬间就绪，
        # 但 xdist 并行(-n auto)下 10 个 worker 争 CPU，真实线程偶发在旧的 5s 内没 set → 误判失败
        # （perf F#48 并行化后暴露；放宽上限只影响慢路径余量，不拖慢正常用例）。
        self.started = threading.Event()
        self.release = threading.Event()
        # 批次7：真实 SDK 在 WSS 建连后才赋值 websocket（is_stream_active 据此探连通）；
        # 假件模拟"已连接"形态，断线窗用例自行改写本属性。
        self.websocket = None
        _FakeClient.instances.append(self)

    def register_callback_handler(self, topic, handler):
        self.handlers[topic] = handler

    def start_forever(self):
        self.websocket = types.SimpleNamespace(closed=False)   # 模拟 WSS 建连成功
        self.started.set()
        self.release.wait(timeout=10)


def _make_fake_sdk():
    mod = types.ModuleType("dingtalk_stream")

    class CallbackHandler:
        pass

    class AckMessage:
        STATUS_OK = "SUCCESS"
        STATUS_SYSTEM_EXCEPTION = "SYSTEM_EXCEPTION"

    class ChatbotMessage:
        TOPIC = "/v1.0/im/bot/messages/get"

    class Credential:
        def __init__(self, client_id, client_secret):
            self.client_id = client_id
            self.client_secret = client_secret

    mod.CallbackHandler = CallbackHandler
    mod.AckMessage = AckMessage
    mod.ChatbotMessage = ChatbotMessage
    mod.Credential = Credential
    mod.Card_Callback_Router_Topic = "/v1.0/card/instances/callback"
    mod.DingTalkStreamClient = _FakeClient
    return mod


@pytest.fixture
def fake_sdk(monkeypatch):
    """以伪 SDK 顶替 dingtalk_stream，并重置 runner 模块态。"""
    mod = _make_fake_sdk()
    _FakeClient.instances = []
    monkeypatch.setitem(sys.modules, "dingtalk_stream", mod)
    monkeypatch.setattr(runner, "_thread", None)
    runner._running.clear()
    yield mod
    # 释放阻塞中的伪客户端线程，避免悬挂线程跨测试。
    # ⚠️ 必须 join 后再 clear（根治 F#48 偶发 flake）：_run() 的 finally 会 _running.clear()，
    # 而它在 release.set() 后【异步】执行。若不 join，前一个测试的线程可能在【下一个】测试
    # 调 _running.set() 之【后】才跑 finally → 把新测试刚设的共享 _running 抹掉 → 下个测试
    # is_stream_active()（=_running.is_set() and _thread.is_alive()）偶发读到 False。
    # join 让线程的 finally 在本测试 teardown 内跑完，绝不泄漏到下一个测试。
    for c in _FakeClient.instances:
        c.release.set()
    t = runner._thread
    if t is not None:
        t.join(timeout=30)
    runner._running.clear()
    runner._thread = None


def _enable(monkeypatch):
    monkeypatch.setenv("DINGTALK_STREAM_MODE", "true")
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "test_client_id")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "test_client_secret")


class _FakeCallback:
    def __init__(self, data):
        self.data = data


# ═══════════════════════════════════════════════════════════════
# 1/2. 门控与回退
# ═══════════════════════════════════════════════════════════════

class TestGating:
    def test_disabled_by_default(self, monkeypatch, fake_sdk):
        monkeypatch.delenv("DINGTALK_STREAM_MODE", raising=False)
        assert runner.stream_mode_enabled() is False
        assert runner.start_stream_client() is False
        assert runner.is_stream_active() is False

    def test_missing_credentials_no_start(self, monkeypatch, fake_sdk):
        monkeypatch.setenv("DINGTALK_STREAM_MODE", "true")
        monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
        monkeypatch.delenv("DINGTALK_CLIENT_SECRET", raising=False)
        assert runner.start_stream_client() is False
        assert runner.is_stream_active() is False

    def test_sdk_missing_graceful_fallback(self, monkeypatch):
        """SDK 未安装：不抛异常，返回 False（HTTP 回调模式继续可用）。"""
        _enable(monkeypatch)
        monkeypatch.setattr(runner, "_thread", None)
        runner._running.clear()
        monkeypatch.setitem(sys.modules, "dingtalk_stream", None)  # import → ImportError
        assert runner.start_stream_client() is False
        assert runner.is_stream_active() is False

    def test_idempotent_start(self, monkeypatch, fake_sdk):
        _enable(monkeypatch)
        assert runner.start_stream_client() is True
        client = _FakeClient.instances[-1]
        assert client.started.wait(timeout=30)
        assert runner.start_stream_client() is True  # 二次调用不再建新客户端
        assert len(_FakeClient.instances) == 1


# ═══════════════════════════════════════════════════════════════
# 3. 客户端装配
# ═══════════════════════════════════════════════════════════════

class TestAssembly:
    def test_registers_both_topics_and_runs(self, monkeypatch, fake_sdk):
        _enable(monkeypatch)
        assert runner.start_stream_client() is True
        client = _FakeClient.instances[-1]
        assert client.started.wait(timeout=30)
        assert set(client.handlers) == {
            "/v1.0/im/bot/messages/get",
            "/v1.0/card/instances/callback",
        }
        assert client.credential.client_id == "test_client_id"
        assert runner.is_stream_active() is True
        # 守护线程：进程退出不被 Stream 线程拖住
        assert runner._thread.daemon is True

    def test_inactive_after_client_exit(self, monkeypatch, fake_sdk):
        _enable(monkeypatch)
        runner.start_stream_client()
        client = _FakeClient.instances[-1]
        client.started.wait(timeout=30)
        client.release.set()  # start_forever 返回 → 线程收尾
        runner._thread.join(timeout=30)
        assert runner.is_stream_active() is False


# ═══════════════════════════════════════════════════════════════
# 4. 处理器语义
# ═══════════════════════════════════════════════════════════════

class TestHandlers:
    def _start_and_get_handlers(self, monkeypatch, fake_sdk):
        _enable(monkeypatch)
        assert runner.start_stream_client() is True
        client = _FakeClient.instances[-1]
        assert client.started.wait(timeout=30)
        return (
            client.handlers["/v1.0/im/bot/messages/get"],
            client.handlers["/v1.0/card/instances/callback"],
        )

    def test_bot_message_routed_to_webhook_core(self, monkeypatch, fake_sdk):
        from opensearch_pipeline import dingtalk_bot
        core = MagicMock(return_value={"msgtype": "empty"})
        # 必须在 start 前打补丁：runner 启动时把核心函数绑进闭包
        monkeypatch.setattr(dingtalk_bot, "_process_webhook_body", core)
        bot_handler, _ = self._start_and_get_handlers(monkeypatch, fake_sdk)

        body = {"msgtype": "text", "text": {"content": "U8怎么登录"},
                "sessionWebhook": "https://oapi.dingtalk.com/robot/x",
                "senderStaffId": "user01", "conversationId": "cid1"}
        status, msg = asyncio.run(bot_handler.process(_FakeCallback(body)))
        assert status == "SUCCESS"
        core.assert_called_once_with(body)

    def test_card_callback_routed_to_card_core(self, monkeypatch, fake_sdk):
        from opensearch_pipeline import dingtalk_bot
        core = MagicMock(return_value={})
        monkeypatch.setattr(dingtalk_bot, "_process_card_callback_body", core)
        _, card_handler = self._start_and_get_handlers(monkeypatch, fake_sdk)

        body = {"outTrackId": "mid1", "userId": "user01",
                "content": '{"cardPrivateData":{"params":{"action":"upvote"}}}'}
        status, _ = asyncio.run(card_handler.process(_FakeCallback(body)))
        assert status == "SUCCESS"
        core.assert_called_once_with(body)

    def test_processing_error_still_acks_ok(self, monkeypatch, fake_sdk):
        """核心抛错也 ACK OK：非 OK ack 触发钉钉重投 → 用户收到两份回答，绝不允许。"""
        from opensearch_pipeline import dingtalk_bot
        monkeypatch.setattr(
            dingtalk_bot, "_process_webhook_body",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        bot_handler, _ = self._start_and_get_handlers(monkeypatch, fake_sdk)
        status, _ = asyncio.run(bot_handler.process(_FakeCallback({"msgtype": "text"})))
        assert status == "SUCCESS"


# ═══════════════════════════════════════════════════════════════
# 5. 卡片 callbackType 路由
# ═══════════════════════════════════════════════════════════════

class TestCardCallbackType:
    @staticmethod
    def _assemble(monkeypatch, *, enabled, active):
        from opensearch_pipeline.dingtalk_card import _assemble_delivery_payload
        monkeypatch.setattr(runner, "stream_mode_enabled", lambda: enabled)
        monkeypatch.setattr(runner, "is_stream_active", lambda: active)
        return _assemble_delivery_payload(
            template_id="tpl-1",
            out_track_id="mid-1",
            card_param_map={"title": "t"},
            conversation_id="cid-1",
            conversation_type="1",
            sender_staff_id="user01",
            client_id="ck",
            callback_route_key="rag_feedback_callback",
        )

    def test_stream_active_uses_stream_callback(self, monkeypatch):
        payload = self._assemble(monkeypatch, enabled=True, active=True)
        assert payload["callbackType"] == "STREAM"
        assert "callbackRouteKey" not in payload

    def test_stream_enabled_but_client_down_falls_back_http(self, monkeypatch):
        """开关开了但客户端没起来：必须回退 HTTP，否则按钮点击丢失。"""
        payload = self._assemble(monkeypatch, enabled=True, active=False)
        assert payload["callbackType"] == "HTTP"
        assert payload["callbackRouteKey"] == "rag_feedback_callback"

    def test_default_http_mode(self, monkeypatch):
        payload = self._assemble(monkeypatch, enabled=False, active=False)
        assert payload["callbackType"] == "HTTP"
        assert payload["callbackRouteKey"] == "rag_feedback_callback"


class TestStreamActiveProbesRealConnection:
    """批次7（ultra dingtalk_stream_runner:140）：is_stream_active 探真实 WSS 连通——
    线程活着 ≠ 连接活着（首连前/3s 退避重连窗按 STREAM 发卡=按钮点击静默丢失）。"""

    def test_inactive_before_first_wss_established(self, monkeypatch, fake_sdk):
        _enable(monkeypatch)
        # 首连未成形态：start_forever 不赋 websocket
        class _NeverConnect(_FakeClient):
            def start_forever(self):
                self.started.set()          # 线程已进循环但 WSS 未建立
                self.release.wait(timeout=10)

        monkeypatch.setattr(fake_sdk, "DingTalkStreamClient", _NeverConnect)
        assert runner.start_stream_client() is True
        client = _NeverConnect.instances[-1]
        assert client.started.wait(timeout=30)
        assert runner.is_stream_active() is False, \
            "WSS 未建连（websocket=None）必须判不活 → 卡片回退 HTTP 回调"

    def test_inactive_during_reconnect_window(self, monkeypatch, fake_sdk):
        _enable(monkeypatch)
        assert runner.start_stream_client() is True
        client = _FakeClient.instances[-1]
        assert client.started.wait(timeout=30)
        assert runner.is_stream_active() is True            # 已连
        client.websocket.closed = True                      # 断线：旧属性形态
        assert runner.is_stream_active() is False
        client.websocket = types.SimpleNamespace(close_code=None)   # 重连成功：新属性形态
        assert runner.is_stream_active() is True
        client.websocket = types.SimpleNamespace(close_code=1006)   # 再断（异常关闭码）
        assert runner.is_stream_active() is False
