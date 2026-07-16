# -*- coding: utf-8 -*-
"""
test_agent_runtime_llm_log_outbox.py — llm_call_log 有界 outbox（perf 批次 C §4.5）

钉住：flag off=原同步函数零包装（默认零行为变化）· flag on=入队攒批 executemany ·
队列满回退同步直写（no-silent-caps）· drain 冲净。
"""
import queue
import threading

from opensearch_pipeline.agent_runtime import llm_log_outbox as ob


class _BatchStore:
    def __init__(self):
        self.batches = []
        self.sync_calls = []
        self._lock = threading.Lock()

    def record_llm_call(self, **kw):
        with self._lock:
            self.sync_calls.append(kw)
        return "cid"

    def record_llm_calls(self, rows):
        with self._lock:
            self.batches.append(list(rows))
        return len(rows)


def _row(i):
    return {"run_id": f"r{i}", "request_id": None, "provider": "dashscope", "model": "m",
            "category": "agent", "prompt_version": None, "tokens_prompt": 1,
            "tokens_completion": 2, "cost_estimate": None, "latency_ms": 10,
            "status": "ok", "user_id": "u", "dept_group": "production"}


def test_flag_off_returns_sync_logger_unwrapped(monkeypatch):
    monkeypatch.delenv("RAG_AGENT_LLM_LOG_ASYNC", raising=False)
    store = _BatchStore()
    assert ob.wrap_call_logger(store) == store.record_llm_call   # 零包装=零行为变化（绑定方法按 ==）


def test_flag_on_enqueues_and_flushes_batched(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_LLM_LOG_ASYNC", "true")
    store = _BatchStore()
    logger_fn = ob.wrap_call_logger(store)
    assert logger_fn is not store.record_llm_call
    for i in range(3):
        logger_fn(**_row(i))
    assert ob.drain_llm_log(timeout=5.0) is True
    flushed = [r for b in store.batches for r in b]
    assert [r["run_id"] for r in flushed] == ["r0", "r1", "r2"]   # FIFO 全落、零同步直写
    assert store.sync_calls == []


def test_queue_full_falls_back_to_sync_write(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_LLM_LOG_QUEUE", "1")
    monkeypatch.setenv("RAG_AGENT_LLM_LOG_ASYNC", "true")
    store = _BatchStore()
    # 不起 worker（确定性触发 Full）：手工装满容量 1 的队列
    monkeypatch.setattr(ob, "_ensure_worker", lambda s: None)
    full_q = queue.Queue(maxsize=1)
    full_q.put_nowait(_row(0))
    monkeypatch.setattr(ob, "_q", full_q)
    logger_fn = ob.wrap_call_logger(store)
    logger_fn(**_row(1))
    assert [r["run_id"] for r in store.sync_calls] == ["r1"]     # 满 → 同步兜底，不丢账
    assert store.batches == []


def test_stub_store_without_batch_method_stays_sync(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_LLM_LOG_ASYNC", "true")

    class _Legacy:
        def record_llm_call(self, **kw):
            return "cid"

    legacy = _Legacy()
    assert ob.wrap_call_logger(legacy) == legacy.record_llm_call   # 无批量法 → 保持同步
