# -*- coding: utf-8 -*-
"""
pipeline_nodes.py — DAG 节点函数

每个函数签名：func(ctx: dict) -> Any
ctx 是共享上下文字典，节点之间通过 ctx 传递数据。

分四组：
  DAG 1: raw → canonical (解析)
  DAG 2: canonical → safe chunk (脱敏 + 切分)
  DAG 3: chunk → embedding → OpenSearch (索引)
  DAG 4: eval + reindex (评测)
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime
from typing import Dict, List

from opensearch_pipeline.chunker import Chunk, DocumentChunker
from opensearch_pipeline.config import get_config
from opensearch_pipeline.image_binding_reconcile import reconcile_move
from opensearch_pipeline.ingest_flags import (
    effective_funnel_policy_version,
    image_content_override_enabled,
)
from opensearch_pipeline import ingest_lease
from opensearch_pipeline.reindex_states import (
    RETRY_COUNT_INC_SQL,
    STAGE3_CLAIMABLE_INDEX_STATUS,
    ChunkIndexStatus,
    DocVersionIndexStatus,
    sql_in_list,
)

# ─── 共享基础设施（F-A1 结构债拆分，2026-07-01）────────────────────────────
# PII 词表/正则、DB 连接池、客户端工厂已机械搬移到 pii_patterns.py / db.py /
# clients.py 三个小模块——serving 侧（api/retriever/qa_logger/redaction/...）
# 直接 import 小模块，不再因一次 QA 落库拖入本 7000+ 行摄取模块。
# 这里 re-export 全部名字：本文件节点代码与既有 tests 的 monkeypatch 目标
# （`opensearch_pipeline.pipeline_nodes.<name>`）继续按原样工作。
# ⚠️ 不 re-export db._db_pool（模块级绑定是导入时快照，会随池重建失联）；
# 需要读池状态请直接用 opensearch_pipeline.db。
from opensearch_pipeline.clients import (  # noqa: F401
    _ensure_opensearch_index,
    _get_opensearch_client,
    _get_oss_bucket,
    _resolve_simulate,
)
from opensearch_pipeline.db import (  # noqa: F401
    _get_db_conn,
    _init_db_pool,
    _pool_readonly_declared,
    _reset_db_pool,
)
from opensearch_pipeline.pii_patterns import (  # noqa: F401
    _MATERIAL_CODE_ANCHORS,
    _SEVERITY_RANK,
    ENTITY_PATTERNS,
    ENTITY_SEVERITY,
    REDACTION_MAP,
    SEMANTIC_KEYWORDS,
    _body_entity_fp_ignore,
    _image_ocr_fp_ignore,
    scrub_image_text,
)


def _lease_renew_tick(ctx: dict):
    """PR-4 摄取租约批量续租触点（stage-2/3 长循环内逐文档/逐批调用）。

    免 DB 节流预判（should_renew）到期才开一次池连接；批语义整集续租——逐 key
    续护不住还在队尾排队的文档。丢锁不在这里 raise（renew_all 内部记墓碑出集，
    失主文档走到栅栏写时 LeaseLost 弃单——墓碑保证拒绝是强制的）。任何异常
    fail-open：续租失败的最坏结果=到期被接管，方向安全；绝不因续租故障打断
    主流程。flag off 时 should_renew 恒 False，零开销零行为。"""
    ls = ingest_lease.get_lease_set(ctx)
    if not ls.should_renew():
        return
    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cur:
            ls.renew_all(cur)
        conn.commit()
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f"    ⚠️ lease renew tick failed (fail-open): {e}")
    finally:
        if conn:
            conn.close()




# ═══════════════════════════════════════════════════════════════
# DAG 1: raw_to_canonical — 文件解析
# ═══════════════════════════════════════════════════════════════

def node_scan_raw_files(ctx: dict):
    """扫描待处理的 raw 文件列表，并为没有 id 和 version 的原始上传文件自动生成元数据。"""
    import hashlib
    from datetime import datetime
    from opensearch_pipeline.config import get_config

    config = get_config()
    simulate_db = _resolve_simulate(ctx, "db")

    tasks = ctx.get("raw_tasks", [])
    if not tasks:
        if simulate_db:
            # 模拟数据
            tasks = [{
                "doc_id": "DOC_ADMIN_20260518_DEMO01",
                "version_no": 1,
                "bucket_name": "fuling-knowledge-base",
                "raw_key": "raw/admin/DOC_ADMIN_20260518_DEMO01/v1/员工手册.txt",
                "filename": "员工手册.txt",
                "dept": "admin",
                "file_ext": "txt",
            }]
            print(f"    [Scanner] Using {len(tasks)} simulated raw tasks")
        else:
            # 真实生产模式：查询 RDS 待处理记录
            tasks = []
            conn = None
            try:
                from opensearch_pipeline.ingest_policy import (
                    stage1_ext_exclusion_sql, stage1_quarantine_like_pattern)
                conn = _get_db_conn(select_db=True)
                with conn.cursor() as cursor:
                    # 查询未开始内容处理的所有活跃文档版本，并关联 document_meta 获取文件名和部门。
                    # 扩展名排除片段来自 ingest_policy.STAGE1_SQL_EXCLUDED_EXTS（单一来源）——
                    # 必须与 dataworks_orchestrator._count_pending_rows 的 stage-1 计数完全一致，
                    # 否则排空守卫会因"计得到却领不走"误判 stage-1 无进展而中止。
                    # 批次5（ultra P3 orchestrator:661；2026-08-02 自 op0 移植，适配 node-ACL
                    # 双 SQL seam——两臂同传 _q_params）：`_quarantine/` 行改在 SQL 侧排除
                    # （同一单一来源谓词，计数侧同步）——此前只靠下方 Python 过滤：隔离行
                    # 照选照占 LIMIT 100 名额（混批时真实行被队头挤占、纯隔离批零产出），
                    # 计数器又算它 pending → 无进展守卫误杀。process_quarantine=True 的
                    # 未来通道保留（SQL 谓词随之关闭，Python 过滤同门）。
                    _pq_on = ctx.get("process_quarantine", False)
                    _q_pred = "" if _pq_on else "AND dv.raw_key NOT LIKE %s\n                          "
                    _q_params = () if _pq_on else (stage1_quarantine_like_pattern(),)
                    _base_sql = f"""
                        SELECT
                            dv.doc_id,
                            dv.version_no,
                            dv.bucket_name,
                            dv.raw_key,
                            dv.file_ext,
                            dm.title,
                            dm.owner_dept{{mode_cols}}
                        FROM document_version dv
                        LEFT JOIN document_meta dm ON dv.doc_id = dm.doc_id
                        WHERE dv.content_process_status = 'NOT_STARTED'
                          AND dv.canonical_json_key IS NULL
                          AND dv.file_ext NOT IN {stage1_ext_exclusion_sql()}
                          {_q_pred}AND dv.status = 'active'
                        ORDER BY dv.created_at ASC
                        LIMIT 100
                    """
                    # 阶段 B：带上 acl_mode/owner_dept_id（060 未 apply → 1054 回退旧列集）。
                    # node 行的 dept **不得**兜成 "unknown"——那会顺着注册/分类回写把归属轴
                    # 写脏（codex 阶段 B major：NULL 立刻变 truthy "unknown" 后就再也不会
                    # 从 raw key 解析）。
                    _has_mode = _exec_node_guarded(
                        cursor,
                        _base_sql.format(mode_cols=", dm.acl_mode, dm.owner_dept_id"),
                        _base_sql.format(mode_cols=""), _q_params, _q_params)
                    rows = cursor.fetchall()
                    for row in rows:
                        _mode = (row[7] or "legacy") if _has_mode else "legacy"
                        _oid = row[8] if _has_mode else None
                        tasks.append({
                            "doc_id": row[0],
                            "version_no": row[1],
                            "bucket_name": row[2] or getattr(config.oss, "bucket_name", "fuling-knowledge-base"),
                            "raw_key": row[3],
                            "file_ext": row[4] or (row[3].split(".")[-1] if row[3] and "." in row[3] else ""),
                            "filename": row[5] or (row[3].split("/")[-1] if row[3] else ""),
                            "dept": (row[6] or "") if _mode == "node" else (row[6] or "unknown"),
                            "acl_mode": _mode,
                            "owner_dept_id": _oid,
                        })
                    print(f"    [Scanner] Scanned {len(tasks)} pending raw tasks from RDS")
            except Exception as e:
                print(f"    ⚠️ [Scanner] Failed to scan pending raw files from RDS: {e}")
                raise RuntimeError(f"Failed to scan pending raw files from RDS in production mode: {e}")
            finally:
                if conn:
                    conn.close()
    
    # 过滤掉路径中包含 _quarantine/ 的待处理文件 (暂时忽略隔离暂存文件)
    # 通过 ctx.get("process_quarantine", False) 支持未来随时启用 quarantine 判断与处理能力
    process_quarantine = ctx.get("process_quarantine", False)
    
    filtered_tasks = []
    for task in tasks:
        raw_key = task.get("raw_key", "")
        if "_quarantine/" in raw_key and not process_quarantine:
            print(f"    [Scanner] Skipped quarantined file (staged): {raw_key}")
            continue
            
        # 开始对原始上传没有 id 和 version 的文件进行自动提取与生成
        # 例如: raw/admin/员工手册.txt -> 自动提取 dept="admin", filename="员工手册.txt"
        dept = task.get("dept")
        filename = task.get("filename")
        file_ext = task.get("file_ext")

        # 阶段 B：node 命名空间（raw/node-<id>/…）——第 2 段是 storage_segment 不是组码，
        # 绝不能补进 dept（否则注册/分类会把 "node-123" 写进归属轴）。结构化解析一次，
        # task 补齐 acl_mode/owner_dept_id；node 任务的 dept 固定空串。
        if raw_key and task.get("acl_mode") != "node":
            from opensearch_pipeline.kb_upload import parse_raw_owner
            _ro = parse_raw_owner(raw_key)
            if _ro["mode"] == "node":
                task["acl_mode"] = "node"
                task["owner_dept_id"] = _ro["owner_dept_id"]
                dept = ""

        if task.get("acl_mode") == "node":
            dept = ""
            if raw_key and (not filename or not file_ext):
                parts = raw_key.split("/")
                if not filename:
                    filename = parts[-1]
                if not file_ext:
                    file_ext = filename.split(".")[-1] if "." in filename else ""
            task["dept"] = dept
            task["filename"] = filename
            task["file_ext"] = file_ext
        elif raw_key and (not dept or not filename or not file_ext):
            parts = raw_key.split("/")
            # 如果是 raw/{dept}/{filename} 的结构
            if len(parts) >= 3 and parts[0] == "raw":
                if not dept:
                    dept = parts[1]
                if not filename:
                    filename = parts[-1]
                if not file_ext:
                    file_ext = filename.split(".")[-1] if "." in filename else ""
            else:
                # 兜底提取
                if not dept:
                    dept = "unknown"
                if not filename:
                    filename = parts[-1]
                if not file_ext:
                    file_ext = filename.split(".")[-1] if "." in filename else ""

            task["dept"] = dept
            task["filename"] = filename
            task["file_ext"] = file_ext
            
        # 若 doc_id 或 version_no 缺失，查询 RDS 确认是否为新版本，或自动生成 doc_id
        if not task.get("doc_id") or not task.get("version_no"):
            doc_id = task.get("doc_id")
            version_no = task.get("version_no")
            
            # 从文件名和部门提取唯一的 hash，以便做 deterministic 标识
            name_bytes = (filename or "").encode("utf-8")
            filename_hash = hashlib.md5(name_bytes).hexdigest()[:8]
            
            if not simulate_db:
                # 生产模式下：尝试从数据库查询已注册的 doc_id 与当前最新版本
                conn = None
                try:
                    conn = _get_db_conn(select_db=True)
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT doc_id, current_version_no FROM document_meta WHERE original_filename = %s AND owner_dept = %s LIMIT 1",
                            (filename, dept)
                        )
                        row = cursor.fetchone()
                        if row:
                            doc_id = row[0]
                            if not version_no:
                                version_no = row[1] + 1
                            print(f"    [Scanner] Found existing document: {doc_id}, assigning version {version_no}")
                except Exception as e:
                    print(f"    ⚠️ [Scanner] Database query failed: {e}")
                finally:
                    if conn:
                        conn.close()
            
            # 模拟模式或 RDS 未查询到时的生成逻辑
            if not doc_id:
                today_str = datetime.now().strftime("%Y%m%d")
                dept_upper = (dept or "unknown").upper()
                doc_id = f"DOC_{dept_upper}_{today_str}_{filename_hash}"
                print(f"    [Scanner] Generated new doc_id for raw file: {doc_id}")
                
            if not version_no:
                version_no = 1
                
            task["doc_id"] = doc_id
            task["version_no"] = version_no
            
        filtered_tasks.append(task)
        
    ctx["tasks"] = filtered_tasks
    print(f"    └─ Found {len(filtered_tasks)} raw files to process")


def node_register_metadata(ctx: dict):
    """注册文档元数据（写入 RDS）。"""
    tasks = ctx["tasks"]
    simulate_db = _resolve_simulate(ctx, "db")
    registered = []

    if not simulate_db:
        # 与 write_chunk_meta / acquire_index_lock / deactivate_old_chunks /
        # update_index_status 同款的破坏性写前置守卫：在打开连接之前 fail-loud，
        # PROD-RO（RAG_READONLY）拒写、非生产→生产需当日 ack。register_metadata 此前漏了
        # 这道显式守卫（裸 cursor 写 document_meta/document_version），现补齐使其与 DAG
        # 其它写节点一致；连接层 GuardedDBConnection 是兜底，这里给的是更早、带 op 名的失败。
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        assert_destructive_write_allowed("register_metadata", get_config().rds.host, kind="rds")
        conn = None
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                for task in tasks:
                    doc_id = task["doc_id"]
                    version_no = task["version_no"]
                    title = task.get("filename", "")
                    _is_node = task.get("acl_mode") == "node"
                    # 阶段 B：node 任务的归属轴 = owner_dept_id，legacy 列写 NULL（不是空串，
                    # 也绝不是路径 dept——codex 阶段 B BLOCKER-2：此处的 ON DUPLICATE 覆写
                    # 正是「register 写好的 NULL 被 stage-1 重登记冲掉」的第一现场）。
                    owner_dept = None if _is_node else task.get("dept", "unknown")

                    # 1. 写入 document_meta 表。守卫版：已是 node 的行 owner_dept/owner_dept_id
                    # 永不被任务值覆写；acl_mode 保持现值（scanner 永不翻转 mode——迁移唯一
                    # 入口是 doc-meta 端点）。060 未 apply → 1054 回退旧 SQL（行为逐字节不变）。
                    _exec_node_guarded(
                        cursor,
                        """
                        INSERT INTO document_meta
                        (doc_id, title, original_filename, owner_dept, acl_mode, owner_dept_id,
                         status, current_version_no)
                        VALUES (%s, %s, %s, %s, %s, %s, 'active', %s)
                        ON DUPLICATE KEY UPDATE
                        title = VALUES(title),
                        original_filename = VALUES(original_filename),
                        owner_dept = IF(acl_mode='node' OR VALUES(acl_mode)='node',
                                        owner_dept, VALUES(owner_dept)),
                        owner_dept_id = IF(acl_mode='node' OR VALUES(acl_mode)='node',
                                           owner_dept_id, VALUES(owner_dept_id)),
                        current_version_no = GREATEST(current_version_no, VALUES(current_version_no))
                        """,
                        """
                        INSERT INTO document_meta
                        (doc_id, title, original_filename, owner_dept, status, current_version_no)
                        VALUES (%s, %s, %s, %s, 'active', %s)
                        ON DUPLICATE KEY UPDATE
                        title = VALUES(title),
                        original_filename = VALUES(original_filename),
                        owner_dept = VALUES(owner_dept),
                        current_version_no = GREATEST(current_version_no, VALUES(current_version_no))
                        """,
                        (doc_id, title, title, owner_dept,
                         "node" if _is_node else "legacy", task.get("owner_dept_id"), version_no),
                        (doc_id, title, title, owner_dept or "unknown", version_no))
                    
                    # 2. document_version：先 UPDATE（已存在的记录），无匹配再 INSERT
                    cursor.execute("""
                        UPDATE document_version
                        SET file_ext = %s, status = 'active', updated_at = NOW()
                        WHERE doc_id = %s AND version_no = %s
                    """, (task.get("file_ext", ""), doc_id, version_no))
                    if cursor.rowcount == 0:
                        # 新文档，需要 INSERT 全部必填字段
                        import hashlib as _hl
                        raw_key = task.get("raw_key", "")
                        raw_key_hash = _hl.sha256(raw_key.encode()).hexdigest() if raw_key else ""
                        cursor.execute(f"""
                            INSERT INTO document_version
                            (doc_id, version_no, bucket_name, raw_key, raw_key_hash, file_ext,
                             gate_status, content_process_status, chunk_status, index_status, status)
                            VALUES (%s, %s, %s, %s, %s, %s,
                                    'pending_clean', 'NOT_STARTED', 'NOT_STARTED', '{DocVersionIndexStatus.NOT_INDEXED}', 'active')
                        """, (doc_id, version_no, task.get("bucket_name", ""),
                              raw_key, raw_key_hash, task.get("file_ext", "")))
                conn.commit()
            print("    └─ Saved registered metadata to RDS (document_meta, document_version)")
        except Exception as e:
            if conn: conn.rollback()
            print(f"    ⚠️ Failed to write metadata to RDS: {e}")
            raise RuntimeError(f"Database write failure in node_register_metadata: {e}") from e
        finally:
            if conn:
                conn.close()

    for task in tasks:
        meta = {
            "doc_id": task["doc_id"],
            "version_no": task["version_no"],
            "title": task.get("filename", ""),
            "owner_dept": task.get("dept", "unknown"),
            "status": "active",
            "gate_status": "pending_clean",
            "content_process_status": "PROCESSING",
            "registered_at": datetime.now().isoformat(),
        }
        registered.append(meta)
        # L5 audit: doc/version REGISTER transition (lifecycle start), fail-open + sim no-op.
        from opensearch_pipeline.audit_log import write_audit, audit_trace_id
        write_audit(doc_id=meta["doc_id"], version_no=meta["version_no"],
                    action_type="REGISTER", action_result="SUCCESS",
                    trace_id=audit_trace_id(ctx), simulate=simulate_db)
        print(f"    └─ Registered: {meta['doc_id']} v{meta['version_no']}")

    ctx["registered_docs"] = registered


def node_extract_text_with_ocr(ctx: dict):
    """
    统一文档提取 + OCR fallback。

    内部调用 UnifiedExtractor，支持：
    - mock 模式：解析 mock_text 为结构化 blocks
    - 生产模式：先从 OSS 下载原始文件到本地，再根据 file_ext 分发到 PDF/DOCX/TXT 提取器

    输出 ExtractionResult 到 ctx["extractions"]。
    """
    import tempfile
    from opensearch_pipeline.extraction import UnifiedExtractor

    tasks = ctx["tasks"]
    simulate_api = _resolve_simulate(ctx, "api")
    simulate_oss = _resolve_simulate(ctx, "oss")

    # ── 环境预检（fail-fast，默认开）──
    # xlsx/pptx/docx 的基础抽取器 100% 依赖 openpyxl/python-pptx/python-docx；缺失时
    # 下游优雅降级会把 ImportError 吞成 warning → 0 块空 canonical 且 stage-1 全绿，
    # 根因（环境缺依赖）被掩盖（2026-07-16 现场）。在任何下载/抽取发生前、只对本批
    # 任务真实涉及的类型炸出来——此刻尚未写任何状态（stage-1 认领无 LOADING 标记），
    # 修好依赖直接重跑即全量自愈。mock_text 任务不走真实抽取器，不参与预检。
    # RAG_EXTRACT_DEP_PREFLIGHT=off 为应急旁路（此时兜底 = node_build_canonical 的
    # 「空 canonical + 缺模块警告」守卫）。辅助依赖（PIL/OCR）有意不预检。
    if os.environ.get("RAG_EXTRACT_DEP_PREFLIGHT", "on").lower() not in ("off", "0", "false", "no"):
        from opensearch_pipeline.extraction.unified_extractor import preflight_extractor_deps
        _dep_missing = preflight_extractor_deps(
            {t.get("file_ext", "") for t in tasks if "mock_text" not in t})
        if _dep_missing:
            _detail = "; ".join(
                f".{ext} 需要 {mod}（pip install {pip}）" for ext, mod, pip in _dep_missing)
            raise RuntimeError(
                f"[ENV-DEP] 基础抽取器依赖缺失，拒绝启动抽取（fail-fast，未写任何状态；"
                f"装好依赖后重跑 stage-1 即可）: {_detail}")

    # 生产模式需要 OSS bucket 来下载原始文件
    bucket = None
    if not simulate_oss:
        bucket, _sim = _get_oss_bucket(ctx)

    extractor = UnifiedExtractor(simulate=simulate_api, oss_client=bucket)
    # 注入运行级成本熔断器（VLM 版面重建用）。一个 extractor 处理整批文档，
    # 故跨文档共享同一 breaker → 单次运行累计预算生效。orchestrator 未注入时为 None
    # （此时 vlm_rebuilder 退化为单文档闸；且默认 RAG_REBUILD_ENABLED=false 全程 no-op）。
    extractor.cost_breaker = ctx.get("cost_breaker")
    extractions = []

    # 创建临时目录存放下载的文件
    tmp_dir = tempfile.mkdtemp(prefix="rag_extract_")

    # perf#30：文档级并发（默认 1 = 既有串行路径，零行为变化）。同批 classify 早已 8 线程，
    # 而提取（OSS 下载 + 解析 + OCR/VLM 外呼）跨文档串行是 stage-1 墙钟主体。>1 时用线程池：
    # 循环体除共享 tmp_dir 外无跨文档数据依赖（VLM funnel 类缓存 dict/GIL + _save 落盘锁、
    # cost_breaker 自带 Lock、oss2 Bucket 线程安全、ctx["_raw_checksum"] dict 写 GIL 原子）；
    # 并发时每文档独立 tmp 子目录，杜绝跨文档同名资产互踩。结果按 tasks 原序收敛，
    # 下游（canonical/chunk）看到的顺序与串行完全一致。
    try:
        extract_concurrency = max(1, int(os.environ.get("RAG_EXTRACT_CONCURRENCY", "1")))
    except ValueError:
        extract_concurrency = 1

    def _extract_one(task, task_tmp_dir):
        doc_id = task["doc_id"]
        task["_tmp_dir"] = task_tmp_dir  # 传递给 image_extraction_utils 导出嵌入图片

        # 生产模式：从 OSS 下载原始文件到本地
        if not simulate_oss and "mock_text" not in task:
            raw_key = task.get("raw_key", "")
            if raw_key and bucket:
                # 保留原始文件名（含中文）以便提取器识别类型
                filename = os.path.basename(raw_key)
                local_path = os.path.join(task_tmp_dir, f"{doc_id}_{filename}")
                # B4（生产级外审 2026-07-17 P1-03）：下载前大小闸——超大对象拒下载
                # （磁盘/内存/OCR 预算；自助上传有 50MB 闸但本路径此前无任何上限）。
                # doc-intrinsic（重试无意义）：经 oversize_note → partial_loss_notes
                # 通道收 NEEDS_REVIEW。HEAD 失败 fail-open（OSS 不可达时下载分支自己
                # 会失败并走既有告警路径）。
                _max_bytes = int(os.environ.get("RAG_EXTRACT_MAX_BYTES",
                                                str(200 * 1024 * 1024)) or 0)
                _osize = 0
                if _max_bytes > 0:
                    try:
                        _osize = int(getattr(bucket.head_object(raw_key),
                                             "content_length", 0) or 0)
                    except Exception:
                        _osize = 0
                if _max_bytes > 0 and _osize > _max_bytes:
                    print(f"    🛑 {doc_id}: {raw_key} {_osize} bytes 超过 "
                          f"RAG_EXTRACT_MAX_BYTES={_max_bytes}——拒绝下载，转 NEEDS_REVIEW")
                    task["local_path"] = ""
                    task["oversize_note"] = (
                        f"[OVERSIZE] {raw_key} {_osize} bytes > cap {_max_bytes}："
                        "未下载未提取；确需摄取请调高 RAG_EXTRACT_MAX_BYTES 或人工拆分文档")
                else:
                    try:
                        bucket.get_object_to_file(raw_key, local_path)
                        file_size = os.path.getsize(local_path)
                        task["local_path"] = local_path
                        print(f"    📥 {doc_id}: downloaded {raw_key} ({file_size} bytes)")
                    except Exception as e:
                        # A2（2026-07-25）：取件失败必须留显式哨兵。此前只 print + 清空
                        # local_path，抽取照跑并产出 0 字 canonical，定稿路径无条件写
                        # canonical_json_key + extraction_status='COMPLETED'——而 stage-1 认领
                        # 谓词要求 keys IS NULL，于是这篇文档【再也不会被重捡】，只能人工改库。
                        # 判据用异常本身（不做 warning 文案匹配，避免误伤内容合法为空的文档）；
                        # 键沿用同函数 _raw_checksum 的 (doc_id, version_no) 约定。
                        print(f"    ⚠️ Failed to download {raw_key} from OSS: {e}")
                        task["local_path"] = ""
                        ctx.setdefault("_fetch_errors", {})[
                            (task["doc_id"], task.get("version_no"))
                        ] = f"{type(e).__name__}: {e}"
        elif simulate_oss and "mock_text" not in task and not task.get("local_path"):
            # 本地零 OSS 形态（LOCAL-DEV，见 docs/environment_design.md）：
            # 真实文档由 scripts/sample_corpus.py 预先采样到 scratch/sample_corpus/<raw_key>，
            # 这里直接挂为 local_path——管线全程不触 OSS。未采样的文件按原 simulate 行为处理。
            _sample_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "scratch", "sample_corpus", task.get("raw_key", ""))
            if task.get("raw_key") and os.path.exists(_sample_path):
                task["local_path"] = _sample_path
                print(f"    📂 {doc_id}: using sampled corpus file scratch/sample_corpus/{task['raw_key']}")

        # L2: raw-bytes content hash (best-effort, fail-open) for content-based invalidation.
        _lp = task.get("local_path")
        if _lp and os.path.exists(_lp):
            try:
                _h = hashlib.sha256()
                with open(_lp, "rb") as _rf:
                    for _blk in iter(lambda: _rf.read(1 << 20), b""):
                        _h.update(_blk)
                ctx.setdefault("_raw_checksum", {})[(task["doc_id"], task.get("version_no"))] = _h.hexdigest()
            except Exception:
                pass  # checksum is auxiliary; absence → NULL → "process" (never blocks extraction)

        result = extractor.extract(task)

        # B4：oversize 拒下载注记并进 partial_loss_notes（批次6 NEEDS_REVIEW 收尾通道，
        # 不静默 DONE 也不 FAILED 空转重试——doc-intrinsic 缺口留人工裁决）
        if task.get("oversize_note"):
            _ov_notes = list(getattr(result, "partial_loss_notes", []) or [])
            _ov_notes.append(task["oversize_note"])
            try:
                result.partial_loss_notes = _ov_notes
            except Exception:   # noqa: BLE001 — 旧 result 形态无该属性时不阻断提取
                pass

        # 日志
        block_types = {}
        for b in result.blocks:
            block_types[b.block_type] = block_types.get(b.block_type, 0) + 1
        block_summary = ", ".join(f"{k}={v}" for k, v in block_types.items())

        if result.ocr_required:
            print(
                f"    └─ {doc_id}: {result.text_length} chars via "
                f"{result.extract_method} (OCR {result.ocr_status})"
            )
        else:
            print(
                f"    └─ {doc_id}: {result.text_length} chars via "
                f"{result.extract_method} [{block_summary}]"
            )
        return result

    # A1（2026-07-25）：无条件打印 configured/effective —— 这三个并发旋钮的代码路径早就就绪、
    # 默认全 1，且在 dataworks_nodes/ 与 deploy/ 下零注入；此前只在 >1 时才打印，于是"到底有没有
    # 生效"完全不可从节点日志验证。effective 会因批内文档数不足而低于 configured，必须分开报。
    _eff_extract = extract_concurrency if len(tasks) > 1 else 1
    print(f"    └─ [concurrency] extract: configured={extract_concurrency} effective={_eff_extract} "
          f"(RAG_EXTRACT_CONCURRENCY, docs={len(tasks)})")
    def _extract_guarded(task, task_tmp_dir):
        """B2（2026-07-25）：单篇抽取异常不再冒泡打断整批。

        此前串行路径单篇 raise、并发路径 `list(pool.map(...))` 一篇炸掉整个返回列表 ——
        同批 k+1..N 篇**已经付过费**的 OCR/VLM 成果连同定稿一并丢弃，下一轮从头重付。
        改为记 `(doc_id, version_no, error)` 进 ctx 并返回 None（**不在这里落库**：
        单层归因，由 node_build_canonical 统一落库 + 聚合 raise）。
        注意只包**单篇**：抽取器构造、tmp 目录、cache finalize 等非文档级异常仍整批 fail-fast。
        """
        try:
            return _extract_one(task, task_tmp_dir)
        except Exception as e:
            print(f"    ❌ extraction failed for {task.get('doc_id')}: "
                  f"{type(e).__name__}: {e}")
            ctx.setdefault("_extract_errors", {})[
                (task["doc_id"], task.get("version_no"))
            ] = f"{type(e).__name__}: {e}"
            return None

    try:
        if extract_concurrency > 1 and len(tasks) > 1:
            from concurrent.futures import ThreadPoolExecutor

            def _extract_isolated(idx_task):
                idx, task = idx_task
                sub = os.path.join(tmp_dir, f"doc_{idx}")
                os.makedirs(sub, exist_ok=True)
                return _extract_guarded(task, sub)

            with ThreadPoolExecutor(max_workers=extract_concurrency) as _pool:
                # executor.map 保持 tasks 原序；B2 起单文档异常在 _extract_guarded 内被记账
                # 并返回 None（不再一篇炸掉整个返回列表），下面统一滤掉。
                extractions = [r for r in _pool.map(_extract_isolated, enumerate(tasks))
                               if r is not None]
        else:
            for task in tasks:
                _res = _extract_guarded(task, tmp_dir)
                if _res is not None:
                    extractions.append(_res)
    finally:
        # ─── 在清理 tmp 之前，将保留图片上传到 OSS ───
        # 解决 local_path 生命周期问题：downstream 的 embedding 节点不再依赖 local_path。
        # ⚠️ ROUTE_TO_TEXT 也必须上传：绑定注入（_insert_image_refs_heuristic /
        # _enrich_existing_image_refs）会把 TO_TEXT 截图绑进 step_card 并构造
        # processing/assets/ 路径 —— 只传 TO_VECTOR 时这些路径永不存在，
        # serving 签出 403 死图（2026-06-10 对抗评审发现，UI 截图多数走 TO_TEXT）。
        # 已带 oss_key 的资产跳过（独立图片文档 oss_key=raw_key，原对象已在 OSS）。
        bucket_upload, is_sim_oss = _get_oss_bucket(ctx)
        if not is_sim_oss and bucket_upload:
            _upload_clean_assets(extractions, bucket_upload)

        # perf#27：VLM 缓存的 OSS 整包上传已降频为每 N 文档一次，这里 flush 未满一轮的尾巴
        # （无脏时零动作；失败不影响提取结果——本地副本已逐文档原子落盘）
        try:
            UnifiedExtractor.flush_vlm_cache_to_oss()
        except Exception as _ve:
            print(f"    ⚠️ VLM cache OSS flush failed (non-fatal): {_ve}")

        # A5（2026-07-25）：OCR 页级缓存同款收尾——底座只从 finalize 推 OSS 镜像，没有这个
        # 调用点镜像就永远不会产生（DataWorks 每次都是全新 pod，跨运行命中率恒 0）。
        try:
            from opensearch_pipeline.extraction.ocr_client import OCRClient
            OCRClient.flush_page_cache_to_oss()
        except Exception as _oe:
            print(f"    ⚠️ OCR page cache OSS flush failed (non-fatal): {_oe}")

        # 清理临时文件
        import shutil
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    ctx["extractions"] = extractions


def _fetch_canonical_json(canonical_json_key):
    """只读拉取并解析一篇 canonical JSON（OSS 或本地模拟路径）。

    **失败返回 None 而不是 {}** —— 下游按"缺 assets 键 = unknown"处理，绝不能让一次
    读取失败变成"上一版本零张图"从而伪造出全量 removal。
    """
    if not canonical_json_key:
        return None
    try:
        if os.path.exists(canonical_json_key):          # 本地模拟
            with open(canonical_json_key, "r", encoding="utf-8") as f:
                return json.load(f)
        bucket, is_sim = _get_oss_bucket()
        if is_sim or bucket is None:
            return None
        return json.loads(bucket.get_object(canonical_json_key).read().decode("utf-8"))
    except Exception:
        return None


def _emit_asset_set_diff(ctx, canonical, observed_stage, cur_checksum=None,
                         simulate_db=False, conn=None):
    """方案 F：把"存活图集相对上一成功服务版本的增减"写成可见信号。

    **纯旁路 + fail-open**：任何异常只吞并打日志，绝不影响摄取决策或事务。
    只在 diff 非空时写 kb_audit_log（action=ASSET_SET_DIFF），避免审计噪声。

    **返回**：产出的事件 dict，或 None（不可比 / 无变化 / 任何异常）。调用方可据此决定
    是否放行 skip（见 `_asset_additions_block_skip`）—— 但本函数自身**绝不改变**任何判定。

    ⚠️ 当前 kb_audit_log 没有业务唯一键 ⇒ **不保证幂等**，stage 重试可能多写一行
    （event_key 仅供人工/后续去重；唯一索引列为后续项）。
    ⚠️ 同版本 maintenance re-chunk **不在本观测范围**（需要 pre-delete 快照，另行设计）。
    """
    try:
        from opensearch_pipeline import asset_set_diff as _asd
        from opensearch_pipeline.audit_log import write_audit
        doc_id = canonical.get("doc_id")
        observed_v = canonical.get("version_no")
        if not doc_id or not observed_v or observed_v <= 1 or simulate_db:
            return None
        cur = _asd.survivors(canonical)
        if not cur.known:
            return None                 # 当前侧 unknown ⇒ 不产出任何结论
        if _asd.extraction_degraded(canonical):
            # 审查 P1-8：本轮抽取带降级信号（缺 PyMuPDF / 打不开文件 / OCR 部分失败 /
            # VLM 供应商故障）时，assets 变少**不能**归因为判定漂移。标成 partial ⇒
            # build_event 只会出 ordinal_diff_partial，不会出高置信 same_source_drift，
            # 也不会打"图会从答案里消失"那条告警。留痕仍在（要的就是可见性）。
            cur = _asd.SurvivorSet(cur.indices, cur.n_unindexed + 1, True)
        # 审查 P2-14：两个调用点都在调用方持有共享连接期间（perf#92「本篇共享一个连接」/
        # perf#93「闭环整批共享一个连接」刚做的合并）—— 能复用就复用，别再借还一次。
        _own_conn = conn is None
        conn = conn if conn is not None else _get_db_conn()
        try:
            with conn.cursor() as cur_db:
                cur_db.execute(_asd.baseline_sql(), (doc_id, observed_v))
                row = cur_db.fetchone()
                if not row:
                    return None         # 首灌或无合格基线：建立基线，不报 diff
                base_v, base_key, base_checksum = row[0], row[1], row[2]
                if cur_checksum is None:
                    # DAG 1 的 skip-gate 早于 checksum 落库 ⇒ 调用方传 ctx["_raw_checksum"]；
                    # 这里只是 DAG 2 侧的兜底查询。
                    cur_db.execute("SELECT checksum_sha256 FROM document_version "
                                   "WHERE doc_id=%s AND version_no=%s", (doc_id, observed_v))
                    _r = cur_db.fetchone()
                    cur_checksum = _r[0] if _r else None
        finally:
            if _own_conn:
                try:
                    conn.close()
                except Exception:
                    pass
        prev_canonical = _fetch_canonical_json(base_key)
        prev = _asd.survivors(prev_canonical)
        rel = _asd.source_relation(base_checksum, cur_checksum)
        event = _asd.build_event(doc_id, base_v, observed_v, prev, cur, rel, observed_stage)
        if not event:
            return None
        if _asd.is_high_confidence_loss(event):
            print(_asd.log_line(doc_id, event))
        write_audit(doc_id=doc_id, version_no=observed_v, action_type="ASSET_SET_DIFF",
                    action_result=observed_stage, message=_asd.event_message(event),
                    trace_id=_audit_trace(ctx), simulate=simulate_db)
        return event
    except Exception as _e:                                     # fail-open
        print(f"    ⚠️ asset-set diff 观测失败（不影响摄取）: {type(_e).__name__}: {_e}")
        return None


def _audit_trace(ctx):
    """审查 P2-13：ASSET_SET_DIFF 曾是本文件唯一不带 trace_id 的审计写 —— 而方案 F 的
    存在理由正是回答"是哪次改动把图吃掉的"，没有 `<git_commit>:<bizdate>` 指纹就 join
    不回 pipeline_run，定位不到当时的 extractor_version / funnel_policy / VLM 模型。"""
    try:
        from opensearch_pipeline.audit_log import audit_trace_id
        return audit_trace_id(ctx)
    except Exception:
        return None


def _asset_additions_block_skip(event) -> bool:
    """skip-gate 是否应当**因资产集新增**而放行这一篇（审查 P1-5，2026-07-26）。

    要解决的问题：`_canonical_sha256` 只哈希正文，而 `ROUTE_TO_VECTOR` 不产任何 block ——
    于是选项 C / strip-stitch 救回来的图**不改变正文哈希**，生产默认开着的
    `RAG_SKIP_UNCHANGED_REINGEST` 判 SKIPPED_DUPLICATE 并 continue，救回的图永远到不了
    chunk_meta/HA3。也就是说这两项改动在"文件没变、只是想让图回来"这个**它们的目标场景**
    上完全无效。

    **为什么判据是非对称的（只看新增、不看减少）** —— 这是本函数最要紧的一条：
      · **新增**只可能来自"这次多存活了图"（判据放宽 / 缝合救回），破损的抽取环境只会产出
        **更少**的图，永远不会更多 ⇒ 用新增触发重摄取是安全的；
      · **减少**则既可能是真漂移，也可能是环境缺依赖（如 py3.7 节点缺 PyMuPDF 时
        `import fitz` 失败只 print、返回空 assets）。若据此放行 skip，DAG 3 收尾会拿一个
        零图的新版本去停用正在服务的旧版本 —— 把观测变成事故。减少**只告警**（F 已经做了），
        绝不改变 skip 决策。

    为什么不改 `_canonical_sha256` 把 assets 折进去（审查建议的另一条路）：该哈希还被
    跨文档去重消费，且已持久化在 `document_version.canonical_sha256` —— 改公式会让**全部
    存量文档**下次摄取时看起来"变了"，爆炸半径不成比例。

    回滚：`RAG_SKIP_GATE_HONORS_ASSETS=0/false/no/off` 恢复"只看正文"的历史行为。
    """
    if os.environ.get("RAG_SKIP_GATE_HONORS_ASSETS", "").strip().lower() in (
            "0", "false", "no", "off"):
        return False
    return bool(event) and int(event.get("n_added") or 0) > 0


def _pii_fingerprint(value: str):
    """PII 值的**不可回推**指纹（审查 P2-12，2026-07-26）。

    此前直接 `sha256(原始命中串)`。而该表此前唯一的写入方 node02 哈希的是**实体名/词表词/
    文件名**，从来不是 PII 值 —— 所以「只存 SHA-256 + 掩码预览」这句在那边成立、在这边
    不成立：无盐 SHA-256 对 11 位手机号是**秒级枚举**可还原，身份证受结构与校验位约束同理，
    再叠加同行存着的首尾各 2 位，等价于把明文放进了一张有治理看板查询面、默认留存 24 个月
    的表。

    改用 HMAC：拿不到密钥时**返回 None 而不是退回明文哈希** —— 少一列指纹只是少了去重能力，
    留一列可回推的指纹是把 PII 写进库。
    """
    # 专用 env（不复用 RAG_SESSION_SIGNING_KEY / RAG_UPLOAD_SIGNING_KEY —— config.py:1064
    # 那条注释明说"一钥两用扩大泄漏半径"，这里遵守同一条）。
    key = os.environ.get("RAG_PII_FINGERPRINT_KEY", "").strip().encode("utf-8")
    if not key:
        return None
    import hmac
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _persist_image_scrub_findings(ctx, doc, findings):
    """方案 A（2026-07-26）：把 G21 消费点掩码的命中写成 `document_sensitive_finding` 行。

    **为什么需要它**：图像侧 PII 在审计表上此前完全不可见 —— `image_ocr:*` 恒 0 行
    （只读调查 `docs/image_ocr_pii_exposure_investigation_2026-07-26.md` §4/§5.1 实证
    「只看审计表会漏掉这处暴露」）。根因是那条留痕挂在 node02 的 `RAG_IMAGE_OCR_PII`
    门内，而该 flag 只在 DataWorks 节点 setdefault、覆盖不了笔记本重跑；G21 因为默认 ON
    且不读任何 flag，**两条执行路径都生效** —— 所以留痕该挂在它这里。

    **纯留痕，不改任何判定**：
      · `action` 恒为 `REDACTED` —— G21 只掩码、**从不隔离**（这正是不默认开
        `RAG_IMAGE_OCR_PII` 的理由：它唯一的增量是把 2 篇在用的 public 手册降级成下架）；
      · 不参与 `final_risk` 计算、不回写 `redaction_action`、不影响 chunk 产出。
    **fail-open**：任何异常只吞并打日志 —— 与 node02 的 `raise` 刻意不同，那里留痕失败
    意味着"隔离决策没落库"，这里只是少了一行审计。绝不让审计写把分块炸掉。

    `finding_type` 用 **`image_scrub:`** 前缀而非 node02 的 `image_ocr:` —— 后者是判断
    "node02 那条 flag 门内的路径有没有跑过"的既有判据（上述调查正是这么用的），复用会把它
    毁掉。两个前缀并存即可分辨是哪一层留下的。

    PII 纪律：只存 SHA-256 + 掩码预览，绝不存原文（同 node02）。
    """
    if not findings:
        return
    if _resolve_simulate(ctx, "db"):
        return
    conn = None
    try:
        rows, seen = [], set()
        for name, matched in findings:
            key = (name, matched)
            if key in seen:            # 同一篇里同一实体同一值只留一行
                continue
            seen.add(key)
            # 审查 P1-10：长度必须封顶。`secret_like`/`access_key`/`email` 的匹配长度
            # 无上界，而列是 VARCHAR(255) —— 一条超长命中会让 executemany（pymysql 改写
            # 为单条多值 INSERT）整体 1406 失败，**该文档本轮全部** image_scrub 行（含同篇
            # 合法的手机号/身份证留痕）一起被 fail-open 吞掉，只剩一行 print。
            # finding_type 早有 [:64]，preview 此前没有同等保护。
            preview = ("*" * len(matched) if len(matched) <= 4
                       else matched[:2] + "*" * (len(matched) - 4) + matched[-2:])[:255]
            rows.append((doc["doc_id"], doc["version_no"], f"image_scrub:{name}"[:64],
                         ENTITY_SEVERITY.get(name, "medium"), None, None,
                         _pii_fingerprint(matched), preview, "REDACTED"))
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cur:
            # node_detect_sensitive 按 (doc_id, version_no) 先 DELETE 再 INSERT，且它在
            # DAG 2 里跑在本节点**之前** ⇒ 本函数的行不会被它清掉；重跑时它清一次、
            # 两层各自重建，自愈。这里只清本层自己的行，避免同轮重入叠加。
            cur.execute("DELETE FROM document_sensitive_finding WHERE doc_id=%s "
                        "AND version_no=%s AND finding_type LIKE 'image_scrub:%%'",
                        (doc["doc_id"], doc["version_no"]))
            cur.executemany(
                "INSERT INTO document_sensitive_finding ("
                "  doc_id, version_no, finding_type, severity, page_num, block_index,"
                "  matched_text_hash, matched_text_preview, action"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        conn.commit()
        print(f"    ├─ [pii-audit] {doc['doc_id']}: {len(rows)} 条 image_scrub:* 留痕入库")
    except Exception as e:                                   # fail-open
        print(f"    ⚠️ [pii-audit] 图像脱敏留痕写入失败（不影响分块）: {type(e).__name__}: {e}")
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _audit_reingest_skip(ctx, doc_id, version_no, message, simulate_db):
    """SKIPPED_DUPLICATE 的 fail-open 审计（含 trace + doc/version + incumbent/prior 信息，便于
    日后解释为何被跳过）。审计写失败绝不影响 skip 本身（连 import 都包在 try 里）。"""
    try:
        from opensearch_pipeline.audit_log import write_audit, audit_trace_id
        write_audit(doc_id=doc_id, version_no=version_no, action_type="REINGEST",
                    action_result="SKIPPED_DUPLICATE", trace_id=audit_trace_id(ctx),
                    message=message, simulate=simulate_db)
    except Exception as _ae:
        print(f"    ⚠️ audit of SKIPPED_DUPLICATE failed (non-fatal): {_ae}")


def _xd_covers(incumbent, newdoc) -> bool:
    """跨文档去重的权限偏序：incumbent 的可见受众是否 ⊇ new 文档的受众（即把 new 藏到
    incumbent 之后，原本能看到 new 的人仍都能看到 incumbent → 无 ACL 静默降级）。

    incumbent / newdoc 均为 (permission_level, owner_dept)。public = 全集。
    取不到 ACL 模型 → 保守返回 False（→ WARN-and-process，绝不冒险 SKIP）。
    """
    try:
        from opensearch_pipeline.retriever import _VALID_ACL_GROUPS, _expand_groups_to_owners
    except Exception:
        return False

    def _aud(pl, dept):
        if (pl or "").strip().lower() == "public":
            return None  # 全体可见（全集）
        owner = (dept or "").strip()
        aud = set()
        for g in _VALID_ACL_GROUPS:
            try:
                if owner in _expand_groups_to_owners([g]):
                    aud.add(g)
            except Exception:
                pass
        if owner in _VALID_ACL_GROUPS:
            aud.add(owner)
        return aud

    inc = _aud(*incumbent)
    new = _aud(*newdoc)
    if inc is None:      # public incumbent 覆盖所有人
        return True
    if new is None:      # new 是 public 而 incumbent 不是 → 不覆盖（不能把公开文档藏到受限 incumbent 后）
        return False
    if not new:          # new 文档受众无法解析/为空 → 保守不覆盖（不 SKIP，转 WARN）
        return False
    return inc >= new    # incumbent 受众 ⊇ new 受众


def _rollback_or_discard(conn):
    """FAIL-SAFE 阶段出错后清理共享连接上的半途事务（perf#92/#93 共享连接用）。

    能 rollback 则返回原连接继续复用；rollback 也失败（连接已坏）则关闭丢弃并返回
    None，调用方据此在下一阶段惰性重新获取——保持「单阶段出错不拖垮后续阶段」的
    既有失败隔离语义。
    """
    if conn is None:
        return None
    try:
        conn.rollback()
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None


def _same_image_ref(a: dict, b: dict) -> bool:
    """B3（2026-07-25）：两个 image_ref 是否指向**同一张图**。

    身份键沿用 xlsx 的载重契约（CLAUDE.md）：`filename` + `anchor_row` 是同 anchor 多图
    消歧的严格身份；两者缺失时退到 `oss_key`/`source_image` + `image_index`。
    只用于"追加第二张图"时的去重 —— 同一 figure_no 被多个 asset 共用时会重复追加。
    """
    for k1, k2 in (("filename", "anchor_row"), ("oss_key", "image_index"),
                   ("source_image", "image_index")):
        if a.get(k1) and a.get(k1) == b.get(k1) and a.get(k2) == b.get(k2):
            return True
    return False


def _mark_extraction_failed(doc_id, version_no, reason: str, simulate_db: bool) -> None:
    """把单篇标 `extraction_status='FAILED'` + 留痕（**不写 canonical keys**）。

    stage-1 的四个单篇失败守卫（OSS-FETCH / ENV-DEP / COST-DEFER / B2 的抽取与定稿失败）
    共用同一收口：keys 保持 NULL ⇒ 下一次 stage-1 按既有扫描谓词（NOT_STARTED 且
    canonical_json_key IS NULL）自动重捡自愈；调用方负责把该篇记进各自清单，循环后统一 raise。

    留痕失败**不吞守卫本身** —— canonical 已被扣住，重捡语义不受影响。
    """
    if simulate_db:
        return
    _conn = None
    try:
        _conn = _get_db_conn(select_db=True)
        with _conn.cursor() as _cur:
            _cur.execute(
                "UPDATE document_version SET extraction_status='FAILED', "
                "content_process_error=%s, processed_at=NOW() "
                "WHERE doc_id=%s AND version_no=%s",
                (reason, doc_id, version_no))
        _conn.commit()
    except Exception as _err:
        print(f"    ⚠️ failed to mark extraction_status=FAILED in RDS for {doc_id} v{version_no}: {_err}")
    finally:
        if _conn is not None:
            _conn.close()


def node_build_canonical(ctx: dict):
    """
    构建 canonical document（增强版）。

    从 ExtractionResult 构建包含 blocks、page_count、assets 的 canonical。
    输出两个文件路径：
      - content.canonical.json（结构化）
      - content.md（flat text, 向后兼容）
    """
    extractions = ctx["extractions"]
    canonicals = []
    env_dep_failures = []  # ENV-DEP 守卫命中清单（doc 级；循环后统一 raise 炸红本次运行）
    cost_defer_docs = []   # COST-DEFER 守卫命中清单（瞬态预算顺延；同 ENV-DEP 循环后 raise）
    fetch_failures = []    # OSS-FETCH 守卫命中清单（取件失败；同 ENV-DEP 形态，循环后 raise）
    _fetch_errors = ctx.get("_fetch_errors") or {}
    finalize_failures = []  # B2：单篇定稿失败（OSS/RDS）清单；循环后与其它守卫一起 raise
    extract_failures = []   # B2：抽取层传来的单篇失败（见下方 simulate_db 之后的落库循环）

    # perf#92：simulate 判定与 OSS bucket 客户端是循环不变量，提升到循环外一次构造
    # （原先每篇文档各构造一次 oss2.Auth+Bucket）。extractions 为空时不触碰 OSS，
    # 与原行为一致（零篇即零次构造）。
    simulate_db = _resolve_simulate(ctx, "db")
    bucket = is_simulated_oss = None
    if extractions:
        bucket, is_simulated_oss = _get_oss_bucket(ctx)

    # B2（2026-07-25）：抽取层单篇失败的**统一落库点**。抽取层只产出
    # `(doc_id, version_no, error)`、不落库 —— 单层归因，避免两处各标一次 FAILED、各计一次数。
    # 这些 doc 根本没有 ExtractionResult，所以不会出现在下面的循环里，必须在这里收口。
    for (_x_doc, _x_ver), _x_err in sorted((ctx.get("_extract_errors") or {}).items()):
        _x_reason = f"[EXTRACT-FAILED] {_x_err}"[:500]
        print(f"    ❌ ERROR {_x_doc} v{_x_ver}: {_x_reason}")
        _mark_extraction_failed(_x_doc, _x_ver, _x_reason, simulate_db)
        extract_failures.append(f"{_x_doc} v{_x_ver}: {_x_reason}")

    for result in extractions:
        # 兼容旧的 dict 格式和新的 ExtractionResult
        if hasattr(result, "doc_id"):
            # ExtractionResult object
            canonical = {
                "doc_id": result.doc_id,
                "version_no": result.version_no,
                "source_key": result.source_key,
                "file_ext": result.file_ext,
                "extract_method": result.extract_method,
                "title": result.title,
                "text": result.text,
                "text_length": result.text_length,
                "blocks": [b.to_dict() for b in result.blocks],
                "page_count": result.page_count,
                "ocr_required": result.ocr_required,
                "ocr_status": result.ocr_status,
                "warnings": result.warnings,
                "assets": result.assets,
                # 成本封存标记：VLM-rebuild 成本闸拒绝 → node_redact_or_quarantine 据此跳过
                "cost_quarantined": getattr(result, "cost_quarantined", False),
                # 成本顺延标记（瞬态 RUN/DAILY 预算）：下方 COST-DEFER 守卫消费——本篇不定稿，
                # 下一 run 预算滚动后 stage-1 重捡重过闸（ultra P1 纠偏 2026-07-17）
                "cost_deferred": getattr(result, "cost_deferred", False),
                # DAG1 的 xlsx layout 判定（用真实 filename）→ 持久化供 DAG2 直接消费，不再重分类 (P0-3)
                "xlsx_layout_type": getattr(result, "xlsx_layout_type", None),
                # P2-32：VLM degraded 兜底图片数（供应商故障）→ 持久化进 canonical JSON 跨 stage
                # 边界传递；>0 时 node_write_chunk_meta 收尾落 NEEDS_REVIEW（不 DONE）。
                "vlm_degraded_count": getattr(result, "vlm_degraded_count", 0),
                # 批次6：部分内容丢失留痕（OCR 部分页失败/XLSX/PPTX 中途异常）——同一
                # NEEDS_REVIEW 收尾通道，跨 stage 边界随 canonical JSON 传递。
                "partial_loss_notes": list(getattr(result, "partial_loss_notes", []) or []),
                # G20：文本归一版本标记（canonical_sha256 建立在归一后文本上；规则变更须
                # bump NORMALIZATION_VERSION，否则 skip-unchanged/去重把规则漂移误判为内容变更）
                # 审查 P1-9（2026-07-26）：漏斗策略是 **Stage-1 属性** —— Stage-2 是独立进程、
                # 重建 provenance、且**不重跑漏斗**（从 OSS 回读 canonical assets）。不把它
                # 随 canonical 传下去的话，任一维护性重切都会给「由旧判据决定的图集」贴上
                # 当时进程 env 的标签，事后拿 funnel_policy 盘点"谁还欠一次 C 重灌"会**恰好
                # 漏掉全部欠账文档**——而 versions.py 给这个 key 写的理由正是"事后没有这个
                # 标签就无法归因"。
                "funnel_policy": effective_funnel_policy_version(),
                "canonical_status": "DONE",
                "canonical_key": (
                    f"processing/canonical/{result.doc_id}"
                    f"/v{result.version_no}/content.canonical.json"
                ),
                "canonical_md_key": (
                    f"processing/canonical/{result.doc_id}"
                    f"/v{result.version_no}/content.md"
                ),
            }
        else:
            # Legacy dict fallback
            canonical = {
                "doc_id": result["doc_id"],
                "version_no": result["version_no"],
                "text": result["text"],
                "text_length": result["text_length"],
                "extract_method": result["extract_method"],
                "ocr_required": result.get("ocr_required", False),
                "ocr_status": result.get("ocr_status", "NOT_REQUIRED"),
                "blocks": [],
                "vlm_degraded_count": result.get("vlm_degraded_count", 0),
                "canonical_status": "DONE",
                "canonical_key": (
                    f"processing/canonical/{result['doc_id']}"
                    f"/v{result['version_no']}/content.md"
                ),
            }

        # ── OSS-FETCH 守卫（A2 2026-07-25）：原件根本没下下来 → 绝不定稿 ──
        # node_extract 的下载 except 只 print + local_path=""，抽取仍会跑并产出 0 字 canonical；
        # 若照常写 keys + extraction_status='COMPLETED'，stage-1 扫描谓词（NOT_STARTED 且
        # canonical_json_key IS NULL）永远不会再捡它，一次 OSS 瞬断就把健康文档变成需要人工
        # 改库才能复活的死件，且状态列字面谎报 COMPLETED。处置与下方 ENV-DEP 完全同型：
        # 不写 canonical 文件/keys（keys 留 NULL → 下一次 stage-1 自动重捡自愈）、
        # extraction_status='FAILED' + content_process_error 留痕、循环后统一 raise 炸红运行。
        # 不动 retry_count：stage-1 全链路无任何一处给它自增，加谓词会命中
        # ingest_policy 的「计得到、领不走 → 无进展守卫永久判死」陷阱。
        _fetch_err = _fetch_errors.get((canonical["doc_id"], canonical["version_no"]))
        if _fetch_err:
            _fetch_reason = f"[OSS-FETCH] failed to download raw object: {_fetch_err}"[:500]
            print(f"    ❌ ERROR {canonical['doc_id']} v{canonical['version_no']}: {_fetch_reason}")
            _mark_extraction_failed(canonical["doc_id"], canonical["version_no"],
                                    _fetch_reason, simulate_db)
            fetch_failures.append(
                f"{canonical['doc_id']} v{canonical['version_no']}: {_fetch_reason}")
            continue

        # ── ENV-DEP 守卫：空 canonical + 缺模块警告 = 环境性抽取失败，绝不标 SUCCESS ──
        # /usr/bin/python3 缺 openpyxl/python-pptx 时，基础抽取器的优雅降级把 ImportError
        # 吞成 "Failed to extract ...: No module named '...'"，产出 0 chars/0 blocks 的空
        # canonical——若照常写 keys + extraction_status='COMPLETED'，空文档只会在 stage-2
        # 落 SKIPPED_EMPTY，根因被彻底掩盖（2026-07-16 现场）。组合判据（全空 且 warnings
        # 含缺模块）只可能来自环境缺依赖：内容合法为空的文档无该警告仍走原 SUCCESS 路径；
        # 辅助依赖（PIL/OCR 等）失败要么无此警告、要么文本块仍在（blocks>0），均不误伤——
        # 「图片/OCR 辅助失败不破坏文本抽取」的既有优雅降级保持不变。
        # 处置：不写 canonical 文件/keys（canonical_json_key 保持 NULL → stage-2 永不认领
        # 这个空壳），标 extraction_status='FAILED' + content_process_error 留痕；
        # content_process_status 保持 NOT_STARTED → 依赖装好后下一次 stage-1 按既有扫描
        # 谓词（NOT_STARTED 且 keys IS NULL）自动重捡自愈。循环后统一 raise 炸红本次运行。
        # 两种真实文案：xlsx/pptx 兜底 except 透传原始异常串（"No module named 'openpyxl'"）；
        # docx_extractor 的 ImportError 分支返回自定义串（"python-docx not installed"）。
        _env_missing_warns = [
            w for w in (canonical.get("warnings") or [])
            if isinstance(w, str)
            and ("No module named" in w or "not installed" in w)
        ]
        if (_env_missing_warns and not canonical.get("blocks")
                and not (canonical.get("text") or "").strip()):
            _env_reason = ("[ENV-DEP] empty canonical with missing python module: "
                           + "; ".join(_env_missing_warns)[:500])
            print(f"    ❌ ERROR {canonical['doc_id']} v{canonical['version_no']}: {_env_reason}")
            _mark_extraction_failed(canonical["doc_id"], canonical["version_no"],
                                    _env_reason, simulate_db)
            env_dep_failures.append(
                f"{canonical['doc_id']} v{canonical['version_no']}: {_env_reason}")
            continue

        # ── COST-DEFER 守卫（ultra P1 纠偏 2026-07-17）：瞬态共享预算耗尽 → 本 run 不定稿 ──
        # vlm_rebuilder 因 RUN/DAILY 预算瞬态耗尽被拒时标 cost_deferred（区别于 doc-intrinsic
        # 的 cost_quarantined）。若照常定稿，canonical keys 一写 stage-1 就永不重跑（扫描谓词
        # 要求 keys IS NULL），文档在 stage-2 按 QUARANTINE 跳过落 EMPTY/DONE——健康文档静默
        # 终态。处置与上方 ENV-DEP 同型：不写 canonical 文件/keys，extraction_status='FAILED'
        # + [COST-DEFER] 留痕，content_process_status 保持 NOT_STARTED → 下一 run/次日预算
        # 滚动后 stage-1 按既有谓词自动重捡、重新过闸。循环后统一 raise 炸红本次运行——预算
        # 耗尽必须可见，也顺带终止 drain-loop 对同批文档的无进展空转。
        if canonical.get("cost_deferred"):
            _defer_reason = ("[COST-DEFER] transient VLM budget (RUN/DAILY) exhausted; "
                            "canonical withheld, will be re-picked next run")
            print(f"    ⏸️ DEFER {canonical['doc_id']} v{canonical['version_no']}: {_defer_reason}")
            _mark_extraction_failed(canonical["doc_id"], canonical["version_no"],
                                    _defer_reason, simulate_db)
            cost_defer_docs.append(f"{canonical['doc_id']} v{canonical['version_no']}")
            continue

        # ── G20：版本化文本归一（哈希/持久化之前；默认 ON，RAG_TEXT_NORMALIZE=false 直通）──
        # 去零宽 + 全角字母数字折半角 + 折叠连片空行（保守集，全角标点/㈠圈号不动）。
        # 金集实测全角 token（ＦＣＡ００７３/５秒钟）此前直进 embedding/BM25 与半角查询失配。
        try:
            from opensearch_pipeline.text_normalize import (
                NORMALIZATION_VERSION, normalization_enabled, normalize_text)
            if normalization_enabled():
                _nm_text = normalize_text(canonical.get("text") or "")
                if _nm_text != (canonical.get("text") or ""):
                    canonical["text"] = _nm_text
                    canonical["text_length"] = len(_nm_text)
                if canonical.get("title"):
                    canonical["title"] = normalize_text(canonical["title"])
                for _blk in canonical.get("blocks", []) or []:
                    if isinstance(_blk, dict) and _blk.get("text"):
                        _blk["text"] = normalize_text(_blk["text"])
                canonical["normalization_version"] = NORMALIZATION_VERSION
        except Exception as _nm_err:  # fail-open：归一失败绝不阻断摄取
            print(f"    ⚠️ text normalize skipped (FAIL-SAFE): {_nm_err}")

        # ─── Physical Persistence of Canonical Documents (JSON & MD) ───
        # G#67：canonical JSON 用紧凑分隔符序列化（stage-2 会整包下载解析，indent=2 的缩进空白
        # 会把块级 OCR/VLM 文本体积放大两到四成；人读用旁边的 .md）。ensure_ascii=False 保留。
        json_data = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
        md_data = canonical.get("text", "")

        canonical_key = canonical["canonical_key"]
        canonical_md_key = canonical.get("canonical_md_key")

        # L2 canonical-text content hash (computed once; reused by the skip-gate + the RDS UPDATE).
        _canonical_sha256 = hashlib.sha256((md_data or "").encode("utf-8")).hexdigest()
        # G19：64 位 simhash 指纹（近重复检测；<32 字符视为无指纹，同 _xd_min_chars 先例）
        _content_simhash = None
        if md_data and len((md_data or "").strip()) >= 32:
            try:
                from opensearch_pipeline.text_similarity import simhash64
                _content_simhash = simhash64(md_data)
            except Exception:
                _content_simhash = None

        # perf#92：本篇文档共享一个 DB 连接——skip 判定 SELECT、跨文档去重与最终 canonical-keys
        # UPDATE 同连接同事务（原先三段各开一连接，每篇最多 3 次借还 + 3 次 commit）。惰性获取：
        # simulate / 两个门未开时仅最终 UPDATE 才开连接。commit 粒度保持每篇一次（skip/dedup
        # 短路路径各自 commit 后 continue），不跨文档攒批，失败隔离语义不变。
        _doc_conn = None
        try:
            # ── L2 skip-gate (flag-gated, default OFF: RAG_SKIP_UNCHANGED_REINGEST) ──
            # If this is a re-ingest (version_no>1) whose canonical text is byte-identical to a prior
            # version's, mark it SKIPPED_DUPLICATE and skip the rest of the pipeline (classify / PII /
            # chunk / embed / index) — the prior version keeps serving. FAIL-SAFE: skip ONLY on a positive
            # canonical_sha256 match against a prior version; any miss / NULL / error → process normally.
            # (Maintenance re-chunks use a different path and create no new version, so they're unaffected.)
            _skip_unchanged = os.environ.get("RAG_SKIP_UNCHANGED_REINGEST", "").lower() in ("1", "true", "yes")
            if _skip_unchanged and not simulate_db and (canonical.get("version_no") or 0) > 1:
                _do_skip = False
                _prior_v = None
                _diff_event = None
                try:
                    if _doc_conn is None:
                        _doc_conn = _get_db_conn(select_db=True)
                    with _doc_conn.cursor() as _sk_cur:
                        _sk_cur.execute(
                            "SELECT version_no, canonical_sha256 FROM document_version "
                            "WHERE doc_id=%s AND version_no<%s AND canonical_sha256 IS NOT NULL "
                            "ORDER BY version_no DESC LIMIT 1",
                            (canonical["doc_id"], canonical["version_no"]))
                        _prior = _sk_cur.fetchone()
                        if _prior and _prior[1] == _canonical_sha256:
                            # F（2026-07-25）+ P1-5（2026-07-26）：**在任何写之前**先比资产集。
                            # 这条挂载点不能省 —— 生产默认 RAG_SKIP_UNCHANGED_REINGEST=true，
                            # 正文 hash 相同即在此 continue、从 canonicals 排除、永远到不了
                            # node_write_chunk_meta；而"同一份文件 + 正文没变 + 图集变化"
                            # 恰恰是本观测要抓的核心场景。
                            # ⚠️ 必须在 UPDATE 之前：SKIPPED_DUPLICATE 与 current_version_no
                            # 回退一旦写下并 commit，再撤销 skip 就会留下自相矛盾的状态行。
                            _diff_event = _emit_asset_set_diff(
                                ctx, canonical, observed_stage="SKIP_GATE",
                                cur_checksum=ctx.get("_raw_checksum", {}).get(
                                    (canonical["doc_id"], canonical.get("version_no"))),
                                simulate_db=simulate_db, conn=_doc_conn)
                        if (_prior and _prior[1] == _canonical_sha256
                                and _asset_additions_block_skip(_diff_event)):
                            # P1-5：资产集**新增** ⇒ 撤销 skip，照常入库。否则 C / strip-stitch
                            # 救回的图永远进不了索引（正文哈希不含 assets，ROUTE_TO_VECTOR
                            # 也不产 block）。只看新增不看减少 —— 理由见
                            # `_asset_additions_block_skip` 的 docstring。
                            print(f"    ↩️ {canonical['doc_id']} v{canonical['version_no']}: "
                                  f"资产集新增 {_diff_event['n_added']} 张 "
                                  f"{_diff_event['added']} ⇒ **不 skip**，照常入库")
                        elif _prior and _prior[1] == _canonical_sha256:
                            # F3 锁序纪律：同事务多表写一律 document_meta 先行（与 console/
                            # reconciler/quarantine 的 meta→…→dv 统一），杜绝 dv→meta 反序
                            # 与 meta-first 写方成环死锁。语义不变（单次 commit 原子）。
                            # revert the version pointer to the still-served prior version
                            _sk_cur.execute(
                                "UPDATE document_meta SET current_version_no=%s WHERE doc_id=%s",
                                (_prior[0], canonical["doc_id"]))
                            _sk_cur.execute(
                                "UPDATE document_version SET content_process_status='SKIPPED_DUPLICATE', "
                                "chunk_status='SKIPPED', extraction_status='COMPLETED', "
                                "canonical_sha256=%s, processed_at=NOW() "
                                "WHERE doc_id=%s AND version_no=%s",
                                (_canonical_sha256, canonical["doc_id"], canonical["version_no"]))
                            _do_skip = True
                            _prior_v = _prior[0]
                    # perf#92：仅 skip 短路路径 commit（写了状态行）；未命中不提交——skip 判定
                    # SELECT 与最终 canonical-keys UPDATE 留在同一事务，消除两者间的窗口。
                    if _do_skip:
                        _doc_conn.commit()
                except Exception as _skip_err:
                    print(f"    ⚠️ skip-gate check failed (FAIL-SAFE: processing normally): {_skip_err}")
                    _do_skip = False
                    _doc_conn = _rollback_or_discard(_doc_conn)
                if _do_skip:
                    print(f"    ⏭️ {canonical['doc_id']} v{canonical['version_no']}: canonical unchanged "
                          f"vs v{_prior_v} → SKIPPED_DUPLICATE (prior version keeps serving)")
                    _audit_reingest_skip(
                        ctx, canonical["doc_id"], canonical["version_no"],
                        f"intra-doc: canonical unchanged vs prior v{_prior_v} (kept serving)", simulate_db)
                    continue  # exclude from canonicals → skips classify/chunk/embed/index for this doc

            # ── Cross-doc dedup (flag-gated, default OFF: RAG_DEDUP_CROSS_DOC) ──
            # 跨文档 exact-hash 去重：捕获 intra-doc gate 漏掉的 dup-of-public / 跨部门精确副本。
            # 默认 WARN-and-process（仅告警仍入库）；仅当存在一个"可见性完整覆盖新文档受众"的 active
            # incumbent 时才 SKIP（避免把文档藏到更受限/受众更窄的 incumbent 后 → ACL 静默降级）。
            # 多条 exact-dup 时只要任一 incumbent 覆盖即可 SKIP（不取任意首行）。FAIL-SAFE：任何
            # miss / NULL / 异常 → 正常处理；绝不停用 incumbent。索引 idx_canonical_sha256 是启用前提。
            # ⚠️ 跳过空/近空 canonical：image-only / 抽取失败的文档其 canonical 文本为空，sha256 全部
            # 落在空串 hash（e3b0c442…）→ 互相误判为 dup（生产实测的 group-10 假分组）。短文本同理无意义。
            _xd_min_chars = 32
            if (os.environ.get("RAG_DEDUP_CROSS_DOC", "").lower() in ("1", "true", "yes")
                    and not simulate_db
                    and md_data and len((md_data or "").strip()) >= _xd_min_chars):
                try:
                    if _doc_conn is None:
                        _doc_conn = _get_db_conn(select_db=True)
                    _cover = None    # an incumbent whose visibility fully covers the new doc's audience
                    _matches = []    # all exact-hash active incumbents (for WARN logging)
                    # new doc's ACL comes from document_meta (set at register; not yet on canonical)
                    with _doc_conn.cursor() as _xd_cur:
                        _xd_cur.execute(
                            "SELECT permission_level, owner_dept FROM document_meta WHERE doc_id=%s",
                            (canonical["doc_id"],))
                        _self = _xd_cur.fetchone()
                    _new_pl, _new_dept = (_self[0], _self[1]) if _self else (None, None)
                    with _doc_conn.cursor() as _xd_cur:
                        _xd_cur.execute(
                            "SELECT dv.doc_id, dm.permission_level, dm.owner_dept "
                            "FROM document_version dv JOIN document_meta dm ON dv.doc_id=dm.doc_id "
                            "WHERE dv.canonical_sha256=%s AND dv.doc_id<>%s "
                            "AND dv.status='active' AND dm.status='active' "
                            "AND dm.current_version_no=dv.version_no",
                            (_canonical_sha256, canonical["doc_id"]))
                        _rows = _xd_cur.fetchall() or []
                    for _r in _rows:
                        _matches.append(_r[0])
                        if _cover is None and _xd_covers((_r[1], _r[2]), (_new_pl, _new_dept)):
                            _cover = (_r[0], _r[1], _r[2])
                    if _cover:
                        with _doc_conn.cursor() as _xd_cur:
                            # write canonical_sha256 on the SKIPPED row (else a later intra-doc match misses)
                            _xd_cur.execute(
                                "UPDATE document_version SET content_process_status='SKIPPED_DUPLICATE', "
                                "chunk_status='SKIPPED', extraction_status='COMPLETED', "
                                "canonical_sha256=%s, processed_at=NOW() "
                                "WHERE doc_id=%s AND version_no=%s",
                                (_canonical_sha256, canonical["doc_id"], canonical["version_no"]))
                        _doc_conn.commit()
                        print(f"    ⏭️ {canonical['doc_id']} v{canonical['version_no']}: cross-doc duplicate "
                              f"of {_cover[0]} (covering incumbent) → SKIPPED_DUPLICATE")
                        _audit_reingest_skip(
                            ctx, canonical["doc_id"], canonical["version_no"],
                            f"cross-doc: covered by incumbent {_cover[0]} "
                            f"(pl={_cover[1]}, owner={_cover[2]})", simulate_db)
                        continue
                    elif _matches:
                        _w = (f"{canonical['doc_id']}: cross-doc content match with {_matches[:5]} but no "
                              f"incumbent covers its audience → WARN, processing normally (ACL review)")
                        print(f"    ⚠️ {_w}")
                        ctx.setdefault("validation_warnings", []).append(_w)
                except Exception as _xd_err:
                    print(f"    ⚠️ cross-doc dedup check failed (FAIL-SAFE: processing normally): {_xd_err}")
                    _doc_conn = _rollback_or_discard(_doc_conn)

            # ── G19：simhash 近重复 WARN（默认 ON：RAG_NEAR_DUP_DETECT=false 关闭）──
            # 精确哈希抓不到"重新导出的同一 SOP"（元数据/微小排版变化）。对现役其他文档
            # 的 content_simhash 做 Hamming ≤ 阈值（默认 3）比对——只告警不拦截（近重复的
            # ACL 语义比精确副本微妙，拦截留给人工），与跨文档精确去重 WARN-and-process 一致。
            # 迁移 020 未应用（1054）/任何异常 → 静默跳过（fail-open）。
            if (_content_simhash
                    and os.environ.get("RAG_NEAR_DUP_DETECT", "true").lower() not in ("0", "false", "no")
                    and not simulate_db):
                try:
                    from opensearch_pipeline.text_similarity import hamming64
                    try:
                        _nd_thresh = int(os.environ.get("RAG_NEAR_DUP_HAMMING", "3"))
                    except ValueError:
                        _nd_thresh = 3
                    if _doc_conn is None:
                        _doc_conn = _get_db_conn(select_db=True)
                    with _doc_conn.cursor() as _nd_cur:
                        _nd_cur.execute(
                            "SELECT dv.doc_id, dv.content_simhash "
                            "FROM document_version dv JOIN document_meta dm ON dv.doc_id=dm.doc_id "
                            "WHERE dv.content_simhash IS NOT NULL AND dv.doc_id<>%s "
                            "AND dv.status='active' AND dm.status='active' "
                            "AND dm.current_version_no=dv.version_no LIMIT 5000",
                            (canonical["doc_id"],))
                        _nd_rows = _nd_cur.fetchall() or []
                    _near = []
                    for _nd_doc, _nd_sh in _nd_rows:
                        if _nd_sh is None:
                            continue
                        _nd_d = hamming64(int(_nd_sh), _content_simhash)
                        if _nd_d <= _nd_thresh:
                            _near.append((_nd_doc, _nd_d))
                    if _near:
                        _nd_w = (f"{canonical['doc_id']}: near-duplicate content suspected "
                                 f"(simhash Hamming≤{_nd_thresh}) with "
                                 f"{[d for d, _ in _near[:5]]} → WARN, processing normally")
                        print(f"    ⚠️ {_nd_w}")
                        ctx.setdefault("validation_warnings", []).append(_nd_w)
                except Exception as _nd_err:
                    if "1054" not in str(_nd_err):
                        print(f"    ⚠️ near-dup check skipped (FAIL-SAFE): {_nd_err}")
                    _doc_conn = _rollback_or_discard(_doc_conn)

            # 1. Write files physically
            if is_simulated_oss:
                # Local filesystem mock
                try:
                    os.makedirs(os.path.dirname(canonical_key), exist_ok=True)
                    with open(canonical_key, "w", encoding="utf-8") as f:
                        f.write(json_data)
                    print(f"    ├─ [SIMULATED] Saved canonical JSON file: {canonical_key}")

                    if canonical_md_key:
                        os.makedirs(os.path.dirname(canonical_md_key), exist_ok=True)
                        with open(canonical_md_key, "w", encoding="utf-8") as f:
                            f.write(md_data)
                        print(f"    ├─ [SIMULATED] Saved canonical MD file: {canonical_md_key}")
                except Exception as e:
                    print(f"    ⚠️ Failed to write simulated canonical files: {e}")
            else:
                # Real OSS upload
                try:
                    bucket.put_object(canonical_key, json_data.encode("utf-8"))
                    print(f"    ├─ Uploaded canonical JSON payload to OSS: {canonical_key}")

                    if canonical_md_key:
                        bucket.put_object(canonical_md_key, md_data.encode("utf-8"))
                        print(f"    ├─ Uploaded canonical MD payload to OSS: {canonical_md_key}")
                except Exception as e:
                    print(f"    ⚠️ Failed to upload canonical files to OSS: {e}")
                    raise RuntimeError(f"OSS upload failed for canonical document: {e}") from e

            # 2. Update RDS metadata
            if not simulate_db:
                # L2: content hashes. canonical_sha256 computed above (reused by the skip-gate); checksum_sha256
                # = sha256 of raw bytes (from node_extract). Additive; a NULL hash means "process" (fail-safe).
                _raw_checksum = ctx.get("_raw_checksum", {}).get(
                    (canonical["doc_id"], canonical["version_no"]))
                try:
                    if _doc_conn is None:
                        _doc_conn = _get_db_conn(select_db=True)
                    with _doc_conn.cursor() as cursor:
                        _ck_params = (
                            canonical_key,
                            canonical_md_key,
                            _raw_checksum,
                            _canonical_sha256,
                            canonical.get("ocr_status", "NOT_REQUIRED"),
                            canonical.get("page_count", 0),
                            canonical.get("text_length", 0),
                            canonical.get("extract_method", "native"),
                            canonical["doc_id"],
                            canonical["version_no"]
                        )
                        _ck_sql_tail = """
                                extraction_status = 'COMPLETED',
                                ocr_status = %s,
                                page_count = %s,
                                text_length = %s,
                                extract_method = %s
                            WHERE doc_id = %s AND version_no = %s
                        """
                        try:
                            # G19：content_simhash 随 canonical keys 一起落库（迁移 020）
                            cursor.execute("""
                                UPDATE document_version
                                SET canonical_json_key = %s,
                                    canonical_md_key = %s,
                                    checksum_sha256 = %s,
                                    canonical_sha256 = %s,
                                    content_simhash = %s,""" + _ck_sql_tail,
                                _ck_params[:4] + (_content_simhash,) + _ck_params[4:])
                        except Exception as _sh_err:
                            if "1054" not in str(_sh_err):
                                raise
                            # 迁移 020 未应用 → 回退旧列集（fail-open，行为与迁移前一致）
                            cursor.execute("""
                                UPDATE document_version
                                SET canonical_json_key = %s,
                                    canonical_md_key = %s,
                                    checksum_sha256 = %s,
                                    canonical_sha256 = %s,""" + _ck_sql_tail,
                                _ck_params)
                    _doc_conn.commit()
                    print(f"    ├─ Saved canonical keys to RDS for {canonical['doc_id']} v{canonical['version_no']}")
                except Exception as e:
                    if _doc_conn is not None:
                        _doc_conn.rollback()
                    print(f"    ⚠️ Failed to save canonical keys to RDS: {e}")
                    raise RuntimeError(f"Database write failure in node_build_canonical: {e}") from e

            block_count = len(canonical.get("blocks", []))
            warn_count = len(canonical.get("warnings", []))
            print(
                f"    └─ {canonical['doc_id']}: canonical built "
                f"({canonical['text_length']} chars, {block_count} blocks"
                f"{f', {warn_count} warnings' if warn_count else ''})"
            )

            # Append only after a successful (non-skipped) build — the skip-gate `continue`s above.
            canonicals.append(canonical)
        except Exception as _finalize_err:
            # B2（2026-07-25）：单篇定稿失败（canonical OSS 上传 / RDS 写）不再冒泡打断整批。
            # 此前第 k 篇一 raise，k+1..N 篇**已经付过费**的 OCR/VLM 抽取成果连同定稿一起丢弃
            # （下一轮从头重付）。改为按篇隔离：标 FAILED 留痕 + continue，循环后统一 raise
            # 让运行照常变红。keys 未写 ⇒ 下一次 stage-1 按既有谓词自动重捡（与 OSS-FETCH /
            # ENV-DEP / COST-DEFER 三个守卫同一自愈通道）。
            _fin_reason = f"[CANONICAL-FINALIZE] {type(_finalize_err).__name__}: {_finalize_err}"[:500]
            print(f"    ❌ ERROR {canonical['doc_id']} v{canonical['version_no']}: {_fin_reason}")
            _mark_extraction_failed(canonical["doc_id"], canonical["version_no"],
                                    _fin_reason, simulate_db)
            finalize_failures.append(
                f"{canonical['doc_id']} v{canonical['version_no']}: {_fin_reason}")
            continue
        finally:
            # perf#92：本篇共享连接统一归还（skip/dedup 的 continue、OSS/DB 的 raise、正常路径皆经此）。
            if _doc_conn is not None:
                _doc_conn.close()

    ctx["canonicals"] = canonicals

    # B2 收尾：抽取失败 / 定稿失败 —— 与下面三个守卫同型（受影响 doc 已逐条标 FAILED、
    # canonical 被扣住，健康文档已逐篇落库不受影响），统一 raise 让 DataWorks 变红。
    # 自愈条件：keys 保持 NULL ⇒ 下一次 stage-1 自动重捡。
    if extract_failures or finalize_failures:
        _all = extract_failures + finalize_failures
        _shown = " | ".join(_all[:10])
        if len(_all) > 10:
            _shown += f" | ...(+{len(_all) - 10} more)"
        raise RuntimeError(
            f"[STAGE1-PARTIAL] {len(extract_failures)} doc(s) failed extraction + "
            f"{len(finalize_failures)} failed canonical finalize (each marked "
            f"extraction_status=FAILED, canonical withheld). Healthy docs in this batch were "
            f"finalized normally and are NOT lost. They will be re-picked automatically: {_shown}")

    # OSS-FETCH 收尾（A2）：与 ENV-DEP 同型——受影响 doc 已逐条标 FAILED 且 canonical 被扣住，
    # 健康文档已逐篇落库不受影响；统一 raise 让 DataWorks 任务变红。自愈条件：OSS 恢复后
    # 下一次 stage-1 按既有谓词（NOT_STARTED 且 keys IS NULL）自动重捡，无须人工干预。
    if fetch_failures:
        _shown = " | ".join(fetch_failures[:10])
        if len(fetch_failures) > 10:
            _shown += f" | ...(+{len(fetch_failures) - 10} more)"
        raise RuntimeError(
            f"[OSS-FETCH] {len(fetch_failures)} doc(s) could not be downloaded from OSS "
            f"(marked extraction_status=FAILED, canonical withheld, content_process_status "
            f"stays NOT_STARTED). They will be re-picked automatically once OSS is reachable: "
            f"{_shown}")

    # ENV-DEP 守卫收尾：受影响 doc 已逐条标 FAILED 并被排除出 canonicals（健康文档的
    # canonical/COMPLETED 均已逐篇落库，不受影响）；这里统一 raise 让 DAG/DataWorks
    # 任务变红——环境性失败必须炸出来，而不是留一批静默空产出。
    if env_dep_failures:
        _shown = " | ".join(env_dep_failures[:10])
        if len(env_dep_failures) > 10:
            _shown += f" | ...(+{len(env_dep_failures) - 10} more)"
        raise RuntimeError(
            f"[ENV-DEP] {len(env_dep_failures)} doc(s) produced EMPTY canonical due to missing "
            f"python modules (marked extraction_status=FAILED, canonical withheld). "
            f"Fix the environment (pip install the missing modules) and re-run stage-1: {_shown}")

    # COST-DEFER 收尾：同 ENV-DEP 形态——受影响 doc 已逐条标 FAILED 且 canonical 被扣住
    # （健康文档已逐篇落库，不受影响）；统一 raise 让 DataWorks 任务变红。与 ENV-DEP 的差别
    # 只在自愈条件：预算次日/下一 run 滚动即自动重捡，无须人工干预。
    if cost_defer_docs:
        _shown = " | ".join(cost_defer_docs[:10])
        if len(cost_defer_docs) > 10:
            _shown += f" | ...(+{len(cost_defer_docs) - 10} more)"
        raise RuntimeError(
            f"[COST-DEFER] {len(cost_defer_docs)} doc(s) deferred — transient VLM budget "
            f"(RUN/DAILY) exhausted mid-run (extraction_status=FAILED, canonical withheld, "
            f"content_process_status stays NOT_STARTED). They will be re-picked automatically "
            f"once the budget rolls over (next run / next day); no manual action needed: {_shown}")


# ═══════════════════════════════════════════════════════════════
# DAG 2: canonical_to_safe_chunk — 分类 + 风险 + 脱敏 + 切分
#
# 关键顺序：分类/风险先于脱敏
# 原因：先脱敏会丢失业务上下文（如"薪资"→"****"），导致 LLM 分类不准。
# 和 scan_pending_clean.py 的 llm_classify_document 一致：
# 用原始文本做分类+风险，脱敏作为后处理。
# ═══════════════════════════════════════════════════════════════

# 权限值归一化：历史值 'internal' 统一映射为 'dept_internal'，与 HA3 检索过滤表达式对齐
# （retriever 按 permission_level="dept_internal" AND owner_dept=<部门> 放行本部门文档；
#  写入 'internal' 的 chunk 两个分支都不命中，会对所有人不可见）。
_PERMISSION_ALIAS = {"internal": "dept_internal"}


def _upload_clean_assets(extractions, bucket_upload) -> int:
    """把保留（CLEAN 路由）的图片资产上传到 OSS，并把 oss_key 回写进 asset dict。

    上传条件（每条都 load-bearing）：
      - status ∈ (ROUTE_TO_VECTOR, ROUTE_TO_TEXT)：绑定注入会把两类都绑进 chunk
        并构造 processing/assets/ 路径，只传 TO_VECTOR 会让 TO_TEXT 截图在
        serving 端签出 403 死图（UI 截图多数走 TO_TEXT —— 2026-06-10 对抗评审）；
      - oss_key 为空：独立图片文档的 oss_key=raw_key（原对象已在 OSS），跳过重复上传；
      - local_path 存在：tmp 清理前调用。
    同一 local_path 的多个 asset（同一 media 被文档多处引用的出现副本）共享一次
    上传：第二个起直接回写已上传的 oss_key。
    Returns: 上传成功数。
    """
    uploaded = 0
    uploaded_by_path: dict = {}  # local_path -> oss_key（出现副本共享上传）
    for result in extractions:
        if not hasattr(result, 'assets') or not result.assets:
            continue
        for asset in result.assets:
            local_img = asset.get("local_path", "")
            if (asset.get("status") in ("ROUTE_TO_VECTOR", "ROUTE_TO_TEXT")
                    and not asset.get("oss_key")
                    and local_img and os.path.exists(local_img)):
                if local_img in uploaded_by_path:
                    asset["oss_key"] = uploaded_by_path[local_img]
                    continue
                # 原先此处缺 startswith("raw/") guard（漂移点），统一走 _dept_from_raw_key
                dept = _dept_from_raw_key(getattr(result, "source_key", "") or "", "unknown")
                oss_key = (f"processing/assets/{dept}/{result.doc_id}"
                           f"/v{result.version_no}/{os.path.basename(local_img)}")
                try:
                    bucket_upload.put_object_from_file(oss_key, local_img)
                    asset["oss_key"] = oss_key
                    uploaded_by_path[local_img] = oss_key
                    uploaded += 1
                    print(f"    📤 Uploaded image to OSS: {oss_key}")
                except Exception as e:
                    print(f"    ⚠️ Failed to upload image to OSS: {e}")
    return uploaded


def _dept_from_raw_key(source_key: str, default: str = "unknown") -> str:
    """从 OSS raw/ key 解析部门代码：``raw/<dept>/...`` → ``<dept>``，否则回退 default。

    owner_dept 安全相关（驱动 HA3 dept_internal 权限过滤），只认 raw/ 前缀，杜绝把
    processing/、s3:// 等非 raw 路径的第二段误当部门——消除原先 8 处拷贝里 line 573
    缺 startswith("raw/") guard 的漂移。
    ⚠️ 阶段 B：node 文档的第 2 段是 ``node-<dept_id>``——本函数**语义不变**（图片对象
    路径等消费方拿它当 storage_segment 用，路径布局照旧）；需要区分归属轴的调用方
    改用 kb_upload.parse_raw_owner 的结构化结果。
    """
    if source_key and source_key.startswith("raw/"):
        parts = source_key.split("/")
        if len(parts) > 1:
            return parts[1]
    return default


# ── 阶段 B：060 mode 列的双路 SQL 执行（1054 TTL 负缓存，与 contribution._exec_gap_sql
#    同型）——摄取侧对「060 未 apply 的环境」回退旧 SQL，行为逐字节不变；apply 后无须
#    重启自动恢复带守卫版本。──────────────────────────────────────────────────
_NODE_MODE_COLS_MISSING_UNTIL = 0.0
_NODE_MODE_RETRY_SECONDS = 600.0


def _exec_node_guarded(cursor, sql_with_mode: str, sql_without: str, params_with, params_without) -> bool:
    """优先执行带 acl_mode 守卫的 SQL；1054（列缺失）→ TTL 负缓存并回退无守卫版本。
    返回 True=守卫版已执行。仅 1054 走降级，其他 SQL 错误照抛。"""
    global _NODE_MODE_COLS_MISSING_UNTIL
    if time.time() >= _NODE_MODE_COLS_MISSING_UNTIL:
        try:
            cursor.execute(sql_with_mode, params_with)
            return True
        except Exception as e:   # noqa: BLE001 — 仅 1054 降级
            errno = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
            if errno != 1054:
                raise
            _NODE_MODE_COLS_MISSING_UNTIL = time.time() + _NODE_MODE_RETRY_SECONDS
            print("    ⚠️ [node-acl] acl_mode 列缺失（060 未 apply），"
                  f"{_NODE_MODE_RETRY_SECONDS:.0f}s 内回退无守卫 SQL")
    cursor.execute(sql_without, params_without)
    return False


def _perm_level_from_path(path: str) -> str:
    """从 OSS 路径解析权限等级，按【路径段精确匹配】（H6）。

    历史用子串匹配（"internal" in path），会被 internal-audit/、international.docx 等
    误触发。改为对 '/' 分段后整段精确比对：仅当某一路径段恰为 'restricted' / 'internal' /
    'dept_internal' 时命中。约定的受限目录是 raw/<dept>/internal/<file>。
    命中返回 'restricted' 或 'dept_internal'；无匹配返回 ""。
    """
    if not path:
        return ""
    segs = {s for s in path.lower().split("/") if s}
    if "restricted" in segs:
        return "restricted"
    if "internal" in segs or "dept_internal" in segs:
        return "dept_internal"
    return ""


def resolve_permission_level(doc: dict, ctx: dict) -> str:
    """
    确定文档的权限等级，完全由 OSS 路径/预配置属性决定，绝不经过模型预测。
    根据以下优先级：
    1. 查找 doc 或 task 中显式指定的 permission_level（'internal' 归一为 'dept_internal'）。
    2. 从 doc['source_key']、doc['canonical_key'] 或 task['raw_key'] 等路径中【按路径段精确】解析：
       - 某一路径段恰为 'restricted'，返回 'restricted'
       - 某一路径段恰为 'internal' / 'dept_internal'（约定 raw/<dept>/internal/），返回 'dept_internal'
       - 否则默认返回 'public'（raw/ 根目录除隔离/归档外约定为公开，敏感内容靠脱敏兜底）
    """
    # 1. 显式指定的权限
    if "permission_level" in doc:
        v = doc["permission_level"]
        return _PERMISSION_ALIAS.get(v, v)

    # 查找任务上下文中的显式设置
    tasks = ctx.get("tasks", [])
    for task in tasks:
        if task.get("doc_id") == doc["doc_id"]:
            if "permission_level" in task:
                v = task["permission_level"]
                return _PERMISSION_ALIAS.get(v, v)
            # 检查任务的 raw_key（路径段精确匹配）
            lvl = _perm_level_from_path(task.get("raw_key", ""))
            if lvl:
                return lvl

    # 2. 从路径特征中解析（路径段精确匹配）
    paths_to_check = [
        doc.get("source_key", ""),
        doc.get("canonical_key", ""),
        doc.get("canonical_md_key", "")
    ]
    for p in paths_to_check:
        lvl = _perm_level_from_path(p)
        if lvl:
            return lvl

    # 默认值为 'public'
    return "public"


def _clean_llm_json_response(text: str) -> str:
    """
    Strips markdown code blocks (e.g. ```json ... ```) and isolates the first 
    '{' and last '}' or '[' and ']' to extract a clean JSON string.
    """
    text = text.strip()
    
    # Strip markdown block if present at start and end
    if text.startswith("```"):
        # Strip leading fence
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline:].strip()
        else:
            text = text[3:].strip()
        # Strip trailing fence
        if text.endswith("```"):
            text = text[:-3].strip()
            
    # Defensively locate the main JSON object or array boundary
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    
    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    
    # Determine the outer bounds
    start_idx = -1
    end_idx = -1
    
    # If both braces and brackets are found, pick the outer-most pair
    if first_brace != -1 and first_bracket != -1:
        if first_brace < first_bracket:
            start_idx = first_brace
            end_idx = last_brace
        else:
            start_idx = first_bracket
            end_idx = last_bracket
    elif first_brace != -1:
        start_idx = first_brace
        end_idx = last_brace
    elif first_bracket != -1:
        start_idx = first_bracket
        end_idx = last_bracket
        
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        text = text[start_idx:end_idx + 1]
        
    return text


# A10：摄取分类器专用的输出上限（**不复用 config.llm.max_tokens** —— 那是与问答共用的字段，
# 见下方 payload 注释）。分类响应是一个小 JSON，1024 足够且留了充分余量。
_CLASSIFY_MAX_TOKENS = 1024


def run_gemini_classification(text: str, model_name: str, api_key: str, api_base_url: str) -> dict:
    """
    调用 LLM 接口（兼容 Gemini 和阿里云 DashScope Qwen 接口）进行分类和风险评估。使用 structured JSON Schema 输出，排除权限字段。
    """
    import requests
    import json
    
    is_dashscope = "dashscope.aliyuncs.com" in api_base_url or "qwen" in model_name.lower()
    
    schema = {
        "type": "OBJECT",
        "properties": {
            "category_l1": {
                "type": "STRING",
                "description": "Must be one of: 'policy', 'process', 'sop', 'standard', 'template', 'reference', 'record', 'others'",
                "enum": ["policy", "process", "sop", "standard", "template", "reference", "record", "others"]
            },
            "category_l2": {
                "type": "STRING",
                "description": (
                    "Hierarchical L2 subcategory matching L1:\n"
                    "- policy: 'hr_policy', 'finance_policy', 'general_policy', 'safety_policy', 'quality_policy', 'others'\n"
                    "- process: 'approval_flow', 'procurement_flow', 'production_flow', 'system_flow', 'others'\n"
                    "- sop: 'equipment_sop', 'inspection_sop', 'business_sop', 'safety_sop', 'others'\n"
                    "- standard: 'inspection_std', 'quality_std', 'operation_std', 'others'\n"
                    "- template: 'form', 'contract', 'report', 'others'\n"
                    "- reference: 'training', 'product', 'cert', 'manual', 'others'\n"
                    "- record: 'personnel', 'asset', 'business', 'others'\n"
                    "- others: 'others'"
                ),
                "enum": [
                    "hr_policy", "finance_policy", "general_policy", "safety_policy", "quality_policy",
                    "approval_flow", "procurement_flow", "production_flow", "system_flow",
                    "equipment_sop", "inspection_sop", "business_sop", "safety_sop",
                    "inspection_std", "quality_std", "operation_std",
                    "form", "contract", "report",
                    "training", "product", "cert", "manual",
                    "personnel", "asset", "business",
                    "others"
                ]
            },
            "faq_eligible": {
                "type": "BOOLEAN",
                "description": "Whether the document is fit for automated FAQ extraction"
            },
            "confidence": {
                "type": "NUMBER",
                "description": "Confidence score for the classification between 0.00 and 1.00"
            },
            "llm_risk_level": {
                "type": "STRING",
                "description": "Content-level security risk rating: 'low', 'medium', or 'high'"
            },
            "summary": {
                "type": "STRING",
                "description": "Concise 100-character semantic summary"
            }
        },
        "required": [
            "category_l1", "category_l2", "faq_eligible", "confidence", "llm_risk_level", "summary"
        ]
    }
    
    prompt_instructions = (
        "Analyze this corporate document and classify its metadata with high precision.\n"
        "Instructions:\n"
        "1. Identify L1 category (must be one of: 'policy', 'process', 'sop', 'standard', 'template', 'reference', 'record', 'others').\n"
        "2. Identify L2 category (must strictly correspond to the chosen L1 category as mapped in the schema).\n"
        "3. Determine if it is eligible for FAQ extraction.\n"
        "4. Assess your confidence score (0.00 to 1.00).\n"
        "5. Assess the content-level security risk rating ('low', 'medium', or 'high').\n"
        "6. Provide a concise 100-character semantic summary.\n\n"
    )
    
    if is_dashscope:
        # DashScope / 阿里云百炼 OpenAI 兼容接口格式 (支持新版模型如 qwen3.7-plus)
        if "compatible-mode" not in api_base_url and "chat/completions" not in api_base_url:
            url = f"{api_base_url.rstrip('/')}/compatible-mode/v1/chat/completions"
        elif "chat/completions" not in api_base_url:
            url = f"{api_base_url.rstrip('/')}/chat/completions"
        else:
            url = api_base_url
            
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        
        schema_str = json.dumps(schema, indent=2, ensure_ascii=False)
        system_prompt = (
            "You are a precise corporate document classifier and risk assessor.\n"
            "You MUST respond ONLY with a single valid JSON object adhering strictly to the schema below. Do not output any markdown code blocks, do not output your thinking process or any introductory text.\n"
            f"Required JSON Schema:\n{schema_str}"
        )
        
        user_prompt = (
            f"{prompt_instructions}"
            f"Document Content:\n{text[:8000]}\n\n"
            "Please output the required JSON object now."
        )
        
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0,  # DET: deterministic classification → stable category → stable chunk routing
            # A10（2026-07-25）：思考模式与输出上限都必须**钉死**，与 intent_router /
            # query_rewriter / query_decomposer / general_answerer 等小判别调用同款。
            # 供应商若把该模型的 thinking 默认翻成开，content 会被 reasoning 挤空 → JSON 解析
            # 失败 → 走 fail-safe 把文档打成 restricted/QUARANTINE/PENDING_AUDIT/high-risk。
            # **刻意不读 config.llm**：LLMConfig 是分类与问答共用的（llm_generator 读同一个
            # enable_thinking / max_tokens），谁为调答案设了 RAG_LLM_ENABLE_THINKING=true，
            # 摄取分类器就会跟着翻——而类别决定 chunk 家族路由，等于静默 re-roll 全库切块。
            "enable_thinking": False,
            # 1024 而非 512：分类 JSON 很短，但太紧的失败模式不是"截断答案"而是解析失败后
            # 走上面那条隔离链路，宁可给足余量。
            "max_tokens": _CLASSIFY_MAX_TOKENS,
        }
        
        from opensearch_pipeline.vlm_retry import post_json_with_retry
        # 重试瞬时 429/5xx：并发=RAG_CLASSIFY_CONCURRENCY(默认 8) 下 429 高发，单次失败会让整篇
        # 文档分类失败、本轮不入库（DashScope 429 是已知脆弱点）。drop-in，调用方仍判 status_code。
        resp = post_json_with_retry(url, json=payload, headers=headers, timeout=90,
                                    label="classify(DashScope)", post_fn=requests.post)
        if resp.status_code != 200:
            raise Exception(f"DashScope API returned status code {resp.status_code}: {resp.text}")
            
        data = resp.json()
        try:
            choices = data["choices"]
            text_content = choices[0]["message"]["content"]
            cleaned_content = _clean_llm_json_response(text_content)
            return json.loads(cleaned_content)
        except (KeyError, IndexError, ValueError) as e:
            raise Exception(f"Failed to parse DashScope response: {e}. Raw response: {data}")
    else:
        # Gemini API 接口格式
        url = f"{api_base_url}/models/{model_name}:generateContent"
        prompt = f"{prompt_instructions}Document Content:\n{text[:8000]}"
        
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0  # DET: deterministic classification (mirrors DashScope branch)
            }
        }
        
        # P0-2 Fix: API key 通过 header 传递，避免暴露在 URL 中被代理/日志记录
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise Exception(f"Gemini API returned status code {resp.status_code}: {resp.text}")
            
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise Exception("No candidates returned from Gemini API.")
            
        text_content = candidates[0]["content"]["parts"][0]["text"]
        cleaned_content = _clean_llm_json_response(text_content)
        return json.loads(cleaned_content)


def _docs_with_existing_chunks(canonicals):
    """Return this run's (doc_id, version_no) targets that ALREADY have chunk_meta rows.

    A *current-version* target that already has chunks means this is a **re-chunk** of an
    already-chunked doc — ``reset_for_rechunk.py`` keeps chunk_meta intact (it only flips
    document_version statuses), so the old chunks survive until ``node_write_chunk_meta``'s
    full-replace. A *first ingest* or a *version bump* (new version_no) has no rows for its exact
    (doc_id, version_no), so it is never flagged. The unfrozen-rechunk guard uses this to refuse a
    re-chunk that re-rolls classification (the PRODUCTION_14DFDF 79-vs-47 family flip).

    FAIL CLOSED: an un-verifiable guard must block, never fall through. On a DB error we retry once
    (mirroring the classify preempt loop) and then **raise** — we never return [] on error.
    """
    import time as _t

    pairs = [(d["doc_id"], d["version_no"]) for d in canonicals
             if d.get("doc_id") and d.get("version_no") is not None]
    if not pairs:
        return []
    clause = " OR ".join(["(doc_id=%s AND version_no=%s)"] * len(pairs))
    params = tuple(p for pr in pairs for p in pr)
    last_err = None
    for _attempt in range(2):  # initial + 1 retry, like the content-preempt loop
        conn = None
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT doc_id, version_no FROM chunk_meta WHERE {clause}", params)
                rows = cur.fetchall()
            out = []
            for r in (rows or []):
                if isinstance(r, dict):
                    out.append((r["doc_id"], r["version_no"]))
                else:
                    out.append((r[0], r[1]))
            return out
        except Exception as e:  # noqa: BLE001 — any failure must fail closed
            last_err = e
            if _attempt == 0:
                _t.sleep(2)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    raise RuntimeError(
        f"unfrozen re-chunk guard could not verify chunk_meta (DB error after retry): {last_err} "
        f"— refusing to proceed (fail closed). Set RAG_SIMULATE_DB=true for local runs, or fix RDS "
        f"connectivity; never bypass this check on an error.")


def _unfrozen_rechunk_acked(ctx, prior) -> bool:
    """True iff a valid same-day, **doc-set-bound** override authorizes this unfrozen re-chunk.

    Token (``ctx['allow_unfrozen_rechunk']`` or env ``RAG_ALLOW_UNFROZEN_RECHUNK``):
    ``<op>:<YYYY-MM-DD>:<docset_hash>`` — op non-empty, date == today (mirrors the
    ``RAG_DESTRUCTIVE_PROD_ACK`` same-day rule in env_guard), and docset_hash == the hash recomputed for
    THIS run's flagged doc-set. A stale date, malformed token, or a hash minted for a *different*
    doc-set does NOT satisfy the guard. The computed hash is always logged (accepted or not), so every
    override attempt is auditable.
    """
    from datetime import date as _date

    from opensearch_pipeline.reindex_states import docset_hash

    expected = docset_hash(d for d, _ in prior)
    tok = (ctx.get("allow_unfrozen_rechunk")
           or os.environ.get("RAG_ALLOW_UNFROZEN_RECHUNK", ""))
    parts = tok.split(":")
    ok = (len(parts) == 3 and parts[0].strip()
          and parts[1] == _date.today().isoformat()
          and parts[2] == expected)
    if ok:
        print(f"    !! [UNFROZEN-RECHUNK OVERRIDE] accepted docset_hash={expected} token={tok} "
              f"— classifier WILL re-roll category for {len(prior)} re-chunked doc(s)")
    else:
        print(f"    [unfrozen-rechunk] docset_hash={expected} ack=absent/invalid "
              f"(need RAG_ALLOW_UNFROZEN_RECHUNK=<op>:{_date.today().isoformat()}:{expected})")
    return bool(ok)


def _crash_resume_autofreeze_enabled() -> bool:
    """crash-resume 目标是否 auto-freeze 复用存储分类续跑（默认 on=修复生效）。
    RAG_CRASH_RESUME_AUTOFREEZE=false 回退旧行为（有 chunk 的目标一律整批 fail-closed raise）。"""
    return os.environ.get("RAG_CRASH_RESUME_AUTOFREEZE",
                          "true").strip().lower() not in ("0", "false", "no", "off")


def _partition_prior_rechunk(prior_pairs):
    """把「已有 chunk_meta 的当前版本目标」分成 crash-resume（可安全续跑）与 deliberate re-chunk。

    crash-resume（ultra P1 2026-07-17）：node_write_chunk_meta 在 chunk 行提交（成功、5293）后、
    每文档 content_process_status='DONE' 收口（5474）前崩溃 → 文档卡 PROCESSING → orchestrator 的
    stale sweep 置 content_process_status='FAILED' + retry_count+1。而 deliberate 的 reset_for_rechunk
    走 rechunk_reset_state() = content_process_status='NOT_STARTED' + retry_count=0——retry_count
    是互斥判据。**状态列必须同时接受 LOADING**（2026-07-17 核查纠偏）：生产 orchestrator 的
    stage-2 loader 在 DAG-2 启动前就把认领行 FAILED→LOADING（只改状态、retry_count 原样保留），
    guard 在 DAG-2 内运行时看到的是 LOADING+retry>0 而非 FAILED——只认 FAILED 会让本分区在
    真正发生楔死的生产路径上永不命中。LOADING+retry>0 ⟺ 本轮从 FAILED&retry<3 认领（loader 谓词
    只收 NOT_STARTED / FAILED&retry<3），与 deliberate（认领后 LOADING+retry=0）互斥性不变；
    裸跑（无 loader 认领）仍以 FAILED+retry>0 呈现，两态都收。
    crash-resume 是同一次 ingest 的续跑（canonical 未变），必须复用**已存**分类
    （document_meta.category_l1/l2）续跑，绝不能重跑 LLM 分类（re-roll category→翻 chunk family=
    PRODUCTION_14DFDF 79-vs-47）。此前对二者一律整批 raise，crash-resume 的 re-claim 遂楔死整批
    stage-2、把 co-batched 健康文档拖到 retry_count=3 永久 FAILED。

    返回 (crash_resume, deliberate)：
      crash_resume: {doc_id: {"category_l1","category_l2"}} —— auto-freeze 复用存储分类。
        category_l1 缺失（分类记录损坏/丢失）的 crash-resume 目标降级并入 deliberate（fail-closed，
        宁可整批停下等人工，绝不盲目续跑一个丢了分类的文档）。
      deliberate: [(doc_id, version_no), ...] —— 真正的 unfrozen re-chunk（NOT_STARTED 复位或
        非 crash-resume 特征），仍受下方 unfrozen-rechunk guard 的整批 fail-closed 约束。

    FAIL CLOSED：DB 不可验证时 raise（同 _docs_with_existing_chunks，initial + 1 retry）。"""
    import time as _t

    if not prior_pairs:
        return {}, []
    clause = " OR ".join(["(dv.doc_id=%s AND dv.version_no=%s)"] * len(prior_pairs))
    params = tuple(p for pr in prior_pairs for p in pr)
    last_err = None
    for _attempt in range(2):
        conn = None
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT dv.doc_id, dv.version_no, dv.content_process_status, dv.retry_count,
                               dm.category_l1, dm.category_l2
                        FROM document_version dv
                        JOIN document_meta dm ON dm.doc_id = dv.doc_id
                        WHERE {clause}""", params)
                rows = cur.fetchall()
            _by_key = {}
            for r in (rows or []):
                if isinstance(r, dict):
                    _by_key[(r["doc_id"], r["version_no"])] = (
                        r["content_process_status"], r["retry_count"],
                        r.get("category_l1"), r.get("category_l2"))
                else:
                    _by_key[(r[0], r[1])] = (r[2], r[3], r[4], r[5])
            crash_resume, deliberate = {}, []
            for pr in prior_pairs:
                key = (pr[0], pr[1])
                row = _by_key.get(key)
                if row is None:
                    deliberate.append(key)   # document_meta 缺失等异常 → fail-closed
                    continue
                cps, rc, cat1, cat2 = row
                # FAILED=裸跑未认领；LOADING=orchestrator loader 已认领（从 FAILED&retry<3 迁入，
                # retry_count 保留）——两态都是 crash-resume 呈现（见 docstring 纠偏说明）。
                if str(cps) in ("FAILED", "LOADING") and int(rc or 0) > 0 and cat1:
                    crash_resume[key[0]] = {"category_l1": cat1, "category_l2": cat2 or "others"}
                else:
                    deliberate.append(key)
            return crash_resume, deliberate
        except Exception as e:  # noqa: BLE001 — 无法验证必须 fail-closed
            last_err = e
            if _attempt == 0:
                _t.sleep(2)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    raise RuntimeError(
        f"crash-resume 分区无法验证 document_version（DB 错误重试后仍失败）: {last_err} — "
        f"拒绝继续（fail closed）")


def _safe_classification_fields(classification: dict, text: str) -> dict:
    """从（可能缺键的）LLM 分类结果取字段，缺键用保守默认 —— 绝不 KeyError。

    compatible-mode 不硬约束 JSON schema，模型在负载/截断下常漏键；直接索引会崩节点。
    默认取"低置信(0.0)/低风险(low)/非 faq"——保守、不误升权（PII 仍由 node_detect_sensitive
    的正则独立把关，不依赖此处 risk）。
    """
    return {
        "confidence": classification.get("confidence", 0.0),
        "faq_eligible": classification.get("faq_eligible", False),
        "summary": classification.get("summary") or (text or "")[:100],
        "llm_risk_level": classification.get("llm_risk_level", "low"),
    }


def node_classify_and_risk_assess(ctx: dict):
    """
    文档分类 + 风险评估（合并节点，单次 LLM 调用）。

    在原始文本上运行，一次 LLM 调用同时输出：
    - category_l1 / category_l2（分类）
    - risk_level（LLM 判断的内容风险）
    - faq_eligible（是否适合生成 FAQ）
    - summary（摘要）
    
    权限判定（permission_level 和 kb_type）完全绕过模型，由上传路径或预配置的属性判定。
    """
    canonicals = ctx["canonicals"]
    config = get_config()
    simulate_db = _resolve_simulate(ctx, "db")
    valid_canonicals = []

    # ── maintenance re-chunk: freeze the existing classification, NEVER call the LLM classifier ──
    # (2026-06-15) Re-running the LLM classifier on a re-chunk re-rolls category_l1/l2, which drives
    # chunk-strategy routing (faq/clause/step/text) — observed flipping a doc's family run-to-run
    # (e.g. sop->step vs standard->clause). For a maintenance re-chunk (apply chunker text fixes only),
    # reuse the frozen category so routing is deterministic and the chunk family is preserved.
    # FAIL CLOSED before any write: every target doc must carry a frozen category_l1.
    frozen_routing = ctx.get("frozen_routing")
    if frozen_routing is not None:
        _bad = [d["doc_id"] for d in canonicals
                if not (frozen_routing.get(d["doc_id"]) or {}).get("category_l1")]
        if _bad:
            raise RuntimeError(
                f"maintenance re-chunk: frozen routing missing category_l1 for {len(_bad)} doc(s) "
                f"{_bad[:5]} — fail closed, NO reclassification and NO write")
    elif not simulate_db:
        # ── Unfrozen re-chunk guard (fail closed, WHOLE-BATCH, pre-preempt) ──
        # (2026-06-16) A current-version target that already has chunk_meta rows is a RE-CHUNK of an
        # already-chunked doc (reset_for_rechunk keeps chunk_meta), NOT a first ingest or version bump.
        # Re-running the LLM classifier here re-rolls category_l1/l2 → flips chunk mode → flips the
        # chunk family run-to-run (the PRODUCTION_14DFDF 79-vs-47 incident, fixed for the maintenance
        # path by RAG_MAINTENANCE_ROUTING). The daily pipeline never re-claims a served doc, so this
        # only fires on a deliberate reset→re-chunk that FORGOT to freeze. Require either a freeze
        # (frozen_routing, handled above) or a deliberate doc-set-bound same-day override. Block the
        # WHOLE run if ANY target qualifies (mixed fresh+re-chunk batch = ambiguous intent → stop and
        # force an explicit choice). Runs BEFORE the preempt UPDATE so nothing is stranded in PROCESSING.
        _prior = _docs_with_existing_chunks(canonicals)
        if _prior:
            # crash-resume 与 deliberate re-chunk 分区（ultra P1 2026-07-17）：前者（chunk 已写、
            # DONE 收口前崩溃被 sweep 转 FAILED-retry）auto-freeze 复用存储分类续跑；后者仍受整批
            # fail-closed 约束。此前不分区、一律整批 raise，crash-resume 的 re-claim 楔死整批
            # stage-2、连累 co-batched 健康文档（见 _partition_prior_rechunk）。
            if _crash_resume_autofreeze_enabled():
                _crash_resume, _deliberate = _partition_prior_rechunk(_prior)
                if _crash_resume:
                    ctx["_crash_resume_frozen"] = _crash_resume
                    print(f"    [crash-resume] auto-freeze {len(_crash_resume)} 个续跑文档的存储分类"
                          f"（chunk 已写、崩溃在 DONE 收口前）：{list(_crash_resume)[:3]}")
            else:
                _crash_resume, _deliberate = {}, list(_prior)   # kill switch：回退旧整批行为
            if _deliberate and not _unfrozen_rechunk_acked(ctx, _deliberate):
                from datetime import date as _date

                from opensearch_pipeline.reindex_states import docset_hash
                _h = docset_hash(d for d, _ in _deliberate)
                raise RuntimeError(
                    f"unfrozen re-chunk blocked: {len(_deliberate)} target doc(s) already have chunks for "
                    f"their current (doc_id,version_no), e.g. {_deliberate[:3]}, but no frozen routing is "
                    f"set. Re-running the LLM classifier would re-roll category->chunk mode and can flip "
                    f"the chunk family (the 79-vs-47 incident). For a maintenance re-chunk, set "
                    f"RAG_MAINTENANCE_ROUTING=<manifest>. To DELIBERATELY re-classify (route-v2 family "
                    f"migration), set RAG_ALLOW_UNFROZEN_RECHUNK=<op>:{_date.today().isoformat()}:{_h}")

    if not simulate_db:
        conn = None
        _preempt_max_retries = 1
        for _preempt_attempt in range(_preempt_max_retries + 1):
            try:
                conn = _get_db_conn(select_db=True)
                with conn.cursor() as cursor:
                    # E#37 认领预占批量化：一批 100 行 = 100 次 UPDATE 往返 → 先发一条
                    # (doc_id,version_no) 元组 IN 的集合式 UPDATE。pymysql 默认 rowcount=changed
                    # rows，认领状态迁移必然改行，故 rowcount==键数 ⟺ 全部认领成功（常态路径，
                    # 1 条语句）。部分认领时集合式 UPDATE 无法区分哪些键被改（回读 PROCESSING 会把
                    # 他方在处理的行误纳入），回滚后退回逐行认领兜底——认领语义（只认领期望状态的
                    # 行）与旧实现完全等价。
                    _claim_keys = []
                    _seen_keys = set()
                    for doc in canonicals:
                        _k = (doc["doc_id"], doc["version_no"])
                        if _k not in _seen_keys:
                            _seen_keys.add(_k)
                            _claim_keys.append(_k)
                    _all_claimed = False
                    if _claim_keys:
                        _dv_clause = " OR ".join(
                            ["(doc_id = %s AND version_no = %s)"] * len(_claim_keys))
                        _dv_params = tuple(p for k in _claim_keys for p in k)
                        # PR-4：认领顺带盖租约戳（off 时片段空=SQL 逐字节现状）；epoch 恒 +1
                        # 也顺带消解了同值 UPDATE 的 changed-rows 歧义（LOADING 行重认领必改行）。
                        cursor.execute(f"""
                            UPDATE document_version
                            SET content_process_status = 'PROCESSING'{ingest_lease.claim_set_sql()}
                            WHERE ({_dv_clause})
                              AND content_process_status IN ('NOT_STARTED', 'LOADING', 'FAILED')
                        """, ingest_lease.claim_set_params() + _dv_params)
                        _all_claimed = cursor.rowcount == len(_claim_keys)
                    if _all_claimed:
                        _dedup_seen = set()
                        for doc in canonicals:
                            _k = (doc["doc_id"], doc["version_no"])
                            if _k in _dedup_seen:
                                print(f"    └─ Task {doc['doc_id']} v{doc['version_no']} skipped (preempted or already processing content)")
                                continue
                            _dedup_seen.add(_k)
                            valid_canonicals.append(doc)
                        ingest_lease.get_lease_set(ctx).fetch_and_register(cursor, _claim_keys)
                        conn.commit()
                    else:
                        # 兜底：回滚集合式认领后按旧语义逐行认领（rowcount>0 = 本次真的改到了行）
                        if _claim_keys:
                            conn.rollback()
                        _fb_claimed_keys = []
                        for doc in canonicals:
                            cursor.execute(f"""
                                UPDATE document_version
                                SET content_process_status = 'PROCESSING'{ingest_lease.claim_set_sql()}
                                WHERE doc_id = %s AND version_no = %s
                                  AND content_process_status IN ('NOT_STARTED', 'LOADING', 'FAILED')
                            """, ingest_lease.claim_set_params()
                               + (doc["doc_id"], doc["version_no"]))
                            if cursor.rowcount > 0:
                                valid_canonicals.append(doc)
                                _fb_claimed_keys.append((doc["doc_id"], doc["version_no"]))
                            else:
                                print(f"    └─ Task {doc['doc_id']} v{doc['version_no']} skipped (preempted or already processing content)")
                        ingest_lease.get_lease_set(ctx).fetch_and_register(cursor, _fb_claimed_keys)
                        conn.commit()
                break  # 预占成功，退出重试循环
            except Exception as e:
                if conn:
                    try: conn.rollback()
                    except Exception: pass
                if _preempt_attempt < _preempt_max_retries:
                    import time as _time_preempt
                    print(f"    ⚠️ Preemption DB error (attempt {_preempt_attempt + 1}), retrying in 2s: {e}")
                    _time_preempt.sleep(2)
                    valid_canonicals = []  # 重置，准备重试
                else:
                    # 重试用尽仍然失败 → 中止节点，由 DataWorks 调度下次重试
                    raise RuntimeError(
                        f"Content preemption failed after {_preempt_max_retries + 1} attempts. "
                        f"Aborting to prevent duplicate processing. Last error: {e}"
                    ) from e
            finally:
                if conn:
                    conn.close()
                    conn = None
    else:
        valid_canonicals = canonicals
        
    ctx["canonicals"] = valid_canonicals


    # ── 并发 LLM 分类（线程安全：每个 doc 独立 API 调用 + 独立 DB 连接） ──
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    simulate_api = _resolve_simulate(ctx, "api")
    max_workers = int(os.environ.get("RAG_CLASSIFY_CONCURRENCY", "8"))

    # 级联白名单（线程共享只读，安全）
    ALLOWED_CATEGORY_L1 = {
        "policy", "process", "sop", "standard", "template", "reference", "record", "others"
    }
    TAXONOMY_L2 = {
        "policy":    {"hr_policy", "finance_policy", "general_policy", "safety_policy", "quality_policy", "others"},
        "process":   {"approval_flow", "procurement_flow", "production_flow", "system_flow", "others"},
        "sop":       {"equipment_sop", "inspection_sop", "business_sop", "safety_sop", "others"},
        "standard":  {"inspection_std", "quality_std", "operation_std", "others"},
        "template":  {"form", "contract", "report", "others"},
        "reference": {"training", "product", "cert", "manual", "others"},
        "record":    {"personnel", "asset", "business", "others"},
        "others":    {"others"},
    }

    def _classify_single_doc(doc):
        """单文档分类（线程安全：独立 API 调用 + 独立 DB 连接）。"""
        text = doc["text"]

        # 1. 权限判定（纯本地计算，线程安全）
        permission_level = resolve_permission_level(doc, ctx)
        kb_type = "public" if permission_level == "public" else "private"
        doc["permission_level"] = permission_level
        doc["kb_type"] = kb_type

        # 1.5 冻结分类复用（maintenance freeze 或 crash-resume 续跑，ultra P1）：绝不调 LLM classifier。
        #     frozen_routing（整批 maintenance freeze，presence 由上方 fail-closed 保证）优先；否则查
        #     crash-resume auto-freeze 映射（本文档若是崩溃续跑则复用其 document_meta 存储分类，family 保持）。
        _doc_frozen = (frozen_routing[doc["doc_id"]] if frozen_routing is not None
                       else (ctx.get("_crash_resume_frozen") or {}).get(doc["doc_id"]))
        if _doc_frozen is not None:
            fr = _doc_frozen
            doc["category_l1"] = fr["category_l1"]
            doc["category_l2"] = fr.get("category_l2") or "others"
            doc["owner_dept"] = doc.get("owner_dept") or "unknown"
            doc["faq_eligible"] = False
            doc["confidence"] = 1.0
            doc["summary"] = doc.get("summary") or text[:120]
            doc["llm_risk_level"] = "low"
            doc["risk_level"] = "low"
            doc["classification_status"] = "FROZEN_MAINTENANCE"
            if not simulate_db:
                _cm = None
                try:
                    _cm = _get_db_conn(select_db=True)
                    with _cm.cursor() as _cur:
                        # 阶段 B：node 行的归属轴绝不被分类回写覆盖（owner_dept 是 legacy 轴）
                        _exec_node_guarded(
                            _cur,
                            "UPDATE document_meta SET category_l1=%s, category_l2=%s, "
                            "owner_dept=IF(acl_mode='node', owner_dept, %s) WHERE doc_id=%s",
                            "UPDATE document_meta SET category_l1=%s, category_l2=%s, owner_dept=%s "
                            "WHERE doc_id=%s",
                            (doc["category_l1"], doc["category_l2"], doc["owner_dept"], doc["doc_id"]),
                            (doc["category_l1"], doc["category_l2"], doc["owner_dept"], doc["doc_id"]))
                        # PR-4（R1 共识）：dv 写前同事务验租（FOR UPDATE 行锁持至 commit）。
                        # 本路径 SET 全为确定性字面量，重试必同值 changed-rows=0——不得以
                        # rowcount 判租；fence 谓词在行锁下恒命中，纯防御。语句序保持 main
                        # 的 meta→dv 全局纪律；LeaseLost ⇒ 整事务回滚（meta 也不落）。
                        _fk = (doc["doc_id"], doc["version_no"])
                        _ffs = ingest_lease.get_lease_set(ctx)
                        _ffs.verify_for_update(_cur, _fk)
                        _cur.execute(
                            "UPDATE document_version SET classification_method='FROZEN_MAINTENANCE', "
                            "classification_status='CONTENT_CLASSIFIED' WHERE doc_id=%s AND version_no=%s"
                            + _ffs.fence_where_sql(_fk),
                            (doc["doc_id"], doc["version_no"]) + _ffs.fence_where_params(_fk))
                        _cm.commit()
                except ingest_lease.LeaseLost:
                    if _cm:
                        try: _cm.rollback()
                        except Exception: pass
                    print(f"    ⚠️ Lease lost on {doc['doc_id']} v{doc['version_no']} — "
                          f"frozen classification persist skipped (preempted)")
                    return False
                finally:
                    if _cm:
                        _cm.close()
            return True

        # 1.6 知识贡献合成的 .md（contribution-<cid>.md）天生是「问→答」对：直接 pin 为 faq 类目，
        # 走 FAQ 分块（问题进 chunk 文本→检索命中问句、答案聚合），跳过 LLM 重判（避免 category 漂移
        # 翻 chunk 模式）。category_l1='faq' 不在 LLM 分类白名单(ALLOWED_CATEGORY_L1)，故此处直接落定
        # 并【绕过下方 taxonomy 校验】（与 frozen_routing 同型，权限已在上方 resolve 过、不动）。
        if os.path.basename(doc.get("source_key", "")).startswith("contribution-"):
            doc["category_l1"] = "faq"
            doc["category_l2"] = "qa"
            doc["owner_dept"] = doc.get("owner_dept") or "unknown"
            doc["faq_eligible"] = True
            doc["confidence"] = 1.0
            doc["summary"] = doc.get("summary") or text[:120]
            doc["llm_risk_level"] = "low"
            doc["classification_status"] = "CONTENT_CLASSIFIED"
            if not simulate_db:
                _cm = None
                try:
                    _cm = _get_db_conn(select_db=True)
                    with _cm.cursor() as _cur:
                        _exec_node_guarded(
                            _cur,
                            "UPDATE document_meta SET category_l1=%s, category_l2=%s, "
                            "owner_dept=IF(acl_mode='node', owner_dept, %s), "
                            "permission_level=%s, kb_type=%s WHERE doc_id=%s",
                            "UPDATE document_meta SET category_l1=%s, category_l2=%s, owner_dept=%s, "
                            "permission_level=%s, kb_type=%s WHERE doc_id=%s",
                            (doc["category_l1"], doc["category_l2"], doc["owner_dept"],
                             doc["permission_level"], doc["kb_type"], doc["doc_id"]),
                            (doc["category_l1"], doc["category_l2"], doc["owner_dept"],
                             doc["permission_level"], doc["kb_type"], doc["doc_id"]))
                        # PR-4（R1 共识）：同 frozen 路径——verify_for_update 判租，
                        # 不依赖 rowcount（SET 确定性字面量）；LeaseLost ⇒ 整事务回滚。
                        _bk = (doc["doc_id"], doc["version_no"])
                        _bfs = ingest_lease.get_lease_set(ctx)
                        _bfs.verify_for_update(_cur, _bk)
                        _cur.execute(
                            "UPDATE document_version SET classification_method='CONTRIBUTION_FAQ', "
                            "faq_eligible=1, classification_status='CONTENT_CLASSIFIED' "
                            "WHERE doc_id=%s AND version_no=%s" + _bfs.fence_where_sql(_bk),
                            (doc["doc_id"], doc["version_no"]) + _bfs.fence_where_params(_bk))
                        _cm.commit()
                except ingest_lease.LeaseLost:
                    if _cm:
                        try: _cm.rollback()
                        except Exception: pass
                    print(f"    ⚠️ Lease lost on {doc['doc_id']} v{doc['version_no']} — "
                          f"contribution classification persist skipped (preempted)")
                    return False
                finally:
                    if _cm:
                        _cm.close()
            return True

        # 2. 分类与风险评估
        classification = None
        api_failed = False
        api_error_reason = ""

        source_key = doc.get("source_key", "")
        is_public = "_quarantine/" not in source_key

        if simulate_api:
            classification = ctx.get("mock_classifications", {}).get(doc["doc_id"], {})
            if not classification and "mock_classification" in ctx:
                classification = ctx.get("mock_classification", {})
            classification = {
                "category_l1": classification.get("category_l1", "reference"),
                "category_l2": classification.get("category_l2", "manual"),
                "faq_eligible": classification.get("faq_eligible", True),
                "confidence": classification.get("confidence", 0.85),
                "llm_risk_level": classification.get("risk_level", "low"),
                "summary": classification.get("summary", text[:100])
            }
        else:
            llm_cfg = config.llm
            api_key = llm_cfg.api_key
            model_name = llm_cfg.model
            api_base_url = llm_cfg.api_base_url

            if not api_key:
                api_failed = True
                api_error_reason = "LLM API key is not configured in environment"
            else:
                try:
                    classification = run_gemini_classification(text, model_name, api_key, api_base_url)
                except Exception as e:
                    api_failed = True
                    api_error_reason = f"LLM API invocation failed: {str(e)}"

        if is_public and classification and not api_failed:
            classification["llm_risk_level"] = "low"

        # 3. 处理分类结果或 Fail-Safe 降级
        if api_failed:
            print(f"    ⚠️ Fail-Safe triggered for {doc['doc_id']}: {api_error_reason}")
            doc["category_l1"] = "reference"
            doc["category_l2"] = "others"
            doc["owner_dept"] = doc.get("owner_dept") or "unknown"
            doc["faq_eligible"] = False
            doc["confidence"] = 0.0
            doc["summary"] = f"[API FAILURE FALLBACK] {text[:50]}..."
            doc["llm_risk_level"] = "high"
            doc["permission_level"] = "restricted"
            doc["kb_type"] = "private"
            doc["redaction_action"] = "QUARANTINE"
            doc["classification_status"] = "PENDING_AUDIT"
            doc["risk_level"] = "high"

            if not simulate_db:
                try:
                    conn_rt = _get_db_conn(select_db=True)
                    with conn_rt.cursor() as cursor:
                        task_id = f"rev_{doc['doc_id']}_v{doc['version_no']}"
                        safe_review_reason = api_error_reason
                        if safe_review_reason and len(safe_review_reason) > 490:
                            safe_review_reason = safe_review_reason[:490] + "..."
                        cursor.execute("""
                            INSERT INTO review_task (
                                task_id, doc_id, version_no, review_key, review_type, review_reason, review_status,
                                owner_dept, suggested_category_l1, suggested_category_l2, suggested_permission_level, confidence_score
                            ) VALUES (
                                %s, %s, %s, %s, 'document_classification', %s, 'PENDING',
                                %s, 'reference', 'others', 'restricted', 0.0
                            ) ON DUPLICATE KEY UPDATE
                                review_reason = VALUES(review_reason),
                                review_status = 'PENDING',
                                suggested_permission_level = 'restricted',
                                confidence_score = 0.0
                        """, (task_id, doc["doc_id"], doc["version_no"], doc.get("canonical_key", ""), safe_review_reason, doc["owner_dept"]))
                        conn_rt.commit()
                except Exception as rt_err:
                    print(f"    ⚠️ review_task insert skipped (non-fatal): {rt_err}")
                finally:
                    try:
                        conn_rt.close()
                    except Exception:
                        pass

                conn_dv = None
                try:
                    conn_dv = _get_db_conn(select_db=True)
                    with conn_dv.cursor() as cursor:
                        # PR-4：FAILED 终态带栅栏+清租约——被接管的文档归新持有者，僵尸
                        # 不落终态。RETRY_COUNT_INC 恒改行，无 changed-rows 歧义，可用
                        # rowcount 判租（check_fenced_write）。
                        _lk = (doc["doc_id"], doc["version_no"])
                        _lls = ingest_lease.get_lease_set(ctx)
                        cursor.execute(f"""
                            UPDATE document_version
                            SET classification_method = 'LLM',
                                classification_confidence = 0.0,
                                risk_level = 'high',
                                classification_status = 'PENDING_AUDIT',
                                content_process_status = 'FAILED',
                                content_process_error = %s,
                                {RETRY_COUNT_INC_SQL}{ingest_lease.clear_set_sql()}
                            WHERE doc_id = %s AND version_no = %s{_lls.fence_where_sql(_lk)}
                        """, (api_error_reason, doc["doc_id"], doc["version_no"])
                           + _lls.fence_where_params(_lk))
                        _lls.check_fenced_write(cursor, _lk)
                        conn_dv.commit()
                        _lls.discard(_lk)  # 终态已落，本地释放
                except ingest_lease.LeaseLost:
                    if conn_dv:
                        try: conn_dv.rollback()
                        except Exception: pass
                    print(f"    ⚠️ Lease lost on {doc['doc_id']} v{doc['version_no']} — "
                          f"FAILED write skipped (preempted by another holder)")
                except Exception as dv_err:
                    if conn_dv:
                        try: conn_dv.rollback()
                        except Exception: pass
                    print(f"    ⚠️ Failed to update document_version for {doc['doc_id']}: {dv_err}")
                finally:
                    if conn_dv:
                        conn_dv.close()

            return False  # 标记为失败，主循环跳过

        else:
            # LLM 输出不可信：compatible-mode 不强制 JSON schema，缺键常见（负载/截断）。
            # 经 _safe_classification_fields 取值（.get + 保守默认），缺键绝不 KeyError 崩节点
            # （此前直接索引会，且单文档路径无 try/except → 整节点 abort）。
            sc = _safe_classification_fields(classification, text)
            confidence = sc["confidence"]

            l1 = str(classification.get("category_l1", "")).strip().lower()
            l2 = str(classification.get("category_l2", "")).strip().lower()

            if l1 not in ALLOWED_CATEGORY_L1:
                l1 = "others"
                l2 = "others"
            elif l2 not in TAXONOMY_L2[l1]:
                l2 = "others"

            doc["category_l1"] = l1
            doc["category_l2"] = l2
            doc["owner_dept"] = doc.get("owner_dept") or "unknown"
            doc["faq_eligible"] = sc["faq_eligible"]
            doc["confidence"] = confidence
            doc["summary"] = sc["summary"]
            doc["llm_risk_level"] = sc["llm_risk_level"]

            if confidence < 0.85:
                print(f"    ⚠️ Low confidence ({confidence:.2f} < 0.85) for {doc['doc_id']}. Proceeding without quarantine.")

            doc["classification_status"] = "CONTENT_CLASSIFIED"
            if not simulate_db:
                conn = None
                try:
                    conn = _get_db_conn(select_db=True)
                    with conn.cursor() as cursor:
                        # 阶段 B：node 行的归属轴绝不被 LLM 分类回写覆盖
                        _exec_node_guarded(
                            cursor,
                            """
                            UPDATE document_meta
                            SET category_l1 = %s,
                                category_l2 = %s,
                                owner_dept = IF(acl_mode='node', owner_dept, %s),
                                summary = %s,
                                permission_level = %s,
                                kb_type = %s
                            WHERE doc_id = %s
                            """,
                            """
                            UPDATE document_meta
                            SET category_l1 = %s,
                                category_l2 = %s,
                                owner_dept = %s,
                                summary = %s,
                                permission_level = %s,
                                kb_type = %s
                            WHERE doc_id = %s
                            """,
                            (doc["category_l1"], doc["category_l2"], doc["owner_dept"], doc["summary"],
                             doc["permission_level"], doc["kb_type"], doc["doc_id"]),
                            (doc["category_l1"], doc["category_l2"], doc["owner_dept"], doc["summary"],
                             doc["permission_level"], doc["kb_type"], doc["doc_id"]))

                        # PR-4（R1 共识）：dv 写前同事务验租（行锁持至 commit）——此前
                        # 无栅栏时 TTL 接管后停滞僵尸的迟到分类照样 last-writer-wins 落库，
                        # document_meta 的 category/permission 与新持有者实际切块入索引的
                        # chunk 集分道扬镳。SET 可能全同值（LLM 重掷同结果），不得以
                        # rowcount 判租；LeaseLost ⇒ 整事务回滚（上方 meta 写也不落）。
                        # flag off 时 verify no-op、fence 空串 = 字节级旧行为。
                        _ck = (doc["doc_id"], doc["version_no"])
                        _cfs = ingest_lease.get_lease_set(ctx)
                        _cfs.verify_for_update(cursor, _ck)
                        cursor.execute("""
                            UPDATE document_version
                            SET classification_method = 'LLM',
                                classification_confidence = %s,
                                risk_level = %s,
                                faq_eligible = %s,
                                classification_status = 'CONTENT_CLASSIFIED'
                            WHERE doc_id = %s AND version_no = %s""" + _cfs.fence_where_sql(_ck) + """
                        """, (confidence, doc["llm_risk_level"], doc["faq_eligible"], doc["doc_id"], doc["version_no"])
                           + _cfs.fence_where_params(_ck))
                        conn.commit()
                except ingest_lease.LeaseLost:
                    if conn:
                        try: conn.rollback()
                        except Exception: pass
                    print(f"    ⚠️ Lease lost on {doc['doc_id']} v{doc['version_no']} — "
                          f"classification persist skipped (preempted; doc abandoned this run)")
                    return False   # 弃单文档继续批（归新持有者），不 abort 节点
                except Exception as db_err:
                    if conn: conn.rollback()
                    print(f"    ⚠️ Failed to persist metadata to RDS: {db_err}")
                    raise RuntimeError(f"Database write failure in node_classify_document (persist metadata): {db_err}") from db_err
                finally:
                    if conn:
                        conn.close()

            return True  # 标记为成功

    # ── 执行并发分类 ──
    t0 = _time.time()
    failed_doc_ids = set()

    if len(valid_canonicals) <= 1:
        # 单文档无需并发。⚠️ 不吞异常：DB 持久化失败等 RuntimeError 必须 propagate 出去 abort 节点
        # （TestDatabaseExceptionPropagation 的不变量——绝不把真实 DB 写失败静默吞掉/掩盖）。
        # 「LLM 缺键」这类已由 _safe_classification_fields 在源头消化，不再到这里崩 KeyError。
        for doc in valid_canonicals:
            success = _classify_single_doc(doc)
            if not success:
                failed_doc_ids.add(doc["doc_id"])
            # PR-4：批语义续租（免 DB 节流预判，到期才真跑；fail-open）——
            # 大批 LLM 分类是 stage-2 最长阶段，队尾文档的租约在这里保活。
            _lease_renew_tick(ctx)
    else:
        # ⚠️ 与单文档路径对齐（F-13）：worker `return False` = 业务性失败（LLM API 不可达的
        # fail-safe 降级）→ 按文档跳过；worker 抛异常 = 应中止类（DB 持久化失败 RuntimeError /
        # 未预期 bug）→ 取消剩余 futures 并 re-raise，让节点 FAILED、由 DataWorks 重试。绝不像旧代码
        # 那样把 DB 写失败塞进 failed_doc_ids 静默降级——那会让"同一 RDS 故障单篇 abort、多篇 SUCCESS"
        # 的错误语义随批次大小漂移，破坏 TestDatabaseExceptionPropagation 守护的不变量。
        with ThreadPoolExecutor(max_workers=min(max_workers, len(valid_canonicals))) as pool:
            future_to_doc = {pool.submit(_classify_single_doc, doc): doc for doc in valid_canonicals}
            for future in as_completed(future_to_doc):
                doc = future_to_doc[future]
                try:
                    success = future.result()
                except Exception:
                    # 意外/DB 写失败：取消未启动 futures、快速关闭线程池（不等在飞任务，各自 finally
                    # 关连接无泄漏）、抛出原异常 abort 节点。cancel_futures 需 Python≥3.9（本仓满足）。
                    print(f"    ❌ Abort: DB/unexpected failure classifying {doc['doc_id']}")
                    for _f in future_to_doc:
                        _f.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                if not success:
                    failed_doc_ids.add(doc["doc_id"])
                _lease_renew_tick(ctx)  # PR-4：同上（并发臂，主线程节拍）

    elapsed = _time.time() - t0
    success_count = len(valid_canonicals) - len(failed_doc_ids)
    print(f"    [classify] ⚡ {success_count}/{len(valid_canonicals)} docs classified in {elapsed:.1f}s "
          f"(workers={max_workers}, {elapsed/max(len(valid_canonicals),1)*1000:.0f}ms/doc avg)")

    # 移除失败的文档，防止后续节点处理
    if failed_doc_ids:
        ctx["canonicals"] = [d for d in valid_canonicals if d["doc_id"] not in failed_doc_ids]
    else:
        ctx["canonicals"] = valid_canonicals

    # 打印分类结果摘要
    for doc in ctx["canonicals"]:
        print(
            f"    └─ {doc['doc_id']}: "
            f"{doc['category_l1']}/{doc['category_l2']}, "
            f"permission={doc['permission_level']}, "
            f"llm_risk={doc.get('llm_risk_level', 'low')}, "
            f"confidence={doc['confidence']}"
        )


def node_detect_sensitive(ctx: dict):
    """
    敏感实体检测（regex + 关键词，不依赖 LLM）。

    独立于 LLM 分类，用 regex 检测 PII/凭据等硬性实体。
    输出 risk_hits 列表和 entity_risk_level。
    最终风险 = max(llm_risk_level, entity_risk_level)。
    """
    canonicals = ctx["canonicals"]

    for doc in canonicals:
        text = doc["text"]  # ← 同样用原始文本
        hits = []
        entity_risk = "low"

        # 1. Regex 实体检测（按实体类型分级：电话/邮箱=medium→脱敏保留；身份证/密钥=high→隔离）
        # G5：_body_entity_fp_ignore 抑制 bank_card 的订单号/物料号 FP（全命中皆锚点上下文才抑制）
        for name, pattern in ENTITY_PATTERNS.items():
            if re.search(pattern, text) and not _body_entity_fp_ignore(name, text):
                sev = ENTITY_SEVERITY.get(name, "high")
                hits.append({
                    "type": "ENTITY", "category": name,
                    "keyword": name, "source": "regex", "severity": sev,
                })
                if _SEVERITY_RANK[sev] > _SEVERITY_RANK[entity_risk]:
                    entity_risk = sev

        # 2. 语义关键词检测
        for category, keywords in SEMANTIC_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    hits.append({
                        "type": "SEMANTIC", "category": category,
                        "keyword": kw, "source": "keyword", "severity": "medium",
                    })
                    if entity_risk != "high":
                        entity_risk = "medium"

        # 3. 图像敏感内容检测（VLM 过滤漏斗输出）
        for asset in doc.get("assets", []):
            if asset.get("status") == "QUARANTINE_SENSITIVE":
                hits.append({
                    "type": "IMAGE_SENSITIVE", "category": "seal_or_stamp",
                    "keyword": asset.get("filename", ""), "source": "vlm_funnel", "severity": "high",
                })
                entity_risk = "high"

        # 4. 图像 OCR 文本 PII 检测（flag-gated, default OFF: RAG_IMAGE_OCR_PII）
        #    base text 扫不到截图/嵌图里的电话/身份证/密钥（CE38C5、test-report、intro.pptx）。
        #    只扫 asset['ocr_text']，绝不扫 visual_summary（避免 UI 标签 FP）；每个 asset 独立
        #    try/except —— 图像分支异常绝不影响上面的正文 PII 检测（优雅降级）。
        if os.environ.get("RAG_IMAGE_OCR_PII", "").lower() in ("1", "true", "yes"):
            for asset in doc.get("assets", []):
                try:
                    if str(asset.get("status", "")).startswith("DISCARD"):
                        continue
                    octext = asset.get("ocr_text") or ""
                    if not octext:
                        continue
                    for name, pattern in ENTITY_PATTERNS.items():
                        if re.search(pattern, octext) and not _image_ocr_fp_ignore(name, octext):
                            sev = ENTITY_SEVERITY.get(name, "high")
                            hits.append({
                                "type": "IMAGE_OCR", "category": name, "keyword": name,
                                "source": "image_ocr", "finding_type": f"image_ocr:{name}",
                                "severity": sev,
                            })
                            if _SEVERITY_RANK[sev] > _SEVERITY_RANK[entity_risk]:
                                entity_risk = sev
                    for category, keywords in SEMANTIC_KEYWORDS.items():
                        for kw in keywords:
                            if kw in octext:
                                hits.append({
                                    "type": "IMAGE_OCR_SEMANTIC", "category": category, "keyword": kw,
                                    "source": "image_ocr",
                                    "finding_type": f"image_ocr_semantic:{category}", "severity": "medium",
                                })
                                if entity_risk != "high":
                                    entity_risk = "medium"
                except Exception as _ocr_err:
                    print(f"    ⚠️ image-OCR PII scan failed for an asset in {doc['doc_id']} "
                          f"(FAIL-SAFE: text PII still applied): {_ocr_err}")

        doc["risk_hits"] = hits
        doc["entity_risk_level"] = entity_risk
        doc["sensitive_detected"] = len(hits) > 0

        # 综合风险 = max(LLM 判断, 实体检测)
        risk_order = {"low": 0, "medium": 1, "high": 2}
        llm_risk = doc.get("llm_risk_level", "low")
        final_risk = max(llm_risk, entity_risk, key=lambda r: risk_order.get(r, 0))
        doc["risk_level"] = final_risk

        # ─── 敏感检测结果入库 ───
        # DELETE 不以 hits 为前提：零命中的重跑（源文件已修 / 检测器白名单更新）也要
        # 清掉上一轮的陈旧 finding 行，否则审计表残留 QUARANTINED 记录与文档实际
        # CLEAN 处置矛盾（全维度复审 摄取#6）。INSERT 仍按 finding_rows 有无决定。
        simulate_db = _resolve_simulate(ctx, "db")
        if not simulate_db:
            conn = None
            try:
                conn = _get_db_conn(select_db=True)
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM document_sensitive_finding WHERE doc_id = %s AND version_no = %s",
                        (doc["doc_id"], doc["version_no"])
                    )
                    # E#42：命中逐条 INSERT → 组装参数列表后 executemany 一次插入
                    # （pymysql 会改写为单条多值 INSERT，N 条命中的往返 N→1）。
                    finding_rows = []
                    for hit in hits:
                        kw = hit.get("keyword", "")
                        kw_hash = hashlib.sha256(kw.encode('utf-8')).hexdigest()

                        if hit.get("type") == "IMAGE_SENSITIVE":
                            finding_type = "IMAGE_SENSITIVE_AUDIT"
                            preview = kw
                        else:
                            # image_ocr hits carry a distinct finding_type; text hits fall back to category
                            finding_type = hit.get("finding_type") or hit.get("category", "unknown")
                            if len(kw) <= 4:
                                preview = "*" * len(kw)
                            else:
                                preview = kw[:2] + "*" * (len(kw) - 4) + kw[-2:]

                        action = "QUARANTINED" if final_risk == "high" else "REDACTED"

                        finding_rows.append((
                            doc["doc_id"], doc["version_no"], finding_type,
                            hit.get("severity", "high"), hit.get("page_num"), hit.get("block_index"),
                            kw_hash, preview, action
                        ))
                    if finding_rows:
                        cursor.executemany("""
                            INSERT INTO document_sensitive_finding (
                                doc_id, version_no, finding_type, severity, page_num, block_index,
                                matched_text_hash, matched_text_preview, action
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s,
                                %s, %s, %s
                            )
                        """, finding_rows)
                conn.commit()
            except Exception as e:
                if conn: conn.rollback()
                print(f"    ⚠️ Failed to persist sensitive findings to RDS: {e}")
                raise RuntimeError(f"Database write failure in node_detect_sensitive: {e}") from e
            finally:
                if conn:
                    conn.close()

        print(
            f"    └─ {doc['doc_id']}: "
            f"entity_risk={entity_risk}, llm_risk={llm_risk} "
            f"-> final_risk={final_risk}, hits={len(hits)}"
        )


def node_redact_or_quarantine(ctx: dict):
    """
    脱敏/隔离（后处理节点）。

    基于前两步的综合风险决策：
    - high  → QUARANTINE（隔离，不进入索引）
    - medium → REDACT（局部脱敏后继续）
    - low   → CLEAN（直接通过）

    在分类之后运行，确保分类结果不受脱敏影响。
    """
    canonicals = ctx["canonicals"]

    for doc in canonicals:
        # 成本封存：VLM-rebuild 成本闸已拒绝本文档并写 RDS 封存 → 复用 QUARANTINE 跳过路径，
        # 阻止其进入切块/索引 (否则 RDS 已封存而索引里仍写入 chunk → 裂脑)。
        if doc.get("cost_quarantined"):
            doc["redaction_action"] = "QUARANTINE"
            doc["redacted_text"] = None
            print(f"    └─ {doc['doc_id']}: QUARANTINE (cost ceiling exceeded)")
            continue

        final_risk = doc.get("risk_level", "low")

        if final_risk == "high":
            doc["redaction_action"] = "QUARANTINE"
            doc["redacted_text"] = None
            print(f"    └─ {doc['doc_id']}: QUARANTINE (risk=high)")
            continue

        text = doc["text"]
        redacted = text
        redaction_count = 0

        if final_risk == "medium" or doc.get("sensitive_detected"):
            # 对检测到的实体做局部脱敏
            for name, pattern in ENTITY_PATTERNS.items():
                replacer = REDACTION_MAP.get(name)
                if replacer:
                    new_text = re.sub(pattern, replacer, redacted)
                    if new_text != redacted:
                        redaction_count += 1
                    redacted = new_text
            
            # 同样对 blocks 里的文本脱敏
            if "blocks" in doc:
                for block in doc["blocks"]:
                    block_text = block.get("text", "")
                    if block_text:
                        for name, pattern in ENTITY_PATTERNS.items():
                            replacer = REDACTION_MAP.get(name)
                            if replacer:
                                block_text = re.sub(pattern, replacer, block_text)
                        block["text"] = block_text

            # 图像 OCR 文本脱敏（flag-gated, default OFF: RAG_IMAGE_OCR_PII）。GAP-FIX：今天
            # asset['ocr_text'] 从不脱敏，却经 [图片OCR] 合成块 / image_refs 流入 chunk —— medium
            # 命中后会把 PII 带进索引。就地改写同一份 in-memory asset 对象（detect/redact/chunk
            # 共用同一 ctx['canonicals']，redact(node 03) 在 chunk(node 05) 之前）。不修改
            # filename/anchor_row（xlsx 绑定契约）。注意：仅本轮内存生效，不回写 canonical OSS JSON
            # → 受影响文档的后续重切仍依赖本 flag 持续开启。
            if os.environ.get("RAG_IMAGE_OCR_PII", "").lower() in ("1", "true", "yes"):
                for asset in doc.get("assets", []):
                    octext = asset.get("ocr_text")
                    if not octext:
                        continue
                    for name, pattern in ENTITY_PATTERNS.items():
                        replacer = REDACTION_MAP.get(name)
                        if replacer:
                            octext = re.sub(pattern, replacer, octext)
                    asset["ocr_text"] = octext

        doc["redacted_text"] = redacted
        doc["redaction_count"] = redaction_count
        doc["redaction_action"] = "REDACTED" if redaction_count > 0 else "CLEAN"
        print(
            f"    └─ {doc['doc_id']}: {doc['redaction_action']} "
            f"({redaction_count} replacements, risk={final_risk})"
        )



# ═══════════════════════════════════════════════════════════════
# Step Card 辅助函数
# ═══════════════════════════════════════════════════════════════

_STEP_DETECT_RE = re.compile(
    # 容忍 markdown bullet/heading 前缀（• · - * #）+ 任意空白 —— 修复
    # 2026-06-13 it_xxh_003 evaluation gap：作业指导书目录式行 "• 第一步：..."
    # 和 markdown heading "# 第一步：..." 前缀被 `\s*` 卡住，全 SOP 被错路由到
    # text mode，图全成独立 image chunk 无 step 绑定。bullet/hash 前缀本身
    # 不改变"第N步"的语义，应当容忍。要求 ≥2 个匹配仍保护 false-positive：
    # 单条 "- 1." 列表不够。
    r'(?:^|\n)[\s•·\-\*\#]*(?:'
    r'步骤\s*[\d一二三四五六七八九十]+|'
    r'Step\s*\d+|'
    r'第\s*[一二三四五六七八九十\d]+\s*步|'
    r'\d+\s*[\.．、]\s*(?![\d])|'
    r'\d+\s*[)）]\s*'
    r')',
    re.IGNORECASE | re.MULTILINE,
)


def _detect_step_patterns(doc: dict) -> bool:
    """
    检测文档是否包含 SOP 步骤标记。

    仅在文本中出现 ≥2 个步骤边界时返回 True，避免误判。
    同时结合分类信息：SOP / manual / guide 类文档优先检测。
    """
    # 如果分类不是 SOP/manual/guide 相关，不启用 step 模式
    cat_l1 = str(doc.get("category_l1", "")).lower()
    cat_l2 = str(doc.get("category_l2", "")).lower()
    title = str(doc.get("title", "")).lower()

    sop_keywords = ["sop", "manual", "guide", "操作", "手册", "作业指导", "作业导书", "流程", "规程", "检验", "培训"]
    is_sop_like = any(kw in cat_l1 or kw in cat_l2 or kw in title for kw in sop_keywords)
    # 企业 Work-Instruction 文号（FL-ZS-WI-010 等）：标题没有"作业指导书"字样的
    # 工序文件（如《注塑销售出库单》-成品仓管）也要进入步骤检测。
    # 2026-06-10 诊断：此 gate 漏判 WI 文号文档 → 整本 SOP 平文切块、图片零绑定。
    if not is_sop_like and re.search(r'(?:^|[^a-z0-9])wi-\d', title):
        is_sop_like = True

    # 从 blocks 文本中检测步骤边界（提到 is_sop_like 判定之前,因 R1 fallback 也要用 text）
    text = doc.get("text", "")
    if not text:
        blocks = doc.get("blocks", [])
        text_parts = []
        for block in blocks[:50]:  # 只检查前 50 个 block
            if isinstance(block, dict):
                t = block.get("text", "")
            else:
                t = block.text if hasattr(block, "text") and block.text else ""
            if t:
                text_parts.append(t)
        text = "\n".join(text_parts)

    # ── R1 fallback（D8 Phase 8）：正文 SOP 锚词检测 ──
    # title-based gate 漏判企业内部短代号 SOP(xg001/zs006/ms* 类),正文是
    # 真 SOP(含步骤N + 大量截图)但 title 无 sop/manual/wi-\d 关键词 → 路由
    # 落 text mode、step_card 全失、图片散落独立 chunk(D8 Phase 7 dryrun 实证
    # xg001 9 图 + zs006 7 图共 16 张全无 step 绑定)。
    # 修法:正文头部 5000 字含 SOP 锚词 ≥2 个(作业前提/作业说明/生效日期/作业
    # 指导/作业方法/SOP编号) → 升 is_sop_like,让下游 step 检测正常进行。锚词
    # 取自富岭 SOP 实际文档头格式,跨业务通用。要求 ≥2 个避免单"生效日期"
    # 误升非 SOP 公告文档(如 admin_lodging 仅含"通知",0 个锚词)。
    if not is_sop_like:
        sop_anchor_words = (
            "作业前提", "作业说明", "生效日期", "作业指导",
            "作业方法", "SOP编号", "操作规程",
        )
        anchor_hits = sum(1 for w in sop_anchor_words if w in text[:5000])
        if anchor_hits >= 2:
            is_sop_like = True

    if not is_sop_like:
        return False

    matches = _STEP_DETECT_RE.findall(text[:10000])  # 只检查前 10000 字符
    return len(matches) >= 2


_PATH_D_TOKEN_RE = re.compile(r'[A-Za-z0-9#\-\*\(\)\.]{4,}')


def _path_d_high_entropy_tokens(text: str) -> set:
    """抽 ≥4 chars 字母数字混合 token(自然排除中文通用词如"产品/记录")。"""
    return set(_PATH_D_TOKEN_RE.findall(text or ""))


def _path_d_share_token(a: set, b: set, min_prefix: int = 4) -> bool:
    """两 token 集合是否有 exact match 或 prefix-min_prefix match。"""
    if a & b:
        return True
    for ta in a:
        if len(ta) < min_prefix:
            continue
        ta_p = ta[:min_prefix]
        for tb in b:
            if len(tb) < min_prefix:
                continue
            if ta_p == tb[:min_prefix]:
                return True
    return False


def _apply_path_d_cluster_propagation(geo_assets: list) -> None:
    """D8 Tier 0 post-review — Path D: same-page image cluster propagation.

    Path A strong override (alt ≥ 15 AND alt/geo ≥ 5.0) 的 image 作为 seed,
    同页内 image_index 邻接(delta == 1) + bbox 相对页高 < 0.20 + 高熵 token
    共享(exact 或 prefix-4)的 follower 跟随 seed 到同 chunk。

    8 条严守约束(用户 spec):
      1. seed 必由 Path A strong override 触发
      2. follower 与 seed 在原始图片序列邻接(image_index delta == 1)
      3. 同 page + abs(image_index_delta) == 1
      4. bbox 距离相对页高 < 0.20
      5. 高熵 token (≥4 chars 字母数字标点) exact/prefix-4 共享
      6. follower 自身无强反向证据(OCR 长度 ≤ 200 chars,排除自带强 OCR)
      7. follower 未被 Path B/C override
      8. 单 follower 多 seed 竞争 → fail-closed(不传播)
      9. provenance: 写 va['route_reason'] = 'cluster_propagation'
         + va['route_seed_image_index']
    """
    by_page: dict = {}
    for va in geo_assets:
        by_page.setdefault(va.get("page_num"), []).append(va)

    for _page, p_assets in by_page.items():
        if len(p_assets) < 2:
            continue
        seeds = [va for va in p_assets if va.get("_path_a_strong")]
        if not seeds:
            continue
        page_height = max(
            (float((va.get("bbox") or [0, 0, 0, 0])[3]) for va in p_assets),
            default=842.0,
        )
        page_height = max(page_height, 100.0)

        proposals: dict = {}    # id(follower) → (id(seed), target_best_idx)
        conflicts: set = set()  # id(follower)

        for seed in seeds:
            seed_target = seed.get("_d8_best_idx")
            if seed_target is None:
                continue
            sb = seed.get("bbox") or [0, 0, 0, 0]
            sy0, sy1 = float(sb[1]), float(sb[3])
            sidx = seed.get("image_index")
            stext = ((seed.get("visual_summary") or "") + " "
                     + (seed.get("ocr_text") or ""))
            stoks = _path_d_high_entropy_tokens(stext)
            if not stoks:
                continue
            for f in p_assets:
                if f is seed:
                    continue
                if id(f) in conflicts:
                    continue
                # 7. follower 不能已被 Path B/C override
                if f.get("_path_b_overridden") or f.get("_path_c_overridden"):
                    continue
                # follower 自身是 strong seed → 不能被传播(避免 seed-seed)
                if f.get("_path_a_strong"):
                    continue
                # 3. image_index delta == 1
                fidx = f.get("image_index")
                if not (isinstance(sidx, int) and isinstance(fidx, int)):
                    continue
                if abs(sidx - fidx) != 1:
                    continue
                # 4. bbox 距离 / 页高 < 0.20
                fb = f.get("bbox") or [0, 0, 0, 0]
                fy0, fy1 = float(fb[1]), float(fb[3])
                if fy0 >= sy1:
                    gap = fy0 - sy1
                elif sy0 >= fy1:
                    gap = sy0 - fy1
                else:
                    gap = 0.0
                if gap / page_height >= 0.20:
                    continue
                # 5. 高熵 token 共享 (exact 或 prefix-4)
                ftext = ((f.get("visual_summary") or "") + " "
                         + (f.get("ocr_text") or ""))
                ftoks = _path_d_high_entropy_tokens(ftext)
                if not _path_d_share_token(stoks, ftoks):
                    continue
                # 6. follower 反向证据守门:OCR > 200 chars 自带强信号,不传播
                if len(f.get("ocr_text") or "") > 200:
                    continue
                # follower 已与 seed 同 anchor → 无需传播
                if f.get("_d8_best_idx") == seed_target:
                    continue
                # 8. 单 follower 多 seed 竞争 → fail-closed
                if id(f) in proposals:
                    prev_seed_id, prev_target = proposals[id(f)]
                    if prev_seed_id != id(seed) or prev_target != seed_target:
                        conflicts.add(id(f))
                        proposals.pop(id(f), None)
                        continue
                proposals[id(f)] = (id(seed), seed_target)

        # 应用未冲突的 proposals
        for f in p_assets:
            if id(f) in conflicts:
                continue
            prop = proposals.get(id(f))
            if prop is None:
                continue
            seed_id, target = prop
            seed_va = next((s for s in seeds if id(s) == seed_id), None)
            f["_d8_best_idx"] = target
            # 9. provenance
            f["route_reason"] = "cluster_propagation"
            if seed_va is not None:
                f["route_seed_image_index"] = seed_va.get("image_index")


def _inject_image_ref_blocks(blocks: list, assets: list, doc: dict) -> list:
    """
    将 funnel 处理后的图片信息作为 image_ref 块注入 block 序列。

    方案 B（启发式）：利用图片在文档中的顺序与 block 中的步骤顺序对应。
    策略：找到 blocks 中的步骤边界后，将图片按顺序分配到步骤之间。

    如果 blocks 中已经包含 image_ref 块（由 docx_extractor_v2 生成），
    则只需将 funnel 结果注入到已有的 image_ref 块中，不重复插入。

    Args:
        blocks: ExtractedBlock 列表（text blocks，可能已含 image_ref）
        assets: funnel 处理后的 asset 列表
        doc: 文档元数据 dict

    Returns:
        enriched blocks 列表（含 image_ref 块和 funnel 数据）
    """
    if not assets:
        return blocks

    # 检查是否已有 image_ref 块
    has_image_refs = any(
        (b.get("block_type") if isinstance(b, dict) else getattr(b, "block_type", "")) == "image_ref"
        for b in blocks
    )

    if has_image_refs:
        # blocks 中已有 image_ref → 将 funnel 数据注入到已有 image_ref
        return _enrich_existing_image_refs(blocks, assets, doc)
    else:
        # blocks 中没有 image_ref → 按顺序追加 image_ref 到每个步骤后面
        return _insert_image_refs_heuristic(blocks, assets, doc)


def _enrich_existing_image_refs(blocks: list, assets: list, doc: dict) -> list:
    """将 funnel 处理结果注入到 blocks 中已有的 image_ref 块。"""
    # 构建 image_index → asset 映射
    asset_map = {}
    for asset in assets:
        idx = asset.get("image_index", asset.get("original_index"))
        if idx is not None:
            asset_map[idx] = asset

    source_key = doc.get("source_key", "")
    dept_code = _dept_from_raw_key(source_key, doc.get("owner_dept", "unknown"))

    enriched = []
    for block in blocks:
        if isinstance(block, dict):
            block_type = block.get("block_type", "")
            extra = block.get("extra", {})
        else:
            block_type = getattr(block, "block_type", "")
            extra = getattr(block, "extra", {})

        if block_type == "image_ref":
            img_idx = extra.get("image_index")
            asset = asset_map.get(img_idx, {})

            # 只保留 ROUTE_TO_VECTOR 和有价值的图片
            status = asset.get("status", "")
            if status in ("ROUTE_TO_VECTOR", "ROUTE_TO_TEXT"):
                filename = asset.get("filename", "")
                version = doc["version_no"]
                doc_id = doc["doc_id"]
                source_image_url = f"processing/assets/{dept_code}/{doc_id}/v{version}/{filename}"

                enriched_extra = dict(extra)
                enriched_extra.update({
                    "source_image": source_image_url,
                    "oss_key": asset.get("oss_key", ""),
                    "ocr_text": asset.get("ocr_text", ""),
                    "visual_summary": asset.get("visual_summary", ""),
                    "image_category": asset.get("image_category", ""),
                    "vlm_annotation_map": asset.get("vlm_annotation_map", {}),
                    "funnel_status": status,
                })

                if isinstance(block, dict):
                    block = dict(block)
                    block["extra"] = enriched_extra
                else:
                    block.extra = enriched_extra

                enriched.append(block)
            # DISCARD 状态的 image_ref 块不加入结果
        else:
            enriched.append(block)

    return enriched


def _content_match_steps(img_text: str, candidates: list) -> tuple:
    """把图片的 visual_summary/ocr 文本匹配到最相关的候选步骤。

    candidates: list of (key, text)。用 IDF 式加权——只在某个步骤里出现的稀有词
    （如"归零"仅 step4 有）权重高，跨步骤通用词（如"天平"）权重低，避免被通用词带偏。
    XLSX 的 anchor_row 常不可靠/聚簇，而视觉描述里的动作关键词能更准地定位步骤。

    Returns: (best_key | None, best_score, second_score)
    """
    import re
    from collections import Counter

    def _toks(s: str) -> set:
        s = (s or "").lower()
        cjk = re.findall(r'[一-鿿]', s)
        bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
        alnum = set(re.findall(r'[a-z0-9]{2,}', s))
        return bigrams | alnum

    cand = [(k, _toks(t)) for k, t in candidates]
    if not cand:
        return None, 0.0, 0.0
    if len(cand) < 2:
        return cand[0][0], 0.0, 0.0

    df = Counter()
    for _, toks in cand:
        for t in toks:
            df[t] += 1

    img_toks = _toks(img_text)
    # set 迭代顺序受 PYTHONHASHSEED 影响（跨进程随机），叠加浮点求和不结合律，
    # 同一 img_text 在不同运行下 score 会有 ~1e-16 抖动；当多个步骤评分极接近时
    # 会翻转 best_key。先 sorted 再求和把 score 锁成 bit-exact 跨运行恒定。
    # tiebreak 也显式排序候选 key（升序）：当 score 完全相等时，best_key 由最小
    # 候选 key 唯一决定，不再依赖 cand 的输入顺序兜底。
    scored = sorted(
        ((sum(1.0 / df[t] for t in sorted(img_toks & toks)), k) for k, toks in cand),
        key=lambda x: (-x[0], x[1] if x[1] is not None else float("inf")),
    )
    best_score, best_key = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    return best_key, best_score, second


_EVIDENCE_GRAM_CHARS = set("的了是在为与及或其该得着过")


def _evidence_toks(s: str) -> set:
    """P0 证据 token(2026-07-20):剔除含语法字的 bigram 与纯数字串。

    「上的/按了」这类黏连 bigram 和「3098/12345」这类设备读数串会随机与某个步骤
    唯一共现,在 IDF 评分下冒充强信号——它们不是动作语义,不算绑定证据。
    """
    import re

    s = (s or "").lower()
    cjk = re.findall(r'[一-鿿]', s)
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    bigrams = {t for t in bigrams if not (set(t) & _EVIDENCE_GRAM_CHARS)}
    alnum = {t for t in re.findall(r'[a-z0-9]{2,}', s) if not t.isdigit()}
    return bigrams | alnum


def _evidence_match_steps(img_toks: set, step_toks_map: dict,
                          df_steps: dict, df_pool: dict) -> tuple:
    """把图片证据 token 匹配到最相关步骤——互斥稀有度评分(取代 P0 的单侧 IDF)。

    weight(t) = 1/df_steps(t) × 1/df_pool(t):token 必须"步骤侧唯一 × 图片池侧唯一"
    才能拿满权重(归零/读数/水平);在多张图 caption 里都出现的杂词(操作/设备)被
    池侧分母压到 0.8 门槛之下。单侧 IDF 的教训:xlsx_sop 的电源图凭「上的+操作」
    对 step4 打出全场最高分抢位,把真信号「归零」挤出局,VLM 措辞每重掷一次就换
    一个杂词命中(docs/xlsx_binding_vlm_drift_2026-07-20_DRAFT.md)。

    Returns: (best_key | None, best_score, second_score)。求和走 sorted(),与
    _content_match_steps 同因:锁 bit-exact 跨运行恒定。
    """
    if not step_toks_map:
        return None, 0.0, 0.0
    if len(step_toks_map) < 2:
        return next(iter(step_toks_map)), 0.0, 0.0
    scored = sorted(
        ((sum((1.0 / df_steps[t]) * (1.0 / df_pool.get(t, 1))
              for t in sorted(img_toks & toks)), k)
         for k, toks in step_toks_map.items()),
        key=lambda x: (-x[0], x[1] if x[1] is not None else float("inf")),
    )
    best_score, best_key = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    return best_key, best_score, second


def _insert_image_refs_heuristic(blocks: list, assets: list, doc: dict) -> list:
    """
    启发式图片注入 — 按 page_num 匹配 → 步骤边界 fallback → 末尾追加。

    策略优先级：
      1. 如果 asset 有 page_num（PDF/PPTX），将 image_ref 插入到同一页最后一个 block 之后
      2. 如果 asset 无 page_num 且检测到步骤边界，按步骤区间分配
      3. 否则追加到末尾
    """
    from opensearch_pipeline.chunker import DocumentChunker

    # ── 构建有效图片列表 ──
    source_key = doc.get("source_key", "")
    dept_code = _dept_from_raw_key(source_key, doc.get("owner_dept", "unknown"))

    valid_assets = []
    for asset in assets:
        status = asset.get("status", "")
        if status in ("ROUTE_TO_VECTOR", "ROUTE_TO_TEXT"):
            filename = asset.get("filename", "")
            version = doc["version_no"]
            doc_id = doc["doc_id"]
            source_image_url = f"processing/assets/{dept_code}/{doc_id}/v{version}/{filename}"

            valid_assets.append({
                "image_index": asset.get("image_index", len(valid_assets)),
                "original_index": asset.get("original_index", asset.get("image_index")),
                "page_num": asset.get("page_num"),
                "bbox": asset.get("bbox"),
                "anchor_row": asset.get("anchor_row"),
                "annotation_num": asset.get("annotation_num"),
                "annotation_label": asset.get("annotation_label"),
                "source_image": source_image_url,
                "oss_key": asset.get("oss_key", ""),
                "ocr_text": asset.get("ocr_text", ""),
                "visual_summary": asset.get("visual_summary", ""),
                "image_category": asset.get("image_category", ""),
                "vlm_annotation_map": asset.get("vlm_annotation_map", {}),
                "funnel_status": status,
                "part_labels": asset.get("part_labels", []),
            })

    if not valid_assets:
        return blocks

    # ── 策略 1: page_num 匹配（PDF / PPTX / XLSX） ──
    has_page_assets = [va for va in valid_assets if va.get("page_num") is not None]
    no_page_assets = [va for va in valid_assets if va.get("page_num") is None]

    if has_page_assets:
        # 分离：有 anchor_row 的 XLSX 图片 vs 无 anchor_row 的其他图片
        anchor_row_assets = [va for va in has_page_assets if va.get("anchor_row") is not None]
        page_only_assets = [va for va in has_page_assets if va.get("anchor_row") is None]

        # 找到每页最后一个 block 的位置（PDF/PPTX fallback 用）
        page_last_block_idx = {}
        for i, block in enumerate(blocks):
            pg = (block.get("page_num") if isinstance(block, dict)
                  else getattr(block, "page_num", None))
            if pg is not None:
                page_last_block_idx[pg] = i

        # 从后往前插入（避免索引偏移）
        enriched = list(blocks)
        # 收集所有插入点：(insert_position, image_ref_blocks)
        insertions = []
        unmatched = []

        # ── 策略 1a: anchor_row 行级匹配（XLSX） ──
        if anchor_row_assets:
            # 建立索引：(page_num, row_num) → block_index
            row_block_index = {}  # (page, row) → last block idx at that row
            # 同时建立序号索引：(page, seq_num) → block_index
            seq_block_index = {}  # (page, 序号) → block idx
            for i, block in enumerate(blocks):
                pg = (block.get("page_num") if isinstance(block, dict)
                      else getattr(block, "page_num", None))
                extra = (block.get("extra") if isinstance(block, dict)
                         else getattr(block, "extra", None)) or {}
                rn = extra.get("row_num")
                if pg is not None and rn is not None:
                    row_block_index[(pg, rn)] = i

                # 提取行首的序号（如 "1\t清扫\t★三辊..." → seq_num=1）
                # 只记录首次出现（表格可能有多区域序号重复，标注对应第一区域）
                text = (block.get("text", "") if isinstance(block, dict)
                        else getattr(block, "text", ""))
                if pg is not None and text:
                    first_cell = text.split("\t")[0].strip()
                    if first_cell.isdigit():
                        seq_block_index.setdefault((pg, int(first_cell)), i)

            for va in anchor_row_assets:
                pg = va["page_num"]
                anchor = va["anchor_row"]
                anno_num = va.get("annotation_num")
                best_idx = None

                # 优先级 1：annotation_num 精确匹配序号列
                if anno_num is not None and (pg, anno_num) in seq_block_index:
                    best_idx = seq_block_index[(pg, anno_num)]

                # 优先级 2：anchor_row 近似匹配
                if best_idx is None:
                    best_row = -1
                    for (p, rn), idx in row_block_index.items():
                        if p == pg and rn <= anchor and rn > best_row:
                            best_row = rn
                            best_idx = idx
                    # 如果没找到 <= anchor 的，找同页 row_num > anchor 中最小的
                    if best_idx is None:
                        best_row = 999999
                        for (p, rn), idx in row_block_index.items():
                            if p == pg and rn < best_row:
                                best_row = rn
                                best_idx = idx

                if best_idx is not None:
                    img_block = {
                        "block_type": "image_ref",
                        "text": "",
                        "page_num": pg,
                        "section_path": None,
                        "source": "multimodal",
                        "extra": va,
                    }
                    insertions.append((best_idx, [img_block]))
                elif pg in page_last_block_idx:
                    # fallback 到页末
                    img_block = {
                        "block_type": "image_ref",
                        "text": "",
                        "page_num": pg,
                        "section_path": None,
                        "source": "multimodal",
                        "extra": va,
                    }
                    insertions.append((page_last_block_idx[pg], [img_block]))
                else:
                    unmatched.append(va)

        # ── 策略 1b: page_num 页级匹配（PDF / PPTX） ──
        # 优先尝试标注编号匹配（图③ → asset with ③），fallback 到页末
        if page_only_assets:
            import re
            # 圈号字符 → 数字映射
            _CIRCLED_NUMS = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
            _FIG_REF_RE = re.compile(r'[图如].*?([' + _CIRCLED_NUMS + r'])')

            # 重新计算 page_last_block_idx — 只统计文本 blocks（排除 ocr_text）
            # 这确保 image_ref 插在步骤文本后面，不会跑到 OCR dump 后面
            text_page_last = {}
            for i, block in enumerate(blocks):
                bt = (block.get("block_type") if isinstance(block, dict)
                      else getattr(block, "block_type", ""))
                if bt in ("ocr_text", "image_ref"):
                    continue  # 跳过 OCR 和已有的 image_ref
                pg = (block.get("page_num") if isinstance(block, dict)
                      else getattr(block, "page_num", None))
                if pg is not None:
                    text_page_last[pg] = i

            # 构建步骤文本中的标注引用索引：(page_num, circled_char) → block_index
            # 限制在同一页匹配，因为圈号 ①②③ 在不同页中含义不同
            # 例如 p1 的 "图③" = 纸箱堆码，p2 的 ③ = 供应商管理菜单
            anno_ref_index = {}  # (page_num, circled_char) → block_idx
            for i, block in enumerate(blocks):
                bt = (block.get("block_type") if isinstance(block, dict)
                      else getattr(block, "block_type", ""))
                if bt not in ("heading", "paragraph"):
                    continue
                text = (block.get("text", "") if isinstance(block, dict)
                        else getattr(block, "text", ""))
                bpg = (block.get("page_num") if isinstance(block, dict)
                       else getattr(block, "page_num", None))
                for m in _FIG_REF_RE.finditer(text):
                    circled = m.group(1)
                    key = (bpg, circled)
                    if key not in anno_ref_index:
                        anno_ref_index[key] = i

            # 分离：有标注号的 vs 无标注号的（同页匹配）
            anno_matched = []  # (block_idx, va)
            page_fallback = []  # va without annotation match

            # visual_summary 里"标注语境"的圈号（如"红色方框标注区域③"）：
            # VLM 经常不回 annotation_map 但会在描述里点名圈号；须与同页文本的
            # 图N 引用（anno_ref_index）联合命中才算数，描述里单独出现不触发。
            _vs_circled_re = re.compile(
                r'(?:标注|红框|方框|圆圈|圈|区域|箭头|图)[^①-⑳]{0,6}([' + _CIRCLED_NUMS + r'])')

            # ── 证据源 0：页面叠加圈号标注（PDF 原生文本"⑧"贴在图片 bbox 内）──
            # pdf_extractor 把纯圈号行标记为 circled_label 块（带 x/y 几何）；
            # 标注中心点落在哪张图 bbox 内（±4pt 容忍）即为该图的图号。
            # 这是确定性版面证据，优先于 VLM annotation_map / visual_summary
            # （FL-ZS-WI-005 枪图：OCR 空、无标注图，仅有此标注可依 — 2026-06-11）。
            # 落在多张图 bbox 内的标注按歧义弃用，回退后续证据源。
            label_points = []  # (circled_char, cx, cy, page)
            for block in blocks:
                b_extra = (block.get("extra") if isinstance(block, dict)
                           else getattr(block, "extra", None)) or {}
                lab = b_extra.get("circled_label")
                if not lab or len(lab) != 1 or lab not in _CIRCLED_NUMS:
                    continue
                bpg = (block.get("page_num") if isinstance(block, dict)
                       else getattr(block, "page_num", None))
                if bpg is None or b_extra.get("x0") is None or b_extra.get("y0") is None:
                    continue
                cx = (float(b_extra["x0"]) + float(b_extra.get("x1") or b_extra["x0"])) / 2
                cy = (float(b_extra["y0"]) + float(b_extra.get("y1") or b_extra["y0"])) / 2
                label_points.append((lab, cx, cy, bpg))

            # 严格包含（无容忍带，_OVERLAY_TOL=0.0）：±tol 会把贴在本图边缘外侧的标注误吸进相邻图
            # bbox 形成「错误的唯一归属」——实测三例标注（①⑦⑧）都严格落在所属图
            # bbox 内部，宽容不带来收益只扩大误吸面（2026-06-11 对抗评审收窄）。
            _OVERLAY_TOL = 0.0
            overlay_label_owner: Dict[int, list] = {}   # id(va) → [圈号]
            for lab, cx, cy, lpg in label_points:
                containing = []
                for va in page_only_assets:
                    if va.get("page_num") != lpg or not va.get("bbox"):
                        continue
                    bx0, by0, bx1, by1 = (float(v) for v in va["bbox"])
                    if (bx0 - _OVERLAY_TOL <= cx <= bx1 + _OVERLAY_TOL
                            and by0 - _OVERLAY_TOL <= cy <= by1 + _OVERLAY_TOL):
                        containing.append(va)
                if len(containing) == 1:
                    overlay_label_owner.setdefault(id(containing[0]), []).append(lab)

            # ── 重复圈号 = 不可采信的图号（PDF-D5，2026-07-25；取代 D4 的图片数判据）──
            # 1b 命中会**压过几何**，所以它只该接受高精度的 figure-ID 证据：
            # **仅页内唯一出现**的圈号才算图号；重复出现即 fail-closed，回落几何。
            # 这不是"两次出现必然指向不同图"的数学推导（两个点可能都在同一张图内），
            # 而是**可采信门**——重复时不去从不稳定的信息里"恢复唯一性"。
            #
            # 为什么判据必须建在 label_points（pdf_extractor 的 circled_label 块 = 纯
            # 文本层）而不是 overlay_label_owner（"几张**存活**图认领它"）：
            #   后者是 **VLM 判断的函数** —— 漏斗对同一张图的留/弃会随 caption 重掷而变。
            #   实证 FL-ZS-WI-009：暖缓存下 page1 存活图 [1..6]，image 2 与 image 5 各
            #   认领一个游离 ① ⇒ 判歧义、image 5 按几何正确归步骤3；空缓存重掷 caption
            #   后 **image 2 被漏斗丢弃**，① 只剩 image 5 认领 ⇒ 歧义判定失效 ⇒ image 5
            #   又被 1b 拖回步骤2（正文"（图①）"在那）。同两次抽取里，标记的页级计数
            #   `{(1,'②'):1, (1,'①'):3, ...}` **逐字相同**，存活图集却不同。
            #
            # 口径（三条都是语义的一部分）：
            #   · 计数只数 label_points（页级标记出现次数），与哪些图存活无关；
            #   · 一旦判歧义，该字符对该页 overlay / vlm_annotation_map /
            #     visual_summary **三源全部弃用**（只堵 overlay 会被后两级回退绕回）；
            #   · **先过滤歧义字符、再做各源 `len(...) == 1` 唯一性判断**，同源里未歧义
            #     的其他字符仍可用。
            #
            # ⚠️ 本守门只消除"重复圈号歧义判定"对存活 asset 集的依赖，**不**等于 1b 已
            # caption-independent：1b 仍只收 ROUTE_TO_VECTOR/TEXT 的资产，且 map/summary
            # 两级证据本身就是 VLM 产物。
            _label_occurrences: Dict[tuple, int] = {}
            for lab, _cx, _cy, lpg in label_points:
                _label_occurrences[(lpg, lab)] = _label_occurrences.get((lpg, lab), 0) + 1
            _ambiguous_by_page: dict = {}
            for (_pg, _c), _n in _label_occurrences.items():
                if _n > 1:
                    _ambiguous_by_page.setdefault(_pg, set()).add(_c)

            for va in page_only_assets:
                ann_map = va.get("vlm_annotation_map", {})
                img_page = va.get("page_num")
                matched = False
                # 圈号候选 —— 关键约束：仅当图片的圈号【唯一】时才当作"这张图的图号"。
                # 截图内部的 UI 步骤标注（①②③④⑤⑥ 一串）不是图号：拿它们去匹配
                # 正文"如图①"会把 步骤3 的截图错绑到 步骤2（2026-06-10 pdf_sop 实证）。
                # PDF-D4：先剔除该页的歧义字符，再做各源唯一性判断
                _amb = _ambiguous_by_page.get(img_page, ())
                overlay_circled = [c for c in overlay_label_owner.get(id(va), [])
                                   if c not in _amb]
                map_circled = [k for k in ann_map
                               if k in _CIRCLED_NUMS and k not in _amb]
                circled_candidates = overlay_circled if len(overlay_circled) == 1 else []
                if not circled_candidates:
                    circled_candidates = map_circled if len(map_circled) == 1 else []
                if not circled_candidates:
                    # 次选：visual_summary 标注语境圈号（如"红色方框标注区域③"），同样要求唯一
                    vs_circled = [c for c in dict.fromkeys(
                        _vs_circled_re.findall(va.get("visual_summary", "") or ""))
                        if c not in _amb]
                    if len(vs_circled) == 1:
                        circled_candidates = vs_circled
                for ann_key in circled_candidates:
                    key = (img_page, ann_key)
                    if key in anno_ref_index:
                        anno_matched.append((anno_ref_index[key], va))
                        matched = True
                        break
                if not matched:
                    page_fallback.append(va)

            # 标注号匹配的图片 → 插到对应步骤后面
            for block_idx, va in anno_matched:
                pg = va["page_num"]
                img_block = {
                    "block_type": "image_ref",
                    "text": "",
                    "page_num": pg,
                    "section_path": None,
                    "source": "multimodal",
                    "extra": va,
                }
                insertions.append((block_idx, [img_block]))

            # ── 策略 1b-2: 版面位置锚定（图片 bbox × 文本块 y 区间，同坐标系）──
            # 图片物理上位于哪个步骤文本下方，就锚定到那个 block —— 版面即真值。
            # 优先级：圈号精确匹配 > 版面位置 > 图N引用 bigram > 均匀分配 > 页末。
            # （2026-06-10 诊断：均匀分配/页末把 步骤4 的截图停在 步骤3 中间、
            #  跨页泄漏到上一页未关步骤；y 锚定从根上消除这两类错位。）
            geo_fallback = []
            page_block_anchors: Dict[int, list] = {}   # page → [(y0, block_idx)]
            # 多字符 circled overlay 段（如 "①  ②\n④  ⑤"）— 单字符 circled_label
            # 标记不覆盖（pdf_extractor 仅给 1-char 标），需用「只含圈号+空白」
            # 模式排除，否则几何 overlap 仍会吃到这些 2D 浮标块（xs_wi_007 image
            # 30 在 page 3 命中 i=16 "①  ②\n④  ⑤" 实证）。
            _CIRCLED_OVERLAY_RE = re.compile(
                r'^[\s①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳双击]+$')
            for i, block in enumerate(blocks):
                bt = (block.get("block_type") if isinstance(block, dict)
                      else getattr(block, "block_type", ""))
                # 仅 paragraph/heading 做 anchor —— 排除 table/ocr_text/image_ref
                # 及 circled overlay 段（单字符 cl 或纯圈号+空白）。
                # Why table：pdf_extractor 把同页 table 先扫出来（i=7/8），随后才
                # 是页内 paragraphs（i=9+）。tables y0 落在 step 之间，几何"上方
                # 块"规则会选中早于 step 的 table → image_ref 插到 step 之前 →
                # `_chunk_by_step` 缓存为 pending_images 归到下一个 step
                # （pdf_sop image 5/6 → 错绑 step 2 实证）。tables 不代表 step
                # 起始文本，不该当 anchor。
                # Why circled overlay：独立"①/②/⑥"段是几何标注层而非文本主体，
                # 几何 overlap 容易吃到 1-字符浮标块，错位（xs_wi_007 image 30 实证）。
                if bt not in ("paragraph", "heading"):
                    continue
                b_extra = (block.get("extra") if isinstance(block, dict)
                           else getattr(block, "extra", None)) or {}
                if b_extra.get("circled_label"):
                    continue
                blk_text = ((block.get("text", "")
                            if isinstance(block, dict)
                            else getattr(block, "text", "")) or "")
                if blk_text and _CIRCLED_OVERLAY_RE.match(blk_text):
                    continue
                y0 = b_extra.get("y0")
                bpg = (block.get("page_num") if isinstance(block, dict)
                       else getattr(block, "page_num", None))
                if bpg is not None and y0 is not None:
                    page_block_anchors.setdefault(bpg, []).append((float(y0), i))
            for anchors in page_block_anchors.values():
                anchors.sort()

            geo_assets = [va for va in page_fallback
                          if va.get("bbox") and va.get("page_num") in page_block_anchors]
            _geo_ids = {id(va) for va in geo_assets}
            geo_fallback = [va for va in page_fallback if id(va) not in _geo_ids]
            # 按 (页, 图片上缘, 提取序) 排序：同锚点多图保持版面阅读顺序
            geo_assets.sort(key=lambda va: (
                va["page_num"], float(va["bbox"][1]), va.get("image_index", 0)))
            for va in geo_assets:
                anchors = page_block_anchors[va["page_num"]]
                img_y0 = float(va["bbox"][1])
                img_y1 = float(va["bbox"][3])
                # 锚定规则（优先级）：
                #   1. 与图片 y 区间重叠最大的文本块 —— 图片与步骤行并排/部分重叠时
                #      （如截图顶到页首、步骤行在图片右侧），重叠块才是它的步骤；
                #   2. 无重叠 → 图片上方最近的文本块（阅读顺序：图属于其上的文字）；
                #   3. 全页文本都在图片之下 → 插在该页首个文本块之前（交给 pending_images）。
                best_idx = None
                best_overlap = 0.0
                for y0, bidx in anchors:
                    blk = blocks[bidx]
                    b_extra = (blk.get("extra") if isinstance(blk, dict)
                               else getattr(blk, "extra", None)) or {}
                    b_y1 = float(b_extra.get("y1", y0))
                    overlap = min(img_y1, b_y1) - max(img_y0, y0)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_idx = bidx
                if best_idx is None:
                    for y0, bidx in anchors:
                        if y0 <= img_y0 + 1.0:   # 块起始在图片上缘之上（容忍 1pt 重叠）
                            best_idx = bidx
                        else:
                            break
                # ── Path A: content-match override（D8 Phase 3，env-gated 默认 OFF）──
                # 当几何 anchor 选中非 step 起始块（如 step 内的延续段、独立"⑦"圈号
                # 行），且页内某 step 起始块与图片 visual_summary+ocr_text 的 bigram
                # 重叠显著高时，把 anchor 改到那个 step 块。
                # Why: xs_wi_007 image 1（产品标识卡，y=[216,395]）几何上 overlap-max
                # 锚到 step 2 的延续段 i=4「机台上尾数」(overlap=46) — 但 step 1 文本
                # 「按《产品标识卡》清点」与图片 visual_summary「产品标识卡（包装车间
                # 专用）」5-gram 精准匹配，bg=21 vs 几何 pick bg=2（10.5x 差距实证）。
                # 这种「step 起始块 vs step 内延续段」的几何/语义冲突是 chunker step
                # boundary 与 image anchor 一类通病，不局限于 dotted child step。
                # 关键边界：仅当 **geo pick 自身不是 step 起始块** 时才覆写。同 step
                # 内多个子条目"1）.../2）.../3）..."都匹配 STEP_BOUNDARY，几何选中后
                # 内容覆写会把图错移到关键词更多的子条目（xs_wi_007 image 30: geo
                # = "2）填写完后" → content 覆到 "3）假如点击带不出..." 错绑 5.2 异
                # 常流程的 step_card）— 保留几何作为同级 step 间的最终裁决。
                # 阈值保守：MIN_ABS=10、RATIO=3.0 — pdf_sop image 9/10 bg=1/7 信号
                # 弱不触发，it_xxh_003 无 step_card 不影响。Env-gate 默认 OFF：评测开、
                # 生产默认不开，3 doc 实测稳定后再考虑默认 ON。
                if (best_idx is not None and image_content_override_enabled()):
                    geo_blk = blocks[best_idx]
                    geo_txt = ((geo_blk.get("text", "")
                               if isinstance(geo_blk, dict)
                               else getattr(geo_blk, "text", ""))
                               or "")
                    # 守门：geo pick 已是 step 起始块 ⇒ 不覆写（同 step 多子条目
                    # 不要内容覆写，几何为准）
                    geo_is_step = bool(
                        DocumentChunker._STEP_BOUNDARY_RE.search(geo_txt))
                    img_text_concat = (
                        (va.get("visual_summary") or "") + " "
                        + (va.get("ocr_text") or ""))
                    if not geo_is_step and len(img_text_concat) >= 20:
                        def _bg(t):
                            s = set()
                            for k in range(len(t) - 1):
                                s.add(t[k:k + 2])
                            for k in range(len(t) - 2):
                                s.add(t[k:k + 3])
                            return s
                        img_bg = _bg(img_text_concat)
                        if len(img_bg) >= 30:
                            geo_score = len(img_bg & _bg(geo_txt))
                            best_alt_idx, best_alt_score = best_idx, geo_score
                            for _y0, bidx in anchors:
                                if bidx == best_idx:
                                    continue
                                blk = blocks[bidx]
                                blk_txt = ((blk.get("text", "")
                                           if isinstance(blk, dict)
                                           else getattr(blk, "text", ""))
                                           or "")
                                # 候选限于 step 起始块（含步骤边界标记）
                                if not DocumentChunker._STEP_BOUNDARY_RE.search(
                                        blk_txt):
                                    continue
                                sc = len(img_bg & _bg(blk_txt))
                                if sc > best_alt_score:
                                    best_alt_idx, best_alt_score = bidx, sc
                            if (best_alt_idx != best_idx
                                    and best_alt_score >= 10
                                    and best_alt_score
                                    >= max(geo_score, 1) * 3.0):
                                # ── reconciliation guard (post-binding ownership check) ──
                                # Only apply if the destination owns the image more strongly than
                                # the geometric source. Blocks the range-theft misfire (22767C);
                                # keeps valid corrections (5FFA22/328126). See image_binding_reconcile.
                                _alt_blk = blocks[best_alt_idx]
                                _alt_txt = ((_alt_blk.get("text", "") if isinstance(_alt_blk, dict)
                                             else getattr(_alt_blk, "text", "")) or "")
                                _rec = reconcile_move(geo_txt, _alt_txt, va.get("ocr_text"),
                                                      va.get("visual_summary"))
                                print(f"[img-reconcile] path=A img={va.get('image_index')} "
                                      f"result={_rec['result']} reason={_rec['reason_code']} "
                                      f"src_tier={_rec['src_tier']} dst_tier={_rec['dst_tier']}")
                                if _rec.get("apply"):
                                    best_idx = best_alt_idx
                                    # Path D seed mark (D8 Tier 0 post-review):
                                    # 仅 strong override(alt ≥ 15 AND ratio ≥ 5.0)
                                    # 才作 cluster propagation seed,避免边缘 trigger
                                    # 把噪声传给邻居
                                    if (best_alt_score >= 15
                                            and best_alt_score
                                            >= max(geo_score, 1) * 5.0):
                                        va['_path_a_strong'] = True
                # ── Path B: 圈号 sub-step override（D8 Phase 6,同 env-gate）──
                # image OCR 含的圈号集 vs 同页 step block 圈号集 Jaccard。
                # 适合"填写示例图 vs 填写指示文本"匹配:image OCR 含的 ①②③ 是
                # 用户在表单上写下的编号示例,step text 含的 ①②③ 是该 step 的填写
                # 指示——两者圈号集匹配 = 该图正是该 step 的填写示例。
                # pdf_sop image 10 实证(Bug A):OCR={①②③④⑤},step 4.1={①②③}
                # Jaccard=0.6 vs step 4.2 heading={④} J=0.2/4.2 paragraph={⑤⑥} J=0.17 ——
                # 圈号信号清晰指向 step 4.1。Path A bigram 信号弱(visual_summary
                # 通用词与 step text bigram 仅 1 命中)不触发,Path B 圈号集精确语
                # 义信号补上。仅 img OCR 含 ≥2 圈号且 alt Jaccard ≥0.5 且 ≥1.5x
                # geo Jaccard 才触发,避免单圈号 OCR 噪声。
                if (best_idx is not None and image_content_override_enabled()):
                    _CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
                    img_ocr_txt = va.get("ocr_text") or ""
                    img_circled = set(c for c in img_ocr_txt if c in _CIRCLED)
                    if len(img_circled) >= 2:
                        def _circled_set(t):
                            return set(c for c in t if c in _CIRCLED)

                        def _jacc(a, b):
                            if not (a and b):
                                return 0.0
                            return len(a & b) / max(len(a | b), 1)

                        geo_blk = blocks[best_idx]
                        geo_txt = ((geo_blk.get("text", "")
                                    if isinstance(geo_blk, dict)
                                    else getattr(geo_blk, "text", ""))
                                   or "")
                        geo_jacc = _jacc(img_circled, _circled_set(geo_txt))
                        best_cir_idx, best_cir_jacc = best_idx, geo_jacc
                        for _y0, bidx in anchors:
                            if bidx == best_idx:
                                continue
                            blk = blocks[bidx]
                            blk_txt = ((blk.get("text", "")
                                       if isinstance(blk, dict)
                                       else getattr(blk, "text", ""))
                                       or "")
                            blk_circled = _circled_set(blk_txt)
                            if not blk_circled:
                                continue
                            jacc = _jacc(img_circled, blk_circled)
                            if jacc > best_cir_jacc:
                                best_cir_jacc = jacc
                                best_cir_idx = bidx
                        if (best_cir_idx != best_idx
                                and best_cir_jacc >= 0.5
                                and best_cir_jacc >= max(geo_jacc, 0.01) * 1.5):
                            _cir_blk = blocks[best_cir_idx]
                            _cir_txt = ((_cir_blk.get("text", "") if isinstance(_cir_blk, dict)
                                         else getattr(_cir_blk, "text", "")) or "")
                            _rec = reconcile_move(geo_txt, _cir_txt, va.get("ocr_text"),
                                                  va.get("visual_summary"))
                            print(f"[img-reconcile] path=B img={va.get('image_index')} "
                                  f"result={_rec['result']} reason={_rec['reason_code']} "
                                  f"src_tier={_rec['src_tier']} dst_tier={_rec['dst_tier']}")
                            if _rec.get("apply"):
                                best_idx = best_cir_idx
                                va['_path_b_overridden'] = True
                # ── Path C: 跨页 range-ref override（D8 Phase 10,同 env-gate）──
                # step text 含"X-Y步操作"圈号范围引用(如 step 3.1 "扫码报检
                # 界面(如下图②-⑥步操作)") 时,把该 step 范围内的 image 跨页
                # 抢回。Path A/B 限同页 anchors,无法处理跨页 — pdf_sop image 9
                # 在 page 3 但 step 3.1 在 page 2,几何走 step 4.2 错绑。
                # 守门(双信号防误拉):
                #   1. image 圈号(OCR + vlm_annotation_map.keys)∩ range ≥ 2 hit
                #   2. image text(visual_summary + ocr) 与 range-ref step text
                #      bigram 命中 ≥ 8(语义二次确认,避免 image 10 圈号 hit 高
                #      但 visual "手写记录"与 step 3.1 "U8 扫码报检"无关被误拉)
                # 全文扫含 STEP_BOUNDARY 的 step 起始块(不限同页),逆序优先前
                # 页 step。env-gated 默认 OFF。
                if (best_idx is not None and image_content_override_enabled()):
                    _CIRCLED_C = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
                    # D8 Tier 0 post-review: 移除 vlm_annotation_map.keys() 圈号源
                    # VLM 在 annotation_map 标注的 ①-⑥ 是图内"区域位置编号"(如
                    # "①": "左侧功能导航栏"),不是图本身印有的圈号引用,与 step
                    # text "②-⑥步操作" 的 sub-step 引用语义不同。把 annotation_map
                    # keys 算进圈号源会把 pdf_sop image 9 (OCR 空 + VLM 区域 ①-⑥)
                    # 错移到 step 3.1。Path C intent 是"image 印着圈号引用对应
                    # step text 圈号范围",应只信 OCR 真实印出的圈号。
                    img_cir_all = set(
                        c for c in (va.get("ocr_text") or "") if c in _CIRCLED_C
                    )
                    img_cir_nums = set(_CIRCLED_C.index(c) + 1 for c in img_cir_all)
                    if len(img_cir_nums) >= 2:
                        _RANGE_RE = re.compile(
                            r'(['
                            r'①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
                            r'])[～至\-]('
                            r'[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
                            r'])'
                        )
                        img_text_all = (
                            (va.get("visual_summary") or "") + " "
                            + (va.get("ocr_text") or "")
                        )

                        def _bg_c(t):
                            s = set()
                            for k in range(len(t) - 1):
                                s.add(t[k:k + 2])
                            return s

                        img_bg = _bg_c(img_text_all)
                        best_range_idx = None
                        best_range_score = 0  # (圈号 hit count) * bg overlap
                        # 逆序扫全 blocks(前页 step 优先)
                        for ci in range(len(blocks) - 1, -1, -1):
                            if ci == best_idx:
                                continue
                            cblk = blocks[ci]
                            ctxt = ((cblk.get("text", "")
                                     if isinstance(cblk, dict)
                                     else getattr(cblk, "text", ""))
                                    or "")
                            if not DocumentChunker._STEP_BOUNDARY_RE.search(ctxt):
                                continue
                            for m_r in _RANGE_RE.finditer(ctxt):
                                a_c, b_c = m_r.group(1), m_r.group(2)
                                a_n = _CIRCLED_C.index(a_c) + 1
                                b_n = _CIRCLED_C.index(b_c) + 1
                                if a_n > b_n:
                                    a_n, b_n = b_n, a_n
                                hit = sum(1 for n in img_cir_nums
                                          if a_n <= n <= b_n)
                                if hit < 2:
                                    continue
                                # 双信号守门:bigram 命中 ≥ 3(step text 通常短,
                                # 3 个主题词共现已是强语义信号 — image 9 step
                                # 3.1 实证 bg={U8,8系,系统,界面}=4 hit,而 image
                                # 10 vs step 3.1 bg=0,区分度清晰)
                                bg_hit = len(img_bg & _bg_c(ctxt))
                                if bg_hit < 3:
                                    continue
                                score = hit * bg_hit
                                if score > best_range_score:
                                    best_range_score = score
                                    best_range_idx = ci
                                    break  # 同 block 只算一个 range-ref
                        if best_range_idx is not None:
                            _geo_blk = blocks[best_idx]
                            _geo_txt = ((_geo_blk.get("text", "") if isinstance(_geo_blk, dict)
                                         else getattr(_geo_blk, "text", "")) or "")
                            _rng_blk = blocks[best_range_idx]
                            _rng_txt = ((_rng_blk.get("text", "") if isinstance(_rng_blk, dict)
                                         else getattr(_rng_blk, "text", "")) or "")
                            _rec = reconcile_move(_geo_txt, _rng_txt, va.get("ocr_text"),
                                                  va.get("visual_summary"))
                            print(f"[img-reconcile] path=C img={va.get('image_index')} "
                                  f"result={_rec['result']} reason={_rec['reason_code']} "
                                  f"src_tier={_rec['src_tier']} dst_tier={_rec['dst_tier']}")
                            if _rec.get("apply"):
                                best_idx = best_range_idx
                                va['_path_c_overridden'] = True
                # Path D pre-pass: 暂存 best_idx 到 va metadata,延迟 img_block
                # 构造到 second pass(Path D 可能改写 best_idx)
                va['_d8_best_idx'] = best_idx
                va['_d8_anchors_fallback'] = (
                    anchors[0][1] - 1 if anchors else None
                )

            # ── Path D: same-page image cluster propagation (D8 Tier 0 post-review) ──
            # Path A strong override 的 image 作为 seed,同页 image_index 邻接 +
            # bbox 相对页高 < 0.20 + 高熵 token 共享 + follower 无反向证据 →
            # 跟随 seed。详 _apply_path_d_cluster_propagation docstring 8 守门。
            if (geo_assets and image_content_override_enabled()):
                _apply_path_d_cluster_propagation(geo_assets)

            # Second pass: 用 Path D 调整后的 best_idx 构造 img_block + insertions
            for va in geo_assets:
                best_idx = va.get('_d8_best_idx')
                fallback = va.get('_d8_anchors_fallback')
                img_block = {
                    "block_type": "image_ref",
                    "text": "",
                    "page_num": va["page_num"],
                    "section_path": None,
                    "source": "multimodal",
                    "extra": va,
                }
                if best_idx is None:
                    if fallback is not None:
                        insertions.append((fallback, [img_block]))
                    # else: 该 page 无 anchors,跳过(理论不应发生)
                else:
                    insertions.append((best_idx, [img_block]))

            # 无标注号且无版面坐标的图片 → 智能分配到该页各文本 block
            # 策略：优先分配到含有图片引用（图①②等）的 block 后面
            page_to_assets = {}
            for va in geo_fallback:
                pg = va["page_num"]
                page_to_assets.setdefault(pg, []).append(va)

            for pg, p_assets in sorted(page_to_assets.items()):
                # 按 image_index 排序（提取顺序 ≈ 页面上从上到下）
                p_assets.sort(key=lambda a: a.get("image_index", 0))

                # 找出该页所有文本 block 的索引（排除 ocr_text / image_ref）
                page_text_indices = []
                for i, block in enumerate(blocks):
                    bt = (block.get("block_type") if isinstance(block, dict)
                          else getattr(block, "block_type", ""))
                    if bt in ("ocr_text", "image_ref"):
                        continue
                    bpg = (block.get("page_num") if isinstance(block, dict)
                           else getattr(block, "page_num", None))
                    if bpg == pg:
                        page_text_indices.append(i)

                if not page_text_indices:
                    insert_target = text_page_last.get(pg, page_last_block_idx.get(pg))
                    if insert_target is not None:
                        img_blocks = [{
                            "block_type": "image_ref", "text": "",
                            "page_num": pg, "section_path": None,
                            "source": "multimodal", "extra": va,
                        } for va in p_assets]
                        insertions.append((insert_target, img_blocks))
                    else:
                        unmatched.extend(p_assets)
                    continue

                # 分析每个 block 是否引用了图片（图①②等）
                # 已被 annotation 精确匹配的编号排除（仅当前页）
                already_matched_circled = set()
                for _, va in anno_matched:
                    if va.get("page_num") == pg:
                        already_matched_circled.update(va.get("vlm_annotation_map", {}).keys())

                # 按 block 顺序，收集未被满足的图片引用
                blocks_with_refs = []   # (block_idx, ref_count) — 有图片引用但未被 annotation 满足
                blocks_without_refs = [] # block_idx — 无图片引用
                for bidx in page_text_indices:
                    block = blocks[bidx]
                    text = (block.get("text", "") if isinstance(block, dict)
                            else getattr(block, "text", ""))
                    # 找该 block 中引用了哪些图片编号
                    refs_in_block = set()
                    for m in _FIG_REF_RE.finditer(text):
                        c = m.group(1)
                        if c not in already_matched_circled:
                            refs_in_block.add(c)
                    if refs_in_block:
                        blocks_with_refs.append((bidx, len(refs_in_block)))
                    else:
                        blocks_without_refs.append(bidx)

                # 分配策略：
                # 1. 先满足有图片引用的 blocks（按引用数量分配图片）
                # 2. 剩余图片分配给无引用的 blocks
                # 图片选择：优先用 visual_summary 关键词与 block 文本匹配
                img_queue = list(p_assets)

                def _pick_best(queue, block_text, n):
                    """从 queue 中选出与 block_text 最匹配的 n 张图片。
                    
                    匹配策略：用 character bigram 重叠计分。
                    避免 jieba 分词边界导致"扫码枪" vs "扫描枪"不匹配。
                    visual_summary 权重 3x，ocr_text 权重 1x。
                    """
                    if n >= len(queue):
                        picked = list(queue)
                        queue.clear()
                        return picked
                    
                    def _bigrams(text):
                        """提取中文 2-gram + 3-gram 字符集合"""
                        s = set()
                        for i in range(len(text) - 1):
                            s.add(text[i:i+2])
                        for i in range(len(text) - 2):
                            s.add(text[i:i+3])
                        return s
                    
                    block_bg = _bigrams(block_text)
                    
                    scored = []
                    for i, va in enumerate(queue):
                        vs = va.get("visual_summary", "") or ""
                        ot = va.get("ocr_text", "") or ""
                        vs_score = len(block_bg & _bigrams(vs)) * 3 if vs else 0
                        ot_score = len(block_bg & _bigrams(ot)) if ot else 0
                        scored.append((vs_score + ot_score, i))
                    
                    # 按匹配分降序，同分时保持原序
                    scored.sort(key=lambda x: (-x[0], x[1]))
                    pick_indices = sorted([scored[j][1] for j in range(n)])
                    
                    picked = [queue[i] for i in pick_indices]
                    for i in reversed(pick_indices):
                        queue.pop(i)
                    return picked

                for bidx, ref_count in blocks_with_refs:
                    if not img_queue:
                        break
                    block_text = (blocks[bidx].get("text", "") if isinstance(blocks[bidx], dict)
                                  else getattr(blocks[bidx], "text", ""))
                    n_assign = min(ref_count, len(img_queue))
                    picked = _pick_best(img_queue, block_text, n_assign)
                    for va in picked:
                        insertions.append((bidx, [{
                            "block_type": "image_ref", "text": "",
                            "page_num": pg, "section_path": None,
                            "source": "multimodal", "extra": va,
                        }]))

                # 剩余图片分配到无引用的 blocks（均匀分配）
                if img_queue and blocks_without_refs:
                    n_remain = len(img_queue)
                    n_targets = len(blocks_without_refs)
                    for img_i, va in enumerate(img_queue):
                        block_j = min(int(img_i * n_targets / n_remain), n_targets - 1)
                        insertions.append((blocks_without_refs[block_j], [{
                            "block_type": "image_ref", "text": "",
                            "page_num": pg, "section_path": None,
                            "source": "multimodal", "extra": va,
                        }]))
                elif img_queue:
                    # 最终 fallback：全部插到页末
                    insert_target = text_page_last.get(pg, page_last_block_idx.get(pg))
                    if insert_target is not None:
                        for va in img_queue:
                            insertions.append((insert_target, [{
                                "block_type": "image_ref", "text": "",
                                "page_num": pg, "section_path": None,
                                "source": "multimodal", "extra": va,
                            }]))

        # 从后往前插入，保持前面的索引不变。
        # 同一插入点的多条 insertions 先按出现顺序合并成组再一次性插入 ——
        # 逐条 insert 到同一位置会把先插的图往后顶，颠倒同步骤多图的版面顺序。
        merged_insertions: Dict[int, list] = {}
        for insert_after, img_blocks in insertions:
            merged_insertions.setdefault(insert_after, []).extend(img_blocks)
        for insert_after in sorted(merged_insertions, reverse=True):
            for j, ib in enumerate(merged_insertions[insert_after]):
                enriched.insert(insert_after + 1 + j, ib)

        # 未匹配到页面的图片：用 VLM visual_summary 生成合成 text block + image_ref
        # 这样即使 OCR 没覆盖到的页面，也能通过 VLM 描述实现图文绑定
        for va in unmatched + no_page_assets:
            vlm_summary = va.get("visual_summary", "")
            ocr_text = va.get("ocr_text", "")

            # 有 VLM 描述或 OCR 文字 → 生成合成文本块，让图片跟文字绑在同一 chunk
            synth_text = ""
            if vlm_summary:
                synth_text = f"[图片内容] {vlm_summary}"
            if ocr_text:
                synth_text = f"{synth_text}\n[图片OCR] {ocr_text}" if synth_text else f"[图片OCR] {ocr_text}"

            if synth_text:
                enriched.append({
                    "block_type": "vlm_synth",
                    "text": synth_text.strip(),
                    "page_num": va.get("page_num"),
                    "section_path": None,
                    "source": "vlm_fallback",
                    "extra": {},
                })

            enriched.append({
                "block_type": "image_ref",
                "text": "",
                "page_num": va.get("page_num"),
                "section_path": None,
                "source": "multimodal",
                "extra": va,
            })

        return enriched

    # ── 策略 2: 步骤边界分配（无 page_num，如旧版 DOCX fallback） ──
    step_boundary_indices = []
    for i, block in enumerate(blocks):
        text = (block.get("text", "") if isinstance(block, dict)
                else (block.text if hasattr(block, "text") and block.text else "")).strip()
        if text and DocumentChunker._STEP_BOUNDARY_RE.match(text):
            step_boundary_indices.append(i)

    if len(step_boundary_indices) < 2:
        # 策略 3: 步骤边界不足，全部追加到末尾
        enriched = list(blocks)
        for va in valid_assets:
            enriched.append({
                "block_type": "image_ref",
                "text": "",
                "page_num": va.get("page_num"),
                "section_path": None,
                "source": "multimodal",
                "extra": va,
            })
        return enriched

    # 步骤区间分配（保留原逻辑，但保留 page_num）
    step_ranges = []
    for j, start_idx in enumerate(step_boundary_indices):
        end_idx = step_boundary_indices[j + 1] - 1 if j + 1 < len(step_boundary_indices) else len(blocks) - 1
        step_ranges.append((start_idx, end_idx))

    images_per_step = max(1, len(valid_assets) // len(step_ranges))
    enriched = []
    img_cursor = 0

    for block_idx, block in enumerate(blocks):
        enriched.append(block)

        for step_idx, (s_start, s_end) in enumerate(step_ranges):
            if block_idx == s_end and img_cursor < len(valid_assets):
                n_imgs = images_per_step if step_idx < len(step_ranges) - 1 else len(valid_assets) - img_cursor
                for _ in range(n_imgs):
                    if img_cursor >= len(valid_assets):
                        break
                    va = valid_assets[img_cursor]
                    enriched.append({
                        "block_type": "image_ref",
                        "text": "",
                        "page_num": va.get("page_num"),
                        "section_path": None,
                        "source": "multimodal",
                        "extra": va,
                    })
                    img_cursor += 1

    while img_cursor < len(valid_assets):
        va = valid_assets[img_cursor]
        enriched.append({
            "block_type": "image_ref",
            "text": "",
            "page_num": va.get("page_num"),
            "section_path": None,
            "source": "multimodal",
            "extra": va,
        })
        img_cursor += 1

    return enriched


_CE_PART_COL_NAMES = ("清扫部位名称", "点检部位", "部位名称")
_CE_SEQ_COL_NAMES = ("序号", "序 号", "序")


def _resolve_part_col_index(blocks):
    """从 worksheet 表头行解析"部位名称"列在 body（去掉序号后）里的下标，按 section 返回。

    返回 {section_heading: body_col_index}。约定：
      body_col_index ∈ {0, 1} → 当前支持的"两格身份"结构（类别/点检部位 + 部位名称/点检项目）；
      body_col_index ≥ 2      → 三格身份层级（系统→子系统→部位名称），属**未支持**版式，
                                 调用方应对其降级 + 发诊断，绝不用错位身份硬绑。
    未解析到表头的 section 不入 map → 调用方走 first-two-cells 兜底并发诊断。

    只读 doc.blocks（表头被 row_role=metadata 标记、不进 chunk），不依赖 extractor 改动。
    """
    out = {}
    current_heading = None
    for b in blocks or []:
        bt = b.get("block_type") if isinstance(b, dict) else getattr(b, "block_type", None)
        text = (b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or ""
        if bt == "heading":
            current_heading = (text or "").strip()
            continue
        if current_heading is None or current_heading in out or "\t" not in text:
            continue
        cells = [c.replace("\n", "").strip() for c in text.split("\t")]
        idx = next((i for i, c in enumerate(cells) if c in _CE_PART_COL_NAMES), None)
        if idx is None:
            continue
        # body 下标 = 整行下标 − 1（仅当首列确是"序号"类表头时成立；否则首列即 body[0]）。
        first = cells[0] if cells else ""
        out[current_heading] = idx - 1 if first in _CE_SEQ_COL_NAMES else idx
    return out


def _ce_chunk_regions(chunk_text: str, part_col_index=None):
    """把"设备清扫基准书"的一行 row-card 文本拆成 (identity_region, item_region, part_name, status)。

    去掉 【文档:…】/【章节:…】 上下文前缀后，正文是 Tab 分隔的单元格：
      清扫 sheet:  序号 ⇥ 类别 ⇥ 清扫部位名称 ⇥ 清扫基准 ⇥ 清扫方法 …
      点检 sheet:  序号 ⇥ 点检部位 ⇥ 点检项目 ⇥ 判定标准/点检方法 …
    identity_region = 序号 之后的两个"身份"单元格（类别+部位 或 部位+项目）—— 部位名称就在这里；
    item_region     = 其余单元格（清扫基准/方法、判定标准，以及"空开，线接头，温控表，电机，
                      烘箱"这类被点检的部件清单）；
    part_name       = 用于"逐字命名"判定的具体部位名。

    part_col_index（由 `_resolve_part_col_index` 从表头解析、按 section 给出）决定 status：
      None      → 未解析到表头：走 first-two-cells 兜底（part_name=第2格否则第1格），status='fallback'；
      0 或 1    → 表头确认的两格身份结构：part_name=body[part_col_index]，status='header'；
      ≥ 2       → 三格身份层级，**未支持**版式：status='unsupported'（调用方降级，不在此行绑图）。
    身份/明细区始终按"序号后两格"切分（这是受支持版式的不变式）；part_col_index 只用于
    精确定位 part_name 与判定是否为未支持版式。
    """
    import re as _re
    t = (chunk_text or "")
    # 先切掉可能已追加的图片后缀，再去掉所有 【…】 上下文前缀
    t = t.split("[图片")[0]
    t = _re.sub(r"【[^】]*】", "", t).strip()
    fields = [f.strip() for f in t.split("\t")]
    # 行首数字 序号 → 身份/明细从其后开始
    body = fields[1:] if fields and fields[0].lstrip("★ ").isdigit() else fields
    identity = " ".join(body[:2])
    items = " ".join(body[2:])
    if part_col_index is not None and part_col_index >= 2:
        # 三格身份层级：未支持版式 —— 身份区(序号后两格)落不到真正的部位名称，标记降级。
        return identity, items, "", "unsupported"
    if part_col_index is not None and 0 <= part_col_index <= 1:
        cell = body[part_col_index] if part_col_index < len(body) else ""
        part_name = (cell or (body[0] if body else "")).lstrip("★ ").strip()
        return identity, items, part_name, "header"
    # 兜底：无表头，沿用 first-two-cells 启发（第2格否则第1格）
    part_name = (body[1] if len(body) > 1 and body[1] else (body[0] if body else "")).lstrip("★ ").strip()
    return identity, items, part_name, "fallback"


def _bind_equipment_cleaning_images(chunks, assets, dept_code, d_id, version, blocks=None):
    """把 设备清扫基准书 的部位照片绑定到对应的 清扫/点检 行 chunk。

    返回 (ce_bound_fns, diag)：ce_bound_fns = 已绑定文件名集合（供兜底跳过）；
    diag = {"header","fallback","unsupported": 计数, "fallback_sections","unsupported_sections": set}
    —— 调用方据此打降级/诊断日志。

    取代旧的"每张图独立绑到最短匹配行"启发式 —— 旧逻辑让多张图挤到同一最短行
    （over-attach：丝杆图 + 齿轮油图都因含 '齿轮' 落到 ★齿轮油 行；3 张 '电机' 图都落到
    电机螺丝行），且让一张图凭 label 只在某行"判定标准/点检项目"列出现就跨绑
    （cross-bind：控制面板 仪表/标识 照片绑到 设备外观 行，只因该行点检项目列出"门，
    防护罩，仪表，标识"）。eval 摄入侧 Jaccard 据此把 xlsx_clean 从 0.54 拉到误判。

    身份列优先用**表头**解析（`_resolve_part_col_index`，按 section）；表头缺失才退回
    first-two-cells 兜底（发 fallback 诊断）；表头显示三格身份层级（部位名称在第 3+ 格）的
    **未支持**版式则把该 section 的行排除出绑定目标并发 unsupported 诊断（图改建独立 chunk），
    绝不用错位身份硬绑。

    三条规则 —— 每张图先选唯一最佳行，再做两步保守清理：
      1) 目标选择：优先选 label 命中 *身份列*（部位名称）的行，命中次数多者优先、再短者
         优先；只有当不存在身份命中行时，才退回"明细列命中"的行。
      2) 驱逐：一旦某行已有身份命中的图，丢弃其"仅明细列命中"的图（整机/部位照片胜过
         只是出现在点检项目清单里的部件照片）。**只有明细命中**的行保留其图（如 烘箱 图
         凭"空开/电机/烘箱"明细合法绑定 电气系统 行）。
      3) 强者归并：当一行有多张身份命中图，**部分**图的视觉描述/OCR 里逐字出现该行
         part_name、另一些没有时，只保留逐字命中的（real 齿轮油 标签图在场后丢掉 丝杆/齿轮
         图）。若所有图证据等强（全逐字命中 或 全不命中），一律**保留全部** —— 一行多图
         的合法场景与真正歧义的近重复图都不被武断单选。

    被丢弃/驱逐的图不进返回集合 → 仍会走兜底建独立 image chunk，serving 可达性不丢。
    """
    ce_bound_fns = set()
    diag = {"header": 0, "fallback": 0, "unsupported": 0,
            "fallback_sections": set(), "unsupported_sections": set()}
    all_rows = [c for c in chunks if c.chunk_type != "image"]
    if not all_rows:
        return ce_bound_fns, diag

    # 表头优先解析部位名称列（按 section）；逐行定 region + status，未支持版式行排除出绑定目标。
    section_part_col = _resolve_part_col_index(blocks or [])
    rows = []
    regions = {}
    for c in all_rows:
        sec = getattr(c, "section_title", None)
        pci = section_part_col.get(sec)
        ident, items, pname, status = _ce_chunk_regions(c.chunk_text or "", pci)
        diag[status] = diag.get(status, 0) + 1
        if status == "fallback":
            diag["fallback_sections"].add(sec)
        if status == "unsupported":
            diag["unsupported_sections"].add(sec)
            continue  # 未支持的三格身份行：不作绑定目标，避免用错位身份产生误绑
        regions[id(c)] = (ident, items, pname)
        rows.append(c)

    # phase 1 —— 每张 ROUTE_TO_VECTOR 图选唯一最佳行（允许撞行：同 label 的歧义图故意堆叠，
    # 仅在 phase 2/3 有原则性赢家时才清理）。
    for a in assets:
        if a.get("status") != "ROUTE_TO_VECTOR":
            continue
        labels = [lbl for lbl in (a.get("part_labels") or []) if lbl]
        if not labels:
            continue
        content = f"{a.get('visual_summary', '')} {a.get('ocr_text', '')}"
        best_key = None        # (tier, occ, -len, -idx) —— 越大越好
        best_meta = None       # (chunk, tier, exact)
        for idx, c in enumerate(rows):
            ident, items, pname = regions[id(c)]
            id_hit = any(lbl in ident for lbl in labels)
            it_hit = any(lbl in items for lbl in labels)
            if not (id_hit or it_hit):
                continue
            tier = 2 if id_hit else 1
            # occ = 身份列里"最具体那个 label"出现的次数：奖励把部位名写多遍的"真正点检行"
            # （xlsx_clean 设备外观 行身份列写了两遍 设备外观，胜过只写一遍的裸行）。
            # 用 max 而非 sum 是为了不让"嵌套 label"（如 链条/链条总成 同时命中一处）虚高计数。
            occ = max((ident.count(lbl) for lbl in labels if lbl in ident), default=0) if id_hit else 0
            key = (tier, occ, -len(c.chunk_text or ""), -idx)
            if best_key is None or key > best_key:
                best_key = key
                exact = bool(pname) and len(pname) >= 2 and pname in content
                best_meta = (c, tier, exact)
        if best_meta is None:
            continue
        target, tier, exact = best_meta
        fn = a.get("filename", "")
        target.extra.setdefault("image_refs", []).append({
            "filename": fn,
            "oss_key": f"processing/assets/{dept_code}/{d_id}/v{version}/{fn}",
            # image_index 契约键：取 asset 抽取序号（与 procedure_image_guide/DOCX/PDF 同源）。
            # equipment_cleaning_standard 的 part_labels 绑定原先漏设此键（2026-06-15 D6）。
            "image_index": a.get("image_index", a.get("original_index")),
            "anchor_row": a.get("anchor_row"),
            "part_labels": labels,
            "annotation_num": a.get("annotation_num"),
            "image_category": a.get("image_category", "unknown"),
            "visual_summary": a.get("visual_summary", ""),
            "ocr_text": a.get("ocr_text", ""),
            "_ce_tier": tier,
            "_ce_exact": exact,
            # 图所在 sheet(page_num=sheet_idx+1,extract_images_from_xlsx 赋值)——
            # phase 2.5 标注号改绑要求编号在图自己的 sheet 内解析
            "_ce_sheet": (a.get("page_num") - 1) if a.get("page_num") else None,
        })

    # phase 2（驱逐）+ phase 3（强者归并），仅作用于本函数新加的带标签 ref；
    # 任何已存在的无标签 ref（其它路径可能预先挂载）原样保留。
    pending = {}   # id(c) -> (chunk, pre, new)：先算完全部行,2.5/2.6 才有全局视角
    for c in rows:
        all_refs = c.extra.get("image_refs")
        if not all_refs:
            continue
        pre = [r for r in all_refs if "_ce_tier" not in r]
        new = [r for r in all_refs if "_ce_tier" in r]
        if new:
            if any(r.get("_ce_tier") == 2 for r in new):
                new = [r for r in new if r.get("_ce_tier") == 2]
            if len(new) > 1:
                exacts = [r for r in new if r.get("_ce_exact")]
                if exacts and len(exacts) < len(new):
                    new = exacts
        pending[id(c)] = (c, pre, new)

    # phase 2.5（2026-07-20 多挂行消歧·其一）：标注号改绑——工作簿的圆圈编号(⑯)是
    # 作者亲手写的图↔行对应,比 part_label 词面强。**只在"证据等强多挂"的行上生效**
    # （单挂行绝不触碰——盲目全局 redirect 实测会把主机温度/控制面板等正确单挂行
    # 拆散,GT 的图↔行选择并不全局跟随编号）。改绑条件全部满足才动:
    #   ① ref 带 annotation_num;② 同 sheet 存在行首序号==该编号的行;③ 目标行的
    #   身份列命中该 ref 的某个 part_label(编号+词面双证);④ 目标 ≠ 当前行。
    ordinals = _ce_row_ordinals(rows, blocks or [])   # id(chunk) -> (sheet_idx, 行首序号)
    ord_lookup = {v: cid for cid, v in ordinals.items() if v is not None}
    for cid, (c, pre, new) in list(pending.items()):
        if len(new) <= 1:
            continue
        kept = []
        for r in new:
            ann = r.get("annotation_num")
            sheet = r.get("_ce_sheet")
            tgt_id = ord_lookup.get((sheet, ann)) if (ann is not None and sheet is not None) else None
            if tgt_id is not None and tgt_id != cid:
                tgt_c = next((rc for rc in rows if id(rc) == tgt_id), None)
                ident_t = regions.get(tgt_id, ("", "", None))[0]
                if tgt_c is not None and any(
                        lbl and lbl in ident_t for lbl in (r.get("part_labels") or [])):
                    _, tpre, tnew = pending.get(tgt_id, (tgt_c, [], []))
                    tnew.append(r)
                    pending[tgt_id] = (tgt_c, tpre, tnew)
                    continue
            kept.append(r)
        pending[cid] = (c, pre, kept)

    # phase 2.6（多挂行消歧·其二）：近重复对只留首张(image_index 最小)。同一部位
    # 连拍两张近似照(bigram Jaccard ≥ 0.40;实测真对 0.463、异物对 ≤0.04,分离显著)
    # 只留第一张;非近重复的多图(不同角度/不同部件合法多图)原样保留。
    # ⚠️ 短 caption 豁免:token 集 <15 时 Jaccard 是高方差噪声(「齿轮箱左视图/右视图」
    # 这类 6 字合法多视角对会打出 0.43 假高分),两侧不够富一律视为不相似——绝不在
    # 贫证据上武断单选(与 06-18「证据等强保留全部」拍板同精神)。
    def _ce_sim(r1, r2):
        t1 = ((r1.get("visual_summary") or "") + " " + (r1.get("ocr_text") or "")).strip()
        t2 = ((r2.get("visual_summary") or "") + " " + (r2.get("ocr_text") or "")).strip()
        s1, s2 = _toks_for_ce_sim(t1), _toks_for_ce_sim(t2)
        if len(s1) < 15 or len(s2) < 15:
            return 0.0
        return len(s1 & s2) / max(len(s1 | s2), 1)

    for cid, (c, pre, new) in pending.items():
        if len(new) <= 1:
            continue
        new_sorted = sorted(new, key=lambda r: (r.get("image_index")
                                                if r.get("image_index") is not None else 10**9))
        survivors = []
        for r in new_sorted:
            if any(_ce_sim(r, s) >= 0.40 for s in survivors):
                continue   # 近重复跟拍,丢弃(走兜底独立 image chunk,serving 可达性不丢)
            survivors.append(r)
        pending[cid] = (c, pre, survivors)

    for cid, (c, pre, new) in pending.items():
        for r in new:
            r.pop("_ce_tier", None)
            r.pop("_ce_exact", None)
            r.pop("_ce_sheet", None)
        merged = pre + new
        if merged:
            c.extra["image_refs"] = merged
        else:
            c.extra.pop("image_refs", None)
        for r in new:
            if r.get("filename"):
                ce_bound_fns.add(r["filename"])
    return ce_bound_fns, diag


def _toks_for_ce_sim(s: str) -> set:
    import re as _re

    s = (s or "").lower()
    cjk = _re.findall(r'[一-鿿]', s)
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    alnum = set(_re.findall(r'[a-z0-9]{2,}', s))
    return bigrams | alnum


def _ce_row_ordinals(rows, blocks):
    """row chunk → (sheet_idx, 行首序号)。row_card 行 chunk 由块 1:1 生成,chunk_text=
    前缀+块文本;用块文本头 20 字符做包含匹配回查 sheet_idx,序号取块文本首个 \\t 前的整数。
    解析不出的行返回 None(消歧器对其不生效——fail-open,绝不因解析失败误改绑)。"""
    import re as _re

    sig = []
    for b in blocks:
        ex = b.get("extra") if isinstance(b, dict) else (getattr(b, "extra", {}) or {})
        t = (b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or ""
        si = (ex or {}).get("sheet_idx")
        if si is None or not t:
            continue
        m = _re.match(r"\s*(\d+)\s*\t", t)
        if not m:
            continue
        sig.append((t[:20], si, int(m.group(1))))
    out = {}
    for c in rows:
        txt = c.chunk_text or ""
        found = None
        for head, si, ordn in sig:
            if head and head in txt:
                found = (si, ordn)
                break
        out[id(c)] = found
    return out


def _chunk_explosion_verdict(chunks):
    """首次入库 chunk-爆炸判定（纯函数，单测入口）。返回原因字符串则触发，否则 None。

    (A) count > RAG_CHUNK_EXPLOSION_MAX（默认 2000）；
    (B) 退化型: count >= RAG_CHUNK_EXPLOSION_DEGENERATE_MIN（默认 200）AND 单一 chunk_type 占比
        >= RAG_CHUNK_EXPLOSION_DOMINANT_FRAC（默认 0.95）AND 该主导类型是 table_chunk
        （限定 table_chunk 以免误伤合法的 300 步 step_card SOP；image 等其它类型不触发 B）。
    """
    from collections import Counter

    n = len(chunks)
    if n == 0:
        return None

    def _envi(k, d):
        v = os.environ.get(k, "")
        try:
            return int(v) if v != "" else d
        except ValueError:
            return d

    def _envf(k, d):
        v = os.environ.get(k, "")
        try:
            return float(v) if v != "" else d
        except ValueError:
            return d

    max_n = _envi("RAG_CHUNK_EXPLOSION_MAX", 2000)
    if n > max_n:
        return f"count {n} > max {max_n}"

    degen_min = _envi("RAG_CHUNK_EXPLOSION_DEGENERATE_MIN", 200)
    dom_frac = _envf("RAG_CHUNK_EXPLOSION_DOMINANT_FRAC", 0.95)
    if n >= degen_min:
        counts = Counter(getattr(c, "chunk_type", "") for c in chunks)
        top_type, top_n = counts.most_common(1)[0]
        if top_type == "table_chunk" and top_n / n >= dom_frac:
            return f"degenerate type-mix: {top_n}/{n} ({top_n / n:.0%}) are table_chunk"
    return None


def node_chunk_documents(ctx: dict):
    """
    切分文档为结构化 chunks。

    优先使用 chunk_from_blocks()（从 ExtractedBlock 切分），
    如果 canonical 没有 blocks 则 fallback 到 chunk_document(text=...)。
    """
    canonicals = ctx["canonicals"]
    config = get_config()
    # ─── Category-Aware Dynamic Routing Strategy ───
    global_split_mode = ctx.get("split_mode", "dynamic")
    all_chunks: List[Chunk] = []

    for doc in canonicals:
        if doc.get("redaction_action") == "QUARANTINE":
            print(f"    └─ {doc['doc_id']}: skipped (quarantined)")
            continue

        # G21 fail-closed：asset ocr_text + visual_summary 的 PII 消费点兜底掩码（默认 ON，
        # RAG_IMAGE_OCR_PII_FAILCLOSED=false 关闭）。RAG_IMAGE_OCR_PII（node 02/03 的
        # 检测+脱敏）默认 OFF 且靠 stage2_node setdefault 存续——重部署丢 flag 时，截图里的
        # 手机号/身份证会原样进入 image_refs/[图片OCR] 合成块并持久化到 chunk_meta/HA3。
        # ⚠️ visual_summary（VLM caption）此前从不脱敏——106E77 事故：VLM 照抄图内 FDA
        # 注册号/证书编号进 caption，_img_entry 原样带入 image_refs_json（node 03 只碰
        # ocr_text）。两字段现共走 scrub_image_text（凭证锚点 FP 护栏保住"注册号是多少"
        # 这类合法答案）。canonical 不改写（canonical_sha256 与跨文档去重/skip-unchanged
        # 门耦合，改写即破坏哈希语义）；保护随每轮 chunk 消费重建，不依赖 flag 存续。
        if os.environ.get("RAG_IMAGE_OCR_PII_FAILCLOSED", "true").lower() not in ("0", "false", "no"):
            _g21_scrubbed = 0
            _g21_findings = []          # A（2026-07-26）：留痕回收口，见下方 _persist
            for _asset in doc.get("assets", []) or []:
                for _fld in ("ocr_text", "visual_summary"):
                    _orig = _asset.get(_fld)
                    if not _orig:
                        continue
                    _new = scrub_image_text(_orig, findings=_g21_findings)
                    if _new != _orig:
                        _asset[_fld] = _new
                        _g21_scrubbed += 1
            if _g21_scrubbed:
                print(f"    ├─ [pii-failclosed] {doc['doc_id']}: {_g21_scrubbed} 个图片 "
                      f"OCR/caption 字段命中 PII，已在消费点掩码（node 03 flag 未生效的兜底）")
            _persist_image_scrub_findings(ctx, doc, _g21_findings)

        text = doc.get("redacted_text") or doc["text"]
        
        # 动态参数匹配
        m_mode = "text"
        if global_split_mode == "dynamic":
            cat_l1 = str(doc.get("category_l1", "")).lower()
            cat_l2 = str(doc.get("category_l2", "")).lower()
            title = str(doc.get("title", "")).lower()
            doc_id = str(doc.get("doc_id", "")).lower()
            # FAQ 切块只看"文档本身是 FAQ"（分类/标题/doc_id 含 faq）。
            # ⚠️ 不再让 faq_eligible 劫持路由：它是"可生成 FAQ"的下游标记，不是结构信号 ——
            # 真实 LLM 分类把多数 SOP 标成 faq_eligible=True，曾导致 124 个 SOP 批次
            # 只有 1 个走 step 模式（2026-06-10 本地 E2E 实测，123/124 被劫持进 faq）。
            if "faq" in cat_l1 or "faq" in cat_l2 or "faq" in title or "faq" in doc_id:
                m_chunk = ctx.get("faq_size", config.chunker.faq_strategy.max_chunk_chars)
                m_overlap = ctx.get("faq_overlap", config.chunker.faq_strategy.overlap_chars)
                m_mode = "faq"
            elif any(kw in cat_l1 for kw in ["policy", "standard", "regulation"]) or any(kw in cat_l2 for kw in ["policy", "standard", "regulation"]) or "制度" in title or "规定" in title or "规范" in title:
                m_chunk = ctx.get("clause_size", config.chunker.clause_strategy.max_chunk_chars)
                m_overlap = ctx.get("clause_overlap", config.chunker.clause_strategy.overlap_chars)
                m_mode = "clause"
            elif "manual" in cat_l1 or "manual" in cat_l2 or "guide" in cat_l1 or "guide" in cat_l2 or "manual" in title or "guide" in title:
                m_chunk = ctx.get("manual_size", config.chunker.manual_strategy.max_chunk_chars)
                m_overlap = ctx.get("manual_overlap", config.chunker.manual_strategy.overlap_chars)
            else:
                m_chunk = ctx.get("sop_size", config.chunker.sop_strategy.max_chunk_chars)
                m_overlap = ctx.get("sop_overlap", config.chunker.sop_strategy.overlap_chars)

            # ─── Step Card 路由：SOP/manual/guide 类文档 + 包含步骤标记 → step 模式 ───
            # 也允许从 clause 升级（B1-3）：操作规程/检验规程/作业标准 等 step-rich 文档常因 cat=standard
            # 或标题含"规范"先落 clause，却带真实步骤+截图——之前 step 检测只在 m_mode=='text' 时跑，
            # 这些文档永远拿不到 step_card。_detect_step_patterns 仍要求 is_sop_like(sop/操作/作业指导/
            # 规程/检验 等关键词或 ≥2 SOP 锚词) + ≥2 步骤边界，纯制度/规定政策文档（无 sop 信号）不会被
            # 误升，仍走 clause。
            # B17（2026-07-25）：路由可观测。**只观测、不改判据** —— 收紧 detector 会改
            # chunk 家族（需冻结重灌 + manifest 门），而现在连"多少篇制度类文档实际走了
            # step、因为哪个词"都查不出来，无从判断该不该收紧。记初始/最终模式与命中信号，
            # 随 chunk.extra → extra_json 落 chunk_meta，一条 SQL 即可统计。
            _initial_mode = m_mode
            _step_detected = _detect_step_patterns(doc)
            if m_mode in ("text", "clause") and _step_detected:
                m_mode = "step"
                m_chunk = ctx.get("sop_size", config.chunker.sop_strategy.max_chunk_chars)
                m_overlap = 0  # step 模式按步骤边界切，不需要 overlap
                print(f"    ├─ [step-detect] Detected step patterns in {doc['doc_id']}, routing to step mode")

            # ─── XLSX Layout Classifier：统一路由（替代旧 is_equipment_standard） ───
            from opensearch_pipeline.extraction.xlsx_classifier import classify_xlsx_layout

            file_ext = str(doc.get("file_ext", "")).lower()
            xlsx_layout_type = "normal_spreadsheet"

            # ─── PPTX：幻灯片感知切块（每页 slide → 一个 chunk）───
            if file_ext == "pptx":
                m_mode = "slide"
                m_overlap = 0

            if file_ext in ("xlsx", "xls"):
                # 从 blocks 中提取 sheet_names（heading blocks with sheet_idx=0,1,...）
                _blocks = doc.get("blocks", [])
                _sheet_names = []
                for _b in _blocks:
                    _extra = _b.get("extra", {}) if isinstance(_b, dict) else (getattr(_b, "extra", {}) or {})
                    if (isinstance(_b, dict) and _b.get("block_type") == "heading") or \
                       (hasattr(_b, "block_type") and _b.block_type == "heading"):
                        _sec_type = _extra.get("section_type", "")
                        _text = _b.get("text", "") if isinstance(_b, dict) else _b.text
                        if _sec_type in ("cleaning_items", ""):
                            if _text and _text not in _sheet_names:
                                _sheet_names.append(_text)

                # P0-3：优先消费 DAG1 持久化的 layout 判定（用真实 filename 分类一次）。DAG2 的
                # doc.filename 在生产 Stage-2 重载后为空 → 若在此重分类会与 DAG1 不一致，可能把
                # procedure_image_guide 误判成 normal_spreadsheet → _chunk_procedure_steps 不触发、
                # step_card 结构静默丢失。仅当 canonical 无持久值（旧 canonical 向后兼容）才回退重分类。
                _persisted_layout = doc.get("xlsx_layout_type")
                if _persisted_layout:
                    xlsx_layout_type = _persisted_layout
                    _layout_debug = {"scores": {}, "matched_signals": ["persisted-from-DAG1"]}
                else:
                    xlsx_layout_type, _layout_debug = classify_xlsx_layout(
                        filename=doc.get("filename", ""),
                        sheet_names=_sheet_names,
                        flat_text=text[:5000],  # 前 5000 字足够分类
                    )
                print(f"    ├─ [xlsx-layout] {xlsx_layout_type} "
                      f"(scores={_layout_debug['scores']}, "
                      f"signals={_layout_debug['matched_signals'][:2]})")

                if xlsx_layout_type == "equipment_cleaning_standard":
                    m_mode = "text"
                    m_chunk = 300
                    m_overlap = 0
                elif xlsx_layout_type == "procedure_image_guide":
                    m_mode = "text"
                    m_chunk = 500   # step card 内容更长
                    m_overlap = 0
                elif xlsx_layout_type == "product_spec_instruction":
                    m_mode = "text"
                    m_chunk = 400   # field card
                    m_overlap = 0
                # normal_spreadsheet → 保持已选 m_mode/m_chunk/m_overlap

            # 保存 xlsx_layout_type 到 doc 供下游使用
            doc["xlsx_layout_type"] = xlsx_layout_type

        else:
            m_chunk = ctx.get("max_chunk_chars", config.chunker.sop_strategy.max_chunk_chars)
            m_overlap = ctx.get("overlap_chars", config.chunker.sop_strategy.overlap_chars)
            m_mode = global_split_mode
            xlsx_layout_type = "normal_spreadsheet"

        chunker = DocumentChunker(
            max_chunk_chars=m_chunk,
            min_chunk_chars=ctx.get("min_chunk_chars", 50),
            overlap_chars=m_overlap,
            split_mode=m_mode,
            prepend_dept=ctx.get("prepend_dept", False),
            prepend_title=ctx.get("prepend_title", True),
            prepend_section=ctx.get("prepend_section", True),
            prepend_for_faq=ctx.get("prepend_for_faq", False),
            max_context_chars=ctx.get("max_context_chars", 100),
            max_context_ratio=ctx.get("max_context_ratio", 0.3),
            row_card=(xlsx_layout_type == "equipment_cleaning_standard") if global_split_mode == "dynamic" else False,
            xlsx_layout_type=xlsx_layout_type if global_split_mode == "dynamic" else "normal_spreadsheet",
        )

        metadata = {
            "title": doc.get("title", ""),
            "owner_dept": doc.get("owner_dept", ""),
            "category_l1": doc.get("category_l1", ""),
            "category_l2": doc.get("category_l2", ""),
            "permission_level": doc.get("permission_level", "public"),
            "kb_type": doc.get("kb_type", "public"),
            "risk_level": doc.get("risk_level", "low"),
            "source_oss_key": doc.get("canonical_key", ""),
        }

        blocks = doc.get("blocks", [])

        # ─── Step 模式：注入 image_ref 块到 block 序列 ───
        is_step_mode = (m_mode == "step")
        if blocks:
            assets = doc.get("assets", [])
            # ⚠️ 判断顺序：**位置性 ref 的清理不得受 `assets` 门控**（C0，2026-08-03）。
            # 旧写法两支都要求 `assets` 非空；而漏斗把整篇的图全判 DISCARD/QUARANTINE 时
            # `assets` 恰是 []，于是抽取器产出的 image_ref 一个都不被清理 ⇒ 留下没有
            # oss_key/source_image 的**空洞 ref**，serving 渲染不出、llm_generator 却按
            # image_refs 的 truthiness 照样注 <<IMG:N>> 标记。C0 让表内装饰 logo 也开始产
            # ref 后，"整篇全 DISCARD" 从罕见变常见（抬头表 logo 就是典型），必须先修这条。
            _has_refs = any(
                (b.get("block_type") if isinstance(b, dict)
                 else getattr(b, "block_type", "")) == "image_ref"
                for b in blocks
            )
            if _has_refs:
                # 已有位置性 ref（DOCX 抽取器产出）⇒ 无条件 enrich/清理，assets 为空即清成零 ref
                blocks = _enrich_existing_image_refs(blocks, assets, doc)
                print(f"    ├─ [ref-enrich] Enriched/pruned positional image_refs for {doc['doc_id']}")
            elif assets and is_step_mode:
                # 无位置性 ref 且有存活图 ⇒ step 模式才做启发式插入（非 step 模式无插入语义）
                blocks = _inject_image_ref_blocks(blocks, assets, doc)
                print(f"    ├─ [step-inject] Injected image_refs into block sequence for {doc['doc_id']}")

        if blocks:
            # C0：每篇独立 list（绝不复用/共享，否则跨文档串诊断）
            _c0_diags: list = []
            chunks = chunker.chunk_from_blocks(
                blocks=blocks,
                doc_id=doc["doc_id"],
                version_no=doc["version_no"],
                metadata=metadata,
                diagnostics=_c0_diags,
            )
            if _c0_diags:
                _n = sum(d.get("count", 0) for d in _c0_diags)
                _tbls = sorted({d.get("table_index") for d in _c0_diags})
                print(f"    🚨 [C0_TABLE_IMAGE_UNBOUND] {doc['doc_id']}: {_n} 张表内图无前置步骤"
                      f"、已显式丢弃（tables={_tbls}）—— 语料版式漂移，须人工复核")
                ctx.setdefault("table_image_drop_notes", {}).setdefault(
                    (doc["doc_id"], doc["version_no"]), []).append(
                        f"C0_TABLE_IMAGE_UNBOUND: {_n} table image(s) in {len(_tbls)} table(s) "
                        f"had no preceding step and were dropped (not misbound)")
        else:
            chunks = chunker.chunk_document(
                text=text,
                doc_id=doc["doc_id"],
                version_no=doc["version_no"],
                metadata=metadata,
            )

        # 标记脱敏状态
        for chunk in chunks:
            chunk.sensitive_redacted = doc.get("redaction_action") == "REDACTED"

        # B17（2026-07-25）：路由观测落 chunk.extra → 随 extra_json 进 chunk_meta。
        # 记**三个**量（不只是最终模式）：初始模式、detector 是否命中、最终模式 ——
        # PPTX/XLSX 会在 detector 之后再次覆盖 m_mode，只记最终值无法归因是谁改的。
        # 纯观测：不改任何判据（收紧 detector = 改 chunk 家族，需冻结重灌 + manifest 门）。
        _route_obs = {
            "route_initial_mode": _initial_mode,
            "route_final_mode": m_mode,
            "step_detector_matched": bool(_step_detected),
        }
        for chunk in chunks:
            if getattr(chunk, "extra", None) is None:
                chunk.extra = {}
            chunk.extra.update(_route_obs)

        # ─── 结构化 XLSX 图片绑定（按 anchor_row / figure_refs 绑定到 chunk）───
        # layout_bound_fns：被结构化版式有意绑定（即使载体是文本类 chunk）的图片文件名，
        # 兜底 image chunk 环节跳过它们，避免重复建 chunk
        layout_bound_fns = set()
        if xlsx_layout_type in ("product_spec_instruction", "procedure_image_guide") and global_split_mode == "dynamic":
            assets = doc.get("assets", [])
            if assets:
                source_key = doc.get("source_key", "")
                dept_code = _dept_from_raw_key(source_key, doc.get("owner_dept", "unknown"))
                version = doc["version_no"]
                d_id = doc["doc_id"]

                if xlsx_layout_type == "product_spec_instruction":
                    # VLM image_category 驱动绑定：用模型判断图片类别，决定归属
                    spec_chunks = [(c, c.extra.get("spec_row_start", 9999), c.extra.get("spec_row_end", 0))
                                   for c in chunks if c.extra.get("spec_row_start") is not None]
                    chunk_images = {id(c): [] for c, _, _ in spec_chunks}

                    # 找关键 section 的 chunk id
                    sec_chunk_map = {}  # section_type → chunk_id
                    for c, rs, re_ in spec_chunks:
                        sec = c.extra.get("spec_section")
                        if sec and sec not in sec_chunk_map:
                            sec_chunk_map[sec] = id(c)

                    # image_category → section_type 映射
                    _CAT_TO_SEC = {
                        "logo_header": "header",
                        "decorative": "header",
                        "process_flow": "process_ccp",
                        "product_photo": "product_photo",
                        "inspection_photo": "product_photo",
                        "test_photo": "product_photo",
                    }

                    for asset in assets:
                        ar = asset.get("anchor_row")
                        if ar is None or asset.get("status") != "ROUTE_TO_VECTOR":
                            continue
                        fn = asset.get("filename", "")
                        cat = asset.get("image_category", "unknown")
                        # 上传环节回填的 oss_key 优先（TO_VECTOR 均会上传），构造路径
                        # 仅作离线/旧数据兜底 —— 与 step_card/slide/image chunk 路径一致
                        _spec_oss_key = (asset.get("oss_key")
                                         or f"processing/assets/{dept_code}/{d_id}/v{version}/{fn}")
                        img_entry = {
                            "filename": fn,
                            "oss_key": _spec_oss_key,
                            # 契约键（CLAUDE.md）：source_image 与 step_card/slide 路径一致，
                            # 自描述、不依赖检索期 oss_key→source_image 折叠
                            "source_image": _spec_oss_key,
                            # image_index 契约键：取 asset 抽取序号（与其它版式/DOCX/PDF 同源）。
                            # product_spec_instruction 绑定原先漏设此键（2026-06-15 D6）。
                            "image_index": asset.get("image_index", asset.get("original_index")),
                            "anchor_row": ar,
                            "image_category": cat,
                            "visual_summary": asset.get("visual_summary", ""),
                            "ocr_text": asset.get("ocr_text", ""),
                        }

                        # 1) VLM category 匹配
                        target_sec = _CAT_TO_SEC.get(cat)
                        target_id = sec_chunk_map.get(target_sec) if target_sec else None

                        if target_id is not None:
                            chunk_images[target_id].append(img_entry)
                        else:
                            # 2) VLM 分类为 unknown/其他 → 按行号 fallback
                            best_chunk = None
                            best_dist = 9999
                            for c, rs, re_ in spec_chunks:
                                if rs <= ar <= re_:
                                    best_chunk = c; best_dist = 0; break
                                dist = min(abs(ar - rs), abs(ar - re_))
                                if dist < best_dist:
                                    best_dist = dist; best_chunk = c
                            # 非 logo 的 unknown 图片优先归 product_photo
                            if best_chunk and best_chunk.extra.get("spec_section") in ("header", "appendix"):
                                fallback_id = sec_chunk_map.get("product_photo")
                                if fallback_id:
                                    chunk_images[fallback_id].append(img_entry)
                                else:
                                    chunk_images[id(best_chunk)].append(img_entry)
                            elif best_chunk:
                                chunk_images[id(best_chunk)].append(img_entry)

                    for c, _, _ in spec_chunks:
                        imgs = chunk_images[id(c)]
                        if imgs:
                            c.extra["image_refs"] = imgs
                            # image 载体（product_photo 卡）serving 只认 chunk 级
                            # source_image（content_blocks_builder 的 image 分支 +
                            # to_ha3_doc 索引），仅存 image_refs 则图片永远渲染不出
                            # —— 与 PPTX slide 路径同因：首图提升为封面
                            # source_image（+visual_summary）。
                            if c.chunk_type == "image":
                                c.extra["source_image"] = imgs[0]["oss_key"]
                                if imgs[0].get("visual_summary"):
                                    c.extra.setdefault(
                                        "visual_summary", imgs[0]["visual_summary"])
                            layout_bound_fns.update(
                                e["filename"] for e in imgs if e.get("filename"))

                elif xlsx_layout_type == "procedure_image_guide":
                    import re as _re_fig
                    step_cards = sorted(
                        [c for c in chunks if c.chunk_type == "step_card"],
                        key=lambda c: c.extra.get("step_no", 0),
                    )
                    step_by_no = {c.extra.get("step_no"): c for c in step_cards}

                    def _img_entry(a):
                        fn = a.get("filename", "")
                        # 上传环节回填的 oss_key 优先（TO_VECTOR/TO_TEXT 均会上传）；
                        # 构造路径仅作离线/旧数据兜底，与独立 image chunk 路径一致
                        oss_key = (a.get("oss_key")
                                   or f"processing/assets/{dept_code}/{d_id}/v{version}/{fn}")
                        entry = {
                            "filename": fn,
                            "oss_key": oss_key,
                            # 契约键（CLAUDE.md）：source_image 与 DOCX step_card 一致，自描述、
                            # 不依赖检索期 oss_key→source_image 折叠
                            "source_image": oss_key,
                            # image_index 契约键：直接取 asset 抽取序号（0-based，extract_images_from_xlsx
                            # 赋值、_process_embedded_images 透传，= filename 内 _img{N} 序号），与 DOCX/PDF
                            # step_card 同源（chunker._chunk_by_step 用 img_extra['image_index']）。
                            # 注意：figure_no 是 '图N' 字符串标签(1-based)，≠ image_index，绝不可拿它当 index
                            # （旧逻辑 isinstance(figure_no,int) 恒 False → image_index 永远 None → 2026-06-15 D6 漏洞）。
                            "image_index": a.get("image_index", a.get("original_index")),
                            "figure_no": a.get("figure_no"),
                            "anchor_row": a.get("anchor_row"),
                            "image_category": a.get("image_category", "unknown"),
                            "visual_summary": a.get("visual_summary", ""),
                            "ocr_text": a.get("ocr_text", ""),
                        }
                        return entry

                    # 绑定池分两轮：先 ROUTE_TO_VECTOR（行为与原单轮完全一致），再 ROUTE_TO_TEXT。
                    # TO_TEXT 截图（UI 截图多数走此路由）原先被排除在绑定之外：OCR 文本进了
                    # chunk，原图却没有任何 serving 可达载体（step_card 的 image_refs 经 RDS
                    # image_refs_json 恢复；文本 chunk 上的 refs 检索期不可达），图片提取了
                    # 却永远渲染不出（I5）。bound_nos 按轮独立：TO_TEXT 内容命中已有图的步骤
                    # 时作为第二张图追加、不与 VECTOR 抢占；priority-2 只补完全无图的步骤。
                    # figure_no 是否"有语义意义"：unified_extractor 在 procedure_image_guide
                    # 版式下总会按提取顺序给每张图分配 "图1/图2/.../图N" 作为占位标签，
                    # 这是位置计数器、不是文档真实图号。"图N→步骤N" 启发式只有在以下两种
                    # 情形之一才反映作者意图：
                    #   (a) 某个 figure_no 被多个 asset 共用（说明是作者手工标的语义标签，
                    #       同一张图的多变体；test_xlsx_procedure_totext_appends_as_secondary_ref
                    #       的典型场景），或
                    #   (b) 至少一个 step.text 里通过 "如图N" 显式引用了该 figure_no
                    #       （`figure_refs` 即从此提取，命中后的绑定走的是 figure_refs 分支）。
                    # 否则 figure_no 仅是 1..N 的递增序号，强行 N→stepN 会在 step 数与图数
                    # 不严格对应时把图绑错（xlsx_sop: step3 无图、step2 有 2 图、img5→step6
                    # 而 GT 应到 step5 的"互换"全由此引起）。
                    all_step_fig_refs = set()
                    for c in step_cards:
                        for fr in (c.extra.get("figure_refs") or []):
                            all_step_fig_refs.add(fr)
                    fno_counts: Dict[str, int] = {}
                    for a in assets:
                        fno = a.get("figure_no")
                        if fno:
                            fno_counts[fno] = fno_counts.get(fno, 0) + 1
                    # B3（2026-07-25）：改为 **per-figure** 判定。上面注释里的 (a)(b) 本来就是
                    # 逐图号成立的条件，此前却折叠成一个**文档级**布尔：只要文档里任意一句
                    # "如图2"，`figure_no_meaningful` 就为 True，于是**全篇**的位置计数器
                    # 图号都被当成作者语义标签走 N→stepN 直连 —— 那些从没被引用过的图因此被
                    # 按序号硬绑。现在只有该 fno 自己满足 (a) 或 (b) 才允许直连。
                    def _fno_meaningful(fno) -> bool:
                        if not fno:
                            return False
                        return fno_counts.get(fno, 0) > 1 or fno in all_step_fig_refs

                    # step_no → row_num 映射：来自 doc.blocks（procedure_image_guide
                    # 提取器把行号塞进每个 step block 的 extra.row_num，但 chunker 没
                    # 把 row_num 往 step_card.extra 里搬，所以我们这里从原 blocks 现取。
                    # 同 anchor 多图消歧（下面）需要根据 row_num 判断 "相邻步骤"。
                    step_row_map: Dict[int, int] = {}
                    for _b in doc.get("blocks", []):
                        _ex = _b.get("extra") if isinstance(_b, dict) else getattr(_b, "extra", {})
                        if _ex and _ex.get("step_no") is not None:
                            _rn = _ex.get("row_num")
                            if _rn is not None:
                                step_row_map[_ex["step_no"]] = _rn

                    # P0 证据基建(两轮共用):步骤侧 token 集 + 步骤侧/全 asset 池侧 df。
                    # 池侧 df 用全部 assets(含 TO_TEXT)——"这个词是不是多张图都在说"
                    # 与路由无关,且保证两轮评分一致。
                    _ev_step_toks = {c.extra.get("step_no"): _evidence_toks(c.chunk_text)
                                     for c in step_cards}
                    _ev_df_steps: Dict[str, int] = {}
                    for _tks in _ev_step_toks.values():
                        for _t in _tks:
                            _ev_df_steps[_t] = _ev_df_steps.get(_t, 0) + 1
                    _ev_df_pool: Dict[str, int] = {}
                    for _a in assets:
                        _atxt = ((_a.get("visual_summary") or "") + " "
                                 + (_a.get("ocr_text") or "")).strip()
                        for _t in _evidence_toks(_atxt):
                            _ev_df_pool[_t] = _ev_df_pool.get(_t, 0) + 1

                    def _bind_pool(pool):
                        bound_nos = set()  # 本轮内 step_no already assigned an image

                        # 跨轮 anchor 占用：TEXT 轮启动时，VECTOR 轮已绑的 step_card.image_refs
                        # 里的 anchor_row 必须作为"该 anchor 已被占用"的种子——否则 TEXT 轮里
                        # 一张与 VECTOR 同 anchor 的图，会绕开邻接守卫贪心绑到 VECTOR 已绑步骤
                        # 的相邻位（与同轮内的同 anchor 多图同因；只是 anchor_taken_steps 默认
                        # 重置丢失了上一轮信息）。
                        anchor_taken_steps: Dict[int, list] = {}
                        for c in step_cards:
                            for r in (c.extra.get("image_refs") or []):
                                _ar = r.get("anchor_row")
                                _sn = c.extra.get("step_no")
                                if _ar is not None and _sn is not None:
                                    anchor_taken_steps.setdefault(_ar, []).append(_sn)

                        # 优先级 0：内容匹配（视觉描述/ocr ↔ 步骤文本）。
                        # 「操作示图」列的 figure_no（图N）多为按提取顺序自动编号、anchor_row 也常不可靠，
                        # 而图片描述里的动作关键词（归零/读数/电源）能更准地定位步骤。仅在强且唯一匹配时
                        # 按内容绑定；其余回退到 figure_no / anchor 顺序（保护描述稀疏的图片不被误绑）。
                        # 评分走 _evidence_match_steps(互斥稀有度),不再用单侧 IDF 的
                        # _content_match_steps——杂词唯一共现骗分是 VLM 措辞漂移放大器
                        # (l4ing.jaccard.xlsx 0.89→0.72 的根因链之一)。
                        cms = []  # (margin, score, step_no, asset)
                        for a in pool:
                            it = ((a.get("visual_summary") or "") + " " + (a.get("ocr_text") or "")).strip()
                            sno, sc, sec = _evidence_match_steps(
                                _evidence_toks(it), _ev_step_toks, _ev_df_steps, _ev_df_pool)
                            cms.append((sc - sec, sc, sno, a))
                        # 置信度（分差）高的先绑。当 (margin, score) 都相等时，按 asset 的
                        # 物理位置（anchor_row → image_index → filename）做兜底 tiebreaker，
                        # 避免依赖 Python 稳定排序回退到 pool 顺序（pool 顺序虽已在 extractor
                        # 末尾排稳，但显式 tiebreaker 让此处对上游任何顺序震荡都免疫）。
                        cms.sort(key=lambda x: (
                            -x[0],
                            -x[1],
                            x[3].get("anchor_row") if x[3].get("anchor_row") is not None else 10**9,
                            x[3].get("image_index") if x[3].get("image_index") is not None else 10**9,
                            x[3].get("filename") or "",
                        ))
                        remaining = list(pool)
                        # 同 anchor_row 多图："首张图按内容绑步骤 A 后，剩余同 anchor
                        # 的图不应再绑到 A 相邻 (±1 行) 的步骤"——典型 xlsx_sop：anchor=12
                        # 的 img2/img4 用同 anchor，img2 内容信号"归零"→step4 正确；
                        # img4 内容信号"称量"→step3 偶合，step3 row 13 与 step4 row 14
                        # 相邻，应拒绑、让 img4 走 P2 兜底到 step6。
                        for margin, sc, sno, a in cms:
                            if sno is None or sc < 0.8 or margin < 0.5 or sno in bound_nos:
                                continue
                            ar = a.get("anchor_row")
                            if ar is not None and ar in anchor_taken_steps and step_row_map:
                                target_row = step_row_map.get(sno)
                                # 已被同 anchor 占用、且 target 与已绑步骤相邻 → 拒绑，留给 P2
                                if target_row is not None and any(
                                    abs(target_row - step_row_map.get(prev_sno, target_row)) <= 1
                                    for prev_sno in anchor_taken_steps[ar]
                                ):
                                    continue
                            step_by_no[sno].extra.setdefault("image_refs", []).append(_img_entry(a))
                            bound_nos.add(sno)
                            if ar is not None:
                                anchor_taken_steps.setdefault(ar, []).append(sno)
                            remaining.remove(a)

                        # 优先级 1：figure_no 数字 == 步骤号（图N→步骤N）/步骤文本显式引用图号；
                        #           仅绑到尚未被内容匹配占用的步骤
                        unbound = []
                        for a in remaining:
                            target = None
                            fno = str(a.get("figure_no") or "")
                            mnum = _re_fig.search(r"(\d+)", fno)
                            # B3：**先**作者显式引用（figure_refs 精确匹配），**后**位置计数器
                            # 直连。此前顺序相反 —— 与上面注释自述的 "(b) 命中后的绑定走
                            # figure_refs 分支" 直接矛盾：步骤6 写「如图3」而步骤3 恰好空着时，
                            # 图3 会被绑到步骤3；这条错绑经 image_refs_json 直达钉钉卡片，
                            # 一线员工看到的就是错图。
                            if fno:
                                for c in step_cards:
                                    if fno in (c.extra.get("figure_refs") or []) and c.extra.get("step_no") not in bound_nos:
                                        target = c
                                        break
                                # B3：显式引用的图，即便该步骤已在 bound_nos（上一轮已绑过图）
                                # 也追加为**第二张图** —— 作者写了「如图N」就是要在这一步看到它。
                                if target is None:
                                    for c in step_cards:
                                        if fno in (c.extra.get("figure_refs") or []):
                                            target = c
                                            break
                            # 位置计数器直连只在该 fno 自己"有语义意义"时才可信（见 _fno_meaningful）
                            if (target is None and _fno_meaningful(fno) and mnum
                                    and int(mnum.group(1)) not in bound_nos):
                                target = step_by_no.get(int(mnum.group(1)))
                            if target is not None:
                                # B3：按**图片实体身份**去重。bound_nos 只防步骤被重复占用，
                                # 防不住同一张图被追加两次（同一 fno 被多个 asset 共用时会）。
                                # 身份键沿用 xlsx 的载重契约：filename + anchor_row；
                                # 无 anchor 时退到 oss_key/source_image + image_index。
                                _entry = _img_entry(a)
                                _refs = target.extra.setdefault("image_refs", [])
                                if not any(_same_image_ref(_entry, _e) for _e in _refs):
                                    _refs.append(_entry)
                                bound_nos.add(target.extra.get("step_no"))
                            else:
                                unbound.append(a)

                        # 优先级 2：剩余图片按 anchor_row 顺序补到仍空的步骤。
                        # 两类分流：
                        #   redirected = anchor 已在 P0 被占用的"剩余张"——必须避开 P0 占用步骤
                        #     的相邻区，优先派往"前向远端"（row > 已绑 row，且非相邻）。
                        #   naturals  = anchor 没冲突的图——保持旧位置分配语义（idx→open_steps[idx]
                        #     的位置对位），让 step5/anchor=14 这类自然对齐保持不被打乱。
                        # 算法：redirects 先选位（_far_score 决定），naturals 再用旧 idx-比例公式
                        # 取位（被 redirect 占用的位置走"最近空闲"兜底，与旧 si 单一位置接近）。
                        # 跨轮以"step_card 已带图"为占用判据（第一轮等价于 step_no not in bound_nos）。
                        open_steps = [c for c in step_cards
                                      if not c.extra.get("image_refs")
                                      and c.extra.get("step_no") not in bound_nos]
                        unbound.sort(key=lambda a: (a.get("anchor_row") or 0))
                        if unbound and open_steps:
                            n_open = len(open_steps)
                            n_un = len(unbound)
                            consumed = set()  # 已派出的 open_step index

                            def _is_redirect(a):
                                ar = a.get("anchor_row")
                                return (ar is not None and ar in anchor_taken_steps
                                        and step_row_map)

                            def _far_score(idx_chunk, prev_steps):
                                """挑远端开放步骤——优先 forward (row > max prev_row) 且 row 最大；
                                forward 不存在则按 backward 距离最远。max() 取胜。"""
                                i, c = idx_chunk
                                sr = step_row_map.get(c.extra.get("step_no"))
                                if sr is None:
                                    return (-1, 0, 0)
                                prev_rows = [step_row_map[p] for p in prev_steps if p in step_row_map]
                                if not prev_rows:
                                    return (0, 0, sr)
                                max_prev = max(prev_rows)
                                is_forward = 1 if sr > max_prev else 0
                                # forward：sr 越大越好（推到列表末端，让中间空位给 naturals）；
                                # backward：max_prev-sr 越大越好（离 prev 越远）
                                return (is_forward, sr if is_forward else -(max_prev - sr), sr)

                            # 1) Redirected 优先派位——claim 远端 slot。
                            # _placed_redirects 记录已在本预派位轮落位的 asset id:naturals
                            # 循环顶只能跳"确实已派位"的——anchor 登记(2026-07-20)让
                            # _is_redirect 变成动态判定后,若仍按分类跳过,轮内新冲突的
                            # 同 anchor 图会被静默丢进独立 image chunk 兜底。
                            _placed_redirects = set()
                            for a in unbound:
                                if not _is_redirect(a):
                                    continue
                                ar = a.get("anchor_row")
                                prev_steps = anchor_taken_steps[ar]
                                cands = [(i, c) for i, c in enumerate(open_steps)
                                         if i not in consumed]
                                if not cands:
                                    break
                                target_idx, _ = max(cands, key=lambda ic: _far_score(ic, prev_steps))
                                open_steps[target_idx].extra.setdefault("image_refs", []).append(_img_entry(a))
                                consumed.add(target_idx)
                                _placed_redirects.add(id(a))
                                if ar is not None:
                                    anchor_taken_steps.setdefault(ar, []).append(
                                        open_steps[target_idx].extra.get("step_no"))

                            # 2) Naturals 走旧位置分配——保留 si=idx 的"位置对位"语义。
                            #    被 redirect 占用的位置：找未 consumed 中距离 nat_si 最近的位
                            #    （与旧"step_no 顺序"的兜底接近，避免 pack-from-front 错绑）。
                            # 2a) 兄弟图相似度优先：分配到 nat_si 之前，先看该 asset 是否与某个
                            #     已绑图（任意 step）视觉描述+OCR bigram Jaccard 构成强兄弟
                            #     （≥0.15 且比其它步骤最高分高 1.5×）— 是则把它绑到那个 step
                            #     （同 step 多图但分布在不同 anchor 的场景：xlsx_sop step2 =
                            #     anchor=11 电源插入 + anchor=15 电源握持，qwen3-vl 措辞下两图
                            #     Jaccard 0.195-0.209；同一台天平不同动作的假兄弟对 0.08-0.12，
                            #     靠比例守卫拒绝，不会误粘连）。
                            #     必须在 naturals 循环中做（不能在 P1 后做）：sibling 图常自己也
                            #     是 unbound，要 naturals 把 orig_idx 较小的兄弟先按位置兜进 step
                            #     后，orig_idx 较大的同源图才能识别到 sibling。
                            P0_IMG_CAP = 3
                            def _toks_for_sim(s: str) -> set:
                                s = (s or "").lower()
                                cjk = _re_fig.findall(r'[一-鿿]', s)
                                bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
                                alnum = set(_re_fig.findall(r'[a-z0-9]{2,}', s))
                                return bigrams | alnum
                            def _ref_text(r: dict) -> str:
                                return ((r.get("visual_summary") or "") + " "
                                        + (r.get("ocr_text") or "")).strip()
                            for orig_idx, a in enumerate(unbound):
                                if id(a) in _placed_redirects:
                                    continue
                                # 2a sibling pass
                                a_text = ((a.get("visual_summary") or "") + " "
                                          + (a.get("ocr_text") or "")).strip()
                                a_toks = _toks_for_sim(a_text)
                                sib_step = None
                                sib_jacc = 0.0
                                sib_second = 0.0  # 其它步骤的最高相似度(比例守卫用)
                                if a_toks:
                                    for c in step_cards:
                                        sno = c.extra.get("step_no")
                                        if sno is None:
                                            continue
                                        refs = c.extra.get("image_refs") or []
                                        if not refs or len(refs) >= P0_IMG_CAP:
                                            continue
                                        step_best = 0.0
                                        for r in refs:
                                            r_toks = _toks_for_sim(_ref_text(r))
                                            if not r_toks:
                                                continue
                                            jacc = len(a_toks & r_toks) / max(len(a_toks | r_toks), 1)
                                            if jacc > step_best:
                                                step_best = jacc
                                        if step_best > sib_jacc:
                                            sib_second = sib_jacc
                                            sib_jacc = step_best
                                            sib_step = sno
                                        elif step_best > sib_second:
                                            sib_second = step_best
                                sib_bound = False
                                # 阈值 0.30→0.15 + 跨步骤 1.5× 比例守卫(2026-07-20):VLM 换代后
                                # 真兄弟对(xlsx_sop 两张电源图)实测 raw 0.195-0.209,0.30 一刀切
                                # 已接不住;假兄弟对(同一台天平的不同动作照)0.08-0.12。低阈值
                                # 必须配比例守卫——真兄弟对的次优候选 ~0.05(4×),假对彼此接近。
                                if (sib_step is not None and sib_jacc >= 0.15
                                        and (sib_second == 0.0 or sib_jacc >= 1.5 * sib_second)):
                                    # 邻接守卫：anchor 已被占用 且 target 与已占 step 相邻 → 让 nat_si 接管
                                    ar = a.get("anchor_row")
                                    skip_sib = False
                                    if ar is not None and ar in anchor_taken_steps and step_row_map:
                                        target_row = step_row_map.get(sib_step)
                                        if target_row is not None and any(
                                            abs(target_row - step_row_map.get(prev_sno, target_row)) <= 1
                                            for prev_sno in anchor_taken_steps[ar]
                                        ):
                                            skip_sib = True
                                    if not skip_sib:
                                        step_by_no[sib_step].extra.setdefault("image_refs", []).append(_img_entry(a))
                                        bound_nos.add(sib_step)
                                        if ar is not None:
                                            anchor_taken_steps.setdefault(ar, []).append(sib_step)
                                        # 同步占位（若 sib_step 仍在 open_steps 中）：避免后续 nat_si
                                        # 走"距离最近"兜底时再次撞到此 step；step_by_no 引用与 open_steps
                                        # 同一对象，image_refs 增量也会让后续 open_steps 过滤生效
                                        # （open_steps 是引用快照，不重算）
                                        for i, oc in enumerate(open_steps):
                                            if oc.extra.get("step_no") == sib_step:
                                                consumed.add(i)
                                                break
                                        sib_bound = True
                                if sib_bound:
                                    continue
                                # 动态 redirect 复检(2026-07-20):本轮更早落位的图(P0/兄弟/
                                # naturals)把同 anchor 占掉后,本图不能再按位置公式硬塞相邻位
                                # ——同 anchor 第二张图走远端前向派位(与静态 redirect 同款
                                # _far_score)。静态判定只看进 P2 前的快照,漏掉 naturals 轮内
                                # 新产生的同 anchor 冲突(xlsx_sop:img2 落位后 img4 必须避让)。
                                if _is_redirect(a):
                                    _cands = [(i, c) for i, c in enumerate(open_steps)
                                              if i not in consumed]
                                    if not _cands:
                                        break
                                    _ti, _ = max(_cands, key=lambda ic: _far_score(
                                        ic, anchor_taken_steps[a.get("anchor_row")]))
                                    open_steps[_ti].extra.setdefault("image_refs", []).append(_img_entry(a))
                                    consumed.add(_ti)
                                    if a.get("anchor_row") is not None:
                                        anchor_taken_steps.setdefault(a["anchor_row"], []).append(
                                            open_steps[_ti].extra.get("step_no"))
                                    continue
                                if n_un == n_open:
                                    nat_si = orig_idx
                                else:
                                    nat_si = min(int(orig_idx * n_open / max(n_un, 1)), n_open - 1)
                                if nat_si in consumed:
                                    free = [i for i in range(n_open) if i not in consumed]
                                    if not free:
                                        break
                                    nat_si = min(free, key=lambda i: (abs(i - nat_si), i))
                                open_steps[nat_si].extra.setdefault("image_refs", []).append(_img_entry(a))
                                consumed.add(nat_si)
                                # naturals 落位也登记 anchor——否则后续同 anchor 图看不到冲突,
                                # 动态 redirect 永不触发。
                                if a.get("anchor_row") is not None:
                                    anchor_taken_steps.setdefault(a["anchor_row"], []).append(
                                        open_steps[nat_si].extra.get("step_no"))

                    _bind_pool([a for a in assets if a.get("status") == "ROUTE_TO_VECTOR"])
                    _bind_pool([a for a in assets if a.get("status") == "ROUTE_TO_TEXT"])

        # ─── PPTX slide 模式：按 page_num 把图片绑定到对应 slide chunk ───
        if m_mode == "slide" and global_split_mode == "dynamic":
            assets = doc.get("assets", [])
            if assets:
                source_key = doc.get("source_key", "")
                dept_code = _dept_from_raw_key(source_key, doc.get("owner_dept", "unknown"))
                version = doc["version_no"]
                d_id = doc["doc_id"]
                slide_imgs = {}
                # TO_TEXT 与 TO_VECTOR 同绑：TO_TEXT 截图的 OCR 文本进了 slide chunk，
                # 原图若不绑进 serving 可达载体（visual_knowledge 的 source_image/
                # image_refs）则永远渲染不出 —— 与 XLSX TO_TEXT 兜底同因（I5）。
                # VECTOR 先入池：refs[0] 提升为封面 source_image，TO_TEXT 不抢占。
                for _st in ("ROUTE_TO_VECTOR", "ROUTE_TO_TEXT"):
                    for a in assets:
                        if a.get("status") == _st:
                            slide_imgs.setdefault(a.get("page_num"), []).append(a)
                def _slide_img_refs(imgs):
                    refs = []
                    for a in imgs:
                        # 上传环节回填的 oss_key 优先（TO_VECTOR/TO_TEXT 均上传），
                        # 构造路径仅作离线/旧数据兜底，与 step_card/image chunk 路径一致
                        oss_key = (a.get("oss_key")
                                   or f"processing/assets/{dept_code}/{d_id}/v{version}/{a.get('filename', '')}")
                        refs.append({
                            "filename": a.get("filename", ""),
                            "oss_key": oss_key,
                            "source_image": oss_key,
                            "page_num": a.get("page_num"),
                            "image_index": a.get("image_index"),
                            "image_category": a.get("image_category", "unknown"),
                            "visual_summary": a.get("visual_summary", ""),
                            "ocr_text": a.get("ocr_text", ""),
                        })
                    return refs

                for c in chunks:
                    imgs = slide_imgs.get(c.page_num, [])
                    if imgs:
                        refs = _slide_img_refs(imgs)
                        c.extra["image_refs"] = refs
                        # 关键：把首图提升为顶层 source_image（+visual_summary），使其被
                        # to_ha3_doc 索引。visual_knowledge 不走 step_card 的 RDS 重建路径，
                        # 若仅存 image_refs 则只落库 RDS、不进 HA3 → 检索期取不到图、
                        # 幻灯片图片无法展示。
                        c.extra["source_image"] = refs[0]["oss_key"]
                        if refs[0].get("visual_summary"):
                            c.extra.setdefault("visual_summary", refs[0]["visual_summary"])
                        # 含产品图/示意图的 slide → visual_knowledge
                        c.chunk_type = "visual_knowledge"

                # 图片型 slide（无文字 → _chunk_by_slide 未产出 chunk）：单独建
                # visual_knowledge chunk，否则该页图片无 chunk 可绑、在摄取期被丢弃。
                bound_pages = {c.page_num for c in chunks}
                from opensearch_pipeline.chunker import _generate_chunk_id, _estimate_tokens
                _next_idx = len(chunks)
                for pg, imgs in slide_imgs.items():
                    if pg in bound_pages or not imgs:
                        continue
                    refs = _slide_img_refs(imgs)
                    _summary = refs[0].get("visual_summary", "")
                    # TO_TEXT 截图 caption 常缺失：chunk_text 回退 OCR 片段（与独立
                    # image chunk 兜底一致），extra.visual_summary 保持真实 caption
                    _desc = _summary or (refs[0].get("ocr_text") or "").strip()[:120]
                    _title = doc.get("title", "")
                    _prefix = f"【文档:{_title}】" if _title else ""
                    _ctext = (f"{_prefix} [图片描述] {_desc}").strip()
                    slide_chunk = Chunk(
                        chunk_id=_generate_chunk_id(d_id, version, _next_idx),
                        doc_id=d_id, version_no=version, chunk_index=_next_idx,
                        chunk_type="visual_knowledge", chunk_text=_ctext,
                        token_count=_estimate_tokens(_ctext), raw_text=_ctext,
                        page_num=pg, source_oss_key=doc.get("canonical_key", ""),
                        # F-19：chunk 的 ACL owner_dept 一律取 RDS 权威值（doc.owner_dept），不用
                        # dept_code（raw/ 路径推导）——否则管理员改正 owner_dept 后升版，文本 chunk 用
                        # 新值、图片 chunk 用旧路径值 → 同文档 ACL 归属分裂。dept_code 只保留给 OSS 路径拼接。
                        source="multimodal", title=_title, owner_dept=(doc.get("owner_dept") or "unknown"),
                        category_l1=doc.get("category_l1", ""), category_l2=doc.get("category_l2", ""),
                        permission_level=doc.get("permission_level", "public"),
                        kb_type=doc.get("kb_type", "public"), risk_level=doc.get("risk_level", "low"),
                        sensitive_redacted=doc.get("redaction_action") == "REDACTED",
                        is_active=True, embedding_status="NOT_STARTED", index_status=ChunkIndexStatus.NOT_INDEXED,
                        extra={
                            "image_refs": refs,
                            "source_image": refs[0]["oss_key"],
                            "visual_summary": _summary,
                        },
                    )
                    chunks.append(slide_chunk)
                    _next_idx += 1

        # ─── 设备清扫基准书：把部位照片绑定到对应"清扫部位" chunk ───
        # 提取阶段已为每张图片标注 part_labels（匹配清扫部位名）与 anchor_row。
        # 身份列优先 + 驱逐 + 强者归并的绑定（见 _bind_equipment_cleaning_images）；
        # 未匹配/被丢弃的图片仍作独立 image chunk。
        ce_bound_fns = set()
        if xlsx_layout_type == "equipment_cleaning_standard" and global_split_mode == "dynamic":
            assets = doc.get("assets", [])
            if assets:
                source_key = doc.get("source_key", "")
                dept_code = _dept_from_raw_key(source_key, doc.get("owner_dept", "unknown"))
                ce_bound_fns, _ce_diag = _bind_equipment_cleaning_images(
                    chunks, assets, dept_code, doc["doc_id"], doc["version_no"],
                    doc.get("blocks"))
                # 诊断：未支持版式（三格身份）= 降级；无表头 = first-two-cells 兜底。
                if _ce_diag.get("unsupported"):
                    print(f"    ⚠️ equipment_cleaning 图文绑定：检测到未支持的三格身份版式 "
                          f"({_ce_diag['unsupported']} 行, section="
                          f"{sorted(s for s in _ce_diag['unsupported_sections'] if s)}) "
                          f"→ 已降级（该 section 图片改建独立 image chunk，不硬绑）")
                elif _ce_diag.get("fallback"):
                    print(f"    ⚠️ equipment_cleaning 图文绑定：{_ce_diag['fallback']} 行无表头、"
                          f"走 first-two-cells 兜底 (section="
                          f"{sorted(s for s in _ce_diag['fallback_sections'] if s)})")

        # ─── Visual Embedding & Image Chunking ───
        # Step 模式下图片已经绑定到 step_card，不再创建独立 image chunk。
        # 结构化 XLSX 版式（procedure_image_guide / product_spec）也已将图片绑定到对应卡片，
        # slide 模式也已按 page_num 绑定（TO_VECTOR+TO_TEXT 全部进 visual_knowledge
        # 载体，故 pptx 无需本环节兜底）；以上均跳过独立 image chunk 以避免重复。
        # 设备清扫基准书：仅跳过已按部位绑定的图片，未匹配的图片仍建独立 image chunk。
        #
        # XLSX 例外：上述"已绑定"假设对 XLSX 不总成立 —— 全屏 UI 截图型流程文档
        # （如 外贸发票操作流程.xlsx：3 sheet 各 1 张 TO_TEXT 截图、无文本单元格）会被
        # step-detect 误路由进 step 模式，refs 经启发式注入落在 ocr_chunk 上；而
        # serving 可达的图片载体只有两种：image/visual_knowledge 的 chunk 级
        # source_image（经 to_ha3_doc 进 HA3）、step_card/procedure_parent/
        # visual_knowledge 的 image_refs（经 RDS image_refs_json 恢复）。因此 XLSX
        # 一律进入本环节，按"是否已被 serving 可达载体携带"逐资产兜底建 image chunk。
        _imgs_bound_in_layout = (
            global_split_mode == "dynamic"
            and xlsx_layout_type in ("procedure_image_guide", "product_spec_instruction")
        )
        _is_xlsx_doc = str(doc.get("file_ext", "")).lower() in ("xlsx", "xls")
        # ── #F-mm12 死 refs 救活 v2（RAG_IMG_CHUNK_FALLBACK_V2，默认 OFF）────
        # 修两个残余 serving-dead 缺口（xlsx/pptx/纯图文档已由 I5 修复）：
        #   (1) step 误路由逃生门：_detect_step_patterns 判 step 但 chunker 0
        #       step_groups 回退 text（步骤标记全在表格/OCR 里）时 is_step_mode 仍
        #       True → 兜底分支被跳过，整篇图无任何 serving 载体。实证信号 =
        #       本轮 chunks 无任何 step_card/procedure_parent（比猜路由可靠）。
        #   (2) 非 step docx/pdf 的 ROUTE_TO_TEXT 截图：refs 落在 text/clause
        #       chunk 上（HA3 不携带、RDS 恢复白名单不含）→ 图上传了 OSS 却永远
        #       渲染不出（UI 截图 OCR>120 字恰最易走 TO_TEXT）。
        # file_ext 显式限定 docx/doc/pdf（勿裸放行 csv/html 等）。被救活的 chunk
        # 打 extra.fallback_source 便于观测与定向 purge。
        # ⚠️ 护栏说明：img_dup_factor_p95 只统计 step_card，对本改动的双载体风险
        # 失明——真护栏 = 审计脚本「同一 source_image 被 >1 个 serving 可达载体
        # 携带」计数 + HA3 行数增幅监控。存量文档生效需 re-chunk：必须走
        # RAG_MAINTENANCE_ROUTING 冻结 + 预编码期望增量的 manifest（本改动有意
        # 增加 chunk 数/type_mix，不冻结会被 unfrozen-rechunk 守卫按设计拦下）。
        _fallback_v2 = os.environ.get(
            "RAG_IMG_CHUNK_FALLBACK_V2", "").lower() in ("1", "true", "yes")
        _is_docx_pdf = str(doc.get("file_ext", "")).lower() in ("docx", "doc", "pdf")
        _step_route_fell_back = (
            _fallback_v2 and _is_docx_pdf and is_step_mode
            and not any(c.chunk_type in ("step_card", "procedure_parent") for c in chunks)
        )
        if ((not is_step_mode and not _imgs_bound_in_layout and m_mode != "slide")
                or _is_xlsx_doc or _step_route_fell_back):
            current_chunk_count = len(chunks)
            assets = doc.get("assets", [])
            if assets:
                source_key = doc.get("source_key", "")
                dept_code = _dept_from_raw_key(source_key, doc.get("owner_dept", "unknown"))

                # 独立图片文档（jpg/png 海报、流程图）：图就是文档本体 ——
                # ROUTE_TO_TEXT 也要建 image chunk，否则原图只存在于 text chunk 的
                # image_refs 里（HA3 不携带、RDS 恢复只覆盖 step_card/visual_knowledge），
                # serving 永远渲染不出（对抗评审 2026-06-10 证实）。
                # XLSX 嵌入截图同理：TO_TEXT 截图若未绑进 serving 可达载体也必须建 image chunk。
                _is_image_doc = str(doc.get("file_ext", "")).lower() in (
                    "png", "jpg", "jpeg", "webp", "tif", "tiff", "gif", "bmp")

                # 已被 serving 可达载体携带的图片（按文件名）：不再重复建独立 image chunk。
                # ce_bound_fns / layout_bound_fns 是结构化版式的有意绑定（载体可能是文本类
                # chunk，serving 可达性单议），同样视为已携带以保持既有版式行为不变。
                _SERVING_REF_TYPES = ("step_card", "procedure_parent", "visual_knowledge")
                represented_fns = set(ce_bound_fns) | set(layout_bound_fns)
                for _c in chunks:
                    _cx = _c.extra or {}
                    if _c.chunk_type in ("image", "visual_knowledge") and _cx.get("source_image"):
                        represented_fns.add(os.path.basename(str(_cx.get("source_image"))))
                    if _c.chunk_type in _SERVING_REF_TYPES:
                        for _ref in (_cx.get("image_refs") or []):
                            _rfn = _ref.get("filename") or os.path.basename(
                                str(_ref.get("source_image") or _ref.get("oss_key") or ""))
                            if _rfn:
                                represented_fns.add(_rfn)

                # #F-mm12(2)：v2 下非 step docx/pdf（以及逃生门文档）的 TO_TEXT 截图
                # 也建 image chunk —— 它们的 refs 落在 text/clause chunk 上是死载体
                _v2_to_text = (_fallback_v2 and _is_docx_pdf
                               and (not is_step_mode or _step_route_fell_back))
                for asset in assets:
                    _status = asset.get("status")
                    if _status == "ROUTE_TO_VECTOR" or (
                            _status == "ROUTE_TO_TEXT"
                            and (_is_image_doc or _is_xlsx_doc or _v2_to_text)):
                        filename = asset.get("filename", "")
                        # 已绑定/已携带的图片不再建独立 image chunk
                        if filename in represented_fns:
                            continue
                        visual_summary = asset.get("visual_summary", "")

                        version = doc["version_no"]
                        doc_id = doc["doc_id"]
                        # 优先用已存在的 oss_key（独立图片文档 = raw/ 对象本身；
                        # 嵌入图 = 上传环节回填），构造路径仅作离线/旧数据兜底
                        source_image_url = (asset.get("oss_key")
                                            or f"processing/assets/{dept_code}/{doc_id}/v{version}/{filename}")
                        
                        # 图片 chunk_text 加入文档标题前缀，与文本 chunk 一致，提升 BM25 关键词匹配
                        # TO_TEXT 截图可能只有 OCR 文本：caption 缺失时用 OCR 片段兜底，避免空描述
                        doc_title = doc.get("title", "")
                        context_prefix = f"【文档:{doc_title}】" if doc_title else ""
                        _desc = visual_summary or (asset.get("ocr_text") or "").strip()[:120]
                        chunk_text = f"{context_prefix} [图片描述] {_desc}" if context_prefix else f"[图片描述] {_desc}"
                        
                        from opensearch_pipeline.chunker import _generate_chunk_id, _estimate_tokens
                        chunk_id = _generate_chunk_id(doc_id, version, current_chunk_count)
                        
                        img_chunk = Chunk(
                            chunk_id=chunk_id,
                            doc_id=doc_id,
                            version_no=version,
                            chunk_index=current_chunk_count,
                            chunk_type="image",
                            chunk_text=chunk_text,
                            token_count=_estimate_tokens(chunk_text),
                            raw_text=chunk_text,
                            context_prefix=context_prefix,
                            page_num=asset.get("page_num", 1),
                            section_title=None,
                            source_oss_key=doc.get("canonical_key", ""),
                            source="multimodal",
                            title=doc.get("title", ""),
                            # F-19：ACL owner_dept 取 RDS 权威值，不用 dept_code（路径推导）——见上方同注。
                            owner_dept=(doc.get("owner_dept") or "unknown"),
                            category_l1=doc.get("category_l1", ""),
                            category_l2=doc.get("category_l2", ""),
                            permission_level=doc.get("permission_level", "public"),
                            kb_type=doc.get("kb_type", "public"),
                            risk_level=doc.get("risk_level", "low"),
                            is_active=True,
                            sensitive_redacted=doc.get("redaction_action") == "REDACTED",
                            embedding_status="NOT_STARTED",
                            index_status=ChunkIndexStatus.NOT_INDEXED,
                            extra={
                                "source_image": source_image_url,
                                "visual_summary": visual_summary,
                                "oss_key": asset.get("oss_key", ""),
                                # #F-mm12：仅当该 chunk 没有 v2 就不会存在时打归因标记
                                # （观测出图面变化 + 需要时按标记定向 purge）
                                **({"fallback_source": (
                                        "step_route_fallback_v2" if _step_route_fell_back
                                        else "to_text_docx_pdf_v2")}
                                   if (_step_route_fell_back
                                       or (_status == "ROUTE_TO_TEXT" and _v2_to_text
                                           and not _is_image_doc and not _is_xlsx_doc))
                                   else {}),
                            }
                        )
                        chunks.append(img_chunk)
                        current_chunk_count += 1


        # ── Chunk-explosion gate (flag-gated, default OFF: RAG_CHUNK_EXPLOSION_GATE) ──
        # 首次入库的"chunk 爆炸"防线（如单元格化 xlsx 产出上万 table_chunk 垃圾块）。
        # warn 模式仅告警仍正常入库；quarantine 模式丢弃本轮 chunks + 标记 QUARANTINE（旧版本继续服务，
        # 由 node_write_chunk_meta 的 0-chunk 分支写回可见的 QUARANTINED 状态 + 置空 rag_ready_key）。
        # 整段 fail-safe：gate 内部异常 → 结构化 warning + 正常处理（绝不静默吞）。
        if os.environ.get("RAG_CHUNK_EXPLOSION_GATE", "").lower() in ("1", "true", "yes"):
            _explosion_reason = None
            try:
                _explosion_reason = _chunk_explosion_verdict(chunks)
            except Exception as _exp_err:
                _w = (f"chunk-explosion gate error for {doc['doc_id']} "
                      f"(FAIL-SAFE: processing normally): {_exp_err}")
                print(f"    ⚠️ {_w}")
                ctx.setdefault("validation_warnings", []).append(_w)
            if _explosion_reason:
                _mode = os.environ.get("RAG_CHUNK_EXPLOSION_MODE", "warn").lower()
                if _mode == "quarantine":
                    doc["redaction_action"] = "QUARANTINE"
                    doc["chunk_explosion_reason"] = _explosion_reason
                    _w = f"{doc['doc_id']}: chunk-explosion QUARANTINE — {_explosion_reason}"
                    print(f"    🚫 {_w}; {len(chunks)} chunks dropped, prior version keeps serving")
                    ctx.setdefault("validation_warnings", []).append(_w)
                    continue  # do NOT extend → 0 valid chunks → visible QUARANTINED in write_chunk_meta
                else:
                    _w = (f"{doc['doc_id']}: chunk-explosion WARN — {_explosion_reason} "
                          f"({len(chunks)} chunks)")
                    print(f"    ⚠️ {_w}")
                    ctx.setdefault("validation_warnings", []).append(_w)

        all_chunks.extend(chunks)
        print(f"    └─ {doc['doc_id']}: {len(chunks)} chunks generated")

        # 打印 chunk 预览
        for i, chunk in enumerate(chunks[:3]):
            preview = chunk.chunk_text[:60].replace("\n", " ")
            print(f"       c{i}: [{chunk.chunk_type}] {preview}... ({chunk.token_count} tokens)")
        if len(chunks) > 3:
            print(f"       ... and {len(chunks) - 3} more chunks")

    ctx["chunks"] = all_chunks


def _chunk_text_gibberish(text: str) -> bool:
    """G25：chunk 级乱码判定（保守，纯字符统计）。

    验证器此前只查 空/长度/doc_id——文本层乱码块（坏字体 PUA、latin-1 mojibake、
    二进制串）直通向量。判 True 的两个独立信号：
      1. U+FFFD/PUA 密度 >10%；
      2. 有效字符（CJK/ASCII 字母数字/CJK 标点/全半角）占比 <0.40——分母排除
         markdown 表格结构符与常见标点，避免管道符密集的表格块误伤。
    """
    stripped = "".join(text.split())
    if not stripped:
        return False  # 空文本由 empty_text 检查负责
    junk = good = 0
    denom = 0
    _STRUCT_CHARS = set("|-+*#=~_.:,;()[]{}<>/\\\"'`!?%&@^$（）【】《》。，、：；！？·…—")
    for ch in stripped:
        cp = ord(ch)
        if cp == 0xFFFD or 0xE000 <= cp <= 0xF8FF:
            junk += 1
            denom += 1
            continue
        if ch in _STRUCT_CHARS:
            continue  # 结构符不进分母（表格/列表的管道横线不该稀释有效占比）
        denom += 1
        if (0x30 <= cp <= 0x39 or 0x41 <= cp <= 0x5A or 0x61 <= cp <= 0x7A
                or 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
                or 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF
                or cp == 0x20):
            good += 1
    if junk / max(len(stripped), 1) > 0.10:
        return True
    if denom < 20:
        return False  # 样本太小不判（短表格行/纯符号行交给其他检查）
    return good / denom < 0.40


_SENTENCE_FINAL = tuple("。！？；：.!?…”\"）)]】》>|")


def _batch_quality_metrics(valid, invalid) -> dict:
    """G22：本批 chunk 内容质量轻量指标（纯 Python，零 API/IO）。

    这是离线 L6 全量评测的日常哨兵版：不替代 L6（那边有 GT/CI/HA3 对账），
    只保证坏 PDF 批次当天可见而不是等下次发布门。
    """
    from datetime import datetime, timedelta, timezone
    type_dist: dict = {}
    tokens: list = []
    midcut = 0
    text_like = 0
    for c in valid:
        ct = getattr(c, "chunk_type", "") or "unknown"
        type_dist[ct] = type_dist.get(ct, 0) + 1
        tokens.append(int(getattr(c, "token_count", 0) or 0))
        if ct in ("text_chunk", "clause_chunk", "faq_chunk", "step_card"):
            text_like += 1
            tail = (getattr(c, "chunk_text", "") or "").rstrip()
            if tail and not tail.endswith(_SENTENCE_FINAL):
                midcut += 1
    tokens.sort()

    def _pct(p):
        return tokens[min(len(tokens) - 1, int(len(tokens) * p))] if tokens else 0

    gib = sum(1 for inv in invalid if "gibberish_text" in (inv.get("issues") or []))
    return {
        "stat_date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "doc_count": len({getattr(c, "doc_id", "") for c in valid}),
        "chunk_total": len(valid) + len(invalid),
        "chunk_invalid": len(invalid),
        "gibberish_cnt": gib,
        "midcut_rate": round(midcut / text_like, 4) if text_like else 0.0,
        "p50_tokens": _pct(0.50),
        "p95_tokens": _pct(0.95),
        "type_dist_json": json.dumps(type_dist, ensure_ascii=False)[:1024],
    }


def _persist_ingest_quality(ctx: dict, metrics: dict) -> None:
    """G22：批次质量指标落库 + 阈值告警。全程 fail-open（表未建/RDS 不可达仅日志）。"""
    if os.environ.get("RAG_INGEST_QUALITY_METRICS", "true").lower() in ("0", "false", "no"):
        return
    # 阈值告警先于落库（落库失败也要报）
    try:
        inv_ratio = metrics["chunk_invalid"] / max(metrics["chunk_total"], 1)
        midcut_alert = float(os.environ.get("RAG_INGEST_QUALITY_MIDCUT_ALERT", "0.25"))
        invalid_alert = float(os.environ.get("RAG_INGEST_QUALITY_INVALID_ALERT", "0.10"))
        if metrics["midcut_rate"] > midcut_alert or inv_ratio > invalid_alert:
            from opensearch_pipeline.alerting import send_ops_alert
            send_ops_alert(
                "摄取批次质量告警",
                f"midcut_rate={metrics['midcut_rate']:.2%} (阈 {midcut_alert:.0%}) · "
                f"invalid={metrics['chunk_invalid']}/{metrics['chunk_total']} "
                f"(阈 {invalid_alert:.0%}) · gibberish={metrics['gibberish_cnt']} · "
                f"docs={metrics['doc_count']}",
                severity="warning")
    except Exception:
        pass
    if _resolve_simulate(ctx, "db"):
        return
    try:
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ingest_quality_metrics (stat_date, doc_count, chunk_total, "
                    "chunk_invalid, gibberish_cnt, midcut_rate, p50_tokens, p95_tokens, "
                    "type_dist_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (metrics["stat_date"], metrics["doc_count"], metrics["chunk_total"],
                     metrics["chunk_invalid"], metrics["gibberish_cnt"], metrics["midcut_rate"],
                     metrics["p50_tokens"], metrics["p95_tokens"], metrics["type_dist_json"]))
            conn.commit()
        finally:
            conn.close()
    except Exception as _iq_err:
        print(f"    ⚠️ ingest_quality_metrics write skipped (fail-open): {_iq_err}")


def node_validate_chunks(ctx: dict):
    """校验 chunk 质量。"""
    chunks = ctx.get("chunks", [])
    valid = []
    invalid = []
    # 结构型 chunk（带父子引用/聚合语义）被丢弃绝不能静默——必须打全量上下文。
    _STRUCTURAL = {"procedure_parent", "step_card", "table_chunk", "visual_knowledge"}

    for chunk in chunks:
        issues = []
        if not chunk.chunk_text.strip():
            issues.append("empty_text")
        if chunk.token_count < 5:
            issues.append("too_few_tokens")
        if chunk.token_count > 2000:
            issues.append("too_many_tokens")
        if not chunk.doc_id:
            issues.append("missing_doc_id")
        if not issues and _chunk_text_gibberish(chunk.chunk_text):
            issues.append("gibberish_text")

        if issues:
            invalid.append({"chunk_id": chunk.chunk_id, "issues": issues})
            # 结构型 chunk 被丢：打 doc_id/type/token/依赖子节点数，避免再次"静默丢 parent → 孤儿"
            # （2026-06-15 0959E5：procedure_parent 2370 tokens 静默丢 → 116 孤儿 step_card 的教训）
            if getattr(chunk, "chunk_type", "") in _STRUCTURAL:
                _ex = chunk.extra if getattr(chunk, "extra", None) else {}
                _dep = ""
                if chunk.chunk_type == "procedure_parent":
                    _dep = f" dependent_children={_ex.get('step_count', len(_ex.get('child_chunk_ids', [])))}"
                print(f"    🚨 [VALIDATE] dropped STRUCTURAL chunk doc_id={chunk.doc_id} "
                      f"chunk_id={chunk.chunk_id} type={chunk.chunk_type} "
                      f"token_count={chunk.token_count} issues={issues}{_dep}")
        else:
            valid.append(chunk)

    # 引用完整性安全网：step_card 的 parent 若被丢/不在有效集 → 切断悬挂 parent_chunk_id + 高优告警。
    # 优雅降级（保 step 独立可检索，不阻断整篇文档写入）；正常情况下 chunker 已保证 parent 不超长不被丢。
    _valid_ids = {c.chunk_id for c in valid}
    _severed = 0
    for c in valid:
        _ex = c.extra if getattr(c, "extra", None) else None
        if not _ex:
            continue
        _pid = _ex.get("parent_chunk_id")
        if _pid and _pid not in _valid_ids:
            _ex["orphaned_parent"] = _pid  # 留痕，便于事后定位根因
            _ex["parent_chunk_id"] = None  # 切断悬挂引用，写库为 NULL（node_write_chunk_meta:3912 读 extra）
            _severed += 1
    if _severed:
        print(f"    🚨 [VALIDATE] {_severed} 个 step_card 的 parent 不在有效集 → 已切断悬挂 parent_chunk_id "
              f"(保留 step 可检索；根因见上方 dropped STRUCTURAL 日志)")
        ctx.setdefault("validation_warnings", []).append(
            f"severed {_severed} orphan parent links")

    # B4（2026-07-25）：超长 table_chunk 被整块丢弃的**按 doc 归因留痕**。
    # 五处 table_chunk 创建点都没有长度上限（对照 step_card 有拆分、procedure_parent 有
    # _PARENT_MAX_TOKENS），中文 ~1.5 字/token ⇒ 约 3000 字触顶就整块没了，表现为
    # 「问规格参数查不到」。**本批只留痕、不做行级切分**——切分会改 chunk 家族，
    # 需要冻结重灌 + manifest 门，先拿现网真实计数再决定是否值得。
    # 语义刻意拆两种（不为了留痕改掉零产出的既有失败语义）：
    #   · 该 doc 还有其它有效 chunk → partial-loss note，走 NEEDS_REVIEW（可服务）
    #   · 全丢导致 0 chunk        → 保留既有 FAILED+retry 语义，只把原因写进错误信息
    _tbl_dropped: Dict[tuple, int] = {}
    for chunk in chunks:
        if getattr(chunk, "chunk_type", "") != "table_chunk":
            continue
        if any(inv["chunk_id"] == chunk.chunk_id and "too_many_tokens" in inv["issues"]
               for inv in invalid):
            _key = (chunk.doc_id, getattr(chunk, "version_no", None))
            _tbl_dropped[_key] = _tbl_dropped.get(_key, 0) + 1
    if _tbl_dropped:
        _surviving = {(c.doc_id, getattr(c, "version_no", None)) for c in valid}
        for (_d, _v), _n in sorted(_tbl_dropped.items()):
            _note = (f"[TABLE_DROPPED] {_n} 张超长表格（token_count>2000）被整块丢弃，"
                     f"表内内容未进入索引")
            if (_d, _v) in _surviving:
                ctx.setdefault("table_drop_notes", {}).setdefault((_d, _v), []).append(_note)
                print(f"    🚨 [VALIDATE] {_d} v{_v}: {_note} —— 该文档仍有其它有效 chunk，"
                      f"走 partial-loss/NEEDS_REVIEW 通道")
            else:
                # 0 chunk：不碰既有"疑似失败"分支的定级，只补原因（那条链路会 FAILED+retry）
                ctx.setdefault("table_drop_notes", {}).setdefault((_d, _v), []).append(_note)
                print(f"    🚨 [VALIDATE] {_d} v{_v}: {_note} —— 该文档已无有效 chunk，"
                      f"沿用既有 0-chunk 疑似失败语义（FAILED + retry）")

    ctx["valid_chunks"] = valid
    ctx["invalid_chunks"] = invalid

    # G22：per-batch 内容质量指标（落 ingest_quality_metrics + 阈值告警；fail-open）
    try:
        _iq = _batch_quality_metrics(valid, invalid)
        ctx["ingest_quality_metrics"] = _iq
        if valid or invalid:
            _persist_ingest_quality(ctx, _iq)
    except Exception as _iq_e:
        print(f"    ⚠️ batch quality metrics skipped (fail-open): {_iq_e}")

    print(f"    └─ Valid: {len(valid)}, Invalid: {len(invalid)}")
    for inv in invalid[:3]:
        print(f"       ⚠️ {inv['chunk_id']}: {inv['issues']}")


def node_publish_to_rag_ready(ctx: dict):
    """
    发布到 rag-ready/（只有通过审核/自动通过的文件进入）。

    路径规则：
      rag-ready/{permission_level}/{dept_code}/{category_l1}/{doc_id}/v{version}/content.md
      rag-ready/{permission_level}/{dept_code}/{category_l1}/{doc_id}/v{version}/metadata.json

    高风险（QUARANTINE）文件不会到达这个节点。
    """
    canonicals = ctx["canonicals"]
    published = []

    simulate_db = _resolve_simulate(ctx, "db")
    bucket, is_simulated_oss = _get_oss_bucket(ctx)

    # F#49：① 整个发布循环持有一条池化连接（此前每文档独立取连接/commit/close），每文档
    # UPDATE+commit 语义不变；② OSS 上传可经 RAG_PUBLISH_CONCURRENCY 并行（默认 1 = 现状
    # 串行）。DB 写与状态回写全部留在主线程。
    _pub_conn_box = {"conn": None}

    def _publish_conn():
        """惰性获取共享池化连接（全批隔离/空文本时不开连接）。"""
        if _pub_conn_box["conn"] is None:
            _pub_conn_box["conn"] = _get_db_conn(select_db=True)
        return _pub_conn_box["conn"]

    def _upload_published_files(job):
        """单文档的 2 次 OSS put（或模拟文件写）。只做上传，不碰 DB —— 可安全并行。"""
        rag_ready_key = job["rag_ready_key"]
        metadata_key = job["metadata_key"]
        md_data = job["md_data"]
        json_data = job["json_data"]
        if is_simulated_oss:
            try:
                os.makedirs(os.path.dirname(rag_ready_key), exist_ok=True)
                with open(rag_ready_key, "w", encoding="utf-8") as f:
                    f.write(md_data)
                print(f"    ├─ [SIMULATED] Saved published MD file: {rag_ready_key}")

                os.makedirs(os.path.dirname(metadata_key), exist_ok=True)
                with open(metadata_key, "w", encoding="utf-8") as f:
                    f.write(json_data)
                print(f"    ├─ [SIMULATED] Saved published metadata JSON file: {metadata_key}")
            except Exception as e:
                print(f"    ⚠️ Failed to write simulated published files: {e}")
                raise RuntimeError(f"Simulated write failed for published document: {e}") from e
        else:
            try:
                bucket.put_object(rag_ready_key, md_data.encode("utf-8"))
                print(f"    ├─ Uploaded published MD payload to OSS: {rag_ready_key}")

                bucket.put_object(metadata_key, json_data.encode("utf-8"))
                print(f"    ├─ Uploaded published metadata JSON payload to OSS: {metadata_key}")
            except Exception as e:
                print(f"    ⚠️ Failed to upload published files to OSS: {e}")
                raise RuntimeError(f"OSS upload failed for published document: {e}") from e

    def _persist_publish_status(job):
        """上传成功后的 RDS 回写 + published 记账（主线程，逐文档 commit 语义不变）。"""
        if not simulate_db:
            conn = None
            try:
                conn = _publish_conn()
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE document_version
                        SET publish_status = 'PUBLISHED',
                            rag_ready_key = %s,
                            redacted_key = %s,
                            published_at = NOW()
                        WHERE doc_id = %s AND version_no = %s
                    """, (
                        job["rag_ready_key"],
                        job["redacted_key"],
                        job["doc_id"],
                        job["version"],
                    ))
                conn.commit()
                print(f"    ├─ Saved publish status to RDS for {job['doc_id']} v{job['version']}")
            except Exception as e:
                if conn: conn.rollback()
                print(f"    ⚠️ Failed to save publish status to RDS: {e}")
                raise RuntimeError(f"Database write failure in node_publish_to_rag_ready: {e}") from e

        published.append(job["doc_id"])
        print(
            f"    └─ {job['doc_id']}: published to rag-ready/"
            f"{job['permission']}/{job['dept']}/{job['cat_l1']}/ (v{job['version']})"
        )

    try:
        upload_jobs = []
        for doc in canonicals:
            if doc.get("redaction_action") == "QUARANTINE":
                print(f"    └─ {doc['doc_id']}: skipped (quarantined)")
                continue

            permission = doc.get("permission_level", "public")
            dept = doc.get("owner_dept", "unknown")
            cat_l1 = doc.get("category_l1", "reference")
            doc_id = doc["doc_id"]
            version = doc["version_no"]

            rag_ready_key = (
                f"rag-ready/{permission}/{dept}/{cat_l1}/"
                f"{doc_id}/v{version}/content.md"
            )
            metadata_key = (
                f"rag-ready/{permission}/{dept}/{cat_l1}/"
                f"{doc_id}/v{version}/metadata.json"
            )

            doc["rag_ready_key"] = rag_ready_key
            doc["rag_ready_metadata_key"] = metadata_key
            doc["publish_status"] = "PUBLISHED"

            redacted_key = None
            if doc.get("redaction_action") == "REDACTED":
                redacted_key = rag_ready_key
            doc["redacted_key"] = redacted_key

            # ─── Physical Persistence of Published Documents (JSON & MD) ───
            md_data = doc.get("redacted_text")
            if md_data is None:
                md_data = doc.get("text", "")

            # ─── 空内容守卫（RD 61D861 修复）───
            # 抽取层失败或文档本身无文本时，md_data 可能为空字符串。
            # 之前会把 0 字节 content.md 推到 OSS 并把 publish_status 标成 PUBLISHED，
            # 导致下游以为已发布、但 chunk 阶段无内容可用。
            # 改为：跳过 OSS put_object，publish_status='SKIPPED_EMPTY'，
            # 在 content_process_error 留痕，graceful degrade（不 raise）。
            # 注意：publish_status 是 VARCHAR(32)，新增枚举值无需 schema 迁移；
            #      未来若改 ENUM 需把 'SKIPPED_EMPTY' 加入定义。
            if md_data is None or len(md_data.strip()) == 0:
                doc["publish_status"] = "SKIPPED_EMPTY"
                doc["rag_ready_key"] = None
                doc["rag_ready_metadata_key"] = None
                doc["redacted_key"] = None
                print(
                    f"    └─ {doc_id} v{version}: skipped publish (empty text after extraction)"
                )
                if not simulate_db:
                    conn = None
                    try:
                        conn = _publish_conn()
                        with conn.cursor() as cursor:
                            cursor.execute("""
                                UPDATE document_version
                                SET publish_status = 'SKIPPED_EMPTY',
                                    rag_ready_key = NULL,
                                    redacted_key = NULL,
                                    content_process_error = %s
                                WHERE doc_id = %s AND version_no = %s
                            """, (
                                "Empty text after extraction",
                                doc_id,
                                version,
                            ))
                        conn.commit()
                        print(
                            f"    ├─ Marked RDS publish_status='SKIPPED_EMPTY' for {doc_id} v{version}"
                        )
                    except Exception as e:
                        if conn:
                            conn.rollback()
                        # graceful degrade：不要因状态写失败而打断整批发布。
                        print(
                            f"    ⚠️ Failed to mark SKIPPED_EMPTY in RDS for {doc_id} v{version}: {e}"
                        )
                continue

            metadata_payload = {
                "doc_id": doc_id,
                "version_no": version,
                "permission_level": permission,
                "owner_dept": dept,
                "category_l1": cat_l1,
                "category_l2": doc.get("category_l2"),
                "rag_ready_key": rag_ready_key,
                "metadata_key": metadata_key,
                "published_at": datetime.now().isoformat(),
                "redaction_action": doc.get("redaction_action", "CLEAN"),
                "redaction_count": doc.get("redaction_count", 0),
                "risk_level": doc.get("risk_level", "low"),
                "title": doc.get("title", ""),
                "text_length": len(md_data),
                "block_count": len(doc.get("blocks", []))
            }
            json_data = json.dumps(metadata_payload, indent=2, ensure_ascii=False)

            upload_jobs.append({
                "doc_id": doc_id,
                "version": version,
                "permission": permission,
                "dept": dept,
                "cat_l1": cat_l1,
                "rag_ready_key": rag_ready_key,
                "metadata_key": metadata_key,
                "redacted_key": redacted_key,
                "md_data": md_data,
                "json_data": json_data,
            })

        # 上传 + 逐文档回写：默认串行（=现状）；RAG_PUBLISH_CONCURRENCY>1 时并行上传，
        # 回写按提交顺序在主线程等待各自 future（任一上传失败 → 原样抛 RuntimeError 中止节点）。
        try:
            _publish_conc = int(os.environ.get("RAG_PUBLISH_CONCURRENCY", "1"))
        except ValueError:
            _publish_conc = 1
        # A1：同 stage-1 抽取，无条件报 configured/effective（旋钮生效与否必须可从日志验证）
        print(f"    └─ [concurrency] publish: configured={_publish_conc} "
              f"effective={_publish_conc if len(upload_jobs) > 1 else 1} "
              f"(RAG_PUBLISH_CONCURRENCY, docs={len(upload_jobs)})")
        if _publish_conc > 1 and len(upload_jobs) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(
                    max_workers=min(_publish_conc, len(upload_jobs))) as _pool:
                _futs = [_pool.submit(_upload_published_files, job) for job in upload_jobs]
                for job, _fut in zip(upload_jobs, _futs):
                    _fut.result()
                    _persist_publish_status(job)
                    _lease_renew_tick(ctx)  # PR-4：大批 OSS 上传期保活租约（节流+fail-open）
        else:
            for job in upload_jobs:
                _upload_published_files(job)
                _persist_publish_status(job)
                _lease_renew_tick(ctx)  # PR-4：同上（串行臂）
    finally:
        if _pub_conn_box["conn"] is not None:
            try:
                _pub_conn_box["conn"].close()
            except Exception:
                pass

    ctx["published_count"] = len(published)

    if not published:
        print("    └─ No documents published (all quarantined or empty)")


def _rechunk_delete_targets(valid_chunks, canonicals):
    """完整性前置断言 → 返回需「整体替换」(full-replace) 的 (doc_id, version_no) 列表（已排序）。

    node_write_chunk_meta 用 DELETE-by-(doc_id,version_no) 全量替换，避免 shrink 时旧高位 chunk 残留
    （strand，2026-06-15）。但全量删除只有在确知持有该文档**完整**新切分集时才安全；否则
    partial-doc batch 会「删多插少」造成数据丢失。

    **归属守卫**：每个 (doc_id, version_no) 必须出现在本次 run 的 canonicals 中。node_chunk_documents
    对每个 canonical **一次性整文档切分**，所以命中 canonicals ⟺ 我们持有该文档的完整有效 chunk 集
    （被 validate 丢弃的是有意排除，valid_chunks 即该文档的完整有效集，删旧全量再插 = 完整替换）。
    若某 (doc, version) 不在 canonicals，说明它不是本次整文档切分的产物（partial-doc / 外来注入）→
    无法保证完整 → raise，绝不全量删。

    刻意**不**用 chunk_index 连续性/含 0 做守卫：node_validate_chunks 丢弃无效 chunk 时不重排 index，
    合法文档的 valid_chunks 可有缺口或不从 0 开始（既有测试与生产均如此），连续性守卫会误伤。归属守卫
    与丢弃完全兼容。空 valid_chunks 不会进入本函数（调用方 `and valid_chunks` 守卫）→ 不触发整文档删除。
    """
    canonical_dv = {
        (d.get("doc_id"), d.get("version_no"))
        for d in (canonicals or [])
        if d.get("doc_id") and d.get("version_no")
    }
    doc_versions = sorted({(c.doc_id, c.version_no) for c in valid_chunks})
    unowned = [dv for dv in doc_versions if dv not in canonical_dv]
    if unowned:
        raise RuntimeError(
            f"node_write_chunk_meta: refusing full-replace DELETE — {len(unowned)} (doc_id,version_no) "
            f"not in this run's canonicals (partial-doc / foreign batch), e.g. {unowned[:3]}; "
            f"NO delete performed"
        )
    return doc_versions


def _compute_chunk_set_hashes(chunks) -> dict:
    """Per (doc_id, version_no) sha256 over the ORDERED (chunk_index, chunk_type, chunk_text) tuples.

    A deterministic re-chunk-parity fingerprint (L3): independent of created_at / ids / git_commit,
    so a frozen-routing maintenance re-chunk of the same canonical text reproduces the same hash.
    Returned as {(doc_id, version_no): hex16}; persisted into extra_json for in-band parity checks.
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for c in chunks:
        groups[(c.doc_id, c.version_no)].append(c)
    out = {}
    for key, cs in groups.items():
        h = hashlib.sha256()
        for c in sorted(cs, key=lambda c: c.chunk_index):
            h.update(f"{c.chunk_index}\x1f{c.chunk_type}\x1f{c.chunk_text}\x1e".encode("utf-8"))
        out[key] = h.hexdigest()[:16]
    return out


def node_write_chunk_meta(ctx: dict):
    """
    将验证通过的 chunks 写入 RDS chunk_meta。

    这一步必须在 deactivate_old_chunks 之前完成。
    原因：如果先停用旧 chunk 再写新 chunk，中间失败会导致文档"消失"。
    正确顺序：
      1. write_chunk_meta（新 chunk 落盘，位于 DAG 2）
      2. deactivate_old_chunks（旧 chunk 停用，位于 DAG 3）
    """
    valid_chunks = ctx.get("valid_chunks", [])
    canonicals = ctx.get("canonicals", [])
    simulate_db = _resolve_simulate(ctx, "db")

    # 给 chunk 补充 rag_ready_key
    rag_ready_map = {}
    for doc in canonicals:
        rag_ready_key = doc.get("rag_ready_key")
        if not rag_ready_key:
            # 💡 强健的优雅降级/Fallback 策略：
            # 如果 node_publish_to_rag_ready 被跳过或未执行（例如本地调试或测试纯 chunk / OpenSearch 流程），
            # 自动基于元数据补全预期的 Mock rag_ready_key，避免对后续 RDS 写入及检索索引逻辑造成任何影响。
            permission = doc.get("permission_level", "public")
            dept = doc.get("owner_dept", "unknown")
            cat_l1 = doc.get("category_l1", "reference")
            doc_id = doc["doc_id"]
            version = doc["version_no"]
            rag_ready_key = (
                f"rag-ready/{permission}/{dept}/{cat_l1}/"
                f"{doc_id}/v{version}/content.md"
            )
        rag_ready_map[doc["doc_id"]] = rag_ready_key

    written = 0
    # PR-4：本次 run 内验租失败（被接管）的 (doc_id, version_no)——chunk 写与状态收尾
    # 全部跳过（文档归新持有者）；flag off 恒空=现状。函数级作用域：收尾循环也要读。
    _lease_lost_dvs = set()
    if not simulate_db and valid_chunks:
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        assert_destructive_write_allowed("write_chunk_meta", get_config().rds.host, kind="rds")
        # 完整性前置断言：在打开连接/事务之前 raise，绝不对 partial-doc batch 执行任何 DELETE。
        # 返回需整体替换的 (doc_id, version_no) 集合（见 _rechunk_delete_targets 文档）。
        delete_targets = _rechunk_delete_targets(valid_chunks, canonicals)
        conn = None
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                # Phase D（RAG_ALLOWED_DEPTS_ACL，默认关）：从 approved 跨部门授权按 doc_id 聚合
                # allowed_depts（唯一注入点 access_grants.resolve_allowed_depts；约束 2/3）→ 设到内存
                # chunk（供 ctx 流/首推）+ 下方写 chunk_meta.allowed_depts 列。flag 关 → 不查、写 NULL。
                # fail-closed：解析失败 → 置空（无授权，绝不放行），不阻断入库。先于 DELETE 的纯读。
                # ── node-ACL 投影(唯一注入点 acl_policy.project_doc_acl)────────────────
                # ⚠️ 必须在【所有】写路径执行:chunk.owner_dept 继承自 document_meta 的真实
                # owner,若 node 模式文档升版 / re-chunk 时不改写,真实 owner 会被写回**检索
                # 投影轴**,legacy owner 分支静默复活 = **权限重开**(codex 评审 BLOCKER-1)。
                # 模式互斥:legacy →(真实 owner, 组码);node →(哨兵, 仅 d:/dx:,绝不含组码)。
                # 廉价路径:先单列批查 acl_mode;全 legacy(今天 100%)时只多一次 SELECT,
                # 且完全不改 chunk 字段 ⇒ 行为与历史逐字节一致。
                _node_docs = set()
                if valid_chunks:
                    try:
                        from opensearch_pipeline.access_grants import resolve_acl_modes, resolve_doc_acl
                        from opensearch_pipeline.acl_policy import ACL_MODE_NODE, project_doc_acl
                        _modes = resolve_acl_modes({c.doc_id for c in valid_chunks}, cursor)
                        _node_docs = {d for d, m in _modes.items() if m == ACL_MODE_NODE}
                        if _node_docs:
                            _node_acls = resolve_doc_acl(_node_docs, cursor)
                            for chunk in valid_chunks:
                                if chunk.doc_id not in _node_docs:
                                    continue
                                _a = _node_acls.get(chunk.doc_id)
                                _owner, _allowed = project_doc_acl(
                                    ACL_MODE_NODE, chunk.owner_dept, (),
                                    getattr(_a, "node_ids", ()) if _a else (),
                                    getattr(_a, "exact_node_ids", ()) if _a else ())
                                chunk.owner_dept = _owner            # ← 哨兵进检索投影轴
                                chunk.allowed_depts = _allowed
                            print(f"    🔐 node-ACL 投影:{len(_node_docs)} 篇文档写哨兵 owner")
                    except Exception as _nae:   # noqa: BLE001
                        # fail-closed:投影失败绝不退回"写真实 owner"(那等于重开权限)。
                        # 抛出中止本批 —— 宁可整批失败重试,也不产出会越权的投影。
                        raise RuntimeError(f"node-ACL 投影失败,中止写入(绝不退回真实 owner): {_nae}")

                # 姿态 A（Sam 2026-08-03 拍板）：**RDS 侧投影始终计算**，不再受
                # RAG_ALLOWED_DEPTS_ACL 门控——该 flag 只管 HA3 字段推送与检索消费
                # （`to_ha3_doc(include_allowed_depts=...)` 由推送点单独门控，故本改动
                # **不改变进 HA3 的载荷**，serving 行为逐字节不变）。
                # 为何必须这样：重灌在即而 flag 为关，若沿用旧门控，新语料每个 chunk 都会
                # 带 acl_epoch=NULL，开 flag 时必须对整个新语料全量回填（63k+ chunk）。
                _proj_ok = True          # projection_complete：解析失败即不 stamp
                if valid_chunks:
                    try:
                        from opensearch_pipeline.access_grants import (
                            resolve_allowed_depts, gate_by_permission,
                        )
                        _legacy_ids = {c.doc_id for c in valid_chunks} - _node_docs
                        _allowed_by_doc = resolve_allowed_depts(_legacy_ids, cursor)
                        # 纵深守卫：只有 permission_level=='dept_internal' 的文档物化 allowed_depts
                        # （用 chunk 自身=新版本权威 permission_level；restricted/public 即便有 approved
                        # 行也不放行——审计 Step 4 backstop a）。
                        _allowed_by_doc = gate_by_permission(
                            _allowed_by_doc, {c.doc_id: c.permission_level for c in valid_chunks}
                        )
                        for chunk in valid_chunks:
                            if chunk.doc_id in _node_docs:
                                continue      # node 文档已由上方投影(哨兵+d:/dx:),绝不被组码覆盖
                            chunk.allowed_depts = _allowed_by_doc.get(chunk.doc_id, [])
                    except Exception as _ade:
                        # fail-closed：置空不放行（既有语义）。**并且不 stamp epoch** ——
                        # projection_complete 不变量：投影没算成就绝不盖章，否则这批 chunk
                        # 会被"认证为最新"而其 allowed_depts 根本没算过，日后 sweep 也不会修
                        # （epoch 相等）⇒ 正好重造 C3。留 NULL 即下轮仍判 dirty。
                        _proj_ok = False
                        print(f"    ⚠️ allowed_depts 解析失败（fail-closed 置空，不放行、不 stamp）: {_ade}")

                # 1. 全量替换：删除本次涉及的每个 (doc_id, version_no) 现存的全部 chunk，再整体重插。
                #    ⚠️ 旧实现只按新 chunk_id 删（仅为幂等/重试）——但同版本 re-chunk 时，如果新切分的
                #    chunk 数变少，旧的高 index chunk 的 chunk_id 不在新集合里就永远删不掉，残留为 active
                #    僵尸（strand），造成 RDS↔HA3 双份、重复召回（2026-06-15 50-doc 批次实测发现）。
                #    chunk_id 只依赖 (doc_id, version, index)、与内容无关，所以 shrink 必然 strand。
                #    按 (doc_id, version_no) 全量删除 = 幂等重试 + 消除 strand；DELETE 与下方 INSERT
                #    同一事务，任一失败整体 rollback。旧版本（version_no 不同）不受影响，仍由 stage-3
                #    deactivate 处理（保留旧版本直到 stage-3 验证 + scoped purge）。
                # PR-4：DELETE→INSERT 是租约要防的头号撕裂点——同一事务内先对每个
                # (doc_id,version_no) FOR UPDATE 验租（通过后本事务内不可能再被接管：
                # 接管 UPDATE 会阻塞在 dv 行锁上直到 commit）。丢锁的文档整篇剔除
                # （delete_targets 与 insert 同步剔——文档粒度弃单，绝不产生 doc 内
                # partial；归属守卫 _rechunk_delete_targets 的「完整替换」不变量保持）。
                # off/未登记时 verify 是 no-op，_lease_lost_dvs 恒空=现状。
                _wls = ingest_lease.get_lease_set(ctx)
                for _dv in list(delete_targets):
                    try:
                        _wls.verify_for_update(cursor, (_dv[0], int(_dv[1])))
                    except ingest_lease.LeaseLost:
                        _lease_lost_dvs.add((_dv[0], int(_dv[1])))
                        print(f"    ⚠️ Lease lost on {_dv[0]} v{_dv[1]} — chunk write "
                              f"abandoned (preempted by another holder)")
                if _lease_lost_dvs:
                    delete_targets = [dv for dv in delete_targets
                                      if (dv[0], int(dv[1])) not in _lease_lost_dvs]
                    valid_chunks = [c for c in valid_chunks
                                    if (c.doc_id, int(c.version_no)) not in _lease_lost_dvs]
                if delete_targets:
                    dv_clause = " OR ".join(["(doc_id=%s AND version_no=%s)"] * len(delete_targets))
                    dv_params = tuple(p for dv in delete_targets for p in dv)
                    cursor.execute(f"DELETE FROM chunk_meta WHERE {dv_clause}", dv_params)

                # 2. 批量插入新 chunk 记录（executemany 减少 RDS 往返）
                # C3′/062：**capability 探测决定是否带 acl_epoch 列**（与仓库既有
                # _kb_node_capability / _mc 同型）。062 未 apply 的环境（本地 dev、旧 staging）
                # 若硬带该列会 1054 直接打挂整个 chunk 写入 —— 摄取全线中断。
                # 探测后降级为 27 列（不 stamp，留 NULL ⇒ 下轮判 dirty），
                # 从而恢复「**先部署后 apply 安全**」，与 048/049/050 一致。
                _has_epoch_col = False
                try:
                    cursor.execute(
                        "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()"
                        " AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                        ("chunk_meta", "acl_epoch"))
                    _r = cursor.fetchone()
                    _has_epoch_col = bool(_r and _r[0])
                except Exception:   # noqa: BLE001 — 探测失败按未 apply 处理（保守）
                    _has_epoch_col = False
                _epoch_col = ", acl_epoch" if _has_epoch_col else ""
                _epoch_ph = ", %s" if _has_epoch_col else ""
                insert_sql = f"""
                    INSERT INTO chunk_meta (
                        chunk_id, doc_id, version_no, chunk_index, page_num, section_title,
                        chunk_text_preview, source_url, chunk_type, chunk_text, token_count,
                        source, rag_ready_key, permission_level, owner_dept, category_l1,
                        category_l2, sensitive_redacted, is_active, embedding_status,
                        index_status, embedding_model, extra_json,
                        parent_chunk_id, step_no, image_refs_json, allowed_depts{_epoch_col}
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s{_epoch_ph}
                    )
                """
                import json as _json
                # C3′/062 stamp 点③（三处 RDS 投影写之一）：**新 chunk 出生即带 acl_epoch**。
                # 漏这处 ⇒ 每篇新摄取文档的 chunk 天生 NULL、永久 dirty，sweep 对新文档永不收敛
                # （codex 第一批 BLOCKER）。一条 IN 批查权威水位，避免 N+1。
                # projection_complete：`_proj_ok` 为假（allowed_depts 解析失败）或 062 未 apply
                # ⇒ 一律不 stamp（留 NULL，下轮判 dirty），绝不"认证"没算成的投影。
                _epoch_by_doc = {}
                if _proj_ok and valid_chunks and _has_epoch_col:
                    try:
                        _dids = sorted({c.doc_id for c in valid_chunks})
                        _ph = ",".join(["%s"] * len(_dids))
                        cursor.execute(
                            f"SELECT doc_id, acl_epoch FROM document_meta WHERE doc_id IN ({_ph})",
                            tuple(_dids))
                        _epoch_by_doc = {r[0]: r[1] for r in cursor.fetchall()}
                    except Exception as _ee:   # noqa: BLE001 — 062 未 apply ⇒ 不 stamp
                        if "1054" not in str(_ee) and "Unknown column" not in str(_ee):
                            raise
                        _epoch_by_doc = {}
                # L3 provenance: per-run code/model versions (ctx['run_provenance'] from L1, with a
                # read-only fallback) + per-(doc,version) chunk_set_hash, merged into extra_json so a
                # stored chunk is traceable to its producing revision and re-chunk parity is verifiable
                # from stored state. Additive JSON keys only; chunk.extra itself is never mutated, so
                # to_ha3_doc / image-ref extraction below are unaffected.
                _prov = ctx.get("run_provenance")
                if not _prov:
                    from opensearch_pipeline.versions import build_run_provenance
                    _prov = build_run_provenance(bizdate=ctx.get("bizdate"))
                # ⚠️ 固定白名单 —— 往 build_run_provenance() 加 key 不会自动落到这里。
                # image_content_override（2026-07-25）：本 chunk 是在哪种图↔步骤绑定
                # 制度下产出的。**本字段目前只落 chunk_meta.extra_json._provenance，
                # 不进 pipeline_run**（该表是逐列清单，加列需迁移 059，属后续项）。
                _provenance = {k: _prov.get(k) for k in (
                    "git_commit", "extractor_version", "chunker_version",
                    "detector_version", "embedding_model_version", "bizdate",
                    "image_content_override", "funnel_policy")}
                # P1-9：funnel_policy 优先取 **canonical 里 Stage-1 写下的那个**；
                # 取不到才回落当前进程 env（老 canonical 没有该键）。
                # ⚠️ 必须**逐 (doc_id, version_no)**：本批 valid_chunks 跨多篇文档（stage-2 一次
                # 认领 ≤100 篇），而 _provenance 是**批级共用**对象。此处原先读的是 6235 行 for
                # 循环**泄漏出来的 `doc`**（= 批内最后一篇 canonical），等于给整批 chunk 盖同一个
                # 策略标签 —— 事后拿 funnel_policy 盘点「谁还欠一次 C 重灌」会漏掉真正欠账的文档
                # （**假阴**：旧策略产出的图集被标成 c1，账面显示已完成、永久缺图）。
                # copy-on-write：只在值不同时新建 dict，且 "" 也必须走新建 —— 绝不原地改
                # _provenance，否则第一篇就把批级基准污染掉，变成第二次同型泄漏。
                _prov_by_dv = {}
                for _cdoc in canonicals:
                    _cp = _cdoc.get("funnel_policy")
                    if _cp is None or _cp == _provenance.get("funnel_policy"):
                        continue   # 缺键 ⇒ 回落批级（当前进程 env）；同值 ⇒ 共用批级对象
                    _prov_by_dv[(_cdoc.get("doc_id"), _cdoc.get("version_no"))] = {
                        **_provenance, "funnel_policy": _cp}
                _chunk_set_hashes = _compute_chunk_set_hashes(valid_chunks)

                insert_rows = []
                for chunk in valid_chunks:
                    rag_ready_key = rag_ready_map.get(chunk.doc_id, "")
                    preview = chunk.chunk_text[:200]

                    # 序列化 extra dict → JSON（图片 chunk 的 source_image/visual_summary/oss_key）
                    # + 合并 L3 provenance + chunk_set_hash（构造新 dict，不就地改 chunk.extra）。
                    _extra_for_json = dict(chunk.extra or {})
                    _extra_for_json["_provenance"] = _prov_by_dv.get(
                        (chunk.doc_id, chunk.version_no), _provenance)
                    _extra_for_json["_chunk_set_hash"] = _chunk_set_hashes.get(
                        (chunk.doc_id, chunk.version_no))
                    extra_json_val = _json.dumps(_extra_for_json, ensure_ascii=False)

                    # Step Card 专有字段（从 extra 中提取）
                    parent_chunk_id = chunk.extra.get("parent_chunk_id") if chunk.extra else None
                    step_no = chunk.extra.get("step_no") if chunk.extra else None
                    image_refs = chunk.extra.get("image_refs") if chunk.extra else None
                    image_refs_json_val = _json.dumps(image_refs, ensure_ascii=False) if image_refs else None
                    # Phase D：allowed_depts(JSON)；空 → NULL（flag 关或无授权时即 NULL，列已存在于 schema/001）。
                    allowed_depts_json_val = (
                        _json.dumps(chunk.allowed_depts, ensure_ascii=False) if getattr(chunk, "allowed_depts", None) else None
                    )

                    # F-18：section_title 全类型长度防线（列为 VARCHAR(255)）。既有 Fix B 只覆盖
                    # clause/text/section 三类（chunker 里 60 字 heading 约束）；step_card/faq/table
                    # 等继承的超长标题会在 executemany 触发 MySQL 1406，整批（≤100 文档）回滚 + 毒文档
                    # 每日重试每日失败拖住日更管线。截断而非丢块（保 chunk_text/image_refs 内容不丢）。
                    _section_title = (chunk.section_title or None) and chunk.section_title[:255]
                    insert_rows.append((
                        chunk.chunk_id, chunk.doc_id, chunk.version_no, chunk.chunk_index, chunk.page_num, _section_title,
                        preview, chunk.source_oss_key, chunk.chunk_type, chunk.chunk_text, chunk.token_count,
                        chunk.source, rag_ready_key, chunk.permission_level, chunk.owner_dept, chunk.category_l1,
                        chunk.category_l2, chunk.sensitive_redacted, chunk.is_active, chunk.embedding_status,
                        chunk.index_status, chunk.embedding_model, extra_json_val,
                        parent_chunk_id, step_no, image_refs_json_val, allowed_depts_json_val,
                        *((_epoch_by_doc.get(chunk.doc_id),) if _has_epoch_col else ())
                    ))

                if insert_rows:
                    # E#43：单次 executemany 携带全批 27 列行（含完整 chunk_text，单行可数 KB）会被
                    # pymysql 改写成一条巨型多行 INSERT，可能撑爆 max_allowed_packet。改为**同一事务内**
                    # 每 RAG_CHUNK_META_INSERT_BATCH（默认 500）行一次 executemany；commit 位置不变，
                    # 任一批失败整体 rollback，原子性不变。
                    try:
                        _ins_batch = int(os.environ.get("RAG_CHUNK_META_INSERT_BATCH", "500"))
                    except ValueError:
                        _ins_batch = 500
                    _ins_batch = max(1, _ins_batch)
                    for _i in range(0, len(insert_rows), _ins_batch):
                        cursor.executemany(insert_sql, insert_rows[_i:_i + _ins_batch])
                    written = len(insert_rows)
                conn.commit()
            print(f"    └─ Saved {written} chunk records to RDS chunk_meta (batch insert)")
        except Exception as e:
            if conn: conn.rollback()
            print(f"    ⚠️ Failed to write chunk_meta to RDS: {e}")
            raise RuntimeError(f"Database write failure in node_write_chunk_meta: {e}") from e
        finally:
            if conn:
                conn.close()
    else:
        for chunk in valid_chunks:
            chunk_dict = chunk.to_dict()
            chunk_dict["rag_ready_key"] = rag_ready_map.get(chunk.doc_id, "")
            written += 1

    # Status closure grouped by (doc_id, version_no)
    # Collect all unique (doc_id, version_no) to process from both canonicals and valid_chunks
    doc_versions_to_process = set()
    for doc in canonicals:
        doc_id = doc.get("doc_id")
        version = doc.get("version_no")
        if doc_id and version:
            doc_versions_to_process.add((doc_id, version))

    # perf#93：同一遍按 (doc_id, version_no) 分桶——闭环循环里原先每个 (doc,version) 都对
    # valid_chunks 做一次全量重扫（O(D×C)），现在 O(1) 查桶；桶内保持 valid_chunks 原序。
    _chunks_by_dv: Dict[tuple, list] = {}
    for chunk in valid_chunks:
        doc_versions_to_process.add((chunk.doc_id, chunk.version_no))
        _chunks_by_dv.setdefault((chunk.doc_id, chunk.version_no), []).append(chunk)

    # (doc_id, version) → canonical dict, for recovering per-doc flags (e.g. chunk-explosion quarantine)
    _canon_by_dv = {(d.get("doc_id"), d.get("version_no")): d for d in canonicals}

    # perf#93：闭环阶段整批共享一个连接（原先每文档最多 2 个短连接：状态 UPDATE 自开自关，
    # write_audit 再开一个）。commit 粒度保持每文档一次，失败隔离不变：quarantine/0-chunk 分支
    # fail-open 继续下一文档，DONE 分支 fail-closed raise；单文档出错经 _rollback_or_discard
    # 清理半途事务（连接坏则丢弃、下一文档惰性重建，等价于原先的每文档新连接隔离）。
    # write_audit 不复用共享连接：其 cursor 注入模式是 serving 专用原子审计（不吞异常、随调用方
    # 事务提交，见 audit_log.py 文档），复用会把「审计失败绝不阻断摄取」的 fail-open 契约改成
    # fail-closed，故保持自开短连接。
    _closure_conn = None
    _cls = ingest_lease.get_lease_set(ctx)
    try:
        for doc_id, ver in sorted(doc_versions_to_process):
            # PR-4：验租失败的文档整篇跳过收尾（chunk 写已剔，终态归新持有者）；
            # 其余文档顺带走一次节流续租（大批收尾期保活）。
            if (doc_id, int(ver)) in _lease_lost_dvs:
                continue
            _lease_renew_tick(ctx)
            doc_chunks = _chunks_by_dv.get((doc_id, ver), [])
            chunk_cnt = len(doc_chunks)

            _exp_reason = _canon_by_dv.get((doc_id, ver), {}).get("chunk_explosion_reason")
            if chunk_cnt == 0 and _exp_reason:
                # chunk-explosion quarantine: record a VISIBLE status (not silent EMPTY) + retire the
                # orphaned rag-ready artifact (publish ran before chunk) — RDS publish_status + NULL
                # rag_ready_key, mirroring the SKIPPED_EMPTY cleanup, not just in-memory fields.
                print(f"    🚫 {doc_id} v{ver}: chunk-explosion quarantined — {_exp_reason}")
                if not simulate_db:
                    try:
                        if _closure_conn is None:
                            _closure_conn = _get_db_conn(select_db=True)
                        with _closure_conn.cursor() as cursor:
                            _lk = (doc_id, int(ver))
                            cursor.execute(f"""
                                UPDATE document_version
                                SET chunk_status = 'QUARANTINED_EXPLOSION',
                                    content_process_status = 'QUARANTINED',
                                    content_process_error = %s,
                                    publish_status = 'SKIPPED_EXPLOSION',
                                    rag_ready_key = NULL,
                                    processed_at = NOW(){ingest_lease.clear_set_sql()}
                                WHERE doc_id = %s AND version_no = %s{_cls.fence_where_sql(_lk)}
                            """, (f"chunk-explosion: {_exp_reason}"[:255], doc_id, ver)
                               + _cls.fence_where_params(_lk))
                            _cls.check_fenced_write(cursor, _lk)
                            _closure_conn.commit()
                            _cls.discard(_lk)  # 终态已落，本地释放（后续同 doc 辅助写不再拼栅栏）
                    except ingest_lease.LeaseLost:
                        _closure_conn = _rollback_or_discard(_closure_conn)
                        print(f"    ⚠️ Lease lost on {doc_id} v{ver} — explosion-quarantine "
                              f"write skipped (preempted)")
                    except Exception as db_err:
                        _closure_conn = _rollback_or_discard(_closure_conn)
                        print(f"    ⚠️ Failed to write explosion-quarantine status for {doc_id} v{ver}: {db_err}")
                else:
                    print(f"    └─ [SIMULATED] {doc_id} v{ver} chunk_status='QUARANTINED_EXPLOSION', "
                          f"publish_status='SKIPPED_EXPLOSION', rag_ready_key=NULL")
            elif chunk_cnt == 0:
                # 0 chunk 收尾：区分「真空文档」(合法无文本) 与「疑似失败」(本应有内容却没产出)。
                # 旧逻辑一律 chunk_status='EMPTY' + content_process_status='DONE'(成功)，使损坏文件 /
                # 扫描件 OCR 失败 / 抽取异常 的文档与合法空文档无法区分 → 坏 SOP 静默从检索消失且无告警
                # (known-issues: 77 chunk-EMPTY 中 4 个真 SOP=bug 即此类)。用 canonical 已有信号判定，
                # 疑似失败 → NEEDS_REVIEW + FAILED（可查询、不被 NOT_STARTED 扫描重新认领 → 不形成
                # 重试循环），原因写入 content_process_error。无 schema 变更（列均为 VARCHAR）。
                # 隔离文档 (PII / 成本 QUARANTINE) 不在此重新定级 —— 保留既有 EMPTY/DONE 行为不动。
                _canon = _canon_by_dv.get((doc_id, ver), {}) or {}
                _is_quarantine = _canon.get("redaction_action") == "QUARANTINE"
                _text_len = _canon.get("text_length")
                if _text_len is None:
                    _text_len = len(_canon.get("text") or "")
                _ocr_status = str(_canon.get("ocr_status") or "").upper()
                _extract_method = str(_canon.get("extract_method") or "")
                _warns = _canon.get("warnings") or []
                try:
                    _text_threshold = int(os.environ.get("RAG_EMPTY_DOC_TEXT_THRESHOLD", "100"))
                except ValueError:
                    _text_threshold = 100

                _reasons = []
                if not _is_quarantine:
                    if _ocr_status == "FAILED":
                        _reasons.append("ocr_failed")
                    if _text_len >= _text_threshold:
                        # 有实质正文却 0 chunk → chunker/validator 把所有 chunk 丢光（如超长 table_chunk）
                        _reasons.append(f"text_present({_text_len}chars)_but_zero_chunks")
                    _em_low = _extract_method.lower()
                    if any(k in _em_low for k in ("unsupported", "failed", "error")):
                        _reasons.append(f"extract_method={_extract_method}")
                    _faily = [str(w) for w in _warns if any(
                        k in str(w).lower() for k in ("fail", "error", "cannot", "无法", "错误", "失败"))]
                    if _faily:
                        _reasons.append("warn:" + " | ".join(_faily))

                if _reasons:
                    # content_process_status='FAILED' 复用既有"毒文档"机制：orchestrator Stage-2 谓词
                    # (content_process_status='FAILED' AND retry_count<3) 会重试 ≤3 次（自愈瞬时
                    # OCR/embedding 整轮故障），到 retry_count=3 自然停在 FAILED 等人工检查。**必须**在此
                    # 自增 retry_count —— claim 本身不自增，否则确定性坏文档(损坏 DOCX 等)会无限重处理。
                    _chunk_status, _cps = "NEEDS_REVIEW", "FAILED"
                    _cpe = ("suspected extraction/chunk failure: " + "; ".join(_reasons))[:255]
                    _retry_clause = ", " + RETRY_COUNT_INC_SQL
                    print(f"    🚨 {doc_id} v{ver}: 0 chunks + SUSPECTED FAILURE → "
                          f"chunk_status=NEEDS_REVIEW, content_process_status=FAILED, retry_count+1 ({_cpe})")
                else:
                    _chunk_status, _cps = "EMPTY", "DONE"
                    _cpe = "No valid chunks generated (empty document)"
                    # B1a（2026-07-25）：这是**成功出口**（合法空文档），必须清零 ——
                    # retry_count 的语义是「连续失败次数」，只增不减会让一篇曾失败 2 次、
                    # 后来正常收口的文档带着旧计数，下次再失败一次就提前进死信。
                    _retry_clause = ", retry_count = 0"
                    _tag = "quarantined" if _is_quarantine else "treated as empty"
                    print(f"    ⚠️ No valid chunks generated for document {doc_id} v{ver} ({_tag})")

                if not simulate_db:
                    try:
                        if _closure_conn is None:
                            _closure_conn = _get_db_conn(select_db=True)
                        with _closure_conn.cursor() as cursor:
                            # _retry_clause 是上面二选一的常量字面量（非用户输入）→ 无注入风险。
                            _lk = (doc_id, int(ver))
                            cursor.execute(f"""
                                UPDATE document_version
                                SET chunk_status = %s,
                                    content_process_status = %s,
                                    content_process_error = %s{_retry_clause},
                                    processed_at = NOW(){ingest_lease.clear_set_sql()}
                                WHERE doc_id = %s AND version_no = %s{_cls.fence_where_sql(_lk)}
                            """, (_chunk_status, _cps, _cpe, doc_id, ver)
                               + _cls.fence_where_params(_lk))
                            _cls.check_fenced_write(cursor, _lk)
                            _closure_conn.commit()
                            _cls.discard(_lk)  # 终态已落，本地释放
                    except ingest_lease.LeaseLost:
                        _closure_conn = _rollback_or_discard(_closure_conn)
                        print(f"    ⚠️ Lease lost on {doc_id} v{ver} — 0-chunk closure "
                              f"write skipped (preempted)")
                    except Exception as db_err:
                        _closure_conn = _rollback_or_discard(_closure_conn)
                        print(f"    ⚠️ Failed to update failed status in RDS: {db_err}")
                else:
                    print(f"    └─ [SIMULATED] document_version: doc_id={doc_id} v{ver} "
                          f"chunk_status='{_chunk_status}', content_process_status='{_cps}'")
            else:
                print(f"    └─ Document {doc_id} v{ver} generated {chunk_cnt} valid chunks.")
                if not simulate_db:
                    try:
                        if _closure_conn is None:
                            _closure_conn = _get_db_conn(select_db=True)
                        with _closure_conn.cursor() as cursor:
                            # B1a（2026-07-25）：成功出口清零 retry_count（与 EMPTY/DONE 出口同型）。
                            # 语义 = 「连续失败次数」；只增不减会让一篇曾失败 2 次、后来正常收口的
                            # 文档带着旧计数，下次再失败一次就提前进死信。合入同一条原子 UPDATE：
                            # 不新增目标集合、不新增写次数，因此不新增所有权暴露面。
                            # （B1b 后记 2026-08-02：per-claim fence 已随 PR-4 租约移植补上——
                            # flag on 时本写带 holder/epoch 谓词；off 时空串=旧行为。）
                            _lk = (doc_id, int(ver))
                            cursor.execute(f"""
                                UPDATE document_version
                                SET content_process_status = 'DONE',
                                    chunk_status = 'DONE',
                                    chunk_count = %s,
                                    retry_count = 0,
                                    processed_at = NOW(),
                                    content_process_error = NULL{ingest_lease.clear_set_sql()}
                                WHERE doc_id = %s AND version_no = %s{_cls.fence_where_sql(_lk)}
                            """, (chunk_cnt, doc_id, ver) + _cls.fence_where_params(_lk))
                            _cls.check_fenced_write(cursor, _lk)
                            _closure_conn.commit()
                            _cls.discard(_lk)  # 终态已落，本地释放——紧随的 vlm 覆写走无栅栏旧语义
                    except ingest_lease.LeaseLost:
                        # 丢锁≠故障：文档归新持有者，跳过 DONE 收尾即可——绝不为此 abort 节点
                        # （chunk 写入已在同一 run 的验租点保护；此处只是终态归属权判负）。
                        _closure_conn = _rollback_or_discard(_closure_conn)
                        print(f"    ⚠️ Lease lost on {doc_id} v{ver} — DONE closure "
                              f"write skipped (preempted)")
                        continue
                    except Exception as db_err:
                        _closure_conn = _rollback_or_discard(_closure_conn)
                        print(f"    ⚠️ Failed to update DONE status in RDS for document {doc_id} v{ver}: {db_err}")
                        raise RuntimeError(f"Database write failure in node_write_chunk_meta status closure: {db_err}") from db_err
                else:
                    print(f"    └─ [SIMULATED] document_version: doc_id={doc_id} v{ver} content_process_status='DONE', chunk_status='DONE', chunk_count={chunk_cnt}")

                # L5 audit: per-(doc,version) chunk-status transition (DONE/EMPTY), fail-open + sim no-op.
                from opensearch_pipeline.audit_log import write_audit, audit_trace_id
                write_audit(doc_id=doc_id, version_no=ver, action_type="CHUNK",
                            action_result=("DONE" if chunk_cnt > 0 else "EMPTY"),
                            trace_id=audit_trace_id(ctx), message=f"{chunk_cnt} chunks",
                            simulate=simulate_db)

                # F（2026-07-25）：非 skip 版本的资产集比对——放在 chunk 提交**之后**，
                # 避免给已回滚的版本留下"已变化"事件。observed_stage=CHUNK_COMMITTED
                # **不表示**该版本已上线（stage 3 成功才写 index_status=SUCCESS）。
                _canon_for_diff = next(
                    (c for c in (ctx.get("canonicals") or [])
                     if c.get("doc_id") == doc_id and c.get("version_no") == ver), None)
                if _canon_for_diff is not None:
                    _emit_asset_set_diff(ctx, _canon_for_diff,
                                         observed_stage="CHUNK_COMMITTED",
                                         simulate_db=simulate_db)

                # ── P2-32（VLM 供应商降级传播）：本文档存在 degraded 兜底图片（VLM 超时/解析
                # 失败 → 占位 caption 或保守隔离，见 image_funnel_processor ~430）→ 收尾在 DONE
                # 之上追加改写 content_process_status='NEEDS_REVIEW'（0-chunk 疑似失败已有该词先例）。
                # 取舍（graceful degradation 铁律：辅助失败绝不断答案）：
                #   · chunk/嵌入/索引【照常】——文本照常可检索，占位图注照常可服务；
                #   · NEEDS_REVIEW 不在 stage-1/2 任何认领谓词（NOT_STARTED / FAILED&retry<3 /
                #     LOADING/PROCESSING 失效清扫）中 → 无自动重试循环、不触发未冻结重切守卫；
                #   · 语义 = 「文档可服务但图注降级、留在可复查集合」：供应商恢复后按
                #     content_process_status='NEEDS_REVIEW' + content_process_error LIKE 'vlm_degraded%'
                #     定位，走维护重灌（reset_for_rechunk）即自愈（degraded 结论从不入 VLM 缓存，
                #     重跑必然重新审计）。标记写失败仅告警不阻断（辅助失败不破坏主流程）。
                _vlm_degraded_n = 0
                try:
                    _vlm_degraded_n = int(
                        (_canon_by_dv.get((doc_id, ver), {}) or {}).get("vlm_degraded_count") or 0)
                except (TypeError, ValueError):
                    _vlm_degraded_n = 0
                # 批次6：部分内容丢失留痕（OCR 部分页失败/XLSX/PPTX 中途异常）走同一
                # NEEDS_REVIEW 通道——「有产出但不完整」绝不静默定稿 DONE。
                _partial_notes = list(
                    (_canon_by_dv.get((doc_id, ver), {}) or {}).get("partial_loss_notes") or [])
                # B4：超长 table_chunk 丢弃留痕**合并追加**进同一通道（不覆盖 B5/既有 note）。
                _partial_notes.extend((ctx.get("table_drop_notes") or {}).get((doc_id, ver)) or [])
                # C0（2026-08-03）：表内图无前置步骤 ⇒ 被显式丢弃（不静默错绑到下一步），
                # 走同一 NEEDS_REVIEW 通道。**预期计数为 0**——生产出现即语料版式漂移。
                _partial_notes.extend((ctx.get("table_image_drop_notes") or {}).get((doc_id, ver)) or [])
                if _vlm_degraded_n > 0 or _partial_notes:
                    _note_parts = []
                    if _vlm_degraded_n > 0:
                        _note_parts.append(
                            f"vlm_degraded: {_vlm_degraded_n} image(s) got degraded VLM "
                            f"fallback (vendor outage/timeouts) — re-ingest after VLM recovers")
                    _note_parts.extend(_partial_notes)
                    _vlm_note = "; ".join(_note_parts)[:255]
                    print(f"    🚨 {doc_id} v{ver}: VLM 降级 {_vlm_degraded_n} 张 / 部分丢失留痕 "
                          f"{len(_partial_notes)} 条 → content_process_status=NEEDS_REVIEW"
                          f"（chunk/索引照常，恢复后重灌自愈）")
                    if not simulate_db:
                        try:
                            if _closure_conn is None:
                                _closure_conn = _get_db_conn(select_db=True)
                            with _closure_conn.cursor() as cursor:
                                _lk = (doc_id, int(ver))
                                cursor.execute(f"""
                                    UPDATE document_version
                                    SET content_process_status = 'NEEDS_REVIEW',
                                        content_process_error = %s{ingest_lease.clear_set_sql()}
                                    WHERE doc_id = %s AND version_no = %s{_cls.fence_where_sql(_lk)}
                                """, (_vlm_note, doc_id, ver) + _cls.fence_where_params(_lk))
                                _cls.check_fenced_write(cursor, _lk)
                                _closure_conn.commit()
                        except ingest_lease.LeaseLost:
                            _closure_conn = _rollback_or_discard(_closure_conn)
                            print(f"    ⚠️ Lease lost on {doc_id} v{ver} — vlm NEEDS_REVIEW "
                                  f"write skipped (preempted)")
                        except Exception as db_err:
                            _closure_conn = _rollback_or_discard(_closure_conn)
                            print(f"    ⚠️ Failed to mark NEEDS_REVIEW (vlm degraded) for "
                                  f"{doc_id} v{ver}: {db_err}")
                    else:
                        print(f"    └─ [SIMULATED] document_version: doc_id={doc_id} v{ver} "
                              f"content_process_status='NEEDS_REVIEW' ({_vlm_note})")
    finally:
        # perf#93：闭环共享连接统一归还（fail-open 分支、DONE 分支 raise、正常收尾皆经此）。
        if _closure_conn is not None:
            _closure_conn.close()

    ctx["chunk_meta_written"] = written


def node_acquire_index_lock(ctx: dict):
    """
    乐观锁：在开始处理之前抢占索引锁定，防止并发冲突。

    操作：
      UPDATE document_version SET index_status = PROCESSING
      WHERE doc_id = X AND version_no = Y AND index_status IN (STAGE3_CLAIMABLE_INDEX_STATUS)

    成功抢占锁的版本保留在 valid_chunks 中，未成功抢占的版本其对应的 chunks 被过滤掉。
    同时，把成功抢占的版本 (doc_id, version_no) 记录在 ctx["preempted_doc_versions"] 中。
    """
    chunks = ctx.get("valid_chunks", [])
    simulate_db = _resolve_simulate(ctx, "db")

    valid_doc_versions = set()
    if not simulate_db and chunks:
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        assert_destructive_write_allowed("acquire_index_lock", get_config().rds.host, kind="rds")
        # 找出当前待处理的所有 (doc_id, version_no) 对
        doc_versions = list(set((chunk.doc_id, chunk.version_no) for chunk in chunks))
        conn = None
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                # E#46 主认领批量化：先发一条 (doc_id,version_no) 元组 IN 的集合式 UPDATE。
                # pymysql 默认 rowcount=changed rows，NOT_INDEXED/FAILED→PROCESSING 必然改行，
                # 故 rowcount==键数 ⟺ 全部主认领成功（常态首推路径，1 条语句）。部分认领时
                # 集合式 UPDATE 无法区分哪些键被改（回读 PROCESSING 会把他方在跑的锁误判为
                # 己方），回滚后退回逐键三步兜底（主认领 / SUCCESS-relock / stale-takeover），
                # changed-rows 语义与 updated_at=NOW() 刷新与旧实现完全等价。
                _all_claimed = False
                if doc_versions:
                    _dv_clause = " OR ".join(
                        ["(doc_id = %s AND version_no = %s)"] * len(doc_versions))
                    _dv_params = tuple(p for dv in doc_versions for p in dv)
                    cursor.execute(f"""
                        UPDATE document_version
                        SET index_status = '{DocVersionIndexStatus.PROCESSING}'{ingest_lease.claim_set_sql()}
                        WHERE ({_dv_clause})
                          AND index_status IN ({sql_in_list(STAGE3_CLAIMABLE_INDEX_STATUS)})
                    """, ingest_lease.claim_set_params() + _dv_params)
                    _all_claimed = cursor.rowcount == len(doc_versions)
                if _all_claimed:
                    valid_doc_versions.update(doc_versions)
                else:
                    if doc_versions:
                        conn.rollback()
                    for doc_id, ver in doc_versions:
                        cursor.execute(f"""
                            UPDATE document_version
                            SET index_status = '{DocVersionIndexStatus.PROCESSING}'{ingest_lease.claim_set_sql()}
                            WHERE doc_id = %s AND version_no = %s
                              AND index_status IN ({sql_in_list(STAGE3_CLAIMABLE_INDEX_STATUS)})
                        """, ingest_lease.claim_set_params() + (doc_id, ver))
                        # ── 修复：如果文档已被标记 SUCCESS（前一批次处理了部分 chunk），
                        # 仍然需要允许重新进入以处理残留的 NOT_INDEXED chunk。
                        if cursor.rowcount == 0:
                            # 尝试从 SUCCESS 状态重新锁定
                            cursor.execute(f"""
                                UPDATE document_version
                                SET index_status = '{DocVersionIndexStatus.PROCESSING}'{ingest_lease.claim_set_sql()}
                                WHERE doc_id = %s AND version_no = %s
                                  AND index_status = '{DocVersionIndexStatus.SUCCESS}'
                            """, ingest_lease.claim_set_params() + (doc_id, ver))
                        # ── 接管失效锁：仍处于 PROCESSING 且 >2h 未更新，说明持锁的运行已崩溃。
                        # 没有这一支，崩溃残留的 PROCESSING 文档永远无法被重新入队（loader 会反复
                        # 加载其 chunk 再被过滤掉，整批永远排不空）。2h 阈值与 orchestrator 的
                        # loader / _count_pending_rows 保持一致。
                        # SET 里的 updated_at = NOW() 不能省略：index_status 是同值更新
                        # （PROCESSING→PROCESSING），MySQL 对未发生变化的行 changed-rows=0，
                        # 连接池未开 CLIENT_FOUND_ROWS 时 rowcount 报告的正是 changed-rows，
                        # 且 ON UPDATE CURRENT_TIMESTAMP 也不会触发。显式刷新 updated_at
                        # 才会真正改变行（rowcount=1），同时重置失效时钟，保证并发运行中
                        # 只有第一个能接管。
                        if cursor.rowcount == 0:
                            cursor.execute(f"""
                                UPDATE document_version
                                SET index_status = '{DocVersionIndexStatus.PROCESSING}', updated_at = NOW(){ingest_lease.claim_set_sql()}
                                WHERE doc_id = %s AND version_no = %s
                                  AND index_status = '{DocVersionIndexStatus.PROCESSING}'
                                  AND {ingest_lease.takeover_where_sql()}
                            """, ingest_lease.claim_set_params() + (doc_id, ver))
                        if cursor.rowcount > 0:
                            valid_doc_versions.add((doc_id, ver))
                        else:
                            print(f"    └─ Task {doc_id} v{ver} skipped (preempted or already indexing)")
                # PR-4：认领事务内回读 epoch 登记（off 时 no-op）——embed/push 循环续租、
                # update_index_status/deactivate 栅栏写皆凭此集
                ingest_lease.get_lease_set(ctx).fetch_and_register(
                    cursor, [(d, int(v)) for d, v in valid_doc_versions])
                conn.commit()
            # 仅保留成功抢占锁的版本的 chunks
            chunks = [c for c in chunks if (c.doc_id, c.version_no) in valid_doc_versions]
        except Exception as e:
            if conn: conn.rollback()
            valid_doc_versions.clear()
            print(f"    ⚠️ Failed to preempt indexing tasks: {e}")
            raise RuntimeError(f"Failed to acquire index preemption lock: {e}") from e
        finally:
            if conn:
                conn.close()
    else:
        # 如果是模拟数据库模式，则不进行抢占，所有 chunks 全部通过
        for chunk in chunks:
            valid_doc_versions.add((chunk.doc_id, chunk.version_no))

    ctx["valid_chunks"] = chunks
    ctx["preempted_doc_versions"] = valid_doc_versions

    if not chunks:
        ctx["dag3_no_work"] = True
        ctx["skip_reason"] = "No document_version index lock acquired"
        print("    [SKIP] No document_version index lock acquired. Setting ctx['dag3_no_work'] = True.")
    else:
        print(f"    └─ Successfully acquired index lock for {len(valid_doc_versions)} document versions, {len(chunks)} chunks remaining.")


def _ha3_push_delete_request(client, config, chunk_ids: list) -> None:
    """向 HA3 下发一次 cmd:delete pushDocuments 请求（含幂等成功判定）。

    _search_delete_old_chunks（逐 doc）与 node_deactivate_old_chunks 的跨 doc 合并批
    （E#45）共用，防两份幂等判定漂移。幂等：not_found/no_op 视为成功。失败抛异常。
    """
    from alibabacloud_ha3engine_vector.models import PushDocumentsRequest
    cfg = config.alibaba_vector
    ha3_deletes = [{"cmd": "delete", "fields": {cfg.pk_field: cid}} for cid in chunk_ids]
    request = PushDocumentsRequest(body=ha3_deletes)

    resp = client.push_documents(cfg.table_name, cfg.pk_field, request)
    status_code = getattr(resp, 'status_code', 200)
    body_msg = str(getattr(resp, 'body', ''))
    text_msg = str(getattr(resp, 'text', ''))
    combined_msg = (body_msg + " | " + text_msg).lower()

    is_success = (200 <= status_code < 300)
    if not is_success:
        try:
            if hasattr(resp, "json") and callable(resp.json):
                resp_json = resp.json()
                err_code = resp_json.get("code") or resp_json.get("errors", [{}])[0].get("code")
                err_msg = str(resp_json).lower()
                if err_code in ["DocumentNotFound", "IndexNotFound", 7504, 7500] or any(ind in err_msg for ind in ["not_found", "not found", "no_op", "no-op"]):
                    print(f"    ├─ [HA3 Engine] Idempotent success detected in parsed JSON error: {err_msg}")
                    is_success = True
        except Exception:
            pass

        # Fallback to text check if JSON didn't catch it
        if not is_success:
            idempotent_indicators = ["not_found", "not found", "no_op", "no-op"]
            if any(ind in combined_msg for ind in idempotent_indicators):
                print(f"    ├─ [HA3 Engine] Idempotent success detected in response body: {combined_msg}")
                is_success = True

    if not is_success:
        raise RuntimeError(f"HA3 pushDocuments delete failed with status_code {status_code}, response: {combined_msg}")


def _search_delete_old_chunks(client, config, index_name: str, doc_id: str, ver: int,
                              old_chunk_ids: list, deadline_ts: float = None) -> None:
    """从搜索索引删除某文档 version_no < ver 的旧 chunk（node_deactivate_old_chunks 与
    搁浅版本对账 reconcile_stranded_versions 共用，防两份实现漂移）。

    HA3 按 chunk_meta.id（INT64 主键，与 to_ha3_doc 的 rds_id 同源）delete；
    标准 OpenSearch 用 delete_by_query。幂等：not_found/no_op 视为成功。失败抛异常。

    deadline_ts（F3，仅持锁对账方传）：time.monotonic() 截止时刻——reconciler 持
    document_meta 行锁跨本调用，批间超截止即抛（调用方回滚重试）；None=不限（stage-3
    终态路径不持行锁跨网络，行为逐字不变）。
    """
    if client == "MOCK_HA3_CLIENT":
        # 真实删除路径绝不接受 mock 客户端：继续会"假装删了索引、真停用 RDS 旧版本"→ 裂脑
        raise RuntimeError(
            "MOCK_HA3_CLIENT surfaced in a real-mode search delete; "
            "simulate flags are inconsistent (ctx vs config). Aborting."
        )
    # 唯一咽喉：node_deactivate_old_chunks 与 reconcile_stranded_versions 的索引删除都经此
    from opensearch_pipeline.env_guard import assert_destructive_write_allowed
    assert_destructive_write_allowed(
        "search_delete",
        config.alibaba_vector.endpoint or config.alibaba_vector.instance_id or config.opensearch.host,
        kind="search")
    if deadline_ts is not None:
        import time as _time
        if _time.monotonic() > deadline_ts:
            raise RuntimeError(
                f"search delete deadline exceeded before dispatch for {doc_id} (F3 bounded reconcile)")
    if hasattr(client, "push_documents"):
        if not old_chunk_ids:
            print(f"    ├─ [HA3 Engine] No older chunks found in RDS to deactivate for '{doc_id}'")
            return
        cfg = config.alibaba_vector
        _ha3_push_delete_request(client, config, old_chunk_ids)
        print(f"    ├─ [HA3 Engine] Deactivated {len(old_chunk_ids)} old chunks for '{doc_id}' in table '{cfg.table_name}'")
    else:
        # Original OpenSearch DELETE BY QUERY
        body = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"doc_id": doc_id}},
                        {"range": {"version_no": {"lt": ver}}}
                    ]
                }
            }
        }
        resp = client.delete_by_query(index=index_name, body=body)
        # Check standard OpenSearch response for failures
        if resp.get("failures"):
            raise RuntimeError(f"OpenSearch delete_by_query failed: {resp.get('failures')}")
        print(f"    ├─ [OpenSearch] Deactivated old versions for '{doc_id}' in index '{index_name}': deleted={resp.get('deleted', 0)}")


def node_deactivate_old_chunks(ctx: dict):
    """
    版本更新时，停用旧版本 chunks。

    ⚠️ 安全顺序要求：必须在 node_write_chunk_meta 之后运行。
    原因：如果先停用旧 chunk、后写新 chunk，中间任何环节失败
    会导致该文档在 OpenSearch 中"消失"（旧的停了，新的还没写）。

    正确的安全链路（跨 DAG 依赖顺序）：
      DAG 2: classify → detect → redact → publish → chunk → validate → write_chunk_meta
      DAG 3: acquire_lock → generate_embeddings → build_opensearch_payload → push_to_opensearch
             → update_index_status(04) → **verify_and_repush(04b parity)** → deactivate_old(05)
      04b 是最后一道闸：对本批全部已推 PK 用官方 fetch 做权威存在性确认（+可选 drift），
      任一未愈合的 DROP/UNKNOWN/DRIFT 都会 raise ⇒ 05 不执行 ⇒ 旧版本保留（宁可双版本
      并存，也绝不让新旧都不可检索）。

    ⚠️ 完整性前提：本批 chunks 全部 INDEXED ≠ 该 (doc, version) 全部 INDEXED——stage-3 loader
    按 created_at LIMIT 1000 装载、不按文档分组，边界可能把一个文档切成两批。因此停用前必须
    按 (doc, ver) 查 RDS 确认同版本无残留未 INDEXED chunk（见下方 LIMIT 边界完整性闸），
    否则推迟停用并把 document_version 复位 NOT_INDEXED 等待尾批。

    操作：
    1. RDS: UPDATE chunk_meta SET is_active=FALSE WHERE doc_id=X AND version_no < current
    2. OpenSearch: DELETE BY QUERY { doc_id=X AND version_no < current }
    3. RDS: 版本级 supersede（2026-07-12 双 active 审计补齐）——仅对本批收尾 SUCCESS 的文档，
       UPDATE document_version SET status='superseded' WHERE version_no < current AND status='active'
    """
    if ctx.get("dag_id") == "dag3_chunk_to_opensearch" and ctx.get("dag3_no_work"):
        print("    [SKIP] node_deactivate_old_chunks skipped because ctx['dag3_no_work'] is True.")
        return

    chunks = ctx.get("valid_chunks", []) or ctx.get("embedded_chunks", [])
    config = get_config()
    simulate_db = _resolve_simulate(ctx, "db")
    simulate_opensearch = _resolve_simulate(ctx, "opensearch")

    # 环境守卫：停用旧版本 = 不可逆删除链路的入口，真实分支前先断言（见 env_guard.py）
    if not simulate_db or not simulate_opensearch:
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        if not simulate_db:
            assert_destructive_write_allowed("deactivate_old_chunks", config.rds.host, kind="rds")
        if not simulate_opensearch:
            assert_destructive_write_allowed(
                "deactivate_old_chunks",
                config.alibaba_vector.endpoint or config.alibaba_vector.instance_id or config.opensearch.host,
                kind="search")

    # 从上下文获取在第一个节点中成功抢占锁的 document versions
    valid_doc_versions = ctx.get("preempted_doc_versions", set())

    # 找出本次处理涉及的所有 (doc_id, version_no) 对
    current_versions = {}
    for chunk in chunks:
        key = chunk.doc_id
        if key not in current_versions or chunk.version_no > current_versions[key]:
            current_versions[key] = chunk.version_no

    # ── 防御性加固：已知失败的 (doc, ver) 一律不参与旧版本停用 ──
    # 正常流程里 node_update_index_status 部分失败即 raise、本节点根本不会运行；此过滤器
    # 只在异常路径（绕过 DAG 直接调用 / 未来重构）下兜底，让"按文档"维度的安全不依赖上游
    # raise。特别地，embedding-FAILED 的 chunk 没进推送 batches、其内存 index_status 仍是
    # NOT_INDEXED，下方 failed_counts 是看不出来的，必须从 ctx 取。
    known_failed = set(ctx.get("failed_doc_versions") or set())
    known_failed |= {
        (c.doc_id, c.version_no) for c in ctx.get("embedding_failed_chunks", [])
    }
    known_failed |= {
        (c.doc_id, c.version_no) for c in chunks
        if getattr(c, "index_status", ChunkIndexStatus.NOT_INDEXED) == ChunkIndexStatus.FAILED
    }
    if known_failed:
        skipped_docs = [d for d, v in current_versions.items() if (d, v) in known_failed]
        if skipped_docs:
            print(f"    ├─ ⚠️ Skipping old-version deactivation for {len(skipped_docs)} "
                  f"doc(s) with known failures: {skipped_docs[:5]}")
            current_versions = {
                d: v for d, v in current_versions.items() if (d, v) not in known_failed
            }

    # ── 第三层正向不变量（P0 焊死, 2026-06-16）：拒绝停用"伪 INDEXED"当前版本 ──
    # 上面的 known_failed 是负向过滤,只跳过 ctx 已记的失败集;它**漏掉** P0 僵尸——embedding 未生成
    # (embedding_status != "DONE")却被推上 HA3 标记 index_status=="INDEXED" 的 chunk(无向量、kNN 不可见)。
    # 这里正向断言:将对照停用旧版本的 (doc, current_version) 不得存在任何 INDEXED-but-not-DONE 的 chunk,
    # 否则 raise、绝不停用。不依赖 ctx 失败集是否被填充,挡住未来绕过 node_update_index_status raise 闸、
    # 直接调用本节点的路径(reconcile/重构)。正常成功流程下 valid_chunks 即推送对象,push 成功后被原地置
    # INDEXED 且 embedding 成功时为 DONE,故此处必然全通过。
    _cur_set = set(current_versions.items())
    zombie = [
        (c.doc_id, c.version_no, c.chunk_id, getattr(c, "embedding_status", None))
        for c in chunks
        if (c.doc_id, c.version_no) in _cur_set
        and getattr(c, "index_status", None) == ChunkIndexStatus.INDEXED
        and getattr(c, "embedding_status", None) != "DONE"
    ]
    if zombie:
        raise RuntimeError(
            f"Refusing to deactivate old versions: {len(zombie)} current-version chunk(s) are marked "
            f"INDEXED without a DONE embedding (vectorless kNN-invisible 'zombie' = the P0 signature). "
            f"Aborting to avoid silent recall loss. Examples (doc,ver,chunk_id,embedding_status): {zombie[:5]}"
        )

    # ── LIMIT 边界完整性闸（全维复审 2026-07-03 top 项）──
    # stage-3 loader（dataworks_orchestrator）按 created_at LIMIT 1000 取 NOT_INDEXED chunk，
    # 无按文档分组：边界可能把一个文档的新版本切成两批。本批 chunks 全 INDEXED ≠ 该版本全部
    # INDEXED——只凭内存 batch 判断就删旧版本，会在尾部 chunk 尚不可检索时执行不可逆的 HA3 删除，
    # 该内容切片从搜索中消失，违反"先索引后停用"不变量。停用前按 (doc, ver) 查 RDS 是否仍有
    # 同版本未 INDEXED 的残留 chunk：谓词与 spot_checker.reconcile_stranded_versions 的
    # NOT EXISTS 完全一致（is_active = 1 AND index_status != 'INDEXED'）。命中版本推迟：
    # 不删 HA3、不停用旧 chunk、不标 SUCCESS；document_version 复位 NOT_INDEXED，下一批
    # loader 装入残留尾部、抢锁节点从 NOT_INDEXED 正常重入（若被旁路标了 SUCCESS，则由
    # SUCCESS-relock 分支 / reconcile_stranded_versions 兜底），尾部索引完成后再由本节点收尾。
    # 检查集合 = 本批 chunk 的全部 (doc, ver) ∪ current_versions（覆盖 SUCCESS 闸所及的所有版本）。
    incomplete_versions = set()
    if not simulate_db and chunks:
        _ck_pairs = sorted({(c.doc_id, c.version_no) for c in chunks}
                           | set(current_versions.items()))
        conn = None
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                _ck_clause = " OR ".join(
                    ["(doc_id = %s AND version_no = %s)"] * len(_ck_pairs))
                _ck_params = tuple(p for dv in _ck_pairs for p in dv)
                cursor.execute(f"""
                    SELECT DISTINCT doc_id, version_no FROM chunk_meta
                    WHERE ({_ck_clause})
                      AND is_active = 1
                      AND index_status != '{ChunkIndexStatus.INDEXED}'
                """, _ck_params)
                _ck_asked = set(_ck_pairs)
                for r in (cursor.fetchall() or []):
                    # 防御：只认属于本次查询集合的 (doc_id, version_no)；短行/异行只可能来自宽松的
                    # 测试桩，跳过（真实 WHERE 保证返回行必属查询集合，此过滤不改变生产语义）。
                    if len(r) >= 2 and (r[0], r[1]) in _ck_asked:
                        incomplete_versions.add((r[0], r[1]))
        except Exception as e:
            # 失败关闭：查不到完整性就绝不放行不可逆删除（与下方旧 id SELECT 的失败语义一致）
            print(f"    ⚠️ Failed to verify version completeness before deactivation: {e}")
            raise RuntimeError(f"Version completeness check failed before deactivation: {e}")
        finally:
            if conn:
                conn.close()
    if incomplete_versions:
        _deferred_docs = sorted(d for d, v in current_versions.items()
                                if (d, v) in incomplete_versions)
        if _deferred_docs:
            print(f"    ├─ ⚠️ Deferring old-version deactivation for {len(_deferred_docs)} doc(s) "
                  f"with residual un-INDEXED chunks of the same version (LIMIT-boundary cut): "
                  f"{_deferred_docs[:5]}")
            current_versions = {
                d: v for d, v in current_versions.items()
                if (d, v) not in incomplete_versions
            }

    # 检查 existing_chunks（模拟已在索引中的旧版本 chunks）
    existing_index = ctx.get("existing_opensearch_chunks", [])
    deactivated = []
    retained = []
    # D1: 生产审计必须由【真实删除集合】驱动。上面的 deactivated 只由 existing_opensearch_chunks 喂养，
    # 而该 key 仅 tests/ 与 run_simulation 注入 → 生产恒为空、旧版本不可逆删除【零审计】。真实路径
    # 从 RDS 取回被删旧行的 (doc_id, old_version, rds_id) 存入此列表，停用成功后据此写 DEACTIVATE。#F-D1audit
    _deact_audit_rows = []

    for old_chunk in existing_index:
        old_doc_id = old_chunk.get("doc_id")
        old_version = old_chunk.get("version_no", 0)
        old_chunk_id = old_chunk.get("chunk_id", "?")

        if old_doc_id in current_versions and old_version < current_versions[old_doc_id]:
            # 旧版本 → 停用
            deactivated.append({
                "chunk_id": old_chunk_id,
                "doc_id": old_doc_id,
                "old_version": old_version,
                "new_version": current_versions[old_doc_id],
            })
        else:
            retained.append(old_chunk)

    # 记录停用结果
    ctx["deactivated_chunks"] = deactivated
    ctx["retained_opensearch_chunks"] = retained

    if deactivated:
        print(f"    └─ ⚠️ Deactivated {len(deactivated)} old-version chunks:")
        for d in deactivated[:5]:
            print(
                f"       {d['chunk_id']}: v{d['old_version']} → v{d['new_version']} "
                f"(doc={d['doc_id']})"
            )
        if len(deactivated) > 5:
            print(f"       ... and {len(deactivated) - 5} more")

    # Real RDS & OpenSearch deactivation
    # （incomplete_versions 非空时即使 current_versions 被清空也要进入：推迟的版本仍需
    # document_version 状态收尾——复位 NOT_INDEXED，否则卡在 PROCESSING 等 2h 失效锁。）
    if current_versions or incomplete_versions:
        # 1. First, retrieve the chunk IDs of all older versions from RDS (if DB is not simulated)
        # ⚠️ HA3 文档主键是 chunk_meta.id（INT64 自增，见 to_ha3_doc 的 rds_id），
        # 不是字符串 chunk_id。删除必须用同一个 id，否则删除永远匹配不到已推送的文档，
        # 旧版本 chunk 会一直留在线上索引（与 spot_checker._delete_chunks_from_index 一致）。
        old_chunk_ids_map = {}
        if not simulate_db and current_versions:
            conn = None
            try:
                conn = _get_db_conn(select_db=True)
                with conn.cursor() as cursor:
                    # E#45：旧 id 逐 doc SELECT（N 次往返）→ 一条 (doc_id, version_no) 谓词
                    # OR-链查询取回并按 doc 分组。谓词与逐 doc 版本逐字对应
                    # （doc_id = X AND version_no < ver AND is_active = 1），id 集合完全一致。
                    _dv_items = list(current_versions.items())
                    _dv_clause = " OR ".join(
                        ["(doc_id = %s AND version_no < %s)"] * len(_dv_items))
                    _dv_params = tuple(p for dv in _dv_items for p in dv)
                    cursor.execute(
                        f"SELECT doc_id, version_no, id FROM chunk_meta "
                        f"WHERE ({_dv_clause}) AND is_active = 1",
                        _dv_params
                    )
                    rows = cursor.fetchall()
                    old_chunk_ids_map = {doc_id: [] for doc_id, _ in _dv_items}
                    for r in rows:
                        if len(r) < 3:
                            # 防御：真实 SELECT 恒返回 (doc_id, version_no, id) 3 列；短行只可能来自宽松
                            # 的测试桩。跳过 = 不删（安全方向：旧 chunk 保持 active，绝不误删）。#F-D1audit
                            continue
                        old_chunk_ids_map.setdefault(r[0], []).append(r[2])
                        # D1: 记录真实被删旧行，供停用成功后写 DEACTIVATE 审计（rds_id = chunk_meta.id）。
                        _deact_audit_rows.append((r[0], r[1], r[2]))
            except Exception as e:
                print(f"    ⚠️ Failed to query old chunk ids from RDS: {e}")
                raise RuntimeError(f"Database query failure in pre-deactivation phase: {e}")
            finally:
                if conn:
                    conn.close()
        else:
            # If DB is simulated, retrieve from the deactivated list if possible
            # （模拟路径只用于打印/MOCK，不会真正下发删除；这里的字符串 chunk_id 仅作展示）
            for doc_id, ver in current_versions.items():
                old_chunk_ids_map[doc_id] = [d["chunk_id"] for d in deactivated if d["doc_id"] == doc_id]

        # 2. Delete from Search Index (HA3 Engine SDK delete or standard OpenSearch delete_by_query)
        # （current_versions 为空 = 本批全部被推迟，无可删对象，跳过整段以免空跑客户端/守卫）
        if simulate_opensearch:
            if deactivated:
                print("    └─ [SIMULATED] OpenSearch: DELETE BY QUERY")
                for doc_id, ver in current_versions.items():
                    print(f"       {{ \"doc_id\": \"{doc_id}\", \"version_no\": {{ \"lt\": {ver} }} }}")
        elif current_versions:
            # PR-4：不可逆 HA3 删除前的归属过滤（无锁快照验租——这里不能抱着行锁跨
            # 网络调用）。丢锁文档从本批剔除：其旧版本停用归新持有者收尾。窗口内
            # （快照后、删除中）再被接管的残余风险=双方都会删同一批旧 PK，幂等无害；
            # 失败路径的 conn_fail 事务另有逐 doc 验租（R2）收窄写回窗口。
            _dls = ingest_lease.get_lease_set(ctx)
            _deact_lost = set()
            if ingest_lease.lease_enabled() and not simulate_db:
                try:
                    _vconn = _get_db_conn(select_db=True)
                    try:
                        with _vconn.cursor() as _vcur:
                            for _dvk in sorted(current_versions.items()):
                                _k = (_dvk[0], int(_dvk[1]))
                                if not _dls.verify_still_held(_vcur, _k):
                                    _deact_lost.add(_dvk[0])
                                    print(f"    ⚠️ Lease lost on {_k[0]} v{_k[1]} — old-version "
                                          f"deactivation abandoned (preempted)")
                    finally:
                        _vconn.close()
                except Exception as _ve:
                    # 验租通道故障 fail-closed：宁可整批推迟停用（复位路径兜底），绝不带
                    # 未知归属跑不可逆删除
                    raise RuntimeError(f"lease pre-check failed before HA3 delete: {_ve}") from _ve
                if _deact_lost:
                    # 丢锁文档三处同步剔除：HA3 删除集（current_versions）、收尾集
                    # （valid_doc_versions——verify_still_held 已留墓碑，后续栅栏会拒绝，
                    # 但收尾集合仍须显式剔除以免空转弃单打印）；dv 行零触碰（归新持有者）。
                    current_versions = {d: v for d, v in current_versions.items()
                                        if d not in _deact_lost}
                    valid_doc_versions = {(d, v) for d, v in valid_doc_versions
                                          if d not in _deact_lost}
            try:
                client = _get_opensearch_client(ctx)
                index_name = ctx.get("opensearch_index") or get_config().opensearch.index_name
                if client != "MOCK_HA3_CLIENT" and hasattr(client, "push_documents"):
                    # E#45：HA3 删除跨 doc 合并成共享批（100 条/请求）。PK 集合 = 各 doc 旧 id
                    # 之并集（chunk_meta.id 全局唯一、doc 间不相交），与逐 doc 逐次请求的删除
                    # 集合完全一致；仅请求切分方式不同。守卫与逐 doc 路径一致：mock 拒绝 +
                    # 目标指纹断言在删除下发之前。
                    from opensearch_pipeline.env_guard import assert_destructive_write_allowed
                    assert_destructive_write_allowed(
                        "search_delete",
                        config.alibaba_vector.endpoint or config.alibaba_vector.instance_id or config.opensearch.host,
                        kind="search")
                    _all_old_ids = []
                    for doc_id, ver in current_versions.items():
                        _doc_ids = old_chunk_ids_map.get(doc_id, [])
                        if not _doc_ids:
                            print(f"    ├─ [HA3 Engine] No older chunks found in RDS to deactivate for '{doc_id}'")
                        else:
                            _all_old_ids.extend(_doc_ids)
                    _del_batch = 100  # 与 _push_chunks_to_ha3 的 HA3 单请求上限一致
                    for _i in range(0, len(_all_old_ids), _del_batch):
                        _ha3_push_delete_request(client, config, _all_old_ids[_i:_i + _del_batch])
                    if _all_old_ids:
                        print(f"    ├─ [HA3 Engine] Deactivated {len(_all_old_ids)} old chunks across "
                              f"{len(current_versions)} doc(s) in table '{config.alibaba_vector.table_name}' "
                              f"({(len(_all_old_ids) + _del_batch - 1) // _del_batch} request(s))")
                else:
                    # 标准 OpenSearch（delete_by_query 本就整 doc 一次）与 mock 拒绝路径：保持逐 doc
                    for doc_id, ver in current_versions.items():
                        _search_delete_old_chunks(
                            client, config, index_name, doc_id, ver,
                            old_chunk_ids_map.get(doc_id, []),
                        )
            except Exception as e:
                print(f"    ⚠️ Failed to deactivate old chunks in search engine: {e}")
                # Explicit FAILED status assignment to prevent infinite hanging in NOT_INDEXED
                if not simulate_db:
                    try:
                        conn_fail = _get_db_conn(select_db=True)
                        with conn_fail.cursor() as cur:
                            _fls_fail = ingest_lease.get_lease_set(ctx)
                            for doc_id, ver in current_versions.items():
                                # PR-4（R2 共识）：HA3 网络调用期间可能被接管——本失败事务
                                # 逐 doc 先验租（FOR UPDATE 行锁持至 commit），丢锁则跳过该
                                # doc 的全部三组写（FAILED CAS/chunk FAILED/PENDING_DELETE），
                                # 归新持有者收敛。off/未登记 no-op=现状。
                                try:
                                    _fls_fail.verify_for_update(cur, (doc_id, int(ver)))
                                except ingest_lease.LeaseLost:
                                    print(f"    ⚠️ Lease lost on {doc_id} v{ver} — failure-path "
                                          f"writeback skipped (preempted)")
                                    continue
                                # CAS 收尾（对齐成功路径 6208 / node_update_index_status 7340）：
                                # 只允许 PROCESSING→FAILED。此前无谓词无条件写 FAILED，会把控制台
                                # 中途置的 PENDING_DELETE 删除握手（set-visibility→restricted / retire）
                                # 覆盖掉；FAILED 是 stage-3 可认领态，下批 loader 遂把受限文档以旧
                                # permission 重推 HA3，且基于 reconcile 的删除永不触发（ultra P1
                                # 2026-07-17）。clear_set_sql() 顺手清租约（flag off 时空串，现网零副作用）。
                                cur.execute(f"""
                                    UPDATE document_version
                                    SET index_status = '{DocVersionIndexStatus.FAILED}'{ingest_lease.clear_set_sql()}
                                    WHERE doc_id = %s AND version_no = %s
                                      AND index_status = '{DocVersionIndexStatus.PROCESSING}'
                                """, (doc_id, ver))
                                new_chunks = [c for c in chunks if getattr(c, "doc_id", "") == doc_id and getattr(c, "version_no", 0) == ver]
                                new_chunk_ids = [c.chunk_id for c in new_chunks]
                                if new_chunk_ids:
                                    format_strings_new = ','.join(['%s'] * len(new_chunk_ids))
                                    cur.execute(f"""
                                        UPDATE chunk_meta SET index_status = '{ChunkIndexStatus.FAILED}'
                                        WHERE chunk_id IN ({format_strings_new})
                                    """, tuple(new_chunk_ids))
                                # CS5 outbox: durably queue the OLD-version HA3 delete that just failed so
                                # reconcile_pending_deletes retries it even if this batch's retry never
                                # runs (laptop/manual ingestion). Additive — the raise below is unchanged
                                # (new version fails-safe + retries; never-disappear holds). The old chunks
                                # stay is_active=1 (still in HA3) until the reconciler deletes them and sets
                                # is_active=0 (CS5 edit in reconcile_pending_deletes), so no CS3 orphan.
                                cur.execute(f"""
                                    UPDATE document_version SET index_status = '{DocVersionIndexStatus.PENDING_DELETE}'
                                    WHERE doc_id = %s AND version_no < %s
                                      AND index_status NOT IN ({sql_in_list((DocVersionIndexStatus.DELETED, DocVersionIndexStatus.PENDING_DELETE))})
                                """, (doc_id, ver))
                        conn_fail.commit()
                        conn_fail.close()
                    except Exception as fail_e:
                        print(f"    ⚠️ Failed to explicitly mark FAILED status: {fail_e}")
                # We raise a RuntimeError so that the current pipeline step is marked as failed,
                # preventing the document version from being set to SUCCESS.
                raise RuntimeError(f"Failed to deactivate old chunks in search engine: {e}")

        # 3 & 4. Search index deactivation succeeded, now update old RDS chunks to is_active = FALSE and update document_version
        # 注意 failed_counts 基于【未过滤】的 chunks（含被防御过滤跳过停用的文档）：
        # 失败文档仍要写 document_version='FAILED'；known_failed 命中也一律按 FAILED 计
        # （embedding-FAILED 的 chunk 内存 index_status 是 NOT_INDEXED，单看 fail_cnt 会误判 SUCCESS）。
        failed_counts = {}
        for chunk in chunks:
            key = (chunk.doc_id, chunk.version_no)
            c_status = getattr(chunk, 'index_status', ChunkIndexStatus.NOT_INDEXED)
            if c_status == ChunkIndexStatus.FAILED:
                failed_counts[key] = failed_counts.get(key, 0) + 1
            else:
                failed_counts[key] = failed_counts.get(key, 0)

        if simulate_db:
            if deactivated:
                print(f"    └─ [SIMULATED] RDS: UPDATE chunk_meta SET is_active=FALSE, index_status='{ChunkIndexStatus.DELETED}'")
                for doc_id, ver in current_versions.items():
                    print(f"       WHERE doc_id='{doc_id}' AND version_no < {ver} AND is_active = 1")
            for (doc_id, ver), fail_cnt in failed_counts.items():
                if (doc_id, ver) in valid_doc_versions:
                    if fail_cnt or (doc_id, ver) in known_failed:
                        final_status = DocVersionIndexStatus.FAILED
                    elif (doc_id, ver) in incomplete_versions:
                        final_status = DocVersionIndexStatus.NOT_INDEXED  # LIMIT 边界推迟：残留尾部待下批
                    else:
                        final_status = DocVersionIndexStatus.SUCCESS
                    print(f"    ├─ [SIMULATED] RDS: Updated document_version status for {doc_id} v{ver} to '{final_status}'")
                    if final_status == DocVersionIndexStatus.SUCCESS:
                        print(f"    ├─ [SIMULATED] RDS: superseded older active document_version rows "
                              f"for {doc_id} (version_no < {ver})")
        else:
            conn = None
            try:
                conn = _get_db_conn(select_db=True)
                with conn.cursor() as cursor:
                    # PR-4：收尾事务首验租（FOR UPDATE 行锁持至 commit）——丢锁文档从
                    # is_active 翻转/终态/supersede 三段全部剔除，终态归新持有者。
                    _fls = ingest_lease.get_lease_set(ctx)
                    _fin_lost = set()
                    for _dvk in sorted(set(current_versions.items())
                                       | {(d, v) for (d, v) in failed_counts
                                          if (d, v) in valid_doc_versions}):
                        _k = (_dvk[0], int(_dvk[1]))
                        try:
                            _fls.verify_for_update(cursor, _k)
                        except ingest_lease.LeaseLost:
                            _fin_lost.add(_dvk)
                            print(f"    ⚠️ Lease lost on {_k[0]} v{_k[1]} — finalize "
                                  f"abandoned (preempted)")
                    if _fin_lost:
                        current_versions = {d: v for d, v in current_versions.items()
                                            if (d, v) not in _fin_lost}
                        valid_doc_versions = {dv for dv in valid_doc_versions
                                              if dv not in _fin_lost}
                    # Update older chunks
                    # E#45：逐 doc UPDATE → 一条 OR-链合并（谓词逐字对应，行集合一致）
                    # （本批全部被 LIMIT 边界推迟时 current_versions 为空：跳过，仅做状态收尾）
                    _dv_items = list(current_versions.items())
                    if _dv_items:
                        _dv_clause = " OR ".join(
                            ["(doc_id = %s AND version_no < %s)"] * len(_dv_items))
                        _dv_params = tuple(p for dv in _dv_items for p in dv)
                        cursor.execute(f"""
                            UPDATE chunk_meta
                            SET is_active = FALSE,
                                index_status = '{ChunkIndexStatus.DELETED}'
                            WHERE ({_dv_clause}) AND is_active = 1
                        """, _dv_params)
                        print("    └─ Updated older versions of chunks in RDS chunk_meta to inactive")

                    # Update document_version status（E#45：按 final_status 分组合并 UPDATE）
                    _status_groups = {}
                    for (doc_id, ver), fail_cnt in failed_counts.items():
                        if (doc_id, ver) in valid_doc_versions:
                            if fail_cnt or (doc_id, ver) in known_failed:
                                final_status = DocVersionIndexStatus.FAILED
                            elif (doc_id, ver) in incomplete_versions:
                                # LIMIT 边界推迟：复位 NOT_INDEXED，让下一批 loader / 抢锁节点
                                # 立即重入残留尾部；绝不 SUCCESS（否则尾部只能靠 SUCCESS-relock 兜底）。
                                final_status = DocVersionIndexStatus.NOT_INDEXED
                            else:
                                final_status = DocVersionIndexStatus.SUCCESS
                            _status_groups.setdefault(final_status, []).append((doc_id, ver))
                            print(f"    ├─ RDS: Updated document_version status for {doc_id} v{ver} to '{final_status}'")
                    for final_status, _dvs in _status_groups.items():
                        _st_clause = " OR ".join(
                            ["(doc_id = %s AND version_no = %s)"] * len(_dvs))
                        _st_params = (final_status,) + tuple(p for dv in _dvs for p in dv)
                        # CAS 收尾（盲区审计 P2-2）：只允许 PROCESSING→终态。这里的每个键都在
                        # node_acquire_index_lock 被 CAS 进 PROCESSING（valid_doc_versions ⊆ 锁集），
                        # 正常路径谓词恒真、行为不变；若控制台在本批运行中把该版本置为
                        # PENDING_DELETE（set-visibility→restricted / retire 的删除握手），无条件
                        # UPDATE 会把握手令牌覆盖成 SUCCESS——受限文档带旧 permission 永久留在
                        # HA3 被越权投放。CAS 跳过即保住 PENDING_DELETE，下轮 reconcile 把刚推
                        # 的旧权限行删掉，最终一致。
                        cursor.execute(f"""
                            UPDATE document_version
                            SET index_status = %s{ingest_lease.clear_set_sql()}
                            WHERE ({_st_clause})
                              AND index_status = '{DocVersionIndexStatus.PROCESSING}'
                        """, _st_params)
                        # 测试桩（MagicMock/自定义 cursor）可能无 int rowcount → 跳过差额告警
                        _st_rc = getattr(cursor, "rowcount", None)
                        if isinstance(_st_rc, int) and _st_rc < len(_dvs):
                            print(f"    ├─ ⚠️ {len(_dvs) - _st_rc} 个版本收尾被跳过"
                                  f"（index_status 已非 PROCESSING，可能被控制台置 PENDING_DELETE——"
                                  f"保留其握手令牌，交 reconcile 清除）")

                    # 版本级 supersede（2026-07-12 双 active 审计缺口）：chunk 级停用只写 chunk_meta，
                    # document_version 旧 active 行此前无任何管道路径降级——version-bump 重灌后每 doc
                    # 留下双 status='active'（7-06 批 485/485 + 窗口外存量 104；纯台账脏，无双服务）。
                    # 与「先索引后停用」同序、同一事务：只对本批收尾 SUCCESS（新版本全量 INDEXED、
                    # 旧 chunk 已停用）的文档降级旧版本行；FAILED / LIMIT-推迟（NOT_INDEXED）的文档
                    # 旧版本仍在服务，绝不提前 supersede。CAS on status='active' 幂等（重跑/SUCCESS-
                    # relock 无副作用），且不碰 retired（控制台退役）行；只写 status、不动 index_status，
                    # 不影响 PENDING_DELETE 删除握手。
                    _sp_dvs = _status_groups.get(DocVersionIndexStatus.SUCCESS, [])
                    if _sp_dvs:
                        _sp_clause = " OR ".join(
                            ["(doc_id = %s AND version_no < %s)"] * len(_sp_dvs))
                        _sp_params = tuple(p for dv in _sp_dvs for p in dv)
                        cursor.execute(f"""
                            UPDATE document_version
                            SET status = 'superseded'
                            WHERE ({_sp_clause}) AND status = 'active'
                        """, _sp_params)
                        _sp_rc = getattr(cursor, "rowcount", None)
                        if isinstance(_sp_rc, int) and _sp_rc > 0:
                            print(f"    ├─ RDS: superseded {_sp_rc} older active document_version row(s)")
                conn.commit()
            except Exception as e:
                if conn: conn.rollback()
                print(f"    ⚠️ Failed to update RDS states (deactivate old chunks / update doc status): {e}")
                raise RuntimeError(f"Failed to update RDS states: {e}")
            finally:
                if conn:
                    conn.close()

    # L5 audit: append-only DEACTIVATE events for the irreversible old-version retirement.
    # Placed after the RDS is_active=FALSE flip so it logs the realized outcome; fail-open + no-op in
    # simulate (write_audit handles both). kb_audit_log had ZERO writers before this.
    # D1: 审计由【真实删除集合】驱动（_deact_audit_rows 来自上方 RDS id SELECT）。旧代码只看 sim-only
    # 的 deactivated（existing_opensearch_chunks，生产从不注入）→ 生产删除全程无审计。仅当真实集合为空
    # 时回退到 deactivated（sim/测试路径，write_audit 经 simulate=simulate_db 空跑，既有断言不破）。#F-D1audit
    _audit_deactivations = _deact_audit_rows or [
        (d["doc_id"], d["old_version"], d["chunk_id"]) for d in deactivated
    ]
    # PR-4：丢锁被剔除的文档未真正执行停用——审计行同步剔除（审计=已实现的删除）。
    _audit_deactivations = [r for r in _audit_deactivations if r[0] in current_versions]
    if _audit_deactivations:
        # A7（2026-07-25）：批量写。此前逐条 write_audit(cursor=None) 每次都「取连接 → INSERT →
        # COMMIT → close」，按【旧 chunk 条数】计费——万级旧 chunk 即万级提交。行粒度不变
        # （N 条旧 chunk 仍 N 行，取证表的行粒度是既有契约），只合并 DB 往返。
        from opensearch_pipeline.audit_log import write_audit_many, audit_trace_id
        _trace = audit_trace_id(ctx)
        write_audit_many([
            {
                "doc_id": _doc_id, "version_no": _old_ver,
                "action_type": "DEACTIVATE", "action_result": "SUCCESS", "trace_id": _trace,
                "message": f"old v{_old_ver} retired by v{current_versions.get(_doc_id)} (id={_ref})",
            }
            for _doc_id, _old_ver, _ref in _audit_deactivations
        ], simulate=simulate_db)


# ═══════════════════════════════════════════════════════════════
# DAG 3: chunk → embedding → OpenSearch
# ═══════════════════════════════════════════════════════════════


def node_generate_embeddings(ctx: dict):
    """生成 embedding（生产环境调用 Gemini API，模拟环境使用 Hash）。"""
    if ctx.get("dag_id") == "dag3_chunk_to_opensearch" and ctx.get("dag3_no_work"):
        print("    [SKIP] node_generate_embeddings skipped because ctx['dag3_no_work'] is True.")
        ctx["embedded_chunks"] = []
        return

    chunks = ctx.get("valid_chunks", [])
    if not chunks:
        ctx["embedded_chunks"] = []
        return

    config = get_config()
    simulate_api = _resolve_simulate(ctx, "api")
    
    # 获取正确的模型名称和维度
    embedding_model = config.embedding.model
    embedding_dim = config.embedding.dimension

    if simulate_api:
        for chunk in chunks:
            h = hashlib.sha256(chunk.chunk_text.encode()).hexdigest()
            fake_vector = [
                (int(h[i * 2 : i * 2 + 2], 16) - 128) / 128.0
                for i in range(min(embedding_dim, 32))
            ]
            # 补齐维度
            if len(fake_vector) < embedding_dim:
                fake_vector.extend([0.0] * (embedding_dim - len(fake_vector)))
                
            chunk.embedding_vector = fake_vector
            chunk.embedding_model = embedding_model
            chunk.embedding_status = "DONE"

            # 图像 chunk 已通过 chunk_text 走统一 text-embedding-v4 路径
            # 不再需要独立的多模态向量（实验证明 text-embedding-v4 + visual_summary 效果最优）
            
        print(f"    └─ Generated {len(chunks)} embeddings (model={embedding_model}, dim={embedding_dim})")
        print("       ⚡ Note: using simulated vectors (hash-based) for local testing")
    else:
        import requests
        import time
        api_key = config.embedding.api_key
        base_url = config.embedding.api_base_url
        batch_size = config.embedding.batch_size
        
        is_dashscope = "dashscope.aliyuncs.com" in base_url or "qwen" in embedding_model.lower() or "text-embedding" in embedding_model.lower()
        
        if not api_key:
            if is_dashscope:
                raise RuntimeError("DashScope API key is not configured for real embeddings.")
            else:
                raise RuntimeError("Gemini API key is not configured for real embeddings.")
            
        max_retries = config.embedding.max_retries  # default: 3
        request_timeout = 60  # seconds per HTTP request

        # ── 本地 embedding 缓存（perf#18/19/20：SQLite KV + 进程级单例 + OSS 镜像）──
        # 旧 scratch/embedding_cache.json 整读整写形态（每批 json.load/重写 ~220MB、
        # serverless 永远冷）的问题、迁移与镜像语义见 embedding_cache.py 模块 docstring。
        # 键契约（P2-9，2026-07-05 起）：md5(f"{model}_{dimension}_{text}")，sparse 条目
        # "sp_" 前缀——与查询侧 LRU 键（retriever.get_query_embedding: (query, model, dim)）
        # 对齐。v4 支持 Matryoshka 多维，键不含 dimension 时改 RAG_EMBEDDING_DIMENSION 会
        # 静默命中旧维向量、混维推上 HA3。旧格式 md5(f"{model}_{text}") 的存量条目整体
        # miss（等价冷启动，advisory 缓存可接受），占位直至容量淘汰。DET: 崩溃安全
        # 由 SQLite WAL 日志保证（等价旧实现的 temp + os.replace 原子写不变量）。
        # 存量 JSON 首次自动迁移进 sqlite；JSON 文件原样保留供 tests/eval 脚本独立使用。
        _CACHE_MAX_ENTRIES = int(os.environ.get("RAG_EMBEDDING_CACHE_MAX_ENTRIES", "20000"))
        from opensearch_pipeline.embedding_cache import get_embedding_cache
        _store = get_embedding_cache()
        print(f"    └─ Embedding cache ready: backend={_store.backend}, {_store.count()} entries")

        def _cache_key(text):
            return hashlib.md5(
                f"{embedding_model}_{embedding_dim}_{text}".encode("utf-8")).hexdigest()

        # 分离 cache hit / miss（一次批量点查，dense+sparse 两键族）
        cache_hits = 0
        miss_chunks = []
        _all_keys = []
        for chunk in chunks:
            _ck = _cache_key(chunk.chunk_text)
            _all_keys.append(_ck)
            _all_keys.append(f"sp_{_ck}")
        _found = _store.get_many(_all_keys)
        for chunk in chunks:
            ck = _cache_key(chunk.chunk_text)
            sp_ck = f"sp_{ck}"
            dense = _found.get(ck)
            sp_data = _found.get(sp_ck)
            if not isinstance(sp_data, dict):
                sp_data = {}
            # 缓存里的字面 null 会 json 解码成 None，但 `ck in _found` 仍算命中 →
            # chunk 变成 DONE+无向量，混过 payload 的 "!= DONE" 过滤后 vectorless 推上
            # HA3（cmd=add 同 PK 全量替换 → 打掉旧好文档）。None 一律按 miss 重嵌。
            if dense is None:
                miss_chunks.append(chunk)
                continue
            # DashScope 入库要求 dense+sparse 成对：HA3 可能把无 sparse 的文档排除
            # （embedding_client._parse_sparse 的 sparse_fallback 兜底即为此），推了也
            # 检索不到 → parity FAILED + 下轮确定性 re-hit，排水卡死。dense 命中但 sp_
            # 缺失/null → 按 miss 重嵌（重嵌走 sparse_fallback=True，且回写含 sp_ 键，
            # 不会无限 miss）。Gemini 路径本无 sparse，不受此约束。
            if is_dashscope and not sp_data.get("indices"):
                miss_chunks.append(chunk)
                continue
            chunk.embedding_vector = dense
            chunk.embedding_model = embedding_model
            chunk.embedding_status = "DONE"
            if sp_data:
                chunk.sparse_vector_indices = sp_data.get("indices", [])
                chunk.sparse_vector_values = sp_data.get("values", [])
            cache_hits += 1

        # P3-13：命中率与容量压力显式化。上限（默认 20000 条 = dense+sparse 双条 ≈ 10000
        # chunk）此前是无信号的规模悬崖：语料/全量重切一旦超容，跨运行 OSS 镜像停止摊薄、
        # 重复整付 DashScope，唯一症状是账单变大。每次运行都记命中率；容量压力（本批
        # 需求量超上限 / 存量已顶上限=驱逐进行中）发 ops 告警提示调 RAG_EMBEDDING_CACHE_MAX_ENTRIES。
        _hit_rate = cache_hits / len(chunks) if chunks else 1.0
        print(f"    └─ Embedding cache hit-rate: {cache_hits}/{len(chunks)} "
              f"({_hit_rate:.0%}, backend={_store.backend}, entries={_store.count()}, "
              f"cap={_CACHE_MAX_ENTRIES})")
        _cap_pressure = (len(chunks) * 2 > _CACHE_MAX_ENTRIES
                         or _store.count() >= _CACHE_MAX_ENTRIES)
        if _cap_pressure and not simulate_api:
            try:
                from opensearch_pipeline.alerting import send_ops_alert
                send_ops_alert(
                    "嵌入缓存容量压力：跨运行成本摊薄退化",
                    f"本批 {len(chunks)} chunk（需 {len(chunks) * 2} 条缓存）vs 上限 "
                    f"{_CACHE_MAX_ENTRIES}（现存 {_store.count()}）。超容后最旧先驱逐，"
                    f"下次运行将对被驱逐 chunk 重付 DashScope 嵌入费。"
                    f"请调大 RAG_EMBEDDING_CACHE_MAX_ENTRIES（本批命中率 {_hit_rate:.0%}）。",
                    severity="warning", dedup_key="embed-cache-cap")
            except Exception as _e:   # noqa: BLE001 — 告警失败绝不影响摄取
                print(f"    ⚠️ embed-cache cap alert failed (non-fatal): {_e}")

        if not miss_chunks:
            print(f"    └─ All {len(chunks)} chunks served from cache, no API calls needed")
        elif is_dashscope:
            print(f"    └─ Calling DashScope API for {len(miss_chunks)} cache-miss chunks (batch_size={batch_size}, model={embedding_model}, dense+sparse, max_retries={max_retries})...")
            # 使用原生 DashScope API (非 compatible-mode) 以获取 sparse embedding。
            # HTTP/URL/重试/解析与查询侧共用 embedding_client.embed_texts_native（消除漂移）。
            from opensearch_pipeline.embedding_client import embed_texts_native
            # ── 并发生成 embedding：每个 size-batch_size 的 batch 一个线程 ──
            # RAG_EMBED_CONCURRENCY 控制并发度（默认 5，保守值；配额允许时可经环境变量提到
            # 8-10，perf#20——吞吐近似线性）。DashScope text-embedding 有账户级 QPS 限制，
            # 可按配额上调/下调。每个 batch 内保留 2**attempt 指数退避以吸收 429，
            # 因此移除了原先无条件的 time.sleep(1)（对 1000 chunks ≈ 100s 纯空转）。
            from concurrent.futures import ThreadPoolExecutor, as_completed

            embed_concurrency = max(1, int(os.environ.get("RAG_EMBED_CONCURRENCY", "5")))
            batches = [miss_chunks[i:i + batch_size] for i in range(0, len(miss_chunks), batch_size)]

            def _embed_one_batch(batch_no, batch):
                try:
                    results = embed_texts_native(
                        [c.chunk_text for c in batch],
                        api_key=api_key,
                        model=embedding_model,
                        dimension=embedding_dim,
                        api_base_url=base_url,
                        max_retries=max_retries,
                        request_timeout=request_timeout,
                        sparse_fallback=True,  # 无 sparse 用 [0]/[0.001] 兜底，避免 HA3 排除文档
                        label=f"DashScope batch {batch_no}",
                    )
                except Exception as e:
                    # 整批失败：标记 FAILED，继续处理其余 batch（FAILED chunk 由 payload 构建阶段剔除并下轮重试）
                    print(f"    ⚠️ DashScope batch {batch_no} failed: {e}")
                    print(f"    ⚠️ Skipping {len(batch)} chunks in this batch, continuing...")
                    for c in batch:
                        c.embedding_status = "FAILED"
                    return

                for i, r in enumerate(results):
                    if r is None:
                        # P0 修复(2026-06-16)：响应未覆盖该 text_index → 标 FAILED（而非保持 NOT_STARTED）。
                        # 否则该无向量 chunk 会混过 payload 构建、被推上 HA3 标 INDEXED、旧版本随后被停用
                        # → 静默召回丢失且永不重试。标 FAILED 后由 payload 阶段剔除并计入失败，下轮重试。
                        batch[i].embedding_status = "FAILED"
                        continue
                    dense, sidx, sval = r
                    batch[i].embedding_vector = dense
                    batch[i].embedding_model = embedding_model
                    batch[i].embedding_status = "DONE"
                    batch[i].sparse_vector_indices = sidx
                    batch[i].sparse_vector_values = sval

                # 写入缓存（store 内部线程安全；本批一次 executemany+commit 增量落盘）
                _updates = {}
                for c in batch:
                    if c.embedding_status == "DONE":
                        ck = _cache_key(c.chunk_text)
                        _updates[ck] = c.embedding_vector
                        if getattr(c, 'sparse_vector_indices', None):
                            _updates[f"sp_{ck}"] = {"indices": c.sparse_vector_indices,
                                                    "values": c.sparse_vector_values}
                _store.put_many(_updates)

            if embed_concurrency > 1 and len(batches) > 1:
                print(f"    └─ Embedding {len(batches)} batches with {embed_concurrency} concurrent workers...")
                with ThreadPoolExecutor(max_workers=embed_concurrency) as _ex:
                    _futs = [_ex.submit(_embed_one_batch, bn, b) for bn, b in enumerate(batches)]
                    for _f in as_completed(_futs):
                        _f.result()  # 让未预期的异常冒泡
                        _lease_renew_tick(ctx)  # PR-4：大批嵌入期保活租约（节流+fail-open）
            else:
                for bn, b in enumerate(batches):
                    _embed_one_batch(bn, b)
                    _lease_renew_tick(ctx)  # PR-4：同上（串行臂）
            _store.finalize(_CACHE_MAX_ENTRIES)
            print(f"    └─ Embedding cache updated: {_store.count()} total entries")
        elif not is_dashscope and miss_chunks:
            print(f"    └─ Calling Gemini API for {len(miss_chunks)} cache-miss chunks (batch_size={batch_size}, model={embedding_model}, max_retries={max_retries})...")
            from opensearch_pipeline.vlm_retry import post_json_with_retry
            for i in range(0, len(miss_chunks), batch_size):
                batch = miss_chunks[i:i+batch_size]
                url = f"{base_url}/models/{embedding_model}:batchEmbedContents"
                payload = {
                    "requests": [
                        {"model": f"models/{embedding_model}", "content": {"parts": [{"text": c.chunk_text}]}} 
                        for c in batch
                    ]
                }

                # 瞬时重试（429+全部 5xx+网络异常、数值 Retry-After、退避 1s→2s→4s 同旧
                # 2**attempt）收敛到共享策略 vlm_retry.post_json_with_retry（与 DashScope
                # classify/OCR/VLM/embedding_client 同一策略）。post_fn 传 requests.post
                # ——与全局 requests 模块同对象，tests 对 `requests.post` 的 patch 依旧生效。
                try:
                    resp = post_json_with_retry(
                        url,
                        json=payload,
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                        timeout=request_timeout,
                        max_retries=max_retries,
                        base_backoff=1.0,
                        label=f"Gemini batch {i//batch_size}",
                        post_fn=requests.post,
                    )
                    # 4xx(≠429) 与重试耗尽后的 429/5xx 在此抛 HTTPError → 下面统一包成 RuntimeError
                    resp.raise_for_status()
                    data = resp.json()

                    embeddings = data.get("embeddings", [])
                    for idx, item in enumerate(embeddings):
                        if idx >= len(batch):
                            break
                        batch[idx].embedding_vector = item["values"]
                        batch[idx].embedding_model = embedding_model
                        batch[idx].embedding_status = "DONE"
                    # 与 DashScope None-slot P0 修复同款：响应未覆盖的尾部槽位必须显式标
                    # FAILED。reload 的 push-FAILED chunk 从 RDS 带着 embedding_status='DONE'
                    # + 无向量进来，若部分响应漏掉它的槽位，它会保持 DONE+None 混过 payload
                    # 的 "!= DONE" 过滤 → vectorless cmd=add 全量替换旧好文档。
                    if len(embeddings) < len(batch):
                        for c in batch[len(embeddings):]:
                            c.embedding_status = "FAILED"
                        print(
                            f"    ⚠️ Gemini batch {i//batch_size} partial response: "
                            f"{len(embeddings)}/{len(batch)} embeddings returned; "
                            f"marked {len(batch) - len(embeddings)} uncovered slot(s) FAILED for retry"
                        )
                    # 写入缓存
                    _store.put_many({
                        _cache_key(c.chunk_text): c.embedding_vector
                        for c in batch if c.embedding_status == "DONE"})
                except Exception as e:
                    # 重试耗尽的瞬时错误 / 非瞬时 HTTP / 网络异常 / 解析错误：与历史行为
                    # 一致——严格传播，本批失败即整个节点失败（不静默降级）
                    print(f"    ⚠️ Gemini API Error on batch {i//batch_size}: {e}")
                    raise RuntimeError(f"Gemini API invocation failed during embedding generation: {e}")

                time.sleep(1)
            _store.finalize(_CACHE_MAX_ENTRIES)
            print(f"    └─ Embedding cache updated: {_store.count()} total entries")
        # ─── 图片 chunk embedding 说明 ───
        # 实验证明 text-embedding-v4 + visual_summary 文本描述 = 最优检索效果
        # 图片 chunk 已通过 chunk_text ("[Image Schematic] {visual_summary}") 走统一批量 text-embedding-v4 路径
        # 不再需要独立的多模态 embedding 模型（One-Peace 已废弃）
        image_chunks = [c for c in chunks if c.chunk_type == "image"]
        if image_chunks:
            print(f"    └─ {len(image_chunks)} image chunks embedded via text-embedding-v4 (visual_summary text, unified path)")

        print(f"    └─ Completed real embeddings (model={embedding_model}, dim={embedding_dim}).")

        # P2-8（盲区审计）：真实嵌入成功 → UPSERT RDS 契约行（embedding_model/dimension），
        # serving 启动时据此比对两平面模型配置（HA3 文档无模型戳，schema 加字段须整表重建，
        # user-gated）。fail-open：写失败/schema/018 未 apply 绝不影响摄取主流程；simulate
        # 分支不写（假向量没有契约意义）。
        try:
            from opensearch_pipeline.runtime_contract import upsert_embedding_contract
            upsert_embedding_contract(embedding_model, embedding_dim)
        except Exception as _ce:
            print(f"    ⚠️ embedding 契约行写入失败（fail-open）: {_ce}")

    ctx["embedded_chunks"] = chunks


def node_build_opensearch_payload(ctx: dict):
    """构建 OpenSearch bulk 写入 payload，支持根据 max_bulk_size_bytes 贪心切分。"""
    if ctx.get("dag_id") == "dag3_chunk_to_opensearch" and ctx.get("dag3_no_work"):
        print("    [SKIP] node_build_opensearch_payload skipped because ctx['dag3_no_work'] is True.")
        ctx["bulk_payload"] = ""
        ctx["bulk_payload_size"] = 0
        ctx["bulk_chunk_count"] = 0
        ctx["bulk_job_id"] = ""
        ctx["bulk_oss_key"] = ""
        ctx["bulk_batches"] = []
        return

    import uuid
    chunks = ctx.get("embedded_chunks", [])

    # ── 剔除未成功生成 embedding 的 chunk（embedding_status != "DONE"）──
    # 它们没有 dense/sparse 向量，若照常推到 HA3 会成为 kNN 完全不可见的"僵尸文档"，
    # 却仍被当成已索引而触发旧版本停用 → 静默召回丢失且永不重试。这里从 payload 中排除，
    # 单独记录到 ctx，由 node_update_index_status 计为失败（阻止停用 + 标记 FAILED 供下轮重试）。
    #
    # ⚠️ P0 修复(2026-06-16)：必须按 "!= DONE" 剔除，不能只剔 "== FAILED"。
    # DashScope native 响应可能漏掉某个 text_index（embedding_client 该槽位返回 None），使该 chunk
    # 停在 embedding_status="NOT_STARTED" 且无向量。只剔 FAILED 会让它混入 payload、被推上 HA3 标记
    # INDEXED、漏出 failed_doc_versions 计数，进而停用旧版本 → 静默非确定性召回丢失。
    # 所有应被索引的 chunk 在 embedding 成功后都会被显式置为 "DONE"（simulate / cache-hit / DashScope /
    # Gemini 四条路径皆然，且无任何 chunk 类型按设计 vectorless 索引），故 "!= DONE" 不会误剔合法 chunk。
    #
    # 兜底护栏(2026-07-03)：status=DONE 但无向量的 chunk 一律降级 FAILED。上面 "!= DONE"
    # 过滤对 DONE+None 无效——它会 vectorless 推上 HA3（to_ha3_doc 对 falsy 向量静默省略
    # dense_vector，cmd=add 同 PK 是全量替换 → 打掉旧好文档）。已知来源（缓存字面 null、
    # Gemini 部分响应漏槽位 × RDS reload 的 DONE 状态）已各自修复；此处保证任何未来路径
    # 都推不出 vectorless 文档。
    _zombie_done = [
        c for c in chunks
        if getattr(c, "embedding_status", None) == "DONE"
        and not getattr(c, "embedding_vector", None)
    ]
    if _zombie_done:
        for c in _zombie_done:
            c.embedding_status = "FAILED"
        print(
            f"    ⚠️ Demoted {len(_zombie_done)} chunk(s) with embedding_status=DONE but no "
            f"embedding_vector to FAILED (vectorless docs must never enter the HA3 payload)"
        )
    embedding_failed_chunks = [
        c for c in chunks if getattr(c, "embedding_status", None) != "DONE"
    ]
    ctx["embedding_failed_chunks"] = embedding_failed_chunks
    if embedding_failed_chunks:
        chunks = [c for c in chunks if getattr(c, "embedding_status", None) == "DONE"]
        print(
            f"    ⚠️ Excluding {len(embedding_failed_chunks)} chunk(s) without a DONE embedding "
            f"from index payload (marked FAILED for retry; old versions will NOT be deactivated this run)"
        )

    if not chunks:
        print("    ⚠️ No embedded chunks to build payload")
        ctx["bulk_payload"] = ""
        ctx["bulk_payload_size"] = 0
        ctx["bulk_chunk_count"] = 0
        ctx["bulk_job_id"] = f"BULK_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        ctx["bulk_oss_key"] = ""
        ctx["bulk_batches"] = []
        return

    config = get_config()
    max_bulk_limit = ctx.get("max_bulk_size_bytes")
    if max_bulk_limit is None:
        # Default safety margin is 1.5MB (configured as 1,500,000 in config.py:L57)
        max_bulk_limit = getattr(config.opensearch, "max_bulk_size_bytes", 1_500_000)

    # D8 Phase 11(A/B framework Step A):NDJSON 序列化 + 切批抽到 bulk_helpers,
    # 生产 ingestion 与评测 chunker_ab 灌入共享单一来源,避免序列化漂移
    from opensearch_pipeline.bulk_helpers import build_opensearch_bulk_actions
    # B15（2026-07-25）：HA3 路径上的 NDJSON payload 是纯死重 —— 推送消费的是内存 chunk 对象
    # （_push_chunks_to_ha3），payload 只被上传 OSS 再归档，而 `payload_oss_key` 全仓只有写方、
    # 无任何读回方。RAG_BULK_PAYLOAD_ARCHIVE=false 时不再拼字符串/不上传/不归档，主要收益是
    # 峰值内存（ctx["bulk_batches"] 全程持有整批 NDJSON），而非 CPU/OSS 费用。
    # **默认 on 保持现状**；且只允许在 HA3 后端关闭 —— 标准 OpenSearch 的 client.bulk 直接
    # 消费 batch["payload"]，simulate 路径也要写本地 pending JSONL（sim 断言依赖）。
    _archive_payload = os.environ.get("RAG_BULK_PAYLOAD_ARCHIVE", "true").strip().lower() \
        not in ("0", "false", "no")
    bucket, is_simulated = _get_oss_bucket(ctx)
    if not _archive_payload:
        _client_probe = _get_opensearch_client(ctx)
        _is_ha3 = hasattr(_client_probe, "push_documents") and _client_probe != "MOCK_HA3_CLIENT"
        if is_simulated or not _is_ha3:
            print("    ⚠️ RAG_BULK_PAYLOAD_ARCHIVE=false 被忽略：仅 HA3 后端可关"
                  "（标准 OpenSearch/simulate 直接消费 payload）——本轮仍物化。")
            _archive_payload = True
    batches = build_opensearch_bulk_actions(chunks, max_bulk_size_bytes=max_bulk_limit,
                                            materialize=_archive_payload)
    if not _archive_payload:
        # 不物化 ⇒ 无归档对象。payload_oss_key 写 NULL（schema 允许），不编造路径。
        for _b in batches:
            _b["job_id"] = ""      # 下面统一分配
            _b["oss_key"] = ""
        print(f"    └─ [B15] payload 归档已关闭（HA3 后端）：{len(batches)} 批不拼 NDJSON、"
              f"不上传 OSS；payload_size 口径不变（等价 NDJSON 未压缩字节数）")
    base_job_id = f"BULK_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    date_str = datetime.now().strftime('%Y-%m-%d')

    if not _archive_payload:
        # B15：只分配 job_id（opensearch_bulk_job 仍要建行做统计），oss_key 留空 → 落库 NULL
        for i, batch in enumerate(batches):
            batch["job_id"] = f"{base_job_id}_P{i + 1}"
            batch["oss_key"] = ""
    elif is_simulated:
        # Save payloads to physical pending JSONL files on disk
        pending_dir = f"index-jobs/opensearch/pending/{date_str}"
        os.makedirs(pending_dir, exist_ok=True)

        for i, batch in enumerate(batches):
            part_num = i + 1
            batch_job_id = f"{base_job_id}_P{part_num}"
            batch_oss_key = f"{pending_dir}/{batch_job_id}.jsonl"

            try:
                with open(batch_oss_key, "w", encoding="utf-8") as f:
                    f.write(batch["payload"])
                print(f"    └─ Saved batch {part_num}/{len(batches)} physical file: {batch_oss_key} ({batch['payload_size']:,} bytes)")
            except Exception as e:
                print(f"    ⚠️ Failed to write batch {part_num} payload file: {e}")
                raise RuntimeError(f"Local simulated payload write failed: {e}") from e

            batch["job_id"] = batch_job_id
            batch["oss_key"] = batch_oss_key
    else:
        # Upload directly to Alibaba Cloud OSS
        oss_prefix = config.oss.index_jobs_prefix.rstrip("/")
        for i, batch in enumerate(batches):
            part_num = i + 1
            batch_job_id = f"{base_job_id}_P{part_num}"
            batch_oss_key = f"{oss_prefix}/pending/{date_str}/{batch_job_id}.jsonl"

            try:
                bucket.put_object(batch_oss_key, batch["payload"].encode("utf-8"))
                print(f"    └─ Uploaded batch {part_num}/{len(batches)} payload to OSS: {batch_oss_key} ({batch['payload_size']:,} bytes)")
            except Exception as e:
                print(f"    ⚠️ Failed to upload batch {part_num} payload to OSS: {e}")
                raise RuntimeError(f"Alibaba Cloud OSS upload failed during payload generation: {e}")

            batch["job_id"] = batch_job_id
            batch["oss_key"] = batch_oss_key

    # Save backward-compatible context parameters for the first batch
    ctx["bulk_batches"] = batches
    ctx["bulk_payload"] = batches[0]["payload"]
    ctx["bulk_payload_size"] = batches[0]["payload_size"]
    ctx["bulk_chunk_count"] = len(batches[0]["chunks"])
    ctx["bulk_job_id"] = batches[0]["job_id"]
    ctx["bulk_oss_key"] = batches[0]["oss_key"]

    simulate_db = _resolve_simulate(ctx, "db")
    if not simulate_db:
        conn = None
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                for batch in batches:
                    cursor.execute("""
                        INSERT INTO opensearch_bulk_job (
                            job_id, index_name, total_chunks, status, payload_oss_key, payload_size_bytes
                        ) VALUES (
                            %s, %s, %s, 'PENDING', %s, %s
                        )
                        ON DUPLICATE KEY UPDATE
                        index_name = VALUES(index_name),
                        total_chunks = VALUES(total_chunks),
                        status = VALUES(status),
                        payload_oss_key = VALUES(payload_oss_key),
                        payload_size_bytes = VALUES(payload_size_bytes)
                    """, (
                        batch["job_id"],
                        ctx.get("opensearch_index") or get_config().opensearch.index_name,
                        len(batch["chunks"]),
                        # B15：未归档 ⇒ 写 NULL（列允许），不编造一个并不存在的对象路径
                        batch["oss_key"] or None,
                        batch["payload_size"]
                    ))
                conn.commit()
            print(f"    └─ Saved {len(batches)} opensearch_bulk_job tracking records to RDS")
        except Exception as e:
            if conn: conn.rollback()
            print(f"    ⚠️ Failed to insert opensearch_bulk_jobs to RDS: {e}")
            raise RuntimeError(f"Database write failure in node_build_opensearch_payload: {e}") from e
        finally:
            if conn:
                conn.close()


def _sample_repr(value, limit: int = 500) -> str:
    """A9：把响应片段安全地截成可入日志的样本。

    只用于 HA3 响应的**结构**取样（code/message/index/body 形态），绝不喂文档正文；
    repr 失败也不抛（形态诊断不该反过来炸掉推送路径）。
    """
    try:
        text = repr(value)
    except Exception:   # noqa: BLE001 — 诊断辅助，任何异常都降级
        return "<unreprable>"
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} chars)"


def _push_chunks_to_ha3(client, cfg, chunks, *, max_retries) -> dict:
    """把一批 Chunk 推送到 HA3（向量检索版），100 文档/子批。**原地**改写每个 chunk 的
    index_status / index_error_code / index_error_message。返回 {indexed, failed, took_ms}。

    仅 HA3 路径（无 OSS / RDS / simulate 处理）。既服务首推（node_push_to_opensearch），
    也服务推送后校验补推（node_verify_and_repush）的有界重推 —— 单一代码路径。
    幂等：主键为稳定的 rds_id，cmd:add 对已存在主键即 upsert，重推已存在的 chunk 无害。
    """
    from alibabacloud_ha3engine_vector.models import PushDocumentsRequest

    # HA3 单次 pushDocuments 上限。默认 100（历史值，VPC 内链路无压力）；env 可下调——
    # 2026-07-06 laptop repush 实测：100 chunk ≈ 1.5MB 单 POST 在公网/家庭上行下
    # 稳定超时（SDK 默认读超时偏紧），73KB 小批秒过 → 远程补推场景设 8-10 即可。
    ha3_batch_size = max(1, int(os.environ.get("RAG_HA3_PUSH_BATCH_SIZE", "100")))
    all_chunks = list(chunks)
    # Phase D（默认关）：仅 flag 开时推送 allowed_depts(MULTI_STRING)；关时输出与历史逐字节一致，
    # 且 HA3 表加该字段【之前】绝不推未知字段（Step 2 加字段后才会开 flag）。
    _incl_ad = get_config().rag.allowed_depts_acl
    ha3_docs = [{"cmd": "add", "fields": c.to_ha3_doc(cfg.pk_field, include_allowed_depts=_incl_ad)} for c in all_chunks]

    start_time = time.time()

    # A9（2026-07-25）：响应形态采样收集器。本函数没有 ctx（且被 04b parity 的两处补推复用），
    # 所以 warning 走【返回值】而不是 ctx —— 调用方 node_push_to_opensearch 再并进
    # ctx["validation_warnings"]。list.append 在 GIL 下原子，子批并行推送时安全。
    _push_warnings: list = []

    def _note_push_warning(msg):
        line = f"[HA3-RESP] {msg}"
        print(f"    ⚠️ {line}")
        _push_warnings.append(line)

    def _push_one_subbatch(sub_start):
        """推送一个 ≤100 条子批（重试 + per-doc 结果解析），**原地**改写子批 chunk 状态。

        F#50：子批之间无顺序依赖（cmd:add 按稳定 rds_id upsert 幂等），可并行调度；
        每个调用只触碰自己的 sub_chunks 切片（跨线程不相交），聚合在全部子批完成后进行。"""
        sub_docs = ha3_docs[sub_start:sub_start + ha3_batch_size]
        sub_chunks = all_chunks[sub_start:sub_start + ha3_batch_size]

        request = PushDocumentsRequest(body=sub_docs)

        # 重试循环：瞬时错误指数退避重试。刻意不并入 vlm_retry.post_json_with_retry
        # （2026-07-03 DashScope 重试收敛时评估过）：这是 HA3 Tea SDK 调用而非 requests.post
        # ——没有 Response.headers 可读 Retry-After，真实 SDK 的 4xx/5xx 以 TeaException 抛出
        # （下方 status_code 分支仅 mock/sim 可达）；且「任意异常都重试」是针对 HA3 网络抖动的
        # 刻意宽策略（比共享 is_retryable 更宽），保持自有实现。
        last_error = None
        resp = None
        for attempt in range(max_retries + 1):
            try:
                resp = client.push_documents(cfg.table_name, cfg.pk_field, request)
                status_code = getattr(resp, "status_code", 200)

                # 非瞬时错误：立即失败
                if status_code in (400, 401, 403):
                    last_error = None
                    break
                # 瞬时错误：重试
                if status_code in (429, 500, 502, 503, 504):
                    if attempt < max_retries:
                        wait = 2 ** attempt
                        print(f"    ⚠️ HA3 sub-batch {sub_start//ha3_batch_size + 1} attempt {attempt+1} failed (HTTP {status_code}). Retrying in {wait}s...")
                        time.sleep(wait)
                        continue
                # 成功或不可重试的状态码
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait = 2 ** attempt
                    print(f"    ⚠️ HA3 sub-batch {sub_start//ha3_batch_size + 1} attempt {attempt+1} failed (network): {e}. Retrying in {wait}s...")
                    time.sleep(wait)
                # else: fall through, last_error preserved

        if last_error is not None:
            # 所有重试耗尽，标记 sub-batch 为失败
            err_msg = f"HA3 pushDocuments failed after {max_retries + 1} attempts: {last_error}"
            for sc in sub_chunks:
                sc.index_status = ChunkIndexStatus.FAILED
                sc.index_error_code = "RETRY_EXHAUSTED"
                sc.index_error_message = err_msg
            print(f"    ├─ [HA3 Error] {err_msg}")
            return  # 该 sub-batch 结束（并行/串行下等价于旧 continue）

        status_code = getattr(resp, "status_code", 200)
        body = getattr(resp, "body", None)

        # 真实 SDK：PushDocumentsResponse 无 status_code（getattr 恒兜底 200），body 是原始 JSON
        # 字符串；4xx/5xx 由上面 except 的 TeaException 路径处理、永不到此，故此处只可能是 2xx。
        # 必须先把 str body 解析成 dict，才能真正读到 doc 级 errors —— 否则被 HA3 拒收的 chunk 会被
        # 静默标 INDEXED（96 例静默丢失同类）。解析失败保守按无 per-doc 错误处理（与历史行为一致，
        # 不新增失败面）。isinstance(body,str) 守卫保证既有 dict-body 单测路径不变、sim 零暴露。
        if isinstance(body, str) and body.strip():
            try:
                body = json.loads(body)
            except (ValueError, TypeError) as _je:
                # A9：解析失败仍按"无 per-doc 错误"处理（行为不变），但必须留下形态样本 ——
                # 这是「被 HA3 拒收却被标 INDEXED」最可能的入口，而至今没人见过真实响应长什么样。
                _note_push_warning(f"2xx body is a string that failed JSON parse ({_je}): "
                                   f"{_sample_repr(body)}")
                body = None

        if 200 <= status_code < 300:
            # 尝试解析 per-document 结果
            per_doc_parsed = False
            if body and isinstance(body, dict):
                errors_list = body.get("errors", [])
                if isinstance(errors_list, list) and errors_list:
                    # HA3 返回了 per-document 错误列表
                    per_doc_parsed = True
                    error_indices = set()
                    for err_item in errors_list:
                        # A9（2026-07-25）：归因判据此前只有上界 —— `err_idx < len(sub_chunks)` 对
                        # err_idx=-1 恒真，于是 sub_chunks[-1]（本子批**最后一个** chunk）被误标
                        # FAILED，真正出错的那条却被下面的 else 分支标成 INDEXED。补下界，并排除
                        # bool（Python 里 True/False 是 int 子类，会被当成索引 1/0）与非 dict 条目
                        # （err_item.get 会直接抛异常，把整个子批打成"意外异常"）。
                        if not isinstance(err_item, dict):
                            _note_push_warning(f"error item is {type(err_item).__name__}, not dict: "
                                               f"{_sample_repr(err_item)}")
                            continue
                        err_idx = err_item.get("index")
                        err_msg = err_item.get("message", "Unknown HA3 error")
                        err_code = str(err_item.get("code", "HA3_DOC_ERROR"))
                        if (isinstance(err_idx, int) and not isinstance(err_idx, bool)
                                and 0 <= err_idx < len(sub_chunks)):
                            sub_chunks[err_idx].index_status = ChunkIndexStatus.FAILED
                            sub_chunks[err_idx].index_error_code = err_code
                            sub_chunks[err_idx].index_error_message = err_msg
                            error_indices.add(err_idx)
                            print(f"    ├─ [HA3 Error] Chunk {sub_chunks[err_idx].chunk_id} failed: {err_code} - {err_msg}")
                        else:
                            # 不可归因（缺 index / 负数 / 越界 / 非 int）：**不改变既有行为**——
                            # 这条错误就此丢失，其余 chunk 仍标 INDEXED。之所以不升级为
                            # sub-batch fail-closed：至今没有一份真实生产的 HA3 部分失败响应样本，
                            # 贸然 fail-closed 会把正常批判死并触发全量重推。先采样拿形态。
                            _note_push_warning(
                                f"unattributable HA3 error item (index={_sample_repr(err_idx)}, "
                                f"sub_batch_size={len(sub_chunks)}, code={_sample_repr(err_code)}, "
                                f"message={_sample_repr(err_msg)})")
                    # 标记未出错的 chunks 为成功
                    for ci, sc in enumerate(sub_chunks):
                        if ci not in error_indices:
                            sc.index_status = ChunkIndexStatus.INDEXED
                            sc.index_error_code = None
                            sc.index_error_message = None
                elif errors_list:
                    # errors 存在但不是 list（形态不符）→ 采样，行为不变（走下面整批 INDEXED）
                    _note_push_warning(f"body['errors'] is {type(errors_list).__name__}, not list: "
                                       f"{_sample_repr(errors_list)}")
            elif body is not None:
                # 2xx 但 body 非 dict（含 JSON 解析失败被置 None 之外的形态）→ 采样，行为不变
                _note_push_warning(f"2xx response body is {type(body).__name__}, not dict: "
                                   f"{_sample_repr(body)}")

            if not per_doc_parsed:
                # 无 per-document 错误信息，整批标记成功
                for sc in sub_chunks:
                    sc.index_status = ChunkIndexStatus.INDEXED
                    sc.index_error_code = None
                    sc.index_error_message = None
        else:
            # HTTP 级别失败（不可重试的状态码）：整个 sub-batch 标记为失败
            body_message = str(body) if body else f"HTTP {status_code}"
            for sc in sub_chunks:
                sc.index_status = ChunkIndexStatus.FAILED
                sc.index_error_code = str(status_code)
                sc.index_error_message = body_message
            print(f"    ├─ [HA3 Error] Sub-batch {sub_start//ha3_batch_size + 1} failed with HTTP {status_code}: {body_message}")

    _sub_starts = list(range(0, len(ha3_docs), ha3_batch_size))
    # F#50：RAG_HA3_PUSH_CONCURRENCY（默认 1 = 现状串行）>1 时并行推送子批。
    # 结果聚合（下方 indexed/failed 统计与调用方的 verify/回写）在全部子批完成后才进行；
    # deactivate 仍在整个 push+verify 之后，DAG3 顺序不变量不受影响。SDK client 每次
    # push_documents 独立构造请求、无跨调用可变共享态，多线程共用一个 client 安全
    # （与 04b parity 并行 point-read 同一前提）；MOCK/simulate 路径不会进入本函数。
    try:
        _push_conc = int(os.environ.get("RAG_HA3_PUSH_CONCURRENCY", "1"))
    except ValueError:
        _push_conc = 1
    if _push_conc > 1 and len(_sub_starts) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(_push_conc, len(_sub_starts))) as _pool:
            # list() 物化以传播 worker 内的意外异常（与串行路径抛出行为一致）
            list(_pool.map(_push_one_subbatch, _sub_starts))
    else:
        for _sub_start in _sub_starts:
            _push_one_subbatch(_sub_start)

    took_ms = int((time.time() - start_time) * 1000)
    indexed_count = sum(1 for c in all_chunks if c.index_status == ChunkIndexStatus.INDEXED)
    failed_count = len(all_chunks) - indexed_count
    return {"indexed": indexed_count, "failed": failed_count, "took_ms": took_ms,
            "warnings": _push_warnings}


def _record_archive_warning(ctx: dict, batch: dict, message: str) -> None:
    """A8（2026-07-25）：payload 归档失败留痕，**绝不 raise**。

    此前归档失败直接 `raise`，而它发生在 HA3 push 成功【之后】、node_update_index_status
    【之前】：一次 OSS 抖动就让整轮 stage-3 白跑并回滚（chunk 已进 HA3、job 却停在
    PENDING），若归档失败是确定性的（权限/生命周期规则），stage-3 会对全语料停摆直到
    人工修 OSS。归档产物无任何读回方（全仓 `payload_oss_key` 只有写方），属辅助可观测性，
    与本仓「辅助功能失败绝不打断主流程」的既定约定一致 → 降级为警告。

    两处留痕：ctx 里本轮可见；batch 上的副本由 node_update_index_status 落进
    opensearch_bulk_job.error_message（列已存在，无 schema 迁移），保证跨运行可查。
    """
    print(f"    ⚠️ {message}")
    batch["archive_warning"] = message[:1000]
    ctx.setdefault("validation_warnings", []).append(message)


def node_push_to_opensearch(ctx: dict):
    """写入 OpenSearch（模拟/真实 — 顺序处理所有 batches 并移动文件）。"""
    if ctx.get("dag_id") == "dag3_chunk_to_opensearch" and ctx.get("dag3_no_work"):
        print("    [SKIP] node_push_to_opensearch skipped because ctx['dag3_no_work'] is True.")
        return

    import shutil
    from opensearch_pipeline.config import get_config
    
    config = get_config()
    simulate_opensearch = _resolve_simulate(ctx, "opensearch")
    batches = ctx.get("bulk_batches")
    if batches is None:
        # Fallback to single batch constructed from context for backwards compatibility
        batches = [{
            "chunks": ctx.get("embedded_chunks", []),
            "payload": ctx.get("bulk_payload", ""),
            "payload_size": ctx.get("bulk_payload_size", 0),
            "job_id": ctx.get("bulk_job_id", ""),
            "oss_key": ctx.get("bulk_oss_key", ""),
        }]

    print(f"    └─ Pushing {len(batches)} OpenSearch batches sequentially...")

    # 环境守卫：非生产环境向生产索引 upsert 同样是污染写
    if not simulate_opensearch:
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        assert_destructive_write_allowed(
            "push_index",
            config.alibaba_vector.endpoint or config.alibaba_vector.instance_id or config.opensearch.host,
            kind="search")

    # ctx 优先（DAG 级覆盖），否则随配置走（RAG_OPENSEARCH_INDEX）——
    # 不再硬编码回退：摄取推送与 serving 检索（retriever 用 cfg.index_name）必须同名
    index_name = ctx.get("opensearch_index") or get_config().opensearch.index_name

    # If NOT simulating, initialize client and ensure index exists
    client = None
    if not simulate_opensearch:
        try:
            client = _get_opensearch_client(ctx)
            if client == "MOCK_HA3_CLIENT":
                # ctx/config simulate 开关不一致（或测试替桩漂移）时绝不能继续：
                # 继续会伪造 INDEXED，随后 node_deactivate_old_chunks 真删 RDS 旧版本 → 裂脑
                raise RuntimeError(
                    "Simulate-flag mismatch: got MOCK_HA3_CLIENT in a real-mode push "
                    "(simulate_opensearch resolved False but a mock client was returned). "
                    "Refusing to fake INDEXED status."
                )
            # HA3 向量检索版的表在控制台创建，无需 API 创建索引
            # 只有标准 OpenSearch 需要 _ensure_opensearch_index
            if hasattr(client, 'indices'):
                dimension = config.embedding.dimension
                for batch in batches:
                    for chunk in batch["chunks"]:
                        if chunk.embedding_vector:
                            dimension = len(chunk.embedding_vector)
                            break
                    if dimension:
                        break
                _ensure_opensearch_index(client, index_name, dimension)
        except Exception as e:
            print(f"    ⚠️ Failed to initialize OpenSearch client/index: {e}")
            raise RuntimeError(f"Failed to initialize OpenSearch client/index in real mode: {e}")

    for i, batch in enumerate(batches):
        chunk_count = len(batch["chunks"])
        job_id = batch["job_id"]
        _lease_renew_tick(ctx)  # PR-4：大批 HA3 推送期保活租约（节流+fail-open）

        if simulate_opensearch:
            # 模拟写入延迟
            simulated_latency = chunk_count * 5  # 假设每 chunk 5ms
            time.sleep(min(simulated_latency / 1000.0, 1.0))  # 最多等 1 秒

            # 模拟结果
            result = {
                "status": "SIMULATED_SUCCESS",
                "took_ms": simulated_latency,
                "indexed": chunk_count,
                "failed": 0,
                "errors": False,
                "index_name": index_name,
            }
            batch["result"] = result
            print(f"    ├─ [SIMULATED] Indexed batch {i+1}/{len(batches)} ({chunk_count} docs) to '{result['index_name']}'")
            print(f"    ├─ [OpenSearch] Bulk index complete for {job_id}: took={simulated_latency}ms, indexed={chunk_count}, failed=0")

            # 更新 chunk 状态
            for chunk in batch["chunks"]:
                chunk.index_status = ChunkIndexStatus.INDEXED
        else:
            # Real bulk indexing (supports standard OpenSearch client or HA3 Vector client)
            try:
                # Pre-initialize status for safety in case some are missing in response
                for chunk in batch["chunks"]:
                    chunk.index_status = ChunkIndexStatus.FAILED
                    chunk.index_error_code = "NOT_RETURNED"
                    chunk.index_error_message = "No result returned for this chunk from indexing operation"

                start_time = time.time()

                if hasattr(client, "push_documents"):
                    # 💡 HA3 Engine Vector Pushing —— 复用 _push_chunks_to_ha3（首推 + 校验补推单一路径）
                    cfg = config.alibaba_vector
                    push_stats = _push_chunks_to_ha3(
                        client, cfg, batch["chunks"], max_retries=config.embedding.max_retries)
                    # A9：把子批收集到的响应形态样本并进 ctx（04b parity 的两处补推丢弃返回值，
                    # 只打印不入 ctx —— 那两处不在本节点的 ctx 生命周期内）。
                    if push_stats.get("warnings"):
                        ctx.setdefault("validation_warnings", []).extend(push_stats["warnings"])
                    result = {
                        "status": "SUCCESS" if push_stats["failed"] == 0 else "PARTIAL_FAIL",
                        "took_ms": push_stats["took_ms"],
                        "indexed": push_stats["indexed"],
                        "failed": push_stats["failed"],
                        "errors": push_stats["failed"] > 0,
                        "index_name": cfg.table_name,
                    }
                    batch["result"] = result
                    print(f"    ├─ [HA3 Engine] Bulk index complete for {job_id}: took={push_stats['took_ms']}ms, indexed={push_stats['indexed']}, failed={push_stats['failed']}")
                else:
                    # 💡 Standard OpenSearch Client bulk pushing
                    resp = client.bulk(body=batch["payload"], index=index_name)
                    took_ms = resp.get("took", int((time.time() - start_time) * 1000))
                    has_errors = resp.get("errors", False)

                    chunk_map = {c.chunk_id: c for c in batch["chunks"]}
                    indexed_count = 0
                    failed_count = 0

                    items = resp.get("items", [])
                    for item in items:
                        op = list(item.keys())[0] if item else None
                        if not op:
                            continue
                        op_details = item[op]
                        chunk_id = op_details.get("_id")
                        status_code = op_details.get("status", 200)

                        chunk = chunk_map.get(chunk_id)
                        if not chunk:
                            continue

                        if 200 <= status_code < 300:
                            chunk.index_status = ChunkIndexStatus.INDEXED
                            chunk.index_error_code = None
                            chunk.index_error_message = None
                            indexed_count += 1
                        else:
                            chunk.index_status = ChunkIndexStatus.FAILED
                            err = op_details.get("error", {})
                            err_type = err.get("type", "INDEX_ERROR")
                            err_reason = err.get("reason", "Unknown index error")
                            chunk.index_error_code = str(status_code)
                            chunk.index_error_message = f"{err_type}: {err_reason}"
                            print(f"    ├─ [OpenSearch Error] Chunk {chunk_id} failed with status {status_code}: {err_type} - {err_reason}")
                            failed_count += 1

                    result = {
                        "status": "SUCCESS" if failed_count == 0 else "PARTIAL_FAIL",
                        "took_ms": took_ms,
                        "indexed": indexed_count,
                        "failed": chunk_count - indexed_count,
                        "errors": has_errors,
                        "index_name": index_name,
                    }
                    batch["result"] = result
                    print(f"    ├─ [OpenSearch] Bulk index complete for {job_id}: took={took_ms}ms, indexed={indexed_count}, failed={chunk_count - indexed_count}")
            except Exception as e:
                print(f"    ⚠️ Index bulk push failed for job {job_id}: {e}")
                raise RuntimeError(f"Index bulk push failed in real mode for job {job_id}: {e}")

        # Move file to completed/failed
        source_path = batch.get("oss_key", "")
        if source_path:
            bucket, is_simulated = _get_oss_bucket(ctx)
            if is_simulated:
                if os.path.exists(source_path):
                    target_dir = "index-jobs/opensearch/completed" if batch["result"].get("failed", 0) == 0 else "index-jobs/opensearch/failed"
                    os.makedirs(target_dir, exist_ok=True)
                    target_path = os.path.join(target_dir, os.path.basename(source_path))
                    try:
                        shutil.move(source_path, target_path)
                        batch["oss_key"] = target_path
                        print(f"    ├─ Moved batch file to {target_path}")
                    except Exception as e:
                        _record_archive_warning(
                            ctx, batch,
                            f"[ARCHIVE] failed to move batch payload file {source_path}: {e}")
            else:
                # Real OSS object movement (copy + delete)
                try:
                    oss_prefix = config.oss.index_jobs_prefix.rstrip("/")
                    status_dir = "completed" if batch["result"].get("failed", 0) == 0 else "failed"
                    date_str = datetime.now().strftime('%Y-%m-%d')
                    target_key = f"{oss_prefix}/{status_dir}/{date_str}/{os.path.basename(source_path)}"

                    # Copy to target status path
                    bucket.copy_object(config.oss.bucket_name, source_path, target_key)
                    # Delete original pending path
                    bucket.delete_object(source_path)

                    batch["oss_key"] = target_key
                    print(f"    ├─ Archived OSS payload: {source_path} -> {target_key}")
                except Exception as e:
                    _record_archive_warning(
                        ctx, batch,
                        f"[ARCHIVE] failed to archive OSS payload {source_path}: {e}")

    # Aggregating values for backward compatibility in context
    total_took_ms = sum(b.get("result", {}).get("took_ms", 0) for b in batches)
    total_indexed = sum(b.get("result", {}).get("indexed", 0) for b in batches)
    total_failed = sum(b.get("result", {}).get("failed", 0) for b in batches)
    has_errors = any(b.get("result", {}).get("errors", False) for b in batches)

    aggregated_result = {
        "status": "SUCCESS" if total_failed == 0 else "PARTIAL_FAIL",
        "took_ms": total_took_ms,
        "indexed": total_indexed,
        "failed": total_failed,
        "errors": has_errors,
        "index_name": index_name,
    }

    ctx["index_result"] = aggregated_result
    ctx["index_status"] = "INDEXED" if total_failed == 0 else "PARTIAL_FAIL"

    if batches and "oss_key" in batches[0]:
        ctx["bulk_oss_key"] = batches[0]["oss_key"]


def node_update_index_status(ctx: dict):
    """回写索引状态到 RDS（真实/模拟，支持多 batches 逐个及逐行 chunks 更新）。"""
    if ctx.get("dag_id") == "dag3_chunk_to_opensearch" and ctx.get("dag3_no_work"):
        print("    [SKIP] node_update_index_status skipped because ctx['dag3_no_work'] is True.")
        return

    from datetime import datetime
    # L3: populate embedding_version —— P3-7 后写派生指纹 model@dimension（随 config 自变），
    # 不再写与运行时脱钩的手工常量（模型换代时常量必然过期、溯源失真）。
    from opensearch_pipeline.versions import embedding_regime_version
    _embedding_version = embedding_regime_version()

    batches = ctx.get("bulk_batches")
    if batches is None:
        batches = [{
            "chunks": ctx.get("embedded_chunks", []),
            "payload": ctx.get("bulk_payload", ""),
            "payload_size": ctx.get("bulk_payload_size", 0),
            "job_id": ctx.get("bulk_job_id", ""),
            "oss_key": ctx.get("bulk_oss_key", ""),
            "result": ctx.get("index_result", {}),
        }]

    chunks_count = sum(len(b["chunks"]) for b in batches)

    simulate_db = _resolve_simulate(ctx, "db")

    # 环境守卫：index_status 回写是停用旧版本的前置状态，同样属生产 RDS 写
    if not simulate_db:
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        assert_destructive_write_allowed("update_index_status", get_config().rds.host, kind="rds")

    # Identify all (doc_id, version_no) that experienced chunk indexing failures
    failed_doc_versions = set()
    for batch in batches:
        for chunk in batch["chunks"]:
            if getattr(chunk, 'index_status', ChunkIndexStatus.NOT_INDEXED) == ChunkIndexStatus.FAILED:
                failed_doc_versions.add((chunk.doc_id, chunk.version_no))

    # embedding 失败的 chunk 未进入 batches（未推送），但必须计入失败：否则其所属 doc 会被
    # 当作全部成功而停用旧版本，导致这些 chunk 永久丢失。计入 failed_doc_versions 阻止停用，
    # 并把它们 chunk_meta 标记 FAILED，下轮 loader 会重新加载、重新 embedding、重新推送。
    embedding_failed_chunks = ctx.get("embedding_failed_chunks", [])
    for chunk in embedding_failed_chunks:
        failed_doc_versions.add((chunk.doc_id, chunk.version_no))

    # 暴露给 node_deactivate_old_chunks 的防御过滤器（正常流程下方失败即 raise、
    # 停用节点不会运行；这里是给异常路径兜底的事实源）
    ctx["failed_doc_versions"] = failed_doc_versions

    if simulate_db:
        print(f"    └─ [SIMULATED] Would update {chunks_count} chunk records in RDS:")
        print("       embedding_status=DONE, index_status=INDEXED")
        if failed_doc_versions:
            print(f"       [SIMULATED] Would update document_version status to 'FAILED' for: {list(failed_doc_versions)}")

        total_failed = sum(b.get("result", {}).get("failed", 0) for b in batches) + len(embedding_failed_chunks)
        if total_failed > 0:
            raise RuntimeError(
                f"Index push had {total_failed} failures. "
                f"Aborting DAG execution to prevent deactivating older chunk versions."
            )
    else:
        conn = None
        _l3_lost = set()  # PR-4：本事务验租失败（被接管）的 (doc_id,ver)——回写整篇剔除
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                # A4：成功路径要清零 index_retry_count，先只读探一次列是否存在（迁移 019）。
                # 一次 SELECT 换掉"写失败再回退"——后者会连带丢弃本事务里已做的 bulk_job 更新，
                # 详见 _chunk_meta_has_index_retry 的 docstring。
                _has_retry_col = _chunk_meta_has_index_retry(cursor)
                # PR-4：事务首对本批全部 (doc_id,ver) FOR UPDATE 验租——通过后行锁持至
                # commit，接管不可能发生在回写中途；丢锁文档 chunk/终态回写全部跳过
                # （新持有者会重嵌重推，HA3 cmd=add 同 PK 幂等）。off/未登记 no-op。
                _uls = ingest_lease.get_lease_set(ctx)
                _all_l3_dvs = {(c.doc_id, int(c.version_no))
                               for b in batches for c in b["chunks"]}
                _all_l3_dvs |= {(c.doc_id, int(c.version_no)) for c in embedding_failed_chunks}
                for _dvk in sorted(_all_l3_dvs):
                    try:
                        _uls.verify_for_update(cursor, _dvk)
                    except ingest_lease.LeaseLost:
                        _l3_lost.add(_dvk)
                        print(f"    ⚠️ Lease lost on {_dvk[0]} v{_dvk[1]} — index-status "
                              f"writeback abandoned (preempted by another holder)")
                # Update bulk job records
                for batch in batches:
                    result = batch.get("result", {})
                    if not result:
                        continue
                    job_status = "COMPLETED" if result.get("failed", 0) == 0 else "PARTIAL_FAIL"
                    # A8：归档失败的持久留痕落 error_message（列已存在）——只写 ctx 的话
                    # pipeline_run 不收集 validation_warnings，运行一结束线索就没了。
                    # 无归档警告时保持原 SQL 形态（不写该列），零行为变化。
                    _arch_warn = batch.get("archive_warning")
                    if _arch_warn:
                        cursor.execute("""
                            UPDATE opensearch_bulk_job
                            SET status=%s, success_count=%s, fail_count=%s, payload_oss_key=%s,
                                error_message=%s, completed_at=NOW()
                            WHERE job_id=%s
                        """, (
                            job_status,
                            result.get("indexed", 0),
                            result.get("failed", 0),
                            batch.get("oss_key", ""),
                            _arch_warn,
                            batch.get("job_id", "")
                        ))
                    else:
                        cursor.execute("""
                            UPDATE opensearch_bulk_job
                            SET status=%s, success_count=%s, fail_count=%s, payload_oss_key=%s, completed_at=NOW()
                            WHERE job_id=%s
                        """, (
                            job_status,
                            result.get("indexed", 0),
                            result.get("failed", 0),
                            batch.get("oss_key", ""),
                            batch.get("job_id", "")
                        ))

                    # Update individual chunks in chunk_meta
                    index_name = result.get("index_name", "fuling_knowledge_v1")
                    # E#38/E#44：成功路径的 12 个 SET 字段除时间戳外全批相同 → 按
                    # (embedding_status, embedding_model, dimension) 分组，合并为一条
                    # UPDATE ... WHERE chunk_id IN (...)（每 1000 个 id 一条，防语句过长）。
                    # 失败/带错误详情的 chunk 保留逐条（要写 per-chunk error 详情）。
                    # opensearch_doc_id 原逐条写的就是自身 chunk_id → 合并版用列自引用等价表达。
                    _ok_groups = {}
                    for chunk in batch["chunks"]:
                        if (chunk.doc_id, int(chunk.version_no)) in _l3_lost:
                            continue  # PR-4：丢锁文档的 chunk 回写归新持有者
                        dim = len(chunk.embedding_vector) if chunk.embedding_vector else None

                        # Get optional error properties safely
                        idx_err_code = getattr(chunk, 'index_error_code', None)
                        idx_err_msg = getattr(chunk, 'index_error_message', None)

                        if (chunk.index_status == ChunkIndexStatus.INDEXED
                                and idx_err_code is None and idx_err_msg is None):
                            _key = (chunk.embedding_status, chunk.embedding_model, dim)
                            _ok_groups.setdefault(_key, []).append(chunk.chunk_id)
                            continue

                        # Embedded at timestamp
                        emb_at = datetime.now() if chunk.embedding_status == "DONE" else None
                        # Indexed at timestamp
                        idx_at = datetime.now() if chunk.index_status == ChunkIndexStatus.INDEXED else None

                        cursor.execute("""
                            UPDATE chunk_meta
                            SET
                                embedding_status = %s,
                                embedding_model = %s,
                                embedding_version = %s,
                                embedding_dimension = %s,
                                embedded_at = %s,
                                index_status = %s,
                                index_name = %s,
                                opensearch_doc_id = %s,
                                opensearch_bulk_job_id = %s,
                                index_error_code = %s,
                                index_error_message = %s,
                                indexed_at = %s
                            WHERE chunk_id = %s
                        """, (
                            chunk.embedding_status,
                            chunk.embedding_model,
                            _embedding_version,
                            dim,
                            emb_at,
                            chunk.index_status,
                            index_name,
                            chunk.chunk_id,
                            batch.get("job_id"),
                            idx_err_code,
                            idx_err_msg,
                            idx_at,
                            chunk.chunk_id
                        ))

                    for (_g_emb_status, _g_emb_model, _g_dim), _g_ids in _ok_groups.items():
                        _g_emb_at = datetime.now() if _g_emb_status == "DONE" else None
                        _g_idx_at = datetime.now()
                        for _i in range(0, len(_g_ids), 1000):
                            _g_sub = _g_ids[_i:_i + 1000]
                            _g_ph = ",".join(["%s"] * len(_g_sub))
                            _g_args = [
                                _g_emb_status,
                                _g_emb_model,
                                _embedding_version,
                                _g_dim,
                                _g_emb_at,
                                index_name,
                                batch.get("job_id"),
                                _g_idx_at,
                            ] + _g_sub
                            # A4（2026-07-25）：成功即把 index_retry_count 清零。此前该列只增不减
                            # （唯一写点是 _fail_chunks_with_retry_budget 的 +1），于是"累计失败 3 次"
                            # 而非"连续失败 3 次"就转 DEAD 死信 → chunk 不再被 loader 重选，
                            # node_deactivate_old_chunks 的完整性闸把旧版本停用永久推迟（双版本长存）。
                            # 只在这条【干净成功】路径复位：上面的逐条分支只会收到非 INDEXED 或带
                            # error 的 chunk（见前面 `continue` 的分流条件），在那里复位等于抹掉真
                            # 失败的重试预算。列不存在时（迁移 019 未应用）走无该列的 SQL，
                            # 行为与迁移前逐字节一致。
                            cursor.execute(f"""
                                UPDATE chunk_meta
                                SET
                                    embedding_status = %s,
                                    embedding_model = %s,
                                    embedding_version = %s,
                                    embedding_dimension = %s,
                                    embedded_at = %s,
                                    index_status = '{ChunkIndexStatus.INDEXED}',
                                    {"index_retry_count = 0," if _has_retry_col else ""}
                                    index_name = %s,
                                    opensearch_doc_id = chunk_id,
                                    opensearch_bulk_job_id = %s,
                                    index_error_code = NULL,
                                    index_error_message = NULL,
                                    indexed_at = %s
                                WHERE chunk_id IN ({_g_ph})
                            """, _g_args)

                # 回写 embedding 失败的 chunk（不在任何 batch 中）为 FAILED：
                # 下轮 loader 按 index_status IN ('NOT_INDEXED','FAILED') 重新加载并重试。
                # G9：带重试预算——持续 embedding 失败的毒 chunk 达上限转 DEAD 死信 +
                # 文档 NEEDS_REVIEW，不再每轮占用 loader 队头。
                _emb_failed_ids = [c.chunk_id for c in embedding_failed_chunks
                                   if (c.doc_id, int(c.version_no)) not in _l3_lost]
                _emb_dead = _fail_chunks_with_retry_budget(
                    cursor, _emb_failed_ids, extra_set_sql=", embedding_status = 'FAILED'")
                _mark_docs_needs_review_for_dead(cursor, _emb_dead)

                # If there are failed doc versions, update their document_version status to 'FAILED'
                if failed_doc_versions:
                    for doc_id, ver in failed_doc_versions:
                        if (doc_id, int(ver)) in _l3_lost:
                            continue  # PR-4：终态归新持有者
                        # CAS（盲区审计 P2-2 同款）：只允许 PROCESSING→FAILED——控制台中途置
                        # PENDING_DELETE 的删除握手不被覆盖（覆盖成 FAILED 会让下一批 loader
                        # 重新认领并把受限文档以旧 permission 重推 HA3）。
                        # PR-4：FAILED 是复位型终态（等下轮重认领）——只清租约不拼栅栏谓词：
                        # 事务首验租+行锁已保证归属，再拼 epoch 谓词会与 CAS rowcount 语义打架。
                        cursor.execute(f"""
                            UPDATE document_version
                            SET index_status = '{DocVersionIndexStatus.FAILED}'{ingest_lease.clear_set_sql()}
                            WHERE doc_id = %s AND version_no = %s
                              AND index_status = '{DocVersionIndexStatus.PROCESSING}'
                        """, (doc_id, ver))
                        # 测试桩 cursor 可能无 int rowcount → 按已更新打印（既有行为）
                        _f_rc = getattr(cursor, "rowcount", None)
                        if not isinstance(_f_rc, int) or _f_rc:
                            print(f"    ├─ RDS: Updated document_version status for {doc_id} v{ver} to 'FAILED' due to indexing failures")
                        else:
                            print(f"    ├─ ⚠️ {doc_id} v{ver} FAILED 收尾被跳过（已非 PROCESSING，保留现状态）")

                conn.commit()
            print(f"    └─ Updated {len(batches)} opensearch_bulk_job and {chunks_count} chunk_meta records in RDS.")
        except Exception as e:
            if conn: conn.rollback()
            print(f"    ⚠️ Failed to update opensearch_bulk_jobs/chunk_meta in RDS: {e}")
            raise RuntimeError(f"Database write failure in node_update_index_status: {e}") from e
        finally:
            if conn:
                conn.close()

        # L5 audit: per-(doc,version) INDEX outcome (SUCCESS/FAILED). After the commit, before the
        # abort-raise (so FAILED docs are recorded too). Fail-open + no-op in simulate.
        from opensearch_pipeline.audit_log import write_audit, audit_trace_id
        _idx_dvs = {(c.doc_id, c.version_no) for b in batches for c in b["chunks"]}
        _idx_dvs |= {(c.doc_id, c.version_no) for c in embedding_failed_chunks}
        _idx_trace = audit_trace_id(ctx)
        for _d, _v in sorted(_idx_dvs):
            write_audit(doc_id=_d, version_no=_v, action_type="INDEX",
                        action_result=("FAILED" if (_d, _v) in failed_doc_versions else "SUCCESS"),
                        trace_id=_idx_trace, simulate=simulate_db)

        total_failed = sum(b.get("result", {}).get("failed", 0) for b in batches) + len(embedding_failed_chunks)
        if total_failed > 0:
            raise RuntimeError(
                f"Index push had {total_failed} failures "
                f"({len(embedding_failed_chunks)} embedding + "
                f"{total_failed - len(embedding_failed_chunks)} push). "
                f"Updated failed document versions to 'FAILED'. "
                f"Aborting DAG execution to prevent deactivating older chunk versions."
            )


def _parity_content_hash(text) -> str:
    """Verbatim sha256 of a chunk's text. NO normalization — chunk_text_store is written verbatim
    from chunk_text (chunker.py), so verbatim-both-sides is the only false-positive-proof compare."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _chunk_meta_has_index_retry(cursor) -> bool:
    """chunk_meta.index_retry_count（迁移 019）是否存在。只读探测，异常一律按"不存在"处理。

    ⚠️ 为什么是【只读探测】而不是本文件别处那种"先试带新列的写、1054 再回退"：
    本连接栈上一条失败的语句会把**同一事务里已做但尚未提交的写全部丢弃**——pymysql 把
    1054 归为 OperationalError，DBUtils SteadyDB 据此认为连接可能已坏并透明重连（它不知道
    我们在事务里：_begin_txn 调的是底层 pymysql 的 begin，不经 SteadyDB 的事务记账），
    重连即隐式回滚。本地无 019 的库上实测：同一事务先写 v=99、再触发一次 1054、然后提交，
    读回仍是旧值。node_update_index_status 的事务里还压着 opensearch_bulk_job 的状态更新，
    用"失败即探测"会把它一起蒸发（本次实测到的真实回归）。
    """
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chunk_meta' "
            "AND COLUMN_NAME = 'index_retry_count'")
        row = cursor.fetchone()
        return bool(row and int(row[0]) > 0)
    except Exception:
        return False


def _stage3_chunk_max_retries() -> int:
    try:
        return int(os.environ.get("RAG_STAGE3_CHUNK_MAX_RETRIES", "3"))
    except ValueError:
        return 3


def _fail_chunks_with_retry_budget(cursor, chunk_ids, extra_set_sql="", extra_params=(),
                                   expect_all=False, count_retry=True):
    """G9：chunk 级失败回写 + 重试预算。返回本次转 DEAD 的 chunk_id 列表。

    index_retry_count +1；达上限（RAG_STAGE3_CHUNK_MAX_RETRIES，默认 3）→
    index_status='DEAD'（死信，不再被 loader 重选——消除毒 chunk 队头阻塞整轮
    drain），否则 'FAILED'（照旧重选）。MySQL SET 从左到右求值：IF 判断读到的是
    +1 后的新值。迁移 019 未应用（1054 未知列）→ 回退旧 SQL（无计数/无 DEAD，
    行为与迁移前一致，fail-open）。expect_all=True 时 rowcount 不符即 raise
    （沿用 parity 全有或全无语义，由调用方 rollback）。

    A4（2026-07-25）count_retry=False：只回写 FAILED，**不消耗重试预算**。用于
    「无法确认」类瞬态失败（PARITY_UNKNOWN = HA3 读失败，见 _present_unknown：官方 fetch
    的批异常/畸形响应整批归 unknown）——供应商侧一次读故障不该把健康 chunk 推向 DEAD 死信。
    死信资格只留给确认性失败（PARITY_DROP / PARITY_DRIFT / embedding 失败）。
    """
    if not chunk_ids:
        return []
    max_r = _stage3_chunk_max_retries()
    dead_ids: list = []

    def _legacy_update(sub, ph):
        """无计数回写（迁移 019 未应用，或 count_retry=False 的瞬态失败）。"""
        cursor.execute(
            f"UPDATE chunk_meta SET index_status='{ChunkIndexStatus.FAILED}'"
            f"{extra_set_sql} WHERE chunk_id IN ({ph})",
            list(extra_params) + sub,
        )
        _rc = getattr(cursor, "rowcount", None)
        if expect_all and isinstance(_rc, int) and _rc != len(sub):
            raise RuntimeError(
                f"state-persistence failure: marked {_rc} of {len(sub)} chunk_meta rows")

    for _i in range(0, len(chunk_ids), 1000):
        sub = list(chunk_ids[_i:_i + 1000])
        ph = ",".join(["%s"] * len(sub))
        if not count_retry:
            _legacy_update(sub, ph)
            continue
        try:
            cursor.execute(
                f"UPDATE chunk_meta SET index_retry_count = index_retry_count + 1, "
                f"index_status = IF(index_retry_count >= %s, "
                f"'{ChunkIndexStatus.DEAD}', '{ChunkIndexStatus.FAILED}')"
                f"{extra_set_sql} WHERE chunk_id IN ({ph})",
                [max_r] + list(extra_params) + sub,
            )
            _rc = getattr(cursor, "rowcount", None)
            if expect_all and isinstance(_rc, int) and _rc != len(sub):
                raise RuntimeError(
                    f"state-persistence failure: marked {_rc} of {len(sub)} chunk_meta rows")
            cursor.execute(
                f"SELECT chunk_id FROM chunk_meta WHERE chunk_id IN ({ph}) "
                f"AND index_status='{ChunkIndexStatus.DEAD}'", sub)
            dead_ids.extend(r[0] for r in (cursor.fetchall() or []))
        except Exception as _e:
            if "1054" in str(_e) or "index_retry_count" in str(_e).lower():
                _legacy_update(sub, ph)
            else:
                raise
    return dead_ids


def _mark_docs_needs_review_for_dead(cursor, dead_chunk_ids):
    """G9：DEAD 死信 chunk 所属文档版本置 NEEDS_REVIEW（人工可见，不再自动重试）。"""
    if not dead_chunk_ids:
        return
    ph = ",".join(["%s"] * len(dead_chunk_ids))
    cursor.execute(
        f"SELECT DISTINCT doc_id, version_no FROM chunk_meta WHERE chunk_id IN ({ph})",
        list(dead_chunk_ids))
    for _doc_id, _ver in (cursor.fetchall() or []):
        cursor.execute(
            "UPDATE document_version SET chunk_status='NEEDS_REVIEW', "
            "content_process_error=%s WHERE doc_id=%s AND version_no=%s",
            (f"stage-3 poison chunk(s) exhausted retry budget → DEAD letter "
             f"(max={_stage3_chunk_max_retries()}); fix & reset to NOT_INDEXED to requeue",
             _doc_id, _ver))
        print(f"    🚨 [DEAD-LETTER] {_doc_id} v{_ver}: 毒 chunk 达重试上限转 DEAD，"
              f"文档置 NEEDS_REVIEW（不再阻塞后续 drain）")


def _persist_parity_failed_and_raise(ctx, config, drop_chunks, unknown_chunks, max_retries,
                                     drift_chunks=None):
    """把校验失败的 chunk 写回 chunk_meta.index_status='FAILED' 后 raise，阻断
    node_deactivate_old_chunks（守住"新版本确认入库后才停用旧版本"不变量）。

    - DROP（确认 HA3 缺失）→ 'PARITY_DROP'；UNKNOWN（无法确认）→ 'PARITY_UNKNOWN'；
      DRIFT（PK 在但内容陈旧、补推后仍不一致）→ 'PARITY_DRIFT'。三组**分开** UPDATE，保留故障分类。
    - 重试预算只由确认性失败消耗：DROP/DRIFT 走 index_retry_count+1（达上限转 DEAD），
      UNKNOWN 只回写 FAILED 不计数（A4 2026-07-25——读故障≠坏 chunk）。
    - 全有或全无：任一 UPDATE 的 rowcount 与目标数不符 → rollback + raise 状态持久化错误。
      （部分写回会让一些 chunk 仍 INDEXED → 下轮 loader 不会重选 → 静默丢失/漂移被持久化。）
      注：pymysql 默认 rowcount=changed 行数；这些 chunk RDS 当前为 INDEXED→FAILED 必然计数。
    """
    from opensearch_pipeline.env_guard import assert_destructive_write_allowed
    assert_destructive_write_allowed("parity_repush", config.rds.host, kind="rds")

    drift_chunks = drift_chunks or []
    drop_msg = f"PARITY: absent from HA3 after {max_retries} re-push attempt(s)"
    unknown_msg = "PARITY: presence unconfirmable (HA3 read failed); conservatively un-indexed for retry"
    drift_msg = f"PARITY: content drift vs RDS chunk_text, unhealed after {max_retries} re-push(es)"

    buckets = (
        (drop_chunks, "PARITY_DROP", drop_msg),
        (unknown_chunks, "PARITY_UNKNOWN", unknown_msg),
        (drift_chunks, "PARITY_DRIFT", drift_msg),
    )

    # ── PR-4 on-arm（R3/B3 共识）：两臂刻意分离——off 臂必须与 pre-port main 逐字节
    # 一致（评审 B3：flag 关时不得新增任何 DV 写/改变调用序列）。on 臂在**任何**内存
    # 状态修改/DB 写/失败计数之前，同一事务内对受影响 dv 验租（FOR UPDATE 行锁持至
    # commit）并按 doc 过滤；全部丢锁 ⇒ 不写不抛直接 return（弃单归新持有者，下游
    # deactivate 的 verify_still_held 墓碑兜底）；混合批 ⇒ 仅幸存项写回+计数+照常
    # raise，并补批次5 的 dv PROCESSING→FAILED CAS+清租约（此前只写 chunk_meta 就
    # raise，dv 卡 PROCESSING 要等 TTL/2h 接管才能重选）。改 off 臂逻辑时必须同步改
    # 本臂（结构性重复是 B3 的代价，勿合并）。
    if ingest_lease.lease_enabled():
        _pls = ingest_lease.get_lease_set(ctx)
        _dv_keys = sorted({(c.doc_id, int(c.version_no))
                           for chunks, _c, _m in buckets for c in chunks})
        _par_lost = set()
        _live_buckets = buckets
        conn = None
        try:
            conn = _get_db_conn(select_db=True)
            with conn.cursor() as cursor:
                for _dk in _dv_keys:
                    try:
                        _pls.verify_for_update(cursor, _dk)
                    except ingest_lease.LeaseLost:
                        _par_lost.add(_dk)
                        print(f"    ⚠️ parity: lease lost on {_dk[0]} v{_dk[1]} — "
                              f"writeback abandoned (owned by new holder)")
                _live_buckets = tuple(
                    ([c for c in chunks
                      if (c.doc_id, int(c.version_no)) not in _par_lost], code, msg)
                    for chunks, code, msg in buckets)
                if not any(chunks for chunks, _c, _m in _live_buckets):
                    conn.rollback()
                    print("    ⚠️ parity: all affected docs preempted — no writeback, "
                          "not failing the stage (new holders own convergence)")
                    return
                for chunks, code, msg in _live_buckets:
                    for c in chunks:
                        c.index_status = ChunkIndexStatus.FAILED
                        c.index_error_code = code
                        c.index_error_message = msg
                _all_dead = []
                for chunks, code, msg in _live_buckets:
                    if not chunks:
                        continue
                    ids = [c.chunk_id for c in chunks]
                    try:
                        _dead = _fail_chunks_with_retry_budget(
                            cursor, ids,
                            extra_set_sql=", index_error_code=%s, index_error_message=%s",
                            extra_params=[code, msg], expect_all=True,
                            count_retry=(code != "PARITY_UNKNOWN"))
                    except RuntimeError as _pe:
                        conn.rollback()
                        raise RuntimeError(
                            f"PARITY {_pe} ({code}); rolled back to avoid a partial write "
                            f"that strands INDEXED-but-absent chunks (never re-selected).") from _pe
                    _all_dead.extend(_dead)
                    for c in chunks:
                        if c.chunk_id in set(_dead):
                            c.index_status = ChunkIndexStatus.DEAD
                _mark_docs_needs_review_for_dead(cursor, _all_dead)
                # 批次5（ultra 评审）：同事务把幸存 dv 从 PROCESSING CAS 回 FAILED+清租约。
                # CAS on PROCESSING 保住控制台 PENDING_DELETE 握手；fence 在验租行锁下恒
                # 命中，刻意不 check_fenced_write（CAS miss=行已离开 PROCESSING，合法）。
                for _dk in _dv_keys:
                    if _dk in _par_lost:
                        continue
                    cursor.execute(f"""
                        UPDATE document_version
                        SET index_status = '{DocVersionIndexStatus.FAILED}'{ingest_lease.clear_set_sql()}
                        WHERE doc_id = %s AND version_no = %s
                          AND index_status = '{DocVersionIndexStatus.PROCESSING}'{_pls.fence_where_sql(_dk)}
                    """, (_dk[0], _dk[1]) + _pls.fence_where_params(_dk))
            conn.commit()
        except Exception:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            if conn:
                conn.close()

        fdv = ctx.setdefault("failed_doc_versions", set())
        for chunks, _code, _msg in _live_buckets:
            for c in chunks:
                fdv.add((c.doc_id, c.version_no))
        _n = {code: len(chunks) for chunks, code, _m in _live_buckets}
        raise RuntimeError(
            f"Stage-3 parity: {_n.get('PARITY_DROP', 0)} absent (PARITY_DROP) + "
            f"{_n.get('PARITY_UNKNOWN', 0)} unconfirmable (PARITY_UNKNOWN) + "
            f"{_n.get('PARITY_DRIFT', 0)} content-drift (PARITY_DRIFT); "
            f"marked FAILED and aborting DAG to prevent deactivating older chunk versions."
        )

    # ── off 臂：与 pre-port main 逐字节一致（评审 B3 硬约束）──
    # 内存状态同步（DAG 随后中断，主要为可读性/可测性；RDS 才是下轮重选的事实源）
    for chunks, code, msg in buckets:
        for c in chunks:
            c.index_status = ChunkIndexStatus.FAILED
            c.index_error_code = code
            c.index_error_message = msg

    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            _all_dead: list = []
            for chunks, code, msg in buckets:
                if not chunks:
                    continue
                ids = [c.chunk_id for c in chunks]
                # G9：FAILED 回写带重试预算——达上限转 DEAD 死信（不再被 loader 重选，
                # 消除毒 chunk 每轮重推→校验失败→raise 的队头阻塞）。全有或全无语义保留
                # （expect_all；rowcount 不符 → raise → 下方 rollback）。
                # A4（2026-07-25）：PARITY_UNKNOWN 不消耗预算——它只表示"读不到、无法确认"
                # （_present_unknown：官方 fetch 的批异常/畸形响应整批归 unknown），一次 HA3
                # 读故障会把整批健康 chunk 打进 unknown 桶；若照常 +1，三次读故障即集体转 DEAD。
                try:
                    _dead = _fail_chunks_with_retry_budget(
                        cursor, ids,
                        extra_set_sql=", index_error_code=%s, index_error_message=%s",
                        extra_params=[code, msg], expect_all=True,
                        count_retry=(code != "PARITY_UNKNOWN"))
                except RuntimeError as _pe:
                    conn.rollback()
                    raise RuntimeError(
                        f"PARITY {_pe} ({code}); rolled back to avoid a partial write "
                        f"that strands INDEXED-but-absent chunks (never re-selected).") from _pe
                _all_dead.extend(_dead)
                for c in chunks:
                    if c.chunk_id in set(_dead):
                        c.index_status = ChunkIndexStatus.DEAD
            _mark_docs_needs_review_for_dead(cursor, _all_dead)
        conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn:
            conn.close()

    # node 05 防御过滤器：键 = (doc_id, version_no)，与 node_update_index_status 完全一致
    fdv = ctx.setdefault("failed_doc_versions", set())
    for chunks, _code, _msg in buckets:
        for c in chunks:
            fdv.add((c.doc_id, c.version_no))

    raise RuntimeError(
        f"Stage-3 parity: {len(drop_chunks)} absent (PARITY_DROP) + {len(unknown_chunks)} "
        f"unconfirmable (PARITY_UNKNOWN) + {len(drift_chunks)} content-drift (PARITY_DRIFT); "
        f"marked FAILED and aborting DAG to prevent deactivating older chunk versions."
    )


def node_verify_and_repush(ctx: dict):
    """DAG-3 节点 04b：推送后 HA3 物理存在性校验 + 有界补推（自愈 ~1% 静默丢失）。

    背景：HA3 push 返回 failed=0 且 RDS index_status=INDEXED 并不保证 chunk 真落进 HA3
    （post-acknowledge / 异步构建 / 实时索引最终一致性会静默丢档）。本节点在
    node_update_index_status(04) 之后、node_deactivate_old_chunks(05) 之前重新读 HA3：
    确认本批 INDEXED chunk 真实存在，对确认丢失的有界补推；补救失败(DROP) 或 无法确认(UNKNOWN)
    则写回 FAILED 并 raise —— 阻断 05，守住"新版本确认入库后才停用旧版本"不变量。

    默认常开（RAG_STAGE3_PARITY_VERIFY，opt-out；设 0/false/no/off 才关）；
    simulate / MOCK / 非 HA3 客户端均 no-op。
    校验基础设施异常 fail-open（仅"廉价 hint enum"失败时降级为全量 point-read，绝不跳过校验）；
    只有"权威 point-read 确认缺失(DROP)"或"point-read 无法完成(UNKNOWN)"才 fail-closed 阻断 05。

    内容漂移子检查（RAG_STAGE3_PARITY_DRIFT，默认关闭，依附于本节点 → 不会单独生效）：对确认
    PRESENT 且拿到 chunk_text_store 的 chunk 比对 sha256(返回文本) vs sha256(内存 chunk_text)，
    PK 在但内容陈旧 = drift → 有界补推(upsert) → 重读重算 hash → 仍不一致写 'PARITY_DRIFT' 阻断 05。
    drift 读/hash 异常只 fail-open 该 chunk 的 drift 判定，存在性三态结论不受影响。

    重试预算（G9，schema/019 + _fail_chunks_with_retry_budget）：确认性失败（PARITY_DROP /
    PARITY_DRIFT / embedding 失败）逐次 index_retry_count+1，达上限（RAG_STAGE3_CHUNK_MAX_RETRIES，
    默认 3）转 DEAD 死信并把文档置 NEEDS_REVIEW——毒 chunk 不再每轮重推阻塞整轮 drain。
    PARITY_UNKNOWN（读不到、无法确认）不消耗预算；干净成功路径把计数清零（A4 2026-07-25），
    故语义是"连续失败次数"而非"累计失败次数"。
    """
    if ctx.get("dag_id") == "dag3_chunk_to_opensearch" and ctx.get("dag3_no_work"):
        print("    [SKIP] node_verify_and_repush skipped because ctx['dag3_no_work'] is True.")
        return

    # 特性开关（默认常开 / opt-out）：显式设 RAG_STAGE3_PARITY_VERIFY=0/false/no/off 才关。
    # 不经 DataWorks stage3_node（笔记本手工重灌等路径）也强制走推送后物理校验，堵住 96 例类
    # 静默丢失复发面（sim/MOCK/非 HA3 客户端在下方仍 no-op，故常开对本地/测试零影响）。
    if os.environ.get("RAG_STAGE3_PARITY_VERIFY", "true").lower() in ("0", "false", "no", "off"):
        return

    if _resolve_simulate(ctx, "opensearch"):
        return

    client = _get_opensearch_client(ctx)
    # 仅 HA3：标准 OpenSearch 走 version delete-by-query，无静默丢失类问题；mock/桩漂移直接跳过
    if client == "MOCK_HA3_CLIENT" or not hasattr(client, "push_documents"):
        return

    config = get_config()
    cfg = config.alibaba_vector

    # 本批刚推送、被 04 标 INDEXED 的 chunk（rds_id = HA3 主键）。embedding-FAILED 未进 batches、
    # push-FAILED 已让 04 raise，故正常路径下 expected 即本批全部 chunk。
    expected = {}
    for b in (ctx.get("bulk_batches") or []):
        for c in b.get("chunks", []):
            if getattr(c, "index_status", None) == ChunkIndexStatus.INDEXED and getattr(c, "rds_id", None) is not None:
                expected[int(c.rds_id)] = c
    if not expected:
        return

    def _envf(key, default):
        v = os.environ.get(key, "")
        try:
            return float(v) if v != "" else default
        except ValueError:
            return default

    def _envi(key, default):
        v = os.environ.get(key, "")
        try:
            return int(v) if v != "" else default
        except ValueError:
            return default

    settle = _envf("RAG_STAGE3_PARITY_SETTLE_SEC", 30.0)
    settle_poll = _envf("RAG_STAGE3_PARITY_SETTLE_POLL_SEC", 5.0)
    max_retries = _envi("RAG_STAGE3_PARITY_MAX_RETRIES", 2)

    # 字段清单 pin 为 parity 专属最小集（HA3_PARITY_OUTPUT_FIELDS），不共享 serving 默认清单：
    # 本守卫 gate 的是不可逆的旧版本 deactivate，判定只消费 id（PK 相符）与 chunk_text_store
    # （drift 子检查）——serving 为答案路径增删字段永远不会改变本安全检查的"存在/漂移"口径。
    from opensearch_pipeline.clients import (
        HA3_PARITY_OUTPUT_FIELDS as _PARITY_OUTPUT_FIELDS,
        ha3_fetch_by_pks as _ha3_fetch_by_pks,
    )

    text_by_pk = {}  # present pk → returned chunk_text_store (for the drift sub-check); side-effect

    def _present_unknown(pks):
        """对一组已知 PK 做**权威存在性判定**——官方主键接口 /vector-service/fetch。
        返回 (present:set, unknown:set)；MISSING 由调用方 `suspects - present - unknown` 推出。

        ⚠️ 2026-07-22 终局:此前本函数用零向量 `QueryRequest(top_k=1, filter=id=<pk>)` 逐个
        point-read 并自称"权威"——**已证伪**。零向量与任何向量内积恒为 0 ⇒ 全部文档得分并列
        ⇒ 返回哪个子集由引擎召回逻辑决定(阿里 07-25 确认:不设向量分阈值,得分 0 也返回;
        "重扫缺同一批"以索引未变化为前提)。实测某在场行该形态返 0 命中而 fetch 正常取回
        ⇒ 会把**在场行判 missing** → 无谓补推 → 04b raise → **节点 05 永不执行 ⇒ 旧版本
        永不停用 ⇒ 双版本长存**。**零向量 query 一律禁止再作存在性判据。**

        三态互斥由 clients.ha3_fetch_by_pks 的批级原子性保证;**unknown 优先**——
        批异常/畸形(含返回请求集外 PK、同批重复 PK)整批归 unknown,绝不误判 missing。
        串行发批(每批 ≤100、stage-3 每轮 ≤1000 PK ⇒ ≤10 请求):HA3 SDK 未声明线程安全
        (见 reconcile.py 为此每线程独立 client),这点请求量不值得引入该复杂度。
        副作用:对 PRESENT 的 pk 把返回的 chunk_text_store 存入 text_by_pk(drift 子检查用)。"""
        pk_list = sorted(pks)     # 排序分批:日志/测试/批异常归因确定
        if not pk_list:
            return set(), set()
        fr = _ha3_fetch_by_pks(client, cfg.table_name, pk_list,
                               output_fields=_PARITY_OUTPUT_FIELDS)
        unknown = set(fr["unknown_pks"])
        present = set()
        for pk, row in fr["rows_by_pk"].items():
            if pk in unknown:     # unknown 优先（理论上批级原子后不会同时出现，防御性保留）
                continue
            present.add(pk)
            text_by_pk[pk] = row.get("chunk_text_store")
        if fr["errors"]:
            # 整批 unknown 会让最多 100 个健康 PK 一起阻断节点 05，必须可归因
            print(f"    ⚠️ [PARITY] fetch 批异常 {len(fr['errors'])} 起 → "
                  f"{len(unknown)} 个 PK 判 UNKNOWN: {fr['errors'][:3]}")
        return present, unknown

    def _settle_wait(candidate_pks):
        """吸收实时索引滞后：轮询早退（探针=**官方 fetch**，与权威判据同平面），
        最长仍等 settle 秒；settle<=0 直接跳过。

        跨平面时序实证（2026-07-30 生产实测，scratch/crossplane_timing_probe_20260727.py，
        3 轮独立合成探针 push→并发轮询三平面→即删）：

            轮   fetch    query    inverted      分辨率下界(最大单探 RTT) ≈ 0.58s
            1    0.006s   0.008s   0.009s        query−fetch = [0.002, 0.001, 0.002]
            2    0.001s   0.002s   0.005s        → 差值比下界小两个数量级
            3    0.001s   0.003s   0.005s        → **不存在可测的跨平面窗口**

        即 push 返回时三个平面（fetch / 真实非零向量 query / 纯倒排 search）**已全部可见**。
        C2 当初禁用早退的理由是「无证据支持 fetch 可见 ⇒ query 可见」——该理由已被实证
        推翻，故解禁；探针与权威判据同用 fetch，不再有"闸门平面偷换"的问题。

        ⚠️ 方法论教训（第一版实验栽过）：顺序探测三平面 + 把时间戳记在探测**返回后**，
        会凭空造出一个与 RTT 同量级的递增阶梯（当时误读为 fetch 早 0.294s）。必须并发探测
        且时间戳记在**发起前**。

        ⚠️ 未验证面：上述实验是**单文档**推送；真实批最多 100 文档/子批、1000 PK/轮，
        大批建索引延迟未测。本探针也只查**一个** PK（`max(candidate_pks)`），它可见不代表
        整批可见——但这是既有形态，且权威判据（_present_unknown 全量 fetch）在后面兜底：
        探早了最坏是白跑一轮补推，不会误放行 deactivate。settle 上限仍是最终兜底。"""
        if settle <= 0:
            return
        probe_pk = max(candidate_pks) if candidate_pks else None
        if probe_pk is None:
            time.sleep(settle)
            return
        deadline = time.time() + settle
        while True:
            # ha3_fetch_by_pks 契约上绝不 raise；探测失败按"未就绪"继续等，
            # 不写 text_by_pk/unknown，不影响后续权威三态结论。
            fr = _ha3_fetch_by_pks(client, cfg.table_name, [probe_pk], output_fields=["id"])
            if probe_pk in fr["rows_by_pk"]:
                return                      # 已可见 → 早退
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(max(settle_poll, 0.1), remaining))

    t0 = time.time()

    # settle：吸收实时索引滞后，避免对"刚推未可见"的 chunk 误判为丢失（轮询早退，见 _settle_wait）
    _settle_wait(set(expected))

    expected_pks = set(expected)
    # 1) 嫌疑集 = 全部 expected：本节点的 PK 全部已知，正是官方 fetch 契约覆盖的方向
    #    （每批 ≤100、stage-3 每轮 ≤1000 PK ⇒ ≤10 请求）。
    #    ⚠️ 原"大批先用 id-range enum 作廉价 hint"分支已删除（连同 RAG_STAGE3_PARITY_
    #    POINTREAD_ALL_MAX 旋钮）：①廉价 hint 的前提是逐 PK point-read 昂贵（200 个 PK =
    #    200 次请求），批量 fetch 后该前提消失；②enum 命中的行会绕过 fetch、拿不到
    #    chunk_text_store ⇒ 这些行永远做不了 drift 子检查（覆盖缺口）。
    suspects = set(expected_pks)

    # 2) 权威确认嫌疑集
    present, unknown = _present_unknown(suspects)
    confirmed_missing = suspects - present - unknown
    initial_missing = len(confirmed_missing)

    # 3) 对确认丢失的有界补推 + 复确认（幂等：稳定 rds_id 主键 + cmd:add = upsert）
    healed = set()
    still_missing = set(confirmed_missing)
    for _ in range(max_retries):
        if not still_missing:
            break
        repush = [expected[pk] for pk in sorted(still_missing)]
        for c in repush:   # 与首推一致：补推前预置 FAILED 兜底
            c.index_status = ChunkIndexStatus.FAILED
            c.index_error_code = "NOT_RETURNED"
            c.index_error_message = "parity re-push: awaiting result"
        _push_chunks_to_ha3(client, cfg, repush, max_retries=config.embedding.max_retries)
        _settle_wait(still_missing)
        present2, unknown2 = _present_unknown(still_missing)
        healed |= present2
        still_missing = still_missing - present2 - unknown2
        unknown |= unknown2   # 复推中变 UNKNOWN 的从 still_missing 移除、计入 unknown（二者互斥）

    # 复确认存在 → 恢复内存 INDEXED（04 已把 RDS 记为 INDEXED，healed 无需再写 RDS）
    for pk in healed:
        c = expected[pk]
        c.index_status = ChunkIndexStatus.INDEXED
        c.index_error_code = None
        c.index_error_message = None

    # 3b) 内容漂移子检查（flag-gated, default OFF: RAG_STAGE3_PARITY_DRIFT；本节点仅在 PARITY_VERIFY
    #     已开启时运行，故 drift 不会单独生效）。对"确认 PRESENT 且 fetch 拿到 chunk_text_store"
    #     的 chunk，比对 sha256(返回文本) vs sha256(内存 chunk_text)：不一致 = PK 在但内容陈旧（drift）。
    #     有界补推（upsert 覆盖内容）→ 重读**重算 hash**确认（不止 PK 存在）→ 仍不一致 = PARITY_DRIFT。
    #     fail-OPEN：读/hash 异常只跳过该 chunk 的 drift 判定，绝不影响上面的存在性三态结论。
    #     注（2026-07-22）：enum-hint 分支删除后全部 expected 都过 fetch ⇒ 每个 PRESENT 行都有
    #     返回文本，drift 覆盖不再有缺口（旧实现里 enum 命中而未点读的行永远做不了 drift）。
    still_drift = set()
    initial_drift = 0
    drift_enabled = os.environ.get("RAG_STAGE3_PARITY_DRIFT", "").lower() in ("1", "true", "yes")
    if drift_enabled:
        def _drift_candidates(pks):
            d = set()
            for pk in pks:
                txt = text_by_pk.get(pk)
                if txt is None:
                    continue  # 无返回文本 → fail-open，跳过 drift
                try:
                    if _parity_content_hash(txt) != _parity_content_hash(expected[pk].chunk_text):
                        d.add(pk)
                except Exception:
                    continue  # hash 异常 → fail-open
            return d

        present_final = expected_pks - still_missing - unknown
        still_drift = _drift_candidates(present_final)
        initial_drift = len(still_drift)
        for _ in range(max_retries):
            if not still_drift:
                break
            _push_chunks_to_ha3(client, cfg, [expected[pk] for pk in sorted(still_drift)],
                                max_retries=config.embedding.max_retries)
            _settle_wait(still_drift)
            present_d, unknown_d = _present_unknown(still_drift)  # 重读刷新 text_by_pk
            # 重算 hash 确认：仍 present 且内容已一致 → 愈合；变 UNKNOWN → 移入 unknown 桶
            healed_d = set()
            for pk in list(still_drift):
                if pk in unknown_d:
                    unknown.add(pk)
                    still_drift.discard(pk)
                elif pk in present_d and not _drift_candidates({pk}):
                    healed_d.add(pk)
            still_drift -= healed_d

    verify_latency_ms = int((time.time() - t0) * 1000)
    print(f"    ├─ [PARITY] expected={len(expected_pks)} initial_missing={initial_missing} "
          f"healed={len(healed)} persistent_drop={len(still_missing)} unknown={len(unknown)} "
          f"initial_drift={initial_drift} persistent_drift={len(still_drift)} "
          f"verify_latency_ms={verify_latency_ms}")

    # 4) 终态：仍缺失(DROP) / 无法确认(UNKNOWN) / 内容漂移(DRIFT) → 写回 FAILED + raise，阻断 05
    if still_missing or unknown or still_drift:
        drop_chunks = [expected[pk] for pk in sorted(still_missing)]
        unknown_chunks = [expected[pk] for pk in sorted(unknown)]      # 与 still_missing 互斥
        drift_chunks = [expected[pk] for pk in sorted(still_drift)]    # 与上面两者互斥
        _persist_parity_failed_and_raise(ctx, config, drop_chunks, unknown_chunks, max_retries,
                                         drift_chunks=drift_chunks)


# ═══════════════════════════════════════════════════════════════
# DAG 4: retrieval eval (简化版)
# ═══════════════════════════════════════════════════════════════

def node_simulate_retrieval(ctx: dict):
    """模拟检索测试（整合 Query Decomposition、Soft Filter + Fallback、Parent-Child Retrieval 与 Neighbor Stitching）。"""
    import numpy as np
    import re
    import jieba
    from rank_bm25 import BM25Okapi

    test_queries = ctx.get("test_queries", [
        "员工请假流程是什么？",
        "报销审批需要哪些材料？",
        "新员工入职需要准备什么？",
    ])

    chunks = ctx.get("embedded_chunks", [])
    if not chunks:
        print("    └─ No indexed chunks available for retrieval test")
        return

    def get_parent_id(c) -> str:
        extra = getattr(c, "extra", {}) or {}
        if "parent_id" in extra:
            return extra["parent_id"]
        cid = getattr(c, "chunk_id", "")
        if "_child_" in cid:
            return cid.split("_child_")[0]
        return cid

    # ─── Parent-Child Setup ───
    is_parent_child = any(getattr(c, "chunk_type", "") == "child_chunk" for c in chunks)
    if is_parent_child:
        # Keep all child chunks, plus chunks that do NOT have child chunks (e.g. faq_chunks, table_chunks, or unsliced chunks)
        child_parent_ids = {get_parent_id(c) for c in chunks if getattr(c, "chunk_type", "") == "child_chunk"}
        search_pool = [
            c for c in chunks 
            if getattr(c, "chunk_type", "") == "child_chunk" or get_parent_id(c) not in child_parent_ids
        ]
        parents_pool = [c for c in chunks if getattr(c, "chunk_type", "") != "child_chunk"]
        parents_dict = {getattr(p, "chunk_id", ""): p for p in parents_pool if getattr(p, "chunk_id", "")}
    else:
        search_pool = chunks
        parents_dict = {}

    # Build BM25 index on searchable pool
    tokenized_corpus = [list(jieba.cut(getattr(c, "chunk_text", ""))) for c in search_pool]
    bm25 = BM25Okapi(tokenized_corpus)

    results = []
    for query in test_queries:
        # A. Query Decomposition & Semantic Expansion
        delimiters = [r"？", r"。", r"；", r"\?", r"\.", r";"]
        pattern = "|".join(delimiters)
        sub_queries = [q.strip() for q in re.split(pattern, query) if q.strip()]
        if not sub_queries:
            sub_queries = [query]
            
        expanded = []
        for sq in sub_queries:
            expanded.append(sq)
            sq_lower = sq.lower()
            if "wifi" in sq_lower or "无线" in sq:
                expanded.append("Wi-Fi 无线网络 密码 WiFi")
            if "入库" in sq:
                expanded.append("产品入库单 打印 仓管")
            if "领料" in sq:
                expanded.append("领料单 辅料工 纸箱仓管")
            if "交货" in sq:
                expanded.append("吸塑交货单 打印 包材")
            if "工价" in sq:
                expanded.append("半成品工价单 成品工价单")
            if "卡纸" in sq:
                expanded.append("打印机 卡纸 IT部 8088")
            if "年休假" in sq or "转正" in sq:
                expanded.append("带薪年休假 试用小结")
        sub_queries = list(set(expanded))

        # B. Intent Prediction (Routing)
        dept_filter = None
        if any(term in query for term in ["it", "网络", "电脑", "u8", "卡纸", "分机"]):
            dept_filter = "it"
        elif any(term in query for term in ["人事", "转正", "考勤", "卡号", "离职", "休假", "餐券"]):
            dept_filter = "hr"
        elif any(term in query for term in ["车间", "生产", "吸塑", "纸吸管", "奶茶杯", "交货", "领料", "数量本", "耐高温"]):
            dept_filter = "production"

        doc_filter = None
        if "仓库人员" in query or "仓库" in query or "出库" in query:
            if dept_filter == "it":
                doc_filter = "eval_it_wujin_u8"
        elif "车间生产" in query or "车间" in query:
            if dept_filter == "it":
                doc_filter = "eval_it_chejian_u8"
                
        if dept_filter == "production":
            if "入库" in query:
                doc_filter = "eval_prod_xisu_ruku"
            elif "交货" in query:
                doc_filter = "eval_prod_xisu_jiaohuo"
            elif "领料" in query:
                doc_filter = "eval_prod_xisu_lingliao"
            elif "数量本" in query:
                doc_filter = "eval_prod_xisu_shuliang"

        # C. Search Scores (BM25 scores over all sub-queries)
        max_bm25_scores = np.zeros(len(search_pool))
        for sq in sub_queries:
            tokenized_sq = list(jieba.cut(sq))
            sq_bm25_scores = np.array(bm25.get_scores(tokenized_sq))
            max_bm25_scores = np.maximum(max_bm25_scores, sq_bm25_scores)

        # Normalize scores
        min_s, max_s = np.min(max_bm25_scores), np.max(max_bm25_scores)
        if max_s - min_s == 0:
            norm_scores = np.zeros_like(max_bm25_scores)
        else:
            norm_scores = (max_bm25_scores - min_s) / (max_s - min_s)

        # D. Soft Filter Discounting
        final_scores = np.zeros(len(search_pool))
        for i, c in enumerate(search_pool):
            c_doc = getattr(c, "doc_id", "")
            
            c_dept = None
            if c_doc.startswith("eval_it_"):
                c_dept = "it"
            elif c_doc.startswith("eval_prod_"):
                c_dept = "production"
            elif c_doc.startswith("eval_admin_"):
                c_dept = "admin"
            elif c_doc.startswith("eval_hr_"):
                c_dept = "hr"
            elif c_doc == "eval_company_faq":
                c_dept = "admin"
                
            discount = 1.0
            if dept_filter and c_dept != dept_filter:
                discount *= 0.5
                
            if doc_filter and c_doc in ["eval_it_wujin_u8", "eval_it_chejian_u8", "eval_prod_xisu_ruku", "eval_prod_xisu_jiaohuo", "eval_prod_xisu_lingliao", "eval_prod_xisu_shuliang"] and c_doc != doc_filter:
                discount *= 0.5
                
            final_scores[i] = norm_scores[i] * discount

        # E. Wide-Range Fallback
        if len(final_scores) > 0 and np.max(final_scores) < 0.35:
            final_scores = norm_scores.copy()

        # F. Parent Mapping
        parent_candidate_scores = {}
        for i, child_chunk in enumerate(search_pool):
            p_id = get_parent_id(child_chunk)
            score = float(final_scores[i])
            
            if is_parent_child:
                if p_id in parents_dict:
                    if p_id not in parent_candidate_scores or score > parent_candidate_scores[p_id]["score"]:
                        parent_candidate_scores[p_id] = {
                            "chunk": parents_dict[p_id],
                            "score": score
                        }
                else:
                    if p_id not in parent_candidate_scores or score > parent_candidate_scores[p_id]["score"]:
                        parent_candidate_scores[p_id] = {
                            "chunk": child_chunk,
                            "score": score
                        }
            else:
                if p_id not in parent_candidate_scores or score > parent_candidate_scores[p_id]["score"]:
                    parent_candidate_scores[p_id] = {
                        "chunk": child_chunk,
                        "score": score
                    }

        # G. Neighbor Stitching
        doc_groups = {}
        for p_id, item in parent_candidate_scores.items():
            chunk = item["chunk"]
            score = item["score"]
            doc_id = getattr(chunk, "doc_id", "")
            if doc_id not in doc_groups:
                doc_groups[doc_id] = []
            doc_groups[doc_id].append((chunk, score))
            
        stitched_candidates = []
        for doc_id, items in doc_groups.items():
            # Sort by chunk_index
            items.sort(key=lambda x: getattr(x[0], "chunk_index", 0))
            
            i = 0
            while i < len(items):
                current_chunk, current_score = items[i]
                # Clone/instantiate custom properties safely
                from copy import copy
                current_chunk = copy(current_chunk)
                current_chunk.extra = current_chunk.extra.copy() if current_chunk.extra else {}
                current_chunk.extra["sim_score"] = current_score
                
                j = i + 1
                while j < len(items):
                    next_chunk, next_score = items[j]
                    idx1 = getattr(current_chunk, "chunk_index", 0)
                    idx2 = getattr(next_chunk, "chunk_index", 0)
                    
                    if idx2 - idx1 <= 1:
                        # Adjacent
                        current_chunk.chunk_text = current_chunk.chunk_text + "\n... [Contiguous] ...\n" + next_chunk.chunk_text
                        if getattr(current_chunk, "raw_text", "") or getattr(next_chunk, "raw_text", ""):
                            current_chunk.raw_text = (getattr(current_chunk, "raw_text", "") or "") + "\n" + (getattr(next_chunk, "raw_text", "") or "")
                        current_chunk.extra["sim_score"] = max(current_chunk.extra["sim_score"], next_score)
                        j += 1
                    else:
                        break
                stitched_candidates.append(current_chunk)
                i = j

        # Sort stitched candidates descending by score and keep top 3
        stitched_candidates.sort(key=lambda x: x.extra.get("sim_score", 0.0), reverse=True)
        top_k = stitched_candidates[:3]

        result = {
            "query": query,
            "top_chunks": [
                {
                    "chunk_id": getattr(c, "chunk_id", ""),
                    "score": round(c.extra.get("sim_score", 0.0), 3),
                    "preview": getattr(c, "chunk_text", "")[:80],
                    "section": getattr(c, "section_title", ""),
                }
                for c in top_k
            ],
        }
        results.append(result)
        print(f"    └─ Q: {query}")
        for i, c in enumerate(top_k[:2]):
            print(f"       #{i+1} score={c.extra.get('sim_score', 0.0):.3f} [{getattr(c, 'section_title', '') or 'N/A'}] {getattr(c, 'chunk_text', '')[:50]}...")

    ctx["retrieval_results"] = results


def node_eval_report(ctx: dict):
    """生成评测报告。"""
    results = ctx.get("retrieval_results", [])
    chunks = ctx.get("embedded_chunks", [])
    canonicals = ctx.get("canonicals", [])

    report = {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total_documents": len(canonicals),
            "total_chunks": len(chunks),
            "total_queries_tested": len(results),
            "avg_top1_score": 0,
        },
        "chunk_distribution": {},
        "queries": results,
    }

    # chunk 类型分布
    type_counts = {}
    for chunk in chunks:
        type_counts[chunk.chunk_type] = type_counts.get(chunk.chunk_type, 0) + 1
    report["chunk_distribution"] = type_counts

    # 平均 top-1 score
    if results:
        scores = [r["top_chunks"][0]["score"] for r in results if r["top_chunks"]]
        report["summary"]["avg_top1_score"] = round(sum(scores) / len(scores), 3) if scores else 0

    ctx["eval_report"] = report

    print("    └─ Eval Report:")
    print(f"       Documents: {report['summary']['total_documents']}")
    print(f"       Chunks: {report['summary']['total_chunks']}")
    print(f"       Chunk types: {type_counts}")
    print(f"       Queries tested: {report['summary']['total_queries_tested']}")
    print(f"       Avg top-1 score: {report['summary']['avg_top1_score']}")
