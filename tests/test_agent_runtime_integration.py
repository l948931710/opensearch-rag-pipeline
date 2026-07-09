# -*- coding: utf-8 -*-
"""
test_agent_runtime_integration.py — 端到端影子链路（减 HTTP 入口）（Task 14 收尾）

把全链拼起来跑一轮真实组装：
  ExecutionContext → ThreadedRunExecutor(B1(b)) → DefaultAgentLoop
    → make_model_fn ← ModelGateway ← provider（提 knowledge_search 工具）
    → adjudicator(schema 校验 + PolicyEngine deny-first allow + registry.resolve)
    → KnowledgeSearchTool.run → retrieve_and_enrich（mock；断言 ACL 从 ctx 一路注入）
    → 结果回注模型 → 最终答案 RunCompleted，run_store 状态机 running→succeeded。
证明 12+ 模块协同闭合。真检索/真模型均以桩隔离。
"""
from opensearch_pipeline.agent_runtime import (
    ChatResponse,
    DefaultAgentLoop,
    ExecutionContext,
    ModelGateway,
    ThreadedRunExecutor,
    ToolCall,
    Usage,
    default_policy_engine,
    make_adjudicator,
    make_model_fn,
)
from opensearch_pipeline.agent_runtime.events import RunCompleted, ToolCallProposed
from opensearch_pipeline.agent_tools import build_default_registry


class _FakeStore:
    def __init__(self):
        self.transitions = []
        self.budget = {"turns_used": 0, "tool_calls_used": 0, "tokens_used": 0}
        self._n = 0
        self._step = 0
        self.invocations = []

    def create_run(self, ctx, profile):
        self._n += 1
        return f"run{self._n}"

    def transition(self, run_id, frm, to):
        self.transitions.append((run_id, frm, to))
        return True

    def consume_budget(self, run_id, *, turns=0, tool_calls=0, tokens=0):
        self.budget["turns_used"] += turns
        self.budget["tool_calls_used"] += tool_calls
        self.budget["tokens_used"] += tokens
        return dict(self.budget)

    def append_step(self, run_id, step):
        self._step += 1
        return self._step

    def record_invocation(self, run_id, step_no, **kw):
        iid = f"inv{len(self.invocations) + 1}"
        self.invocations.append({"id": iid, "step_no": step_no, **kw})
        return iid

    def finish_invocation(self, invocation_id, **kw):
        for inv in self.invocations:
            if inv["id"] == invocation_id:
                inv.update(kw)

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return None


class _FakeProvider:
    name = "dashscope"

    def capabilities(self, model):
        return None

    def chat(self, model, req):
        # 已有工具结果 → 给最终答案；否则提 knowledge_search
        if any(m.get("role") == "tool" for m in req.messages):
            return ChatResponse(text="根据知识库：富岭包装应遵循 GB/T 标准。",
                                tool_calls=[], usage=Usage(tokens_prompt=20, tokens_completion=10),
                                model=model)
        return ChatResponse(text="", tool_calls=[ToolCall(id="c1", name="knowledge_search",
                                                          arguments={"query": "富岭包装规范"})],
                            usage=Usage(tokens_prompt=15, tokens_completion=5), model=model)


def _ctx():
    return ExecutionContext.create(request_id="r", user_id="u",
                                   acl_groups=["production", "marketing"], roles=["employee"],
                                   channel="console", thread_id="t")


def test_shadow_link_end_to_end(monkeypatch):
    captured = {}

    def fake_retrieve(query, *, top_k=None, user_dept=None, **k):
        captured["user_dept"] = user_dept          # ACL 从 ctx 一路注入到检索
        captured["query"] = query
        return [{"doc_id": "D1", "chunk_text": "GB/T 包装标准全文…", "doc_title": "包装规范"}]

    monkeypatch.setattr("opensearch_pipeline.retriever.retrieve_and_enrich", fake_retrieve)

    registry = build_default_registry()
    store = _FakeStore()
    adjudicator = make_adjudicator(registry, default_policy_engine(), store)
    gateway = ModelGateway({"dashscope": _FakeProvider()},
                           routes={"default": [("dashscope", "qwen3.6-plus")]})
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=2)
    ctx = _ctx()
    loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "default"))
    tools = [registry.get("knowledge_search").spec]

    handle = executor.submit(ctx, loop, [{"role": "user", "content": "富岭包装规范?"}], tools)
    events = list(handle.events())
    executor.shutdown()

    # 工具被提案并执行、ACL 一路注入、最终答案产出、run 成功
    assert any(isinstance(e, ToolCallProposed) and e.tool_name == "knowledge_search" for e in events)
    completed = [e for e in events if isinstance(e, RunCompleted)]
    assert completed and "GB/T" in completed[0].final_text
    assert captured["user_dept"] == ["production", "marketing"]   # 🔒 ctx.acl_groups → retrieve
    assert captured["query"] == "富岭包装规范"
    assert ("run1", "running", "succeeded") in store.transitions
    assert store.budget["tool_calls_used"] == 1
    # tool_invocation 落库:knowledge_search 执行 → executing→succeeded
    assert store.invocations and store.invocations[-1]["status"] == "succeeded"
    assert store.invocations[-1]["tool_name"] == "knowledge_search"
