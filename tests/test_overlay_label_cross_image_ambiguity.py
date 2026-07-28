# -*- coding: utf-8 -*-
"""PDF-D4/D5（2026-07-25）：重复圈号不可当图号，且判据必须建在文本层。

1b 命中会**压过几何**，所以它只该接受高精度的 figure-ID 证据：**仅页内唯一出现**的
圈号才算图号；重复出现即 fail-closed，回落几何。这是"可采信门"，不是"两次出现必然
指向不同图"的数学推导（两个点可能都在同一张图内）。

**D4（第一版）判据是"几张存活图各自认领它"——那是 VLM 判断的函数，已被 D5 取代。**
实证 FL-ZS-WI-009 p1：三个游离 ① 中两个分别落在 image 2 与 image 5 内 ⇒ 判歧义、
image 5 按几何正确归步骤3；但空缓存重掷 caption 后 **image 2 被漏斗丢弃**，① 只剩
image 5 认领 ⇒ 歧义判定失效 ⇒ image 5 又被拖回步骤2（正文"（图①）"在那）。
同两次抽取里，标记的页级计数逐字相同、存活图集却不同 —— 所以 **D5 把判据改建在
label_points（pdf_extractor 的 circled_label 块 = 纯文本层）的页级出现次数上**。

⚠️ 本守门只消除"重复圈号歧义判定"对存活 asset 集的依赖，**不**等于 1b 已
caption-independent：1b 仍只收 ROUTE_TO_VECTOR/TEXT 的资产，且 map/summary 两级证据
本身就是 VLM 产物。

判据口径（本文件逐条钉死）：
  · 计数只数页级标记出现次数，与哪些图存活无关；
  · 判歧义后该字符对该页 overlay / vlm_annotation_map / visual_summary 三源全弃；
  · 先过滤歧义字符、再做各源 len(...)==1 唯一性判断。
"""
import pytest

from opensearch_pipeline.chunker import DocumentChunker
from opensearch_pipeline.pipeline_nodes import _inject_image_ref_blocks

DOC = {"doc_id": "T_D4", "version_no": 1, "title": "t.pdf"}


@pytest.fixture(autouse=True)
def _content_override_off(monkeypatch):
    """隔离 1b/1b-2：本文件只测圈号归属与几何回落，Path A/B/C/D 必须关掉。

    **必须显式设 "0"**：2026-07-25 起该开关默认 ON，`delenv` 等于开着 —— 那样本文件
    测的就不是它自称的那一侧了。
    """
    monkeypatch.setenv("RAG_IMAGE_CONTENT_OVERRIDE", "0")


def _para(text, page, y0, y1):
    return {"block_type": "paragraph", "text": text, "page_num": page,
            "extra": {"y0": y0, "y1": y1}}


def _label(char, page, x0, x1, y0, y1):
    return {"block_type": "paragraph", "text": char, "page_num": page,
            "extra": {"detected_by": "circled_label", "circled_label": char,
                      "x0": x0, "x1": x1, "y0": y0, "y1": y1}}


def _asset(idx, page, bbox, ocr="", summary="", ann_map=None):
    return {"image_index": idx, "page_num": page, "bbox": bbox,
            "status": "ROUTE_TO_VECTOR", "filename": f"img{idx}.jpeg",
            "oss_key": f"k{idx}", "ocr_text": ocr, "visual_summary": summary,
            "vlm_annotation_map": ann_map or {}}


# FL-ZS-WI-009 p1 的最小同构：步骤2 引用「图①」在上，步骤3 在下；
# 两张不重叠的图各含一个独立 ① 标记点，且图B 与步骤3 的 y 区间有重叠。
def _blocks():
    return [
        _para("步骤2： 根据《发货单》(图①）打印纸质版发货单3份；", 1, 340, 366),
        _para("步骤3： 进入U8系统的“货位存量查询”界面，输入商检号确认现货数量；", 1, 542, 609),
    ]


def _imgs_by_step(chunks):
    out = {}
    for c in chunks:
        if c.chunk_type != "step_card":
            continue
        txt = c.chunk_text or ""
        key = "A" if "打印纸质版发货单" in txt else ("B" if "货位存量查询" in txt else None)
        if key and key not in out:
            out[key] = {r.get("image_index") for r in (c.extra.get("image_refs") or [])}
    return out


def _run(assets, blocks=None):
    enriched = _inject_image_ref_blocks([dict(b) for b in (blocks or _blocks())],
                                        [dict(a) for a in assets], dict(DOC))
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        enriched, "T_D4", 1, metadata={"title": "t.pdf"})
    return _imgs_by_step(chunks)


# ── 新故障形态：同字符 + 不同标记点 + 不同图 ────────────────────────────────

def test_same_char_claimed_by_two_images_is_not_a_figure_id():
    """两张不重叠的图各含一个 ① ⇒ ① 不能指图，两张都不得被拖到「图①」所在的步骤A。

    图B（y 592-720）与步骤B（y 542-609）重叠 ⇒ 几何回落必须把它归到步骤3。
    """
    assets = [
        _asset(2, 1, [43, 432, 218, 536]),      # 图A：落在步骤2 与步骤3 之间
        _asset(5, 1, [201, 592, 341, 720]),     # 图B：与步骤3 的 y 区间重叠
    ]
    blocks = _blocks() + [
        _label("①", 1, 185, 196, 460, 474),     # 落在图A 内
        _label("①", 1, 305, 316, 626, 639),     # 落在图B 内
    ]
    got = _run(assets, blocks)
    assert 5 not in got.get("A", set()), "图B 被 ① 错拖到步骤2（PDF-D4 回归）"
    assert got.get("B") == {5}, f"图B 应按几何归步骤3，实得 {got.get('B')}"


@pytest.mark.parametrize("source", ["vlm_annotation_map", "visual_summary"])
def test_ambiguous_char_cannot_be_reintroduced_by_other_sources(source):
    """歧义字符不得经 map / visual_summary 两级回退绕回（两个分支各测一次）。"""
    extra = ({"ann_map": {"①": "箭头"}} if source == "vlm_annotation_map"
             else {"summary": "红色箭头标注区域①，指向查询条件输入框"})
    assets = [
        _asset(2, 1, [43, 432, 218, 536]),
        _asset(5, 1, [201, 592, 341, 720],
               ann_map=extra.get("ann_map"), summary=extra.get("summary", "")),
    ]
    blocks = _blocks() + [
        _label("①", 1, 185, 196, 460, 474),
        _label("①", 1, 305, 316, 626, 639),
    ]
    got = _run(assets, blocks)
    assert 5 not in got.get("A", set()), f"歧义 ① 经 {source} 绕回，图B 又被拖到步骤2"
    assert got.get("B") == {5}


# ── 边界：不得把"同图重复"误判成"跨图歧义" ─────────────────────────────────

def test_duplicate_label_within_one_image_is_also_ambiguous():
    """D5 语义（取代 D4）：同一张图内重复同字符**也**判歧义、三源全弃、回落几何。

    D4 时代这里断言的是相反语义（同图重复不算歧义、仍可经 map 走 1b）。改判的理由是
    确定性：D4 的"几张图认领"是存活 asset 集的函数，而存活与否由漏斗（VLM）决定。
    页级出现次数则是文本层属性，两次抽取逐字相同。
    """
    assets = [
        _asset(5, 1, [201, 592, 341, 720], ann_map={"①": "查询条件"}),
    ]
    blocks = _blocks() + [
        _label("①", 1, 210, 221, 600, 613),     # 同一张图内的两个标记点
        _label("①", 1, 305, 316, 626, 639),
    ]
    got = _run(assets, blocks)
    assert 5 not in got.get("A", set()), (
        f"同图重复 ① 仍被当图号（map 源未被歧义门拦住），实得步骤2={got.get('A')}")
    assert got.get("B") == {5}, f"应回落几何归步骤3，实得 {got.get('B')}"


def test_binding_unchanged_when_a_claimant_asset_disappears():
    """**D5 的核心因果钉子**：owner 资产消失不得改变其余图的绑定。

    同一组 blocks（两个 ①：一个在 image 2 的原 bbox 内、一个在 image 5 内），
    第一次传 assets [2, 5]，第二次只传 [5]（模拟 image 2 被漏斗以新 caption 丢弃）。
    两次都必须：image 5 **不**经 1b 去步骤2，而按几何留在步骤3。

    D4 判据下第二次会失败 —— ① 只剩一个 owner ⇒ 不再判歧义 ⇒ image 5 被拖到步骤2。
    这正是 caption 重掷经漏斗改变绑定的完整链条。
    """
    blocks = _blocks() + [
        _label("①", 1, 185, 196, 460, 474),     # 落在 image 2 的 bbox 内
        _label("①", 1, 305, 316, 626, 639),     # 落在 image 5 的 bbox 内
    ]
    both = _run([_asset(2, 1, [43, 432, 218, 536]),
                 _asset(5, 1, [201, 592, 341, 720])], blocks)
    assert both.get("B") == {5} and 5 not in both.get("A", set())

    # image 2 被漏斗丢弃：blocks 一字未改（文本层不受 caption 影响）
    dropped = _run([_asset(5, 1, [201, 592, 341, 720])], blocks)
    assert 5 not in dropped.get("A", set()), (
        "owner 资产消失后 image 5 又被 ① 拖到步骤2 —— 歧义判据仍依赖存活 asset 集（D5 回归）")
    assert dropped.get("B") == {5}, f"应按几何留在步骤3，实得 {dropped.get('B')}"


def test_same_char_on_different_pages_is_not_ambiguous():
    """跨页隔离：p1 与 p2 各一个 ① 不得互相形成歧义（判据键含 page_num）。"""
    assets = [_asset(5, 1, [201, 592, 341, 720])]
    blocks = _blocks() + [
        _label("①", 1, 305, 316, 626, 639),     # p1 唯一
        _label("①", 2, 100, 111, 200, 213),     # p2 的另一个 ①
    ]
    got = _run(assets, blocks)
    assert got.get("A") == {5}, "跨页的同字符被误算成同页歧义"


def test_unique_owner_label_still_overrides_geometry():
    """正例不回退：单一 owner 的圈号仍走 1b 并压过几何。

    图B 几何上属步骤B，但它是该页唯一拥有 ① 的图 ⇒ 仍应被「图①」拖到步骤2。
    """
    assets = [
        _asset(5, 1, [201, 592, 341, 720]),
    ]
    blocks = _blocks() + [
        _label("①", 1, 305, 316, 626, 639),     # 只有这一个 ①
    ]
    got = _run(assets, blocks)
    assert got.get("A") == {5}, "单一 owner 的图号绑定被改坏（1b 应压过几何）"
