# -*- coding: utf-8 -*-
"""test_answer_flow.py — answer_flow 纯函数簿记模块的单元测试。"""

import inspect

from opensearch_pipeline.answer_flow import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    NO_RESULT_MESSAGE,
    build_qa_log_kwargs,
    is_refusal_answer,
    should_append_history,
    top_score_of,
)


class TestHelpers:
    def test_top_score_of(self):
        assert top_score_of(None) is None
        assert top_score_of([]) is None
        assert top_score_of([{"score": 3.0}, {"score": 9.5}, {}]) == 9.5

    def test_should_append_history_truth_table(self):
        assert should_append_history("答案", "SUCCESS") is True
        assert should_append_history("答案", "LLM_ERROR") is False   # 出错的部分回答不入史
        assert should_append_history("", "SUCCESS") is False
        assert should_append_history(None, "SUCCESS") is False
        assert should_append_history(None, "NO_RESULT") is False

    def test_constants(self):
        assert "未找到" in NO_RESULT_MESSAGE
        assert DEFAULT_MAX_TOKENS == 2048
        assert DEFAULT_TEMPERATURE == 0.1


class TestIsRefusalAnswer:
    """no_result 契约的拒答判定：两种「未找到」形态（检索空 / LLM 带弱来源拒答）都要命中。"""

    def test_canonical_and_llm_phrases(self):
        assert is_refusal_answer(NO_RESULT_MESSAGE) is True            # 检索空的标准文案
        assert is_refusal_answer("抱歉，当前知识库中未找到相关信息") is True   # 规则 2 指示的 LLM 拒答
        assert is_refusal_answer("知识库中没有该流程的相关文档。") is True

    def test_anchored_long_refusal_still_counts(self):
        long_tail = "不过您可以尝试联系行政部获取纸质版制度文件，或在钉钉工作台提交工单咨询。" * 3
        assert is_refusal_answer("抱歉，当前知识库中未找到相关信息。" + long_tail) is True

    def test_normal_answer_with_midtext_mention_not_refusal(self):
        ans = ("U8 登录步骤如下：第一步打开客户端，第二步选择账套并输入工号密码，"
               "第三步点击登录。若系统提示未找到相关信息或账号异常，请联系信息部（分机 8021）。"
               "另外请注意月末结账期间系统将暂停登录维护，结账完成后方可恢复使用，"
               "请合理安排单据录入时间，避免影响当月成本核算与出入库对账。")
        assert len(ans) > 110
        assert is_refusal_answer(ans) is False      # 拒答句式出现在 30 字之后且全文长 → 正常回答

    def test_empty_and_none(self):
        assert is_refusal_answer(None) is False
        assert is_refusal_answer("") is False
        assert is_refusal_answer("   ") is False


class TestBuildQaLogKwargs:
    def test_full_key_set_always_emitted(self):
        """无论传多少参数，输出键集恒定 —— 调用方再也不可能"漏一个字段"。"""
        kw = build_qa_log_kwargs(session_id="s", message_id="m", question="q")
        assert set(kw) == {
            "session_id", "message_id", "user_id", "user_name", "user_dept",
            "query_text", "answer_text", "retrieved_docs", "cited_docs",
            "latency_ms", "retrieval_latency_ms", "llm_latency_ms",
            "answer_status", "model_name", "error_message",
            "opensearch_hit_count", "top_score", "conversation_type",
            "content_blocks_json",
        }

    def test_keys_match_log_qa_session_signature(self):
        """签名守卫：组装的每个键都必须是 log_qa_session 的合法参数（防未来漂移）。"""
        from opensearch_pipeline.qa_logger import log_qa_session

        params = set(inspect.signature(log_qa_session).parameters)
        kw = build_qa_log_kwargs(session_id="s", message_id="m", question="q")
        unknown = set(kw) - params
        assert not unknown, f"build_qa_log_kwargs 含 log_qa_session 不认识的键: {unknown}"

    def test_chunks_none_vs_empty_vs_populated(self):
        # None：检索未完成
        kw = build_qa_log_kwargs(session_id="s", message_id="m", question="q", chunks=None)
        assert kw["opensearch_hit_count"] is None
        assert kw["retrieved_docs"] is None
        assert kw["top_score"] is None
        # []：NO_RESULT
        kw = build_qa_log_kwargs(session_id="s", message_id="m", question="q", chunks=[])
        assert kw["opensearch_hit_count"] == 0
        assert kw["retrieved_docs"] is None
        assert kw["top_score"] is None
        # 有结果
        chunks = [{"score": 8.5}, {"score": 3.0}]
        kw = build_qa_log_kwargs(session_id="s", message_id="m", question="q", chunks=chunks)
        assert kw["opensearch_hit_count"] == 2
        assert kw["retrieved_docs"] == chunks
        assert kw["top_score"] == 8.5

    def test_empty_string_coercions(self):
        kw = build_qa_log_kwargs(session_id="s", message_id="m", question="q",
                                 answer_text="", content_blocks_json="")
        assert kw["answer_text"] is None
        assert kw["content_blocks_json"] is None

    def test_passthrough_fields(self):
        kw = build_qa_log_kwargs(
            session_id="s", message_id="m", question="问",
            user_id="u1", user_name="张三", user_dept="生产中心",
            answer_text="答", cited_docs=[{"title": "T"}],
            latency_ms=100, retrieval_latency_ms=20, llm_latency_ms=80,
            answer_status="LLM_ERROR", model_name="qwen-test",
            error_message="[trace=x] boom", conversation_type="2",
            content_blocks_json='[{"type":"image"}]',
        )
        assert kw["query_text"] == "问"
        assert kw["user_dept"] == "生产中心"
        assert kw["cited_docs"] == [{"title": "T"}]
        assert kw["answer_status"] == "LLM_ERROR"
        assert kw["error_message"] == "[trace=x] boom"
        assert kw["conversation_type"] == "2"
        assert kw["content_blocks_json"] == '[{"type":"image"}]'
