# Gotchas catalog (failure modes + fixes)

Every item here cost real debugging time during a live rebuild. Skim it before starting so you don't
re-discover them.

## Architecture / access

### G1 — The data plane is VPC-only; your laptop isn't in the VPC
OSS-internal and the HA3 intranet endpoint resolve to `100.64/10` addresses (carrier-grade NAT / VPC) —
**non-routable from the public internet**, so no whitelist exposes them. The DataWorks nodes run in-VPC
and reach them fine. **RDS is the exception**: its public endpoint is laptop-reachable, so the helper
scripts (pre-flight, seed, monitor, verify) run locally against RDS. OSS and HA3 have **public** endpoints
that are laptop-reachable *if* public access + whitelist are configured (see G2).
**Implication:** the actual DAG stages must run in DataWorks; only the RDS-side prep/monitor/verify runs locally.

### G2 — HA3 public endpoint is HTTP/80, not HTTPS/443
The 公网域名 `…public.ha.aliyuncs.com` serves on **HTTP port 80**. Probing 443 times out and looks like
a whitelist/connectivity failure — it isn't. Use `protocol="HTTP"` in the SDK `Config`. Also: enabling
the IP allowlist is **not** enough — 公网访问 must be *enabled* on the instance (separate toggle). And the
Bash tool runs on the **user's** laptop, so it's the user's egress IP that needs whitelisting.

### G3 — The DataWorks MCP can't connect mid-session
MCP servers load only at Claude Code **startup**. After writing `.mcp.json` + exporting the AK/SK, the
user must fully restart. You cannot make the server appear in the running session. Also, the safety
classifier blocks *you* from writing `.mcp.json` with literal creds (auto-runs npx with secrets) — hand
the user the config instead.

### G4 — Stage nodes are manual, not scheduled — and can't be triggered via the ops-center API
`ListTasks` shows only a meta-collector + root; `ListWorkflows` is empty. The `opensearch_stage*` nodes
are DataStudio **manual** nodes (`recurrenceType: NoneAuto`). They're run from the **IDE "运行"**, not via
`CreateWorkflowInstances`/`TriggerSchedulerTaskInstance`. `ExecuteAdhocWorkflowInstance` (the only API path
that could run arbitrary script) is **blocked by the credential-leakage guard** because the script embeds
RDS/HA3 creds. → You orchestrate by guiding the user to run nodes in the IDE; you monitor/verify via RDS.

### G5 — `opensearch_stage3_push_index` is broken; use `清理stage3`
The plain Stage-3 node downloads `opensearch_pipeline.zip` which **doesn't exist** as a resource (only
`opensearch_pipeline_production.zip` does). It fails at the download step. `清理stage3` references the
correct archive, purges `is_active=0` chunks from HA3+RDS first, then runs Stage 3. Use it.

### G6 — Code deploy: the zip is what runs, and OpenAPI upload is blocked
Editing the working tree does nothing until the archive resource is rebuilt + re-uploaded. The OpenAPI
`UpdateResource` path requires a publicly-accessible (presigned) URL to the zip — the guard blocks that
as data exfiltration. **Re-upload via the DataStudio console** (Resources → replace → Submit). Build the
zip from `opensearch_pipeline/` only (the nodes `extractall` then `import opensearch_pipeline.…`).

## RDS / state

### G7 — `uk_raw_key_hash` UNIQUE blocks same-`raw_key` versioning
Production `document_version` has `UNIQUE KEY uk_raw_key_hash (raw_key_hash)` (absent from repo
`schema/001`). A new version row reusing the same `raw_key` (→ same SHA256) collides. **Fix:** salt the
hash → `raw_key_hash = SHA2(CONCAT(raw_key,'#v',new_ver),256)`, keep `raw_key` = the real OSS path.
`raw_key_hash` is only a uniqueness/dedup token; extraction reads `raw_key`. (`scripts/seed_versions.py`
already does this.)

### G8 — Zero-downtime swap **requires** a new `version_no`
`node_write_chunk_meta` deletes only `WHERE chunk_id IN (new_ids)`, and `node_deactivate_old_chunks`
removes only `version_no < current`. So a **same-version in-place** re-extract (new logic → new chunk_ids)
**orphans** the old chunks (never cleaned in RDS or HA3). Always bump versions; never reset-in-place
unless you also explicitly purge old chunks.

### G9 — Quarantined / empty docs keep their OLD chunks
A doc that produces 0 new chunks (quarantined or empty) is **not** re-indexed, so `deactivate_old` never
runs for it → its old `v1` chunks stay `is_active=1` in HA3. Consequence: a "quarantine" that yields 0
chunks doesn't actually hide anything (old content lingers) **and** doesn't update the doc. Decide
explicitly whether to purge those old chunks.

### G10 — Legacy `.doc`/`.xls`/`.pptx` are silently excluded
Stage-1 scanner filters `file_ext NOT IN ('doc')`; `.xls` is mis-routed to openpyxl and fails silently.
These never index. Don't count them in "queue size". `HOLD` them so they don't pollute the run. Getting
them into the KB needs format conversion (a `.doc`→`.docx`/text pre-step) — out of scope for a rebuild.

### G11 — Counts move in ~100-step batches; flat ≠ stalled
Stage-1 commits canonical keys at the **end** of each 100-doc batch, so the RDS pending count drops in
~100 steps with long flat stretches (a VLM-heavy batch is 15-40 min). Extraction writes nothing to RDS
mid-batch, so `updated_at`-based liveness is spiky. Don't false-alarm; the real failure signal is
`FAILED > 0` or the IDE log. Stage-2's atomic `LOADING` claim makes its count drop at claim time, then
`new_chunks` jumps at write.

### G12 — Throughput knobs: what's real, what's dead
Real: the **drain-loop** (`run_stage_drained` wraps `run_stage` to process all batches in one invocation)
and **parallel embedding** (`RAG_EMBED_CONCURRENCY`, default 5; the unconditional `time.sleep(1)` removed).
Dead config (read nowhere): `RAG_MAX_CONCURRENT_TASKS`, `RAG_SCAN_BATCH_SIZE`. `--bizdate` is exported but
**not** used for scoping — every run drains the global oldest-N pending rows. Node scripts call
`run_stage(N)` (one batch); switch to `run_stage_drained(N)` (and fix the import) to drain in one run.

## Processing-logic bugs (pass `make sim`, fail in production)

### G13 — Chunker dict-vs-object `AttributeError`
**Symptom:** Stage 2 node 05 (`Chunk Documents`) fails fast (~30 ms): `'dict' object has no attribute
'block_type'`. **Cause:** simulate mode passes `ExtractedBlock` *objects*; production Stage 2 loads
canonical from OSS as **JSON → dict blocks**. Methods using attribute access (`blk.block_type`, `blk.text`,
`blk.extra`, `blk.page_num`) crash. The xlsx-layout methods `_chunk_procedure_steps` /
`_chunk_product_spec` had this; other modes already guarded with `isinstance(block, dict)`.
**Fix:** route all block-field reads through a dict-or-object helper (`_blk_get(b, name, default)`).
**Validate:** a dict-block test (`scripts/test_chunk_dict_blocks.py`) — `make sim` won't catch it.
**General lesson:** any new chunker method must handle dict **and** object blocks (chunk_from_blocks
docstring: "List[ExtractedBlock] 或 List[dict]").

### G14 — PII over-quarantine drops content-bearing docs
**Symptom:** docs with real text produce 0 chunks (`chunk_status='EMPTY'`); a doc with 27k chars → 0 chunks.
**Cause:** `node_detect_sensitive` marks all `ENTITY_PATTERNS` regex hits `severity="high"` →
`entity_risk="high"` → `redaction_action="QUARANTINE"` → `node_chunk_documents` skips the doc. Internal
SOPs with emergency **phone numbers** (`cn_mobile`) got dropped. **Fix (policy decision — confirm with
user):** add a per-entity `ENTITY_SEVERITY` map so `cn_mobile`/`email` are `medium` (→ REDACT: masked
in-place via `REDACTION_MAP`, doc kept + indexed) while `cn_id_card`/secrets stay `high` (→ QUARANTINE).
Medium-risk docs already redact fine. **Validate:** `scripts/test_pii_policy.py` (phone/email →
REDACTED+masked, id-card → QUARANTINE). See also G9 (quarantined docs keep old chunks).

### G15 — DashScope transient timeouts during classification
A classification LLM call can hit the 90 s read timeout (`vpc-cn-beijing.dashscope…`). The classify node
catches it per-doc (sets `FAILED` + a `review_task`) and continues. The **drain-loop auto-retries** it
(`content_process_status='FAILED' AND retry_count<3` is re-claimable), so a single timeout self-heals
without intervention. Don't panic on a transient `FAILED=1`; watch whether it clears on the next batch.

## Retrieval / verification

### G16 — Raw HA3 query ≠ production relevance
A bare dense+sparse `QueryRequest` ranks poorly (sparse term-overlap on generic words pulls in unrelated
docs). Production (`retriever.py`) uses `SearchRequest(knn=…, text=TextQuery("chunk_text:'…'"), rank=…)`
with `knn.weight=0.7 / text.weight=0.3` via `client.search()`, **plus** neighbor-stitching, step-card
expansion, cover-page demotion, and the LLM answer-gen reads the top-7 + knn-100 pool. A small specific
SOP can lose to a large doc (员工手册) that shares vocabulary. **`scripts/ha3_query_prod.py` replicates the
fusion query but not the stitching/rerank** — so for true relevance/UX, **test via the DingTalk bot /
`/api/ask`**, not a laptop query. To confirm content is merely *present*, filter by `doc_id` (definitive)
rather than relying on free-text ranking.

### G17 — Verify the *swap*, not just the index
After Stage 3, confirm all four: new chunks `INDEXED`+embedded, `document_version` → `SUCCESS`, **old `v1`
chunks `is_active=0`/`index_status='DELETED'`** (proves deactivation ran), and `FAILED=0`. `old_v1_active`
won't reach exactly 0 — the EMPTY/quarantined docs (no new chunks) keep their old `v1` (see G9).

## Vector index / ANN (HA3 HNSW)

> ## 🛑 READ FIRST — G18–G28 were a FALSE ALARM (resolved 2026-06-07). See **G29.**
> The entire "dense/HNSW is broken" investigation below (G18–G28: stale graph, never-built, offline-build
> stuck, is_embedding_saved, adsampling, Table_Active, instance upgrades…) was triggered by **one bug in the
> diagnostic scripts**: dense self-queries omitted **`order="DESC"`**. The index is **InnerProduct** (higher
> score = more similar), so without `order=DESC` the engine returns results **ascending (worst-first)** and the
> score-1.0 self-match is buried at the bottom → top1≈0.4, "NOT IN TOP500" → a convincing-but-false "HNSW is
> empty" signal. **Dense retrieval was working the whole time** (production `retriever.py` correctly uses
> `order="DESC"`; a console vector query test with DESC returns the self-match at rank-1 @ 0.9999). G18–G28
> below are kept as a record of the wrong path; **the truth is G29.** What was *genuinely* real: the legit
> DAG-1→3 rebuild; the destructive `reindex`-on-swift outage + recovery (G20); and the new-table `Table_Active`
> hang (G28) — but that last one is **moot** because dense already works and no new table is needed.

### G18 — A stale HNSW graph silently breaks dense retrieval while everything else looks fine
**Symptom:** a doc is fully indexed (RDS `INDEXED`, `is_active=1`), its text is top-ranked by BM25,
`include_vector` returns the *correct* stored vector — yet the bot answers "未找到相关信息" and dense/kNN
can't find the doc at all. The tell: a **self-query** (embed a chunk's own text, search dense-only) does
**not** return that chunk at rank 1 — the global top-1 score is a mediocre ~0.3–0.5 for *every* query.
**Cause:** the table's data source is **`autoBuildIndex: false`** (confirm via `cli.get_table`). A push
updates the **forward field + inverted (BM25) index** in realtime, but the **HNSW graph is only rebuilt by
a FULL build**. After a rebuild the graph is stale: new vectors aren't in it, and a freshly-added doc that
never had a prior indexed version (e.g. a `.doc`→`.docx` first-time index like `A37-2触电应急`) is
*completely* unfindable by ANN. Embeddings are fine — `cos(stored, fresh_embed) ≈ 1.0`, `cos(query, stored)
≈ 0.77`; the **graph** is the problem.
**NOT the cause:** distance type / normalization — `distanceType=InnerProduct` is correct *given*
unit-normalized vectors (text-embedding-v4 returns L2 norm ≈ 1.0; verify). And it's **not** under-recall:
raising `proxima.hnsw.searcher.ef` (even to 2000) via per-query `search_params` changes nothing (top-1
stays identical to the digit) — a wrong graph, not a shallow search.
**Diagnose:** `scripts/diag_ann_selfquery.py` (self-query several docs; healthy = rank 1 @ ~1.0),
`scripts/diag_ha3_index_def.py` (`get_table` → `vectorIndex`/`advanceParams`/`autoBuildIndex`, `stats`
docCount, + an `ef` sweep), `scripts/bot_query_test.py` (faithful `/api/ask` replication end-to-end).
**⚠️ Fix — CORRECTED, read G20 + G21 before acting. Do NOT just `reindex`.** The intuitive fix
("trigger a full build / `cli.reindex(...)`") is a **TRAP** on this table: its only data source is the
**Swift realtime topic** (limited retention), so `reindex(data_time_sec=…)` replays from an empty/expired
log, **builds an empty generation, and swaps it into serving → full production outage** (this happened —
see G20 for what it cost and how it was recovered). The deeper truth (G21): on this table the vector index
was likely **never built at all** (no offline data source; realtime push doesn't build the ANN index), so
there's no "stale graph to refresh" — the framing above is a first approximation, superseded by G21.
The **durable fix** is to give the table a **persistent offline full-data source (OSS/MaxCompute) + a real
offline build**, done as a validated, console-driven migration — see `docs/ha3-dense-fix-plan.md`.
**Two findings that DON'T need that migration:** (a) deleting old-version duplicate chunks so the index is
clean (G19) materially improves *hybrid* ranking via BM25 even with dense still broken — it's what made the
触电 SOP rank #1 post-recovery; (b) diagnosis is cheap — `self-query` and a real bot query catch this, while
offline eval (`text_quality_eval.py`, brute-force cosine) and `include_vector` fetch all pass with it broken.

### G19 — HA3 keeps logically-deleted old-version docs until a full build compacts
After the swap, RDS correctly shows old chunks `is_active=0 / index_status='DELETED'` (observed: **3,898
deleted vs 3,669 active**), but HA3 `stats.totalDocCount` still counts **both** (observed: **7,567**). The
realtime deletes from `node_deactivate_old_chunks` are applied logically, but the docs are **physically
retained — and still ANN/BM25-searchable — until a full build** compacts them (same `autoBuildIndex=false`
root cause as G18; this is the cross-cloud split-brain flagged in the known-issues doc). Until then, stale
old-version vectors compete in results. The same full build that fixes G18 compacts these. Verify with
`diag_ha3_index_def.py` (`stats`) vs RDS `SELECT COUNT(*) ... WHERE is_active=1`.

### G20 — `reindex` on a Swift/API-push-fed table EMPTIES the index (production outage) — and how to recover
**What happened:** to "fix" dense (G18) we called `cli.reindex(table, ReindexRequest(data_time_sec=now))`.
A new generation registered (`status: building`); the old generation kept serving ~10 min (zero-downtime so
far); then `docCount` dropped **7567 → 0**, the new (empty) generation went `ready` and **swapped into
serving → search returned nothing for every query.** Cause: this table's build source is the **Swift realtime
topic**, whose retention had expired (~3 weeks since the pushes), so the full build had nothing to replay and
produced an empty index.
**Pre-empt:** before ANY `reindex`/全量构建, run `cli.get_table` — if `dataSource.type=="swift"` and there is
**no** persistent OSS/MaxCompute full-data source, a full build replays the (expiring) realtime log. **Don't
do it.** (`reindex`'s `data_time_sec` is documented as "required for API-push source" precisely because that
is what it replays.) The proper dense fix is G21 / `docs/ha3-dense-fix-plan.md`.
**Recovery (NO data loss — everything is in RDS + OSS + the embedding cache):**
  1. `scripts/reset_index_for_repush.py` (preview → `--commit`): flip active chunks `index_status`
     `INDEXED`→`NOT_INDEXED` (RDS still said INDEXED while HA3 was empty — split-brain) so Stage 3 re-pushes.
  2. Run the **`清理stage3`** node (uses `run_stage_drained` → one run drains all). It purges `is_active=0`
     (no-op on the empty index), re-embeds (cache-first; cold cache → DashScope, still fast) and re-pushes via
     realtime `push_documents`. Monitor with `scripts/poll_repush.py` (INDEXED climbs 0→N, docCount rises).
  3. Result: serving restored via realtime in **minutes**, and the index comes back **clean** (only active
     docs — the G19 bloat is gone as a bonus). BM25 + hybrid work again; pure dense stays broken until G21.

### G21 — Dense/ANN was likely NEVER built on this table (supersedes G18's "stale graph" framing)
`get_table` shows `dataSource={type:swift, autoBuildIndex:false}`, `dataProcessConfig=[]`, and
`linearBuildThreshold=5000`. We *inferred* that ~3669 docs (< 5000) would force an **exact brute-force** scan
(so a self-query should be rank 1 @ ~1.0) — but **public docs do NOT define `linearBuildThreshold` /
`minScanDocCnt` / `enable_rt_build`** (verified across 5 doc pages; they're Proxima-internal knobs, *暂不开放*),
so that inference is **unconfirmed** — do not state it as fact. The real basis is empirical: a self-query
returns nothing (top-1 ~0.4), and a fresh push (both array & comma-string, 200 OK) is unsearchable after 80s.
⇒ the **vector/ANN index is unpopulated**: realtime push fills the forward field + BM25 inverted index but NOT a searchable
vector index, and no offline full build ever ran from a complete source. So dense has likely been broken for
the entire life of this table — unnoticed because BM25 carried retrieval and the offline eval
(`text_quality_eval.py`) used brute-force cosine, never this ANN index. **Durable fix:** provision a
**persistent offline full-data source (OSS or MaxCompute)** holding all active chunks (`id`,
`dense_vector[1024]` from the embedding cache, `sparse_vector_indices/values`, `chunk_text`, metadata) and run
a **real offline build, validated before swap** — see `docs/ha3-dense-fix-plan.md`. **Not** a Swift `reindex`
(G20). Until then dense recall is weak, but BM25 + a clean index serve most real (keyword-bearing) queries.

## Dense-fix rebuild attempts — v2/v3/v4 saga (2026-06-07): verified mechanics + the backend wall

The G18–G21 "offline full build" fix was attempted end-to-end (tables `fuling_kb_chunks_v2/v3/v4`). It hit
a wall that turned out to be in Alibaba's **backend build system**, not our data. These gotchas capture the
verified mechanics so the next attempt skips every dead end we hit.

### G22 — `对象存储OSS+API` source silently forces `tableFormat: paimon`
In this console version, choosing 全量数据来源 = **对象存储OSS+API** (even with 数据格式=json) stamps
`dataSource.config.tableFormat: paimon` on the table, with **no UI toggle to disable it**. Paimon expects
Parquet/ORC, not raw JSONL → build fails (`init gig stream failed`). The Alibaba AI insists paimon is "just a
UI default," but the table is empirically paimon-typed. **For raw OSS JSONL, OSS+API is the wrong entry point
here** — use DLF Object Table (G24) or DataWorks 数据集成「单表离线同步」 (the *documented* offline-sync method).

### G23 — `/indexes/{t}` ≠ `/tables/{t}`; `autoBuildIndex` lives on the INDEX
Two raw-HTTP API surfaces on `…/openapi/ha3/instances/{id}/`:
- `GET /tables/{t}` — table view; `dataSource` is an object.
- `GET /indexes/{t}` — **index** view; has `content` (full schema: analyzer, `enable_rt_build`,
  `ignore_invalid_doc`, vector params), **`dataSourceInfo` (where `autoBuildIndex` actually lives)**,
  `configWhenBuild`, `cluster`.
To flip autoBuildIndex: `PUT /indexes/{t}` with the **minimal** body `{"dataSource":"<ref>","autoBuildIndex":true}`
(full-spec bodies hit `ModifyVariableNotAllowed` on read-only fields like `process_parallel_num`).
⚠️ The PUT **no-ops while the table is `building`/`RESTORE_USE`** (returns 200, value unchanged).

### G24 — DLF Object Table mechanics (the documented raw-JSONL path)
For raw OSS JSONL the correct source is **数据湖构建(DLF) + Object Table** (not OSS+API). Traps:
- An Object Table's schema is FIXED (`path/name/length/mtime/atime/owner` — it catalogs *files*, not
  contents). You do **not** define the 23 content fields in DLF; OpenSearch parses file contents per the
  **OpenSearch-side** field schema.
- A **Managed** Object Table stores files in a **DLF-managed bucket** (`oss://clg-paimon-…`), **NOT** yours.
  Your OSS AccessKey **cannot write** to it (`AccessDenied: bucket does not belong to you`). Upload the data
  file via the DLF console **文件列表 → 上传文件** (uses DLF's own permissions).
- OpenSearch 数据源 fields: 数据目录=DLF catalog, 数据库, 数据表, **相对路径 = the filename as shown in 文件列表**
  (it keeps the uploaded file's local name, e.g. `/v3_data_….jsonl`, NOT `/data.jsonl`), 数据格式=json.
  `数据来源校验` is read-only/safe and tells you if the relative path resolves.

### G25 — Realtime-push format ≠ OSS-offline-file format (don't mix them up)
- **Realtime push** (`push_documents`): `{"cmd":"add","fields":{…,"dense_vector":[0.1,0.2,…],…}}` —
  **lowercase `cmd`**, a **`fields` wrapper**, **numeric arrays**, no multi-value delimiter
  (《实时推送文档格式》). Stage 3's `to_ha3_doc` already emits this correctly.
- **OSS offline file**: `{"CMD":"add","id":"…","dense_vector":["0.1","0.2",…],…}` — **uppercase `CMD`**,
  **flat** (no wrapper), **string values** incl. string vector arrays (《OSS+API 数据源》 doc example).
The two Alibaba-AI answers contradicted each other on string-vs-numeric; the **doc** settles it per-channel
(push=numeric, file=string), and empirically **neither** mattered — both still failed the build (G27).

### G26 — `reindex` on an API/swift-source table is DESTRUCTIVE — never run it
《表的索引重建》 verbatim: *"API推送数据源重建会将之前推送的数据清空，从指定的时间戳开始追实时数据"* — it **wipes
all data**, recovers only ~3 days of swift increments. This is exactly what
`cli.reindex(ReindexRequest(data_time_sec=now))` did to the live table (the outage; recovered via re-push).
**There is no non-destructive in-place rebuild for an API source.** Never reindex a swift table to "fix" it.

### G27 — Offline builds don't complete on THIS instance; live HNSW empty despite correct config → backend/support
End state after exhaustive testing (2026-06-07):
- **v2** (OSS+Paimon), **v3** (DLF+numeric), **v4** (OSS+API+string, **doc-correct format**): ALL stuck in
  `building` indefinitely (v4 ~40 min), `stats` → `errorCode:3000, "init gig stream failed"`, docCount never
  appears.
- **live** (API push): `enable_rt_build=true`, correct push format, vectors present in forward field
  (cos=1.0) — yet HNSW returns nothing for a self-query. `ignore_invalid_doc=true` **silently hides** whatever
  rejects the vectors from the graph.
  - **Controlled push test (rules out the DAG-3 push):** pushing 2 fresh test docs to the live table — same
    vector encoded as a **numeric array** AND as a **comma-string** — both returned `200 OK`, and after 80 s
    of realtime indexing **neither was kNN-searchable by its own vector**. So the push code/format is NOT the
    cause; **realtime HNSW doesn't index even freshly-pushed docs**. Also confirmed 500/500 stored vectors are
    valid (dim 1024, norm 1.0000, no NaN/zero) — so it's not invalid data either. Purely a backend build issue.
  - **A/B test (rules out enable_adsampling) + the sharpest symptom:** created a brand-new MINIMAL swift table
    (`fuling_abtest_noads`: 3 fields, no offline source, no TEXT/sparse, **`enable_adsampling=false`**). It
    **never reached `IN_USE`** — stuck in `RESTORE_USE` / errorCode 3000 for 200s+; pushed docs returned 200 OK
    but self-query was 0/10 (returns empty). ⇒ `enable_adsampling` is NOT the cause, AND **every newly-created
    table on this instance (OSS / DLF / plain swift, any config) is stuck in `RESTORE_USE` and never activates** —
    only the pre-existing live table is `IN_USE`. This is an **instance-level build/activation failure**, the
    cleanest single symptom for support, and it also explains the live table (the build subsystem can't
    materialize new HNSW segments, so re-pushed vectors never index). Also locally proven the vectors are fine:
    brute-force cosine over the same vectors gives 15/15 self@1.0 + sensible neighbors.

### G28 — `cli.list_tasks(ListTasksRequest())` reads the build/activation FSM — RUN THIS FIRST when a build sticks
This is the diagnostic we should have used hours earlier. When a table is stuck (`RESTORE_USE`, errorCode 3000
`init gig stream failed`, never `IN_USE`), `cli.list_tasks(ListTasksRequest())` (args: `start`,`end`) returns
each FSM with `taskNodes[].nodeInfo.{index,name,status,msg}` — the **same content as the console 变更历史**, but
via API. It shows the **exact hang node + message** instead of guessing. (`get_table_generation` only gives a
coarse `building`/`ready`; `list_tasks` gives the node-level truth.)
**What it revealed (2026-06-07) — the actual root cause + a key asymmetry:**
- **New-table activation** (`update_biz_depend_index_fsm`, `operateType=Table_Active`): hangs at node
  **`target checking`**, msg **「引擎未收到该索引的切换目标，继续等待」** (the engine never receives the new
  index's switch target). EVERY new table (v1–v6, rawhttp, abtest_noads) stuck here → never `IN_USE`. An
  instance-level admin↔suez/searcher coordination failure, independent of schema/data/vectors/config.
- **Full build** (`datasource_flow_fsm`, `operateType=Index_Rebuild`): SUCCEEDS end-to-end —
  `init→trigger→scan→bs_submit→build→suez_submit(「引擎收到目标，开始切换」)→switch(「switch finished」)`.
**Implications:** (1) Creating a NEW table to fix dense is futile while `Table_Active` is broken — **stop making
tables.** (2) The build+switch *mechanism* works, BUT you can't simply "Index_Rebuild the live table from OSS":
the live table's source is **swift** — its `reindex` only tails the Swift log (`data_time_sec`) and clears history
(that's what emptied it), and it has **no OSS source to build from**. Changing `dataSource.type` swift→oss needs
`modify_table` (errors `ModifyVariableNotAllowed` client-side). So **both client paths are blocked** — new
OSS-source table (blocked by `Table_Active`) and live swift table (no OSS build) — and the fix needs Alibaba:
fix `Table_Active` so a fresh OSS-source table can activate, OR a backend rebuild/source-change on the live table.
(3) When ANY HA3 build/activation sticks, run `list_tasks` FIRST — it turns a black box into a named FSM node.
(4) **Leading hypothesis for the `Table_Active` hang: query-node capacity.** The instance has **查询节点数量=1**,
already hosting the live table; a single query node may have no slot to load a 2nd index, so the engine never
receives the new switch target (the hang is at load/switch, *after* build — consistent). If so, this is
potentially **user-fixable: scale up query nodes (变配 1→2+)** → new tables activate → then build an OSS-source
table + cut over, no Alibaba backend needed. **Confirm with support that capacity is the cause before paying for
a scale-up** (1 node doesn't always mean a 1-table limit — depends on node memory/per-node index limits).
**Empirical update (2026-06-07):** upgraded **data nodes** (2核8G→2核16G) → a fresh minimal table STILL stuck at
`target_checking=RETRY`, 0/10, never IN_USE. So **data-node capacity is ruled out.** Remaining levers: query-node
capacity (查询节点=1, untested) OR — more likely given a capacity bump changed nothing — an admin↔engine
coordination **bug**. Don't keep blindly upgrading; have support read the backend reason `target_checking` never
receives the switch target.

### G29 — ✅ RESOLUTION: dense was NEVER broken — kNN self-queries just need `order="DESC"`
**Root cause of the entire G18–G28 saga:** the dense index is **InnerProduct** (higher score = more similar).
HA3 `QueryRequest`/`SearchRequest` order defaults to **ascending**; for InnerProduct you MUST pass
**`order="DESC"`**, or results come back **worst-first** and the score-1.0 self-match sits at position ~N
(buried far below any `top_k`). Our diagnostic scripts omitted it → false "HNSW empty" (top1≈0.4, "NOT IN TOP500"),
which is what we chased through G18–G28.
**Proof (2026-06-07):**
- Console 查询测试 (向量, 结果排列顺序=**DESC**) on `fuling_kb_chunks`: pk **5733 → rank 1 @ 0.999988**.
- SDK `QueryRequest(..., order="DESC")` → pk 5733 rank1@1.0; with `order="ASC"`/unset → MISS, top1 0.67 (same vector).
- Production `retriever.py` ALREADY uses `order="DESC"` (search_chunks ~L352, expand ~L507) → **the bot's dense +
  hybrid retrieval has worked all along.** End-to-end confirmed: a paraphrase query ("员工被电击受伤了现场怎么抢救",
  no literal 触电) surfaced the 触电急救/应急 SOPs at the top + a correct LLM answer.
**Lessons:**
- For an **InnerProduct** vector index, **`order="DESC"` is mandatory on every kNN query.** A self-query that
  doesn't return itself @~1.0 is almost certainly a **sort-order/query bug, not a missing index** — check `order`
  FIRST (the cheapest possible check) before suspecting build/data/index. `diag_ann_selfquery.py` is now fixed.
- **Sanity-check the diagnostic harness before concluding prod is broken.** A self-query is supposed to be the
  ground-truth probe; ours was silently miscalibrated and manufactured a phantom for hours. Bake a known-good
  assertion (a doc that MUST match → assert rank-1@~1.0) into the harness itself.
**Net:** nothing to fix in production dense. Only real leftover: the stuck test tables (`v4` / `abtest_noads` /
`acttest1`) need Alibaba force-delete (the `Table_Active` hang, G28) — cosmetic; production is healthy on both
BM25 and dense.
- Stuck tables **cannot be deleted** (`delete_table`→`IndexInUse`; `stop_table` no-ops; console delete needs
  `NOT_USE`).
Every client-side hypothesis eliminated (format, source type, encoding, `autoBuildIndex`, dimension,
`实时索引`/`enable_rt_build`). The cause is in the backend build pipeline (logs suppressed by
`ignore_invalid_doc`). **→ Alibaba support ticket** (`docs/ha3-dense-fix-support-ticket.md`): (1) why pushed
vectors don't enter HNSW; (2) why offline builds never complete (build-resource starvation from ≥3 stuck
tables?); (3) force-delete the stuck tables; (4) confirm DLF/数据集成 vs 全量数据来源 as the right method.
**Production stays on BM25 throughout — no outage.** Do NOT keep creating tables (worsens contention + clutter).
