# -*- coding: utf-8 -*-
"""VLM funnel doc_title 死参修复（#F-mm7, RAG_VLM_DOC_CONTEXT 默认 OFF）。

死参根因：extraction task 构造从不设 doc_title 键 → funnel prompt 的【文档标题】
上下文分支从未生效。修复 = _funnel_doc_title 在 flag ON 时用 filename 词干兜底。
开启前置（放行走 eval）：换 RAG_VLM_CACHE_VERSION + L4-ingestion 全格式闸
（xlsx ≥0.8917 / docx ≥0.95 硬线）。
"""

from opensearch_pipeline.extraction.unified_extractor import _funnel_doc_title


def test_off_by_default_stays_empty(monkeypatch):
    """OFF（默认）：与历史 task.get('doc_title','') 完全一致——恒空串。"""
    monkeypatch.delenv("RAG_VLM_DOC_CONTEXT", raising=False)
    assert _funnel_doc_title({"filename": "注塑机安全操作规程.docx"}) == ""
    assert _funnel_doc_title({}) == ""


def test_off_explicit_doc_title_passthrough(monkeypatch):
    """OFF 时显式 doc_title（未来若有）仍透传——与旧表达式语义一致。"""
    monkeypatch.delenv("RAG_VLM_DOC_CONTEXT", raising=False)
    assert _funnel_doc_title({"doc_title": "显式标题"}) == "显式标题"


def test_on_derives_from_filename_stem(monkeypatch):
    monkeypatch.setenv("RAG_VLM_DOC_CONTEXT", "true")
    assert _funnel_doc_title({"filename": "注塑机安全操作规程.docx"}) == "注塑机安全操作规程"
    # 路径形 filename 取 basename 词干
    assert _funnel_doc_title({"filename": "raw/it/富岭U8+成品仓库操作手册.pdf"}) == "富岭U8+成品仓库操作手册"


def test_on_explicit_doc_title_wins(monkeypatch):
    monkeypatch.setenv("RAG_VLM_DOC_CONTEXT", "1")
    assert _funnel_doc_title({"doc_title": "显式标题", "filename": "x.docx"}) == "显式标题"


def test_on_missing_filename_stays_empty(monkeypatch):
    monkeypatch.setenv("RAG_VLM_DOC_CONTEXT", "yes")
    assert _funnel_doc_title({}) == ""
    assert _funnel_doc_title({"filename": ""}) == ""


def test_env_value_must_be_truthy(monkeypatch):
    monkeypatch.setenv("RAG_VLM_DOC_CONTEXT", "false")
    assert _funnel_doc_title({"filename": "a.docx"}) == ""
    monkeypatch.setenv("RAG_VLM_DOC_CONTEXT", "")
    assert _funnel_doc_title({"filename": "a.docx"}) == ""
