# Fuling HA3 Vector Index — Dense kNN Recovery Plan (FINAL)

> **Status:** READ-ONLY plan. No mutating API calls until each ⚠️ step is explicitly approved AND every pre-execution verification listed below is complete.
> **Engine:** Alibaba Cloud OpenSearch 向量检索版 (Havenask), `alibabacloud_ha3engine_vector` SDK.
> **Table under repair:** `fuling_kb_chunks` (pk=`id`, partitionCount=2, IN_USE, ~3669 active docs).
> **Symptom being fixed:** pure dense kNN self-query returns nothing (top500 misses the queried doc; global top1 ~0.4); BM25 works. Forward field returns the unit-normalized vector correctly (`cos(stored,fresh)=1.0`). The vector index segment is materially empty even though the forward field is healthy.
> **Root cause (verified):** a prior `cli.reindex(ReindexRequest(data_time_sec=now))` on a swift-fed source whose retention had expired produced an empty generation and swapped it online. The reindex doc is explicit: *"API数据源全量时会将以前推送的数据清空, 从指定的时间戳开始追实时数据"* — only the last 3 days of swift can be backfilled ([Alibaba reindex doc](https://help.aliyun.com/zh/open-search/vector-search-edition/index-rebuild)). The subsequent `push_documents` recovery repopulated the forward field + BM25 inverted index but did NOT rebuild the HNSW/Linear segment (matches [Havenask issue #167](https://github.com/alibaba/havenask/issues/167)).

---

## 0. Current interim state (revised — more honest)

- BM25 + the realtime-pushed forward index are intact.
- Hybrid queries that carry distinctive keywords (e.g. `触电应急`) still surface a usable doc because BM25 ranks it #1, but **with the kNN leg returning empty, the weighted-fusion score collapses to the BM25 leg only**. The production score-threshold labels (`高=8.0`, `中=5.0`) were calibrated against fused dense+sparse+BM25 scores; running them against BM25-only scores almost certainly under-labels relevance to the LLM, increasing hedging and 未找到答案 false negatives **for all queries, not only paraphrases**.
- **Action item for §0:** before cutover, sample 20 answers from the last 24h of `qa_session_log`, grep for elevated 未找到答案 / hedged-language rate vs. the pre-outage baseline (see `work_report.md`). If degradation is measurable, the rollout urgency escalates and we should consider a manual `score_threshold_*` recalibration as an interim hotfix while the v2 rebuild proceeds.
- **What this plan fixes:** restore a built, queryable dense vector segment, lossless at 3669 docs (because `linearBuildThreshold=5000` should route the builder through LinearBuilder/LinearSearcher per [vector-index params doc](https://help.aliyun.com/zh/open-search/retrieval-engine-edition/vector-indexes/) — see Confidence section below for the caveat).
- **What this plan does NOT change:** chunking, embeddings, retrieval fusion weights, prompt, permission filter logic. Pure index-layer repair.

---

## 1. Recommended path: **(A) RECREATE** — new sibling table, validate, then cutover via config

### Why A, not B

1. `cli.reindex(...)` on the current swift-backed table is the **exact code path that emptied the index last time** — documented behavior, not a bug.
2. Realtime push alone does not produce the searchable HNSW/Linear segment in this configuration (Havenask issue #167 — direct match).
3. Whether `ModifyTable` can flip `dataSource.type` from `swift` to `oss` on a live IN_USE vector-edition table is **not documented**; the documented change-data-source flow is from a different product edition and is UI-only / offline.
4. Path B (modify the live table in place) stacks an unverified type-flip with a full rebuild whose misfire lands on the only table serving prod. Path A lets us prove correctness before any traffic sees the new index, and rollback is one config flip.

**Decision: execute Path A.** Path B is moved out to a separate last-resort runbook (see §10).

---

## 2. Pre-flight (READ-ONLY)

### 2.1 Snapshot the current table's exact schema

```python
# READ-ONLY
ot     = cli.get_table(table_name="fuling_kb_chunks")
gens   = cli.list_table_generations(table_name="fuling_kb_chunks")
stats0 = cli.stats(table_name="fuling_kb_chunks")
```

Persist every response verbatim to `scratch/ha3_snapshot_<ts>.json`. We will mirror **every field** on the new table — same `dimension`, same `distanceType=InnerProduct`, same HNSW params (`efconstruction=500`, `max_neighbor_count=100`, `searcher.ef=400`), same `linearBuildThreshold=5000`, `minScanDocCnt=20000`, same `partitionCount=2`, same primaryKey=`id`, same `fieldSchema` (verbatim, including field order — see §2.3), same `dataProcessConfig=[]`.

### 2.2 Inventory the offline corpus (tightened)

Run exactly:

```sql
SELECT COUNT(*), MIN(updated_at), MAX(updated_at)
FROM chunk_meta
WHERE is_active = 1 AND kb_type = 'fuling';
```

Record the count and the `MAX(updated_at)` — call it `snapshot_ts`. **This is the snapshot frontier for the §9.1 delta-push.**

Then per active row, compute `key = md5(model || chunk_text)` and probe `embedding_cache.json`. Emit two reports:

- `scratch/missing_<ts>.json` — keys not in cache.
- `scratch/malformed_<ts>.json` — cache hits where ANY of:
  - dense vector length ≠ 1024,
  - dense vector L2 norm ∉ [0.99, 1.01],
  - sparse indices not strictly ascending after sort,
  - sparse indices contain non-uint32 values,
  - sparse indices contain duplicates (see §3.4).

**Halt the export if either report is non-empty.** Re-embed via DashScope native (see §2.2a), fold results back into `embedding_cache.json`, re-run. The full row count must match the SELECT exactly.

### 2.2a Re-embedding policy (model-drift guard)

If we re-embed any chunk:

- Pin `model='text-embedding-v4'`; record per-chunk `{model, endpoint, ts, request_id}` into `scratch/rebuild_provenance_<ts>.json`.
- **Drift pre-check:** pick 10 chunks already in the cache, re-embed them via DashScope native right now, assert `cos(cached, fresh) >= 0.9999`. If drift > 0.0001, the cached corpus is partially stale; either re-embed the entire corpus, or fall back to **option (a): exclude unresolvable rows from the rebuild** — they remain in `chunk_meta` but are absent from HA3 until a post-cutover delta push fixes them.
- Bias §5.2's paraphrase spot-check toward re-embedded chunks.

### 2.2b Parent-child integrity check

```sql
-- Orphan step_cards
SELECT id FROM chunk_meta
WHERE is_active=1 AND chunk_type='step_card' AND parent_chunk_id IS NOT NULL
  AND parent_chunk_id NOT IN (
        SELECT id FROM chunk_meta
        WHERE is_active=1 AND chunk_type='procedure_parent');

-- procedure_parents whose children are all inactive
SELECT id FROM chunk_meta
WHERE is_active=1 AND chunk_type='procedure_parent'
  AND id NOT IN (
        SELECT DISTINCT parent_chunk_id FROM chunk_meta
        WHERE is_active=1 AND chunk_type='step_card' AND parent_chunk_id IS NOT NULL);
```

Either query returning rows = hard stop. Reconcile in `chunk_meta` (deactivate or repair) before export.

### 2.3 Identify the export field list (exact mirror of the live schema)

The new table's `fieldSchema` MUST be a verbatim copy of the live one (same names, same types, **same order**, same `multiValueSeparator` per field, same store/index flags). The export writer MUST emit KV pairs in the live field order. The table below lists the *expected* superset — diff it against the §2.1 snapshot; the snapshot wins. **Do not invent fields the live table doesn't have. Do not omit fields the live table does have.**

| Field | Type | Source | Notes |
|---|---|---|---|
| `id` | STRING (pk) | `chunk_meta.id` | |
| `dense_vector` | multi-value FLOAT, 1024 dims | `embedding_cache[key].dense` | unit-normalized |
| `sparse_vector_indices` | multi-value UINT32, ascending, deduped | `embedding_cache[key].sparse.indices` | see §3.4 |
| `sparse_vector_values` | multi-value FLOAT, same length as indices | `embedding_cache[key].sparse.values` | reordered to match |
| `chunk_text` | TEXT (BM25) | `chunk_meta.chunk_text` | |
| `doc_id` | STRING | `chunk_meta.doc_id` | |
| `version_no` | INT | `chunk_meta.version_no` | |
| `chunk_index` | INT | `chunk_meta.chunk_index` | |
| `page_num` | INT | `chunk_meta.page_num` | nullable → 0 |
| `section_title` | STRING | `chunk_meta.section_title` | |
| `chunk_type` | STRING | `chunk_meta.chunk_type` | |
| `permission_level` | STRING | `chunk_meta.permission_level` | filter field |
| `owner_dept` | STRING | `chunk_meta.owner_dept` | filter field |
| `category_l1` | STRING | `chunk_meta.category_l1` | |
| `category_l2` | STRING | `chunk_meta.category_l2` | |
| `parent_chunk_id` | STRING | `chunk_meta.parent_chunk_id` | |
| `step_no` | INT | `chunk_meta.step_no` | |
| `image_refs_json` | STRING (JSON) | `chunk_meta.image_refs_json` | load-bearing for DingTalk card |
| `source_image` | STRING | derived | only if live schema has it |
| `visual_summary` | STRING | derived | only if live schema has it |
| `is_active` | INT | constant `1` | |
| `kb_type` | STRING | constant `fuling` | |

### 2.4 Determine per-field `multiValueSeparator` (BLOCKER — no full export until resolved)

The OSS+API doc says verbatim: *"多值分隔符默认使用分割符英文逗号进行切分，也可以输入自定义多值分隔符"* — the OSS-source-file default per-value-list separator is **comma**, not `^]`. The actual separator is per-field in the schema.

Do this BEFORE writing any production export:

1. Parse the §2.1 snapshot. For every multi-value field (`dense_vector`, `sparse_vector_indices`, `sparse_vector_values`), extract the exact `multiValueSeparator` key value (the JSON key name in the snapshot is the ground truth — do not guess).
2. If the snapshot enumerates separators: use them per-field in the export. The export writer reads the schema; the separator is NOT a global constant.
3. If the snapshot does NOT enumerate per-field separators (only the field type), run the §4.3 sandbox path (see §4.3 dry-run) with BOTH a comma-separated 10-row file AND a `^]`-separated 10-row file. Empirically establish which the engine accepts. Pin the empirical answer in `scripts/export_chunks_to_oss_ha3.py` and log it in the manifest.
4. The export writer fails fast if a multi-value field's separator is neither `,` nor `^]`.

### 2.5 Code change: thread `table_name` through config (prerequisite, lands BEFORE §4)

Cutover safety depends on this code change landing and stabilizing first.

1. `grep -rn 'fuling_kb_chunks' opensearch_pipeline/ scripts/ dataworks_nodes/` — enumerate every occurrence.
2. For each non-comment occurrence, replace the literal with `get_config().ha3.table_name`.
3. Add a config field `ha3.table_name` defaulting to `fuling_kb_chunks` with env override `RAG_HA3_TABLE_NAME`.
4. Ship as a separate commit + deploy. Run prod for ≥24h with the env var unset (behavior unchanged). Confirm `/api/ask` and DingTalk path log resolved table name == `fuling_kb_chunks`.
5. Only after this stabilizes is §5 validation against `fuling_kb_chunks_v2` meaningful. Without this, §5.4 silently runs against the wrong table.

### 2.6 Sandbox dry-run plan (BLOCKER for §4 production calls)

Provision `fuling_dryrun_<ts>` (vector edition, same schema as snapshot, OSS source). Run the experiments below; each must produce a documented answer before production. Tear down with `delete_table` after.

| Experiment | Question answered |
|---|---|
| E1 | Create OSS-source table with `autoBuildIndex` placed as a TOP-LEVEL request field; GetTable; confirm it returns `autoBuildIndex=false`. If it doesn't stick, where does the SDK accept it? |
| E2 | Create OSS-source table BEFORE uploading any files. Wait 5 min. Does an empty generation build automatically? |
| E3 | Build trigger: after upload, try in order — (a) wait 5 min, no call; (b) `start_table`; (c) `reindex(oss_data_path=..., data_time_sec=None)`; (d) `reindex(oss_data_path=..., data_time_sec=int(now))`. Document the minimal sufficient call. |
| E4 | Promotion model: after build completes, query the sandbox without any extra call. Does it serve immediately, or does a generation need explicit promotion? |
| E5 | Sparse field encoding: write one record with `sparse_vector_indices=12,345,2048` (comma) and one with `^]`-separated. Build, self-query by sparse-only and by dense-only. Record which encoding the engine accepted. |
| E6 | Special-character handling: write one record whose `chunk_text` contains a literal `\x1D` byte. Does the build (a) ingest cleanly, (b) reject cleanly, or (c) silently mis-split? If (c), HA3 format is unsafe for arbitrary Chinese text and we MUST switch to the JSON shape for this corpus. |
| E7 | After E3 succeeds, push 5 NEW chunks via `push_documents` and immediately self-query each by dense vector. **If recall < 100%, realtime push on OSS-source tables has the same forward-only bug as swift — this changes the entire operational model.** See §9.1 conditional plan. |
| E8 | JSON-format alternative: write the same 5 chunks using JSON-lines shape (cmd/fields envelope with native arrays for dense + sparse). Confirm whether the engine accepts it on a vector-edition table. If yes, this is a strictly safer format than HA3 for our corpus (no separator/escape ambiguity). |

The §4 production calls inherit whatever §2.6 empirically establishes. **If any sandbox experiment fails or produces undocumented behavior, open an Alibaba support ticket before proceeding.**

---

## 3. Offline source: OSS, file format

### 3.1 Format choice (HA3 vs JSON) — to be locked by §2.6 E5/E6/E8

Default to HA3 if (E5 confirms the engine accepts the multi-value bytes our schema declares) AND (E6 shows HA3 ingests text safely or our corpus contains zero `\x1C`–`\x1F` bytes). Switch to JSON envelope `{"cmd":"ADD","fields":{...}}` with native arrays if (E6 shows silent mis-splits) OR (E8 confirms JSON works and the team prefers the safer encoding).

Document the locked choice in `scratch/format_decision_<ts>.md` before writing the production export.

### 3.2 HA3 record layout (only valid IF §2.6 confirms it)

| Purpose | Display | Bytes |
|---|---|---|
| Key-value separator | `^_` + `\n` | `\x1F\x0A` |
| Record terminator | `^^` + `\n` | `\x1E\x0A` |
| Multi-value separator | **per-field, from §2.4** | `\x1D` OR `,` per field |

WARNING: the multi-value separator inside a value (e.g. the 1024 dense floats) is the **per-field** value learned in §2.4 — NOT a hardcoded `^]`. The OSS+API doc default is comma. Below, `<MV_SEP_DENSE>` / `<MV_SEP_SPARSE_IDX>` / `<MV_SEP_SPARSE_VAL>` are placeholders resolved at export time from the field schema.

```
CMD=add^_
id=<chunk_id>^_
dense_vector=0.0123<MV_SEP_DENSE>0.0456<MV_SEP_DENSE>...^_      # exactly 1024 floats
sparse_vector_indices=12<MV_SEP_SPARSE_IDX>345<MV_SEP_SPARSE_IDX>2048^_
sparse_vector_values=0.42<MV_SEP_SPARSE_VAL>0.31<MV_SEP_SPARSE_VAL>0.18^_
chunk_text=<utf8 text, control bytes already stripped per §3.4.5>^_
doc_id=<...>^_
version_no=3^_
chunk_index=12^_
page_num=4^_
section_title=<...>^_
chunk_type=text^_
permission_level=internal^_
owner_dept=<...>^_
category_l1=<...>^_
category_l2=<...>^_
parent_chunk_id=<...>^_
step_no=0^_
image_refs_json=<JSON-encoded, no raw control bytes>^_
is_active=1^_
kb_type=fuling^_
^^
```

Encoding: UTF-8, no BOM. Field emission order MUST mirror the live `fieldSchema` order (§2.3).

### 3.3 OSS path layout

From OSS+API doc:

- Directory must start with `/`; must not contain `=`, `&`, `?` etc.; must not be the bucket root.
- The directory must contain **only** data files (no foreign objects).
- Same **region** as the OpenSearch instance is documented. Same **account** is a strong convention but NOT a documented hard rule — verify the configured bucket's owner UID matches the OpenSearch instance's owner UID and abort if they differ (out-of-scope to debug cross-account RAM here).
- The convention "directory name must contain the substring `opensearch`" appears on the ZH page only; the EN page is silent. Keep `opensearch` in our path as a safe convention; don't cite it as a documented hard rule.

Layout:

```
oss://<existing-fuling-bucket>/opensearch/fuling-kb-chunks-rebuild-<YYYYMMDDhhmm>/
    part-0000.data
    part-0001.data
    ...
```

Shard at ~256 MB (conservative — file/total size limits are not documented). `manifest.json` lives **one directory level above** the data directory (foreign files inside the data dir break the load).

### 3.4 Sparse-index hygiene

The docs state indices must be ascending; behavior on **duplicate indices** is undefined. The export writer must:

1. Dedup repeated indices BEFORE sort. Policy: sum-merge values (document the choice in the script header). Log every chunk that required dedup so the embedding pipeline can be investigated.
2. Sort indices strictly ascending; reorder values to match.
3. Assert strict monotonicity (`i[k] < i[k+1]`) before emitting; fail the row if violated.

### 3.4.5 Special-character sanitization (corpus integrity)

Before emitting any record, scan every TEXT/STRING field (`chunk_text`, `image_refs_json`, `section_title`, `visual_summary`, `category_*`, `owner_dept`) for raw `\x1C`/`\x1D`/`\x1E`/`\x1F` bytes. Policy:

- Strip + log to `scratch/sanitization_<ts>.jsonl`. Replace with U+FFFD if context demands a placeholder. Never emit a raw control byte into an HA3 file.
- If the JSON envelope format is chosen, this risk class disappears.

### 3.5 Export script outline (`scripts/export_chunks_to_oss_ha3.py`)

1. Read the §2.1 snapshot; build a `FieldSchema` runtime object with names, order, types, per-field `multiValueSeparator`.
2. `SELECT * FROM chunk_meta WHERE is_active=1 AND kb_type='fuling'`.
3. Load `embedding_cache.json`.
4. For each row: resolve cache; sanitize text fields (§3.4.5); dedup+sort sparse (§3.4); emit one HA3 (or JSON) record with fields in live schema order.
5. On any cache miss / malformed entry: collect; halt if non-empty (re-embed per §2.2a then re-run).
6. Stream-write local shards; upload each to OSS atomically with oss2 `put_object_from_file`.
7. Write `manifest.json` ONE LEVEL ABOVE the data directory: `expected_row_count`, per-file `{name, row_count, sha256}`, `snapshot_ts`, `format` (`ha3` or `json`), `multi_value_separators`, `model='text-embedding-v4'`, `script_git_sha`.
8. Final assertion: emitted rows == active `chunk_meta` count == manifest `expected_row_count`.

---

## 4. Build the new table

### Step 4.0 — Schedule a low-traffic window

Schedule §4.1–§4.4 in the lowest-traffic window (per `qa_session_log`, expected 02:00–05:00 CST for an internal corporate DingTalk bot). Document an abort criterion: if production `fuling_kb_chunks` p99 latency degrades by >2x during the new-table build, `stop_table` on the new table immediately.

### Pre-step guard for every mutating call

A helper that all §4 mutating call sites use:

```python
LIVE_TABLE_ALIASES = {"fuling_kb_chunks"}

def assert_not_live(table_name: str, op: str) -> None:
    if table_name in LIVE_TABLE_ALIASES:
        raise RuntimeError(f"REFUSED {op} on live table {table_name}")
    audit_log.info({"op": op, "table_name": table_name, "ts": now()})
```

Every `create_table`, `reindex`, `modify_table`, `stop_table`, `delete_table` in §4 calls `assert_not_live` first. This is the single guard against the typo class that re-creates the original outage.

### Step 4.1 — Upload OSS data FIRST ⚠️ (writes OSS objects; touches no HA3 table)

Reordered from the draft: the OSS upload happens BEFORE `create_table`. Rationale: even if a future SDK version misinterprets `autoBuildIndex` and auto-builds on table create, it now builds against a complete corpus rather than an empty directory. List the OSS prefix after upload completes and assert: every expected shard is present, no foreign objects, total bytes match the manifest. The manifest lives one directory above the data dir.

### Step 4.2 — Provision new sibling table ⚠️

```python
# ⚠️ MUTATES — creates fuling_kb_chunks_v2; does NOT touch fuling_kb_chunks
assert_not_live(new_table, "create_table")
new_table = "fuling_kb_chunks_v2"   # versioned, NOT a "tmp" name

req = CreateTableRequest(
    table_name=new_table,
    partition_count=2,
    primary_key="id",
    field_schema=<verbatim from §2.1 snapshot, same field ORDER>,
    vector_index=<verbatim from §2.1 snapshot>,
    auto_build_index=False,          # TOP-LEVEL, NOT nested under data_source
    data_source={
        # Key names + casing copied verbatim from the §2.1 snapshot.
        # Per Alibaba API reference the documented credential keys are
        # `accessKey` / `accessSecret`, NOT `accessKeyId` / `accessKeySecret`.
        # Defer to the snapshot.
        "type": "oss",
        "ossPath": "/opensearch/fuling-kb-chunks-rebuild-<ts>/",
        "bucket": "<fuling-bucket>",
        "accessKey": "<from RAM secret>",
        "accessSecret": "<from RAM secret>",
        # Add `endpoint` and any other keys if present in the live snapshot.
    },
    data_process_config=[],
)
cli.create_table(req)
```

**Immediately after `create_table` returns** (in this order, all reads):

1. `cli.get_table(new_table)` → assert returned `autoBuildIndex == False`. If missing or `True`, immediately `stop_table` before any build can fire, then investigate.
2. `cli.get_table(new_table)` → deep-diff `fieldSchema` and `vectorIndex` against the §2.1 snapshot. Allowed diffs: `dataSource` block, internal ids, timestamps. Any other diff is a hard stop → `delete_table` the new table and re-investigate.
3. `cli.get_table(new_table)` → assert `dataSource.type == "oss"` (case-exact) and `dataSource.ossPath` equals the configured directory. **If the SDK silently fell back to `swift` or `api`, do not proceed to reindex — the wrong-source-type hazard is what corrupted us last time.**

### Step 4.3 — Trigger full build from OSS ⚠️

```python
# ⚠️ MUTATES — triggers build on fuling_kb_chunks_v2 only
assert_not_live(new_table, "reindex")

# Dump SDK signature at runtime to catch undocumented required kwargs
import inspect
print(inspect.signature(ReindexRequest.__init__))

# Use whatever E3 in §2.6 empirically established as the minimal sufficient call.
# Two candidate forms (one of them was validated in sandbox):
req = ReindexRequest(oss_data_path="oss://<bucket>/opensearch/fuling-kb-chunks-rebuild-<ts>/")
# OR:
# req = ReindexRequest(oss_data_path="...", data_time_sec=int(time.time()))

cli.reindex(table_name=new_table, request=req)
```

**Pre-execution verification (required, gated):**

- E3 in §2.6 must have established which form actually triggers a build on OSS-source vector-edition tables. If E3 was inconclusive (e.g. both forms accepted, neither produced a built generation), DO NOT run §4.3 in production — open an Alibaba ticket.
- The `data_time_sec` decision rests on E3. The published reindex doc says "Timestamp" is mentioned for OSS reindex but does not enumerate request semantics; we trust empirical sandbox output over the doc here.

### Step 4.4 — Watch the build to completion

Poll `list_table_generations` + `get_table_generation` every 60s. Replace the load-bearing `IN_USE` string check with a **functional** definition derived from §2.6 E4:

```python
def is_serving(table: str) -> bool:
    s = cli.stats(table_name=table)
    if s.doc_count == 0:
        return False
    # functional serving test: a known sandbox-style probe must round-trip
    probe = run_self_query_probe(table)
    return probe.rank1_is_self and probe.score >= 0.99
```

Log the actual generation `status` enum string observed (so we update operator knowledge). If E4 established that explicit promotion is required, call the promotion API discovered in E4 before checking `is_serving`.

While the build runs: monitor instance-level CPU/IO and the LIVE table's p99. Documented abort: if `fuling_kb_chunks` p99 exceeds 2x baseline for ≥5 min, call `stop_table(new_table)` to relieve the instance, reschedule for a quieter window.

---

## 5. VALIDATE-BEFORE-SWAP — exhaustive checklist

The serving config still points at `fuling_kb_chunks` throughout this section.

### 5.0 Pre-validation gate

Re-run `is_serving(fuling_kb_chunks_v2)` from §4.4. If false, do NOT run §5.1–§5.7 — diagnose the build first (an unpromoted generation can mimic an empty index).

### 5.1 Self-query health check — dense (the deterministic gate)

```python
sample = random.sample(active_chunks, 100)
fails = []
for c in sample:
    dense  = embedding_cache[c.key].dense
    fresh  = dashscope_embed_native(c.chunk_text).dense   # NEW: also test fresh embed
    hits_c = retriever_v2.knn_only(dense_vector=dense, top_k=2)
    hits_f = retriever_v2.knn_only(dense_vector=fresh, top_k=2)
    if not hits_c or hits_c[0].id != c.id or hits_c[0].score < 0.99:
        fails.append(("cache", c.id))
    if not hits_f or hits_f[0].id != c.id or hits_f[0].score < 0.99:
        fails.append(("fresh", c.id))
    # Anti-trivial check: rank-2 must be a different chunk with score < 0.99
    if len(hits_c) > 1 and (hits_c[1].id == c.id or hits_c[1].score >= 0.99):
        fails.append(("trivial", c.id))
assert not fails
```

Pass criterion: 100/100 cache self-query rank-1==self with score≥0.99 AND 100/100 fresh-embed self-query rank-1==self with score≥0.99 AND rank-2 is a different chunk with score < 0.99. The fresh-embed leg is what catches DashScope model drift between cache time and now — without it the gate degenerates to "data we wrote read back correctly," which is the same failure mode that broke prod.

### 5.1.5 Self-query health check — sparse

Production retrieval is Dense+Sparse in the kNN path. A built dense index with an empty sparse index passes §5.1 but degrades production. For the same 100-sample, issue a sparse-only query (zero dense, send only `sparse_vector_indices` + `sparse_vector_values`). Pass criterion: ≥95/100 rank-1==self with score above a documented sparse-only threshold (sparse is intrinsically lossier than lossless-Linear-dense, so 100/100 is too tight; <95/100 indicates the sparse field schema wrote/built wrong).

### 5.2 Cross-doc semantic spot-check

20 known paraphrase pairs from `tests/eval/*.md`. For each, query the paraphrase against the new table; source doc must appear in top-10. Bias the sample toward chunks that were re-embedded in §2.2a (these are the ones most exposed to drift).

### 5.3 BM25 leg regression check

20 keyword-bearing queries that work today (e.g. `触电应急`). Text-only against the new table; top-1 must match production.

### 5.4 Hybrid 3-way parity check

Now safe because §2.5 has landed and `RAG_HA3_TABLE_NAME` is honored. Override `RAG_HA3_TABLE_NAME=fuling_kb_chunks_v2` for the harness. The harness MUST log the resolved table name in every query path and assert it equals `fuling_kb_chunks_v2` — fail loud if not. Compare answer doc-set overlap vs. production for 50 representative queries: dense-leg-dependent queries IMPROVE; keyword-dominant queries unchanged within fusion noise.

### 5.5 Permission-filter regression

10 queries with `permission_level` filters reflecting real bot traffic. Same docs admitted/rejected as on production. Dept-value injection probe must still be blocked.

### 5.6 Stats sanity

```python
stats_new = cli.stats(table_name=new_table)
assert stats_new.doc_count == manifest["expected_row_count"]   # exact, not approx
assert stats_new.vector_index_segment_size > 0
assert stats_new.bm25_index_segment_size > 0
```

### 5.7 End-to-end serving-stack dry-run (NEW — blocks cutover)

Stand up a staging copy of the SAE service (or a local FastAPI) configured with `RAG_HA3_TABLE_NAME=fuling_kb_chunks_v2` — full `retrieve_and_enrich` + LLM + `content_blocks_builder` + DingTalk path, not just `retriever.knn_only`. Run the existing `text_quality_eval.py` harness (51 queries / 16 docs incl PDFs, 3-judge panel). Required outcomes:

- Score-threshold labels (`高/中/低` from `score_threshold_high=8.0`, `medium=5.0`) distribute reasonably. If Linear-built scores compress into a narrower range than HNSW, **recalibrate `score_threshold_*` before cutover** — do not silently mislabel chunks to the LLM.
- Step-card expansion renders (parent + child chunks via `parent_chunk_id`).
- Neighbor stitching (±1 from RDS) joins correctly using `chunk_index`.
- Permission filter rejects one `restricted` and one `internal` mismatch.
- DingTalk image attachment surfaces an `image_refs_json` blob round-tripped through HA3 with no escaping damage.

If any check fails: STOP. Do not cutover. The production table is untouched.

---

## 6. Cutover ⚠️

### 6.1 Pre-deploy smoke + flip the env var

1. Re-run a smoke version of §5.1 (10-chunk self-query, both cache and fresh) and §5.3 (5 BM25 queries) against `fuling_kb_chunks_v2`. Gate the deploy on these passing.
2. Flip `RAG_HA3_TABLE_NAME=fuling_kb_chunks_v2` in SAE config; rolling restart.
3. Re-run the same smoke immediately after deploy completes. If it regresses, revert config and redeploy.

### 6.2 First-hour observation (named metrics)

| Metric | Threshold to rollback | Source |
|---|---|---|
| p99 `/api/ask` latency | > 1.5x baseline sustained ≥5 min | SAE alerting |
| Empty-answer rate | > 2x baseline sustained ≥5 min | `qa_session_log` |
| Thumbs-down rate | > 1.5x baseline over ≥30 sessions | `qa_session_log` + `feedback` |
| 5xx in `/api/ask` traceable to retrieval | any | error tracker |

Owner: on-call human for the first hour, then SAE alerts.

### 6.3 Dual-write window (NEW — rollback-safety)

For the **first 7 days post-cutover**, DAG-3 `node_push_to_index` writes to **both** `fuling_kb_chunks` and `fuling_kb_chunks_v2` (chunk id is the dedup key — duplicate-write cost is minor). This is the change to §9.3 that makes the rollback in §7 truly zero-state-loss for the full 7-day hold. If dual-write fails on either table, the DAG fails (preserves the "never disappear from index" invariant).

---

## 7. ROLLBACK plan

### 7.1 Triggers

Any of §6.2's red thresholds, OR any `/api/ask` regression with a clear retrieval signature.

### 7.2 Mechanics

Revert `RAG_HA3_TABLE_NAME=fuling_kb_chunks`; SAE rolling restart.

### 7.3 State

- Within the 7-day dual-write window (T0..T0+7d): **zero-state-loss** because both tables received every DAG-3 push.
- Beyond T0+7d, after dual-write is turned off (§9.3): rollback requires either (i) replaying DAG-3 writes since the dual-write stopped, OR (ii) accepting some staleness on the old table. Add `scripts/replay_dag3_to_table.py` as a required artifact (idempotent push from `chunk_meta` keyed on `updated_at >`).

### 7.4 Cleanup for v2-served sessions

Tag `qa_session_log` rows produced during the v2-served window with `served_from_table=fuling_kb_chunks_v2`. If rollback fires, exclude these rows from quality dashboards and from feedback-as-ground-truth datasets (the answers may have been against a malformed index).

---

## 8. DO-NOT list (rationale tied to research)

- ⚠️ **Do not call `cli.reindex(ReindexRequest(data_time_sec=now))` on `fuling_kb_chunks` or any swift-fed table.** This is the documented behavior that produced the original outage.
- ⚠️ **Do not flip `dataSource.type` on the live `fuling_kb_chunks` via `modify_table`.** Undocumented; moved out to a separately-reviewed last-resort runbook.
- ⚠️ **Do not delete `fuling_kb_chunks` until `fuling_kb_chunks_v2` has served production cleanly for ≥7 days AND a rollback drill has succeeded.** Use `stop_table` first, then hold STOPPED for 14 days before `delete_table`.
- ⚠️ **Do not set `autoBuildIndex=true` on the new table until all OSS shards have finished uploading.** Race risk with partial generations.
- ⚠️ **Do not include `manifest.json` or any non-data file INSIDE the OSS data directory.** Documented constraint.
- ⚠️ **Do not hardcode `^]` as the multi-value separator.** Read per-field from the schema snapshot (§2.4). The OSS+API doc's default is comma.
- ⚠️ **Do not skip the sparse-index ascending sort or duplicate-index handling.** Silent recall failure if violated.
- ⚠️ **Do not use OpenAI-compat mode for re-embedding.** It drops the sparse vector.
- ⚠️ **Do not touch the DAG-3 deactivation step (`node_deactivate_old_chunks`) ordering.** The "never disappear from index" invariant is independent of this rebuild.
- ⚠️ **Do not run mutating §4 calls without `assert_not_live`.** The single typo guard.
- ⚠️ **Do not skip §2.6 sandbox dry-run.** Multiple format/build-trigger questions are unanswered by public docs; sandbox is the only ground truth.

---

## 9. Operational details and edges

### 9.1 Realtime push during the rebuild window

DAG-3 continues to target `fuling_kb_chunks` only during the rebuild window. New chunks between `snapshot_ts` and cutover are missing from `fuling_kb_chunks_v2`. **Critical dependency:** §2.6 E7 must establish whether `push_documents` against an OSS-source table produces a built HNSW segment OR has the same forward-only bug as swift. Two cases:

- **E7 PASSES (push_documents builds the vector segment too):** post-cutover delta push from `chunk_meta` where `updated_at > snapshot_ts AND is_active=1` into `fuling_kb_chunks_v2`. Then start dual-write (§6.3) from cutover onward.
- **E7 FAILS (push_documents is forward-only on OSS-source tables too):** the entire operational model changes. DAG-3 cannot realtime-push to the OSS-source table without re-creating the original bug, just slower. Plan B: DAG-3 only updates RDS during the week; a weekly cron runs `scripts/export_chunks_to_oss_ha3.py` + `reindex(oss_data_path=...)` to refresh the vector segment. This is a strict operational regression and means the rebuild fixes one outage but commits the team to weekly rebuild cadence. **Do not cutover until E7's answer is known.**

### 9.2 `autoBuildIndex` re-enable

Defer until cutover stabilizes ≥24h. If a weekly OSS-export pipeline exists (per §9.1 plan B), `modify_table` to set `autoBuildIndex=true` so weekly OSS uploads auto-trigger. Otherwise leave false and trigger rebuilds explicitly.

### 9.3 When to stop dual-write

After cutover is stable ≥7 days AND a rollback drill has succeeded, stop dual-write. From that point, DAG-3 targets `fuling_kb_chunks_v2` only.

### 9.4 `is_embedding_saved`

Defer to post-cutover. Not on the critical path.

---

## 10. (B) MODIFY path

**Moved out of this plan into a separately-reviewed last-resort runbook.** This document intentionally contains no executable code blocks targeting `fuling_kb_chunks` for mutation. If Path B is ever genuinely needed (e.g. hard instance-level table-name pinning by an external integration), it requires a fresh plan, fresh review, and fresh approvals. Do NOT copy-paste from a draft of Path B onto the live table under pressure.

---

## 11. Concrete artifacts this plan produces

- `scratch/ha3_snapshot_<ts>.json` — verbatim GetTable / list_generations / stats output.
- `scratch/missing_<ts>.json` + `scratch/malformed_<ts>.json` — embedding-cache integrity reports.
- `scratch/rebuild_provenance_<ts>.json` — per-chunk re-embedding model/endpoint/timestamp.
- `scratch/sanitization_<ts>.jsonl` — list of records with stripped control bytes.
- `scratch/format_decision_<ts>.md` — HA3 vs JSON choice + per-field MV separator decisions.
- `scripts/export_chunks_to_oss_ha3.py` — export utility (or `_json.py` if JSON wins).
- `scripts/replay_dag3_to_table.py` — idempotent push from `chunk_meta` for rollback-state-recovery.
- `oss://<bucket>/opensearch/fuling-kb-chunks-rebuild-<ts>/part-*.data` — the corpus.
- `oss://<bucket>/opensearch/manifest-<ts>.json` — placed ONE level above data dir.
- `scratch/self_query_validation_<ts>.json` — §5.1 + §5.1.5 + §5.2 report.
- `tests/rebuild_v2/bot_parity_<ts>.md` — §5.4 parity-check results.
- `tests/rebuild_v2/serving_e2e_<ts>.md` — §5.7 end-to-end stack dry-run.

---

## 12. References

1. [OSS+API data source (ZH)](https://help.aliyun.com/zh/open-search/vector-search-edition/oss-api-data-source) — HA3 + JSON formats; default per-value-list multi-value separator is comma; path constraints; same-region rule.
2. [Hybrid search best practices (ZH)](https://help.aliyun.com/zh/open-search/vector-search-edition/hybrid-search-best-practices) — sparse field types (uint32 + float multi-value), ascending-indices rule.
3. [Index rebuild — vector edition (EN)](https://www.alibabacloud.com/help/en/open-search/vector-search-edition/index-rebuild) — "Reindexing from an API data source ... clears all existing data"; 3-day swift backfill.
4. [Restore data from an index version](https://help.aliyun.com/zh/open-search/vector-search-edition/restore-data-from-an-index-version) — preservation requires every field be stored/summary; silent on sparse multi-value.
5. [Change a data source — industry algorithm edition](https://help.aliyun.com/zh/open-search/industry-algorithm-edition/change-a-data-source) — different edition, UI/offline only.
6. [Vector index parameters](https://help.aliyun.com/zh/open-search/retrieval-engine-edition/vector-indexes/) — `linear_build_threshold`, `min_scan_doc_cnt` definitions.
7. [Havenask issue #167](https://github.com/alibaba/havenask/issues/167) — forward field returns vectors, MATCHINDEX returns 0; direct symptom match.
8. [Vector-search product overview](https://help.aliyun.com/zh/open-search/vector-search-edition/vector-search-product-overview/) — multi-version isolation guarantees.
9. [ModifyTable API (EN)](https://www.alibabacloud.com/help/en/open-search/developer-reference/api-searchengine-2021-10-25-modifytable) — body fields; `dataSource` modifiable but child-key mutability not enumerated.
10. [Reindex API (EN)](https://www.alibabacloud.com/help/en/open-search/developer-reference/api-searchengine-2021-10-25-reindex) — `dataTimeSec` semantics; `ossDataPath` mentioned; OSS-source request shape not fully enumerated.

---

## 13. Confidence & boundaries

### Verified (with citations)

- API-source `reindex` clears prior data → ref [3].
- Swift retention default short (~3 days) → ref [3].
- Sparse vector field schema (uint32 indices + float values multi-value, ascending) → ref [2].
- `linear_build_threshold` definition → ref [6]. *Strict "LinearSearcher is brute-force lossless" not directly quoted in the doc — see "must confirm" below.*
- Forward-field-OK + MATCHINDEX-empty failure mode is a real Havenask bug → ref [7].
- OSS+API supports HA3 OR JSON envelope formats; default multi-value separator is comma → ref [1].
- OSS data directory must contain only data files and not be bucket root → ref [1].
- Multi-version isolation (old generation keeps serving while new builds) → ref [8].

### Must confirm before executing (treat as low-confidence until sandbox-validated in §2.6)

1. **§2.6 E1**: exact placement of `autoBuildIndex` in CreateTableRequest (top-level vs nested under `dataSource`).
2. **§2.6 E2**: whether OSS-source table-create alone triggers a build if files are already present.
3. **§2.6 E3**: minimal sufficient build trigger for OSS-source vector-edition tables (`create_table` alone? `start_table`? `reindex` with `oss_data_path`? with or without `data_time_sec`?).
4. **§2.6 E4**: whether new-table OSS rebuild auto-promotes to serving, or requires explicit promotion.
5. **§2.6 E5**: exact per-field `multiValueSeparator` (comma vs `^]`) for `dense_vector`, `sparse_vector_indices`, `sparse_vector_values` on THIS table.
6. **§2.6 E6**: HA3 behavior on raw `\x1C`/`\x1D`/`\x1E`/`\x1F` bytes embedded in text values (ingest / reject / silent mis-split).
7. **§2.6 E7**: whether `push_documents` against an OSS-source vector-edition table produces a built HNSW segment, or has the same forward-only bug as swift-source. **This determines whether realtime ingestion can continue, or whether we are committed to weekly OSS rebuilds.**
8. **§2.6 E8**: whether JSON envelope `{"cmd":"ADD","fields":{...}}` with native arrays is accepted on vector-edition.
9. **Exact dataSource JSON key casing** for the OSS source (`accessKey` vs `accessKeyId`, `ossPath` casing, `endpoint` presence). Resolve by reading the live table's snapshot.
10. **Generation `status` enum values** (e.g. `IN_USE` vs `READY` vs `ONLINE`). Replaced in §4.4 with a functional `is_serving` check, but log the observed string to update operator knowledge.
11. **Whether LinearSearcher at <`linear_build_threshold` is exhaustive at query time** (the §5.1 100/100 self-query gate rests on this). If non-exhaustive, treat self-query≥0.99 as a strong signal but not a mathematical guarantee; lean on §5.2's paraphrase recall as the more meaningful check.
12. **§9.3 dual-write idempotency on `push_documents`** — confirm that pushing the same chunk id twice (once per table) is safe and doesn't corrupt a previously-pushed doc.
13. **OSS cross-account support** for the data source — assert same-UID before assuming it works.
14. **§2.5 codebase enumeration**: every site that hardcodes `fuling_kb_chunks` is actually surfaced by `grep` and refactored to config-driven. Confirm by deploying the no-op refactor and watching prod for 24h before §4 starts.

### Operational boundaries

- This plan does NOT cover: chunking changes, embedding-model changes, retrieval fusion-weight changes, prompt changes, permission-filter logic changes.
- This plan is index-layer ONLY. If the §0 score-threshold investigation reveals the LLM prompt needs recalibration, that is a separate change with its own rollout.
- If §2.6 E7 fails, this plan's "rebuild once, then resume normal realtime push" model is wrong, and the team must commit to either a weekly OSS-export+rebuild cadence OR migrate off the OSS-source data source type entirely. Both are out-of-scope here.
