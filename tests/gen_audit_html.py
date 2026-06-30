#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTML 图文对照报告生成器 v2
使用 pipeline 的 extract_docx_with_images 作为唯一图片来源，
确保 image_index 对齐。
"""

import os, sys, glob, re, base64, shutil

os.environ["RAG_ENV"] = "local"

from opensearch_pipeline.extraction.docx_extractor import extract_docx_with_images
from opensearch_pipeline.chunker import DocumentChunker
import docx

OUT_DIR = "/Users/laijunchen/Downloads/opensearch-rag-pipeline/tests/step_card_audit"
if os.path.exists(OUT_DIR):
    shutil.rmtree(OUT_DIR)
os.makedirs(OUT_DIR, exist_ok=True)

chunker = DocumentChunker(
    max_chunk_chars=600, min_chunk_chars=20, overlap_chars=0,
    split_mode="step", prepend_title=True, prepend_section=True,
)

DOCS = [
    "fuling_chunk_exp/production_注塑事业部_FL-ZS-WI-002《奶茶杯测水试验》作业指导书.docx",
    "fuling_chunk_exp/production_注塑事业部_FL-ZS-WI-003《注塑原料领用》作业指导书.docx",
    "fuling_chunk_exp/it_富岭U8+成品仓库操作手册.docx",
    "fuling_chunk_exp/it_工资核算管理操作手册（2025年5月28日初版）.docx",
    "fuling_chunk_exp/it_富岭U8+财务部操作手册.docx",
    # OSS 新增 U8 操作手册
    "fuling_chunk_exp/oss_富岭U8+人事部操作手册.docx",
    # OSS 新增作业指导书
    "fuling_chunk_exp/oss_FL-QT-WI-014《外方验货工作》作业指导书.docx",
    "fuling_chunk_exp/oss_FL-XG-WI-008《吸管-纸吸管耐热测试》作业指导书-检验员.docx",
    "fuling_chunk_exp/oss_FL-XS-WI-001《吸塑数量本填写》作业指导书-班组长.docx",
    "fuling_chunk_exp/oss_FL-XS-WI-005《吸塑领料申请单打印》作业指导书-班组长.docx",
    "fuling_chunk_exp/oss_FL-XS-WI-006《吸塑交货单打印》作业指导书-班组长.docx",
    "fuling_chunk_exp/oss_FL-XS-WI-009《吸塑-产品入库打印》作业指导书-成品仓管.docx",
    "fuling_chunk_exp/oss_FL-XSNQ-WI-004《截单申报及报关资料缮制》作业指导书.docx",
]


def extract_image_blobs(docx_path, image_assets):
    """
    使用 pipeline 返回的 image_assets 中的 rel_id / target_ref
    直接从 docx package 中按 image_index 提取二进制图片。
    保证 image_index 和 pipeline 完全对齐。
    """
    doc = docx.Document(docx_path)
    blobs = {}  # image_index → (bytes, ext)

    # 方法 1: 通过 rel_id 直接查找
    for asset in image_assets:
        idx = asset.image_index
        orig = asset.original_name  # e.g. "media/image3.png"
        if idx in blobs:
            continue

        # 尝试通过遍历 rels 找到匹配的 target_ref
        for rel in doc.part.rels.values():
            if hasattr(rel, 'target_ref') and rel.target_ref == orig:
                try:
                    blob = rel.target_part.blob
                    ext = os.path.splitext(orig)[1] or ".png"
                    blobs[idx] = (blob, ext)
                    break
                except Exception:
                    pass

    return blobs


for doc_path in DOCS:
    full = os.path.join("/Users/laijunchen/Downloads/opensearch-rag-pipeline", doc_path)
    if not os.path.exists(full):
        continue
    bn = os.path.basename(full)
    label = bn.replace(".docx", "").replace(" ", "_")[:45]

    blocks, image_assets, _ = extract_docx_with_images(full)
    if not blocks:
        continue

    # 使用 pipeline 对齐的方法提取图片
    image_blobs = extract_image_blobs(full, image_assets)

    # ── 可选：VLM 真实调用（AUDIT_VLM=1 启用）──
    vlm_results = {}  # image_index -> {visual_summary, image_category, vlm_keywords, vlm_annotation_map}
    if os.environ.get("AUDIT_VLM") == "1":
        import tempfile
        from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor
        processor = ImageFunnelProcessor(simulate=False)
        tmp_dir = tempfile.mkdtemp()
        doc_title = bn.replace(".docx", "")
        vlm_count = 0
        for img_idx, (blob, ext) in image_blobs.items():
            # 写临时文件
            tmp_path = os.path.join(tmp_dir, f"img_{img_idx}{ext}")
            with open(tmp_path, "wb") as tf:
                tf.write(blob)
            result = processor.process_image(
                local_path=tmp_path, doc_id="AUDIT", is_public=True,
                doc_title=doc_title,
            )
            if result.get("visual_summary") or result.get("image_category"):
                vlm_results[img_idx] = {
                    "visual_summary": result.get("visual_summary", ""),
                    "image_category": result.get("image_category", ""),
                    "vlm_keywords": result.get("vlm_keywords", []),
                    "vlm_annotation_map": result.get("vlm_annotation_map", {}),
                }
                vlm_count += 1
        print(f"    [VLM] Processed {vlm_count}/{len(image_blobs)} images with real VLM")

    blocks_dict = [{"block_type": b.block_type, "text": b.text or "",
                    "page_num": b.page_num, "section_path": b.section_path,
                    "source": b.source or "native", "extra": b.extra or {}} for b in blocks]

    # 注入 VLM 结果到 image_ref blocks
    if vlm_results:
        for bd in blocks_dict:
            if bd["block_type"] == "image_ref":
                img_idx = bd["extra"].get("image_index")
                if img_idx in vlm_results:
                    bd["extra"].update(vlm_results[img_idx])

    meta = {"title": bn.replace(".docx", ""), "owner_dept": "test", "category_l1": "sop",
            "category_l2": "", "permission_level": "public", "kb_type": "public",
            "risk_level": "low", "source_oss_key": f"raw/{bn}"}
    chunks = chunker.chunk_from_blocks(blocks_dict, "AUDIT", 1, meta)
    step_cards = [c for c in chunks if c.chunk_type == "step_card"]

    # 看有多少 step_card 引用的图片能找到 blob
    total_refs = sum(len((c.extra or {}).get("image_refs", [])) for c in step_cards)
    found_refs = sum(1 for c in step_cards for r in (c.extra or {}).get("image_refs", [])
                     if r.get("image_index") in image_blobs)

    html_path = os.path.join(OUT_DIR, label, "audit_report.html")
    os.makedirs(os.path.dirname(html_path), exist_ok=True)

    html = [f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Audit: {bn}</title>
<style>
body {{ font-family: -apple-system, sans-serif; background:#0f0f1a; color:#e0e0e0; max-width:1200px; margin:0 auto; padding:20px; }}
h1 {{ color:#00d4ff; font-size:22px; }}
.stats {{ background:#16213e; border-radius:8px; padding:12px; margin:10px 0; font-size:14px; }}
.step {{ background:#16213e; border:1px solid #0f3460; border-radius:10px; padding:16px; margin:14px 0; }}
.step-head {{ color:#00d4ff; font-weight:bold; font-size:17px; margin-bottom:8px; }}
.step-text {{ background:#0f3460; border-radius:6px; padding:10px; white-space:pre-wrap; font-size:14px; line-height:1.6; }}
.anno {{ background:#1a2a0a; border:1px solid #3a5a1a; border-radius:6px; padding:8px; margin-top:8px; color:#90ee90; }}
.imgs {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; }}
.img-box {{ border:2px solid #444; border-radius:8px; padding:4px; background:#fff; max-width:380px; }}
.img-box img {{ max-width:100%; height:auto; }}
.img-lbl {{ text-align:center; font-size:11px; color:#888; margin-top:3px; }}
.meta {{ color:#888; font-size:12px; margin-top:6px; }}
.no-img {{ color:#ff6b6b; font-style:italic; }}
</style></head><body>
<h1>📋 Step Card 图文对照报告</h1>
<div class="stats">
  <b>文档:</b> {bn}<br>
  <b>step_card:</b> {len(step_cards)} | <b>pipeline 图片:</b> {len(image_assets)} 张 |
  <b>步骤引用图片:</b> {total_refs} | <b>图片命中:</b> {found_refs}
</div>
"""]

    for sc in step_cards:
        sno = (sc.extra or {}).get("step_no", "?")
        sec_no = (sc.extra or {}).get("section_no", "")
        sec = sc.section_title or "默认"
        imgs = (sc.extra or {}).get("image_refs", [])
        anno = (sc.extra or {}).get("annotation_map", {})
        circled = (sc.extra or {}).get("circled_refs", [])
        lines = [l for l in sc.chunk_text.split("\n") if not l.startswith("【文档:")]
        content = "\n".join(lines).strip()

        # 显示标签: §3.2.4 for heading steps, 步骤 N for text steps
        if sec_no:
            step_label = f"§{sec_no}"
        elif sno == 0:
            step_label = "§"
        else:
            step_label = f"步骤 {sno}"

        html.append(f'<div class="step">')
        html.append(f'<div class="step-head">{step_label} · {sec[:40]}</div>')
        html.append(f'<div class="step-text">{content[:800]}</div>')

        if anno:
            anno_items = ", ".join(f"{k}={v}" for k, v in sorted(anno.items()))
            html.append(f'<div class="anno">📌 标注: {anno_items}</div>')

        if circled:
            html.append(f'<div class="meta">引用圈数字: {"".join(circled)}</div>')

        # relation audit flags
        audit_flags = (sc.extra or {}).get("relation_audit", [])
        if audit_flags:
            for af in audit_flags:
                html.append(f'<div class="meta" style="color:#e67e22">⚠️ 审计: img={af["image_index"]} '
                           f'{af["relation"]}({af["confidence"]:.2f}) — {af["reason"]}</div>')

        if imgs:
            html.append('<div class="imgs">')
            for im in imgs:
                idx = im.get("image_index")
                tref = im.get("target_ref", "")
                rel = im.get("relation", "")
                rel_conf = im.get("relation_confidence", 0)
                caption = im.get("caption", "")
                img_cat = im.get("image_category", "")
                vlm_kw = im.get("vlm_keywords", [])
                vlm_anno = im.get("vlm_annotation_map", {})

                # relation 标签样式（大号醒目）
                badge_style = "padding:2px 10px;border-radius:4px;font-size:13px;font-weight:bold;"
                rel_badge = ""
                if rel == "primary":
                    rel_badge = f'<span style="background:#27ae60;color:#fff;{badge_style}">🟢 primary</span>'
                elif rel == "supporting":
                    rel_badge = f'<span style="background:#2980b9;color:#fff;{badge_style}">🔵 supporting</span>'
                elif rel == "visual_knowledge":
                    rel_badge = f'<span style="background:#8e44ad;color:#fff;{badge_style}">🟣 visual_knowledge</span>'

                # image_category 标签
                cat_badge = ""
                if img_cat and img_cat != "unknown":
                    cat_colors = {
                        "step_screenshot": "#e67e22",
                        "test_photo": "#16a085",
                        "inspection_photo": "#d35400",
                        "form_image": "#2c3e50",
                        "visual_knowledge": "#8e44ad",
                        "decorative": "#7f8c8d",
                    }
                    bg = cat_colors.get(img_cat, "#555")
                    cat_badge = f'<span style="background:{bg};color:#fff;padding:1px 6px;border-radius:3px;font-size:11px">{img_cat}</span>'

                if idx is not None and idx in image_blobs:
                    blob, ext = image_blobs[idx]
                    b64 = base64.b64encode(blob).decode()
                    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                            "gif": "image/gif", "emf": "image/x-emf", "wmf": "image/x-wmf"
                            }.get(ext.lstrip(".").lower(), "image/png")
                    html.append(f'<div class="img-box"><img src="data:{mime};base64,{b64}"/>')

                    # 第一行：idx + relation badge + confidence
                    label_parts = [f'<b>#{idx}</b>']
                    if rel_badge: label_parts.append(rel_badge)
                    if rel_conf: label_parts.append(f'<span style="color:#aaa">conf={rel_conf:.2f}</span>')
                    html.append(f'<div class="img-lbl" style="font-size:13px;margin-top:5px">{" · ".join(label_parts)}</div>')

                    # 第二行：category badge + target_ref
                    if cat_badge or tref:
                        meta_parts = []
                        if cat_badge: meta_parts.append(cat_badge)
                        if tref: meta_parts.append(f'<span style="color:#666">{tref}</span>')
                        html.append(f'<div class="img-lbl" style="margin-top:2px">{" · ".join(meta_parts)}</div>')

                    # 第三行：VLM caption
                    if caption:
                        html.append(f'<div class="img-lbl" style="color:#7fb3d8;font-size:12px;margin-top:3px">💬 {caption[:120]}</div>')

                    # 第四行：VLM keywords
                    if vlm_kw:
                        kw_tags = " ".join(f'<span style="background:#333;color:#aaa;padding:1px 4px;border-radius:2px;font-size:10px">{k}</span>' for k in vlm_kw[:8])
                        html.append(f'<div class="img-lbl" style="margin-top:2px">🏷️ {kw_tags}</div>')

                    # 第五行：VLM annotation_map
                    if vlm_anno:
                        anno_str = " | ".join(f'{k}→{v}' for k, v in vlm_anno.items())
                        html.append(f'<div class="img-lbl" style="color:#e8a838;font-size:11px;margin-top:2px">📌 {anno_str}</div>')

                    html.append('</div>')
                else:
                    html.append(f'<div class="img-box"><div class="no-img">⚠️ idx={idx} 无法加载</div></div>')
            html.append('</div>')
        else:
            html.append('<div class="meta">（无新图片 — 可能引用上文图片标注）</div>')

        html.append('</div>')

    html.append('</body></html>')
    with open(html_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    print(f"  ✅ {label}/audit_report.html ({len(step_cards)} steps, pipeline imgs={len(image_assets)}, blobs={len(image_blobs)}, refs={found_refs}/{total_refs})")

print(f"\n  完成。")
