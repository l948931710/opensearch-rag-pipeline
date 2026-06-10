# -*- coding: utf-8 -*-
"""
spot_checker.py — 定时安全抽检任务 (Spot-Check Safety Daemon)
"""
import logging
import random
import requests
import json
from opensearch_pipeline.config import get_config
from opensearch_pipeline.pipeline_nodes import (
    _clean_llm_json_response,
    _get_db_conn,
    _get_opensearch_client,
    _search_delete_old_chunks,
)

logger = logging.getLogger(__name__)


def _delete_chunks_from_index(doc_id: str, version_no: int, conn, config) -> None:
    """从搜索索引中删除指定文档的所有 chunks。

    成功时静默返回，失败时抛出异常由调用方处理。
    """
    os_client = _get_opensearch_client()
    if os_client == "MOCK_HA3_CLIENT":
        # simulate 开关错配时绝不静默：mock 字符串会掉进 delete_by_query 分支炸出晦涩的
        # AttributeError；这里换成明确错误，由调用方按删除失败处理（PENDING_DELETE 重试）
        raise RuntimeError(
            "MOCK_HA3_CLIENT in real-mode index delete; simulate flags are inconsistent."
        )

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

    Returns:
        {"total": int, "success": int, "failed": int, "errors": [str]}
    """
    result = {"total": 0, "success": 0, "failed": 0, "errors": []}
    config = get_config()

    try:
        conn = _get_db_conn(select_db=True)
    except Exception as e:
        result["errors"].append(f"DB connect failed: {e}")
        return result

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT doc_id, version_no FROM document_version
                WHERE index_status = 'PENDING_DELETE'
            """)
            rows = cursor.fetchall()

        result["total"] = len(rows)
        if not rows:
            return result

        logger.info("[RECONCILE] Found %d documents with PENDING_DELETE", len(rows))

        for doc_id, version_no in rows:
            try:
                _delete_chunks_from_index(doc_id, version_no, conn, config)

                # 删除成功 → 标记为 DELETED
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE document_version
                        SET index_status = 'DELETED'
                        WHERE doc_id = %s AND version_no = %s
                          AND index_status = 'PENDING_DELETE'
                    """, (doc_id, version_no))
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

    Returns:
        {"total": int, "success": int, "failed": int, "errors": [str]}
    """
    result = {"total": 0, "success": 0, "failed": 0, "errors": []}
    config = get_config()

    try:
        conn = _get_db_conn(select_db=True)
    except Exception as e:
        result["errors"].append(f"DB connect failed: {e}")
        return result

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
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
                  AND (dv.index_status != 'PROCESSING'
                       OR dv.updated_at < NOW() - INTERVAL 2 HOUR)
                  AND EXISTS (SELECT 1 FROM chunk_meta o
                              WHERE o.doc_id = cm_new.doc_id
                                AND o.version_no < cm_new.version_no AND o.is_active = 1)
                  AND EXISTS (SELECT 1 FROM chunk_meta n
                              WHERE n.doc_id = cm_new.doc_id
                                AND n.version_no = cm_new.version_no
                                AND n.is_active = 1 AND n.index_status = 'INDEXED')
                  AND NOT EXISTS (SELECT 1 FROM chunk_meta n2
                                  WHERE n2.doc_id = cm_new.doc_id
                                    AND n2.version_no = cm_new.version_no
                                    AND n2.is_active = 1 AND n2.index_status != 'INDEXED')
                LIMIT 200
            """)
            # 谓词解读：最新 active 版本的 chunk【全部】INDEXED（≥1 条，"已验证索引成功"）、
            # 旧版本仍有 active chunk（搁浅特征）、未被隔离、且不与 2h 内在跑的 stage-3 抢锁。
            rows = cursor.fetchall()

        result["total"] = len(rows)
        if not rows:
            return result

        logger.info(
            "[RECONCILE] Found %d stranded doc version(s) (new fully INDEXED, old still active)",
            len(rows),
        )

        os_client = _get_opensearch_client()
        index_name = getattr(config.opensearch, "index_name", "fuling_knowledge_v1")

        for doc_id, version_no in rows:
            try:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT id FROM chunk_meta
                        WHERE doc_id = %s AND version_no < %s AND is_active = 1
                    """, (doc_id, version_no))
                    old_ids = [r[0] for r in cursor.fetchall()]

                # 先删索引、成功后才动 RDS（与 node_deactivate_old_chunks 同序）
                _search_delete_old_chunks(os_client, config, index_name, doc_id, version_no, old_ids)

                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE chunk_meta
                        SET is_active = FALSE, index_status = 'DELETED'
                        WHERE doc_id = %s AND version_no < %s AND is_active = 1
                    """, (doc_id, version_no))
                    cursor.execute("""
                        UPDATE document_version
                        SET index_status = 'SUCCESS'
                        WHERE doc_id = %s AND version_no = %s
                    """, (doc_id, version_no))
                conn.commit()
                result["success"] += 1
                logger.info(
                    "[RECONCILE] Healed stranded version %s v%s (deactivated %d old chunks)",
                    doc_id, version_no, len(old_ids),
                )
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
              f"{reconcile_result['success']} success, {reconcile_result['failed']} failed")
        report["errors"].extend(reconcile_result["errors"])

    # 再对账：修复搁浅的双版本文档（orchestrator stage-3 启动前也会跑，这里是独立兜底）
    stranded_result = reconcile_stranded_versions()
    if stranded_result["total"] > 0:
        print(f"    └─ [RECONCILE] Healed {stranded_result['success']}/{stranded_result['total']} "
              f"stranded doc versions, {stranded_result['failed']} failed")
        report["errors"].extend(stranded_result["errors"])
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
            cursor.execute("""
                SELECT dv.doc_id, dv.version_no, dm.permission_level, dm.title, dm.owner_dept
                FROM document_version dv
                JOIN document_meta dm ON dv.doc_id = dm.doc_id
                WHERE dv.index_status = 'SUCCESS' AND dv.status = 'active'
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

        # 4. 调用 secondary/safety LLM check
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

        try:
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
                safety_eval = json.loads(cleaned_content)
            else:
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
                safety_eval = json.loads(cleaned_content)
            
            suggested_perm = safety_eval["suggested_permission_level"]
            safety_status = safety_eval["safety_status"]
            reason = safety_eval["reason"]
            
            report["checked_documents"] += 1
            print(f"       └─ Safety Check Result: status={safety_status}, suggested_permission={suggested_perm}")

            # 5. 安全等级比对 (权限降级判定)
            # 权限严重等级顺序： public (0) < internal (1) < restricted (2)
            # 'dept_internal' 是 'internal' 的归一化写法（与 HA3 过滤表达式对齐），同级
            perm_order = {"public": 0, "internal": 1, "dept_internal": 1, "restricted": 2}
            suggested_rank = perm_order.get(suggested_perm, 0)
            current_rank = perm_order.get(current_permission, 0)

            if suggested_rank > current_rank:
                # 判定为权限泄露 mismatch！触发 quarantine 锁定
                print(f"       🚨 SECURITY WARNING: Permission mismatch detected for {doc_id}! Indexed as '{current_permission}' but spot-check recommends '{suggested_perm}'. Reason: {reason}")
                report["mismatch_detected"] += 1
                
                # 执行隔离 (Quarantine)
                try:
                    conn.begin()
                    # a. 更新 document_version & document_meta
                    with conn.cursor() as cursor:
                        # ⚠️ content_process_status 必须是终态 'QUARANTINED'，不能用 'FAILED'：
                        # 'FAILED' 正好命中 stage-2 的抢占谓词（FAILED AND retry_count<3），
                        # 下一次日跑会重新分块/重新发布，把隔离悄悄撤销掉。
                        cursor.execute("""
                            UPDATE document_version
                            SET risk_level = 'high',
                                publish_status = 'QUARANTINED',
                                gate_status = 'quarantined',
                                content_process_status = 'QUARANTINED',
                                content_process_error = %s
                            WHERE doc_id = %s AND version_no = %s
                        """, (f"[SPOT CHECK MISMATCH] Spot-check recommends tightening permission to {suggested_perm}", doc_id, version_no))

                        cursor.execute("""
                            UPDATE document_meta
                            SET permission_level = %s,
                                kb_type = 'private'
                            WHERE doc_id = %s
                        """, (suggested_perm, doc_id))

                        # b. 停用 chunk_meta 记录
                        cursor.execute("""
                            UPDATE chunk_meta
                            SET is_active = FALSE
                            WHERE doc_id = %s AND version_no = %s
                        """, (doc_id, version_no))

                        # c. 注册人工审核任务
                        task_id = f"spot_rev_{doc_id}_v{version_no}"
                        review_reason = f"Spot-check permission level mismatch: current={current_permission}, suggested={suggested_perm}. Reason: {reason}"
                        # Defensively truncate to prevent database column VARCHAR(500) limit issues
                        if review_reason and len(review_reason) > 490:
                            review_reason = review_reason[:490] + "..."
                        cursor.execute("""
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
                        """, (task_id, doc_id, version_no, f"processing/canonical/{doc_id}/v{version_no}/content.md",
                              review_reason, doc["owner_dept"], suggested_perm))
                    conn.commit()
                except Exception as db_err:
                    conn.rollback()
                    print(f"       ⚠️ Failed to update RDS for quarantine: {db_err}")
                    report["errors"].append(f"RDS quarantine error for {doc_id}: {db_err}")
                    continue  # Skip OpenSearch delete if RDS failed

                # d. 从搜索索引中彻底 DELETE 这些 chunks
                try:
                    _delete_chunks_from_index(doc_id, version_no, conn, config)
                    # 删除成功 → 标记为 DELETED
                    with conn.cursor() as cursor:
                        cursor.execute("""
                            UPDATE document_version
                            SET index_status = 'DELETED'
                            WHERE doc_id = %s AND version_no = %s
                        """, (doc_id, version_no))
                    conn.commit()
                    print(f"       └─ ✅ Chunks deleted from search index for {doc_id} v{version_no}")
                except Exception as os_err:
                    logger.error(
                        "Failed to delete chunks from search index for %s v%s: %s",
                        doc_id, version_no, os_err, exc_info=True,
                    )
                    report["errors"].append(f"Search index delete error for {doc_id}: {os_err}")
                    # 关键修复：标记为 PENDING_DELETE，下次 spot-check 或对账任务会重试
                    try:
                        with conn.cursor() as cursor:
                            cursor.execute("""
                                UPDATE document_version
                                SET index_status = 'PENDING_DELETE'
                                WHERE doc_id = %s AND version_no = %s
                            """, (doc_id, version_no))
                        conn.commit()
                        print(f"       ⚠️ Marked {doc_id} v{version_no} as PENDING_DELETE for retry")
                    except Exception as mark_err:
                        conn.rollback()
                        logger.error(
                            "Failed to mark PENDING_DELETE for %s v%s: %s",
                            doc_id, version_no, mark_err,
                        )

                report["quarantined_documents"].append({
                    "doc_id": doc_id,
                    "version_no": version_no,
                    "previous_permission": current_permission,
                    "suggested_permission": suggested_perm,
                    "reason": reason
                })

        except Exception as e:
            err_msg = f"Spot-check safety assessment failed for {doc_id}: {e}"
            print(f"    ⚠️ {err_msg}")
            report["errors"].append(err_msg)

    conn.close()
    print("🔍 [SPOT CHECK] Spot-check safety checker finished.")
    return report
