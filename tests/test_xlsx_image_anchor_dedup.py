# -*- coding: utf-8 -*-
"""XLSX 图片去重：同字节图复用在不同 anchor 行必须各自保留（步骤1/步骤3 共用一张截图），
真重复（同 anchor / 无 anchor）仍去重。回归 image_extraction_utils.extract_images_from_xlsx。"""
import base64
import os

import pytest

# 1x1 PNG
_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _build_xlsx(path, anchors):
    openpyxl = pytest.importorskip("openpyxl")
    from openpyxl.drawing.image import Image as XlImage

    img_file = path + ".png"
    with open(img_file, "wb") as f:
        f.write(_PNG)
    wb = openpyxl.Workbook()
    ws = wb.active
    for a in anchors:
        ws.add_image(XlImage(img_file), a)  # 每次同一文件 → 同字节 → 同 MD5
    wb.save(path)


def test_xlsx_same_image_distinct_anchors_kept(tmp_path):
    from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_xlsx
    xlsx = str(tmp_path / "proc.xlsx")
    _build_xlsx(xlsx, ["A10", "A40"])  # 同图字节，两个不同 anchor 行
    out_dir = str(tmp_path / "out")
    os.makedirs(out_dir, exist_ok=True)
    assets = extract_images_from_xlsx(xlsx, out_dir)
    rows = sorted(a.anchor_row for a in assets if a.anchor_row is not None)
    assert rows == [9, 39]                       # 两个 anchor 都保留，第二处不再丢图
    assert len({a.local_path for a in assets}) == 1  # 仅落盘一次，别名复用同一文件


def test_xlsx_true_duplicate_same_anchor_deduped(tmp_path):
    from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_xlsx
    xlsx = str(tmp_path / "proc2.xlsx")
    _build_xlsx(xlsx, ["A10", "A10"])  # 同图同 anchor → 真重复
    out_dir = str(tmp_path / "out2")
    os.makedirs(out_dir, exist_ok=True)
    assets = extract_images_from_xlsx(xlsx, out_dir)
    assert len(assets) == 1
