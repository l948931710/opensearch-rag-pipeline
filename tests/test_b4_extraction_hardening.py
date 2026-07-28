# -*- coding: utf-8 -*-
"""B4（生产级外审 2026-07-17 P1-03/P2-03）：提取面硬化。

① _safe_xml_fromstring：Office 内部 XML 拒 DOCTYPE/ENTITY + 尺寸预算（defusedxml
   的零依赖窄化替代）；② _zip_member_bytes：xlsx 部件读取前预算；③ VLM 缓存键
   MD5→SHA-256 + 命名空间默认版本 "2"；④ 摄取路径 OSS 大小闸 → partial_loss_notes。
"""
import zipfile
from types import SimpleNamespace

import pytest

from opensearch_pipeline.extraction.image_extraction_utils import (
    _safe_xml_fromstring,
    _zip_member_bytes,
)


class TestSafeXmlFromstring:
    def test_normal_xml_parses(self):
        root = _safe_xml_fromstring(b"<root><a>1</a></root>")
        assert root.find("a").text == "1"

    def test_doctype_rejected(self):
        with pytest.raises(ValueError, match="DOCTYPE/ENTITY"):
            _safe_xml_fromstring(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "b">]><r>&a;</r>')

    def test_billion_laughs_entity_rejected(self):
        payload = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                   b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><lolz>&lol2;</lolz>')
        with pytest.raises(ValueError, match="DOCTYPE/ENTITY"):
            _safe_xml_fromstring(payload)

    def test_oversize_rejected(self):
        with pytest.raises(ValueError, match="超预算"):
            _safe_xml_fromstring(b"<r>" + b"x" * (21 * 1024 * 1024) + b"</r>")


class TestZipMemberBudget:
    def test_normal_member_reads(self, tmp_path):
        p = tmp_path / "a.xlsx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
        with zipfile.ZipFile(p) as z:
            assert _zip_member_bytes(z, "xl/workbook.xml") == b"<workbook/>"

    def test_oversize_member_rejected(self):
        fake = SimpleNamespace(getinfo=lambda name: SimpleNamespace(
            file_size=30 * 1024 * 1024, compress_size=30 * 1024 * 1024))
        with pytest.raises(ValueError, match="超预算"):
            _zip_member_bytes(fake, "xl/big.xml")

    def test_ratio_bomb_rejected(self):
        fake = SimpleNamespace(getinfo=lambda name: SimpleNamespace(
            file_size=10 * 1024 * 1024, compress_size=1024))
        with pytest.raises(ValueError, match="压缩比异常"):
            _zip_member_bytes(fake, "xl/bomb.xml")


class TestVlmCacheKeyUpgrade:
    def test_ns_default_version_2(self, monkeypatch):
        from opensearch_pipeline.extraction.unified_extractor import _vlm_cache_ns
        # 本用例钉的是**缓存版本段**（B4 P2-03），不是漏斗策略段。2026-07-26 起
        # RAG_FUNNEL_DISCARD_REQUIRES_CATEGORY 默认 ON ⇒ 键默认多一个 `:c1` 后缀
        # （策略段的契约由 tests/test_funnel_discard_requires_category.py 单独钉）。
        # 这里显式关掉它，让断言只覆盖版本段、保持本用例原意。
        monkeypatch.setenv("RAG_FUNNEL_DISCARD_REQUIRES_CATEGORY", "0")
        monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
        assert _vlm_cache_ns(True) == "pub:2"          # B4 P2-03：默认版本 2
        assert _vlm_cache_ns(False) == "sec:2"
        monkeypatch.setenv("RAG_VLM_CACHE_VERSION", "")
        assert _vlm_cache_ns(True) == "pub"            # 显式清空可还原历史裸键形

    def test_funnel_hash_is_sha256(self):
        """源码契约：安全判定缓存主键不得再用 MD5（构造碰撞可继承 CLEAN 结论）。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "opensearch_pipeline/extraction/unified_extractor.py").read_text(
            encoding="utf-8")
        seg = src[src.index("Phase 1.5"):src.index("Phase 1.5") + 1500]
        assert "hashlib.sha256" in seg and "hashlib.md5" not in seg


class TestIngestOversizeGate:
    def test_oversize_object_skips_download_and_flags_review(self, monkeypatch):
        import opensearch_pipeline.pipeline_nodes as pn

        calls = {"download": 0}

        class _FakeBucket:
            def head_object(self, key):
                return SimpleNamespace(content_length=10 * 1024 * 1024)

            def get_object_to_file(self, key, path):
                calls["download"] += 1
                raise AssertionError("oversize 对象不应被下载")

        monkeypatch.setenv("RAG_EXTRACT_MAX_BYTES", str(1024 * 1024))   # 1MB 闸
        monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (_FakeBucket(), False))
        # simulate_oss=False 细粒度打开 OSS 下载分支（bucket 被 monkeypatch 成假件），
        # 其余面保持 simulate（_resolve_simulate: 细粒度键 > 全局 simulate）
        ctx = {"tasks": [{"doc_id": "d-big", "version_no": 1, "file_ext": "txt",
                          "raw_key": "raw/prod/超大.txt"}],
               "simulate": True, "simulate_oss": False}
        pn.node_extract_text_with_ocr(ctx)
        assert calls["download"] == 0
        notes = list(getattr(ctx["extractions"][0], "partial_loss_notes", []) or [])
        assert any("[OVERSIZE]" in n for n in notes)    # → NEEDS_REVIEW 收尾通道

    def test_normal_size_downloads(self, monkeypatch):
        import opensearch_pipeline.pipeline_nodes as pn

        class _FakeBucket:
            def head_object(self, key):
                return SimpleNamespace(content_length=100)

            def get_object_to_file(self, key, path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("正文内容")

        monkeypatch.setenv("RAG_EXTRACT_MAX_BYTES", str(1024 * 1024))
        monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (_FakeBucket(), False))
        ctx = {"tasks": [{"doc_id": "d-ok", "version_no": 1, "file_ext": "txt",
                          "raw_key": "raw/prod/正常.txt"}],
               "simulate": True, "simulate_oss": False}
        pn.node_extract_text_with_ocr(ctx)
        res = ctx["extractions"][0]
        assert not any("[OVERSIZE]" in n
                       for n in (getattr(res, "partial_loss_notes", []) or []))
        assert res.text_length > 0                      # 正常下载并提取
