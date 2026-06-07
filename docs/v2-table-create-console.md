# Create `fuling_kb_chunks_v2` via HA3 Console — exact spec

> **Why console:** the SDK's CreateTable API doesn't accept TEXT-field analyzer config (the `fieldSchema` dict format rejects `"TEXT"`). The console UI has analyzer dropdowns and writes the richer underlying schema. Once v2 is created and built, all subsequent ops (validate, cutover) drive from the SDK.
>
> **What this changes vs the live table:** ONE thing — `dataSource.type` goes from `swift` → `oss`, which enables a real offline HNSW build. Schema and HNSW params are byte-identical to live.

## Pre-flight (already done — don't re-run)
- ✅ Embedding cache complete: 3,669/3,669 chunks have correct dense+sparse vectors (drift cos=1.000000 vs fresh DashScope).
- ✅ Data exported: `oss://fuling-knowledge-base/opensearch/fuling-kb-chunks-v2-20260606_215101/data.json` (91 MB, JSONL).
- ✅ Live snapshot: `scratch/preflight_20260606_213109/ha3_snapshot.json` (the authoritative schema mirror).

## Console steps

In the **OpenSearch 向量检索版** console for instance `ha-cn-kgl4slr1n01`:

### 1. New table — basics
| Field | Value |
|---|---|
| 表名 (Table name) | **`fuling_kb_chunks_v2`** |
| 分片数 (Partitions) | **2** |
| 主键 (Primary key) | **`id`** |

### 2. Field schema (23 fields, exact mirror of live)

Add each field below. **Pay extra attention to `chunk_text` (TEXT type — needs an analyzer)** and the multi-value fields.

| Field | Type | Notes |
|---|---|---|
| `id` | INT64 | Primary key |
| `chunk_id` | STRING | |
| `doc_id` | STRING | |
| `version_no` | INT64 | |
| `chunk_index` | INT32 | |
| `page_num` | INT32 | |
| `section_title` | STRING | |
| `chunk_type` | MULTI_STRING | Multi-value |
| **`chunk_text`** | **TEXT** | **Analyzer: `chn_standard`** (Chinese tokenizer — BM25 indexed). If `chn_standard` isn't offered, use `aliws` or `chinese_simple` — whichever the UI provides. Live used a Chinese analyzer; pick the same one. |
| `chunk_text_store` | STRING | Storage copy (returned in query results) |
| `dense_vector` | MULTI_FLOAT | 1024 dims (set in vector index, not here) |
| `sparse_vector_indices` | MULTI_UINT32 | |
| `sparse_vector_values` | MULTI_FLOAT | |
| `permission_level` | STRING | Filter field |
| `owner_dept` | STRING | Filter field |
| `category_l1` | STRING | |
| `category_l2` | STRING | |
| `is_active` | INT64 | |
| `kb_type` | STRING | |
| `title` | STRING | From document_meta JOIN |
| `source_url` | STRING | |
| `source_image` | STRING | Multimodal |
| `visual_summary` | STRING | Multimodal |

### 3. Vector index

| Field | Value |
|---|---|
| 索引名 (Index name) | `dense_vector` |
| 向量字段 (Vector field) | `dense_vector` |
| 维度 (Dimension) | **1024** |
| 距离度量 (Distance type) | **InnerProduct** |
| 索引类型 (Index type) | **HNSW** |
| Sparse index field | `sparse_vector_indices` |
| Sparse value field | `sparse_vector_values` |

**Advanced parameters** (mirror live exactly):
- `linearBuildThreshold`: **5000**
- `minScanDocCnt`: **20000**
- `buildIndexParams` (JSON):
  ```json
  {"proxima.hnsw.builder.max_neighbor_count":100,"proxima.hnsw.builder.efconstruction":500,"proxima.hnsw.builder.enable_adsampling":true,"proxima.hnsw.builder.slack_pruning_factor":1.1,"proxima.hnsw.builder.thread_count":16}
  ```
- `searchIndexParams` (JSON):
  ```json
  {"proxima.hnsw.searcher.ef":400,"proxima.hnsw.searcher.dynamic_termination.prob_threshold":0.7}
  ```

### 4. Data source — **OSS** (this is the change vs live)

| Field | Value |
|---|---|
| 数据源类型 (Type) | **OSS + API** (NOT Swift/API-push) |
| Bucket | **`fuling-knowledge-base`** |
| OSS endpoint | `oss-cn-hangzhou-internal.aliyuncs.com` (the engine uses internal endpoint) |
| **OSS Path** | **`/opensearch/fuling-kb-chunks-v2-20260606_215101/data.json`** |
| 文件格式 (Data Format) | **JSON** (one `{"cmd":"add","fields":{...}}` per line) |
| autoBuildIndex (自动重建) | **Set to whatever the console asks for the FIRST build to fire automatically.** If there's a "build now" button instead, set autoBuildIndex=`false` and trigger build manually after create. |

### 5. Create + watch the build

Hit **Create** (创建表). The console will provision the table, fetch the OSS file, and start the HNSW build. Expected timing for 3,669 docs (well under `linearBuildThreshold=5000` so it'll use exact brute-force search) — **~5 minutes** based on the prior failed-rebuild generation that took ~10 min on the same dataset size.

The new table's `status` should go: `NEW` → `RESTORE_USE` → `IN_USE`. Its newest generation's status should go: `building` → `ready`.

## When v2 status is `IN_USE`, ping me

I'll run the validation:
1. **Self-query gate**: 100 random chunks, query each by its own dense vector → must rank-1 @ ≥0.99 (this is what proves the HNSW segment is actually built). Same for sparse.
2. **BM25 regression**: 20 keyword-bearing queries → top-1 matches the live table's top-1.
3. **Bot end-to-end**: `bot_query_test.py "触电了怎么应急处理"` against v2 → 触电 SOP ranks #1 with a real answer.
4. **Stats sanity**: `docCount == 3669` exactly; segmentCount > 0 for both partitions.

If all four pass, we proceed to cutover. If any fail, the new table is unused (live `fuling_kb_chunks` is untouched throughout) and we diagnose with zero impact.
