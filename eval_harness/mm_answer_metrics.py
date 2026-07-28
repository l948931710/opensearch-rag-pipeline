# -*- coding: utf-8 -*-
"""
mm_answer_metrics.py — DETERMINISTIC metrics for the image-text interleaving layer
of a generated answer.  No model calls; fully reproducible.

It measures *placement quality* of `<<IMG:N>>` markers, NOT text/number fidelity
(that is a separate, out-of-scope measurement).

Production-faithful by construction: it reuses the EXACT image-indexing
(`_extract_image_chunks`, 1-based, enumerate(chunks,1)) and marker pattern
(`_IMG_PLACEHOLDER_PATTERN`) that `content_blocks_builder.build_content_blocks`
uses, so "what would be shown" here matches what the DingTalk card would show.

Key concepts (REFERENCED-ONLY semantics, aligned 2026-07-01)
------------------------------------------------------------
Production `build_content_blocks` is referenced-only: it renders ONLY images the
LLM actively placed via a valid `<<IMG:N>>`, quota'd by round-robin rotation, and
returns [] when nothing valid was referenced (over-attachment fix).  The old
"unreferenced images get appended at the end" model no longer exists — metrics
here mirror the current renderer (n_shown was min(available, cap) before, which
made `dangling` blind to the most common failure: figure-phrase + zero markers
→ real card shows 0 images, old metric said n_shown>0 → dangling=False).

- available  : retrieved chunks that carry at least one image (image_map keys).
- referenced : available indices the LLM actively placed via a valid <<IMG:N>>.
- n_shown    : simulated render count = round-robin rotation over the referenced
               docs' images, capped at `max_images` (plan_image_rotation — the
               SAME quota semantics production uses; sign-failure/near-dup are
               unknowable offline and don't consume quota in production either).
- rendered_any / answer_image_rate : did the card show >=1 image / fraction of
               image-bearing answers that did.  The primary end-to-end 出图率.
- orphan     : available but NOT referenced -> NOT shown at all (was: appended).
               High orphan rate = the model isn't *placing* images.
- invalid    : a <<IMG:N>> whose N has no image in image_map (out-of-range or
               points at a text-only chunk) = hallucinated marker.
- dangling   : answer text promises a figure (如图/见下图/...) but NOTHING is
               shown — under referenced-only this includes the zero-marker case.
- strategy   : "interleaved" (>=1 in-range marker) / "referenced_none" (images
               available, none validly referenced → card renders []) /
               "text_only" (no images available).  Legacy stored dets may carry
               "appended" — the pre-referenced-only name for referenced_none;
               aggregate() maps it for replay compatibility.
"""
import re
from typing import Any, Dict, List, Optional

from opensearch_pipeline.content_blocks_builder import (
    _extract_image_chunks,
    _IMG_PLACEHOLDER_PATTERN,
    plan_image_rotation,
)

# Chinese (and a couple EN) "see the figure" cues the answer text may use.
#
# 2026-06-12 扩充(scripts/audit_figure_ref_phrases.py 校准):原版只覆盖「如下图/见图」
# 类教科书指代,真生产 LLM(Qwen)清一色用「截图显示 / 图示 / 图片描述 / 参考图」等
# 自由话术 —— 68 条 SUCCESS 样本 top 120 短语原版只命中 2 条 = 装饰性闸。
# 扩充后须配 dump_dangling_cases() 人工抽检,避免误报名词性短语(图标/图样/图片参数)。
_FIGURE_REF_RE = re.compile(
    r"(如下图|见下图|如上图|见上图|如图所示|下图所示|上图所示|如图|见图|下图|上图|"
    r"见下方图|如下所示图|"
    # 新增:截图相关(LLM 描述 SOP 截图最常见话术)
    r"截图显示|界面截图|参考截图|系统截图|"
    # 新增:图示/图片显示
    r"图示|参考图示|参考图|如参考图所示|"
    r"图片显示|参考图片|图片提示|图片描述|"
    # 英文
    r"see\s+(the\s+)?(figure|image|picture|screenshot))"
)

# 明确不算 dangling 指代的名词性短语(避免新正则误报)。
# 用法: dangling 判定时若 _FIGURE_REF_RE 命中但全文也含这些名词,需做边界判断。
# 目前 analyze_answer 不主动排除(过严容易漏真 dangling),由 dump_dangling_cases()
# 输出 case 让人工首轮抽 20 条复核;升 hard 前若误报 >5%,在此扩充排除集。
_FIGURE_REF_FALSE_POSITIVES = (
    "图标", "图样", "图片参数",
    "组织架构图", "部门设置图", "流程图",  # 实物名词
)


def dump_dangling_cases(per_answer: List[Dict[str, Any]],
                        case_meta: List[Dict[str, Any]] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
    """输出所有 dangling=True 的 case,供首轮 soft 闸下人工抽检校准。

    Args:
        per_answer: analyze_answer 的逐 case 输出列表
        case_meta: 同长度的 case 元信息列表(qid/query/answer 等),可选
        limit: 最多返回多少条(取首)

    Returns:
        [{qid, query, answer_excerpt, matched_phrase, n_available, n_shown=0}]
    """
    out: List[Dict[str, Any]] = []
    for i, det in enumerate(per_answer):
        if not det.get("dangling_ref"):
            continue
        meta = case_meta[i] if case_meta and i < len(case_meta) else {}
        answer = meta.get("answer", "") or ""
        # 抓首个匹配的短语 + 前后 20 字上下文
        m = _FIGURE_REF_RE.search(answer)
        phrase, ctx = "", ""
        if m:
            s, e = m.span()
            phrase = answer[s:e]
            ctx = answer[max(0, s - 20):min(len(answer), e + 20)]
        out.append({
            "qid": meta.get("qid", f"#{i}"),
            "query": meta.get("query", "")[:80],
            "answer_excerpt": ctx,
            "matched_phrase": phrase,
            "n_available": det["n_available"],
            "is_likely_false_positive": any(fp in ctx for fp in _FIGURE_REF_FALSE_POSITIVES),
        })
        if len(out) >= limit:
            break
    return out


L4SRV_EVALUATOR_VERSION = "3.0.0"
# 2.0.0 (2026-07-27)：orphan 分母收窄到「模型实际看得见的图」。
# 3.0.0 (2026-07-27, codex 复核后)：**全部** serving 主指标（marker_validity / n_shown /
#   dangling / distinctness / strategy…）改按可见集判——2.0.0 只收窄了 orphan 分母,
#   marker_validity 仍按全量算,于是"引用了未进 context 的图"被判合法而生产实渲染 0 张。
#   同时拆出独立的 referenced_all（旧口径不再被主口径污染）、新增 post-rotation 的
#   shown_image_summary 供 MM judge、可见集优先取结构化 included_doc_indices。

# `<<IMG:N>>` 与其子下标形式 `<<IMG:N.m>>`（RAG_IMG_SUBINDEX 开时 context 发的是后者）。
_VISIBLE_MARKER_TMPL = r"<{{1,2}}IMG:{n}(?:\.\d+)?>{{1,2}}"


def _visible_indices(image_map: Dict[int, Any], context: Optional[str],
                     included_doc_indices: "Optional[List[int]]" = None) -> set:
    """`image_map` 里**模型实际看得见**的那些文档号。

    优先级（codex PROPOSED-3）：
      1. `included_doc_indices` —— `_format_context_ex` 返回的**结构化**可见集，与生产
         `build_content_blocks(..., included_indices=…)` 同源，是权威判据；
      2. `context` 正则扫 —— 仅作**回放兼容**（老 det 没有结构化字段）。正则扫的是整个
         context 串，文档正文若自带字面量 `<<IMG:N>>` 会污染判定（现网实测 0 例，但
         结构化源不受此影响，故优先）；
      3. 都没有 → 全量（老调用方语义不变）。
    """
    if included_doc_indices is not None:
        vis = set(included_doc_indices)
        return {n for n in image_map if n in vis}
    if context is None:
        return set(image_map.keys())
    return {n for n in image_map
            if re.search(_VISIBLE_MARKER_TMPL.format(n=n), context)}


def analyze_answer(
    answer: str,
    used_chunks: List[Dict[str, Any]],
    max_images: int = 6,
    context: Optional[str] = None,
    included_doc_indices: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Per-answer deterministic placement metrics (referenced-only semantics).

    Args:
        answer: raw LLM answer (with <<IMG:N>> markers, as generate_answer returns).
        used_chunks: the retrieved chunks fed to generate_answer / build_content_blocks
                     (same order, 1-based indexing).
        max_images: the card's image cap. Default 6 = production default
                    (config.rag.max_answer_images / RAG_MAX_ANSWER_IMAGES) — the
                    old default 3 mismatched production and skewed
                    n_shown/over_cap/avg_images_shown.
        context: `_format_context` 给模型的那一份上下文串。给了就把 orphan 的分母收窄到
                 **标记真的进了 context** 的图（见下）。不给 → 老口径（全量）。

    orphan 分母为什么必须是"可见集"（2026-07-27）
    ─────────────────────────────────────────────
    `available` 原本数的是**全部**带图 chunk，但 `_format_context` 有 `max_context_chars`
    （6000）预算，尾部 chunk 会被截掉 —— 它们的 `<<IMG:N>>` 根本没进 context，模型看不见，
    自然引用不了，却照样计入 orphan 分母。实测 35 题：available 152 / **可见仅 110**，
    即 **27.6% 的分母是模型无从引用的图**。最典型 QA-94：9 个可用、只有 1 个可见、模型
    引用了那 1 个 —— 实际 **1/1 全对**，旧口径记成 1/9。

    这与本模块文档串自称的语义（"High orphan rate = the model isn't *placing* images"）
    直接矛盾：27.6% 的部分与"placing"无关，只与截断有关。

    收窄后 orphan_rate 0.6184 → 0.4727，**仍高于 0.30 soft 门槛** —— 这不是往绿了调。
    旧口径保留为 `orphan_rate_all`，逐轮可对照。
    """
    image_map = _extract_image_chunks(used_chunks)  # {1-based idx: [img dicts]}
    available_all = set(image_map.keys())
    available = _visible_indices(image_map, context, included_doc_indices)

    markers = [int(m.group(1)) for m in _IMG_PLACEHOLDER_PATTERN.finditer(answer)]
    # ── 两套集合，互不污染（codex BLOCKER-1）────────────────────────────────
    # 主 serving 指标一律走 **visible**：生产 `build_content_blocks(..., included_indices=…)`
    # 会把未进 context 的文档从 image_map 里删掉,引用它们**渲染不出任何图**。
    # 旧口径（*_all）单独用 `referenced_all` 算,否则改一处就把对照基准也改了。
    invalid = [n for n in markers if n not in available]          # 渲染不出图 ⇒ 非法
    referenced_in_order: List[int] = []
    for n in markers:
        if n in available and n not in referenced_in_order:
            referenced_in_order.append(n)
    referenced = set(referenced_in_order)
    orphans = available - referenced

    # 旧口径（全量 image_map，不看可见性）——只供逐轮对照，不进任何闸门
    referenced_all = {n for n in markers if n in image_map}
    orphans_all = available_all - referenced_all

    # What the card actually renders (referenced-only): round-robin rotation over
    # the REFERENCED docs' images, capped at max_images — the same quota semantics
    # as production (plan_image_rotation is the shared single source).
    _ref_counts = [len(image_map[n]) for n in referenced_in_order]
    n_referenced_images = sum(_ref_counts)
    _plan = plan_image_rotation(_ref_counts, max_images)
    n_shown = sum(_plan)
    # post-rotation 的**实际展示图**（codex BLOCKER-2）：MM judge 的 rubric 写的是
    # "卡片实际展示的图",此前喂的却是全量 image_map ⇒ 语义通道也在测错东西。
    shown_image_summary = {
        n: [(im.get("visual_summary", "") or "")[:50] for im in image_map[n][:k]]
        for n, k in zip(referenced_in_order, _plan) if k > 0
    }

    has_fig_phrase = bool(_FIGURE_REF_RE.search(answer or ""))
    # under referenced-only, zero valid markers → card renders [] → a figure
    # phrase is a REAL dangling promise (the old n_shown=min(available,cap)
    # model was blind to exactly this case)
    dangling = has_fig_phrase and n_shown == 0

    if referenced:
        strategy = "interleaved"
    elif available:
        strategy = "referenced_none"  # images available, none validly referenced → renders []
    else:
        strategy = "text_only"

    return {
        # availability / what gets shown
        "n_available": len(available),             # 可见集（模型真能引用的）
        # 旧口径（全量带图 chunk）—— 保留以便逐轮对照"收窄了多少"，不参与闸门。
        "n_available_all": len(available_all),
        "n_orphan_all": len(orphans_all),
        "orphan_rate_all": ((len(orphans_all) / len(available_all))
                            if available_all else None),
        "n_shown": n_shown,
        "n_referenced_images": n_referenced_images,
        # rendered_any = card 实际出图（answer_image_rate 的分子；referenced-only 主指标）
        "rendered_any": n_shown > 0,
        # over_cap: referenced-only 下配额裁的是【被引用文档的图】，不是全部可用图
        "over_cap": n_referenced_images > max_images,
        # marker placement
        "n_markers": len(markers),                       # total <<IMG:N>> occurrences
        "n_invalid_markers": len(invalid),               # occurrences whose N is out-of-range
        "n_inrange_markers": len(markers) - len(invalid),  # valid occurrences (incl. reuse)
        "n_distinct_markers": len(referenced),           # distinct valid image indices placed
        "n_valid_markers": len(referenced),              # back-compat alias (= distinct)
        "invalid_marker_idxs": sorted(invalid),
        # marker_validity = fraction of marker OCCURRENCES that resolve to a real in-range image.
        # Reuse of a VALID image (same <<IMG:N>> placed twice, e.g. across two related steps) is NOT
        # penalised here — only out-of-range/hallucinated markers lower it. (Reuse → marker_distinctness.)
        "marker_validity": ((len(markers) - len(invalid)) / len(markers)) if markers else None,
        # advisory: among in-range markers, fraction that are DISTINCT images. 1.0 = no reuse;
        # <1.0 = an image was referenced more than once (often a chunk that bundles several step-level
        # images under one addressable marker index, so distinct steps can't get distinct markers).
        "marker_distinctness": (len(referenced) / (len(markers) - len(invalid)))
                               if (len(markers) - len(invalid)) else None,
        # placement quality（interleaved = 至少一个在界内的有效标记；纯无效标记
        # 渲染不出任何图，与 strategy=referenced_none 保持一致）
        "interleaved": bool(referenced),
        "strategy": strategy,
        "n_referenced": len(referenced),
        "n_orphan": len(orphans),
        "orphan_rate": (len(orphans) / len(available)) if available else None,
        "referenced_idxs": sorted(referenced),
        "orphan_idxs": sorted(orphans),
        # dangling reference (text promises a figure but none is shown)
        "has_figure_phrase": has_fig_phrase,
        "dangling_ref": dangling,
        # context for the audit / judge
        "available_idxs": sorted(available),
        # 全量候选图（诊断用）。⚠️ **不要**再把它当 "shown" 喂给 MM judge。
        "image_map_summary": {
            i: [(im.get("visual_summary", "") or "")[:50] for im in v]
            for i, v in image_map.items()
        },
        # post-rotation 实际展示图 —— MM judge 的 shown_image_captions 应取这个
        "shown_image_summary": shown_image_summary,
    }


def _safe_mean(xs: List[float]):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def aggregate(per_answer: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate deterministic metrics across answers.

    Rates that only make sense when images are available are computed over the
    subset of answers that actually retrieved an image chunk.
    """
    n = len(per_answer)
    with_imgs = [a for a in per_answer if a["n_available"] > 0]
    n_wi = len(with_imgs)

    total_available = sum(a["n_available"] for a in with_imgs)
    total_orphan = sum(a["n_orphan"] for a in with_imgs)
    # 旧口径合计（老 det 缺这两个键时退回新口径值，回放兼容）
    # 旧口径必须用**独立**分母（codex BLOCKER-1 的后半）：沿用 with_imgs（按新口径
    # n_available>0 过滤）会把"全部图都被截断"的题整题排除,逐题 orphan_rate_all=1.0
    # 聚合却返回 None ⇒ 根本无法逐轮对照。
    with_imgs_all = [a for a in per_answer
                     if a.get("n_available_all", a.get("n_available", 0)) > 0]
    total_available_all = sum(a.get("n_available_all", a["n_available"]) for a in with_imgs_all)
    total_orphan_all = sum(a.get("n_orphan_all", a["n_orphan"]) for a in with_imgs_all)
    total_markers = sum(a["n_markers"] for a in per_answer)
    # in-range / distinct occurrences (fallbacks recompute from older stored dets lacking the new keys)
    total_inrange = sum(a.get("n_inrange_markers", a["n_markers"] - a["n_invalid_markers"])
                        for a in per_answer)
    total_distinct = sum(a.get("n_distinct_markers", a.get("n_valid_markers", 0)) for a in per_answer)

    # rendered_any: new-key dets carry it; legacy dets (pre referenced-only) lack it —
    # recompute: in-range markers by definition point at image_map entries, so under
    # referenced-only semantics the card rendered iff >=1 in-range marker existed.
    def _rendered(a):
        if "rendered_any" in a:
            return bool(a["rendered_any"])
        return a.get("n_inrange_markers", a["n_markers"] - a["n_invalid_markers"]) > 0

    return {
        "n_answers": n,
        "n_answers_with_images": n_wi,
        # core image-text-layer rates
        # answer_image_rate = 出图率主指标：有图可用的答案里，卡片实际渲染出 ≥1 张的比例
        # （referenced-only 下 = LLM 有效引用率;补图/召回/协议类改动的唯一端到端读数）
        "answer_image_rate": _safe_mean([1.0 if _rendered(a) else 0.0 for a in with_imgs]),
        "interleave_rate": _safe_mean([1.0 if a["interleaved"] else 0.0 for a in with_imgs]),
        "orphan_rate": (total_orphan / total_available) if total_available else None,
        # 旧口径（全量分母）：与上一行的差 = 被 max_context_chars 截掉、模型无从引用的图
        "orphan_rate_all": ((total_orphan_all / total_available_all)
                            if total_available_all else None),
        "n_available_visible": total_available,
        "n_available_all": total_available_all,
        # validity = in-range occurrences / total occurrences (reuse not penalised)
        "marker_validity": (total_inrange / total_markers) if total_markers else None,
        # advisory: distinct in-range images / in-range occurrences (1.0 = no reuse)
        "marker_distinctness": (total_distinct / total_inrange) if total_inrange else None,
        "dangling_ref_rate": _safe_mean([1.0 if a["dangling_ref"] else 0.0 for a in per_answer]),
        "over_cap_rate": _safe_mean([1.0 if a["over_cap"] else 0.0 for a in with_imgs]),
        # placement participation: fraction of image answers where the LLM placed >=1 valid marker
        "placement_rate": _safe_mean(
            [1.0 if a.get("n_inrange_markers", a["n_markers"] - a["n_invalid_markers"]) > 0 else 0.0
             for a in with_imgs]
        ),
        # volume
        "avg_images_shown": _safe_mean([a["n_shown"] for a in with_imgs]),
        "total_markers": total_markers,
        "total_invalid_markers": sum(a["n_invalid_markers"] for a in per_answer),
        "total_inrange_markers": total_inrange,
        "total_distinct_markers": total_distinct,
        # legacy replay: "appended" 是 referenced_none 的 pre-referenced-only 旧名 —
        # 新旧存档同一口径计数（n_appended_strategy 保留旧键名，值含两代标签）
        "n_referenced_none_strategy": sum(
            1 for a in with_imgs if a["strategy"] in ("referenced_none", "appended")),
        "n_appended_strategy": sum(
            1 for a in with_imgs if a["strategy"] in ("referenced_none", "appended")),
    }
