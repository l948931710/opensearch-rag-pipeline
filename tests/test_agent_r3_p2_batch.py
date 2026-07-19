# -*- coding: utf-8 -*-
"""R3-P2（ARR 外审 P2-RT-11..25）抽样回归：每项一锚点。"""
from types import SimpleNamespace

import pytest


def test_p2_13_sanitizer_keys_and_numbers():
    from opensearch_pipeline.agent_runtime.sanitize import sanitize_args
    out = sanitize_args({"phone": 13812345678, "身份证": "110101199001011234",
                        "note": "正常文本", "count": 42, "big": 999999999999})
    assert out["phone"] == "***" and out["身份证"] == "***"      # 键感知（含数字标量）
    assert out["count"] == 42                                    # 短数字不动
    assert out["big"] == "***"                                   # 11+ 位裸数字掩码
    assert out["note"]                                           # 普通文本保留


def test_p2_16_run_suspended_internal_fields_excluded():
    from opensearch_pipeline.agent_runtime.events import RunSuspended, dump_event
    ev = RunSuspended(state_messages=[{"role": "user", "content": "秘密"}],
                      remaining_calls=[{"tool": "x"}], approval_request_id="a")
    d = dump_event(ev)
    assert "state_messages" not in d and "remaining_calls" not in d


def test_p2_22_no_sql_preauth():
    from opensearch_pipeline.agent_runtime.policy import default_policy_engine
    eng = default_policy_engine()
    scopes = [s for r in getattr(eng, "rules", []) for s in getattr(r, "scopes", ())]
    if not scopes:                                   # 引擎不暴露 rules：退源码断言
        from pathlib import Path as _P
        import opensearch_pipeline.agent_runtime.policy as _pol
        src = _P(_pol.__file__).read_text(encoding="utf-8")
        assert 'PolicyRule(effect="allow", scopes=("kb.search", "sql.readonly.*")' not in src
        return
    assert "sql.readonly.*" not in scopes and "kb.search" in scopes


def test_p2_23_high_tier_has_fallback():
    from opensearch_pipeline.agent_runtime.model_gateway import _DEFAULT_ROUTES
    assert len(_DEFAULT_ROUTES["high"]) >= 2                     # 用户档不再单模型裸奔
    assert len(_DEFAULT_ROUTES["light"]) == 1                    # 廉价档有意快败


def test_p2_25_egress_guard():
    from opensearch_pipeline.agent_runtime.context import ExecutionContext, RunBudget
    from opensearch_pipeline.agent_runtime.model_gateway import (
        ChatRequest, ModelError, ModelGateway)
    ctx = ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"],
                                  roles=["employee"], channel="console", thread_id="t",
                                  budget=RunBudget(max_turns=2))
    object.__setattr__(ctx, "max_data_classification", "confidential")
    gw = ModelGateway(providers={}, routes={})
    import os
    os.environ["RAG_AGENT_EGRESS_MAX_CLASS"] = "internal"
    try:
        with pytest.raises(ModelError, match="密级"):
            gw.complete(ctx, "light", ChatRequest(messages=[]))
    finally:
        os.environ.pop("RAG_AGENT_EGRESS_MAX_CLASS", None)
    gw._egress_guard(ctx)                                        # 未配置=不启用


def test_p2_17_drain_uses_task_ledger(monkeypatch):
    import opensearch_pipeline.agent_runtime.llm_log_outbox as ob
    import queue as q
    fake = q.Queue()
    monkeypatch.setattr(ob, "_q", fake)
    assert ob.drain_llm_log(timeout=0.2) is True                 # 账本归零=清空
    fake.put({"x": 1})
    assert ob.drain_llm_log(timeout=0.2) is False                # 在途未 task_done=不清
    fake.get()
    fake.task_done()
    assert ob.drain_llm_log(timeout=0.2) is True


def test_p2_11_finish_invocation_cas(monkeypatch):
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    class _Cur:
        def __init__(self, rc):
            self.rowcount_v = rc
            self.sqls = []

        def execute(self, sql, args=None):
            self.sqls.append(sql)
            self.rowcount = self.rowcount_v

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _wire(rc):
        cur = _Cur(rc)
        store = RDSRunStore()
        conn = SimpleNamespace(cursor=lambda: cur, commit=lambda: None,
                               rollback=lambda: None, close=lambda: None)
        monkeypatch.setattr(store, "_conn", lambda: conn)
        monkeypatch.setattr("opensearch_pipeline.agent_runtime.run_store._op_db",
                            lambda: "op")
        return store, cur

    store, cur = _wire(1)
    assert store.finish_invocation("i1", status="succeeded") is True
    assert "status='executing'" in cur.sqls[0]                   # 条件 CAS
    store2, _ = _wire(0)
    assert store2.finish_invocation("i2", status="succeeded") is False   # 输家不覆盖


def test_p2_14_obligations_preserve_artifacts():
    from opensearch_pipeline.agent_runtime.tool import ContentBlock, ToolResult
    from opensearch_pipeline.agent_runtime.tool_executor import (
        ToolExecutor, register_obligation_handler)
    register_obligation_handler(
        "noop_rebuild",
        lambda param, r: ToolResult(status=r.status, content=r.content,
                                    receipt=r.receipt))   # 义务重建=丢 artifacts 的形态
    r = ToolResult(status="succeeded", content=[ContentBlock.of_text("x")],
                   artifacts={"chunks": [1, 2]})
    out = ToolExecutor._apply_obligations_safe(("noop_rebuild:",), r)
    assert out.artifacts == {"chunks": [1, 2]}                   # 旁路载荷不静默消失
