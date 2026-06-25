# Phase 3 — OFF-arm results (read-only, no judge) — gated N=130, new 27.7k corpus

Branch `eval/goldset-rebuild` · 2026-06-23 · **PAUSE for ON-arm (judge) go/no-go.**
Reports: `eval_harness/reports/p3_on_arm/` (rerank ON) + `p3_off_arm/` (weighted).

## 1. Retrieval quality (new corpus) — HEALTHY
| metric (gated, 56 scorable positives) | rerank ON | rerank OFF (weighted) |
|---|---|---|
| Recall@1 | **0.857** [0.768–0.946] | — |
| Recall@5 / @10 | **0.893** | 0.821 |
| MRR | **0.872** | — |
| nDCG@10 | **0.838** | — |
| xlsx Recall@5 | **0.932** | 0.841 |

- Rerank ON = **+7.2pp recall@5** on the new corpus (consistent with the old +10.5pp R@1 A/B) → rerank still pays.
- These are ≈ the old 251-q baseline (R@1 0.876) — **retrieval did NOT degrade with ~4.8× growth.** The ~11% not-found includes the twin-contention cases (see §4).

## 2. Calibration (dim-7) — production regime is FINE; only the weighted fallback is stale
- **rerank-ON bands (0.9/0.8): `thresholds_ok=True`, off-topic AUC 0.966** (≥0.85 gate). **Production serves rerank-ON** (per qa_session_log top_score in the 0-1 band) → **the active thresholds are healthy on the new corpus; no urgent change.** This walks back the re-evaluation's dim-7 urgency.
- **weighted (7.7/5.8): `thresholds_ok=False`** — the score scale **compressed** post-rebuild (weighted top-1 now ~0.6, never reaches 7.7). `recalibrate.py` → recommended **weighted `high=0.6 / medium=-0.9`** (Youden J=0.62 @ 0.604). **Recommendation only, NOT applied** — and only relevant to the rerank-OFF fallback. (The negative medium = the low band is ~empty; treat the exact values as advisory.)

## 3. Index health (L0) — PASS
docCount/self-query/vector-fidelity all green on the live corpus.

## 4. L6 chunk-artifact quality + parity
**Chunk artifacts: all hard checks PASS** — tokens-in-range, no oversize, orphan step_cards=0, procedure_parent balance {missing 0, dup 0}, RDS↔HA3 step_card drift=0, image_refs shape 0.983, img_dup p95 1.0, JSON parseable. (L6-soft advisory: mid-sentence 0.50 [known-crude detector], routing-family 0.909.)

**⚠️ The one real parity item — L6 Family-H all-type idset: `missing=117, extra=0`.** 117 RDS-active chunks not found in the paged HA3 enum (0.42% — same magnitude as the prior 0.34% silent-drop rate, since healed). **This is an UPPER BOUND**: the zero-vector enum is hint-only for missing-detection (G30 non-determinism), so some of the 117 may be enum-misses, not real drops. **Action (separate, gated):** per-chunk `ha3_verify.verify_chunks_present` on the 117 → confirm real-drop vs enum-miss; if real, it's a heal candidate (Stage-3 re-push, a prod write — your call). Drives L6 verdict = NO_GO_DEFECT (correctly fail-closed).

## 5. Same-source saturation (dim-4) — confirmed live
Phase-2 authoring already surfaced 3 concrete twin/duplicate-doc contention cases (U8+操作手册 ×3 across IT/marketing; 纸杯 `0207(1)` literal dup) where the right doc is crowded out of top-7. The ~11% not-found in §1 is partly this. → cross-doc-dedup / doc-quota candidates (separate, gated).

## 6. What the OFF arm did NOT measure (needs the ON-arm judge run, ~2h)
Answer quality (faithfulness/correctness/completeness/source-leak), the 3-judge Claude panel + inter-judge-stdev gate, L4-serving marker hygiene, L6 chunk-judge. The OFF arm covers retrieval + calibration + parity only.

## 7. Verdict for the ON-arm decision
**Retrieval + production-calibration + chunk-artifacts are healthy on the new corpus.** Open items are (a) the weighted-threshold recalibration (recommendation ready, prod-irrelevant since rerank-ON), (b) the Family-H 117-missing parity gap (verify→maybe-heal, gated), (c) same-source twins (gated dedup). None block the gold set itself. The ON-arm judge run would add answer-quality + the freezable production-regime baseline.
