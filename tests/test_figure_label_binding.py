# -*- coding: utf-8 -*-
"""图号引用绑定（RC1）+ 混合编号继承（RC2）回归测试 — 2026-06-11 FL-ZS-WI-005 修复批。

RC1：PDF 版式"图在引用文字之前"时，阅读序/y 锚定会把图吞进前一步骤。
     修法 = 页面叠加圈号标注（"⑧" 独立文本元素，贴在图片 bbox 内）作为
     第 0 优先级证据：标注中心落在哪张图 bbox 内 ⇒ 该图图号；与同页正文
     "（图⑧）"引用联合命中 ⇒ image_ref 插到引用块之后（绑定到引用步骤）。
RC2：混合编号（步骤3 之下出现 3.1/3.2/4.1 条款行）打乱 X.Y ordinal 状态机：
     3.2→step_no=0、4.1→ordinal 跳号。修法 = X.Y 主号与当前【显式】主步骤号
     一致时继承主号、Y 记 sub_step_no；纯 X.Y 编号文档不受影响。
"""
from opensearch_pipeline.chunker import DocumentChunker
from opensearch_pipeline.pipeline_nodes import _inject_image_ref_blocks


# ──────────────────────────── RC1：图号引用绑定 ────────────────────────────

def _para(text, page, y0, y1):
    return {"block_type": "paragraph", "text": text, "page_num": page,
            "extra": {"y0": y0, "y1": y1}}


def _label(char, page, x0, x1, y0, y1):
    """页面叠加圈号标注块（pdf_extractor detected_by=circled_label 形态）。"""
    return {"block_type": "paragraph", "text": char, "page_num": page,
            "extra": {"detected_by": "circled_label", "circled_label": char,
                      "x0": x0, "x1": x1, "y0": y0, "y1": y1}}


def _asset(idx, page, bbox, ocr=""):
    return {"image_index": idx, "page_num": page, "bbox": bbox,
            "status": "ROUTE_TO_VECTOR", "filename": f"img{idx}.jpeg",
            "oss_key": f"k{idx}", "ocr_text": ocr, "visual_summary": "",
            "vlm_annotation_map": {}}


DOC = {"doc_id": "T_ZSWI005", "version_no": 1, "title": "t.pdf"}

# ZS-WI-005 page-2 形态：3.1 文本 → [三张图并排 y482-706，"⑧"标注贴在右图内]
# → 3.2 文本（引用 图⑧）。阅读序会把三张图全部吞进 3.1。
ZSWI_BLOCKS = [
    _para("步骤3： 3.1 进入U8系统的“扫码报检”界面（如下图②-⑥步操作）。", 2, 455, 470),
    _label("⑧", 2, 414, 428, 493, 504),          # 贴在 img2（枪图）bbox 内
    {"block_type": "heading", "text": "3.2 进入扫描报检页面后，扫码枪红光照准条形码区域，"
     "扫描《交货单》（图⑧）；", "level": 3, "page_num": 2, "extra": {"y0": 714, "y1": 728}},
]
ZSWI_ASSETS = [
    _asset(0, 2, [43, 482, 167, 706], ocr="U8菜单"),
    _asset(1, 2, [184, 482, 386, 706], ocr="采购订单"),
    _asset(2, 2, [397, 482, 534, 706]),            # 枪图：OCR 空、无标注图
]


def test_overlay_label_binds_image_to_referencing_step():
    """"⑧" 标注贴在枪图内 + 3.2 正文引用（图⑧）⇒ 枪图绑到 3.2，其余留 3.1。"""
    blocks = _inject_image_ref_blocks([dict(b) for b in ZSWI_BLOCKS],
                                      [dict(a) for a in ZSWI_ASSETS], dict(DOC))
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        blocks, "T_ZSWI005", 1, metadata={"title": "t.pdf"})

    def imgs_of(substr):
        for c in chunks:
            if c.chunk_type == "step_card" and substr in (c.chunk_text or ""):
                return {r.get("image_index") for r in c.extra.get("image_refs", [])}
        return None

    assert imgs_of("扫码枪红光") == {2}, "枪图(⑧)必须绑定到引用它的 3.2 步骤"
    assert imgs_of("如下图②-⑥") == {0, 1}, "②-⑥ 截图留在 3.1，不被图号引用改绑"


def test_ambiguous_overlay_label_is_dropped():
    """标注中心同时落在两张重叠图 bbox 内 ⇒ 歧义弃用，不触发图号改绑。"""
    assets = [
        _asset(0, 2, [43, 482, 430, 706]),     # 与 img1 重叠且都含 "⑧" 标注点
        _asset(1, 2, [397, 482, 534, 706]),
    ]
    blocks = _inject_image_ref_blocks([dict(b) for b in ZSWI_BLOCKS],
                                      [dict(a) for a in assets], dict(DOC))
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        blocks, "T_ZSWI005", 1, metadata={"title": "t.pdf"})
    for c in chunks:
        if c.chunk_type == "step_card" and "扫码枪红光" in (c.chunk_text or ""):
            bound = {r.get("image_index") for r in c.extra.get("image_refs", [])}
            assert bound == set(), "歧义标注不得触发改绑（回退 y 锚定 → 图留在 3.1）"


def test_reading_order_layout_unaffected():
    """图随文后的版式（WI-007 形态，无叠加标注）：阅读序绑定保持原样。"""
    blocks = [
        _para("步骤2：每天上午9点左右，向各区班长收集《交货单》，核对抄录（如图①）。", 1, 100, 130),
        _para("步骤3：进入U8系统的“扫码报检”界面。", 1, 400, 415),
    ]
    assets = [_asset(0, 1, [50, 140, 300, 380]), _asset(1, 1, [50, 430, 300, 600])]
    enriched = _inject_image_ref_blocks(blocks, assets, dict(DOC))
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        enriched, "T_WI007", 1, metadata={"title": "t.pdf"})
    for c in chunks:
        if c.chunk_type != "step_card":
            continue
        bound = {r.get("image_index") for r in c.extra.get("image_refs", [])}
        if "收集《交货单》" in c.chunk_text:
            assert bound == {0}
        elif "扫码报检" in c.chunk_text:
            assert bound == {1}


def test_label_text_suppressed_from_card_text():
    """独立圈号标注块不得混入步骤卡正文（"⑧ 图中…" 噪声）。"""
    blocks = _inject_image_ref_blocks([dict(b) for b in ZSWI_BLOCKS],
                                      [dict(a) for a in ZSWI_ASSETS], dict(DOC))
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        blocks, "T_ZSWI005", 1, metadata={"title": "t.pdf"})
    for c in chunks:
        if c.chunk_type == "step_card" and "如下图②-⑥" in (c.chunk_text or ""):
            assert "\n⑧" not in c.chunk_text and not c.chunk_text.startswith("⑧"), \
                "独立 ⑧ 标注不应出现在 3.1 卡正文"


# ──────────────────────────── RC2：混合编号继承 ────────────────────────────

def _chunk_steps(blocks):
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        blocks, "T_MIX", 1, metadata={"title": "t.pdf"})
    return [c for c in chunks if c.chunk_type == "step_card"
            and not c.extra.get("is_step_continuation")]


MIX_BLOCKS = [
    {"block_type": "paragraph", "text": "步骤3： 3.1 进入U8系统的“扫码报检”界面操作。",
     "page_num": 2},
    {"block_type": "heading", "text": "3.2 进入扫描报检页面后，扫描《交货单》（图⑧）；",
     "level": 3, "page_num": 2},
    {"block_type": "paragraph", "text": "步骤4：报检（根据交货单信息填相对应数据）；",
     "page_num": 3},
    {"block_type": "paragraph", "text": "4.1 按《交货单》填写设备、班次、数量的信息记录；",
     "page_num": 3},
    {"block_type": "heading", "text": "4.2 填写完后，依次点击根据设备带出班组人员完成报检；",
     "level": 3, "page_num": 3},
    {"block_type": "paragraph", "text": "步骤5：发送到群通知统计报检完成。", "page_num": 3},
]


def test_mixed_numbering_inherits_explicit_main_no():
    """步骤3 下的 3.2、步骤4 下的 4.1/4.2 继承显式主号（不再 0 / ordinal 跳号）。"""
    steps = _chunk_steps(MIX_BLOCKS)
    got = [(c.extra.get("step_no"), c.extra.get("section_no")) for c in steps]
    assert got == [(3, None), (3, "3.2"), (4, None), (4, "4.1"), (4, "4.2"), (5, None)], got
    subs = [c.extra.get("sub_step_no") for c in steps]
    assert subs == [None, 2, None, 1, 2, None], subs


def test_mixed_numbering_major_mismatch_keeps_old_behavior():
    """主号与当前显式主步骤不一致（步骤3 下出现 7.2）：不继承。"""
    blocks = [
        {"block_type": "paragraph", "text": "步骤3：进入系统界面操作流程说明。", "page_num": 1},
        {"block_type": "heading", "text": "7.2 这是一个编号漂移的条款标题行；",
         "level": 3, "page_num": 1},
    ]
    steps = _chunk_steps(blocks)
    assert [(c.extra.get("step_no"), c.extra.get("section_no")) for c in steps] == \
        [(3, None), (0, "7.2")]


def test_pure_xy_numbering_doc_unchanged():
    """纯 X.Y 编号文档（无显式 步骤N）：仍走 ordinal，不因巧合继承塌号。"""
    blocks = [
        {"block_type": "paragraph", "text": "1.1 打开U8输入账号密码并选择对应的账套。", "page_num": 1},
        {"block_type": "paragraph", "text": "1.2 进入系统业务导航选择供应链销售管理。", "page_num": 1},
        {"block_type": "paragraph", "text": "1.3 得到销售发票列表后导出到桌面文件。", "page_num": 1},
    ]
    steps = _chunk_steps(blocks)
    assert [c.extra.get("step_no") for c in steps] == [1, 2, 3], \
        "纯 X.Y 文档的 ordinal 行为必须保持（防整篇塌到同一 step_no）"


def test_finance_manual_numbered_heading_keeps_section():
    """财务手册模式（全篇编号标题、无显式 步骤N、无结构性标题兜底）：
    编号操作标题必须照常更新 section_title——它是 embedding 章节前缀与
    兄弟扩展展示的唯一语境（2026-06-11 对抗评审确认的回归，已修正）。"""
    blocks = [
        {"block_type": "heading", "text": "3.2.4正常单据记账", "level": 3, "page_num": 1,
         "section_path": "3.2.4正常单据记账"},
        {"block_type": "paragraph",
         "text": "1. 点击记账按钮，系统弹出记账确认窗口后核对凭证信息。", "page_num": 1},
    ]
    steps = _chunk_steps(blocks)
    target = next(c for c in steps if "点击记账按钮" in c.chunk_text)
    assert (target.section_title or "") == "3.2.4正常单据记账", target.section_title


def test_non_pdf_circled_paragraph_not_suppressed():
    """圈号降噪只认 pdf_extractor 的 circled_label 几何证据标志：
    非 PDF 来源（DOCX 等）的单圈号段落不应被无条件吞掉。"""
    blocks = [
        {"block_type": "paragraph", "text": "步骤1：收取交货单并与标识卡核对数量信息。",
         "page_num": 1},
        {"block_type": "paragraph", "text": "④", "page_num": 1},   # 无 circled_label 标志
    ]
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        blocks, "T_DOCX", 1, metadata={"title": "t.docx"})
    step1 = next(c for c in chunks if c.chunk_type == "step_card")
    assert "④" in step1.chunk_text, "非 PDF 圈号段落被误吞"


def test_step_like_heading_does_not_pollute_section():
    """步骤型标题（1.3 …）不渗透为后续步骤卡的 section_title；结构性标题正常更新。"""
    blocks = [
        {"block_type": "heading", "text": "三、审核流程", "level": 2, "page_num": 1,
         "section_path": "三、审核流程"},
        {"block_type": "paragraph", "text": "步骤1：收取交货单并与标识卡核对数量信息。", "page_num": 1},
        {"block_type": "heading", "text": "1.3  按下列数据抄录订单信息，用于后续跟踪；",
         "level": 3, "page_num": 1},
        {"block_type": "paragraph", "text": "步骤2：交货单按分类放置四堆按顺序依次放置。", "page_num": 2},
    ]
    steps = _chunk_steps(blocks)
    step2 = next(c for c in steps if "分类放置" in c.chunk_text)
    assert "1.3" not in (step2.section_title or ""), \
        f"步骤型标题渗透为 section_title: {step2.section_title!r}"
    assert (step2.section_title or "").startswith("三、"), step2.section_title
