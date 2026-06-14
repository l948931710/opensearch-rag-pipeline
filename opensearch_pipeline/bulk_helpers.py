# -*- coding: utf-8 -*-
"""bulk_helpers.py — OpenSearch bulk action NDJSON 序列化(生产 + 评测共享).

为什么独立模块:
  生产 ingestion (pipeline_nodes.node_build_opensearch_payload) 与评测 A/B 框架
  (scratch/local_chunker_ab_ingest, eval_harness/chunker_ab_worker) 都需要把
  chunks 序列化为 OpenSearch bulk NDJSON 格式 + 按 max_bulk_size_bytes 贪心切分.
  抽出共享 helper 避免序列化逻辑漂移 (生产改了 doc 字段评测不同步会失真).

接口:
  build_opensearch_bulk_actions(chunks, *, max_bulk_size_bytes=1_500_000)
    → List[Dict[str, Any]]  每个 dict 是 {chunks, payload, payload_size}

调用方:
  - pipeline_nodes.node_build_opensearch_payload (生产 ingestion DAG-3)
  - scratch/local_chunker_ab_ingest.py (本地 Tier 2)
  - scratch/staging_chunker_ab_ingest.py (staging Tier 3)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List


def build_opensearch_bulk_actions(
    chunks: List[Any],
    *,
    max_bulk_size_bytes: int = 1_500_000,
) -> List[Dict[str, Any]]:
    """把 chunks 序列化为 OpenSearch bulk NDJSON 批次.

    每 chunk 产 2 行 NDJSON:action line ({"index":{"_id": chunk_id}}) + doc line
    (chunk.to_opensearch_doc()). 按 max_bulk_size_bytes 贪心切分,确保单批不
    超过 OpenSearch bulk API 上限(默认 100MB,实际取 1.5MB 安全余量).

    Args:
        chunks: 含 chunk_id + to_opensearch_doc() 的对象(Chunk dataclass 或兼容)
        max_bulk_size_bytes: 单批 payload 字节上限(默认 1.5MB,留生产安全余量)

    Returns:
        List of batches,每批含:
          - "chunks": List[Chunk]  本批 chunks
          - "payload": str          NDJSON 字符串(action + doc 行交替)
          - "payload_size": int     payload UTF-8 字节数

    Notes:
        - ensure_ascii=False:中文 chunk_text 保留原文,节省字节(prod 配置一致)
        - 不做 embedding 校验:embedding 失败的 chunk 由上层过滤(node_build_*)
        - 序列化逻辑与生产 ingestion 完全一致(共享单一来源)
    """
    batches: List[Dict[str, Any]] = []
    current_chunks: List[Any] = []
    current_lines: List[str] = []
    current_size = 0

    for chunk in chunks:
        action = {"index": {"_id": chunk.chunk_id}}
        doc = chunk.to_opensearch_doc()
        action_line = json.dumps(action, ensure_ascii=False)
        doc_line = json.dumps(doc, ensure_ascii=False)
        chunk_payload = f"{action_line}\n{doc_line}\n"
        chunk_size = len(chunk_payload.encode("utf-8"))

        if current_size > 0 and current_size + chunk_size > max_bulk_size_bytes:
            payload = "".join(current_lines)
            batches.append({
                "chunks": current_chunks,
                "payload": payload,
                "payload_size": len(payload.encode("utf-8")),
            })
            current_chunks = []
            current_lines = []
            current_size = 0

        current_chunks.append(chunk)
        current_lines.append(chunk_payload)
        current_size += chunk_size

    if current_chunks:
        payload = "".join(current_lines)
        batches.append({
            "chunks": current_chunks,
            "payload": payload,
            "payload_size": len(payload.encode("utf-8")),
        })

    return batches
