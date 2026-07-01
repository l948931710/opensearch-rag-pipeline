# -*- coding: utf-8 -*-
"""
test_rrf_hybrid_search.py — RRF 与混合检索请求构造的单元测试

无需阿里云实例。通过 Mock SDK + 拦截验证：
1. RRF 模式下 RankQuery 构造正确 (rankConstant 传递)
2. weighted 模式下 kNN/text 权重设置正确
3. enable_hybrid=True 走 client.search()，False 走 client.query()
4. hybrid_fusion 配置切换不互相污染
5. rrf_rank_constant 自定义值传播
6. BM25 text query 构造正确
7. sparse_data 传递到 kNN query
"""

import sys
import types
import pytest
from unittest.mock import MagicMock, patch


# ═══════════════════════════════════════════════════════════════
# 预注入 HA3 SDK 的 mock module
# ═══════════════════════════════════════════════════════════════

# 记录实际构造的 SDK 对象，用于断言
_captured_search_requests = []
_captured_rank_queries = []
_captured_text_queries = []
_captured_knn_queries = []


def _ensure_ha3_mock_modules():
    """在 sys.modules 中注入 HA3 SDK 的 mock 模块，包含检索相关的类。"""
    if "alibabacloud_ha3engine_vector" in sys.modules:
        ha3_models = sys.modules["alibabacloud_ha3engine_vector.models"]
        # 只在缺少 SearchRequest 时补充
        if hasattr(ha3_models, "SearchRequest"):
            return

    ha3_pkg = sys.modules.get(
        "alibabacloud_ha3engine_vector",
        types.ModuleType("alibabacloud_ha3engine_vector"),
    )
    ha3_models = sys.modules.get(
        "alibabacloud_ha3engine_vector.models",
        types.ModuleType("alibabacloud_ha3engine_vector.models"),
    )
    ha3_client = sys.modules.get(
        "alibabacloud_ha3engine_vector.client",
        types.ModuleType("alibabacloud_ha3engine_vector.client"),
    )

    # ── QueryRequest (kNN 路) ──
    class MockQueryRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            _captured_knn_queries.append(self)

    # ── SparseData ──
    class MockSparseData:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    # ── TextQuery (BM25 路) ──
    class MockTextQuery:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            _captured_text_queries.append(self)

    # ── RankQuery (融合策略) ──
    class MockRankQuery:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            _captured_rank_queries.append(self)

    # ── SearchRequest (混合检索总请求) ──
    class MockSearchRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            _captured_search_requests.append(self)

    class MockPushDocumentsRequest:
        def __init__(self, body=None):
            self.body = body
        def set_body(self, body):
            self.body = body

    ha3_models.QueryRequest = MockQueryRequest
    ha3_models.SparseData = MockSparseData
    ha3_models.TextQuery = MockTextQuery
    ha3_models.RankQuery = MockRankQuery
    ha3_models.SearchRequest = MockSearchRequest
    ha3_models.PushDocumentsRequest = MockPushDocumentsRequest
    ha3_models.Config = MagicMock

    ha3_client.Client = MagicMock

    sys.modules["alibabacloud_ha3engine_vector"] = ha3_pkg
    sys.modules["alibabacloud_ha3engine_vector.models"] = ha3_models
    sys.modules["alibabacloud_ha3engine_vector.client"] = ha3_client


_ensure_ha3_mock_modules()

from opensearch_pipeline.config import (
    PipelineConfig, AlibabaVectorSearchConfig, EmbeddingConfig,
)


# ═══════════════════════════════════════════════════════════════
# 辅助：构造 config 和 mock 响应
# ═══════════════════════════════════════════════════════════════

def _make_config(
    *,
    enable_hybrid=True,
    hybrid_fusion="rrf",
    rrf_rank_constant=60,
    knn_weight=0.7,
    text_weight=0.3,
) -> PipelineConfig:
    """构造一个最小化的 PipelineConfig，用于测试 search_chunks。"""
    return PipelineConfig(
        alibaba_vector=AlibabaVectorSearchConfig(
            endpoint="test.ha3.aliyuncs.com",
            instance_id="test-instance",
            access_user_name="user",
            access_pass_word="pass",
            table_name="test_table",
            enable_hybrid=enable_hybrid,
            hybrid_fusion=hybrid_fusion,
            rrf_rank_constant=rrf_rank_constant,
            knn_weight=knn_weight,
            text_weight=text_weight,
            text_search_field="chunk_text",
            hybrid_knn_top_k=100,
        ),
        embedding=EmbeddingConfig(
            api_key="test-key",
            api_base_url="https://test.example.com",
        ),
    )


def _make_ha3_response(results=None):
    """构造一个 HA3 查询返回的 mock response。"""
    resp = MagicMock()
    resp.body = {
        "result": results or [
            {
                "fields": {
                    "chunk_text_store": "测试返回文本",
                    "title": "测试文档",
                    "section_title": "第一节",
                    "doc_id": "doc1",
                    "category_l1": "manual",
                    "chunk_index": 0,
                    "page_num": 1,
                    "kb_type": "public",
                    "permission_level": "public",
                    "owner_dept": "",
                },
                "score": 0.032,
            }
        ],
    }
    return resp


# ═══════════════════════════════════════════════════════════════
# Fixture: 每个测试前清空捕获列表
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clear_captured():
    _captured_search_requests.clear()
    _captured_rank_queries.clear()
    _captured_text_queries.clear()
    _captured_knn_queries.clear()
    yield


# ═══════════════════════════════════════════════════════════════
# Section A: RRF 模式请求构造
# ═══════════════════════════════════════════════════════════════

class TestRRFFusionConstruction:
    """验证 hybrid_fusion='rrf' 时 SearchRequest 的构造正确性。"""

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_rrf_rank_query_has_rank_constant(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """RRF 模式下 RankQuery 必须包含 rrf.rankConstant=60。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf", rrf_rank_constant=60)
        mock_embedding.return_value = ([0.1] * 1024, [1, 5, 10], [0.5, 0.3, 0.2])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("测试查询")

        # 验证 RankQuery 构造
        assert len(_captured_rank_queries) == 1, "应该恰好构造 1 个 RankQuery"
        rq = _captured_rank_queries[0]
        assert hasattr(rq, "rrf"), "RRF 模式下 RankQuery 必须包含 rrf 属性"
        assert rq.rrf == {"rankConstant": 60}, (
            f"rrf.rankConstant 应为 60，实际为 {rq.rrf}"
        )

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_rrf_custom_rank_constant_propagates(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """自定义 rrf_rank_constant=30 应传播到 RankQuery。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf", rrf_rank_constant=30)
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("自定义 rank constant 测试")

        rq = _captured_rank_queries[0]
        assert rq.rrf == {"rankConstant": 30}, (
            f"自定义 rankConstant 应为 30，实际为 {rq.rrf}"
        )

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_rrf_mode_calls_client_search(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """RRF 混合模式必须使用 client.search() 而非 client.query()。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1, 2], [0.5, 0.3])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("检索路径测试")

        client.search.assert_called_once()
        client.query.assert_not_called()

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_rrf_mode_does_not_set_knn_weight(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """RRF 模式下不应设置 kNN/text 权重（权重属于 weighted 模式）。"""
        mock_config.return_value = _make_config(
            hybrid_fusion="rrf", knn_weight=0.7, text_weight=0.3
        )
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("权重隔离测试")

        # kNN query 不应有 weight 属性
        knn = _captured_knn_queries[0]
        assert not hasattr(knn, "weight"), (
            "RRF 模式下 kNN query 不应设置 weight 属性"
        )

        # text query 不应有 weight 属性
        tq = _captured_text_queries[0]
        assert not hasattr(tq, "weight"), (
            "RRF 模式下 text query 不应设置 weight 属性"
        )


# ═══════════════════════════════════════════════════════════════
# Section B: Weighted 模式请求构造
# ═══════════════════════════════════════════════════════════════

class TestWeightedFusionConstruction:
    """验证 hybrid_fusion='weighted' 时权重设置和 RankQuery 差异。"""

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_weighted_sets_knn_and_text_weights(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """weighted 模式下 kNN.weight 和 text.weight 应被正确设置。"""
        mock_config.return_value = _make_config(
            hybrid_fusion="weighted", knn_weight=0.6, text_weight=0.4
        )
        mock_embedding.return_value = ([0.1] * 1024, [1, 5], [0.5, 0.3])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("加权模式测试")

        knn = _captured_knn_queries[0]
        assert hasattr(knn, "weight"), "weighted 模式 kNN query 必须有 weight"
        assert knn.weight == 0.6, f"kNN weight 应为 0.6，实际 {knn.weight}"

        tq = _captured_text_queries[0]
        assert hasattr(tq, "weight"), "weighted 模式 text query 必须有 weight"
        assert tq.weight == 0.4, f"text weight 应为 0.4，实际 {tq.weight}"

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_weighted_rank_query_has_no_rrf(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """weighted 模式下 RankQuery 不应包含 rrf 字段。"""
        mock_config.return_value = _make_config(hybrid_fusion="weighted")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("无 RRF 测试")

        rq = _captured_rank_queries[0]
        assert not hasattr(rq, "rrf"), (
            f"weighted 模式 RankQuery 不应有 rrf 属性，实际: {rq.__dict__}"
        )


# ═══════════════════════════════════════════════════════════════
# Section C: Hybrid vs 纯向量路径选择
# ═══════════════════════════════════════════════════════════════

class TestHybridPathRouting:
    """验证 enable_hybrid 控制的检索路径分发。"""

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_hybrid_disabled_uses_query_not_search(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """enable_hybrid=False 应调用 client.query()（纯向量），不走 client.search()。"""
        mock_config.return_value = _make_config(enable_hybrid=False)
        mock_embedding.return_value = ([0.1] * 1024, [1, 2], [0.5, 0.3])

        client = MagicMock()
        client.query.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("纯向量测试")

        client.query.assert_called_once()
        client.search.assert_not_called()

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_hybrid_disabled_no_text_query_constructed(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """enable_hybrid=False 时不应构造 TextQuery 或 SearchRequest。"""
        mock_config.return_value = _make_config(enable_hybrid=False)
        mock_embedding.return_value = ([0.1] * 1024, [], [])

        client = MagicMock()
        client.query.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("无混合测试")

        assert len(_captured_text_queries) == 0, "纯向量模式不应构造 TextQuery"
        assert len(_captured_search_requests) == 0, "纯向量模式不应构造 SearchRequest"
        assert len(_captured_rank_queries) == 0, "纯向量模式不应构造 RankQuery"


# ═══════════════════════════════════════════════════════════════
# Section D: BM25 TextQuery 构造验证
# ═══════════════════════════════════════════════════════════════

class TestBM25TextQueryConstruction:
    """验证混合检索中 BM25 text 查询的构造。"""

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_text_query_uses_configured_field(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """TextQuery 的 query_string 应使用配置的 text_search_field。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("字段名测试")

        tq = _captured_text_queries[0]
        assert "chunk_text:" in tq.query_string, (
            f"TextQuery 应使用 chunk_text 字段，实际: {tq.query_string}"
        )

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_text_query_escapes_single_quotes(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """用户输入中的单引号应被 HA3 转义，不破坏查询语法。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("it's a test 'query'")

        tq = _captured_text_queries[0]
        # 单引号应被移除或转义，不应裸露出现在 query_string 中
        # _escape_ha3_query 将 ' 替换为空格
        inner = tq.query_string.split(":", 1)[1]  # 取 chunk_text: 之后的部分
        assert "'" not in inner.strip("'"), (
            f"查询中不应有裸露单引号，实际: {tq.query_string}"
        )


# ═══════════════════════════════════════════════════════════════
# Section E: Sparse Data 传递
# ═══════════════════════════════════════════════════════════════

class TestSparseDataPropagation:
    """验证 sparse embedding 被正确传递到 kNN query。"""

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_sparse_data_passed_to_knn_query(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """get_query_embedding 返回的 sparse 数据应出现在 kNN query 中。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        sparse_idx = [3, 7, 15]
        sparse_val = [0.8, 0.5, 0.2]
        mock_embedding.return_value = ([0.1] * 1024, sparse_idx, sparse_val)

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("sparse 传递测试")

        knn = _captured_knn_queries[0]
        assert knn.sparse_data is not None, "sparse_data 不应为 None"
        assert knn.sparse_data.indices == sparse_idx
        assert knn.sparse_data.values == sparse_val

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_empty_sparse_passes_none(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """当 sparse indices 为空时，sparse_data 应为 None。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [], [])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("空 sparse 测试")

        knn = _captured_knn_queries[0]
        assert knn.sparse_data is None, (
            "sparse indices 为空时 sparse_data 应为 None"
        )


# ═══════════════════════════════════════════════════════════════
# Section F: SearchRequest 整体结构验证
# ═══════════════════════════════════════════════════════════════

class TestSearchRequestStructure:
    """验证 SearchRequest 的完整结构。"""

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_search_request_has_all_required_fields(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """SearchRequest 应包含 table_name, knn, text, rank, size, order。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("结构完整性测试", top_k=3)

        assert len(_captured_search_requests) == 1
        sr = _captured_search_requests[0]

        assert sr.table_name == "test_table"
        assert sr.size == 3
        assert sr.order == "DESC"
        assert sr.knn is not None, "SearchRequest 必须包含 knn"
        assert sr.text is not None, "SearchRequest 必须包含 text"
        assert sr.rank is not None, "SearchRequest 必须包含 rank"

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_knn_top_k_uses_config_value(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """kNN 路的 top_k 应使用 hybrid_knn_top_k 配置值（100），而非最终 top_k。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("kNN top_k 测试", top_k=5)

        knn = _captured_knn_queries[0]
        assert knn.top_k == 100, (
            f"kNN top_k 应为 hybrid_knn_top_k=100，实际 {knn.top_k}"
        )


# ═══════════════════════════════════════════════════════════════
# Section G: HA3 Filter 注入防护
# ═══════════════════════════════════════════════════════════════

class TestHA3FilterInjectionPrevention:
    """验证 user_dept 注入攻击被 _sanitize_ha3_filter_value 阻止。"""

    def test_sanitize_strips_double_quotes(self):
        """双引号是 HA3 filter 的值边界符，必须被剥离。"""
        from opensearch_pipeline.retriever import _sanitize_ha3_filter_value
        # 典型注入: 闭合引号 + OR 绕过权限
        malicious = 'x" OR permission_level="restricted'
        result = _sanitize_ha3_filter_value(malicious)
        assert '"' not in result, f"双引号应被剥离，实际: {result}"
        assert ' ' not in result, f"空格应被剥离，实际: {result}"
        assert '=' not in result, f"等号应被剥离，实际: {result}"
        # 注入被中和: 没有引号和空格，"OR" 只是字面值的一部分，不是 HA3 操作符
        assert result == "xORpermission_levelrestricted"

    def test_sanitize_preserves_normal_dept_codes(self):
        """正常部门代码（中文、字母数字、下划线、连字符）应原样通过。"""
        from opensearch_pipeline.retriever import _sanitize_ha3_filter_value
        normal_cases = [
            ("tech_dept", "tech_dept"),
            ("研发部", "研发部"),
            ("dept-01", "dept-01"),
            ("IT_研发_2部", "IT_研发_2部"),
        ]
        for input_val, expected in normal_cases:
            result = _sanitize_ha3_filter_value(input_val)
            assert result == expected, (
                f"正常输入 '{input_val}' 应原样保留，实际: '{result}'"
            )

    def test_sanitize_strips_special_chars(self):
        """反斜杠、单引号、括号、空格等特殊字符应被剥离。"""
        from opensearch_pipeline.retriever import _sanitize_ha3_filter_value
        evil_cases = [
            ('dept\\";DROP', 'deptDROP'),
            ("dept' OR '1'='1", 'deptOR11'),
            ('dept()', 'dept'),
            ('dept AND 1=1', 'deptAND11'),
        ]
        for input_val, expected in evil_cases:
            result = _sanitize_ha3_filter_value(input_val)
            assert result == expected, (
                f"输入 '{input_val}' 净化后应为 '{expected}'，实际: '{result}'"
            )

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_injection_in_search_chunks_is_neutralized(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """端到端验证：注入 payload 经过 search_chunks 后 filter 中无裸引号。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        # 模拟注入攻击
        search_chunks("测试", user_dept='x" OR 1=1 OR owner_dept="y')

        # 验证 kNN query 的 filter 中注入被中和：注入串既非合法权限组，被白名单整体丢弃 →
        # 退化为仅 public（fail-closed），不含任何注入片段
        knn = _captured_knn_queries[0]
        filter_val = knn.filter
        assert filter_val == '(permission_level="public")', f"实际 filter: {filter_val}"
        assert "1=1" not in filter_val and '"y' not in filter_val

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_group_code_filter_expression(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """已解析的 ACL 组代码（如 marketing）必须完整进入 HA3 权限过滤表达式。

        中文部门名在 dingtalk_identity 上游已归一化为组代码；filter 只接受组代码，
        非白名单值（含中文名）被丢弃为仅 public（见 test_filter_exact_strings）。
        """
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks, _expand_groups_to_owners
        search_chunks("产品报价流程", user_dept="marketing")

        knn = _captured_knn_queries[0]
        owners = " OR ".join('owner_dept="%s"' % o for o in _expand_groups_to_owners(["marketing"]))
        assert knn.filter == (
            '(permission_level="public")'
            ' OR (permission_level="dept_internal" AND (' + owners + '))'
        ), f"实际 filter: {knn.filter}"
        # marketing 现含 production-family（共享访问策略），且自身 owner 仍在
        assert 'owner_dept="marketing"' in knn.filter
        assert 'owner_dept="production_mold"' in knn.filter

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_multi_dept_filter_expression(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """多组用户（marketing+production）→ filter OR-join 等值；'production' 伞组展开为各子线 owner。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks, _expand_groups_to_owners
        search_chunks("产品报价流程", user_dept=["marketing", "production"])

        knn = _captured_knn_queries[0]
        owners = " OR ".join('owner_dept="%s"' % o
                             for o in _expand_groups_to_owners(["marketing", "production"]))
        assert knn.filter == (
            '(permission_level="public")'
            ' OR (permission_level="dept_internal" AND (' + owners + '))'
        ), f"实际 filter: {knn.filter}"
        assert 'owner_dept="production_mold"' in knn.filter  # 伞组确实展开了子线

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_no_dept_means_public_only_filter(
        self, mock_client_fn, mock_config, mock_embedding
    ):
        """user_dept=None（匿名/无令牌）→ filter 仅放行 public。"""
        mock_config.return_value = _make_config(hybrid_fusion="rrf")
        mock_embedding.return_value = ([0.1] * 1024, [1], [0.5])

        client = MagicMock()
        client.search.return_value = _make_ha3_response()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("测试", user_dept=None)

        knn = _captured_knn_queries[0]
        assert knn.filter == '(permission_level="public")', f"实际 filter: {knn.filter}"


class TestHA3FilterInjectionAPIBoundary:
    """验证 Pydantic 模型在 API 边界拒绝非法 user_dept。"""

    def test_pydantic_rejects_injection_payload(self):
        """包含双引号的 user_dept 应被 Pydantic pattern 拒绝。"""
        from pydantic import ValidationError
        from opensearch_pipeline.api import AskRequest

        with pytest.raises(ValidationError) as exc_info:
            AskRequest(
                question="测试",
                user_dept='x" OR permission_level="restricted',
            )
        # 验证是 user_dept 字段的校验错误
        errors = exc_info.value.errors()
        dept_errors = [e for e in errors if "user_dept" in str(e.get("loc", []))]
        assert len(dept_errors) > 0, f"应有 user_dept 校验错误，实际: {errors}"

    def test_pydantic_accepts_normal_dept(self):
        """正常部门代码应通过 Pydantic 校验。"""
        from opensearch_pipeline.api import SearchRequest

        req = SearchRequest(query="测试", user_dept="IT_研发部-01")
        assert req.user_dept == "IT_研发部-01"

    def test_pydantic_accepts_none_dept(self):
        """user_dept 为 None 时应通过校验。"""
        from opensearch_pipeline.api import AskRequest

        req = AskRequest(question="测试")
        assert req.user_dept is None


class TestSharedPermissionFilter:
    """_build_permission_filter / _DEFAULT_OUTPUT_FIELDS 抽取后必须与原内联实现逐字一致。"""

    def test_filter_exact_strings(self):
        from opensearch_pipeline.retriever import _build_permission_filter
        # fail-closed：空/None/全空白/非白名单 → 仅 public（完整括号 H6）
        assert _build_permission_filter(None) == '(permission_level="public")'
        assert _build_permission_filter("") == '(permission_level="public")'
        assert _build_permission_filter("   ") == '(permission_level="public")'
        assert _build_permission_filter([""]) == '(permission_level="public")'
        assert _build_permission_filter("营销中心") == '(permission_level="public")'  # 中文名非组代码→丢弃
        assert _build_permission_filter("production_injection") == '(permission_level="public")'  # 非白名单
        # 合法组代码：'production' 伞组 + 'marketing'(共享访问→含 production family) 均展开；
        # 期望值由 _expand_groups_to_owners 派生（单一来源，新增子线自动跟随）。
        from opensearch_pipeline.retriever import _expand_groups_to_owners
        def _expect(ud):
            owners = _expand_groups_to_owners(ud if isinstance(ud, list) else [ud])
            clause = " OR ".join('owner_dept="%s"' % o for o in owners)
            return ('(permission_level="public")'
                    ' OR (permission_level="dept_internal" AND (' + clause + '))')
        assert _build_permission_filter("marketing") == _expect("marketing")
        assert _build_permission_filter(["marketing", "production"]) == _expect(["marketing", "production"])
        # production-family 子线对 production 与 marketing 两组都可见（共享访问策略）
        assert 'owner_dept="production_mold"' in _build_permission_filter(["production"])
        assert 'owner_dept="production_mold"' in _build_permission_filter(["marketing"])
        # CSV 字符串等价于列表（伞组展开一致）
        assert _build_permission_filter("marketing,production") == _build_permission_filter(
            ["marketing", "production"])

    def test_filter_neutralizes_injection(self):
        from opensearch_pipeline.retriever import _build_permission_filter
        # 纯注入串：非合法组 → 整体丢弃 → 仅 public
        f = _build_permission_filter('x" OR owner_dept="y')
        assert f == '(permission_level="public")'
        assert '"y' not in f
        # 合法组 + 注入元素混入：合法组保留(marketing 共享访问展开为含 production-family)，
        # 注入元素被净化+白名单丢弃，不产生越权 OR
        from opensearch_pipeline.retriever import _expand_groups_to_owners
        f2 = _build_permission_filter(['marketing', 'x" OR permission_level="restricted'])
        owners = " OR ".join('owner_dept="%s"' % o for o in _expand_groups_to_owners(["marketing"]))
        assert f2 == (
            '(permission_level="public")'
            ' OR (permission_level="dept_internal" AND (' + owners + '))'
        )
        assert "restricted" not in f2          # 注入元素被丢弃
        assert 'owner_dept="x' not in f2       # 注入未引入伪 owner
        # OR 数量 = 顶层 public OR(1) + marketing 展开 owner 间的 OR(N-1)；注入未引入额外 owner
        assert f2.count(" OR ") == 1 + (len(_expand_groups_to_owners(["marketing"])) - 1)

    def test_output_fields_canonical_set(self):
        from opensearch_pipeline.retriever import _DEFAULT_OUTPUT_FIELDS
        assert _DEFAULT_OUTPUT_FIELDS[:3] == ["id", "chunk_id", "doc_id"]
        assert "permission_level" in _DEFAULT_OUTPUT_FIELDS and "owner_dept" in _DEFAULT_OUTPUT_FIELDS
        # version_no 加入用于答案血缘（served chunk -> 精确文档版本可溯源）
        assert "version_no" in _DEFAULT_OUTPUT_FIELDS
        assert len(_DEFAULT_OUTPUT_FIELDS) == 16


# ═══════════════════════════════════════════════════════════════
# Section: 纯向量降级分支 order="DESC"（F-20 / G29）
# ═══════════════════════════════════════════════════════════════

class TestVectorOnlyOrderDesc:
    """enable_hybrid=False 逃生路径的 QueryRequest 必须带 order='DESC'（InnerProduct 越高越相似）。"""

    @patch("opensearch_pipeline.retriever.get_query_embedding")
    @patch("opensearch_pipeline.retriever.get_config")
    @patch("opensearch_pipeline.retriever._get_ha3_client")
    def test_vector_only_query_request_has_order_desc(self, mock_client_fn, mock_config, mock_embedding):
        mock_config.return_value = _make_config(enable_hybrid=False)
        mock_embedding.return_value = ([0.1] * 1024, [1, 5, 10], [0.5, 0.3, 0.2])
        client = MagicMock()
        client.query.return_value = _make_ha3_response()   # 纯向量走 client.query()
        mock_client_fn.return_value = client

        from opensearch_pipeline.retriever import search_chunks
        search_chunks("纯向量降级排序测试")

        assert client.query.called and not client.search.called, "enable_hybrid=False 应走 client.query()"
        assert len(_captured_knn_queries) == 1
        req = _captured_knn_queries[0]
        assert getattr(req, "order", None) == "DESC", "纯向量 QueryRequest 缺 order='DESC'（F-20/G29）"
