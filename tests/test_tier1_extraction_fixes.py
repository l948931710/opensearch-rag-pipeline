# -*- coding: utf-8 -*-
"""tests/test_tier1_extraction_fixes.py — 工业级审计第一梯队修复的回归测试。

G4:  WMF/EMF/SVG 矢量图跳过必须计数并落 [VECTOR_IMAGE_SKIPPED] warning（此前静默丢弃）。
G13: PPTX GroupShape 递归——组合形状内文本框此前静默消失。
G14: 加密/带密码文件前置探测——此前只表现为通用打开失败并空烧 OCR 与重试。
G15: 乱码质量门——坏字体页 ≥50 字符 mojibake 此前永不重 OCR。
"""
import os
import sys
import tempfile
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")


# ═══════════════════ G4: 矢量图跳过计数 ═══════════════════

class _FakePart:
    def __init__(self, blob, content_type):
        self.blob = blob
        self.content_type = content_type


class _FakeRel:
    def __init__(self, target_ref, blob, content_type):
        self.reltype = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        self.target_ref = target_ref
        self.target_part = _FakePart(blob, content_type)


def _fake_document(rels):
    part = types.SimpleNamespace(rels={f"rId{i}": r for i, r in enumerate(rels)})
    return types.SimpleNamespace(part=part)


def test_g4_docx_emf_skip_is_counted(tmp_path):
    from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_docx
    doc = _fake_document([
        _FakeRel("media/image1.emf", b"\x01\x00\x00\x00emf-bytes", "image/x-emf"),
        _FakeRel("media/image2.wmf", b"\xd7\xcd\xc6\x9awmf-bytes", "image/x-wmf"),
        _FakeRel("media/image3.png", b"\x89PNG\r\n\x1a\npng-bytes", "image/png"),
    ])
    stats = {}
    assets = extract_images_from_docx("/tmp/fake.docx", str(tmp_path), document=doc, stats=stats)
    assert stats["skipped_vector_images"] == 2
    assert stats["skipped_vector_formats"] == {"emf", "wmf"}
    assert len(assets) == 1 and assets[0].local_path.endswith(".png")


def test_g4_stats_none_keeps_old_behavior(tmp_path):
    """独立调用（不传 stats）不报错、跳过行为不变。"""
    from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_docx
    doc = _fake_document([_FakeRel("media/a.svg", b"<svg/>", "image/svg+xml")])
    assets = extract_images_from_docx("/tmp/fake.docx", str(tmp_path), document=doc)
    assert assets == []


# ═══════════════════ G13: PPTX GroupShape 递归 ═══════════════════

def test_g13_group_shape_text_extracted(tmp_path):
    """组合形状（含嵌套组）内的文本框此前被 hasattr(shape,'text') 静默跳过。"""
    from pptx import Presentation
    from pptx.util import Emu

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    tb = slide.shapes.add_textbox(Emu(0), Emu(0), Emu(914400), Emu(457200))
    tb.text_frame.text = "顶层文本框"
    group = slide.shapes.add_group_shape()
    g_tb = group.shapes.add_textbox(Emu(0), Emu(500000), Emu(914400), Emu(457200))
    g_tb.text_frame.text = "组内说明文字"
    nested = group.shapes.add_group_shape()
    n_tb = nested.shapes.add_textbox(Emu(0), Emu(1000000), Emu(914400), Emu(457200))
    n_tb.text_frame.text = "嵌套组内文字"
    path = str(tmp_path / "g13.pptx")
    prs.save(path)

    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    res = UnifiedExtractor(simulate=True).extract({
        "doc_id": "g13", "version_no": 1, "file_ext": "pptx", "raw_key": "raw/g13.pptx",
        "filename": "g13.pptx", "local_path": path, "_tmp_dir": str(tmp_path),
    })
    assert "顶层文本框" in res.text
    assert "组内说明文字" in res.text      # the bug: previously silently dropped
    assert "嵌套组内文字" in res.text      # recursion depth > 1


# ═══════════════════ G14: 加密文件前置探测 ═══════════════════

def _extract_task(path, ext, tmp_path):
    return {"doc_id": "g14", "version_no": 1, "file_ext": ext, "raw_key": f"raw/g14.{ext}",
            "filename": f"g14.{ext}", "local_path": path, "_tmp_dir": str(tmp_path)}


def test_g14_encrypted_pdf_short_circuits(tmp_path):
    from pypdf import PdfReader, PdfWriter
    import fitz
    src = fitz.open()
    src.new_page().insert_text((72, 72), "secret content")
    plain = str(tmp_path / "plain.pdf")
    src.save(plain); src.close()
    writer = PdfWriter(clone_from=plain)
    writer.encrypt(user_password="s3cret")
    enc = str(tmp_path / "enc.pdf")
    with open(enc, "wb") as f:
        writer.write(f)
    assert PdfReader(enc).is_encrypted

    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    ux = UnifiedExtractor(simulate=True)
    res = ux.extract(_extract_task(enc, "pdf", tmp_path))
    assert res.extract_method == "unsupported:password_protected_pdf"
    assert res.ocr_status == "NOT_REQUIRED"          # 不空烧 OCR
    assert any("[PASSWORD_PROTECTED]" in w for w in res.warnings)


def test_g14_owner_password_only_pdf_passes_through(tmp_path):
    """仅 owner-password 限权（空用户密码、内容可读）的 PDF 应放行走正常抽取。"""
    from pypdf import PdfWriter
    import fitz
    src = fitz.open()
    src.new_page().insert_text((72, 72), "readable body text " * 10)
    plain = str(tmp_path / "p2.pdf")
    src.save(plain); src.close()
    writer = PdfWriter(clone_from=plain)
    writer.encrypt(user_password="", owner_password="admin")
    enc = str(tmp_path / "owner_only.pdf")
    with open(enc, "wb") as f:
        writer.write(f)

    from opensearch_pipeline.extraction.unified_extractor import _detect_password_protected
    assert _detect_password_protected(enc, "pdf") is None


def test_g14_ole_wrapped_docx_detected(tmp_path):
    """Office 加密 OOXML（或误改后缀的旧版二进制）文件头是 OLE 魔数而非 PK。"""
    fake = str(tmp_path / "enc.docx")
    with open(fake, "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)

    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    ux = UnifiedExtractor(simulate=True)
    res = ux.extract(_extract_task(fake, "docx", tmp_path))
    assert res.extract_method == "unsupported:password_protected_docx"
    assert any("[PASSWORD_PROTECTED]" in w for w in res.warnings)


def test_g14_normal_docx_not_flagged(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("正常文档")
    path = str(tmp_path / "ok.docx")
    d.save(path)
    from opensearch_pipeline.extraction.unified_extractor import _detect_password_protected
    assert _detect_password_protected(path, "docx") is None


# ═══════════════════ G12: 页眉页脚数字归一 + 非正立词过滤 ═══════════════════

def _make_pdf(tmp_path, page_builder, n_pages=3):
    import fitz
    doc = fitz.open()
    for i in range(n_pages):
        page_builder(doc.new_page(), i)
    path = str(tmp_path / "g12.pdf")
    doc.save(path)
    doc.close()
    return path


def test_g12_varying_page_number_footer_is_cropped(tmp_path):
    """"Page 1/Page 2/Page 3" 逐页变化的页码：精确文本键下永达不到频次阈值，
    数字归一（"Page #"）后按模式聚合 → 页脚被几何裁剪。"""
    from opensearch_pipeline.extraction.pdf_extractor import extract_pdf

    def build(page, i):
        page.insert_text((72, 300), "Body alpha beta gamma delta " * 4)
        page.insert_text((280, 770), f"Page {i + 1}")

    blocks, page_count, warnings = extract_pdf(_make_pdf(tmp_path, build))
    text = "\n".join(b.text for b in blocks)
    assert "Body alpha" in text
    assert "Page 1" not in text and "Page 2" not in text


def test_g12_rotated_watermark_words_dropped(tmp_path):
    """非正立（旋转）文字——典型为斜置水印——不得混入正文行。"""
    from opensearch_pipeline.extraction.pdf_extractor import extract_pdf

    def build(page, i):
        page.insert_text((72, 300), "Normal body sentence one " * 4)
        page.insert_text((300, 500), "WATERMARKTEXT", rotate=90)

    blocks, page_count, warnings = extract_pdf(_make_pdf(tmp_path, build, n_pages=1))
    text = "\n".join(b.text for b in blocks)
    assert "Normal body sentence" in text
    assert "WATERMARKTEXT" not in text


def test_g4_warning_text_emitted():
    from opensearch_pipeline.extraction.unified_extractor import _warn_skipped_vector_images
    warnings = []
    _warn_skipped_vector_images(warnings, {"skipped_vector_images": 3,
                                           "skipped_vector_formats": {"emf", "svg"}})
    assert len(warnings) == 1
    assert "[VECTOR_IMAGE_SKIPPED]" in warnings[0]
    assert "3 张" in warnings[0] and "emf/svg" in warnings[0]
    # 零跳过 → 不产生 warning
    _warn_skipped_vector_images(warnings, {})
    assert len(warnings) == 1
