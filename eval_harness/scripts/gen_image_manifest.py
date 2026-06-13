#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_image_manifest.py — 跑 UnifiedExtractor 出一份逐图清单(供人工标 GT 用)

用法:
  python -m eval_harness.scripts.gen_image_manifest \
      --doc-path ~/Downloads/opensearch-rag-data/eval_samples/documents/pdf_sop.pdf \
      --doc-label pdf_sop \
      --out scratch/eval_manifest/pdf_sop_images.json

产出(manifest JSON):
{
  "_meta": {
    "doc_label": "pdf_sop",
    "fmt": "pdf",
    "extractor_version": "<UnifiedExtractor 输出哈希>",
    "doc_sha256": "<源文档防替换>",
    "generated_at": "2026-06-12T20:50",
    "n_images": 12
  },
  "images": [
    {
      "asset_index": 1,
      "ref_key": {"page": 3, "in_page_idx": 1},   # 按格式自动推
      "filename": "...",
      "page_num": 3,
      "visual_summary": "...(VLM 描述,80 字)",
      "ocr_preview": "...(OCR 文本前 80 字)",
      "image_category": "operation_screenshot",
      "local_path": "/tmp/..."   # 缩略图可选
    }
  ]
}

人工标注流程:
  1. 跑 gen_image_manifest 产 manifest
  2. 打开 gt_pdf_analysis.json,逐 step 抄 ref_key 进 expected_image_refs
  3. 提交后 CI 跑 validate_gt_refs.py 校验 ref_key 全在 manifest

PPTX 暂不支持(生产 0 step_card,Day 1 audit 已确认)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime


def _doc_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _detect_fmt(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in ("docx", "pdf", "xlsx", "pptx"):
        return ext
    raise ValueError(f"不支持的扩展名: {ext}(仅 docx/pdf/xlsx/pptx)")


def _build_ref_key(fmt: str, asset: dict) -> dict:
    """从 asset dict 推出 ref_key(union ImageRef 形态)。

    PDF 用 extractor 给的全文 image_index(2026-06-12 修正,原按页重启 in_page_idx
    与生产 chunker 的全文 image_index 不匹配,导致 GT 坐标系错位)。
    """
    rk: dict = {}
    if fmt == "docx":
        rk["image_index"] = asset.get("image_index")
    elif fmt == "pdf":
        # PDF 同 docx:用 extractor 的全文 image_index;page 作辅助可读
        rk["image_index"] = asset.get("image_index")
        rk["page"] = asset.get("page_num")
    elif fmt == "xlsx":
        # xlsx 优先用 anchor_row(行级精度),回退 image_index
        if asset.get("anchor_row") is not None:
            rk["block_index"] = asset["anchor_row"]
        else:
            rk["block_index"] = asset.get("image_index")
    elif fmt == "pptx":
        # 最小支持:slide_no 来自 page_num,shape_idx 无现成字段 → 留空
        rk["slide_no"] = asset.get("page_num")
    return {k: v for k, v in rk.items() if v is not None}


def gen_manifest(doc_path: str, doc_label: str) -> dict:
    """跑 UnifiedExtractor 出 manifest dict。注意:本函数需要真依赖(prod env),
    模拟模式可能图为空 — 调用前应确保 RAG_SIMULATE=false 或在能跑 extractor 的环境。"""
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor

    fmt = _detect_fmt(doc_path)

    extractor = UnifiedExtractor()
    task = {
        "doc_id": f"MANIFEST_{doc_label}",
        "version_no": 1,
        "raw_key": f"raw/{doc_label}.{fmt}",
        "file_ext": fmt,
        "local_path": doc_path,
        "doc_title": doc_label,
    }
    result = extractor.extract(task)
    assets = result.assets or []

    # 全格式统一:直接用 extractor 的 asset 顺序 + image_index(2026-06-12 简化,
    # 不再为 PDF 按页重启 — extractor 已给全文连续 image_index)
    images_out: list[dict] = []
    for i, a in enumerate(assets, start=1):
        images_out.append({
            "asset_index": i,
            "ref_key": _build_ref_key(fmt, a),
            "filename": a.get("filename"),
            "page_num": a.get("page_num"),
            "visual_summary": (a.get("visual_summary") or "")[:80],
            "ocr_preview": (a.get("ocr_text") or "")[:80],
            "image_category": a.get("image_category"),
            "local_path": a.get("local_path"),
            "status": a.get("status"),
        })

    # extractor_version: 由 extract_method + result.text_length 派生(简版,
    # 真正版本锁应该用 extractor 模块的 git sha;follow-up 改进)
    ev_payload = f"{result.extract_method}|len={result.text_length}|assets={len(assets)}"
    extractor_version = hashlib.sha1(ev_payload.encode()).hexdigest()[:16]

    return {
        "_meta": {
            "doc_label": doc_label,
            "fmt": fmt,
            "extractor_version": extractor_version,
            "doc_sha256": _doc_sha256(doc_path),
            "generated_at": datetime.now().isoformat(timespec="minutes"),
            "n_images": len(images_out),
            "extract_method": result.extract_method,
        },
        "images": images_out,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-path", required=True, help="源文档本地路径")
    ap.add_argument("--doc-label", required=True, help="GT label(如 pdf_sop)")
    ap.add_argument("--out", required=True, help="manifest JSON 输出路径")
    args = ap.parse_args()

    manifest = gen_manifest(args.doc_path, args.doc_label)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(manifest, open(args.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"✓ {args.doc_label} ({manifest['_meta']['fmt']}): "
          f"{manifest['_meta']['n_images']} images → {args.out}")
    print(f"  extractor_version={manifest['_meta']['extractor_version']}, "
          f"doc_sha256={manifest['_meta']['doc_sha256'][:12]}…")


if __name__ == "__main__":
    main()
