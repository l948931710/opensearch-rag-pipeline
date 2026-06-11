# HA3 staging 表建表规格（fuling_kb_chunks_stg）

> 从生产表 `fuling_kb_chunks` 实时导出（2026-06-10，只读 get_table）。
> API 创建返回空 400（疑似实例表数配额或管控权限），控制台建表时照此配置。
> 注意：若实例规格表数上限=1，需先升配或改用『版本号复用生产表』方案（见文末）。

- 分区数 partitionCount: **2**
- 主键 primaryKey: **id**
- 数据处理资源 dataProcessorCount: **2**
- 数据源: API 推送（autoBuildIndex=true）

## 字段（23 个）

| 字段 | 类型 |
|---|---|
| version_no | INT64 |
| owner_dept | STRING |
| chunk_index | INT32 |
| sparse_vector_indices | MULTI_UINT32 |
| section_title | STRING |
| is_active | INT64 |
| chunk_text_store | STRING |
| kb_type | STRING |
| page_num | INT32 |
| title | STRING |
| chunk_text | TEXT |
| doc_id | STRING |
| category_l1 | STRING |
| source_url | STRING |
| chunk_id | STRING |
| dense_vector | MULTI_FLOAT |
| category_l2 | STRING |
| permission_level | STRING |
| visual_summary | STRING |
| sparse_vector_values | MULTI_FLOAT |
| source_image | STRING |
| id | INT64 |
| chunk_type | MULTI_STRING |

## 向量索引

- indexName/vectorField: **dense_vector** | 类型 **HNSW** | 维度 **1024** | 距离 **InnerProduct**
- sparse: indices=**sparse_vector_indices** values=**sparse_vector_values**
- buildIndexParams: `{"proxima.hnsw.builder.max_neighbor_count":100,"proxima.hnsw.builder.efconstruction":500,"proxima.hnsw.builder.enable_adsampling":true,"proxima.hnsw.builder.slack_pruning_factor":1.1,"proxima.hnsw.builder.thread_count":16}`
- searchIndexParams: `{"proxima.hnsw.searcher.ef":400,"proxima.hnsw.searcher.dynamic_termination.prob_threshold":0.7}`
- linearBuildThreshold: 5000 | minScanDocCnt: 20000

## 若表配额不足的替代方案

复用生产表 + STAGING 专属 doc_id/版本段隔离不可取（检索面混居）；
正确替代：升配实例 或 接受 STAGING 检索走本地 OpenSearch（牺牲引擎一致性，标注效度边界）。
