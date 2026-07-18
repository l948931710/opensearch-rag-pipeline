# -*- coding: utf-8 -*-
"""test_general_ability_chains.py — 通用能力分级开放的链路级测试。

覆盖：
  · I4 flag-off：/api/ask 空结果与话术逐字节等于现状，前置层零介入；
  · flag-on 四类前置路由（寒暄 canned / 计算 / T2 仿真桩 / 敏感 BLOCKED / 实时信息）；
  · 失败序列：空结果引导拒答（sim 分诊 fail-closed → enterprise）、REFUSAL 替换；
  · 落库标注（intent_type / risk_blocked / answer_status 口径）；
  · 钉钉侧适配层（群聊降档、执行器配额文案映射）。
"""

import types
from unittest.mock import patch

import pytest

import opensearch_pipeline.config as CONF
from opensearch_pipeline.answer_flow import (
    GENERAL_QUOTA_MESSAGE,
    GENERAL_TIER_OFF_MESSAGE,
    NO_RESULT_MESSAGE,
    REALTIME_BLOCK_MESSAGE,
    SENSITIVE_BLOCK_MESSAGE,
)
from opensearch_pipeline.auth_token import issue_session_token
from opensearch_pipeline.rate_limiter import Denial


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    return TestClient(app)


def _auth(uid="U100", dept=None):
    return {"Authorization": "Bearer " + issue_session_token(uid, dept=dept or ["it"])}


@pytest.fixture
def general_on(monkeypatch):
    """开启通用能力（office 档 + 引导话术），并重置配置单例；退出时还原。"""
    monkeypatch.setenv("RAG_GENERAL_ABILITY_MODE", "office")
    monkeypatch.setenv("RAG_GUIDED_REFUSAL", "true")
    CONF._config = None
    yield
    CONF._config = None


# I4 基线：现状文案逐字节锚定（改动这条 = 改动现网行为，必须显式评审）
def test_i4_no_result_message_byte_identical():
    assert NO_RESULT_MESSAGE == "抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述您的问题。"


class TestFlagOff:
    """双 flag 默认关：行为与现状一致（I4）。"""

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_empty_retrieval_unchanged(self, mock_retrieve, mock_log, client):
        mock_retrieve.return_value = []
        resp = client.post("/api/ask", json={"question": "你好"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == NO_RESULT_MESSAGE   # 寒暄也照旧拒答（现状）
        assert data["no_result"] is True
        assert data["source"] == "kb"
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "NO_RESULT"
        assert kw["intent_type"] is None            # 存量落库载荷不写新维度
        assert kw["risk_blocked"] is False

    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_pre_route_not_engaged(self, mock_retrieve, client):
        """off 下前置层零介入：寒暄照样触发检索。"""
        mock_retrieve.return_value = []
        client.post("/api/ask", json={"question": "你好"})
        assert mock_retrieve.called


class TestPreRoute:
    """flag-on 前置确定性路由（检索永不发生 —— retrieve mock 设为爆炸断言）。"""

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_smalltalk_canned(self, mock_retrieve, mock_log, client, general_on):
        mock_retrieve.side_effect = AssertionError("前置命中不应触发检索")
        resp = client.post("/api/ask", json={"question": "你好"})
        data = resp.json()
        assert data["source"] == "smalltalk"
        assert data["model"] == "canned"
        assert "富岭知识库助手" in data["answer"]
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "SUCCESS"
        assert kw["intent_type"] == "smalltalk"
        assert kw["opensearch_hit_count"] is None   # chunks=None：检索未发生

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_calc_deterministic(self, mock_retrieve, mock_log, client, general_on):
        mock_retrieve.side_effect = AssertionError("前置命中不应触发检索")
        resp = client.post("/api/ask", json={"question": "123*45等于多少"},
                           headers=_auth())
        data = resp.json()
        assert data["model"] == "calc"
        assert data["answer"].endswith("5535")
        assert mock_log.call_args.kwargs["intent_type"] == "office"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_office_llm_sim_stub(self, mock_retrieve, mock_log, client, general_on):
        mock_retrieve.side_effect = AssertionError("前置命中不应触发检索")
        resp = client.post("/api/ask", json={"question": "帮我把这段话翻译成英文"},
                           headers=_auth())
        data = resp.json()
        assert data["source"] == "general"
        assert "不代表公司口径" in data["answer"]    # 免责尾注
        assert mock_log.call_args.kwargs["intent_type"] == "office"

    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_office_requires_identity(self, mock_retrieve, client, general_on):
        """匿名无 T2：翻译请求照走知识库（回落现状而非报错）。"""
        mock_retrieve.return_value = []
        resp = client.post("/api/ask", json={"question": "帮我把这段话翻译成英文"})
        assert resp.json()["source"] in ("kb", "guard")   # 走了检索路径
        assert mock_retrieve.called

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_sensitive_blocked(self, mock_retrieve, mock_log, client, general_on):
        mock_retrieve.side_effect = AssertionError("敏感拦截不应触发检索")
        resp = client.post("/api/ask", json={"question": "谁的工资比我高"})
        data = resp.json()
        assert data["answer"] == SENSITIVE_BLOCK_MESSAGE
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "BLOCKED"
        assert kw["risk_blocked"] is True
        assert kw["risk_level"] == "sensitive"
        assert kw["intent_type"] == "refuse_sensitive"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_realtime_blocked(self, mock_retrieve, mock_log, client, general_on):
        mock_retrieve.side_effect = AssertionError("实时信息不应触发检索")
        resp = client.post("/api/ask", json={"question": "今天天气怎么样"})
        assert resp.json()["answer"] == REALTIME_BLOCK_MESSAGE
        kw = mock_log.call_args.kwargs
        # SUCCESS + intent 标注：实时信息不是知识库缺口，不进 NO_RESULT/REFUSAL 口径
        assert kw["answer_status"] == "SUCCESS"
        assert kw["intent_type"] == "refuse_realtime"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_i1_company_translate_goes_kb(self, mock_retrieve, mock_log, client,
                                          general_on):
        """I1：涉公司的翻译请求必须走知识库路径（此处检索空 → 引导拒答而非通用翻译）。"""
        mock_retrieve.return_value = []
        resp = client.post("/api/ask", json={"question": "帮我把报销流程翻译成英文"},
                           headers=_auth())
        data = resp.json()
        assert data["no_result"] is True
        assert "知识贡献" in data["answer"]          # 引导式拒答（sim 分诊 → enterprise）
        assert "翻译" not in data["answer"][:12]     # 绝不是通用翻译回答
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "NO_RESULT"    # 缺口挖掘口径不漂移
        assert kw["intent_type"] == "refuse_uncovered_enterprise"


class TestFailureSequence:
    """知识库失败路径（检索空 / LLM 拒答）的统一失败序列。"""

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_empty_retrieval_guided_refusal(self, mock_retrieve, mock_log, client,
                                            general_on):
        mock_retrieve.return_value = []
        resp = client.post("/api/ask", json={"question": "不存在的内容XYZW"})
        data = resp.json()
        assert data["no_result"] is True
        assert "知识贡献" in data["answer"]
        assert data["source"] == "guard"
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "NO_RESULT"
        assert kw["intent_type"] == "refuse_uncovered_enterprise"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.generate_answer")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_refusal_replaced_with_guided(self, mock_retrieve, mock_gen, mock_log,
                                          client, general_on):
        """LLM 拒答（有弱候选）→ 替换为带相近标题的引导话术；状态维持 REFUSAL。"""
        mock_retrieve.return_value = [
            {"chunk_text": "..", "title": "出差报销制度", "doc_id": "D1", "score": 0.9}]
        mock_gen.return_value = {
            "answer": "抱歉，当前知识库中未找到相关信息",
            "sources": [], "model": "qwen-test", "usage": {},
        }
        resp = client.post("/api/ask", json={"question": "外派驻场怎么报销"})
        data = resp.json()
        assert data["no_result"] is True
        assert "出差报销制度" in data["answer"]      # 相近文档标题（手头 chunks，零成本）
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "REFUSAL"      # 口径不漂移
        assert kw["intent_type"] == "refuse_uncovered_enterprise"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.generate_answer")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_success_answer_untouched(self, mock_retrieve, mock_gen, mock_log,
                                      client, general_on):
        """正常回答（非拒答）不进失败序列：flag-on 对成功路径零影响。"""
        mock_retrieve.return_value = [
            {"chunk_text": "..", "title": "手册", "doc_id": "D1", "score": 0.9}]
        mock_gen.return_value = {
            "answer": "按手册第三章执行。", "sources": [], "model": "qwen-test", "usage": {},
        }
        resp = client.post("/api/ask", json={"question": "怎么执行"})
        data = resp.json()
        assert data["answer"] == "按手册第三章执行。"
        assert data["source"] == "kb"
        assert mock_log.call_args.kwargs["intent_type"] is None


class TestStreamChain:
    """SSE 链路：前置路由帧协议 + 空结果失败序列帧。"""

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_stream_smalltalk_frames(self, mock_retrieve, mock_log, client, general_on):
        mock_retrieve.side_effect = AssertionError("前置命中不应触发检索")
        resp = client.post("/api/ask/stream", json={"question": "你好"})
        body = resp.text
        assert "session" in body and "[DONE]" in body
        assert "富岭知识库助手" in body
        assert '"source": "smalltalk"' in body
        assert mock_log.call_args.kwargs["intent_type"] == "smalltalk"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_stream_empty_guided_frames(self, mock_retrieve, mock_log, client,
                                        general_on):
        mock_retrieve.return_value = []
        resp = client.post("/api/ask/stream", json={"question": "不存在的内容XYZW"})
        body = resp.text
        assert "知识贡献" in body
        assert '"no_result": true' in body
        assert mock_log.call_args.kwargs["answer_status"] == "NO_RESULT"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_stream_flag_off_unchanged(self, mock_retrieve, mock_log, client):
        mock_retrieve.return_value = []
        resp = client.post("/api/ask/stream", json={"question": "你好"})
        assert NO_RESULT_MESSAGE in resp.text
        assert mock_log.call_args.kwargs["intent_type"] is None


class TestExecuteGeneralQuota:
    """配额拒绝 → 友好话术（不抛 HTTP）。"""

    def test_quota_denial_maps_to_quota_message(self, monkeypatch):
        # 分支契约：api 按模块属性访问限流器（_rate_limiter.LIMITER）→ patch 打 rate_limiter 侧
        import opensearch_pipeline.api as API
        import opensearch_pipeline.rate_limiter as RL
        monkeypatch.setattr(
            RL.LIMITER, "admit_general",
            lambda actor, is_user: Denial(429, "x", 60, "general_quota"))
        ident = types.SimpleNamespace(user_id="U1", acl_groups=["it"], name="张")
        out = API._execute_general_llm("帮我翻译", history=None, tier="office",
                                       identity=ident)
        assert out["answer"] == GENERAL_QUOTA_MESSAGE
        assert out["intent_type"] == "refuse_quota"

    def test_off_denial_maps_to_tier_off_message(self, monkeypatch):
        import opensearch_pipeline.api as API
        import opensearch_pipeline.rate_limiter as RL
        monkeypatch.setattr(
            RL.LIMITER, "admit_general",
            lambda actor, is_user: Denial(403, "x", 0, "general_off"))
        ident = types.SimpleNamespace(user_id="U1", acl_groups=["it"], name="张")
        out = API._execute_general_llm("帮我翻译", history=None, tier="office",
                                       identity=ident)
        assert out["answer"] == GENERAL_TIER_OFF_MESSAGE


class TestRateLimiterGeneralQuota:
    """rate_limiter.admit_general 本体（照 thinking 配额先例）。"""

    def _limiter(self, monkeypatch, quota):
        from opensearch_pipeline.rate_limiter import ServingRateLimiter
        monkeypatch.setenv("RAG_RATE_LIMIT_ENABLE", "true")
        monkeypatch.setenv("RAG_GENERAL_DAILY_QUOTA", str(quota))
        lim = ServingRateLimiter()
        lim.reload_limits()
        return lim

    def test_quota_counts_and_denies(self, monkeypatch):
        lim = self._limiter(monkeypatch, 2)
        assert lim.admit_general("u:a", is_user=True) is None
        assert lim.admit_general("u:a", is_user=True) is None
        d = lim.admit_general("u:a", is_user=True)
        assert d is not None and d.reason == "general_quota"
        # 其他用户不受影响
        assert lim.admit_general("u:b", is_user=True) is None

    def test_anon_denied(self, monkeypatch):
        lim = self._limiter(monkeypatch, 5)
        d = lim.admit_general("ip:1.2.3.4", is_user=False)
        assert d is not None and d.reason == "general_anon"

    def test_zero_quota_means_off(self, monkeypatch):
        lim = self._limiter(monkeypatch, 0)
        d = lim.admit_general("u:a", is_user=True)
        assert d is not None and d.reason == "general_off"


class TestDingtalkAdapters:
    """钉钉侧薄适配层（不起 webhook，直接测函数）。"""

    def _cfg(self, monkeypatch, mode="office", guided=True):
        import opensearch_pipeline.dingtalk_bot as BOT
        from opensearch_pipeline.config import RAGConfig
        cfg = types.SimpleNamespace(rag=RAGConfig(
            general_ability_mode=mode, guided_refusal=guided))
        monkeypatch.setattr(BOT, "get_config", lambda: cfg)
        return BOT

    def test_group_chat_downgrades_office_to_smalltalk(self, monkeypatch):
        BOT = self._cfg(monkeypatch, mode="office")
        assert BOT._bot_general_flags("2")[0] == "smalltalk"
        assert BOT._bot_general_flags("1")[0] == "office"
        # 群聊里翻译请求回落知识库（pre_route 与降档双保险）
        import opensearch_pipeline.intent_router as IR
        monkeypatch.setattr(IR, "get_config", lambda: types.SimpleNamespace(
            rag=__import__("opensearch_pipeline.config", fromlist=["RAGConfig"])
            .RAGConfig(general_ability_mode="office")))
        assert BOT._bot_pre_route("帮我把这段话翻译成英文", sender_staff_id="S1",
                                  user_dept=None, conversation_type="2") is None

    def test_bot_failure_group_office_gets_tier_off(self, monkeypatch):
        """群聊失败路径分诊为 office 时：降档使其落引导话术而非通用回答。"""
        BOT = self._cfg(monkeypatch, mode="office", guided=True)
        monkeypatch.setattr(BOT, "triage_failed_query", lambda q, t=None: "office")
        out = BOT._bot_failure_outcome("帮我写首诗", [], sender_staff_id="S1",
                                       history=None, conversation_type="2")
        assert out is not None
        assert out["answer"] == GENERAL_TIER_OFF_MESSAGE

    def test_bot_failure_flags_off_returns_none(self, monkeypatch):
        BOT = self._cfg(monkeypatch, mode="off", guided=False)
        assert BOT._bot_failure_outcome("任意问题", [], sender_staff_id="S1",
                                        history=None, conversation_type="1") is None


class TestCalcQuotaExemption:
    """批次4（ultra general_answerer:203）：确定性计算器先于配额——calc 命中零 LLM 成本
    免配额（此前先 admit_general 再进 calc 分支：算术题白烧配额格，配额用尽后连
    「3+5」都吃配额拒答话术）。api 侧与 dingtalk_bot 侧同源修复。"""

    def test_calc_bypasses_quota_even_when_exhausted(self, monkeypatch):
        import opensearch_pipeline.api as API
        import opensearch_pipeline.rate_limiter as RL
        calls = []

        def _deny(actor, is_user):
            calls.append(actor)
            return Denial(429, "x", 60, "general_quota")

        monkeypatch.setattr(RL.LIMITER, "admit_general", _deny)
        ident = types.SimpleNamespace(user_id="U1", acl_groups=["it"], name="张")
        out = API._execute_general_llm("3+5", history=None, tier="office", identity=ident)
        assert out is not None and out["model"] == "calc"
        assert "8" in out["answer"]
        assert calls == [], "calc 命中免配额：admit_general 不得被调用"
        assert out["answer_status"] == "SUCCESS" and out["source"] == "general"

    def test_non_calc_still_goes_through_quota(self, monkeypatch):
        import opensearch_pipeline.api as API
        import opensearch_pipeline.rate_limiter as RL
        calls = []

        def _admit(actor, is_user):
            calls.append(actor)
            return Denial(429, "x", 60, "general_quota")

        monkeypatch.setattr(RL.LIMITER, "admit_general", _admit)
        ident = types.SimpleNamespace(user_id="U1", acl_groups=["it"], name="张")
        out = API._execute_general_llm("帮我翻译一句话", history=None, tier="office",
                                       identity=ident)
        assert calls, "非 calc 问题仍须过配额"
        assert out["intent_type"] == "refuse_quota"

    def test_bot_side_calc_bypasses_quota(self, monkeypatch):
        import opensearch_pipeline.dingtalk_bot as BOT

        def _deny(actor, is_user):
            raise AssertionError("calc 命中不得触达配额")

        monkeypatch.setattr(BOT, "LIMITER",
                            types.SimpleNamespace(admit_general=_deny))
        out = BOT._bot_execute_general_llm("12*3", history=None, tier="office",
                                           sender_staff_id="s1")
        assert out is not None and out["model"] == "calc"
        assert "36" in out["answer"]
