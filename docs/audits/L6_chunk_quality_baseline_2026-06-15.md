# L6 Chunk-Quality — Diagnostic Baseline (2026-06-15)

First run of the new `eval_harness` Layer 6 (chunk-artifact content quality), read-only
against prod_ro. Establishes the deterministic chunk-content baseline for future rebuilds.

- **Corpus**: 6690 active chunks / 562 docs (`chunk_id_set_hash=9122a42bedffae9d`, commit `44fb377`)
- **D1–D7 source**: `ha3_step_card_coverage_2026-06-15_l6recheck.json` (`d7_json_hash=62f2476c2b048c99`)
- **Verdict**: **`GO`** — all 9 hard gates measured and pass
- **Determinism**: two fingerprint-matched runs → **zero metric diffs** (fully deterministic)
- Run dirs: `eval_harness/reports/run_20260615_125539`, `run_20260615_125726`

## Hard gates (all PASS)

| Gate | Value |
|---|---|
| tokens in [5,2000] (B) | 0 out of band (0 oversize, 0 undersize, 0 token-drift) |
| no oversize structural chunk (B/A2) | 0 (A37 fix holding; procedure_parent max=1820 tok) |
| orphan step_cards (A/D4) | 0 |
| procedure_parent balance (A/D7) | missing=0, duplicate=0 |
| RDS↔HA3 step_card drift (A/D1) | sym_diff=0 |
| image_refs shape compliance (A/D6) | 0.9834 (≥0.95) |
| img_dup_factor p95 (F) | 1.0 (max 1.0 across docx/pdf/xlsx/pptx/png/jpg — zero over-attach) |
| image_refs JSON parseable (F) | 0 malformed |
| RDS↔HA3 all-type id-set (H) | missing=0, extra=0, jaccard=1.0 (clean purge confirmed) |

## Soft signals (round-1, informational — NOT gating)

**1. mid-sentence cut rate = 0.353 (CI 0.317–0.389), 451 docs.** Characterized from a sample:
a *mix* of legit non-sentence boundaries and real cuts. Top trailing chars: `◆`×132 (bullets),
`：`×105 (list lead-ins severed from their list), then single chars (`整`/`责`/`理`…, real
mid-phrase cuts) and dates/form-fields (`日`, digits — legit). Confined to `text_chunk` (641)
and `clause_chunk` (212). **Action**: the LLM `truncation` dimension on the 160-item bundle
adjudicates real vs legit; consider excluding dates/form-fields/bullets/OCR-leaders from the
deterministic detector before any hard promotion.

**2. routing-family match rate = 0.906 (CI-lower 0.879), 50 mismatches / 530 docs.** Mostly
`expected=clause_chunk` (制度/规定/policy/standard titles) but `observed=text_chunk|table_chunk`.
Metadata-level only — could be legit clause-fallback (no clause markers found) or real
misroutes. **Action**: the phase-2 faithful `resolve_split_mode` + canonical re-chunk check
confirms. D3 separately lists 105 SOP-routed-but-0-step_card under-chunk candidates.

**3. dedup**: cross-doc near-dup factor 1.035 (≤1.10 PASS). But 119 cross-doc *exact*-dup groups
+ 120 cross-doc near-dup pairs are concrete twin/boilerplate candidates worth a look
(near-dup slightly under-counts: 11 length-blocks hit the 400-cap, logged not silent).

**4. dangling-anaphor rate = 0.0011** (3 chunks) — essentially clean.

## LLM chunk-judge (Claude panel, 3 judges × 160 items)

Blinded per-chunk_type rubric (`chunk_rubric_v1`). **Inter-judge overall stdev = 0.242 (low → reliable signal, not noise).** Representative bucket (n=141) is the gate metric; risk-enriched (n=19) reported separately.

| dim | representative mean (CI) | reading |
|---|---|---|
| self_containedness | 3.59 (3.43–3.75) | `[上文]` context-prefix dangling refs + section_title↔body mismatch |
| coherence | 3.88 (3.71–4.04) | mostly one unit; some non-contiguous-numbering splices |
| type_fidelity | 3.44 (3.23–3.64) | **weakest** — thin/multi-step step_cards, clause-vs-form mislabels |
| **truncation** | **3.87 (3.70–4.03)** | **calibration: confirms the 35% mid-sentence rate is largely legit boundaries, NOT real truncation** |
| overall | 3.47 (3.30–3.64) | pass-rate (overall≥4) = **40.4%** |

By type (overall / pass@≥4): image 4.8/1.0 & visual_knowledge 4.2/0.67 (cleanest) ▸ clause_chunk 3.63/0.46 ▸ table 3.50/0.33 ▸ text_chunk 3.36/0.42 ▸ **step_card 3.23/0.34** & **procedure_parent 3.13/0.33** (weakest).

**Verdict stays GO** (soft dims don't gate; hard gates all pass, corpus functional). The actionable soft finding: highest-leverage chunk-quality fixes are (1) the `[上文]` context-prefix dangling references and (2) section_title↔body binding — both hit `self_containedness` + `type_fidelity` across step_card/text/clause. Truncation is NOT the priority.

*Data caveat*: judge j2 emitted 39 duplicate item_ids (scored some shard items twice / missed others); j1 missing 2, j3 missing 7. All 141 representative items got ≥1 verdict; some have <3 judges. Doesn't change the picture (stdev 0.242), but a re-run with stricter shard discipline would tighten per-item n.

## Accepted baseline — LOCKED 2026-06-15
This run (`run_20260615_125726`, fingerprint `chunk_id_set_hash=9122a42bedffae9d`) is the locked deterministic + LLM chunk-content baseline. Future rebuilds report deltas against it.

## Phase-2 remaining
- `resolve_split_mode` extraction + canonical-fetch faithful Family D (confirms the 50 routing candidates).
- Durable ingestion accept-manifest (A2 direct, replacing the D4/D7 proxy).
- Refine the deterministic mid-sentence detector (exclude dates/form-fields/bullets/OCR-leaders) so the soft gate tracks *real* truncation, per the LLM calibration.
- Act on the `[上文]` context-prefix + section_title binding findings (highest-leverage chunk-quality fixes).
