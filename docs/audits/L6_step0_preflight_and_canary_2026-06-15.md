# L6 prod re-chunk — Step 0 pre-flight (read-only) + canary plan + supplements

Read-only Step 0 executed 2026-06-15. **No production writes.** Canary execution awaits separate authorization.

## Step 0 pre-flight — ALL GREEN

| check | result |
|---|---|
| **Affected manifest** | **277 docs** (not 280 — see consistency). Full CSV: `docs/audits/L6_prod_rechunk_manifest_277.csv`. Depts: hr 155, admin 27, quality 22, rd 21, production 17, marketing 15, finance 10, it 6, supply 3, pmc 1. Fix mix: Fix A only 6 / Fix B only 154 / A∩B 117. All permission=public. |
| **Preview set consistency** | re-derived (current committed chunker) = **277**; saved manifest was 280 (pre-`ae441f1` length-cap). only-in-derived 0. **3 docs dropped** (`DOC_HR_…028CB9`, `DOC_RD_…1697E4`, `DOC_RD_…8F6BB9`) — their only change was a long-heading own-heading replacement now suppressed by the ≤60 cap → no-change under the current chunker. `affected_docs.json` re-saved = 277 (authoritative). |
| **Current version state** | 277/277: `content_process_status=DONE`, `chunk_status=DONE`, `status=active`, version = `current_version_no`. **0 stale PROCESSING/LOADING locks.** |
| **Canonical integrity (RDS)** | `canonical_json_key` present on 277/277 (0 missing). |
| **Canonical integrity (OSS HEAD)** | **277/277 exist & non-empty** (bucket `fuling-knowledge-base`; sizes 2.5KB–497KB, median 17KB; 0 missing, 0 empty). |
| **DataWorks scheduler** | project `default_workspace_6na2` (id 609583) Available; **scheduled workflows = 0** → no periodic instances can race the laptop run. (Stage nodes are manual, per the rebuild runbook.) |

## Canary (10 docs) — for the next authorization step
Covers Fix-A-only / Fix-B-only / A∩B, 9 depts, 6 long-section (maxSecLen≥50), incl. both smoking-gun docs. List: `scratch/l6_ab/canary_docs.json`.

| scenario | doc | dept | A / B | maxSec |
|---|---|---|---|---|
| A∩B (smoking-gun) | DOC_HR_…120631_D097E9 员工手册 | hr | 144 / 134 | 60 |
| A∩B (smoking-gun) | DOC_MARKETING_…A9D6B9 进出口规范 | marketing | 71 / 61 | 55 |
| FixB_only | DOC_FINANCE_…C64FBD 出口退税备案 | finance | 0 / 2 | 13 |
| FixB_only | DOC_QUALITY_…435278 清洁消毒培训 | quality | 0 / 2 | 18 |
| FixA_only | DOC_HR_…78AB52 厂证/访客证 | hr | 7 / 0 | 0 |
| A∩B | DOC_ADMIN_…233653 外来人员留宿 | admin | 6 / 6 | 60 |
| A∩B | DOC_PRODUCTION_…869857 仓库管理 | production | 2 / 2 | 54 |
| A∩B | DOC_RD_…8FF018 研发组织管理 | rd | 27 / 19 | 57 |
| A∩B | DOC_IT_…BBDF6E 信息发送平台 | it | 4 / 4 | 58 |
| A∩B | DOC_SUPPLY_…236EDE 领付款审批 | supply | 3 / 4 | 36 |

Canary runs the full path: reset → stage 2 → stage 3 → 6-dim verify → reconcile purge → post-E2E.

---

> **Adversarial review applied (2026-06-15, 4 independent verifiers vs the actual code).** Net: the
> 277 set is independently confirmed **authoritative** (all fix code paths covered), the reconcile delete
> predicate is **exact**, and the rollback determinism claim **survives**. The verifiers also found 5
> real defects in the recipe — all corrected below. (Verdicts: all 4 `sound_with_caveats`.)

## Supplement 1 — Executable rollback (incl. post-purge)
The fix is **deterministic text transforms, no boundary change** (boundaries are decided on `raw_body`
*before* the `章节:`/`[上文]` prefix is applied, verified at `chunker.py` _chunk_by_clause), and **canonical
is unchanged** → re-chunking with the old chunker reproduces the old chunks.

- **What "restore" means (precisely):** the revert restores **byte-identical `chunk_id`, `chunk_text`,
  `section_title`, `parent_chunk_id`, `image_refs`** — i.e., everything serving/retrieval keys on (retriever
  keys on the *string* chunk_id/parent_chunk_id, not the int PK). The integer `chunk_meta.id` (= HA3 PK) is
  **re-minted** by `node_write_chunk_meta`'s DELETE→INSERT, so the restored row is a new PK — invisible to
  serving. ⚠️ Byte-identity also depends on the **`prepend_*` flags** (`prepend_section`/`prepend_title`/
  `max_context_*`) being unchanged — the `ffb0150`/`ae441f1` revert touches none of them, so it holds; note
  it explicitly because `prepend_section=True` pulls `section_title` into `chunk_text`.
- **Before purge (steps 1–4):** trivial — old chunks still `is_active=1`, serving. Skip purge or reset.
- **Pre-purge snapshot:** before step 5, export the canary docs' OLD `chunk_meta` rows + their HA3 PKs to
  `scratch/l6_ab/canary_rollback_<ts>.json` (the exact delete set).
- **After purge, if the fix is bad — deterministic full restore:**
  ```bash
  git revert --no-edit ae441f1 ffb0150     # ⚠️ REVERSE-chronological order (ffb0150-first CONFLICTS — verified)
  # reset canary docs: content_process_status='NOT_STARTED' AND index_status='NOT_INDEXED', KEEP canonical (preview→commit)
  # stage 2 + stage 3 (re-chunk with reverted chunker → reproduces old chunk_text/section)
  # PRE-RECONCILE GATE (mirror forward dims 1-2): per-doc HA3 count(restored) == chunk_meta active count;
  #   AND chunk_meta active-row count for the canary set == expected (catches a corrupted is_active BEFORE the irreversible purge)
  python -m opensearch_pipeline.ha3_reconcile --commit
  ```
- **Reset must clear `index_status` too** (`NOT_INDEXED`) — `reset_stuck.py` resets only
  `content_process_status`; do not lean on stage-3's SUCCESS-relock fallback (it exists for partial-batch
  resumption, not rollback).
- **Over-delete protection** is by construction (`_classify_stale` asserts `delete_pks ∩ rds_active_ids == ∅`)
  **but only as strong as `chunk_meta.is_active` truth** — the very column the 2026-06-15 WHERE-less-UPDATE
  incident corrupted. Hence the **pre-reconcile sanity gate above is mandatory for the rollback purge**
  (the forward path already has its 6-dim gate; the rollback purge must not be less guarded).

## Supplement 2 — Build ID / commit / audit-record + provenance gate
- **Provenance gate (HARD pre-run ABORT):** laptop mode runs the **working tree**, so record + assert at run
  start: `git rev-parse HEAD` + a **tree-hash of `opensearch_pipeline/chunker.py`** (not just HEAD) + a hash
  of the 277-set. ⚠️ **The tree is currently NOT clean** (`M docs/architecture.md`, pre-existing/unrelated to
  this work) — **must be committed or stashed before any prod write** so provenance is unambiguous.
- **Audit record (per canary step)** → `docs/audits/L6_canary_run_<ts>.json`:
  `{step, timestamp, git_head, chunker_tree_hash, set_hash, doc_ids, preview_assertion, rows_affected,
  embed_model + endpoint_mode(native/compat) + embedding_cache_disposition, per_doc_canonical_md5,
  verify_results(6-dim incl. sparse coverage), reconcile{checked,stale,deleted,skipped},
  before_after{old_chunk_count, new_chunk_count, old_PKs, new_PKs, deleted_PKs}, operator_host, prod_rw_ack}`.
  Markdown summary `L6_canary_run_<ts>.md` committed for the trail.

## Supplement 3 — Reconcile's exact old-chunk delete condition (`ha3_reconcile._classify_stale`) — verified exact
Truth = `chunk_meta.id WHERE is_active=1` (read fresh, line 113). A HA3 PK is **deleted iff**:
1. `pk ∉ rds_active_ids` (not a current active `chunk_meta.id`), **AND**
2. NOT `dup_replacement_absent` — its `chunk_id`'s current active id is **already in HA3** (G3: never delete
   the old carrier before its replacement lands).

Delete **by HA3 PK (INT `id`)**, never by `chunk_id`. Hard invariant `assert delete_pks ∩ rds_active_ids == ∅`
(raises). Env-gated, idempotent (`not_found`/`no_op` = success). **For the canary:** stage-2 DELETE→INSERT
deactivates the old `chunk_meta.id`, new id active+pushed → old PK stale → reconcile deletes exactly the old
PKs of the 10 docs. **Over-delete is impossible (G1); the residual risk is UNDER-delete** (a stale PK survives):
- ⚠️ **Enumeration corrected:** the `id>=s AND id<s+500` bucket scan eliminates **top-k truncation** (window
  500 < top_k 600) but **NOT HA3 segment-visibility non-determinism (G30 names it separately)** — a single
  pass can miss a PK. Under-delete is **benign + self-healing** (reconcile is idempotent + wired into
  `spot_checker`). **For the canary specifically, enumerate per-`doc_id` (`filter='doc_id="X"'` over the 10
  docs) — the doc set is RDS-authoritative — which sidesteps G30 entirely**, plus a point-id (`filter='id=X'`)
  confirm after `--commit`. (No `_enumerate_ha3_pks` completeness test exists yet — the per-doc path avoids
  relying on it.)

## Supplement 4 — Sparse non-empty + coverage verification (corrected — do NOT rely on L0-G3 as-is)
Staging proved a dense-only embed cache silently drops sparse (cache stores dense under `ck`, sparse under a
separate `sp_ck`; a `ck`-hit skips re-embed → no sparse). The cited **L0-G3 does NOT prove doc-side sparse** —
it re-embeds *query-side*, samples 40 chunks *corpus-wide* (not the 10 canary docs), and `if not si: continue`
silently drops empty-sparse chunks from the denominator. Corrected gate:
1. **Real doc-side coverage check (the decisive one):** for the 10 canary docs, query HA3 (output_fields incl.
   `sparse_vector_indices`/`sparse_vector_values`, or PK-range scan the canary PKs) and **assert every active
   canary chunk has a non-empty stored sparse field** — counting the `[0]/[0.001]` `sparse_fallback` as
   present-by-design (short/low-content texts legitimately get it). **Require 100% on the canary** (10 docs is
   small); reserve the ≥98% gate for corpus-wide runs.
2. **Scope the self-query to the canary** chunk set (not `sample_active_chunks` corpus-wide); a chunk with
   empty sparse counts as **FAIL/logged**, not silently dropped.
3. **`rm -f scratch/embedding_cache.json` immediately before the canary stage 3** + log the cache-hit count
   (`pipeline_nodes.py:4577`) — belt-and-suspenders so every canary chunk re-embeds fresh native dense+sparse.
4. If sparse coverage < 100% on the canary → **STOP, do not purge** (old chunks still serve), investigate.

---

**Step 0 = GREEN. The 4 supplements are adversarially reviewed and corrected.** One prerequisite before any
prod write: **clean the working tree** (`M docs/architecture.md`). Awaiting authorization to run the 10-doc
canary (reset incl. `index_status` → stage 2/3 with fresh cache → 6-dim verify incl. real doc-side sparse
coverage → per-doc reconcile purge → post-E2E), each prod write under its own same-day `PROD-RW` token.
