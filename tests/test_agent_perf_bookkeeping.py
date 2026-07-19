# -*- coding: utf-8 -*-
"""PERF-1..4（记账盖章合并,2026-07-19）探针驱动回归。"""
from types import SimpleNamespace

from opensearch_pipeline.agent_runtime.run_store import RDSRunStore, _begin


class _Cur:
    def __init__(self):
        self.sqls = []

    def execute(self, sql, args=None):
        self.sqls.append(sql)

    def fetchone(self):
        return (1,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.cur = _Cur()
        self.begins = 0
        self.commits = 0

    def begin(self):
        self.begins += 1

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _wire(monkeypatch):
    conn = _Conn()
    store = RDSRunStore()
    monkeypatch.setattr(store, "_conn", lambda: conn)
    monkeypatch.setattr("opensearch_pipeline.agent_runtime.run_store._op_db",
                        lambda: "op")
    return store, conn


class TestPerf1BeginOnce:
    def test_marked_conn_skips_second_begin(self):
        """探针翻绿：db 层已 begin(带标记)→ run_store._begin 零第二次往返。"""
        conn = _Conn()
        conn._rag_txn_begun = True
        _begin(conn)
        assert conn.begins == 0

    def test_unmarked_conn_still_begins_exactly_once(self):
        """RAG_DB_TXN_BEGIN=false 姿态：标记缺席 → 本处 begin 恰一次（安全保证不丢）。"""
        conn = _Conn()
        _begin(conn)
        assert conn.begins == 1

    def test_db_layer_sets_marker(self, monkeypatch):
        from opensearch_pipeline.db import _begin_txn
        monkeypatch.setenv("RAG_DB_TXN_BEGIN", "true")
        conn = _Conn()
        _begin_txn(conn)
        assert conn.begins == 1 and getattr(conn, "_rag_txn_begun", False) is True


class TestPerf2RecordToolCall:
    def test_single_commit_budget_plus_step(self, monkeypatch):
        """预算+心跳+tool_call step 一次 commit（旧=预算裸 UPDATE 独立提交）。"""
        store, conn = _wire(monkeypatch)
        store.record_tool_call("r1", payload={"tool_name": "kb.search"})
        assert conn.commits == 1
        joined = "\n".join(conn.cur.sqls)
        assert "tool_calls_used=tool_calls_used+1" in joined
        assert "heartbeat_at=NOW(3)" in joined
        assert "'tool_call'" in joined

    def test_executor_falls_back_when_store_lacks_method(self):
        from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
        from opensearch_pipeline.agent_runtime.tool import ToolResult
        calls = []

        class _S:
            def create_run(self, ctx, p):
                return "r1"

            def increment_budget(self, run_id, **kw):
                calls.append(kw)

        ex = ThreadedRunExecutor(_S(), lambda c, e: ToolResult.text_ok("x"),
                                 max_concurrent=1)
        ex._budget_used("r1", tool_calls=1)
        assert calls and calls[0].get("tool_calls") == 1


class TestPerf3LlmFold:
    def test_record_turn_writes_llm_rows_same_tx(self, monkeypatch):
        store, conn = _wire(monkeypatch)
        store.record_turn("r1", turn_index=0, tokens_total=5,
                          llm_rows=[{"provider": "dashscope", "model": "m",
                                     "category": "light", "status": "ok",
                                     "latency_ms": 9}])
        assert conn.commits == 1
        assert any("llm_call_log" in s for s in conn.cur.sqls)

    def test_gateway_folds_success_into_ctx_buffer(self, monkeypatch):
        """探针翻绿：成功调用日志入 ctx 缓冲、不再热路径直写。"""
        from opensearch_pipeline.agent_runtime.model_gateway import ModelGateway, Usage
        logged = []
        gw = ModelGateway(providers={}, routes={}, call_logger=lambda **kw: logged.append(kw))
        ctx = SimpleNamespace(run_id="r1", request_id="q", acl_groups=["g"],
                              user_id="u", prompt_version=None, _llm_fold=[])
        gw._record_call(ctx, "light", "dashscope", "m",
                        Usage(tokens_prompt=1, tokens_completion=2), 12, "ok")
        assert logged == [] and len(ctx._llm_fold) == 1
        assert ctx._llm_fold[0]["status"] == "ok" and ctx._llm_fold[0]["call_id"]

    def test_gateway_failed_attempt_stays_sync(self, monkeypatch):
        """失败 attempt 维持同步直写（罕见+天然 durable——#4 的记录在案取舍）。"""
        from opensearch_pipeline.agent_runtime.model_gateway import ModelGateway
        logged = []
        gw = ModelGateway(providers={}, routes={}, call_logger=lambda **kw: logged.append(kw))
        ctx = SimpleNamespace(run_id="r1", request_id="q", acl_groups=["g"],
                              user_id="u", prompt_version=None, _llm_fold=[])
        gw._record_call(ctx, "light", "dashscope", "m", None, 12, "error")
        assert len(logged) == 1 and ctx._llm_fold == []

    def test_fold_kill_switch(self, monkeypatch):
        from opensearch_pipeline.agent_runtime.model_gateway import ModelGateway, Usage
        monkeypatch.setenv("RAG_AGENT_LLM_LOG_TURN_FOLD", "false")
        logged = []
        gw = ModelGateway(providers={}, routes={}, call_logger=lambda **kw: logged.append(kw))
        ctx = SimpleNamespace(run_id="r1", request_id="q", acl_groups=["g"],
                              user_id="u", prompt_version=None, _llm_fold=[])
        gw._record_call(ctx, "light", "dashscope", "m",
                        Usage(tokens_prompt=1, tokens_completion=2), 12, "ok")
        assert len(logged) == 1 and ctx._llm_fold == []

    def test_executor_drains_fold_into_record_turn(self):
        from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
        from opensearch_pipeline.agent_runtime.tool import ToolResult
        seen = {}

        class _S:
            def create_run(self, ctx, p):
                return "r1"

            def record_turn(self, run_id, *, llm_rows=None, **kw):
                seen["rows"] = list(llm_rows or [])

        ex = ThreadedRunExecutor(_S(), lambda c, e: ToolResult.text_ok("x"),
                                 max_concurrent=1)
        ctx = SimpleNamespace(_llm_fold=[{"status": "ok"}])
        ex._record_turn("r1", 0, usage=None, ctx=ctx)
        assert seen["rows"] == [{"status": "ok"}]
        assert ctx._llm_fold == []                       # 缓冲已清（不双写）

    def test_legacy_store_typeerror_flushes_rows(self):
        from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
        from opensearch_pipeline.agent_runtime.tool import ToolResult
        flushed = []

        class _S:
            def create_run(self, ctx, p):
                return "r1"

            def record_turn(self, run_id, *, turn_index, tokens_prompt=None,
                            tokens_completion=None, tokens_total=0, final=False):
                return None                              # 旧签名：无 llm_rows

            def record_llm_calls(self, rows):
                flushed.extend(rows)

        ex = ThreadedRunExecutor(_S(), lambda c, e: ToolResult.text_ok("x"),
                                 max_concurrent=1)
        ctx = SimpleNamespace(_llm_fold=[{"status": "ok"}])
        ex._record_turn("r1", 0, usage=None, ctx=ctx)
        assert flushed == [{"status": "ok"}]             # 旧桩不丢账
