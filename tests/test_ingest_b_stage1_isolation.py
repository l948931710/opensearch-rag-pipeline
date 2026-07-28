# -*- coding: utf-8 -*-
"""B2（2026-07-25）：stage-1 单篇失败隔离 —— 一篇炸掉不再丢弃同批已付费的抽取成果。

缺陷：抽取是**全批先跑完**、canonical 再逐篇定稿。此前
  · 串行路径单篇 raise 直接冒泡；
  · 并发路径 `list(pool.map(...))` 一篇异常炸掉整个返回列表；
  · 定稿阶段（canonical OSS 上传 / RDS 写）单篇 raise 同样打断整批。
后果：第 k 篇一失败，k+1..N 篇**已经付过费**的 OCR/VLM 成果连同定稿一并丢弃，下一轮从头重付。

修复口径（单层归因）：抽取层只产出 `(doc_id, version_no, error)` **不落库**，
canonical 层统一落库 + 聚合 raise。非文档级异常（抽取器构造、tmp、cache finalize）仍整批 fail-fast。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import pytest

import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.extraction.schema import ExtractedBlock, ExtractionResult


def _mk_result(doc_id, version_no=1, text="正文内容"):
    return ExtractionResult(
        doc_id=doc_id, version_no=version_no, source_key=f"raw/x/{doc_id}.pdf",
        file_ext="pdf", extract_method="native", title=doc_id,
        text=text, text_length=len(text),
        blocks=[ExtractedBlock(block_type="paragraph", text=text, page_num=1, source="native")],
        warnings=[])


class _CaptureCursor:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.append((sql, params))

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


# ═══════════════════ 抽取层：单篇异常不炸整批 ═══════════════════

class _FakeExtractor:
    """第 k 篇抛异常的替身抽取器。"""

    def __init__(self, boom_docs, *a, **kw):
        self.boom_docs = boom_docs
        self.cost_breaker = None

    def extract(self, task):
        if task["doc_id"] in self.boom_docs:
            raise RuntimeError("parser exploded")
        return _mk_result(task["doc_id"], task.get("version_no", 1))

    @classmethod
    def flush_vlm_cache_to_oss(cls):
        return None


def _run_extract(monkeypatch, tmp_path, boom_docs, concurrency="1"):
    import opensearch_pipeline.extraction as extraction_pkg
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAG_EXTRACT_CONCURRENCY", concurrency)
    monkeypatch.setattr(extraction_pkg, "UnifiedExtractor",
                        lambda *a, **kw: _FakeExtractor(boom_docs))
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (None, True))
    ctx = {"tasks": [{"doc_id": f"DOC_{i}", "version_no": 1, "file_ext": "pdf",
                      "raw_key": f"raw/x/DOC_{i}.pdf", "filename": f"DOC_{i}.pdf"}
                     for i in range(4)],
           "simulate_oss": True, "simulate_api": True, "simulate_db": True}
    pn.node_extract_text_with_ocr(ctx)
    return ctx


@pytest.mark.parametrize("concurrency", ["1", "3"])
def test_single_doc_extraction_failure_keeps_the_rest(monkeypatch, tmp_path, concurrency):
    """串行与并发两条路径都必须保住同批其余文档（并发路径此前是 list(pool.map) 全丢）。"""
    ctx = _run_extract(monkeypatch, tmp_path, {"DOC_1"}, concurrency)
    assert [r.doc_id for r in ctx["extractions"]] == ["DOC_0", "DOC_2", "DOC_3"]
    assert ("DOC_1", 1) in ctx["_extract_errors"]
    assert "RuntimeError" in ctx["_extract_errors"][("DOC_1", 1)]


def test_extraction_layer_does_not_write_db(monkeypatch, tmp_path):
    """单层归因：抽取层只记账不落库（否则两层各标一次 FAILED、各计一次数）。"""
    def _no_db(**kw):
        raise AssertionError("抽取层不得落库")

    monkeypatch.setattr(pn, "_get_db_conn", _no_db)
    ctx = _run_extract(monkeypatch, tmp_path, {"DOC_0"})
    assert ctx["_extract_errors"]


def test_extract_guard_only_wraps_per_document():
    """非文档级异常（抽取器构造 / tmp / cache finalize）仍必须整批 fail-fast。"""
    src = inspect.getsource(pn.node_extract_text_with_ocr)
    guard = src[src.index("def _extract_guarded"):src.index("def _extract_guarded") + 900]
    assert "return _extract_one(task, task_tmp_dir)" in guard, "守卫只能包单篇抽取调用"
    assert "UnifiedExtractor(" not in guard and "flush_vlm_cache_to_oss" not in guard


# ═══════════════════ canonical 层：统一落库 + 聚合 raise ═══════════════════

def _canon_ctx(monkeypatch, tmp_path, extractions, extract_errors=None):
    monkeypatch.chdir(tmp_path)
    store = []
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _CaptureConn(store))
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (None, True))
    ctx = {"extractions": extractions, "simulate_db": False,
           "simulate_oss": True, "simulate_opensearch": True}
    if extract_errors:
        ctx["_extract_errors"] = extract_errors
    return ctx, store


def test_extract_errors_are_persisted_by_canonical_layer(monkeypatch, tmp_path):
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [_mk_result("DOC_OK")],
                            extract_errors={("DOC_BAD", 2): "RuntimeError: parser exploded"})
    with pytest.raises(RuntimeError, match=r"STAGE1-PARTIAL"):
        pn.node_build_canonical(ctx)
    joined = " ".join(s for s, _p in store if isinstance(s, str))
    assert "extraction_status='FAILED'" in joined
    failed = [p for s, p in store if isinstance(s, str) and "extraction_status='FAILED'" in s]
    assert failed and "[EXTRACT-FAILED]" in str(failed[0])
    assert failed[0][-2:] == ("DOC_BAD", 2)
    # 健康文档照常定稿，绝不能被一起丢
    assert [c["doc_id"] for c in ctx["canonicals"]] == ["DOC_OK"]
    assert "extraction_status = 'COMPLETED'" in joined


def test_finalize_failure_isolates_and_keeps_paid_work(monkeypatch, tmp_path):
    """第 k 篇定稿失败不得丢弃 k+1..N 篇已付费的抽取成果。"""
    monkeypatch.chdir(tmp_path)
    store = []
    calls = {"n": 0}

    class _FlakyConn(_CaptureConn):
        def cursor(self):
            calls["n"] += 1
            if calls["n"] == 1:      # 第一篇的 RDS 定稿写炸
                raise RuntimeError("rds write exploded")
            return _CaptureCursor(self.store)

    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _FlakyConn(store))
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda ctx: (None, True))
    ctx = {"extractions": [_mk_result("DOC_A"), _mk_result("DOC_B")],
           "simulate_db": False, "simulate_oss": True, "simulate_opensearch": True}
    with pytest.raises(RuntimeError, match=r"STAGE1-PARTIAL"):
        pn.node_build_canonical(ctx)
    assert [c["doc_id"] for c in ctx["canonicals"]] == ["DOC_B"], \
        "第二篇必须照常定稿（此前第一篇 raise 会把它一起丢）"
    failed = [p for s, p in store if isinstance(s, str) and "extraction_status='FAILED'" in s]
    assert failed and "[CANONICAL-FINALIZE]" in str(failed[0])


def test_all_four_stage1_guards_share_one_closure():
    """OSS-FETCH / ENV-DEP / COST-DEFER / B2 共用同一收口（keys 留 NULL → 下轮重捡）。"""
    src = inspect.getsource(pn.node_build_canonical)
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert code.count("_mark_extraction_failed(") >= 4
    assert "extraction_status='FAILED'" not in code, "守卫不应再各自内联 DB 写"
    # 只看 SQL 字面量：docstring 里会引用 canonical_json_key 解释重捡谓词
    helper = inspect.getsource(pn._mark_extraction_failed)
    sql_part = helper[helper.index("UPDATE document_version"):]
    assert "canonical_json_key" not in sql_part, "标失败绝不能写 canonical keys"


def test_healthy_batch_raises_nothing(monkeypatch, tmp_path):
    ctx, store = _canon_ctx(monkeypatch, tmp_path, [_mk_result("DOC_OK")])
    pn.node_build_canonical(ctx)      # 不得抛
    assert len(ctx["canonicals"]) == 1
