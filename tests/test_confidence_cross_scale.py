# -*- coding: utf-8 -*-
"""#7：低置信护栏的跨量纲归一（RAG_CONFIDENCE_CROSS_SCALE，默认 OFF）。

rerank + cosurface 同开时，is_low_confidence_band 旧逻辑只看 rerank 分(0-1)、忽略只带融合分的
cosurface 补图 → 重排文本弱而某图强时过度拒答。开启 flag 后把两种量纲各按自阈值归一到统一 [0,1]
置信度再取 max（medium 线=0.5）。本文件确定性地验证归一逻辑与两路行为差异（不依赖 live 服务）。
"""
import types

import opensearch_pipeline.llm_generator as G
from opensearch_pipeline.config import RAGConfig


def _cfg(cross_scale: bool):
    # 阈值取 RAGConfig 默认（rerank 0.9/0.8，融合 7.7/5.8）
    return types.SimpleNamespace(rag=RAGConfig(confidence_cross_scale=cross_scale))


def _reranked(rscore):
    """重排后的文本块：带 rerank_score（0-1 量纲），同时保留融合 score。"""
    return {"chunk_type": "step_card", "rerank_score": rscore, "score": 5.0, "chunk_text": "x"}


def _cosurface_img(fused):
    """cosurface 补图块：只有融合 score（无 rerank_score，rerank 之后才注入）。"""
    return {"chunk_type": "image", "score": fused, "source_image": "a.png"}


def test_confidence_norm_anchors(monkeypatch):
    monkeypatch.setattr(G, "get_config", lambda: _cfg(True))
    # rerank 量纲：med=0.8→0.5，high=0.9→1.0
    assert abs(G._confidence_norm({"rerank_score": 0.8}) - 0.5) < 1e-9
    assert abs(G._confidence_norm({"rerank_score": 0.9}) - 1.0) < 1e-9
    # 融合量纲：med=5.8→0.5，high=7.7→1.0
    assert abs(G._confidence_norm({"score": 5.8}) - 0.5) < 1e-9
    assert abs(G._confidence_norm({"score": 7.7}) - 1.0) < 1e-9
    # 强融合分外推 >1.0；无分 → None（调用方跳过）
    assert G._confidence_norm({"score": 9.0}) > 1.0
    assert G._confidence_norm({"chunk_text": "x"}) is None


def test_mixed_scale_strong_cosurface_image_refused_only_under_old_logic(monkeypatch):
    """#7 核心：重排文本弱(0.4/0.5) + cosurface 图强(融合 8.1)。

    旧逻辑只看 rerank 分 → max0.5<0.8 → 判低置信（过度拒答）；cross-scale 开 → 图归一>1.0 →
    max≥0.5 → 不判低。"""
    chunks = [_reranked(0.4), _reranked(0.5), _cosurface_img(8.1)]
    monkeypatch.setattr(G, "get_config", lambda: _cfg(False))
    assert G.is_low_confidence_band(chunks) is True     # 旧行为：过度拒答
    monkeypatch.setattr(G, "get_config", lambda: _cfg(True))
    assert G.is_low_confidence_band(chunks) is False    # 新行为：强图救回


def test_cross_scale_still_refuses_when_all_weak(monkeypatch):
    """不是无脑放宽：重排文本弱 + 图也弱(融合 3.0<5.8) → 仍判低置信（护栏不被架空）。"""
    monkeypatch.setattr(G, "get_config", lambda: _cfg(True))
    assert G.is_low_confidence_band([_reranked(0.4), _cosurface_img(3.0)]) is True


def test_cross_scale_pure_rerank_agrees_with_old(monkeypatch):
    """纯 rerank（无融合-only 块）时新旧一致：0.85≥0.8 medium → 均非低；0.6<0.8 → 均低。"""
    for rscore, expect_low in [(0.85, False), (0.6, True)]:
        monkeypatch.setattr(G, "get_config", lambda: _cfg(False))
        old = G.is_low_confidence_band([_reranked(rscore)])
        monkeypatch.setattr(G, "get_config", lambda: _cfg(True))
        new = G.is_low_confidence_band([_reranked(rscore)])
        assert old is expect_low and new is expect_low, f"rscore={rscore}"


def test_empty_and_scoreless_not_low(monkeypatch):
    monkeypatch.setattr(G, "get_config", lambda: _cfg(True))
    assert G.is_low_confidence_band([]) is False
    assert G.is_low_confidence_band([{"chunk_text": "x"}]) is False  # 无任何分 → 非低
