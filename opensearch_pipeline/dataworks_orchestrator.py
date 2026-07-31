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

⚠️ bizdate 语义（盲区审计 P3-10 如实声明）：摄取是**纯状态 drain**——各阶段按
content_process_status / index_status 认领行，bizdate **从不进入任何 WHERE**，只作
溯源标注（pipeline_run / chunk_meta.extra_json / kb_audit_log）。因此：
  · `--stage 2 --bizdate 20260701` **不是**「回填 7 月 1 日」——它照常 drain 当前全部
    待处理行，只是把 20260701 写进本次运行的血缘标注；
  · 想重处理指定文档集，用 reset_for_rechunk.py / rebuild_from_rds.py 显式定位后重跑；
  · 各 stage 节点漏配调度参数时的兜底 = 北京 T-1（固定 UTC+8，不随容器时区漂移）。
"""

import argparse
import sys
import os
import threading
import time

# 保证当前目录在 python path 中
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opensearch_pipeline.config import get_config, load_config
from opensearch_pipeline.reindex_states import (
    RETRY_COUNT_INC_SQL,
    STAGE3_CHUNK_RESELECT_INDEX_STATUS,
    ChunkIndexStatus,
    DocVersionIndexStatus,
    sql_in_list,
)
from opensearch_pipeline.dag_definitions import (
    build_dag1_raw_to_canonical,
    build_dag2_canonical_to_chunk,
    build_dag3_chunk_to_opensearch,
)
from opensearch_pipeline.run_simulation import get_test_data


def _loader_fetch_concurrency() -> int:
    """Stage-2 canonical 预取并发度（perf F#54）。RAG_LOADER_FETCH_CONCURRENCY，默认 1=维持
    串行现状（写路径保守）；DataWorks 节点可自行调大——认领事务已提交，OSS get_object 是
    无锁纯读，失败回写仍逐条串行。"""
    try:
        return max(1, int(os.environ.get("RAG_LOADER_FETCH_CONCURRENCY", "1")))
    except (TypeError, ValueError):
        return 1


def run_stage(stage: int, bizdate: str, simulate: bool, cost_breaker=None):
    """根据 stage 和业务日期运行相应的 DAG。

    cost_breaker: 可选注入的运行级成本熔断器（P2-10）。drain 循环（run_stage_drained）
        在循环外建一次并逐批传入，让 run_budget_rmb 覆盖整个 drain——此前每批新建实例
        把 _run_total_rmb 归零，聚合花费上界=批数×预算。None（单批直调/模拟）时自建，
        保持既有行为不变。
    """
    config = get_config()

    # 强制将业务日期覆盖到环境变量，以便底层节点代码能够正确感知
    os.environ["RAG_BIZDATE"] = bizdate
    print(f"[Orchestrator] Starting Stage {stage} for business date: {bizdate}")
    print(f"[Orchestrator] Operating Mode: {'SIMULATION' if simulate else 'PRODUCTION'}")

    # 运行级成本熔断器（VLM 版面重建用）。一次 DataWorks 运行一个实例 → 单次运行累计预算
    # （drain 场景由 run_stage_drained 注入共享实例，跨批累计）。
    # 默认 RAG_REBUILD_ENABLED=false 时熔断器为 no-op，不影响现有行为。
    if cost_breaker is None:
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

    # 每次运行的来源指纹（git sha + extractor/chunker/detector/embedding 版本 + 解析后模型名 + bizdate）。
    # 纯只读、不读未用时零行为变化（Phase-1 L1，provenance/lineage 地基）；供 chunk provenance、
    # kb_audit_log、pipeline_run、受影响文档集 diff 复用——trace_id 的单一来源。
    from opensearch_pipeline.versions import build_run_provenance
    ctx["run_provenance"] = build_run_provenance(stage=stage, bizdate=bizdate)
    print(f"[Orchestrator] run provenance: commit={ctx['run_provenance']['git_commit']} "
          f"chunker={ctx['run_provenance']['chunker_version']} "
          f"detector={ctx['run_provenance']['detector_version']} "
          f"embed={ctx['run_provenance']['embedding_model_version']}")

    # Maintenance re-chunk: freeze classification/routing (no LLM classifier). When
    # RAG_MAINTENANCE_ROUTING points at a frozen-routing manifest, node_classify reuses each doc's
    # frozen category_l1/l2 (deterministic routing, chunk family preserved) and fails closed on any
    # missing entry. Absent the env var, ingestion behaves exactly as before (normal LLM classify).
    _maint_path = os.environ.get("RAG_MAINTENANCE_ROUTING")
    if _maint_path and not simulate:
        import json as _json
        with open(_maint_path, encoding="utf-8") as _mf:
            ctx["frozen_routing"] = _json.load(_mf)
        print(f"[Orchestrator] MAINTENANCE re-chunk: frozen routing for "
              f"{len(ctx['frozen_routing'])} docs (LLM classifier disabled)")

    # Deliberate UNFROZEN re-chunk override (route-v2 family migration). node_classify fail-closes
    # when a re-chunk of an already-chunked doc runs WITHOUT a freeze; this doc-set-bound, same-day
    # token (<op>:<date>:<docset_hash>) is the explicit escape hatch. Surfaced here for an auditable
    # run-log banner; the node also reads the env var directly as a fallback. Ignored when a freeze is
    # set (frozen_routing wins) and under --simulate.
    _unfreeze_ack = os.environ.get("RAG_ALLOW_UNFROZEN_RECHUNK")
    if _unfreeze_ack and not simulate:
        ctx["allow_unfrozen_rechunk"] = _unfreeze_ack
        print("[Orchestrator] ⚠️ UNFROZEN re-chunk override present "
              "(RAG_ALLOW_UNFROZEN_RECHUNK set) — classifier WILL re-roll category for re-chunked "
              "docs whose doc-set hash matches the token")

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
        claimed_ids = []          # B1a：本批认领到的 document_version.id（供收尾残留断言）
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
                    # ── P0-02 Fix: 实例级原子认领（FOR UPDATE SKIP LOCKED） ──
                    # 旧实现先 UPDATE...SET LOADING 再 SELECT WHERE status='LOADING'：第二次
                    # SELECT 不带实例归属谓词，会读到【所有】实例的 LOADING 行 → 并发 Stage-2
                    # 实例重复处理同一批（旧注释自称"只取本实例抢占到的行"，与实现不符）。
                    # 现改为单步认领：SELECT ... FOR UPDATE OF dv SKIP LOCKED 锁定候选并跳过其它
                    # 实例已锁的行 → 各实例认领集合天然不相交；随即按 id 置 LOADING 并提交释放
                    # 行锁。**不再做第二次按状态 SELECT**——认领到的行已在 rows 内存里。行锁只在
                    # 认领事务内短暂持有，绝不跨后续 OSS/DAG 长流程（MySQL 8 支持，生产实测 8.0.36）。
                    # 进程在 commit 后崩溃留下的 LOADING 仍由 _reset_stale_stage2_locks（2h）兜底回收。
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
                            dv.raw_key,
                            dv.id
                        FROM document_version dv
                        LEFT JOIN document_meta dm ON dv.doc_id = dm.doc_id
                        WHERE (content_process_status = 'NOT_STARTED'
                               OR (content_process_status = 'FAILED' AND retry_count < 3))
                          AND dv.status = 'active'
                          AND dv.canonical_json_key IS NOT NULL
                          AND (dv.publish_status IS NULL OR dv.publish_status != 'QUARANTINED')
                        ORDER BY dv.created_at ASC
                        LIMIT 100
                        FOR UPDATE OF dv SKIP LOCKED
                    """)
                    rows = cursor.fetchall()
                    preempted_count = len(rows)

                    if preempted_count == 0:
                        conn.commit()   # 结束（空）认领事务，释放可能的间隙锁
                        print("[Orchestrator] No pending canonical documents found (or all preempted by another instance).")
                    else:
                        # 仅对本实例锁定到的行置 LOADING（按 id），提交后释放行锁。
                        claimed_ids = [r[12] for r in rows]
                        _ph = ",".join(["%s"] * len(claimed_ids))
                        cursor.execute(
                            "UPDATE document_version SET content_process_status = 'LOADING', "
                            f"updated_at = NOW() WHERE id IN ({_ph})",
                            claimed_ids,
                        )
                        conn.commit()
                        print(f"[Orchestrator] Preempted {preempted_count} documents for processing.")
                    has_load_errors = False

                    def _fetch_canonical(canonical_json_key):
                        """只读拉取并解析单篇 canonical JSON（OSS 或本地模拟）→ (content_json, read_error)。
                        纯读、无 DB/锁副作用——可安全并行；失败回写由主循环逐条串行执行。"""
                        if is_simulated_oss:
                            if os.path.exists(canonical_json_key):
                                try:
                                    with open(canonical_json_key, "r", encoding="utf-8") as f:
                                        return json.load(f), None
                                except Exception as sim_err:
                                    return {}, f"Failed to parse local canonical file: {sim_err}"
                            return {}, f"Local canonical file not found: {canonical_json_key}"
                        try:
                            oss_data = bucket.get_object(canonical_json_key).read()
                            return json.loads(oss_data.decode("utf-8")), None
                        except Exception as oss_err:
                            return {}, (f"Failed to fetch/parse canonical {canonical_json_key} "
                                        f"from OSS: {oss_err}")

                    # perf F#54：认领事务已提交（行锁已释放）后的 canonical 拉取是纯读——
                    # RAG_LOADER_FETCH_CONCURRENCY>1 时线程池并行预取（默认 1=串行现状）；
                    # 结果按原行序消费，失败回写逻辑保持逐条串行语义不变。
                    prefetched = {}
                    _fetch_conc = _loader_fetch_concurrency()
                    # A1（2026-07-25）：无条件报 configured/effective —— 该旋钮默认 1 且部署侧
                    # 零注入，不打日志就无法验证它到底有没有生效。
                    print(f"    └─ [concurrency] loader-fetch: configured={_fetch_conc} "
                          f"effective={_fetch_conc if len(rows) > 1 else 1} "
                          f"(RAG_LOADER_FETCH_CONCURRENCY, rows={len(rows)})")
                    if _fetch_conc > 1 and len(rows) > 1:
                        from concurrent.futures import ThreadPoolExecutor
                        _keys = [r[2] for r in rows]
                        with ThreadPoolExecutor(
                                max_workers=min(_fetch_conc, len(rows))) as _pool:
                            for _i, _res in enumerate(_pool.map(_fetch_canonical, _keys)):
                                prefetched[_i] = _res

                    for row_idx, row in enumerate(rows):
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

                        # Load content from OSS or local storage（预取命中直接用，否则现场拉）
                        if row_idx in prefetched:
                            content_json, read_error = prefetched[row_idx]
                        else:
                            content_json, read_error = _fetch_canonical(canonical_json_key)

                        if read_error:
                            has_load_errors = True
                            print(f"    ⚠️ OSS/Local canonical read failure: {read_error}")
                            if not simulate_db:
                                try:
                                    cursor.execute(f"""
                                        UPDATE document_version
                                        SET content_process_status = 'FAILED',
                                            content_process_error = %s,
                                            {RETRY_COUNT_INC_SQL},
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
                            # 成本封存标记必须跨 stage 边界回读：stage-1 成本闸拒绝的文档在
                            # canonical JSON 里带 cost_quarantined=True，stage-2 据此跳过切块/索引
                            # (否则 RDS 已封存而索引仍写入 chunk → 裂脑)。
                            "cost_quarantined": content_json.get("cost_quarantined", False),
                            # xlsx layout 判定必须跨 stage 边界回读（F-2）：DAG1 用真实 filename 分类一次
                            # 并写入 canonical JSON；stage-2 重载若丢弃它 → DAG2 消费点回退重分类，此时
                            # doc.filename 为空 → procedure_image_guide 被误判成 normal_spreadsheet →
                            # step_card / 图片绑定结构静默丢失。filename 从未写入 canonical JSON，用 RDS
                            # title 兜底供 DAG2 回退分类器（正常路径有 xlsx_layout_type 即不回退）。
                            "xlsx_layout_type": content_json.get("xlsx_layout_type"),
                            # P2-32：VLM degraded 兜底图片数同样跨 stage 边界回读——stage-2 的
                            # node_write_chunk_meta 据此把文档收尾改走 NEEDS_REVIEW（不 DONE），
                            # 丢弃它 = degraded 标志在 DAG1→DAG2 边界静默蒸发、文档照样 INDEXED 终态。
                            "vlm_degraded_count": content_json.get("vlm_degraded_count", 0),
                            # 批次6：部分内容丢失留痕（OCR 部分页/中途异常）→ 同一 NEEDS_REVIEW 通道
                            "partial_loss_notes": content_json.get("partial_loss_notes", []) or [],
                            "filename": content_json.get("filename") or title,
                            "canonical_status": "DONE",
                            "canonical_key": canonical_json_key,
                            "canonical_md_key": canonical_md_key,
                        }
                        # F（2026-07-25）：**保留 assets 的字段存在性**。旧写法
                        # `content_json.get("assets", [])` 把"canonical 里根本没有该键"
                        # 抹成空集，会让资产集比对把一次读取/格式异常误判成"整篇图没了"
                        # 从而伪造全量 removal。缺键就别放这个键，下游按 unknown 处理。
                        if "assets" in content_json:
                            canonical_doc["assets"] = content_json["assets"]
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

        # B1a（2026-07-25）：残留断言 —— 必须在所有 closure 完成之后、打印成功之前。
        # 堵的出口：0-chunk / explosion 收尾的落库是 fail-open（只 print 不上抛），
        # 于是 DAG 可以"成功"而这些行仍留在 LOADING/PROCESSING —— 认领谓词不收它们、
        # 计数谓词也看不见 → 本次运行报绿、行却楔死到 2h 陈旧接管为止。
        # **纯只读**（不写任何状态，因此不需要所有权证明，无 ABA 面）；
        # **fail-closed**：查询本身失败也 raise，不沿用可观测探针的 fail-open 风格。
        if not simulate_db and claimed_ids:
            _assert_no_claimed_residue(claimed_ids)

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
                    cursor.execute(f"""
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
                        WHERE cm.index_status IN ({sql_in_list(STAGE3_CHUNK_RESELECT_INDEX_STATUS)})
                          AND cm.is_active = 1
                          AND (
                              dv.index_status != '{DocVersionIndexStatus.PROCESSING}'
                              OR dv.updated_at < NOW() - INTERVAL 2 HOUR
                          )
                        ORDER BY cm.created_at ASC
                        LIMIT 1000
                    """)
                    # ⚠️ LIMIT 1000 不按文档分组，边界可能把一个文档的新版本切成两批。
                    # 中切安全性由 node_deactivate_old_chunks 的 LIMIT 边界完整性闸保证：
                    # 同版本仍有残留未 INDEXED chunk 时推迟停用旧版本并把 document_version
                    # 复位 NOT_INDEXED，尾批在 drain-loop 下一轮被装入后再收尾。
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

                    # Phase D（RAG_ALLOWED_DEPTS_ACL，默认关）：重推路径也必须经唯一 helper 从 approved
                    # 授权【重解析】allowed_depts（约束 2：不读可能过时的 chunk_meta 投影；约束 3：按
                    # doc_id 聚合、跟随 current version）。fail-closed：失败置空，不放行。
                    # ── node-ACL 投影(与 stage-2 同一唯一注入点)────────────────────
                    # ⚠️ stage-3 reload 从 chunk_meta 读回 owner 后直接重推 HA3;若 node 文档
                    # 不在此改写成哨兵,一次重推就把真实 owner 送回检索面 = 权限重开。
                    _node_docs = set()
                    if valid_chunks:
                        try:
                            from opensearch_pipeline.access_grants import (
                                resolve_acl_modes, resolve_doc_acl,
                            )
                            from opensearch_pipeline.acl_policy import ACL_MODE_NODE, project_doc_acl
                            _modes = resolve_acl_modes({c.doc_id for c in valid_chunks}, cursor)
                            _node_docs = {d for d, m in _modes.items() if m == ACL_MODE_NODE}
                            if _node_docs:
                                _nacl = resolve_doc_acl(_node_docs, cursor)
                                for c in valid_chunks:
                                    if c.doc_id not in _node_docs:
                                        continue
                                    _a = _nacl.get(c.doc_id)
                                    c.owner_dept, c.allowed_depts = project_doc_acl(
                                        ACL_MODE_NODE, c.owner_dept, (),
                                        getattr(_a, "node_ids", ()) if _a else (),
                                        getattr(_a, "exact_node_ids", ()) if _a else ())
                                print(f"[Orchestrator] 🔐 node-ACL 投影:{len(_node_docs)} 篇写哨兵 owner")
                        except Exception as _nae:
                            # fail-closed:绝不退回"推真实 owner"。中止本批,交下轮重试。
                            raise RuntimeError(
                                f"node-ACL 投影失败,中止 stage-3 装载(绝不退回真实 owner): {_nae}")

                    if config.rag.allowed_depts_acl and valid_chunks:
                        try:
                            from opensearch_pipeline.access_grants import (
                                resolve_allowed_depts, gate_by_permission,
                            )
                            _allowed = resolve_allowed_depts(
                                {c.doc_id for c in valid_chunks} - _node_docs, cursor)
                            # 纵深守卫：只有 permission_level=='dept_internal' 的文档物化 allowed_depts
                            # （用 chunk 自身权威 permission_level；审计 Step 4 backstop a）。
                            _allowed = gate_by_permission(
                                _allowed, {c.doc_id: c.permission_level for c in valid_chunks}
                            )
                            for c in valid_chunks:
                                if c.doc_id in _node_docs:
                                    continue   # 已投影为哨兵+d:/dx:,绝不被组码覆盖
                                c.allowed_depts = _allowed.get(c.doc_id, [])
                        except Exception as _ade:
                            print(f"[Orchestrator] ⚠️ allowed_depts 重解析失败（fail-closed 置空）: {_ade}")

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
                            cursor.execute(f"""
                                UPDATE document_version
                                SET index_status = '{DocVersionIndexStatus.FAILED}'
                                WHERE doc_id = %s AND version_no = %s AND index_status = '{DocVersionIndexStatus.PROCESSING}'
                            """, (doc_id, ver))
                        conn_rb.commit()
                except Exception as e:
                    if 'conn_rb' in locals() and conn_rb:
                        conn_rb.rollback()
                    print(f"[Orchestrator] ERROR: Failed to rollback locks: {e}", file=sys.stderr)
                finally:
                    if 'conn_rb' in locals() and conn_rb:
                        conn_rb.close()
            raise RuntimeError(f"DAG 3 execution failed at nodes: {failed_nodes}")
        
        index_res = result_ctx.get("index_result", {})
        print(f"[Orchestrator] Stage 3 successfully completed. Indexed status: {index_res.get('status', 'SUCCESS')}")

    else:
        raise ValueError(f"Invalid stage number: {stage}. Must be 1, 2, or 3.")

    # OBS-3: hand the DAG result ctx back so the drain loop can extract per-run metric counters.
    return result_ctx


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
            cur.execute(f"""
                UPDATE document_version
                SET content_process_status = 'FAILED',
                    content_process_error = CONCAT('[STALE_LOCK_TAKEOVER] was ',
                        content_process_status, ' >2h without progress; reset for retry'),
                    {RETRY_COUNT_INC_SQL},
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
        3: f"""
            SELECT COUNT(*) FROM chunk_meta cm
            JOIN document_version dv
              ON cm.doc_id = dv.doc_id AND cm.version_no = dv.version_no
            WHERE cm.index_status IN ({sql_in_list(STAGE3_CHUNK_RESELECT_INDEX_STATUS)})
              AND cm.is_active = 1
              AND (dv.index_status != '{DocVersionIndexStatus.PROCESSING}'
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


# A3（2026-07-25）：drain 收尾的"不可认领"探针 —— 只读，不改任何状态、不改认领谓词。
#
# 为什么需要：_count_pending_rows 的谓词与认领谓词**必须**同源（不一致就会踩
# ingest_policy.py 的「计得到、领不走 → 无进展守卫永久判死」陷阱），但这同时意味着
# **被楔住的行对 drain 完全隐形**：
#   · stage-2 的批次级失败分支不回滚已认领行（对照 stage-3 有逐条回滚），这些行停在
#     LOADING/PROCESSING —— 既不被重新认领，也不在 stage-2 的计数谓词里；
#   · 于是下一次运行会打印「drained: 0 pending rows」并 exit 0：**一边报绿，一边有整批
#     文档楔死**；三次批次级崩溃后 retry_count>=3，该行同时从认领谓词与计数谓词消失，
#     变成永久绿灯 + 永不入库。
#
# 本探针只做可见性，**刻意不改退出码**：现网存量死信/隔离行的基线未知，贸然让它翻红会把
# 每一次运行都变成红灯（同 C1「先定基线再上告警」的教训）。基线先由这里的报数建立。
_UNCLAIMABLE_PROBES = {
    1: [],   # stage-1 认领不写任何状态（这是"装好依赖重跑即全量自愈"的前提），无楔住态
    2: [
        ("stage-2 认领中未收口 (LOADING/PROCESSING)",
         """
            SELECT COUNT(*) FROM document_version
            WHERE content_process_status IN ('LOADING','PROCESSING')
              AND status = 'active'
              AND canonical_json_key IS NOT NULL
         """,
         "并发运行会正常出现；否则=上一次批次级失败未回滚认领行，"
         "满 2h 后由 _reset_stale_stage2_locks 接管重排"),
        ("stage-2 死信 (FAILED 且 retry_count>=3)",
         """
            SELECT COUNT(*) FROM document_version
            WHERE content_process_status = 'FAILED'
              AND retry_count >= 3
              AND status = 'active'
              AND canonical_json_key IS NOT NULL
         """,
         "已耗尽重试预算，认领谓词与计数谓词都不再包含它们——不查这一行就永远不会有人发现"),
    ],
    3: [
        ("stage-3 毒 chunk 死信 (chunk_meta.index_status=DEAD)",
         f"""
            SELECT COUNT(*) FROM chunk_meta
            WHERE index_status = '{ChunkIndexStatus.DEAD}' AND is_active = 1
         """,
         "达 RAG_STAGE3_CHUNK_MAX_RETRIES 上限，不在 loader 重选集内；"
         "所属文档已置 chunk_status='NEEDS_REVIEW'，需人工复位 NOT_INDEXED + 计数清零"),
        ("stage-3 版本锁未释放 (index_status=PROCESSING 且未满 2h)",
         f"""
            SELECT COUNT(*) FROM document_version
            WHERE index_status = '{DocVersionIndexStatus.PROCESSING}'
              AND updated_at >= NOW() - INTERVAL 2 HOUR
         """,
         "2h 内的锁按设计不可抢占（正常并发/刚崩溃都会出现）；持续不降即上一轮中断残留"),
    ],
}


def _probe_unclaimable_rows(stage: int) -> list:
    """返回 [(label, count, hint), ...]，只统计 count>0 的项。失败 fail-open 返回 []。"""
    probes = _UNCLAIMABLE_PROBES.get(stage) or []
    if not probes:
        return []
    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    out = []
    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cur:
            for label, sql, hint in probes:
                try:
                    cur.execute(sql)
                    cnt = int(cur.fetchone()[0])
                except Exception as e:   # 单条探针失败不牵连其余（列/表缺失等）
                    print(f"[Orchestrator] WARNING: unclaimable probe '{label}' failed: {e}",
                          file=sys.stderr)
                    continue
                if cnt:
                    out.append((label, cnt, hint))
    except Exception as e:
        # 纯可观测性，绝不影响入库结论
        print(f"[Orchestrator] WARNING: unclaimable probes skipped (non-fatal): {e}",
              file=sys.stderr)
        return []
    finally:
        if conn:
            conn.close()
    return out


class DrainTimeBudgetExceeded(RuntimeError):
    """B12：drain 触到墙钟预算（RAG_DRAIN_MAX_SECONDS）在批次边界中止。

    专用类型而非裸 RuntimeError —— 调用方/监控要能把"跑太久被叫停"与"数据出错"分开。
    """


def _assert_no_claimed_residue(claimed_ids) -> None:
    """B1a（2026-07-25）：本批认领行不得在"成功"收尾时仍停在 LOADING/PROCESSING。

    为什么需要：0-chunk / chunk-explosion 的状态收尾在落库失败时是 **fail-open**
    （`_rollback_or_discard` + print，不上抛），于是 DAG 全绿、行却停在处理中态。
    这类行既不被认领谓词（NOT_STARTED / FAILED&retry<3）收，也不在 pending 计数里 →
    「一边报绿、一边楔死」，直到 2h 陈旧接管才恢复。

    刻意的两个性质：
      · **纯只读**：不写任何状态。即时回滚需要 per-claim 所有权证明（本仓有三条转手通道：
        2h 陈旧接管、reset_for_rechunk、scratch/reset_stuck，且无 claim identity 列），
        没有它的回滚会踩到接管者 —— 那部分留作 B1b，不在此实现。
      · **fail-closed**：查询本身失败也 raise。这是安全断言不是可观测探针，查不出来
        ≠ 没问题。也刻意不加 `status='active'` 过滤：并发退役但未收口的认领行同样要被看见。
    """
    from opensearch_pipeline.pipeline_nodes import _get_db_conn
    ph = ",".join(["%s"] * len(claimed_ids))
    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, doc_id, version_no, content_process_status FROM document_version "
                f"WHERE id IN ({ph}) AND content_process_status IN ('LOADING','PROCESSING')",
                list(claimed_ids))
            rows = cur.fetchall() or []
    except Exception as e:
        raise RuntimeError(
            f"Stage 2 residue assertion could not run ({e}). Refusing to report success: "
            f"unverified claimed rows may be wedged in LOADING/PROCESSING.") from e
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    if rows:
        _shown = "; ".join(f"{r[1]} v{r[2]} (id={r[0]}, {r[3]})" for r in rows[:10])
        if len(rows) > 10:
            _shown += f" | ...(+{len(rows) - 10} more)"
        raise RuntimeError(
            f"Stage 2 finished but {len(rows)} claimed row(s) are still LOADING/PROCESSING — "
            f"their terminal state never persisted (0-chunk/explosion closure writes are "
            f"fail-open). They are invisible to both the claim and the pending-count predicates "
            f"and will only recover via the 2h stale-lock takeover. Failing the run instead of "
            f"reporting green: {_shown}")


def _stage_lock_name(stage: int, config) -> str:
    """按 **数据库** 命名空间隔离的锁名。

    MySQL 的 GET_LOCK 名字是**实例级**的：staging 与 prod 若共用同一 RDS 实例，
    裸 `rag_ingest_stage3` 会让两个环境互相阻塞。加库名后各自独立。
    """
    # 两层 getattr：锁名解析绝不能因为配置对象不完整（测试桩 / 降级配置）而抛异常 ——
    # 那会把一个纯命名操作变成入库路径上的硬失败。真实配置恒有 rds.database。
    return "rag_ingest:%s:stage%s" % (
        getattr(getattr(config, "rds", None), "database", "unknown"), stage)


class _StageLock:
    """A12 锁句柄。**不是**裸连接 —— 必须携带所有者信息才能安全可重入。

    · `conn`          持锁的裸连接（no-op 句柄为 None）
    · `name`          锁名（按库+stage 隔离）
    · `owner`         取锁线程的 ident —— 同进程**另一线程**不算可重入
    · `acquired_here` 只有它为 True 的那层才可以 RELEASE/close（嵌套层绝不释放外层锁）
    """

    __slots__ = ("conn", "name", "owner", "acquired_here")

    def __init__(self, conn, name, owner, acquired_here):
        self.conn = conn
        self.name = name
        self.owner = owner
        self.acquired_here = acquired_here


# 进程内已持有的 stage 锁：{lock_name: _StageLock}。按锁名隔离（同进程持 stage-1 时
# 绝不能把 stage-2 误判成可重入），注册表本身用一把小锁串行化。
_HELD_STAGE_LOCKS = {}
_HELD_LOCKS_GUARD = threading.Lock()


def _acquire_stage_lock(stage: int, config, simulate: bool):
    """A12（2026-07-25）：取 stage 级单实例互斥锁，返回 `_StageLock` 句柄。

    为什么需要：全仓无任何跨进程互斥（`GET_LOCK|flock|fcntl` 零命中）。stage-1 的认领是
    纯 `SELECT ... LIMIT 100`（有意为之：不写状态 ⇒ 装好依赖重跑即全量自愈），两个实例会
    领到**同一批**文档，重复付 OCR/VLM；成本熔断器也明确假定单 orchestrator，多实例各算各的。
    今天生产是人工批、单操作者，所以这是前瞻性防线；一旦挂上调度（与人工批/补数据并行）
    立刻兑现。

    三态语义（fail-**closed**）：
      · 1     → 拿到锁，返回连接（必须一直持有到 run 结束：GET_LOCK 是**会话级**的）
      · 0     → 已有实例在跑，打印后 exit 0（不是错误，不该让 DataWorks 标红）
      · NULL / 连接失败 / SQL 异常 → **exit 非零**。互斥是并发安全边界，不是辅助能力：
        取锁不成却继续跑，等于在最需要保护的场景（配置/权限/网络异常）失去保护。
        GET_LOCK 本身不需要特权，取锁异常基本只可能是"连不上 RDS"——那种情况这轮 run
        本来也跑不下去，fail-closed 零代价。逃生口是显式 RAG_INGEST_SINGLETON_LOCK=false。

    进程被 kill -9 时锁随连接断开自动释放（会话锁语义），无需陈旧锁处理。

    可重入（B1a 2026-07-25）：本函数**同时**被 `main()`（CLI，须早于 run_start 取锁）与
    `run_stage_drained()`（三个正式 DataWorks 节点的实际入口，见 stage{1,2,3}_node.py）调用。
    CLI 路径下后者是前者的嵌套调用 —— 用 (锁名, 线程 ident) 判定可重入并返回
    `acquired_here=False` 的句柄，嵌套层返回时**不释放外层锁**。同进程另一线程不算可重入
    （否则两个 drain 会在同一进程并发跑，正是本锁要防的事）。

    ⚠️ 覆盖面如实声明：本锁覆盖 CLI 与三个正式 DataWorks 节点；**不**覆盖直接调用单批
    `run_stage()` 的遗留脚本（如 scripts/dataworks_stage3_with_cleanup.py），也不覆盖
    reset_for_rechunk / scratch/reset_stuck 这类主动接管工具。
    """
    name = _stage_lock_name(stage, config)
    if simulate:
        return _StageLock(None, name, None, False)      # SIM 承诺零外部服务
    if os.environ.get("RAG_INGEST_SINGLETON_LOCK", "true").strip().lower() in ("0", "false", "no"):
        print("[Orchestrator] 单实例互斥已显式关闭（RAG_INGEST_SINGLETON_LOCK=false）")
        return _StageLock(None, name, None, False)

    _me = threading.get_ident()
    with _HELD_LOCKS_GUARD:
        _held = _HELD_STAGE_LOCKS.get(name)
        if _held is not None and _held.owner == _me:
            # 本线程已持有同名锁 → 嵌套层，不重复 GET_LOCK、不接管释放责任
            return _StageLock(_held.conn, name, _me, False)

    try:
        import pymysql
        rds = config.rds
        # 裸连接（不走 DBUtils 池）：池化连接归还后可能被复用/重置，会话锁随之释放。
        # SSL/超时必须显式复用配置——pymysql_ssl_args() 未配 CA 时返回 {"ssl_disabled": True}
        # （显式明文），同时满足 tests/test_rds_ssl_wiring 对所有 pymysql.connect 位点的扫描门。
        conn = pymysql.connect(
            host=rds.host, port=rds.port, user=rds.user, password=rds.password,
            database=rds.database, charset=rds.charset,
            connect_timeout=rds.connect_timeout, read_timeout=rds.read_timeout,
            **rds.pymysql_ssl_args()
        )
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 0)", (name,))
            row = cur.fetchone()
        got = row[0] if row else None
    except Exception as e:
        print(f"[Orchestrator] FATAL: 无法获取单实例互斥锁 '{name}': {e}\n"
              f"  互斥是并发安全边界，取不到就不跑（要绕开请显式设 "
              f"RAG_INGEST_SINGLETON_LOCK=false）。", file=sys.stderr)
        sys.exit(1)

    if got == 1:
        print(f"[Orchestrator] 已取得单实例互斥锁 '{name}'")
        handle = _StageLock(conn, name, _me, True)
        with _HELD_LOCKS_GUARD:
            _HELD_STAGE_LOCKS[name] = handle
        return handle
    try:
        conn.close()
    except Exception:
        pass
    if got == 0:
        print(f"[Orchestrator] 另一实例正在跑 stage {stage}（锁 '{name}' 被占）——本次退出，不重复处理。")
        sys.exit(0)
    print(f"[Orchestrator] FATAL: GET_LOCK('{name}') 返回 {got!r}（NULL=锁机制异常）——"
          f"拒绝在无互斥保护下运行。", file=sys.stderr)
    sys.exit(1)


def _release_stage_lock(handle) -> None:
    """释放锁并关连接。**只有 acquired_here=True 的那层**才真正释放（嵌套层是 no-op）。

    全程 fail-open —— 连接一断锁自然释放，收尾失败不该改变退出码。注册表在 finally 里清除：
    线程退出后 ident 可被复用，残留条目会让后来的线程被误判成可重入。
    """
    if handle is None or not getattr(handle, "acquired_here", False):
        return
    try:
        if handle.conn is not None:
            with handle.conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", (handle.name,))
    except Exception as e:
        print(f"[Orchestrator] WARNING: 释放互斥锁失败（连接关闭时会自动释放）: {e}",
              file=sys.stderr)
    finally:
        with _HELD_LOCKS_GUARD:
            if _HELD_STAGE_LOCKS.get(handle.name) is handle:
                del _HELD_STAGE_LOCKS[handle.name]
        try:
            if handle.conn is not None:
                handle.conn.close()
        except Exception:
            pass


def run_stage_drained(stage: int, bizdate: str, simulate: bool):
    """排空式执行：生产模式下循环调用 run_stage，直到该 stage 没有待处理行（一次调用排空整个语料）。

    - 模拟模式只跑一次：run_simulation 注入的是固定测试数据，循环会无限重复同一批。
    - no-progress 守卫：若一整批跑完后剩余行数没有下降（例如某批文档持续失败、停留在
      NOT_STARTED/FAILED），则停止并告警，避免死循环。Balanced 级别未加 Stage-1 原子抢占，
      因此该守卫是必需的。
    - run_stage 在任何批次失败时仍会 raise（沿用 fail-fast 语义），异常会冒泡到 main 退出。
    """
    from opensearch_pipeline.pipeline_run import accumulate_metrics, extract_run_metrics
    run_metrics: dict = {}

    if simulate:
        run_stage(stage, bizdate, simulate)
        return run_metrics

    # A12 补齐（B1a 2026-07-25）：单实例互斥必须在**这里**取。
    # 此前只加在 CLI main() 里，而三个正式 DataWorks 节点（stage{1,2,3}_node.py）是直接
    # `from ... import run_stage_drained` 再调用的 —— 整把锁被绕过，现网路径实际零保护。
    # CLI 路径下 main() 已在 run_start 之前取过锁，这里按 (锁名, 线程) 判定为嵌套、
    # 返回 acquired_here=False 的句柄，返回时不释放外层锁。
    _lock = _acquire_stage_lock(stage, get_config(), simulate)
    try:
        return _run_stage_drained_locked(stage, bizdate, simulate, run_metrics,
                                         accumulate_metrics, extract_run_metrics)
    finally:
        _release_stage_lock(_lock)


def _run_stage_drained_locked(stage, bizdate, simulate, run_metrics,
                              accumulate_metrics, extract_run_metrics):
    """drain 主体（已在 stage 互斥锁保护内）。拆出来只为让锁的 try/finally 覆盖全程。"""
    # P2-10：drain 全程共享一个运行级成本熔断器。此前每批 run_stage 内部新建实例，
    # __init__ 把 _run_total_rmb 归零 → run_budget_rmb 门每批重置，整个 drain 的聚合
    # 花费上界被击穿为 批数×预算。CostBreaker 线程安全（内部 Lock）、无落库副作用
    # （quarantine 由 gate 调用方触发且按 doc 幂等）；per-doc 预留台账跨批保留正是
    # 期望语义（同一 doc 的 rebuild+refine 共享 doc_budget），熔断告警也收敛为
    # 每次 drain 至多一次。
    from opensearch_pipeline.extraction.cost_breaker import CostBreaker
    shared_cost_breaker = CostBreaker(get_config())

    if stage == 3:
        # ── 搁浅版本对账：上一次部分失败可能留下「新版本已全量 INDEXED 但旧版本仍 active」
        # 的文档（双版本同时被检索）。必须在 drain 循环之前跑：这类文档没有待处理 chunk，
        # _count_pending_rows(3)==0 时 run_stage 根本不会执行。失败不阻断当日入库（优雅降级）。
        from opensearch_pipeline.spot_checker import reconcile_pending_deletes, reconcile_stranded_versions
        try:
            rec = reconcile_stranded_versions()
            if rec["total"]:
                print(f"[Orchestrator] Stranded-version reconcile: {rec['success']}/{rec['total']} "
                      f"healed, {rec['failed']} failed, "
                      f"{rec.get('skipped_stale', 0)} skipped-stale")
        except Exception as e:
            print(f"[Orchestrator] WARNING: stranded-version reconcile failed (non-fatal): {e}",
                  file=sys.stderr)
        # CS5: drain the PENDING_DELETE outbox every stage-3 run (previously only drained inside the
        # un-scheduled spot-check). Retries old-version HA3 deletes that node_deactivate / spot-check
        # retirement queued on failure. Fail-open — never blocks the day's ingestion.
        try:
            pd = reconcile_pending_deletes()
            if pd["total"]:
                print(f"[Orchestrator] Pending-delete outbox drain: {pd['success']}/{pd['total']} "
                      f"deleted, {pd['failed']} failed, "
                      f"{pd.get('skipped_stale', 0)} skipped-stale")
        except Exception as e:
            print(f"[Orchestrator] WARNING: pending-delete reconcile failed (non-fatal): {e}",
                  file=sys.stderr)
        # #2：同版本 re-chunk / chunk_meta-cleared 重灌会 strand 旧 HA3 PK——version_no 与新块【相同】，
        # 故既不在 deactivate(version_no<N) 也不在 PENDING_DELETE（按 version 删）覆盖内，此前只有【未
        # 排期】的 spot-check 会清（漏跑即长期双份/过期召回）。挂进每日 drain 做安全网：**默认 dry-run
        # 只报数**（不做不可逆 HA3 删除，尊重「HA3 删不可逆」的谨慎），仅 RAG_STAGE3_ORPHAN_PURGE=true
        # 时才真删。失败不阻断入库（fail-open，与相邻对账一致）。
        # A6（2026-07-25）：未开 RAG_STAGE3_ORPHAN_PURGE 时【整块跳过】，不再空扫。
        # 理由：reconcile_ha3_orphan_pks 的 dry_run 分支是在**枚举与分类全部完成之后**才 return，
        # 所以 dry-run 不省任何扫描时间——每轮 stage-3 都会在 drain 之前把 HA3 全 id 空间
        # （0 → MAX(chunk_meta.id)+headroom，每 500 PK 一桶）枚举一遍，只为产出一行统计；
        # 耗时随自增 id 高水位线性增长，与本轮实际工作量无关。真删仍需显式开 flag（语义不变）。
        # 注意：只改这个调用点，不动 ha3_reconcile 内部——其 __main__ 默认 dry-run 是该 CLI 的
        # 正常用法，spot_checker 也在调。
        _orphan_purge = os.environ.get("RAG_STAGE3_ORPHAN_PURGE", "").lower() in ("true", "1", "yes")
        if _orphan_purge:
            try:
                from opensearch_pipeline.ha3_reconcile import reconcile_ha3_orphan_pks
                orp = reconcile_ha3_orphan_pks(dry_run=False)
                if orp.get("stale"):
                    print(f"[Orchestrator] HA3 orphan-PK reconcile [purged]: stale={orp['stale']} "
                          f"deleted={orp.get('deleted', 0)} errors={len(orp.get('errors', []))}")
            except Exception as e:
                print(f"[Orchestrator] WARNING: HA3 orphan-PK reconcile failed (non-fatal): {e}",
                      file=sys.stderr)
        else:
            print("[Orchestrator] HA3 orphan-PK reconcile 已跳过"
                  "（未设 RAG_STAGE3_ORPHAN_PURGE=true；开启才扫描并真删）")
        # Phase D（flag 开）：投影 outbox 定向 drain——decide 端点同事务入队的受影响 doc，逐文档幂等
        # materialize（标脏 chunk_meta + index_status='NOT_INDEXED'），交本轮 drain 推 HA3。这是
        # 「decide 内联 materialize best-effort（抛/skipped_locked 漏标脏）」的【必达】兜底，先于下面的
        # 全扫 reconcile 跑（定向必达 + 全扫兜底互补）。flag 关 → skipped no-op。失败不阻断入库。
        try:
            from opensearch_pipeline.access_grants import drain_acl_projection_outbox
            ob = drain_acl_projection_outbox(commit=True)
            if not ob.get("skipped") and ob["processed"]:
                print(f"[Orchestrator] ACL projection outbox drain: done={ob['done']} "
                      f"locked={ob['locked']} failed={ob['failed']} (processed={ob['processed']})")
        except Exception as e:
            print(f"[Orchestrator] WARNING: ACL projection outbox drain failed (non-fatal): {e}",
                  file=sys.stderr)
        # Phase D（flag 开）：跨部门授权投影对账——从 approved authority 重算 allowed_depts，drift
        # 文档标脏（chunk_meta.allowed_depts + index_status='NOT_INDEXED'），交本轮 drain 推 HA3。
        # 兜住 decide 端点漏标脏 / 直接改库的 authority。flag 关 → skipped no-op。失败不阻断入库。
        try:
            from opensearch_pipeline.allowed_depts_reconcile import reconcile_allowed_depts
            ad = reconcile_allowed_depts(commit=True)
            if not ad.get("skipped") and (ad["materialized"] or ad["retracted"]):
                print(f"[Orchestrator] allowed_depts reconcile: materialized={ad['materialized']} "
                      f"retracted={ad['retracted']} reset_chunks={ad['reset_chunks']} "
                      f"errors={len(ad['errors'])}")
        except Exception as e:
            print(f"[Orchestrator] WARNING: allowed_depts reconcile failed (non-fatal): {e}",
                  file=sys.stderr)

    max_iters = int(os.environ.get("RAG_DRAIN_MAX_ITERS", "100000"))
    # B12（2026-07-25）：墙钟预算。drain 此前只有次数上限（默认十万，等于无界），
    # 一个持续慢但有进展的批可以跑到天荒地老而没有任何硬界。
    # **只在批次边界检查**：不用 signal/线程异步中断当前批（那会在任意语句处撕开事务）。
    # 到点 **raise 专用异常**而不是 exit 0 —— 「绝不静默成功」是本函数的既有不变量。
    # 默认 0 = 关（与 A1 同一姿态：机制先就位，取值是待实验的运维决策，缺 P95 证据不硬设）。
    try:
        _max_seconds = float(os.environ.get("RAG_DRAIN_MAX_SECONDS", "0") or 0)
    except ValueError:
        _max_seconds = 0.0
    _drain_started = time.monotonic()
    if _max_seconds > 0:
        print(f"[Orchestrator] drain 墙钟预算：{_max_seconds:.0f}s（RAG_DRAIN_MAX_SECONDS，批次边界检查）")
    prev_remaining = None
    iteration = 0
    while True:
        iteration += 1
        if _max_seconds > 0 and iteration > 1:
            _elapsed = time.monotonic() - _drain_started
            if _elapsed > _max_seconds:
                raise DrainTimeBudgetExceeded(
                    f"Stage {stage} drain exceeded RAG_DRAIN_MAX_SECONDS="
                    f"{_max_seconds:.0f}s (elapsed {_elapsed:.0f}s after "
                    f"{iteration - 1} batch(es)); aborting at a batch boundary so the run is "
                    f"marked failed rather than silently succeeding. Remaining rows stay claimable.")
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
            # A3：收尾必须区分「真全清」与「没有可认领的了，但还有楔住的行」——后者绝不能
            # 打印无保留的全清字样（那正是"报绿+永不入库"的观感来源）。只报数，不改退出码。
            _unclaimable = _probe_unclaimable_rows(stage)
            _blocked = sum(c for _, c, _ in _unclaimable)
            if _blocked:
                print(f"[Orchestrator] Stage {stage}: 0 claimable rows after {iteration - 1} "
                      f"batch(es)，但另有 {_blocked} 行处于不可认领状态 —— **未全清**：")
                for _label, _cnt, _hint in _unclaimable:
                    print(f"[Orchestrator]   · {_label}: {_cnt} —— {_hint}")
            else:
                print(f"[Orchestrator] Stage {stage} drained: 0 pending rows after {iteration - 1} batch(es).")
            break
        # (metrics accumulate after each batch below)
        if prev_remaining is not None and remaining >= prev_remaining:
            # 一整批跑完后剩余行数没有下降 = 有卡住/持续失败的行。必须抛错，让退出码非零，
            # 否则 DataWorks 会把卡死的运行标记为成功（绿色），无人察觉语料停止入库。
            raise RuntimeError(
                f"Stage {stage} made no progress (remaining={remaining} did not decrease "
                f"from {prev_remaining}). Stuck/failing rows — failing the run; inspect FAILED rows."
            )
        print(f"[Orchestrator] Stage {stage} drain batch #{iteration} — {remaining} rows pending...")
        prev_remaining = remaining
        _batch_ctx = run_stage(stage, bizdate, simulate, cost_breaker=shared_cost_breaker)
        accumulate_metrics(run_metrics, extract_run_metrics(_batch_ctx))

    return run_metrics


def main():
    parser = argparse.ArgumentParser(description="DataWorks Scheduling Orchestrator")
    parser.add_argument(
        "--stage", type=int, required=True, choices=[1, 2, 3],
        help="Pipeline Stage to run (1: Raw->Canonical, 2: Canonical->Chunk, 3: Chunk->OpenSearch)"
    )
    parser.add_argument(
        "--bizdate", type=str, required=True,
        help="Business date of the execution schedule (format: YYYYMMDD). "
             "PROVENANCE LABEL ONLY: stages drain by row status, bizdate never filters "
             "row selection — it cannot backfill/reprocess a specific day (P3-10)."
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
    parser.add_argument(
        "--resume", action="store_true",
        help="Print a READ-ONLY resume/recovery report (current RDS state) before draining; "
             "the drain itself is the unchanged run_stage_drained. Also via RAG_INGEST_RESUME."
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

    # Resume/recovery: print a READ-ONLY report of what the (unchanged) drain will pick up from
    # current RDS state. The drain below is untouched — no new version, no reset, never bypasses 04b.
    if args.resume or os.environ.get("RAG_INGEST_RESUME", "").lower() in ("1", "true", "yes"):
        try:
            from opensearch_pipeline.ingestion_resume import build_resume_report, format_report
            print(format_report(build_resume_report(args.stage)))
        except Exception as e:
            print(f"[Orchestrator] resume report failed (non-fatal): {e}", file=sys.stderr)

    # L6prov: per-run provenance header (RUNNING → SUCCESS/FAILED). Fail-open + no-op in simulate;
    # joins to kb_audit_log via the same git_commit/bizdate. Records which run/code/model ran this
    # stage and how it ended — the lineage capstone.
    # A12（2026-07-25）：单实例互斥。必须在 run_start **之前**取锁 —— 竞争失败要直接退出，
    # 不能留下一条 RUNNING 的 pipeline_run 记录。
    _lock_handle = _acquire_stage_lock(args.stage, config, simulate_mode)

    from opensearch_pipeline.versions import build_run_provenance
    from opensearch_pipeline.pipeline_run import run_start, run_finish
    _prov = build_run_provenance(stage=args.stage, bizdate=args.bizdate)
    _run_id = run_start(_prov, simulate=simulate_mode)

    try:
        _metrics = run_stage_drained(args.stage, args.bizdate, simulate_mode)
        run_finish(_run_id, "SUCCESS", metrics=_metrics, simulate=simulate_mode)
        print(f"\n[Orchestrator] SUCCESS: Stage {args.stage} finished successfully.")
        sys.exit(0)
    except Exception as e:
        run_finish(_run_id, "FAILED", error_message=str(e), simulate=simulate_mode)
        # OBS-4: orchestrator non-zero exit → ops alert (fail-open, no-op if webhook unset).
        try:
            from opensearch_pipeline.alerting import send_ops_alert
            send_ops_alert(
                title=f"Ingestion stage {args.stage} FAILED ({args.bizdate})",
                text=(f"- **bizdate**: `{args.bizdate}`\n- **stage**: `{args.stage}`\n"
                      f"- **run_id**: `{_run_id or 'n/a'}`\n- **error**: `{str(e)[:400]}`"),
                severity="critical", dedup_key=f"orch-fail:{args.stage}:{args.bizdate}",
            )
        except Exception:
            pass
        print(f"\n[Orchestrator] ERROR: Stage {args.stage} failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    finally:
        _release_stage_lock(_lock_handle)


if __name__ == "__main__":
    main()
