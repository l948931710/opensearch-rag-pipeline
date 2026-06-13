# -*- coding: utf-8 -*-
"""eval_harness.binding — 统一图文绑定评测核心(2026-06-12)

把"摄入侧绑定精度"的 4 种坐标系(DOCX image_index / PDF page+idx /
XLSX block_index / PPTX slide+shape)抽到独立子包,关注点分离:

  - ref_keys.py        union ImageRef dataclass + per-fmt 匹配 + Jaccard
                       (empty-vs-empty=1.0 显式负例正确)
  - gt_loader.py       GT JSON 加载 + extractor_version/doc_sha256 锁
                       + degraded weak GT 标记(GT 半完工时排除主闸)
  - ingestion_binding  跑真 UnifiedExtractor + DocumentChunker 出逐格式 Jaccard
                       (本文件,即 l4_multimodal.ingestion 支柱的核心)

设计依据:工作流 wu71s7igd 3 评委一致采纳 UNIFIED-L4 + 抢救 SplitL4 的关注点
分离思路,这样未来若需拆出 l4_binding.py 仅需一行 import 改动。
"""

from .ref_keys import ImageRef, jaccard, parse_ref_dict
from .gt_loader import GtChunk, GtDoc, load_gt, validate_gt_against_manifest

__all__ = [
    "ImageRef", "jaccard", "parse_ref_dict",
    "GtChunk", "GtDoc", "load_gt", "validate_gt_against_manifest",
]
