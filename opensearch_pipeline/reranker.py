# -*- coding: utf-8 -*-
"""
reranker.py — 路由式重排序（DashScope rerank），插入在混合检索与邻居拼接之间。

策略（数据驱动，见 eval_harness/reports/rerank_findings.md）：
  - 候选池含图片 → qwen3-vl-rerank（图文重排，附签名图片 URL）：图片类问题 nDCG@10 +6.6pp
  - 纯文本候选池 → qwen3-rerank（文本重排）：文本类问题 recall@1 +13.1pp
  - 路由整体：full-set recall@1 +10.5pp, nDCG@10 +7.9pp；重排分对正/负样本区分度
    （Youden J 0.46→0.61/0.66）远优于融合分。

设计原则（遵循本仓库的 graceful-degradation 约定）：任何失败都**返回原始 chunks**，
绝不因重排异常而破坏检索/回答主流程。重排只对 over-fetch 的候选池做一次 API 调用。
"""

import logging
from typing import Any, Dict, List, Optional


from opensearch_pipeline.http_session import http_post as _http_post

from .config import get_config

logger = logging.getLogger(__name__)

RERANK_URL_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"
_DOC_MAX_CHARS = 1200


def _img_key(chunk: Dict[str, Any]) -> Optional[str]:
    """取 chunk 的代表性图片 OSS key（优先 source_image，其次 image_refs[0]）。"""
    if chunk.get("source_image"):
        return chunk["source_image"]
    for ref in (chunk.get("image_refs") or []):
        if isinstance(ref, dict) and ref.get("oss_key"):
            return ref["oss_key"]
    return None


def _signed(oss_key: str, expires: int = 3600) -> str:
    # Sign fresh each call (local HMAC, no network) — caching would serve URLs that
    # expire after `expires` seconds and start 403-ing in a long-running server.
    if not oss_key:
        return ""
    try:
        from .oss_url import generate_signed_url
        return generate_signed_url(oss_key, expires=expires) or ""
    except Exception as e:
        logger.warning("rerank: sign url failed for %s: %s", oss_key[:60], e)
        return ""


def _doc_text(chunk: Dict[str, Any]) -> str:
    """每个 chunk 用于重排的文本：取最丰富的可用文本。

    图片 chunk 的 chunk_text 往往很薄（如「【文档:X】[图片描述]」），真正的语义内容在
    visual_summary / ocr_text 里——直接拼接，避免纯文本重排把图片 chunk 误降权。
    """
    parts = [chunk.get("chunk_text") or "", chunk.get("visual_summary") or "",
             chunk.get("ocr_text") or ""]
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)[:_DOC_MAX_CHARS]


def _build_documents(chunks: List[Dict[str, Any]], use_images: bool):
    """构建 rerank 的 documents；返回 (documents, n_images_attached)。"""
    docs: List[Any] = []
    n_img = 0
    for ch in chunks:
        text = _doc_text(ch)
        if use_images:
            url = _signed(_img_key(ch) or "")
            if url:
                docs.append({"text": text, "image_url": url})
                n_img += 1
            else:
                docs.append({"text": text})
        else:
            docs.append(text)
    return docs, n_img


def _call_rerank(query: str, documents: List[Any], model: str, api_key: str,
                 base_url: str, timeout: int) -> List[Dict[str, Any]]:
    url = base_url.rstrip("/") + RERANK_URL_PATH
    resp = _http_post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "input": {"query": query, "documents": documents},
              "parameters": {"return_documents": False, "top_n": len(documents)}},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["output"]["results"]  # [{index, relevance_score}, ...]


def rerank_chunks(
    query: str,
    chunks: List[Dict[str, Any]],
    *,
    top_k: Optional[int] = None,
    multimodal: bool = False,
) -> List[Dict[str, Any]]:
    """对候选 chunks 做路由式重排，返回重排后的前 top_k 个。

    路由（按服务模式，一个 query 用一个模型，保证分数可比）：
      - multimodal=True（图文渲染路径）→ qwen3-vl-rerank（整池图文重排，附 image_url）
      - multimodal=False（纯文本/钉钉机器人）→ qwen3-rerank（文本重排，doc 取最丰富文本）
        · 若 rerank_route_vl=True 且候选池含图片，纯文本路径也会路由到 VL（数据驱动开关）。

    成功时：按重排分降序重排，并在每个返回 chunk 上写入 ``rerank_score``，同时把
    ``score`` 切换为 rerank 分（原融合分保留在 ``_fused_score``），使下游标签/排序统一。
    失败/降级时：原样返回（截断到 top_k）。
    """
    cfg = get_config().alibaba_vector
    if not chunks or len(chunks) < 2:
        return chunks[: top_k] if top_k else chunks

    api_key = get_config().embedding.api_key
    if not api_key:
        logger.warning("rerank: no DashScope key; skipping rerank")
        return chunks[: top_k] if top_k else chunks

    base_url = get_config().embedding.api_base_url
    # native rerank endpoint lives at the DashScope root; strip a trailing /api/v1 if present
    if base_url.rstrip("/").endswith("/api/v1"):
        base_url = base_url.rstrip("/")[: -len("/api/v1")]

    # multimodal path always uses VL; pure-text path uses VL only if the opt-in flag is set
    # AND the pool actually has images (decided by the rerank A/B).
    use_images = bool(multimodal) or (bool(cfg.rerank_route_vl) and any(_img_key(c) for c in chunks))
    model = cfg.rerank_vl_model if use_images else cfg.rerank_text_model

    try:
        documents, n_img = _build_documents(chunks, use_images)
        results = _call_rerank(query, documents, model, api_key, base_url, cfg.rerank_timeout)
    except Exception as e:
        logger.warning("rerank failed (model=%s, fail-open): %s", model, e)
        return chunks[: top_k] if top_k else chunks

    # reorder by returned index; annotate scores
    reordered: List[Dict[str, Any]] = []
    for r in results:
        idx = r.get("index")
        if idx is None or not (0 <= idx < len(chunks)):
            continue
        ch = chunks[idx]
        ch["_fused_score"] = ch.get("score")
        ch["rerank_score"] = float(r.get("relevance_score", 0.0))
        ch["score"] = ch["rerank_score"]
        reordered.append(ch)

    if not reordered:  # defensive: nothing usable came back
        return chunks[: top_k] if top_k else chunks

    logger.info("rerank ok: model=%s, in=%d, images=%s, out=%d",
                model, len(chunks), n_img if use_images else 0, len(reordered))
    return reordered[: top_k] if top_k else reordered
