# GATE 2 — Rebuilt gold set + Phase-3 cost projection (read-only / local-only)

Branch `eval/goldset-rebuild` · 2026-06-23 · **PAUSE here for the Phase-3 decision.**

## A. Final composition

**`eval_harness/goldset/golden_full_plus.json` — 338 cases (302 positive / 36 negative).**
- Survivors **269**: 243 from `golden_full` (251 − 8 retired) + **26 carried from `golden_50`** (the S5-* hand-authored off_topic/metadata/positives that the builder output never had) — all dept-normalized; carried S5 marked `_carried_from`.
- New authored **69** (verify-live'd, see §B).
- **Negatives 36:** `off_topic 15 · near_miss_answer_absent 11 · live_data 4 · modality_gap 3 · metadata 3` (+ `refusal_class` on the 15 boundary refusals).
- **Permission:** 35 dept_internal positives (was **0**), 225 public-tagged, 78 unset (negatives + cross-dept).
- **Dept spread (normalized):** hr 105, admin 53, finance 51, production 25, it 21, quality 10, marketing 9, rd 8, pmc 4, supply 3 (+49 None = boundary/cross-dept negatives).

**`golden_gated_proposal.json` — 130 cases** (the Tier-B set that would run on every Phase-3 gate): 36 negatives + 59 new positives + 35 stratified survivor positives. **off_topic 15** (L2 AUC gate needs ≥5 ✓), **dept_internal 35** (first-ever ACL-path coverage), **image-flagged 7**.

## B. Per-case live-verification + keyword grounding (the 69 new)
Authored 72 → **69 PASSED, 3 dropped** by the strict verify gate. Every PASS required **both**: (1) each `keyword_gt` a **verbatim** substring of an `is_active=1` chunk of the expected doc (re-confirmed by SQL `LIKE` count ≥1), and (2) the expected doc retrieved in **top-7** by `retrieve_and_enrich` (rerank ON). Yield by stratum: dept_internal 24, public 16, SOP 10, image 9, off_topic 10. All positives carry `_grounding_chunk_id`; off_topic absence was SQL-probed. 0 qid collisions; schema validator = **OK**.

**The 3 drops are NOT authoring defects — they are live retrieval bugs surfaced by authoring (P1):** all three are **twin/duplicate-document contention** — the expected doc was crowded out of top-7 by near-identical copies:
- `S6-sop-43/44` (U8+操作手册 1.2.1新增存货档案): the **same 贸易部操作手册 content exists 3× ** — `DOC_IT_…EF9075`, `DOC_MARKETING_…C3F2B3`, plus the 生产部 copy `DOC_IT_…5AE1B3` wins top-7.
- `S6-image-57` (纸杯大杯 内控指标): expected `DOC_PRODUCTION_…4C5107` lost to `…F36478`, which is **literally a duplicate copy of the same `0207(1)` source file**.

These are concrete instances of the **same-source saturation / un-deduplicated twins** flagged in the re-evaluation (dim-4) — strong cross-doc-dedup candidates. (No gold case was forced through; they were honestly dropped.)

## C. Judge-calibration sample — status (honest)
The mechanism (`human_calibration_template.json`, 40 blank items) is ready, but it **cannot be populated now**: it needs (1) Phase-3 L3 *answers* to rate, then (2) **human** labels. Claude filling the `human{}` ratings would be circular/fabrication (per the project's standing rule). → **Deliverable: the calibration sample is a Phase-3-output-then-human step**, not a Gate-2 artifact. The automated 3-judge panel + the inter-judge-stdev≤1.2 gate still run in Phase 3 regardless; human calibration (②) gates only the *final* baseline trust, separately.

## D. Projected Phase-3 call volume + cost (gated N=130)
| Arm | Layers | DashScope | Claude judge | Wall-clock (est.) |
|---|---|---|---|---|
| **OFF** (weighted recalibration arm) | L0,L1,L2 | ~260 embeds (L0 self-query ~100 + L1 130 queries) | **none** | ~10 min (L1 = 130 × 3.08s serial ≈ 6.7 min + L0 ~2 min) |
| **ON** (production-baseline + judge) | L0–L6 | ~130 Qwen gen + ~130 query embeds + ~2,600 rerank-scored chunks (pool 20) + L4-srv 25 + L6 28k-row HA3 enum | **~50–60 `claude -p` calls** (L3 3×⌈130/20⌉=21 + L4 ~12 + L6-chunk ~27), **sequential, ≤900s each** | **judge-dominated: ~2.5–3.5 h** |
| `rerank_ab.py` sweep (dim-3) | pools {10,20,30} | 130 × 3 retrieves | none | ~20–30 min |

- **Gated N=130 is exactly at the re-check threshold** I set (>130 → re-estimate). Options: **(a)** run as-is (~3 h ON arm, judge-dominated); **(b)** trim the gated set to ~100 (drop ~30 survivor positives) → ~20% fewer judge calls. Recommend **smoke-measuring one `claude -p` batch first** to get real per-batch wall-clock before committing to the full judge run.
- **$ figures are not in code** → `TBD (measure on first judge batch)`. The **call counts above are real/derivable** (`run_judge` panels=3, batch=20, sequential).
- **L6 precondition:** raise the Family-H enum `top_k` from 12,000 to ≥ `total_active_chunks()*1.2` (~33k) **or page**, else L6 truncates → can never reach GO (this is a code change — flagged for Phase-3 approval).

## E. Open items / WARNs (non-blocking)
- **Image-required cases:** the 9 new image-stratum cases are text-answerable spec lookups (`expect_images=false`) — they test retrieval over image-bearing docs but don't assert image rendering. Genuine image-required cases (with `expected_images` gists) are a **follow-up**; L4-serving's own 25-case `golden_l4_serving.json` covers rendering.
- **metadata negatives = 3** (all carried S5) — adequate for the gate; could add more.
- **Twin-dup retrieval bugs (§B)** — 3 concrete cross-doc-dedup candidates for a separate (gated) cleanup.
- **neg_type taxonomy** — recommend adding a `refusal_class`/`sensitive_pii` dimension (Gate-1 finding).

## F. Local changes (git)
All on branch `eval/goldset-rebuild`, **0 prod writes**. New/changed files: `goldset/golden_full_plus.json`, `goldset/additions.authored_20260623.json`, `goldset/golden_gated_proposal.json`, `reports/gate1_report_2026-06-23.md`, `reports/gate2_report_2026-06-23.md`, `reports/coverage_gap_2026-06-23.json`, `reports/goldset_rebuild_plan_2026-06-23.md`. Originals (`golden_full.json`/`golden_50.json`/`baseline.json`) **untouched** (the rebuild wrote to `_plus` files).
