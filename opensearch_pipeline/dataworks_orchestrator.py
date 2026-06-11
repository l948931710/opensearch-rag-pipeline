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
        # 细粒度开关显式下传：_resolve_simulate 的优先级是 ctx 细粒度 > ctx 全局 > config。
        # 此前 orchestrator 只设 ctx["simulate"]，RAG_SIMULATE_DB/OSS/OPENSEARCH 在调度链路下
        # 全是死配置。`simulate or ...`：--simulate 运行必须全模拟（细粒度键优先级最高，
        # 不强制 True 会让 production 配置在模拟跑里做真实 I/O）；生产跑则透传 config。
        "simulate_db": simulate or config.simulate_db,
        "simulate_oss": simulate or config.simulate_oss,
        "simulate_opensearch": simulate or config.simulate_opensearch,
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
                from opensearch_pipeline.pipeline_nodes import (
                    _get_db_conn, _get_oss_bucket, _resolve_simulate,
                )
                import json

                # 与各节点同一套三层解析（此前这里漏了 ctx["simulate"] 一层：CLI --simulate
                # 与环境变量 RAG_SIMULATE_DB 不一致时，loader 和节点会各走半真半假的分支）
                simulate_db = _resolve_simulate(ctx, "db")
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
                          AND (publish_status IS NULL OR publish_status != 'QUARANTINED')
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
        if not simulate and config.simulate_db != config.simulate_opensearch:
            # DAG 3 同时改写 RDS 与 HA3（推送新版本 + 停用旧版本）。一真一假必然裂脑：
            # 只删一边/只停用一边，文档双版本同时被检索或直接消失。宁可拒跑。
            raise RuntimeError(
                f"Refusing stage 3: simulate_db={config.simulate_db} but "
                f"simulate_opensearch={config.simulate_opensearch}. DAG 3 writes both stores; "
                "mixed real/mock between them causes split-brain."
            )
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
            # ⚠️ 锁信息由节点写入 dag.run() 内部的 context 副本（dag_engine.DAG.run 第一行
            # self.context = dict(initial_context)），必须从返回的 result_ctx 读取，
            # 而不是传入的 ctx —— 后者永远是空集，回滚会变成死代码。
            preempted = result_ctx.get("preempted_doc_versions", set())
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


def _reset_stale_stage2_locks() -> int:
    """Stage-2 失效锁接管：LOADING（loader 抢占后崩溃）和 PROCESSING（DAG2 节点内崩溃）
    都没有年龄守卫，进程崩溃会让行永久卡死，且 _count_pending_rows(2) 两个状态都看不见
    （静默 wedge）。复用 node_acquire_index_lock 的 2h 失效约定：重置为 FAILED 并
    retry_count+1，由既有抢占谓词 (FAILED AND retry_count<3) 自然重新入队；持续把进程
    搞崩的"毒文档"3 次后停在 FAILED 等人工检查，不会无限崩溃循环。
    updated_at=NOW() 显式刷新：并发实例中只有第一个接管成功（changed-rows 语义）。"""
    from opensearch_pipeline.pipeline_nodes import _get_db_conn
    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE document_version
                SET content_process_status = 'FAILED',
                    content_process_error = CONCAT('[STALE_LOCK_TAKEOVER] was ',
                        content_process_status, ' >2h without progress; reset for retry'),
                    retry_count = retry_count + 1,
                    updated_at = NOW()
                WHERE content_process_status IN ('LOADING', 'PROCESSING')
                  AND status = 'active'
                  AND updated_at < NOW() - INTERVAL 2 HOUR
            """)
            n = cur.rowcount
            conn.commit()
        if n:
            print(f"[Orchestrator] Stage 2: reset {n} stale LOADING/PROCESSING row(s) to FAILED")
        return n
    finally:
        if conn:
            conn.close()


def _count_pending_rows(stage: int) -> int:
    """生产模式下统计某 stage 仍待处理的行数（用于 drain-loop 的进度判定）。

    各 stage 的谓词与 run_stage / node_scan_raw_files 的认领条件保持一致：
      Stage 1: NOT_STARTED & canonical_json_key IS NULL
               & file_ext ∉ ingest_policy.STAGE1_SQL_EXCLUDED_EXTS（与认领 SQL 同一常量；
               不一致 = 计数器看得到、认领挑不走 → 无进展守卫永久判死 stage-1）& active
      Stage 2: (NOT_STARTED 或 FAILED&retry_count<3) & active & canonical_json_key IS NOT NULL
      Stage 3: chunk_meta NOT_INDEXED/FAILED & is_active & (dv 非 PROCESSING 或 已过 2h 失效锁)
    """
    from opensearch_pipeline.ingest_policy import stage1_ext_exclusion_sql
    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    queries = {
        1: f"""
            SELECT COUNT(*) FROM document_version
            WHERE content_process_status = 'NOT_STARTED'
              AND canonical_json_key IS NULL
              AND file_ext NOT IN {stage1_ext_exclusion_sql()}
              AND status = 'active'
        """,
        2: """
            SELECT COUNT(*) FROM document_version
            WHERE (content_process_status = 'NOT_STARTED'
                   OR (content_process_status = 'FAILED' AND retry_count < 3))
              AND status = 'active'
              AND canonical_json_key IS NOT NULL
              AND (publish_status IS NULL OR publish_status != 'QUARANTINED')
        """,
        3: """
            SELECT COUNT(*) FROM chunk_meta cm
            JOIN document_version dv
              ON cm.doc_id = dv.doc_id AND cm.version_no = dv.version_no
            WHERE cm.index_status IN ('NOT_INDEXED', 'FAILED')
              AND cm.is_active = 1
              AND (dv.index_status != 'PROCESSING'
                   OR dv.updated_at < NOW() - INTERVAL 2 HOUR)
        """,
    }
    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cur:
            cur.execute(queries[stage])
            return int(cur.fetchone()[0])
    finally:
        if conn:
            conn.close()


def run_stage_drained(stage: int, bizdate: str, simulate: bool):
    """排空式执行：生产模式下循环调用 run_stage，直到该 stage 没有待处理行（一次调用排空整个语料）。

    - 模拟模式只跑一次：run_simulation 注入的是固定测试数据，循环会无限重复同一批。
    - no-progress 守卫：若一整批跑完后剩余行数没有下降（例如某批文档持续失败、停留在
      NOT_STARTED/FAILED），则停止并告警，避免死循环。Balanced 级别未加 Stage-1 原子抢占，
      因此该守卫是必需的。
    - run_stage 在任何批次失败时仍会 raise（沿用 fail-fast 语义），异常会冒泡到 main 退出。
    """
    if simulate:
        run_stage(stage, bizdate, simulate)
        return

    if stage == 3:
        # ── 搁浅版本对账：上一次部分失败可能留下「新版本已全量 INDEXED 但旧版本仍 active」
        # 的文档（双版本同时被检索）。必须在 drain 循环之前跑：这类文档没有待处理 chunk，
        # _count_pending_rows(3)==0 时 run_stage 根本不会执行。失败不阻断当日入库（优雅降级）。
        from opensearch_pipeline.spot_checker import reconcile_stranded_versions
        try:
            rec = reconcile_stranded_versions()
            if rec["total"]:
                print(f"[Orchestrator] Stranded-version reconcile: {rec['success']}/{rec['total']} "
                      f"healed, {rec['failed']} failed")
        except Exception as e:
            print(f"[Orchestrator] WARNING: stranded-version reconcile failed (non-fatal): {e}",
                  file=sys.stderr)

    max_iters = int(os.environ.get("RAG_DRAIN_MAX_ITERS", "100000"))
    prev_remaining = None
    iteration = 0
    while True:
        iteration += 1
        if iteration > max_iters:
            # 抛错而非 break：让 DataWorks 通过非零退出码识别异常，不能静默成功。
            raise RuntimeError(
                f"Stage {stage} drain-loop hit RAG_DRAIN_MAX_ITERS={max_iters} without draining; "
                f"aborting so the run is marked failed."
            )
        if stage == 2:
            # 失效锁接管放在计数之前：被接管的行变回 FAILED&retry<3，本轮计数即可看见，
            # 也能恢复 drain 中途 wedge 的行
            _reset_stale_stage2_locks()
        remaining = _count_pending_rows(stage)
        if remaining == 0:
            print(f"[Orchestrator] Stage {stage} drained: 0 pending rows after {iteration - 1} batch(es).")
            break
        if prev_remaining is not None and remaining >= prev_remaining:
            # 一整批跑完后剩余行数没有下降 = 有卡住/持续失败的行。必须抛错，让退出码非零，
            # 否则 DataWorks 会把卡死的运行标记为成功（绿色），无人察觉语料停止入库。
            raise RuntimeError(
                f"Stage {stage} made no progress (remaining={remaining} did not decrease "
                f"from {prev_remaining}). Stuck/failing rows — failing the run; inspect FAILED rows."
            )
        print(f"[Orchestrator] Stage {stage} drain batch #{iteration} — {remaining} rows pending...")
        prev_remaining = remaining
        run_stage(stage, bizdate, simulate)


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
        run_stage_drained(args.stage, args.bizdate, simulate_mode)
        print(f"\n[Orchestrator] SUCCESS: Stage {args.stage} finished successfully.")
        sys.exit(0)
    except Exception as e:
        print(f"\n[Orchestrator] ERROR: Stage {args.stage} failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
