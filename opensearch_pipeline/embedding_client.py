# -*- coding: utf-8 -*-
"""
embedding_client.py — DashScope 原生 dense+sparse text-embedding HTTP 客户端（共享）

查询侧（retriever.get_query_embedding）与入库侧（pipeline_nodes.node_generate_embeddings）
原本各有一份调用代码且已经漂移：查询侧缺少 /api/v1 去重、重试与 sparse 兜底，入库侧有。
这里统一为一个加固实现，两边复用：
  - URL 幂等去重 /api/v1（唯一返回 sparse 的端点）
  - 瞬时重试统一走 vlm_retry.post_json_with_retry（429+全部 5xx、数值 Retry-After、
    指数退避 1s→2s→4s）；4xx(≠429) 立即失败；重试耗尽抛出
  - sparse_fallback：入库 True（无 sparse 用 [0]/[0.001]，避免 HA3 把文档排除在索引外）；
    查询 False（空 sparse = 该查询不参与 sparse 匹配，更准确）

必须用 native API 而非 compatible-mode：compatible-mode 不返回 sparse embedding，
会让三路混合检索退化为纯 dense，严重影响召回。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

import requests

from opensearch_pipeline.http_session import http_post as _http_post
from opensearch_pipeline.vlm_retry import post_json_with_retry

logger = logging.getLogger(__name__)

# (dense_vector, sparse_indices, sparse_values)
EmbeddingResult = Tuple[List[float], List[int], List[float]]


def build_native_embedding_url(api_base_url: str) -> str:
    """DashScope 原生 text-embedding 端点（唯一支持 sparse），对 /api/v1 幂等。"""
    base = (api_base_url or "").rstrip("/")
    if "/api/v1" in base:
        return f"{base}/services/embeddings/text-embedding/text-embedding"
    return f"{base}/api/v1/services/embeddings/text-embedding/text-embedding"


def _parse_sparse(sparse_list, *, fallback: bool) -> Tuple[List[int], List[float]]:
    """native API 的 sparse_embedding 是 list[dict]（index/token/value），按 index 升序展开。"""
    if sparse_list:
        ordered = sorted(sparse_list, key=lambda x: x["index"])
        return [s["index"] for s in ordered], [float(s["value"]) for s in ordered]
    if fallback:
        # 保底 sparse，避免 HA3 索引把无 sparse 的文档排除（仅入库需要）
        return [0], [0.001]
    return [], []


def embed_texts_native(
    texts: List[str],
    *,
    api_key: str,
    model: str,
    dimension: int,
    api_base_url: str,
    max_retries: int = 2,
    request_timeout: int = 30,
    sparse_fallback: bool = False,
    label: str = "embedding",
) -> List[Optional[EmbeddingResult]]:
    """批量生成 dense+sparse embedding，返回与输入对齐的结果列表。

    返回值长度 == len(texts)，按响应里的 text_index 对齐填充；未返回的槽位为 None
    （调用方据此跳过——与原入库逻辑一致：未返回的 chunk 既不标 DONE 也不标 FAILED）。

    失败（重试耗尽 / 400/401/403 / 网络错误）抛出最后一个异常，由调用方决定如何处理
    （查询侧让其冒泡 → 接口 500；入库侧 catch → 整批标 FAILED）。
    """
    if not texts:
        return []
    if not api_key:
        raise RuntimeError("DashScope API Key 未配置，无法生成 embedding")

    url = build_native_embedding_url(api_base_url)
    payload = {
        "model": model,
        "input": {"texts": list(texts)},
        "parameters": {"dimension": dimension, "output_type": "dense&sparse"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # 瞬时重试（429+全部 5xx+网络异常、数值 Retry-After、退避 1s→2s→4s 同旧 2**attempt）
    # 收敛到共享策略 post_json_with_retry。post_fn 传本模块的 _http_post：既保留
    # http_session 连接池，也保留 tests 的 `embedding_client._http_post` patch 接缝
    # （该名字在此处每次调用时才解析）。sleep_fn 同理走本模块的 time.sleep 接缝。
    resp = post_json_with_retry(
        url,
        json=payload,
        headers=headers,
        timeout=request_timeout,
        max_retries=max_retries,
        base_backoff=1.0,
        sleep_fn=time.sleep,
        label=label,
        post_fn=_http_post,
    )
    try:
        # 4xx(≠429) 与重试耗尽后的 429/5xx 在此抛 HTTPError（重试期间的瞬时错误已在
        # post_json_with_retry 内消化）
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if getattr(e.response, "status_code", None) == 400:
            logger.warning("%s HTTP 400 (non-retryable): %s",
                           label, getattr(e.response, "text", "")[:500])
        raise
    data = resp.json()
    items = data.get("output", {}).get("embeddings", [])
    results: List[Optional[EmbeddingResult]] = [None] * len(texts)
    for idx, item in enumerate(items):
        item_idx = item.get("text_index", idx)
        if 0 <= item_idx < len(texts):
            sidx, sval = _parse_sparse(item.get("sparse_embedding", []), fallback=sparse_fallback)
            results[item_idx] = (item["embedding"], sidx, sval)
    return results
