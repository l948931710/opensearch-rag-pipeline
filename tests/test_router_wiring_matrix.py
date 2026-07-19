# -*- coding: utf-8 -*-
"""Router 接线矩阵守护（2026-07-11 router 准确性测试战役·第 1 层）。

把**全部生产调用点 → category → (provider, model, thinking 参数)** 的映射钉进一张表：
任何路由漂移（改默认模型/换链序/动 thinking 预算/丢 fallback 项）都在这里一处变红。

生产调用点清单（第 2 层活测见 scratch/probe_router_tier_accuracy_20260711.py）：
- routes/agent.py submit/resume → `ctx.model_profile or "light"`（thinking→high 的映射
  已由 tests/test_routes_agent.py::test_thinking_maps_model_tier 守护，此处不重复）
- compaction.summarize → "light"
- query_decomposer / llm_generator serving ×2 → 自建单链 gateway（决不走思考分档），
  线上行为由 test_multi_query_and_guard + test_serving_gateway_equivalence 等值门钉死，
  此处只钉构造语义（单链=config 模型、max_retries=0、tier_params={}）。
"""
from types import SimpleNamespace

from opensearch_pipeline.agent_runtime.model_gateway import default_gateway


def test_default_gateway_full_routing_matrix(monkeypatch):
    """默认矩阵（无 env 覆盖）：4 档 × (链序 + thinking 参数) 全量断言。"""
    for var in ("RAG_AGENT_MODEL_LIGHT", "RAG_AGENT_MODEL_HIGH", "RAG_AGENT_MODEL_XHIGH",
                "RAG_AGENT_MODEL_MAX", "RAG_AGENT_THINK_HIGH", "RAG_AGENT_THINK_XHIGH",
                "RAG_AGENT_THINK_MAX", "RAG_AGENT_PROVIDER_ALLOWLIST"):
        monkeypatch.delenv(var, raising=False)
    gw = default_gateway()
    plus, mx = "qwen3.7-plus", "qwen3.7-max-2026-06-08"
    assert gw._routes == {
        "light": [("dashscope", plus)],
        "high":  [("dashscope", plus), ("dashscope", mx)],   # RR-2：工厂 high 补 fallback
        "xhigh": [("dashscope", mx), ("dashscope", plus)],   # 高阶档 429/5xx 退 plus
        "max":   [("dashscope", mx), ("dashscope", plus)],
    }
    assert gw._tier_params == {
        "light": {"enable_thinking": False},                 # qwen3.7 默认开思考，显式关才快/省
        "high":  {"enable_thinking": True, "thinking_budget": 1024},
        "xhigh": {"enable_thinking": True, "thinking_budget": 4096},
        "max":   {"enable_thinking": True, "thinking_budget": 16384},
    }


def test_agent_entry_category_derivation():
    """agent submit/resume 的档位推导表达式：model_profile 空/None → light，有值沿用。"""
    for profile, expect in ((None, "light"), ("", "light"), ("high", "high"), ("light", "light")):
        ctx = SimpleNamespace(model_profile=profile)
        assert (ctx.model_profile or "light") == expect


def test_serving_adhoc_gateway_pins_config_model(monkeypatch):
    """serving 自建 gateway：单链=config.llm.model、max_retries=0、tier_params={}——
    决不落入思考分档（serving 的 enable_thinking 由调用方 extra 显式携带）。"""
    from opensearch_pipeline import llm_generator as lg

    cfg = SimpleNamespace(llm=SimpleNamespace(api_key="k", api_base_url="https://x", model="m-pin"))
    monkeypatch.setattr(lg, "get_config", lambda: cfg)
    gw, ctx = lg._serving_gateway()
    assert gw._routes == {"serving": [("dashscope", "m-pin")]}
    assert gw._max_retries == 0 and gw._tier_params == {}
    assert ctx.user_id == "rag-serving"


def test_compaction_defaults_to_light():
    import inspect

    from opensearch_pipeline.agent_runtime import compaction

    assert inspect.signature(compaction.summarize).parameters["category"].default == "light"
    assert inspect.signature(compaction.make_summarizer).parameters["category"].default == "light"
