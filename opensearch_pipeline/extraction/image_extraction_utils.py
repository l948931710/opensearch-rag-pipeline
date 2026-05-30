# -*- coding: utf-8 -*-
"""
image_extraction_utils.py — 文档嵌入图片提取工具

从 DOCX / PDF / XLSX 文件中提取嵌入图片到本地临时目录。
返回 ImageAsset 列表，供 ImageFunnelProcessor 三阶段过滤使用。

设计原则：
  - 所有图片都应被处理，不设人为上限（Funnel 1 的 heuristic 过滤会自然淘汰装饰图）
  - MD5 去重：docx 中同一图片可能被多次引用，只处理一次
  - 异常隔离：图片提取失败不影响文本提取主流程，只记录 warning
"""

import hashlib
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ImageAsset:
    """从文档中提取的单张嵌入图片。"""
    local_path: str                         # 导出到 tmp_dir 的本地文件路径
    page_num: Optional[int] = None          # 所在页码（PDF 可精确提供，DOCX 为 None）
    image_index: int = 0                    # 在文档中的顺序索引
    original_name: str = ""                 # 在文档包内的原始名称（如 media/image3.jpeg）


# ═══════════════════════════════════════════════════════════════
# DOCX — python-docx relationship 遍历
# ═══════════════════════════════════════════════════════════════

def extract_images_from_docx(
    local_path: str,
    output_dir: str,
) -> List[ImageAsset]:
    """
    从 DOCX 文件中提取所有嵌入图片。

    策略：遍历 document.part.rels 中 reltype 包含 'image' 的关系，
    读取 target_part.blob 获取图片二进制数据。

    DOCX 不提供原生页码信息，所有图片 page_num 为 None。

    Args:
        local_path: DOCX 文件的本地路径。
        output_dir: 图片导出目标目录。

    Returns:
        导出的 ImageAsset 列表（已 MD5 去重）。
    """
    try:
        import docx
    except ImportError:
        return []

    assets: List[ImageAsset] = []
    seen_hashes: set = set()

    try:
        document = docx.Document(local_path)
    except Exception as e:
        print(f"      ⚠️ Failed to open DOCX for image extraction: {e}")
        return []

    doc_basename = os.path.splitext(os.path.basename(local_path))[0]
    img_index = 0

    for rel in document.part.rels.values():
        if "image" not in rel.reltype:
            continue

        try:
            blob = rel.target_part.blob
        except Exception:
            continue

        # MD5 去重
        md5 = hashlib.md5(blob).hexdigest()
        if md5 in seen_hashes:
            continue
        seen_hashes.add(md5)

        # 确定文件扩展名
        content_type = getattr(rel.target_part, 'content_type', '')
        ext = _content_type_to_ext(content_type, rel.target_ref)

        # 跳过不支持的矢量格式（WMF/EMF）
        if ext in (".wmf", ".emf", ".svg"):
            continue

        # 导出到 tmp_dir
        filename = f"{doc_basename}_img{img_index:04d}{ext}"
        out_path = os.path.join(output_dir, filename)

        try:
            with open(out_path, "wb") as f:
                f.write(blob)
        except Exception as e:
            print(f"      ⚠️ Failed to export DOCX image {rel.target_ref}: {e}")
            continue

        assets.append(ImageAsset(
            local_path=out_path,
            page_num=None,
            image_index=img_index,
            original_name=rel.target_ref,
        ))
        img_index += 1

    if assets:
        print(f"      [docx-img] Extracted {len(assets)} unique images "
              f"(deduped from {len([r for r in document.part.rels.values() if 'image' in r.reltype])} refs)")

    return assets


# ═══════════════════════════════════════════════════════════════
# PDF — PyMuPDF (fitz) 逐页提取
# ═══════════════════════════════════════════════════════════════

def extract_images_from_pdf(
    local_path: str,
    output_dir: str,
    max_pages: int = 20,
) -> List[ImageAsset]:
    """
    从 PDF 文件中提取嵌入图片，带精确 page_num。

    策略：使用 PyMuPDF (fitz) 逐页 get_images + extract_image。
    PyMuPDF 可提供每张图片所在的精确页码。

    Args:
        local_path: PDF 文件的本地路径。
        output_dir: 图片导出目标目录。
        max_pages: 最大处理页数（与 pdf_extractor 保持一致）。

    Returns:
        导出的 ImageAsset 列表（已 MD5 去重）。
    """
    try:
        import fitz
    except ImportError:
        print("      ⚠️ PyMuPDF (fitz) not installed, skipping PDF image extraction")
        return []

    assets: List[ImageAsset] = []
    seen_hashes: set = set()

    try:
        pdf = fitz.open(local_path)
    except Exception as e:
        print(f"      ⚠️ Failed to open PDF for image extraction: {e}")
        return []

    doc_basename = os.path.splitext(os.path.basename(local_path))[0]
    img_index = 0

    for page_idx in range(min(len(pdf), max_pages)):
        page = pdf[page_idx]
        page_num = page_idx + 1

        try:
            image_list = page.get_images(full=True)
        except Exception:
            continue

        for img_info in image_list:
            xref = img_info[0]

            try:
                base_image = pdf.extract_image(xref)
            except Exception:
                continue

            if not base_image or "image" not in base_image:
                continue

            blob = base_image["image"]

            # MD5 去重（PDF 中相同图片可能在多页出现）
            md5 = hashlib.md5(blob).hexdigest()
            if md5 in seen_hashes:
                continue
            seen_hashes.add(md5)

            ext = f".{base_image.get('ext', 'png')}"
            # 跳过不支持的格式
            if ext in (".wmf", ".emf", ".svg"):
                continue

            filename = f"{doc_basename}_p{page_num}_img{img_index:04d}{ext}"
            out_path = os.path.join(output_dir, filename)

            try:
                with open(out_path, "wb") as f:
                    f.write(blob)
            except Exception as e:
                print(f"      ⚠️ Failed to export PDF image xref={xref}: {e}")
                continue

            assets.append(ImageAsset(
                local_path=out_path,
                page_num=page_num,
                image_index=img_index,
                original_name=f"xref_{xref}",
            ))
            img_index += 1

    total_pages = min(len(pdf), max_pages)
    pdf.close()

    if assets:
        print(f"      [pdf-img] Extracted {len(assets)} unique images from {total_pages} pages")

    return assets


# ═══════════════════════════════════════════════════════════════
# XLSX — openpyxl _images 遍历
# ═══════════════════════════════════════════════════════════════

def extract_images_from_xlsx(
    local_path: str,
    output_dir: str,
) -> List[ImageAsset]:
    """
    从 XLSX 文件中提取嵌入图片。

    策略：遍历 openpyxl 每个 worksheet 的 _images 列表。

    Args:
        local_path: XLSX 文件的本地路径。
        output_dir: 图片导出目标目录。

    Returns:
        导出的 ImageAsset 列表（已 MD5 去重）。
    """
    try:
        from openpyxl import load_workbook
        from openpyxl.drawing.image import Image as XlImage
    except ImportError:
        return []

    assets: List[ImageAsset] = []
    seen_hashes: set = set()

    try:
        wb = load_workbook(local_path, data_only=True)
    except Exception as e:
        print(f"      ⚠️ Failed to open XLSX for image extraction: {e}")
        return []

    doc_basename = os.path.splitext(os.path.basename(local_path))[0]
    img_index = 0

    for sheet_idx, ws in enumerate(wb.worksheets):
        if not hasattr(ws, '_images'):
            continue

        for xl_img in ws._images:
            try:
                # openpyxl Image 对象的 _data 方法或 ref 属性
                if hasattr(xl_img, '_data') and callable(xl_img._data):
                    blob = xl_img._data()
                elif hasattr(xl_img, 'ref') and hasattr(xl_img.ref, 'read'):
                    xl_img.ref.seek(0)
                    blob = xl_img.ref.read()
                else:
                    continue
            except Exception:
                continue

            if not blob:
                continue

            # MD5 去重
            md5 = hashlib.md5(blob).hexdigest()
            if md5 in seen_hashes:
                continue
            seen_hashes.add(md5)

            # 推测扩展名
            ext = ".png"  # openpyxl 默认
            if blob[:3] == b'\xff\xd8\xff':
                ext = ".jpeg"
            elif blob[:4] == b'\x89PNG':
                ext = ".png"

            filename = f"{doc_basename}_sheet{sheet_idx}_img{img_index:04d}{ext}"
            out_path = os.path.join(output_dir, filename)

            try:
                with open(out_path, "wb") as f:
                    f.write(blob)
            except Exception as e:
                print(f"      ⚠️ Failed to export XLSX image: {e}")
                continue

            assets.append(ImageAsset(
                local_path=out_path,
                page_num=sheet_idx + 1,
                image_index=img_index,
                original_name=f"sheet{sheet_idx}_image",
            ))
            img_index += 1

    wb.close()

    if assets:
        print(f"      [xlsx-img] Extracted {len(assets)} unique images from {len(wb.worksheets)} sheets")

    return assets


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _content_type_to_ext(content_type: str, target_ref: str = "") -> str:
    """将 MIME content_type 或文件引用名转换为文件扩展名。"""
    ct_map = {
        "image/jpeg": ".jpeg",
        "image/jpg": ".jpeg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/webp": ".webp",
        "image/x-wmf": ".wmf",
        "image/x-emf": ".emf",
        "image/svg+xml": ".svg",
    }
    if content_type in ct_map:
        return ct_map[content_type]

    # fallback: 从 target_ref 推断（如 media/image3.jpeg）
    if target_ref:
        _, ext = os.path.splitext(target_ref)
        if ext:
            return ext.lower()

    return ".png"
