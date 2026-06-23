# Pipeline robustness & content-hashing — design note (2026-06-22)

Distilled from the large dept-internal ingestion campaign (production specs, rd, marketing,
the 96-spec cohort, the redaction derivatives, and the 85-doc re-adjudication). Each item is
tied to a **failure mode we actually hit**, not a hypothetical.

## TL;DR on the three hashes

| hash | status today | verdict |
|---|---|---|
| **document_content_hash** | **already exists** = `document_version.canonical_sha256` (migration `003`, sha256 of canonical text). Computed every Stage-1; **~62% backfilled**; skip-gate `RAG_SKIP_UNCHANGED_REINGEST` is **default-OFF** and **scoped to same-doc prior-versions only**. | **Keep & leverage harder — do not re-add.** |
| **chunk_content_hash** | **does not exist** (no hash column on `chunk_meta`). | **ADD — highest-value new hash.** |
| **section_content_hash** | does not exist. | **Skip / defer** (no incremental-section-reindex use case). |

`document_version` already has `raw_key_hash` (OSS-path uniqueness token, ≠ content) and
`checksum_sha256` (raw bytes). The **content** key is `canonical_sha256` (normalized text) —
that is the correct design (raw-byte hashes are useless for our dedup: docx re-saves and
docx-vs-pdf twins are never byte-identical).

## Why each verdict (evidence)

### document_content_hash (`canonical_sha256`) — exists, under-used
It did **not** save us from the dup-of-public trap (we nearly ingested 8 docs whose content
already serves publicly, caught only by a brittle title heuristic) because the skip-gate
compares a doc to **its own prior versions**, not **across documents**. Fixes:
1. **Backfill** the ~38% NULL so the gate is universal.
2. **Cross-document dedup at register**: index `canonical_sha256`; before copy+register,
   check "does this content already serve under *any* doc/dept?" → deterministically catches
   dup-of-public, docx/pdf twins, cross-dept copies. Replaces the 3 layers of stem/jaccard/
   title heuristics we needed this campaign.
3. **Turn the skip-gate ON by default** → idempotent re-ingest: unchanged content ⇒ no
   re-chunk/re-embed ⇒ **eliminates the LLM-category re-roll** that flips chunk families
   (the 79-vs-47 incident) *and* saves embedding cost. Gate is already fail-safe (skip only on
   a positive match; any miss/NULL/error ⇒ process normally).

### chunk_content_hash — genuinely missing, add it
Buys two things we lacked:
- **Content-level HA3 parity, not just presence.** Our parity only asked "is the PK present?";
  it cannot detect a PK that is present but holds **stale content** (drift). Store the hash on
  the HA3 doc and compare on verify to close that gap.
- **Embed-skip on re-ingest** — only re-embed chunks whose hash changed (embeddings are the
  cost).
- ⚠️ Caveat: the chunker is non-deterministic in one input (LLM category → chunk family), so
  chunk hashes shift on re-chunk unless routing is frozen (`RAG_MAINTENANCE_ROUTING`). So it is
  reliable for **parity/drift** always, but for **idempotency** only under frozen routing.
  `canonical_sha256` (pre-chunk) is the stable idempotency key; chunk_content_hash is the
  downstream parity/cost key.

### section_content_hash — skip
Only pays off with **incremental section-level re-indexing**, which we do not do (we
full-replace). Defining stable section boundaries adds complexity now for a use case we don't
have. Revisit only if incremental updates become real.

## Robustness wins (hashes are enablers; the main fixes are process)

Ranked by the pain actually felt:

1. **Post-push HA3 parity as a built-in Stage-3 step.** The #1 recurring failure: **~1% silent
   HA3 push-drops** — Stage 3 reports `indexed=N, failed=0` but the chunk is absent from HA3
   (`ignore_invalid_doc=true` hides the cause). Found 27, then 16, then 3 across cohorts, all
   by hand. Bake in: after push → enumerate HA3 vs RDS-active by id-range → bounded-retry the
   drops → fail loud if unresolved. With `chunk_content_hash`, verify content match, not just
   presence.

2. **Eventual-consistency-aware verify (settle + double-pass).** HA3 realtime index lags, so a
   single post-push parity **over-counts** missing (freshly-pushed not yet queryable). Rule:
   settle ~120 s and require missing-in-**both** passes before acting. Corollary: "0 missing"
   is always trustworthy; ">0" needs the double-pass.

3. **Cross-doc content-hash dedup at register** (`canonical_sha256`) — the dup-of-public /
   cross-dept / docx-pdf-twin trap. Deterministic; replaces heuristics.

4. **Idempotent re-ingest** (enable the `canonical_sha256` skip-gate) — avoids category re-roll
   + saves cost.

5. **First-ingest chunk-explosion gate.** A marketing data-table xlsx blew up to **22,662**
   near-useless zip-code/price chunks; killed by hand before embed. Add a per-doc Stage-2
   bound: warn/quarantine if a doc yields > N chunks or a degenerate type-mix (e.g. ~all
   single-cell table_chunks). The count/type gate exists for *re-chunks* — extend to first
   ingest.

6. **Image-OCR PII in the gate by default.** Base-text PII detection repeatedly missed PII in
   **screenshots / embedded images** (CE38C5, test-report contact phones, an intro `.pptx`).
   Fold the chunk-level gate into Stage 2 as the authoritative gate, scanning
   `chunk_text + image_refs.ocr_text` (exclude VLM `visual_summary` to avoid UI-label FPs).
   Material codes (130****000X near `物料编码`) and numeric ratio tables are known FPs → allow.

7. **Formalize resume-from-RDS.** An orphaned chained run crashed *after* Stage-1 succeeded;
   recovery required manual RDS reconciliation to find completed scope. The RDS state machine
   (`content_process_status` / `canonical_json_key` / `index_status`) already makes this
   possible — wrap it: reconcile completed scope, re-enter only safely-reentrant docs, never a
   full re-run; verify no dup version/chunk/HA3 PK first. (`pipeline_run` from `003` is the
   per-run lineage header — populate it so every chunk traces to a run/commit/model.)

8. **VLM cross-doc cache as first-class infra.** The warm `scratch/vlm_cache.json` turned a
   "days" image-spec crawl into minutes. Treat the cache (already OSS-backed) as
   infrastructure; perceptual-hash-dedup loose/embedded images before VLM so identical images
   aren't re-paid.

## Concrete next steps (proposed, not yet done)

- **Migration `004`**: `ALTER TABLE chunk_meta ADD COLUMN content_sha256 VARCHAR(64) NULL`
  (sha256 of normalized `chunk_text`); index it. Backfill opportunistically on next re-chunk.
- **Stage-3 verify step**: id-range HA3 enumeration vs `chunk_meta(is_active=1)` + bounded
  re-push of stable-missing (settle + double-pass), content-hash-aware once `content_sha256`
  exists.
- **Register-time dedup**: unique/index on `canonical_sha256`; "already-serving content" check.
- **Default-on** `RAG_SKIP_UNCHANGED_REINGEST` + backfill `canonical_sha256`.
- **Stage-2 chunk-explosion guard** for first ingest.

Operational tooling proven this campaign lives under `scratch/acl_rollout/`,
`scratch/readjudicate/`, `scratch/marketing_image_discovery/` (chained driver, parent-search,
re-adjudication workflow, value-discovery) — reusable patterns, not production code.
