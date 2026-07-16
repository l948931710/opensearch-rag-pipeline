# -*- coding: utf-8 -*-
"""
test_agent_tools_knowledge_search.py — 首工具（Task 13 / 报告 §4）

核心：**ACL 从 ctx.acl_groups 注入 retrieve_and_enrich 的 user_dept，绝不从 args**；
伪造 user_dept 参数经 schema additionalProperties:false 被拒（越权护栏）。
retrieve_and_enrich 以 mock 隔离，不碰真检索。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from opensearch_pipeline.agent_runtime.tool import EnterpriseTool, RiskLevel, ToolArgsError
from opensearch_pipeline.agent_tools import build_default_registry
from opensearch_pipeline.agent_tools.knowledge_search import (
    KnowledgeSearchTool,
    SearchSession,
    SpeculativeSearch,
    _dedup_key,
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
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_BUDGET", "false")   # 旧格式契约测试
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
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_BUDGET", "false")   # 断言旧 artifacts 形状
    rows = [{"doc_id": "D1", "chunk_text": "内容A", "doc_title": "标准A"}]
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)
    res = KnowledgeSearchTool().run(_ctx(), {"query": "包装规范"})
    assert res.artifacts == {"chunks": rows}
    assert "artifacts" not in res.model_dump()


def test_img_marker_appended_for_renderable_chunks(monkeypatch):
    """带可渲染图的 chunk → 条目行追加 ` [📷 图片] <<IMG:i>>`（与普通问答同一实现/编号同源）；
    无图 chunk 不加。"""
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_BUDGET", "false")   # 旧手工格式专属断言
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
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_BUDGET", "false")   # 断言旧 artifacts 形状
    calls = {"n": 0}
    rows = [{"doc_id": "D1", "chunk_text": "流程内容", "doc_title": "不合格品控制程序"}]

    def fake_retrieve(query, **k):
        calls["n"] += 1
        return rows

    monkeypatch.setattr(_RETRIEVE, fake_retrieve)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        spec = SpeculativeSearch(_Q, ["production"], pool)
        spec.start()   # 模拟已准入（F1：起跑在 executor 占槽后）
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
        ctx.speculative_search.start()
        res = KnowledgeSearchTool().run(ctx, {"query": "不合格品评审单 填写要求"})
        assert res.status == "succeeded" and res.receipt.get("speculative") is None
        # miss：显式 top_k
        ctx2 = _ctx()
        ctx2.speculative_search = SpeculativeSearch(_Q, None, pool)
        ctx2.speculative_search.start()
        res2 = KnowledgeSearchTool().run(ctx2, {"query": _Q, "top_k": 5})
        assert res2.receipt.get("speculative") is None
        # 预取异常 → 命中判定过但 future 抛 → 回退真检索
        ctx3 = _ctx()
        ctx3.speculative_search = SpeculativeSearch("预取会炸的问题", None, pool)
        ctx3.speculative_search.start()
        res3 = KnowledgeSearchTool().run(ctx3, {"query": "预取会炸的问题"})
        assert res3.status == "succeeded" and res3.receipt.get("speculative") is None
    finally:
        pool.shutdown(wait=False)


def test_speculative_not_started_falls_back_and_start_idempotent(monkeypatch):
    """F1 懒启动：构造零成本、从未 start()（= submit 被 429 拒）→ 预取零发生，
    take_if_match 恒 None、工具照走真检索；start() 幂等（重复调用只提交一次预取）。"""
    calls = {"n": 0}

    def fake_retrieve(query, **k):
        calls["n"] += 1
        return [{"doc_id": "D", "chunk_text": "x", "doc_title": "t"}]

    monkeypatch.setattr(_RETRIEVE, fake_retrieve)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        ctx = _ctx()
        spec = SpeculativeSearch(_Q, None, pool)
        ctx.speculative_search = spec              # 挂上 ctx 但从未准入起跑
        assert spec.take_if_match(_Q, None) is None
        res = KnowledgeSearchTool().run(ctx, {"query": _Q})
        assert res.status == "succeeded" and res.receipt.get("speculative") is None
        assert calls["n"] == 1                     # 只有回退的真检索——预取零发生
        spec.start()
        spec.start()                               # 幂等：第二次是 no-op
        spec._future.result(timeout=2)
        assert calls["n"] == 2                     # 预取恰好一次
    finally:
        pool.shutdown(wait=False)


def test_speculative_finalize_observability(monkeypatch):
    """§4.8（perf 批次 A）：finalize() 汇报 started/hit/miss/wasted_ms/arms_est（纯观测，
    四态覆盖：命中 / 起跑未消费 / 从未起跑 / arms 来自 config）。"""
    rows = [{"doc_id": "D", "chunk_text": "x", "doc_title": "t"}]
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        # 1) 起跑 + 命中 → spec_hit，无浪费
        hit = SpeculativeSearch(_Q, None, pool)
        hit.start()
        assert hit.take_if_match(_Q, None) == rows
        f1 = hit.finalize()
        assert f1["spec_started"] is True and f1["spec_hit"] is True
        assert f1["spec_miss"] is False and f1["spec_wasted_ms"] == 0
        assert isinstance(f1["spec_arms_est"], int) and f1["spec_arms_est"] >= 1

        # 2) 起跑但从未消费（换主题 miss）→ spec_miss + wasted_ms=检索**真实耗时**
        #    （批次 B 口径修正：模拟 run 已跑 100s——wasted 必须取 _fetch_ms 而非整窗）
        miss = SpeculativeSearch(_Q, None, pool)
        miss.start()
        miss._future.result(timeout=2)             # 等预取完成，_fetch_ms 已回写
        miss._started_at = time.monotonic() - 100.0
        f2 = miss.finalize()
        assert f2["spec_started"] is True and f2["spec_hit"] is False
        assert f2["spec_miss"] is True
        assert f2["spec_wasted_ms"] == miss._fetch_ms and f2["spec_wasted_ms"] < 100_000

        # 2b) 检索仍在途（run 先收尾）→ 退回「起跑至今」在途上界估计
        gate = threading.Event()
        monkeypatch.setattr(_RETRIEVE, lambda query, **k: (gate.wait(5), rows)[1])
        inflight = SpeculativeSearch(_Q, None, pool)
        inflight.start()
        inflight._started_at = time.monotonic() - 7.0
        f2b = inflight.finalize()
        assert f2b["spec_miss"] is True and f2b["spec_wasted_ms"] >= 7_000
        gate.set()
        inflight._future.result(timeout=2)
        monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)

        # 3) 从未起跑（submit 被 429 拒）→ 全 False，无浪费
        never = SpeculativeSearch(_Q, None, pool)
        f3 = never.finalize()
        assert f3 == {"spec_started": False, "spec_hit": False, "spec_miss": False,
                      "spec_wasted_ms": 0, "spec_arms_est": never._arms_est}
    finally:
        pool.shutdown(wait=False)


# ── 上下文预算打包（延迟优化第三刀，2026-07-11；设计 v2 + 三评审条件）──────────────


def _mk_chunk(i, text_len=400, title=None, cid=None, parent=None, score=8.0):
    return {"doc_id": f"D{i}", "chunk_id": cid or f"C{i}", "title": title or f"文档{i}.docx",
            "doc_title": title or f"文档{i}.docx", "chunk_text": f"块{i}正文。" * (text_len // 5),
            "score": score, "chunk_type": "text_chunk",
            **({"parent_chunk_id": parent} if parent else {})}


def _budget_ctx(session=None):
    ns = SimpleNamespace(acl_groups=("production",))
    ns.search_session = session
    ns.speculative_search = None
    return ns


def test_budget_flag_off_bytes_identical(monkeypatch):
    """kill switch（默认已翻 on）：显式 false → 工具文本走旧手工格式，
    artifacts 无 included 键——逐字节回旧行为。"""
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_BUDGET", "false")
    rows = [_mk_chunk(1), _mk_chunk(2)]
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)
    res = KnowledgeSearchTool().run(_budget_ctx(), {"query": "q"})
    assert res.content[0].text.startswith("[1] 文档1.docx")
    assert "included" not in (res.artifacts or {})


def test_budget_packs_with_normal_path_semantics(monkeypatch):
    """flag on：_format_context_ex 同源打包（[文档N] header + 相关度标签）+ 预算生效
    + 诚实注记 + artifacts 三键契约 + receipt 计数。"""
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_BUDGET", "true")
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_CHARS", "1200")
    rows = [_mk_chunk(i) for i in range(1, 8)]           # 7×~400 字 ≫ 1200 预算
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)
    res = KnowledgeSearchTool().run(_budget_ctx(SearchSession()), {"query": "q"})
    text = res.content[0].text
    assert "[文档1] 文档1.docx" in text                    # 普通路径 header 同源
    assert "相关度" in text                                # 标签进文本
    assert "因篇幅未展开" in text                          # 诚实注记
    assert res.receipt["chunk_count"] == 7 and res.receipt["dropped"] >= 3
    assert res.receipt["ctx_chars"] == len(text)
    arts = res.artifacts
    assert list(arts) >= ["chunks"] and set(arts) == {"chunks", "included", "dedup_keys"}
    assert arts["chunks"] == rows                          # 打包列表=IMG 编号基准（首检索全量）
    assert len(arts["included"]) < 7                       # 预算截断感知
    # seen 只登记完整展开块：dedup_keys ⊆ included 且不含半截块
    assert 0 < len(arts["dedup_keys"]) <= len(arts["included"])


def test_budget_dedup_byte_equal_and_pagination(monkeypatch):
    """跨检索去重=字节等同；送达点提交后第二次检索：重复块被聚合、腾出预算让首查
    被截的尾块完整展开（翻页涌现性质）；第二次 pure_text（无 <<IMG:）。"""
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_BUDGET", "true")
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_CHARS", "1200")
    rows = [_mk_chunk(i) for i in range(1, 8)]
    rows[0]["image_refs"] = [{"oss_key": "processing/assets/x.png", "source_image": "x.png"}]
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)
    session = SearchSession()
    tool = KnowledgeSearchTool()
    r1 = tool.run(_budget_ctx(session), {"query": "q1"})
    assert "<<IMG:" in r1.content[0].text                  # 首检索有标记
    session.commit_keys(r1.artifacts["dedup_keys"])        # 模拟 executor 送达点提交
    n_full_1 = len(r1.artifacts["dedup_keys"])
    r2 = tool.run(_budget_ctx(session), {"query": "q2"})
    t2 = r2.content[0].text
    assert r2.receipt["dedup"] == n_full_1                 # 完整展开过的被去重
    assert "条与先前检索重复" in t2
    assert "<<IMG:" not in t2                              # 第 2+ 次 pure_text
    # 翻页：首查完整展开的 块1/块2 被去重腾出预算，首查被截/被丢的 块3/块4 此次完整展开
    assert "块1正文" not in t2 and "块4正文" in t2
    # 字节等同：同 chunk_id 不同文本不去重
    rows2 = [dict(_mk_chunk(1), chunk_text="块1改写后的不同正文。" * 40)]
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows2)
    r3 = tool.run(_budget_ctx(session), {"query": "q3"})
    assert r3.receipt["dedup"] == 0 and "块1改写后的不同正文" in r3.content[0].text


def test_budget_step_family_note_and_all_dup_edge(monkeypatch):
    """步骤族形态注记（同 parent 后续步骤 ≠ 较低相关度）；全重复空包给显式说明。"""
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_BUDGET", "true")
    monkeypatch.setenv("RAG_AGENT_TOOL_CONTEXT_CHARS", "900")
    rows = [_mk_chunk(1, parent="P1"), _mk_chunk(2, parent="P1"),
            _mk_chunk(3, parent="P1"), _mk_chunk(4, parent="P1")]
    monkeypatch.setattr(_RETRIEVE, lambda query, **k: rows)
    session = SearchSession()
    r1 = KnowledgeSearchTool().run(_budget_ctx(session), {"query": "流程"})
    assert "个后续步骤因篇幅未展开" in r1.content[0].text   # 步骤族措辞
    # 全重复边界：seen 收下全部 → 第二次同结果全被去重
    session.commit_keys([_dedup_key(c) for c in rows])
    r2 = KnowledgeSearchTool().run(_budget_ctx(session), {"query": "流程"})
    assert "完全重复" in r2.content[0].text and r2.artifacts["chunks"] == []


def test_dedup_key_fallback_full_text_hash():
    """chunk_id 缺失退 (doc_id, sha1(完整文本))——拼接前缀相同、尾部不同的两块不同 key。"""
    a = {"doc_id": "D", "chunk_text": "同前缀" * 100 + "尾A"}
    b = {"doc_id": "D", "chunk_text": "同前缀" * 100 + "尾B"}
    assert _dedup_key(a) != _dedup_key(b)
    assert _dedup_key(a)[0] == "doc"
