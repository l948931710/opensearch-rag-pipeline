# -*- coding: utf-8 -*-
"""test_query_rewriter.py — 多轮追问检索前改写（RAG_FOLLOWUP_REWRITE，默认关）。

铁律验证：flag 关=零行为；门控绝不因「history 非空且短」就触发（有效短问题不调 LLM）；
历史切片只含 user/assistant 可见消息；输出确定性 cleanup + 护栏；任何失败 fail-open。
patch 接缝：query_rewriter._http_post（http_session 惯例）。
"""
import pytest

import opensearch_pipeline.query_rewriter as qr

_HIST = [{"role": "user", "content": "U8 采购入库怎么操作？"},
         {"role": "assistant", "content": "第一步打开采购管理…第二步…"}]


# ── 门控 looks_followup ──────────────────────────────────────────────────
def test_gate_requires_referential_signal_not_just_short():
    """约束 1：仅「history 非空且短」不触发——必须命中指代/接续/缺主体。"""
    for q in ("怎么请假", "如何报销", "库存多少", "请假流程"):
        assert not qr.looks_followup(q, _HIST), q
    for q in ("那第二步呢", "然后呢", "上面那个", "还有别的吗", "第3步呢"):
        assert qr.looks_followup(q, _HIST), q


def test_gate_no_history_or_junk_never_triggers():
    assert not qr.looks_followup("那第二步呢", [])       # 无历史
    assert not qr.looks_followup("那第二步呢", None)
    assert not qr.looks_followup("？？？", _HIST)         # 确定性垃圾不值得一次 LLM
    assert not qr.looks_followup("", _HIST)


def test_gate_long_referential_query_not_triggered():
    """含指代词但内容自足的长问题：超 2×MAX_LEN 辅助上限 → 不触发。"""
    q = "那台注塑机换模流程的第二步操作里质检确认环节的审批人是谁需要哪些表单"
    assert not qr.looks_followup(q, _HIST)


# ── 历史切片 build_rewrite_history ───────────────────────────────────────
def test_history_slice_role_whitelist_and_cap():
    """约束 4：只留 user/assistant；system/tool/隐藏消息一律丢；截最近 3 轮。"""
    hist = ([{"role": "system", "content": "你是内部系统提示"},
             {"role": "tool", "content": "tool trace"},
             {"role": "", "content": "无角色"}]
            + [{"role": "user", "content": f"问{i}"} for i in range(5)]
            + [{"role": "assistant", "content": "答"}])
    out = qr.build_rewrite_history(hist)
    assert all(m["role"] in ("user", "assistant") for m in out)
    assert len(out) <= 6
    assert all("系统提示" not in m["content"] and "tool trace" not in m["content"] for m in out)


def test_history_slice_truncates_long_content():
    out = qr.build_rewrite_history([{"role": "assistant", "content": "长" * 2000}])
    assert len(out[0]["content"]) == 500


# ── 输出 cleanup 护栏 ────────────────────────────────────────────────────
def test_cleanup_strips_model_prefixes_and_quotes():
    """约束 5：剥「完整问题：/改写后：」等口癖前缀与引号，纯确定性。"""
    assert qr._cleanup_output("完整问题：U8 第二步怎么操作", "那第二步呢") == "U8 第二步怎么操作"
    assert qr._cleanup_output("改写后：「U8 第二步怎么操作」", "那第二步呢") == "U8 第二步怎么操作"
    assert qr._cleanup_output("“U8 第二步怎么操作”", "那第二步呢") == "U8 第二步怎么操作"


def test_cleanup_guardrails():
    assert qr._cleanup_output("", "q") is None                       # 空输出
    assert qr._cleanup_output("超" * 300, "q") is None               # 超长=跑偏
    assert qr._cleanup_output("那第二步呢", "那第二步呢") is None      # 与原文同=未改写
    assert qr._cleanup_output("那第二步呢！", "那第二步呢") is None    # 归一化后同=未改写


# ── rewrite_followup / maybe_rewrite ─────────────────────────────────────
def _fake_llm(monkeypatch, content):
    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    def _post(url, **kw):
        calls.append((url, kw))
        return _Resp()
    monkeypatch.setattr(qr, "_http_post", _post)
    return calls


def test_rewrite_happy_path(monkeypatch):
    calls = _fake_llm(monkeypatch, "完整问题：U8 采购入库的第二步怎么操作")
    out = qr.rewrite_followup("那第二步呢", _HIST)
    assert out == "U8 采购入库的第二步怎么操作"
    payload = calls[0][1]["json"]
    assert payload["temperature"] == 0 and payload["stream"] is False
    # 历史切片进了 messages（system 提示 + 历史 + 当前追问）
    roles = [m["role"] for m in payload["messages"]]
    assert roles[0] == "system" and roles[-1] == "user"
    assert "那第二步呢" in payload["messages"][-1]["content"]


def test_rewrite_llm_failure_fails_open(monkeypatch):
    def _boom(url, **kw):
        raise RuntimeError("dashscope down")
    monkeypatch.setattr(qr, "_http_post", _boom)
    assert qr.rewrite_followup("那第二步呢", _HIST) is None


def test_maybe_rewrite_flag_off_is_noop(monkeypatch):
    """默认关：不调 LLM（_http_post 若被调则测试炸）。"""
    monkeypatch.delenv("RAG_FOLLOWUP_REWRITE", raising=False)

    def _boom(url, **kw):
        raise AssertionError("flag off 不应发起 LLM 调用")
    monkeypatch.setattr(qr, "_http_post", _boom)
    assert qr.maybe_rewrite("那第二步呢", _HIST) is None


def test_maybe_rewrite_flag_on_gated(monkeypatch):
    monkeypatch.setenv("RAG_FOLLOWUP_REWRITE", "true")
    _fake_llm(monkeypatch, "U8 采购入库的第二步怎么操作")
    assert qr.maybe_rewrite("那第二步呢", _HIST) == "U8 采购入库的第二步怎么操作"
    # 门控不命中（有效短问题）→ 不调 LLM
    def _boom(url, **kw):
        raise AssertionError("门控未命中不应调用 LLM")
    monkeypatch.setattr(qr, "_http_post", _boom)
    assert qr.maybe_rewrite("怎么请假", _HIST) is None


def test_api_prepare_ask_uses_rewritten_for_retrieval(monkeypatch):
    """api 接线：flag 开 + 追问 → retrieve_and_enrich 收到改写后的 query，
    返回值携带 rewritten_query 供落库。"""
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")
    from opensearch_pipeline import api
    monkeypatch.setattr("opensearch_pipeline.query_rewriter.maybe_rewrite",
                        lambda q, h: "U8 采购入库的第二步怎么操作")
    seen = {}

    def _fake_retrieve(query, **kw):
        seen["query"] = query
        return [{"chunk_text": "x", "score": 9.0}]
    monkeypatch.setattr(api, "retrieve_and_enrich", _fake_retrieve)

    class _Req:
        question = "那第二步呢"
        session_id = None
        history = [api.ChatMessage(role="user", content="U8 采购入库怎么操作？"),
                   api.ChatMessage(role="assistant", content="第一步…")]
        top_k = 7
        user_id = ""
        conversation_id = None
    out = api._prepare_ask(_Req(), None)
    assert seen["query"] == "U8 采购入库的第二步怎么操作"
    assert out[-1] == "U8 采购入库的第二步怎么操作"   # rewritten_query 随返回值透传


def test_api_prepare_ask_flag_off_uses_original(monkeypatch):
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")
    monkeypatch.delenv("RAG_FOLLOWUP_REWRITE", raising=False)
    from opensearch_pipeline import api
    seen = {}

    def _fake_retrieve(query, **kw):
        seen["query"] = query
        return []
    monkeypatch.setattr(api, "retrieve_and_enrich", _fake_retrieve)

    class _Req:
        question = "那第二步呢"
        session_id = None
        history = None
        top_k = 7
        user_id = ""
        conversation_id = None
    out = api._prepare_ask(_Req(), None)
    assert seen["query"] == "那第二步呢" and out[-1] is None
