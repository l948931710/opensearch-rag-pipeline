# Reranker prototype A/B — does it boost performance? (Yes, significantly)

**Date:** 2026-06-07 · **Index:** `fuling_kb_chunks` · **Method:** `eval_harness/rerank_ab.py`, 251-case `golden_full`, hybrid top-20 candidate pool (rerankers only REORDER) · **Models:** DashScope `qwen3-rerank` (text), `qwen3-vl-rerank` (image+text, passes signed OSS image URLs). Data: `rerank_ab.json`.

## Result — significant, and the two rerankers are complementary

| Query set (n) | Metric | Baseline (hybrid) | qwen3-rerank (text) | qwen3-vl-rerank (img+text) |
|---|---|---|---|---|
| **Text-only pool (122)** | recall@1 | 0.754 | **0.885 (+13.1pp)** | (text path) |
| | recall@5 | 0.934 | **0.984** | |
| | MRR | 0.841 | **0.926** | |
| | nDCG@10 | 0.769 | **0.852** | |
| **Image-bearing pool (40)** | recall@1 | 0.825 | 0.700 (**−12.5pp, hurts**) | **0.850 (+2.5pp)** |
| | MRR | 0.861 | 0.802 | **0.895** |
| | nDCG@10 | 0.750 | 0.748 | **0.816 (+6.6pp)** |
| **Confidence (pos vs neg)** | Youden J on top-1 score | 0.46 (fused) | 0.61 | **0.66** |

**Routed (VL when the pool has images, else text), full set (162 positives):**
recall@1 0.772 → **0.877 (+10.5pp)** · recall@5 0.926 → **0.975** · MRR 0.846 → **0.918 (+7.2pp)** · nDCG@10 0.764 → **0.843 (+7.9pp)**.

## Why complementary

- **Text reranker** jointly attends query+text and is a big win on text queries (+13pp recall@1), but image-bearing chunks carry thin `chunk_text` (often `[图片描述]`/`visual_summary`), so it **demotes** them → it *regresses* image queries (0.825→0.70).
- **VL reranker** sees the actual image, so it correctly promotes the answer-bearing figure (e.g. `QA-30`: baseline #6, text-rerank #12, **VL #1**; `SRC-06`/`J-r120_30/31/36`: VL → #1). It only fires when the pool has images (40/162 here); elsewhere it's a no-op.

## Confidence / discrimination (the root-cause win)

The reranker score (0–1) separates answerable from unanswerable far better than the fused score: **Youden J 0.46 → 0.61 (text) / 0.66 (VL)** (VL fpr=0.278). This is the real fix for the weak score discrimination behind the calibration finding — a rerank-score threshold (~0.76 text / ~0.84 VL) is a much cleaner confidence/refusal signal than the fused 8.0/5.0 bands.

## Recommended design — routed rerank inside `retrieve_and_enrich`

1. **Over-fetch** the hybrid pool (top-20) in `search_chunks` (already supported).
2. **Route**: if the pool contains image-bearing chunks → `qwen3-vl-rerank` (attach signed `image_url` via `oss_url.generate_signed_url`); else → `qwen3-rerank` (text).
3. Take the reranked **top-k (7)**, *then* neighbor-stitch + step-expand (unchanged).
4. Gate behind config: `RAG_RERANK_ENABLE` (default off until shipped), `RAG_RERANK_TEXT_MODEL=qwen3-rerank`, `RAG_RERANK_VL_MODEL=qwen3-vl-rerank`, `RAG_RERANK_POOL=20`.
5. **Recalibrate the 高/中/低 labels onto the rerank score** (0–1 scale) — the fused-score thresholds (7.7/5.8) don't apply once ranking is by rerank score; use the separation points (~0.76/lower) and `eval_harness/recalibrate.py` on a rerank run.

## Caveats

- Adds 1 rerank API call per query: ~1–2s (text), more for VL (image fetch + VL inference) — a real latency add to the bot; mitigate with the over-fetch pool kept small (20) and VL only on image pools.
- Sample sizes (text 122 / image 40) are modest, but the deltas are large and directionally consistent (text-rerank uniformly ↑ on text, uniformly ↓ on image; VL ↑ on image), so the routing conclusion is robust.
- VL depends on signed OSS image URLs being fetchable from DashScope (verified: GET 206 image/jpeg). The internal→public endpoint rewrite in `oss_url.py` is required.

---

## IMPLEMENTED + END-TO-END VALIDATED (2026-06-07)

Routed reranker shipped (OFF by default) in `opensearch_pipeline/reranker.py`, wired into
`retrieve_and_enrich` (over-fetch `rerank_pool`=20 → route-rerank → top_k → stitch/expand).
Routing is by **serving mode**: multimodal/image-text path → `qwen3-vl-rerank` (signed
`image_url`); pure-text/bot → `qwen3-rerank`, with image-heavy pools also routed to VL
(`rerank_route_vl=True`, data-driven). Text reranker docs use the richest text
(chunk_text + visual_summary + ocr). `_format_context` labels switch to rerank thresholds
(0.9/0.8, recalibrated) when a chunk carries `rerank_score`. Enable via `RAG_RERANK_ENABLE=true`.

### End-to-end eval — rerank OFF → ON (golden_full 251, 3-judge Claude panel)

| Metric | OFF | ON | Δ |
|---|---|---|---|
| retrieval recall@1 | 0.778 | **0.876** | +9.9pp |
| retrieval recall@5 | 0.926 | **0.975** | +4.9pp |
| retrieval MRR | 0.849 | **0.918** | +6.9pp |
| retrieval nDCG@10 | 0.854 | **0.903** | +4.9pp |
| source-attr recall@1 (来源标注) | 0.789 | **0.947** | +15.8pp |
| answer correctness (/5) | 4.29 | **4.51** | +0.21 |
| answer completeness (/5) | 4.00 | **4.23** | +0.23 |
| answer relevance (/5) | 4.56 | **4.68** | +0.12 |
| answer overall (/5) | 4.31 | **4.48** | +0.17 |
| pass-rate overall≥4 (pos) | 0.758 | **0.791** | +3.3pp |
| faithfulness (/5) | 4.985 | 4.978 | ~flat (no regression) |
| positive fabrication | 0.000 | 0.004 | ~0 |
| keyword coverage (pos) | 0.687 | **0.746** | +5.9pp |
| gen latency (ms) | 6506 | 7849 | +1.3s (rerank call) |

**Conclusion:** the routed reranker delivers a large retrieval lift that *carries through to
answer quality* (correctness +0.21, completeness +0.23, pass-rate +3.3pp) with **no
faithfulness/fabrication regression**. Cost is ~+1.3s/answer. Recommend enabling in serving.
