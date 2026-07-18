# -*- coding: utf-8 -*-
"""stage-1 环境性抽取失败必须炸出来（2026-07-16 现场：/usr/bin/python3 缺 openpyxl）。

缺陷链条：基础抽取器（openpyxl/python-pptx/python-docx）缺失时，unified_extractor
的兜底 except 把 ImportError 吞成 "Failed to extract ...: No module named '...'"
warning → node_build_canonical 照常产出 0 chars/0 blocks 的空 canonical 并标
extraction_status='COMPLETED'（SUCCESS）→ 空文档只在 stage-2 落 SKIPPED_EMPTY，
根因（环境缺依赖）被彻底掩盖。

两道防线（本文件逐条验证）：
  1. node_extract_text_with_ocr 预检（fail-fast，默认开，RAG_EXTRACT_DEP_PREFLIGHT=off
     应急旁路）：本批任务涉及 xlsx/pptx/docx 而对应模块缺失 → 在任何下载/抽取前
     RuntimeError，零状态写入；mock_text 任务不参与预检。
  2. node_build_canonical ENV-DEP 守卫（兜底）：「0 块且 0 字 且 warnings 含
     'No module named'」组合 → 该 doc 标 extraction_status='FAILED'（不写 canonical
     keys → stage-2 永不认领空壳），循环后统一 raise 炸红运行；健康文档照常落库。
铁律不破：图片/OCR 等辅助依赖失败（文本块仍在，或无缺模块警告）照旧优雅降级 SUCCESS。
"""
import builtins

import pytest

import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.extraction.schema import ExtractionResult, ExtractedBlock
from opensearch_pipeline.extraction.unified_extractor import (
    BASE_EXTRACTOR_DEPS,
    UnifiedExtractor,
    preflight_extractor_deps,
)


# ── 防线 1a：preflight_extractor_deps 纯函数 ─────────────────────────────────

def _fake_import(missing_modules):
    def _imp(name):
        if name in missing_modules:
            raise ImportError(f"No module named '{name}'")
        return object()
    return _imp


def test_preflight_reports_missing_modules_only_for_base_extractor_exts():
    missing = preflight_extractor_deps(
        ["XLSX", ".pptx", "docx", "pdf", "txt", "", None],
        _import=_fake_import({"openpyxl", "pptx"}))
    # 只有映射表内且真缺的类型上报；pdf/txt/空值一概不管
    assert [(ext, mod) for ext, mod, _pip in missing] == [
        ("pptx", "pptx"), ("xlsx", "openpyxl")]
    # pip 包名与 import 名不同的（python-pptx）必须给对提示
    assert {mod: pip for _e, mod, pip in missing}["pptx"] == "python-pptx"


def test_preflight_ok_when_all_importable():
    exts = list(BASE_EXTRACTOR_DEPS) + ["pdf", "md"]
    assert preflight_extractor_deps(exts, _import=_fake_import(set())) == []


# ── 防线 1b：node_extract_text_with_ocr fail-fast ────────────────────────────

def _preflight_missing_if_any(exts, _import=None):
    """有真实任务类型 → 报缺 openpyxl；空集合（纯 mock 批）→ 全可用。"""
    return [("xlsx", "openpyxl", "openpyxl")] if set(exts) else []


def test_extract_node_fails_fast_before_any_extraction(monkeypatch):
    import opensearch_pipeline.extraction.unified_extractor as ue
    monkeypatch.delenv("RAG_EXTRACT_DEP_PREFLIGHT", raising=False)
    monkeypatch.setattr(ue, "preflight_extractor_deps", _preflight_missing_if_any)
    ctx = {"tasks": [{"doc_id": "d1", "version_no": 1, "file_ext": "xlsx",
                      "raw_key": "raw/prod/表.xlsx"}],
           "simulate": True}
    with pytest.raises(RuntimeError, match=r"ENV-DEP.*openpyxl"):
        pn.node_extract_text_with_ocr(ctx)
    # fail-fast 语义：raise 发生在抽取器构造/任何抽取之前，节点零产出
    assert "extractions" not in ctx


def test_extract_node_skips_preflight_for_mock_tasks(monkeypatch):
    import opensearch_pipeline.extraction.unified_extractor as ue
    monkeypatch.delenv("RAG_EXTRACT_DEP_PREFLIGHT", raising=False)
    seen = []

    def _spy(exts, _import=None):
        seen.append(set(exts))
        return _preflight_missing_if_any(exts)

    monkeypatch.setattr(ue, "preflight_extractor_deps", _spy)
    ctx = {"tasks": [{"doc_id": "d1", "version_no": 1, "file_ext": "xlsx",
                      "raw_key": "raw/prod/表.xlsx", "mock_text": "# 标题\n正文内容"}],
           "simulate": True}
    pn.node_extract_text_with_ocr(ctx)   # mock 任务不走真实抽取器 → 不因"缺依赖"拦截
    assert seen == [set()], "mock_text 任务不得参与预检"
    assert len(ctx["extractions"]) == 1
    assert ctx["extractions"][0].text_length > 0


def test_extract_node_preflight_optout_env(monkeypatch):
    import opensearch_pipeline.extraction.unified_extractor as ue
    monkeypatch.setenv("RAG_EXTRACT_DEP_PREFLIGHT", "off")

    def _boom(exts, _import=None):
        raise AssertionError("RAG_EXTRACT_DEP_PREFLIGHT=off 时预检不得执行")

    monkeypatch.setattr(ue, "preflight_extractor_deps", _boom)
    ctx = {"tasks": [{"doc_id": "d1", "version_no": 1, "file_ext": "xlsx",
                      "raw_key": "raw/prod/表.xlsx", "mock_text": "内容"}],
           "simulate": True}
    pn.node_extract_text_with_ocr(ctx)
    assert len(ctx["extractions"]) == 1


# ── 抽取器告警文案契约：守卫判据依赖 "No module named" 真实出现 ────────────────

def test_extract_xlsx_warning_carries_no_module_named_when_openpyxl_missing(
        monkeypatch, tmp_path):
    """poison import 复现现场：缺 openpyxl 时 _extract_xlsx 吞错产出的 warning 必须
    携带 'No module named'（守卫的匹配词）且 blocks==0——防止未来改写告警文案让守卫失明。"""
    import opensearch_pipeline.extraction.image_extraction_utils as ieu
    import opensearch_pipeline.extraction.xlsx_classifier  # noqa: F401  # 预热，避开 poison
    monkeypatch.setattr(ieu, "extract_images_from_xlsx", lambda *a, **kw: [])

    real_import = builtins.__import__

    def _poison(name, *args, **kwargs):
        if name == "openpyxl":
            raise ModuleNotFoundError("No module named 'openpyxl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _poison)

    ex = object.__new__(UnifiedExtractor)   # 绕开 __init__（无需 OCR/OSS 客户端）
    ex.simulate = True
    f = tmp_path / "空表.xlsx"
    f.write_bytes(b"PK\x03\x04dummy")
    res = ex._extract_xlsx({"doc_id": "D", "version_no": 1, "raw_key": "raw/x/空表.xlsx",
                            "filename": "空表.xlsx", "local_path": str(f),
                            "_tmp_dir": str(tmp_path)})
    assert not res.blocks and res.text_length == 0
    assert any("No module named 'openpyxl'" in w for w in res.warnings), res.warnings


# ── 防线 2：node_build_canonical ENV-DEP 守卫 ────────────────────────────────

class _CaptureCursor:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.append((sql, params))

    def executemany(self, sql, rows):
        self.store.append((sql, rows))

    def close(self):
        pass


class _CaptureConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _CaptureCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _mk_result(**kw):
    base = dict(doc_id="DOC_ENVDEP", version_no=1, source_key="raw/prod/表.xlsx",
                file_ext="xlsx", extract_method="openpyxl", title="表",
                text="", text_length=0, blocks=[], warnings=[])
    base.update(kw)
    return ExtractionResult(**base)


def _canon_ctx(monkeypatch, tmp_path, extractions, simulate_db=False):
    """simulate OSS（本地落盘，chdir 进 tmp 防污染）+ capture DB。"""
    monkeypatch.chdir(tmp_path)
    store = []
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _CaptureConn(store))
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (None, True))
    ctx = {"extractions": extractions, "simulate_db": simulate_db,
           "simulate_oss": True, "simulate_opensearch": True}
    return ctx, store


def test_empty_canonical_with_missing_module_marks_failed_and_raises(monkeypatch, tmp_path):
    res = _mk_result(
        warnings=["Failed to extract Excel file: No module named 'openpyxl'"])
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [res])
    with pytest.raises(RuntimeError, match=r"ENV-DEP.*DOC_ENVDEP"):
        pn.node_build_canonical(ctx)
    joined = " ".join(s for s, _p in store if isinstance(s, str))
    assert "extraction_status='FAILED'" in joined
    # 空壳绝不写 canonical keys / COMPLETED：canonical_json_key 保持 NULL → stage-2 不认领
    assert "canonical_json_key" not in joined
    assert "COMPLETED" not in joined
    failed_params = [p for s, p in store if isinstance(s, str) and "FAILED" in s]
    assert failed_params and "No module named 'openpyxl'" in str(failed_params[0])
    assert ctx["canonicals"] == []


def test_empty_canonical_docx_not_installed_phrasing_also_caught(monkeypatch, tmp_path):
    """docx 缺依赖走 docx_extractor 自定义文案（"python-docx not installed"，
    不含 "No module named"）——守卫必须两种文案都认。"""
    res = _mk_result(doc_id="DOC_DOCX", file_ext="docx", extract_method="python_docx",
                     warnings=["python-docx not installed, cannot extract DOCX"])
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [res])
    with pytest.raises(RuntimeError, match=r"ENV-DEP.*DOC_DOCX"):
        pn.node_build_canonical(ctx)
    joined = " ".join(s for s, _p in store if isinstance(s, str))
    assert "extraction_status='FAILED'" in joined
    assert ctx["canonicals"] == []


def test_empty_canonical_without_module_warning_keeps_success(monkeypatch, tmp_path):
    """内容合法为空（无缺模块警告）→ 原路径不变：COMPLETED，交给 stage-2 SKIPPED_EMPTY。"""
    res = _mk_result(warnings=["some benign note"])
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [res])
    pn.node_build_canonical(ctx)   # 不 raise
    joined = " ".join(s for s, _p in store if isinstance(s, str))
    assert "extraction_status = 'COMPLETED'" in joined
    assert "extraction_status='FAILED'" not in joined
    assert len(ctx["canonicals"]) == 1


def test_partial_text_with_aux_module_warning_stays_success(monkeypatch, tmp_path):
    """辅助依赖缺失（如 PIL）但文本抽取成功（blocks>0）：优雅降级铁律保持，绝不误标 FAILED。"""
    blk = ExtractedBlock(block_type="paragraph", text="正文内容", page_num=1,
                         source="openpyxl")
    res = _mk_result(text="正文内容", text_length=4, blocks=[blk],
                     warnings=["image helper degraded: No module named 'PIL'"])
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [res])
    pn.node_build_canonical(ctx)   # 不 raise
    joined = " ".join(s for s, _p in store if isinstance(s, str))
    assert "extraction_status = 'COMPLETED'" in joined
    assert "extraction_status='FAILED'" not in joined
    assert len(ctx["canonicals"]) == 1


def test_mixed_batch_healthy_doc_still_completes_then_raises(monkeypatch, tmp_path):
    """混合批：健康文档照常落库（canonical+COMPLETED），环境失败文档标 FAILED，收尾统一炸红。"""
    ok_blk = ExtractedBlock(block_type="paragraph", text="正常内容", page_num=1,
                            source="markdown")
    ok = _mk_result(doc_id="DOC_OK", file_ext="txt", extract_method="markdown",
                    text="正常内容", text_length=4, blocks=[ok_blk])
    bad = _mk_result(doc_id="DOC_BAD", file_ext="pptx", extract_method="python_pptx",
                     warnings=["Failed to extract PPTX text: No module named 'pptx'"])
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [ok, bad])
    with pytest.raises(RuntimeError, match=r"DOC_BAD"):
        pn.node_build_canonical(ctx)
    assert [c["doc_id"] for c in ctx["canonicals"]] == ["DOC_OK"]
    joined = " ".join(s for s, _p in store if isinstance(s, str))
    assert "extraction_status = 'COMPLETED'" in joined     # DOC_OK 照常收尾
    assert "extraction_status='FAILED'" in joined          # DOC_BAD 留痕
    failed_params = [p for s, p in store
                     if isinstance(s, str) and "extraction_status='FAILED'" in s]
    assert failed_params and failed_params[0][-2:] == ("DOC_BAD", 1)


def test_env_dep_guard_raises_in_simulate_db_without_db_write(monkeypatch, tmp_path):
    """simulate_db 下守卫仍要炸（可在本地/CI 复现），但绝不碰 DB。"""
    res = _mk_result(
        warnings=["Failed to extract Excel file: No module named 'openpyxl'"])
    monkeypatch.chdir(tmp_path)

    def _no_db(**kw):
        raise AssertionError("simulate_db 下不得开 DB 连接")

    monkeypatch.setattr(pn, "_get_db_conn", _no_db)
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (None, True))
    ctx = {"extractions": [res], "simulate": True}
    with pytest.raises(RuntimeError, match=r"ENV-DEP"):
        pn.node_build_canonical(ctx)
    assert ctx["canonicals"] == []

# ── COST-DEFER 守卫（与 ENV-DEP 同型；ultra P1 纠偏 2026-07-17）─────────────────
# 瞬态共享预算（RUN/DAILY）耗尽被成本闸拒绝的文档（cost_deferred）本 run 不定稿：
# 不写 canonical keys（stage-1 谓词 keys IS NULL → 下一 run 自动重捡重过闸），
# extraction_status='FAILED' + [COST-DEFER] 留痕，循环后统一 raise 炸红运行。


def test_cost_deferred_doc_withheld_marks_failed_and_raises(monkeypatch, tmp_path):
    res = _mk_result(doc_id="DOC_DEFER", text="正文内容", text_length=4, cost_deferred=True)
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [res])
    with pytest.raises(RuntimeError, match=r"COST-DEFER.*DOC_DEFER"):
        pn.node_build_canonical(ctx)
    joined = " ".join(s for s, _p in store if isinstance(s, str))
    assert "extraction_status='FAILED'" in joined
    # 顺延文档绝不写 canonical keys / COMPLETED：keys 保持 NULL → 下一 run stage-1 重捡
    assert "canonical_json_key" not in joined
    assert "COMPLETED" not in joined
    failed_params = [p for s, p in store if isinstance(s, str) and "FAILED" in s]
    assert failed_params and "COST-DEFER" in str(failed_params[0])
    assert ctx["canonicals"] == []


def test_cost_deferred_doc_does_not_wedge_healthy_sibling(monkeypatch, tmp_path):
    """整批不被顺延文档楔死：健康文档照常定稿（COMPLETED+keys），随后统一 raise 让运行可见。"""
    res_defer = _mk_result(doc_id="DOC_DEFER", text="正文", text_length=2, cost_deferred=True)
    res_ok = _mk_result(doc_id="DOC_OK", text="健康文档正文", text_length=6,
                        warnings=["some benign note"])
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [res_defer, res_ok])
    with pytest.raises(RuntimeError, match=r"COST-DEFER"):
        pn.node_build_canonical(ctx)
    assert [c["doc_id"] for c in ctx["canonicals"]] == ["DOC_OK"]
    ok_sqls = " ".join(s for s, p in store
                       if isinstance(s, str) and p and "DOC_OK" in str(p))
    assert "COMPLETED" in ok_sqls, "健康文档必须照常 COMPLETED 落库"
