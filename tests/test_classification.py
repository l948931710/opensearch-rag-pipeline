# -*- coding: utf-8 -*-
"""
test_classification.py — 针对升级版文档分类、路径权限推导、Fail-Safe 兜底和 Spot-Check 安全检测的单元与集成测试
"""

import pytest
import unittest.mock as mock
import requests
from datetime import datetime

from opensearch_pipeline.pipeline_nodes import (
    resolve_permission_level,
    node_classify_and_risk_assess,
    _get_db_conn
)
from opensearch_pipeline.spot_checker import run_spot_check_pipeline
from opensearch_pipeline.config import get_config
from tests.local_stack import requires_local_db

@pytest.fixture(autouse=True)
def force_gemini_config():
    """强制在单元测试中将全局 LLM 配置重置为 Gemini 默认值，避免受本地 .env 的 Qwen/DashScope 环境变量干扰。"""
    config = get_config()
    orig_api_key = config.llm.api_key
    orig_api_base_url = config.llm.api_base_url
    orig_model = config.llm.model
    
    # 设为默认的 Gemini 模式值
    config.llm.api_key = "AIzaSy_mock_gemini_key_for_tests"
    config.llm.api_base_url = "https://generativelanguage.googleapis.com/v1beta"
    config.llm.model = "gemini-3.1-flash-lite"
    
    yield
    
    # 恢复原状
    config.llm.api_key = orig_api_key
    config.llm.api_base_url = orig_api_base_url
    config.llm.model = orig_model

@pytest.fixture(autouse=True)
def clean_db():
    """清理数据库状态，保证测试隔离性。"""
    # 🛡️ 防 sim→prod 泄露（2026-06-13 事故根因）：
    # 这个 fixture 是 autouse=True 且无条件 DELETE FROM chunk_meta + UPDATE document_version。
    # 不是 local host 直接 skip，绝不让它穿透到生产或 staging RDS。
    from tests.local_stack import ensure_local_db_wired, local_db_unavailable_reason
    if not ensure_local_db_wired():
        pytest.skip(f"clean_db fixture refusing non-local RDS: {local_db_unavailable_reason()}")

    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as c:
            c.execute("UPDATE document_version SET extraction_status='NOT_STARTED', content_process_status='NOT_STARTED', chunk_status='NOT_STARTED', index_status='NOT_INDEXED'")
            c.execute("DELETE FROM chunk_meta")
            c.execute("DELETE FROM review_task")
            c.execute("DELETE FROM document_meta")
            c.execute("DELETE FROM document_version")
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Failed to reset DB: {e}")

class TestPathPermissionResolution:
    """路径权限自动判定测试（完全绕过模型）。"""

    def test_restricted_path(self):
        doc = {"doc_id": "doc1", "source_key": "raw/restricted/secret_payroll.pdf"}
        ctx = {"tasks": []}
        assert resolve_permission_level(doc, ctx) == "restricted"

    def test_internal_path(self):
        doc = {"doc_id": "doc2", "source_key": "raw/dept_internal/team_sop.docx"}
        ctx = {"tasks": []}
        # 与 HA3 过滤表达式对齐：permission_level="dept_internal" 才会按 owner_dept 放行
        assert resolve_permission_level(doc, ctx) == "dept_internal"

    def test_legacy_internal_alias_normalized(self):
        """显式写入的历史值 'internal' 必须归一为 'dept_internal'，否则对所有人不可见。"""
        doc = {"doc_id": "doc2b", "source_key": "raw/whatever/file.docx",
               "permission_level": "internal"}
        ctx = {"tasks": []}
        assert resolve_permission_level(doc, ctx) == "dept_internal"

    def test_public_path(self):
        doc = {"doc_id": "doc3", "source_key": "raw/public/faq.txt"}
        ctx = {"tasks": []}
        assert resolve_permission_level(doc, ctx) == "public"

    def test_explicit_doc_permission(self):
        doc = {"doc_id": "doc4", "source_key": "raw/restricted/secret.pdf", "permission_level": "public"}
        ctx = {"tasks": []}
        # 显式优先
        assert resolve_permission_level(doc, ctx) == "public"

    def test_task_metadata_permission(self):
        doc = {"doc_id": "doc5", "source_key": "raw/some_random_path/file.pdf"}
        ctx = {"tasks": [{"doc_id": "doc5", "permission_level": "restricted"}]}
        assert resolve_permission_level(doc, ctx) == "restricted"


class TestLiveClassificationPipeline:
    """生产级分类与安全兜底测试。"""

    def test_simulate_mode_backward_compatibility(self):
        """测试模拟模式依然返回预置 mock 元数据。"""
        doc = {
            "doc_id": "doc_sim",
            "version_no": 1,
            "text": "车间安全规程",
            "source_key": "raw/public/sop.txt"
        }
        ctx = {
            "canonicals": [doc],
            "simulate": True,
            "mock_classification": {
                "category_l1": "sop",
                "category_l2": "equipment_sop",
                "owner_dept": "production",
                "confidence": 0.95,
                "risk_level": "low"
            }
        }
        node_classify_and_risk_assess(ctx)
        
        # 检查上下文更新
        assert doc["category_l1"] == "sop"
        assert doc["category_l2"] == "equipment_sop"
        assert doc["permission_level"] == "public"  # 由路径自动判定
        assert doc["confidence"] == 0.95
        assert doc["llm_risk_level"] == "low"
        assert doc["classification_status"] == "CONTENT_CLASSIFIED"

    @requires_local_db
    @mock.patch("opensearch_pipeline.pipeline_nodes.run_gemini_classification")
    def test_low_confidence_no_quarantine(self, mock_gemini):
        """测试置信度低于 0.85 时，记录警告但继续正常入库（review 机制已关闭）。"""
        mock_gemini.return_value = {
            "category_l1": "sop",
            "category_l2": "hr",
            "owner_dept": "hr",
            "faq_eligible": True,
            "confidence": 0.72,  # 低置信度！
            "llm_risk_level": "medium",
            "summary": "低置信度测试文档"
        }

        # 插入基础数据到 RDS，以执行真实 DB 写操作
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO document_meta (doc_id, title) VALUES ('doc_low', 'Low Confidence Doc')")
            cursor.execute("INSERT INTO document_version (doc_id, version_no, content_process_status) VALUES ('doc_low', 1, 'NOT_STARTED')")
            conn.commit()
        conn.close()

        doc = {
            "doc_id": "doc_low",
            "version_no": 1,
            "text": "一些模棱两可的内容",
            "source_key": "raw/public/sample.txt"
        }
        ctx = {
            "canonicals": [doc],
            "simulate": False
        }

        node_classify_and_risk_assess(ctx)

        # 1. 验证正常入库，不隔离
        assert doc["classification_status"] == "CONTENT_CLASSIFIED"
        assert doc["category_l1"] == "sop"
        assert doc["confidence"] == 0.72
        assert doc["permission_level"] == "public"  # 路径判定，不降权
        assert "redaction_action" not in doc or doc.get("redaction_action") != "QUARANTINE"

        # 2. 验证 RDS 中未插入 review_task
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM review_task WHERE doc_id='doc_low'")
            count = cursor.fetchone()[0]
            assert count == 0

            # 验证 document_version 状态为正常
            cursor.execute("SELECT classification_status, classification_confidence FROM document_version WHERE doc_id='doc_low'")
            ver = cursor.fetchone()
            assert ver[0] == "CONTENT_CLASSIFIED"
            assert float(ver[1]) == 0.72
        conn.close()

    @requires_local_db
    @mock.patch("opensearch_pipeline.pipeline_nodes.run_gemini_classification")
    def test_api_failure_failsafe(self, mock_gemini):
        """测试 Gemini API 调用超时或出错时，触发 Fail-Safe 安全保护及 review_task 注册。"""
        # 模拟 API 抛出超时异常
        mock_gemini.side_effect = requests.exceptions.ConnectTimeout("Connection timed out to Google APIs")

        # 插入基础数据到 RDS
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO document_meta (doc_id, title) VALUES ('doc_fail', 'API Timeout Doc')")
            cursor.execute("INSERT INTO document_version (doc_id, version_no, content_process_status) VALUES ('doc_fail', 1, 'NOT_STARTED')")
            conn.commit()
        conn.close()

        doc = {
            "doc_id": "doc_fail",
            "version_no": 1,
            "text": "网络故障文档",
            "source_key": "raw/public/broken_net.txt"
        }
        ctx = {
            "canonicals": [doc],
            "simulate": False
        }

        # 触发调用
        node_classify_and_risk_assess(ctx)

        # 1. 验证内存被强制锁定为隔离和受限
        assert doc["redaction_action"] == "QUARANTINE"
        assert doc["risk_level"] == "high"
        assert doc["permission_level"] == "restricted"
        assert doc["kb_type"] == "private"
        assert doc["confidence"] == 0.0
        assert doc["classification_status"] == "PENDING_AUDIT"

        # 2. 验证 RDS 写入了 Fail-Safe 审核记录
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("SELECT review_status, review_reason, suggested_permission_level FROM review_task WHERE doc_id='doc_fail'")
            task = cursor.fetchone()
            assert task is not None
            assert task[0] == "PENDING"
            assert "Connection timed out" in task[1] or "Gemini API invocation failed" in task[1]
            assert task[2] == "restricted"

            # 验证 document_version 状态为 FAILED 且记录了错误信息
            cursor.execute("SELECT content_process_status, content_process_error, risk_level FROM document_version WHERE doc_id='doc_fail'")
            ver = cursor.fetchone()
            assert ver[0] == "FAILED"
            assert "Connection timed out" in ver[1] or "Gemini API invocation failed" in ver[1]
            assert ver[2] == "high"
        conn.close()

    @requires_local_db
    @mock.patch("requests.post")
    def test_gemini_classification_markdown_json_parsing(self, mock_post):
        """测试 Gemini API 返回带 Markdown 围栏的 JSON 时，能够被稳健解析且不触发隔离。"""
        # Mock 接口返回带 ```json \n { ... } \n ``` 围栏的 JSON 字符串
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": "```json\n{\n  \"category_l1\": \"reference\",\n  \"category_l2\": \"manual\",\n  \"owner_dept\": \"hr\",\n  \"faq_eligible\": true,\n  \"confidence\": 0.92,\n  \"llm_risk_level\": \"low\",\n  \"summary\": \"员工福利手册摘要\"\n}\n```"
                            }
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_response

        # 插入基础数据到 RDS，以执行真实 DB 写操作
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO document_meta (doc_id, title) VALUES ('doc_markdown', 'Markdown Wrapped Doc')")
            cursor.execute("INSERT INTO document_version (doc_id, version_no, content_process_status) VALUES ('doc_markdown', 1, 'NOT_STARTED')")
            conn.commit()
        conn.close()

        doc = {
            "doc_id": "doc_markdown",
            "version_no": 1,
            "text": "本手册适用于全体员工...",
            "source_key": "raw/public/manual_v2.pdf"
        }
        ctx = {
            "canonicals": [doc],
            "simulate": False
        }

        # 调用节点处理
        node_classify_and_risk_assess(ctx)

        # 验证是否成功解析并分类（置信度高，无安全风险，不应被隔离）
        assert doc["category_l1"] == "reference"
        assert doc["category_l2"] == "manual"
        assert doc["permission_level"] == "public"  # 由路径自动判定
        assert doc["confidence"] == 0.92
        assert doc["llm_risk_level"] == "low"
        assert doc.get("redaction_action") != "QUARANTINE"
        assert doc["classification_status"] == "CONTENT_CLASSIFIED"

        # 验证数据库状态已更新为 PROCESSING (preempt)
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("SELECT content_process_status, classification_status, risk_level FROM document_version WHERE doc_id='doc_markdown'")
            ver = cursor.fetchone()
            assert ver[0] == "PROCESSING"
            assert ver[1] == "CONTENT_CLASSIFIED"
            assert ver[2] == "low"
            
            # 确认没有产生针对此 doc_markdown 的任何 review_task
            cursor.execute("SELECT COUNT(*) FROM review_task WHERE doc_id='doc_markdown'")
            count = cursor.fetchone()[0]
            assert count == 0
        conn.close()

    @requires_local_db
    @mock.patch("opensearch_pipeline.pipeline_nodes.run_gemini_classification")
    def test_api_failure_exceptionally_long_error_truncation(self, mock_gemini):
        """测试 Gemini API 抛出极其庞大的错误信息（超过 1000 字符）时，
        是否能够在应用层成功截断并在 review_task 成功插入，而不抛出 Data too long 异常。
        """
        # 构造超过 1000 字符的超长错误信息
        extremely_long_error = "Google Cloud API Authentication Error Details: " + "A" * 1200
        mock_gemini.side_effect = Exception(extremely_long_error)

        # 插入基础数据到 RDS
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO document_meta (doc_id, title) VALUES ('doc_long_fail', 'Long Error Doc')")
            cursor.execute("INSERT INTO document_version (doc_id, version_no, content_process_status) VALUES ('doc_long_fail', 1, 'NOT_STARTED')")
            conn.commit()
        conn.close()

        doc = {
            "doc_id": "doc_long_fail",
            "version_no": 1,
            "text": "异常错误截断测试内容",
            "source_key": "raw/public/long_error.txt"
        }
        ctx = {
            "canonicals": [doc],
            "simulate": False
        }

        # 触发调用，若无截断，这将在 pymysql 执行插入时抛出 DataTooLong 错误并导致 silent error，甚至使测试失败
        node_classify_and_risk_assess(ctx)

        # 验证 RDS 写入了 Fail-Safe 审核记录，且内容已被截断到 500 字符以内且包含 "..."
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("SELECT review_status, review_reason FROM review_task WHERE doc_id='doc_long_fail'")
            task = cursor.fetchone()
            assert task is not None
            assert task[0] == "PENDING"
            
            reason = task[1]
            assert len(reason) <= 500
            assert reason.endswith("...")
            assert "Google Cloud API Authentication" in reason

            # 验证 document_version 状态为 FAILED 且记录了错误信息（其 column 为 TEXT，因此可保存较长的报错，但应不受溢出影响）
            cursor.execute("SELECT content_process_status FROM document_version WHERE doc_id='doc_long_fail'")
            ver = cursor.fetchone()
            assert ver[0] == "FAILED"
        conn.close()

    def test_clean_llm_json_response_edge_cases(self):
        """测试 _clean_llm_json_response 各种边缘格式的清洗逻辑。"""
        from opensearch_pipeline.pipeline_nodes import _clean_llm_json_response
        import json

        # 1. 干净的标准 JSON
        raw_1 = '{"key": "value"}'
        assert json.loads(_clean_llm_json_response(raw_1)) == {"key": "value"}

        # 2. 带 markdown 围栏的标准包裹
        raw_2 = '```json\n{"key": "value"}\n```'
        assert json.loads(_clean_llm_json_response(raw_2)) == {"key": "value"}

        # 3. 带 markdown 围栏但没有指定语言且没有换行
        raw_3 = '```{"key": "value"}```'
        assert json.loads(_clean_llm_json_response(raw_3)) == {"key": "value"}

        # 4. 前后伴随解释性废话
        raw_4 = 'Here is the result:\n```json\n{"key": "value"}\n```\nHope this is correct.'
        assert json.loads(_clean_llm_json_response(raw_4)) == {"key": "value"}

        # 5. JSON 数组测试
        raw_5 = 'Result: [{"id": 1}, {"id": 2}] - end'
        assert json.loads(_clean_llm_json_response(raw_5)) == [{"id": 1}, {"id": 2}]


class TestSpotCheckerSafetyDaemon:
    """定时抽检及 mismatches 隔离删除测试。"""

    @requires_local_db
    @mock.patch("requests.post")
    def test_spot_check_permission_leak_quarantine(self, mock_post):
        """测试已发布文档在抽检时被识别出权限泄露时，自动触发彻底下线和删除。"""
        # 1. 准备 mock 的 Gemini 抽检审核建议：建议权限为 restricted (存在安全泄露)
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"safety_status": "unsafe", "suggested_permission_level": "restricted", "reason": "Contains payroll information."}'
                            }
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_response

        # 2. 写入已发布并成功的 document_meta, document_version 和 chunk_meta
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO document_meta (doc_id, title, permission_level, kb_type) 
                VALUES ('doc_leak', 'Payroll Spreadsheet', 'public', 'public')
            """)
            cursor.execute("""
                INSERT INTO document_version (doc_id, version_no, index_status, status)
                VALUES ('doc_leak', 1, 'SUCCESS', 'active')
            """)
            cursor.execute("""
                INSERT INTO chunk_meta (chunk_id, doc_id, version_no, chunk_index, chunk_text, permission_level, is_active)
                VALUES ('doc_leak_v1_c0', 'doc_leak', 1, 0, 'Employees payroll monthly: Alice $8000', 'public', TRUE)
            """)
            conn.commit()
        conn.close()

        # 3. 运行抽检，配置采样比例为 1.0 强制检查所有样本
        # mock OpenSearch delete_by_query 行为以防止连接真正的 OpenSearch 服务出错
        with mock.patch("opensearch_pipeline.spot_checker._get_opensearch_client") as mock_os:
            mock_client = mock.Mock()
            mock_client.delete_by_query.return_value = {"deleted": 1}
            # 确保 mock 不暴露 push_documents，走 Standard OpenSearch 路径
            del mock_client.push_documents
            mock_os.return_value = mock_client

            report = run_spot_check_pipeline(limit_or_percent=1.0, simulate=False)

            # 4. 验证抽检报告
            assert report["total_indexed_documents"] == 1
            assert report["mismatch_detected"] == 1
            assert len(report["quarantined_documents"]) == 1
            assert report["quarantined_documents"][0]["doc_id"] == "doc_leak"
            assert report["quarantined_documents"][0]["suggested_permission"] == "restricted"

            # 验证 OpenSearch 被调用删除了该文档的 chunks
            mock_client.delete_by_query.assert_called_once()

        # 5. 验证 RDS 状态已被彻底隔离和下线
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            # document_version -> QUARANTINED
            cursor.execute("SELECT publish_status, gate_status, risk_level, index_status FROM document_version WHERE doc_id='doc_leak'")
            ver = cursor.fetchone()
            assert ver[0] == "QUARANTINED"
            assert ver[1] == "quarantined"
            assert ver[2] == "high"
            assert ver[3] == "DELETED"  # 索引删除成功后标记

            # document_meta -> restricted
            cursor.execute("SELECT permission_level, kb_type FROM document_meta WHERE doc_id='doc_leak'")
            meta = cursor.fetchone()
            assert meta[0] == "restricted"
            assert meta[1] == "private"

            # chunk_meta -> is_active = FALSE
            cursor.execute("SELECT is_active FROM chunk_meta WHERE doc_id='doc_leak'")
            chunk = cursor.fetchone()
            assert not chunk[0]

            # review_task -> PENDING spot_check_mismatch审核
            cursor.execute("SELECT review_status, review_type, suggested_permission_level FROM review_task WHERE doc_id='doc_leak'")
            task = cursor.fetchone()
            assert task is not None
            assert task[0] == "PENDING"
            assert task[1] == "spot_check_mismatch"
            assert task[2] == "restricted"
        conn.close()

    @requires_local_db
    def test_local_real_simulation_mode(self):
        """测试本地真实模拟模式：simulate=False, simulate_api=True。"""
        doc = {
            "doc_id": "doc_local_real_sim",
            "version_no": 1,
            "text": "财务流水账单",
            "source_key": "raw/restricted/finance_2026.pdf"
        }
        
        # 写入前置 DB 数据
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO document_meta (doc_id, title, original_filename, owner_dept, status, current_version_no)
                VALUES ('doc_local_real_sim', '财务流水账单', 'finance_2026.pdf', 'finance', 'active', 1)
            """)
            cursor.execute("""
                INSERT INTO document_version (doc_id, version_no, file_ext, gate_status, content_process_status, chunk_status, index_status, status)
                VALUES ('doc_local_real_sim', 1, 'pdf', 'pending_clean', 'NOT_STARTED', 'NOT_STARTED', 'NOT_INDEXED', 'active')
            """)
            conn.commit()
        conn.close()

        ctx = {
            "canonicals": [doc],
            "simulate": False,
            "simulate_api": True,
            "mock_classification": {
                "category_l1": "record",
                "category_l2": "business",
                "owner_dept": "finance",
                "confidence": 0.96,
                "risk_level": "medium",
                "summary": "财务流水"
            }
        }

        # 运行分类评估。这应该执行真实的 DB 更新（因为 simulate=False），但是模拟 API 调用（因为 simulate_api=True）
        node_classify_and_risk_assess(ctx)

        # 检查上下文更新
        assert doc["category_l1"] == "record"
        assert doc["category_l2"] == "business"
        assert doc["confidence"] == 0.96
        assert doc["classification_status"] == "CONTENT_CLASSIFIED"

        # 验证数据库更新
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cursor:
            cursor.execute("SELECT content_process_status, classification_confidence FROM document_version WHERE doc_id='doc_local_real_sim'")
            row = cursor.fetchone()
            assert row[0] == "PROCESSING" # 被 preempt 并处于处理中或已处理状态
            cursor.execute("SELECT category_l1, category_l2 FROM document_meta WHERE doc_id='doc_local_real_sim'")
            meta_row = cursor.fetchone()
            assert meta_row[0] == "record"
            assert meta_row[1] == "business"
        conn.close()

    @mock.patch("requests.post")
    def test_dashscope_qwen_classification(self, mock_post):
        """测试使用 DashScope 接口调用 Qwen 模型成功分类。"""
        import json
        from opensearch_pipeline.pipeline_nodes import run_gemini_classification
        
        # 模拟 DashScope (阿里云百炼) 的 OpenAI 兼容返回格式
        mock_response = mock.Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({
                            "category_l1": "sop",
                            "category_l2": "equipment_sop",
                            "owner_dept": "production",
                            "faq_eligible": False,
                            "confidence": 0.95,
                            "llm_risk_level": "low",
                            "summary": "车间流水线操作安全规范说明书"
                        })
                    }
                }
            ],
            "usage": {
                "total_tokens": 120,
                "input_tokens": 80,
                "output_tokens": 40
            },
            "id": "test-request-id-123"
        }
        mock_post.return_value = mock_response

        # 调用 run_gemini_classification
        res = run_gemini_classification(
            text="车间流水线操作安全规范说明书：所有工人必须戴好安全帽才能进入车间...",
            model_name="qwen-plus",
            api_key="sk-test-dashscope-key",
            api_base_url="https://dashscope.aliyuncs.com/api/v1"
        )

        # 验证返回内容
        assert res["category_l1"] == "sop"
        assert res["category_l2"] == "equipment_sop"
        assert res["confidence"] == 0.95
        assert res["llm_risk_level"] == "low"
        
        # 验证 requests.post 的参数
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://dashscope.aliyuncs.com/api/v1/compatible-mode/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test-dashscope-key"
        payload = kwargs["json"]
        assert payload["model"] == "qwen-plus"
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"
        assert "Required JSON Schema:" in payload["messages"][0]["content"]

    @mock.patch("requests.post")
    def test_dashscope_qwen_classification_error(self, mock_post):
        """测试 DashScope 接口调用失败时的异常处理。"""
        from opensearch_pipeline.pipeline_nodes import run_gemini_classification
        
        mock_response = mock.Mock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized: Invalid API Key"
        mock_post.return_value = mock_response

        with pytest.raises(Exception) as exc_info:
            run_gemini_classification(
                text="测试内容",
                model_name="qwen-plus",
                api_key="bad-key",
                api_base_url="https://dashscope.aliyuncs.com/api/v1"
            )
        assert "DashScope API returned status code 401" in str(exc_info.value)

