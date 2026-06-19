# -*- coding: utf-8 -*-
"""
test_equipment_cleaning_binding.py — 设备清扫基准书（equipment_cleaning_standard）图文绑定回归。

锁定 2026-06-18 修复（over-attach / cross-bind → xlsx 摄入侧 Jaccard 0.8167→0.8917）的两条
关键不变式，外加一条强者归并的正向锁：

  1. **未支持版式必须被检测/降级，而非静默误绑**：表头显示三格身份层级
     （序号⇥系统⇥子系统⇥部位名称，部位名称落在 body 第 3 格）时，该 section 的行被排除
     出绑定目标并发 unsupported 诊断，图片不会被用错位身份硬绑（改建独立 image chunk）。
  2. **一部位多图（证据等强）不被强者归并误删**：当一行有多张图、且都逐字命中该行部位名
     （或都不命中）时，全部保留 —— 只有"部分逐字命中、部分不命中"才归并到逐字命中者。
  3. （配套正向锁）混合证据时强者归并确实丢弱者（real 部位标签图在场后丢掉旁蹭图）。

均为纯函数级单测，不连任何外部服务。
"""

from opensearch_pipeline.pipeline_nodes import (
    _resolve_part_col_index,
    _ce_chunk_regions,
    _bind_equipment_cleaning_images,
)


class _Row:
    """极简 row-card chunk 替身：绑定逻辑只读 chunk_type/chunk_text/section_title/extra。"""

    def __init__(self, text, section=None, chunk_type="text_chunk"):
        self.chunk_type = chunk_type
        self.chunk_text = text
        self.section_title = section
        self.extra = {}


def _asset(fn, labels, vsum, ocr=""):
    return {
        "status": "ROUTE_TO_VECTOR",
        "part_labels": labels,
        "filename": fn,
        "image_index": 0,
        "anchor_row": 1,
        "visual_summary": vsum,
        "ocr_text": ocr,
    }


# ── 1) 三格身份层级 → 检测 + 降级（不误绑）─────────────────────────────────────
def test_three_cell_identity_layout_is_detected_and_degraded():
    # 表头：序号 ⇥ 系统 ⇥ 子系统 ⇥ 清扫部位名称 ⇥ … → 部位名称在 body 第 2 格（>=2 未支持）
    blocks = [
        {"block_type": "heading", "text": "三格基准书"},
        {"block_type": "paragraph",
         "text": "序号\t系统\t子系统\t清扫部位名称\t清扫基准\t清扫方法"},
    ]
    part_col = _resolve_part_col_index(blocks)
    assert part_col["三格基准书"] == 2, "部位名称列应解析在 body 第 2 格（三格身份）"

    # 直接函数级：part_col_index>=2 → status='unsupported'
    _ident, _items, _pname, status = _ce_chunk_regions(
        "【章节:三格基准书】1\t液压\t泵组\t齿轮箱\t无油污\t擦拭", part_col_index=2)
    assert status == "unsupported"

    # 端到端：该行被排除出绑定目标，图片不被用错位身份（液压/泵组）硬绑
    row = _Row("【章节:三格基准书】1\t液压\t泵组\t齿轮箱\t无油污\t擦拭", section="三格基准书")
    asset = _asset("g.jpg", ["齿轮箱"], "齿轮箱设备特写")
    bound, diag = _bind_equipment_cleaning_images([row], [asset], "dept", "doc", 1, blocks)

    assert diag["unsupported"] >= 1
    assert "三格基准书" in diag["unsupported_sections"]
    assert not row.extra.get("image_refs"), "未支持版式行不得绑图（避免错位身份误绑）"
    assert "g.jpg" not in bound, "未绑定的图应回退独立 image chunk，不进 ce_bound_fns"


def test_missing_header_falls_back_with_diagnostic():
    # 无表头（section 不在 part_col map）→ first-two-cells 兜底 + fallback 诊断，但仍能绑
    row = _Row("【章节:无表头】3\t传动\t链轮\t无松动\t目视", section="无表头")
    asset = _asset("c.jpg", ["链轮"], "链轮齿形特写")
    bound, diag = _bind_equipment_cleaning_images([row], [asset], "dept", "doc", 1, blocks=[])
    assert diag["fallback"] >= 1
    assert "无表头" in diag["fallback_sections"]
    # 兜底下身份列=body[:2]=传动 链轮 → 仍正确绑定（链轮 命中身份）
    assert "c.jpg" in bound


# ── 2) 一部位多图（证据等强）→ 不被强者归并误删 ──────────────────────────────
def test_multiple_images_same_part_kept_when_evidence_equally_strong():
    blocks = [
        {"block_type": "heading", "text": "清扫表"},
        {"block_type": "paragraph",
         "text": "序号\t类别\t清扫部位名称\t清扫基准\t清扫方法"},
    ]
    assert _resolve_part_col_index(blocks)["清扫表"] == 1  # 两格身份，受支持

    row = _Row("【章节:清扫表】5\t传动\t齿轮箱\t无油污\t擦拭", section="清扫表")
    # 两张图都逐字命中部位名 齿轮箱（证据等强）
    a1 = _asset("g1.jpg", ["齿轮箱"], "齿轮箱左视图")
    a2 = _asset("g2.jpg", ["齿轮箱"], "齿轮箱右视图")
    bound, diag = _bind_equipment_cleaning_images([row], [a1, a2], "dept", "doc", 1, blocks)

    fns = {r["filename"] for r in (row.extra.get("image_refs") or [])}
    assert fns == {"g1.jpg", "g2.jpg"}, f"证据等强的多图必须全保留，实得 {fns}"
    assert diag["unsupported"] == 0 and diag["fallback"] == 0  # 表头解析、受支持


def test_multiple_images_kept_when_none_is_exact():
    # 都不逐字命中部位名（近重复歧义图）→ 同样全保留（不武断单选）
    blocks = [
        {"block_type": "heading", "text": "点检表"},
        {"block_type": "paragraph", "text": "序号\t点检部位\t点检项目\t判定标准"},
    ]
    row = _Row("【章节:点检表】4\t链条总成\t链条总成", section="点检表")
    a1 = _asset("l1.jpg", ["链条"], "链条与齿轮啮合特写")
    a2 = _asset("l2.jpg", ["链条"], "链条传动局部")
    _bound, _diag = _bind_equipment_cleaning_images([row], [a1, a2], "dept", "doc", 1, blocks)
    fns = {r["filename"] for r in (row.extra.get("image_refs") or [])}
    assert fns == {"l1.jpg", "l2.jpg"}, f"无逐字赢家的歧义近重复图应全保留，实得 {fns}"


# ── 3) 配套正向锁：混合证据 → 强者归并丢弱者 ─────────────────────────────────
def test_strong_collapse_drops_weak_when_one_image_names_exact_part():
    blocks = [
        {"block_type": "heading", "text": "每班清扫"},
        {"block_type": "paragraph", "text": "序号\t类别\t清扫部位名称\t清扫基准"},
    ]
    # 部位名 ★齿轮油；一张逐字命中、一张只蹭"齿轮"
    row = _Row("【章节:每班清扫】4\t点检\t★齿轮油\t不低于50", section="每班清扫")
    strong = _asset("oil.jpg", ["齿轮"], "L-CKC320 工业齿轮油 产品标签")   # 含"齿轮油"
    weak = _asset("shaft.jpg", ["齿轮"], "丝杆与齿轮传动组件 内部结构")     # 仅"齿轮"
    _bound, _diag = _bind_equipment_cleaning_images([row], [strong, weak], "dept", "doc", 1, blocks)
    fns = {r["filename"] for r in (row.extra.get("image_refs") or [])}
    assert fns == {"oil.jpg"}, f"逐字命中部位名者应胜出、旁蹭图被归并丢弃，实得 {fns}"
