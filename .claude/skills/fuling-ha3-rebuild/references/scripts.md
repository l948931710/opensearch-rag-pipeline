# Bundled scripts

All scripts read credentials from `~/Downloads/opensearch-rag-pipeline/.env` + `.env.production`
(no secrets baked in) and are meant to run **from the repo root**: `cd ~/Downloads/opensearch-rag-pipeline`.
Mutating scripts follow **preview → `--commit`**; read-only scripts are safe to run anytime. They were
written for one rebuild — adjust the table/endpoint/doc-id constants if the environment changed.

> If `pymysql` / `oss2` / `alibabacloud_ha3engine_vector` aren't importable locally, the scripts
> pip-install `PyMySQL` on demand; for OSS/HA3 ensure `oss2` and `alibabacloud_ha3engine_vector` are
> available (`make prod`, or `pip install`).

## Read-only inspection
- **`run_preflight.py`** — Phase-1 pre-flight. Active-doc count, `content_process_status`×`index_status`
  distribution, stale locks, active/inactive chunk counts, version distribution, quarantine count,
  pending bulk jobs. Run first, every time.
- **`diag_schema.py`** — dump `document_version` UNIQUE keys (catches `uk_raw_key_hash`), confirm a
  rollback left state intact, inspect a specific doc's versions.
- **`diag_backlog.py`** — characterize the never-processed backlog (file types, folders) before HOLDing.
- **`check_extraction_quality.py`** — text_length buckets across the rebuild docs; lists docs that
  extracted to empty/near-empty (find content gaps after Stage 1).
- **`check_progress.py`** — finer-grained live progress (classification_status, recent `updated_at`).
- **`verify_state.py`** — snapshot: rebuild rows by status, old-version DONE/SUCCESS untouched, etc.
- **`verify_stage2.py`** — after Stage 2: Stage-3 queue size, chunk-type distribution (`step_card`,
  `image`, `text_chunk`, …), `chunk_status` (DONE vs EMPTY), which docs produced 0 chunks.
- **`verify_oss_canonical.py`** — after Stage 1: HEAD every rebuilt doc's `canonical_json_key` in OSS
  (via the **public** OSS endpoint) → present/missing/empty counts + structure spot-check.

## Mutating (preview, then `--commit`) — scoped to `version_no >= 2`, never touches live `v1`
- **`seed_versions.py`** — Phase-2 seed. Inserts `version_no+1` `NOT_STARTED` rows for every currently-live
  doc, **salting `raw_key_hash`** to satisfy `uk_raw_key_hash` (G7), and bumps
  `document_meta.current_version_no`. Excludes `_quarantine`. Preview first, then `--commit`.
- **`hold_backlog.py`** — set legacy/never-processed docs (`.doc`/`.xls`/`.pptx`, no DONE version) to
  `content_process_status='HOLD'` so the queue is exactly the intended rebuild set.
- **`reset_stuck.py`** — reset rebuild rows stuck in `LOADING`/`PROCESSING` (from a crashed/failed stage)
  back to `NOT_STARTED` so a re-run re-claims them.
- **`reset_empty.py`** — reset rebuild docs that produced 0 chunks (`chunk_status='EMPTY'`) to
  `NOT_STARTED` so they reprocess (e.g. after a chunker or PII-policy fix).

## Monitoring (run via the **Monitor** tool so events stream)
- **`monitor_rebuild.py`** — Stage 1: pending → 0 in ~100-step batches, `FAILED`, liveness.
- **`monitor_stage2.py`** — Stage 2: `awaiting_chunk`→0, in-flight batch, `new_chunks` accumulating, `FAILED`.
- **`monitor_stage3.py`** — Stage 3: `s3_pending`→0 (chunks indexing), `old_v1_active` dropping (the swap),
  `dv_SUCCESS`, `FAILED`.
> The hardcoded starting count (e.g. `398`) is cosmetic — update or ignore it; the live numbers are real.

## HA3 query / smoke test
- **`ha3_query_prod.py "<query>"`** — replicates the production **weighted hybrid** (knn 0.7 / text 0.3 +
  BM25 on `chunk_text`) via `SearchRequest`/`client.search()` over the **public HTTP/80** endpoint. Also
  supports a `doc_id` filter (definitive "is this content indexed + masked?" check). Note: does **not**
  include neighbor-stitching/rerank — for true relevance use the bot (G16).
- **`ha3_query_node.py`** — same query as a **PyODPS3 node** to paste into DataStudio and run **in-VPC**
  (when the public endpoint isn't available). Fill the two cred placeholders from the `清理stage3` node.

## Validation tests for code fixes (offline, no prod) — `make sim` won't catch the prod (dict) path
- **`test_chunk_dict_blocks.py`** — runs each chunk mode with **dict** blocks (not objects) and asserts
  chunks are produced. Reproduces/guards G13.
- **`test_pii_policy.py`** — runs `node_detect_sensitive` + `node_redact_or_quarantine` on synthetic docs;
  asserts phone/email → `REDACTED`+masked, id-card → `QUARANTINE`. Validates G14.
- **`repro_faq_empty.py`** — downloads a doc's canonical from OSS and runs each chunk mode locally to
  diagnose why it produced 0 chunks (used to isolate the chunker bug vs the quarantine cause).

## Retrieval / ANN health & index inspection (Phase 4 — catches G18/G19)
These run the **real serving code** over the public HA3 endpoint (env forced to `RAG_ENVIRONMENT=test` so
DashScope uses the public domain, not the VPC one; HA3 `protocol=HTTP`). Adjust the `TARGET` doc-id /
`QUERY` constants for the doc you're checking.
- **`bot_query_test.py "<query>"`** — the **faithful `/api/ask` replication**: runs production
  `retrieve_and_enrich` (3-way hybrid + neighbor-stitch + step-card expansion + cover-page demotion) →
  `generate_answer`. Looks up the doc's `permission_level`/`owner_dept` first so it queries as a real
  employee would. This is the definitive "what would the bot answer?" test (G16). Prints ranked chunks +
  the LLM answer + cited sources.
- **`diag_ann_selfquery.py`** — the **HNSW health check**: embeds several chunks by their *own text* and
  does a dense-only kNN. Healthy index → every self-query at **rank 1 @ ~1.0**. Mediocre top-1 (~0.3–0.5)
  or self-misses ⇒ **stale graph, run a full build** (G18). The single fastest "is dense broken?" probe.
- **`diag_ha3_index_def.py`** — reads the table definition via `cli.get_table` (vector index type,
  `distanceType`, HNSW build/search params, **`autoBuildIndex`**), `stats` (docCount per partition — vs
  RDS active count, G19), `list_table_generations` (build history), and sweeps `proxima.hnsw.searcher.ef`
  to prove the failure is graph-staleness, not under-recall.
- **`diag_blast_radius.py`** — vector-integrity audit: fetches a sample of chunks' **stored** vectors
  (`include_vector=True`) and compares each to a **fresh** embedding of its text via cosine. `~1.0` =
  stored vector matches text (rules out a push/embedding-alignment bug); confirms the problem is the ANN
  graph, not corrupted vectors.

## Recovery — after an accidental index wipe (G20 runbook)
If a `reindex` emptied the index (Swift-fed table, expired retention — G20), repopulate from RDS+cache;
no data loss, no re-extraction.
- **`reset_index_for_repush.py`** (preview → `--commit`) — flips active chunks `index_status`
  `INDEXED`→`NOT_INDEXED` so Stage 3 re-pushes them (RDS still says INDEXED while HA3 is empty — split-brain).
  Non-destructive (status flip on `is_active=1` only); also clears any version stuck `PROCESSING`. Then run
  the **`清理stage3`** node (`run_stage_drained` → one run re-embeds cache-first + re-pushes all active chunks).
- **`poll_repush.py`** — monitors the re-push: RDS active `INDEXED` climbing 0→N (+ pending/failed) and HA3
  `docCount` + a live BM25 hit count. Run via the **Monitor** tool / a background `until` loop; signals DONE
  when pending==0 and serving returns. (The re-push restores BM25 + a clean index; pure dense still needs the
  G21 offline-build fix — see `docs/ha3-dense-fix-plan.md`.)
