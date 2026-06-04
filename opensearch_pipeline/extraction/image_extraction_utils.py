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

            # 提取 anchor 行号（用于行级图片绑定）
            anchor_row = None
            anchor = getattr(xl_img, 'anchor', None)
            if anchor and hasattr(anchor, '_from'):
                anchor_row = getattr(anchor._from, 'row', None)

            asset = ImageAsset(
                local_path=out_path,
                page_num=sheet_idx + 1,
                image_index=img_index,
                original_name=f"sheet{sheet_idx}_image",
            )
            if anchor_row is not None:
                asset.anchor_row = anchor_row
            assets.append(asset)
            img_index += 1

    wb.close()

    # ── 后处理：从 Drawing XML 提取 group 编号标注 ──
    # openpyxl 的 _images 无法读取 grpSp 里的文字标注（如①②③序号圆圈）
    # 需要直接解析 xlsx zip 内的 drawing XML
    if assets:
        try:
            _enrich_xlsx_annotations(local_path, assets, doc_basename)
        except Exception as e:
            print(f"      ⚠️ Drawing XML annotation extraction failed: {e}")

        print(f"      [xlsx-img] Extracted {len(assets)} unique images from {len(wb.worksheets)} sheets")

    return assets


def _enrich_xlsx_annotations(xlsx_path: str, assets: List[ImageAsset], doc_basename: str):
    """从 XLSX Drawing XML 解析标注编号，绑定到对应图片。

    支持两种 Drawing 结构：
      A) grpSp 分组：图片和标注在同一个 <xdr:grpSp> 内（直接配对）
      B) standalone：图片和标注是独立的 <xdr:twoCellAnchor>（按坐标邻近配对）
    """
    import zipfile
    import xml.etree.ElementTree as ET

    ns = {
        'xdr': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing',
        'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
        'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    }

    def _get_anchor_pos(anchor_el):
        """从 <xdr:from> 提取 (row, col, colOff) 绝对位置。"""
        from_el = anchor_el.find('xdr:from', ns)
        if from_el is None:
            return None, None, 0
        row = int(from_el.find('xdr:row', ns).text)
        col = int(from_el.find('xdr:col', ns).text)
        col_off = int(from_el.find('xdr:colOff', ns).text)
        return row, col, col_off

    def _find_asset_by_md5(media_blob, sheet_idx):
        """通过 MD5 找到对应的 ImageAsset。"""
        if not media_blob:
            return None
        media_md5 = hashlib.md5(media_blob).hexdigest()
        for asset in assets:
            if asset.page_num != sheet_idx + 1:
                continue
            try:
                with open(asset.local_path, 'rb') as f:
                    asset_md5 = hashlib.md5(f.read()).hexdigest()
                if asset_md5 == media_md5:
                    return asset
            except Exception:
                continue
        return None

    with zipfile.ZipFile(xlsx_path) as z:
        drawing_files = sorted([n for n in z.namelist()
                                if n.startswith('xl/drawings/drawing') and n.endswith('.xml')])
        rels_files = {n for n in z.namelist()
                      if n.startswith('xl/drawings/_rels/') and n.endswith('.rels')}

        for drawing_path in drawing_files:
            import re
            m = re.search(r'drawing(\d+)\.xml$', drawing_path)
            if not m:
                continue
            drawing_num = int(m.group(1))
            sheet_idx = drawing_num - 1

            # 解析 rels → rId → media filename
            rels_path = f'xl/drawings/_rels/drawing{drawing_num}.xml.rels'
            rid_to_media = {}
            if rels_path in rels_files:
                rels_root = ET.fromstring(z.read(rels_path))
                for rel in rels_root:
                    rid = rel.get('Id', '')
                    target = rel.get('Target', '')
                    if 'image' in target.lower() or 'media' in target.lower():
                        rid_to_media[rid] = os.path.basename(target)

            if not rid_to_media:
                continue

            root = ET.fromstring(z.read(drawing_path))

            # ── 方式 A: grpSp 分组配对 ──
            grp_matched = set()  # 已通过 grpSp 配对的 asset id
            for anchor in root.findall('xdr:twoCellAnchor', ns):
                grp = anchor.find('xdr:grpSp', ns)
                if grp is None:
                    continue

                pics = grp.findall('.//xdr:pic', ns)
                sps = grp.findall('.//xdr:sp', ns)
                if not pics:
                    continue

                group_texts = []
                for sp in sps:
                    for t_el in sp.findall('.//a:t', ns):
                        if t_el.text and t_el.text.strip():
                            group_texts.append(t_el.text.strip())

                annotation_nums = [t for t in group_texts if t.isdigit()]
                annotation_labels = [t for t in group_texts if not t.isdigit()]

                for pic in pics:
                    blip = pic.find(
                        './/{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
                    if blip is None:
                        continue
                    r_id = blip.get(
                        '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed', '')
                    if not r_id or r_id not in rid_to_media:
                        continue

                    media_name = rid_to_media[r_id]
                    media_path = f'xl/media/{media_name}'
                    media_blob = z.read(media_path) if media_path in z.namelist() else None
                    matched_asset = _find_asset_by_md5(media_blob, sheet_idx)
                    if matched_asset:
                        if annotation_nums:
                            matched_asset.annotation_num = int(annotation_nums[0])
                        if annotation_labels:
                            matched_asset.annotation_label = ', '.join(annotation_labels)
                        grp_matched.add(id(matched_asset))

            # ── 方式 B: standalone 邻近配对 ──
            # 收集所有独立的 PIC 和 TEXT anchor
            standalone_pics = []  # [(row, col, col_off, rId)]
            standalone_texts = []  # [(row, col, col_off, text)]

            for anchor in root.findall('xdr:twoCellAnchor', ns):
                if anchor.find('xdr:grpSp', ns) is not None:
                    continue  # 已在方式 A 处理

                row, col, col_off = _get_anchor_pos(anchor)
                if row is None:
                    continue

                pic = anchor.find('xdr:pic', ns)
                sp = anchor.find('xdr:sp', ns)

                if pic is not None:
                    blip = pic.find(
                        './/{http://schemas.openxmlformats.org/drawingml/2006/main}blip')
                    if blip is not None:
                        r_id = blip.get(
                            '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed', '')
                        if r_id and r_id in rid_to_media:
                            standalone_pics.append((row, col, col_off, r_id))
                elif sp is not None:
                    texts = [t.text for t in sp.findall('.//a:t', ns) if t.text]
                    text = ''.join(texts).strip()
                    if text and text.isdigit():
                        standalone_texts.append((row, col, col_off, text))

            if standalone_pics and standalone_texts:
                # 全局贪心匹配：计算所有 (TEXT, PIC) 对的距离，按距离排序，
                # 每个 PIC 和 TEXT 只匹配一次
                EMU_PER_COL = 9525 * 100

                all_pairs = []  # [(distance, text_idx, pic_idx)]
                for ti, (t_row, t_col, t_col_off, t_text) in enumerate(standalone_texts):
                    t_abs = t_col * EMU_PER_COL + t_col_off
                    for pi, (p_row, p_col, p_col_off, p_rid) in enumerate(standalone_pics):
                        row_dist = abs(p_row - t_row)
                        if row_dist > 5:
                            continue
                        p_abs = p_col * EMU_PER_COL + p_col_off
                        col_dist = abs(p_abs - t_abs)
                        total_dist = row_dist * 100000000 + col_dist
                        all_pairs.append((total_dist, ti, pi))

                all_pairs.sort()  # 按距离从近到远

                matched_texts = set()
                matched_pics = set()

                for _, ti, pi in all_pairs:
                    if ti in matched_texts or pi in matched_pics:
                        continue  # 已配对，跳过

                    t_text = standalone_texts[ti][3]
                    p_rid = standalone_pics[pi][3]

                    media_name = rid_to_media[p_rid]
                    media_path = f'xl/media/{media_name}'
                    media_blob = z.read(media_path) if media_path in z.namelist() else None
                    matched_asset = _find_asset_by_md5(media_blob, sheet_idx)
                    if matched_asset and id(matched_asset) not in grp_matched:
                        matched_asset.annotation_num = int(t_text)
                        matched_texts.add(ti)
                        matched_pics.add(pi)


# ═══════════════════════════════════════════════════════════════
# PPTX — python-pptx relationship 遍历
# ═══════════════════════════════════════════════════════════════

def extract_images_from_pptx(
    local_path: str,
    output_dir: str,
) -> List[ImageAsset]:
    """
    从 PPTX 文件中提取所有嵌入图片。

    策略：PPTX 是 OOXML 格式，和 DOCX 类似。
    遍历每个 slide 的 part.rels 中 reltype 包含 'image' 的关系，
    读取 target_part.blob 获取图片二进制数据。

    每张图片会记录所在的 slide 编号作为 page_num。

    Args:
        local_path: PPTX 文件的本地路径。
        output_dir: 图片导出目标目录。

    Returns:
        导出的 ImageAsset 列表（已 MD5 去重）。
    """
    try:
        from pptx import Presentation
    except ImportError:
        return []

    assets: List[ImageAsset] = []
    seen_hashes: set = set()

    try:
        prs = Presentation(local_path)
    except Exception as e:
        print(f"      ⚠️ Failed to open PPTX for image extraction: {e}")
        return []

    doc_basename = os.path.splitext(os.path.basename(local_path))[0]
    img_index = 0

    for slide_idx, slide in enumerate(prs.slides):
        slide_num = slide_idx + 1

        for rel in slide.part.rels.values():
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
            filename = f"{doc_basename}_slide{slide_num}_img{img_index:04d}{ext}"
            out_path = os.path.join(output_dir, filename)

            try:
                with open(out_path, "wb") as f:
                    f.write(blob)
            except Exception as e:
                print(f"      ⚠️ Failed to export PPTX image {rel.target_ref}: {e}")
                continue

            assets.append(ImageAsset(
                local_path=out_path,
                page_num=slide_num,
                image_index=img_index,
                original_name=rel.target_ref,
            ))
            img_index += 1

    total_rels = sum(
        1 for slide in prs.slides
        for rel in slide.part.rels.values()
        if "image" in rel.reltype
    )
    if assets:
        print(f"      [pptx-img] Extracted {len(assets)} unique images "
              f"(deduped from {total_rels} refs across {len(prs.slides)} slides)")

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
