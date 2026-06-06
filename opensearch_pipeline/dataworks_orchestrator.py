# -*- coding: utf-8 -*-
"""
dataworks_orchestrator.py — DataWorks 调度执行主入口

配合 DataWorks 中的可视化调度节点，分别调度执行 3 个核心阶段：
  Stage 1: Raw -> Canonical Document (文件解析)
  Stage 2: Canonical -> Safe Chunks (分类 + 脱敏 + 切分 + chunk_meta)
  Stage 3: Chunks -> OpenSearch Index (Embedding + 批量推送到 OpenSearch)

用法：
  python3 opensearch_pipeline/dataworks_orchestrator.py --stage 1 --bizdate ${bizdate}
  python3 opensearch_pipeline/dataworks_orchestrator.py --stage 2 --bizdate ${bizdate}
  python3 opensearch_pipeline/dataworks_orchestrator.py --stage 3 --bizdate ${bizdate}
"""

import argparse
import sys
import os
from datetime import datetime

# 保证当前目录在 python path 中
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opensearch_pipeline.config import get_config, load_config
from opensearch_pipeline.dag_definitions import (
    build_dag1_raw_to_canonical,
    build_dag2_canonical_to_chunk,
    build_dag3_chunk_to_opensearch,
)
from opensearch_pipeline.run_simulation import get_test_data, get_version_update_data


def run_stage(stage: int, bizdate: str, simulate: bool):
    """根据 stage 和业务日期运行相应的 DAG。"""
    config = get_config()
    
    # 强制将业务日期覆盖到环境变量，以便底层节点代码能够正确感知
    os.environ["RAG_BIZDATE"] = bizdate
    print(f"[Orchestrator] Starting Stage {stage} for business date: {bizdate}")
    print(f"[Orchestrator] Operating Mode: {'SIMULATION' if simulate else 'PRODUCTION'}")
    
    # 运行级成本熔断器（VLM 版面重建用）。一次 DataWorks 运行一个实例 → 单次运行累计预算。
    # 默认 RAG_REBUILD_ENABLED=false 时熔断器为 no-op，不影响现有行为。
    from opensearch_pipeline.extraction.cost_breaker import CostBreaker
    cost_breaker = CostBreaker(config)

    # 构造运行上下文
    ctx = {
        "bizdate": bizdate,
        "simulate": simulate,
        "simulate_api": simulate, # 模拟 API 随 simulate 自动决定
        "cost_breaker": cost_breaker,  # 注入抽取节点 → UnifiedExtractor.cost_breaker
    }

    if stage == 1:
        # ══ Stage 1 运行 ══
        dag = build_dag1_raw_to_canonical()
        if simulate:
            # 模拟环境：从 run_simulation 里的测试数据读取 raw_tasks
            test_data = get_test_data("normal")
            ctx["raw_tasks"] = test_data["raw_tasks"]
            ctx["mock_classifications"] = {
                task["doc_id"]: test_data["mock_classification"]
                for task in test_data["raw_tasks"]
            }
        else:
            # 生产环境：此阶段将由 node_scan_raw_files 在 OSS 中扫描对应 bizdate 目录，
            # 或直接查询 RDS 中注册为 pending 的待解析版本元数据
            pass

        print("[Orchestrator] Executing DAG 1: raw_to_canonical...")
        result_ctx = dag.run(ctx)
        
        # 检查是否成功完成
        failed_nodes = [nid for nid, node in dag.nodes.items() if node.status.name == "FAILED"]
        if failed_nodes:
            raise RuntimeError(f"DAG 1 execution failed at nodes: {failed_nodes}")
        
        print(f"[Orchestrator] Stage 1 successfully completed. Processed {len(result_ctx.get('tasks', []))} documents.")

    elif stage == 2:
        # ══ Stage 2 运行 ══
        dag = build_dag2_canonical_to_chunk()
        has_load_errors = False
        if simulate:
            # 模拟环境：我们需要先运行 Stage 1 DAG 以构造好 canonical 内存结构
            print("[Orchestrator] Preparing simulation context by running Stage 1 first...")
            dag1 = build_dag1_raw_to_canonical()
            test_data = get_test_data("normal")
            stage1_ctx = {
                "bizdate": bizdate,
                "simulate": True,
                "simulate_api": True,
                "raw_tasks": test_data["raw_tasks"],
                "cost_breaker": cost_breaker,
            }
            stage1_res = dag1.run(stage1_ctx)
            ctx["canonicals"] = stage1_res["canonicals"]
            ctx["mock_classifications"] = {
                doc["doc_id"]: test_data["mock_classification"]
                for doc in stage1_res["canonicals"]
            }
        else:
            # 生产环境：我们将从 OSS 或是数据库中检索 Stage 1 输出的 canonical 元数据和内容。
            # 系统会自动在 pipeline 节点中进行 RDS 加锁和前置状态前移，防止重复并发处理。
            print("[Orchestrator] Retrieving pending canonical documents from database...")
            canonicals = []
            conn = None
            try:
                from opensearch_pipeline.pipeline_nodes import _get_db_conn, _get_oss_bucket
                import json
                
                simulate_db = ctx.get("simulate_db", config.simulate_db)
                bucket, is_simulated_oss = _get_oss_bucket(ctx)
                
                conn = _get_db_conn(select_db=True)
                with conn.cursor() as cursor:
                    # ── P0-3 Fix: 原子化抢占 ──
                    # 先用 UPDATE 将符合条件的行状态从 NOT_STARTED/FAILED 改为 LOADING，
                    # 再 SELECT 只取本实例抢占到的行。防止并发实例重复处理同一批文档。
                    cursor.execute("""
                        UPDATE document_version
                        SET content_process_status = 'LOADING'
                        WHERE (content_process_status = 'NOT_STARTED' OR (content_process_status = 'FAILED' AND retry_count < 3))
                          AND status = 'active'
                          AND canonical_json_key IS NOT NULL
                        ORDER BY created_at ASC
                        LIMIT 100
                    """)
                    preempted_count = cursor.rowcount
                    conn.commit()
                    
                    if preempted_count == 0:
                        print("[Orchestrator] No pending canonical documents found (or all preempted by another instance).")
                        rows = []
                    else:
                        print(f"[Orchestrator] Preempted {preempted_count} documents for processing.")
                        cursor.execute("""
                            SELECT 
                                dv.doc_id, 
                                dv.version_no, 
                                dv.canonical_json_key, 
                                dv.canonical_md_key,
                                dv.file_ext,
                                dv.page_count,
                                dv.text_length,
                                dv.extract_method,
                                dv.ocr_status,
                                dm.title,
                                dm.owner_dept,
                                dv.raw_key
                            FROM document_version dv
                            LEFT JOIN document_meta dm ON dv.doc_id = dm.doc_id
                            WHERE dv.content_process_status = 'LOADING'
                              AND dv.status = 'active'
                              AND dv.canonical_json_key IS NOT NULL
                            ORDER BY dv.created_at ASC
                        """)
                        rows = cursor.fetchall()
                    has_load_errors = False
                    for row in rows:
                        doc_id = row[0]
                        version_no = row[1]
                        canonical_json_key = row[2]
                        canonical_md_key = row[3]
                        file_ext = row[4]
                        page_count = row[5]
                        text_length = row[6]
                        extract_method = row[7]
                        ocr_status = row[8]
                        title = row[9] or ""
                        owner_dept = row[10] or "unknown"
                        raw_key = row[11] or ""
                        
                        # Load content from OSS or local storage
                        content_json = {}
                        read_error = None
                        if is_simulated_oss:
                            if os.path.exists(canonical_json_key):
                                try:
                                    with open(canonical_json_key, "r", encoding="utf-8") as f:
                                        content_json = json.load(f)
                                except Exception as sim_err:
                                    read_error = f"Failed to parse local canonical file: {sim_err}"
                            else:
                                read_error = f"Local canonical file not found: {canonical_json_key}"
                        else:
                            try:
                                oss_data = bucket.get_object(canonical_json_key).read()
                                content_json = json.loads(oss_data.decode("utf-8"))
                            except Exception as oss_err:
                                read_error = f"Failed to fetch/parse canonical {canonical_json_key} from OSS: {oss_err}"
                                
                        if read_error:
                            has_load_errors = True
                            print(f"    ⚠️ OSS/Local canonical read failure: {read_error}")
                            if not simulate_db:
                                try:
                                    cursor.execute("""
                                        UPDATE document_version
                                        SET content_process_status = 'FAILED',
                                            content_process_error = %s,
                                            retry_count = retry_count + 1,
                                            processed_at = NOW()
                                        WHERE doc_id = %s AND version_no = %s
                                    """, (read_error, doc_id, version_no))
                                    conn.commit()
                                except Exception as db_err:
                                    print(f"    ⚠️ Failed to update document_version status for OSS read error: {db_err}")
                            continue
                                
                        canonical_doc = {
                            "doc_id": doc_id,
                            "version_no": version_no,
                            "source_key": raw_key,
                            "file_ext": file_ext,
                            "extract_method": extract_method,
                            "title": title,
                            "owner_dept": owner_dept,
                            "text": content_json.get("text", ""),
                            "text_length": content_json.get("text_length", text_length or 0),
                            "blocks": content_json.get("blocks", []),
                            "page_count": page_count or content_json.get("page_count", 0),
                            "ocr_required": ocr_status == "COMPLETED",
                            "ocr_status": ocr_status,
                            "warnings": content_json.get("warnings", []),
                            "assets": content_json.get("assets", []),
                            # 成本封存标记必须跨 stage 边界回读：stage-1 成本闸拒绝的文档在
                            # canonical JSON 里带 cost_quarantined=True，stage-2 据此跳过切块/索引
                            # (否则 RDS 已封存而索引仍写入 chunk → 裂脑)。
                            "cost_quarantined": content_json.get("cost_quarantined", False),
                            "canonical_status": "DONE",
                            "canonical_key": canonical_json_key,
                            "canonical_md_key": canonical_md_key,
                        }
                        canonicals.append(canonical_doc)
                        
                    print(f"[Orchestrator] Successfully loaded {len(canonicals)} canonical documents from RDS/OSS.")
            except Exception as e:
                print(f"[Orchestrator] ERROR: Failed to load Stage 2 production data: {e}", file=sys.stderr)
                raise e
            finally:
                if conn:
                    conn.close()
                    
            ctx["canonicals"] = canonicals

        print("[Orchestrator] Executing DAG 2: canonical_to_chunk...")
        result_ctx = dag.run(ctx)
        
        failed_nodes = [nid for nid, node in dag.nodes.items() if node.status.name == "FAILED"]
        if failed_nodes:
            raise RuntimeError(f"DAG 2 execution failed at nodes: {failed_nodes}")
        
        if has_load_errors:
            raise RuntimeError("Stage 2 completed but had partial OSS load failures. Failing the DataWorks task.")
            
        print(f"[Orchestrator] Stage 2 successfully completed. Generated {len(result_ctx.get('valid_chunks', []))} valid chunks.")

    elif stage == 3:
        # ══ Stage 3 运行 ══
        dag = build_dag3_chunk_to_opensearch()
        if simulate:
            # 模拟环境：我们需要依次运行 Stage 1 & Stage 2，以生成 valid_chunks
            print("[Orchestrator] Preparing simulation context by running Stage 1 & Stage 2 first...")
            dag1 = build_dag1_raw_to_canonical()
            dag2 = build_dag2_canonical_to_chunk()
            test_data = get_test_data("normal")
            
            stage1_ctx = {
                "bizdate": bizdate,
                "simulate": True,
                "simulate_api": True,
                "raw_tasks": test_data["raw_tasks"],
                "cost_breaker": cost_breaker,
            }
            stage1_res = dag1.run(stage1_ctx)
            
            stage2_ctx = {
                "bizdate": bizdate,
                "simulate": True,
                "simulate_api": True,
                "canonicals": stage1_res["canonicals"],
                "mock_classifications": {
                    doc["doc_id"]: test_data["mock_classification"]
                    for doc in stage1_res["canonicals"]
                }
            }
            stage2_res = dag2.run(stage2_ctx)
            
            ctx["valid_chunks"] = stage2_res["valid_chunks"]
        else:
            # 生产环境：加载对应业务日期（bizdate）的已切分好、但仍为 NOT_INDEXED 的 chunk 列表。
            # 底层 node_generate_embeddings 将对这批 chunks 批量生成 embedding 向量并写入 OpenSearch 索引。
            print("[Orchestrator] Retrieving NOT_INDEXED chunks from database...")
            valid_chunks = []
            conn = None
            try:
                from opensearch_pipeline.pipeline_nodes import _get_db_conn
                from opensearch_pipeline.chunker import Chunk
                import json
                
                conn = _get_db_conn(select_db=True)
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT 
                            cm.id, cm.chunk_id, cm.doc_id, cm.version_no, cm.chunk_index, cm.page_num, cm.section_title,
                            cm.source_url, cm.chunk_type, cm.chunk_text, cm.token_count, cm.source,
                            cm.rag_ready_key, cm.permission_level, cm.owner_dept, cm.category_l1, cm.category_l2,
                            cm.sensitive_redacted, cm.is_active, cm.embedding_status, cm.index_status,
                            cm.embedding_model, cm.extra_json,
                            COALESCE(dm.title, dm.original_filename, '') AS doc_title
                        FROM chunk_meta cm
                        JOIN document_version dv
                          ON cm.doc_id = dv.doc_id AND cm.version_no = dv.version_no
                        LEFT JOIN document_meta dm
                          ON cm.doc_id = dm.doc_id
                        WHERE cm.index_status IN ('NOT_INDEXED', 'FAILED')
                          AND cm.is_active = 1
                          AND (
                              dv.index_status != 'PROCESSING'
                              OR dv.updated_at < NOW() - INTERVAL 2 HOUR
                          )
                        ORDER BY cm.created_at ASC
                        LIMIT 1000
                    """)
                    rows = cursor.fetchall()
                    for row in rows:
                        rds_id = row[0]
                        chunk_id = row[1]
                        doc_id = row[2]
                        version_no = row[3]
                        chunk_index = row[4]
                        page_num = row[5]
                        section_title = row[6]
                        source_url = row[7]
                        chunk_type = row[8]
                        chunk_text = row[9]
                        token_count = row[10]
                        source = row[11] or "native"
                        rag_ready_key = row[12]
                        permission_level = row[13]
                        owner_dept = row[14]
                        category_l1 = row[15]
                        category_l2 = row[16]
                        sensitive_redacted = bool(row[17])
                        is_active = bool(row[18])
                        embedding_status = row[19]
                        index_status = row[20]
                        embedding_model = row[21]
                        extra_json_str = row[22]
                        doc_title = row[23] or ""
                        
                        extra = {}
                        if extra_json_str:
                            try:
                                if isinstance(extra_json_str, dict):
                                    extra = extra_json_str
                                else:
                                    parsed = json.loads(extra_json_str)
                                    if isinstance(parsed, dict):
                                        extra = parsed
                            except Exception:
                                pass
                                
                        extra["rag_ready_key"] = rag_ready_key
                        extra["source_url"] = source_url
                        
                        chunk_obj = Chunk(
                            chunk_id=chunk_id,
                            doc_id=doc_id,
                            version_no=version_no,
                            chunk_index=chunk_index,
                            page_num=page_num,
                            section_title=section_title,
                            source_oss_key=rag_ready_key or source_url,
                            chunk_type=chunk_type,
                            chunk_text=chunk_text,
                            token_count=token_count,
                            source=source,
                            permission_level=permission_level,
                            owner_dept=owner_dept,
                            category_l1=category_l1,
                            category_l2=category_l2,
                            sensitive_redacted=sensitive_redacted,
                            is_active=is_active,
                            embedding_status=embedding_status,
                            index_status=index_status,
                            embedding_model=embedding_model,
                            rds_id=rds_id,
                            title=doc_title,
                            extra=extra
                        )
                        valid_chunks.append(chunk_obj)
                        
                    print(f"[Orchestrator] Successfully loaded {len(valid_chunks)} chunks from database.")
            except Exception as e:
                print(f"[Orchestrator] ERROR: Failed to load Stage 3 production data: {e}", file=sys.stderr)
                raise e
            finally:
                if conn:
                    conn.close()
                    
            ctx["valid_chunks"] = valid_chunks

        print("[Orchestrator] Executing DAG 3: chunk_to_opensearch...")
        result_ctx = dag.run(ctx)
        
        failed_nodes = [nid for nid, node in dag.nodes.items() if node.status.name == "FAILED"]
        if failed_nodes:
            preempted = ctx.get("preempted_doc_versions", set())
            if preempted and not simulate:
                print(f"[Orchestrator] DAG 3 failed. Rolling back PROCESSING locks for {len(preempted)} doc versions...")
                try:
                    from opensearch_pipeline.pipeline_nodes import _get_db_conn
                    conn_rb = _get_db_conn(select_db=True)
                    with conn_rb.cursor() as cursor:
                        for doc_id, ver in preempted:
                            cursor.execute("""
                                UPDATE document_version
                                SET index_status = 'FAILED'
                                WHERE doc_id = %s AND version_no = %s AND index_status = 'PROCESSING'
                            """, (doc_id, ver))
                        conn_rb.commit()
                except Exception as e:
                    if 'conn_rb' in locals() and conn_rb: conn_rb.rollback()
                    print(f"[Orchestrator] ERROR: Failed to rollback locks: {e}", file=sys.stderr)
                finally:
                    if 'conn_rb' in locals() and conn_rb: conn_rb.close()
            raise RuntimeError(f"DAG 3 execution failed at nodes: {failed_nodes}")
        
        index_res = result_ctx.get("index_result", {})
        print(f"[Orchestrator] Stage 3 successfully completed. Indexed status: {index_res.get('status', 'SUCCESS')}")

    else:
        raise ValueError(f"Invalid stage number: {stage}. Must be 1, 2, or 3.")


def main():
    parser = argparse.ArgumentParser(description="DataWorks Scheduling Orchestrator")
    parser.add_argument(
        "--stage", type=int, required=True, choices=[1, 2, 3],
        help="Pipeline Stage to run (1: Raw->Canonical, 2: Canonical->Chunk, 3: Chunk->OpenSearch)"
    )
    parser.add_argument(
        "--bizdate", type=str, required=True,
        help="Business date of the execution schedule (format: YYYYMMDD)"
    )
    parser.add_argument(
        "--environment", type=str, default=None,
        choices=["development", "staging", "production"],
        help="Override pipeline target database environment"
    )
    parser.add_argument(
        "--simulate", type=str, default=None,
        choices=["true", "false"],
        help="Explicitly force or disable simulation mode (overrides RAG_SIMULATE)"
    )

    args = parser.parse_args()
    
    # 覆盖环境变量
    if args.environment:
        os.environ["RAG_ENVIRONMENT"] = args.environment
        print(f"[Orchestrator] RAG_ENVIRONMENT overridden to: {args.environment}")
        
    if args.simulate:
        os.environ["RAG_SIMULATE"] = args.simulate
        # 还要同步将 API 模拟设成一样的
        os.environ["RAG_SIMULATE_API"] = args.simulate
        print(f"[Orchestrator] RAG_SIMULATE overridden to: {args.simulate}")

    # 重载全局配置，并写回单例以确保下游 get_config() 拿到更新后的配置
    config = load_config()
    import opensearch_pipeline.config as _cfg_module
    _cfg_module._config = config
    simulate_mode = config.simulate
    
    try:
        run_stage(args.stage, args.bizdate, simulate_mode)
        print(f"\n[Orchestrator] SUCCESS: Stage {args.stage} finished successfully.")
        sys.exit(0)
    except Exception as e:
        print(f"\n[Orchestrator] ERROR: Stage {args.stage} failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
