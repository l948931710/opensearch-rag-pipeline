# -*- coding: utf-8 -*-
"""
test_agent_tools_knowledge_search.py — 首工具（Task 13 / 报告 §4）

核心：**ACL 从 ctx.acl_groups 注入 retrieve_and_enrich 的 user_dept，绝不从 args**；
伪造 user_dept 参数经 schema additionalProperties:false 被拒（越权护栏）。
retrieve_and_enrich 以 mock 隔离，不碰真检索。
"""
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.tool import EnterpriseTool, RiskLevel, ToolArgsError
from opensearch_pipeline.agent_tools import build_default_registry
from opensearch_pipeline.agent_tools.knowledge_search import KnowledgeSearchTool

_RETRIEVE = "opensearch_pipeline.retriever.retrieve_and_enrich"


def _ctx(acl=("production", "marketing")):
    return SimpleNamespace(acl_groups=tuple(acl))


def test_is_enterprise_tool():
    assert isinstance(KnowledgeSearchTool(), EnterpriseTool)


def test_spec_readonly_no_user_dept():
    spec = KnowledgeSearchTool().spec
    assert spec.risk_level is RiskLevel.READ_ONLY and spec.permission_scope == "kb.search"
    assert "user_dept" not in spec.input_schema["properties"]
    assert spec.input_schema["additionalProperties"] is False


def test_acl_injected_from_ctx_not_args(monkeypatch):
    captured = {}

    def fake_retrieve(query, *, top_k=None, user_dept=None, **k):
        captured.update(query=query, top_k=top_k, user_dept=user_dept)
        return [{"doc_id": "D1", "chunk_text": "内容A", "doc_title": "标准A"}]

    monkeypatch.setattr(_RETRIEVE, fake_retrieve)
    res = KnowledgeSearchTool().run(_ctx(("production", "marketing")), {"query": "包装规范", "top_k": 5})
    assert res.status == "succeeded"
    assert captured["query"] == "包装规范" and captured["top_k"] == 5
    assert captured["user_dept"] == ["production", "marketing"]     # ← 从 ctx.acl_groups
    assert res.receipt["doc_ids"] == ["D1"] and res.receipt["chunk_count"] == 1


def test_forged_user_dept_rejected(monkeypatch):
    monkeypatch.setattr(_RETRIEVE, lambda **k: [])
    # 请求体伪造 user_dept → additionalProperties:false → ToolArgsError（越权被拒，检索不会被调）
    with pytest.raises(ToolArgsError):
        KnowledgeSearchTool().run(_ctx(("production",)), {"query": "x", "user_dept": "finance"})


def test_empty_acl_yields_none_user_dept(monkeypatch):
    captured = {}

    def fake(query, *, top_k=None, user_dept=None, **k):
        captured["user_dept"] = user_dept
        return []

    monkeypatch.setattr(_RETRIEVE, fake)
    KnowledgeSearchTool().run(_ctx(()), {"query": "x"})
    assert captured["user_dept"] is None


def test_retrieve_exception_returns_fail(monkeypatch):
    def boom(**k):
        raise RuntimeError("HA3 down")

    monkeypatch.setattr(_RETRIEVE, boom)
    res = KnowledgeSearchTool().run(_ctx(), {"query": "x"})
    assert res.status == "failed" and "HA3 down" in res.error


def test_no_results_message(monkeypatch):
    monkeypatch.setattr(_RETRIEVE, lambda **k: [])
    res = KnowledgeSearchTool().run(_ctx(), {"query": "x"})
    assert res.status == "succeeded" and "未检索到" in res.content[0].text


def test_build_default_registry_has_knowledge_search():
    reg = build_default_registry()
    assert reg.resolve("knowledge_search").spec.name == "knowledge_search"
