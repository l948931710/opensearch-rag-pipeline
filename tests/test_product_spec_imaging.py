# -*- coding: utf-8 -*-
"""test_product_spec_imaging.py — product_spec_instruction 图片渲染 + card 类型映射回归。

两个曾静默退化的缺口（2026-07-03 修复）：
  (1) product_photo 卡发出 chunk_type="image"，但 serving 渲染 image chunk 只认
      chunk 级 source_image（content_blocks_builder / to_ha3_doc）——绑定阶段原先
      只写 extra["image_refs"]（且 img_entry 缺 source_image/visual_summary/
      ocr_text 契约键），兜底 image chunk 环节又按 layout_bound_fns 跳过这些
      文件名 → 规格书产品图永远渲染不出。修复 = img_entry 补全契约键 +
      首图提升为 chunk 级 source_image（与 PPTX slide 路径同法）。
  (2) spec_micro_card（微生物指标）不在 card→通用类型映射里：微生物 section
      末位/独立时非标 chunk_type 直接泄漏进 RDS/HA3（微生物→理化顺序时被
      consecutive-merge 保留末段类型所掩盖）。修复 = 映射补 "table_chunk"。
"""

from opensearch_pipeline.chunker import DocumentChunker
from opensearch_pipeline.content_blocks_builder import renderable_image_refs
from opensearch_pipeline.pipeline_nodes import node_chunk_documents


# ── fixtures ─────────────────────────────────────────────────

def _spec_blocks(rows):
    return [{"block_type": "paragraph", "text": t, "page_num": 1,
             "section_path": None, "source": "native",
             "extra": {"row_num": r}} for r, t in rows]


_FULL_SPEC_ROWS = [
    (1, "富岭塑胶产品规格书（成品）"),
    (3, "一、物料基本信息"),
    (4, "物料名称：PET一次性透明吸管"),
    (5, "品牌名称：富岭"),
    (8, "技术标准要求"),
    (9, "微生物指标"),
    (10, "菌落总数 ≤100 CFU/g，大肠菌群不得检出，霉菌酵母 ≤50 CFU/g"),
    (13, "物料图片"),
    (14, "产品正反面图片、外箱图片见下方粘贴区域"),
]


def _photo_asset(**overrides):
    asset = {
        "filename": "img0001.png", "status": "ROUTE_TO_VECTOR",
        "oss_key": "processing/assets/quality/SPEC01/v1/img0001.png",
        "anchor_row": 14, "image_index": 1, "original_index": 1,
        "image_category": "product_photo",
        "visual_summary": "PET吸管产品正面照片，白色背景平铺拍摄",
        "ocr_text": "",
        "page_num": 1, "width": 800, "height": 600, "file_size_kb": 100.0,
    }
    asset.update(overrides)
    return asset


def _spec_doc(assets):
    rows_text = "\n".join(t for _, t in _FULL_SPEC_ROWS)
    return {
        "doc_id": "SPEC01", "version_no": 1,
        "title": "PET吸管产品规格书",
        "filename": "PET吸管产品规格书.xlsx", "file_ext": "xlsx",
        # DAG1 持久化判定（P0-3）：直接消费，绕开 filename 重分类
        "xlsx_layout_type": "product_spec_instruction",
        "category_l1": "product_spec", "category_l2": "spec",
        "text": rows_text,
        "blocks": _spec_blocks(_FULL_SPEC_ROWS),
        "assets": assets,
        "source_key": "raw/quality/PET吸管产品规格书.xlsx", "canonical_key": "",
        "owner_dept": "quality", "permission_level": "public",
        "kb_type": "public", "risk_level": "low", "redaction_action": "CLEAN",
    }


def _run(doc):
    ctx = {"canonicals": [doc], "split_mode": "dynamic",
           "prepend_title": True, "prepend_section": True}
    node_chunk_documents(ctx)
    return ctx["chunks"]


# ── (1) product_photo 图片渲染链 ─────────────────────────────

def test_product_photo_chunk_carries_source_image_and_renders():
    """产品图 chunk 必须带 chunk 级 source_image，且经 to_ha3_doc → serving 渲染
    单一权威 renderable_image_refs 可出图（修复前该链条全程断裂 = 永不出图）。"""
    chunks = _run(_spec_doc([_photo_asset()]))
    imgs = [c for c in chunks if c.chunk_type == "image"]
    assert len(imgs) == 1, "product_photo section 应产出唯一 image chunk（兜底环节不得重复建卡）"
    img = imgs[0]
    assert img.extra.get("spec_section") == "product_photo"

    # img_entry 必须携带完整 image_refs 契约键（与 step_card/slide 路径同构）
    refs = img.extra.get("image_refs") or []
    assert refs, "image chunk 必须携带 image_refs"
    for key in ("filename", "oss_key", "source_image", "visual_summary",
                "ocr_text", "image_index", "anchor_row", "image_category"):
        assert key in refs[0], f"image_refs 契约键缺失: {key}"
    assert refs[0]["oss_key"] == "processing/assets/quality/SPEC01/v1/img0001.png"
    assert refs[0]["source_image"] == refs[0]["oss_key"]
    assert refs[0]["image_index"] == 1

    # 首图提升为 chunk 级 source_image（serving image 分支 + to_ha3_doc 只认它）
    assert img.extra.get("source_image") == refs[0]["oss_key"]
    assert img.extra.get("visual_summary") == "PET吸管产品正面照片，白色背景平铺拍摄"

    # 端到端：HA3 文档形态 → serving 渲染权威函数必须能出图
    ha3_doc = img.to_ha3_doc()
    rendered = renderable_image_refs(ha3_doc)
    assert rendered, "serving 渲染链出图失败：image chunk 无可渲染图"
    assert rendered[0]["source_image"] == "processing/assets/quality/SPEC01/v1/img0001.png"
    assert rendered[0]["visual_summary"] == "PET吸管产品正面照片，白色背景平铺拍摄"


def test_product_photo_constructed_oss_key_fallback():
    """asset 无回填 oss_key（离线/旧数据）→ 按 dept/doc/version 构造路径兜底。"""
    chunks = _run(_spec_doc([_photo_asset(oss_key=None)]))
    img = [c for c in chunks if c.chunk_type == "image"][0]
    expected = "processing/assets/quality/SPEC01/v1/img0001.png"
    assert img.extra["image_refs"][0]["oss_key"] == expected
    assert img.extra["source_image"] == expected


# ── (2) spec_micro_card 类型映射 ─────────────────────────────

def test_spec_micro_standalone_emits_table_chunk():
    """微生物指标 section 末位/独立时不得泄漏非标 chunk_type spec_micro_card
    （微生物→理化顺序会被 consecutive-merge 保留末段类型所掩盖，独立时必现）。"""
    rows = [
        (3, "技术标准要求"),
        (4, "微生物指标"),
        (5, "菌落总数 ≤100 CFU/g，大肠菌群不得检出，霉菌酵母 ≤50 CFU/g"),
    ]
    chunker = DocumentChunker(max_chunk_chars=400, min_chunk_chars=1, overlap_chars=0,
                              xlsx_layout_type="product_spec_instruction")
    chunks = chunker._chunk_product_spec(_spec_blocks(rows), "SPEC02", 1, {"title": "规格书"})
    micro = [c for c in chunks if c.extra.get("spec_card_type") == "spec_micro_card"]
    assert micro, "微生物 section 应产出 chunk"
    assert micro[0].chunk_type == "table_chunk"
    assert not [c for c in chunks if c.chunk_type == "spec_micro_card"], \
        "非标 chunk_type spec_micro_card 泄漏进下游"


def test_no_spec_card_type_escapes_generic_mapping():
    """完整性闸门：_chunk_product_spec 可能发出的每个 card 类型（section 模式表 +
    子分区表 + 默认起始 header）都必须在 _SPEC_CARD_TO_GENERIC 有映射，且映射值
    只落在下游认识的通用类型集合内 —— 新增 section 忘配映射时此测试先红。"""
    emitted = {ct for _, _, ct in DocumentChunker._SPEC_SECTION_PATTERNS}
    emitted |= {ct for _, _, ct in DocumentChunker._SPEC_SUB_SECTIONS}
    emitted.add("spec_header_card")  # _chunk_product_spec 的默认起始 section
    mapping = DocumentChunker._SPEC_CARD_TO_GENERIC
    missing = emitted - set(mapping)
    assert not missing, f"card 类型缺映射（会把非标 chunk_type 泄漏进 RDS/HA3）: {sorted(missing)}"
    assert set(mapping.values()) <= {"image", "table_chunk", "text_chunk"}, \
        f"映射出现下游不认识的类型: {sorted(set(mapping.values()))}"
