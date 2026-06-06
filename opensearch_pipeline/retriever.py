# -*- coding: utf-8 -*-
"""
retriever.py — 检索模块

封装 DashScope Embedding + OpenSearch HA3 向量检索，为 RAG 问答提供上下文。
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 0. Step Card Query Intent Classifier
# ═══════════════════════════════════════════════════════════════

_STEP_INTENT_PATTERNS = [
    # 匹配顺序：窄 → 宽（先精确匹配，避免宽泛模式抢占）
    (
        "specific_step",
        re.compile(
            r"第几步|第\s*\d+\s*步|步骤\s*\d+|下一步|上一步",
        ),
    ),
    (
        "locate_field",
        re.compile(
            r"哪里|在哪|怎么填|填写|按钮|字段|位置|菜单|选项|入口",
        ),
    ),
    (
        "full_procedure",
        re.compile(
            r"如何|流程|怎么操作|怎么做|怎么用|办理|整个|完整|全部步骤|所有步骤",
        ),
    ),
]


def _classify_step_query_intent(query: str) -> str:
    """根据关键词将用户查询分类为 Step Card 检索意图。

    分类结果：
      - ``full_procedure``  — 用户想要完整流程（怎么、如何、流程 …）
      - ``locate_field``    — 用户想定位某个 UI 元素（哪里、在哪、按钮 …）
      - ``specific_step``   — 用户问特定步骤（第N步、下一步 …）
      - ``general``         — 默认兜底

    Returns:
        意图字符串
    """
    for intent, pattern in _STEP_INTENT_PATTERNS:
        if pattern.search(query):
            return intent
    return "general"


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
        # HA3 的 MULTI_STRING 字段返回列表 (e.g. ['image'])，需要归一化为字符串
        raw_chunk_type = fields.get("chunk_type", "")
        if isinstance(raw_chunk_type, list):
            raw_chunk_type = raw_chunk_type[0] if raw_chunk_type else ""
        parsed.append({
            # chunk_id 是 step_card/visual_knowledge 的 RDS 重建键，也是 expand_step_context
            # 末尾去重的唯一键——必须透传，否则去重会把所有无 id 的 chunk 折叠成一个。
            "chunk_id": fields.get("chunk_id", ""),
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
            "chunk_type": raw_chunk_type,
            "source_image": fields.get("source_image", ""),
            "visual_summary": fields.get("visual_summary", ""),
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
        "id", "chunk_id", "doc_id", "chunk_text_store", "title", "section_title",
        "category_l1", "chunk_index", "page_num", "kb_type",
        "permission_level", "owner_dept", "chunk_type",
        "source_image", "visual_summary",
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

    # 6. 封面/元数据 chunk 降权
    # 短文本 + 无 section_title 的 chunk 通常是封面页或目录，
    # 包含文档标题导致 BM25 高分，但没有实质内容。
    # 策略：正文 chunk 优先排前面，封面 chunk 排后面（不丢弃，避免无结果）。
    # 注意：图片 chunk 天然短文本、无 section_title，但含有 visual_summary 语义信息，不应被降权。
    _COVER_MAX_LEN = 200  # 短于此且无 section_title 视为封面/元数据
    content_results = []
    cover_results = []
    for r in results:
        text = r.get("chunk_text", "")
        has_section = bool(r.get("section_title"))
        chunk_type = r.get("chunk_type", "")
        if chunk_type in ("image", "step_card", "procedure_parent", "visual_knowledge"):
            content_results.append(r)
        elif not has_section and len(text) < _COVER_MAX_LEN:
            cover_results.append(r)
        else:
            content_results.append(r)

    if cover_results:
        logger.info(
            "封面降权: %d 个封面 chunk 被移到末尾 (共 %d 结果)",
            len(cover_results), len(results),
        )
    results = content_results + cover_results

    logger.info("Search completed: query=%r, results=%d (content=%d, cover=%d), hybrid=%s",
                query[:50], len(results), len(content_results), len(cover_results), cfg.enable_hybrid)
    return results


def expand_top_document(
    initial_chunks: List[Dict[str, Any]],
    *,
    expand_size: int = 8,
    min_content_ratio: float = 0.5,
) -> List[Dict[str, Any]]:
    """
    [DEPRECATED] 同文档扩展 — 不再在生产路径中使用。

    封面问题已通过 top_k over-fetch + 封面降权 + 截取前 N 策略解决。
    概括性问题更适合 query classification → 全文摘要路径处理。
    保留代码以备未来需要。

    原始描述：当检索结果中最相关文档只命中了封面/元数据 chunk 时，
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
            "id", "chunk_id", "doc_id", "chunk_text_store", "title", "section_title",
            "category_l1", "chunk_index", "page_num", "kb_type",
            "permission_level", "owner_dept", "chunk_type",
            "source_image", "visual_summary",
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

            # step_card / procedure_parent / visual_knowledge 已是语义完整单元，跳过邻居拼接
            chunk_type = chunk.get("chunk_type", "")
            if chunk_type in ("step_card", "procedure_parent", "visual_knowledge"):
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


# ═══════════════════════════════════════════════════════════════
# 4.5 Step Card 上下文扩展
# ═══════════════════════════════════════════════════════════════

def expand_step_context(
    chunks: List[Dict[str, Any]],
    query: str,
    *,
    max_steps: int = 8,
    max_images_total: int = 8,
) -> List[Dict[str, Any]]:
    """对 step_card / procedure_parent 类型的检索结果进行上下文扩展。

    根据用户查询意图从 RDS 查询同一流程的兄弟步骤或子步骤，
    将语义完整的步骤序列组装后返回，让 LLM 能看到完整操作上下文。

    扩展策略（按意图）：
      - ``full_procedure``  — 包含全部兄弟步骤（上限 max_steps）
      - ``locate_field``    — 仅保留命中步骤本身（不扩展）
      - ``specific_step``   — 命中步骤 + 下一步
      - ``general``         — 命中步骤 ±1 邻居

    排序规则：
      按 parent_chunk_id 分组，组间按组内最高分降序，组内按 step_no 升序。

    Args:
        chunks: 上游检索 + 邻居拼接后的结果列表
        query: 用户原始查询文本
        max_steps: 单个流程最多展示的步骤数
        max_images_total: 全局图片引用上限（预留）

    Returns:
        扩展、去重、重排后的 chunks 列表
    """
    if not chunks:
        return chunks

    intent = _classify_step_query_intent(query)
    logger.info("Step Card 意图分类: query=%r → intent=%s", query[:60], intent)

    # 判断是否存在需要扩展的 chunk 类型，避免无意义的 RDS 连接
    # visual_knowledge：image_refs 仅落库 RDS（HA3 只回 source_image 首图），需按 chunk_id 补全多图。
    need_expand = any(
        c.get("chunk_type") in ("step_card", "procedure_parent", "visual_knowledge")
        for c in chunks
    )
    if not need_expand:
        return chunks

    try:
        import pymysql.cursors
        from opensearch_pipeline.pipeline_nodes import _get_db_conn

        conn = _get_db_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
    except Exception as e:
        logger.warning("expand_step_context: 无法获取 RDS 连接，回退到原始结果: %s", e)
        return chunks

    try:
        expanded_all: List[Dict[str, Any]] = []

        for chunk in chunks:
            ctype = chunk.get("chunk_type", "")

            # ── step_card：查 RDS 获取 parent_chunk_id / step_no，再展开兄弟 ──
            if ctype == "step_card":
                chunk_id = chunk.get("chunk_id") or chunk.get("id", "")
                original_score = chunk.get("score", 0)

                # step_card 从 HA3 返回时不带 parent_chunk_id / step_no，需查 RDS
                cursor.execute(
                    "SELECT parent_chunk_id, step_no, extra_json, image_refs_json "
                    "FROM chunk_meta WHERE chunk_id = %s",
                    (chunk_id,),
                )
                meta_row = cursor.fetchone()
                if not meta_row or not meta_row.get("parent_chunk_id"):
                    # RDS 无记录，原样保留
                    expanded_all.append(chunk)
                    continue

                parent_id = meta_row["parent_chunk_id"]
                hit_step_no = meta_row.get("step_no") or 0

                # 查所有兄弟步骤
                cursor.execute(
                    "SELECT chunk_id, chunk_text, step_no, section_title, "
                    "       extra_json, image_refs_json "
                    "FROM chunk_meta "
                    "WHERE parent_chunk_id = %s AND is_active = 1 "
                    "ORDER BY step_no",
                    (parent_id,),
                )
                siblings = cursor.fetchall()

                # 按意图筛选
                if intent == "full_procedure":
                    selected = siblings[:max_steps]
                elif intent == "locate_field":
                    selected = [s for s in siblings if s["step_no"] == hit_step_no]
                elif intent == "specific_step":
                    selected = [
                        s for s in siblings
                        if s["step_no"] is not None
                        and hit_step_no <= s["step_no"] <= hit_step_no + 1
                    ]
                else:  # general
                    selected = [
                        s for s in siblings
                        if s["step_no"] is not None
                        and hit_step_no - 1 <= s["step_no"] <= hit_step_no + 1
                    ]

                for sib in selected:
                    is_hit = (sib["chunk_id"] == chunk_id)
                    score = original_score if is_hit else original_score * 0.85

                    # 解析 extra_json
                    extra = {}
                    if sib.get("extra_json"):
                        try:
                            extra = json.loads(sib["extra_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass

                    # 解析 image_refs_json
                    image_refs_raw: list = []
                    if sib.get("image_refs_json"):
                        try:
                            image_refs_raw = json.loads(sib["image_refs_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass

                    # 标准化 image_refs 格式
                    image_refs = []
                    for idx, ref in enumerate(image_refs_raw):
                        if isinstance(ref, dict):
                            image_refs.append({
                                "oss_key": ref.get("oss_key", ""),
                                "ocr_text": ref.get("ocr_text", ""),
                                "caption": ref.get("caption", ""),
                                "order": ref.get("order", idx),
                            })

                    expanded_chunk = dict(chunk)  # 继承原始 hit 的 metadata
                    expanded_chunk.update({
                        "chunk_id": sib["chunk_id"],
                        "chunk_text": sib.get("chunk_text", ""),
                        "step_no": sib.get("step_no"),
                        "section_title": sib.get("section_title", ""),
                        "parent_chunk_id": parent_id,
                        "score": score,
                        "image_refs": image_refs,
                        "annotation_map": extra.get("annotation_map", {}),
                        "is_expanded": not is_hit,
                        "expanded_from": chunk_id if not is_hit else None,
                        "expansion_reason": "sibling_step" if not is_hit else None,
                    })
                    expanded_all.append(expanded_chunk)

            # ── procedure_parent：展开子步骤 ──
            elif ctype == "procedure_parent":
                original_score = chunk.get("score", 0)

                # 从 extra_json 获取 child_chunk_ids
                extra_raw = chunk.get("extra_json") or chunk.get("extra", {})
                if isinstance(extra_raw, str):
                    try:
                        extra_raw = json.loads(extra_raw)
                    except (json.JSONDecodeError, TypeError):
                        extra_raw = {}
                child_ids = extra_raw.get("child_chunk_ids", []) if isinstance(extra_raw, dict) else []

                if not child_ids:
                    expanded_all.append(chunk)
                    continue

                # 查子步骤
                format_ph = ",".join(["%s"] * len(child_ids))
                cursor.execute(
                    f"SELECT chunk_id, chunk_text, step_no, section_title, "
                    f"       extra_json, image_refs_json "
                    f"FROM chunk_meta "
                    f"WHERE chunk_id IN ({format_ph}) AND is_active = 1 "
                    f"ORDER BY step_no",
                    tuple(child_ids),
                )
                children = cursor.fetchall()
                total_children = len(children)

                # 截断并添加提示
                if total_children > max_steps:
                    parent_chunk = dict(chunk)
                    parent_chunk["chunk_text"] = (
                        chunk.get("chunk_text", "")
                        + f"\n（该流程共{total_children}步，以下展示前{max_steps}步）"
                    )
                    expanded_all.append(parent_chunk)
                    children = children[:max_steps]
                else:
                    expanded_all.append(chunk)

                parent_chunk_id = chunk.get("chunk_id") or chunk.get("id", "")

                for child in children:
                    child_extra = {}
                    if child.get("extra_json"):
                        try:
                            child_extra = json.loads(child["extra_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass

                    image_refs_raw = []
                    if child.get("image_refs_json"):
                        try:
                            image_refs_raw = json.loads(child["image_refs_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass

                    image_refs = []
                    for idx, ref in enumerate(image_refs_raw):
                        if isinstance(ref, dict):
                            image_refs.append({
                                "oss_key": ref.get("oss_key", ""),
                                "ocr_text": ref.get("ocr_text", ""),
                                "caption": ref.get("caption", ""),
                                "order": ref.get("order", idx),
                            })

                    expanded_chunk = dict(chunk)
                    expanded_chunk.update({
                        "chunk_id": child["chunk_id"],
                        "chunk_text": child.get("chunk_text", ""),
                        "step_no": child.get("step_no"),
                        "section_title": child.get("section_title", ""),
                        "parent_chunk_id": parent_chunk_id,
                        "score": original_score * 0.8,
                        "image_refs": image_refs,
                        "annotation_map": child_extra.get("annotation_map", {}),
                        "is_expanded": True,
                        "expanded_from": parent_chunk_id,
                        "expansion_reason": "parent_children",
                    })
                    expanded_all.append(expanded_chunk)

            # ── visual_knowledge：按 chunk_id 从 RDS 补全全部 image_refs（多图幻灯片）──
            elif ctype == "visual_knowledge":
                enriched = dict(chunk)
                chunk_id = chunk.get("chunk_id") or chunk.get("id", "")
                # HA3 只回 source_image（首图）；image_refs 不在索引里。仅当结果未带
                # image_refs 时回 RDS 取全量，失败/无记录则保留 source_image 首图兜底。
                if chunk_id and not chunk.get("image_refs"):
                    cursor.execute(
                        "SELECT image_refs_json FROM chunk_meta WHERE chunk_id = %s",
                        (chunk_id,),
                    )
                    vk_row = cursor.fetchone()
                    refs_raw: list = []
                    if vk_row and vk_row.get("image_refs_json"):
                        try:
                            refs_raw = json.loads(vk_row["image_refs_json"])
                        except (json.JSONDecodeError, TypeError):
                            refs_raw = []
                    vk_refs = []
                    for idx, ref in enumerate(refs_raw):
                        if isinstance(ref, dict) and (ref.get("oss_key") or ref.get("source_image")):
                            vk_refs.append({
                                "oss_key": ref.get("oss_key") or ref.get("source_image", ""),
                                "visual_summary": ref.get("visual_summary", "") or ref.get("ocr_text", ""),
                                "caption": ref.get("caption", ""),
                                "order": ref.get("order", idx),
                            })
                    if vk_refs:
                        enriched["image_refs"] = vk_refs
                expanded_all.append(enriched)

            else:
                # 非 step 类型，原样保留
                expanded_all.append(chunk)

        cursor.close()
        conn.close()

    except Exception as e:
        logger.warning("expand_step_context 处理异常，回退到原始结果: %s", e, exc_info=True)
        return chunks

    # ── 去重：相同 chunk_id 保留最高分 ──
    seen: Dict[str, Dict[str, Any]] = {}
    for c in expanded_all:
        cid = c.get("chunk_id") or c.get("id", "")
        if cid in seen:
            if c.get("score", 0) > seen[cid].get("score", 0):
                seen[cid] = c
        else:
            seen[cid] = c
    deduped = list(seen.values())

    # ── 排序：按 parent_chunk_id 分组 → 组间按最高分降序 → 组内按 step_no 升序 ──
    groups: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for c in deduped:
        gkey = c.get("parent_chunk_id")
        groups.setdefault(gkey, []).append(c)

    # 组内按 step_no 排序
    for members in groups.values():
        members.sort(key=lambda x: (x.get("step_no") or 0))

    # 组间按最高分降序
    sorted_groups = sorted(
        groups.values(),
        key=lambda grp: max(c.get("score", 0) for c in grp),
        reverse=True,
    )

    result: List[Dict[str, Any]] = []
    for grp in sorted_groups:
        result.extend(grp)

    logger.info(
        "Step Card 扩展完成: %d chunks → %d expanded (去重后 %d), intent=%s",
        len(chunks), len(expanded_all), len(result), intent,
    )
    return result


# ═══════════════════════════════════════════════════════════════
# 5. 统一检索入口
# ═══════════════════════════════════════════════════════════════

def retrieve_and_enrich(
    query: str,
    *,
    top_k: int = 7,
    user_dept: Optional[str] = None,
    stitch_window: int = 1,
) -> List[Dict[str, Any]]:
    """统一检索 + 后处理入口，供 API 和 DingTalk 共用。

    流程：
      1. search_chunks: 三路混合检索（Dense + Sparse + BM25）+ 封面降权
      2. stitch_neighbor_chunks: 邻居拼接解决 chunk 边界断裂

    参数选择依据（数据驱动）：
      - top_k=7 + window=1: 估算 context ~5,700 chars ≤ max_context_chars=6,000
      - 避免 top_k 过大导致 context 溢出后被 _format_context 截断浪费
      - window=1 已验证: CC +3.1pp, AC +2.1pp, 退化率 0%

    Args:
        query: 用户查询文本
        top_k: 检索返回的 chunk 数量
        user_dept: 用户部门（用于权限过滤）
        stitch_window: 邻居拼接窗口大小（±N）

    Returns:
        经过检索 + 邻居拼接后的 chunks 列表
    """
    chunks = search_chunks(query, top_k=top_k, user_dept=user_dept)
    if chunks and stitch_window > 0:
        chunks = stitch_neighbor_chunks(chunks, window=stitch_window)
    # Step Card 上下文扩展
    if chunks:
        chunks = expand_step_context(chunks, query)
    return chunks
