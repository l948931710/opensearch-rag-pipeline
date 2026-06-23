# GATE 3 — Full ON-arm eval (rerank ON, judged) — gated N=130, new 27.7k corpus

Branch `eval/goldset-rebuild` · 2026-06-23 · report dir `eval_harness/reports/p3_on_full/`.

## Headline
**Gold rebuild + public-path retrieval/answer-quality/calibration/ACL are all GREEN on the new corpus.** Two things block a clean full GO, neither is the rebuild: (1) the harness measures only the **public serving path**, so dept_internal answer-quality is confounded (fixable, L3 fixed); (2) **L6 Family-H parity: 117 RDS-active chunks missing from HA3** (pre-existing prod-data item).

## 1. Retrieval (public path, rerank ON) — STRONG
Recall@1 **0.857** · @5 **0.893** · @10 0.893 · MRR **0.872** · nDCG@10 **0.838** (56 public-scorable positives; xlsx R@5 0.932). Rerank = +7.2pp R@5. ≈ old 251-q baseline → **no degradation at 4.8× growth.**

## 2. Calibration (dim-7)
- **Production regime (rerank-ON bands 0.9/0.8): `thresholds_ok=True`, off-topic AUC 0.966** → healthy on the new corpus.
- Weighted fallback (7.7/5.8): stale (scale compressed ~0.6); `recalibrate.py` → `0.6/-0.9` (rerank-OFF only, not applied).

## 3. ACL (dim-8) — STRONG
- **Standalone per-dept probe: 16/16 core PASS** (public-excludes ∧ owner-includes ∧ non-owner-excludes) + marketing-shared PASS + injection-safe PASS. (Umbrella inconclusive — no subline-owned dept_internal doc exists to exercise it.)
- **L5 default probe: PASS** — first time actually exercised (dept_internal now live).

## 4. Answer quality (judged, 3-panel) — public STRONG, dept_internal CONFOUNDED
| bucket | n | faith | correct | complete | overall |
|---|---|---|---|---|---|
| **public positives** | 59 | 4.91 | **4.76** | **4.72** | **4.77** |
| dept_internal positives | 35 | 5.0 | **2.36** | **1.72** | 2.52 |
| negatives | 36 | 4.98 | 4.74 | — | 4.71 |
- **Public-path answer quality passes the 4.0 gates with room** (fabrication 0, source-leak 0.032 ≤0.05, inter-judge stdev 0.015).
- **dept_internal is confounded**: `l3_answer.py` (and `l1_retrieval.py`) retrieve with `user_dept=None` (public path) — a design assumption from the all-public era. So every dept_internal case retrieves empty → refuses ("未找到相关信息") → judged ~2. **80% of dept_internal positives refused vs 37% public.** This is an eval-harness limitation, **not a prod regression** (DingTalk/API authenticate the user's dept → these answer fine).
- The aggregate gates that "FAIL" (correctness 3.87, completeness 3.60, over-refusal 0.277, keyword-coverage 0.338) are **all artifacts of this confound** (keyword-coverage further depressed because the new `keyword_gt` are verbatim chunk tokens like `ＰＶＣ：７５`/`89.3±0.2` that the LLM paraphrases → zero-cov 47/80).

## 5. Multimodal (L4)
Binding pdf 0.856 / xlsx 0.892 / docx 0.990 PASS; img_dup p95 1.0; marker validity 1.0, dangling 0.0 PASS; orphan 0.76 + marker-distinctness 0.286 (advisory/trend).

## 6. L6 chunk quality + parity
All hard chunk-artifact checks PASS (tokens/oversize/orphans/parent-balance/step-drift/image-shape 0.983/img-dup 1.0/JSON). chunk-judge inter-rater 0.201. **NO_GO_DEFECT driven by Family-H idset: missing=117, extra=0** (0.42%, ~ the prior 0.34% silent-drop rate; **upper bound** — zero-vector enum is hint-only, needs per-chunk verify). L6-soft mid-sentence 0.50 + routing 0.909 (advisory).

## 7. Fixes applied this run (committed, eval-only)
- `l6_chunk_quality.py`: **paged Family-H enum** (12000→full PK range; validated truncated=False). 
- `run_judge.py`: **per-batch retry+skip** (one malformed `claude -p` batch no longer crashes the run — it did, losing the first answer-judge).
- `l3_answer.py`: **authenticate as the case dept** (`user_dept=c.get('dept')`) — fixes the dept_internal confound. **`l1_retrieval.py` still needs the same** + recall-inclusion change to fully measure the authenticated path.

## 8. GO / NO-GO
| item | verdict |
|---|---|
| Gold rebuild (338 cases, 35 dept_internal pos) | **GO** — validated, schema-clean, verbatim-grounded |
| Public-path retrieval + calibration + ACL + answer-quality | **GO** — strong, no degradation |
| dept_internal answer quality | **PENDING** — needs authenticated re-run (L1+L3 pass dept) |
| Index parity (L6 Family-H) | **NO-GO** — 117 missing (gated verify→heal; pre-existing) |
| Baseline freeze | **DEFER** — freezing now bakes in the dept_internal confound; freeze after the authenticated re-run |

## 9. Recommended next (each gated)
1. **Authenticated re-run** — apply the L1 dept fix + re-run L1/L3 + answer-judge → valid dept_internal answer quality → then freeze a clean rerank-ON production-regime baseline. (~70 min, judge-dominated.)
2. **L6 Family-H 117** — per-chunk `ha3_verify` the 117 → confirm real-drop vs enum-miss → if real, Stage-3 re-push heal (prod write, separate authorization).
3. **gold keyword_gt** — for answer-coverage, prefer answer-keywords over verbatim chunk tokens (the verbatim choice is optimal for retrieval-grounding but tanks coverage).
4. **same-source twins** (dim-4) — the 3 authoring-surfaced duplicates → cross-doc-dedup candidates.
