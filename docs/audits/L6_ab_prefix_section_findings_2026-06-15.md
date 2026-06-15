# L6 follow-up — `[上文]` context-prefix & section_title↔body mismatch (read-only diagnosis + offline A/B)

2026-06-15. Read-only; **no production writes, no re-chunk**. Investigates the two highest-leverage soft findings from the L6 LLM panel.

## 1. Prevalence (full corpus, 6690 chunks)

| | Issue A — `[上文]` | Issue B — section_title stale/wrong |
|---|---|---|
| chunks | 1188 | 1448 numbered-clause-fragment labels; 90 (doc,label) groups stamped on ≥10 chunks |
| docs | 123 | 89 |
| chunk_type | 100% clause_chunk | step_card 822 / clause 336 / text 133 / table 110 / proc_parent 24 |
| weak/egregious | 46 weak ([上文]→structural header) | `七、安全奖惩制度`×154, `五、保管方法`×119, `3.2 生产车间职责`×104 (a 报关 doc) |
| judge-rationale mentions | 43/478 (~9%) | 76/478 (~16%) |

## 2. Root cause — both in the chunker

- **A → clause-mode context stitching** (`chunker.py:1655-1700`): `[上文] {first line of the previous segment}` prepended unconditionally — a horizontal *previous-sibling* breadcrumb, dangling when that sibling is a header/unrelated. Baked into `chunk_text`.
- **B → section-title inheritance** (`chunker.py:612/1563/1918`): `current_section = section_path or text` advances only on recognized heading blocks; sparse/missed headings → it goes stale and stamps a whole run (154 chunks). Carried in both the `【章节:X】` chunk_text prefix and the `section_title` field.
- Clause chunks carry BOTH prefixes stacked: `【文档|章节】\n[上文] …\n{body}`.

## 3. Offline A/B (60 pairs × before/after, 3 blinded judges, full 3/3 coverage, inter-judge stdev 0.165)

"after" = fix simulated at text level (strip `[上文]` line / drop stale `章节` component + blank section_title). Paired Δ = after − before:

| stratum (n) | self_cont | coherence | type_fid | truncation | overall |
|---|---|---|---|---|---|
| I1 weak `[上文]` (12) | **+0.83** | +0.39 | +0.06 | +0.06 | **+0.72** |
| I1 ok `[上文]` (15) | **+0.89** | +0.84 | +0.18 | +0.91 | **+0.62** |
| I2 stale section (18) | +0.26 | +0.17 | **+0.59** | +0.04 | +0.26 |
| I2 step clausefrag (15) | +0.04 | +0.02 | +0.02 | +0.00 | +0.02 |
| **ALL (60)** | +0.48 | +0.34 | +0.24 | +0.25 | **+0.38** |

overall pass@≥4: 0.133 → 0.233. **28 improved / 31 unchanged / 1 regressed** (benign).

## 4. Recommendations

1. **Fix A — drop `[上文]` from clause chunks (highest value).** Helps weak AND "ok" cases (self_cont +0.83/+0.89, overall +0.62/+0.72) — the previous-sibling breadcrumb is net-harmful to readability even when "real". Conservative variant = gate only the 46 weak; data supports full removal.
2. **Fix B — de-stale section_title for clause/text ONLY, not step_cards.** type_fidelity +0.59 on stale clause/text; **+0.02 on step_cards (no effect — don't bother)**. Real fix = heading-detection / staleness guard so `current_section` can't span a whole doc; or self-correct from the chunk's own leading numbered heading.
3. **Safety**: 1/60 regression, stdev 0.165 → low-risk on readability dims.

## 5. Impact assessment (for the eventual prod change)
- **parent-child structure**: no impact (parent_chunk_id/step_no untouched; section_title is not a structural key).
- **image_refs**: no impact (image_refs_json untouched).
- **retrieval recall**: both fixes change `chunk_text` → embeddings. Removed content is noise (weak sibling) / wrong (stale label) → expected neutral-to-positive. **NOT yet measured** — recommend a recall A/B (re-embed before/after variants, self-retrieval rank-1 via Family G machinery) BEFORE committing to a prod re-chunk.
- Both are **chunker** changes ⇒ require re-chunk + re-index to land (deferred; this round is design + offline A/B only).

Artifacts: `scratch/l6_ab/ab_bundle.json`, `ab_pairs.json`, `shard_{0..5}.json`. Baseline (`L6_chunk_quality_baseline_2026-06-15.md`) stays the usable initial baseline — **GO = hard structural pass; semantic quality has clear, now-quantified room (the `[上文]` + section_title fixes are the levers).**
