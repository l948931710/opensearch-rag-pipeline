"""RAG_STREAM_REASONING：thinking 模式下把 reasoning_content 作为 `reasoning` 帧下发（披露条用）。

加固点：仅「thinking 开 + flag 开」时下发；reasoning 在 chunk 之前；答案 chunk 不含 reasoning（不污染
非流式累积路径，那条只收 chunk）。flag 关 / thinking 关 → 与历史一致（丢弃 reasoning）。
"""
import json
from unittest.mock import MagicMock, patch

from opensearch_pipeline import llm_generator
from opensearch_pipeline.config import get_config


def _sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False)


# 模拟 DashScope 流：先两段 reasoning_content，再两段 content，最后 usage + [DONE]。
_LINES = [
    _sse({"choices": [{"delta": {"reasoning_content": "先想想，"}}]}),
    _sse({"choices": [{"delta": {"reasoning_content": "应查年假制度。"}}]}),
    _sse({"choices": [{"delta": {"content": "每年"}}]}),
    _sse({"choices": [{"delta": {"content": "5 天。"}}]}),
    _sse({"choices": [{"delta": {}}], "usage": {"total_tokens": 9}}),
    "data: [DONE]",
]


def _fake_post(*_a, **_k):
    m = MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.raise_for_status.return_value = None
    m.iter_lines.return_value = iter(_LINES)
    return m


_CHUNKS = [{"doc_id": "d1", "title": "年假制度", "section": "第3条",
            "chunk_text": "年假每年5天。", "text": "年假每年5天。", "score": 9.0,
            "permission_level": "public", "level": "high"}]


def _frames(thinking, flag):
    cfg = get_config()
    orig = cfg.rag.stream_reasoning
    cfg.rag.stream_reasoning = flag
    out = []
    try:
        with patch.object(llm_generator.requests, "post", _fake_post):
            for ev in llm_generator.generate_answer_stream("年假几天", _CHUNKS, thinking=thinking):
                s = ev.strip()
                if s.startswith("data: "):
                    p = s[6:].strip()
                    if p and p != "[DONE]":
                        out.append(json.loads(p))
    finally:
        cfg.rag.stream_reasoning = orig
    return out


def test_reasoning_emitted_when_thinking_and_flag_on():
    frames = _frames(thinking=True, flag=True)
    types = [f.get("type") for f in frames]
    assert "reasoning" in types
    rtext = "".join(f["content"] for f in frames if f.get("type") == "reasoning")
    assert "先想想" in rtext and "年假制度" in rtext
    # reasoning 必在首个 chunk 之前
    assert types.index("reasoning") < types.index("chunk")
    # 答案正文绝不含 reasoning（非流式累积只取 chunk → 不被污染）
    atext = "".join(f["content"] for f in frames if f.get("type") == "chunk")
    assert atext == "每年5 天。"


def test_reasoning_dropped_when_flag_off():
    frames = _frames(thinking=True, flag=False)
    assert "reasoning" not in [f.get("type") for f in frames]
    # 答案仍正常
    atext = "".join(f["content"] for f in frames if f.get("type") == "chunk")
    assert atext == "每年5 天。"


def test_reasoning_dropped_when_thinking_off():
    frames = _frames(thinking=False, flag=True)
    assert "reasoning" not in [f.get("type") for f in frames]
