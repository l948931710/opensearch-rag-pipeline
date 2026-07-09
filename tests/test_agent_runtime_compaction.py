# -*- coding: utf-8 -*-
"""
test_agent_runtime_compaction.py — WS2-3 rolling summary 真 summarizer

summarize() 调廉价模型出摘要（stub gateway）/ 空溢出不调 / fail-open / 合并已有摘要；
install 装进 session_store；端到端：装 compaction + 开 flag → 溢出轮被真 summarizer 压成
反注入分隔符包裹的摘要。
"""
from opensearch_pipeline import session_store as ss
from opensearch_pipeline.agent_runtime import compaction


class _StubGateway:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def complete(self, ctx, category, req):
        from opensearch_pipeline.agent_runtime.events import Usage
        from opensearch_pipeline.agent_runtime.model_gateway import ChatResponse
        self.calls.append((category, req))
        return ChatResponse(text=self._text, tool_calls=[], usage=Usage(), model="stub")


def test_summarize_calls_cheap_model_and_returns_text():
    gw = _StubGateway("摘要：用户问了 A 和 B")
    out = compaction.summarize([{"role": "user", "content": "A"},
                                {"role": "assistant", "content": "a"}], "", gateway=gw)
    assert out == "摘要：用户问了 A 和 B"
    assert gw.calls and gw.calls[0][0] == "light"           # 用最廉价的 light 档（不思考）


def test_summarize_empty_overflow_skips_model():
    gw = _StubGateway("不该被调")
    assert compaction.summarize([], "旧摘要", gateway=gw) == "旧摘要"
    assert not gw.calls


def test_summarize_fail_open_on_model_error():
    class _BoomGW:
        def complete(self, *a, **k):
            raise RuntimeError("model down")
    out = compaction.summarize([{"role": "user", "content": "A"}], "旧摘要", gateway=_BoomGW())
    assert out == "旧摘要"                                    # fail-open：保留原摘要


def test_summarize_merges_prev_summary_into_prompt():
    gw = _StubGateway("merged")
    compaction.summarize([{"role": "user", "content": "新问题"}], "已有摘要X", gateway=gw)
    user_msg = gw.calls[0][1].messages[-1]["content"]
    assert "已有摘要X" in user_msg and "新问题" in user_msg    # 合并已有摘要 + 新增


def test_install_and_uninstall_toggle_session_store_summarizer():
    try:
        compaction.install(gateway=_StubGateway("s"))
        assert ss._summarizer is not None
    finally:
        compaction.uninstall()
        assert ss._summarizer is None


def test_end_to_end_real_summarizer_framed(monkeypatch):
    """装 compaction + 开 flag → 溢出轮被真 summarizer 压缩、读取时反注入分隔符包裹。"""
    monkeypatch.setattr(ss, "MAX_HISTORY_TURNS", 2)          # cap 2 轮
    monkeypatch.setenv("RAG_SESSION_ROLLING_SUMMARY", "true")
    store = ss.MemorySessionStore()
    try:
        compaction.install(gateway=_StubGateway("压缩后的历史摘要"))
        store.get_or_create("t", owner="u")
        for i in range(1, 4):                                # 3 轮 → 溢出轮 1
            store.append("t", f"Q{i}", f"A{i}", owner="u")
        hist = store.get_history("t", owner="u")
        assert hist[0]["role"] == "system"
        assert hist[0]["content"].startswith(ss._SUMMARY_PREFIX)         # 反注入前缀
        assert "压缩后的历史摘要" in hist[0]["content"]                   # 真 summarizer 产出
        assert ss._SUMMARY_SUFFIX.strip() in hist[0]["content"]          # 反注入结束标
    finally:
        compaction.uninstall()
