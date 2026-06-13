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

import os
import sys
from typing import Dict, List, Optional

from .. import envboot  # noqa: F401
from ..gen_nothink import generate_answer_nothink

_SCRIPTS = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples/scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _run_serving(cases: List[Dict], top_k: int, max_images: int) -> Dict:
    """L4-serving:LLM `<<IMG:N>>` 摆放质量(原 l4 路径,封装进 serving 子键)。"""
    import mm_answer_metrics as M
    from opensearch_pipeline.retriever import retrieve_and_enrich

    img_cases = [c for c in cases if c.get("expect_images") and c.get("live_scorable")]
    if not img_cases:
        return {"applicable": False, "n_image_cases": 0,
                "note": "No image-expecting, live-scorable cases"}

    per_query: List[Dict] = []
    det_list: List[Dict] = []
    judge_bundle: List[Dict] = []
    for c in img_cases:
        try:
            chunks = retrieve_and_enrich(c["query"], top_k=top_k, user_dept=None,
                                         cosurface_images=True)
            gen = generate_answer_nothink(c["query"], chunks, pure_text=False)
        except Exception as e:
            per_query.append({"qid": c["qid"], "error": f"{type(e).__name__}: {e}"[:160]})
            continue
        ans = gen["answer"]
        det = M.analyze_answer(ans, chunks, max_images=max_images)
        det_list.append(det)
        per_query.append({"qid": c["qid"], "query": c["query"], "answer": ans, **det})
        judge_bundle.append({
            "qid": c["qid"], "query": c["query"],
            "expected_images": c.get("expected_images", []),
            "shown_image_captions": det.get("image_map_summary", {}),
            "n_available": det["n_available"], "strategy": det["strategy"],
            "answer": ans,
        })

    return {
        "applicable": True,
        "n_image_cases": len(img_cases),
        "aggregate": M.aggregate(det_list) if det_list else {},
        "per_query": per_query,
        "judge_bundle_mm": judge_bundle,
    }


def _run_ingestion(gt_files: List[str], docs_dir: str) -> Optional[Dict]:
    """L4-ingestion:摄入侧图文绑定精度(逐格式 Jaccard)。

    Fail-open:子包内部抛异常返回带 error 的 dict,不阻断 l4 layer 调用。
    """
    try:
        from eval_harness.binding import ingestion_binding
        return ingestion_binding.run(gt_files, docs_dir)
    except Exception as e:
        return {"deterministic": {"errors": [f"l4-ingestion exception: {type(e).__name__}: {e}"]},
                "per_doc": [], "judge_bundle_binding": []}


def run(cases: List[Dict], top_k: int = 7, max_images: int = 3,
        gt_files: Optional[List[str]] = None, docs_dir: Optional[str] = None) -> Dict:
    """L4 双支柱。两个支柱独立触发、独立 fail-open;applicable=True 当任一支柱出数。

    Args:
        cases: 触发 serving(原 l4 路径)— 需要 expect_images=True 的 live_scorable case
        gt_files: 触发 ingestion(L4-ingestion 支柱)— ground_truth/*.json 路径列表
        docs_dir: ingestion 用的源文档目录(eval_samples/documents/)
    """
    serving = _run_serving(cases, top_k=top_k, max_images=max_images)
    ingestion = _run_ingestion(gt_files, docs_dir) if (gt_files and docs_dir) else None

    applicable = bool(serving.get("applicable") or ingestion)
    if not applicable:
        return {"applicable": False,
                "note": ("L4 未触发:cases 里没有 expect_images=True 的 live_scorable case,"
                         " 且未提供 gt_files+docs_dir 触发 ingestion 支柱")}

    return {
        "applicable": True,
        "n_image_cases": serving.get("n_image_cases", 0),
        # ── serving 支柱(保持向后兼容:旧 keys aggregate/per_query/judge_bundle_mm 在顶层)──
        "serving_applicable": serving.get("applicable", False),
        "aggregate": serving.get("aggregate", {}),
        "per_query": serving.get("per_query", []),
        "judge_bundle_mm": serving.get("judge_bundle_mm", []),
        # ── ingestion 支柱(新)──
        "ingestion": ingestion,
        "judge_bundle_binding": (ingestion or {}).get("judge_bundle_binding", []),
    }
