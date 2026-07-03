# -*- coding: utf-8 -*-
"""候选池带图探测 + 近平局带图倾斜（#F-mm10, 两 flag 默认 OFF）回归测试。

(a) RAG_RERANK_IMG_PROBE：rerank 前对池内 step_card 批量探测 RDS image_refs——
    重排在 expand 之前，step_card 无 refs/无 source_image → _img_key 恒 None，
    VL 重排结构性失明。探测后 refs 附上，expand C2 幂等不重复装载。
(b) RAG_IMAGE_TIEBREAK：rerank OFF 时 over-fetch（绑死本分支）→ 近平局
    （分差<eps）带图前移 → 显式截回 top_k。绝不跨真实分差挪位。
"""

import json
from unittest.mock import MagicMock, patch

from opensearch_pipeline import retriever
from opensearch_pipeline.retriever import (
    _image_tiebreak_reorder,
    _probe_pool_image_refs,
)


def _set_rag(monkeypatch, **kw):
    from opensearch_pipeline.config import get_config
    rag = get_config().rag
    for k, v in kw.items():
        monkeypatch.setattr(rag, k, v)


class _ProbeCursor:
    def __init__(self, refs_by_id):
        self.refs_by_id = refs_by_id
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._rows = [
            {"chunk_id": cid, "image_refs_json": refs}
            for cid, refs in self.refs_by_id.items()
            if cid in (params or ())
        ]

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


def _conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    c.close.return_value = None
    return c


# ═══════════════ (a) _probe_pool_image_refs ═══════════════

def test_probe_attaches_refs_to_step_cards():
    refs = json.dumps([{"oss_key": "s1.png", "caption": "图"}])
    cur = _ProbeCursor({"S1": refs})
    chunks = [
        {"chunk_type": "step_card", "chunk_id": "S1", "score": 8.0, "chunk_text": "x"},
        {"chunk_type": "text_chunk", "chunk_id": "T1", "score": 7.9},
    ]
    with patch("opensearch_pipeline.db._get_db_conn", return_value=_conn(cur)):
        out = _probe_pool_image_refs(chunks)
    assert out[0]["image_refs"][0]["oss_key"] == "s1.png"
    assert "image_refs" not in out[1]                      # 非 step_card 不动
    assert len(cur.executed) == 1                          # 单次批量 IN 查询
    assert "is_active = 1" in cur.executed[0][0]


def test_probe_skips_chunks_with_existing_refs_and_is_failopen():
    chunks = [{"chunk_type": "step_card", "chunk_id": "S1", "score": 8.0,
               "image_refs": [{"oss_key": "already.png"}]}]
    # 已带 refs → 无 id 可查 → 不建连接（传 None 连接工厂也不会被调用）
    with patch("opensearch_pipeline.db._get_db_conn", side_effect=AssertionError("不应连接")):
        out = _probe_pool_image_refs(chunks)
    assert out[0]["image_refs"][0]["oss_key"] == "already.png"
    # fail-open：连接失败原样返回
    chunks2 = [{"chunk_type": "step_card", "chunk_id": "S2", "score": 8.0}]
    with patch("opensearch_pipeline.db._get_db_conn", side_effect=RuntimeError("db down")):
        out2 = _probe_pool_image_refs(chunks2)
    assert out2 == chunks2


def test_probe_drops_refs_without_oss_key():
    refs = json.dumps([{"caption": "无图源"}])
    cur = _ProbeCursor({"S1": refs})
    chunks = [{"chunk_type": "step_card", "chunk_id": "S1", "score": 8.0}]
    with patch("opensearch_pipeline.db._get_db_conn", return_value=_conn(cur)):
        out = _probe_pool_image_refs(chunks)
    assert "image_refs" not in out[0]


# ═══════════════ (b) _image_tiebreak_reorder ═══════════════

def _c(cid, score, img=False):
    d = {"chunk_type": "step_card", "chunk_id": cid, "score": score}
    if img:
        d["image_refs"] = [{"oss_key": f"{cid}.png"}]
    return d


def test_tiebreak_swaps_near_tie_only():
    chunks = [_c("A", 8.00), _c("B", 7.98, img=True), _c("C", 7.00)]
    out = _image_tiebreak_reorder(chunks, eps=0.05)
    assert [c["chunk_id"] for c in out] == ["B", "A", "C"]   # 近平局带图前移


def test_tiebreak_never_crosses_real_gap():
    chunks = [_c("A", 8.00), _c("B", 7.50, img=True)]        # 分差 0.5 > eps
    out = _image_tiebreak_reorder(chunks, eps=0.05)
    assert [c["chunk_id"] for c in out] == ["A", "B"]


def test_tiebreak_bubbles_through_consecutive_ties():
    chunks = [_c("A", 8.00), _c("B", 7.99), _c("C", 7.98, img=True)]
    out = _image_tiebreak_reorder(chunks, eps=0.05)
    assert [c["chunk_id"] for c in out] == ["C", "A", "B"]   # 连续平局逐级上浮


def test_tiebreak_image_first_untouched():
    chunks = [_c("A", 8.00, img=True), _c("B", 7.99)]
    out = _image_tiebreak_reorder(chunks, eps=0.05)
    assert [c["chunk_id"] for c in out] == ["A", "B"]


# ═══════════════ retrieve_and_enrich 接线 ═══════════════

def _wire(monkeypatch, *, rerank, probe, tiebreak, pool_chunks):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config().alibaba_vector, "rerank_enable", rerank)
    _set_rag(monkeypatch, rerank_img_probe=probe, image_tiebreak=tiebreak,
             image_tiebreak_eps=0.05, image_tiebreak_pool=14,
             multi_query_mode="off", doc_diversity_cap=0)
    calls = {}

    def _fake_search(query, top_k=7, user_dept=None, query_embedding=None):
        calls["fetch_k"] = top_k
        return [dict(c) for c in pool_chunks][:top_k]

    monkeypatch.setattr(retriever, "search_chunks", _fake_search)
    monkeypatch.setattr(retriever, "get_query_embedding", lambda q: None)
    monkeypatch.setattr(retriever, "stitch_neighbor_chunks", lambda chs, window=1: chs)
    monkeypatch.setattr(retriever, "expand_step_context", lambda chs, q: chs)
    return calls


def test_wiring_tiebreak_overfetch_and_truncate(monkeypatch):
    """tiebreak ON + rerank OFF：over-fetch 到 pool、倾斜后显式截回 top_k。"""
    pool = [_c(f"T{i}", 8.0 - i * 0.3) for i in range(12)]
    # 与窗口末位 T6(6.2) 近平局（gap 0.02 < eps 0.05）、排在第 8 位 → 倾斜应把它换进 top 7
    pool.append(_c("IMG", 6.18, img=True))
    pool.sort(key=lambda c: -c["score"])
    calls = _wire(monkeypatch, rerank=False, probe=False, tiebreak=True, pool_chunks=pool)
    monkeypatch.setattr(retriever, "_probe_pool_image_refs", lambda chs: chs)
    out = retriever.retrieve_and_enrich("怎么操作", top_k=7)
    assert calls["fetch_k"] == 14                            # over-fetch 绑死在分支内
    assert len(out) == 7                                     # 显式截回
    assert any(c["chunk_id"] == "IMG" for c in out), "近平局带图卡应升入 top_k"


def test_wiring_rrf_disables_tiebreak(monkeypatch):
    """#F-mm10c rrf 融合下 image_tiebreak 必须整体关闭（不 over-fetch、不 reorder）：
    eps 按 weighted 融合分标定，rrf 分尺度(~0.0x)下会把所有带图 chunk 无差别顶到最前。"""
    from opensearch_pipeline.config import get_config
    # weighted 尺度池：末位 T6 与带图 IMG 近平局（gap<eps），weighted 下本会被换进 top_k
    pool = [_c(f"T{i}", 8.0 - i * 0.3) for i in range(12)]
    pool.append(_c("IMG", 6.18, img=True))
    pool.sort(key=lambda c: -c["score"])
    calls = _wire(monkeypatch, rerank=False, probe=False, tiebreak=True, pool_chunks=pool)
    monkeypatch.setattr(retriever, "_probe_pool_image_refs", lambda chs: chs)
    monkeypatch.setattr(get_config().alibaba_vector, "hybrid_fusion", "rrf")
    out = retriever.retrieve_and_enrich("怎么操作", top_k=7)
    assert calls["fetch_k"] == 7                             # 未 over-fetch → tiebreak 分支未进
    assert [c["chunk_id"] for c in out] == [f"T{i}" for i in range(7)]  # 顺序原样、IMG 未被顶前
    assert not any(c["chunk_id"] == "IMG" for c in out)


def test_wiring_flags_off_byte_identical(monkeypatch):
    """两 flag 全 OFF + rerank OFF：fetch_k==top_k，顺序原样（历史行为）。"""
    pool = [_c(f"T{i}", 8.0 - i * 0.3) for i in range(10)]
    calls = _wire(monkeypatch, rerank=False, probe=False, tiebreak=False, pool_chunks=pool)
    out = retriever.retrieve_and_enrich("怎么操作", top_k=7)
    assert calls["fetch_k"] == 7
    assert [c["chunk_id"] for c in out] == [f"T{i}" for i in range(7)]


def test_wiring_probe_called_before_rerank(monkeypatch):
    """probe ON + rerank ON：探测在 rerank_chunks 之前发生。"""
    pool = [_c("S1", 8.0), _c("T1", 7.9)]
    order = []
    _wire(monkeypatch, rerank=True, probe=True, tiebreak=False, pool_chunks=pool)
    monkeypatch.setattr(retriever, "_probe_pool_image_refs",
                        lambda chs: (order.append("probe"), chs)[1])
    import opensearch_pipeline.reranker as rr
    monkeypatch.setattr(rr, "rerank_chunks",
                        lambda q, chs, top_k=None, multimodal=False:
                        (order.append("rerank"), chs[:top_k] if top_k else chs)[1])
    retriever.retrieve_and_enrich("怎么操作", top_k=7)
    assert order == ["probe", "rerank"]
