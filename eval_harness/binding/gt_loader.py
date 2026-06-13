# -*- coding: utf-8 -*-
"""gt_loader.py — 图文绑定 GT 加载 + 完整性校验

GT JSON 形状(eval_samples/ground_truth/ 下,2026-06-12 schema v1):

  顶层:{
    "_meta": {
      "image_ref_scheme": "v1",
      "extractor_version": "<UnifiedExtractor 输出哈希>",  // 防 _walk 顺序漂移
      "skip_in_binding": ["xlsx_inspect"],                 // 显式排除主闸的 doc
      ...
    },
    "<doc_label>": {
      "_doc_meta": {
        "doc_sha256": "<源文档防替换>",
        "image_manifest_path": "scratch/eval_manifest/<label>_images.json",
        "degraded": false,           // GT 半完工时设 true 排除主闸
      },
      "gt_chunks": [
        {
          "label": "...",
          "chunk_type": "step_card",
          "keywords": [...],
          "expected_images": 2,             // 旧字段保留 fallback
          "expected_image_refs": [          // 新字段 v1,可选
            {"image_index": 3},
            {"page": 3, "in_page_idx": 1}
          ]
        }
      ]
    }
  }

degraded 路径:GT 没标 expected_image_refs 或只标了 weak ref(page-only/slide-only),
都允许跑 presence 评测,但 deterministic 字典里标 `degraded=True`、
build_gates 不计入 hard 闸,只算趋势监控。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .ref_keys import ImageRef, parse_ref_dict


@dataclass
class GtChunk:
    """一个 GT chunk 的 binding 期望。"""
    label: str
    chunk_type: str
    keywords: List[str] = field(default_factory=list)
    expected_image_refs: List[ImageRef] = field(default_factory=list)
    expected_image_count: Optional[int] = None     # 旧 presence-only fallback
    has_strong_refs: bool = False                  # expected_image_refs 全非 weak
    page: Optional[int] = None                     # source_location 用


@dataclass
class GtDoc:
    """一个 GT doc 的所有 chunk + meta。"""
    label: str
    fmt: str                              # docx/pdf/xlsx/pptx
    doc_sha256: Optional[str]
    extractor_version: Optional[str]
    manifest_path: Optional[str]
    degraded: bool                        # GT 半完工 → 排除主闸
    gt_chunks: List[GtChunk] = field(default_factory=list)


def _detect_fmt(label: str) -> str:
    """从 doc_label 推格式(eval_samples 命名约定: docx_sop / pdf_sop / xlsx_inspect)。"""
    for fmt in ("docx", "pdf", "xlsx", "pptx"):
        if label.startswith(fmt + "_") or label == fmt:
            return fmt
    return "unknown"


def _parse_chunk(d: Dict[str, Any], fmt: str) -> GtChunk:
    raw_refs = d.get("expected_image_refs") or []
    refs = [parse_ref_dict(r, fmt) for r in raw_refs]
    has_strong = bool(refs) and all(not r.is_weak() for r in refs)
    return GtChunk(
        label=d.get("label", ""),
        chunk_type=d.get("chunk_type", ""),
        keywords=d.get("keywords") or [],
        expected_image_refs=refs,
        expected_image_count=d.get("expected_images"),
        has_strong_refs=has_strong,
        page=d.get("page"),
    )


def load_gt(gt_json_path: str) -> Dict[str, GtDoc]:
    """加载 ground_truth/*.json,出 {doc_label: GtDoc}。

    skip_in_binding 列出的 doc 直接 degraded=True(主闸不计)。
    每 doc 自带 degraded 也尊重。
    """
    data = json.load(open(gt_json_path, encoding="utf-8"))
    meta = data.get("_meta") or {}
    skip_set = set(meta.get("skip_in_binding") or [])
    extractor_version = meta.get("extractor_version")

    out: Dict[str, GtDoc] = {}
    for label, doc in data.items():
        if label.startswith("_"):  # _meta 等
            continue
        if not isinstance(doc, dict):
            continue
        fmt = _detect_fmt(label)
        doc_meta = doc.get("_doc_meta") or {}
        chunks_raw = doc.get("gt_chunks") or []
        chunks = [_parse_chunk(c, fmt) for c in chunks_raw]
        degraded = bool(doc_meta.get("degraded")) or (label in skip_set)
        out[label] = GtDoc(
            label=label, fmt=fmt,
            doc_sha256=doc_meta.get("doc_sha256"),
            extractor_version=doc_meta.get("extractor_version") or extractor_version,
            manifest_path=doc_meta.get("image_manifest_path"),
            degraded=degraded,
            gt_chunks=chunks,
        )
    return out


def validate_gt_against_manifest(gt: GtDoc, manifest_path: str) -> Dict[str, Any]:
    """校验 GT 的 ref_key 全部存在于 manifest,extractor_version + doc_sha256 锁档一致。

    返回:{ok: bool, missing_refs: [...], reasons: [...]}
    用法:CI / Day 5 跑 run_eval 前预飞,失败硬报错防止评出错指标。
    """
    reasons: List[str] = []
    if not os.path.exists(manifest_path):
        return {"ok": False, "missing_refs": [], "reasons": [f"manifest not found: {manifest_path}"]}
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    m_meta = manifest.get("_meta") or {}

    # extractor_version 锁
    if gt.extractor_version and m_meta.get("extractor_version"):
        if gt.extractor_version != m_meta["extractor_version"]:
            reasons.append(
                f"extractor_version 漂移: gt={gt.extractor_version} manifest={m_meta['extractor_version']}"
            )
    # doc_sha256 锁
    if gt.doc_sha256 and m_meta.get("doc_sha256"):
        if gt.doc_sha256 != m_meta["doc_sha256"]:
            reasons.append(
                f"doc_sha256 不匹配: gt={gt.doc_sha256[:12]}… manifest={m_meta['doc_sha256'][:12]}…"
            )

    # ref_key 存在性校验
    manifest_keys = {
        tuple(sorted(item.get("ref_key", {}).items()))
        for item in (manifest.get("images") or [])
    }
    missing: List[Dict[str, Any]] = []
    for c in gt.gt_chunks:
        for ref in c.expected_image_refs:
            # 从 ImageRef 构造规范化 dict 与 manifest 比对
            ref_dict = {k: v for k, v in {
                "image_index": ref.image_index, "page": ref.page,
                "in_page_idx": ref.in_page_idx, "block_index": ref.block_index,
                "slide_no": ref.slide_no, "shape_idx": ref.shape_idx,
            }.items() if v is not None}
            if not ref_dict:
                continue
            key = tuple(sorted(ref_dict.items()))
            if key not in manifest_keys:
                missing.append({"chunk_label": c.label, "ref": ref_dict})
    if missing:
        reasons.append(f"{len(missing)} 个 GT ref 在 manifest 找不到")

    return {"ok": not reasons, "missing_refs": missing, "reasons": reasons}
