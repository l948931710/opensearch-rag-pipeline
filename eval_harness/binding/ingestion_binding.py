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
import re
import shutil
import statistics
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .ref_keys import ImageRef, jaccard, img_dup_factor, parse_ref_dict
from .gt_loader import GtChunk, GtDoc, load_gt


# ── DOCX 独立 strict 路径(env-gated)─────────────────────────────────
# gt_docx_analysis.json 当前 0 个 expected_image_refs(D5 verified)→ docx 走主 GT
# Jaccard 出 None。本路径用 repo 内 fuling_chunk_exp/*.docx fixture + extractor 邻
# 近文本 strict 匹配(与 scripts/eval_image_binding_accuracy.py --strict 同口径,
# 锁档 baseline 98.6%),把 per-image 0/1 平均成 micro-accuracy,以 Jaccard 兼容
# 形态写入 per_fmt['docx'] 与 binding_jaccard_docx。
#
# 触发(3 条全满足才跑):
#  1) 调用方未设 only_fmt,或 only_fmt='docx'
#  2) env EVAL_L4_DOCX_BINDING_ENABLE in {1, true, yes}(run_eval.py 默认开)
#  3) fuling_chunk_exp/ 目录存在且 _extract_and_chunk 可用 — 缺任一即 fail-open
#     返回 None
#
# ⚠️ production-faithful:本路径复用 _extract_and_chunk(同 evaluate_doc 路径)
# 即跑 UnifiedExtractor + node_chunk_documents(global_split_mode='dynamic'),
# **不**硬编码 split_mode='step' — 与生产 chunker 路由完全一致。
#
# ⚠️ SOP 筛选:fuling_chunk_exp/ 里 admin_/hr_/eval_*_faq 等非 SOP docx 在生产里
# 不走 step,strict 强测会产生大量假数据。本路径以 basename 启发式标 degraded=True
# 把它们排除在主聚合分子外,只跑 production_/it_/oss_FL-*-WI-* 这类 SOP/手册入主闸。
#
# 优先级:GT Jaccard(per_fmt['docx'].n_strong_chunks >= 5,确保 GT 量足够稳)优先,
# strict 仅顶替 GT 缺数情况;per_doc trend 始终追加(供 D7+ 监控)。
_DOCX_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "fuling_chunk_exp",
)

# 非 SOP basename 前缀:在生产里这些 docx 不走 step,strict 强测产物等于人造噪声
# 仅入 per_doc 趋势,不入主聚合分子。
_NON_SOP_DOCX_PREFIXES = ("admin_", "hr_", "eval_company_faq", "eval_it_support_faq")


def _is_sop_docx(basename: str) -> bool:
    """basename 启发式:production_ / it_富岭U8+*手册 / oss_FL-*-WI-* / *作业指导书 算 SOP。"""
    name = basename.lower()
    if name.startswith(_NON_SOP_DOCX_PREFIXES):
        return False
    if "作业指导书" in basename:
        return True
    if name.startswith("production_") or name.startswith("oss_"):
        return True
    if name.startswith("it_") and ("操作手册" in basename or "管理操作手册" in basename):
        return True
    # 默认保守:其余 it_/未分类视为非 SOP
    return False


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

        # title 取真实文件名(不是 label),否则 _detect_step_patterns 的 sop_keywords
        # 检测会失败 —— 生产路径 title 来自 RDS metadata(真实标题,含"作业指导书"/WI-N
        # 文号),走 step mode；评测路径若把 title=label 则 label 通常是短 doc_id
        # (如"pdf_xs_wi_007"用 underscore 而非 dash,不匹配 wi-\d regex),step mode
        # 不触发,xs_wi_007/it_xxh_003 类 doc 在评测下全切 text_chunk、图全无绑定 ——
        # 与生产路径完全脱节,GT 失去信号。pdf_sop label 恰好含 "sop" 关键词所以
        # 之前没被发现。
        # 进一步:用 realpath 解 symlink。eval_image_binding_pdf 把新 doc 软链
        # 到 docs_dir/{label}.{ext},symlink 名又退回到 label,会重新触发同样
        # 问题。realpath 拿到真实文件名(如 "FL-XS-WI-007.pdf"/含"作业指导书"),
        # 与生产 RDS title 等价。
        filename = os.path.basename(os.path.realpath(doc_path))
        prod_like_title = os.path.splitext(filename)[0]
        doc = {
            "doc_id": label, "version_no": 1,
            "title": prod_like_title, "filename": filename,
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


# GT label → (step_no, sec_no) 抽号正则。命中顺序:中文"步骤" → 英文"Step" → 纯 dotted。
# 兼容:"步骤3.1 U8..." / "步骤 3" / "Step 3.1" / "4.2.1 外观..." / "3.1 收取..."。
# 不命中:"前言..." / "目的-范围-职责" / "流程5a"(clause_chunk,无需 step filter)。
_STEP_LABEL_RES = (
    re.compile(r"^步骤\s*(\d+)(?:\.(\d+(?:\.\d+)*))?\b"),
    re.compile(r"^Step\s+(\d+)(?:\.(\d+(?:\.\d+)*))?\b", re.IGNORECASE),
    re.compile(r"^(\d+)\.(\d+(?:\.\d+)*)\b"),
)


def _extract_step_no_from_label(label: str) -> Tuple[Optional[int], Optional[str]]:
    """从 GT label 抽 (step_no, sec_no)。

    匹配形态:
      "步骤3.1 U8..."    → (3, "3.1")
      "步骤 3"           → (3, None)
      "步骤2 ..."        → (2, None)
      "Step 3.1"        → (3, "3.1")
      "4.2.1 外观..."    → (4, "4.2.1")
      "3.1 抄录..."      → (3, "3.1")
      "前言(...)"        → (None, None)

    sec_no 始终以 step_no 起头(如 "3.1"/"4.2.1"),与 chunker 注入 extra.section_no
    的形态对齐。
    """
    if not label:
        return (None, None)
    s = label.strip()
    for pat in _STEP_LABEL_RES:
        m = pat.match(s)
        if m:
            step_no = int(m.group(1))
            tail = m.group(2)
            sec_no = f"{step_no}.{tail}" if tail else None
            return (step_no, sec_no)
    return (None, None)


def _match_gt_chunk_to_produced(gt: GtChunk, chunks: List[Any]) -> Optional[Any]:
    """keyword recall ≥ 0.3 → density 最大者,但**chunk_type 同类型优先**。

    口径同 gt_eval.py 的 covering+density 但加两道偏好:
      1) chunk_type 同类型优先:GT 标 step_card 时,先在 covering 集里找 chunk_type=
         step_card 的 candidate;若有,density 最大者胜。否则回退全 covering 集。
      2) 2026-06-13 D8 Phase 3 finding 3 修:GT label 含步骤号(如"步骤3.1"/"4.2.1")时,
         在 typed pool 内先按 extra.section_no 完全匹配过滤;若空再按 extra.step_no
         过滤;命中后 recall-max(tie 用 density),全无匹配回退当前 typed density-max。

    Why (1):density = hits / sqrt(len) 让短文本不公平胜出 — 实测 pdf_sop 步骤1.1 时,
    前言段(text_chunk, 123 字, 4 hits = density 0.36)恰好比完整步骤段(step_card,
    300 字, 6 hits = density 0.35)略胜,导致 matcher 选错 chunk 类型,使该 GT
    被无图的 text_chunk 抢戏 jaccard=0。chunk_type 优先反映了 GT 标注者的真实意图。

    Why (2):typed pool 内 density 也会让短文本父 chunk 偶然命中 keyword 抢戏真子
    chunk — pdf_sop GT 3.1 实证:step_no=4 父 43 字(d=0.457, imgs=[])偶然含
    "U8/扫码/报检" 抢戏 step_no=3 main 349 字(d=0.161, imgs=[5,6])。GT label 已
    显式写"步骤3.1",据此抽 step_no=3 锁回真 chunk;过滤后 recall-max 让 keyword
    覆盖更全的真 chunk 胜出(避免 step_no=3 内 sub=2 sec=3.2 短文本再次抢戏)。
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
                # D8 Phase 5 Bug C2 修:GT expected_image_refs 非空时,typed pool
                # 内优先选含 image_refs 的 chunk —— 反映"主步骤含图"标注意图,
                # 避免短的 visual_knowledge/摘要先行 chunk(image_refs=[],但因 text
                # 含 visual_summary 命中 keyword)用 density 抢戏真子步骤。
                # it_xxh_003 实证:step_no=5 sub1 i=8([补充图示] 摘要 200 字,
                # imgs=[]) vs sub2 i=9(主步骤 500+字, imgs=[20-25])—— 旧路径
                # density-max 选 sub1 → pred=[] J=0;含图过滤后选 sub2 J=1.0。
                # 0-image GT(expected_image_refs=[])不进 filter → 无副作用。
                if gt.expected_image_refs:
                    with_imgs = [s for s in typed
                                 if (_gv(s[0], "extra") or {}).get("image_refs")]
                    if with_imgs:
                        typed = with_imgs
                # GT label 含步骤号 → sec_no/step_no 次级过滤(D8 Phase 3 finding 3 修)
                step_no, sec_no = _extract_step_no_from_label(gt.label)
                if step_no is not None:
                    # sec_no 完全匹配优先(最特异,如 "3.1"/"4.2.1")
                    if sec_no is not None:
                        sec_matched = [
                            s for s in typed
                            if (_gv(s[0], "extra") or {}).get("section_no") == sec_no
                        ]
                        if sec_matched:
                            return max(sec_matched, key=lambda s: (s[1], s[2]))[0]
                    # step_no 匹配次之(覆盖父 chunk / 无 section_no 的子 chunk)
                    step_matched = [
                        s for s in typed
                        if (_gv(s[0], "extra") or {}).get("step_no") == step_no
                    ]
                    if step_matched:
                        return max(step_matched, key=lambda s: (s[1], s[2]))[0]
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


def _docx_binding_enabled() -> bool:
    """env 开关:EVAL_L4_DOCX_BINDING_ENABLE=true 才跑 DOCX 独立路径。

    分离独立 helper 是为了让单测能精确钉死 OFF/ON 语义(不耦合到 _run_docx_strict_path
    内部 import 顺序)。run_eval.py 默认 setdefault 为 'true' 实现 make eval/CI 自动闭环。
    """
    return os.getenv("EVAL_L4_DOCX_BINDING_ENABLE", "").lower() in ("1", "true", "yes")


def _run_docx_strict_path(
    fixture_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """DOCX 专路:strict per-image 准确率 → Jaccard 兼容 dict。

    **production-faithful**:复用 _extract_and_chunk(label, 'docx', path)走与
    evaluate_doc 完全一致的路由(UnifiedExtractor + node_chunk_documents,
    global_split_mode='dynamic'),不硬编码 split_mode='step'。

    GT 来源(同 scripts/eval_image_binding_accuracy.py --strict 算法):
      blocks 顺序扫,每张 image_ref 前最近 paragraph/heading text 当 GT;
      chunker 产出的 step_card 的 image_refs 反查 image_index → chunk_text;
      gt_core(去步骤号前缀的前 40 字)是否 in chunk_text → 0/1。

    SOP 筛选:_is_sop_docx 启发式;非 SOP docx 仍跑、仍入 per_doc 趋势,但
    degraded=True,不入 _scores → 不影响主闸 binding_jaccard_docx。

    返回 None 表示禁用 / fixture 缺 / extractor 不可用 / 全 0 样本。
    返回 dict 形态:
      {
        "by_fmt": {n_docs, n_degraded_docs, n_strong_chunks, mean_jaccard,
                   std_jaccard, _source: "strict_fixture"},
        "per_doc": [{label, fmt, degraded, n_strong_chunks, mean_jaccard,
                     img_dup_factor, n_total_image_refs, ...}, ...],
        "errors": [...],
      }

    fail-open:fixture 缺、extractor import 失败、单 doc 异常 — 单 doc 入 errors,
    主流程不阻断。
    """
    if not _docx_binding_enabled():
        return None
    fixture_dir = fixture_dir or _DOCX_FIXTURE_DIR
    if not os.path.isdir(fixture_dir):
        return None

    import glob
    import re

    docx_files = sorted(glob.glob(os.path.join(fixture_dir, "*.docx")))
    if not docx_files:
        return None

    # extractor 探测:不可用就 fail-open(SAE wheel-pack 场景)
    try:
        from opensearch_pipeline.extraction.docx_extractor import extract_docx_with_images
    except Exception:
        return None

    per_doc_items: List[Dict[str, Any]] = []
    sop_doc_scores: List[float] = []  # 仅 SOP 入主聚合
    errors: List[str] = []
    total_correct_sop = 0
    total_checked_sop = 0

    for path in docx_files:
        basename = os.path.basename(path)
        is_sop = _is_sop_docx(basename)
        try:
            # GT 提取:邻近文本(用 docx_extractor 直拿 blocks,不走完整 _extract_and_chunk
            # 是因为我们要的是 blocks.extra.image_index 的顺序锚点)
            blocks, _assets = extract_docx_with_images(path)
            if not blocks:
                continue

            gt_bindings: List[tuple] = []
            last_text = ""
            for b in blocks:
                bt = getattr(b, "block_type", None)
                if bt in ("paragraph", "heading"):
                    t = (getattr(b, "text", "") or "").strip()
                    if t:
                        last_text = t
                elif bt == "image_ref":
                    extra = getattr(b, "extra", None) or {}
                    img_idx = extra.get("image_index")
                    if img_idx is not None and last_text:
                        gt_bindings.append((img_idx, last_text))
            if not gt_bindings:
                continue

            # Pipeline:production-faithful 路由(同 evaluate_doc)
            doc_label = f"BINDING_DOCX_{basename[:40]}"
            with contextlib.redirect_stdout(io.StringIO()):
                chunks = _extract_and_chunk(doc_label, "docx", path)

            pipeline_bindings: Dict[int, str] = {}
            n_step_cards = 0
            for c in chunks:
                if _gv(c, "chunk_type") != "step_card":
                    continue
                n_step_cards += 1
                extra = _gv(c, "extra") or {}
                for ref in (extra.get("image_refs") or []):
                    iidx = ref.get("image_index")
                    if iidx is not None:
                        pipeline_bindings[iidx] = _gv(c, "chunk_text") or ""
            if not pipeline_bindings:
                # 该 doc 没产 step_card 含图绑定 — 仍记 per_doc 趋势,但 skip 算分
                per_doc_items.append({
                    "label": f"docx_strict::{basename}",
                    "fmt": "docx",
                    "degraded": True,
                    "is_sop": is_sop,
                    "n_gt_chunks": len(gt_bindings),
                    "n_strong_chunks": 0,
                    "mean_jaccard": None,
                    "img_dup_factor": 1.0,
                    "n_total_image_refs": 0,
                    "n_step_cards": n_step_cards,
                    "per_chunk": [],
                    "judge_items": [],
                    "_source": "strict_fixture",
                    "_skip_reason": "no_step_card_image_bindings",
                })
                continue

            doc_correct = 0
            doc_checked = 0
            for img_idx, gt_text in gt_bindings:
                if img_idx not in pipeline_bindings:
                    continue
                doc_checked += 1
                gt_core = re.sub(r"^[\s\d\.、）)（(]+", "", gt_text)[:40]
                if gt_core and gt_core in pipeline_bindings[img_idx]:
                    doc_correct += 1
            if doc_checked == 0:
                continue

            acc = doc_correct / doc_checked
            # img_dup_factor 走真聚合(同 evaluate_doc)— 这样 over-attach 信号不丢
            all_refs = _all_step_card_refs(chunks, "docx")
            dup = img_dup_factor(all_refs)

            if is_sop:
                sop_doc_scores.append(acc)
                total_correct_sop += doc_correct
                total_checked_sop += doc_checked

            per_doc_items.append({
                "label": f"docx_strict::{basename}",
                "fmt": "docx",
                # 非 SOP → degraded=True(不入主聚合分子,但保留 per_doc 趋势)
                "degraded": (not is_sop),
                "is_sop": is_sop,
                "n_gt_chunks": len(gt_bindings),
                "n_strong_chunks": doc_checked,
                "mean_jaccard": round(acc, 4),
                "img_dup_factor": round(dup, 4),
                "n_total_image_refs": len(all_refs),
                "n_step_cards": n_step_cards,
                "per_chunk": [],
                "judge_items": [],
                "_source": "strict_fixture",
            })
        except Exception as e:
            errors.append(f"docx_strict::{basename}: {type(e).__name__}: {str(e)[:160]}")

    if total_checked_sop == 0:
        # SOP 0 个 → 还要看是否全部 doc 都失败:仅 errors 返,主闸保持 None
        return {"per_doc": per_doc_items, "errors": errors} if (per_doc_items or errors) else None

    mean_jacc = total_correct_sop / total_checked_sop  # micro-accuracy(同 strict baseline)
    std = statistics.stdev(sop_doc_scores) if len(sop_doc_scores) >= 2 else None
    n_sop_docs = sum(1 for d in per_doc_items if d.get("is_sop"))
    n_degraded = sum(1 for d in per_doc_items if d.get("degraded"))
    return {
        "by_fmt": {
            "n_docs": n_sop_docs,
            "n_degraded_docs": n_degraded,
            "n_strong_chunks": total_checked_sop,
            "mean_jaccard": round(mean_jacc, 4),
            "std_jaccard": std,
            "_source": "strict_fixture",
        },
        "per_doc": per_doc_items,
        "errors": errors,
    }


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

    # ── DOCX 独立 strict 路径(env-gated,run_eval.py 默认开)──
    # 调用方 only_fmt=='pdf'/'xlsx'/'pptx' 时永不触发 — 不会让现有 PDF/XLSX 评测被
    # DOCX 路径污染。GT Jaccard 路径出过数(n_strong_chunks >= 5,防 1-chunk 脆 GT
    # 立刻顶替全量 strict)优先;否则 strict micro-accuracy 顶替 binding_jaccard_docx。
    # per_doc trend 始终追加供 D7+ 监控。
    if only_fmt in (None, "docx"):
        docx_strict = _run_docx_strict_path()
        if docx_strict:
            if "by_fmt" in docx_strict:
                existing = determ["per_fmt"].get("docx") or {}
                # 最小门:GT n_strong_chunks >= 5 才让 GT 顶替 strict
                # (避免 D7+ 补 GT 期间 1 chunk 的脆数据立刻顶替 49 docs 全量 strict)
                has_gt_docx = (existing.get("n_strong_chunks") or 0) >= 5
                if not has_gt_docx:
                    determ["per_fmt"]["docx"] = docx_strict["by_fmt"]
                    determ["binding_jaccard_docx"] = docx_strict["by_fmt"]["mean_jaccard"]
                per_doc.extend(docx_strict["per_doc"])
            else:
                # 只有 per_doc(全 doc 失败或无 SOP)— 仍追加趋势数据
                per_doc.extend(docx_strict.get("per_doc", []))
            errors.extend(docx_strict.get("errors", []))

    # 缺格式补 None(供 build_gates 安全 _g 取值)
    for fmt in ("docx", "pdf", "xlsx", "pptx"):
        determ.setdefault(f"binding_jaccard_{fmt}", None)

    return {
        "deterministic": determ,
        "per_doc": per_doc,
        "judge_bundle_binding": judge_bundle,
    }
