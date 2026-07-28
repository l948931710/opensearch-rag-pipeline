# -*- coding: utf-8 -*-
"""PDF 条带切片资产级缝合（#F-mm9, RAG_PDF_STRIP_STITCH **2026-07-26 起默认 ON**）回归测试。

背景：PDF 转换器把一张照片存成 N 条全宽横条（实证 FL-XS-WI-007 的 23 条
1000×31），每条 aspect>8 逐条死于 Funnel-1，整图彻底消失且零告警。
DOCX 版靠 blocks 连续 run；PDF 版改用【同页 + bbox 纵向邻接】判据。
关键不变式：
  - 只救回本来必死于 Funnel-1 的条（能存活的图绝不动）；
  - run 内任一条 bbox 缺失 → 整 run 放弃（fail-open）；
  - md5 去重洞 = run 边界（碎片化子缝合，各子 run 独立满足判据）；
  - 默认 OFF 原样透传。
"""

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from opensearch_pipeline.extraction.image_extraction_utils import ImageAsset  # noqa: E402
from opensearch_pipeline.extraction.unified_extractor import (  # noqa: E402
    _funnel1_discard_bound,
    _stitch_pdf_strip_assets,
)


def _mk_strip(tmp_path, name, w=300, h=20, color=(120, 130, 140)):
    p = tmp_path / name
    Image.new("RGB", (w, h), color).save(str(p))
    return str(p)


def _strip_asset(tmp_path, idx, y0, h=20, w=300, page=1, x0=50.0, bbox=True):
    """一条横条资产：bbox 用 pt 坐标模拟（宽/高与像素同数值即可）。"""
    return ImageAsset(
        local_path=_mk_strip(tmp_path, f"s{idx}.png", w=w, h=h),
        page_num=page,
        image_index=idx,
        original_name=f"xref_{idx}",
        bbox=(x0, y0, x0 + w, y0 + h) if bbox else None,
    )


def _run_of(tmp_path, n, start_idx=0, page=1, h=20, w=300):
    return [_strip_asset(tmp_path, start_idx + i, y0=100.0 + i * h, h=h, w=w, page=page)
            for i in range(n)]


def test_on_by_default(tmp_path, monkeypatch):
    """2026-07-26 翻默认（Sam 拍板）。依据见 `_stitch_pdf_strip_assets` docstring：
    实测救回 2 张《交货单》，生产影响面恰好 1 篇（580 篇全扫，命中率 0.18%）。"""
    monkeypatch.delenv("RAG_PDF_STRIP_STITCH", raising=False)
    assets = _run_of(tmp_path, 6, h=20)
    out, warns = _stitch_pdf_strip_assets(assets)
    assert len(out) == 1 and len(warns) == 1


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF"])
def test_explicit_off_passthrough(tmp_path, monkeypatch, val):
    """回滚路径：显式关掉时原样透传，一个资产都不动。"""
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", val)
    assets = _run_of(tmp_path, 6)
    out, warns = _stitch_pdf_strip_assets(assets)
    assert out is assets and warns == []


@pytest.mark.parametrize("val", ["", "1", "true", "乱填"])
def test_unknown_values_stay_on(tmp_path, monkeypatch, val):
    """默认 ON 的开关必须"配错也开" —— 静默退回旧行为=整图消失且零告警。"""
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", val)
    out, warns = _stitch_pdf_strip_assets(_run_of(tmp_path, 6, h=20))
    assert len(warns) == 1


def test_on_stitches_adjacent_run(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", "true")
    assets = _run_of(tmp_path, 6, h=20)      # 总高 120 ∈ [100,6000]
    out, warns = _stitch_pdf_strip_assets(assets)
    assert len(out) == 1 and len(warns) == 1
    comp = out[0]
    assert comp.original_name == "stitched:6slices"
    assert comp.page_num == 1 and comp.image_index == 0
    assert comp.bbox == (50.0, 100.0, 350.0, 220.0)          # 成员 bbox 并集
    with Image.open(comp.local_path) as im:
        assert im.size == (300, 120)


def test_missing_bbox_breaks_run(tmp_path, monkeypatch):
    """bbox=None 的条不可参与（get_image_info 可失败）：把 run 切成两段，
    两段都 <MIN_RUN → 全部原样保留。"""
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", "true")
    assets = _run_of(tmp_path, 7)
    assets[3].bbox = None
    out, warns = _stitch_pdf_strip_assets(assets)
    assert len(out) == 7 and warns == []


def test_md5_hole_splits_into_subruns(tmp_path, monkeypatch):
    """md5 去重洞（中间缺一条 → 邻接断裂）= run 边界：两个 ≥MIN_RUN 的子段各自缝合。"""
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", "true")
    upper = [_strip_asset(tmp_path, i, y0=100.0 + i * 20) for i in range(5)]      # y:100-200
    lower = [_strip_asset(tmp_path, 10 + i, y0=300.0 + i * 20) for i in range(5)]  # y:300-400（断裂）
    out, warns = _stitch_pdf_strip_assets(upper + lower)
    assert len(out) == 2 and len(warns) == 2
    assert all(o.original_name == "stitched:5slices" for o in out)


def test_survivable_images_never_touched(tmp_path, monkeypatch):
    """非 Funnel-1 丢弃对象（正常大图）绝不参与缝合——只救必死的图。"""
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", "true")
    big = [_strip_asset(tmp_path, i, y0=100.0 + i * 200, h=200, w=600) for i in range(4)]
    assert not _funnel1_discard_bound(600, 200, 50.0)   # 前提自检：这类图 Funnel-1 存活
    out, warns = _stitch_pdf_strip_assets(big)
    assert len(out) == 4 and warns == []


def test_total_height_bounds(tmp_path, monkeypatch):
    """缝合总高 <100 的 run 不缝（如 4 条 300×20=80）。"""
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", "true")
    assets = [_strip_asset(tmp_path, i, y0=100.0 + i * 15, h=15) for i in range(4)]  # 总高 60
    out, warns = _stitch_pdf_strip_assets(assets)
    assert len(out) == 4 and warns == []


def test_cross_page_never_stitched(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", "true")
    p1 = [_strip_asset(tmp_path, i, y0=100.0 + i * 20, page=1) for i in range(3)]
    p2 = [_strip_asset(tmp_path, 10 + i, y0=100.0 + i * 20, page=2) for i in range(3)]
    out, warns = _stitch_pdf_strip_assets(p1 + p2)
    assert len(out) == 6 and warns == []     # 每页只有 3 条 < MIN_RUN


def test_x_misaligned_not_stitched(tmp_path, monkeypatch):
    """并排窄条（x 区间不对齐）不是纵向堆叠，不缝。"""
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", "true")
    assets = [_strip_asset(tmp_path, i, y0=100.0 + i * 20, x0=50.0 + i * 40) for i in range(5)]
    out, warns = _stitch_pdf_strip_assets(assets)
    assert len(out) == 5 and warns == []


def test_unaffected_assets_kept_in_order(tmp_path, monkeypatch):
    """缝合只影响 run 成员：其余资产原位保留，缝合产物占首条位置（文档序稳定）。"""
    monkeypatch.setenv("RAG_PDF_STRIP_STITCH", "true")
    normal = _strip_asset(tmp_path, 99, y0=700.0, h=200, w=600)   # 存活大图
    run = _run_of(tmp_path, 6, h=20)
    out, _ = _stitch_pdf_strip_assets(run + [normal])
    assert len(out) == 2
    assert out[0].original_name == "stitched:6slices"
    assert out[1].image_index == 99
