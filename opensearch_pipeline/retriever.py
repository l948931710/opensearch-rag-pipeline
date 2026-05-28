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


def _escape_ha3_query(text: str) -> str:
    """转义 HA3 queryString 中的特殊字符，防止查询语法注入。

    HA3 query 语法中单引号 ' 用于包裹查询词，用户输入的单引号会
    破坏语法结构。反斜杠 \\ 和双引号 " 也需转义。
    """
    # 移除单引号（HA3 不支持引号内转义，只能剥离）
    text = text.replace("'", " ")
    # 转义反斜杠和双引号
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text.strip()


def _sanitize_ha3_filter_value(value: str) -> str:
    """清理 HA3 filter 表达式中的字段值，防止过滤条件注入。

    HA3 filter 中双引号用于包裹值，攻击者可通过注入双引号闭合值边界
    并追加额外过滤条件（如绕过 permission_level 限制）。

    策略：仅保留字母、数字、下划线、连字符、中文字符，剥离所有其他字符。
    这比转义更安全，因为 HA3 filter 语法中引号内的转义行为未明确文档化。
    """
    import re
    # 白名单：部门代码通常是字母数字+下划线+连字符+中文
    return re.sub(r'[^\w\-\u4e00-\u9fff]', '', value)


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

    当 enable_hybrid=True（默认）时，使用 HA3 服务端三路混合检索：
      - kNN 路: Dense + Sparse 向量检索
      - Text 路: BM25 全文检索（基于 chunk_text 倒排索引）
      - 融合: RRF 或加权求和

    当 enable_hybrid=False 时，降级为纯向量检索（兼容旧行为）。

    Args:
        query: 用户查询文本
        top_k: 最终返回的结果数
        min_score: (保留兼容) 相似度下限，仅在 score 为 0-1 相似度时使用
        max_distance: HA3 距离上限，score 越小越相关；设为 0 表示不过滤
        output_fields: 自定义返回字段列表

    Returns:
        [{"chunk_text", "title", "section_title", "doc_id", "score", ...}]
    """
    config = get_config()
    cfg = config.alibaba_vector

    # 1. 生成 query embedding
    dense, sparse_idx, sparse_val = get_query_embedding(query)

    # 2. 构建 sparse data
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
    if user_dept:
        safe_dept = _sanitize_ha3_filter_value(user_dept)
        filter_expr = (
            'permission_level="public"'
            ' OR (permission_level="dept_internal" AND owner_dept="' + safe_dept + '")'
        )
    else:
        filter_expr = 'permission_level="public"'

    logger.info("Permission filter: user_dept=%s, filter=%s", user_dept, filter_expr)

    # 3. 构建请求并执行
    client = _get_ha3_client()

    if cfg.enable_hybrid:
        # ── 混合检索: Dense + Sparse + BM25 三路融合 ──
        from alibabacloud_ha3engine_vector.models import (
            SearchRequest, TextQuery, RankQuery,
        )

        # kNN 路（Dense + Sparse）— 复用 QueryRequest 模型
        knn_query = QueryRequest(
            table_name=cfg.table_name,
            vector=dense,
            sparse_data=sparse_data,
            top_k=cfg.hybrid_knn_top_k,
            include_vector=False,
            filter=filter_expr,
        )

        # BM25 Text 路
        escaped_query = _escape_ha3_query(query)
        text_query = TextQuery(
            query_string=f"{cfg.text_search_field}:'{escaped_query}'",
            query_params={"default_op": "OR"},
            filter=filter_expr,
        )

        # 融合策略
        if cfg.hybrid_fusion == "rrf":
            rank = RankQuery(rrf={"rankConstant": cfg.rrf_rank_constant})
        else:
            # 加权模式：通过 knn.weight 和 text.weight 控制
            knn_query.weight = cfg.knn_weight
            text_query.weight = cfg.text_weight
            rank = RankQuery()  # 空 rank = 默认加权策略

        request = SearchRequest(
            table_name=cfg.table_name,
            knn=knn_query,
            text=text_query,
            rank=rank,
            size=top_k,
            order="DESC",
            output_fields=_output_fields,
        )

        logger.info(
            "Hybrid search: fusion=%s, text_field=%s, knn_top_k=%d, size=%d",
            cfg.hybrid_fusion, cfg.text_search_field, cfg.hybrid_knn_top_k, top_k,
        )
        resp = client.search(request)
    else:
        # ── 纯向量检索（降级 / 兼容旧行为）──
        request = QueryRequest(
            table_name=cfg.table_name,
            vector=dense,
            sparse_data=sparse_data,
            top_k=top_k,
            include_vector=False,
            output_fields=_output_fields,
            filter=filter_expr,
        )
        logger.info("Vector-only search: top_k=%d", top_k)
        resp = client.query(request)

    # 4. 解析结果
    results = _parse_ha3_response(resp)

    # 5. 过滤低相关度结果
    # 注意：混合检索模式下 score 是 RRF/加权融合分（越大越相关，DESC 排序），
    # 纯向量模式下 score 是距离分（越小越相关）。max_distance 仅适用于纯向量模式。
    if not cfg.enable_hybrid and max_distance > 0:
        before_count = len(results)
        results = [r for r in results if r.get("score", 0) <= max_distance]
        filtered = before_count - len(results)
        if filtered > 0:
            logger.info("Filtered %d distant results (max_distance=%.2f)", filtered, max_distance)

    logger.info("Search completed: query=%r, results=%d, hybrid=%s", query[:50], len(results), cfg.enable_hybrid)
    return results


def expand_top_document(
    initial_chunks: List[Dict[str, Any]],
    *,
    expand_size: int = 8,
    min_content_ratio: float = 0.5,
) -> List[Dict[str, Any]]:
    """
    同文档扩展：当检索结果中最相关文档只命中了封面/元数据 chunk 时，
    自动补充该文档的更多正文 chunks。

    解决概括性问题（如"XX操作手册主要内容有哪些"）只召回封面页的问题。

    判定逻辑：
      1. 取得分最高的文档 doc_id
      2. 统计该文档在初始结果中有多少 chunk 含 section_title（正文标志）
      3. 如果正文占比低于 min_content_ratio，做一次 BM25 按 doc_id 过滤查询
      4. 将新 chunks（去重）合并到结果中

    Args:
        initial_chunks: search_chunks 返回的初始结果
        expand_size: 补充查询最多获取的 chunk 数量
        min_content_ratio: 当正文 chunk 占比低于此值时触发扩展

    Returns:
        合并后的 chunks 列表（初始结果 + 扩展结果，已去重）
    """
    if not initial_chunks:
        return initial_chunks

    # 找得分最高的文档
    top_doc_id = initial_chunks[0].get("doc_id", "")
    if not top_doc_id:
        return initial_chunks

    # 统计该文档在初始结果中的正文 chunk 数量
    doc_chunks = [c for c in initial_chunks if c.get("doc_id") == top_doc_id]
    content_chunks = [c for c in doc_chunks if c.get("section_title")]
    total_doc_chunks = len(doc_chunks)

    if total_doc_chunks == 0:
        return initial_chunks

    content_ratio = len(content_chunks) / total_doc_chunks

    if content_ratio >= min_content_ratio:
        # 已有足够正文 chunk，不需要扩展
        logger.debug(
            "同文档扩展: doc_id=%s, content_ratio=%.1f%% >= %.0f%%, 跳过",
            top_doc_id, content_ratio * 100, min_content_ratio * 100,
        )
        return initial_chunks

    logger.info(
        "同文档扩展: doc_id=%s, 初始正文=%d/%d (%.0f%%), 触发补充查询",
        top_doc_id, len(content_chunks), total_doc_chunks, content_ratio * 100,
    )

    # 使用 BM25 按 doc_id 过滤查询，获取该文档的更多 chunks
    try:
        config = get_config()
        cfg = config.alibaba_vector
        client = _get_ha3_client()

        from alibabacloud_ha3engine_vector.models import SearchRequest, TextQuery, RankQuery

        _output_fields = [
            "id", "doc_id", "chunk_text_store", "title", "section_title",
            "category_l1", "chunk_index", "page_num", "kb_type",
            "permission_level", "owner_dept",
        ]

        escaped_doc_id = _escape_ha3_query(top_doc_id)
        text_query = TextQuery(
            query_string=f"doc_id:'{escaped_doc_id}'",
            query_params={"default_op": "AND"},
        )
        request = SearchRequest(
            table_name=cfg.table_name,
            text=text_query,
            rank=RankQuery(),
            size=expand_size,
            order="DESC",
            output_fields=_output_fields,
        )

        resp = client.search(request)
        expanded = _parse_ha3_response(resp)

        # 只保留有 section_title 的正文 chunks
        expanded_content = [c for c in expanded if c.get("section_title")]

        if not expanded_content:
            logger.info("同文档扩展: doc_id=%s 无正文 chunk 返回", top_doc_id)
            return initial_chunks

        # 去重：排除初始结果中已有的 chunks（按 chunk_index 去重）
        existing_indices = {
            (c.get("doc_id"), c.get("chunk_index"))
            for c in initial_chunks
        }
        new_chunks = [
            c for c in expanded_content
            if (c.get("doc_id"), c.get("chunk_index")) not in existing_indices
        ]

        if not new_chunks:
            logger.info("同文档扩展: 全部 chunk 已在初始结果中")
            return initial_chunks

        # 为扩展 chunks 赋予一个合理的分数（略低于原始最高分）
        top_score = initial_chunks[0].get("score", 0)
        for c in new_chunks:
            c["score"] = top_score * 0.95  # 略低于最高分，排序时不会喧宾夺主

        # 合并：初始结果 + 扩展结果
        merged = initial_chunks + new_chunks
        logger.info(
            "同文档扩展完成: doc_id=%s, 新增 %d 个正文 chunk, 总计 %d",
            top_doc_id, len(new_chunks), len(merged),
        )
        return merged

    except Exception as e:
        logger.warning("同文档扩展失败: %s", e, exc_info=True)
        return initial_chunks


# ═══════════════════════════════════════════════════════════════
# 4. Neighbor Stitching（邻居扩展）
# ═══════════════════════════════════════════════════════════════

def stitch_neighbor_chunks(
    chunks: List[Dict[str, Any]],
    *,
    window: int = 1,
) -> List[Dict[str, Any]]:
    """
    对检索结果中的每个 chunk，从 RDS 查询 chunk_index ±window 的邻居并拼接。

    解决 chunk 边界切割导致信息不完整的问题：
      - 一个完整条款被切成两个 chunk，检索只命中了一半
      - SOP 流程步骤跨越 chunk 边界

    评测数据 (120 queries)：
      - Context Coverage: +3.1pp (88.8% → 91.8%)
      - Answer Completeness: +2.1pp (82.2% → 84.3%)
      - 退化率: 0% (无负面影响)

    实现细节：
      - 从 RDS chunk_meta 查询邻居（<10ms per query）
      - 按 (doc_id, chunk_index) 去重，不跨文档边界
      - 同一个文档内的邻居 chunk 按 chunk_index 排序后拼接文本
      - 保留原始检索 chunk 的 score / metadata

    Args:
        chunks: search_chunks 或 expand_top_document 返回的结果
        window: 向前/后扩展的 chunk 数量，默认 1（即 ±1）

    Returns:
        扩展后的 chunks 列表，chunk_text 已包含邻居文本
    """
    if not chunks or window <= 0:
        return chunks

    try:
        import pymysql.cursors
        from opensearch_pipeline.pipeline_nodes import _get_db_conn

        conn = _get_db_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        expanded = []
        seen_centers = set()  # 去重：同一个 (doc_id, chunk_index) 只输出一次

        for chunk in chunks:
            doc_id = chunk.get("doc_id", "")
            center_idx = chunk.get("chunk_index", 0)
            center_key = (doc_id, center_idx)

            if not doc_id:
                expanded.append(chunk)
                continue

            # 同一个中心 chunk 已被前面的 hit 处理过（例如 hit A 的邻居 = hit B 的中心）
            if center_key in seen_centers:
                continue
            seen_centers.add(center_key)

            # 查询 ±window 邻居
            cursor.execute("""
                SELECT chunk_index, chunk_text, section_title
                FROM chunk_meta
                WHERE doc_id = %s
                  AND is_active = 1
                  AND chunk_index BETWEEN %s AND %s
                ORDER BY chunk_index
            """, (doc_id, center_idx - window, center_idx + window))
            neighbors = cursor.fetchall()

            if neighbors:
                stitched_text = "\n".join(nb["chunk_text"] or "" for nb in neighbors)
            else:
                stitched_text = chunk.get("chunk_text", "")

            # 构建扩展后的 chunk（保留原始 score 等字段）
            expanded_chunk = dict(chunk)
            expanded_chunk["chunk_text"] = stitched_text
            expanded_chunk["_stitched"] = True
            expanded_chunk["_stitch_window"] = window
            expanded_chunk["_neighbor_count"] = len(neighbors)
            expanded.append(expanded_chunk)

        cursor.close()
        conn.close()

        logger.info(
            "邻居扩展完成: %d chunks → %d expanded (去重 %d), window=±%d",
            len(chunks), len(expanded), len(chunks) - len(expanded), window,
        )
        return expanded

    except Exception as e:
        logger.warning("邻居扩展失败，回退到原始结果: %s", e, exc_info=True)
        return chunks
