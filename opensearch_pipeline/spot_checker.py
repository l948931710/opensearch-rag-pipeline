# -*- coding: utf-8 -*-
"""
spot_checker.py — 定时安全抽检任务 (Spot-Check Safety Daemon)
"""
import random
import requests
import json
from opensearch_pipeline.config import get_config
from opensearch_pipeline.pipeline_nodes import _get_db_conn, _get_opensearch_client, _clean_llm_json_response

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
            perm_order = {"public": 0, "internal": 1, "restricted": 2}
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
                        cursor.execute("""
                            UPDATE document_version
                            SET risk_level = 'high',
                                publish_status = 'QUARANTINED',
                                gate_status = 'quarantined',
                                content_process_status = 'FAILED',
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
                    os_client = _get_opensearch_client()

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
                                print(f"       └─ [HA3] Deleted batch {i//ha3_batch_size + 1} ({len(batch)} chunks). Status: {getattr(resp, 'status_code', 'OK')}")
                        else:
                            print(f"       └─ No chunks found in chunk_meta for {doc_id} v{version_no}")
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
                        print(f"       └─ Deleted chunks from OpenSearch index '{index_name}'. Response: {delete_resp}")
                except Exception as os_err:
                    print(f"       ⚠️ Failed to delete chunks from search index for {doc_id}: {os_err}")
                    report["errors"].append(f"Search index delete error for {doc_id}: {os_err}")

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
