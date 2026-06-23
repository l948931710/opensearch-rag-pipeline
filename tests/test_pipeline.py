# -*- coding: utf-8 -*-
"""
test_pipeline.py — DAG 端到端集成测试
"""

import pytest
from datetime import datetime

from tests.local_stack import requires_local_db, requires_local_opensearch

@pytest.fixture(autouse=True)
def reset_db_state():
    # 🛡️ 防 sim→prod 泄露（2026-06-13 事故根因）：autouse + 无条件 DELETE chunk_meta，
    # 远程 host 直接 skip。
    from tests.local_stack import ensure_local_db_wired, local_db_unavailable_reason
    if not ensure_local_db_wired():
        pytest.skip(f"reset_db_state fixture refusing non-local RDS: {local_db_unavailable_reason()}")

    from opensearch_pipeline.pipeline_nodes import _get_db_conn
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as c:
            c.execute("UPDATE document_version SET extraction_status='NOT_STARTED', content_process_status='NOT_STARTED', chunk_status='NOT_STARTED', index_status='NOT_INDEXED'")
            c.execute("DELETE FROM chunk_meta")
            c.execute("DELETE FROM document_sensitive_finding")
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Failed to reset DB: {e}")

@pytest.fixture(autouse=True)
def force_simulate_api():
    import os
    from opensearch_pipeline.config import get_config
    config = get_config()
    orig_simulate_api = config.simulate_api
    
    # 💡 只有当本地环境变量显式且直接配置了 RAG_SIMULATE_API=false 时，测试套件才会调用真实的外部大模型 API。
    # 否则（例如仅配置了 RAG_SIMULATE=false 用以连接本地真实数据库/OpenSearch，但未指定 RAG_SIMULATE_API），
    # 测试套件会自动保持使用模拟 API（config.simulate_api = True），以极大加快测试执行速度并避免产生大模型 API 资费。
    env_simulate_api = os.environ.get("RAG_SIMULATE_API", "").lower()
    
    wants_real_api = (env_simulate_api in ("false", "0", "no"))
    has_api_keys = bool(config.embedding.api_key or config.llm.api_key)
    
    if wants_real_api and has_api_keys:
        config.simulate_api = False
    else:
        config.simulate_api = True
        
    yield
    config.simulate_api = orig_simulate_api

from opensearch_pipeline.dag_definitions import (
    build_dag1_raw_to_canonical,
    build_dag2_canonical_to_chunk,
    build_dag3_chunk_to_opensearch,
    build_dag4_retrieval_eval,
    build_full_pipeline,
)
from opensearch_pipeline.run_simulation import (
    get_test_data,
    get_version_update_data,
)


class TestDAGStructure:
    """DAG 结构验证。"""

    def test_dag1_has_4_nodes(self):
        dag = build_dag1_raw_to_canonical()
        assert len(dag.nodes) == 4

    def test_dag2_has_7_nodes(self):
        dag = build_dag2_canonical_to_chunk()
        assert len(dag.nodes) == 7

    def test_dag3_has_7_nodes(self):
        # 00 lock / 01 embed / 02 payload / 03 push / 04 update / 04b parity-verify / 05 deactivate
        dag = build_dag3_chunk_to_opensearch()
        assert len(dag.nodes) == 7

    def test_dag4_has_2_nodes(self):
        dag = build_dag4_retrieval_eval()
        assert len(dag.nodes) == 2

    def test_full_pipeline_has_4_dags(self):
        dags = build_full_pipeline()
        assert len(dags) == 4

    def test_dag3_safe_ordering(self):
        """验证 DAG 3 的安全顺序：update_index_status 在 deactivate 之前。"""
        dag = build_dag3_chunk_to_opensearch()
        order = dag._topological_sort()
        node_names = [dag.nodes[nid].name for nid in order]

        update_idx = next(
            i for i, name in enumerate(node_names)
            if "状态" in name or "update" in name.lower()
        )
        deactivate_idx = next(
            i for i, name in enumerate(node_names)
            if "停用" in name or "deactivate" in name.lower()
        )
        assert update_idx < deactivate_idx, (
            f"update_index_status (idx={update_idx}) must come before "
            f"deactivate_old_chunks (idx={deactivate_idx})"
        )

    def test_dag2_classify_before_redact(self):
        """验证分类在脱敏之前。"""
        dag = build_dag2_canonical_to_chunk()
        order = dag._topological_sort()
        node_names = [dag.nodes[nid].name for nid in order]

        classify_idx = next(
            i for i, name in enumerate(node_names)
            if "classify" in name.lower()
        )
        redact_idx = next(
            i for i, name in enumerate(node_names)
            if "redact" in name.lower()
        )
        assert classify_idx < redact_idx

    def test_dag2_publish_before_chunk(self):
        """验证发布在切分之前。"""
        dag = build_dag2_canonical_to_chunk()
        order = dag._topological_sort()
        node_names = [dag.nodes[nid].name for nid in order]

        publish_idx = next(
            i for i, name in enumerate(node_names)
            if "publish" in name.lower() or "rag-ready" in name.lower()
        )
        chunk_idx = next(
            i for i, name in enumerate(node_names)
            if "chunk doc" in name.lower()
        )
        assert publish_idx < chunk_idx


class TestNormalScenario:
    """Normal 场景端到端测试。"""

    def test_dag1_produces_canonicals(self):
        ctx = get_test_data("normal")
        dag = build_dag1_raw_to_canonical()
        ctx = dag.run(ctx)

        assert "canonicals" in ctx
        assert len(ctx["canonicals"]) == 1
        canonical = ctx["canonicals"][0]
        assert canonical["text_length"] > 0
        assert len(canonical["blocks"]) > 0

    def test_dag1_dag2_full_flow(self):
        ctx = get_test_data("normal")

        ctx = build_dag1_raw_to_canonical().run(ctx)
        ctx = build_dag2_canonical_to_chunk().run(ctx)

        # 验证输出
        assert len(ctx["valid_chunks"]) > 0
        assert ctx["published_count"] == 1
        assert ctx["chunk_meta_written"] > 0

        # 验证 chunk 结构（2026-06-10 起 faq_eligible 不再劫持路由：
        # 含步骤标记的模拟文档会合法进入 step 模式，产出 step_card/procedure_parent）
        for chunk in ctx["valid_chunks"]:
            assert chunk.doc_id
            assert chunk.chunk_id
            assert chunk.chunk_text
            assert chunk.chunk_type in (
                "text_chunk", "table_chunk", "ocr_chunk",
                "step_card", "procedure_parent", "image", "visual_knowledge",
            )

    def test_full_pipeline(self):
        ctx = get_test_data("normal")

        for dag in build_full_pipeline():
            ctx = dag.run(ctx)

        assert ctx.get("index_result", {}).get("status") in ("SIMULATED_SUCCESS", "SUCCESS", "PARTIAL_SUCCESS")
        assert ctx.get("eval_report", {}).get("summary", {}).get("total_queries_tested", 0) > 0

    def test_pipeline_skips_publish_gracefully(self):
        """测试跳过 node_publish_to_rag_ready 节点时，后续的 chunking 与 rds 写入仍能正常工作，且 rag_ready_key 自动优雅 fallback 补齐。"""
        from opensearch_pipeline.dag_engine import DAG, DAGNode
        from opensearch_pipeline.pipeline_nodes import (
            node_classify_and_risk_assess,
            node_detect_sensitive,
            node_redact_or_quarantine,
            node_chunk_documents,
            node_validate_chunks,
            node_write_chunk_meta,
        )

        ctx = get_test_data("normal")
        ctx["simulate"] = False
        ctx["simulate_api"] = True
        ctx = build_dag1_raw_to_canonical().run(ctx)

        # 构建一个去掉了 "Publish to rag-ready/" 节点的自定义 DAG 2
        dag2 = DAG(
            dag_id="dag2_test_skip_publish",
            name="Canonical -> Safe Chunks (Skip Publish)",
            description="Skip publish node to test robust fallback logic in node_write_chunk_meta",
        )
        dag2.add_node(DAGNode(
            "01", "Classify + Risk Assess (LLM)",
            node_classify_and_risk_assess,
        ))
        dag2.add_node(DAGNode(
            "02", "Detect Sensitive Entities",
            node_detect_sensitive,
            depends_on=["01"],
        ))
        dag2.add_node(DAGNode(
            "03", "Redact or Quarantine",
            node_redact_or_quarantine,
            depends_on=["02"],
        ))
        # ⚠️ 直接依赖 "03"，跳过 "04" 发布节点
        dag2.add_node(DAGNode(
            "05", "Chunk Documents",
            node_chunk_documents,
            depends_on=["03"],
        ))
        dag2.add_node(DAGNode(
            "06", "Validate Chunks",
            node_validate_chunks,
            depends_on=["05"],
        ))
        dag2.add_node(DAGNode(
            "07", "Write chunk_meta to RDS",
            node_write_chunk_meta,
            depends_on=["06"],
        ))

        ctx = dag2.run(ctx)

        # 验证输出与行为
        assert len(ctx["valid_chunks"]) > 0
        assert "published_count" not in ctx  # 确认没有执行发布节点
        assert ctx["chunk_meta_written"] > 0

        # 从数据库中查询刚才写入的记录，确保 fallback 的 rag_ready_key 完美写入
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT rag_ready_key, doc_id, version_no, permission_level, owner_dept, category_l1 "
                    "FROM chunk_meta LIMIT 1"
                )
                row = cursor.fetchone()
                assert row is not None
                db_rag_ready_key = row[0]
                doc_id = row[1]
                version = row[2]
                permission = row[3]
                dept = row[4]
                category = row[5]

                expected_fallback_key = (
                    f"rag-ready/{permission}/{dept}/{category}/"
                    f"{doc_id}/v{version}/content.md"
                )
                assert db_rag_ready_key == expected_fallback_key
        finally:
            conn.close()


class TestSensitiveScenario:
    """敏感文档场景测试。"""

    def test_quarantine_blocks_publish_and_chunk(self):
        ctx = get_test_data("sensitive")

        ctx = build_dag1_raw_to_canonical().run(ctx)
        ctx = build_dag2_canonical_to_chunk().run(ctx)

        assert ctx["published_count"] == 0
        assert len(ctx["valid_chunks"]) == 0
        assert ctx["chunk_meta_written"] == 0

        # 验证文档被隔离
        doc = ctx["canonicals"][0]
        assert doc["risk_level"] == "high"
        assert doc["redaction_action"] == "QUARANTINE"


class TestVersionUpdateScenario:
    """版本更新场景测试。"""

    def test_dag1_dag2_leaves_old_active(self):
        ctx = get_version_update_data()

        ctx = build_dag1_raw_to_canonical().run(ctx)
        ctx = build_dag2_canonical_to_chunk().run(ctx)

        # 新 chunk 已写入
        assert ctx["chunk_meta_written"] > 0

        # 旧 chunk 尚未停用
        deactivated = ctx.get("deactivated_chunks", [])
        assert len(deactivated) == 0

    def test_dag3_deactivates_old_after_index(self):
        ctx = get_version_update_data()

        ctx = build_dag1_raw_to_canonical().run(ctx)
        ctx = build_dag2_canonical_to_chunk().run(ctx)
        ctx = build_dag3_chunk_to_opensearch().run(ctx)

        # 新 chunk 已 indexed, 旧 chunk 已停用
        deactivated = ctx.get("deactivated_chunks", [])
        assert len(deactivated) == 2
        for d in deactivated:
            assert d["old_version"] == 1
            assert d["new_version"] == 2

    def test_full_pipeline_version_update(self):
        ctx = get_version_update_data()

        for dag in build_full_pipeline():
            ctx = dag.run(ctx)

        assert len(ctx["deactivated_chunks"]) == 2
        assert ctx["chunk_meta_written"] > 0
        assert ctx.get("index_result", {}).get("status") in ("SIMULATED_SUCCESS", "SUCCESS", "PARTIAL_SUCCESS")


class TestMultiDocScenario:
    """多文档场景测试。"""

    def test_multi_doc_processing(self):
        ctx = get_test_data("multi")

        ctx = build_dag1_raw_to_canonical().run(ctx)
        ctx = build_dag2_canonical_to_chunk().run(ctx)

        assert ctx["published_count"] == 2
        assert len(ctx["valid_chunks"]) > 0
        assert ctx["chunk_meta_written"] > 0

        # 每个文档都有 chunk
        doc_ids = set(c.doc_id for c in ctx["valid_chunks"])
        assert len(doc_ids) == 2


class TestDynamicRoutingScenario:
    """测试 category-aware 动态路由切分模式。"""

    def test_dynamic_routing_sop_and_manual_and_faq(self):
        # 准备带有不同 category 属性的 canonicals 数据
        canonicals = [
            {
                "doc_id": "test_doc_sop",
                "version_no": 1,
                "text": "这是一份公司宿舍管理制度。公司对离职人员迁离有严格规定，所有人员必须遵守。",
                "title": "公司宿舍管理制度.docx",
                "owner_dept": "admin",
                "category_l1": "policy",
                "category_l2": "hr_policy",
                "blocks": [{"text": "这是一份公司宿舍管理制度。公司对离职人员迁离有严格规定，所有人员必须遵守。", "page_num": 1, "block_type": "paragraph"}],
            },
            {
                "doc_id": "test_doc_manual",
                "version_no": 1,
                "text": "本手册用于叉车安全操作规程。叉车每次启动时间不能超过5秒，连续三次不成需间隔5分钟。",
                "title": "叉车操作手册.docx",
                "owner_dept": "hr",
                "category_l1": "reference",
                "category_l2": "manual",
                "blocks": [{"text": "本手册用于叉车安全操作规程。叉车每次启动时间不能超过5秒，连续三次不成需间隔5分钟。", "page_num": 1, "block_type": "paragraph"}],
            },
            {
                "doc_id": "test_doc_faq",
                "version_no": 1,
                "text": "问：电脑蓝屏了怎么办？答：找IT系统管理员报修。问：新入职前几天怎么吃饭？答：食堂领餐券。",
                "title": "IT故障FAQ.docx",
                "owner_dept": "it",
                "category_l1": "others",
                "category_l2": "others",
                "blocks": [
                    {"text": "问：电脑蓝屏了怎么办？", "page_num": 1, "block_type": "paragraph"},
                    {"text": "答：找IT系统管理员报修。", "page_num": 1, "block_type": "paragraph"},
                    {"text": "问：新入职前几天怎么吃饭？", "page_num": 1, "block_type": "paragraph"},
                    {"text": "答：食堂领餐券。", "page_num": 1, "block_type": "paragraph"},
                ],
            }
        ]

        from opensearch_pipeline.pipeline_nodes import node_chunk_documents
        ctx = {
            "canonicals": canonicals,
            "split_mode": "dynamic",
            "min_chunk_chars": 5,
        }

        # 执行切分节点
        node_chunk_documents(ctx)

        chunks = ctx["chunks"]
        # 验证是否针对不同类型动态使用了不同切分设置
        # FAQ 文档 (test_doc_faq) 使用 split_mode='faq'，应该产生 2 个基于 Q&A 提取的 chunks
        faq_chunks = [c for c in chunks if c.doc_id == "test_doc_faq"]
        assert len(faq_chunks) == 2, f"Expected 2 FAQ chunks, got {len(faq_chunks)}"
        assert any("电脑蓝屏了怎么办" in c.chunk_text for c in faq_chunks)
        assert any("新入职前几天怎么吃饭" in c.chunk_text for c in faq_chunks)

        # SOP 和 Manual 文档也应该成功产生 chunks
        sop_chunks = [c for c in chunks if c.doc_id == "test_doc_sop"]
        assert len(sop_chunks) > 0

        manual_chunks = [c for c in chunks if c.doc_id == "test_doc_manual"]
        assert len(manual_chunks) > 0


class TestBulkPayloadSplitting:
    """验证 OpenSearch bulk payload 自动切分与 sub-job 跟踪。"""

    @requires_local_db
    @requires_local_opensearch
    def test_bulk_payload_splitting_over_limit(self):
        """设定非常小的 max_bulk_size_bytes 触发贪心切分，并验证多 batch 跟踪。
        本地栈集成测试：真实写本地 MySQL（opensearch_bulk_job）+ 真实推送本地 OpenSearch。"""
        from opensearch_pipeline.chunker import Chunk
        from opensearch_pipeline.pipeline_nodes import (
            node_build_opensearch_payload,
            node_push_to_opensearch,
            node_update_index_status,
            _get_db_conn,
        )

        # 1. 构造 3 个 chunks。本测试验证 payload 切分/推送机制（非向量内容），且真实推送本地
        #    OpenSearch(simulate=False)——其 knn_vector 字段维度=1024，故沿用原 fixture 的"无向量"
        #    文档(to_opensearch_doc 在无向量时省略 chunk_vector，本地索引可接受)。
        #    payload 现按 embedding_status != "DONE" 剔除，故必须显式置 DONE 让 chunk 进入 payload。
        chunks = [
            Chunk(chunk_id="chunk_split_1", doc_id="doc_split", version_no=1, chunk_index=1, chunk_type="text_chunk", chunk_text="This is chunk 1" * 10, token_count=10, page_num=1, permission_level="PUBLIC", embedding_status="DONE"),
            Chunk(chunk_id="chunk_split_2", doc_id="doc_split", version_no=1, chunk_index=2, chunk_type="text_chunk", chunk_text="This is chunk 2" * 10, token_count=10, page_num=1, permission_level="PUBLIC", embedding_status="DONE"),
            Chunk(chunk_id="chunk_split_3", doc_id="doc_split", version_no=1, chunk_index=3, chunk_type="text_chunk", chunk_text="This is chunk 3" * 10, token_count=10, page_num=1, permission_level="PUBLIC", embedding_status="DONE"),
        ]

        # 2. 设置 context，设定较小的 payload limit 比如 200 字节，强制定向分包
        ctx = {
            "embedded_chunks": chunks,
            "max_bulk_size_bytes": 200,  # 强制多 batches
            "simulate": False,  # 真实写入 RDS 以检验 SQL 插入/更新
            "opensearch_index": "test_split_index",
        }

        # 3. 运行 build node
        node_build_opensearch_payload(ctx)

        # 4. 验证是否产生了多个 batches，且 backward compatibility 字段被赋予了第一个 batch
        batches = ctx.get("bulk_batches", [])
        assert len(batches) >= 2, f"Expected at least 2 batches, got {len(batches)}"
        
        # 验证 backward-compatible 字段
        assert ctx["bulk_payload"] == batches[0]["payload"]
        assert ctx["bulk_payload_size"] == batches[0]["payload_size"]
        assert ctx["bulk_chunk_count"] == len(batches[0]["chunks"])
        assert ctx["bulk_job_id"] == batches[0]["job_id"]
        assert ctx["bulk_oss_key"] == batches[0]["oss_key"]

        # 检查 physical file 是否存在
        import os
        for b in batches:
            assert os.path.exists(b["oss_key"]), f"Physical file {b['oss_key']} should exist"

        # 检查数据库是否正确插入了多个 pending 记录
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cursor:
                for b in batches:
                    cursor.execute("SELECT status, payload_size_bytes FROM opensearch_bulk_job WHERE job_id=%s", (b["job_id"],))
                    row = cursor.fetchone()
                    assert row is not None, f"Job {b['job_id']} not found in RDS"
                    assert row[0] == "PENDING"
                    assert row[1] == b["payload_size"]
        finally:
            conn.close()

        # 5. 运行 push node
        node_push_to_opensearch(ctx)

        # 6. 验证 chunk index_status 状态
        for chunk in chunks:
            assert chunk.index_status == "INDEXED"

        # 验证 physical file 是否被正确移动到了 completed 文件夹下
        for b in batches:
            assert "index-jobs/opensearch/completed" in b["oss_key"]
            assert os.path.exists(b["oss_key"]), f"Physical completed file {b['oss_key']} should exist"

        # 7. 运行 update index status node
        node_update_index_status(ctx)

        # 8. 验证数据库中这些 batch records 是否全部更新为 COMPLETED
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cursor:
                for b in batches:
                    cursor.execute("SELECT status, success_count FROM opensearch_bulk_job WHERE job_id=%s", (b["job_id"],))
                    row = cursor.fetchone()
                    assert row is not None
                    assert row[0] == "COMPLETED"
                    assert row[1] == len(b["chunks"])
        finally:
            conn.close()


class TestStrictFailurePropagation:
    """验证 real 模式下严格的错误传播（异常立即抛出而非静默 fallback）。"""

    def test_real_embedding_missing_api_key_raises_runtime_error(self, monkeypatch):
        from opensearch_pipeline.pipeline_nodes import node_generate_embeddings
        from opensearch_pipeline.chunker import Chunk
        
        # 模拟 config 没有 API key 且 simulate_api=False
        from opensearch_pipeline.config import get_config
        config = get_config()
        monkeypatch.setattr(config.embedding, "api_key", "")
        monkeypatch.setattr(config.embedding, "model", "gemini-embedding-2")
        monkeypatch.setattr(config.embedding, "api_base_url", "https://generativelanguage.googleapis.com/v1beta")
        
        ctx = {
            "valid_chunks": [
                Chunk(chunk_id="chunk_test_err", doc_id="doc_err", version_no=1, chunk_index=1, chunk_type="text_chunk", chunk_text="test text", token_count=2, page_num=1, permission_level="PUBLIC")
            ],
            "simulate_api": False,
        }
        
        with pytest.raises(RuntimeError, match="Gemini API key is not configured for real embeddings."):
            node_generate_embeddings(ctx)

    def test_real_opensearch_init_failure_raises_runtime_error(self, monkeypatch):
        from opensearch_pipeline.pipeline_nodes import node_push_to_opensearch
        from opensearch_pipeline.chunker import Chunk
        
        # 强行使 _get_opensearch_client 抛出异常
        import opensearch_pipeline.pipeline_nodes
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "_get_opensearch_client", lambda *a, **k: (_ for _ in ()).throw(ValueError("Mocked OS client connection failure")))
        
        chunks = [
            Chunk(chunk_id="chunk_test_err2", doc_id="doc_err2", version_no=1, chunk_index=1, chunk_type="text_chunk", chunk_text="test text", token_count=2, page_num=1, permission_level="PUBLIC")
        ]
        ctx = {
            "embedded_chunks": chunks,
            "bulk_batches": [{
                "chunks": chunks,
                "payload": "dummy",
                "payload_size": 5,
                "job_id": "JOB_ERR",
                "oss_key": "dummy_path.jsonl",
            }],
            "simulate": False,
            "opensearch_index": "test_err_index",
        }
        
        with pytest.raises(RuntimeError, match="Failed to initialize OpenSearch client/index in real mode: Mocked OS client connection failure"):
            node_push_to_opensearch(ctx)


class TestDatabaseExceptionPropagation:
    """验证数据库写入异常正确向上抛出而不被默默吞掉。"""

    def test_node_register_metadata_raises_on_db_error(self, monkeypatch):
        import opensearch_pipeline.pipeline_nodes
        from opensearch_pipeline.pipeline_nodes import node_register_metadata
        
        # 强行使 _get_db_conn 抛出异常，并关闭 simulate_db
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "_get_db_conn", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Mock RDS connection failed")))
        
        ctx = {
            "tasks": [{"doc_id": "doc1", "version_no": 1, "filename": "test.txt", "dept": "HR", "file_ext": "txt"}],
            "simulate_db": False,
        }
        
        with pytest.raises(RuntimeError, match="Database write failure in node_register_metadata: Mock RDS connection failed"):
            node_register_metadata(ctx)

    def test_node_classify_document_failsafe_graceful_on_db_error(self, monkeypatch):
        """Fail-safe 路径的 DB 写入（review_task + document_version）是 non-fatal 的。
        当 LLM API 失败 + DB 不可用时，应该不崩溃，仅标记文档为失败。"""
        import opensearch_pipeline.pipeline_nodes
        from opensearch_pipeline.pipeline_nodes import node_classify_and_risk_assess
        
        # Mock: 第一次 _get_db_conn 成功（预占锁），后续调用失败（fail-safe 写入）
        class MockCursor:
            def execute(self, query, params=None): pass
            @property
            def rowcount(self): return 1
            def fetchall(self): return []  # unfrozen-rechunk guard's chunk_meta probe → no prior chunks
            def __enter__(self): return self
            def __exit__(self, *a): pass
        class MockConn:
            def cursor(self): return MockCursor()
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass
        
        call_count = {"n": 0}
        def _mock_get_db_conn(**kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return MockConn()  # call#1 = unfrozen-rechunk guard read, call#2 = content preempt
            raise RuntimeError("Mock RDS fail-safe write failed")
        
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "_get_db_conn", _mock_get_db_conn)
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "run_gemini_classification", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("Mock LLM API error")))

        ctx = {
            "tasks": [{"doc_id": "doc1", "version_no": 1, "filename": "test.txt", "dept": "HR", "file_ext": "txt"}],
            "canonicals": [{"doc_id": "doc1", "version_no": 1, "text": "dummy content", "filename": "test.txt", "dept": "HR", "file_ext": "txt", "source_key": "_quarantine/"}],
            "extractions": [{"doc_id": "doc1", "version_no": 1, "text": "dummy content", "blocks": []}],
            "simulate_db": False,
            "simulate_api": False,
        }
        
        # Fail-safe 路径不应抛出异常，而是标记文档为失败并从 canonicals 中移除
        node_classify_and_risk_assess(ctx)
        
        # 验证文档因 fail-safe 被移除（_classify_single_doc returns False → failed_doc_ids → 过滤掉）
        assert len(ctx["canonicals"]) == 0, "Failed doc should be removed from canonicals after fail-safe"

    def test_node_classify_document_low_confidence_raises_on_db_error(self, monkeypatch, llm_key_present):
        import opensearch_pipeline.pipeline_nodes
        from opensearch_pipeline.pipeline_nodes import node_classify_and_risk_assess
        
        # Mock: 第一次 _get_db_conn 成功（预占锁），后续调用失败（持久化写入）
        class MockCursor:
            def execute(self, query, params=None): pass
            @property
            def rowcount(self): return 1
            def fetchall(self): return []  # unfrozen-rechunk guard's chunk_meta probe → no prior chunks
            def __enter__(self): return self
            def __exit__(self, *a): pass
        class MockConn:
            def cursor(self): return MockCursor()
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass
        
        call_count = {"n": 0}
        def _mock_get_db_conn(**kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return MockConn()  # call#1 = unfrozen-rechunk guard read, call#2 = content preempt
            raise RuntimeError("Mock RDS quarantine write failed")
        
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "_get_db_conn", _mock_get_db_conn)
        # 返回低于 0.85 置信度的分类（review 已关闭，走正常入库路径）
        mock_low_conf = {
            "category_l1": "policy",
            "category_l2": "hr_policy",
            "confidence": 0.5,
            "faq_eligible": False,
            "summary": "low conf doc",
            "llm_risk_level": "high"
        }
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "run_gemini_classification", lambda *args, **kwargs: mock_low_conf)

        ctx = {
            "tasks": [{"doc_id": "doc1", "version_no": 1, "filename": "test.txt", "dept": "HR", "file_ext": "txt"}],
            "canonicals": [{"doc_id": "doc1", "version_no": 1, "text": "dummy content", "filename": "test.txt", "dept": "HR", "file_ext": "txt", "source_key": "_quarantine/"}],
            "extractions": [{"doc_id": "doc1", "version_no": 1, "text": "dummy content", "blocks": []}],
            "simulate_db": False,
            "simulate_api": False,
        }
        
        with pytest.raises(RuntimeError, match="Database write failure in node_classify_document \\(persist metadata\\): Mock RDS quarantine write failed"):
            node_classify_and_risk_assess(ctx)

    def test_node_classify_document_high_confidence_raises_on_db_error(self, monkeypatch, llm_key_present):
        import opensearch_pipeline.pipeline_nodes
        from opensearch_pipeline.pipeline_nodes import node_classify_and_risk_assess
        
        # Mock: 第一次 _get_db_conn 成功（预占锁），后续调用失败（持久化写入）
        class MockCursor:
            def execute(self, query, params=None): pass
            @property
            def rowcount(self): return 1
            def fetchall(self): return []  # unfrozen-rechunk guard's chunk_meta probe → no prior chunks
            def __enter__(self): return self
            def __exit__(self, *a): pass
        class MockConn:
            def cursor(self): return MockCursor()
            def commit(self): pass
            def rollback(self): pass
            def close(self): pass
        
        call_count = {"n": 0}
        def _mock_get_db_conn(**kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return MockConn()  # call#1 = unfrozen-rechunk guard read, call#2 = content preempt
            raise RuntimeError("Mock RDS persist metadata failed")
        
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "_get_db_conn", _mock_get_db_conn)
        # 返回高置信度分类
        mock_high_conf = {
            "category_l1": "policy",
            "category_l2": "hr_policy",
            "confidence": 0.9,
            "faq_eligible": False,
            "summary": "high conf doc",
            "llm_risk_level": "low"
        }
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "run_gemini_classification", lambda *args, **kwargs: mock_high_conf)

        ctx = {
            "tasks": [{"doc_id": "doc1", "version_no": 1, "filename": "test.txt", "dept": "HR", "permission_level": "public", "kb_type": "public", "file_ext": "txt"}],
            "canonicals": [{"doc_id": "doc1", "version_no": 1, "text": "dummy content", "filename": "test.txt", "dept": "HR", "file_ext": "txt", "source_key": "_quarantine/"}],
            "extractions": [{"doc_id": "doc1", "version_no": 1, "text": "dummy content", "blocks": []}],
            "simulate_db": False,
            "simulate_api": False,
        }
        
        with pytest.raises(RuntimeError, match="Database write failure in node_classify_document \\(persist metadata\\): Mock RDS persist metadata failed"):
            node_classify_and_risk_assess(ctx)

    def test_node_write_chunk_meta_raises_on_db_error(self, monkeypatch):
        import opensearch_pipeline.pipeline_nodes
        from opensearch_pipeline.pipeline_nodes import node_write_chunk_meta
        from opensearch_pipeline.chunker import Chunk
        
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "_get_db_conn", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Mock RDS chunk write failed")))
        
        ctx = {
            "valid_chunks": [
                Chunk(chunk_id="chunk1", doc_id="doc1", version_no=1, chunk_index=1, chunk_type="text_chunk", chunk_text="test", token_count=1, page_num=1, permission_level="PUBLIC")
            ],
            "canonicals": [{"doc_id": "doc1", "version_no": 1}],
            "simulate_db": False,
        }
        
        with pytest.raises(RuntimeError, match="Database write failure in node_write_chunk_meta: Mock RDS chunk write failed"):
            node_write_chunk_meta(ctx)

    def test_node_build_opensearch_payload_raises_on_db_error(self, monkeypatch):
        import opensearch_pipeline.pipeline_nodes
        from opensearch_pipeline.pipeline_nodes import node_build_opensearch_payload
        from opensearch_pipeline.chunker import Chunk
        
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "_get_db_conn", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Mock RDS bulk job insert failed")))
        
        chunks = [
            Chunk(chunk_id="chunk1", doc_id="doc1", version_no=1, chunk_index=1, chunk_type="text_chunk", chunk_text="test", token_count=1, page_num=1, permission_level="PUBLIC", embedding_status="DONE", embedding_vector=[0.1, 0.2, 0.3])
        ]
        ctx = {
            "embedded_chunks": chunks,
            "simulate_db": False,
            "simulate_oss": True,
        }
        
        with pytest.raises(RuntimeError, match="Database write failure in node_build_opensearch_payload: Mock RDS bulk job insert failed"):
            node_build_opensearch_payload(ctx)

    def test_node_update_index_status_raises_on_db_error(self, monkeypatch):
        import opensearch_pipeline.pipeline_nodes
        from opensearch_pipeline.pipeline_nodes import node_update_index_status
        from opensearch_pipeline.chunker import Chunk
        
        monkeypatch.setattr(opensearch_pipeline.pipeline_nodes, "_get_db_conn", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Mock RDS update index status failed")))
        
        chunks = [
            Chunk(chunk_id="chunk1", doc_id="doc1", version_no=1, chunk_index=1, chunk_type="text_chunk", chunk_text="test", token_count=1, page_num=1, permission_level="PUBLIC")
        ]
        ctx = {
            "bulk_batches": [{
                "chunks": chunks,
                "payload": "dummy",
                "payload_size": 5,
                "job_id": "JOB1",
                "oss_key": "dummy.jsonl",
                "result": {"indexed": 1, "failed": 0},
            }],
            "simulate_db": False,
        }
        
        with pytest.raises(RuntimeError, match="Database write failure in node_update_index_status: Mock RDS update index status failed"):
            node_update_index_status(ctx)


class TestStage3ProductionLoaderRegression:
    """Stage 3 生产模式加载器的回归与字段映射测试。"""

    def test_stage3_production_loader_no_crash(self, monkeypatch):
        # 1. 准备 Mock 数据库数据
        # 列序与 run_stage(stage=3) 生产 SELECT 对齐：
        # cm.id 在首位（HA3 主键 = chunk_meta.id → rds_id），doc_title 在末位（JOIN document_meta）
        mock_row_1 = (
            101,                       # 0: cm.id (rds_id → HA3 主键)
            "test_chunk_id_001",       # 1: chunk_id
            "test_doc_id",            # 2: doc_id
            1,                         # 3: version_no
            0,                         # 4: chunk_index
            2,                         # 5: page_num
            "Section 1",               # 6: section_title
            "http://oss.com/raw.pdf",  # 7: source_url (maps to source_url / source_oss_key)
            "text_chunk",              # 8: chunk_type
            "This is chunk text",      # 9: chunk_text
            10,                        # 10: token_count
            "native",                  # 11: source
            "rag-ready/public/admin/policy/test_doc_id/v1/content.md", # 12: rag_ready_key
            "PUBLIC",                  # 13: permission_level
            "admin",                   # 14: owner_dept
            "policy",                  # 15: category_l1
            "hr_policy",               # 16: category_l2
            1,                         # 17: sensitive_redacted
            1,                         # 18: is_active
            "NOT_STARTED",             # 19: embedding_status
            "NOT_INDEXED",             # 20: index_status
            "gemini-embedding-2",      # 21: embedding_model
            '{"custom_key": "custom_val"}', # 22: extra_json
            "测试文档标题",              # 23: doc_title
        )

        mock_row_fallback = (
            102,                       # 0: cm.id (rds_id)
            "test_chunk_id_002",       # 1: chunk_id
            "test_doc_id",            # 2: doc_id
            1,                         # 3: version_no
            1,                         # 4: chunk_index
            3,                         # 5: page_num
            "Section 2",               # 6: section_title
            "http://oss.com/raw2.pdf", # 7: source_url (maps to source_url / source_oss_key)
            "text_chunk",              # 8: chunk_type
            "This is fallback text",   # 9: chunk_text
            12,                        # 10: token_count
            "native",                  # 11: source
            None,                      # 12: rag_ready_key (NULL)
            "PUBLIC",                  # 13: permission_level
            "admin",                   # 14: owner_dept
            "policy",                  # 15: category_l1
            "hr_policy",               # 16: category_l2
            1,                         # 17: sensitive_redacted
            1,                         # 18: is_active
            "NOT_STARTED",             # 19: embedding_status
            "NOT_INDEXED",             # 20: index_status
            "gemini-embedding-2",      # 21: embedding_model
            None,                      # 22: extra_json (NULL)
            "",                        # 23: doc_title (COALESCE 空串)
        )

        mock_row_dict_extra = (
            103,                       # 0: cm.id (rds_id)
            "test_chunk_id_003",       # 1: chunk_id
            "test_doc_id",            # 2: doc_id
            1,                         # 3: version_no
            2,                         # 4: chunk_index
            4,                         # 5: page_num
            "Section 3",               # 6: section_title
            "http://oss.com/raw3.pdf", # 7: source_url
            "text_chunk",              # 8: chunk_type
            "This is pre-parsed text", # 9: chunk_text
            15,                        # 10: token_count
            "native",                  # 11: source
            "rag-ready/key3.md",       # 12: rag_ready_key
            "PUBLIC",                  # 13: permission_level
            "admin",                   # 14: owner_dept
            "policy",                  # 15: category_l1
            "hr_policy",               # 16: category_l2
            1,                         # 17: sensitive_redacted
            1,                         # 18: is_active
            "NOT_STARTED",             # 19: embedding_status
            "NOT_INDEXED",             # 20: index_status
            "gemini-embedding-2",      # 21: embedding_model
            {"pre_parsed": True},      # 22: extra_json is a DICT!
            "测试文档标题",              # 23: doc_title
        )

        # 2. Mock 数据库 Connection / Cursor
        class MockCursor:
            def execute(self, query, params=None):
                pass
            def fetchall(self):
                return [mock_row_1, mock_row_fallback, mock_row_dict_extra]
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockConnection:
            def cursor(self):
                return MockCursor()
            def close(self):
                pass

        # Monkeypatch 数据库连接函数
        import opensearch_pipeline.pipeline_nodes
        monkeypatch.setattr(
            opensearch_pipeline.pipeline_nodes,
            "_get_db_conn",
            lambda **kwargs: MockConnection()
        )

        # 3. Mock build_dag3_chunk_to_opensearch 以拦截 DAG 执行
        class MockDAG:
            def __init__(self):
                self.nodes = {}
            def run(self, ctx):
                # 这里进行断言验证，保证 Chunks 加载成功且字段映射完全正确
                assert "valid_chunks" in ctx
                chunks = ctx["valid_chunks"]
                assert len(chunks) == 3

                # 验证第一个 Chunk (带有有效的 rag_ready_key)
                c1 = chunks[0]
                # rds_id 必须取自 cm.id —— 它是 to_ha3_doc 的主键，重推/删除都靠它对齐
                assert c1.rds_id == 101
                assert c1.title == "测试文档标题"
                assert c1.chunk_id == "test_chunk_id_001"
                assert c1.doc_id == "test_doc_id"
                assert c1.version_no == 1
                assert c1.chunk_index == 0
                assert c1.page_num == 2
                assert c1.section_title == "Section 1"
                # source_oss_key 应正确映射为 rag_ready_key
                assert c1.source_oss_key == "rag-ready/public/admin/policy/test_doc_id/v1/content.md"
                assert c1.chunk_type == "text_chunk"
                assert c1.chunk_text == "This is chunk text"
                assert c1.token_count == 10
                assert c1.source == "native"
                assert c1.permission_level == "PUBLIC"
                assert c1.owner_dept == "admin"
                assert c1.category_l1 == "policy"
                assert c1.category_l2 == "hr_policy"
                assert c1.sensitive_redacted is True
                assert c1.is_active is True
                assert c1.embedding_status == "NOT_STARTED"
                assert c1.index_status == "NOT_INDEXED"
                assert c1.embedding_model == "gemini-embedding-2"
                # extra 应保存 traceability 信息
                assert c1.extra["rag_ready_key"] == "rag-ready/public/admin/policy/test_doc_id/v1/content.md"
                assert c1.extra["source_url"] == "http://oss.com/raw.pdf"
                assert c1.extra["custom_key"] == "custom_val"

                # 验证第二个 Chunk (rag_ready_key 为 NULL，触发 fallback)
                c2 = chunks[1]
                assert c2.rds_id == 102
                assert c2.chunk_id == "test_chunk_id_002"
                # source_oss_key 应 fallback 映射为 source_url
                assert c2.source_oss_key == "http://oss.com/raw2.pdf"
                assert c2.extra["rag_ready_key"] is None
                assert c2.extra["source_url"] == "http://oss.com/raw2.pdf"

                # 验证第三个 Chunk (extra_json 已经是 dict 格式)
                c3 = chunks[2]
                assert c3.rds_id == 103
                assert c3.chunk_id == "test_chunk_id_003"
                assert c3.source_oss_key == "rag-ready/key3.md"
                assert c3.extra["pre_parsed"] is True
                assert c3.extra["rag_ready_key"] == "rag-ready/key3.md"
                assert c3.extra["source_url"] == "http://oss.com/raw3.pdf"

                ctx["index_result"] = {"status": "SUCCESS"}
                return ctx

        import opensearch_pipeline.dataworks_orchestrator
        monkeypatch.setattr(
            opensearch_pipeline.dataworks_orchestrator,
            "build_dag3_chunk_to_opensearch",
            lambda: MockDAG()
        )

        # 4. 执行 Stage 3 生产模式加载器逻辑并进行断言验证
        from opensearch_pipeline.dataworks_orchestrator import run_stage
        run_stage(stage=3, bizdate="20260521", simulate=False)


class TestIndexingPartialFailureSafety:
    """Test safety handling of partial failures during indexing (DAG 3)."""

    def test_indexing_partial_failure_blocks_deactivation(self, monkeypatch):
        """Verify that when a chunk fails to index, DAG 3 raises RuntimeError and skips deactivation."""
        ctx = get_version_update_data()
        
        # Run DAG1 & DAG2 to construct normal canonicals and chunks
        ctx = build_dag1_raw_to_canonical().run(ctx)
        ctx = build_dag2_canonical_to_chunk().run(ctx)
        
        # Mock node_push_to_opensearch to simulate a partial failure (1 failed chunk)
        import opensearch_pipeline.pipeline_nodes
        original_push = opensearch_pipeline.pipeline_nodes.node_push_to_opensearch
        
        def mock_push_to_opensearch(context):
            # Run normal simulated push
            original_push(context)
            
            # Artificially inject a failure in the first chunk
            batches = context.get("bulk_batches")
            if batches:
                for batch in batches:
                    if batch["chunks"]:
                        batch["chunks"][0].index_status = "FAILED"
                        batch["chunks"][0].index_error_code = "500"
                        batch["chunks"][0].index_error_message = "Mocked push failure"
                        batch["result"] = {
                            "status": "PARTIAL_FAIL",
                            "took_ms": 10,
                            "indexed": len(batch["chunks"]) - 1,
                            "failed": 1,
                            "errors": True,
                            "index_name": "mock_index",
                        }
                context["index_result"] = {
                    "status": "PARTIAL_FAIL",
                    "took_ms": 10,
                    "indexed": sum(len(b["chunks"]) - 1 for b in batches),
                    "failed": len(batches),
                    "errors": True,
                    "index_name": "mock_index",
                }
                context["index_status"] = "PARTIAL_FAIL"

        monkeypatch.setattr(
            opensearch_pipeline.pipeline_nodes,
            "node_push_to_opensearch",
            mock_push_to_opensearch
        )
        import opensearch_pipeline.dag_definitions
        monkeypatch.setattr(
            opensearch_pipeline.dag_definitions,
            "node_push_to_opensearch",
            mock_push_to_opensearch
        )
        
        # Run DAG 3 and expect a RuntimeError in node_update_index_status (Node 04)
        dag3 = build_dag3_chunk_to_opensearch()
        
        # We run the DAG; Node 04 should fail, throwing a RuntimeError
        final_context = dag3.run(ctx)
        
        # Verify Node statuses in the DAG
        node_push = dag3.nodes["03"]
        node_update = dag3.nodes["04"]
        node_verify = dag3.nodes["04b"]
        node_deactivate = dag3.nodes["05"]

        assert node_push.status.name == "SUCCESS"
        assert node_update.status.name == "FAILED"
        assert "Index push had 1 failures" in node_update.error

        # Node 04b (parity verify) MUST be SKIPPED because Node 04 failed (parity flag off here anyway)
        assert node_verify.status.name == "SKIPPED"
        assert "dependency 04 is FAILED" in node_verify.error

        # Crucial Safety check: Node 05 (Deactivate Old Chunks) MUST be SKIPPED (now via 04b)
        assert node_deactivate.status.name == "SKIPPED"
        assert "dependency 04b is SKIPPED" in node_deactivate.error
        
        # Crucial Safety check: Old chunks from v1 must NOT have been deactivated
        deactivated = final_context.get("deactivated_chunks", [])
        assert len(deactivated) == 0

    def test_indexing_partial_failure_rds_update(self, monkeypatch):
        """Verify that when a chunk indexing fails under real DB mode, RDS document_version index_status is set to FAILED."""
        from opensearch_pipeline.pipeline_nodes import Chunk
        chunk = Chunk(
            chunk_id="test_chunk_rds_fail",
            doc_id="test_doc_rds",
            version_no=2,
            chunk_index=0,
            page_num=1,
            section_title="Intro",
            source_oss_key="oss_key",
            chunk_type="text_chunk",
            chunk_text="Some text",
            token_count=10,
            source="native",
            permission_level="PUBLIC",
            owner_dept="admin",
            category_l1="policy",
            category_l2="hr",
            sensitive_redacted=True,
            is_active=True,
            embedding_status="DONE",
            index_status="FAILED",  # Simulates failure
            embedding_model="mock-embedding"
        )
        
        ctx = {
            "simulate_db": False,
            "bulk_batches": [{
                "chunks": [chunk],
                "payload": "payload",
                "payload_size": 100,
                "job_id": "job_123",
                "oss_key": "oss_key",
                "result": {
                    "status": "PARTIAL_FAIL",
                    "took_ms": 5,
                    "indexed": 0,
                    "failed": 1,
                    "errors": True,
                    "index_name": "fuling_knowledge_v1",
                }
            }]
        }
        
        executed_queries = []
        class MockCursor:
            def execute(self, query, params=None):
                executed_queries.append((query, params))
            def fetchall(self):
                return []
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
                
        class MockConnection:
            def cursor(self):
                return MockCursor()
            def commit(self):
                executed_queries.append(("COMMIT", None))
            def close(self):
                pass
                
        import opensearch_pipeline.pipeline_nodes
        monkeypatch.setattr(
            opensearch_pipeline.pipeline_nodes,
            "_get_db_conn",
            lambda **kwargs: MockConnection()
        )
        
        from opensearch_pipeline.pipeline_nodes import node_update_index_status
        
        with pytest.raises(RuntimeError) as exc_info:
            node_update_index_status(ctx)
            
        assert "Index push had 1 failures" in str(exc_info.value)
        
        # Verify document_version status update SQL was executed before COMMIT
        db_updates = [q for q, p in executed_queries if "UPDATE document_version" in q]
        assert len(db_updates) == 1
        
        query, params = [qp for qp in executed_queries if "UPDATE document_version" in qp[0]][0]
        assert "SET index_status = 'FAILED'" in query
        assert params == ("test_doc_rds", 2)
        
        # Verify COMMIT was executed
        assert any(q == "COMMIT" for q, p in executed_queries)



