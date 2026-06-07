# HA3 / OpenSearch 向量检索版 — Vector Index Config Reference

Authoritative field reference for the `dense_vector` index, distilled from Alibaba's
[向量索引通用配置](https://help.aliyun.com/zh/open-search/vector-search-edition/vector-index-general-configuration)
doc + our **verified live config** + hard-won empirical findings. Read this before creating/editing any vector table.

## ⭐ The #1 trap: distance type ↔ query sort `order` (NOT in the official doc)

The doc defines the **score semantics** per distance type, but does **NOT** document the query-side `order`
parameter. You must derive it — getting it wrong silently returns the *worst* matches first (this cost us a
multi-hour false "HNSW is empty" investigation — see gotcha G29):

| distance_type | score meaning (doc, verbatim) | ⇒ correct query `order` |
|---|---|---|
| **InnerProduct** | "Higher score = more similar" | **`order="DESC"`** |
| **Cosine** | "[-1,1]; higher = more similar; 1=identical" | **`order="DESC"`** |
| **SquaredEuclidean** | "Lower score = more similar; 0 = exact match" | `order="ASC"` |

**Our index is `InnerProduct` → every `QueryRequest`/`SearchRequest` MUST pass `order="DESC"`.** The HA3 SDK
defaults to ascending; without DESC, an InnerProduct self-query buries the score-1.0 match at the bottom.
Production `retriever.py` correctly sets `order="DESC"` (search_chunks ~L352, expand ~L507). Any diagnostic
must do the same (`scripts/diag_ann_selfquery.py` is fixed). **A self-query that doesn't return itself @~1.0 →
check `order` FIRST**, before suspecting the index/build/data.

## Live `fuling_kb_chunks` dense_vector config (verified via GetIndex — mirror this)

```
vector_index_type   = HNSW
distance_type       = InnerProduct          # ⇒ query order=DESC; vectors are unit-normalized (text-embedding-v4)
dimension           = 1024
builder_name        = HnswBuilder ; searcher_name = HnswSearcher
major_order         = col
enable_rt_build     = true                  # realtime push indexed into the searchable index
enable_hybrid_vector= true                  # dense + sparse hybrid in the kNN path
ignore_invalid_doc  = true                  # invalid vectors silently skipped (dimension/NaN/empty)
is_embedding_saved  = false                 # original vectors not persisted (default)
embedding_delimiter = ","
linear_build_threshold = 5000               # per-SHARD: <5000 docs/shard ⇒ forced Linear (exact) instead of HNSW
min_scan_doc_cnt    = 20000
build_index_params  = {"proxima.hnsw.builder.max_neighbor_count":100,"proxima.hnsw.builder.efconstruction":500,
                       "proxima.hnsw.builder.enable_adsampling":true,"proxima.hnsw.builder.slack_pruning_factor":1.1,
                       "proxima.hnsw.builder.thread_count":16}
search_index_params = {"proxima.hnsw.searcher.ef":400,"proxima.hnsw.searcher.dynamic_termination.prob_threshold":0.7}
rt_index_params     = {"proxima.oswg.streamer.segment_size":2048}
sparse_index_field  = sparse_vector_indices ; sparse_value_field = sparse_vector_values
```
(With ~3669 docs / 2 partitions ≈ 1835/shard < 5000, the engine actually uses **Linear (exact)** per shard —
so a correctly-ordered self-query is guaranteed rank-1 @ ~1.0. The `proxima.hnsw.searcher.ef` is moot at this
scale; it matters only once a shard exceeds `linear_build_threshold`.)

## Documented fields (from the 通用配置 doc)

- **dimension** — feature count; must equal the embedding model's output dim exactly. Memory scales with it.
- **distance_type** — `Cosine` / `InnerProduct` / `SquaredEuclidean` (see table above for score+order).
- **vector_index_type / 算法** — `FLAT`(Linear) / `HNSW` / `HNSW_RaBitQ` / `CagraHNSW` / `HNSW_SQ`(QGraph) /
  `IVF_SQ8` / `DiskANN`. Choose by data scale + latency. (We use HNSW; small data falls back to Linear, above.)
- **linear_build_threshold** (default 5000) — "当一个分片内的数据量小于此阈值时，系统将强制使用 Linear 算法"
  (per-shard; below it → exact brute-force, ef irrelevant).
- **ignore_invalid_doc** — `true`: skip anomalous records (dimension mismatch / empty) and continue;
  `false`: fail the build on the first bad vector. ⚠️ `true` (default) hides bad data — but it only drops
  *invalid* vectors; valid vectors are NOT affected (so it did NOT cause our issue).
- **enable_rt_build (实时索引)** — `true`: API/push data is indexed immediately; `false`: offline-only.
- **rt_index_params (实时索引参数)** — e.g. `{"proxima.oswg.streamer.segment_size":2048}` (realtime segment size).
- **search_index_params (实时检索参数)** — e.g. `{"proxima.hnsw.searcher.ef":N}`; ef range ~`k`→4096 (recall vs latency).
- **embedding_delimiter (向量分隔符)** — default `,`; separates floats in *string-formatted* vectors (OSS-file path).
- **is_embedding_saved** — "是否保存原始向量", default false. Documented to be **required only when INT8/FP16
  quantization AND realtime are both on** (else batch incremental build fails). We have neither quantization,
  so it's not relevant.

## Not in the doc — known empirically (this project)
- The **query `order` param** and its dependence on `distance_type` (above) — the single most important gap.
- **Realtime-push format ≠ OSS-file format** (push = lowercase `cmd` + `fields` wrapper + numeric arrays;
  OSS file = uppercase `CMD` + flat + string arrays). See gotcha G25.
- **`reindex` on an API/swift source is destructive** (clears data, ~3-day recovery). See G20/G26.
- Changing schema / `is_embedding_saved` / distance_type / dim generally **requires recreate or rebuild**, not
  in-place modify (`modify_table` on those errors `ModifyVariableNotAllowed`).
- Diagnose stuck builds with **`cli.list_tasks(ListTasksRequest())`** — node-level FSM status (G28).
