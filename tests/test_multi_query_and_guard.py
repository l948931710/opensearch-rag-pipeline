# -*- coding: utf-8 -*-
"""
test_multi_query_and_guard.py — 多意图查询分解（RAG_MULTI_QUERY_MODE）与
低置信度护栏（RAG_LOW_CONFIDENCE_GUARD）单元测试。

覆盖：
  1. query_decomposer：启发式触发、LLM 输出解析（含降级路径）、mode 路由。
  2. retriever._multi_query_search：轮转交错合并 + chunk 去重 + 单路失败 fail-open。
  3. retrieve_and_enrich 集成：mode=off 零行为变化；分解触发时走 fan-out。
  4. llm_generator 低置信度护栏：判别逻辑 + system prompt 注入（默认关闭零变更）。
"""
import json
import types

import pytest

import opensearch_pipeline.llm_generator as G
import opensearch_pipeline.query_decomposer as QD
import opensearch_pipeline.retriever as R
from opensearch_pipeline.config import AlibabaVectorSearchConfig, LLMConfig, RAGConfig


# ──────────────────────────────────────────────────────────────
# 1. 启发式触发
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    "新员工从入职到入住宿舍需要经过哪些流程？",       # 从…到
    "员工手册和行政部职责范围分别讲什么？",           # 和 + 分别
    "纸杯检验与淋膜检验的区别是什么",                 # 与 + 区别
    "请假怎么办理？工资什么时候发？",                 # 多个问号
    "外来人员入厂、访客接待有哪些安全要求",           # 顿号并列
])
def test_heuristic_triggers_on_multi_intent(query):
    assert QD.looks_multi_intent(query)


@pytest.mark.parametrize("query", [
    "员工工作服管理有哪些规定？",   # 单主题
    "怎么请假",                     # 短查询
    "U8系统怎么开发票",             # 单主题
])
def test_heuristic_skips_single_intent(query):
    assert not QD.looks_multi_intent(query)


# ──────────────────────────────────────────────────────────────
# 2. LLM 输出解析
# ──────────────────────────────────────────────────────────────

def test_parse_clean_json_array():
    subs = QD._parse_subqueries('["员工入职流程", "宿舍入住安排"]', max_sub=3, original="q")
    assert subs == ["员工入职流程", "宿舍入住安排"]


def test_parse_fenced_and_noisy_output():
    content = '好的，以下是拆分：\n```json\n["A流程", "B流程"]\n```'
    assert QD._parse_subqueries(content, max_sub=3, original="q") == ["A流程", "B流程"]


@pytest.mark.parametrize("content", [
    "[]",                       # LLM 判定无需分解
    '["只有一条"]',             # 不足 2 条
    "这不是JSON",               # 无数组
    '{"a": 1}',                 # 非数组 JSON
    '[1, 2, 3]',                # 非字符串元素
])
def test_parse_degenerate_outputs_return_empty(content):
    assert QD._parse_subqueries(content, max_sub=3, original="q") == []


def test_parse_dedupes_caps_and_drops_original():
    content = json.dumps(["原问题", "A", "A", "B", "C", "D"], ensure_ascii=False)
    subs = QD._parse_subqueries(content, max_sub=3, original="原问题")
    assert subs == ["A", "B", "C"]  # 去掉原问题与重复项，截到 max_sub


# ──────────────────────────────────────────────────────────────
# 3. maybe_decompose 模式路由
# ──────────────────────────────────────────────────────────────

def _qd_cfg(monkeypatch, mode):
    cfg = types.SimpleNamespace(
        rag=RAGConfig(multi_query_mode=mode),
        llm=LLMConfig(api_key="sk-test", api_base_url="https://example.invalid/v1",
                      model="qwen-test"),
    )
    monkeypatch.setattr(QD, "get_config", lambda: cfg)
    return cfg


class _FakeResp:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_mode_off_never_calls_llm(monkeypatch):
    _qd_cfg(monkeypatch, "off")
    monkeypatch.setattr(QD.requests, "post",
                        lambda *a, **k: pytest.fail("mode=off 不应调用 LLM"))
    assert QD.maybe_decompose("员工手册和行政部职责分别讲什么？") == []


def test_mode_auto_skips_llm_when_heuristic_misses(monkeypatch):
    _qd_cfg(monkeypatch, "auto")
    monkeypatch.setattr(QD.requests, "post",
                        lambda *a, **k: pytest.fail("启发式未触发不应调用 LLM"))
    assert QD.maybe_decompose("怎么请假") == []


def test_mode_auto_decomposes_on_trigger(monkeypatch):
    _qd_cfg(monkeypatch, "auto")
    monkeypatch.setattr(QD.requests, "post",
                        lambda *a, **k: _FakeResp('["员工入职流程", "宿舍入住安排"]'))
    assert QD.maybe_decompose("新员工从入职到入住宿舍要经过哪些流程？") == \
        ["员工入职流程", "宿舍入住安排"]


def test_mode_llm_always_asks(monkeypatch):
    _qd_cfg(monkeypatch, "llm")
    calls = []

    def _post(*a, **k):
        calls.append(1)
        return _FakeResp("[]")

    monkeypatch.setattr(QD.requests, "post", _post)
    assert QD.maybe_decompose("怎么请假") == []   # 无启发式信号也会问 LLM
    assert calls, "mode=llm 必须调用 LLM"


def test_llm_failure_fails_open(monkeypatch):
    _qd_cfg(monkeypatch, "llm")

    def _post(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(QD.requests, "post", _post)
    assert QD.maybe_decompose("员工手册和行政部职责分别讲什么？") == []


# ──────────────────────────────────────────────────────────────
# 4. _multi_query_search 合并语义
# ──────────────────────────────────────────────────────────────

def _ch(cid, doc, score=1.0):
    return {"chunk_id": cid, "doc_id": doc, "chunk_index": 0,
            "chunk_text": f"text-{cid}", "title": doc, "score": score}


def test_multi_query_round_robin_merge_and_dedupe(monkeypatch):
    results = {
        "原查询": [_ch("a1", "docA"), _ch("a2", "docA"), _ch("a3", "docA")],
        "子查询B": [_ch("b1", "docB"), _ch("a1", "docA")],   # a1 与原查询重复
        "子查询C": [_ch("c1", "docC")],
    }
    monkeypatch.setattr(R, "search_chunks",
                        lambda q, **k: list(results[q]))
    merged = R._multi_query_search(
        "原查询", ["子查询B", "子查询C"], fetch_k=20, top_k=4,
        user_dept=None, rerank_enable=False, multimodal=False)
    # 轮转：a1(原), b1(B), c1(C), a2(原)；B 路的 a1 重复被去掉
    assert [c["chunk_id"] for c in merged] == ["a1", "b1", "c1", "a2"]


def test_multi_query_one_route_fails_open(monkeypatch):
    def _search(q, **k):
        if q == "坏路":
            raise RuntimeError("HA3 boom")
        return [_ch("a1", "docA"), _ch("a2", "docA")]

    monkeypatch.setattr(R, "search_chunks", _search)
    merged = R._multi_query_search(
        "原查询", ["坏路"], fetch_k=20, top_k=4,
        user_dept=None, rerank_enable=False, multimodal=False)
    assert [c["chunk_id"] for c in merged] == ["a1", "a2"]   # 坏路被忽略


def test_multi_query_all_routes_raise_propagates(monkeypatch):
    """持续性故障（回退单路检索也失败）必须向上传播为错误，不能吞成 NO_RESULT。"""
    def _search(q, **k):
        raise RuntimeError("HA3 boom")

    monkeypatch.setattr(R, "search_chunks", _search)
    with pytest.raises(RuntimeError, match="HA3 boom"):
        R._multi_query_search("原查询", ["子B"], fetch_k=20, top_k=4,
                              user_dept=None, rerank_enable=False, multimodal=False)


def test_multi_query_all_routes_raise_falls_back_to_single(monkeypatch):
    """瞬时故障：各路并发检索全挂，但回退单路重试成功 → 返回单路结果。"""
    calls = {"n": 0}

    def _search(q, **k):
        calls["n"] += 1
        if calls["n"] <= 2:   # 两路并发检索都失败
            raise RuntimeError("transient")
        return [_ch("a1", "docA")]

    monkeypatch.setattr(R, "search_chunks", _search)
    merged = R._multi_query_search("原查询", ["子B"], fetch_k=20, top_k=4,
                                   user_dept=None, rerank_enable=False, multimodal=False)
    assert [c["chunk_id"] for c in merged] == ["a1"]


def test_multi_query_all_routes_empty_no_extra_search(monkeypatch):
    """各路正常返回空（真·无结果）→ 直接 []，不做多余的回退检索。"""
    calls = {"n": 0}

    def _search(q, **k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(R, "search_chunks", _search)
    assert R._multi_query_search("原查询", ["子B"], fetch_k=20, top_k=4,
                                 user_dept=None, rerank_enable=False, multimodal=False) == []
    assert calls["n"] == 2   # 仅两路各一次，无回退重试


def test_multi_query_mixed_score_scale_reverts_to_fused(monkeypatch):
    """一路重排成功（0~1 分）、一路重排降级（融合分）→ 统一回退融合分。"""
    def _search(q, **k):
        if q == "原查询":
            return [{"chunk_id": "a1", "doc_id": "docA", "chunk_index": 0,
                     "chunk_text": "t", "title": "docA", "score": 7.0}]
        return [{"chunk_id": "b1", "doc_id": "docB", "chunk_index": 0,
                 "chunk_text": "t", "title": "docB", "score": 6.0}]

    def _fake_rerank(q, chs, top_k=None, multimodal=False):
        if q == "原查询":   # 该路重排成功：score 切到 rerank 分
            for c in chs:
                c["_fused_score"] = c["score"]
                c["rerank_score"] = 0.95
                c["score"] = 0.95
            return chs
        return chs          # 该路重排降级：保持融合分

    import opensearch_pipeline.reranker as RR
    monkeypatch.setattr(R, "search_chunks", _search)
    monkeypatch.setattr(RR, "rerank_chunks", _fake_rerank)
    merged = R._multi_query_search("原查询", ["子B"], fetch_k=20, top_k=4,
                                   user_dept=None, rerank_enable=True, multimodal=False)
    assert all("rerank_score" not in c for c in merged)
    by_id = {c["chunk_id"]: c for c in merged}
    assert by_id["a1"]["score"] == 7.0   # 还原融合分
    assert by_id["b1"]["score"] == 6.0


def test_multi_query_dedupe_falls_back_to_ha3_id(monkeypatch):
    """chunk_id 为空的历史 chunk：去重键回退 HA3 主键 id，跨路重复仍被去掉。"""
    legacy = {"chunk_id": "", "id": "pk-1", "doc_id": "docA", "chunk_index": 3,
              "chunk_text": "t", "title": "docA", "score": 1.0}

    def _search(q, **k):
        return [dict(legacy)]

    monkeypatch.setattr(R, "search_chunks", _search)
    merged = R._multi_query_search("原查询", ["子B"], fetch_k=20, top_k=4,
                                   user_dept=None, rerank_enable=False, multimodal=False)
    assert len(merged) == 1


# ──────────────────────────────────────────────────────────────
# 4b. _select_with_doc_cap（文档多样性限额）
# ──────────────────────────────────────────────────────────────

def test_doc_cap_lets_second_doc_in():
    pool = [_ch(f"a{i}", "docA") for i in range(6)] + [_ch("b1", "docB")]
    out = R._select_with_doc_cap(pool, top_k=5, cap=4)
    ids = [c["chunk_id"] for c in out]
    assert ids == ["a0", "a1", "a2", "a3", "b1"]   # docA 限 4 席，docB 进 top-5


def test_doc_cap_zero_is_pure_truncation():
    pool = [_ch(f"a{i}", "docA") for i in range(6)]
    out = R._select_with_doc_cap(pool, top_k=5, cap=0)
    assert [c["chunk_id"] for c in out] == ["a0", "a1", "a2", "a3", "a4"]


def test_doc_cap_backfills_when_diversity_short():
    # 池里只有一个文档：限额后不足 top_k，需按原序回填，结果数量不能减少
    pool = [_ch(f"a{i}", "docA") for i in range(6)]
    out = R._select_with_doc_cap(pool, top_k=5, cap=2)
    assert [c["chunk_id"] for c in out] == ["a0", "a1", "a2", "a3", "a4"]


def test_doc_cap_small_pool_untouched():
    pool = [_ch("a1", "docA"), _ch("a2", "docA")]
    assert R._select_with_doc_cap(pool, top_k=5, cap=1) == pool


def test_doc_cap_does_not_promote_cover_chunks():
    """封面 chunk（search_chunks 降权标记）不得借限额"晋升"——只作最后回填。"""
    cover = _ch("cov1", "docC")
    cover["_is_cover"] = True
    pool = [_ch("a0", "docA"), _ch("a1", "docA"), cover,
            _ch("a2", "docA"), _ch("b1", "docB"), _ch("a3", "docA")]
    out = R._select_with_doc_cap(pool, top_k=4, cap=2)
    # docA 限 2 席 → a0,a1；封面被绕过 → b1；回填顺序正文（a2）先于封面
    assert [c["chunk_id"] for c in out] == ["a0", "a1", "b1", "a2"]


def test_doc_cap_cover_backfills_last_when_short():
    cover = _ch("cov1", "docC")
    cover["_is_cover"] = True
    pool = [_ch("a0", "docA"), _ch("a1", "docA"), cover, _ch("a2", "docA")]
    out = R._select_with_doc_cap(pool, top_k=3, cap=2)
    # docA 限 2 → a0,a1；不足 3 时先回填溢出正文 a2（封面仍排最后，进不了 top-3）
    assert [c["chunk_id"] for c in out] == ["a0", "a1", "a2"]


# ──────────────────────────────────────────────────────────────
# 5. retrieve_and_enrich 集成
# ──────────────────────────────────────────────────────────────

def _retriever_cfg(monkeypatch, mode):
    cfg = types.SimpleNamespace(
        rag=RAGConfig(multi_query_mode=mode),
        alibaba_vector=AlibabaVectorSearchConfig(rerank_enable=False),
    )
    monkeypatch.setattr(R, "get_config", lambda: cfg)
    # 旁路外部依赖：embedding / 拼接 / 扩展皆为恒等
    monkeypatch.setattr(R, "get_query_embedding", lambda q, **k: ([0.1], [], []))
    monkeypatch.setattr(R, "stitch_neighbor_chunks", lambda chs, **k: chs)
    monkeypatch.setattr(R, "expand_step_context", lambda chs, q: chs)
    return cfg


def test_retrieve_and_enrich_mode_off_single_path(monkeypatch):
    _retriever_cfg(monkeypatch, "off")
    monkeypatch.setattr(QD, "maybe_decompose",
                        lambda q: pytest.fail("mode=off 不应进入分解"))
    monkeypatch.setattr(R, "search_chunks", lambda q, **k: [_ch("a1", "docA")])
    chunks = R.retrieve_and_enrich("怎么请假", top_k=4)
    assert [c["chunk_id"] for c in chunks] == ["a1"]


def test_retrieve_and_enrich_fans_out_when_decomposed(monkeypatch):
    _retriever_cfg(monkeypatch, "auto")
    monkeypatch.setattr(QD, "maybe_decompose", lambda q: ["入职流程", "宿舍入住"])
    results = {
        "新员工从入职到入住宿舍的流程？": [_ch("a1", "docA")],
        "入职流程": [_ch("b1", "docB")],
        "宿舍入住": [_ch("c1", "docC")],
    }
    monkeypatch.setattr(R, "search_chunks", lambda q, **k: list(results[q]))
    chunks = R.retrieve_and_enrich("新员工从入职到入住宿舍的流程？", top_k=4)
    assert [c["chunk_id"] for c in chunks] == ["a1", "b1", "c1"]


def test_retrieve_and_enrich_empty_decomposition_falls_back(monkeypatch):
    _retriever_cfg(monkeypatch, "auto")
    monkeypatch.setattr(QD, "maybe_decompose", lambda q: [])
    monkeypatch.setattr(R, "search_chunks", lambda q, **k: [_ch("a1", "docA")])
    chunks = R.retrieve_and_enrich("怎么请假", top_k=4)
    assert [c["chunk_id"] for c in chunks] == ["a1"]


def test_retrieve_and_enrich_doc_cap_with_rerank(monkeypatch):
    """重排开启 + cap>0：重排不截断（top_k=None），cap 在全池上选 top_k。"""
    cfg = _retriever_cfg(monkeypatch, "off")
    cfg.rag.doc_diversity_cap = 2
    cfg.alibaba_vector = AlibabaVectorSearchConfig(rerank_enable=True, rerank_pool=6)
    pool = [_ch(f"a{i}", "docA", score=9 - i) for i in range(4)] + [_ch("b1", "docB", score=1)]
    monkeypatch.setattr(R, "search_chunks", lambda q, **k: list(pool))

    seen_top_k = {}

    def _fake_rerank(q, chs, top_k=None, multimodal=False):
        seen_top_k["v"] = top_k
        return list(chs)   # 保持原序，模拟重排

    import opensearch_pipeline.reranker as RR
    monkeypatch.setattr(RR, "rerank_chunks", _fake_rerank)
    chunks = R.retrieve_and_enrich("问题", top_k=3)
    assert seen_top_k["v"] is None              # cap 模式下不在重排内截断
    assert [c["chunk_id"] for c in chunks] == ["a0", "a1", "b1"]   # docA 限 2 席


# ──────────────────────────────────────────────────────────────
# 6. 低置信度护栏
# ──────────────────────────────────────────────────────────────

def _gen_cfg(monkeypatch, guard, **rag_kw):
    cfg = types.SimpleNamespace(
        rag=RAGConfig(low_confidence_guard=guard, **rag_kw),
        llm=LLMConfig(api_key="sk-test", api_base_url="https://example.invalid/v1",
                      model="qwen-test"),
    )
    monkeypatch.setattr(G, "get_config", lambda: cfg)
    return cfg


def test_guard_off_by_default_never_fires(monkeypatch):
    _gen_cfg(monkeypatch, guard=False)
    low = [{"chunk_text": "x", "score": 0.3, "rerank_score": 0.3}]
    assert not G._is_low_confidence(low)


def test_guard_uses_rerank_scale_when_present(monkeypatch):
    _gen_cfg(monkeypatch, guard=True)   # rerank medium 默认 0.8
    assert G._is_low_confidence([{"chunk_text": "x", "score": 0.7, "rerank_score": 0.7}])
    assert not G._is_low_confidence([{"chunk_text": "x", "score": 0.85, "rerank_score": 0.85}])


def test_guard_falls_back_to_fused_scale(monkeypatch):
    _gen_cfg(monkeypatch, guard=True)   # fused medium 默认 5.8
    assert G._is_low_confidence([{"chunk_text": "x", "score": 4.2}])
    assert not G._is_low_confidence([{"chunk_text": "x", "score": 7.9}])


def test_guard_empty_chunks_no_fire(monkeypatch):
    _gen_cfg(monkeypatch, guard=True)
    assert not G._is_low_confidence([])


def test_generate_answer_injects_rule_only_when_low(monkeypatch):
    _gen_cfg(monkeypatch, guard=True)
    captured = {}

    def _post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "答"}}], "usage": {}}

        return _Resp()

    monkeypatch.setattr(G, "_http_post", _post)

    low = [{"chunk_text": "x", "title": "t", "score": 0.5, "rerank_score": 0.5}]
    G.generate_answer("q", low)
    assert G.LOW_CONFIDENCE_RULE.strip() in captured["payload"]["messages"][0]["content"]

    high = [{"chunk_text": "x", "title": "t", "score": 0.95, "rerank_score": 0.95}]
    G.generate_answer("q", high)
    assert G.LOW_CONFIDENCE_RULE.strip() not in captured["payload"]["messages"][0]["content"]
