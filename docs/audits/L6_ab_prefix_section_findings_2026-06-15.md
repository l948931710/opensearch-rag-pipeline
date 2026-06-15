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

## 5. Recall A/B (offline, read-only — `python -m eval_harness.l6_ab recall`)

Same prod DashScope embedding + qwen3-rerank (prod has `RAG_RERANK_ENABLE=true`). Body-query (chunk's prefix-stripped core, identical for before/after) vs a 250 random-distractor pool.

**Embedding stability** cos(before,after): Fix A weak 0.983 / **Fix A ok 0.950 (min 0.851, 3<0.90 — the risk zone)** / Fix B 0.980 / step_card (off) =1.0.

**Dense self-retrieval recall — 0 regressions:**
| condition | n | before r@1/r@5/MRR/nDCG | after |
|---|---|---|---|
| Fix A (drop `[上文]`) | 27 | 1.00/1.00/1.00/1.00 | **1.00/1.00/1.00/1.00** |
| Fix B (de-stale clause/text) | 18 | 0.94/1.00/0.97/0.98 | **0.94/1.00/0.97/0.98** |
| step_card (Fix B OFF, control) | 15 | 1.00/… | 1.00/… (unchanged) |

By chunk_type r@1: clause 0.97→0.97, text 1.00→1.00, step 1.00→1.00. **rank_regressions = 0.** (The one FixB chunk at 0.94 was already not-rank-1 *before* the fix.)

**Rerank score A/B** (risk zone, n=18): mean Δ(after−before) = **+0.011** (slightly positive), 0 drops > 0.02. Removing the prefix noise makes qwen3-rerank score the chunk marginally *more* relevant to its own content.

**parent/step/image (verified against the live retriever):** `expand_step_context` fires only for `step_card`; image cosurface only for image/step/proc/visual types — the fixed chunks are clause/text, so neither triggers; step_cards keep Fix B OFF; neighbor stitching keys on `chunk_index` (untouched). **Zero impact on parent/step expansion or image_refs.**

> Caveat: dense-only offline approximation. Full 3-way hybrid (dense+sparse+BM25) against BOTH variants needs the eventual re-chunk → validate end-to-end in **staging** post-fix (L1–L5).

## 6. DECISION — recall A/B PASSED
Quality significantly improves (readability A/B: +0.38 overall, +0.59 type_fidelity, +0.83/0.89 self_cont) **AND** retrieval shows no material regression (recall@1 flat, 0 rank regressions, rerank +0.011). **Within tolerance → proceed to the chunker-fix plan (below) for approval.** No production re-chunk/re-index performed this round.

**Should L1–L5 run now?** No. They'd only re-measure the current (pre-fix) index — uninformative for the fix. L1–L5 are the **post-re-chunk staging validation gate**, after the chunker fix lands.

## 7. Final chunker fix plan (FOR APPROVAL — not yet applied)

**Fix A — drop the `[上文]` breadcrumb** (`chunker.py:_chunk_by_clause`, ~1668/1687). Remove the `[上文] {prev_clause_title}` prepend. Recall-safe across weak + substantive; readability win on both. *Conservative variant*: gate to non-weak only (`l6_ab.is_weak_prev`) — but the A/B supports full removal. Scope: clause mode. Affected: ~1188 chunks / 123 docs.

**Fix B — section_title staleness** (`current_section` tracking, `chunker.py:612/1563/1918`), **clause/text only, NOT step_card**. Minimal behavior-preserving: when a chunk's own leading line is a numbered heading that differs from the inherited `current_section`, prefer the chunk's own heading; else drop the `章节` component from the `【…】` prefix + leave `section_title` empty rather than wrong. (Deeper fix = better heading detection so `current_section` advances; larger change.) Affected: ~1448 clause-fragment labels / 89 docs (excluding step_card).

**Rollout (all gated on your authorization, none this round):** patch chunker → unit/byte-equal tests → **staging** re-chunk of affected docs → run L6 + L1–L5 in staging (end-to-end hybrid+rerank confirmation) → if green, prod re-chunk + re-index of affected docs via the standard rebuild path.

Artifacts: `scratch/l6_ab/*` (gitignored); reproducible via `eval_harness/l6_ab.py` (`build` / `recall`). Baseline (`L6_chunk_quality_baseline_2026-06-15.md`) stays the usable initial baseline — **GO = hard structural pass; semantic quality has clear, now-quantified room, and the `[上文]` + section_title fixes are validated levers (recall-safe).**

## 8. Chunker patch IMPLEMENTED (code + offline tests only — no re-chunk/re-index)

`chunker.py`: Fix A removes the `[上文]` prepend in `_chunk_by_clause`; Fix B adds `_resolve_clause_section_title` (own numbered/chapter heading → corroborated-inherited → empty), applied in `_create_chunk` **for clause/text/section only** (step_card/table/faq/proc_parent/visual untouched). +2 dead-import cleanups (`uuid`, a local `re`). Tests: `TestL6ContentFixes` (6-req coverage) + updated `TestClauseInterClauseOverlap`; **full suite 1032 green, ruff clean**.

**Implemented Fix B differs from the §3 blank-only A/B** — it *replaces* with the chunk's own heading where available, blanking only when no reliable title. Re-validated the EXACT implemented behavior:
- embedding stability cos(before,after): own-heading-replace 0.936 (min 0.860) / blank 0.954 (min 0.776).
- **dense recall A/B (n=60, implemented resolver): r@1 0.967 → 1.000, r@5/MRR/nDCG → 1.0, 0 rank regressions** — own-heading-replacement is recall-*positive* (own heading is more body-aligned than a stale label).

**Affected preview (read-only estimate, current 6690-chunk corpus):**
| fix | chunks | docs | detail |
|---|---|---|---|
| Fix A (drop `[上文]`) | 1188 | 123 | 100% clause_chunk |
| Fix B (clause/text) | 1519 | 277 | **1005 own-heading-replace + 514 blank**; text 415 / clause 1104 |
| step_card section_title (Fix B OFF) | 0 changed | — | 1897 labels untouched |
| **combined** | **1702 (25.4%)** | **280 / 562** | — |

**Status: code + tests + recall validation complete; awaiting staging re-chunk authorization.** Staging gate = re-chunk the 280 affected docs → run L6 + L1–L5 end-to-end (full hybrid+rerank). No prod write performed.
