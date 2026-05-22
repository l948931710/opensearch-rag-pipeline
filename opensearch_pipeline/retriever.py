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
    调用 DashScope compatible-mode 获取 query 的 dense + sparse embedding。

    compatible-mode 对查询场景足够（单条文本，已验证可返回 sparse）。
    与 pipeline_nodes.py 中批量导入使用 native API 的选择互不冲突。

    Returns:
        (dense_vector, sparse_indices, sparse_values)
    """
    config = get_config()

    _api_key = api_key or config.embedding.api_key
    _model = model or config.embedding.model
    _dim = dimension or config.embedding.dimension

    if not _api_key:
        raise RuntimeError("DashScope API Key 未配置，无法生成 embedding")

    # DashScope compatible-mode endpoint
    base_url = config.embedding.api_base_url.rstrip("/")
    url = f"{base_url}/compatible-mode/v1/embeddings"

    # 如果 base_url 已经包含 compatible-mode，则不再拼接
    if "compatible-mode" in base_url:
        url = f"{base_url}/embeddings"

    resp = requests.post(
        url,
        json={
            "model": _model,
            "input": [query],
            "dimensions": _dim,
            "output_type": "dense&sparse",
        },
        headers={
            "Authorization": f"Bearer {_api_key}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["data"][0]

    dense = data["embedding"]

    # sparse_embedding 为 {str_index: float_value} 的字典
    sparse = data.get("sparse_embedding", {})
    sparse_indices: List[int] = []
    sparse_values: List[float] = []
    if sparse:
        sorted_pairs = sorted(sparse.items(), key=lambda x: int(x[0]))
        sparse_indices = [int(k) for k, _ in sorted_pairs]
        sparse_values = [float(v) for _, v in sorted_pairs]

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
            "chunk_text": fields.get("chunk_text", ""),
            "title": fields.get("title", ""),
            "section_title": fields.get("section_title", ""),
            "doc_id": fields.get("doc_id", ""),
            "category_l1": fields.get("category_l1", ""),
            "score": item.get("score", item.get("_score", 0)),
        })
    return parsed


def search_chunks(
    query: str,
    *,
    top_k: int = 5,
    output_fields: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    端到端检索：query → embedding → HA3 search → 标准化结果。

    Returns:
        [{"chunk_text", "title", "section_title", "doc_id", "score", ...}]
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
        "id", "doc_id", "chunk_text", "title", "section_title", "category_l1",
    ]

    request = QueryRequest(
        table_name=config.alibaba_vector.table_name,
        vector=dense,
        sparse_data=sparse_data,
        top_k=top_k,
        include_vector=False,
        output_fields=_output_fields,
    )

    client = _get_ha3_client()
    resp = client.query(request)

    # 3. 解析结果
    results = _parse_ha3_response(resp)
    logger.info("Search completed: query=%r, results=%d", query[:50], len(results))
    return results
