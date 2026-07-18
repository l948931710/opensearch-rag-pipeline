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
        """脚本化 cursor（P2-04b 原子协议）：steps 按 execute 次序给 rowcount/row。"""

        def __init__(self, steps=None, exc=None):
            self.steps = list(steps or [])
            self.exc = exc
            self.sqls = []
            self.rowcount = -1
            self._row = None

        def execute(self, sql, args=None):
            self.sqls.append(sql)
            if self.exc:
                raise self.exc
            step = self.steps.pop(0) if self.steps else {"rowcount": 0, "row": None}
            self.rowcount = step.get("rowcount", 0)
            self._row = step.get("row")

        def fetchone(self):
            return self._row

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

    def test_insert_winner_is_atomic(self, monkeypatch):
        """rowcount==1（真插入）=唯一赢家；首条 SQL 必须是 INSERT..ODKU（单步原子）。"""
        cur = self._Cur(steps=[{"rowcount": 1}])
        self._wire(monkeypatch, cur)
        assert bot._rds_dedup_claim("m-rds") == (True, None)
        assert "INSERT INTO" in cur.sqls[0] and "ON DUPLICATE KEY" in cur.sqls[0]

    def test_fresh_row_blocks_duplicate_even_if_primary_claims(self, monkeypatch):
        """撞键（rowcount=0）→ 读行 fresh → 判重；主层放行也被兜底拦下。"""
        cur = self._Cur(steps=[{"rowcount": 0},
                               {"row": ("retryable_failed", 2, "MSG-X", 1)}])
        self._wire(monkeypatch, cur)
        claimed, meta = bot._msg_claim("m-rds")
        assert claimed is False                                # 主层放行也被兜底拦下
        assert meta["st"] == "retryable_failed" and meta["mid"] == "MSG-X"

    def test_stale_takeover_single_winner(self, monkeypatch):
        """陈旧行条件接管：UPDATE 带新鲜度 WHERE + 显式 updated_at=NOW(3)（值全同时
        affected-rows=0 会让无人能赢），rowcount==1=接管赢家。"""
        cur = self._Cur(steps=[{"rowcount": 0},
                               {"row": ("processing", 1, None, 0)},   # fresh=0 陈旧
                               {"rowcount": 1}])
        self._wire(monkeypatch, cur)
        assert bot._rds_dedup_claim("m-rds") == (True, None)
        assert "updated_at=NOW(3)" in cur.sqls[2]
        assert "updated_at <= NOW(3) - INTERVAL" in cur.sqls[2]

    def test_stale_takeover_loser_gets_winner_meta(self, monkeypatch):
        """接管输家（条件 UPDATE rowcount=0）→ 重读行把赢家状态带回 (False, meta)。"""
        cur = self._Cur(steps=[{"rowcount": 0},
                               {"row": ("processing", 1, None, 0)},
                               {"rowcount": 0},
                               {"row": ("processing", 1, "MID-W")}])
        self._wire(monkeypatch, cur)
        claimed, meta = bot._rds_dedup_claim("m-rds")
        assert claimed is False
        assert meta["st"] == "processing" and meta["mid"] == "MID-W"

    def test_missing_table_1146_degrades_with_negative_cache(self, monkeypatch):
        cur = self._Cur(exc=RuntimeError("(1146, \"Table 'op.dingtalk_msg_dedup' doesn't exist\")"))
        self._wire(monkeypatch, cur)
        claimed, meta = bot._rds_dedup_claim("m-x")
        assert claimed is True and meta is None                # fail-open
        assert bot._RDS_DEDUP_MISSING_UNTIL > time.time()      # 负缓存已武装

    def test_kill_switch_skips(self, monkeypatch):
        monkeypatch.setattr(bot, "_rds_dedup_on", lambda: False)
        assert bot._rds_dedup_claim("m-y") == (True, None)


def _resp(status=200, body=None, text=""):
    """带 .json() 的假响应（P2-04b errcode 契约用）。"""
    import json as _json
    if body is not None and not text:
        text = _json.dumps(body)

    def _j():
        if body is None:
            raise ValueError("not json")
        return body

    return SimpleNamespace(status_code=status, text=text, json=_j)


class TestErrcodeContract:
    """P2-04b（评审证据 C）：HTTP 200 + 业务 errcode 契约——只有 errcode=0 才是 sent。"""

    def test_200_errcode0_sent(self, monkeypatch):
        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(
            post=lambda *a, **k: _resp(body={"errcode": 0, "errmsg": "ok"})))
        assert bot._send_reply("https://oapi.dingtalk.com/x", "t", "正文", msg_id="m1")
        assert _meta("m1")["st"] == "sent"

    def test_200_permanent_errcode_final_failed_no_retry(self, monkeypatch):
        calls = {"n": 0}

        def _p(*a, **k):
            calls["n"] += 1
            return _resp(body={"errcode": 310000, "errmsg": "keywords not in whitelist"})

        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(post=_p))
        assert bot._send_reply("https://oapi.dingtalk.com/x", "t", "正文",
                               msg_id="m1") is False
        assert calls["n"] == 1                                 # 永久不重试
        assert _meta("m1")["st"] == "final_failed"

    def test_200_throttle_errcode_retries_then_retryable(self, monkeypatch):
        calls = {"n": 0}

        def _p(*a, **k):
            calls["n"] += 1
            return _resp(body={"errcode": 130101, "errmsg": "send too fast"})

        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(post=_p))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        assert bot._send_terminal_text("https://oapi.dingtalk.com/x", "文",
                                       msg_id="m1") is False
        assert calls["n"] == 3                                 # 限流按 5xx 同治
        assert _meta("m1")["st"] == "retryable_failed"

    def test_200_unparseable_body_treated_sent(self, monkeypatch):
        """钉钉域 200 恒回 JSON；不可解析按成功（记录在案取舍，防 never-case 假警报）。"""
        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(
            post=lambda *a, **k: _resp(body=None, text="<html>")))
        assert bot._send_reply("https://oapi.dingtalk.com/x", "t", "正文", msg_id="m1")
        assert _meta("m1")["st"] == "sent"


class TestTerminalTextSender:
    """P2-04b（评审证据 A）：终答文本走分类引擎+状态机；提示语函数误用被源扫描锁死。"""

    def test_terminal_text_payload_and_sent_mark(self, monkeypatch):
        seen = {}

        def _p(url, json=None, **k):
            seen.update(json or {})
            return _resp(body={"errcode": 0})

        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(post=_p))
        assert bot._send_terminal_text("https://oapi.dingtalk.com/x", "终答", msg_id="m1")
        assert seen["msgtype"] == "text" and seen["text"]["content"] == "终答"
        assert _meta("m1")["st"] == "sent"

    def test_terminal_text_connection_error_marks_retryable(self, monkeypatch):
        import requests

        def _p(*a, **k):
            raise requests.exceptions.ConnectionError("refused")

        bot._msg_claim("m1")
        monkeypatch.setattr(bot, "http_requests", SimpleNamespace(post=_p))
        monkeypatch.setattr(time, "sleep", lambda s: None)
        assert bot._send_terminal_text("https://oapi.dingtalk.com/x", "终答",
                                       msg_id="m1") is False
        assert _meta("m1")["st"] == "retryable_failed"

    def test_hint_function_callsites_whitelisted(self):
        """源扫描（closure 2）：fire-and-forget 的 _send_text_reply 只允许提示语四点——
        新增调用点必须过白名单，否则=有人把终答误接回旧函数。"""
        from pathlib import Path
        src = (Path(bot.__file__)).read_text(encoding="utf-8")
        allowed = ("您好！请输入", "已记录你补充的原因", "已开启新会话", "正在为您查询")
        lines = src.split("\n")
        offenders = []
        for i, ln in enumerate(lines):
            if "_send_text_reply(" in ln and "def _send_text_reply" not in ln:
                window = "\n".join(lines[i:i + 3])
                if not any(a in window for a in allowed):
                    offenders.append(f"L{i + 1}: {ln.strip()}")
        assert not offenders, (
            f"_send_text_reply 出现白名单外调用点（终答必须走 _send_terminal_text/"
            f"_send_reply 进状态机）: {offenders}")

    def test_terminal_branches_use_state_machine(self):
        """四个终答分支（前置路由/失败序列/无结果/错误回执）必须带 msg_id 走终答函数。"""
        from pathlib import Path
        src = (Path(bot.__file__)).read_text(encoding="utf-8")
        lines = src.split("\n")
        call_lines = [i for i, ln in enumerate(lines)
                      if "_send_terminal_text(" in ln and "def _send_terminal_text" not in ln]
        assert len(call_lines) >= 4                             # 四个终答调用点
        missing = []
        for i in call_lines:
            window = "\n".join(lines[i:i + 5])                  # 多行调用：msg_id 在续行
            if "msg_id=" not in window:
                missing.append(f"L{i + 1}: {lines[i].strip()}")
        assert not missing, f"终答调用缺 msg_id（状态机旁路）: {missing}"


class TestCrashAndFailover:
    """P2-04b（closure 7 校准）：认领后崩溃/主层故障的重投行为。"""

    def test_crash_mid_processing_swallow_then_reclaim_after_expiry(self):
        claimed, _ = bot._msg_claim("m-crash")
        assert claimed                                          # 领用后进程崩溃（无任何 mark）
        claimed2, prior2 = bot._msg_claim("m-crash")
        assert not claimed2 and prior2["st"] == "processing"    # 窗口内重投被按在途吞
        with bot._seen_msg_lock:                                # 模拟去重窗过期
            bot._seen_msg_ids["m-crash"] = (time.time() - 1,
                                            {"st": "processing", "att": 1})
        claimed3, _ = bot._msg_claim("m-crash")
        assert claimed3                                         # 过期后重投可重新领用

    def test_redis_broken_fail_open_rds_still_blocks(self, monkeypatch):
        """主层（redis）故障 fail-open + RDS 兜底仍拦重复——分层防御语义。"""
        import sys
        monkeypatch.setenv("RAG_MSG_DEDUP_BACKEND", "redis")
        broken = SimpleNamespace(get_client=lambda: (_ for _ in ()).throw(
            OSError("redis down")))
        monkeypatch.setitem(sys.modules, "opensearch_pipeline.redis_client", broken)
        monkeypatch.setattr(bot, "_rds_dedup_claim",
                            lambda m: (False, {"st": "sent", "att": 1, "mid": None}))
        claimed, meta = bot._msg_claim("m-ha")
        assert claimed is False and meta["st"] == "sent"

    def test_both_layers_down_is_accepted_residual(self, monkeypatch):
        """双层同故障 → fail-open 放行=已接受残余风险（台账在案），实现必须是这个语义。"""
        import sys
        monkeypatch.setenv("RAG_MSG_DEDUP_BACKEND", "redis")
        broken = SimpleNamespace(get_client=lambda: (_ for _ in ()).throw(
            OSError("redis down")))
        monkeypatch.setitem(sys.modules, "opensearch_pipeline.redis_client", broken)
        monkeypatch.setattr(bot, "_rds_dedup_claim", lambda m: (True, None))
        assert bot._msg_claim("m-residual") == (True, None)


def test_runner_retry_ack_status_fallback():
    from opensearch_pipeline.dingtalk_stream_runner import _retry_ack_status
    mod = SimpleNamespace(AckMessage=SimpleNamespace(STATUS_OK=200,
                                                     STATUS_SYSTEM_EXCEPTION=500))
    assert _retry_ack_status(mod) == 500
    mod2 = SimpleNamespace(AckMessage=SimpleNamespace(STATUS_OK=200))
    assert _retry_ack_status(mod2) == 200                      # SDK 形态变化保守退 OK
