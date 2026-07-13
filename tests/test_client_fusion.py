# -*- coding: utf-8 -*-
"""
test_client_fusion.py — 三路客户端融合（RAG_HA3_CLIENT_FUSION / w3_s10）单元测试

无需阿里云实例。复用 test_rrf_hybrid_search 的 HA3 SDK mock 注入（同一 mock 语义，
断言全部走 client.call_args，不依赖注入方的捕获列表）。验证：
1. flag 关（默认）→ 只走服务端混合 client.search()，不发 /query
2. flag 开 → 两次 client.query()（D 纯 dense + S 带 sparse）+ 一次 client.search()（B 臂 knn 权重 0）
3. 融合语义：min-max 归一加权、缺席不罚分——盲行（无 sparse 臂命中）靠 D/B 两臂仍居首
4. 对外 score = knn_weight*dense_IP + text_weight*BM25_raw（服务端可比分，保 7.7/5.8 档位语义）
5. S/B 辅臂失败 → 按空臂降级，仍出融合结果、不回落
6. D 主臂失败 → 返回 None 回落服务端混合（knn.weight=0.7 的完整混合请求）
7. 无 sparse（空 sparse_idx）→ 不发 S 臂
"""

import pytest
from unittest.mock import MagicMock, patch

from tests.test_rrf_hybrid_search import (  # noqa: F401 — 导入即完成 SDK mock 注入
    _ensure_ha3_mock_modules,
)

_ensure_ha3_mock_modules()

from opensearch_pipeline.config import (  # noqa: E402
    PipelineConfig, AlibabaVectorSearchConfig, EmbeddingConfig,
)


def _make_config(*, client_fusion_enable=True, **overrides) -> PipelineConfig:
    av = dict(
        endpoint="test.ha3.aliyuncs.com",
        instance_id="test-instance",
        access_user_name="user",
        access_pass_word="pass",
        table_name="test_table",
        enable_hybrid=True,
        hybrid_fusion="weighted",
        knn_weight=0.7,
        text_weight=0.3,
        text_search_field="chunk_text",
        hybrid_knn_top_k=100,
        client_fusion_enable=client_fusion_enable,
        client_fusion_dense_weight=0.7,
        client_fusion_sparse_weight=0.1,
        client_fusion_text_weight=0.3,
        client_fusion_pool=50,
    )
    av.update(overrides)
    return PipelineConfig(
        alibaba_vector=AlibabaVectorSearchConfig(**av),
        embedding=EmbeddingConfig(api_key="test-key", api_base_url="https://test.example.com"),
    )


def _resp(rows):
    """rows: [(pk, score)] → HA3 response mock。"""
    resp = MagicMock()
    resp.body = {
        "result": [
            {
                "fields": {
                    "id": pk,
                    # 带 section_title 且 >200 字，避免被封面降权重排干扰名次断言
                    "chunk_text_store": f"文本{pk}" * 80,
                    "title": f"文档{pk}",
                    "section_title": "第一节",
                    "doc_id": f"DOC_{pk}",
                    "kb_type": "public",
                    "permission_level": "public",
                },
                "score": score,
            }
            for pk, score in rows
        ],
    }
    return resp


# 臂级 canned 数据：A=盲行（无 sparse 臂命中）
DENSE_ROWS = [("A", 1.0), ("B", 0.9), ("C", 0.5)]
SPARSE_ROWS = [("B", 60.0), ("C", 55.0)]
BM25_ROWS = [("A", 20.0), ("B", 5.0)]


def _fusion_client():
    """query() 依 sparse_data 有无分流 D/S 臂；search() 返回 B 臂。"""
    client = MagicMock()

    def _query(req):
        if getattr(req, "sparse_data", None) is not None:
            return _resp(SPARSE_ROWS)
        return _resp(DENSE_ROWS)

    client.query.side_effect = _query
    client.search.return_value = _resp(BM25_ROWS)
    return client


def _run_search(client, config, sparse=([1, 5], [0.5, 0.3]), **kwargs):
    from opensearch_pipeline import retriever

    with patch.object(retriever, "get_config", return_value=config), \
         patch.object(retriever, "_get_ha3_client", return_value=client), \
         patch.object(retriever, "get_query_embedding",
                      return_value=([0.1] * 4, sparse[0], sparse[1])), \
         patch.object(retriever, "_revalidate_main_hits", side_effect=lambda r: r), \
         patch.object(retriever, "_deny_revoked_cross_dept", side_effect=lambda r, d: r):
        return retriever.search_chunks("测试查询", top_k=5, **kwargs)


# ═══════════════════════════════════════════════════════════════
# 1. flag 门控
# ═══════════════════════════════════════════════════════════════

class TestFlagGating:
    def test_flag_off_uses_server_hybrid_only(self):
        client = _fusion_client()
        _run_search(client, _make_config(client_fusion_enable=False))
        client.search.assert_called_once()
        client.query.assert_not_called()

    def test_flag_on_issues_three_arms(self):
        client = _fusion_client()
        _run_search(client, _make_config())
        assert client.query.call_count == 2   # D + S
        client.search.assert_called_once()    # B
        # D/S 两臂请求形态：D 无 sparse_data、S 带；均 order=DESC + 每臂候选池
        q_reqs = [c.args[0] for c in client.query.call_args_list]
        sparse_flags = sorted(getattr(r, "sparse_data", None) is not None for r in q_reqs)
        assert sparse_flags == [False, True]
        assert all(r.order == "DESC" and r.top_k == 50 for r in q_reqs)
        # B 臂：knn 权重清零、text 权重 1.0（纯 BM25 形态），两路 filter 同权限边界
        b_req = client.search.call_args.args[0]
        assert b_req.knn.weight == 0.0
        assert b_req.text.weight == 1.0
        assert b_req.knn.filter == b_req.text.filter

    def test_no_sparse_skips_s_arm(self):
        client = _fusion_client()
        _run_search(client, _make_config(), sparse=([], []))
        assert client.query.call_count == 1   # 仅 D 臂


# ═══════════════════════════════════════════════════════════════
# 2. 融合语义
# ═══════════════════════════════════════════════════════════════

class TestFusionSemantics:
    def test_blind_row_wins_despite_sparse_absence(self):
        """A（盲行）不在 S 臂：min-max 归一后 A=0.7*1+0.3*1=1.0 仍居首——缺席不罚分。"""
        results = _run_search(_fusion_client(), _make_config())
        assert [r["id"] for r in results[:3]] == ["A", "B", "C"]

    def test_server_comparable_score_exposed(self):
        """对外 score = knn_weight*dense + text_weight*bm25（服务端可比分，保档位语义）。"""
        results = _run_search(_fusion_client(), _make_config())
        by_id = {r["id"]: r for r in results}
        # A: 0.7*1.0 + 0.3*20.0 = 6.7；B: 0.7*0.9 + 0.3*5.0 = 2.13；C 无 BM25: 0.7*0.5
        assert by_id["A"]["score"] == pytest.approx(6.7)
        assert by_id["B"]["score"] == pytest.approx(2.13)
        assert by_id["C"]["score"] == pytest.approx(0.35)
        # 融合名次分保留供诊断
        assert by_id["A"]["_fused_score"] == pytest.approx(1.0)

    def test_w3_weights_respected(self):
        """加大 sparse 权重后，有 sparse 的 B 应反超盲行 A（验证权重生效）。"""
        results = _run_search(
            _fusion_client(),
            _make_config(client_fusion_dense_weight=0.1,
                         client_fusion_sparse_weight=1.0,
                         client_fusion_text_weight=0.0),
        )
        assert results[0]["id"] == "B"   # sparse 臂 B=norm1.0*1.0 主导


# ═══════════════════════════════════════════════════════════════
# 3. 降级语义
# ═══════════════════════════════════════════════════════════════

class TestDegradation:
    def test_sparse_arm_failure_degrades_to_two_arms(self):
        client = MagicMock()

        def _query(req):
            if getattr(req, "sparse_data", None) is not None:
                raise RuntimeError("S arm down")
            return _resp(DENSE_ROWS)

        client.query.side_effect = _query
        client.search.return_value = _resp(BM25_ROWS)
        results = _run_search(client, _make_config())
        assert results and results[0]["id"] == "A"   # D+B 融合仍出结果
        client.search.assert_called_once()           # 未回落服务端混合（只有 B 臂那一次）

    def test_bm25_arm_failure_degrades_to_two_arms(self):
        client = _fusion_client()
        client.search.side_effect = RuntimeError("B arm down")
        results = _run_search(client, _make_config())
        # D+S 融合：A 靠纯 dense 1.0*0.7 居首；B 次之（dense 0.8*0.7 + sparse 1.0*0.1）
        assert [r["id"] for r in results[:2]] == ["A", "B"]

    def test_dense_arm_failure_falls_back_to_server_hybrid(self):
        client = MagicMock()

        def _query(req):
            if getattr(req, "sparse_data", None) is None:
                raise RuntimeError("D arm down")
            return _resp(SPARSE_ROWS)

        client.query.side_effect = _query
        client.search.return_value = _resp(BM25_ROWS)
        results = _run_search(client, _make_config())
        # 回落服务端混合：search() 两次（B 臂一次 + 完整混合一次），最后一次 knn 权重 0.7
        assert client.search.call_count == 2
        fallback_req = client.search.call_args_list[-1].args[0]
        assert fallback_req.knn.weight == pytest.approx(0.7)
        assert fallback_req.text.weight == pytest.approx(0.3)
        # 结果来自服务端混合响应（BM25_ROWS mock），A 为首
        assert results and results[0]["id"] == "A"


# ═══════════════════════════════════════════════════════════════
# 4. 档位阈值自动标定（config guard）
# ═══════════════════════════════════════════════════════════════

class TestThresholdAutoCalibration:
    """融合分数域 ~[0.02,0.64]，7.7/5.8 旧阈值会把全部命中标「低」——
    融合开启时 load_config 自动套 2026-07-13 标定值，显式 env 永远优先。"""

    def test_default_load_is_fusion_on_with_calibrated_thresholds(self):
        """生产语义随包生效：默认（不设任何融合 env）即融合开 + 标定阈值。"""
        from tests.test_config_loading import _fresh_load
        config = _fresh_load(RAG_SIMULATE="true")
        assert config.alibaba_vector.client_fusion_enable is True
        assert config.rag.score_threshold_high == pytest.approx(0.57)
        assert config.rag.score_threshold_medium == pytest.approx(0.52)

    def test_explicit_env_thresholds_win_over_auto(self):
        from tests.test_config_loading import _fresh_load
        config = _fresh_load(RAG_SIMULATE="true", RAG_HA3_CLIENT_FUSION="true",
                             RAG_SCORE_THRESHOLD_HIGH="9.9")
        assert config.rag.score_threshold_high == pytest.approx(9.9)
        assert config.rag.score_threshold_medium == pytest.approx(0.52)

    def test_kill_switch_restores_legacy_thresholds(self):
        """RAG_HA3_CLIENT_FUSION=false 逃生舱：回落服务端混合 + 旧阈值语义。"""
        from tests.test_config_loading import _fresh_load
        config = _fresh_load(RAG_SIMULATE="true", RAG_HA3_CLIENT_FUSION="false")
        assert config.alibaba_vector.client_fusion_enable is False
        assert config.rag.score_threshold_high == pytest.approx(7.7)
        assert config.rag.score_threshold_medium == pytest.approx(5.8)
