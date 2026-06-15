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

## Phase-2 (not yet done)
- 180→160-sample LLM chunk-judge (bundle `judge_bundle_chunk.json` written; run the Claude
  panel → `judge_verdicts_chunk.json` → `run_eval merge`) — calibrates mid-sentence/truncation.
- `resolve_split_mode` extraction + canonical-fetch faithful Family D (confirms the 50 routing
  candidates).
- Durable ingestion accept-manifest (A2 direct, replacing the D4/D7 proxy).
- Lock the **accepted** baseline once the LLM pass + faithful routing land.
