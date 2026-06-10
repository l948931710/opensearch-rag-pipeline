# -*- coding: utf-8 -*-
"""
test_latent_minor_fixes.py — 2026-06 系统评审「latent/minor」批次修复的回归测试。

覆盖：
1. HA3 主键兜底必须确定性（不能用 PYTHONHASHSEED 加盐的内建 hash()）
2. 生产安全守卫必须覆盖 VLM 模型（RAG_VLM_MODEL）
"""

import hashlib
import os

import pytest


# ═══════════════════════════════════════════════════════════════
# 1. HA3 pk 兜底确定性
# ═══════════════════════════════════════════════════════════════

class TestStablePkFallback:
    def test_fallback_pk_is_md5_derived(self):
        """无 rds_id 时主键 = chunk_id 的 md5 前 8 字节（63 位），与进程无关。"""
        from opensearch_pipeline.chunker import Chunk, _stable_pk_from_chunk_id

        chunk = Chunk(
            chunk_id="doc-1_v1_0003",
            doc_id="doc-1",
            version_no=1,
            chunk_index=3,
            chunk_type="text_chunk",
            chunk_text="正文",
            token_count=2,
        )
        doc = chunk.to_ha3_doc("id")

        expected = int.from_bytes(
            hashlib.md5(b"doc-1_v1_0003").digest()[:8], "big"
        ) & 0x7FFFFFFFFFFFFFFF
        assert doc["id"] == expected
        assert doc["id"] == _stable_pk_from_chunk_id("doc-1_v1_0003")
        # 63 位非负（HA3 INT64 主键）
        assert 0 <= doc["id"] <= 0x7FFFFFFFFFFFFFFF

    def test_rds_id_wins_over_fallback(self):
        """带 rds_id 时主键必须是 chunk_meta.id（生产路径），兜底不得介入。"""
        from opensearch_pipeline.chunker import Chunk

        chunk = Chunk(
            chunk_id="doc-1_v1_0003",
            doc_id="doc-1",
            version_no=1,
            chunk_index=3,
            chunk_type="text_chunk",
            chunk_text="正文",
            token_count=2,
            rds_id=42,
        )
        assert chunk.to_ha3_doc("id")["id"] == 42


# ═══════════════════════════════════════════════════════════════
# 2. 生产守卫覆盖 VLM 模型
# ═══════════════════════════════════════════════════════════════

def _fresh_load(**env_overrides):
    """在干净环境变量中执行 load_config()（与 test_config_loading.py 同款模式）。"""
    rag_keys = [k for k in os.environ if k.startswith("RAG_")]
    saved = {k: os.environ.pop(k) for k in rag_keys}
    for k in ["DASHSCOPE_API_KEY", "GEMINI_API_KEY"]:
        if k in os.environ:
            saved[k] = os.environ.pop(k)

    import opensearch_pipeline.config as cfg_module
    cfg_module._config = None

    try:
        os.environ.update(env_overrides)
        return cfg_module.load_config()
    finally:
        for k in list(env_overrides.keys()):
            os.environ.pop(k, None)
        os.environ.update(saved)
        cfg_module._config = None


class TestProductionGuardCoversVlmModel:
    def test_gemini_vlm_model_rejected_in_production(self):
        """RAG_VLM_MODEL=gemini-* 在生产环境必须被守卫拦截（此前被跳过）。"""
        with pytest.raises(ValueError, match=r"PRODUCTION SECURITY GUARD.*VLM"):
            _fresh_load(
                RAG_ENVIRONMENT="production",
                RAG_DASHSCOPE_API_KEY="sk-test",
                RAG_VLM_MODEL="gemini-3.1-flash-lite",
            )

    def test_qwen_vlm_model_passes_in_production(self):
        """默认 qwen3-vl-plus 通过守卫（不误伤）。"""
        config = _fresh_load(
            RAG_ENVIRONMENT="production",
            RAG_DASHSCOPE_API_KEY="sk-test",
        )
        assert config.ocr.vlm_model == "qwen3-vl-plus"


# ═══════════════════════════════════════════════════════════════
# 3. Qwen-VL 端点路由单一实现
# ═══════════════════════════════════════════════════════════════

class TestVlmEndpointRouting:
    NATIVE_BASE = "https://dashscope.aliyuncs.com/api/v1"
    COMPAT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_use_compat_mode_rules(self):
        from opensearch_pipeline.vlm_endpoint import use_compat_mode

        assert use_compat_mode("qwen3-vl-plus", self.NATIVE_BASE) is True
        assert use_compat_mode("qwen-vl-ocr-latest", self.NATIVE_BASE) is False
        assert use_compat_mode("qwen-vl-plus", self.COMPAT_BASE) is True
        assert use_compat_mode("", "") is False

    def test_compat_url_rebuilds_from_domain(self):
        """compat URL 必须按域名重建：/api/v1 原生 base 不能拼成 …/api/v1/compatible-mode/…"""
        from opensearch_pipeline.vlm_endpoint import compat_chat_completions_url, resolve_vlm_url

        assert (
            compat_chat_completions_url(self.NATIVE_BASE)
            == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        )
        assert (
            compat_chat_completions_url(self.COMPAT_BASE)
            == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        )
        # base 已是完整 chat/completions URL → 原样返回
        full = "https://gw.example.com/llm/v1/chat/completions"
        assert compat_chat_completions_url(full) == full
        # native 路由
        assert (
            resolve_vlm_url(self.NATIVE_BASE, use_compat=False)
            == "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        )

    def test_payload_shapes(self):
        from opensearch_pipeline.vlm_endpoint import build_image_chat_payload

        compat = build_image_chat_payload("qwen3-vl-plus", "识别", "AAA=", "image/png", True)
        assert compat["messages"][0]["content"][0]["type"] == "image_url"
        assert compat["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert "input" not in compat

        native = build_image_chat_payload(
            "qwen-vl-ocr-latest", "识别", "AAA=", "image/png", False, temperature=0)
        assert native["input"]["messages"][0]["content"][0]["image"].startswith("data:image/png;base64,")
        assert native["parameters"] == {"temperature": 0}
        assert "messages" not in native

    def test_extract_vlm_text_both_modes(self):
        from opensearch_pipeline.vlm_endpoint import extract_vlm_text

        compat_resp = {"choices": [{"message": {"content": "你好"}}]}
        assert extract_vlm_text(compat_resp, True) == "你好"

        native_resp = {"output": {"choices": [{"message": {"content": [{"text": "你"}, {"text": "好"}]}}]}}
        assert extract_vlm_text(native_resp, False) == "你好"

        native_str = {"output": {"choices": [{"message": {"content": "直接字符串"}}]}}
        assert extract_vlm_text(native_str, False) == "直接字符串"

    def test_ocr_client_routes_qwen3_to_compat(self, monkeypatch):
        """qwen3-vl-* 配成 OCR 模型时必须打 compatible-mode 端点（修复前打原生端点报错）。"""
        from opensearch_pipeline.extraction.ocr_client import OCRClient

        client = OCRClient.__new__(OCRClient)  # 跳过 __init__，只测 _call_ocr_api 路由
        client.api_key = "sk-test"
        client.api_base_url = self.NATIVE_BASE
        client.ocr_model = "qwen3-vl-plus"

        captured = {}

        class _FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "OCR文本"}}]}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResp()

        import requests
        monkeypatch.setattr(requests, "post", fake_post)

        text = client._call_ocr_api("AAA=", "image/png")
        assert text == "OCR文本"
        assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        assert captured["payload"]["messages"][0]["content"][0]["type"] == "image_url"


# ═══════════════════════════════════════════════════════════════
# 4. procedure_parent 子步骤展开（RDS parent_chunk_id 反查）
# ═══════════════════════════════════════════════════════════════

class _FakeCursor:
    """最小 DictCursor 假货：按 SQL 内容返回行。"""

    def __init__(self, rows_for_parent_query):
        self.queries = []
        self._rows = []
        self._rows_for_parent_query = rows_for_parent_query

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        self._rows = self._rows_for_parent_query if "parent_chunk_id IN" in sql else []

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *args, **kwargs):
        return self._cursor

    def close(self):
        pass


class TestProcedureParentExpansion:
    def test_children_come_from_rds_parent_chunk_id(self, monkeypatch):
        """procedure_parent 命中必须按 RDS parent_chunk_id 展开子步骤。

        旧实现读 HA3 结果里的 extra_json.child_chunk_ids —— HA3 的 output_fields
        根本不含 extra_json，子步骤展开是永远走不到的死分支。
        """
        import json as _json

        from opensearch_pipeline import pipeline_nodes
        from opensearch_pipeline.retriever import expand_step_context

        children_rows = [
            {
                "chunk_id": "P1-s1", "chunk_text": "第一步：打开业务导航", "step_no": 1,
                "section_title": "登录", "parent_chunk_id": "P1",
                "extra_json": _json.dumps({"annotation_map": {"①": "业务导航"}}),
                "image_refs_json": _json.dumps([{"oss_key": "images/s1.png", "visual_summary": "登录界面"}]),
            },
            {
                "chunk_id": "P1-s2", "chunk_text": "第二步：选择单据", "step_no": 2,
                "section_title": "登录", "parent_chunk_id": "P1",
                "extra_json": None,
                "image_refs_json": None,
            },
        ]
        cursor = _FakeCursor(children_rows)
        monkeypatch.setattr(pipeline_nodes, "_get_db_conn", lambda *a, **k: _FakeConn(cursor))

        hit = {
            "chunk_id": "P1", "chunk_type": "procedure_parent",
            "score": 9.0, "chunk_text": "U8 开单完整流程", "doc_id": "d1",
        }
        out = expand_step_context([hit], "U8 怎么开单")

        ids = [c.get("chunk_id") for c in out]
        assert ids == ["P1", "P1-s1", "P1-s2"]

        s1 = out[1]
        assert s1["is_expanded"] is True
        assert s1["expansion_reason"] == "parent_children"
        assert s1["parent_chunk_id"] == "P1"
        assert s1["score"] == pytest.approx(9.0 * 0.8)
        assert s1["annotation_map"] == {"①": "业务导航"}
        # image_refs 走统一归一化：契约键齐备（source_image 由 oss_key 互补）
        assert s1["image_refs"][0]["oss_key"] == "images/s1.png"
        assert s1["image_refs"][0]["source_image"] == "images/s1.png"
        assert s1["image_refs"][0]["visual_summary"] == "登录界面"

        # 只发一次批量兄弟/子步骤查询，没有逐命中的 child 查询
        assert len(cursor.queries) == 1
        assert "parent_chunk_id IN" in cursor.queries[0][0]
        assert cursor.queries[0][1] == ("P1",)


# ═══════════════════════════════════════════════════════════════
# 5. RAG_TOP_K / RAG_MAX_HISTORY_TURNS 接线（此前定义了但没人读）
# ═══════════════════════════════════════════════════════════════

class TestDeadConfigKnobsWired:
    def test_rag_top_k_env_is_effective(self):
        assert _fresh_load(RAG_TOP_K="3").rag.default_top_k == 3
        # 默认从 5 抬到 7：与生产实际值一致（评测锁定），接线不改变现行为
        assert _fresh_load().rag.default_top_k == 7

    def test_retrieve_and_enrich_default_top_k_from_config(self, monkeypatch):
        """top_k=None → 取 config.rag.default_top_k（穿透到 search_chunks）。"""
        import opensearch_pipeline.retriever as retriever_mod
        from opensearch_pipeline.config import get_config

        captured = {}
        monkeypatch.setattr(retriever_mod, "get_query_embedding", lambda q: None)
        monkeypatch.setattr(
            retriever_mod, "search_chunks",
            lambda query, top_k, user_dept=None, query_embedding=None, **kw:
                captured.update(top_k=top_k) or [],
        )
        monkeypatch.setattr(get_config().alibaba_vector, "rerank_enable", False)

        retriever_mod.retrieve_and_enrich("测试问题")
        assert captured["top_k"] == get_config().rag.default_top_k

        retriever_mod.retrieve_and_enrich("测试问题", top_k=3)
        assert captured["top_k"] == 3

    def test_session_store_history_turns_from_config(self):
        from opensearch_pipeline import session_store
        from opensearch_pipeline.config import get_config

        assert session_store.MAX_HISTORY_TURNS == get_config().rag.max_history_turns


# ═══════════════════════════════════════════════════════════════
# 6. 提取器格式处理：.xls 显式不支持；HTML/CSV 真解析
# ═══════════════════════════════════════════════════════════════

class TestExtractorFormatHandling:
    @staticmethod
    def _task(tmp_path, name, content, ext):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return {
            "doc_id": "d1", "version_no": 1, "file_ext": ext,
            "local_path": str(p), "raw_key": f"raw/it/{name}", "filename": name,
        }

    def test_xls_routes_to_unsupported(self):
        """旧版二进制 .xls 不再误投 openpyxl 静默失败，而是显式 unsupported。"""
        from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor

        extractor = UnifiedExtractor(simulate=True)
        result = extractor.extract({
            "doc_id": "d1", "version_no": 1, "file_ext": "xls",
            "raw_key": "raw/it/旧表.xls", "filename": "旧表.xls",
        })
        assert result.extract_method == "unsupported:xls"
        assert any("Unsupported" in w for w in result.warnings)

    def test_html_is_stripped_to_text(self, tmp_path):
        from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor

        html = (
            "<html><head><style>.x{color:red}</style></head><body>"
            "<h1>差旅报销制度</h1><p>第一条 出差需提前申请。</p>"
            "<script>var tracking = 1;</script></body></html>"
        )
        extractor = UnifiedExtractor(simulate=True)
        result = extractor._extract_text(self._task(tmp_path, "policy.html", html, "html"))

        assert result.extract_method == "html_text"
        assert "差旅报销制度" in result.text
        assert "第一条 出差需提前申请。" in result.text
        assert "tracking" not in result.text
        assert ".x{color:red}" not in result.text
        assert "<p>" not in result.text

    def test_csv_is_parsed_with_quoting(self, tmp_path):
        from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor

        csv_content = '物料,数量\n"吸管,纸质",1000\n'
        extractor = UnifiedExtractor(simulate=True)
        result = extractor._extract_text(self._task(tmp_path, "bom.csv", csv_content, "csv"))

        assert result.extract_method == "csv_table"
        assert "物料 | 数量" in result.text
        # 引号内逗号是单元格内容，不是分隔符
        assert "吸管,纸质 | 1000" in result.text

    def test_plain_txt_unchanged(self, tmp_path):
        from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor

        extractor = UnifiedExtractor(simulate=True)
        result = extractor._extract_text(self._task(tmp_path, "note.txt", "普通文本内容", "txt"))
        assert result.extract_method == "plain_text"
        assert "普通文本内容" in result.text
