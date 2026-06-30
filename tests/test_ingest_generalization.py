# -*- coding: utf-8 -*-
"""摄取准入策略 + 独立图片文档泛化 —— 2026-06-10 OSS 真实分布盘点的回归测试。

盘点背景（raw/ 共 3644 对象）：jpg×1404（绝大多数在 _quarantine/_archive）、
doc×438（基本全在 _archive，docx 转换件在活跃目录）、~$ 临时文件与 Thumbs.db
曾被注册进 document_meta；活跃语料中独立图片（申岗.jpg、磨床操作流程.png 等）
是真实的员工告示/SOP 海报。
"""

import pytest

from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
from opensearch_pipeline.ingest_policy import should_ingest_raw_key


# ═══════════════ ingest 准入策略 ═══════════════

def test_active_documents_ingested():
    for key in (
        "raw/admin/员工手册.docx",
        "raw/production_injection/FL-ZS-WI-010《注塑销售出库单》-成品仓管.pdf",
        "raw/hr/考勤管理.xlsx",
        "raw/marketing/产品介绍.pptx",
        "raw/admin/申岗.jpg",
        "raw/production_mold/磨床操作流程.png",
        "raw/admin/充值饭卡时间.jpeg",
    ):
        ok, reason = should_ingest_raw_key(key)
        assert ok, f"{key} 应纳入，被拒原因: {reason}"


def test_archive_and_quarantine_excluded():
    for key in (
        "raw/_archive/admin/A1员工行为管理标准.doc",
        "raw/admin/_archive/旧版制度.docx",
        "raw/marketing/_quarantine/FULING PPT 英文版本.pptx",
        "raw/_quarantine/x.pdf",
    ):
        ok, _ = should_ingest_raw_key(key)
        assert not ok, f"{key} 应被排除"


def test_junk_and_temp_files_excluded():
    for key in (
        "raw/hr/~$1内部审计控制-改进制度和流程 .doc",
        "raw/production/Thumbs.db",
        "raw/marketing/Desktop.ini",
        "raw/admin/",                      # 目录
        "raw/admin/无扩展名文件",
    ):
        ok, _ = should_ingest_raw_key(key)
        assert not ok, f"{key} 应被排除"


def test_media_archives_and_legacy_excluded():
    """用户决策：mp4/压缩包不进知识库；doc/xls/ppt 走一次性转换后回灌。"""
    for key in (
        "raw/marketing/培训.mp4",
        "raw/admin/打包.zip",
        "raw/it/备份.rar",
        "raw/admin/A1员工行为管理标准.doc",
        "raw/production_paper_cup/纸杯过程自检作业指导书.xls",
        "raw/marketing/培训.ppt",
    ):
        ok, _ = should_ingest_raw_key(key)
        assert not ok, f"{key} 应被排除"


# ═══════════════ 独立图片文档 ═══════════════

def _fake_funnel(monkeypatch, funnel_result):
    class _FakeProcessor:
        def __init__(self, simulate=True):
            pass

        def _static_heuristics(self, local_path):
            # 让嵌入图路径的 Funnel-1 预过滤恒通过（真实现读文件尺寸/大小）
            return 200, 120, 10.0

        def process_image(self, local_path, doc_id, is_public=True, doc_title=""):
            return dict(funnel_result)

    import opensearch_pipeline.image_funnel_processor as ifp
    monkeypatch.setattr(ifp, "ImageFunnelProcessor", _FakeProcessor)


def _extract_image_doc(tmp_path):
    img = tmp_path / "磨床操作流程.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n_fake")
    return UnifiedExtractor(simulate=True).extract({
        "doc_id": "IMG01", "version_no": 1, "local_path": str(img),
        "file_ext": "png", "filename": img.name,
        "raw_key": f"raw/production_mold/{img.name}", "_tmp_dir": str(tmp_path),
    })


def test_route_to_text_image_doc_keeps_renderable_ref(monkeypatch, tmp_path):
    """ROUTE_TO_TEXT 的图片文档：OCR 文本之外必须保留 image_ref —— 图就是文档本体，
    否则 SOP 海报原图永远无法在回答里渲染（2026-06-10 实测磨床操作流程.png）。"""
    _fake_funnel(monkeypatch, {
        "status": "ROUTE_TO_TEXT",
        "ocr_text": "磨床操作规程 一、基本操作规程 机床开动前必须检查",
        "visual_summary": "磨床操作规程文档页面照片",
        "image_category": "step_screenshot",
        "vlm_annotation_map": {}, "reason": "",
        "width": 800, "height": 1200, "file_size_kb": 120.0,
    })
    r = _extract_image_doc(tmp_path)
    block_types = [b.block_type for b in r.blocks]
    assert "ocr_text" in block_types
    assert "image_ref" in block_types, "原图引用丢失：ROUTE_TO_TEXT 图片文档必须附 image_ref"
    assert r.assets and r.assets[0].get("image_index") == 0
    assert r.assets[0].get("page_num") == 1


def test_route_to_vector_image_doc_has_asset(monkeypatch, tmp_path):
    _fake_funnel(monkeypatch, {
        "status": "ROUTE_TO_VECTOR",
        "ocr_text": "",
        "visual_summary": "粉色背景告示牌，标明充值时间",
        "image_category": "notice_photo",
        "vlm_annotation_map": {}, "reason": "",
        "width": 800, "height": 600, "file_size_kb": 80.0,
    })
    r = _extract_image_doc(tmp_path)
    assert r.assets and r.assets[0]["status"] == "ROUTE_TO_VECTOR"
    assert r.assets[0].get("image_index") == 0


def test_tif_gif_routed_to_image_extractor(monkeypatch, tmp_path):
    """tif/gif 也走图片文档路径（语料里存在零星 tif/gif）。"""
    _fake_funnel(monkeypatch, {
        "status": "ROUTE_TO_VECTOR", "ocr_text": "", "visual_summary": "产品图",
        "image_category": "product_photo", "vlm_annotation_map": {}, "reason": "",
        "width": 100, "height": 100, "file_size_kb": 10.0,
    })
    for ext in ("tif", "gif"):
        f = tmp_path / f"样品.{ext}"
        f.write_bytes(b"fake")
        r = UnifiedExtractor(simulate=True).extract({
            "doc_id": "IMG02", "version_no": 1, "local_path": str(f),
            "file_ext": ext, "filename": f.name,
            "raw_key": f"raw/marketing/{f.name}", "_tmp_dir": str(tmp_path),
        })
        assert r.extract_method == "image_funnel", f".{ext} 应走图片文档路径"


def test_xls_explicitly_unsupported():
    """.xls 按用户决策不在管线内支持（转换后回灌），且必须显式可见而非静默空文档。"""
    r = UnifiedExtractor(simulate=True).extract({
        "doc_id": "XLS01", "version_no": 1, "local_path": "/nonexistent/a.xls",
        "file_ext": "xls", "filename": "a.xls", "raw_key": "raw/admin/a.xls",
    })
    assert r.extract_method.startswith("unsupported")


def test_image_doc_asset_carries_raw_key_as_oss_key(monkeypatch, tmp_path):
    """独立图片文档的 asset.oss_key = raw/ 对象本身 —— 资产上传只覆盖 ROUTE_TO_VECTOR，
    构造出的 processing/assets/ 路径对 ROUTE_TO_TEXT 永不存在（serving 403 死图）。"""
    _fake_funnel(monkeypatch, {
        "status": "ROUTE_TO_TEXT",
        "ocr_text": "磨床操作规程 一、基本操作规程 机床开动前必须检查设备状态并确认无误",
        "visual_summary": "磨床操作规程文档页面照片",
        "image_category": "step_screenshot",
        "vlm_annotation_map": {}, "reason": "",
        "width": 800, "height": 1200, "file_size_kb": 120.0,
    })
    r = _extract_image_doc(tmp_path)
    assert r.assets[0].get("oss_key") == "raw/production_mold/磨床操作流程.png"


def test_faq_eligible_does_not_hijack_step_routing():
    """faq_eligible 是"可生成 FAQ"的下游标记，不是切块结构信号：带步骤标记的 SOP
    即使 faq_eligible=True 也必须走 step 模式（2026-06-10 本地 E2E：123/124 SOP
    曾被劫持进 faq 模式，全批次 0 绑定）。"""
    from opensearch_pipeline.pipeline_nodes import node_chunk_documents

    steps = [
        "步骤1：整理《注塑发货拖柜》中的《发货单》，核对发货数量与单据是否一致无误。",
        "步骤2：进入电脑桌面登录U8系统，双击U8快捷方式图标输入用户名和密码进行登录。",
        "步骤3：按照系统路径点击进入销售出库单界面，录入出库日期、仓库编号与货位数据。",
        "步骤4：确认实际发货数量后依次点击保存、审核、打印，将打印好的单子按联次分发。",
    ]
    doc = {
        "doc_id": "HJACK01", "version_no": 1,
        "title": "FL-ZS-WI-010《注塑销售出库单》-成品仓管.docx",
        "filename": "x.docx", "file_ext": "docx",
        "category_l1": "sop", "category_l2": "business_sop",
        "faq_eligible": True,            # ← 真实 LLM 分类常给 SOP 标 True
        "text": "\n".join(steps),
        "blocks": [{"block_type": "paragraph", "text": s, "page_num": None,
                    "section_path": None, "source": "native", "extra": {}} for s in steps],
        "assets": [],
        "source_key": "raw/production/x.docx", "canonical_key": "",
        "owner_dept": "production", "permission_level": "public",
        "kb_type": "public", "risk_level": "low", "redaction_action": "CLEAN",
    }
    ctx = {"canonicals": [doc], "split_mode": "dynamic",
           "prepend_title": True, "prepend_section": True}
    node_chunk_documents(ctx)
    types = {getattr(c, "chunk_type", "") for c in ctx["chunks"]}
    assert "step_card" in types, f"SOP 被 faq_eligible 劫持出 step 模式: {types}"


def _routing_doc(doc_id, title, cat_l1, cat_l2, steps):
    return {
        "doc_id": doc_id, "version_no": 1, "title": title,
        "filename": "x.docx", "file_ext": "docx",
        "category_l1": cat_l1, "category_l2": cat_l2,
        "text": "\n".join(steps),
        "blocks": [{"block_type": "paragraph", "text": s, "page_num": None,
                    "section_path": None, "source": "native", "extra": {}} for s in steps],
        "assets": [], "source_key": "raw/production/x.docx", "canonical_key": "",
        "owner_dept": "production", "permission_level": "public",
        "kb_type": "public", "risk_level": "low", "redaction_action": "CLEAN",
    }


def test_step_rich_regulation_upgrades_clause_to_step():
    """B1-3：操作规程/检验规程类（cat=standard 或标题含'规范'）先落 clause，但带真实步骤标记的
    文档应升级到 step 模式（此前 step 检测只在 m_mode=='text' 时跑 → 这些文档永远拿不到 step_card）。"""
    from opensearch_pipeline.pipeline_nodes import node_chunk_documents
    steps = [
        "步骤1：进入U8系统登录注塑检验模块，双击图标输入用户名与密码完成登录操作。",
        "步骤2：按系统路径点击进入检验录入界面，录入检验批次、产品编号与检验项目数据。",
        "步骤3：核对实测值与判定标准是否一致无误后，依次点击保存、审核并打印检验报告。",
    ]
    doc = _routing_doc("REG_STEP01", "注塑成型操作规范.docx", "standard", "operation_std", steps)
    ctx = {"canonicals": [doc], "split_mode": "dynamic",
           "prepend_title": True, "prepend_section": True}
    node_chunk_documents(ctx)
    types = {getattr(c, "chunk_type", "") for c in ctx["chunks"]}
    assert "step_card" in types, f"step-rich 操作规范 未升级到 step 模式: {types}"


def test_pure_policy_stays_clause_not_step():
    """B1-3 守卫：纯制度/规定政策文档（无 sop 关键词、条款式正文）不得被误升到 step 模式。"""
    from opensearch_pipeline.pipeline_nodes import node_chunk_documents
    clauses = [
        "第一条 为规范公司员工考勤管理，维护正常工作秩序，特制定本制度。",
        "第二条 员工应严格遵守上下班时间，不得迟到、早退或无故旷工。",
        "第三条 因公外出须提前向部门负责人报备，并在考勤系统中登记。",
    ]
    doc = _routing_doc("POL01", "员工考勤管理制度.docx", "policy", "hr_policy", clauses)
    ctx = {"canonicals": [doc], "split_mode": "dynamic",
           "prepend_title": True, "prepend_section": True}
    node_chunk_documents(ctx)
    types = {getattr(c, "chunk_type", "") for c in ctx["chunks"]}
    assert "step_card" not in types, f"纯制度被误升到 step 模式: {types}"


def test_true_faq_doc_still_routes_faq():
    """真 FAQ 文档（标题/分类含 faq）仍走 faq 模式。"""
    from opensearch_pipeline.pipeline_nodes import node_chunk_documents

    qa_text = ("问：员工饭卡怎么充值？\n答：充值时间为每周一、周三及周五中午10:30-12:00，"
               "在行政楼一楼前台办理，补卡同时间办理，需当天下午上班后领取新卡。\n\n"
               "问：新员工宿舍怎么申请？\n答：持入职单到行政部办理宿舍入住手续，"
               "由宿管分配床位并领取门禁卡，退宿时需结清水电费用。")
    doc = {
        "doc_id": "FAQDOC01", "version_no": 1, "title": "人事行政常见问题FAQ.docx",
        "filename": "faq.docx", "file_ext": "docx",
        "category_l1": "faq", "category_l2": "hr_faq", "faq_eligible": True,
        "text": qa_text, "blocks": [], "assets": [],
        "source_key": "raw/hr/faq.docx", "canonical_key": "",
        "owner_dept": "hr", "permission_level": "public",
        "kb_type": "public", "risk_level": "low", "redaction_action": "CLEAN",
    }
    ctx = {"canonicals": [doc], "split_mode": "dynamic",
           "prepend_title": True, "prepend_section": True}
    node_chunk_documents(ctx)
    assert ctx["chunks"], "FAQ 文档应产出 chunks"
    assert not any(getattr(c, "chunk_type", "") == "step_card" for c in ctx["chunks"])


def test_contribution_md_pinned_to_faq_by_classify():
    """知识贡献合成的 contribution-<cid>.md：classify 阶段直接 pin 成 faq 类目（跳过 LLM、
    不被默认 reference 覆盖），从而走 FAQ 分块。"""
    import os as _os
    if _os.environ.get("RAG_SIMULATE", "").lower() not in ("1", "true", "yes"):
        pytest.skip("需 RAG_SIMULATE=true")
    from opensearch_pipeline.pipeline_nodes import node_classify_and_risk_assess
    doc = {
        "doc_id": "CONTRIBFAQ1", "version_no": 1, "title": "员工饭卡怎么充值？",
        "filename": "contribution-01J0.md", "file_ext": "md",
        "text": "问：员工饭卡怎么充值？\n\n答：每周一三五 10:30-12:00 在行政楼一楼前台办理。",
        "blocks": [], "assets": [],
        "source_key": "raw/hr/internal/CONTRIBFAQ1/UP1/contribution-01J0.md", "canonical_key": "",
        "owner_dept": "hr", "permission_level": "dept_internal",
        "kb_type": "private", "risk_level": "low",
    }
    ctx = {"canonicals": [doc], "simulate": True}
    node_classify_and_risk_assess(ctx)
    assert doc["category_l1"] == "faq"      # pin 生效（不走 LLM、不落 reference）
    assert doc["category_l2"] == "qa"
    assert doc["faq_eligible"] is True


def test_contribution_faq_format_yields_faq_chunk():
    """synthesize_markdown 的 问：/答： 段落经 FAQ 分块 → faq_chunk，问题进 chunk 文本、答案聚合。"""
    from opensearch_pipeline.pipeline_nodes import node_chunk_documents
    q = "员工饭卡怎么充值？"
    a = "每周一三五中午 10:30-12:00 在行政楼一楼前台办理，补卡同时间办理需下午领新卡。"
    blocks = [
        {"block_type": "paragraph", "text": f"问：{q}", "page_num": None,
         "section_path": None, "source": "native", "extra": {}},
        {"block_type": "paragraph", "text": f"答：{a}", "page_num": None,
         "section_path": None, "source": "native", "extra": {}},
    ]
    doc = {
        "doc_id": "CONTRIBFAQ2", "version_no": 1, "title": q,
        "filename": "contribution-02.md", "file_ext": "md",
        "category_l1": "faq", "category_l2": "qa", "faq_eligible": True,
        "text": f"问：{q}\n\n答：{a}", "blocks": blocks, "assets": [],
        "source_key": "raw/hr/internal/CONTRIBFAQ2/UP1/contribution-02.md", "canonical_key": "",
        "owner_dept": "hr", "permission_level": "dept_internal",
        "kb_type": "private", "risk_level": "low", "redaction_action": "CLEAN",
    }
    ctx = {"canonicals": [doc], "split_mode": "dynamic",
           "prepend_title": True, "prepend_section": True}
    node_chunk_documents(ctx)
    faq_chunks = [c for c in ctx["chunks"] if getattr(c, "chunk_type", "") == "faq_chunk"]
    assert faq_chunks, f"应产出 faq_chunk，实得: {[getattr(c, 'chunk_type', '') for c in ctx['chunks']]}"
    txt = faq_chunks[0].chunk_text
    assert q in txt and a in txt           # 问题进 chunk 文本（检索命中问句）+ 答案聚合


# ═══════════════ XLSX TO_TEXT 截图的 serving 可渲染性 ═══════════════
# 真实失败件：raw/it/外贸发票操作流程.xlsx —— 3 个 sheet 各 1 张全屏 U8 截图、无文本
# 单元格，VLM 全部路由 ROUTE_TO_TEXT。OCR 文本进了 chunk，但原图没有任何 serving 可达
# 载体（refs 被启发式注入落在 ocr_chunk 上，HA3 不携带、RDS 恢复只覆盖
# step_card/procedure_parent/visual_knowledge）→ 覆盖评测 I5 违例（2026-06-10）。

_I5_SERVING_REF_TYPES = {"step_card", "procedure_parent", "visual_knowledge"}


def _serving_renderable_fns(chunks):
    """收集已被 serving 可达载体携带的图片文件名（与 eval_extraction_coverage I5 同口径）。"""
    import os as _os
    fns = set()
    for c in chunks:
        cx = getattr(c, "extra", {}) or {}
        ctype = getattr(c, "chunk_type", "")
        if ctype in ("image", "visual_knowledge") and cx.get("source_image"):
            fns.add(_os.path.basename(str(cx["source_image"])))
        if ctype in _I5_SERVING_REF_TYPES:
            for ref in (cx.get("image_refs") or []):
                fn = ref.get("filename") or _os.path.basename(
                    str(ref.get("source_image") or ref.get("oss_key") or ""))
                if fn:
                    fns.add(fn)
    return fns


def _chunk_doc(doc):
    from opensearch_pipeline.pipeline_nodes import node_chunk_documents
    ctx = {"canonicals": [doc], "split_mode": "dynamic",
           "prepend_title": True, "prepend_section": True}
    node_chunk_documents(ctx)
    return ctx["chunks"]


def _xlsx_doc(filename, blocks, assets, text=""):
    return {
        "doc_id": "XLSXIMG1", "version_no": 1, "title": filename,
        "filename": filename, "file_ext": "xlsx", "text": text,
        "blocks": blocks, "assets": assets,
        "source_key": f"raw/it/{filename}", "canonical_key": "",
        "owner_dept": "it", "category_l1": "", "category_l2": "",
        "permission_level": "public", "kb_type": "public", "risk_level": "low",
        "redaction_action": "CLEAN",
    }


def _totext_asset(idx, filename, visual_summary, ocr_text, anchor_row, figure_no=None,
                  status="ROUTE_TO_TEXT"):
    a = {
        "filename": filename, "local_path": f"/nonexistent/{filename}",
        "page_num": 1, "image_index": idx, "original_index": idx,
        "status": status, "width": 1900, "height": 1000, "file_size_kb": 300.0,
        "ocr_text": ocr_text, "visual_summary": visual_summary,
        "image_category": "step_screenshot", "vlm_annotation_map": {}, "reason": "",
        "anchor_row": anchor_row,
    }
    if figure_no is not None:
        a["figure_no"] = figure_no
    return a


def test_xlsx_totext_flow_doc_gets_fallback_image_chunks():
    """外贸发票操作流程.xlsx 形态复刻：全屏 TO_TEXT 截图 + 标题含"流程" + OCR 文本带
    步骤标记 → step-detect 误路由 step 模式、refs 落在 ocr_chunk 上不可达。
    兜底必须为每张截图建 chunk_type=image 的独立 chunk（chunk 级 source_image 经
    to_ha3_doc 进 HA3 才 serving 可渲染）。"""
    ocr1 = ("第一步 打开U8系统外贸专用档案界面录入物料的存货编码与HS编码并保存审核。\n"
            "第二步 进入销售普通发票界面点击生成特殊发票按钮完成外销发票开具流程。")
    ocr2 = "单证打印窗口 勾选报关合同与财务发票选项 点击确认输出打印外贸单证资料"
    blocks = [
        {"block_type": "heading", "text": "Sheet1", "page_num": 1,
         "section_path": "Sheet1", "source": "openpyxl",
         "extra": {"section_type": "cleaning_items"}},
        {"block_type": "ocr_text", "text": ocr1, "page_num": 1, "source": "ocr",
         "extra": {"source_image": "流程_sheet0_img0000.png"}},
        {"block_type": "ocr_text", "text": ocr2, "page_num": 1, "source": "ocr",
         "extra": {"source_image": "流程_sheet0_img0001.png"}},
    ]
    assets = [
        _totext_asset(0, "流程_sheet0_img0000.png",
                      "U8系统外贸发票界面截图，显示销售类型为外销", ocr1, anchor_row=1),
        # 第二张故意无 caption —— chunk_text 必须回退 OCR 片段，不能是空描述
        _totext_asset(1, "流程_sheet0_img0001.png", "", ocr2, anchor_row=33),
    ]
    doc = _xlsx_doc("外贸发票操作流程.xlsx", blocks, assets, text=ocr1 + "\n" + ocr2)
    chunks = _chunk_doc(doc)

    img_chunks = [c for c in chunks if c.chunk_type == "image"]
    assert len(img_chunks) == 2, (
        f"TO_TEXT 截图应各得一个兜底 image chunk，实际 {len(img_chunks)}")
    for c in img_chunks:
        assert c.extra.get("source_image"), "image chunk 必须带 chunk 级 source_image"
        assert "processing/assets/it/XLSXIMG1/v1/" in c.extra["source_image"]
    rendered = _serving_renderable_fns(chunks)
    for a in assets:
        assert a["filename"] in rendered, f"{a['filename']} 提取了但 serving 不可渲染（I5）"
    # caption 缺失的截图：描述回退 OCR 片段
    c2 = [c for c in img_chunks if "img0001" in c.extra["source_image"]][0]
    assert "单证打印" in c2.chunk_text, f"空 caption 未回退 OCR 片段: {c2.chunk_text!r}"


def test_xlsx_procedure_totext_bound_to_step_cards():
    """procedure_image_guide 版式：TO_TEXT 截图必须与 TO_VECTOR 一样按
    figure_no/anchor 绑进 step_card（refs 经 RDS image_refs_json 恢复，serving 可达），
    且不再重复建独立 image chunk。"""
    steps = [
        "1\t整理注塑发货拖柜中的发货单，核对发货数量与单据是否一致无误。",
        "2\t登录U8系统进入销售出库单界面，录入出库日期仓库编号与货位数据。",
        "3\t确认实际发货数量后依次点击保存审核打印，将单子按联次分发存档。",
    ]
    blocks = [{"block_type": "heading", "text": "Sheet1", "page_num": 1,
               "section_path": "Sheet1", "source": "openpyxl",
               "extra": {"section_type": "cleaning_items"}}]
    blocks += [{"block_type": "paragraph", "text": s, "page_num": 1, "source": "openpyxl",
                "extra": {"step_no": i + 1, "row_role": "step",
                          "row_num": 10 + i * 10, "sheet_idx": 0}}
               for i, s in enumerate(steps)]
    assets = [
        _totext_asset(0, "wi_img0000.png", "", "", anchor_row=12, figure_no="图1",
                      status="ROUTE_TO_VECTOR"),
        _totext_asset(1, "wi_img0001.png", "", "", anchor_row=22, figure_no="图2"),
        # 无图号、无内容匹配 → priority-2 兜到仍无图的步骤
        _totext_asset(2, "wi_img0002.png", "", "", anchor_row=32),
    ]
    doc = _xlsx_doc("成品包装作业指导书.xlsx", blocks, assets, text="\n".join(steps))
    chunks = _chunk_doc(doc)

    step_cards = {c.extra.get("step_no"): c for c in chunks if c.chunk_type == "step_card"}
    assert set(step_cards) == {1, 2, 3}, f"应产出 3 个 step_card: {sorted(step_cards)}"

    refs1 = step_cards[1].extra.get("image_refs") or []
    assert [r["filename"] for r in refs1] == ["wi_img0000.png"], "VECTOR 图1→步骤1 绑定回归"
    refs2 = step_cards[2].extra.get("image_refs") or []
    assert [r["filename"] for r in refs2] == ["wi_img0001.png"], (
        f"TO_TEXT 截图未按图号绑进 step_card: {refs2}")
    refs3 = step_cards[3].extra.get("image_refs") or []
    assert [r["filename"] for r in refs3] == ["wi_img0002.png"], (
        f"无图号 TO_TEXT 截图未被 priority-2 兜底绑定: {refs3}")
    # image_refs 契约键（CLAUDE.md：extractor → chunker → builder → 卡片全链路依赖）
    for key in ("oss_key", "source_image", "visual_summary", "ocr_text"):
        assert key in refs2[0], f"image_refs 契约键缺失: {key}"
    assert refs2[0]["oss_key"].startswith("processing/assets/it/XLSXIMG1/v1/")
    # 已绑定 → 不再重复建独立 image chunk
    assert not [c for c in chunks if c.chunk_type == "image"], "绑定后不应再建独立 image chunk"
    assert {a["filename"] for a in assets} <= _serving_renderable_fns(chunks)


def test_xlsx_procedure_totext_appends_as_secondary_ref():
    """TO_TEXT 第二轮绑定不与 VECTOR 抢占：图号命中已有 VECTOR 图的步骤时按多图追加，
    VECTOR 绑定结果保持原样。"""
    steps = ["1\t按下设备电源开关，等待系统自检完成并确认指示灯转为绿色常亮状态。",
             "2\t在触摸屏上选择产品对应的工艺程序号，核对模具温度与压力参数设定。"]
    blocks = [{"block_type": "paragraph", "text": s, "page_num": 1, "source": "openpyxl",
               "extra": {"step_no": i + 1, "row_role": "step",
                         "row_num": 10 + i * 10, "sheet_idx": 0}}
              for i, s in enumerate(steps)]
    assets = [
        _totext_asset(0, "mix_img0000.png", "", "", anchor_row=12, figure_no="图1",
                      status="ROUTE_TO_VECTOR"),
        _totext_asset(1, "mix_img0001.png", "", "", anchor_row=13, figure_no="图1"),
    ]
    doc = _xlsx_doc("设备操作作业指导书.xlsx", blocks, assets, text="\n".join(steps))
    chunks = _chunk_doc(doc)

    step_cards = {c.extra.get("step_no"): c for c in chunks if c.chunk_type == "step_card"}
    refs1 = step_cards[1].extra.get("image_refs") or []
    assert [r["filename"] for r in refs1] == ["mix_img0000.png", "mix_img0001.png"], (
        f"TO_TEXT 应作为第二张图追加且不挤掉 VECTOR: {refs1}")


def test_xlsx_procedure_unbindable_totext_falls_back_to_image_chunk():
    """procedure 版式下绑不进任何步骤的 TO_TEXT 截图（步骤全部已带图、又无图号/内容
    匹配）不能静默丢弃 —— 必须走独立 image chunk 兜底。"""
    steps = ["1\t核对来料标签上的物料编码批次号与送检单信息完全一致后签收入库。"]
    blocks = [{"block_type": "paragraph", "text": steps[0], "page_num": 1,
               "source": "openpyxl",
               "extra": {"step_no": 1, "row_role": "step", "row_num": 10, "sheet_idx": 0}}]
    assets = [
        _totext_asset(0, "q_img0000.png", "", "", anchor_row=11, figure_no="图1",
                      status="ROUTE_TO_VECTOR"),
        _totext_asset(1, "q_img0001.png", "", "单证打印窗口选项设置说明", anchor_row=40),
    ]
    doc = _xlsx_doc("来料检验作业指导书.xlsx", blocks, assets, text=steps[0])
    chunks = _chunk_doc(doc)

    step_cards = [c for c in chunks if c.chunk_type == "step_card"]
    assert step_cards and [r["filename"] for r in
                           (step_cards[0].extra.get("image_refs") or [])] == ["q_img0000.png"]
    img_chunks = [c for c in chunks if c.chunk_type == "image"]
    assert [c for c in img_chunks if "q_img0001" in (c.extra.get("source_image") or "")], (
        "绑不进步骤的 TO_TEXT 截图应建独立 image chunk 兜底")
    assert "q_img0001.png" in _serving_renderable_fns(chunks)


def test_xlsx_procedure_autogen_figure_no_does_not_force_step_n():
    """figure_no 是 extractor 给的"图1/.../图N"占位序号、step 文本无"如图N"
    引用时，"图N→步骤N"启发式必须关闭——否则当图数==步骤数但 anchor 顺序
    与步骤顺序不严格对应（xlsx_sop 真实形态：anchor=14 的图 figure_no=图6
    被强行绑到 step6、anchor=15 的电源图被丢到 step5）就会强行错绑。
    本测试锁死："每张图 figure_no 唯一、step 无 figure_refs"时走 anchor 兜底
    而不是 figure_no→stepN 直接映射。"""
    steps = [
        "1\t第一步:打开设备外壳检查",
        "2\t第二步:连接电源线",
        "3\t第三步:启动设备",
    ]
    blocks = [{"block_type": "paragraph", "text": s, "page_num": 1,
               "source": "openpyxl",
               "extra": {"step_no": i + 1, "row_role": "step",
                         "row_num": 10 + i, "sheet_idx": 0}}
              for i, s in enumerate(steps)]
    # 关键设置：3 张图、3 个步骤、anchor 顺序与 figure_no 不严格同步。
    # 若沿用旧"图N→步骤N"启发式：图1→step1、图2→step2、图3→step3——anchor=15
    # 的图会被绑到 step1（因 figure_no=图1）。真值应按 anchor 序：12→step1、
    # 13→step2、15→step3。
    assets = [
        _totext_asset(0, "auto_img0000.png", "", "", anchor_row=15,
                      figure_no="图1", status="ROUTE_TO_VECTOR"),
        _totext_asset(1, "auto_img0001.png", "", "", anchor_row=12,
                      figure_no="图2", status="ROUTE_TO_VECTOR"),
        _totext_asset(2, "auto_img0002.png", "", "", anchor_row=13,
                      figure_no="图3", status="ROUTE_TO_VECTOR"),
    ]
    # 文件名包含 "作业指导书" 触发 procedure_image_guide 分类器
    doc = _xlsx_doc("自动编号示例作业指导书.xlsx", blocks, assets, text="\n".join(steps))
    chunks = _chunk_doc(doc)

    step_cards = {c.extra.get("step_no"): c for c in chunks if c.chunk_type == "step_card"}
    assert set(step_cards) == {1, 2, 3}
    # 按 anchor_row 升序分配：12→step1, 13→step2, 15→step3
    assert [r["filename"] for r in step_cards[1].extra.get("image_refs") or []] == [
        "auto_img0001.png"], (
        "auto-gen figure_no 不应被信任：anchor=12 才是 step1 的图，不是 figure_no=图1")
    assert [r["filename"] for r in step_cards[2].extra.get("image_refs") or []] == [
        "auto_img0002.png"]
    assert [r["filename"] for r in step_cards[3].extra.get("image_refs") or []] == [
        "auto_img0000.png"], (
        "anchor=15 的图必须走 step3（行号兜底），不应因 figure_no=图1 被劫持到 step1")


def test_docx_totext_fallback_unchanged():
    """非 XLSX 路径行为保持不变：非 step 的 DOCX 嵌入 TO_TEXT 图片不建独立
    image chunk（既有 [ref-enrich] 行为，避免本修复扩大爆炸半径）。"""
    blocks = [{"block_type": "paragraph",
               "text": "公司差旅费报销需在出差结束后五个工作日内提交申请单并附发票原件。",
               "page_num": None, "section_path": None, "source": "native", "extra": {}}]
    assets = [_totext_asset(0, "d_img0000.png", "报销单样例截图", "报销流程说明", anchor_row=None)]
    doc = _xlsx_doc("差旅费报销制度说明.docx", blocks, assets)
    doc["file_ext"] = "docx"
    doc["filename"] = "差旅费报销制度说明.docx"
    chunks = _chunk_doc(doc)
    assert not [c for c in chunks if c.chunk_type == "image"], (
        "DOCX TO_TEXT 嵌入图的兜底行为不应被 XLSX 修复改变")


# ═══════════════ PPTX TO_TEXT 嵌入图的 serving 可渲染性 ═══════════════
# 与 XLSX 同类缺陷（2026-06-10 修复的 slide 版）：slide 绑定原先只收
# ROUTE_TO_VECTOR，TO_TEXT 截图的 OCR 文本进了 slide chunk，原图却没有任何
# serving 可达载体（slide 模式整体跳过独立 image chunk 兜底）→ I5 违例。
# 修复：TO_TEXT 与 TO_VECTOR 同绑进 slide 的 visual_knowledge chunk，
# VECTOR 先入池保证已有 VECTOR 封面不被抢占。


def _pptx_doc(filename, blocks, assets, text=""):
    doc = _xlsx_doc(filename, blocks, assets, text=text)
    doc["doc_id"] = "PPTXIMG1"
    doc["file_ext"] = "pptx"
    doc["filename"] = filename
    return doc


def _pptx_asset(idx, filename, page_num, visual_summary="", ocr_text="",
                status="ROUTE_TO_TEXT", oss_key=None):
    a = _totext_asset(idx, filename, visual_summary, ocr_text, anchor_row=None,
                      status=status)
    a["page_num"] = page_num
    if oss_key is not None:
        a["oss_key"] = oss_key
    return a


def _pptx_slide_blocks(page_num, title, body=None, ocr=None, ocr_image=None):
    blocks = [{"block_type": "heading", "text": title, "page_num": page_num,
               "section_path": title, "source": "python_pptx",
               "extra": {"placeholder": "title", "slide_num": page_num}}]
    if body:
        blocks.append({"block_type": "paragraph", "text": body, "page_num": page_num,
                       "section_path": title, "source": "python_pptx", "extra": {}})
    if ocr:
        blocks.append({"block_type": "ocr_text", "text": ocr, "page_num": page_num,
                       "source": "ocr", "extra": {"source_image": ocr_image or ""}})
    return blocks


def test_pptx_totext_slide_images_bound_serving_renderable():
    """TO_TEXT 截图必须绑进所在 slide 的 visual_knowledge chunk（chunk 级
    source_image 经 to_ha3_doc 进 HA3、image_refs 经 RDS image_refs_json 恢复），
    且上传环节回填的 oss_key 优先于构造路径。"""
    ocr1 = "U8系统登录界面 输入操作员编号与密码 选择账套后点击登录进入主菜单"
    ocr2 = "销售出库单界面 录入出库日期仓库与货位 点击保存审核完成出库登记"
    blocks = (
        _pptx_slide_blocks(1, "U8系统登录", body="本节介绍U8系统的登录步骤与注意事项。",
                           ocr=ocr1, ocr_image="培训_slide1_img0000.png")
        + _pptx_slide_blocks(2, "销售出库操作", body="出库单录入的完整操作演示如下。",
                             ocr=ocr2, ocr_image="培训_slide2_img0001.png")
    )
    assets = [
        # slide 1：上传环节已回填 oss_key（出现副本共享上传的形态）→ 必须优先采用
        _pptx_asset(0, "培训_slide1_img0000.png", 1,
                    visual_summary="U8登录界面截图", ocr_text=ocr1,
                    oss_key="processing/assets/it/PPTXIMG1/v1/dedup_shared.png"),
        # slide 2：无回填 → 构造路径兜底；caption 缺失形态
        _pptx_asset(1, "培训_slide2_img0001.png", 2, ocr_text=ocr2),
    ]
    doc = _pptx_doc("U8操作培训.pptx", blocks, assets)
    chunks = _chunk_doc(doc)

    vk = {c.page_num: c for c in chunks if c.chunk_type == "visual_knowledge"}
    assert set(vk) == {1, 2}, (
        f"带图 slide 应升级为 visual_knowledge，实际页: {sorted(vk)}")
    for pg, c in vk.items():
        assert c.extra.get("source_image"), f"slide {pg} 缺 chunk 级 source_image"
        refs = c.extra.get("image_refs") or []
        assert refs, f"slide {pg} 缺 image_refs"
        # image_refs 契约键（CLAUDE.md：extractor → chunker → builder → 卡片全链路依赖）
        for key in ("oss_key", "source_image", "visual_summary", "ocr_text"):
            assert key in refs[0], f"image_refs 契约键缺失: {key}"
    # 回填 oss_key 优先；未回填走构造路径
    assert vk[1].extra["source_image"] == "processing/assets/it/PPTXIMG1/v1/dedup_shared.png"
    assert vk[2].extra["source_image"] == (
        "processing/assets/it/PPTXIMG1/v1/培训_slide2_img0001.png")
    rendered = _serving_renderable_fns(chunks)
    for a in assets:
        assert a["filename"] in rendered, f"{a['filename']} 提取了但 serving 不可渲染（I5）"
    # slide 模式不建独立 image chunk（绑定即载体，避免重复）
    assert not [c for c in chunks if c.chunk_type == "image"]


def test_pptx_vector_cover_not_displaced_by_totext():
    """同一 slide 同时有 VECTOR 图与 TO_TEXT 截图（assets 中 TO_TEXT 在前）：
    VECTOR 必须保持 refs[0]/封面 source_image，TO_TEXT 作后续 ref 追加。"""
    blocks = _pptx_slide_blocks(1, "产品包装规范", body="包装流程与成品外观要求如下所示。")
    assets = [
        _pptx_asset(0, "包装_slide1_img0000.png", 1, ocr_text="包装机参数设置面板截图"),
        _pptx_asset(1, "包装_slide1_img0001.png", 1,
                    visual_summary="成品包装外观示意图", status="ROUTE_TO_VECTOR"),
    ]
    doc = _pptx_doc("产品包装培训.pptx", blocks, assets)
    chunks = _chunk_doc(doc)

    vks = [c for c in chunks if c.chunk_type == "visual_knowledge"]
    assert len(vks) == 1
    refs = vks[0].extra.get("image_refs") or []
    assert [r["filename"] for r in refs] == [
        "包装_slide1_img0001.png", "包装_slide1_img0000.png"], (
        f"VECTOR 应先入池作封面、TO_TEXT 追加: {[r['filename'] for r in refs]}")
    assert vks[0].extra["source_image"].endswith("包装_slide1_img0001.png"), (
        "TO_TEXT 不得抢占已有 VECTOR 封面")
    assert vks[0].extra.get("visual_summary") == "成品包装外观示意图"


def test_pptx_totext_imageonly_slide_standalone_chunk_with_ocr_desc():
    """纯图 slide（该页无任何文本块）的 TO_TEXT 截图：必须单独建 visual_knowledge
    chunk，且 caption 缺失时 chunk_text 回退 OCR 片段、不得是空描述。"""
    ocr3 = "单证打印窗口 勾选报关合同与财务发票选项 点击确认输出打印外贸单证资料"
    blocks = _pptx_slide_blocks(1, "外贸单证培训", body="单证打印的操作入口见下一页截图。")
    assets = [_pptx_asset(0, "单证_slide2_img0000.png", 2, ocr_text=ocr3)]
    doc = _pptx_doc("外贸单证培训.pptx", blocks, assets)
    chunks = _chunk_doc(doc)

    standalone = [c for c in chunks if c.chunk_type == "visual_knowledge"
                  and c.page_num == 2]
    assert standalone, "纯图 slide 的 TO_TEXT 截图未建 visual_knowledge chunk（图被静默丢弃）"
    c = standalone[0]
    assert c.extra.get("source_image", "").endswith("单证_slide2_img0000.png")
    assert "单证打印" in c.chunk_text, f"空 caption 未回退 OCR 片段: {c.chunk_text!r}"
    assert "单证_slide2_img0000.png" in _serving_renderable_fns(chunks)


def test_route_to_text_assets_uploaded(tmp_path):
    """绑定会把 ROUTE_TO_TEXT 截图绑进 chunk 并构造 processing/assets/ 路径 ——
    上传闸只传 TO_VECTOR 时这些路径永不存在，serving 签出 403 死图
    （2026-06-10 对抗评审发现；UI 截图多数路由 TO_TEXT）。"""
    from opensearch_pipeline.pipeline_nodes import _upload_clean_assets

    img_v = tmp_path / "a_p1_img0001.png"
    img_v.write_bytes(b"v")
    img_t = tmp_path / "a_p1_img0002.png"
    img_t.write_bytes(b"t")
    img_doc = tmp_path / "poster.png"
    img_doc.write_bytes(b"p")
    img_d = tmp_path / "a_p1_img0003.png"
    img_d.write_bytes(b"d")

    class _R:
        doc_id = "DOCX1"
        version_no = 2
        source_key = "raw/production/x.pdf"
        assets = [
            {"status": "ROUTE_TO_VECTOR", "local_path": str(img_v), "oss_key": ""},
            {"status": "ROUTE_TO_TEXT", "local_path": str(img_t), "oss_key": ""},
            # 独立图片文档：oss_key 已指向 raw/ 原对象 → 跳过重复上传
            {"status": "ROUTE_TO_TEXT", "local_path": str(img_doc),
             "oss_key": "raw/production/poster.png"},
            {"status": "DISCARD_DECORATIVE", "local_path": str(img_d), "oss_key": ""},
        ]

    puts = []

    class _Bucket:
        def put_object_from_file(self, key, path):
            puts.append(key)

    n = _upload_clean_assets([_R()], _Bucket())
    assert n == 2, f"应上传 TO_VECTOR + TO_TEXT 共 2 张，实际 {n}"
    assert any("img0001" in k for k in puts), "TO_VECTOR 未上传"
    assert any("img0002" in k for k in puts), "TO_TEXT 未上传（403 死图回归）"
    assert not any("poster" in k for k in puts), "已带 oss_key 的独立图片文档不应重复上传"
    assert not any("img0003" in k for k in puts), "DISCARD 不应上传"
    # 上传后 oss_key 回写（绑定注入读它构造 durable 引用）
    assert _R.assets[0]["oss_key"].startswith("processing/assets/production/DOCX1/v2/")
    assert _R.assets[1]["oss_key"].startswith("processing/assets/production/DOCX1/v2/")


def test_upload_occurrence_copies_share_one_put(tmp_path):
    """同一 local_path 的出现副本（同 media 多处引用）只 PUT 一次，
    oss_key 回写到每个副本 —— 否则同字节重复上传白耗带宽。"""
    from opensearch_pipeline.pipeline_nodes import _upload_clean_assets

    img = tmp_path / "b_img0000.png"
    img.write_bytes(b"x")

    class _R:
        doc_id = "DOCX2"
        version_no = 1
        source_key = "raw/hr/y.docx"
        assets = [
            {"status": "ROUTE_TO_TEXT", "local_path": str(img), "oss_key": ""},
            {"status": "ROUTE_TO_TEXT", "local_path": str(img), "oss_key": ""},
        ]

    puts = []

    class _Bucket:
        def put_object_from_file(self, key, path):
            puts.append(key)

    n = _upload_clean_assets([_R()], _Bucket())
    assert n == 1 and len(puts) == 1, f"副本应共享 1 次上传，实际 puts={len(puts)}"
    assert _R.assets[0]["oss_key"] == _R.assets[1]["oss_key"] != ""


# ═══════════════ DOCX 复用 media 的出现位置保全 ═══════════════

def test_docx_alias_rels_same_bytes_keep_refs(monkeypatch, tmp_path):
    """两个不同 rId 指向字节相同的 media：第二个 rel 的 target_ref 必须以
    别名资产保留（共享一次导出）—— 旧逻辑 md5 去重直接 continue，
    正文第二处引用对齐不到资产、整个出现位置丢失。"""
    docx_mod = pytest.importorskip("docx")
    from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_docx

    shared_bytes = b"\x89PNG\r\n\x1a\n_samebytes"

    class _Part:
        blob = shared_bytes
        content_type = "image/png"

    class _Rel:
        reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

        def __init__(self, ref):
            self.target_part = _Part()
            self.target_ref = ref

    class _DocPart:
        rels = {"rId7": _Rel("media/image1.png"), "rId9": _Rel("media/image2.png")}

    class _Doc:
        part = _DocPart()

    monkeypatch.setattr(docx_mod, "Document", lambda p: _Doc())
    assets = extract_images_from_docx(str(tmp_path / "fake.docx"), str(tmp_path))
    assert len(assets) == 2, f"别名 rel 应保留为第 2 条资产，实际 {len(assets)}"
    assert assets[0].local_path == assets[1].local_path, "同字节应共享一次导出"
    assert {a.original_name for a in assets} == {"media/image1.png", "media/image2.png"}
    assert [a.image_index for a in assets] == [0, 1]


def test_reused_docx_media_keeps_every_occurrence(monkeypatch, tmp_path):
    """同一张图在 DOCX 两处出现（python-docx 按内容去重 → 同 rId 复用）：
    两个出现位置都必须保住各自的 image_index —— 旧对齐逻辑原地改共享对象的
    image_index，last-write-wins，步骤1 的图②永远绑不上（已知问题清单 2026-06-10）。"""
    docx_mod = pytest.importorskip("docx")
    pil_image = pytest.importorskip("PIL.Image")

    img_path = tmp_path / "shared.png"
    pil_image.new("RGB", (200, 120), (200, 30, 30)).save(str(img_path))

    d = docx_mod.Document()
    d.add_paragraph("步骤1：打开U8登录界面，输入工号与密码，参考下图图②完成配置。")
    d.add_picture(str(img_path))
    d.add_paragraph("步骤2：进入销售管理模块，依次选择销售出库单功能项并打开。")
    d.add_paragraph("步骤3：再次核对图②中的登录配置，确认服务器地址与账套无误。")
    d.add_picture(str(img_path))
    fp = tmp_path / "reuse.docx"
    d.save(str(fp))

    _fake_funnel(monkeypatch, {
        "status": "ROUTE_TO_TEXT",
        "ocr_text": "用户名 密码 登录",
        "visual_summary": "U8登录界面截图",
        "image_category": "step_screenshot",
        "vlm_annotation_map": {}, "reason": "",
        "width": 200, "height": 120, "file_size_kb": 5.0,
    })
    r = UnifiedExtractor(simulate=True).extract({
        "doc_id": "REUSE01", "version_no": 1, "local_path": str(fp),
        "file_ext": "docx", "filename": fp.name,
        "raw_key": f"raw/production/{fp.name}", "_tmp_dir": str(tmp_path),
    })

    refs = [b for b in r.blocks if b.block_type == "image_ref"]
    assert len(refs) == 2, f"两处出现应有 2 个 image_ref，实际 {len(refs)}"
    assert sorted(b.extra.get("image_index") for b in refs) == [0, 1]

    idx_list = sorted(a.get("image_index") for a in r.assets)
    assert idx_list == [0, 1], (
        f"资产 image_index 应为 [0,1]，实际 {idx_list}（共享对象 last-write-wins 回归）")
    assert len({a.get("local_path") for a in r.assets}) == 1, "同字节 media 应共享一次导出"


# ═══════════════ 条带切片缝合（Word 把一张照片存成 N 条窄条） ═══════════════

def _strip_fixture(tmp_path, n, size, noise=False):
    """生成 n 张 size 尺寸的 PNG + 对应 image_ref 块与 ImageAsset。"""
    pil_image = pytest.importorskip("PIL.Image")
    import os as _os

    from opensearch_pipeline.extraction.image_extraction_utils import ImageAsset
    from opensearch_pipeline.extraction.schema import ExtractedBlock

    blocks, assets = [], []
    for i in range(n):
        p = tmp_path / f"slice{i:02d}.png"
        if noise:  # 噪声图压不下去 → 文件 >3KB（模拟"现状能存活"的真图）
            im = pil_image.frombytes("RGB", size, _os.urandom(size[0] * size[1] * 3))
        else:
            im = pil_image.new("RGB", size, (10 * i % 255, 80, 120))
        im.save(str(p))
        blocks.append(ExtractedBlock(block_type="image_ref", text="",
                                     extra={"image_index": i}))
        assets.append(ImageAsset(local_path=str(p), image_index=i))
    return blocks, assets


def _para(text="正文段落，用于隔断图片 run。"):
    from opensearch_pipeline.extraction.schema import ExtractedBlock
    return ExtractedBlock(block_type="paragraph", text=text)


def test_strip_run_stitched(tmp_path):
    """6 条 600×20 同宽窄条（逐条必被 Funnel-1 丢弃）→ 缝成一张 600×120 复合图，
    ref 块收敛为 1 个（继承首条 image_index，标注 stitched_from）。"""
    pil_image = pytest.importorskip("PIL.Image")
    from opensearch_pipeline.extraction.unified_extractor import _stitch_strip_runs

    refs, assets = _strip_fixture(tmp_path, 6, (600, 20))
    blocks = [_para()] + refs + [_para("结尾段落。")]
    out_blocks, out_assets = _stitch_strip_runs(blocks, assets)

    out_refs = [b for b in out_blocks if b.block_type == "image_ref"]
    assert len(out_refs) == 1, f"6 条应收敛为 1 个 ref，实际 {len(out_refs)}"
    assert out_refs[0].extra.get("image_index") == 0
    assert out_refs[0].extra.get("stitched_from") == 6
    assert len(out_assets) == 1
    comp = out_assets[0]
    assert comp.image_index == 0 and "stitched" in comp.local_path
    with pil_image.open(comp.local_path) as im:
        assert im.size == (600, 120)


def test_icon_row_not_stitched(tmp_path):
    """4 个 32×32 方形小图标（工具栏/①②③枚举）：不满足条状判据 → 原样保留。"""
    from opensearch_pipeline.extraction.unified_extractor import _stitch_strip_runs

    refs, assets = _strip_fixture(tmp_path, 4, (32, 32))
    out_blocks, out_assets = _stitch_strip_runs(list(refs), assets)
    assert len([b for b in out_blocks if b.block_type == "image_ref"]) == 4
    assert len(out_assets) == 4


def test_grid_tiles_not_stitched(tmp_path):
    """23×31 网格小块（交货单案例的真实形态）：纵向堆叠会产生无意义细长柱，
    条状判据故意不放行 —— 行为与现状一致（丢弃），留待网格重建。"""
    from opensearch_pipeline.extraction.unified_extractor import _stitch_strip_runs

    refs, assets = _strip_fixture(tmp_path, 8, (23, 31))
    out_blocks, out_assets = _stitch_strip_runs(list(refs), assets)
    assert len(out_assets) == 8, "网格块不应被缝合"


def test_surviving_images_not_stitched(tmp_path):
    """4 张 200×60 真图（h≥50、aspect<8、>3KB，现状能通过 Funnel-1）：
    缝合只准救回必死的切片，绝不吞并现状存活的图。"""
    from opensearch_pipeline.extraction.unified_extractor import _stitch_strip_runs

    refs, assets = _strip_fixture(tmp_path, 4, (200, 60), noise=True)
    out_blocks, out_assets = _stitch_strip_runs(list(refs), assets)
    assert len(out_assets) == 4, "现状存活的图被错误缝合"


def test_stitch_missing_asset_fail_open(tmp_path):
    """ref 找不到对应资产（对齐缺口）→ 整个 run 原样保留，不抛异常。"""
    from opensearch_pipeline.extraction.schema import ExtractedBlock
    from opensearch_pipeline.extraction.unified_extractor import _stitch_strip_runs

    refs = [ExtractedBlock(block_type="image_ref", text="",
                           extra={"image_index": i}) for i in range(5)]
    out_blocks, out_assets = _stitch_strip_runs(refs, [])
    assert len(out_blocks) == 5 and out_assets == []


# ═══════════════ 圈数字 callout 伪标题 veto ═══════════════

def test_is_pseudo_heading_cases():
    from opensearch_pipeline.extraction.schema import is_pseudo_heading
    for t in ("⑤双击图标", "  ⑤双击图标", "❸点击保存", "⓫输入数量"):
        assert is_pseudo_heading(t), f"{t!r} 应判为 callout"
    for t in ("4.1 检查模具", "第一章 总则", "一、目的", "（一）适用范围", "1. 适用范围", ""):
        assert not is_pseudo_heading(t), f"{t!r} 不应判为 callout"


def test_pdf_callout_demoted_heading_kept(tmp_path):
    """同为标题字号："⑤双击图标"→ 普通段落（旧行为被字号启发判成 heading →
    章节：⑤双击图标 污染下游所有 chunk）；"4.1 检查模具"→ 必须仍是 heading
    （驱动切块边界，veto 故意不碰编号标题）。"""
    fitz = pytest.importorskip("fitz")
    pytest.importorskip("pdfplumber")
    from opensearch_pipeline.extraction.pdf_extractor import extract_pdf

    doc = fitz.open()
    page = doc.new_page()
    y = 60
    page.insert_text((50, y), "FL-ZS-WI-099 测试作业指导书",
                     fontsize=16, fontname="china-s")
    y += 28
    for i in range(14):
        page.insert_text((50, y), f"正文第{i}行，常规操作说明文本，用来建立正文字号的统计分布。",
                         fontsize=10.5, fontname="china-s")
        y += 15
    page.insert_text((50, y), "⑤双击图标", fontsize=16, fontname="china-s")
    y += 28
    page.insert_text((50, y), "4.1 检查模具", fontsize=16, fontname="china-s")
    p = tmp_path / "callout.pdf"
    doc.save(str(p))
    doc.close()

    blocks, _, _ = extract_pdf(str(p))

    def _norm(s):
        return "".join((s or "").split())  # pdfplumber 拼词会插双空格

    callout = [b for b in blocks if "⑤双击图标" in _norm(b.text)]
    numbered = [b for b in blocks if "4.1检查模具" in _norm(b.text)]
    assert callout and all(b.block_type != "heading" for b in callout), \
        "圈数字 callout 不应是 heading"
    assert numbered and any(b.block_type == "heading" for b in numbered), \
        "编号节标题必须仍是 heading"
    assert all("⑤" not in (b.section_path or "") for b in blocks), \
        "section_path 被 callout 污染"


# ═══════════════ 准入策略 × 提取器 契约 ═══════════════

def test_every_ingestable_ext_has_extractor_route():
    """policy 放行的扩展名必须都有非 unsupported 的提取分支 —— 否则未知格式
    会以 unsupported 空文档静默走完生命周期（0-chunk 不可见文档的复发路径）。"""
    from opensearch_pipeline.ingest_policy import INGESTABLE_EXTS
    # unified_extractor.extract() 的路由表（与代码同步维护；新增格式两处都要加）
    extractor_routes = {
        "pdf", "docx", "xlsx", "pptx",
        "txt", "md", "csv", "html", "htm",
        "png", "jpg", "jpeg", "webp", "tif", "tiff", "gif", "bmp",
    }
    missing = INGESTABLE_EXTS - extractor_routes
    assert not missing, f"policy 放行但提取器无路由: {missing}"


def test_stage1_sql_predicates_share_single_source():
    """认领 SQL（node_scan_raw_files）与排空计数 SQL（_count_pending_rows stage-1）
    必须用同一份扩展名排除清单 —— 不一致时计数器看得到、认领挑不走，
    run_stage_drained 的无进展守卫会把 stage-1 永久判死。"""
    import inspect

    from opensearch_pipeline import dataworks_orchestrator, pipeline_nodes
    from opensearch_pipeline.ingest_policy import stage1_ext_exclusion_sql

    frag = "stage1_ext_exclusion_sql()"
    scan_src = inspect.getsource(pipeline_nodes.node_scan_raw_files)
    count_src = inspect.getsource(dataworks_orchestrator._count_pending_rows)
    assert frag in scan_src, "node_scan_raw_files 未使用共享排除清单"
    assert frag in count_src, "_count_pending_rows 未使用共享排除清单"
    # 渲染结果是合法 SQL 元组
    rendered = stage1_ext_exclusion_sql()
    assert rendered.startswith("(") and rendered.endswith(")") and "'doc'" in rendered


def _register_src_path():
    import os as _os
    return _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         "dataworks_nodes", "register_new_files.py")


def _load_register_fallback_ns():
    """AST 抽取 register_new_files.py 内联 fallback 区（PyODPS 节点上实际执行的那份），
    exec 后返回含全部 fallback 函数的命名空间。"""
    import ast
    import os as _os

    src_path = _register_src_path()
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    fallback_body = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if any(isinstance(n, ast.FunctionDef) and n.name == "should_ingest_raw_key"
                       for n in ast.walk(handler)):
                    fallback_body = handler.body
    assert fallback_body, "未找到内联 fallback should_ingest_raw_key"
    ns = {"os": _os, "INGEST_POLICY_REV": "", "print": lambda *a, **k: None}
    exec(compile(ast.Module(body=fallback_body, type_ignores=[]), src_path, "exec"), ns)
    return ns


def test_register_fallback_parity_with_canonical_policy():
    """register_new_files.py 的内联副本（PyODPS 节点上实际执行的那份）必须与
    正本逐键同判 —— 上一次漂移（只跳 _quarantine）正是垃圾注册的来源。"""
    from opensearch_pipeline.ingest_policy import (
        IGNORED_EXTS,
        INGESTABLE_EXTS,
        UNSUPPORTED_LEGACY_EXTS,
        should_ingest_raw_key,
    )

    fallback_fn = _load_register_fallback_ns()["should_ingest_raw_key"]

    # 全矩阵对拍：所有清单扩展名 × 路径形态 + 边角 key
    keys = []
    all_exts = sorted(IGNORED_EXTS | UNSUPPORTED_LEGACY_EXTS | INGESTABLE_EXTS | {"wps", "et", "bak"})
    for ext in all_exts:
        keys += [f"raw/admin/文件.{ext}",
                 f"raw/_archive/admin/文件.{ext}",
                 f"raw/admin/_quarantine/文件.{ext}"]
    keys += ["raw/hr/~$temp.docx", "raw/x/Thumbs.db", "raw/x/.DS_Store",
             "raw/admin/", "raw/admin/无扩展名", "raw/admin/报告.PDF",
             "_quarantine/x.pdf", "_archive/x.pdf"]
    for key in keys:
        canonical_ok, _ = should_ingest_raw_key(key)
        fallback_ok, _ = fallback_fn(key)
        assert canonical_ok == fallback_ok, f"策略分歧 key={key}: 正本={canonical_ok} 副本={fallback_ok}"


# ═══════════════ 注册侧同名(stem)防重 ═══════════════
# 孪生实例（2026-06-11）：FL-ZS-WI-005《注塑收货报检》.pdf/.docx 双注册；
# A1员工行为管理标准.docx 在 hr/admin/marketing/supply 多目录被注册 4 次。
# 策略（用户拍板）：同部门同 stem 已有 active 注册 → skip + 告警；
# 跨部门同 stem → 仅 warn 不拦截（拦截与否是 ACL 归属问题，防重不替它做决定）。


def test_raw_key_stem_shapes():
    from opensearch_pipeline.ingest_policy import raw_key_stem

    key = "raw/production_injection/FL-ZS-WI-005《注塑收货报检》.pdf"
    assert raw_key_stem(key) == "FL-ZS-WI-005《注塑收货报检》"   # 中文文件名
    assert raw_key_stem("raw/admin/a.b.docx") == "a.b"            # 多点：只去最后一层
    assert raw_key_stem("raw/admin/无扩展名") == "无扩展名"        # 无扩展名原样返回
    assert raw_key_stem("raw/admin/报告.PDF") == "报告"            # 大写扩展名
    assert raw_key_stem("raw/it/Report Final.DOCX") == "Report Final"  # stem 大小写保留
    assert raw_key_stem("raw/hr/年假规定 .docx") == "年假规定"      # 去扩展名后 strip
    assert raw_key_stem("员工手册.docx") == "员工手册"             # 裸文件名（无目录）
    assert raw_key_stem("") == ""


def test_stem_twin_action_three_branches():
    from opensearch_pipeline.ingest_policy import stem_twin_action

    existing = {"注塑收货报检": {"production"}}
    # ok：无同名注册
    assert stem_twin_action("admin", "新文档", existing) == ("ok", "")
    # skip：同部门（大小写不敏感），reason 列出已注册部门
    action, reason = stem_twin_action("PRODUCTION", "注塑收货报检", existing)
    assert action == "skip" and "production" in reason
    # warn：异部门，reason 列出已注册部门
    action, reason = stem_twin_action("supply", "注塑收货报检", existing)
    assert action == "warn" and "production" in reason
    # 空 stem 不参与防重
    assert stem_twin_action("admin", "", {"": {"admin"}}) == ("ok", "")


def test_stem_twin_same_dept_cross_extension_skipped():
    """孪生实例复刻：.pdf 已注册，同部门再传 .docx → 同 stem skip（防孪生 doc_id）。"""
    from opensearch_pipeline.ingest_policy import raw_key_stem, stem_twin_action

    registered = "raw/production_injection/FL-ZS-WI-005《注塑收货报检》.pdf"
    incoming = "raw/production_injection/FL-ZS-WI-005《注塑收货报检》.docx"
    existing = {raw_key_stem(registered): {"production"}}
    action, _ = stem_twin_action("production", raw_key_stem(incoming), existing)
    assert action == "skip"


def test_stem_twin_cross_dept_warns_not_blocked():
    """A1员工行为管理标准 多目录形态：跨部门同名 → 仅告警不拦截，reason 列出全部已注册部门。"""
    from opensearch_pipeline.ingest_policy import raw_key_stem, stem_twin_action

    stem = raw_key_stem("raw/supply/A1员工行为管理标准.docx")
    existing = {stem: {"hr", "admin", "marketing"}}
    action, reason = stem_twin_action("supply", stem, existing)
    assert action == "warn"
    for dept in ("hr", "admin", "marketing"):
        assert dept in reason, f"reason 应列出已注册部门 {dept}: {reason!r}"


def test_stem_twin_batch_internal_dedup():
    """批内防重：注册成功后 (stem, dept) 回填 existing_map → 同一批第二个同名同部门
    文件被跳过。（脚本主体的回填语句无法 import —— 此处函数级复刻同一更新逻辑，
    脚本内回填位置靠 parity 注释 + 人工 review。）"""
    from opensearch_pipeline.ingest_policy import raw_key_stem, stem_twin_action

    existing = {}
    k1, k2 = "raw/hr/考勤管理.docx", "raw/hr/考勤管理.pdf"
    action, _ = stem_twin_action("hr", raw_key_stem(k1), existing)
    assert action == "ok"
    existing.setdefault(raw_key_stem(k1), set()).add("hr")   # 注册成功 → 回填
    action, _ = stem_twin_action("hr", raw_key_stem(k2), existing)
    assert action == "skip", "批内第二个同名同部门文件应被跳过"
    # 批内跨部门：仍是 warn 不拦截
    action, _ = stem_twin_action("admin", raw_key_stem(k2), existing)
    assert action == "warn"


def test_register_fallback_parity_stem_twin():
    """同名防重两函数：register_new_files.py 内联副本必须与正本逐例同判
    （与 should_ingest_raw_key 同一 AST 对拍机制）。"""
    from opensearch_pipeline.ingest_policy import raw_key_stem, stem_twin_action

    ns = _load_register_fallback_ns()
    fb_stem = ns.get("raw_key_stem")
    fb_action = ns.get("stem_twin_action")
    assert fb_stem and fb_action, "内联 fallback 缺 raw_key_stem/stem_twin_action"

    keys = [
        "raw/production_injection/FL-ZS-WI-005《注塑收货报检》.pdf",
        "raw/production_injection/FL-ZS-WI-005《注塑收货报检》.docx",
        "raw/hr/A1员工行为管理标准.docx",
        "raw/admin/a.b.docx", "raw/admin/无扩展名", "raw/admin/报告.PDF",
        "raw/hr/年假规定 .docx", "raw/x/.DS_Store", "员工手册.docx", "", "raw/admin/",
    ]
    for key in keys:
        assert raw_key_stem(key) == fb_stem(key), f"raw_key_stem 分歧 key={key}"

    existing = {"A1员工行为管理标准": {"hr", "admin"}, "注塑收货报检": {"production"}}
    cases = [
        ("hr", "A1员工行为管理标准"), ("HR", "A1员工行为管理标准"),
        ("marketing", "A1员工行为管理标准"), ("production", "注塑收货报检"),
        ("supply", "注塑收货报检"), ("admin", "新文档"),
        ("", "A1员工行为管理标准"), ("hr", ""),
    ]
    for dept, stem in cases:
        assert stem_twin_action(dept, stem, existing) == fb_action(dept, stem, existing), \
            f"stem_twin_action 分歧 dept={dept} stem={stem}"


def test_ingest_policy_rev_in_sync():
    """INGEST_POLICY_REV 两侧一致：正本常量 vs register_new_files.py 模块级赋值
    （节点日志靠它核对线上策略版本，单边 bump 等于核对失效）。"""
    import ast

    from opensearch_pipeline.ingest_policy import INGEST_POLICY_REV

    tree = ast.parse(open(_register_src_path(), encoding="utf-8").read())
    register_rev = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "INGEST_POLICY_REV":
                    register_rev = ast.literal_eval(node.value)
    assert register_rev == INGEST_POLICY_REV, (
        f"策略版本漂移: 正本={INGEST_POLICY_REV} register={register_rev}")


# ═══════════════ ImageRef filename 次级身份 + 同 anchor 消歧 ═══════════════

def test_imageref_filename_secondary_identity_backward_compat():
    """ImageRef.filename 作为 xlsx 同 block_index 多图的次级身份：
    - GT 标了 filename：strict 比对要求 filename 一致（否则 jaccard=0）
    - GT 未标 filename：退回旧 (fmt, block_index) presence 语义（pred 任意 filename 都算对）
    避免老 GT 文件破坏 + 新 GT 文件能消歧。"""
    from eval_harness.binding.ref_keys import parse_ref_dict, jaccard
    # 同 block_index 同 filename → 1.0
    gt = [parse_ref_dict({"block_index": 12, "filename": "a.jpg"}, "xlsx")]
    pred_match = [parse_ref_dict({"block_index": 12, "filename": "a.jpg"}, "xlsx")]
    assert jaccard(gt, pred_match) == 1.0
    # 同 block_index 不同 filename → 0.0
    pred_mismatch = [parse_ref_dict({"block_index": 12, "filename": "b.jpg"}, "xlsx")]
    assert jaccard(gt, pred_mismatch) == 0.0
    # 老 GT（无 filename）+ pred 有 filename → 1.0（向后兼容：旧 GT 不应失效）
    gt_old = [parse_ref_dict({"block_index": 12}, "xlsx")]
    assert jaccard(gt_old, pred_match) == 1.0
    assert jaccard(gt_old, pred_mismatch) == 1.0
    # 老 GT vs 不同 block_index pred → 0.0（block_index 本身的匹配不变）
    pred_wrong_anchor = [parse_ref_dict({"block_index": 99, "filename": "x.jpg"}, "xlsx")]
    assert jaccard(gt_old, pred_wrong_anchor) == 0.0
    # 多 ref：GT 标了 2 张不同 filename，pred 给对 1/2 张 → 0.5
    gt_two = [parse_ref_dict({"block_index": 12, "filename": "a.jpg"}, "xlsx"),
              parse_ref_dict({"block_index": 14, "filename": "b.jpg"}, "xlsx")]
    pred_one = [parse_ref_dict({"block_index": 12, "filename": "a.jpg"}, "xlsx"),
                parse_ref_dict({"block_index": 14, "filename": "wrong.jpg"}, "xlsx")]
    assert abs(jaccard(gt_two, pred_one) - 1/3) < 1e-6, (
        f"GT 标 2 文件名、pred 错 1 → intersect=1 union=3 → 1/3，实际 {jaccard(gt_two, pred_one)}")


def test_xlsx_procedure_same_anchor_secondary_falls_to_remote_step():
    """同 anchor_row 多图消歧：首张图按内容信号绑到步骤 A 后，剩余同 anchor 的图
    若内容信号也指向 A 的相邻步骤（±1 行），P0 不应贪心绑、应让 P2 兜到远端步骤。
    模拟 xlsx_sop 形态：anchor=12 有 2 图，img_first 强信号→step4(row 14)，
    img_second 弱偶合信号→step3(row 13) 相邻不绑；P2 把 img_second 推到 step6(row 16)。"""
    steps = [
        "1\t调水平:观察天平水平泡是否居中",
        "2\t仪器开启:接上电源按 on/off 启动",
        "3\t试样准备:湿样品不得直接接触天平托盘称量",
        "4\t天平调零:按 0/T 将天平示值归零",
        "5\t样品称重:放试样读数",
        "6\t仪器关闭:测试结束拔掉电源",
    ]
    blocks = [{"block_type": "paragraph", "text": s, "page_num": 1, "source": "openpyxl",
               "extra": {"step_no": i + 1, "row_role": "step", "row_num": 11 + i, "sheet_idx": 0}}
              for i, s in enumerate(steps)]
    # img_first: 强烈匹配 step4 (含"归零"独有词)
    # img_second: 偶合匹配 step3 (含"称量" — step3 末尾偶然出现)；anchor 同 img_first；语义实际属于 step6（白色天平外观）
    assets = [
        _totext_asset(0, "sa_img0000.png",
                      "梅特勒-托利多电子天平显示 0.00g 手指按 O/T 归零键",
                      "归零", anchor_row=12, figure_no="图1", status="ROUTE_TO_VECTOR"),
        _totext_asset(1, "sa_img0001.png",
                      "白色电子天平 QA--005 称量盘外观 仪器编号",
                      "QA--005", anchor_row=12, figure_no="图2", status="ROUTE_TO_VECTOR"),
    ]
    doc = _xlsx_doc("同anchor消歧测试作业指导书.xlsx", blocks, assets,
                    text="\n".join(steps))
    chunks = _chunk_doc(doc)
    step_cards = {c.extra.get("step_no"): c for c in chunks if c.chunk_type == "step_card"}
    assert set(step_cards) == {1, 2, 3, 4, 5, 6}
    # 关键断言：img_first → step4（强内容信号），img_second NOT → step3
    s4_refs = [r["filename"] for r in (step_cards[4].extra.get("image_refs") or [])]
    assert s4_refs == ["sa_img0000.png"], f"step4 期望 sa_img0000：{s4_refs}"
    s3_refs = step_cards[3].extra.get("image_refs") or []
    assert not s3_refs, f"step3 不应被同 anchor 的偶合内容信号占用：{s3_refs}"
    # img_second 进 P2 redirect 分支：_far_score 优先 forward (row > 已绑步骤 row)
    # 且 row 最大。anchor_taken_steps[12] = [4]（row 14），open_steps row 11/12/13/15/16；
    # forward = {step5(15), step6(16)} → max row = step6（往步骤序列末端推，避免抢占
    # step5 的自然 anchor=14 槽）。
    landed = [c.extra.get("step_no") for c in step_cards.values()
              if "sa_img0001.png" in [r["filename"] for r in (c.extra.get("image_refs") or [])]]
    assert landed == [6], (
        f"sa_img0001 应被 P2 redirect 派到 step6（forward 远端），实际 step{landed}")
