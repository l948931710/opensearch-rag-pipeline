# -*- coding: utf-8 -*-
"""Majors β(M2)钉钉主链有界并发 + admission(2026-07-21 codex 共识)。

锁死的不变量:
- 双 flag 默认 off:零 admission 调用、零信号量、spawn 行为与既有等价(byte-identical);
- RAG_DT_ADMISSION_ENABLE=true:denial → _send_terminal_text 话术终答(msg_id 进发送
  状态机)+ rejected_admission,不 spawn;限流器自身异常 fail-closed 拒绝;
- RAG_DT_MAX_WORKERS>0:饱和 → 忙话术终答 + rejected_busy,不排队不静默丢;槽位在
  RAG 线程 finally 释放;thread.start() 抛错同样释放(零泄漏);
- _run_claimed 对 rejected_* 形态不标 processing(不覆盖发送层已收口的状态)。
"""
import threading
import time

import pytest

import opensearch_pipeline.dingtalk_bot as dt


def _body(msg_id="m1", question="包装规范是什么", staff="u1"):
    return {"text": {"content": question}, "sessionWebhook": "https://oapi.dingtalk.com/x",
            "senderNick": "张三", "conversationId": "cid-1", "senderStaffId": staff,
            "conversationType": "1", "msgId": msg_id}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("RAG_DT_MAX_WORKERS", raising=False)
    monkeypatch.delenv("RAG_DT_ADMISSION_ENABLE", raising=False)
    monkeypatch.setattr(dt, "_DT_RAG_SEMAPHORE", None)
    monkeypatch.setattr(dt, "_DT_SEM_CAP", 0)
    # 隔离外部副作用:补充原因回收(RDS)/查询中提示/终答发送
    monkeypatch.setattr(dt, "take_awaiting_comment", lambda **k: False)
    monkeypatch.setattr(dt, "_send_text_reply", lambda *a, **k: True)
    yield


class _Recorder:
    def __init__(self):
        self.terminal = []          # (text, msg_id)
        self.rag = []               # args 元组
        self.done = threading.Event()

    def send_terminal(self, webhook, text, *, msg_id=""):
        self.terminal.append((text, msg_id))
        return True

    def rag_run(self, *args):
        self.rag.append(args)
        self.done.set()


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(dt, "_send_terminal_text", r.send_terminal)
    monkeypatch.setattr(dt, "_process_rag_query", r.rag_run)
    return r


class _NoCallLimiter:
    def admit_ask(self, *a, **k):
        raise AssertionError("flag off 不得触碰 admit_ask")


def test_flags_off_spawns_thread_no_admission(monkeypatch, rec):
    monkeypatch.setattr(dt, "LIMITER", _NoCallLimiter())
    out = dt._process_claimed_body(_body())
    assert out == {"msgtype": "empty"}
    assert rec.done.wait(5), "RAG 线程未启动"
    args = rec.rag[0]
    assert args[0] == "包装规范是什么" and args[4] == "u1" and args[7] == "m1"
    assert rec.terminal == []                       # 零拒绝
    assert dt._DT_RAG_SEMAPHORE is None             # 零信号量


def test_admission_denial_terminal_reply_no_spawn(monkeypatch, rec):
    from opensearch_pipeline.rate_limiter import Denial
    monkeypatch.setenv("RAG_DT_ADMISSION_ENABLE", "true")
    calls = []

    class _L:
        def admit_ask(self, actor, *, is_user, thinking=False, count_llm=True):
            calls.append((actor, is_user, thinking, count_llm))
            return Denial(429, "提问太频繁了，请稍后再试", 30, "per_min")

    monkeypatch.setattr(dt, "LIMITER", _L())
    out = dt._process_claimed_body(_body())
    assert out == {"msgtype": "rejected_admission"}
    assert calls == [("u:u1", True, False, True)]   # count_llm=True 镜像 /api/ask
    assert rec.rag == []                            # 不 spawn
    text, mid = rec.terminal[0]
    assert "提问太频繁" in text and mid == "m1"      # 话术终答进状态机


def test_admission_admitted_spawns(monkeypatch, rec):
    monkeypatch.setenv("RAG_DT_ADMISSION_ENABLE", "true")

    class _L:
        def admit_ask(self, *a, **k):
            return None

    monkeypatch.setattr(dt, "LIMITER", _L())
    out = dt._process_claimed_body(_body())
    assert out == {"msgtype": "empty"} and rec.done.wait(5)


def test_admission_limiter_error_fail_closed(monkeypatch, rec):
    monkeypatch.setenv("RAG_DT_ADMISSION_ENABLE", "true")

    class _L:
        def admit_ask(self, *a, **k):
            raise RuntimeError("limiter down")

    monkeypatch.setattr(dt, "LIMITER", _L())
    out = dt._process_claimed_body(_body())
    assert out == {"msgtype": "rejected_admission"}   # 宁拒不放
    assert rec.rag == [] and "系统繁忙" in rec.terminal[0][0]


def test_saturation_busy_reply_and_slot_release(monkeypatch, rec):
    monkeypatch.setenv("RAG_DT_MAX_WORKERS", "1")
    gate = threading.Event()
    started = threading.Event()

    def _blocking_rag(*args):
        started.set()
        gate.wait(10)

    monkeypatch.setattr(dt, "_process_rag_query", _blocking_rag)
    assert dt._process_claimed_body(_body("m-a")) == {"msgtype": "empty"}
    assert started.wait(5)
    # 槽位占满 → 第二条忙拒绝(终答+rejected,不排队)
    out2 = dt._process_claimed_body(_body("m-b"))
    assert out2 == {"msgtype": "rejected_busy"}
    assert "咨询人数较多" in rec.terminal[0][0] and rec.terminal[0][1] == "m-b"
    # 释放后槽位回收 → 第三条正常受理
    gate.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        if dt._process_claimed_body(_body("m-c")) == {"msgtype": "empty"}:
            break
        time.sleep(0.05)
    else:
        pytest.fail("槽位未随线程结束释放")


def test_thread_start_failure_releases_slot(monkeypatch, rec):
    monkeypatch.setenv("RAG_DT_MAX_WORKERS", "1")

    class _BoomThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")

    monkeypatch.setattr(dt.threading, "Thread", _BoomThread)
    with pytest.raises(RuntimeError, match="cannot start thread"):
        dt._process_claimed_body(_body("m-x"))
    monkeypatch.undo()   # 还原 Thread(下方真实起线程)
    monkeypatch.setenv("RAG_DT_MAX_WORKERS", "1")
    monkeypatch.setattr(dt, "take_awaiting_comment", lambda **k: False)
    monkeypatch.setattr(dt, "_send_text_reply", lambda *a, **k: True)
    r2 = _Recorder()
    monkeypatch.setattr(dt, "_send_terminal_text", r2.send_terminal)
    monkeypatch.setattr(dt, "_process_rag_query", r2.rag_run)
    # 槽位未泄漏:立即可再受理
    assert dt._process_claimed_body(_body("m-y")) == {"msgtype": "empty"}
    assert r2.done.wait(5)


def test_run_claimed_skips_processing_mark_for_rejected(monkeypatch):
    marks = []
    monkeypatch.setattr(dt, "_msg_mark", lambda mid, st, **k: marks.append((mid, st)))
    monkeypatch.setattr(dt, "_process_claimed_body",
                        lambda body: {"msgtype": "rejected_busy"})
    out = dt._run_claimed("m-r", _body("m-r"), att=1)
    assert out == {"msgtype": "rejected_busy"}
    assert marks == []                              # 不覆盖发送层状态
    monkeypatch.setattr(dt, "_process_claimed_body", lambda body: {"msgtype": "empty"})
    dt._run_claimed("m-e", _body("m-e"), att=1)
    assert ("m-e", "processing") in marks           # 既有语义保留


def test_env_parsing_defensive(monkeypatch):
    monkeypatch.setenv("RAG_DT_MAX_WORKERS", "notanint")
    assert dt._dt_max_workers() == 0
    monkeypatch.setenv("RAG_DT_MAX_WORKERS", "-3")
    assert dt._dt_max_workers() == 0
