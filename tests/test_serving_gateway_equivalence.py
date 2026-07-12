"""Tier B-流式等值门：RAG_SERVING_MODEL_GATEWAY on/off 两条路径，同输入下
（1）线上请求等值——url / JSON 请求体（dict 等值，键序无关）/ headers / timeout / stream；
（2）对外 SSE 帧序列逐字节等值——sources / reasoning / chunk / done / [DONE]。

这是 gateway 收敛的硬门：任何让两条路径线上行为分叉的改动都会在这里红。
usage 保真单测盯 done 帧携带线上原始 usage dict（含 total_tokens /
prompt_tokens_details.cached_tokens 等 Usage 类型化视图之外的字段）。
"""
import json
from unittest.mock import MagicMock, patch

from opensearch_pipeline import llm_generator
from opensearch_pipeline.agent_runtime import model_gateway
from opensearch_pipeline.config import get_config


def _sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False)


# 带 reasoning、多段 content、富 usage（含类型化视图之外字段）的脚本流
_RICH_USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
               "prompt_tokens_details": {"cached_tokens": 64}}
_LINES = [
    _sse({"choices": [{"delta": {"reasoning_content": "先查制度，"}}]}),
    _sse({"choices": [{"delta": {"reasoning_content": "再对条款。"}}]}),
    _sse({"choices": [{"delta": {"content": "每年"}}]}),
    _sse({"choices": [{"delta": {"content": "5 天。"}}]}),
    _sse({"choices": [{"delta": {}}], "usage": _RICH_USAGE}),
    "data: [DONE]",
]

_CHUNKS = [{"doc_id": "d1", "title": "年假制度", "section": "第3条",
            "chunk_text": "年假每年5天。", "text": "年假每年5天。", "score": 9.0,
            "permission_level": "public", "level": "high"}]


class _CapturePost:
    """捕获 _http_post 的完整调用参数并回放脚本响应（流式回 lines、非流式回 json_body）。"""

    def __init__(self, lines=None, json_body=None):
        self._lines = lines or []
        self._json_body = json_body
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        m = MagicMock()
        m.status_code = 200                       # gateway 传输层按 status_code 分类
        m.raise_for_status.return_value = None    # 裸路径用 raise_for_status
        m.__enter__.return_value = m              # 裸路径 with 语境
        m.__exit__.return_value = False
        m.iter_lines.return_value = iter(self._lines)
        m.json.return_value = self._json_body     # 非流式两路径都走 .json()
        m.close.return_value = None
        return m


def _run(flag, *, thinking=False, stream_reasoning=False):
    cfg = get_config()
    orig_flag = cfg.rag.serving_model_gateway
    orig_sr = cfg.rag.stream_reasoning
    cfg.rag.serving_model_gateway = flag
    cfg.rag.stream_reasoning = stream_reasoning
    cap = _CapturePost(_LINES)
    try:
        with patch.object(llm_generator, "_http_post", cap), \
             patch.object(model_gateway, "_http_post", cap):
            frames = list(llm_generator.generate_answer_stream(
                "年假几天", _CHUNKS, thinking=thinking))
    finally:
        cfg.rag.serving_model_gateway = orig_flag
        cfg.rag.stream_reasoning = orig_sr
    assert len(cap.calls) == 1, "两条路径都应恰好发一次 HTTP 调用"
    return cap.calls[0], frames


def test_wire_request_and_frames_equivalent():
    off_call, off_frames = _run(False)
    on_call, on_frames = _run(True)
    assert on_call["url"] == off_call["url"]
    assert on_call["json"] == off_call["json"], "线上 JSON 请求体必须等值（键序无关）"
    assert on_call["headers"] == off_call["headers"]
    assert on_call["timeout"] == off_call["timeout"]
    assert off_call["stream"] is True and on_call["stream"] is True
    assert on_frames == off_frames, "对外 SSE 帧序列必须逐字节等值"


def test_reasoning_frame_parity_when_thinking_on():
    off_call, off_frames = _run(False, thinking=True, stream_reasoning=True)
    on_call, on_frames = _run(True, thinking=True, stream_reasoning=True)
    assert on_call["json"] == off_call["json"]
    assert on_call["json"]["enable_thinking"] is True
    assert on_frames == off_frames
    types = [json.loads(f[6:].strip()).get("type") for f in on_frames
             if f.startswith("data: ") and f[6:].strip() != "[DONE]"]
    assert "reasoning" in types
    assert types.index("reasoning") < types.index("chunk")


def test_reasoning_dropped_without_flag_parity():
    # thinking 开但 RAG_STREAM_REASONING 关：两路径都只发 chunk（reasoning 丢弃）
    _, off_frames = _run(False, thinking=True, stream_reasoning=False)
    _, on_frames = _run(True, thinking=True, stream_reasoning=False)
    assert on_frames == off_frames
    assert not any('"type": "reasoning"' in f for f in on_frames)


def test_done_frame_usage_raw_fidelity():
    _, frames = _run(True)
    done = next(json.loads(f[6:].strip()) for f in frames
                if f.startswith("data: ") and '"type": "done"' in f)
    assert done["usage"] == _RICH_USAGE, "done 帧必须携带线上原始 usage（含明细字段）"


# ── 非流式 generate_answer（Tier B 后半）────────────────────────────
_NONSTREAM_BODY = {
    "choices": [{"message": {"content": "根据[文档1]，年假每年5 天。"},
                 "finish_reason": "stop"}],
    "usage": _RICH_USAGE,
    "model": "qwen3.7-plus",
}


def _run_nonstream(flag, *, thinking=None, body=_NONSTREAM_BODY):
    cfg = get_config()
    orig_flag = cfg.rag.serving_model_gateway
    cfg.rag.serving_model_gateway = flag
    cap = _CapturePost(json_body=body)
    try:
        with patch.object(llm_generator, "_http_post", cap), \
             patch.object(model_gateway, "_http_post", cap):
            result = llm_generator.generate_answer("年假几天", _CHUNKS, thinking=thinking)
    finally:
        cfg.rag.serving_model_gateway = orig_flag
    assert len(cap.calls) == 1
    return cap.calls[0], result


def test_nonstream_wire_and_result_equivalence():
    off_call, off_res = _run_nonstream(False)
    on_call, on_res = _run_nonstream(True)
    assert on_call["url"] == off_call["url"]
    assert on_call["json"] == off_call["json"], "非流式线上请求体必须等值（含 stream:False 键）"
    assert on_call["json"]["stream"] is False
    assert on_call["headers"] == off_call["headers"]
    assert on_call["timeout"] == off_call["timeout"]
    assert "stream" not in on_call and "stream" not in off_call   # kwarg 层面都非流式
    assert on_res == off_res, "返回 dict（answer/sources/model/usage/gen_meta）必须全等"
    assert on_res["usage"] == _RICH_USAGE, "usage 必须是线上原始 dict（含明细字段）"
    assert "[文档1]" not in on_res["answer"], "两路径同过 strip_doc_citations"


def test_nonstream_thinking_override_parity():
    off_call, _ = _run_nonstream(False, thinking=True)
    on_call, _ = _run_nonstream(True, thinking=True)
    assert on_call["json"] == off_call["json"]
    assert on_call["json"]["enable_thinking"] is True


def test_nonstream_no_choices_fail_loud_both_paths():
    """裸路径对「200 但无 choices」KeyError fail-loud；gateway 分支须保持失败模式
    （空答案绝不静默落台账）。"""
    import pytest

    cfg = get_config()
    orig = cfg.rag.serving_model_gateway
    for flag in (False, True):
        cfg.rag.serving_model_gateway = flag
        cap = _CapturePost(json_body={"choices": []})
        try:
            with patch.object(llm_generator, "_http_post", cap), \
                 patch.object(model_gateway, "_http_post", cap), \
                 pytest.raises(Exception):
                llm_generator.generate_answer("年假几天", _CHUNKS)
        finally:
            cfg.rag.serving_model_gateway = orig
