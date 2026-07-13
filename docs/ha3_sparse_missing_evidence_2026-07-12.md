# HA3 向量检索版 — sparse 倒排缺行证据（工单附件）

- **实例**：`ha-cn-kgl4slr1n01`（cn-hangzhou，向量检索版）
- **表**：`fuling_kb_chunks`（数据源 = API/Swift 实时推送，`autoBuildIndex:false`，2 分片）
- **上一次全量构建**：2026-06-27
- **附随文件**：`ha3_sparse_evidence_requestbodies.json`（含下述每个请求的完整请求体 dense 1024 维 + sparseData，可直接回放）
- **受损主键完整清单**：约 522 篇文档（可另附）

## 结论先行

受损文档的 **dense(HNSW)、BM25 文本倒排、正排/doc store 均正常**，唯独 **sparse(稀疏向量)倒排索引里没有这些行**。因此：查询只带 dense 时命中该行 rank 1；一旦查询携带 sparseData，该行被挤出候选（topK 内不返回）。这批受损文档恰好是**上一次全量构建（2026-06-27）之后、通过 API/Swift 实时推送写入的全部文档**。

## 复现方法（self-query）

查询词 = 文档自身正文前 160 字，用它自己的 embedding 作查询，看该行能否检索到自己。对比两个文档：

| | doc_id | pk | 写入方式 |
|---|---|---|---|
| **受损** | DOC_ADMIN_20260513120214_0CD855 | **74583** | 6-27 全量之后实时推送 |
| **正常** | DOC_ADMIN_20260513120213_12182D | **22743** | 6-27 全量之前入库 |

（两者同属 admin 部门、permission_level=public，除写入时间外条件一致。）

## 四组请求与结果（rank = 该文档在结果中的名次；None = topK 内不返回）

| 请求 | 端点 | 构造 | 受损 74583 | 正常 22743 |
|---|---|---|---|---|
| **A** | `/vector-service/query` | 仅 dense 向量 | **rank 1** | rank 1 |
| **B** | `/vector-service/query` | dense **+ sparseData** | **None（消失）** | rank 1 |
| **C** | `/vector-service/search` | knn(dense+sparse) + text(**乱码,匹配不到**) + RRF | **None** | rank 2 |
| **D** | `/vector-service/search` | 纯 BM25 文本（text 真词，无 knn） | rank 1 | rank 1 |

## 关键判读

1. **A vs B（同一行,只加 sparse）**：受损行只加 sparseData 就从 rank 1 掉到 None → sparse 分量把它挤出候选;正常行不受影响。
2. **C（隔离 knn+sparse 臂）**：把 RRF 请求的文本路换成匹配不到任何文档的乱码,等于只留 knn(dense+sparse) 这一路。**正常行仍被自身 sparse 命中(rank 2),受损行完全命中不到(None)**。若 sparse 倒排里确实有受损行,这里应像正常行一样命中——但没有。
3. **D（纯 BM25）**：受损行在文本倒排里 rank 1,正常。→ 说明「改用 RRF 后受损文档能返回」是因为 **RRF 经 BM25 文本路把它捞回**,并非 sparse 路有这些行。

综合 1/2/3：受损文档缺的**只有 sparse 倒排项**,dense/BM25/正排都在。这解释了为何 weighted 融合下它们集体不可召回(无 sparse 项 → 组合打分被压低到掉出 topK),以及为何切 RRF 能规避(名次并集经 BM25 兜底)。

## 请贵方从引擎侧确认

1. **通过 API/Swift 实时推送写入的文档,是否不建立 sparse 倒排项、只有全量构建(Index_Rebuild)才物化 sparse?** 我方推送侧按《实时推送文档格式》成对推送了 `sparse_vector_indices`/`sparse_vector_values`(客户端有 dense+sparse 成对校验闸);单独重推受损文档也无法使其获得 sparse 可检索性。若确为「仅全量物化」,我方需通过一次全量构建根治,请一并告知对 API/Swift 源表安全的全量构建方式。
2. 在 weighted 融合下,knn(dense+sparse) 一路中「无 sparse 项」的文档,其组合得分被压低至掉出 topK(而非按 dense 得分正常参与)——这是否符合预期?
