# -*- coding: utf-8 -*-
"""
spot_checker.py — 定时安全抽检任务 (Spot-Check Safety Daemon)
"""
import logging
import os
import random
import time
import requests
import json
from opensearch_pipeline.config import get_config
from opensearch_pipeline.reindex_states import ChunkIndexStatus, DocVersionIndexStatus, sql_in_list
from opensearch_pipeline.pipeline_nodes import (
    _clean_llm_json_response,
    _get_db_conn,
    _get_opensearch_client,
    _search_delete_old_chunks,
)

logger = logging.getLogger(__name__)


def _lock_doc(cursor, doc_id: str) -> bool:
    """F3 逐文档串行化原语：document_meta 行 FOR UPDATE。

    锁序纪律（本文件 + cost_breaker + stage-2 duplicate-skip 已归一）：任何同事务
    触碰 ≥2 张 {document_meta, chunk_meta, document_version} 的写方，必须【先】取本锁，
    此后 chunk_meta 先于 document_version。console 端点（restore/retire/visibility）
    天然 meta-first（内部 dv→chunk 顺序被 meta 锁串行化掩护）；stage-3 是唯一不取
    meta 锁的写方（chunk→dv），与本纪律的 post-meta 顺序兼容——这不是全仓全序声明，
    只覆盖此处列名的写方族。返回 False = document_meta 行不存在。"""
    cursor.execute("SELECT doc_id FROM document_meta WHERE doc_id = %s FOR UPDATE", (doc_id,))
    return cursor.fetchone() is not None


def _reconcile_ha3_deadline_s() -> float:
    """F3：持锁跨 HA3 网络 I/O 的总时限（批间墙钟检查用）。

    钳位 [5, 25]s、默认 20——配合 bounded 客户端单请求 ≤15s（connect 5 + read 10、
    max_attempts=0 恰一次尝试），最坏持锁 ≈ 25+15=40s < innodb_lock_wait_timeout
    默认 50s。越界值钳位并告警，不允许配置出破坏锁时长上界的值。"""
    raw = os.environ.get("RAG_RECONCILE_HA3_DEADLINE_S", "20")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = 20.0
    clamped = min(max(val, 5.0), 25.0)
    if clamped != val:
        logger.warning(
            "[RECONCILE] RAG_RECONCILE_HA3_DEADLINE_S=%r 越界，钳位到 %.0fs（允许区间 [5,25]）",
            raw, clamped)
    return clamped


def _get_bounded_search_client():
    """F3：持锁对账专用搜索客户端（clients.py bounded=True：HA3 max_attempts=0 +
    connect 5s/read 10s 并构造后核验生效值；OpenSearch 本地分支 timeout=10）。
    能力缺失（SDK 形态变化吞掉 runtime options）在 clients.py 抛 RuntimeError——
    调用方必须 fail-closed（整批拒跑、零 HA3/RDS 写），绝不回退无界老路径。"""
    return _get_opensearch_client(bounded=True)

# 权限严重等级： public(0) < internal/dept_internal(1) < restricted(2)
_PERM_ORDER = {"public": 0, "internal": 1, "dept_internal": 1, "restricted": 2}


def _suggests_tightening(suggested_perm: str, current_permission: str) -> bool:
    """安全复审：LLM 建议的权限是否比现状更严（→ 触发隔离复核）。

    fail-closed（纯函数，可单测）：先归一化大小写/空白；【未知】suggested 按最严（宁可触发复审，
    绝不把不认识的安全建议当 public 放过——这正是此前 `.get(...,0)` 静默 fail-open 的洞）；
    未知 current 按最松（同样偏向触发复审）。
    """
    sp = (suggested_perm or "").strip().lower()
    cp = (current_permission or "").strip().lower()
    suggested_rank = _PERM_ORDER.get(sp, max(_PERM_ORDER.values()) + 1)
    current_rank = _PERM_ORDER.get(cp, 0)
    return suggested_rank > current_rank


def _spotcheck_concurrency() -> int:
    """安全复审 LLM 并发度（perf F#61）。RAG_SPOTCHECK_CONCURRENCY，默认 4（LLM 只读调用，
    与 image_funnel 的并发模式同型）；<=1 退回串行。quarantine 写路径不受影响、恒为主线程串行。"""
    try:
        return max(1, int(os.environ.get("RAG_SPOTCHECK_CONCURRENCY", "4")))
    except (TypeError, ValueError):
        return 4


def _safety_review_llm(title, doc_text, *, api_key, model_name, api_base_url):
    """单篇文档的二次安全复审 LLM 调用（纯只读 HTTP，无 DB/写副作用——可安全并发，F#61）。

    从 run_spot_check_pipeline 主循环机械抽出：schema/prompt/payload/超时/解析逻辑逐字保持。
    返回解析后的 safety_eval dict（safety_status / suggested_permission_level / reason）；
    任何失败向上抛，由调用方按既有 "Spot-check safety assessment failed" 口径聚合。
    """
    is_dashscope = "dashscope.aliyuncs.com" in api_base_url or "qwen" in model_name.lower()
    schema = {
        "type": "OBJECT",
        "properties": {
            "safety_status": {
                "type": "STRING",
                "description": "Must be either 'safe' or 'unsafe'. If document contains highly sensitive payroll, commercial secrets, or PII that shouldn't be public, mark 'unsafe'"
            },
            "suggested_permission_level": {
                "type": "STRING",
                "description": "Must be one of: 'public', 'internal', or 'restricted'"
            },
            "reason": {
                "type": "STRING",
                "description": "Detailed justification for safety classification and permission level suggestion"
            }
        },
        "required": ["safety_status", "suggested_permission_level", "reason"]
    }

    prompt_instructions = (
        "You are a Senior Corporate Security Compliance Auditor.\n"
        "Evaluate this corporate document text and verify if it is suitable to be public-safe or if it contains restricted/confidential information.\n"
        "Provide your structured review:\n"
        "- safety_status: 'safe' or 'unsafe'\n"
        "- suggested_permission_level: 'public', 'internal', or 'restricted'\n"
        "- reason: explain your reasoning\n\n"
    )

    if is_dashscope:
        # 与 funnel / ocr_client / vlm_rebuilder 共用同一 URL 构造（按域名重建路径，
        # 原实现对 /api/v1 这类原生 base 会拼出 /api/v1/compatible-mode/... 的坏 URL）
        from opensearch_pipeline.vlm_endpoint import compat_chat_completions_url
        url = compat_chat_completions_url(api_base_url)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        schema_str = json.dumps(schema, indent=2, ensure_ascii=False)
        system_prompt = (
            "You are a Senior Corporate Security Compliance Auditor.\n"
            "You MUST respond ONLY with a single valid JSON object adhering strictly to the schema below. Do not output any markdown code blocks, do not output your thinking process or any introductory text.\n"
            f"Required JSON Schema:\n{schema_str}"
        )
        user_prompt = (
            f"{prompt_instructions}"
            f"Document Title: {title}\n"
            f"Document Text:\n{doc_text[:8000]}\n\n"
            "Please output the required JSON object now."
        )
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.1
        }

        resp = requests.post(url, json=payload, headers=headers, timeout=90)
        if resp.status_code != 200:
            raise Exception(f"DashScope API returned status code {resp.status_code}: {resp.text}")

        data = resp.json()
        choices = data["choices"]
        text_content = choices[0]["message"]["content"]
        cleaned_content = _clean_llm_json_response(text_content)
        return json.loads(cleaned_content)

    url = f"{api_base_url}/models/{model_name}:generateContent"
    prompt = (
        f"{prompt_instructions}"
        f"Document Title: {title}\n"
        f"Document Text:\n{doc_text[:8000]}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.1
        }
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise Exception(f"Gemini API returned status code {resp.status_code}")

    result = resp.json()
    text_content = result["candidates"][0]["content"]["parts"][0]["text"]
    cleaned_content = _clean_llm_json_response(text_content)
    return json.loads(cleaned_content)


def _delete_chunks_from_index(doc_id: str, version_no: int, conn, config,
                              client=None, deadline_ts: float = None) -> None:
    """从搜索索引中删除指定文档的所有 chunks。

    成功时静默返回，失败时抛出异常由调用方处理。

    client（F3）：调用方传入 bounded 客户端时用之（持锁对账路径）；None = 自取
    默认客户端（quarantine 路径——它在 RDS 事务【外】先删 HA3，不持行锁跨网络）。
    deadline_ts：time.monotonic() 截止时刻，批间超限即抛（调用方回滚重试）。

    ⚠️ 有意不带 is_active 过滤：正常 retire/visibility 流程先把 chunk 停成
    is_active=0 再喂 PENDING_DELETE——按 (doc_id, version_no) 枚举全部 PK 是主路径
    的正确语义（加 is_active=1 过滤会让主路径一行都删不掉）。
    """
    os_client = client if client is not None else _get_opensearch_client()
    if os_client == "MOCK_HA3_CLIENT":
        # simulate 开关错配时绝不静默：mock 字符串会掉进 delete_by_query 分支炸出晦涩的
        # AttributeError；这里换成明确错误，由调用方按删除失败处理（PENDING_DELETE 重试）
        raise RuntimeError(
            "MOCK_HA3_CLIENT in real-mode index delete; simulate flags are inconsistent."
        )
    from opensearch_pipeline.env_guard import assert_destructive_write_allowed
    assert_destructive_write_allowed(
        "spot_check_delete",
        config.alibaba_vector.endpoint or config.alibaba_vector.instance_id or config.opensearch.host,
        kind="search")

    if hasattr(os_client, "push_documents"):
        # HA3 Engine: 查询 chunk 主键后用 push_documents cmd=delete 删除
        ha3_cfg = config.alibaba_vector
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id FROM chunk_meta
                WHERE doc_id = %s AND version_no = %s
            """, (doc_id, version_no))
            chunk_rows = cursor.fetchall()

        if chunk_rows:
            from alibabacloud_ha3engine_vector.models import PushDocumentsRequest

            delete_docs = [
                {"cmd": "delete", "fields": {ha3_cfg.pk_field: row[0]}}
                for row in chunk_rows
            ]
            ha3_batch_size = 100
            for i in range(0, len(delete_docs), ha3_batch_size):
                if deadline_ts is not None and time.monotonic() > deadline_ts:
                    raise RuntimeError(
                        f"HA3 delete deadline exceeded for {doc_id} v{version_no} "
                        f"after {i} of {len(delete_docs)} deletes (F3 bounded reconcile)")
                batch = delete_docs[i:i + ha3_batch_size]
                request = PushDocumentsRequest(body=batch)
                resp = os_client.push_documents(ha3_cfg.table_name, ha3_cfg.pk_field, request)
                logger.info(
                    "[HA3] Deleted batch %d (%d chunks) for %s v%s. Status: %s",
                    i // ha3_batch_size + 1, len(batch), doc_id, version_no,
                    getattr(resp, 'status_code', 'OK'),
                )
        else:
            logger.info("No chunks found in chunk_meta for %s v%s", doc_id, version_no)
    else:
        # Standard OpenSearch: delete_by_query
        delete_query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"doc_id": doc_id}},
                        {"term": {"version_no": version_no}}
                    ]
                }
            }
        }
        os_cfg = config.opensearch
        index_name = getattr(os_cfg, "index_name", "fuling_knowledge_v1")
        delete_resp = os_client.delete_by_query(index=index_name, body=delete_query)
        logger.info(
            "Deleted chunks from OpenSearch index '%s' for %s v%s. Response: %s",
            index_name, doc_id, version_no, delete_resp,
        )


def reconcile_pending_deletes() -> dict:
    """对账任务：重试所有 index_status='PENDING_DELETE' 的文档索引删除。

    在每次 spot-check 启动时自动调用，确保之前失败的索引删除最终完成。
    也可以独立调用（如 DataWorks 定时任务）。

    F3（2026-07-21 评审共识）竞态根治：批扫描只是【提示】；逐 doc 事务内先取
    document_meta 行锁（_lock_doc，console restore/retire/visibility 全被串行化在锁外）、
    锁内重验仍是 PENDING_DELETE，才执行不可逆的 HA3 删除；随后 chunk 停用【先于】
    版本 CAS（锁序对齐 stage-3 的 chunk→dv），CAS 落空 = 锁下不可能的未建模写方
    → 整笔回滚（chunk 写一并撤销）按 failed 上报。HA3 删除全程 bounded（单请求
    max_attempts=0 + 批间 deadline），持锁时长有确定上界。

    Returns:
        {"total": int, "success": int, "failed": int, "skipped_stale": int, "errors": [str]}
        skipped_stale = 锁内重验发现已被并发恢复/终态化而放弃的文档数。
    """
    result = {"total": 0, "success": 0, "failed": 0, "skipped_stale": 0, "errors": []}
    config = get_config()

    try:
        conn = _get_db_conn(select_db=True)
    except Exception as e:
        result["errors"].append(f"DB connect failed: {e}")
        return result

    try:
        with conn.cursor() as cursor:
            cursor.execute(f"""
                SELECT doc_id, version_no FROM document_version
                WHERE index_status = '{DocVersionIndexStatus.PENDING_DELETE}'
            """)
            rows = cursor.fetchall()

        result["total"] = len(rows)
        if not rows:
            return result

        logger.info("[RECONCILE] Found %d documents with PENDING_DELETE", len(rows))

        # fail-closed：bounded 客户端能力缺失 → 整批拒跑，零 HA3/RDS 写（绝不回退
        # 无界老路径——那正是被根治的竞态本体）。行保持 PENDING_DELETE 可重试。
        try:
            os_client = _get_bounded_search_client()
        except Exception as cap_err:
            result["failed"] = len(rows)
            result["errors"].append(
                f"bounded search client unavailable (fail-closed, no mutations): {cap_err}")
            logger.error("[RECONCILE] bounded 客户端不可用——fail-closed 整批拒跑: %s", cap_err)
            return result

        deadline_s = _reconcile_ha3_deadline_s()

        for doc_id, version_no in rows:
            try:
                # 事务边界：结束上一笔（rollback 从不落任何东西），锁内读拿新鲜读视图
                # （autocommit=False + REPEATABLE READ 下沿用批扫描快照会看不见并发恢复）
                conn.rollback()
                with conn.cursor() as cursor:
                    if not _lock_doc(cursor, doc_id):
                        conn.rollback()
                        result["failed"] += 1
                        result["errors"].append(f"document_meta row missing for {doc_id}")
                        continue
                    cursor.execute(
                        "SELECT index_status FROM document_version "
                        "WHERE doc_id = %s AND version_no = %s", (doc_id, version_no))
                    vrow = cursor.fetchone()
                if not vrow or vrow[0] != DocVersionIndexStatus.PENDING_DELETE:
                    conn.rollback()
                    result["skipped_stale"] += 1
                    logger.info(
                        "[RECONCILE] %s v%s 已非 PENDING_DELETE（现 %s）——并发恢复/终态化，跳过",
                        doc_id, version_no, vrow[0] if vrow else None)
                    continue

                _delete_chunks_from_index(
                    doc_id, version_no, conn, config,
                    client=os_client, deadline_ts=time.monotonic() + deadline_s)

                # 删除成功 → 停用 chunk_meta + 标记 DELETED（CS5：自洽——无论是谁喂的
                # PENDING_DELETE（spot-check 退役 / node_deactivate 失败兜底），对账成功后都把
                # chunk_meta 落到 is_active=0，避免留下 RDS-active 但 HA3 已删的孤儿（CS3 会报）。
                # 幂等：再跑命中 0 行。锁序：chunk 先于 dv（对齐 stage-3）。
                with conn.cursor() as cursor:
                    cursor.execute(f"""
                        UPDATE chunk_meta
                        SET is_active = FALSE, index_status = '{ChunkIndexStatus.DELETED}'
                        WHERE doc_id = %s AND version_no = %s AND is_active = 1
                    """, (doc_id, version_no))
                    cursor.execute(f"""
                        UPDATE document_version
                        SET index_status = '{DocVersionIndexStatus.DELETED}'
                        WHERE doc_id = %s AND version_no = %s
                          AND index_status = '{DocVersionIndexStatus.PENDING_DELETE}'
                    """, (doc_id, version_no))
                    cas_rows = cursor.rowcount
                if cas_rows != 1:
                    # meta 锁下本 CAS 不可能输给任何已建模写方——落空即异常：整笔回滚
                    # （chunk 停用一并撤销），观测现值入错误记录，下轮重试（HA3 删除幂等）。
                    conn.rollback()
                    observed = None
                    try:
                        with conn.cursor() as cursor:
                            cursor.execute(
                                "SELECT index_status FROM document_version "
                                "WHERE doc_id = %s AND version_no = %s", (doc_id, version_no))
                            _r = cursor.fetchone()
                            observed = _r[0] if _r else None
                    except Exception:  # noqa: BLE001 — 观测尽力而为
                        pass
                    conn.rollback()
                    result["failed"] += 1
                    err = (f"version CAS missed UNDER document_meta lock for {doc_id} "
                           f"v{version_no} (observed={observed}) — unmodeled writer, rolled back")
                    result["errors"].append(err)
                    logger.error("[RECONCILE] %s", err)
                    continue
                conn.commit()
                result["success"] += 1
                logger.info("[RECONCILE] Successfully deleted index for %s v%s", doc_id, version_no)

            except Exception as e:
                conn.rollback()
                result["failed"] += 1
                err = f"Retry delete failed for {doc_id} v{version_no}: {e}"
                result["errors"].append(err)
                logger.warning("[RECONCILE] %s", err)
    finally:
        conn.close()

    return result


def reconcile_stranded_versions() -> dict:
    """搁浅版本对账：修复「新版本已全量 INDEXED、旧版本 chunk 仍 active」的双版本文档。

    成因：DAG 3 部分失败时 node_update_index_status raise → node_deactivate_old_chunks
    被跳过，orchestrator 回滚把同一跑里**全量推送成功**的文档也标成 FAILED；它们的
    chunk_meta 已是 INDEXED，stage-3 loader（只重选 NOT_INDEXED/FAILED 的 chunk）永远
    不会再碰它们 → 新旧两个版本同时被检索，且无任何任务能自愈。

    与 reconcile_pending_deletes 同型：先删搜索索引里的旧 chunk，成功后才停用 RDS 旧
    chunk 并把 document_version 修成 SUCCESS —— 索引删除失败时 RDS 不动，文档保持可
    检测，下次运行重试。逐文档提交，单文档失败不影响其余。本函数绝不抛异常。

    F3（2026-07-21 评审共识）竞态根治：批扫描只是【提示】；逐 doc 事务内先取
    document_meta 行锁（_lock_doc），锁内以 FOR UPDATE 锁住该 doc 全部 active chunk
    行（含范围/间隙锁，封同 doc 幻影插入）并在 Python 侧重验完整候选事实（当前版本
    仍是最新 active、≥1 INDEXED 且 0 非 INDEXED、旧版本仍有 active chunk、未隔离、
    PROCESSING 陈旧判据以 SQL NOW() 重施），全部成立才执行不可逆 HA3 删除。版本收尾
    按锁内观测态分派（对 stage-3 这一唯一不取 meta 锁的写方 CAS 兜底）：
      · SUCCESS → 已终态，无需写（同值 UPDATE 的 changed-rows=0 不可判赢输，故不写）；
      · FAILED/NOT_INDEXED → 精确观测态 CAS → SUCCESS；
      · 陈旧 PROCESSING → (index_status='PROCESSING' AND updated_at=观测值) 时间戳绑定
        CAS——takeover/live worker 刷新 updated_at 必使本 CAS 落空（healer 认输）；
    CAS 落空 → 旧 chunk 停用保留（与胜者的 node_deactivate 语义一致、幂等）、
    supersede 跳过（success-before-supersede 不变量）、计 skipped_stale。

    Returns:
        {"total": int, "success": int, "failed": int, "skipped_stale": int, "errors": [str]}
    """
    result = {"total": 0, "success": 0, "failed": 0, "skipped_stale": 0, "errors": []}
    config = get_config()

    try:
        conn = _get_db_conn(select_db=True)
    except Exception as e:
        result["errors"].append(f"DB connect failed: {e}")
        return result

    try:
        with conn.cursor() as cursor:
            cursor.execute(f"""
                SELECT cm_new.doc_id, cm_new.version_no
                FROM (
                    SELECT doc_id, MAX(version_no) AS version_no
                    FROM chunk_meta WHERE is_active = 1
                    GROUP BY doc_id
                ) cm_new
                JOIN document_version dv
                  ON dv.doc_id = cm_new.doc_id AND dv.version_no = cm_new.version_no
                WHERE dv.status = 'active'
                  AND (dv.publish_status IS NULL OR dv.publish_status != 'QUARANTINED')
                  AND dv.index_status NOT IN ({sql_in_list((DocVersionIndexStatus.PENDING_DELETE,
                                                            DocVersionIndexStatus.DELETED))})
                  AND (dv.index_status != '{DocVersionIndexStatus.PROCESSING}'
                       OR dv.updated_at < NOW() - INTERVAL 2 HOUR)
                  AND EXISTS (SELECT 1 FROM chunk_meta o
                              WHERE o.doc_id = cm_new.doc_id
                                AND o.version_no < cm_new.version_no AND o.is_active = 1)
                  AND EXISTS (SELECT 1 FROM chunk_meta n
                              WHERE n.doc_id = cm_new.doc_id
                                AND n.version_no = cm_new.version_no
                                AND n.is_active = 1 AND n.index_status = '{ChunkIndexStatus.INDEXED}')
                  AND NOT EXISTS (SELECT 1 FROM chunk_meta n2
                                  WHERE n2.doc_id = cm_new.doc_id
                                    AND n2.version_no = cm_new.version_no
                                    AND n2.is_active = 1 AND n2.index_status != '{ChunkIndexStatus.INDEXED}')
                LIMIT 200
            """)
            # 谓词解读：最新 active 版本的 chunk【全部】INDEXED（≥1 条，"已验证索引成功"）、
            # 旧版本仍有 active chunk（搁浅特征）、未被隔离、未进删除队列（PENDING_DELETE/
            # DELETED = retire/visibility 正在下线，修回 SUCCESS 恒错）、且不与 2h 内在跑的
            # stage-3 抢锁。此扫描只产【提示】，权威判定在下方逐 doc 的 meta 锁内重验。
            rows = cursor.fetchall()

        result["total"] = len(rows)
        if not rows:
            return result

        logger.info(
            "[RECONCILE] Found %d stranded doc version(s) (new fully INDEXED, old still active)",
            len(rows),
        )

        # fail-closed：bounded 客户端能力缺失 → 整批拒跑，候选停留原搁浅态（可重试），
        # 零 HA3/RDS 写。绝不回退无界老路径。
        try:
            os_client = _get_bounded_search_client()
        except Exception as cap_err:
            result["failed"] = len(rows)
            result["errors"].append(
                f"bounded search client unavailable (fail-closed, no mutations): {cap_err}")
            logger.error("[RECONCILE] bounded 客户端不可用——fail-closed 整批拒跑: %s", cap_err)
            return result

        index_name = getattr(config.opensearch, "index_name", "fuling_knowledge_v1")
        deadline_s = _reconcile_ha3_deadline_s()

        for doc_id, version_no in rows:
            try:
                # 事务边界：rollback 结束上一笔（绝不落东西），锁内读拿新鲜读视图
                conn.rollback()
                with conn.cursor() as cursor:
                    if not _lock_doc(cursor, doc_id):
                        conn.rollback()
                        result["failed"] += 1
                        result["errors"].append(f"document_meta row missing for {doc_id}")
                        continue
                    # 权威重验 ①：版本行观测（陈旧判据以 SQL NOW() 重施——等 meta 锁期间
                    # stage-3 可能装入了【新鲜】PROCESSING，绝不能拿新时间戳当陈旧候选）
                    cursor.execute(f"""
                        SELECT index_status, updated_at, status, publish_status,
                               (index_status != '{DocVersionIndexStatus.PROCESSING}'
                                OR updated_at < NOW() - INTERVAL 2 HOUR) AS proc_stale_ok
                        FROM document_version WHERE doc_id = %s AND version_no = %s
                    """, (doc_id, version_no))
                    vrow = cursor.fetchone()
                    if (not vrow
                            or str(vrow[2] or "").lower() != "active"
                            or str(vrow[3] or "").upper() == "QUARANTINED"
                            or vrow[0] in (DocVersionIndexStatus.PENDING_DELETE,
                                           DocVersionIndexStatus.DELETED)
                            or not vrow[4]):
                        conn.rollback()
                        result["skipped_stale"] += 1
                        logger.info(
                            "[RECONCILE] %s v%s 候选重验不成立（版本态变化/新鲜 PROCESSING/隔离/下线中），跳过",
                            doc_id, version_no)
                        continue
                    observed_status, observed_ts = vrow[0], vrow[1]
                    # 权威重验 ②：锁住该 doc 全部 active chunk 行（FOR UPDATE 含间隙锁，
                    # 封同 doc 幻影插入直到提交），Python 侧验证完整性事实。
                    cursor.execute("""
                        SELECT id, version_no, index_status FROM chunk_meta
                        WHERE doc_id = %s AND is_active = 1
                        FOR UPDATE
                    """, (doc_id,))
                    all_chunks = cursor.fetchall()
                cur_statuses = [r[2] for r in all_chunks if r[1] == version_no]
                old_ids = [r[0] for r in all_chunks if r[1] < version_no]
                max_active_ver = max((r[1] for r in all_chunks), default=None)
                if (max_active_ver != version_no
                        or not cur_statuses
                        or any(s != ChunkIndexStatus.INDEXED for s in cur_statuses)
                        or not old_ids):
                    conn.rollback()
                    result["skipped_stale"] += 1
                    logger.info(
                        "[RECONCILE] %s v%s chunk 完整性重验不成立（重分块/可见度标脏/已收敛），跳过",
                        doc_id, version_no)
                    continue

                # 先删索引、成功后才动 RDS（与 node_deactivate_old_chunks 同序）；bounded
                _search_delete_old_chunks(os_client, config, index_name, doc_id, version_no,
                                          old_ids, deadline_ts=time.monotonic() + deadline_s)

                finalized = False
                with conn.cursor() as cursor:
                    # 锁序：chunk 先于 dv（对齐 stage-3）
                    cursor.execute(f"""
                        UPDATE chunk_meta
                        SET is_active = FALSE, index_status = '{ChunkIndexStatus.DELETED}'
                        WHERE doc_id = %s AND version_no < %s AND is_active = 1
                    """, (doc_id, version_no))
                    # 版本收尾分派（docstring §F3）
                    if observed_status == DocVersionIndexStatus.SUCCESS:
                        cursor.execute("""
                            SELECT index_status FROM document_version
                            WHERE doc_id = %s AND version_no = %s
                            FOR UPDATE
                        """, (doc_id, version_no))
                        _cur = cursor.fetchone()
                        finalized = bool(_cur) and _cur[0] == DocVersionIndexStatus.SUCCESS
                    elif observed_status == DocVersionIndexStatus.PROCESSING:
                        cursor.execute(f"""
                            UPDATE document_version
                            SET index_status = '{DocVersionIndexStatus.SUCCESS}'
                            WHERE doc_id = %s AND version_no = %s
                              AND index_status = '{DocVersionIndexStatus.PROCESSING}'
                              AND updated_at = %s
                        """, (doc_id, version_no, observed_ts))
                        finalized = cursor.rowcount == 1
                    else:
                        cursor.execute(f"""
                            UPDATE document_version
                            SET index_status = '{DocVersionIndexStatus.SUCCESS}'
                            WHERE doc_id = %s AND version_no = %s
                              AND index_status = %s
                        """, (doc_id, version_no, observed_status))
                        finalized = cursor.rowcount == 1
                    if finalized:
                        # 版本级 supersede 兜底（与 node_deactivate_old_chunks 收尾同语义，
                        # 2026-07-12 双 active 审计缺口）：仅在当前版本 SUCCESS 已锁定成立时
                        # 执行（success-before-supersede 不变量）。CAS on status='active'
                        # 幂等、不碰 retired。
                        cursor.execute("""
                            UPDATE document_version
                            SET status = 'superseded'
                            WHERE doc_id = %s AND version_no < %s AND status = 'active'
                        """, (doc_id, version_no))
                conn.commit()
                if finalized:
                    result["success"] += 1
                    logger.info(
                        "[RECONCILE] Healed stranded version %s v%s (deactivated %d old chunks)",
                        doc_id, version_no, len(old_ids),
                    )
                else:
                    # 收尾 CAS 落空 = stage-3 胜出（claim/takeover 刷新）：旧 chunk 停用保留
                    # （胜者终态化时本来也会做，幂等），supersede 交胜者自己的收尾路径。
                    result["skipped_stale"] += 1
                    logger.warning(
                        "[RECONCILE] %s v%s 版本收尾 CAS 落空（stage-3 并发接管），"
                        "旧 chunk 停用保留、supersede 跳过", doc_id, version_no)
            except Exception as e:
                conn.rollback()
                result["failed"] += 1
                err = f"Stranded-version heal failed for {doc_id} v{version_no}: {e}"
                result["errors"].append(err)
                logger.warning("[RECONCILE] %s", err)
    except Exception as e:
        # 检测查询/客户端初始化等整体失败：报告但不抛（对账失败不阻断当日入库）
        err = f"reconcile_stranded_versions aborted: {e}"
        result["errors"].append(err)
        logger.error("[RECONCILE] %s", err, exc_info=True)
    finally:
        conn.close()

    return result


def run_spot_check_pipeline(limit_or_percent: float = 0.05, simulate: bool = None) -> dict:
    """
    安全定时抽检守护任务：
    1. 从 RDS 中加载所有已成功发布 (index_status='SUCCESS') 的文档版本。
    2. 随机抽取其中 5% 的文档 (至少 1 篇，如果有的话)。
    3. 重构文档文本，并提交给二次独立的 Gemini 3.1 Flash Lite 实例，进行安全防泄漏及权限合理性审查。
    4. 比对建议权限和当前已发布权限。若发生降级 mismatches (如公开文档被识别为受限 restricted)，
       立即执行隔离锁定 (Quarantine)：
       - 标记 document_version risk_level='high'，publish_status='QUARANTINED'，gate_status='quarantined'
       - 停用 RDS 中该版本的所有 chunks (is_active=FALSE)
       - 从 OpenSearch 索引中彻底 DELETE 这些 chunks，保证不泄露
       - 在 review_task 注册一条人工审核任务
    """
    config = get_config()
    if simulate is None:
        simulate = config.simulate

    report = {
        "total_indexed_documents": 0,
        "sampled_documents": 0,
        "checked_documents": 0,
        "mismatch_detected": 0,
        "unsafe_flagged": 0,
        "quarantined_documents": [],
        "errors": []
    }

    if simulate:
        print("🔍 [SIMULATED SPOT CHECK] Starting spot-check safety checker (simulate=True)...")
        report["total_indexed_documents"] = 10
        report["sampled_documents"] = 1
        report["checked_documents"] = 1
        return report

    print("🔍 [SPOT CHECK] Starting spot-check safety checker (simulate=False)...")

    # 先对账：重试之前失败的索引删除
    reconcile_result = reconcile_pending_deletes()
    if reconcile_result["total"] > 0:
        print(f"    └─ [RECONCILE] Retried {reconcile_result['total']} pending deletes: "
              f"{reconcile_result['success']} success, {reconcile_result['failed']} failed, "
              f"{reconcile_result.get('skipped_stale', 0)} skipped-stale")
        report["errors"].extend(reconcile_result["errors"])

    # 再对账：修复搁浅的双版本文档（orchestrator stage-3 启动前也会跑，这里是独立兜底）
    stranded_result = reconcile_stranded_versions()
    if stranded_result["total"] > 0:
        print(f"    └─ [RECONCILE] Healed {stranded_result['success']}/{stranded_result['total']} "
              f"stranded doc versions, {stranded_result['failed']} failed, "
              f"{stranded_result.get('skipped_stale', 0)} skipped-stale")
        report["errors"].extend(stranded_result["errors"])
    # 三对账：清理重灌残留的孤儿 PK（同 chunk_id 双 PK / chunk_meta 已不认账的旧 id）
    from opensearch_pipeline.ha3_reconcile import reconcile_ha3_orphan_pks
    orphan_pk_result = reconcile_ha3_orphan_pks()
    if orphan_pk_result["stale"] > 0 or orphan_pk_result["errors"]:
        print(f"    └─ [RECONCILE] HA3 orphan PKs: checked={orphan_pk_result['checked']} "
              f"stale={orphan_pk_result['stale']} deleted={orphan_pk_result['deleted']}")
        report["errors"].extend(orphan_pk_result["errors"])
    try:
        conn = _get_db_conn(select_db=True)
    except Exception as e:
        err_msg = f"Failed to connect to RDS for spot-check: {e}"
        print(f"    ❌ {err_msg}")
        report["errors"].append(err_msg)
        return report

    # 1. 查询所有已发布到 OpenSearch 的文档
    docs_to_check = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"""
                SELECT dv.doc_id, dv.version_no, dm.permission_level, dm.title, dm.owner_dept
                FROM document_version dv
                JOIN document_meta dm ON dv.doc_id = dm.doc_id
                WHERE dv.index_status = '{DocVersionIndexStatus.SUCCESS}' AND dv.status = 'active'
            """)
            rows = cursor.fetchall()
            for r in rows:
                docs_to_check.append({
                    "doc_id": r[0],
                    "version_no": r[1],
                    "permission_level": r[2],
                    "title": r[3],
                    "owner_dept": r[4]
                })
    except Exception as e:
        err_msg = f"Failed to query successfully indexed documents: {e}"
        print(f"    ❌ {err_msg}")
        report["errors"].append(err_msg)
        conn.close()
        return report

    report["total_indexed_documents"] = len(docs_to_check)
    if not docs_to_check:
        print("    ℹ️ No published documents found in index. Skipping spot check.")
        conn.close()
        return report

    # 2. 随机采样 5%
    sample_size = max(1, int(len(docs_to_check) * limit_or_percent))
    sampled = random.sample(docs_to_check, sample_size)
    report["sampled_documents"] = len(sampled)
    print(f"    └─ Sampled {len(sampled)} documents out of {len(docs_to_check)} (approx. {limit_or_percent * 100}%)")

    llm_cfg = config.llm
    api_key = llm_cfg.api_key
    model_name = llm_cfg.model
    api_base_url = llm_cfg.api_base_url

    if not api_key:
        err_msg = "Gemini API key is not configured. Cannot perform live safety spot-check."
        print(f"    ⚠️ {err_msg}")
        report["errors"].append(err_msg)
        conn.close()
        return report

    # ── perf F#61 三段式 ─────────────────────────────────────────────
    # ① 串行重构文本（共享 conn 的 DB 读，连接非线程安全）→ ② 并发 LLM 复审（纯只读
    # HTTP，RAG_SPOTCHECK_CONCURRENCY 默认 4）→ ③ 主线程串行裁决 + 隔离写（quarantine
    # 命中率极低，写路径/事务语义保持原样；report 聚合恒在主线程，天然线程安全）。
    review_docs = []   # [(doc, doc_text)] —— 顺序与 sampled 一致（跳过重构失败/空文本）
    for doc in sampled:
        doc_id = doc["doc_id"]
        version_no = doc["version_no"]
        current_permission = doc["permission_level"]
        title = doc["title"]
        print(f"    📄 Checking doc: {doc_id} v{version_no} (title='{title}', current_permission='{current_permission}')...")

        # 3. 重构文档文本（从 chunk_meta 中拼合）
        text_parts = []
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT chunk_text FROM chunk_meta
                    WHERE doc_id = %s AND version_no = %s
                    ORDER BY chunk_index ASC
                """, (doc_id, version_no))
                chunks = cursor.fetchall()
                for c in chunks:
                    text_parts.append(c[0])
        except Exception as e:
            print(f"    ⚠️ Failed to reconstruct text for {doc_id}: {e}")
            report["errors"].append(f"Text reconstruction error for {doc_id}: {e}")
            continue

        doc_text = "\n".join(text_parts)
        if not doc_text.strip():
            print(f"    ⚠️ Reconstructed text for {doc_id} is empty. Skipping.")
            continue
        review_docs.append((doc, doc_text))

    # 4. 调用 secondary/safety LLM check（并发；单篇失败以异常对象占位，③ 段按原口径聚合）
    def _review(item):
        d, txt = item
        return _safety_review_llm(d["title"], txt, api_key=api_key,
                                  model_name=model_name, api_base_url=api_base_url)

    outcomes = []
    _conc = min(_spotcheck_concurrency(), max(1, len(review_docs)))
    if _conc > 1 and len(review_docs) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=_conc) as pool:
            futures = [pool.submit(_review, it) for it in review_docs]
            for fut in futures:
                try:
                    outcomes.append(fut.result())
                except Exception as e:  # noqa: BLE001 — 单篇 LLM 失败不拖垮整轮
                    outcomes.append(e)
    else:
        for it in review_docs:
            try:
                outcomes.append(_review(it))
            except Exception as e:  # noqa: BLE001
                outcomes.append(e)

    # 5+. 主线程串行裁决与隔离（写路径与事务顺序与原实现逐字保持）
    for (doc, _doc_text), outcome in zip(review_docs, outcomes):
        doc_id = doc["doc_id"]
        version_no = doc["version_no"]
        current_permission = doc["permission_level"]

        try:
            if isinstance(outcome, Exception):
                raise outcome
            safety_eval = outcome
            suggested_perm = safety_eval["suggested_permission_level"]
            safety_status = safety_eval["safety_status"]
            reason = safety_eval["reason"]

            report["checked_documents"] += 1
            print(f"       └─ Safety Check Result: status={safety_status}, suggested_permission={suggested_perm}")

            # 5. 安全等级比对 (权限降级判定) —— 纯函数 _suggests_tightening（fail-closed，可单测）
            if _suggests_tightening(suggested_perm, current_permission):
                # 判定为权限泄露 mismatch！触发 quarantine 锁定
                print(f"       🚨 SECURITY WARNING: Permission mismatch detected for {doc_id}! Indexed as '{current_permission}' but spot-check recommends '{suggested_perm}'. Reason: {reason}")
                report["mismatch_detected"] += 1
                
                # 执行隔离 (Quarantine)
                # ⚠️ 顺序不变量：先删 HA3（唯一影响检索的动作），成功后才 commit RDS 隔离态。
                # 检索在 HA3 端做权限过滤（与 RDS is_active 无关），故先 commit is_active=FALSE 并不能
                # 把文档移出检索——只有 HA3 delete 能。若先 commit RDS 再删 HA3，删失败的窗口里 RDS 报
                # 「已隔离」而文档仍以【旧的过宽权限】被检索命中（越权泄漏）。与 node_deactivate_old_chunks
                # / reconcile_stranded_versions 的「先删索引后动 RDS」对齐。_delete_chunks_from_index 按
                # (doc_id,version_no) 枚举 PK（不看 is_active），此处 is_active 尚为 TRUE 不影响其取键。
                task_id = f"spot_rev_{doc_id}_v{version_no}"
                review_reason = f"Spot-check permission level mismatch: current={current_permission}, suggested={suggested_perm}. Reason: {reason}"
                # Defensively truncate to prevent database column VARCHAR(500) limit issues
                if review_reason and len(review_reason) > 490:
                    review_reason = review_reason[:490] + "..."
                _review_sql = """
                    INSERT INTO review_task (
                        task_id, doc_id, version_no, review_key, review_type, review_reason, review_status,
                        owner_dept, suggested_category_l1, suggested_category_l2, suggested_permission_level, confidence_score
                    ) VALUES (
                        %s, %s, %s, %s, 'spot_check_mismatch', %s, 'PENDING',
                        %s, 'reference', 'unknown', %s, 0.5
                    ) ON DUPLICATE KEY UPDATE
                        review_reason = VALUES(review_reason),
                        review_status = 'PENDING',
                        suggested_permission_level = VALUES(suggested_permission_level)
                """
                _review_params = (task_id, doc_id, version_no,
                                  f"processing/canonical/{doc_id}/v{version_no}/content.md",
                                  review_reason, doc["owner_dept"], suggested_perm)

                # Phase 1: 先删 HA3（唯一影响检索的动作）
                try:
                    _delete_chunks_from_index(doc_id, version_no, conn, config)
                except Exception as os_err:
                    # HA3 删除失败 → 文档仍在检索。不翻 is_active/permission/QUARANTINED（RDS 如实反映
                    # 「仍在服务」，绝不谎报已隔离），只标 PENDING_DELETE 供重试 + 登记 review_task 供人工
                    # 介入；下次 spot-check/对账重新命中同一 mismatch 会重试删除。
                    logger.error(
                        "Failed to delete chunks from search index for %s v%s: %s",
                        doc_id, version_no, os_err, exc_info=True,
                    )
                    report["errors"].append(f"Search index delete error for {doc_id}: {os_err}")
                    try:
                        conn.begin()
                        with conn.cursor() as cursor:
                            cursor.execute(f"""
                                UPDATE document_version
                                SET index_status = '{DocVersionIndexStatus.PENDING_DELETE}'
                                WHERE doc_id = %s AND version_no = %s
                            """, (doc_id, version_no))
                            cursor.execute(_review_sql, _review_params)
                        conn.commit()
                        print(f"       ⚠️ HA3 删除失败：{doc_id} v{version_no} 标 PENDING_DELETE 待重试（未翻隔离态，文档仍在检索）")
                    except Exception as mark_err:
                        conn.rollback()
                        logger.error(
                            "Failed to mark PENDING_DELETE for %s v%s: %s",
                            doc_id, version_no, mark_err,
                        )
                    continue

                # Phase 2: HA3 已删（文档此刻起已不在检索）→ 安全 commit RDS 隔离终态
                try:
                    conn.begin()
                    with conn.cursor() as cursor:
                        # F3 锁序纪律：meta 先行（document_meta → chunk_meta → document_version，
                        # 与 reconciler/_lock_doc 族统一）——原 dv→meta→chunk 序与 meta-first
                        # 写方、以及 stage-3 的 chunk→dv 序都可能成环死锁。单次 commit 原子性
                        # 不变，写序纯粹是锁获取顺序问题。
                        # 批次9（ultra P3 spot_checker:686 改判）：LLM 的 suggested_perm 过
                        # normalize_permission_level 再落库——词表外值（如 'internal'）原样写入
                        # 会让检索过滤两个分支都不命中（历史 internal≠dept_internal 事故同类）；
                        # 归一后别名收敛、未知值 fail-closed 到 restricted。
                        from opensearch_pipeline.kb_authz import normalize_permission_level
                        cursor.execute("""
                            UPDATE document_meta
                            SET permission_level = %s,
                                kb_type = 'private'
                            WHERE doc_id = %s
                        """, (normalize_permission_level(suggested_perm), doc_id))

                        cursor.execute("""
                            UPDATE chunk_meta
                            SET is_active = FALSE
                            WHERE doc_id = %s AND version_no = %s
                        """, (doc_id, version_no))

                        # ⚠️ content_process_status 必须是终态 'QUARANTINED'，不能用 'FAILED'：
                        # 'FAILED' 正好命中 stage-2 的抢占谓词（FAILED AND retry_count<3），
                        # 下一次日跑会重新分块/重新发布，把隔离悄悄撤销掉。
                        cursor.execute(f"""
                            UPDATE document_version
                            SET risk_level = 'high',
                                publish_status = 'QUARANTINED',
                                gate_status = 'quarantined',
                                content_process_status = 'QUARANTINED',
                                content_process_error = %s,
                                index_status = '{DocVersionIndexStatus.DELETED}'
                            WHERE doc_id = %s AND version_no = %s
                        """, (f"[SPOT CHECK MISMATCH] Spot-check recommends tightening permission to {suggested_perm}", doc_id, version_no))

                        cursor.execute(_review_sql, _review_params)
                    conn.commit()
                    print(f"       └─ ✅ Chunks deleted from index + RDS quarantined for {doc_id} v{version_no}")
                except Exception as db_err:
                    # HA3 已删但 RDS 隔离态提交失败 → 文档已不在检索（无泄漏），仅 RDS 落后；
                    # CS3 探针/对账检出 is_active=1 而 HA3 缺失并自愈（与 deactivate 同风险面，幂等）。
                    conn.rollback()
                    print(f"       ⚠️ HA3 已删但 RDS 隔离态提交失败（对账自愈）: {db_err}")
                    report["errors"].append(f"RDS quarantine error for {doc_id}: {db_err}")
                    continue

                report["quarantined_documents"].append({
                    "doc_id": doc_id,
                    "version_no": version_no,
                    "previous_permission": current_permission,
                    "suggested_permission": suggested_perm,
                    "reason": reason
                })

            elif (safety_status or "").strip().lower() == "unsafe":
                # #F-spot-safety 复审判定 unsafe 但未建议收紧权限（如 current==suggest）时，
                # 原逻辑只看 _suggests_tightening → 该 unsafe verdict 被完全丢弃，文档继续留在
                # 索引可检索、安全兜底静默失效。此处即便权限未收紧，也登记一条 PENDING 人工审核任务
                # （不隔离/不删索引，交人工裁决），确保 unsafe 结论不被吞掉。
                print(f"       ⚠️ SAFETY UNSAFE (无权限收紧建议) for {doc_id}: {reason} → 登记人工审核")
                report["unsafe_flagged"] += 1
                try:
                    conn.begin()
                    with conn.cursor() as cursor:
                        task_id = f"spot_unsafe_{doc_id}_v{version_no}"
                        review_reason = f"Spot-check safety_status=unsafe (permission unchanged={current_permission}). Reason: {reason}"
                        if review_reason and len(review_reason) > 490:
                            review_reason = review_reason[:490] + "..."
                        cursor.execute("""
                            INSERT INTO review_task (
                                task_id, doc_id, version_no, review_key, review_type, review_reason, review_status,
                                owner_dept, suggested_category_l1, suggested_category_l2, suggested_permission_level, confidence_score
                            ) VALUES (
                                %s, %s, %s, %s, 'spot_check_unsafe', %s, 'PENDING',
                                %s, 'reference', 'unknown', %s, 0.5
                            ) ON DUPLICATE KEY UPDATE
                                review_reason = VALUES(review_reason),
                                review_status = 'PENDING',
                                suggested_permission_level = VALUES(suggested_permission_level)
                        """, (task_id, doc_id, version_no, f"processing/canonical/{doc_id}/v{version_no}/content.md",
                              review_reason, doc["owner_dept"], suggested_perm))
                    conn.commit()
                except Exception as db_err:
                    conn.rollback()
                    print(f"       ⚠️ Failed to register unsafe review task for {doc_id}: {db_err}")
                    report["errors"].append(f"Unsafe review-task error for {doc_id}: {db_err}")

        except Exception as e:
            err_msg = f"Spot-check safety assessment failed for {doc_id}: {e}"
            print(f"    ⚠️ {err_msg}")
            report["errors"].append(err_msg)

    conn.close()
    print("🔍 [SPOT CHECK] Spot-check safety checker finished.")
    return report
