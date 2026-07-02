# -*- coding: utf-8 -*-
"""死 refs 救活 v2（#F-mm12, RAG_IMG_CHUNK_FALLBACK_V2 默认 OFF）回归测试。

两个残余 serving-dead 缺口（xlsx/pptx/纯图文档已由 I5 修）：
  (1) 非 step docx/pdf 的 ROUTE_TO_TEXT 截图：refs 落在 text/clause chunk 上
      （HA3 不携带、RDS 恢复白名单不含）→ 图上传了 OSS 却永远渲染不出；
  (2) step 误路由 + chunker 0 step_groups 回退 text 时 is_step_mode 仍 True →
      兜底分支被跳过，整篇图（含 TO_VECTOR）无任何 serving 载体。
不变式：flag OFF 行为与历史逐字节一致；file_ext 显式限定 docx/doc/pdf；
被救活 chunk 打 extra.fallback_source 归因标记。
"""

from opensearch_pipeline.pipeline_nodes import node_chunk_documents


def _asset(filename, status, oss_key=None):
    return {
        "filename": filename, "status": status,
        "oss_key": oss_key or f"processing/assets/x/{filename}",
        "visual_summary": "U8系统操作界面截图，展开菜单红色箭头指向按钮",
        "ocr_text": "业务导航 常用功能 报表中心 库存管理 销售出库单 保存 审核 打印" * 4,
        "page_num": 1, "width": 800, "height": 600, "file_size_kb": 88.0,
        "image_index": 0, "original_index": 0,
    }


def _clause_doc(assets, file_ext="docx"):
    """非 step 制度类文档（clause/text 路由）。"""
    clauses = [
        "第一条 为规范公司信息系统使用管理，维护数据安全，特制定本制度。",
        "第二条 员工使用U8系统须使用个人账号登录，不得共享账号或密码。",
        "第三条 系统操作界面示例见附图，遇到异常应及时联系信息部处理。",
    ]
    return {
        "doc_id": "V2CLAUSE01", "version_no": 1,
        "title": f"信息系统使用管理制度.{file_ext}",
        "filename": f"x.{file_ext}", "file_ext": file_ext,
        "category_l1": "policy", "category_l2": "it_policy",
        "text": "\n".join(clauses),
        "blocks": [{"block_type": "paragraph", "text": s, "page_num": 1,
                    "section_path": None, "source": "native", "extra": {}} for s in clauses],
        "assets": assets,
        "source_key": f"raw/it/x.{file_ext}", "canonical_key": "",
        "owner_dept": "it", "permission_level": "public",
        "kb_type": "public", "risk_level": "low", "redaction_action": "CLEAN",
    }


def _step_misroute_doc(assets):
    """被判 step（正文含步骤标记）但 blocks 全是 table → chunker 0 step_groups 回退 text。"""
    steps = [
        "步骤1：整理《发货单》，核对发货数量与单据是否一致无误后进行系统录入操作。",
        "步骤2：进入电脑桌面登录U8系统，双击快捷方式图标输入用户名和密码登录系统。",
        "步骤3：按照系统路径点击进入销售出库单界面，录入出库日期、仓库编号等数据。",
    ]
    return {
        "doc_id": "V2MISROUTE01", "version_no": 1,
        "title": "FL-CW-WI-020《表格版发货操作》作业指导书.docx",
        "filename": "x.docx", "file_ext": "docx",
        "category_l1": "sop", "category_l2": "business_sop",
        "text": "\n".join(steps),
        # 步骤标记全在 table 块里（财务手册变体）：_detect_step_patterns 在平文上命中，
        # _chunk_by_step 只扫 paragraph/heading → step_groups 为空 → text 回退
        "blocks": [{"block_type": "table", "text": s, "page_num": 1,
                    "section_path": None, "source": "native", "extra": {}} for s in steps],
        "assets": assets,
        "source_key": "raw/production/x.docx", "canonical_key": "",
        "owner_dept": "production", "permission_level": "public",
        "kb_type": "public", "risk_level": "low", "redaction_action": "CLEAN",
    }


def _run(doc):
    ctx = {"canonicals": [doc], "split_mode": "dynamic",
           "prepend_title": True, "prepend_section": True}
    node_chunk_documents(ctx)
    return ctx["chunks"]


def _img_chunks(chunks):
    return [c for c in chunks if getattr(c, "chunk_type", "") == "image"]


def _set_flag(monkeypatch, on):
    if on:
        monkeypatch.setenv("RAG_IMG_CHUNK_FALLBACK_V2", "true")
    else:
        monkeypatch.delenv("RAG_IMG_CHUNK_FALLBACK_V2", raising=False)


# ═══════════════ (1) 非 step docx/pdf 的 TO_TEXT ═══════════════

def test_off_nonstep_docx_to_text_stays_dead(monkeypatch):
    """OFF（默认）：非 step docx 的 TO_TEXT 截图不建 image chunk —— 现状保持。"""
    _set_flag(monkeypatch, False)
    chunks = _run(_clause_doc([_asset("ui.png", "ROUTE_TO_TEXT")]))
    assert _img_chunks(chunks) == []


def test_on_nonstep_docx_to_text_rescued(monkeypatch):
    """ON：TO_TEXT 截图获得 serving 可达载体（image chunk 带 source_image），
    并打 fallback_source 归因标记。"""
    _set_flag(monkeypatch, True)
    chunks = _run(_clause_doc([_asset("ui.png", "ROUTE_TO_TEXT")]))
    imgs = _img_chunks(chunks)
    assert len(imgs) == 1
    extra = imgs[0].extra or {}
    assert extra.get("source_image")
    assert extra.get("fallback_source") == "to_text_docx_pdf_v2"


def test_on_nonstep_pdf_to_text_rescued(monkeypatch):
    _set_flag(monkeypatch, True)
    chunks = _run(_clause_doc([_asset("ui.png", "ROUTE_TO_TEXT")], file_ext="pdf"))
    assert len(_img_chunks(chunks)) == 1


def test_on_file_ext_guard_html_not_rescued(monkeypatch):
    """file_ext 显式限定 docx/doc/pdf：html 等其他格式的 TO_TEXT 不放行（勿裸 or）。"""
    _set_flag(monkeypatch, True)
    chunks = _run(_clause_doc([_asset("ui.png", "ROUTE_TO_TEXT")], file_ext="html"))
    assert _img_chunks(chunks) == []


def test_on_to_vector_no_attribution_marker(monkeypatch):
    """非 step docx 的 TO_VECTOR 本来就建卡（现状）：ON 时不得打 v2 归因标记。"""
    _set_flag(monkeypatch, True)
    chunks = _run(_clause_doc([_asset("photo.png", "ROUTE_TO_VECTOR")]))
    imgs = _img_chunks(chunks)
    assert len(imgs) == 1
    assert "fallback_source" not in (imgs[0].extra or {})


# ═══════════════ (2) step 误路由逃生门 ═══════════════

def test_misroute_doc_falls_back_to_text_precondition(monkeypatch):
    """前置自检：该 fixture 确实被判 step 后回退 —— 无 step_card/procedure_parent。"""
    _set_flag(monkeypatch, False)
    chunks = _run(_step_misroute_doc([]))
    types = {getattr(c, "chunk_type", "") for c in chunks}
    assert "step_card" not in types and "procedure_parent" not in types


def test_off_misroute_images_stay_dead(monkeypatch):
    _set_flag(monkeypatch, False)
    chunks = _run(_step_misroute_doc([_asset("shot.png", "ROUTE_TO_VECTOR")]))
    assert _img_chunks(chunks) == []      # 现状：整篇图 serving-dead


def test_on_misroute_escape_hatch_rescues_vector_and_text(monkeypatch):
    _set_flag(monkeypatch, True)
    chunks = _run(_step_misroute_doc([
        _asset("shot1.png", "ROUTE_TO_VECTOR"),
        _asset("shot2.png", "ROUTE_TO_TEXT", oss_key="processing/assets/x/shot2.png"),
    ]))
    imgs = _img_chunks(chunks)
    assert len(imgs) == 2
    assert all((c.extra or {}).get("fallback_source") == "step_route_fallback_v2" for c in imgs)


def test_on_true_step_doc_unaffected(monkeypatch):
    """真 step 文档（产出 step_card）不触发逃生门：TO_VECTOR 图仍按现状不建独立卡。"""
    _set_flag(monkeypatch, True)
    steps = [
        "步骤1：整理《发货单》，核对发货数量与单据是否一致无误后进行系统录入操作。",
        "步骤2：进入电脑桌面登录U8系统，双击快捷方式图标输入用户名和密码登录系统。",
    ]
    doc = _step_misroute_doc([_asset("shot.png", "ROUTE_TO_VECTOR")])
    doc["blocks"] = [{"block_type": "paragraph", "text": s, "page_num": 1,
                      "section_path": None, "source": "native", "extra": {}} for s in steps]
    doc["doc_id"] = "V2TRUESTEP01"
    chunks = _run(doc)
    types = {getattr(c, "chunk_type", "") for c in chunks}
    assert "step_card" in types
    assert _img_chunks(chunks) == []      # 逃生门不触发（有 step_card 载体路径）
