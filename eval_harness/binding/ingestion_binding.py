# -*- coding: utf-8 -*-
"""
ingestion_binding.py — 摄入侧图文绑定精度聚合(逐格式 Jaccard)

复用 gt_eval.py 的 production-faithful 路由(node_chunk_documents),
不重写 chunker 调用。流程:

  GT JSON → 每 doc:
     load_gt → extract_doc → chunk_faithful → match GT-chunk to produced-chunk
                                              (keyword density matcher)
              extract pred ImageRefs from chunk.extra.image_refs
              jaccard(gt_refs, pred_refs) per GT chunk(只算 has_strong_refs 的)
              img_dup_factor over all step_cards

聚合契约(l4_multimodal ingestion 支柱直接用):

{
  "deterministic": {
    "binding_jaccard_pdf":  0.X,  # 全部 strong PDF GT chunk 的 Jaccard 均值
    "binding_jaccard_xlsx": 0.X,
    "binding_jaccard_docx": 0.X,
    "binding_jaccard_pptx": None,  # PPTX 默认 degraded
    "img_dup_factor_p95":   1.X,  # 全文档 step_card image_refs/unique 比 p95
    "img_dup_factor_max":   1.X,
    "per_fmt": {"pdf": {n_docs, n_chunks_scored, mean_jaccard, std, n_strong_chunks}, ...},
    "n_degraded": 1,             # GT 半完工被排除的 doc 数
    "errors": [...],             # 单 doc 失败列表(fail-open)
  },
  "per_doc": [{...逐 doc 详情}],
  "judge_bundle_binding": [{...给 Claude 评 image_binding 维度用}],
}

⚠️ 本模块只跑摄入侧(extract+chunk),不碰 HA3/embedding/LLM,适合本地或 prod_ro 都能跑。
⚠️ degraded GT doc 自动 skip 主闸,但 per_doc 仍记录(供趋势监控)。
"""
from __future__ import annotations

import contextlib
import io
import math
import os
import shutil
import statistics
import tempfile
from typing import Any, Dict, List, Optional

from .ref_keys import ImageRef, jaccard, img_dup_factor, parse_ref_dict
from .gt_loader import GtChunk, GtDoc, load_gt


MATCH_THRESHOLD = 0.3  # GT-chunk → produced-chunk: keyword recall 下限(与 gt_eval 同口径)


def _doc_path(docs_dir: str, label: str, fmt: str) -> Optional[str]:
    """按 eval_samples 命名约定推 doc 路径。"""
    candidates = [f"{label}.{fmt}", f"{label}.docx", f"{label}.pdf",
                  f"{label}.xlsx", f"{label}.pptx"]
    for fname in candidates:
        p = os.path.join(docs_dir, fname)
        if os.path.exists(p):
            return p
    return None


def _extract_and_chunk(label: str, fmt: str, doc_path: str) -> List[Any]:
    """跑 production-faithful 路由,出 chunks list。

    复用 gt_eval.py 的 chunk_faithful 模式 — 不重写,确保与 prod chunker routing 一致。
    """
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    from opensearch_pipeline.pipeline_nodes import node_chunk_documents

    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, os.path.basename(doc_path))
        shutil.copy2(doc_path, p)
        ex = UnifiedExtractor()
        r = ex.extract({
            "doc_id": label, "version_no": 1, "local_path": p, "file_ext": fmt,
            "filename": os.path.basename(doc_path), "raw_key": f"raw/eval/{label}",
            "_tmp_dir": tmp,
        })

        doc = {
            "doc_id": label, "version_no": 1, "title": label, "filename": label,
            "file_ext": fmt, "text": r.text, "blocks": r.blocks, "assets": r.assets,
            "source_key": f"raw/eval/{label}", "canonical_key": "", "owner_dept": "eval",
            "category_l1": "", "category_l2": "", "permission_level": "public",
            "kb_type": "public", "risk_level": "low", "redaction_action": "CLEAN",
        }
        ctx = {"canonicals": [doc], "split_mode": "dynamic",
               "prepend_title": True, "prepend_section": True}
        with contextlib.redirect_stdout(io.StringIO()):
            node_chunk_documents(ctx)
        return ctx["chunks"]


def _gv(obj: Any, key: str, default: Any = None) -> Any:
    """安全取属性 — 兼容 Chunk dataclass 和 dict。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _match_gt_chunk_to_produced(gt: GtChunk, chunks: List[Any]) -> Optional[Any]:
    """keyword recall ≥ 0.3 → density 最大者,但**chunk_type 同类型优先**。

    口径同 gt_eval.py 的 covering+density 但加一道 chunk_type 偏好:GT 标 step_card
    时,先在 covering 集里找 chunk_type=step_card 的 candidate;若有,density 最大者
    胜。否则回退全 covering 集。

    Why:density = hits / sqrt(len) 让短文本不公平胜出 — 实测 pdf_sop 步骤1.1 时,
    前言段(text_chunk, 123 字, 4 hits = density 0.36)恰好比完整步骤段(step_card,
    300 字, 6 hits = density 0.35)略胜,导致 matcher 选错 chunk 类型,使该 GT
    被无图的 text_chunk 抢戏 jaccard=0。chunk_type 优先反映了 GT 标注者的真实意图。
    """
    kws = gt.keywords
    if not kws:
        return None

    scored = []
    for c in chunks:
        text = (_gv(c, "chunk_text") or "")
        if not text:
            continue
        hits = sum(1 for kw in kws if kw.lower() in text.lower())
        recall = hits / len(kws) if kws else 0.0
        density = hits / math.sqrt(max(len(text), 1))
        scored.append((c, recall, density))

    covering = [s for s in scored if s[1] >= MATCH_THRESHOLD]
    if covering:
        # chunk_type 同类型优先 — 给 GT 标的 chunk_type 一道偏好;无同类型回退全集
        if gt.chunk_type:
            typed = [s for s in covering if _gv(s[0], "chunk_type") == gt.chunk_type]
            if typed:
                return max(typed, key=lambda s: s[2])[0]
        return max(covering, key=lambda s: s[2])[0]
    return max(scored, key=lambda s: s[1])[0] if scored else None


def _pred_refs_from_chunk(chunk: Any, fmt: str) -> List[ImageRef]:
    """从 chunk.extra.image_refs 列出 pred ImageRef(parse_ref_dict 已容错 page_num/anchor_row 别名)。"""
    extra = _gv(chunk, "extra") or {}
    raw_refs = extra.get("image_refs") or []
    return [parse_ref_dict(r, fmt) for r in raw_refs]


def _all_step_card_refs(chunks: List[Any], fmt: str) -> List[ImageRef]:
    """全文档 step_card 的 image_refs 平铺集合(用于 img_dup_factor)。"""
    out: List[ImageRef] = []
    for c in chunks:
        if _gv(c, "chunk_type") != "step_card":
            continue
        out.extend(_pred_refs_from_chunk(c, fmt))
    return out


def _doc_to_judge_bundle_item(
    label: str, fmt: str, gt: GtChunk, produced: Any, pred_refs: List[ImageRef],
    score: float,
) -> Dict[str, Any]:
    """构造 image_binding judge bundle item — 给 Claude 维度评审用。"""
    extra = _gv(produced, "extra") or {}
    img_refs_raw = extra.get("image_refs") or []
    return {
        "qid": f"{label}::{gt.label}",
        "kind": "binding",
        "fmt": fmt,
        "gt_label": gt.label,
        "gt_keywords": gt.keywords,
        "expected_image_refs": [
            {k: v for k, v in r.__dict__.items() if v is not None and k != "fmt"}
            for r in gt.expected_image_refs
        ],
        "produced_chunk_type": _gv(produced, "chunk_type"),
        "produced_chunk_text_excerpt": (_gv(produced, "chunk_text") or "")[:300],
        "produced_image_refs": [
            {k: (img.get(k) or img.get("page_num") if k == "page" else img.get(k))
             for k in ("image_index", "page_num", "in_page_idx", "block_index",
                      "anchor_row", "slide_no", "visual_summary", "ocr_text")
             if img.get(k) is not None}
            for img in img_refs_raw[:5]
        ],
        "jaccard_score": round(score, 4),
    }


def evaluate_doc(label: str, doc: GtDoc, doc_path: str) -> Dict[str, Any]:
    """跑 extract+chunk,对每个 GT chunk 算 Jaccard。fail-open 单 chunk 错继续。"""
    fmt = doc.fmt
    try:
        chunks = _extract_and_chunk(label, fmt, doc_path)
    except Exception as e:
        return {"label": label, "fmt": fmt, "degraded": doc.degraded,
                "error": f"extract/chunk: {type(e).__name__}: {str(e)[:160]}"}

    per_chunk: List[Dict[str, Any]] = []
    judge_items: List[Dict[str, Any]] = []
    strong_scores: List[float] = []

    for gt in doc.gt_chunks:
        if not gt.has_strong_refs:
            # 弱 GT(只标 page 等 weak ref 或完全没标)— 不计入 Jaccard 均值,
            # 但仍跑 matcher 记录 nimg 供 trend(与 gt_eval.image_accuracy 同口径)
            produced = _match_gt_chunk_to_produced(gt, chunks)
            n_pred = len(_pred_refs_from_chunk(produced, fmt)) if produced else 0
            per_chunk.append({
                "gt_label": gt.label, "weak": True,
                "n_expected_refs": len(gt.expected_image_refs),
                "n_pred_refs": n_pred,
                "matched_produced_type": _gv(produced, "chunk_type") if produced else None,
            })
            continue

        produced = _match_gt_chunk_to_produced(gt, chunks)
        if not produced:
            per_chunk.append({
                "gt_label": gt.label, "weak": False,
                "n_expected_refs": len(gt.expected_image_refs),
                "n_pred_refs": 0,
                "jaccard": 0.0,
                "matched_produced_type": None,
            })
            strong_scores.append(0.0)
            continue

        pred_refs = _pred_refs_from_chunk(produced, fmt)
        score = jaccard(gt.expected_image_refs, pred_refs, strict=True)
        strong_scores.append(score)
        per_chunk.append({
            "gt_label": gt.label, "weak": False,
            "n_expected_refs": len(gt.expected_image_refs),
            "n_pred_refs": len(pred_refs),
            "jaccard": round(score, 4),
            "matched_produced_type": _gv(produced, "chunk_type"),
        })
        # 2026-06-12 D6 改进:Claude image_binding judge bundle 只 emit "图相关" case
        # GT 显式负例(expected_image_refs=[])+ chunker 也没绑图 → 不送评(图无关 case 评 ib=3
        # 中性会拖低均值,D5 baseline 15/36 显式负例使 mean 从真值被稀释到 3.343)
        # GT 含图 OR pred 含图(含 cross-bind/over-attach 真 bug)→ 送评 — 真有图位置可判
        if gt.expected_image_refs or pred_refs:
            judge_items.append(_doc_to_judge_bundle_item(
                label, fmt, gt, produced, pred_refs, score))

    all_refs = _all_step_card_refs(chunks, fmt)
    dup = img_dup_factor(all_refs)

    return {
        "label": label, "fmt": fmt, "degraded": doc.degraded,
        "n_gt_chunks": len(doc.gt_chunks),
        "n_strong_chunks": len(strong_scores),
        "mean_jaccard": (sum(strong_scores) / len(strong_scores)) if strong_scores else None,
        "img_dup_factor": round(dup, 4),
        "n_step_cards": sum(1 for c in chunks if _gv(c, "chunk_type") == "step_card"),
        "n_total_image_refs": len(all_refs),
        "per_chunk": per_chunk,
        "judge_items": judge_items,
    }


def _percentile(xs: List[float], p: float) -> Optional[float]:
    """p ∈ [0, 1] 的百分位(线性插值)。空集返 None。"""
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def run(gt_files: List[str], docs_dir: str, *,
        only_fmt: Optional[str] = None) -> Dict[str, Any]:
    """跑全套 ingestion 绑定评测。

    Args:
        gt_files: 1 个或多个 ground_truth/*.json 路径
        docs_dir: 源文档目录(eval_samples/documents/)
        only_fmt: 只跑指定格式(docx/pdf/xlsx/pptx)— 用于 eval_image_binding_pdf 等独立脚本

    返回 l4_multimodal ingestion 支柱契约的 dict。fail-open:单 doc 失败入 errors 不阻断。
    """
    # 加载所有 GT
    all_docs: Dict[str, GtDoc] = {}
    for gt_path in gt_files:
        for label, d in load_gt(gt_path).items():
            if label in all_docs:
                continue  # 同 label 取第一个
            if only_fmt and d.fmt != only_fmt:
                continue
            all_docs[label] = d

    per_doc: List[Dict[str, Any]] = []
    errors: List[str] = []
    judge_bundle: List[Dict[str, Any]] = []
    by_fmt: Dict[str, Dict[str, Any]] = {}
    dup_factors_all: List[float] = []

    for label, doc in all_docs.items():
        doc_path = _doc_path(docs_dir, label, doc.fmt)
        if not doc_path:
            errors.append(f"{label}: 源文档不存在(docs_dir={docs_dir})")
            continue

        result = evaluate_doc(label, doc, doc_path)
        per_doc.append(result)

        if "error" in result:
            errors.append(f"{label}: {result['error']}")
            continue

        # 2026-06-12 D6:degraded doc 的 dup 不入主 p95 — degraded GT(如 xlsx_inspect
        # schema 简化、pptx 0 step_card)的"同行多图"是文档形态不是 over-attach bug,
        # 入主闸会掩盖真信号。per_doc 仍保留 dup_factor 字段供 trend 监控
        if result.get("img_dup_factor") is not None and not doc.degraded:
            dup_factors_all.append(result["img_dup_factor"])

        if result.get("judge_items"):
            judge_bundle.extend(result["judge_items"])

        # 按格式聚合(degraded 仍记进 by_fmt 但不计入主聚合分子)
        fmt = doc.fmt
        if fmt not in by_fmt:
            by_fmt[fmt] = {"n_docs": 0, "n_strong_chunks": 0, "n_degraded_docs": 0,
                           "_scores": []}
        by_fmt[fmt]["n_docs"] += 1
        if doc.degraded:
            by_fmt[fmt]["n_degraded_docs"] += 1
        else:
            by_fmt[fmt]["n_strong_chunks"] += result["n_strong_chunks"]
            for pc in result["per_chunk"]:
                if not pc.get("weak") and "jaccard" in pc:
                    by_fmt[fmt]["_scores"].append(pc["jaccard"])

    # 出每格式聚合 + 主闸数值
    determ: Dict[str, Any] = {
        "img_dup_factor_p95": _percentile(dup_factors_all, 0.95),
        "img_dup_factor_max": max(dup_factors_all) if dup_factors_all else None,
        "n_degraded_docs": sum(1 for d in all_docs.values() if d.degraded),
        "errors": errors,
        "per_fmt": {},
    }
    for fmt, agg in by_fmt.items():
        scores = agg.pop("_scores")
        determ["per_fmt"][fmt] = {
            "n_docs": agg["n_docs"],
            "n_degraded_docs": agg["n_degraded_docs"],
            "n_strong_chunks": agg["n_strong_chunks"],
            "mean_jaccard": (sum(scores) / len(scores)) if scores else None,
            "std_jaccard": statistics.stdev(scores) if len(scores) >= 2 else None,
        }
        determ[f"binding_jaccard_{fmt}"] = determ["per_fmt"][fmt]["mean_jaccard"]

    # 缺格式补 None(供 build_gates 安全 _g 取值)
    for fmt in ("docx", "pdf", "xlsx", "pptx"):
        determ.setdefault(f"binding_jaccard_{fmt}", None)

    return {
        "deterministic": determ,
        "per_doc": per_doc,
        "judge_bundle_binding": judge_bundle,
    }
