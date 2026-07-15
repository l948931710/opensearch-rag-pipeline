# -*- coding: utf-8 -*-
"""test_general_answerer.py — 通用能力处理器单测：确定性计算器（评审补充 3：
绝不 eval、防资源放大）、canned 模板、通用回答（仿真桩/免责尾注/模型解析/公司边界
由评测集把关，这里测机制）。"""

import types

import pytest

import opensearch_pipeline.general_answerer as GA
from opensearch_pipeline.config import LLMConfig, RAGConfig
from opensearch_pipeline.general_answerer import (
    CANNED_SMALLTALK,
    GENERAL_DISCLAIMER_KNOWLEDGE,
    GENERAL_DISCLAIMER_OFFICE,
    answer_general,
    try_deterministic_calc,
)


def _cfg(monkeypatch, *, simulate=True, api_key="", general_model="", max_tokens=800):
    cfg = types.SimpleNamespace(
        rag=RAGConfig(general_llm_model=general_model, general_max_tokens=max_tokens),
        llm=LLMConfig(api_key=api_key, api_base_url="https://example.invalid/v1",
                      model="qwen-main"),
        simulate_api=simulate,
    )
    monkeypatch.setattr(GA, "get_config", lambda: cfg)
    return cfg


# ──────────────────────────────────────────────────────────────
# 1. 确定性计算器
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q,expected", [
    ("123*45等于多少", "5535"),
    ("1+2", "3"),
    ("(3+5)*2 = ?", "16"),
    ("100÷4", "25"),
    ("3×7等于几", "21"),
    ("(3+5)*20%", "1.6"),
    ("2**8", "256"),
])
def test_calc_arithmetic(q, expected):
    out = try_deterministic_calc(q)
    assert out is not None and out.endswith(expected)


@pytest.mark.parametrize("q,expected", [
    ("3公里等于多少米", "3000 米"),
    ("50kg换算成斤", "100 斤"),
    ("1.5吨等于多少公斤", "1500 公斤"),
    ("2升换算成毫升", "2000 毫升"),
    ("100摄氏度换算成华氏度", "212 华氏度"),
    ("32华氏度换算成摄氏度", "0 摄氏度"),
])
def test_calc_unit_conversion(q, expected):
    out = try_deterministic_calc(q)
    assert out is not None and out.endswith(expected)


@pytest.mark.parametrize("q", [
    "鸡兔同笼怎么算",              # 无法确定性解析 → 回落 LLM
    "9**999999",                   # 幂放大防御
    "1/0",                         # 除零
    "100万美元等于多少人民币",      # 汇率=实时数据，不在静态表
    "1+2+" ,                       # 语法错误
    "",                            # 空
    "3米等于多少公斤",              # 跨量纲
])
def test_calc_refuses_unparseable_or_unsafe(q):
    assert try_deterministic_calc(q) is None


def test_calc_never_uses_eval():
    """安全红线：模块内不得出现 eval/exec 调用。"""
    import inspect
    src = inspect.getsource(GA)
    assert "eval(" not in src.replace("_safe_eval_arith", "").replace("literal_eval", "")
    assert "exec(" not in src


# ──────────────────────────────────────────────────────────────
# 2. canned 模板与通用回答
# ──────────────────────────────────────────────────────────────

def test_canned_templates_declare_boundary():
    assert set(CANNED_SMALLTALK) == {"greeting", "capability", "thanks"}
    # 能力介绍即边界声明（涉公司内容只依据知识库）
    assert "知识库" in CANNED_SMALLTALK["capability"]


def test_answer_general_calc_bypasses_llm(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="sk-x")
    monkeypatch.setattr(GA, "http_post",
                        lambda *a, **k: pytest.fail("确定性计算不应调用 LLM"))
    out = answer_general("123*45等于多少", tier="office")
    assert out["model"] == "calc"
    assert out["answer"].endswith("5535")


def test_answer_general_simulate_stub_with_disclaimer(monkeypatch):
    _cfg(monkeypatch, simulate=True)
    out = answer_general("帮我翻译 hello", tier="office")
    assert out["model"] == "sim-general"
    assert out["answer"].endswith(GENERAL_DISCLAIMER_OFFICE)
    out = answer_general("天为什么是蓝的", tier="general")
    assert out["answer"].endswith(GENERAL_DISCLAIMER_KNOWLEDGE)


class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def test_answer_general_llm_payload_and_disclaimer(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="sk-x", general_model="qwen-turbo-x",
         max_tokens=321)
    seen = {}

    def _capture(url, json=None, **k):
        seen["payload"] = json
        return _FakeResp("Hello!")

    monkeypatch.setattr(GA, "http_post", _capture)
    out = answer_general("帮我翻译：你好", tier="office",
                         history=[{"role": "user", "content": "早"},
                                  {"role": "assistant", "content": "早上好"}])
    assert seen["payload"]["model"] == "qwen-turbo-x"
    assert seen["payload"]["max_tokens"] == 321
    assert seen["payload"]["enable_thinking"] is False
    # system + 2 轮历史 + 本问
    assert [m["role"] for m in seen["payload"]["messages"]] == \
        ["system", "user", "assistant", "user"]
    assert out["answer"] == "Hello!" + GENERAL_DISCLAIMER_OFFICE
    assert out["model"] == "qwen-turbo-x"
    assert out["sources"] == []


def test_general_model_falls_back_to_main_model(monkeypatch):
    """RAG_GENERAL_LLM_MODEL 空 = 复用主模型（开箱可用，不押注未核实的型号名）。"""
    _cfg(monkeypatch, simulate=False, api_key="sk-x", general_model="")
    assert GA._general_model() == "qwen-main"
