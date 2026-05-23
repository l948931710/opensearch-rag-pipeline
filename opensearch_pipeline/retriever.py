# -*- coding: utf-8 -*-
"""
retriever.py — 检索模块

封装 DashScope Embedding + OpenSearch HA3 向量检索，为 RAG 问答提供上下文。
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. Query Embedding
# ═══════════════════════════════════════════════════════════════

def get_query_embedding(
    query: str,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    dimension: Optional[int] = None,
) -> Tuple[List[float], List[int], List[float]]:
    """
    调用 DashScope **native API** 获取 query 的 dense + sparse embedding。

    必须使用 native API 而非 compatible-mode，因为 compatible-mode 不返回 sparse embedding，
    会导致混合检索（dense + sparse）退化为纯 dense 检索，严重影响召回质量。

    Returns:
        (dense_vector, sparse_indices, sparse_values)
    """
    config = get_config()

    _api_key = api_key or config.embedding.api_key
    _model = model or config.embedding.model
    _dim = dimension or config.embedding.dimension

    if not _api_key:
        raise RuntimeError("DashScope API Key 未配置，无法生成 embedding")

    # DashScope native API endpoint（唯一支持 sparse 的端点）
    base_url = config.embedding.api_base_url.rstrip("/")
    url = f"{base_url}/api/v1/services/embeddings/text-embedding/text-embedding"

    # 如果 base_url 已经包含 /api/v1，则不再重复拼接
    if "/api/v1" in base_url:
        url = f"{base_url}/services/embeddings/text-embedding/text-embedding"

    resp = requests.post(
        url,
        json={
            "model": _model,
            "input": {"texts": [query]},
            "parameters": {
                "dimension": _dim,
                "output_type": "dense&sparse",
            },
        },
        headers={
            "Authorization": f"Bearer {_api_key}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()

    embedding_data = result["output"]["embeddings"][0]
    dense = embedding_data["embedding"]

    # native API 的 sparse_embedding 是 list[dict]，每个 dict 含 index, token, value
    sparse_list = embedding_data.get("sparse_embedding", [])
    sparse_indices: List[int] = []
    sparse_values: List[float] = []
    if sparse_list:
        sorted_sparse = sorted(sparse_list, key=lambda x: x["index"])
        sparse_indices = [s["index"] for s in sorted_sparse]
        sparse_values = [float(s["value"]) for s in sorted_sparse]

    logger.debug(
        "Embedding generated: dense=%d dims, sparse=%d nonzero",
        len(dense), len(sparse_indices),
    )
    return dense, sparse_indices, sparse_values


# ═══════════════════════════════════════════════════════════════
# 2. HA3 Vector Search
# ═══════════════════════════════════════════════════════════════

_ha3_client = None


def _get_ha3_client():
    """懒初始化 HA3 客户端（单例）。"""
    global _ha3_client
    if _ha3_client is not None:
        return _ha3_client

    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config

    cfg = get_config().alibaba_vector
    if not cfg.endpoint:
        raise RuntimeError("HA3 endpoint 未配置，无法进行向量检索")

    clean_endpoint = cfg.endpoint.replace("http://", "").replace("https://", "")

    ha3_config = Config(
        endpoint=clean_endpoint,
        instance_id=cfg.instance_id,
        access_user_name=cfg.access_user_name,
        access_pass_word=cfg.access_pass_word,
    )
    _ha3_client = Client(ha3_config)
    logger.info("HA3 client initialized: endpoint=%s", clean_endpoint)
    return _ha3_client


def _parse_ha3_response(resp) -> List[Dict[str, Any]]:
    """将 HA3 QueryResponse 解析为标准化的结果列表。"""
    body = resp.body
    if isinstance(body, str):
        body = json.loads(body)
    elif hasattr(body, "to_map"):
        body = body.to_map()

    results = []
    if isinstance(body, dict):
        raw = body.get("result", body.get("hits", body.get("data", [])))
        if isinstance(raw, dict):
            raw = raw.get("hits", raw.get("items", []))
        if isinstance(raw, list):
            results = raw

    parsed = []
    for item in results:
        fields = item.get("fields", item)
        parsed.append({
            "chunk_text": fields.get("chunk_text_store", fields.get("chunk_text", "")),
            "title": fields.get("title", ""),
            "section_title": fields.get("section_title", ""),
            "doc_id": fields.get("doc_id", ""),
            "category_l1": fields.get("category_l1", ""),
            "chunk_index": fields.get("chunk_index", 0),
            "page_num": fields.get("page_num", 0),
            "kb_type": fields.get("kb_type", "public"),
            "permission_level": fields.get("permission_level", "public"),
            "owner_dept": fields.get("owner_dept", ""),
            "score": item.get("score", item.get("_score", 0)),
        })
    return parsed


def search_chunks(
    query: str,
    *,
    top_k: int = 5,
    min_score: float = 0.0,
    max_distance: float = 0.0,
    output_fields: Optional[List[str]] = None,
    user_dept: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    端到端检索：query → embedding → HA3 search → 标准化结果。

    Args:
        query: 用户查询文本
        top_k: HA3 返回的最大结果数
        min_score: (保留兼容) 相似度下限，仅在 score 为 0-1 相似度时使用
        max_distance: HA3 距离上限，score 越小越相关；设为 0 表示不过滤
        output_fields: 自定义返回字段列表

    Returns:
        [{\"chunk_text\", \"title\", \"section_title\", \"doc_id\", \"score\", ...}]
    """
    config = get_config()

    # 1. 生成 query embedding
    dense, sparse_idx, sparse_val = get_query_embedding(query)

    # 2. 构建 HA3 查询
    from alibabacloud_ha3engine_vector.models import QueryRequest, SparseData

    sparse_data = None
    if sparse_idx:
        sparse_data = SparseData(
            count=[len(sparse_idx)],
            indices=sparse_idx,
            values=sparse_val,
        )

    _output_fields = output_fields or [
        "id", "doc_id", "chunk_text_store", "title", "section_title",
        "category_l1", "chunk_index", "page_num", "kb_type",
        "permission_level", "owner_dept",
    ]

    # ── 权限过滤 ──
    # 企业内部部署，所有钉钉用户都是公司员工：
    #   public        → 所有员工可见（默认）
    #   dept_internal → 仅 owner_dept 匹配的部门成员可见
    if user_dept:
        # 用户部门已知：可以看 public + 自己部门的 dept_internal
        filter_expr = (
            'permission_level="public"'
            ' OR (permission_level="dept_internal" AND owner_dept="' + user_dept + '")'
        )
    else:
        # 用户部门未知（降级安全模式）：只能看 public
        filter_expr = 'permission_level="public"'

    logger.info("Permission filter: user_dept=%s, filter=%s", user_dept, filter_expr)

    request = QueryRequest(
        table_name=config.alibaba_vector.table_name,
        vector=dense,
        sparse_data=sparse_data,
        top_k=top_k,
        include_vector=False,
        output_fields=_output_fields,
        filter=filter_expr,
    )

    client = _get_ha3_client()
    resp = client.query(request)

    # 3. 解析结果
    results = _parse_ha3_response(resp)

    # 4. 过滤低相关度结果
    # HA3 混合检索的 score 是距离分数（越小越相关），用 max_distance 过滤
    if max_distance > 0:
        before_count = len(results)
        results = [r for r in results if r.get("score", 0) <= max_distance]
        filtered = before_count - len(results)
        if filtered > 0:
            logger.info("Filtered %d distant results (max_distance=%.2f)", filtered, max_distance)

    logger.info("Search completed: query=%r, results=%d", query[:50], len(results))
    return results
