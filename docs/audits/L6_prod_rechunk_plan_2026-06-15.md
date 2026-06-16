# Production re-chunk plan — L6 chunker fix (FOR APPROVAL)

Apply the validated L6 chunker fix (Fix A drop `[上文]` + Fix B section_title de-staling,
commits `ffb0150` + `ae441f1`) to production, scoped to the **280 affected docs only**. Nothing
here executes without your **per-step, same-day** authorization. The fix is committed, unit-tested
(1032 green), offline-accepted (GO), and staging-validated (recall up vs prod, 0 regressions).

## Scope (read-only confirmed)
- **280 docs** (Fix A 123 ∪ Fix B 277); `scratch/l6_ab/affected_docs.json`.
- **1702 chunks** change text/section (of 3199 in those docs); the other 282 docs + all
  step_card/table/image chunks are **byte-equal → untouched**.
- **Chunker-only fix → canonical UNCHANGED.** Re-chunk reuses existing canonical (no re-extract,
  no VLM) → **stage 2→3 only**, not stage 1.
- **Same chunk boundaries** (fix is intra-chunk text) → same chunk_id set, same count, parent/step/
  image_refs/chunk_index preserved.

## Mode: laptop push-then-purge, same-version re-chunk (skill `references/laptop-reindex.md`)
Runs the orchestrator from the laptop reading the **local working tree** (the fix is live, no zip
deploy). Public endpoints via shell-override (`DASHSCOPE_VPC_DOMAIN`=public inside
`RAG_ALLOW_SHELL_OVERRIDE` — this is what gives **native dense+sparse**, so prod hybrid stays
intact, unlike the staging `_s` issue). Old chunks are the safety net until the final purge.

## Steps (each prod write = its own same-day `PROD-RW:<date>` authorization)
0. **Pre-flight (read-only, prod_ro)** — confirm the 280 docs' current-version state, canonical
   present, no stale locks. `scripts/run_preflight.py` + the affected-doc list.
1. **Reset** the 280 docs' **current version** → `content_process_status='NOT_STARTED'`,
   **KEEP canonical** (extraction was fine; only chunking changed). Scope strictly to
   `version_no = current_version_no` (G36) — never touch other versions. Preview→commit.
2. **Stage 2** (re-chunk, drained) — classify+PII+publish+chunk+write_meta for exactly the 280
   reset docs, with the fixed chunker. Monitor `scripts/monitor_stage2.py`.
3. **Stage 3** (`run_stage_drained`) — embed (native dense+sparse) + push to prod HA3; new chunks
   coexist with old. Monitor `scripts/monitor_stage3.py`. `FAILED` must stay 0.
4. **6-dim verify gate (read-only, prod_ro) — earn the purge:**
   1. RDS chunk_meta: 280-doc new chunks all `INDEXED`, `opensearch_doc_id`/`indexed_at` set.
   2. HA3 new chunks: per-doc count == chunk_meta; **`[上文]` residual 0**, section_title ≤255.
   3. HA3 old safety net still `is_active=1` (push didn't disturb it).
   4. Dense self-query: new chunks self-retrieve rank-1 (G29 order=DESC).
   5. End-to-end: real per-dept questions surface the new chunks under the production rerank path.
   6. Stage-3 deactivate fired **0** deletes (same-version re-chunk → no old-version rows; old PKs
      linger for step 6).
5. **Reconcile dup PKs** — `python -m opensearch_pipeline.ha3_reconcile --commit` (same-version
   re-chunk gives each chunk_id a new `chunk_meta.id`/PK; deletes the old HA3 PKs not in active
   `chunk_meta.id`, with the never-delete-active / replacement-present guards). This IS the purge
   for the old chunks of the 280 docs.
6. **Post-verify** — RDS↔HA3 parity for the 280 docs == 1.0; D1–D7 orphans/parent 0; a final
   end-to-end spot-check.

## Safety
- **Per-step authorization**: every write previews (asserts the exact 280-doc target set) → you
  approve → commit. The auto-classifier blocks any self-discovered mass delete (correct).
- **Rollback**: if any stage or the verify gate fails, the orchestrator raises and stops — old
  chunks stay active, nothing purged, production serves the (pre-fix but working) index. Reset +
  investigate.
- **DataWorks 周期实例 paused/off** during the laptop run (avoid row-claim races).
- **No version bump** needed (canonical unchanged); if you prefer the zero-downtime versioned swap
  instead, that's the DataWorks path (heavier: re-extract + seed v+1) — not recommended here since
  the fix is chunker-only.
- Post-run: rotate any credentials touched; the fix is already in the working tree (no zip deploy).

## Why this is safe to run now
- Fix validated: offline dense+sparse A/B recall@1 0.988→0.992 / 0 regressions; live staging
  (dense+BM25) matches/beats prod on every L1 metric; structural all-green; unaffected types
  byte-equal; +2 latent bugs caught & fixed (section overflow, embed-cache).
- Blast radius bounded to 280 docs; 282 docs + non-clause/text chunks provably untouched.

**Awaiting your go to start at Step 0 (read-only pre-flight).**
