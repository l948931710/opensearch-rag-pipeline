# -*- coding: utf-8 -*-
"""PDF-D2（2026-07-25）：段落块的 y 包络不得跨栏膨胀，否则图↔步骤绑定整组错位。

故障链（FL-XS-WI-007 p2 实证，本文件用合成夹具 hermetic 复现）：
  ① 页眉最后一行落在 10% 候选带外 ⇒ 不进页眉候选；但它跨在 crop_top 上 ⇒
     pdfplumber 裁剪保留该词并把 top **钳到 crop_top**（假 y）；
  ② 双栏页阅读序 = 左栏整栏 → 右栏整栏 ⇒ 左栏末行到右栏首行的 gap 为**负**；
  ③ 旧判据 `(top - prev_bottom) > 40` 只认向下 ⇒ 不切段 ⇒ Step 4 的块 y 包络
     膨胀成 [95, 507]（罩住整页）；
  ④ 图片按「与锚点块 y 正重叠最大」锚定 ⇒ 整页图塌到 Step 4，落在包络外的图
     反被甩给 Step 3 —— 两个步骤的图集对调。

三层断言：L1 钉几何前提（证明夹具真触发了故障要素，而非碰巧过）、L2 钉分块、
L3 钉端到端 image_refs。L3 在旧判据下必然失败（Step4 会拿到两张图）。
"""
import os

import pytest

pdfplumber = pytest.importorskip("pdfplumber")


@pytest.fixture(autouse=True)
def _content_override_off(monkeypatch):
    """固定姿态：该开关 2026-07-25 起默认 ON。本文件测的是几何锚定，与内容覆写无关，
    显式关掉以免夹具日后增强（补 OCR/caption）后语义漂移。"""
    monkeypatch.setenv("RAG_IMAGE_CONTENT_OVERRIDE", "0")

from opensearch_pipeline.extraction.pdf_extractor import (  # noqa: E402
    _detect_column_split,
    _extract_with_pdfplumber,
    _pass1_analyze,
    extract_pdf,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "two_column_header_straddle.pdf")

# ── 夹具的实测几何常量（make_fixtures.make_two_column_header_straddle_pdf 生成）──
# 改夹具坐标必须同步改这里；它们是"故障要素确实存在"的证据，不是可有可无的数字。
CROP_TOP = 95.0                 # = max(页眉候选 rounded top=80) + 15
STRADDLE_TEXT = "Effective"     # 跨 crop_top 的页眉残词（top 92.07 < 95 < bottom 102.07）
STEP4_Y = (497.07, 507.07)      # 修好后 Step 4 块应有的 y 区间
REMNANT_Y = (95.0, 102.07)      # 页眉残词单独成块后的 y 区间
TOL = 0.5

# 零文本损失 oracle：本判据只改分块边界，绝不增删字符。
# 这两个常量在**旧判据下实测同值**（2026-07-25），所以它们能证明"没丢字"，
# 而不只是记录了新实现的输出。
EXPECTED_CHARS = 414            # 全文非空白字符数（两页）
EXPECTED_TEXT_P1 = (
    "Step 3 open the U8 scan screen follow the marked buttons "
    "the operator confirms the label then the quantity is checked "
    "Step 4 aim the scanner at the code Effective Date 2022-08-19 "
    "click here open the app double click enter code confirm now close window"
)


def _blocks(page_num=1):
    blocks, _npages, _warns = _extract_with_pdfplumber(FIXTURE, max_pages=10)
    return [b for b in blocks if b.page_num == page_num]


def _para_y(blocks, needle):
    """按文本片段找 paragraph 块，返回 (y0, y1)；找不到返回 None。"""
    for b in blocks:
        if b.block_type == "paragraph" and needle in (b.text or ""):
            e = b.extra or {}
            return (e.get("y0"), e.get("y1"))
    return None


# ── L1：几何前提（夹具真的复现了故障要素）────────────────────────────────────

def test_fixture_exists():
    assert os.path.exists(FIXTURE), "夹具缺失，跑 python tests/fixtures/make_fixtures.py 重生成"


def test_l1_header_crop_top_is_as_measured():
    with pdfplumber.open(FIXTURE) as pdf:
        analysis, _ = _pass1_analyze(pdf, max_pages=10)
    assert analysis.header_y_max == CROP_TOP, (
        f"页眉裁剪线漂了（{analysis.header_y_max} != {CROP_TOP}）——夹具或页眉检测被改动，"
        "故障要素①可能已不成立，本文件其余断言随之失去意义")


def test_l1_header_remnant_straddles_crop_line_and_gets_clamped():
    """跨线残词必须①存在于原页、②裁剪后仍在、③top 被钳到 crop_top。"""
    with pdfplumber.open(FIXTURE) as pdf:
        page = pdf.pages[0]
        straddling = [w for w in page.extract_words()
                      if w["text"] == STRADDLE_TEXT and w["top"] < CROP_TOP < w["bottom"]]
        assert straddling, f"{STRADDLE_TEXT!r} 未跨 crop_top —— 故障要素① 不成立"
        cropped = page.crop((0, CROP_TOP, page.width, page.height))
        clamped = [w for w in cropped.extract_words()
                   if w["text"] == STRADDLE_TEXT and abs(w["top"] - CROP_TOP) < 0.01]
        assert clamped, "裁剪后残词消失或未被钳位 —— pdfplumber 行为变了，需重新评估本修复"


def test_l1_page_is_detected_as_two_column():
    """故障要素②：必须真被判为双栏，否则负 gap 不会出现。"""
    with pdfplumber.open(FIXTURE) as pdf:
        page = pdf.pages[0]
        split_x = _detect_column_split(page.extract_words(), page.width)
    assert split_x is not None, "夹具未触发双栏检测（需 ≥30 词 / 两侧各 ≥25% / 纵向重叠 ≥60%）"


# ── L2：分块 ────────────────────────────────────────────────────────────────

def test_l2_step4_and_header_remnant_are_separate_blocks():
    """核心断言：负 gap（跨栏 + 钳位假 y）必须切段。"""
    blocks = _blocks()
    step4 = _para_y(blocks, "Step 4 aim the scanner")
    remnant = _para_y(blocks, "Effective Date")
    assert step4 is not None and remnant is not None
    assert step4 != remnant, "Step 4 与页眉残词仍在同一块 —— 负 gap 未切段（PDF-D2 回归）"
    assert abs(step4[0] - STEP4_Y[0]) < TOL and abs(step4[1] - STEP4_Y[1]) < TOL, \
        f"Step 4 块 y 区间 {step4} 偏离预期 {STEP4_Y}"
    assert abs(remnant[0] - REMNANT_Y[0]) < TOL and abs(remnant[1] - REMNANT_Y[1]) < TOL, \
        f"残词块 y 区间 {remnant} 偏离预期 {REMNANT_Y}"


def test_l2_no_paragraph_spans_the_whole_page():
    """膨胀包络的直接否定：旧判据下 Step 4 块跨度 412pt。"""
    spans = [(b.extra or {}).get("y1", 0) - (b.extra or {}).get("y0", 0)
             for b in _blocks() if b.block_type == "paragraph"
             and (b.extra or {}).get("y0") is not None]
    assert spans and max(spans) < 40, \
        f"存在跨度 {max(spans):.1f}pt 的段落块——本夹具每段最多两行(~25pt)，超出即包络膨胀"


def test_l2_forward_gap_still_splits():
    """防止把判据改坏成"只认负 gap"：正向 >40pt 间隙照旧切段。

    夹具里这两行相距 50pt，且都不是 step/heading/圈号 —— 唯一能切开它们的就是
    正向大间隙分支。
    """
    a = _para_y(_blocks(), "the operator confirms")
    b = _para_y(_blocks(), "then the quantity is checked")
    assert a is not None and b is not None and a != b, "正向大间隙分支失效"


def test_l2_column_reading_order_preserved():
    """双栏「左栏整栏 → 右栏整栏」的既有语义不得被本修复改动。"""
    blocks = _blocks()
    order = [i for i, b in enumerate(blocks) if b.block_type == "paragraph"]
    idx = {}
    for i in order:
        t = blocks[i].text or ""
        for key in ("Step 3", "then the quantity", "Step 4", "click here", "close window"):
            if key in t and key not in idx:
                idx[key] = i
    assert idx["Step 3"] < idx["then the quantity"] < idx["Step 4"], "左栏内部顺序被打乱"
    assert idx["Step 4"] < idx["click here"] < idx["close window"], \
        "右栏块未整体排在左栏之后 —— 双栏阅读序被破坏"


def test_l2_no_text_lost_or_gained():
    """零文本损失 oracle（冻结常量，不依赖"改动前"的实现）。"""
    import collections
    blocks, _n, _w = _extract_with_pdfplumber(FIXTURE, max_pages=10)
    alltext = "".join(b.text or "" for b in blocks)
    assert len("".join(alltext.split())) == EXPECTED_CHARS, "全文非空白字符数变了"
    p1 = " ".join(" ".join((b.text or "").split())
                  for b in blocks if b.page_num == 1).strip()
    assert p1 == EXPECTED_TEXT_P1, f"page1 规范化全文与 oracle 不符:\n{p1!r}"
    # 字符多重集（顺序无关的兜底）
    assert collections.Counter("".join(EXPECTED_TEXT_P1.split())) == \
        collections.Counter("".join(p1.split()))


# ── L3：端到端 image_refs（旧判据下必然失败）──────────────────────────────────

def _fake_assets():
    """伪 assets：只给 heuristic 消费的契约字段，不需要真实图片/VLM。

    图 7 的 bbox 落在 Step 3 块附近；图 8 落在 Step 4 块附近。
    旧判据下 Step 4 的块是 [95, 507]，与图 7 的重叠(45pt) 大于 Step 3 的(18pt)，
    图 7 因此被吸走 —— 这正是要钉死的错绑。
    """
    return [
        {"status": "ROUTE_TO_VECTOR", "page_num": 1, "image_index": 7,
         "bbox": [50.0, 130.0, 300.0, 175.0], "filename": "a.png"},
        {"status": "ROUTE_TO_VECTOR", "page_num": 1, "image_index": 8,
         "bbox": [50.0, 500.0, 300.0, 520.0], "filename": "b.png"},
    ]


def _step_cards():
    from opensearch_pipeline.chunker import DocumentChunker
    from opensearch_pipeline.pipeline_nodes import _inject_image_ref_blocks

    blocks, _n, _w = extract_pdf(FIXTURE, max_pages=10)
    enriched = _inject_image_ref_blocks(
        list(blocks), _fake_assets(),
        {"doc_id": "FX", "version_no": 1, "source_key": "raw/eval/fx", "owner_dept": "eval"})
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        blocks=enriched, doc_id="FX", version_no=1,
        metadata={"title": "FL-TEST-001 work instruction"})
    out = {}
    for c in chunks:
        extra = getattr(c, "extra", None) or {}
        if getattr(c, "chunk_type", None) != "step_card":
            continue
        refs = [r.get("image_index") for r in (extra.get("image_refs") or [])]
        if refs:                      # 续接卡不复制 image_refs，只取带图的主卡
            out[extra.get("step_no")] = sorted(refs)
    return out


def test_l3_images_bind_to_the_positionally_correct_step():
    cards = _step_cards()
    assert cards.get(3) == [7], (
        f"图 7 未绑到 Step 3（实得 {cards.get(3)}）—— 段落块 y 包络膨胀把它吸给了 Step 4")
    assert cards.get(4) == [8], (
        f"图 8 未绑到 Step 4（实得 {cards.get(4)}）")


def test_l3_no_image_bound_to_two_steps():
    cards = _step_cards()
    seen = [i for refs in cards.values() for i in refs]
    assert len(seen) == len(set(seen)), f"同一张图绑到多个步骤: {cards}"
