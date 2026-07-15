# -*- coding: utf-8 -*-
"""test_intent_router.py — 通用能力路由层单测（设计见 docs/general_ability_opening_design.md）。

重点钉死硬性不变量：
  I1 公司信号一票否决先于办公白名单（涉公司的"帮我翻译/写"永不离开知识库路径）；
  I3 分诊 fail-closed（仿真/无 key/超时/解析失败/未知类别/低置信/禁兜底词 → enterprise）；
  I4 mode=off 短路（行为与现状一致）。
"""

import types

import pytest

import opensearch_pipeline.intent_router as IR
from opensearch_pipeline.config import LLMConfig, RAGConfig
from opensearch_pipeline.intent_router import (
    ROUTE_KB,
    ROUTE_OFFICE,
    ROUTE_REALTIME,
    ROUTE_SENSITIVE,
    ROUTE_SMALLTALK,
    TRIAGE_ENTERPRISE,
    TRIAGE_OFFICE,
    TRIAGE_SENSITIVE,
    pre_route,
    triage_failed_query,
)


def _cfg(monkeypatch, *, mode="office", depts="", extra_words="",
         simulate=True, api_key="", triage_timeout=6):
    cfg = types.SimpleNamespace(
        rag=RAGConfig(general_ability_mode=mode, general_ability_depts=depts,
                      sensitive_extra_words=extra_words, triage_timeout=triage_timeout),
        llm=LLMConfig(api_key=api_key, api_base_url="https://example.invalid/v1",
                      model="qwen-test"),
        simulate_api=simulate,
    )
    monkeypatch.setattr(IR, "get_config", lambda: cfg)
    return cfg


class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


# ──────────────────────────────────────────────────────────────
# 1. pre_route：I4 off 短路 + 各路由
# ──────────────────────────────────────────────────────────────

def test_mode_off_short_circuits_to_kb(monkeypatch):
    """I4：off（含非法值）下寒暄/翻译/敏感全部原样走知识库 —— 行为与现状一致。"""
    for mode in ("off", "", "bogus"):
        _cfg(monkeypatch, mode=mode)
        for q in ("你好", "帮我翻译一下这段话", "谁的工资最高", "今天天气怎么样"):
            assert pre_route(q).route == ROUTE_KB


# I1 不变量样本：涉公司制度/客户/订单/系统/部门/人员/产品的问题，
# 即使以"帮我写/翻译/润色/总结"祈使形式提问，也必须一票否决走知识库。
_I1_COMPANY_QUERIES = [
    "帮我把报销流程翻译成英文",
    "写一封感谢员工的邮件",
    "帮我润色这份年假申请",
    "总结一下考勤制度",
    "把这份客户订单信息翻译成英文",
    "U8系统怎么开销售单",
    "富岭的吸管耐温多少度",
    "帮我写一份给供应商的合同模板",
    "FL-QC-015-002 讲了什么",
    "人力资源部的入职流程",
    "帮我算一下车间的物料库存",
    "我司PLA降解餐具的报价",
    "写个通知：明天厂里停电",
    "帮我把生产计划翻译成英文",
]


@pytest.mark.parametrize("q", _I1_COMPANY_QUERIES)
def test_i1_company_signal_vetoes_office_whitelist(monkeypatch, q):
    _cfg(monkeypatch, mode="full")   # 最宽档也不放行
    assert pre_route(q).route == ROUTE_KB


def test_smalltalk_routes(monkeypatch):
    _cfg(monkeypatch, mode="smalltalk")
    assert pre_route("你好").route == ROUTE_SMALLTALK
    assert pre_route("你好").sub == "greeting"
    assert pre_route("谢谢！").sub == "thanks"
    assert pre_route("你能做什么？").sub == "capability"
    # 长句不整句匹配（礼貌前缀不劫持真实问题）
    assert pre_route("你好，请问出差住宿标准是多少钱一晚").route == ROUTE_KB


def test_office_requires_mode_and_identity(monkeypatch):
    _cfg(monkeypatch, mode="smalltalk")
    assert pre_route("帮我把这段话翻译成英文").route == ROUTE_KB   # 档位不够
    _cfg(monkeypatch, mode="office")
    assert pre_route("帮我把这段话翻译成英文").route == ROUTE_OFFICE
    assert pre_route("帮我把这段话翻译成英文", is_user=False).route == ROUTE_KB   # 匿名
    assert pre_route("帮我把这段话翻译成英文",
                     conversation_type="2").route == ROUTE_KB   # 群聊
    assert pre_route("123*45等于多少").route == ROUTE_OFFICE   # 计算祈使


def test_office_dept_allowlist(monkeypatch):
    _cfg(monkeypatch, mode="office", depts="it,admin")
    q = "帮我润色这段自我介绍"
    assert pre_route(q, user_dept=["it"]).route == ROUTE_OFFICE
    assert pre_route(q, user_dept=["hr"]).route == ROUTE_KB
    assert pre_route(q, user_dept=None).route == ROUTE_KB   # 无部门（匿名）
    _cfg(monkeypatch, mode="office", depts="")
    assert pre_route(q, user_dept=["hr"]).route == ROUTE_OFFICE   # 空 = 全员


def test_sensitive_hard_patterns(monkeypatch):
    _cfg(monkeypatch, mode="smalltalk")
    assert pre_route("谁的工资比我高").route == ROUTE_SENSITIVE
    assert pre_route("哪个同事被辞退了").route == ROUTE_SENSITIVE
    assert pre_route("怎么起诉公司").route == ROUTE_SENSITIVE
    # 制度性问题不拦（知识库的正当领地，误拦比放行更伤）
    assert pre_route("工资几号发").route == ROUTE_KB
    assert pre_route("加班工资是多少").route == ROUTE_KB
    assert pre_route("辞退赔偿的制度规定").route == ROUTE_KB


def test_sensitive_extra_words_env(monkeypatch):
    _cfg(monkeypatch, mode="office", extra_words="内部代号,X项目")
    assert pre_route("X项目是什么").route == ROUTE_SENSITIVE


def test_realtime_block(monkeypatch):
    _cfg(monkeypatch, mode="smalltalk")
    assert pre_route("今天天气怎么样").route == ROUTE_REALTIME
    assert pre_route("美元汇率是多少").route == ROUTE_REALTIME
    # 工艺温度类问题有公司域词兜底（注塑 → 否决走知识库）
    assert pre_route("注塑温度设多少").route == ROUTE_KB


# ──────────────────────────────────────────────────────────────
# 2. triage_failed_query：I3 fail-closed
# ──────────────────────────────────────────────────────────────

def _no_http(monkeypatch):
    monkeypatch.setattr(IR.requests, "post",
                        lambda *a, **k: pytest.fail("此场景不应发起 LLM 调用"))


def test_triage_simulate_mode_fail_closed(monkeypatch):
    _cfg(monkeypatch, simulate=True, api_key="sk-x")
    _no_http(monkeypatch)
    assert triage_failed_query("珠海有什么好玩的") == TRIAGE_ENTERPRISE


def test_triage_no_key_fail_closed(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="")
    _no_http(monkeypatch)
    assert triage_failed_query("珠海有什么好玩的") == TRIAGE_ENTERPRISE


def test_triage_strong_company_signal_short_circuits(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="sk-x")
    _no_http(monkeypatch)
    assert triage_failed_query("富岭的年营收是多少") == TRIAGE_ENTERPRISE
    assert triage_failed_query("U8里怎么改凭证") == TRIAGE_ENTERPRISE


def test_triage_no_fallback_words_short_circuit(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="sk-x")
    _no_http(monkeypatch)
    assert triage_failed_query("劳动仲裁流程是什么") == TRIAGE_ENTERPRISE


def test_triage_sensitive_pattern_short_circuits(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="sk-x")
    _no_http(monkeypatch)
    assert triage_failed_query("哪个同事被辞退了") == TRIAGE_SENSITIVE


def test_triage_valid_high_confidence(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="sk-x")
    monkeypatch.setattr(
        IR.requests, "post",
        lambda *a, **k: _FakeResp('{"category": "office", "confidence": "high"}'))
    assert triage_failed_query("帮我把这首诗改成七言") == TRIAGE_OFFICE


@pytest.mark.parametrize("content", [
    '{"category": "office", "confidence": "low"}',     # 低置信
    '{"category": "office"}',                          # 缺 confidence
    '{"category": "chitchat", "confidence": "high"}',  # 未知类别
    '{"category": 3, "confidence": "high"}',           # 类型错误
    "office",                                          # 非 JSON
    "[]",                                              # 非对象
])
def test_triage_malformed_or_low_confidence_fail_closed(monkeypatch, content):
    """I3：任何不合规输出一律按 enterprise 处理（维持今日拒答）。"""
    _cfg(monkeypatch, simulate=False, api_key="sk-x")
    monkeypatch.setattr(IR.requests, "post", lambda *a, **k: _FakeResp(content))
    assert triage_failed_query("这个问题很模糊") == TRIAGE_ENTERPRISE


def test_triage_http_error_fail_closed(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="sk-x")

    def _boom(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(IR.requests, "post", _boom)
    assert triage_failed_query("这个问题很模糊") == TRIAGE_ENTERPRISE


def test_triage_passes_titles_as_evidence(monkeypatch):
    _cfg(monkeypatch, simulate=False, api_key="sk-x")
    seen = {}

    def _capture(url, json=None, **k):
        seen["payload"] = json
        return _FakeResp('{"category": "general", "confidence": "high"}')

    monkeypatch.setattr(IR.requests, "post", _capture)
    assert triage_failed_query("这个问题很模糊", titles=["出差制度", "考勤办法"]) == "general"
    user_msg = seen["payload"]["messages"][1]["content"]
    assert "出差制度" in user_msg and "考勤办法" in user_msg
    assert seen["payload"]["enable_thinking"] is False
    assert seen["payload"]["temperature"] == 0
