# -*- coding: utf-8 -*-
"""
test_pure_text_switch.py — 纯文本生成开关（RAG_PURE_TEXT / pure_text）单元测试。

覆盖：
  1. system prompt 拆分正确且默认（图文）模式逐字节零变更（no-regression）。
  2. _format_context 在 pure_text=True 时去掉所有 <<IMG:N>> 标记，但保留
     [📷 图片] 标签与 visual_summary 文本（图片语义不丢失）。
  3. generate_answer 的 pure_text 解析：显式参数优先，None 时取 config.rag.pure_text。
"""
import types

import pytest

import opensearch_pipeline.llm_generator as G
from opensearch_pipeline.config import LLMConfig, RAGConfig


# ──────────────────────────────────────────────────────────────
# 1. system prompt 拆分 / no-regression
# ──────────────────────────────────────────────────────────────

def test_default_prompt_is_base_plus_image_rule():
    # 默认 prompt 必须是 base + 图片规则 的拼接（保证图文模式零变更）
    assert G.DEFAULT_SYSTEM_PROMPT == G._SYSTEM_PROMPT_BASE + G._IMG_INTERLEAVE_RULE


def test_text_only_prompt_drops_image_rule():
    # 纯文本 prompt = base（不含规则 9 / <<IMG>>）
    assert G.TEXT_ONLY_SYSTEM_PROMPT == G._SYSTEM_PROMPT_BASE
    assert G.DEFAULT_SYSTEM_PROMPT.startswith(G.TEXT_ONLY_SYSTEM_PROMPT)
    # 图文穿插规则（规则 10）只出现在默认 prompt
    assert "<<IMG:N>>" in G.DEFAULT_SYSTEM_PROMPT
    assert "<<IMG" not in G.TEXT_ONLY_SYSTEM_PROMPT
    assert "10. 如果参考文档中包含图片" in G.DEFAULT_SYSTEM_PROMPT
    assert "10. 如果参考文档中包含图片" not in G.TEXT_ONLY_SYSTEM_PROMPT
    # 基础规则在两者中都保留（关键的"不编造 / 无结果时告知 / 不列来源清单 / 数字须出自原文"规则不能丢）
    for shared in ("只基于提供的参考文档内容回答", "抱歉，当前知识库中未找到相关信息",
                   "不要在回答正文或末尾列出参考来源", "必须严格来自参考文档原文"):
        assert shared in G.DEFAULT_SYSTEM_PROMPT
        assert shared in G.TEXT_ONLY_SYSTEM_PROMPT


# ──────────────────────────────────────────────────────────────
# 2. _format_context 行为
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def fake_cfg(monkeypatch):
    """让 _format_context / generate_answer 用一个可控 config（不依赖 env）。"""
    cfg = types.SimpleNamespace(
        # 用真实 RAGConfig 默认值构造 rag mock：自动带上 _format_context 读取的所有
        # 字段（score_threshold_*, rerank_score_threshold_*, pure_text 等），
        # 避免生产端新增配置字段后 mock 漂移再次 AttributeError。
        rag=RAGConfig(pure_text=False),
        # 同理用真实 LLMConfig 默认值构造 llm mock（自动带上 enable_thinking 等），
        # 仅覆盖测试关心的 endpoint/model。
        llm=LLMConfig(
            api_key="sk-test",
            api_base_url="https://example.invalid/v1",
            model="qwen-test",
        ),
    )
    monkeypatch.setattr(G, "get_config", lambda: cfg)
    return cfg


def _img_chunk():
    return {"chunk_type": "image", "title": "电子天平", "section_title": "",
            "chunk_text": "天平水平调节", "score": 9.0,
            "source_image": "raw/x/平衡.png", "visual_summary": "水平气泡居中示意图"}


def _step_chunk():
    return {"chunk_type": "step_card", "title": "天平SOP", "section_title": "调水平",
            "chunk_text": "调节地脚螺丝使气泡居中", "score": 7.0, "step_no": 2,
            "image_refs": [{"oss_key": "raw/x/step2.png", "ocr_text": "气泡"}]}


def _text_img_chunk():
    return {"chunk_type": "text_chunk", "title": "检验SOP", "section_title": "外观",
            "chunk_text": "检查印刷缺陷", "score": 6.0,
            "image_refs": [{"oss_key": "raw/x/defect.png", "visual_summary": "缺陷样例"}]}


def test_format_context_multimodal_injects_markers(fake_cfg):
    ctx = G._format_context([_img_chunk(), _step_chunk(), _text_img_chunk()], pure_text=False)
    # 三类带图 chunk 都注入了 <<IMG:N>> 标记
    assert "<<IMG:1>>" in ctx
    assert "<<IMG:2>>" in ctx
    assert "<<IMG:3>>" in ctx
    assert "[📷 图片]" in ctx
    assert "图片内容：水平气泡居中示意图" in ctx


def test_format_context_pure_text_strips_all_markers(fake_cfg):
    ctx = G._format_context([_img_chunk(), _step_chunk(), _text_img_chunk()], pure_text=True)
    # 纯文本模式：零 <<IMG>> 标记
    assert "<<IMG" not in ctx
    assert "IMG:" not in ctx
    # 但图片语义信息仍保留（标签 + visual_summary 文本）
    assert "[📷 图片]" in ctx
    assert "图片内容：水平气泡居中示意图" in ctx
    # 文本内容本身不变
    assert "调节地脚螺丝使气泡居中" in ctx
    assert "检查印刷缺陷" in ctx


def test_format_context_default_param_is_multimodal(fake_cfg):
    # 不传 pure_text → 默认 False → 与历史行为一致（注入标记）
    ctx = G._format_context([_img_chunk()])
    assert "<<IMG:1>>" in ctx


# ──────────────────────────────────────────────────────────────
# 3. generate_answer 的 pure_text 解析（不触网）
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def capture_payload(monkeypatch):
    """拦截 requests.post，捕获发送给 LLM 的 messages，不真正发请求。"""
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "测试回答"}}], "usage": {}}

    def _fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(G.requests, "post", _fake_post)
    return captured


def _system_of(payload):
    return next(m["content"] for m in payload["messages"] if m["role"] == "system")


def test_generate_answer_explicit_pure_text(fake_cfg, capture_payload):
    G.generate_answer("天平怎么调水平", [_img_chunk()], pure_text=True)
    sys_msg = _system_of(capture_payload["payload"])
    user_msg = next(m["content"] for m in capture_payload["payload"]["messages"] if m["role"] == "user")
    assert sys_msg == G.TEXT_ONLY_SYSTEM_PROMPT
    assert "<<IMG" not in sys_msg
    assert "<<IMG" not in user_msg  # context 也无标记


def test_generate_answer_explicit_multimodal(fake_cfg, capture_payload):
    G.generate_answer("天平怎么调水平", [_img_chunk()], pure_text=False)
    sys_msg = _system_of(capture_payload["payload"])
    assert sys_msg == G.DEFAULT_SYSTEM_PROMPT
    assert "<<IMG:N>>" in sys_msg


def test_generate_answer_none_follows_config_flag(fake_cfg, capture_payload):
    # pure_text=None 时跟随全局 config.rag.pure_text
    fake_cfg.rag.pure_text = True
    G.generate_answer("天平怎么调水平", [_img_chunk()], pure_text=None)
    assert _system_of(capture_payload["payload"]) == G.TEXT_ONLY_SYSTEM_PROMPT

    fake_cfg.rag.pure_text = False
    G.generate_answer("天平怎么调水平", [_img_chunk()], pure_text=None)
    assert _system_of(capture_payload["payload"]) == G.DEFAULT_SYSTEM_PROMPT


def test_generate_answer_explicit_system_prompt_wins(fake_cfg, capture_payload):
    # 调用方显式传 system_prompt 时优先（pure_text 不覆盖它）
    G.generate_answer("q", [_img_chunk()], system_prompt="自定义", pure_text=True)
    assert _system_of(capture_payload["payload"]) == "自定义"
