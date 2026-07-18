# -*- coding: utf-8 -*-
"""P2-32：VLM degraded 兜底必须传播到文档级状态（供应商恢复后可定位重扫）。

链条：image_funnel_processor 的 degraded 兜底 → unified_extractor 逐 asset 透传 +
文档级 vlm_degraded_count 聚合 → canonical JSON（DAG1→DAG2 边界）→
node_write_chunk_meta 收尾 content_process_status='NEEDS_REVIEW'（不默默 DONE）。
取舍：chunk/嵌入/索引照常（graceful degradation 铁律——辅助失败绝不断答案），
NEEDS_REVIEW 只把文档留在可复查集合、无自动重试循环。
"""
from types import SimpleNamespace

import opensearch_pipeline.pipeline_nodes as pn
from opensearch_pipeline.extraction.schema import ExtractionResult
from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor


# ── unified_extractor：逐 asset 透传 + 文档级聚合 ─────────────────────────────

def _mk_asset(tmp_path, name, content):
    p = tmp_path / name
    p.write_bytes(content)
    return SimpleNamespace(local_path=str(p), original_name=name,
                           page_num=1, image_index=0)


def _mk_extractor():
    ex = object.__new__(UnifiedExtractor)   # 绕开 __init__（不需要 OCR/OSS 客户端）
    ex.simulate = True                       # simulate → 不触碰持久缓存落盘
    return ex


def _patch_funnel(monkeypatch, results_by_name):
    monkeypatch.setattr(ImageFunnelProcessor, "__init__",
                        lambda self, simulate=False: None)
    monkeypatch.setattr(ImageFunnelProcessor, "_static_heuristics",
                        lambda self, p: (200, 200, 50.0))

    def _process(self, local_path, doc_id, is_public=True, doc_title=""):
        import os
        res = results_by_name[os.path.basename(local_path)]
        if isinstance(res, Exception):
            raise res
        return res

    monkeypatch.setattr(ImageFunnelProcessor, "process_image", _process)
    monkeypatch.setattr(UnifiedExtractor, "_load_vlm_cache", classmethod(lambda cls: {}))
    monkeypatch.setattr(UnifiedExtractor, "_save_vlm_cache",
                        classmethod(lambda cls, force_oss=False: None))


def test_degraded_funnel_result_marks_asset_and_doc_count(monkeypatch, tmp_path):
    a = _mk_asset(tmp_path, "a.png", b"img-a")
    b = _mk_asset(tmp_path, "b.png", b"img-b")
    _patch_funnel(monkeypatch, {
        "a.png": {"status": "ROUTE_TO_VECTOR", "visual_summary": "[VLM 降级] 图片资产 a.png。",
                  "image_category": "unknown", "ocr_text": "", "degraded": True},
        "b.png": {"status": "ROUTE_TO_VECTOR", "visual_summary": "正常图注",
                  "image_category": "step_screenshot", "ocr_text": ""},
    })
    ex = _mk_extractor()
    assets, _blocks, degraded = ex._process_embedded_images(
        [a, b], {"doc_id": "D", "raw_key": "raw/x.docx"})
    assert degraded == 1
    by_name = {x["filename"]: x for x in assets}
    assert by_name["a.png"].get("degraded") is True
    assert "degraded" not in by_name["b.png"], "非降级 asset 不落键（不膨胀 canonical）"


def test_funnel_exception_counts_as_degraded(monkeypatch, tmp_path):
    a = _mk_asset(tmp_path, "a.png", b"img-a")
    b = _mk_asset(tmp_path, "b.png", b"img-b")
    _patch_funnel(monkeypatch, {
        "a.png": RuntimeError("OCR vendor 5xx"),   # 整张图丢结论 = 同属供应商降级
        "b.png": {"status": "ROUTE_TO_VECTOR", "visual_summary": "ok",
                  "image_category": "step_screenshot", "ocr_text": ""},
    })
    ex = _mk_extractor()
    assets, _blocks, degraded = ex._process_embedded_images(
        [a, b], {"doc_id": "D", "raw_key": "raw/x.docx"})
    assert degraded == 1
    assert [x["filename"] for x in assets] == ["b.png"]


def test_extraction_result_serializes_degraded_count():
    r = ExtractionResult(doc_id="d", version_no=1, source_key="raw/a.pdf", file_ext="pdf",
                         extract_method="pypdf", title="t", text="x", text_length=1,
                         vlm_degraded_count=3)
    assert r.to_dict()["vlm_degraded_count"] == 3
    # 默认 0（存量 canonical / mock 路径零行为变化）
    assert ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="txt",
                            extract_method="markdown", title="", text="", text_length=0
                            ).to_dict()["vlm_degraded_count"] == 0


def test_degraded_flag_survives_stage_boundaries():
    """DAG1 canonical 构建 + orchestrator stage-2 重载清单都必须携带 vlm_degraded_count——
    任一边界丢键，degraded 标志静默蒸发、文档照样 INDEXED 终态（P2-32 的原始缺陷）。"""
    import inspect
    from opensearch_pipeline import dataworks_orchestrator as dw
    assert "vlm_degraded_count" in inspect.getsource(pn.node_build_canonical)
    assert "vlm_degraded_count" in inspect.getsource(dw)


# ── node_write_chunk_meta：收尾 NEEDS_REVIEW（不默默 DONE）──────────────────

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


def _run_closure(monkeypatch, vlm_degraded_count, partial_notes=None):
    from opensearch_pipeline.chunker import Chunk
    store = []
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _CaptureConn(store))
    import opensearch_pipeline.env_guard as eg
    monkeypatch.setattr(eg, "assert_destructive_write_allowed", lambda *a, **k: None)
    chunk = Chunk(chunk_id="d_v1_c0000", doc_id="d", version_no=1, chunk_index=0,
                  chunk_type="text_chunk", chunk_text="正文内容足够长以通过校验阈值的一段文字",
                  token_count=12, permission_level="internal")
    canon = {"doc_id": "d", "version_no": 1, "rag_ready_key": "processing/x"}
    if vlm_degraded_count:
        canon["vlm_degraded_count"] = vlm_degraded_count
    if partial_notes:
        canon["partial_loss_notes"] = list(partial_notes)
    ctx = {"valid_chunks": [chunk], "canonicals": [canon], "simulate_db": False}
    pn.node_write_chunk_meta(ctx)
    return store


def test_write_chunk_meta_marks_needs_review_on_vlm_degraded(monkeypatch):
    store = _run_closure(monkeypatch, vlm_degraded_count=2)
    sqls = [(s, p) for s, p in store if isinstance(s, str) and s.strip().startswith("UPDATE")]
    joined = " ".join(s for s, _ in sqls)
    # chunk/索引照常：DONE 收尾仍发生（graceful degradation，文本照常可检索）
    assert "content_process_status = 'DONE'" in joined
    # 但随后必须改写 NEEDS_REVIEW + vlm_degraded 附注（供应商恢复后可定位重扫）
    assert "NEEDS_REVIEW" in joined
    note_params = [p for s, p in sqls if "NEEDS_REVIEW" in s]
    assert note_params and "vlm_degraded: 2" in str(note_params[0])


def test_write_chunk_meta_stays_done_without_degraded(monkeypatch):
    store = _run_closure(monkeypatch, vlm_degraded_count=0)
    joined = " ".join(s for s, _ in store if isinstance(s, str))
    assert "content_process_status = 'DONE'" in joined
    assert "NEEDS_REVIEW" not in joined, "无降级图片的文档绝不误标 NEEDS_REVIEW"


# ── 批次6（ultra 评审）：partial_loss_notes 走同一 NEEDS_REVIEW 通道 ───────────


def test_write_chunk_meta_marks_needs_review_on_partial_loss(monkeypatch):
    """OCR 部分页失败/XLSX/PPTX 中途异常的留痕（partial_loss_notes）→ 收尾 NEEDS_REVIEW，
    「有产出但不完整」绝不静默定稿 DONE（重灌自愈通道与 vlm_degraded 同型）。"""
    store = _run_closure(monkeypatch, vlm_degraded_count=0,
                         partial_notes=["ocr_partial: 2/10 page(s) failed (pages [3, 7])"])
    sqls = [(s, p) for s, p in store if isinstance(s, str) and s.strip().startswith("UPDATE")]
    joined = " ".join(s for s, _ in sqls)
    assert "content_process_status = 'DONE'" in joined      # 可服务：DONE 收尾仍发生
    assert "NEEDS_REVIEW" in joined
    note_params = [p for s, p in sqls if "NEEDS_REVIEW" in s]
    assert note_params and "ocr_partial" in str(note_params[0])


def test_partial_loss_notes_survive_stage_boundaries():
    """canonical 构建与 orchestrator stage-2 重载都必须携带 partial_loss_notes——
    任一边界丢键，留痕静默蒸发、文档照样 DONE 终态（与 P2-32 同类缺陷形态）。"""
    import inspect

    from opensearch_pipeline import dataworks_orchestrator as dw
    from opensearch_pipeline.extraction.schema import ExtractionResult
    assert "partial_loss_notes" in inspect.getsource(pn.node_build_canonical)
    assert "partial_loss_notes" in inspect.getsource(dw)
    r = ExtractionResult(doc_id="d", version_no=1, source_key="", file_ext="xlsx",
                         extract_method="openpyxl", title="", text="t", text_length=1,
                         partial_loss_notes=["xlsx_partial: boom"])
    assert r.to_dict()["partial_loss_notes"] == ["xlsx_partial: boom"]
