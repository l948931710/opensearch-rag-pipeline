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


# ── /api/ask/stream 透传护栏：reasoning 帧只发给【显式请求 thinking】的调用方 ──
from unittest.mock import patch as _patch  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _fake_stream_with_reasoning(*_a, **_k):
    yield 'data: {"type":"sources","sources":[]}\n\n'
    yield 'data: {"type":"reasoning","content":"先想想，"}\n\n'
    yield 'data: {"type":"reasoning","content":"查制度。"}\n\n'
    yield 'data: {"type":"chunk","content":"每年 5 天。"}\n\n'
    yield 'data: {"type":"done","model":"q","usage":{}}\n\n'
    yield "data: [DONE]\n\n"


def _stream_types(thinking):
    from opensearch_pipeline.api import app
    client = TestClient(app)
    with _patch("opensearch_pipeline.api.generate_answer_stream", side_effect=_fake_stream_with_reasoning), \
         _patch("opensearch_pipeline.api.retrieve_and_enrich", return_value=[{"doc_id": "D1", "title": "制度", "chunk_text": "x", "score": 9.0}]), \
         _patch("opensearch_pipeline.api.build_content_blocks", return_value=[]), \
         _patch("opensearch_pipeline.api.log_qa_session"), \
         _patch("opensearch_pipeline.api._append_to_history"):
        body = {"question": "年假几天"}
        if thinking:
            body["thinking"] = True
        resp = client.post("/api/ask/stream", json=body)
        assert resp.status_code == 200
        types = []
        for ln in resp.text.splitlines():
            ln = ln.strip()
            if ln.startswith("data: ") and ln[6:] != "[DONE]":
                try:
                    types.append(json.loads(ln[6:]).get("type"))
                except json.JSONDecodeError:
                    pass
        return types


def test_endpoint_passes_reasoning_only_when_thinking_requested():
    # thinking=true → reasoning 帧透传给调用方；答案仍在
    t = _stream_types(thinking=True)
    assert "reasoning" in t and "chunk" in t
    # 未请求 thinking → reasoning 帧被护栏丢弃（杜绝把 CoT 广播给未 opt-in 的 SSE 客户端），答案不受影响
    n = _stream_types(thinking=False)
    assert "reasoning" not in n and "chunk" in n
