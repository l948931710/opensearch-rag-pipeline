# -*- coding: utf-8 -*-
"""Regression tests for the PDF image→step override reconciliation (image_binding_reconcile).

Pins the agreed ownership-strength rule against the real audit cases:
  22767C  range-theft must be REJECTED,
  5FFA22 / 328126  valid corrections must be PRESERVED,
  exact-figure destination legitimately overrides a weaker spatial source,
  ambiguous shared-context is NOT forced to a new destination,
plus reference parsing (circled / Arabic / exact / covering range) and OCR-fragment fallback.
"""
import pytest
from opensearch_pipeline.image_binding_reconcile import (
    step_circled_set, image_circled_nums, reconcile_move,
)


# ── reference parsing ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,exp_fig,exp_proc", [
    ("点击“确定”（如图⑭-⑯）", {14, 15, 16}, False),       # FIGURE range (图⑭-⑯) → figure_covered
    ("进入库存管理见图1-3所示", {1, 2, 3}, False),                  # arabic figure range (图1-3)
    ("点击产品检验单（如图⑮）", {15}, False),                       # exact circled
    ("打√见图7，然后点击弃审", {7}, False),                         # exact arabic
    ("填写①设备②班次③数量", {1, 2, 3}, False),                     # inline enumeration → figure_covered
    ("如下图①-⑥步操作", set(), True),                              # PROCEDURAL step range → broad, NOT figure
    ("按下图1-6步操作进入", set(), True),                           # arabic procedural step range → broad
])
def test_step_circled_set(text, exp_fig, exp_proc):
    fig, proc = step_circled_set(text)
    assert fig == exp_fig, (text, fig)
    assert proc == exp_proc, (text, proc)


def test_image_circled_from_ocr():
    assert image_circled_nums("表单上标注①②③") == {1, 2, 3}
    assert image_circled_nums("") == set()


def test_malformed_ocr_fragment_safe():
    # OCR fragments / garbage must not crash and must yield no refs (fall back to spatial)
    cov, broad = step_circled_set("[图片OCR] 我的桌面 待检入库单 产品报检单 打印 删除")
    assert cov == set() and broad is False
    r = reconcile_move("[图片OCR] ☃  乱码", "另一段 OCR 乱码 ###", None, None)
    assert r["apply"] is False and r["decision"] in ("reject_review", "reject_conflict")


# ── the four anchor cases ────────────────────────────────────────────────────
def test_22767C_reject_range_theft():
    """step6 owns the printer-settings dialog (打印机名称/份数, 图⑭-⑯); step1's broad ①-⑥ must
    NOT steal it even though it incidentally shares the doc-common term '生产订单'."""
    geo = "步骤6：选择“打印机名称”，更改“打印份数”，点击“确定”（如图⑭-⑯）"
    dst = "步骤1：进入U8系统，按系统路径进入（如下图①-⑥步操作），双击“生产订单列表”"
    summary = "生产订单条码打印参数设置窗口，显示打印机名称为HP LaserJet 1020，打印份数"
    r = reconcile_move(geo, dst, image_ocr="", image_summary=summary, source_zeroed=True)
    assert r["apply"] is False, r


def test_5FFA22_preserve_list_view_move():
    """list-view image belongs to step1 (进入领料申请单列表界面); step2 (核对日期) has no real
    ownership — the override correctly moves it."""
    geo = "步骤2：核对⑥日期、数量；⑥确认 图中⑥为日期、数量"
    dst = "步骤1：进入U8系统“领料申请单列表”界面（如下图①-⑤步操作） ①搜索 ②点击"
    summary = "用友U8+系统‘领料申请单列表’界面截图，显示左侧导航栏、序号、商检号、手册号"
    r = reconcile_move(geo, dst, image_ocr="", image_summary=summary, source_zeroed=True)
    assert r["apply"] is True, r


def test_328126_preserve_explicit_local_move():
    """产品标识卡 image → step1 which explicitly names 《产品标识卡》(explicit local ownership)."""
    geo = "步骤2：每天上午9点左右，向各区班长收集《交货单》"
    dst = "步骤1：按《产品标识卡》清点实货（7：30、14:30各1次），抄录定单号机台号数量"
    summary = "台州富岭塑胶有限公司产品标识卡（包装车间专用），含手册号、材料配比、订单号"
    r = reconcile_move(geo, dst, image_ocr="", image_summary=summary, source_zeroed=True)
    assert r["apply"] is True, r


def test_exact_figure_dest_overrides_spatial_source():
    """An image carrying circled markers ①②③ should move to the step that references ①②③
    (figure ownership) away from a spatially-anchored sibling that does not."""
    geo = "步骤4.2：填写完后，依次点击“④根据设备带出班组人员”"
    dst = "步骤4.1：按《交货单》填写①设备②班次③数量"
    summary = "一张手写生产记录表单，含按设备、班次、数量等字段填写的作业记录，红框数字标注①②③"
    r = reconcile_move(geo, dst, image_ocr="表单标注①②③", image_summary=summary)
    assert r["apply"] is True and r["dst_tier"] == "figure", r


def test_ambiguous_shared_context_not_forced():
    """Both steps share the 关联单据 window context with comparable local strength → do NOT
    force the image to the destination (keep the geometric source, mark for review)."""
    geo = "步骤5：进入关联单据点击产品检验单"
    dst = "步骤4：进入产品待检入库单，操作整单关联，出现关联单据框"
    summary = "ERP系统关联单据窗口，包含两行产品检验单记录，含仓库、单据类型、单据编号字段"
    r = reconcile_move(geo, dst, image_ocr="", image_summary=summary, source_zeroed=False)
    assert r["apply"] is False and r["decision"] == "reject_review", r


# ── structured diagnostics: reason codes + result + tiers ────────────────────
def test_reason_codes_and_diagnostics():
    # range_theft_blocked: source owns (local); destination rests on a broad procedural range —
    # even though the destination ALSO shares the incidental doc-common term '生产订单' with the
    # image summary (the real 22767C condition).
    r = reconcile_move("步骤6：选择“打印机名称”，更改“打印份数”（如图⑭-⑯）",
                       "步骤1：进入U8系统（如下图①-⑥步操作），双击“生产订单列表”",
                       "", "生产订单条码打印参数设置窗口，打印机名称HP LaserJet 1020，打印份数")
    assert r["result"] == "blocked" and r["reason_code"] == "range_theft_blocked", r
    # source_owner_preserved: source owns (local), destination weaker (non-broad) → keep owner
    r = reconcile_move("步骤5：进入关联单据点击产品检验单",
                       "步骤4：进入产品待检入库单整单关联",
                       "", "ERP系统关联单据窗口，含产品检验单记录、仓库、单据类型字段")
    assert r["result"] == "blocked" and r["reason_code"] == "source_owner_preserved", r
    # stronger_destination_owner: destination explicit local, source spatial-only
    r = reconcile_move("步骤2：核对⑥日期、数量",
                       "步骤1：进入U8系统“领料申请单列表”界面",
                       "", "用友U8+系统‘领料申请单列表’界面截图，序号、商检号、手册号")
    assert r["result"] == "accepted" and r["reason_code"] == "stronger_destination_owner", r
    # ambiguous_kept_at_source: neither side owns meaningfully
    r = reconcile_move("步骤A：执行某操作", "步骤B：执行另一操作", "", "无明显共享主题的界面图")
    assert r["result"] == "blocked" and r["reason_code"] == "ambiguous_kept_at_source", r
    # every result carries the required diagnostic fields
    for key in ("src_tier", "dst_tier", "result", "reason_code"):
        assert key in r
