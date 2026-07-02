# -*- coding: utf-8 -*-
"""会话历史 <<IMG:N>> 标记清洗（#F-mm5, RAG_HISTORY_STRIP_IMG_MARKERS 默认 OFF）。

根因：入史只过 strip_doc_citations，<<IMG:N>> 原样入史并回放 → follow-up 轮 LLM
模仿历史标记，N 按【当前轮】image_map 解析，界内即穿透 referenced-only 全部防线
附上无关噪声图（2026-06-10 已知症状）。三道处理：
  (1) 写侧：四个入史调用点统一过 answer_flow.history_answer_text（只包裹实参，
      result["answer"] 保持原文供 blocks 构建；qa_session_log 不受影响）；
  (2) 回放侧兜底（执法主体）：_build_messages 对 assistant 轮再洗一遍——覆盖存量
      已污染历史与客户端显式传入的 req.history；
  (3) prompt 规则 11 反模仿指令（条件拼装，OFF 时 prompt 逐字节不变）。
"""

import types

from opensearch_pipeline.config import RAGConfig
import opensearch_pipeline.llm_generator as G
from opensearch_pipeline.answer_flow import history_answer_text
from opensearch_pipeline.llm_generator import (
    DEFAULT_SYSTEM_PROMPT,
    TEXT_ONLY_SYSTEM_PROMPT,
    _IMG_HISTORY_ANTIMIMIC_RULE,
    _build_messages,
)

ANSWER = "第一步如图 <<IMG:1>> 所示，然后 <IMG:2> 收尾。"


def _set_flag(monkeypatch, on: bool):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config().rag, "history_strip_img_markers", on)


def _fake_gen_cfg(monkeypatch, on: bool):
    cfg = types.SimpleNamespace(rag=RAGConfig(history_strip_img_markers=on))
    monkeypatch.setattr(G, "get_config", lambda: cfg)


# ═══════════════ 写侧：history_answer_text ═══════════════

def test_write_side_off_is_passthrough(monkeypatch):
    _set_flag(monkeypatch, False)
    assert history_answer_text(ANSWER) == ANSWER


def test_write_side_on_strips_markers(monkeypatch):
    _set_flag(monkeypatch, True)
    out = history_answer_text(ANSWER)
    assert "IMG" not in out and "第一步如图" in out and "收尾" in out


def test_write_side_empty_passthrough(monkeypatch):
    _set_flag(monkeypatch, True)
    assert history_answer_text("") == ""


# ═══════════════ 回放侧：_build_messages ═══════════════

_HISTORY = [
    {"role": "user", "content": "销售出库单怎么开？含 <<IMG:9>> 的用户输入不动"},
    {"role": "assistant", "content": "第一步 <<IMG:3>> 第二步 <<IMG:4>>"},
]


def test_replay_off_history_untouched(monkeypatch):
    _fake_gen_cfg(monkeypatch, False)
    msgs = _build_messages("下一步呢？", "ctx", history=list(_HISTORY))
    assert msgs[2]["content"] == "第一步 <<IMG:3>> 第二步 <<IMG:4>>"
    assert msgs[0]["content"] == DEFAULT_SYSTEM_PROMPT          # 无规则 11，逐字节不变
    assert _IMG_HISTORY_ANTIMIMIC_RULE not in msgs[0]["content"]


def test_replay_on_strips_assistant_turns_only(monkeypatch):
    _fake_gen_cfg(monkeypatch, True)
    msgs = _build_messages("下一步呢？", "ctx", history=list(_HISTORY))
    assert "IMG" not in msgs[2]["content"]                       # assistant 轮已洗
    assert "<<IMG:9>>" in msgs[1]["content"]                    # user 轮不动
    # 原始 history 列表不被原地修改（防调用方共享列表被污染）
    assert "<<IMG:3>>" in _HISTORY[1]["content"]


def test_replay_on_appends_antimimic_rule(monkeypatch):
    _fake_gen_cfg(monkeypatch, True)
    msgs = _build_messages("下一步呢？", "ctx", history=list(_HISTORY))
    assert msgs[0]["content"].endswith(_IMG_HISTORY_ANTIMIMIC_RULE)
    assert "11." in _IMG_HISTORY_ANTIMIMIC_RULE


def test_replay_on_no_rule_for_text_only_prompt(monkeypatch):
    """纯文本 prompt 没有规则 10（图文规则），不追加规则 11。"""
    _fake_gen_cfg(monkeypatch, True)
    msgs = _build_messages("q", "ctx", history=list(_HISTORY),
                           system_prompt=TEXT_ONLY_SYSTEM_PROMPT)
    assert _IMG_HISTORY_ANTIMIMIC_RULE not in msgs[0]["content"]
    assert "IMG" not in msgs[2]["content"]                      # 历史照样洗


def test_replay_on_no_history_prompt_unchanged(monkeypatch):
    """无历史时（首轮）不追加规则 11 也不做任何处理。"""
    _fake_gen_cfg(monkeypatch, True)
    msgs = _build_messages("q", "ctx", history=None)
    assert msgs[0]["content"] == DEFAULT_SYSTEM_PROMPT


def test_module_prompt_constants_unpolluted():
    """规则 11 绝不进模块常量（flag OFF 的 A/B 对照必须干净）。"""
    assert _IMG_HISTORY_ANTIMIMIC_RULE not in DEFAULT_SYSTEM_PROMPT
    assert _IMG_HISTORY_ANTIMIMIC_RULE not in TEXT_ONLY_SYSTEM_PROMPT
