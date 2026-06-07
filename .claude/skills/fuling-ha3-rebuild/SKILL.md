---
name: fuling-ha3-rebuild
description: >-
  MANUAL runbook — invoke explicitly via the `/fuling-ha3-rebuild` command; do NOT auto-trigger.
  Rebuilds the Fuling Plastics HA3 / OpenSearch 向量检索版 vector index through Alibaba DataWorks
  (ingestion DAG 1→2→3: canonicalize → safe-chunk → push-index): connect the DataWorks MCP →
  read-only pre-flight → version-bump seed → deploy code → run the stages → verify, plus a
  gotchas catalog and reusable helper scripts. This is a HIGH-STAKES, in-VPC PRODUCTION operation,
  so run it ONLY when the user explicitly invokes the command or clearly asks to perform the Fuling
  HA3 rebuild. Do not fire on incidental mentions like "answers seem stale", "re-run the pipeline",
  or "reindex" — treat those as conversation unless the user explicitly asks to execute this rebuild.
---

# Fuling HA3 Index Rebuild (DataWorks DAG 1→2→3)

A production runbook for rebuilding the Fuling Plastics RAG vector index (Alibaba **HA3 /
OpenSearch 向量检索版**) by re-running the ingestion pipeline through **DataWorks**. Built from a
real end-to-end rebuild — it captures the safe sequence **and** the traps that aren't obvious until
they bite you.

> **Manual only.** This rebuilds a live production index. Run it only when the user explicitly asks
> (or invokes `/fuling-ha3-rebuild`). Confirm intent before any mutating step.

The repo is `~/Downloads/opensearch-rag-pipeline` (Python). The pipeline:
`raw OSS docs → (DAG1) canonical → (DAG2) classify+redact+chunk → (DAG3) embed+push to HA3 + swap`.

> **Read `references/environment.md` first** — concrete IDs (DataWorks project, resource group, node
> names, HA3/RDS/OSS endpoints, credential locations). **Read `references/gotchas.md`** — the catalog
> of failure modes + fixes (esp. **G29**: dense kNN needs `order="DESC"` for InnerProduct).
> **Read `references/vector-index-config.md`** — authoritative vector-index field reference + the
> distance-type↔sort-order rule + the live table's exact config. `references/scripts.md` documents every helper.

## Golden rules (internalize these)

1. **Execution is in-VPC; your laptop is not.** OSS-internal and the HA3 intranet endpoint resolve
   to `100.64/10` VPC addresses — unreachable from the laptop. **RDS is reachable** from the laptop
   (read+write) so the helper scripts run locally; the actual DAG stages run **only inside DataWorks**.
2. **Read-only first, then preview-then-commit.** Every mutating script does a `SELECT`/preview, then
   the write inside a transaction, then a verification count. Never blind-write production.
3. **Scope every write to the rebuild set (`version_no >= 2`).** Never touch live `v1` rows — that's
   what keeps search serving with zero downtime during the swap.
4. **The DAG-3 safety invariant is sacrosanct:** new chunks must be indexed *before* old ones are
   deactivated. `node_update_index_status` raises on any push failure precisely to protect this.
   Never reorder DAG-3 nodes or move `node_deactivate_old_chunks` earlier.
5. **Validate code changes offline before deploying.** `make sim` exercises the *object* path; the
   production bug class is the *dict* path (canonical loaded from OSS as JSON). Add a dict-block test.
6. **Credentials leak during this work** (into chat, `.mcp.json`, node scripts). Recommend rotating
   the RDS/HA3/OSS/DashScope keys when done, and removing any whitelisted laptop IP from HA3.

## Phase 0 — Connect the DataWorks control path

The stages run as DataWorks nodes; to drive/trigger them you need the **DataWorks MCP**
(`alibabacloud-dataworks-mcp-server`, via `npx`). The user must supply a RAM AccessKey and reconnect:

- `.mcp.json` at repo root with `command: npx`, `args: ["-y","alibabacloud-dataworks-mcp-server"]`,
  env `REGION=cn-hangzhou`, `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET` (from the launching shell, not a file).
- **Restart Claude Code** — MCP servers load only at startup. You cannot connect one mid-session.
- Verify with `ListProjects` (find `default_workspace_6na2`, id `609583`). A `403` means the key lacks
  DataWorks perms.

Do **not** write `.mcp.json` with literal secrets yourself — the safety guard blocks it (it auto-runs
npx with creds). Hand the user the config; they fill creds + restart.

## Phase 1 — Read-only pre-flight (always)

Run `scripts/run_preflight.py` (reads RDS creds from `.env`/`.env.production`). It reports: active-doc
count, `content_process_status`×`index_status` distribution, **stale locks** (`PROCESSING`/`LOADING`),
active/inactive chunk counts, version distribution, quarantine count, pending bulk jobs. This answers
"what needs cleaning to rerun" and surfaces inconsistencies before any write.

Also reconcile the **RDS host**: `.env.production` may list the public endpoint (`…093o`, reachable)
while the nodes use the intranet one (`…09`, `100.x`). Confirm they're the **same instance** (public
+ VPC endpoints) before trusting laptop reads/writes — see `references/gotchas.md` (G1).

## Phase 2 — Decide scope & seed the rebuild

Pick what to reprocess (ask the user):
- **Re-extract from raw (1→2→3)** — extraction/chunking logic changed. Version-bump every currently-live
  doc and re-run all three stages.
- **Re-chunk (2→3)** / **re-index (3)** — narrower; reset the corresponding state instead of bumping.

For the full re-extract with a **zero-downtime versioned swap**, seed new `version_no+1` rows:
- Run `scripts/seed_versions.py` (preview) → review the candidate set → `--commit`.
- ⚠️ **`uk_raw_key_hash` is UNIQUE in prod.** A new version reusing the same `raw_key` collides, so the
  seed sets `raw_key_hash = SHA2(raw_key || '#v' || new_ver)` while keeping `raw_key` = the real OSS
  path (extraction uses `raw_key`; the hash is only a uniqueness token). This is required — same-version
  in-place re-extraction orphans old chunks (G7/G8).
- **Exclude legacy/backlog**: `scripts/hold_backlog.py` sets `.doc`/`.xls`/`.pptx` and never-processed
  docs to `content_process_status='HOLD'` so the queue is exactly the docs you intend (G10).

## Phase 2.5 — Deploy current code (if logic changed)

The DataWorks nodes download a code archive (`opensearch_pipeline_production.zip`, resource id
`8937691206218419696`). **The deployed zip is what runs — not your working tree.** If you changed
processing logic, rebuild and re-upload:

```bash
cd ~/Downloads/opensearch-rag-pipeline
rm -rf /tmp/dw && mkdir -p /tmp/dw
zip -rq /tmp/dw/opensearch_pipeline_production.zip opensearch_pipeline -x '*__pycache__*' '*.pyc'
cp /tmp/dw/opensearch_pipeline_production.zip ~/Downloads/   # easy to find in the upload dialog
```

Then the **user** replaces the resource in DataStudio (Resources → `opensearch_pipeline_production.zip`
→ re-upload → Save → Submit/Deploy). **Do not** try the OpenAPI `UpdateResource` presigned-URL path —
the safety guard blocks it (public-URL exfiltration). The console upload is the way (G6).

## Phase 3 — Run the stages (in-VPC, in order)

The three stage nodes are **manual DataStudio nodes** (not scheduled tasks — `ListTasks`/`ListWorkflows`
are empty for them), so they're run from the **DataStudio IDE**, not the operations-center API (G4).
Run in strict order, each to completion:

1. `opensearch_stage1_canonicalize1` — extract/OCR/VLM → canonical.
2. `opensearch_stage2_safe_chunk` — classify → detect PII → redact → publish → chunk.
3. **`清理stage3`** — embed → push to HA3 → deactivate old. **Use this one, NOT
   `opensearch_stage3_push_index`** (that node references a non-existent `opensearch_pipeline.zip` and
   fails — G5).

**Throughput**: the deployed orchestrator processes one batch per run (`LIMIT 100` docs / `1000` chunks).
To drain the whole corpus in **one run per stage**, edit each node's last line `run_stage(stage=N, …)`
→ `run_stage_drained(stage=N, …)` **and fix the import** (`from … import run_stage_drained`). The
parallel-embedding speedup (`RAG_EMBED_CONCURRENCY`, default 5; the `time.sleep(1)` removed) is in the
code and applies automatically. (`RAG_MAX_CONCURRENT_TASKS` / `RAG_SCAN_BATCH_SIZE` are **dead config**;
`--bizdate` does **not** scope a run — G12.)

**Monitor from the laptop** (RDS is reachable): `scripts/monitor_rebuild.py` (Stage 1),
`monitor_stage2.py` (Stage 2), `monitor_stage3.py` (Stage 3). Launch each with the **Monitor** tool so
events stream as the queue drains. Watch `FAILED` — it should stay 0. Counts move in **~100-doc steps**
(per-batch commit), so a flat count between commits is normal, not a stall (G11). A transient DashScope
timeout self-heals via the drain-loop retry (G15).

If a stage **fails**, the orchestrator raises and stops cleanly (no chunks written, nothing deactivated
— live docs untouched). Diagnose, fix, then:
- Reset stuck `PROCESSING`/`LOADING` rows: `scripts/reset_stuck.py`.
- Reset docs that produced 0 chunks for reprocessing: `scripts/reset_empty.py`.

## Phase 3.5 — Dense/vector index: a KNOWN trap — do NOT `reindex` this table  ⚠️

It is tempting to "finish" a rebuild by triggering a full vector-index build. **On this table that is a
production-outage trap.** The data source is `dataSource={type:swift, autoBuildIndex:false}` with **no
persistent offline full-data source** (verify: `scripts/diag_ha3_index_def.py` → `get_table`). A
`reindex(data_time_sec=…)` replays from the **expiring Swift realtime log**; once retention has passed it
builds an **empty** generation and swaps it into serving → **search goes down** (this happened — G20).

Reality (G21): dense/ANN has likely **never been built** here — realtime push fills the forward field + the
BM25 inverted index, but not a searchable vector index (with ~3,669 docs < `linearBuildThreshold=5000` a
self-query should be exact rank-1, yet returns nothing). So:

- **Do NOT run `cli.reindex(...)` / 全量构建 on the Swift-fed table** — there is nothing complete to build from.
- A Stage-3 re-push restores BM25 **and a clean index**, which is what makes specific SOPs rank well via
  *hybrid* (the 触电 SOP went to #1 this way) — but it does **not** fix pure dense/semantic recall.
- The durable dense fix is a separate, deliberate migration: give the table a **persistent offline source
  (OSS/MaxCompute) + a validated offline build**. See **`docs/ha3-dense-fix-plan.md`** and gotchas **G20/G21**.
- Recover from an accidental wipe with the **G20 runbook**: `scripts/reset_index_for_repush.py` (preview →
  `--commit`) → run `清理stage3` (`run_stage_drained`) → watch `scripts/poll_repush.py`.

> **⚠️ STATUS (2026-06-07): the offline-build dense fix was attempted end-to-end and hit a BACKEND WALL — do
> not blindly re-attempt.** Tables `v2` (OSS+Paimon), `v3` (DLF+numeric), `v4` (OSS+API+string, doc-correct
> format) **all stuck in `building` forever** (`errorCode:3000 init gig stream failed`); the live API table's
> HNSW stays empty despite `enable_rt_build=true` + correct push format (`ignore_invalid_doc=true` hides why).
> Every client-side cause was eliminated — it's in Alibaba's backend build pipeline. **Next step is the
> support ticket, not more table-creation** (which only adds undeletable `IndexInUse` clutter + build
> contention). Full verified mechanics + dead ends: gotchas **G22–G27**. Ready-to-submit ticket + forward
> plan: **`docs/ha3-dense-fix-support-ticket.md`**. Production is unaffected (BM25) throughout.

## Phase 4 — Verify

1. **RDS** (`scripts/verify_state.py`, `verify_stage2.py`, `check_extraction_quality.py`): new chunks
   `INDEXED`+embedded, docs `SUCCESS`, old `v1` chunks `is_active=0`/`DELETED` (the swap), `FAILED=0`,
   chunk-type mix sane (`step_card`, `image`, `text_chunk`, …), and which docs came out `EMPTY` (expect
   only genuinely-empty + intentionally-quarantined `cn_id_card` docs). `old_v1_active` won't hit exactly
   0 — EMPTY/quarantined docs keep their old `v1` (G9/G17).
2. **OSS** (`scripts/verify_oss_canonical.py`, via the OSS *public* endpoint): every rebuilt doc's
   `canonical_json_key` exists and is non-empty; spot-check structure.
3. **HA3 content + masking** (`scripts/ha3_query_prod.py` / `ha3_query_node.py`): filter by `doc_id` to
   confirm rebuilt chunks (`version_no=2`) are present **with PII masked** (e.g. `136****5055`).
   - The HA3 **public** endpoint is `…public.ha.aliyuncs.com` on **HTTP/80** (not 443), whitelist + public
     access required (G2). The intranet endpoint is VPC-only.
   - **Relevance** from a raw query ≠ production: the real retriever does weighted fusion (knn 0.7/text
     0.3) + BM25 on `chunk_text` (`SearchRequest`/`client.search`) + neighbor-stitching + step-card
     expansion. `ha3_query_prod.py` replicates the fusion query but not the stitching/rerank — so the
     **definitive relevance/UX test is the DingTalk bot / `/api/ask`**, not a laptop query (G16).
4. **ANN health — the check that catches a stale HNSW graph (G18), do NOT skip:**
   `scripts/diag_ann_selfquery.py`. A healthy index returns every **self-query** (a chunk searched by its
   own text) at **rank 1 @ ~1.0**; mediocre top-1 (~0.3–0.5) or self-misses ⇒ the graph wasn't rebuilt →
   go back to **Phase 3.5**. Cross-check `stats` docCount ≈ RDS `COUNT(is_active=1)` (G19,
   `scripts/diag_ha3_index_def.py`). Then run the real bot path end-to-end: `scripts/bot_query_test.py`.
   RDS/forward-field checks (items 1–3) **all pass with a broken graph** — this step is what surfaces it.

## When something is wrong with the *content* (not the mechanics)

Two real bugs surfaced last rebuild — both **pass `make sim` but fail in production**, and both are in
the processing logic, not the rebuild mechanics. See `references/gotchas.md` for the fixes:
- **Chunker dict-vs-object (G13)**: chunk methods using attribute access (`blk.block_type`) crash on prod
  dict blocks. Validate with `scripts/test_chunk_dict_blocks.py`-style dict-block tests.
- **PII over-quarantine (G14)**: high-severity PII (phone/email) dropping whole SOPs. Tune
  `ENTITY_SEVERITY` in `pipeline_nodes.py`; validate with `scripts/test_pii_policy.py`.
