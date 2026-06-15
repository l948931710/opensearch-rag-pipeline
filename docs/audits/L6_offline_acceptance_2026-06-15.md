# L6 chunker-fix — Offline full-corpus acceptance (staging substitute, read-only)

2026-06-15. The fixes (Fix A drop `[上文]`; Fix B section_title de-staling, clause/text only) committed in `ffb0150`. Staging holds only the 10-doc dry-run, so live L1–L5 against the full gold set needs a full staging rebuild; per decision we ran the **offline full-corpus validation first** (no staging/prod write). The after-corpus is built by applying the chunker's **real** `_resolve_clause_section_title` + `[上文]` removal to the live chunks — equivalent to re-chunk **by construction** (both fixes apply *after* segmentation, so chunk boundaries are identical; verified count/structure unchanged).

## Staging preview
- **Target**: 280 docs (Fix A 123 ∪ Fix B 277); 3199 active chunks, **2707 text-changed** (84.6%), 492 unchanged-within.
- **Version scope**: none — intra-chunk content replace, new count == old, `chunk_index`/parent/step/image_refs preserved.
- **Unaffected**: 282 docs, 0 chunks touched.

## Acceptance results

| # | Criterion | Result |
|---|---|---|
| **1** | D1–D7 structural: no orphan / missing-parent / drift / image_refs regression | **PASS** — orphan step_cards 0; `[上文]` residual **0**; image_refs_json 0 changed; chunk_index/parent_chunk_id/step_no 0 touched |
| **2** | L6 vs locked baseline (self-cont, type_fidelity, section_title mismatch, `[上文]` residual) | **PASS** — `[上文]` eliminated; **1519 stale section_titles fixed** (514 blanked + 1005 own-heading); semantic gains from locked LLM A/B: self_cont **+0.83/+0.89**, type_fid **+0.59**, overall **+0.38**; deterministic heuristics flat (mid-sentence 0.353→0.353, routing 0.906→0.906; minor: dangling 0.0011→0.0016, near-dup 120→122) |
| **3** | L1–L5 live (full dense+sparse+BM25, prod rerank, real gold queries) | **DEFERRED** — needs full 562-doc staging rebuild (gold queries target the whole corpus). Offline hybrid recall (below) substitutes for the affected-chunk retrieval signal |
| **4** | By-type clause/text recall@1/@5, MRR, nDCG + failures | **PASS** — hybrid dense+sparse: clause r@1 0.980→**0.987**, text 1.0→1.0, ALL 0.988→**0.992**; MRR/nDCG up; **0 rank regressions → no failure samples**. (Cross-checks: dense-only r@1 0.967→1.000; qwen3-rerank Δ +0.011, 0 drops) |
| **5** | step_card / procedure_parent / table / image_refs / unaffected types unchanged | **PASS** — non-clause/text chunks **0 changed** (byte-equal); image_refs_json/parent/step/chunk_index untouched (transforms only edit chunk_text + section_title for clause/text/section) |
| **6** | Same query set; regression threshold; GO/NO-GO | see below |

## #6 — Regression thresholds & verdict
**Significant-regression threshold (pre-set):** recall@1 drop > 2pp on any chunk_type, OR any chunk dropping rank-1→rank>5, OR MRR drop > 0.02. **Observed:** recall@1 **+0.4pp** (ALL), clause **+0.7pp**, text flat; 0 individual regressions; MRR +0.002–0.003 — well inside tolerance (improvement, not regression).

**OFFLINE ACCEPTANCE = GO.** Quality significantly improved AND retrieval shows no material regression (recall up), structure intact, unaffected types byte-equal.

## Remaining gate before prod
Live **L1–L5** with the real gold query set against a real index (full hybrid + prod rerank routing) — requires a **full 562-doc staging rebuild** (the affected 280 alone can't score the broad gold set). Per your gate ("staging L1–L6 + D1–D7 全通过后再申请生产"), this is the one outstanding item. Options: (a) full staging rebuild → live L1–L6, or (b) accept offline evidence (recall up, 0 regressions, structure intact) and authorize a carefully-gated prod re-chunk. No prod/staging write performed.

Artifacts (gitignored): `scratch/l6_ab/affected_docs.json` (280), `after_corpus.json`. Reproducible transforms: `eval_harness/l6_ab.py` + chunker `_resolve_clause_section_title`.
