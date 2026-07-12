# -*- coding: utf-8 -*-
"""stress_harness 冒烟单测（PR-safe：只测 mock 后端与 SSE 解析器，不起被测服务/不碰 DB）。"""
import json

import pytest

requests = pytest.importorskip("requests")

from stress_harness.mockend import MockBackend  # noqa: E402
from stress_harness.sse import parse_sse_text  # noqa: E402


@pytest.fixture()
def mock_backend():
    b = MockBackend().start()
    yield b
    b.stop()


def _chat(b: MockBackend, body: dict):
    return requests.post(f"http://127.0.0.1:{b.port}/compatible-mode/v1/chat/completions",
                         json=body, timeout=10)


def test_mock_llm_tool_call_then_final(mock_backend):
    """有 tools 且未过 tool 轮 → tool_call；带 tool 结果的第二轮 → 终稿文本。"""
    mock_backend.cfg.llm_latency_s = 0.0
    r1 = _chat(mock_backend, {"model": "qwen3.7-plus", "stream": False,
                              "tools": [{"type": "function"}],
                              "messages": [{"role": "user", "content": "问题"}]})
    msg1 = r1.json()["choices"][0]["message"]
    assert msg1["tool_calls"][0]["function"]["name"] == "knowledge_search"
    args = json.loads(msg1["tool_calls"][0]["function"]["arguments"])
    assert args["query"]

    r2 = _chat(mock_backend, {"model": "qwen3.7-plus", "stream": False,
                              "tools": [{"type": "function"}],
                              "messages": [{"role": "user", "content": "问题"},
                                           {"role": "assistant", "tool_calls": []},
                                           {"role": "tool", "content": "检索结果"}]})
    msg2 = r2.json()["choices"][0]["message"]
    assert msg2["content"]
    assert not msg2.get("tool_calls")
    assert mock_backend.stats()["llm"] == 2


def test_mock_llm_stream_frames_and_usage(mock_backend):
    """流式：tool_calls 分片 + usage 终帧 + [DONE]（DashScope include_usage 语义）。"""
    mock_backend.cfg.llm_latency_s = 0.0
    r = _chat(mock_backend, {"model": "qwen3.7-plus", "stream": True,
                             "tools": [{"type": "function"}],
                             "messages": [{"role": "user", "content": "问题"}]})
    assert "[DONE]" in r.text
    # charset 必带：requests iter_lines(decode_unicode=True) 对无 charset 的 text/*
    # 按 latin-1 解码，中文 query 会乱码（投机检索 query 匹配永 miss 的教训回归）
    assert "charset" in r.headers.get("Content-Type", "").lower()
    frames = parse_sse_text(r.text)
    frag = "".join(
        tc["function"].get("arguments", "")
        for f in frames for c in f.get("choices", [])
        for tc in (c.get("delta") or {}).get("tool_calls", []))
    assert json.loads(frag)["query"]     # 分片可重组为合法 JSON
    assert any(f.get("usage") for f in frames)   # usage 终帧


def test_mock_llm_fault_window_and_empty(mock_backend):
    mock_backend.cfg.llm_latency_s = 0.0
    mock_backend.fail_window(429, 5.0)
    r = _chat(mock_backend, {"model": "qwen3.7-plus", "stream": False,
                             "messages": [{"role": "user", "content": "q"}]})
    assert r.status_code == 429
    mock_backend.clear_faults()
    mock_backend.cfg.empty_completion = True
    r2 = _chat(mock_backend, {"model": "qwen3.7-plus", "stream": False,
                              "messages": [{"role": "user", "content": "q"}]})
    msg = r2.json()["choices"][0]["message"]
    assert msg["content"] == "" and not msg.get("tool_calls")


def test_mock_embedding_and_search(mock_backend):
    r = requests.post(
        f"http://127.0.0.1:{mock_backend.port}/api/v1/services/embeddings/"
        "text-embedding/text-embedding",
        json={"model": "text-embedding-v4", "input": {"texts": ["a", "b"]},
              "parameters": {"dimension": 1024}}, timeout=10)
    embs = r.json()["output"]["embeddings"]
    assert len(embs) == 2 and len(embs[0]["embedding"]) == 1024
    r2 = requests.post(
        f"http://127.0.0.1:{mock_backend.port}/fuling_knowledge_v1/_search",
        json={"size": 5, "query": {}}, timeout=10)
    hits = r2.json()["hits"]["hits"]
    assert hits and hits[0]["_source"]["permission_level"] == "public"
    assert mock_backend.stats() == {"llm": 0, "emb": 1, "search": 1, "webhook": 0}


def test_mock_webhook_capture(mock_backend):
    r = requests.post(f"http://127.0.0.1:{mock_backend.port}/robot/send?access_token=x",
                      json={"msgtype": "markdown"}, timeout=10)
    assert r.json()["errcode"] == 0
    assert mock_backend.stats()["webhook"] == 1


def test_sse_parse_ignores_done_and_garbage():
    text = ("data: {\"type\": \"session\", \"run_id\": \"r1\"}\n\n"
            "data: 不是JSON\n\n"
            "data: {\"type\": \"done\", \"usage\": {}}\n\n"
            "data: [DONE]\n\n")
    frames = parse_sse_text(text)
    assert [f["type"] for f in frames] == ["session", "done"]
