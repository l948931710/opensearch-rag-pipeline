# -*- coding: utf-8 -*-
"""rerank fail-open 的**可见性**（2026-07-27）。

起因：查 L1 排序头部空间时发现，20260726 重冻的 gate.log 里有 2 次
`rerank failed (model=qwen3-rerank, fail-open): Read timed out`。`rerank_chunks` 的降级
路径只写一行 warning 就原样返回，返回的 chunk 上**没有任何痕迹** ⇒ 冻结基线 regime 里的
`rerank_enable: True` 是【配置意图】而非【实况】，被重排的题和悄悄按融合序走的题混进了
同一个 recall@1。DashScope 抖一下就能把整轮悄悄降级，而闸门会把它读成管线回退。

本组测试钉三件事：
  1. 三条降级路径都留痕，池 <2 这种"无可排序"不算降级（不误报）；
  2. 成功路径**不**留痕；
  3. 留痕**不**改行为 —— 尤其不能碰档位阈值链路（`score_level` 是按 chunk 有没有
     `rerank_score` 键选阈值的，降级后自动落回融合阈，这条链本来就是对的，别改坏）。
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("RAG_SIMULATE", "true")

import pytest  # noqa: E402

from opensearch_pipeline import reranker  # noqa: E402
from opensearch_pipeline.reranker import RERANK_DEGRADED_KEY, rerank_chunks  # noqa: E402


def _chunks(n=3):
    return [{"chunk_id": f"c{i}", "chunk_text": f"文本{i}", "score": 9.0 - i} for i in range(n)]


@pytest.fixture
def _key(monkeypatch):
    """给一个非空 key，让流程走到真正的 rerank 调用（再由各用例决定它怎么失败）。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg.embedding, "api_key", "sk-test", raising=False)
    monkeypatch.setattr(cfg.alibaba_vector, "rerank_enable", True, raising=False)
    return cfg


def test_call_failure_is_marked_with_reason(monkeypatch, _key):
    def boom(*a, **kw):
        raise TimeoutError("Read timed out")
    monkeypatch.setattr(reranker, "_call_rerank", boom)
    out = rerank_chunks("q", _chunks())
    assert all(c[RERANK_DEGRADED_KEY].startswith("call_failed:") for c in out)
    assert "TimeoutError" in out[0][RERANK_DEGRADED_KEY], "原因要能区分超时/连接/解析"


def test_empty_result_is_marked(monkeypatch, _key):
    monkeypatch.setattr(reranker, "_call_rerank", lambda *a, **kw: [])
    out = rerank_chunks("q", _chunks())
    assert all(c[RERANK_DEGRADED_KEY] == "empty_result" for c in out)


def test_missing_api_key_is_marked(monkeypatch):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config().embedding, "api_key", "", raising=False)
    out = rerank_chunks("q", _chunks())
    assert all(c[RERANK_DEGRADED_KEY] == "no_api_key" for c in out)


def test_pool_too_small_is_not_a_degradation():
    """池 <2 无可排序 —— 打成"降级"会让正常轮次天天报警，噪声淹掉真信号。"""
    out = rerank_chunks("q", _chunks(1))
    assert RERANK_DEGRADED_KEY not in out[0]


def test_success_path_leaves_no_mark(monkeypatch, _key):
    monkeypatch.setattr(reranker, "_call_rerank",
                        lambda *a, **kw: [{"index": 2, "relevance_score": 0.95},
                                          {"index": 0, "relevance_score": 0.7}])
    out = rerank_chunks("q", _chunks())
    assert [c["chunk_id"] for c in out] == ["c2", "c0"], "成功时按重排分重排"
    assert all(RERANK_DEGRADED_KEY not in c for c in out)
    assert out[0]["rerank_score"] == 0.95
    assert out[0]["_fused_score"] == 7.0, "原融合分要保留在 _fused_score（c2 是 9.0-2）"


def test_degradation_does_not_change_order_or_truncation(monkeypatch, _key):
    """留痕是**只读观测**：顺序、截断、分数一律不动。"""
    monkeypatch.setattr(reranker, "_call_rerank",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    src = _chunks(5)
    before = [(c["chunk_id"], c["score"]) for c in src]
    out = rerank_chunks("q", src, top_k=3)
    assert [(c["chunk_id"], c["score"]) for c in out] == before[:3]
    assert all("rerank_score" not in c for c in out), "降级时不得伪造 rerank 分"


def test_degraded_chunk_still_uses_fusion_thresholds_for_level(monkeypatch, _key):
    """**这条是护栏**：降级后档位标签必须仍按融合量纲判。

    `score_level` 按 chunk 有没有 `rerank_score` 键选阈值 —— 降级后没有该键 ⇒ 落回融合阈。
    若哪天有人"顺手"在降级路径补一个 rerank_score，融合分(~0-10)会撞上 rerank 阈(0.9)，
    所有降级题一律标成"高"，把弱命中当高置信推给员工。
    """
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.llm_generator import score_level

    # 用**哨兵阈值**把两套量纲拉开，判据才无歧义：环境里 RAG_HA3_CLIENT_FUSION=true 会把融合阈
    # 标到 0.57/0.52，和 rerank 的 0.9/0.8 挨得太近，随手挑的分数两边同解、测不出东西。
    rag = get_config().rag
    monkeypatch.setattr(rag, "score_threshold_high", 100.0, raising=False)
    monkeypatch.setattr(rag, "score_threshold_medium", 50.0, raising=False)
    monkeypatch.setattr(rag, "rerank_score_threshold_high", 0.9, raising=False)
    monkeypatch.setattr(rag, "rerank_score_threshold_medium", 0.8, raising=False)
    monkeypatch.setattr(reranker, "_call_rerank",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    out = rerank_chunks("q", [{"chunk_id": "a", "chunk_text": "t", "score": 60.0},
                              {"chunk_id": "b", "chunk_text": "t", "score": 55.0}])
    assert out[0][RERANK_DEGRADED_KEY]
    # 60 在融合量纲下是 "mid"(50≤60<100)；若误按 rerank 量纲则 60≥0.9 → "high"
    assert score_level(out[0]) == "mid"


def test_l1_layer_surfaces_the_count():
    """L1 逐题记录 + 汇总计数：A/B 时要能把降级题剔出去，不能只有一行日志。"""
    import ast
    import inspect

    from eval_harness.layers import l1_retrieval
    src = inspect.getsource(l1_retrieval)
    tree = ast.parse(src)
    keys = {k.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    for k in ("rerank_degraded", "n_rerank_degraded", "rerank_degraded_qids"):
        assert k in keys, f"L1 输出缺 {k}"
