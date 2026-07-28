"""Layer 4 — Multimodal binding & answer quality (两支柱).

UNIFIED-L4 设计(2026-06-12,工作流 wu71s7igd 3 评委一致):

  Pillar A — INGESTION:摄入侧图文绑定精度(逐格式 Jaccard)
    调 eval_harness.binding.ingestion_binding 跑 production-faithful 路由
    (UnifiedExtractor + node_chunk_documents)出每 GT chunk 的 Jaccard,
    全文档 img_dup_factor 防 over-attach 回归。覆盖 docx/pdf/xlsx(pptx
    生产 0 step_card,GT degraded)。

  Pillar B — SERVING:`<<IMG:N>>` 摆放质量(LLM 行为)
    复用 mm_answer_metrics.aggregate:marker_validity / orphan_rate /
    dangling_ref_rate 等。判 LLM 是否正确放置标记、口惠图但卡片无图等。

  + Claude image_binding 维度:对 ingestion judge_bundle_binding 评语义
    绑定正确性(可选,N=0 时不计闸)。

cases 触发 Serving;gt_files+docs_dir 触发 Ingestion;两者独立可单跑、可
合跑。任一支柱有数据 applicable=True,否则 N/A。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .. import envboot  # noqa: F401
from ..gen_nothink import generate_answer_nothink


def _run_serving(cases: List[Dict], top_k: int, max_images: int,
                 cosurface: bool = False, with_judge_bundle: bool = True) -> Dict:
    """L4-serving:LLM `<<IMG:N>>` 摆放质量(原 l4 路径,封装进 serving 子键)。

    Args:
        cosurface: retrieve_and_enrich 的 cosurface_images 姿态。
            False = 生产主流量姿态（/api/ask 小程序 + 钉钉 bot 均不传 → 默认 False）
                    —— 主臂,进闸;此前恒以 True 评测,「宽泛问题没图」发生的那条
                    路径从未被度量（2026-07-01 #F-mm1 对齐）。
            True  = /api/ask/stream(console)姿态 —— 对照臂,双臂差 = cosurface 增益
                    的常驻 A/B 装置。
        with_judge_bundle: 对照臂不重复产 judge bundle（评审只评主臂）。
    """
    from eval_harness import mm_answer_metrics as M  # single source of truth, in-repo (was data-repo)
    from opensearch_pipeline.retriever import retrieve_and_enrich

    # multi_turn case（带固定注入 history）只进 history 臂（#F-mm13b）：裸 follow-up
    # 问句检索天然偏弱，混入主臂会拉低已锁定的 answer_image_rate 基线读数
    img_cases = [c for c in cases
                 if c.get("expect_images") and c.get("live_scorable")
                 and not c.get("multi_turn")]
    if not img_cases:
        return {"applicable": False, "n_image_cases": 0,
                "note": "No image-expecting, live-scorable cases"}

    per_query: List[Dict] = []
    det_list: List[Dict] = []
    breadths: List[str] = []
    judge_bundle: List[Dict] = []
    for c in img_cases:
        try:
            chunks = retrieve_and_enrich(c["query"], top_k=top_k, user_dept=None,
                                         cosurface_images=cosurface)
            gen = generate_answer_nothink(c["query"], chunks, pure_text=False)
        except Exception as e:
            per_query.append({"qid": c["qid"], "error": f"{type(e).__name__}: {e}"[:160]})
            continue
        ans = gen["answer"]
        det = M.analyze_answer(ans, chunks, max_images=max_images,
                               context=gen.get("context"))
        det_list.append(det)
        breadths.append(c.get("query_breadth") or "specific")
        per_query.append({"qid": c["qid"], "query": c["query"], "answer": ans, **det})
        if with_judge_bundle:
            judge_bundle.append({
                "qid": c["qid"], "query": c["query"],
                "expected_images": c.get("expected_images", []),
                "shown_image_captions": det.get("image_map_summary", {}),
                "n_available": det["n_available"], "strategy": det["strategy"],
                "answer": ans,
            })

    out = {
        "applicable": True,
        "n_image_cases": len(img_cases),
        "posture": f"cosurface_images={cosurface}",
        "aggregate": M.aggregate(det_list) if det_list else {},
        "per_query": per_query,
        "judge_bundle_mm": judge_bundle,
    }
    # 宽泛/具体分层（仅在 goldset 显式标注 query_breadth 时输出;宽泛问题的
    # answer_image_rate 显著低于具体问题 = 待修召回缺口的量化读数）
    if any(c.get("query_breadth") for c in img_cases) and det_list:
        by_breadth = {}
        for b in sorted(set(breadths)):
            sub = [d for d, br in zip(det_list, breadths) if br == b]
            if sub:
                agg_b = M.aggregate(sub)
                by_breadth[b] = {
                    "n": len(sub),
                    "answer_image_rate": agg_b.get("answer_image_rate"),
                    "avg_images_shown": agg_b.get("avg_images_shown"),
                    "dangling_ref_rate": agg_b.get("dangling_ref_rate"),
                }
        out["by_breadth"] = by_breadth
    return out


def _run_history_arm(cases: List[Dict], top_k: int, max_images: int) -> Dict:
    """history 臂（#F-mm13b，EVAL_L4_HISTORY_ARM 显式开启才跑）：follow-up 噪声图回归钉。

    只跑 multi_turn case（goldset 携带固定注入的 history，含上轮 <<IMG:N>> 标记，
    逐字节不变 → 跨 run 可比 trend）。产出 history_marker_mimic_rate（ADVISORY，
    绝不进 hard 闸——LLM 行为指标、小样本方差大）。

    mimic 的固定操作性定义（写死，配单测）：本轮答案的 <<IMG:N>> 标记集合与注入
    history 的 assistant 文本中出现的标记集合有非空交集。可能包含合法重叠（同一
    文档两轮都真相关）→ 只做 trend：history 固定，比率的跨 run 变化才是信号
    （RAG_HISTORY_STRIP_IMG_MARKERS 改前锁基线/改后验收的口径）。
    """
    from eval_harness import mm_answer_metrics as M
    from opensearch_pipeline.content_blocks_builder import _IMG_PLACEHOLDER_PATTERN
    from opensearch_pipeline.retriever import retrieve_and_enrich

    mt_cases = [c for c in cases
                if c.get("multi_turn") and c.get("history")
                and c.get("expect_images") and c.get("live_scorable")]
    if not mt_cases:
        return {"applicable": False, "n_cases": 0,
                "note": "no multi_turn cases with history in goldset"}

    per_query: List[Dict] = []
    mimic_flags: List[bool] = []
    for c in mt_cases:
        history = c["history"]
        hist_ns = {
            int(m.group(1))
            for msg in history if msg.get("role") == "assistant"
            for m in _IMG_PLACEHOLDER_PATTERN.finditer(msg.get("content", ""))
        }
        try:
            chunks = retrieve_and_enrich(c["query"], top_k=top_k, user_dept=None,
                                         cosurface_images=False)
            gen = generate_answer_nothink(c["query"], chunks, pure_text=False,
                                          history=history)
        except Exception as e:
            per_query.append({"qid": c["qid"], "error": f"{type(e).__name__}: {e}"[:160]})
            continue
        ans = gen["answer"]
        ans_ns = {int(m.group(1)) for m in _IMG_PLACEHOLDER_PATTERN.finditer(ans)}
        mimic = bool(hist_ns & ans_ns)
        mimic_flags.append(mimic)
        det = M.analyze_answer(ans, chunks, max_images=max_images,
                               context=gen.get("context"))
        per_query.append({
            "qid": c["qid"], "query": c["query"], "answer": ans,
            "history_marker_ns": sorted(hist_ns), "answer_marker_ns": sorted(ans_ns),
            "mimic_overlap": mimic,
            "n_shown": det["n_shown"], "rendered_any": det["rendered_any"],
            "n_invalid_markers": det["n_invalid_markers"],
        })

    return {
        "applicable": True,
        "n_cases": len(mt_cases),
        "history_marker_mimic_rate": (
            round(sum(mimic_flags) / len(mimic_flags), 4) if mimic_flags else None),
        "per_query": per_query,
    }


def _run_ingestion(gt_files: List[str], docs_dir: str,
                   manifest_dir: Optional[str] = None) -> Optional[Dict]:
    """L4-ingestion:摄入侧图文绑定精度(逐格式 Jaccard)。

    Fail-open:子包内部抛异常返回带 error 的 dict,不阻断 l4 layer 调用。
    EVAL-2: 透传 manifest_dir 用于 GT-manifest preflight 漂移检测。
    """
    try:
        from eval_harness.binding import ingestion_binding
        return ingestion_binding.run(gt_files, docs_dir, manifest_dir=manifest_dir)
    except Exception as e:
        return {"deterministic": {"errors": [f"l4-ingestion exception: {type(e).__name__}: {e}"]},
                "per_doc": [], "judge_bundle_binding": []}


def run(cases: List[Dict], top_k: int = 7, max_images: int = 6,
        gt_files: Optional[List[str]] = None, docs_dir: Optional[str] = None,
        manifest_dir: Optional[str] = None) -> Dict:
    """L4 双支柱。两个支柱独立触发、独立 fail-open;applicable=True 当任一支柱出数。

    Serving 支柱是双臂（#F-mm1, 2026-07-01）：
      主臂 cosurface_images=False = 生产主流量姿态（/api/ask、钉钉），进闸、进 baseline
        —— 顶层 aggregate/per_query/judge_bundle_mm 键保持不变（report.py 与
        baseline.extract_metrics 的读取路径契约）。
      对照臂 cosurface_images=True = /api/ask/stream 姿态，落 arms.cosurface_true，
        trend-only。双臂差 = cosurface 增益的常驻 A/B。EVAL_L4_COSURFACE_ARM=false
        可关（省一半 LLM 费用）。
    ⚠️ 口径切换说明：2026-07-01 之前主数字恒以 True 姿态产出，跨该日期比趋势前先
    重冻 baseline（run_eval baseline-freeze）。

    Args:
        cases: 触发 serving(原 l4 路径)— 需要 expect_images=True 的 live_scorable case
        gt_files: 触发 ingestion(L4-ingestion 支柱)— ground_truth/*.json 路径列表
        docs_dir: ingestion 用的源文档目录(eval_samples/documents/)
        max_images: 默认 6 = 生产 config.rag.max_answer_images（旧默认 3 与生产
                    口径错位，n_shown/over_cap/avg_images_shown 失真）
    """
    import os

    serving = _run_serving(cases, top_k=top_k, max_images=max_images,
                           cosurface=False, with_judge_bundle=True)
    arms: Dict[str, Dict] = {}
    if serving.get("applicable") and os.environ.get(
            "EVAL_L4_COSURFACE_ARM", "true").lower() not in ("false", "0", "no"):
        arm_true = _run_serving(cases, top_k=top_k, max_images=max_images,
                                cosurface=True, with_judge_bundle=False)
        arms["cosurface_true"] = {
            "posture": arm_true.get("posture"),
            "aggregate": arm_true.get("aggregate", {}),
            "by_breadth": arm_true.get("by_breadth"),
            "per_query": arm_true.get("per_query", []),
        }
    # history 臂（#F-mm13b）：显式开启才跑（额外 LLM 费用 + 需要 goldset multi_turn case）
    if os.environ.get("EVAL_L4_HISTORY_ARM", "").lower() in ("1", "true", "yes"):
        arms["history"] = _run_history_arm(cases, top_k=top_k, max_images=max_images)
    ingestion = _run_ingestion(gt_files, docs_dir, manifest_dir) if (gt_files and docs_dir) else None

    applicable = bool(serving.get("applicable") or ingestion)
    if not applicable:
        return {"applicable": False,
                "note": ("L4 未触发:cases 里没有 expect_images=True 的 live_scorable case,"
                         " 且未提供 gt_files+docs_dir 触发 ingestion 支柱")}

    return {
        "applicable": True,
        "n_image_cases": serving.get("n_image_cases", 0),
        # ── serving 支柱(保持向后兼容:旧 keys aggregate/per_query/judge_bundle_mm 在顶层 =
        #    主臂（生产姿态 cosurface=False）;report.py:l4.get('aggregate') 与
        #    baseline l4srv.* 路径不变)──
        "serving_applicable": serving.get("applicable", False),
        "posture": serving.get("posture"),
        "aggregate": serving.get("aggregate", {}),
        "by_breadth": serving.get("by_breadth"),
        "per_query": serving.get("per_query", []),
        "judge_bundle_mm": serving.get("judge_bundle_mm", []),
        # ── 对照臂(trend-only,不进闸)──
        "arms": arms,
        # ── ingestion 支柱(新)──
        "ingestion": ingestion,
        "judge_bundle_binding": (ingestion or {}).get("judge_bundle_binding", []),
    }
