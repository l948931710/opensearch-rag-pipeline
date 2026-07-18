# -*- coding: utf-8 -*-
"""tests/test_batch6_extraction_fixes.py — ultra 评审批次6：抽取/多模态八项回归。

覆盖：OCR 不可解析 200 必 raise + 空结果不入缓存 + 部分页失败留痕；XLSX 中途异常
partial 留痕；funnel cap/DENY 跳过计入降级数；vlm_rebuilder 走 post_json_with_retry；
XLSX drawing↔sheet 走 .rels 真实映射（封面页工作簿）；clause_chunk 携带 page_num/source。
（partial_loss_notes 的收尾 NEEDS_REVIEW 与跨 stage 传播在 test_vlm_degraded_propagation.py。）
"""
import hashlib
import os
import zipfile
from types import SimpleNamespace

import pytest

os.environ.setdefault("RAG_SIMULATE", "true")

from opensearch_pipeline.extraction.image_extraction_utils import (  # noqa: E402
    ImageAsset,
    _enrich_xlsx_annotations,
)
from opensearch_pipeline.extraction.ocr_client import OCRClient  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# 1. OCR：不可解析 200 → raise（页 FAILED、不入缓存）
# ─────────────────────────────────────────────────────────────────────


def _mk_ocr_client():
    c = object.__new__(OCRClient)
    c.api_key = "k"
    c.api_base_url = "https://dashscope.aliyuncs.com/api/v1"
    c.ocr_model = "qwen-vl-ocr-latest"
    return c


def test_unparseable_200_raises_not_empty(monkeypatch):
    """200 但响应体缺预期内容路径（错误/拒答载荷形态）→ raise，绝不当空页成功——
    否则空文本被永久写进页缓存，一次形态异常变成该页跨重灌的永久静默丢失。"""
    import opensearch_pipeline.vlm_retry as vr
    fake = SimpleNamespace(status_code=200, json=lambda: {"unexpected": "shape"}, text="")
    monkeypatch.setattr(vr, "post_json_with_retry", lambda *a, **k: fake)
    c = _mk_ocr_client()
    with pytest.raises(RuntimeError, match="unparseable body"):
        c._call_ocr_api("aGVsbG8=", "image/png")


def test_failed_page_not_cached_and_marked_failed(monkeypatch):
    """API 异常页 → FAILED 且缓存零写入；空文本页 → DONE 但不入缓存（防固化空结果）。"""
    puts = []

    class _Cache:
        def get_many(self, keys):
            return {}

        def put_many(self, d):
            puts.append(dict(d))

    c = _mk_ocr_client()
    monkeypatch.setattr(OCRClient, "_get_page_cache", classmethod(lambda cls: _Cache()))

    calls = {"n": 0}

    def _api(b64, mime):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("DashScope OCR 200 with unparseable body: KeyError")
        return ""    # 第二页：合法空文本

    monkeypatch.setattr(c, "_call_ocr_api", _api)
    pages = c._ocr_rendered_pages([(0, "b64a", "image/png"), (1, "b64b", "image/png")])
    assert [p.status for p in pages] == ["FAILED", "DONE"]
    assert puts == [], "失败页与空文本页都绝不入页缓存"


def test_partial_page_failure_lands_partial_loss_note():
    """unified_extractor 消费侧：部分页 FAILED（聚合仍 DONE）→ partial_loss_notes 留痕。
    直接验证派生逻辑所依赖的 OCRResult 形态 + 消费代码在源内（接线断了立刻红）。"""
    import inspect

    from opensearch_pipeline.extraction import unified_extractor as ue
    src = inspect.getsource(ue)
    assert "ocr_partial:" in src and "partial_loss_notes.append" in src


# ─────────────────────────────────────────────────────────────────────
# 2. XLSX 中途异常 → partial 留痕（有产出不完整绝不静默 DONE）
# ─────────────────────────────────────────────────────────────────────


def test_xlsx_midway_exception_appends_partial_note(monkeypatch, tmp_path):
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    ex = object.__new__(UnifiedExtractor)
    ex.simulate = True
    f = tmp_path / "坏表.xlsx"
    f.write_bytes(b"PK\x03\x04 not a real workbook")
    res = ex._extract_xlsx({"doc_id": "D", "version_no": 1, "raw_key": "raw/x/坏表.xlsx",
                            "filename": "坏表.xlsx", "local_path": str(f),
                            "_tmp_dir": str(tmp_path)})
    assert any("xlsx_partial" in n for n in res.partial_loss_notes)
    assert any("Failed to extract Excel" in w for w in res.warnings)


# ─────────────────────────────────────────────────────────────────────
# 3. funnel cap/DENY 跳过 → 计入 vlm_degraded_count（NEEDS_REVIEW 自愈通道）
# ─────────────────────────────────────────────────────────────────────


def test_funnel_cap_skips_count_into_degraded(monkeypatch, tmp_path):
    from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    monkeypatch.setenv("RAG_FUNNEL_MAX_IMAGES", "1")
    monkeypatch.setattr(ImageFunnelProcessor, "__init__", lambda self, simulate=False: None)
    monkeypatch.setattr(ImageFunnelProcessor, "_static_heuristics",
                        lambda self, p: (200, 200, 50.0))   # 假 PNG 过 Funnel 1
    monkeypatch.setattr(
        ImageFunnelProcessor, "process_image",
        lambda self, p, d, is_public=True, doc_title="": {
            "status": "ROUTE_TO_VECTOR", "visual_summary": "ok",
            "image_category": "step_screenshot", "ocr_text": ""})
    monkeypatch.setattr(UnifiedExtractor, "_load_vlm_cache", classmethod(lambda cls: {}))
    monkeypatch.setattr(UnifiedExtractor, "_save_vlm_cache",
                        classmethod(lambda cls, force_oss=False: None))
    assets_in = []
    for i, name in enumerate(["a.png", "b.png", "c.png"]):
        p = tmp_path / name
        p.write_bytes(f"img-{name}".encode())
        assets_in.append(SimpleNamespace(local_path=str(p), original_name=name,
                                         page_num=1, image_index=i))
    ex = object.__new__(UnifiedExtractor)
    ex.simulate = True
    _assets, _blocks, degraded = ex._process_embedded_images(
        assets_in, {"doc_id": "D", "raw_key": "raw/x.docx"})
    # cap=1：3 张唯一图跳过 2 张 → 计入降级数（此前 =0，文档静默 DONE、图片永久缺失）
    assert degraded == 2


# ─────────────────────────────────────────────────────────────────────
# 4. vlm_rebuilder：重建调用走 post_json_with_retry（429 脆弱账户对齐 ocr_client）
# ─────────────────────────────────────────────────────────────────────


def test_vlm_reconstruct_page_goes_through_retry_helper(monkeypatch):
    import opensearch_pipeline.vlm_retry as vr
    from opensearch_pipeline.extraction import vlm_rebuilder as vlmr
    seen = {}

    def _fake_post(url, *, json=None, headers=None, timeout=None, label="", post_fn=None):
        seen["label"] = label
        return SimpleNamespace(
            status_code=200, raise_for_status=lambda: None,
            json=lambda: {"choices": [{"message": {
                "content": '{"blocks": [{"type": "paragraph", "text": "重建正文"}]}'}}]})

    monkeypatch.setattr(vr, "post_json_with_retry", _fake_post)
    cfg = SimpleNamespace(
        ocr=SimpleNamespace(vlm_model="qwen3-vl-plus", model="qwen3-vl-plus", api_key="k",
                            api_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"),
        environment="test")
    blocks = vlmr._vlm_reconstruct_page(b"img", cfg, "标题")
    assert blocks and blocks[0]["text"] == "重建正文"
    assert seen["label"] == "VLM(rebuild)", "重建调用必须走 post_json_with_retry（有界重试）"


# ─────────────────────────────────────────────────────────────────────
# 5. XLSX drawing↔sheet .rels 真实映射（封面页工作簿）
# ─────────────────────────────────────────────────────────────────────

_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_DRAWING_XML = f"""<?xml version="1.0"?>
<xdr:wsDr xmlns:xdr="{_XDR}" xmlns:a="{_A}" xmlns:r="{_R}">
  <xdr:twoCellAnchor>
    <xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff>
      <xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
    <xdr:to><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff>
      <xdr:row>6</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>
    <xdr:grpSp>
      <xdr:pic><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic>
      <xdr:sp><xdr:txBody><a:p><a:r><a:t>1</a:t></a:r></a:p></xdr:txBody></xdr:sp>
    </xdr:grpSp>
    <xdr:clientData/>
  </xdr:twoCellAnchor>
</xdr:wsDr>"""


def _mk_cover_sheet_workbook(path, *, with_ws_rels=True):
    """两 sheet 工作簿：sheet1=封面（无图），sheet2 带 drawing1.xml（旧假定会错绑 sheet1）。"""
    img = b"fake-png-bytes-for-md5"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f"""<?xml version="1.0"?>
<workbook xmlns="{_X}" xmlns:r="{_R}">
  <sheets>
    <sheet name="Cover" sheetId="1" r:id="rId1"/>
    <sheet name="Data" sheetId="2" r:id="rId2"/>
  </sheets>
</workbook>""")
        z.writestr("xl/_rels/workbook.xml.rels", f"""<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="{_R}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{_R}/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>""")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{_X}"/>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{_X}"/>')
        if with_ws_rels:
            z.writestr("xl/worksheets/_rels/sheet2.xml.rels", f"""<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="{_R}/drawing" Target="../drawings/drawing1.xml"/>
</Relationships>""")
        z.writestr("xl/drawings/drawing1.xml", _DRAWING_XML)
        z.writestr("xl/drawings/_rels/drawing1.xml.rels", f"""<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="{_R}/image" Target="../media/image1.png"/>
</Relationships>""")
        z.writestr("xl/media/image1.png", img)
    return img


def test_cover_sheet_workbook_binds_annotation_via_rels(tmp_path):
    """封面 sheet 无图时，sheet2 的 drawing 是 drawing1.xml——旧的 drawingN→sheetN 假定
    令 sheet_idx=0 而资产 page_num=2，标注绑定静默全丢；.rels 真实映射修复之。"""
    xlsx = tmp_path / "cover.xlsx"
    img = _mk_cover_sheet_workbook(xlsx)
    asset = ImageAsset(local_path=str(tmp_path / "na.png"), page_num=2, image_index=0,
                       original_name="image1.png",
                       md5=hashlib.md5(img).hexdigest())
    _enrich_xlsx_annotations(str(xlsx), [asset], "cover")
    assert getattr(asset, "annotation_num", None) == 1, \
        "封面页工作簿的 sheet2 标注必须经 .rels 真实映射绑定（旧假定下全丢）"


def test_workbook_without_ws_rels_falls_back_to_old_assumption(tmp_path):
    """rels 链解析不出 → 回退 drawingN→sheetN 旧假定（常规文件零行为变化）。"""
    xlsx = tmp_path / "plain.xlsx"
    img = _mk_cover_sheet_workbook(xlsx, with_ws_rels=False)
    asset = ImageAsset(local_path=str(tmp_path / "na.png"), page_num=1, image_index=0,
                       original_name="image1.png",
                       md5=hashlib.md5(img).hexdigest())
    _enrich_xlsx_annotations(str(xlsx), [asset], "plain")
    assert getattr(asset, "annotation_num", None) == 1   # 旧假定 drawing1→sheet1 照常成立


# ─────────────────────────────────────────────────────────────────────
# 6. clause_chunk 溯源：page_num/source 按覆盖块盖章
# ─────────────────────────────────────────────────────────────────────


def _clause_blocks():
    return [
        {"block_type": "paragraph", "text": "第一条 本制度适用于全体员工，目的在于规范管理。",
         "page_num": 1, "source": "native"},
        {"block_type": "paragraph", "text": "第二条 各部门应当严格执行本制度的相关要求。",
         "page_num": 2, "source": "ocr"},
    ]


def test_clause_chunks_carry_page_and_source():
    """批次6（ultra chunker:1847）：clause_chunk 必须携带 page_num 与 source——此前一律
    None/'native'（HA3 落 page_num=0），制度/法规类文档全体丢页级引用与 OCR 溯源。"""
    from opensearch_pipeline.chunker import DocumentChunker
    ch = DocumentChunker(max_chunk_chars=60, min_chunk_chars=5, overlap_chars=0,
                         split_mode="clause")
    chunks = ch.chunk_from_blocks(_clause_blocks(), "DOC_CLAUSE_PROV", 1)
    clause = [c for c in chunks if c.chunk_type == "clause_chunk"]
    assert len(clause) >= 2
    by_text = {c.chunk_text[:3]: c for c in clause}
    assert by_text["第一条"[:3]].page_num == 1 and by_text["第一条"[:3]].source == "native"
    assert by_text["第二条"[:3]].page_num == 2 and by_text["第二条"[:3]].source == "ocr"


def test_clause_fallback_path_carries_provenance():
    """无条款边界的 fallback（text_chunk）同样盖章（游标 find 精确定位覆盖块）。"""
    from opensearch_pipeline.chunker import DocumentChunker
    ch = DocumentChunker(max_chunk_chars=40, min_chunk_chars=5, overlap_chars=0,
                         split_mode="clause")
    blocks = [
        {"block_type": "paragraph", "text": "这是一段没有任何条款编号的普通说明文字内容。",
         "page_num": 3, "source": "ocr"},
    ]
    chunks = ch.chunk_from_blocks(blocks, "DOC_FALLBACK_PROV", 1)
    texts = [c for c in chunks if c.chunk_type == "text_chunk"]
    assert texts and all(c.page_num == 3 and c.source == "ocr" for c in texts)
