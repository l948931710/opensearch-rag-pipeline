# Phase 3 — CLOSE-OUT (authenticated re-run) — new 27.7k corpus, gated N=130

Branch `eval/goldset-rebuild` · 2026-06-23 · read-only eval; the only prod follow-up is the gated 117-heal. Baseline frozen: `goldset/baseline_authenticated_2026-06-23.json`.

## Final verdict: the rebuilt gold + the new-corpus serving path are GREEN
The Gate-3 "answer-quality failure" was **100% an eval-harness confound** (L1/L3 retrieved on the public path, `user_dept=None`, so dept_internal — 76% of the corpus — was structurally refused). After fixing L1+L3 to authenticate as the case dept (mirrors prod), the authenticated re-run is clean.

| gate | public-path run (confounded) | **authenticated run** |
|---|---|---|
| retrieval recall@5 / MRR | 0.893 / 0.872 (n=56, public only) | **0.945 / 0.913 (n=91, +dept_internal)** |
| over-refusal | 0.277 | **0.0** |
| answer correctness (Claude) | 3.87 ❌ | **4.84 ✅** |
| answer completeness | 3.60 ❌ | **4.82 ✅** |
| faithfulness / fabrication | 4.94 / 0 | **4.94 / 0** |
| source-leak / inter-judge stdev | 0.032 / 0.015 | **0.011 / 0.015** |
| calibration (rerank-ON 0.9/0.8) | thresholds_ok=True, AUC 0.966 | **same** |
| dept_internal answer quality | 2.36 / 1.72 (refused) | **4.87 / 4.86** |

**ACL (dim-8):** per-dept probe 16/16 + marketing-shared + injection-safe + L5 PASS (first real exercise). **Multimodal (L4):** binding pdf .856/xlsx .892/docx .990, marker validity 1.0.

## The one real issue (not the rebuild): 117 confirmed HA3 silent drops
L6 Family-H: **117 RDS-active chunks missing from HA3**, authoritatively confirmed by per-chunk exact-`chunk_id` point-read (0 are enum artifacts). 0.42% of active, ~100 docs (1–2 chunks each), mostly 06-21/22 campaign production/rd + some 05-13/14 HR/admin. Same class as the earlier healed 96. **Fix = Stage-3 re-push heal (gated prod write).** List: `scratch/goldset_rebuild/verify_117_result.json`.

## Eval-harness fixes committed this phase (eval-only, additive)
1. `l6_chunk_quality.py` — **paged Family-H enum** (12000→full PK range; was un-GO-able on 28k). 
2. `run_judge.py` — **per-batch retry+skip** (one malformed `claude -p` batch no longer crashes the run; proved itself: 129/130 graceful).
3. `l3_answer.py` + `l1_retrieval.py` — **authenticate as the case dept** (the confound fix).

## Frozen baseline
`baseline_authenticated_2026-06-23.json` — regime: fusion=weighted, **rerank_enable=true**, eval_set_sha=778a97d5 (gated 130), code 3163349, 47 metrics. `threshold_version` records what **prod currently serves** (7.7/5.8 + 0.9/0.8) — NOT changed here. **Note: prod serves rerank-ON; the rerank-ON 0.9/0.8 bands are healthy on the new corpus.**

## Remaining items (all gated / separate)
| item | type | note |
|---|---|---|
| **117-drop heal** | prod write | Stage-3 re-push w/ parity-verify (your authorization) — the only prod action |
| Apply recalibrated **weighted** thresholds (0.6/-0.9) | config/deploy | rerank-OFF fallback only; prod is rerank-ON so low priority |
| Deploy the 4 eval-harness fixes | PR/merge | open a PR from `eval/goldset-rebuild` → main |
| `keyword_gt` for coverage | gold quality | new cases use verbatim chunk tokens (great for retrieval, tanks coverage 0.468; correctness 4.84 shows answers are right) — regen as answer-keywords if the coverage gate matters |
| same-source twins (3 found) | prod dedup | cross-doc-dedup candidates surfaced by authoring |
| umbrella ACL | n/a | no subline-owned dept_internal doc exists to exercise (unit-tested only) |

## GO / NO-GO
- **Gold rebuild + new-corpus retrieval/answer/calibration/ACL: GO** — trust the new baseline for these.
- **Index parity: NO-GO until the 117-heal** — but this is a pre-existing prod-data gap, independent of the gold/serving quality.
