# -*- coding: utf-8 -*-
"""B7-P2-04（Sam 拍板 2026-07-18）：消息 ACK/发送结果状态机。

二维分类回归：错误可重试性 × 发送结果确定性——
永久→final_failed 收口；瞬态→重投（attempts 封顶）；发送成功后内部错误→ACK OK；
ReadTimeout→DELIVERY_UNKNOWN 不盲重发；重投复用已算答案；RDS msgId 兜底优雅降级。
"""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from opensearch_pipeline import dingtalk_bot as bot


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("RAG_MSG_DEDUP_BACKEND", raising=False)   # memory 后端
    monkeypatch.delenv("RAG_MSG_MAX_ATTEMPTS", raising=False)
    monkeypatch.setattr(bot, "_rds_dedup_on", lambda: False)     # RDS 层单测单独开
    with bot._seen_msg_lock:
        bot._seen_msg_ids.clear()
    yield
    with bot._seen_msg_lock:
        bot._seen_msg_ids.clear()


def _meta(msg_id):
    with bot._seen_msg_lock:
        ent = bot._seen_msg_ids.get(msg_id)
    return bot._meta_decode(ent[1] if isinstance(ent, tuple) else None)


BODY = {"msgId": "m1", "senderNick": "工", "conversationType": "1",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=x"}


class TestClaimAndStates:
    def test_fresh_claim_then_duplicate_with_meta(self):
        claimed, prior = bot._msg_claim("m1")
        assert claimed and prior is None
        claimed2, prior2 = bot._msg_claim("m1")
        assert not claimed2 and prior2["st"] == "processing"

    def test_legacy_value_decode(self):
        assert bot._meta_decode("done")["st"] == "sent"
        assert bot._meta_decode("processing")["st"] == "processing"
        assert bot._meta_decode(b'{"st": "sent", "att": 2}')["att"] == 2
        assert bot._meta_decode("{bad json")["st"] == "processing"
        assert bot._meta_decode(None)["st"] == "processing"


class TestAckLadder:
    def test_permanent_error_final_failed_and_swallowed_on_redelivery(self, monkeypatch):
        monkeypatch.setattr(bot, "_process_claimed_body",
                            lambda b: (_ for _ in ()).throw(
                                bot.PermanentMessageError("坏载荷")))
        out = bot._process_webhook_body(dict(BODY))
        assert out == {"msgtype": "final_failed"}
        assert _meta("m1")["st"] == "final_failed"
        out2 = bot._process_webhook_body(dict(BODY))   # 重投 → 吞掉不空转
        assert out2 == {"msgtype": "duplicate"}

    def test_http_4xx_is_permanent(self, monkeypatch):
        monkeypatch.setattr(bot, "_process_claimed_body",
                            lambda b: (_ for _ in ()).throw(
                                HTTPException(status_code=400, detail="缺 sessionWebhook")))
        with pytest.raises(HTTPException):
            bot._process_webhook_body(dict(BODY))
        assert _meta("m1")["st"] == "final_failed"

    def test_transient_raises_retry_and_redelivery_reprocesses(self, monkeypatch):
        calls = {"n": 0}

        def _flaky(b):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("DB 抖动")
            return {"msgtype": "empty"}

        monkeypatch.setattr(bot, "_process_claimed_body", _flaky)
        with pytest.raises(bot.TransientMessageError):
            bot._process_webhook_body(dict(BODY))     # 首次：瞬态 → 请求重投
        assert _meta("m1")["st"] == "retryable_failed" and _meta("m1")["att"] == 2
        out = bot._process_webhook_body(dict(BODY))   # 重投：无 mid → 重算成功
        assert out == {"msgtype": "empty"} and calls["n"] == 2
        assert _meta("m1")["st"] == "processing"      # 在途；sent 由发送层标

    def test_attempts_cap_final_failed_with_alert(self, monkeypatch):
        alerts = []
        monkeypatch.setattr(bot, "_process_claimed_body",
                            lambda b: (_ for _ in ()).throw(RuntimeError("持续故障")))
        import opensearch_pipeline.alerting as alerting
        monkeypatch.setattr(alerting, "send_ops_alert",
                            lambda *a, **k: alerts.append(a) or True)
        # 前两轮瞬态重投
        for expected_att in (2, 3):
            with pytest.raises(bot.TransientMessageError):
                bot._process_webhook_body(dict(BODY))
            assert _meta("m1")["att"] == expected_att
        out = bot._process_webhook_body(dict(BODY))   # 第三轮 att=3 达上限 → 收口
        assert out == {"msgtype": "retries_exhausted"}
        assert _meta("m1")["st"] == "final_failed" and alerts


class TestRedeliveryRouting:
    def test_retryable_with_mid_reuses_answer(self, monkeypatch):
        sent = []
        bot._msg_mark("m1", "retryable_failed", att=2, mid="MSG-X")
        monkeypatch.setattr("opensearch_pipeline.qa_logger.fetch_answer_by_message_id",
                            lambda mid: {"message_id": mid, "answer_text": "已算答案"})
        monkeypatch.setattr(bot, "_send_reply",
                            lambda sw, t, x, msg_id="": sent.append((sw, x, msg_id)) or True)
        out = bot._process_webhook_body(dict(BODY))
        assert out == {"msgtype": "resend"}
        assert sent and sent[0][1] == "已算答案" and sent[0][2] == "m1"   # 不重算不重计费

    def test_delivery_unknown_never_auto_resends(self, monkeypatch):
        bot._msg_mark("m1", "delivery_unknown")
        monkeypatch.setattr(bot, "_process_claimed_body",
                            lambda b: pytest.fail("DELIVERY_UNKNOWN 不得重算"))
        out = bot._process_webhook_body(dict(BODY))
        assert out == {"msgtype": "delivery_unknown"}

    def test_sent_redelivery_swallowed(self):
        bot._msg_mark("m1", "sent")
        assert bot._process_webhook_body(dict(BODY)) == {"msgtype": "duplicate"}


class TestSendClassification:
    def _post_raiser(self, exc):
        def _p(*a, **k):
            raise exc
        return _p

    def test_read_timeout_marks_delivery_unknown_no_blind_resend(self, monkeypatch):
        import requests
        alerts = []
        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests",
                            SimpleNamespace(post=self._post_raiser(
                                requests.exceptions.ReadTimeout("no echo"))))
        import opensearch_pipeline.alerting as alerting
        monkeypatch.setattr(alerting, "send_ops_alert",
                            lambda *a, **k: alerts.append(a) or True)
        ok = bot._send_reply("https://oapi.dingtalk.com/x", "t", "正文", msg_id="m1")
        assert ok is False
        assert _meta("m1")["st"] == "delivery_unknown" and alerts

    def test_connection_error_retries_then_retryable(self, monkeypatch):
        import requests
        tries = {"n": 0}

        def _p(*a, **k):
            tries["n"] += 1
            raise requests.exceptions.ConnectionError("refused")

        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(post=_p))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        ok = bot._send_reply("https://oapi.dingtalk.com/x", "t", "正文", msg_id="m1")
        assert ok is False and tries["n"] == 3                 # 明确未送达 → 进程内自重试
        assert _meta("m1")["st"] == "retryable_failed"          # ack 丢包重投可复用

    def test_4xx_permanent_final_failed(self, monkeypatch):
        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(
            post=lambda *a, **k: SimpleNamespace(status_code=403, text="perm denied")))
        ok = bot._send_reply("https://oapi.dingtalk.com/x", "t", "正文", msg_id="m1")
        assert ok is False and _meta("m1")["st"] == "final_failed"

    def test_200_marks_sent(self, monkeypatch):
        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(
            post=lambda *a, **k: SimpleNamespace(status_code=200, text="ok")))
        assert bot._send_reply("https://oapi.dingtalk.com/x", "t", "正文", msg_id="m1")
        assert _meta("m1")["st"] == "sent"

    def test_no_msgid_zero_state_side_effect(self, monkeypatch):
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(
            post=lambda *a, **k: SimpleNamespace(status_code=200, text="ok")))
        assert bot._send_reply("https://oapi.dingtalk.com/x", "t", "提示语")
        with bot._seen_msg_lock:
            assert not bot._seen_msg_ids                       # 非终答场景零状态写


class TestRdsFallback:
    class _Cur:
        def __init__(self, row=None, exc=None):
            self.row = row
            self.exc = exc
            self.sqls = []

        def execute(self, sql, args=None):
            self.sqls.append(sql)
            if self.exc:
                raise self.exc

        def fetchone(self):
            return self.row

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _conn(self, cur):
        return SimpleNamespace(cursor=lambda: cur, commit=lambda: None,
                               close=lambda: None)

    def _wire(self, monkeypatch, cur):
        monkeypatch.setattr(bot, "_rds_dedup_on", lambda: True)
        monkeypatch.setattr(bot, "_RDS_DEDUP_MISSING_UNTIL", 0.0)
        monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                            lambda *a, **k: self._conn(cur))
        monkeypatch.setattr("opensearch_pipeline.config.get_config",
                            lambda: SimpleNamespace(
                                simulate_db=False,
                                rds=SimpleNamespace(operation_database="op")))

    def test_fresh_row_blocks_duplicate_even_if_primary_claims(self, monkeypatch):
        cur = self._Cur(row=("retryable_failed", 2, "MSG-X", 1))
        self._wire(monkeypatch, cur)
        claimed, meta = bot._msg_claim("m-rds")
        assert claimed is False                                # 主层放行也被兜底拦下
        assert meta["st"] == "retryable_failed" and meta["mid"] == "MSG-X"

    def test_missing_table_1146_degrades_with_negative_cache(self, monkeypatch):
        cur = self._Cur(exc=RuntimeError("(1146, \"Table 'op.dingtalk_msg_dedup' doesn't exist\")"))
        self._wire(monkeypatch, cur)
        claimed, meta = bot._rds_dedup_claim("m-x")
        assert claimed is True and meta is None                # fail-open
        assert bot._RDS_DEDUP_MISSING_UNTIL > time.time()      # 负缓存已武装

    def test_kill_switch_skips(self, monkeypatch):
        monkeypatch.setattr(bot, "_rds_dedup_on", lambda: False)
        assert bot._rds_dedup_claim("m-y") == (True, None)


def test_runner_retry_ack_status_fallback():
    from opensearch_pipeline.dingtalk_stream_runner import _retry_ack_status
    mod = SimpleNamespace(AckMessage=SimpleNamespace(STATUS_OK=200,
                                                     STATUS_SYSTEM_EXCEPTION=500))
    assert _retry_ack_status(mod) == 500
    mod2 = SimpleNamespace(AckMessage=SimpleNamespace(STATUS_OK=200))
    assert _retry_ack_status(mod2) == 200                      # SDK 形态变化保守退 OK
