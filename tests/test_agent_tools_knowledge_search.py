# -*- coding: utf-8 -*-
"""
test_agent_tools_knowledge_search.py — 首工具（Task 13 / 报告 §4）

核心：**ACL 从 ctx.acl_groups 注入 retrieve_and_enrich 的 user_dept，绝不从 args**；
伪造 user_dept 参数经 schema additionalProperties:false 被拒（越权护栏）。
retrieve_and_enrich 以 mock 隔离，不碰真检索。
"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.tool import EnterpriseTool, RiskLevel, ToolArgsError
from opensearch_pipeline.agent_tools import build_default_registry
from opensearch_pipeline.agent_tools.knowledge_search import (
    KnowledgeSearchTool,
    SpeculativeSearch,
    query_matches_question,
)

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


# ── 答案契约对齐（2026-07-11）：artifacts 旁路 + [📷 图片] <<IMG:N>> 标记 ──────────
def test_artifacts_carry_chunks_but_never_serialize(monkeypatch):
    """chunks 经 artifacts 进程内直通（供 serving 构建 sources/content_blocks）；
    exclude=True → model_dump 不带（digest/落库/线协议零泄漏面）。"""
    rows = [{"doc_id": "D1", "chunk_text": "内容A", "doc_title": "标准A"}]
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)
    res = KnowledgeSearchTool().run(_ctx(), {"query": "包装规范"})
    assert res.artifacts == {"chunks": rows}
    assert "artifacts" not in res.model_dump()


def test_img_marker_appended_for_renderable_chunks(monkeypatch):
    """带可渲染图的 chunk → 条目行追加 ` [📷 图片] <<IMG:i>>`（与普通问答同一实现/编号同源）；
    无图 chunk 不加。"""
    rows = [
        {"doc_id": "D1", "chunk_text": "第一步…", "doc_title": "SOP",
         "image_refs": [{"oss_key": "processing/assets/a.png", "source_image": "a.png"}]},
        {"doc_id": "D2", "chunk_text": "纯文字条款", "doc_title": "制度"},
    ]
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)
    monkeypatch.setattr("opensearch_pipeline.content_blocks_builder.renderable_image_refs",
                        lambda c: c.get("image_refs") or [])
    res = KnowledgeSearchTool().run(_ctx(), {"query": "步骤"})
    text = res.content[0].text
    assert "[1] SOP [📷 图片] <<IMG:1>>" in text
    assert "<<IMG:2>>" not in text and "[2] 制度\n" in text


# ── 投机检索（2026-07-11 延迟优化）────────────────────────────────────────────
_Q = "质检发现纸杯杯口尺寸超差，应该走什么处理流程？"


def test_query_match_rules():
    assert query_matches_question(_Q, _Q)                                   # 全等
    assert query_matches_question("纸杯杯口尺寸超差", _Q)                    # 子串凝练
    assert query_matches_question("纸杯杯口尺寸超差 处理流程", _Q)           # 乱序凝练（bigram 包含）
    assert not query_matches_question("不合格品评审单 填写要求", _Q)         # 换主题（二次检索）
    assert not query_matches_question("", _Q) and not query_matches_question(_Q, "")


def test_speculative_hit_skips_real_retrieve(monkeypatch):
    """命中：预取结果直接复用，真检索零调用；receipt 打 speculative 标。"""
    calls = {"n": 0}
    rows = [{"doc_id": "D1", "chunk_text": "流程内容", "doc_title": "不合格品控制程序"}]

    def fake_retrieve(query, **k):
        calls["n"] += 1
        return rows

    monkeypatch.setattr(_RETRIEVE, fake_retrieve)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        spec = SpeculativeSearch(_Q, ["production"], pool)
        ctx = _ctx()
        ctx.speculative_search = spec
        res = KnowledgeSearchTool().run(ctx, {"query": "纸杯杯口尺寸超差 处理流程"})
        assert res.status == "succeeded" and res.receipt.get("speculative") is True
        assert res.artifacts == {"chunks": rows}
        assert calls["n"] == 1                       # 只有预取那一次
        # 单次消费：同 run 第二次检索（即使同查询）走真检索
        res2 = KnowledgeSearchTool().run(ctx, {"query": "纸杯杯口尺寸超差 处理流程"})
        assert res2.receipt.get("speculative") is None and calls["n"] == 2
    finally:
        pool.shutdown(wait=False)


def test_speculative_miss_and_failure_fall_back(monkeypatch):
    """miss（换主题/显式 top_k）与预取异常：恒回退真检索，fail-open。"""
    calls = {"n": 0, "queries": []}

    def fake_retrieve(query, **k):
        calls["n"] += 1
        calls["queries"].append(query)
        # 该查询只在【第一次】（预取）炸，回退的真检索成功——检验 fail-open 链路
        if query == "预取会炸的问题" and calls["queries"].count(query) == 1:
            raise RuntimeError("boom")
        return [{"doc_id": "D", "chunk_text": "x", "doc_title": "t"}]

    monkeypatch.setattr(_RETRIEVE, fake_retrieve)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        # miss：换主题
        ctx = _ctx()
        ctx.speculative_search = SpeculativeSearch(_Q, None, pool)
        res = KnowledgeSearchTool().run(ctx, {"query": "不合格品评审单 填写要求"})
        assert res.status == "succeeded" and res.receipt.get("speculative") is None
        # miss：显式 top_k
        ctx2 = _ctx()
        ctx2.speculative_search = SpeculativeSearch(_Q, None, pool)
        res2 = KnowledgeSearchTool().run(ctx2, {"query": _Q, "top_k": 5})
        assert res2.receipt.get("speculative") is None
        # 预取异常 → 命中判定过但 future 抛 → 回退真检索
        ctx3 = _ctx()
        ctx3.speculative_search = SpeculativeSearch("预取会炸的问题", None, pool)
        res3 = KnowledgeSearchTool().run(ctx3, {"query": "预取会炸的问题"})
        assert res3.status == "succeeded" and res3.receipt.get("speculative") is None
    finally:
        pool.shutdown(wait=False)
