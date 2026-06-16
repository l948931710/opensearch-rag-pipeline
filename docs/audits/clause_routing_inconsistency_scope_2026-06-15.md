# Scope: clause-mode → text downgrade for 制度/规定 docs (2026-06-15)

Surfaced by the GT chunk-eval cohort expansion (tranche-2): several numbered 制度 docs
(`docx_smoking`, `docx_forklift`) routed to **clause mode** yet emitted `text_chunk`, not
`clause_chunk`. This scopes the root cause, impact, fix, and risk. **Not a content-loss bug**
(content stays retrievable) — it is a **chunk-granularity + type-metadata** quality issue.

## Symptom & quantification

Mode routing (`pipeline_nodes.py:2891`) sends a doc to clause mode when
`category_l1/l2 ∈ {policy,standard,regulation}` **or** the title contains `制度/规定/规范`.
That part works. But the **clause chunker then downgrades to `text_chunk`**.

Local corpus dry-run over 15 `制度/规定/规范`-titled docx in `fuling_chunk_exp/`:
**7/15 (47%) downgraded clause→text.** Substantial ones: `吸烟管理制度` (1926c), `叉车管理制度`
(1866c). (Several others are short stubs.)

## Root cause

`chunker.py::_chunk_by_clause` builds `full_text` from **paragraph blocks only**, then runs
`_CLAUSE_RE` (`chunker.py:1589-1599`). If it finds **no** clause boundaries it
**falls back to `text_chunk`** (`chunker.py:1659-1675`). Two reasons it finds none:

1. **Regex gaps** (primary). `_CLAUSE_RE`'s Arabic-decimal alternative is
   `\d+\.\d+(?:\.\d+)?\s` — it **requires a trailing space**. Real docs write `3.1公司办…`
   / `4.2.1检查…` (no space) → no match. Single-level `1.目的`/`2.适用范围` isn't matched at all
   (only `1、` 顿号). Letter sub-items `A、` aren't matched (only bracketed `a）`).
   - `叉车` uses `1.` / `3.1` / `4.1.1` (no spaces) → **0 matches** today.
   - `吸烟` uses `3.1` / `4.1` / `A、` → **0 matches** today.

2. **Heading-consumption** (secondary). Clause headers like `三、职权职责` are extracted as
   `block_type="heading"` and consumed as `current_section` (`chunker.py:1631-1633`), removed
   from `full_text` before `_CLAUSE_RE` runs. Same family as the docx `Subtitle` bug
   (see `docx-subtitle-heading-collapse`), but here it suppresses clause *segmentation*, not content.

## Impact

A downgraded 制度 is chunked as a few large `text_chunk`s instead of per-clause `clause_chunk`s:
- **Retrieval precision**: a query about one rule returns a big multi-clause chunk (worse ranking,
  noisier context). recall is unaffected (content is present) — this is why the GT recall stayed 1.0
  and only `type_accuracy` flagged it.
- **Type-metadata inconsistency**: identical-genre docs split into different chunk_types
  (`clause_chunk` vs `text_chunk`), which downstream layers (L6 chunk-quality, serving labels) treat
  differently.
- Production scale **unquantified** — needs a `prod_ro` pass counting clause-mode docs whose
  `chunk_meta` has only `text_chunk` (no `clause_chunk`). Local sample suggests ~⅓–½ of 制度 docs.

## Proposed fix

**Primary — broaden `_CLAUSE_RE` (`chunker.py:1589`):**
- Drop the trailing `\s` on the decimal alternative: `\d+\.\d+(?:\.\d+)?` (matches `3.1公司办`).
- Add single-level Arabic-dot, decimal-guarded: `\d{1,2}\.(?=\D)` (matches `1.目的`, **not** `2.5kg`).
- Add `、` to the letter sub-item class: `[a-zA-Z][）)、]` (matches `A、`).

**Projected (tested in-memory against the corpus): rescues 6/7 downgraded docs → clause-detectable.**
The 8 already-working docs are **unchanged** in match-count (食堂 90/90, 公务车 32/32, 工作服 20/20,
安全隐患 5/5, 安全教育 10/10, 采购 4/4) — good regression containment. The title-gate means **only
clause-mode docs are affected**; non-制度 text docs (e.g. 消防知识) never enter clause mode.

**Secondary (optional, Phase 2) — heading re-incorporation:** in `_chunk_by_clause`, treat a
`heading` block whose text matches a clause-numbering pattern as a clause boundary (keep its text in
the segment) instead of only as `current_section`. Rescues the last doc (`工作时间及加班规定程序`,
whose markers are all heading-consumed) and de-stales clause section titles.

## Regression risk

- **Finer splitting of already-working clause docs.** Two working docs gain boundaries under the new
  regex: `宿舍管理制度` 9→40, `IT信息系统内控制度` 2→3. Their `clause_chunk` output **changes**
  (finer segmentation). `docx_dorm` (宿舍) is a locked GT PASS case and was byte-stable through L6 —
  this fix will alter it. **Mandatory: A/B the 8 working docs + the full gt_eval cohort; expect
  intended changes on dorm/IT-内控 and confirm they're improvements, not splintering.**
- The single-level `\d{1,2}\.(?=\D)` is the riskiest token (could catch a stray `1.` line); the
  `(?=\D)` decimal-guard + `^`-anchor + clause-mode-only gating bound it. Watch for over-splitting on
  enumerated *prose* (e.g. "1.… 2.… 3.…" inline lists that aren't clauses).

## Verification plan

1. Unit: extend `tests/` clause-chunker cases with the `叉车`/`吸烟` numbering styles + a decimal
   false-positive guard test (`2.5kg` must not split).
2. gt_eval cohort: `docx_smoking`/`docx_forklift` type_accuracy should jump (text→clause); the 8
   working 制度 + `docx_dorm` must not regress recall; re-author/confirm dorm GT if its boundaries
   shift.
3. L6 chunk-quality layer (`l6_chunk_quality`) full-corpus run — confirm no new defects.
4. `prod_ro` dry-run: count affected production 制度 docs (clause-mode, text_chunk-only) → the
   re-chunk work-list.

## Effort & rollout

- Code: **S** (a regex change + 1 fallback tweak + tests) for Phase 1; **M** for Phase 2 (heading
  re-incorporation touches the clause loop).
- Rollout: changes chunk output → a **maintenance re-chunk** of affected production 制度 docs,
  reusing the L6 batch machinery (`RAG_MAINTENANCE_ROUTING` freeze, HA3-frozen manifest, offline
  dry-run, PK scoped purge). Per-batch user authorization, same as the L6 rollout.

## Recommendation

Ship Phase 1 (regex broadening) behind the standard A/B + L6-layer gate; defer Phase 2 unless the
`prod_ro` count shows many heading-consumed 制度 docs. Re-chunk affected docs via the L6 batch
pattern with explicit authorization.

---

## Phase 1 — implemented & verified (2026-06-16)

**Code:** `chunker.py::_chunk_by_clause::_CLAUSE_RE` broadened exactly as specified, plus one
hardening beyond the spec:
- (a) decimal alt drops trailing `\s`: `\d+\.\d+(?:\.\d+)?` → matches `3.1公司办` / `4.2.1检查`.
- (b) decimal-guarded single-level `\d{1,2}\.(?=\D)` → matches `1.目的`, not `2.5kg`.
- (c) letter class gains `、`: `[a-zA-Z][）)、]` → matches `A、`.
- **(extra) unit-guard on the multi-level decimal `…(?![A-Za-z])`.** The spec attributed the
  `2.5kg` guard to the single-level alt only, but the *multi-level* alt (after dropping `\s`)
  matches `2.5` in a line-start `2.5kg`. Adding `(?![A-Za-z])` blocks that. **Empirically
  count-identical to the un-guarded spec across the whole 15-doc corpus** (no `GUARD-DIFF`), so
  it is pure-upside: it satisfies the regression contract AND makes line-start measurements safe.

**Isolation A/B (boundary match-count, replicating `_chunk_by_clause` full_text):** matches the
projection exactly — 食堂 90/90, 公务车 32/32, 工作服 20/20, 安全隐患 5/5, 安全教育 10/10, 采购
4/4 (all unchanged); 宿舍 9→40, IT-内控 2→3; rescued 0→N: 吸烟 41, 叉车 48, 自行车 15, 考勤请休假
3, 注塑机保养规范 3, 更衣室规范 7; `工作时间及加班规定程序` stays 0→0 (heading-consumed = Phase 2).

**gt_eval cohort A/B (30 docs, faithful production routing):**
- Aggregate **recall 1.000 → 1.000** (zero content loss, as predicted), **type_accuracy
  0.836 → 0.886 (+5.0pp)**.
- Targets jumped: **docx_forklift type 0.00 → 1.00** (text→clause, 3→14 chunks);
  **docx_smoking 0.25 → 0.75** (text→clause, 4→12 chunks).
- Regression guard: **docx_dorm byte-identical** (6/6 chunks, all SHA-256 match — the 40 finer
  boundaries merge back to the same 6 chunks; the locked GT case is untouched, **no GT
  re-authoring needed**). **docx_itctrl** 4→4, content re-segmented but *improved* — a new
  `3.1变更管理` boundary replaces a fragment carrying a stale inherited section title
  (`三、IT运维管理流程`); recall 1.0 / type 0.80 both unchanged. **docx_qc** 34→36 (+2 granular
  clause chunks); all 9 image chunks present with identical SHAs (index-shifted only) — **no
  image loss, img_dup_factor 1.0→1.0**.
- The two small aggregate dips (image_acc 0.765→0.736, evidence_hit 0.964→0.941) are localized to
  exactly the 3 intended clause docs and are *metric artifacts of correct finer splitting*
  (density-based representative selection on qc image GTs; ≥50%-keywords-in-one-chunk naturally
  falls when a multi-clause blob is split per-clause). Recall stayed 1.0 throughout.

**L6 chunk-quality layer:**
- Pure unit tests (`tests/test_l6_chunk_quality.py`) pass.
- Offline family pass over all 15 clause-mode docs chunked post-fix (112 chunks): **all
  artifact-level HARD gates GO** — out-of-band tokens 0, oversize-structural 0, token_drift 0,
  malformed image JSON 0, near_dup_cross_factor 1.0, dangling_anaphor 0.0. The lone soft metric
  above target (midsentence_cut) **improved 0.097 → 0.060** vs the old chunker (finer clause
  boundaries cut mid-sentence *less* than the big text blobs). (D7/H gates need production data →
  INCOMPLETE_EVIDENCE offline by design, not a defect.)

**Unit tests added** (`tests/test_chunker.py::TestClauseNumberingStyles`): 叉车 style
(`1.`/`3.1`/`4.1.1`), 吸烟 style (`3.1`/`A、`), single-level `1.`/`2.`, and the `2.5kg`/`3.5cm`
decimal false-positive guard. Full suite: **1055 passed**.

## prod_ro sizing — the re-chunk work-list (2026-06-16, read-only `fuling_ro`)

`scratch/clause_downgrade_prodro_sizing.py` replicates the routing gate against current
`document_meta`, then inspects each clause-mode doc's **active** `chunk_meta` chunk_types:

| bucket | count |
|---|---|
| active docs total | 566 |
| clause-mode-gated (non-faq, non-xlsx/pptx) | 168 |
| ├─ working (has `clause_chunk`) | 125 |
| ├─ **DOWNGRADED (`text_chunk`-only) → work-list** | **39** |
| ├─ other-type-only (table-only — review separately) | 3 |
| └─ no active chunks | 1 |

**39 docs (23% of clause-mode)** by dept: hr 16, admin 8, quality 6, production 5, finance 2,
it 1, marketing 1. Includes the validated cases (A18叉车 `text_chunk=3`, A52吸烟 `text_chunk=4`).
Note: some downgrades predate this regex (older deployed code) — the re-chunk applies the *current*
fixed chunker uniformly to all 39.

**Phase 2 signal:** heading-consumption is rare (1/15 locally — `工作时间及加班规定程序`, also in
the work-list). Recommend **deferring Phase 2**; the per-batch offline dry-run (L6 pattern) will
flag any doc still `text_chunk`-only after the fixed re-chunk as the exact Phase-2 candidate set.

**Rollout status: NOT executed** — this is a production-write op (maintenance re-chunk → embed/push
→ PK scoped purge) requiring per-batch explicit user authorization, via the L6 batch machinery
(`RAG_MAINTENANCE_ROUTING` freeze, HA3-frozen manifest, offline dry-run, PK scoped purge). Lower
urgency than the docx_itctrl Subtitle fix: **not content loss** (recall unaffected), purely
chunk-granularity/type quality.
