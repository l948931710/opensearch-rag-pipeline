# -*- coding: utf-8 -*-
"""
retriever.py — 检索模块

封装 DashScope Embedding + OpenSearch HA3 向量检索，为 RAG 问答提供上下文。
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

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

    # 与入库侧共用加固实现（URL 去重 + 429/5xx 重试 + 退避）。查询侧 sparse_fallback=False：
    # 空 sparse 表示该查询不参与 sparse 匹配，比塞入 [0]/[0.001] 假项更准确。
    from opensearch_pipeline.embedding_client import embed_texts_native

    results = embed_texts_native(
        [query],
        api_key=api_key or config.embedding.api_key,
        model=model or config.embedding.model,
        dimension=dimension or config.embedding.dimension,
        api_base_url=config.embedding.api_base_url,
        max_retries=getattr(config.embedding, "max_retries", 2),
        request_timeout=30,
        sparse_fallback=False,
        label="query embedding",
    )
    r = results[0] if results else None
    if r is None:
        raise RuntimeError("DashScope 未返回 query embedding")
    dense, sparse_indices, sparse_values = r

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


# HA3 查询统一返回字段（search_chunks / cosurface_doc_images / 文档展开共用，避免漂移）
_DEFAULT_OUTPUT_FIELDS = [
    "id", "chunk_id", "doc_id", "chunk_text_store", "title", "section_title",
    "category_l1", "chunk_index", "page_num", "kb_type",
    "permission_level", "owner_dept", "chunk_type",
    "source_image", "visual_summary",
]


def _build_permission_filter(user_dept: Optional[str]) -> str:
    """构建 HA3 权限过滤表达式（安全边界，单一实现）。

    放行 public；有部门时额外放行该部门的 dept_internal。部门值经 _sanitize_ha3_filter_value
    白名单净化，防止 filter 注入。多个调用点（search_chunks / cosurface_doc_images）共用，
    确保权限规则只有一处、不会某处改了另一处漏改而越权或漏召回。
    """
    if user_dept:
        safe_dept = _sanitize_ha3_filter_value(user_dept)
        return (
            'permission_level="public"'
            ' OR (permission_level="dept_internal" AND owner_dept="' + safe_dept + '")'
        )
    return 'permission_level="public"'


def search_chunks(
    query: str,
    *,
    top_k: int = 5,
    min_score: float = 0.0,
    max_distance: float = 0.0,
    output_fields: Optional[List[str]] = None,
    user_dept: Optional[str] = None,
    query_embedding: Optional[Tuple[List[float], List[int], List[float]]] = None,
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

    # 1. 生成 query embedding（retrieve_and_enrich 会预算一次并传入，避免重复嵌入）
    dense, sparse_idx, sparse_val = query_embedding if query_embedding is not None else get_query_embedding(query)

    # 2. 构建 sparse data
    from alibabacloud_ha3engine_vector.models import QueryRequest, SparseData

    sparse_data = None
    if sparse_idx:
        sparse_data = SparseData(
            count=[len(sparse_idx)],
            indices=sparse_idx,
            values=sparse_val,
        )

    _output_fields = output_fields or list(_DEFAULT_OUTPUT_FIELDS)

    # ── 权限过滤（安全边界，统一实现）──
    filter_expr = _build_permission_filter(user_dept)

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

        _output_fields = list(_DEFAULT_OUTPUT_FIELDS)

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
        # 单次批量查询所有命中 chunk 的 ±window 邻居（消除 N+1：原先每个 hit 一次 RDS 往返）。
        # 第一遍：分流 pass-through（无 doc_id / step·proc·visual 语义单元）与待拼接 chunk，
        # 用占位符保留输出顺序，并按 (doc_id, center_idx) 去重（重复中心整条丢弃，与原行为一致）。
        expanded: List[Optional[Dict[str, Any]]] = []
        seen_centers = set()
        pending = []          # (slot, chunk, doc_id, center_idx)
        ranges = []           # (doc_id, lo, hi)

        for chunk in chunks:
            doc_id = chunk.get("doc_id", "")
            center_idx = chunk.get("chunk_index", 0)
            chunk_type = chunk.get("chunk_type", "")

            if not doc_id or chunk_type in ("step_card", "procedure_parent", "visual_knowledge"):
                expanded.append(chunk)
                continue

            center_key = (doc_id, center_idx)
            if center_key in seen_centers:
                continue  # 重复中心：丢弃（hit A 的邻居恰是 hit B 的中心）
            seen_centers.add(center_key)

            slot = len(expanded)
            expanded.append(None)  # 占位，批量查询后回填
            pending.append((slot, chunk, doc_id, center_idx))
            ranges.append((doc_id, center_idx - window, center_idx + window))

        # 只有存在待拼接 chunk 时才连库
        neighbors_by_doc: Dict[str, Dict[int, Dict[str, Any]]] = {}
        if pending:
            import pymysql.cursors
            from opensearch_pipeline.pipeline_nodes import _get_db_conn

            conn = _get_db_conn()
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            try:
                where_parts = []
                params: List[Any] = []
                for doc_id, lo, hi in ranges:
                    where_parts.append("(doc_id = %s AND chunk_index BETWEEN %s AND %s)")
                    params.extend([doc_id, lo, hi])
                cursor.execute(
                    "SELECT doc_id, chunk_index, chunk_text, section_title "
                    "FROM chunk_meta WHERE is_active = 1 AND (" + " OR ".join(where_parts) + ")",
                    tuple(params),
                )
                for row in cursor.fetchall():
                    neighbors_by_doc.setdefault(row["doc_id"], {})[row["chunk_index"]] = row
            finally:
                cursor.close()
                conn.close()

        # 第二遍：回填每个待拼接 chunk 的拼接文本（按 chunk_index 升序，含中心本身）
        for slot, chunk, doc_id, center_idx in pending:
            doc_neighbors = neighbors_by_doc.get(doc_id, {})
            lo, hi = center_idx - window, center_idx + window
            neighbor_rows = [doc_neighbors[i] for i in sorted(doc_neighbors) if lo <= i <= hi]
            if neighbor_rows:
                stitched_text = "\n".join(nb["chunk_text"] or "" for nb in neighbor_rows)
            else:
                stitched_text = chunk.get("chunk_text", "")
            expanded_chunk = dict(chunk)
            expanded_chunk["chunk_text"] = stitched_text
            expanded_chunk["_stitched"] = True
            expanded_chunk["_stitch_window"] = window
            expanded_chunk["_neighbor_count"] = len(neighbor_rows)
            expanded[slot] = expanded_chunk

        logger.info(
            "邻居扩展完成: %d chunks → %d expanded (去重 %d), window=±%d, RDS 往返=%d",
            len(chunks), len(expanded), len(chunks) - len(expanded), window,
            1 if pending else 0,
        )
        return expanded

    except Exception as e:
        logger.warning("邻居扩展失败，回退到原始结果: %s", e, exc_info=True)
        return chunks


# ═══════════════════════════════════════════════════════════════
# 4.5 Step Card 上下文扩展
# ═══════════════════════════════════════════════════════════════

def _normalize_image_refs(image_refs_json) -> List[Dict[str, Any]]:
    """把 RDS image_refs_json 归一化为统一的 image_refs 列表（单一实现，消除三份漂移）。

    保留 CLAUDE.md 标注的载荷契约键 oss_key/source_image/visual_summary/ocr_text/caption/
    order/image_index，互相兜底（oss_key↔source_image）。下游 content_blocks_builder 读
    ``oss_key or source_image`` 与 ``caption or visual_summary or ocr_text``，因此键越全越好；
    原先三个分支各自只发部分键（两处丢 visual_summary、一处丢 ocr_text），导致 XLSX 绑定的
    图注（存在 visual_summary）渲染不出来。

    入参可为 JSON 字符串或已解析的 list。
    """
    raw: list = []
    if image_refs_json:
        if isinstance(image_refs_json, str):
            try:
                raw = json.loads(image_refs_json)
            except (json.JSONDecodeError, TypeError):
                raw = []
        elif isinstance(image_refs_json, list):
            raw = image_refs_json
    out: List[Dict[str, Any]] = []
    for idx, ref in enumerate(raw):
        if not isinstance(ref, dict):
            continue
        oss_key = ref.get("oss_key") or ref.get("source_image", "")
        out.append({
            "oss_key": oss_key,
            "source_image": ref.get("source_image") or oss_key,
            "visual_summary": ref.get("visual_summary", ""),
            "ocr_text": ref.get("ocr_text", ""),
            "caption": ref.get("caption", ""),
            "order": ref.get("order", idx),
            "image_index": ref.get("image_index", idx),
        })
    return out


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
        # ── A2: 批量预取，消除 N+1（原先每个 step_card 2 次、每个 visual_knowledge 1 次往返）──
        step_card_ids = [
            cid for cid in (
                (c.get("chunk_id") or c.get("id", ""))
                for c in chunks if c.get("chunk_type") == "step_card"
            ) if cid
        ]
        vk_ids = [
            cid for cid in (
                (c.get("chunk_id") or c.get("id", ""))
                for c in chunks
                if c.get("chunk_type") == "visual_knowledge" and not c.get("image_refs")
            ) if cid
        ]

        # 1) step_card 元数据：parent_chunk_id / step_no / extra_json / image_refs_json
        meta_by_id: Dict[str, Dict[str, Any]] = {}
        if step_card_ids:
            ph = ",".join(["%s"] * len(step_card_ids))
            cursor.execute(
                "SELECT chunk_id, parent_chunk_id, step_no, extra_json, image_refs_json "
                f"FROM chunk_meta WHERE chunk_id IN ({ph})",
                tuple(step_card_ids),
            )
            for row in cursor.fetchall():
                meta_by_id[row["chunk_id"]] = row

        # 2) 所有相关 parent 的兄弟/子步骤（一次取齐，按 parent 分组、组内按 step_no 升序）。
        # procedure_parent 命中也并入：其子步骤靠 RDS 的 parent_chunk_id 反查——
        # 旧实现读 chunk["extra_json"].child_chunk_ids，但 HA3 的 output_fields 不含
        # extra_json，child_ids 永远为空（死分支），子步骤及其图片从未被展开过。
        procedure_parent_ids = {
            cid for cid in (
                (c.get("chunk_id") or c.get("id", ""))
                for c in chunks if c.get("chunk_type") == "procedure_parent"
            ) if cid
        }
        parent_ids = sorted(
            {r["parent_chunk_id"] for r in meta_by_id.values() if r.get("parent_chunk_id")}
            | procedure_parent_ids
        )
        siblings_by_parent: Dict[str, List[Dict[str, Any]]] = {}
        if parent_ids:
            ph = ",".join(["%s"] * len(parent_ids))
            cursor.execute(
                "SELECT chunk_id, chunk_text, step_no, section_title, "
                "       extra_json, image_refs_json, parent_chunk_id "
                f"FROM chunk_meta WHERE parent_chunk_id IN ({ph}) AND is_active = 1 "
                "ORDER BY step_no",
                tuple(parent_ids),
            )
            for row in cursor.fetchall():
                siblings_by_parent.setdefault(row["parent_chunk_id"], []).append(row)

        # 3) visual_knowledge 的 image_refs_json（一次取齐）
        vk_refs_by_id: Dict[str, Any] = {}
        if vk_ids:
            ph = ",".join(["%s"] * len(vk_ids))
            cursor.execute(
                f"SELECT chunk_id, image_refs_json FROM chunk_meta WHERE chunk_id IN ({ph})",
                tuple(vk_ids),
            )
            for row in cursor.fetchall():
                vk_refs_by_id[row["chunk_id"]] = row.get("image_refs_json")

        expanded_all: List[Dict[str, Any]] = []

        for chunk in chunks:
            ctype = chunk.get("chunk_type", "")

            # ── step_card：查 RDS 获取 parent_chunk_id / step_no，再展开兄弟 ──
            if ctype == "step_card":
                chunk_id = chunk.get("chunk_id") or chunk.get("id", "")
                original_score = chunk.get("score", 0)

                meta_row = meta_by_id.get(chunk_id)
                if not meta_row or not meta_row.get("parent_chunk_id"):
                    # C2：无 procedure_parent（如 XLSX procedure_image_guide）。RDS 里已绑定的
                    # image_refs（HA3 不返回）必须在此附上，否则该 step_card 的图片永远到不了答案。
                    if meta_row and not chunk.get("image_refs"):
                        refs = _normalize_image_refs(meta_row.get("image_refs_json"))
                        if refs:
                            chunk = dict(chunk)
                            chunk["image_refs"] = refs
                    expanded_all.append(chunk)
                    continue

                parent_id = meta_row["parent_chunk_id"]
                hit_step_no = meta_row.get("step_no") or 0
                siblings = siblings_by_parent.get(parent_id, [])

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

                    expanded_chunk = dict(chunk)  # 继承原始 hit 的 metadata
                    expanded_chunk.update({
                        "chunk_id": sib["chunk_id"],
                        "chunk_text": sib.get("chunk_text", ""),
                        "step_no": sib.get("step_no"),
                        "section_title": sib.get("section_title", ""),
                        "parent_chunk_id": parent_id,
                        "score": score,
                        "image_refs": _normalize_image_refs(sib.get("image_refs_json")),
                        "annotation_map": extra.get("annotation_map", {}),
                        "is_expanded": not is_hit,
                        "expanded_from": chunk_id if not is_hit else None,
                        "expansion_reason": "sibling_step" if not is_hit else None,
                    })
                    expanded_all.append(expanded_chunk)

            # ── procedure_parent：展开子步骤（按 RDS parent_chunk_id 反查，已随兄弟查询预取）──
            elif ctype == "procedure_parent":
                original_score = chunk.get("score", 0)
                parent_chunk_id = chunk.get("chunk_id") or chunk.get("id", "")

                children = siblings_by_parent.get(parent_chunk_id, [])
                total_children = len(children)

                if not children:
                    expanded_all.append(chunk)
                    continue

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

                for child in children:
                    child_extra = {}
                    if child.get("extra_json"):
                        try:
                            child_extra = json.loads(child["extra_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass

                    expanded_chunk = dict(chunk)
                    expanded_chunk.update({
                        "chunk_id": child["chunk_id"],
                        "chunk_text": child.get("chunk_text", ""),
                        "step_no": child.get("step_no"),
                        "section_title": child.get("section_title", ""),
                        "parent_chunk_id": parent_chunk_id,
                        "score": original_score * 0.8,
                        "image_refs": _normalize_image_refs(child.get("image_refs_json")),
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
                # image_refs 时用预取的 RDS 全量补齐，失败/无记录则保留 source_image 首图兜底。
                if chunk_id and not chunk.get("image_refs"):
                    refs = _normalize_image_refs(vk_refs_by_id.get(chunk_id))
                    refs = [r for r in refs if r["oss_key"]]  # 保留原 vk 语义：无图源的 ref 丢弃
                    if refs:
                        enriched["image_refs"] = refs
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

def cosurface_doc_images(
    query: str,
    chunks: List[Dict[str, Any]],
    *,
    user_dept: Optional[str] = None,
    max_docs: int = 3,
    max_images: int = 3,
    query_embedding: Optional[Tuple[List[float], List[int], List[float]]] = None,
) -> List[Dict[str, Any]]:
    """图片召回增强：为已检索高分文档补充其最相关的 image chunk。

    背景：图片以独立 ``chunk_type="image"`` chunk 存在，与正文 chunk 在同一向量排序中
    竞争。文本类 / 流程类查询往往让正文挤掉同文档的图片，导致答案缺图（即便文档其实有图）。

    做法：对 top 文档做一次按 ``doc_id + chunk_type="image"`` 过滤的 kNN 查询，取与 query
    最相关的图片，并**插入到其同文档正文 chunk 之后**（而非追加到末尾）—— 这样 ``<<IMG:N>>``
    提示不会被 ``_format_context`` 的 ``max_context_chars`` 截断，LLM 才能引用到正确序号，
    ``content_blocks_builder`` 才能绑定。``source_image`` 仅存于 HA3（不在 RDS chunk_meta），
    故必须走 HA3 查询。

    - 结果已含 image chunk（如可视化查询）→ 原样返回，不打扰既有多模态路径。
    - 任何异常都 fail-open 返回原 chunks，绝不影响回答（与本模块整体降级风格一致）。

    Args:
        query: 用户查询文本（用于按相关度挑图）
        chunks: 已检索 + 拼接后的 chunk 列表
        user_dept: 用户部门（沿用权限过滤）
        max_docs: 最多为前 N 个文档补图
        max_images: 最多补充的图片总数

    Returns:
        在同文档正文之后插入了 image chunk 的新列表（原文本 chunk 顺序不变）。
    """
    if not chunks:
        return chunks
    # 已经有图片 chunk（可视化查询）→ 不重复补充
    if any(c.get("chunk_type") == "image" for c in chunks):
        return chunks

    # top 文档（按结果顺序 = 相关度）
    doc_ids: List[str] = []
    for c in chunks:
        d = c.get("doc_id")
        if d and d not in doc_ids:
            doc_ids.append(d)
        if len(doc_ids) >= max_docs:
            break
    if not doc_ids:
        return chunks

    try:
        cfg = get_config().alibaba_vector
        from alibabacloud_ha3engine_vector.models import QueryRequest, SparseData

        dense, sparse_idx, sparse_val = (
            query_embedding if query_embedding is not None else get_query_embedding(query)
        )
        sparse_data = (
            SparseData(count=[len(sparse_idx)], indices=sparse_idx, values=sparse_val)
            if sparse_idx else None
        )

        doc_clause = " OR ".join(
            f'doc_id="{_sanitize_ha3_filter_value(d)}"' for d in doc_ids
        )
        # 权限子句与 search_chunks 共用同一实现（安全边界单一来源）
        perm = _build_permission_filter(user_dept)
        filter_expr = f'chunk_type="image" AND ({doc_clause}) AND ({perm})'

        _output_fields = list(_DEFAULT_OUTPUT_FIELDS)
        req = QueryRequest(
            table_name=cfg.table_name,
            vector=dense,
            sparse_data=sparse_data,
            top_k=max_images * 2,
            include_vector=False,
            output_fields=_output_fields,
            filter=filter_expr,
        )
        img_results = _parse_ha3_response(_get_ha3_client().query(req))
    except Exception as e:
        logger.warning("图片召回补充失败 (non-fatal): %s", e)
        return chunks

    # 每个文档取最相关（首个）的有效图片
    best_by_doc: Dict[str, Dict[str, Any]] = {}
    for r in img_results:
        if r.get("chunk_type") != "image" or not r.get("source_image"):
            continue
        d = r.get("doc_id")
        if d and d not in best_by_doc:
            best_by_doc[d] = r
    if not best_by_doc:
        return chunks

    # 插入到同文档首个正文 chunk 之后；总量 ≤ max_images
    out: List[Dict[str, Any]] = []
    used_docs: set = set()
    for c in chunks:
        out.append(c)
        d = c.get("doc_id")
        if d in best_by_doc and d not in used_docs and len(used_docs) < max_images:
            out.append(best_by_doc[d])
            used_docs.add(d)

    if used_docs:
        logger.info("图片召回补充: 为 %d 个文档插入 image chunk（共 %d 候选文档）",
                    len(used_docs), len(doc_ids))
    return out


def retrieve_and_enrich(
    query: str,
    *,
    top_k: int = 7,
    user_dept: Optional[str] = None,
    stitch_window: int = 1,
    cosurface_images: bool = False,
) -> List[Dict[str, Any]]:
    """统一检索 + 后处理入口，供 API 和 DingTalk 共用。

    流程：
      1. search_chunks: 三路混合检索（Dense + Sparse + BM25）+ 封面降权
      2. stitch_neighbor_chunks: 邻居拼接解决 chunk 边界断裂
      3. expand_step_context: step card 上下文扩展
      4. cosurface_doc_images: 图片召回增强（仅多模态渲染路径 opt-in）

    参数选择依据（数据驱动）：
      - top_k=7 + window=1: 估算 context ~5,700 chars ≤ max_context_chars=6,000
      - 避免 top_k 过大导致 context 溢出后被 _format_context 截断浪费
      - window=1 已验证: CC +3.1pp, AC +2.1pp, 退化率 0%

    Args:
        query: 用户查询文本
        top_k: 检索返回的 chunk 数量
        user_dept: 用户部门（用于权限过滤）
        stitch_window: 邻居拼接窗口大小（±N）
        cosurface_images: 是否为高分文档补充 image chunk（图文渲染路径传 True；
            纯文本路径 / 不展示图片的 /api/ask 保持 False，避免无谓的 HA3 查询）。
            另受全局开关 RAG_IMAGE_COSURFACE 控制。

    Returns:
        经过检索 + 邻居拼接（+ 可选图片召回）后的 chunks 列表
    """
    # 路由式重排开启时：over-fetch rerank_pool 个候选 → 重排 → 取 top_k；否则直接取 top_k。
    _av = get_config().alibaba_vector
    _fetch_k = max(_av.rerank_pool, top_k) if _av.rerank_enable else top_k
    # query embedding 只算一次，传给 search_chunks 与 cosurface（后者原本会重复嵌入一次）
    _emb = get_query_embedding(query)
    chunks = search_chunks(query, top_k=_fetch_k, user_dept=user_dept, query_embedding=_emb)
    if _av.rerank_enable and chunks:
        from .reranker import rerank_chunks
        # multimodal 渲染路径（cosurface_images=True）用 VL 重排；纯文本/钉钉机器人用文本重排。
        chunks = rerank_chunks(query, chunks, top_k=top_k,
                               multimodal=bool(cosurface_images))  # 失败自动降级为原始顺序
    if chunks and stitch_window > 0:
        chunks = stitch_neighbor_chunks(chunks, window=stitch_window)
    # Step Card 上下文扩展
    if chunks:
        chunks = expand_step_context(chunks, query)
    # 图片召回增强（仅多模态渲染路径 opt-in；可经 RAG_IMAGE_COSURFACE 全局关闭）
    if chunks and cosurface_images and get_config().rag.image_cosurface:
        chunks = cosurface_doc_images(query, chunks, user_dept=user_dept, query_embedding=_emb)
    return chunks
