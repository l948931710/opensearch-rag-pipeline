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


# ──────────── Path C 收紧：vlm_annotation_map.keys() 不再算圈号源 ──────────────


def test_path_c_ignores_vlm_annotation_map_keys():
    """Path C range-ref override 必须只信 OCR 真实印出的圈号,不能用
    vlm_annotation_map.keys() 中的 ①-⑥(VLM 标的"图内区域位置编号",
    与 step text 的"②-⑥步操作"sub-step 引用是不同语义)。

    pdf_sop image 9 复现:OCR 空 + annotation_map={'①':'左侧导航栏',...,'⑥':'右侧弹窗'}
    几何 anchor 在 step 4.2 area。step 3.1 文本含 "②-⑥步操作" range。
    旧 Path C 触发 → image 9 错移到 step 3.1。
    新 Path C(只信 OCR):img_cir_nums={} → 不触发 → image 留 step 4.2 ✓
    """
    import os
    blocks = [
        _para("步骤3： 3.1 进入U8系统的“扫码报检”界面（如下图②-⑥步操作）。", 2, 100, 130),
        _para("步骤4：报检（根据交货单信息在U8扫码报检处填相对应数据）；", 3, 50, 80),
        _para("4.2 填写完后，依次点击根据设备带出班组人员完成报检；", 3, 200, 230),
    ]
    asset_image_9_like = {
        "image_index": 9, "page_num": 3, "bbox": [42, 240, 500, 400],
        "status": "ROUTE_TO_VECTOR", "filename": "img9.jpeg",
        "oss_key": "k9", "ocr_text": "",
        "visual_summary": "U8系统‘档案查询’界面截图，左侧为功能导航栏",
        "vlm_annotation_map": {
            "①": "左侧功能导航栏", "②": "品名下拉框",
            "③": "数量与单位输入框", "④": "检索结果数据表格",
            "⑤": "结果表格中的具体记录行", "⑥": "右侧ItemSelectForm选择窗口",
        },
    }
    old_env = os.environ.get("RAG_IMAGE_CONTENT_OVERRIDE")
    os.environ["RAG_IMAGE_CONTENT_OVERRIDE"] = "1"
    try:
        enriched = _inject_image_ref_blocks([dict(b) for b in blocks],
                                             [dict(asset_image_9_like)], dict(DOC))
        chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
            enriched, "T_ANN_MAP", 1, metadata={"title": "t.pdf"})
        for c in chunks:
            if c.chunk_type != "step_card":
                continue
            bound = {r.get("image_index") for r in c.extra.get("image_refs", [])}
            if "②-⑥步操作" in (c.chunk_text or ""):
                assert 9 not in bound, (
                    f"Path C 错触发: image 9(OCR 空+annotation_map ①-⑥)"
                    f"被错移到 step 3.1。step 3 chunk bound={bound}"
                )
    finally:
        if old_env is None:
            os.environ.pop("RAG_IMAGE_CONTENT_OVERRIDE", None)
        else:
            os.environ["RAG_IMAGE_CONTENT_OVERRIDE"] = old_env


def test_path_c_still_works_with_real_ocr_circled():
    """Path C 收紧后,OCR 真实印有圈号 ≥2 的 image 仍可触发跨页救援。
    这是 Path C 的 intended use case,不能被收紧误伤。

    场景:某 image OCR 含 "②③④" 真实圈号(图上印的引用),
    几何 anchor 错位,step text 含 "②-⑤步" range → Path C 应当救活。
    """
    import os
    blocks = [
        _para("步骤3：进入扫码报检界面操作 ②-⑤ 步,如下图所示完成填表。", 2, 100, 130),
        _para("步骤4：备注 — 报检后核对。", 3, 50, 80),
    ]
    asset = {
        "image_index": 5, "page_num": 3, "bbox": [42, 90, 500, 200],
        "status": "ROUTE_TO_VECTOR", "filename": "img5.jpeg",
        "oss_key": "k5",
        "ocr_text": "② 扫码区 ③ 输入货号 ④ 选择班次 ⑤ 点击报检",
        "visual_summary": "U8 扫码报检界面操作流程图,显示扫码、输入、选择、点击四步",
        "vlm_annotation_map": {},
    }
    old_env = os.environ.get("RAG_IMAGE_CONTENT_OVERRIDE")
    os.environ["RAG_IMAGE_CONTENT_OVERRIDE"] = "1"
    try:
        enriched = _inject_image_ref_blocks([dict(b) for b in blocks],
                                             [dict(asset)], dict(DOC))
        chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
            enriched, "T_REAL_OCR", 1, metadata={"title": "t.pdf"})
        for c in chunks:
            if c.chunk_type != "step_card":
                continue
            bound = {r.get("image_index") for r in c.extra.get("image_refs", [])}
            if "②-⑤" in (c.chunk_text or ""):
                assert 5 in bound, (
                    f"Path C 真 OCR 圈号 case 应救活: step 3 bound={bound}"
                )
                return
    finally:
        if old_env is None:
            os.environ.pop("RAG_IMAGE_CONTENT_OVERRIDE", None)
        else:
            os.environ["RAG_IMAGE_CONTENT_OVERRIDE"] = old_env


# ──────────── Path D: image cluster propagation (8 约束验证) ──────────────


import os as _os  # noqa: E402


class _PathDCtx:
    """Path D test 共用 context: set RAG_IMAGE_CONTENT_OVERRIDE=1。"""
    def __enter__(self):
        self.old = _os.environ.get("RAG_IMAGE_CONTENT_OVERRIDE")
        _os.environ["RAG_IMAGE_CONTENT_OVERRIDE"] = "1"
        return self
    def __exit__(self, *a):
        if self.old is None:
            _os.environ.pop("RAG_IMAGE_CONTENT_OVERRIDE", None)
        else:
            _os.environ["RAG_IMAGE_CONTENT_OVERRIDE"] = self.old


def _path_d_doc():
    """构造 xs_wi_007 image 1+2 复现拓扑(step 1 + step 2 + image 1/2 在 step 2 area)."""
    return {"doc_id": "T_PATH_D", "version_no": 1, "title": "t.pdf"}


def _path_d_blocks():
    """复现 xs_wi_007 page 1 真实拓扑: step 1 起始(top) + step 2 延续段
    (image_1 几何 anchor 落到该非 step 起始块) + step 2 起始(在 image 2 下方)。
    Path A 凭 image_1 OCR'产品标识卡' bigram 救活到 step 1; Path D 让 image 2
    在同页/邻接 image_index/共享 P325 token 时跟随 image 1 到 step 1。
    geo 延续段刻意避开'产品/机台/数量/标识'等 image_1 OCR 关键词,确保
    alt_score/geo_score ≥ 3.0x 真触发 Path A。"""
    return [
        _para("步骤1：按《产品标识卡》清点实货,抄录定单号机台号组号数量到纸上;", 1, 100, 200),
        _para("另需参考前述时间安排细则,可见相关章节附注说明;", 1, 250, 320),
        _para("步骤2：每天上午9点左右,向各区班长收集《交货单》,核对抄录信息;", 1, 480, 500),
    ]


def _seed_asset(idx, page, bbox, ocr="", vsum="",
                image_index=None, am=None):
    """image asset with required fields."""
    return {
        "image_index": image_index if image_index is not None else idx,
        "page_num": page, "bbox": bbox,
        "status": "ROUTE_TO_VECTOR", "filename": f"img{idx}.jpeg",
        "oss_key": f"k{idx}", "ocr_text": ocr,
        "visual_summary": vsum, "vlm_annotation_map": am or {},
    }


def test_path_d_positive_propagation_image_2_follows_image_1():
    """正例: image 1(强 Path A) + image 2(同页/邻接 index/相邻 bbox/共享 P325 token)
    → image 2 跟随 image 1 到 step 1。provenance 写 route_reason=cluster_propagation。
    """
    blocks = _path_d_blocks()
    image_1 = _seed_asset(
        1, 1, [42, 216, 202, 395],
        ocr="台州富岭塑胶有限公司 产品标识卡(包装车专用) 对应定单号:2326 包装日期:8.25 货号产品规格:3.25*125杯 50*50 机台号组号:107# 221# 数量:4个 P325-PP",
        vsum="台州富岭塑胶有限公司产品标识卡(包装车间专用),含手册号、材料配比、订单号2326")
    image_2 = _seed_asset(
        2, 1, [42, 403, 342, 457],
        ocr="2336 P325-PPB D07(38) 22 7 +45",
        vsum="手写文本'2336 P325-PPB D07(38) 22 7 +45',疑似产品编号、批次或工艺参数记录")
    with _PathDCtx():
        enriched = _inject_image_ref_blocks([dict(b) for b in blocks],
                                             [dict(image_1), dict(image_2)], _path_d_doc())
    asset_2 = next(b for b in enriched if b.get("block_type") == "image_ref"
                   and (b.get("extra") or {}).get("image_index") == 2)
    assert asset_2["extra"].get("route_reason") == "cluster_propagation", \
        f"image 2 应被 Path D 传播,实 route_reason={asset_2['extra'].get('route_reason')}"
    assert asset_2["extra"].get("route_seed_image_index") == 1
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        enriched, "T_PATH_D", 1, metadata={"title": "t.pdf"})
    step1 = next(c for c in chunks if c.chunk_type == "step_card" and "产品标识卡" in c.chunk_text)
    bound = {r.get("image_index") for r in step1.extra.get("image_refs", [])}
    assert {1, 2}.issubset(bound), f"image 1+2 应都在 step 1, 实 bound={bound}"


def test_path_d_negative_no_shared_token_blocks_propagation():
    """负例 1: 同页相邻 + image_index delta=1, 但无高熵 token 共享 → 不传播。"""
    blocks = _path_d_blocks()
    image_1 = _seed_asset(
        1, 1, [42, 216, 202, 395],
        ocr="台州富岭塑胶有限公司 产品标识卡 对应定单号:2326 P325-PP",
        vsum="台州富岭塑胶产品标识卡(包装车间专用),含订单号2326")
    image_2 = _seed_asset(
        2, 1, [42, 403, 342, 457],
        ocr="完全不相关 XYZ9999 AAAA",
        vsum="完全不相关的随机内容")
    with _PathDCtx():
        enriched = _inject_image_ref_blocks([dict(b) for b in blocks],
                                             [dict(image_1), dict(image_2)], _path_d_doc())
    asset_2 = next(b for b in enriched if b.get("block_type") == "image_ref"
                   and (b.get("extra") or {}).get("image_index") == 2)
    assert asset_2["extra"].get("route_reason") != "cluster_propagation"


def test_path_d_negative_common_chinese_word_not_shared_token():
    """负例 2: 仅"产品/记录"通用中文词共享(非高熵字母数字 token)→ 不传播。"""
    blocks = _path_d_blocks()
    image_1 = _seed_asset(
        1, 1, [42, 216, 202, 395],
        ocr="台州富岭塑胶有限公司 产品标识卡 对应定单号:2326 P325-PP",
        vsum="台州富岭塑胶产品标识卡(包装车间专用),含订单号2326 产品记录")
    image_2 = _seed_asset(
        2, 1, [42, 403, 342, 457],
        ocr="产品记录信息",
        vsum="产品记录信息表单")
    with _PathDCtx():
        enriched = _inject_image_ref_blocks([dict(b) for b in blocks],
                                             [dict(image_1), dict(image_2)], _path_d_doc())
    asset_2 = next(b for b in enriched if b.get("block_type") == "image_ref"
                   and (b.get("extra") or {}).get("image_index") == 2)
    assert asset_2["extra"].get("route_reason") != "cluster_propagation", \
        "仅中文通用词不应当作高熵 token 共享触发传播"


def test_path_d_negative_follower_has_strong_self_evidence():
    """负例 3: follower 自身 OCR > 200 chars 自带强证据 → 不传播。"""
    blocks = _path_d_blocks()
    image_1 = _seed_asset(
        1, 1, [42, 216, 202, 395],
        ocr="台州富岭塑胶有限公司 产品标识卡 对应定单号:2326 P325-PP",
        vsum="台州富岭塑胶产品标识卡(包装车间专用),含订单号2326")
    image_2 = _seed_asset(
        2, 1, [42, 403, 342, 457],
        ocr="P325-PPB " + "X" * 220,
        vsum="follower 自带 long OCR")
    with _PathDCtx():
        enriched = _inject_image_ref_blocks([dict(b) for b in blocks],
                                             [dict(image_1), dict(image_2)], _path_d_doc())
    asset_2 = next(b for b in enriched if b.get("block_type") == "image_ref"
                   and (b.get("extra") or {}).get("image_index") == 2)
    assert asset_2["extra"].get("route_reason") != "cluster_propagation"


def test_path_d_negative_two_seeds_conflict_fails_closed():
    """负例 4: 两个 strong seed 同时提案传给一个 follower → fail-closed 不传播。"""
    blocks = [
        _para("步骤1: 按产品标识卡清点实货,抄录定单号", 1, 100, 130),
        _para("步骤3: 进入U8系统扫码报检界面操作", 1, 460, 490),
    ]
    image_1 = _seed_asset(
        1, 1, [42, 100, 202, 200],
        ocr="台州富岭塑胶有限公司 产品标识卡 对应定单号:2326 P325-PP",
        vsum="产品标识卡(包装车间专用)")
    image_3 = _seed_asset(
        3, 1, [42, 460, 202, 560],
        ocr="用友U8 P325-PP 扫码报检界面 已扫条码",
        vsum="用友U8系统扫码报检界面截图,显示扫码条码区")
    image_2 = _seed_asset(
        2, 1, [42, 280, 342, 380],
        ocr="P325-PPB 2336",
        vsum="手写P325-PPB 工艺参数")
    with _PathDCtx():
        enriched = _inject_image_ref_blocks([dict(b) for b in blocks],
                                             [dict(image_1), dict(image_2), dict(image_3)], _path_d_doc())
    asset_2 = next(b for b in enriched if b.get("block_type") == "image_ref"
                   and (b.get("extra") or {}).get("image_index") == 2)
    rr = asset_2["extra"].get("route_reason")
    if rr == "cluster_propagation":
        assert False, "两 seed 竞争 follower 应 fail-closed,实际传播了 (seed=%s)" % \
            asset_2["extra"].get("route_seed_image_index")


def test_path_d_negative_image_index_delta_not_1_blocks():
    """负例 5: image_index delta != 1(如 1 与 3)→ 不传播,即使其他条件全满足。"""
    blocks = _path_d_blocks()
    image_1 = _seed_asset(
        1, 1, [42, 216, 202, 395],
        ocr="台州富岭塑胶有限公司 产品标识卡 对应定单号:2326 P325-PP",
        vsum="产品标识卡(包装车间专用)")
    image_3 = _seed_asset(
        3, 1, [42, 403, 342, 457],
        ocr="P325-PPB 2336",
        vsum="手写P325-PPB 工艺参数",
        image_index=3)
    with _PathDCtx():
        enriched = _inject_image_ref_blocks([dict(b) for b in blocks],
                                             [dict(image_1), dict(image_3)], _path_d_doc())
    asset_3 = next(b for b in enriched if b.get("block_type") == "image_ref"
                   and (b.get("extra") or {}).get("image_index") == 3)
    assert asset_3["extra"].get("route_reason") != "cluster_propagation", \
        "image_index delta != 1 应阻止传播"


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


# ──────────────────────── DOCX 文本框抽取（w:txbxContent）────────────────────────

def test_docx_textbox_texts_extraction():
    """para.text 不含文本框文字；_textbox_texts 应取回，并对 AlternateContent
    的 Choice/Fallback 双份去重、丢弃纯圈号标注框（FL-XS-WI-005 实证缺口）。"""
    from lxml import etree
    from opensearch_pipeline.extraction.docx_extractor import _textbox_texts
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = f'''<w:p xmlns:w="{W}">
      <w:r><w:t>锚段落自身文字</w:t></w:r>
      <w:r><w:drawing>
        <w:txbxContent>
          <w:p><w:r><w:t>在电脑桌面打开U8</w:t></w:r></w:p>
          <w:p><w:r><w:t>输入密码登录</w:t></w:r></w:p>
        </w:txbxContent>
        <w:txbxContent>
          <w:p><w:r><w:t>在电脑桌面打开U8</w:t></w:r></w:p>
          <w:p><w:r><w:t>输入密码登录</w:t></w:r></w:p>
        </w:txbxContent>
      </w:drawing></w:r>
      <w:r><w:drawing><w:txbxContent>
        <w:p><w:r><w:t>④</w:t></w:r></w:p>
      </w:txbxContent></w:drawing></w:r>
    </w:p>'''
    texts = _textbox_texts(etree.fromstring(xml))
    assert texts == ["在电脑桌面打开U8\n输入密码登录"], texts
