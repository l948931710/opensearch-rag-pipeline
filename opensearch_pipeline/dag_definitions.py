# -*- coding: utf-8 -*-
"""
dag_definitions.py — 四条 DAG 定义

DAG 1: raw_to_canonical        — 文件解析
DAG 2: canonical_to_safe_chunk — 分类 + 脱敏 + 发布 + 切分 + chunk_meta
DAG 3: chunk_to_opensearch     — embedding + 索引写入 + 旧版停用
DAG 4: retrieval_eval          — 检索评测
"""

from opensearch_pipeline.dag_engine import DAG, DAGNode
from opensearch_pipeline.pipeline_nodes import (
    # DAG 1
    node_scan_raw_files,
    node_register_metadata,
    node_extract_text_with_ocr,
    node_build_canonical,
    # DAG 2
    node_classify_and_risk_assess,
    node_detect_sensitive,
    node_redact_or_quarantine,
    node_publish_to_rag_ready,
    node_chunk_documents,
    node_validate_chunks,
    node_write_chunk_meta,
    # DAG 3
    node_acquire_index_lock,
    node_generate_embeddings,
    node_build_opensearch_payload,
    node_push_to_opensearch,
    node_update_index_status,
    node_deactivate_old_chunks,
    # DAG 4
    node_simulate_retrieval,
    node_eval_report,
)


def build_dag1_raw_to_canonical() -> DAG:
    """
    DAG 1: raw -> canonical document

    raw -> scan -> [register, extract+OCR] -> canonical
    """
    dag = DAG(
        dag_id="dag1_raw_to_canonical",
        name="Raw -> Canonical Document",
        description="scan, extract, OCR fallback, build canonical",
    )

    dag.add_node(DAGNode(
        "01", "Scan Raw Files",
        node_scan_raw_files,
        description="scan OSS raw/ for pending files",
    ))
    dag.add_node(DAGNode(
        "02", "Register Metadata",
        node_register_metadata,
        depends_on=["01"],
        description="write document_meta + document_version to RDS",
    ))
    dag.add_node(DAGNode(
        "03", "Extract Text + OCR Fallback",
        node_extract_text_with_ocr,
        depends_on=["01"],
        description="native extract -> OCR fallback if text insufficient",
    ))
    dag.add_node(DAGNode(
        "04", "Build Canonical Document",
        node_build_canonical,
        depends_on=["02", "03"],
        description="produce content.md + content.canonical.json",
    ))

    return dag


def build_dag2_canonical_to_chunk() -> DAG:
    """
    DAG 2: canonical -> safe chunks

    Safe ordering for version updates:
      classify → detect → redact → publish → chunk → validate → write_chunk_meta

    Key safety invariant:
      New chunks must be persisted (write_chunk_meta) and indexed in OpenSearch
      BEFORE old chunks are deactivated (which is handled downstream in DAG 3).
      If deactivation runs first and chunking/indexing later fails,
      the document "disappears" from search results.
    """
    dag = DAG(
        dag_id="dag2_canonical_to_chunk",
        name="Canonical -> Safe Chunks",
        description="classify, redact, publish, chunk, validate, and persist new chunks",
    )

    dag.add_node(DAGNode(
        "01", "Classify + Risk Assess (LLM)",
        node_classify_and_risk_assess,
        description="LLM: category, permission, risk on ORIGINAL text",
    ))
    dag.add_node(DAGNode(
        "02", "Detect Sensitive Entities",
        node_detect_sensitive,
        depends_on=["01"],
        description="regex PII/credential detection, merge with LLM risk",
    ))
    dag.add_node(DAGNode(
        "03", "Redact or Quarantine",
        node_redact_or_quarantine,
        depends_on=["02"],
        description="high->quarantine, medium->redact, low->pass",
    ))
    dag.add_node(DAGNode(
        "04", "Publish to rag-ready/",
        node_publish_to_rag_ready,
        depends_on=["03"],
        description="write content.md + metadata.json to rag-ready/",
    ))
    dag.add_node(DAGNode(
        "05", "Chunk Documents",
        node_chunk_documents,
        depends_on=["04"],
        description="section/paragraph/table-aware chunking from blocks",
    ))
    dag.add_node(DAGNode(
        "06", "Validate Chunks",
        node_validate_chunks,
        depends_on=["05"],
        description="check empty, too long, missing metadata",
    ))
    dag.add_node(DAGNode(
        "07", "Write chunk_meta to RDS",
        node_write_chunk_meta,
        depends_on=["06"],
        description="persist new chunks to RDS",
    ))

    return dag


def build_dag3_chunk_to_opensearch() -> DAG:
    """
    DAG 3: chunks -> embedding -> OpenSearch -> deactivate old

    embedding -> bulk payload -> push -> status update -> deactivate old version chunks
    """
    dag = DAG(
        dag_id="dag3_chunk_to_opensearch",
        name="Chunks -> OpenSearch Index",
        description="生成 embedding 并写入 OpenSearch",
    )

    dag.add_node(DAGNode(
        "00", "抢占索引锁",
        node_acquire_index_lock,
        description="乐观锁：在开始处理之前抢占索引锁定，防止并发冲突",
    ))
    dag.add_node(DAGNode(
        "01", "生成 Embedding",
        node_generate_embeddings,
        depends_on=["00"],
        description="调用 embedding 模型生成向量",
    ))
    dag.add_node(DAGNode(
        "02", "构建 Bulk Payload",
        node_build_opensearch_payload,
        depends_on=["01"],
        description="组装 OpenSearch NDJSON 格式",
    ))
    dag.add_node(DAGNode(
        "03", "推送到 OpenSearch",
        node_push_to_opensearch,
        depends_on=["02"],
        description="批量写入 OpenSearch 索引",
    ))
    dag.add_node(DAGNode(
        "04", "回写索引状态",
        node_update_index_status,
        depends_on=["03"],
        description="更新 chunk_meta.index_status 等字段，索引失败时中断 DAG 防止停用旧版本",
    ))
    dag.add_node(DAGNode(
        "05", "停用旧版本",
        node_deactivate_old_chunks,
        depends_on=["04"],
        description="在新版本索引确认成功后，停用 RDS 和 OpenSearch 中的旧版本 chunk",
    ))

    return dag


def build_dag4_retrieval_eval() -> DAG:
    """
    DAG 4: retrieval eval

    模拟检索 → 评测报告
    """
    dag = DAG(
        dag_id="dag4_retrieval_eval",
        name="Retrieval Evaluation",
        description="检索质量评测",
    )

    dag.add_node(DAGNode(
        "01", "模拟检索测试",
        node_simulate_retrieval,
        description="用测试 query 检索已索引 chunks",
    ))
    dag.add_node(DAGNode(
        "02", "生成评测报告",
        node_eval_report,
        depends_on=["01"],
        description="统计命中率、chunk 分布等指标",
    ))

    return dag


def build_full_pipeline() -> list:
    """返回完整的四条 DAG。"""
    return [
        build_dag1_raw_to_canonical(),
        build_dag2_canonical_to_chunk(),
        build_dag3_chunk_to_opensearch(),
        build_dag4_retrieval_eval(),
    ]
