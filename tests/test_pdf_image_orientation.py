# -*- coding: utf-8 -*-
"""
PDF 嵌入图片朝向校正测试 — extract_images_from_pdf

背景（2026-06 真实事故）：FL-XS-WI-007 扫描 PDF 的位图按垂直镜像存储、
内容流用负 d CTM 补偿（viewer 显示正常）。extract_image 导出存储字节，
落到 OSS 的资产全部上下颠倒 → Qwen-VL 描述失真、钉钉卡片图片倒置。

本测试用合成 PDF 覆盖 9 种朝向组合（正常 / page /Rotate 90·180·270 /
CTM 旋转 90·180·270 / 镜像 CTM / 镜像 CTM + /Rotate 180），断言导出文件
的像素朝向与页面渲染一致。
"""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")
PIL_Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
from PIL import Image  # noqa: E402

from opensearch_pipeline.extraction.image_extraction_utils import (  # noqa: E402
    extract_images_from_pdf,
)


# ── 方向无歧义的标记图：顶部绿条 + 左上红块 + 右下蓝块 ──

def _make_marker(w=120, h=80) -> Image.Image:
    img = Image.new("RGB", (w, h), "white")
    px = img.load()
    for x in range(w):
        for y in range(h):
            if y < 6:
                px[x, y] = (0, 200, 0)
            elif x < w // 2 and y < h // 2:
                px[x, y] = (220, 0, 0)
            elif x >= w // 2 and y >= h // 2:
                px[x, y] = (0, 0, 220)
    return img


def _marker_bytes(transpose=None, fmt="JPEG") -> bytes:
    img = _make_marker()
    if transpose is not None:
        img = img.transpose(transpose)
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=95)
    return buf.getvalue()


def _build_pdf(path, page_rotate=0, insert_rotate=0, mirror_ctm=False):
    """构造单页 PDF。mirror_ctm=True 复刻真实坏档：
    存储字节垂直镜像 + 内容流负 d CTM 补偿（显示正常）。"""
    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    rect = fitz.Rect(80, 100, 280, 240)

    stream = _marker_bytes(Image.FLIP_TOP_BOTTOM if mirror_ctm else None)
    page.insert_image(rect, stream=stream, rotate=insert_rotate)

    if mirror_ctm:
        import re
        xref_c = page.get_contents()[0]
        content = doc.xref_stream(xref_c).decode("latin-1")
        m = re.search(
            r"([\d.+-]+) ([\d.+-]+) ([\d.+-]+) ([\d.+-]+) ([\d.+-]+) ([\d.+-]+) cm",
            content,
        )
        a, b, c, d, e, f = (float(g) for g in m.groups())
        content = (
            content[: m.start()]
            + f"{a} {b} {c} {-d} {e} {f + d} cm"
            + content[m.end():]
        )
        doc.update_stream(xref_c, content.encode("latin-1"))

    if page_rotate:
        page.set_rotation(page_rotate)

    doc.save(path)
    doc.close()


def _render_truth(pdf_path) -> Image.Image:
    """渲染图片所在区域作为 ground truth（get_pixmap 完整执行 CTM + /Rotate）。"""
    doc = fitz.open(pdf_path)
    page = doc[0]
    info = [i for i in page.get_image_info(xrefs=True) if i.get("xref", 0) > 0][0]
    bbox = fitz.Rect(info["bbox"]) * page.rotation_matrix
    bbox.normalize()
    pm = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=bbox)
    truth = Image.open(io.BytesIO(pm.tobytes("png"))).convert("RGB")
    doc.close()
    return truth


def _mse(img_a: Image.Image, img_b: Image.Image, size=48) -> float:
    a = img_a.convert("RGB").resize((size, size))
    b = img_b.convert("RGB").resize((size, size))
    return sum(
        (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2
        for p, q in zip(a.getdata(), b.getdata())
    ) / (size * size)


CASES = [
    ("normal", dict()),
    ("pgrot90", dict(page_rotate=90)),
    ("pgrot180", dict(page_rotate=180)),
    ("pgrot270", dict(page_rotate=270)),
    ("ctmrot90", dict(insert_rotate=90)),
    ("ctmrot180", dict(insert_rotate=180)),
    ("ctmrot270", dict(insert_rotate=270)),
    ("mirror_ctm", dict(mirror_ctm=True)),
    ("mirror_pgrot180", dict(mirror_ctm=True, page_rotate=180)),
]


@pytest.mark.parametrize("name,kwargs", CASES, ids=[c[0] for c in CASES])
def test_extracted_image_matches_displayed_orientation(tmp_path, name, kwargs):
    pdf_path = str(tmp_path / f"{name}.pdf")
    out_dir = str(tmp_path / "out")
    os.makedirs(out_dir, exist_ok=True)
    _build_pdf(pdf_path, **kwargs)

    assets = extract_images_from_pdf(pdf_path, out_dir)
    assert len(assets) == 1, f"{name}: expected 1 asset, got {len(assets)}"

    extracted = Image.open(assets[0].local_path)
    truth = _render_truth(pdf_path)
    mse = _mse(extracted, truth)
    # JPEG 噪声 + 缩放容差远低于该阈值；任何 90°/180°/镜像错位都在 5 万以上
    assert mse < 2000, (
        f"{name}: extracted orientation differs from displayed page "
        f"(MSE={mse:.0f}); orientation fix regressed"
    )


def test_mirror_ctm_raw_bytes_would_fail(tmp_path):
    """对照组：镜像坏档若直接导出存储字节，与显示朝向显著不符。
    （守住测试本身的辨别力：阈值能区分对错。）"""
    pdf_path = str(tmp_path / "mirror.pdf")
    _build_pdf(pdf_path, mirror_ctm=True)

    doc = fitz.open(pdf_path)
    xref = doc[0].get_images(full=True)[0][0]
    raw = Image.open(io.BytesIO(doc.extract_image(xref)["image"]))
    doc.close()

    truth = _render_truth(pdf_path)
    assert _mse(raw, truth) > 50000, "control: raw mirrored bytes should NOT match display"


def test_same_bytes_no_correction_single_asset(tmp_path):
    """正常 PDF 同一图片多页复用 → 仍按 MD5 去重为 1 个 asset（行为不回归）。"""
    doc = fitz.open()
    stream = _marker_bytes()
    for _ in range(3):
        page = doc.new_page(width=400, height=500)
        page.insert_image(fitz.Rect(80, 100, 280, 240), stream=stream)
    pdf_path = str(tmp_path / "dup.pdf")
    doc.save(pdf_path)
    doc.close()

    out_dir = str(tmp_path / "out")
    os.makedirs(out_dir, exist_ok=True)
    assets = extract_images_from_pdf(pdf_path, out_dir)
    assert len(assets) == 1, f"dedup regressed: {len(assets)} assets from 3 identical placements"
