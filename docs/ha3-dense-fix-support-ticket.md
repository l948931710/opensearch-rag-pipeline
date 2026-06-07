# HA3 向量检索版 — Dense/HNSW 修复工单（Alibaba 支持）

> ## 🛑 RESOLVED 2026-06-07 — dense was NOT broken; this ticket's premise was a diagnostic artifact.
> Our self-query diagnostics omitted **`order="DESC"`**. The index is **InnerProduct** (higher = more similar),
> so without DESC the engine returns ascending (worst-first) and the score-1.0 self-match is buried → false
> "HNSW empty". With `order="DESC"`, the console query test returns pk **5733 @ 0.999988 rank-1**; production
> `retriever.py` already uses DESC, so **dense + hybrid retrieval has worked all along** (verified end-to-end
> with a paraphrase query). **Problems 1, 2, 4 below are MOOT** (no dense/build fault). **The ONLY remaining
> real ask is Problem 3: force-delete the stuck test tables** (`fuling_kb_chunks_v4 / abtest_noads / acttest1`),
> a side effect of the `Table_Active` activation hang — cosmetic; production is healthy. Everything below is
> retained only as the record of the (wrong) investigation. See skill gotcha **G29**.

> ~~Status: READY TO SUBMIT. Drafted 2026-06-07 after exhaustive client-side investigation
> (see skill `references/gotchas.md` G18–G27). Every client-side hypothesis was eliminated;
> the remaining causes are in Alibaba's backend build pipeline.~~ (Superseded — see RESOLVED note above.)

---

## 标题
向量检索版稠密 HNSW 检索完全失效 + 多张离线构建表卡死无法删除（已系统排查，疑后端问题）

## 实例
`ha-cn-kgl4slr1n01`（cn-hangzhou，向量检索版）

## 【最关键诊断】`list_tasks` 暴露的 FSM 卡点（请优先看这里）

通过 `cli.list_tasks()` 拿到任务 FSM 明细，直接定位到卡点，且发现一个关键不对称：

- **新表激活 FSM**（`update_biz_depend_index_fsm`，`operateType=Table_Active`）：**所有新建表**
  （`fuling_test_v1/v2/v3/v4/v6/rawhttp`、`fuling_abtest_noads`）都走到节点 **`target checking` 卡住/重试**，
  msg：**「引擎未收到该索引的切换目标，继续等待」**（status=retry/cancel）。即 admin 已完成
  `start → biz updating → online updating`，**但引擎(suez/searcher)始终未收到新索引的切换目标**，导致新表
  **永远到不了 `IN_USE`**。
- **对照——全量构建 FSM 成功**（`datasource_flow_fsm`，`operateType=Index_Rebuild`，"手动触发全量"）在生产表上
  **全部节点 SUCCESS**：`init→trigger→scan→bs_submit→build(generation 1777709191)→suez_submit→switch`，
  其中 `suez_submit` msg=**「引擎收到目标，开始切换」**、`switch` msg=「switch finished」。

→ **结论：「全量构建 + 切换」机制本身是通的（Index_Rebuild 能让引擎收到目标并完成切换）；坏的是「新表激活
（Table_Active）」——引擎收不到新表的切换目标。** 这是 admin↔引擎(suez) 协调层问题（疑似引擎侧无法承载/拉起
新索引 target），与表的 schema/数据/向量/配置/`enable_adsampling`/`is_embedding_saved` 均无关。

**请优先解答：**
1. **为何 `Table_Active` 的 `target checking` 节点「引擎收不到切换目标」**？该实例 **查询节点数量 = 1**，已承载
   生产表 `fuling_kb_chunks`（2 分片）。**是否因为单个查询节点容量/槽位已满、无法再加载第二张表的索引，导致引擎
   收不到新索引的切换目标**？（卡点在 build 成功之后的「加载到查询节点+切换」阶段，与此高度吻合。）该实例当前
   `IN_USE` 的只有 1 张生产表，却有 7+ 张新表全部卡在此节点。**如果是查询节点容量问题，增加查询节点数量（变配
   1→2+）能否让新表正常激活？** 请确认根因，避免我们盲目变配。
   **重要实测**：已把**数据节点**升配（2核8G→2核16G，2 节点），之后新建的最小表 `fuling_acttest1` **仍卡在
   `target_checking=RETRY`、自查询 0/10、到不了 IN_USE**。→ **已排除「数据节点容量」**。所以要么是**查询节点
   (=1) 容量**问题，要么根本是 admin↔engine 协调 bug（加了数据节点容量也没用，更像 bug）。请从后端确认到底是哪种，
   再决定是否升级查询节点。
2. 既然 `Index_Rebuild`（全量构建+切换）机制本身是通的，**如何用我们已有的完整 OSS JSONL 数据
   （`oss://fuling-knowledge-base/opensearch/fuling-kb-chunks-v3-…/data.jsonl`，3669条，已校验）把可检索的 HNSW
   重新建好**？注意：生产表 `fuling_kb_chunks` 的数据源是 **swift（纯 API 推送）**，据文档其 `reindex` 只会从 Swift
   日志 `data_time_sec` 追实时、并清空历史（上次正是因此建成空表），**无法从 OSS 文件做全量构建**。所以请明确以下哪条可行：
   - (a) `reindex` 的 `oss_data_path` 能否对 **swift 源的表**做一次性「从 OSS 全量重建」？
   - (b) 能否把生产表的 `dataSource.type` 由 `swift` 改为 `oss`（`modify_table`，我们客户端尝试报 `ModifyVariableNotAllowed`）后再全量构建？
   - (c) 还是必须**新建一张 OSS 源的表**——但那又卡在 `Table_Active`（问题见上），所以得先修好新表激活？
   - (d) 或由阿里云后台直接基于我们的 OSS 数据为该实例重建/迁移？
3. 清理卡在 `target checking` 的新表（见问题 3）。

---

## 问题 1（核心）：生产表 `fuling_kb_chunks`（API/swift 源）稠密 HNSW 为空，但配置与推送格式均正确
- **索引配置正确**（`GET /indexes/fuling_kb_chunks` 实测）：`dense_vector` 索引
  `enable_rt_build=true`、`vector_index_type=HNSW`、`distance_type=InnerProduct`、`dimension=1024`、
  `builder_name=HnswBuilder`、`searcher_name=HnswSearcher`、`ignore_invalid_doc=true`、
  `rt_index_params={"proxima.oswg.streamer.segment_size":2048}`。
- **推送格式正确**：通过 `push_documents` 以 `{"cmd":"add","fields":{…,"dense_vector":[<1024 个 float 数字>],
  "sparse_vector_indices":[…],"sparse_vector_values":[…],…}}` 推送，符合《实时推送文档格式》。
- **现象**：正排字段能取回正确向量（与重新 embedding 的 cosine=1.0）；BM25 正常；但**纯稠密 kNN 自查询
  完全命中不到自己**。随机抽样 **60 条** active chunk 用各自向量自查询：**0/60 命中自身**（全局 top1 分数仅
  0.59–0.95，最高 0.947，**从无 ≈1.0 的精确自身**）。说明可检索的向量索引里**几乎不含当前 active 向量**
  （图谱疑似只含极少量陈旧/残留向量）。注：索引类型确为 **HNSW**（`builder_name=HnswBuilder`），非 Qc/IVF。
- **已排除「向量内容非法被 `ignore_invalid_doc` 丢弃」**：抽取了 **500 条已存储向量**逐一校验，
  **全部合法**（维度=1024、无 NaN/Inf、非零向量、L2 范数=1.0000 单位归一化）。即向量**不是**非法数据，
  不应被丢弃。且**每个文档**的自查询都失败（不是部分失败），若仅「非法向量被丢弃」，合法向量仍应可检索——
  但全部不可检索，故该假设不成立。
- **已验证向量本身有效且质量良好（与 HA3 解耦的对照实验）**：用**同一批向量在本地做暴力 cosine 检索**，
  随机 15 条自查询 **15/15 命中自身 @1.0**，且最近邻均为**同文档/同章节/同主题**的合理结果。**同一批向量
  在本地完美、在 HA3 稠密 kNN 却 0/60** —— 证明问题 100% 在 HA3 的可检索向量索引侧，**与向量数据本身的
  有效性或语义质量无关**（无需重新 embedding）。
- **已验证 push 推送格式不是原因（受控实验）**：向 live 表推送 **2 条测试文档**（同一合法向量，dense_vector
  分别用「数字数组 `[0.1,…]`」和「逗号字符串 `"0.1,…"`」两种编码），**push 均返回 `{"status":"OK","code":200}`**；
  等待 80 秒实时索引后，**两条都无法通过自身向量 kNN 检索到**（不在 top10，自查询命中不到自己）。已删除测试文档。
  → 说明：(a) 推送格式（数组 vs 字符串）**都不是原因**；(b) **连全新推送的文档也进不了可检索的 HNSW**——
  即 **realtime HNSW 构建对新推送的文档同样不生效**，不止是历史批量数据。
- **已排除检索参数/召回调优**：对同一自查询，分别设 `ef=8000`、关闭 `dynamic_termination`
  (`prob_threshold=0`)、强制 `scan_ratio=1.0`（全量精确扫描），结果**完全一致**（top1 恒为 0.627、永不含自身）。
  连**强制全量精确扫描都命中不到自身**——证明向量根本不在可检索的向量索引里，不是 `ef`/早停/近似等召回参数问题。
  （索引构建参数为标准值：`max_neighbor_count=100`、`efconstruction=500`，偏高质量，不会导致漏召。）
- **推断**：合法且已归一化的向量进入了正排，但**整体没有进入可检索的 HNSW 图谱**（realtime 向量段未构建/
  未合并进可检索索引，或后台构建异常）。自查询全局 top1 仅 ≈0.4–0.67、且永不含精确自身，疑似图谱只含极少量/
  陈旧向量。
- **请协助 + 关键疑问**：查后端构建日志，确认**为何这些已验证合法的 `dense_vector` 没有进入可检索的 HNSW 图谱**。
  尤其请明确（公开文档均未说明这几个参数）：
  - **`enable_rt_build=true` 到底是否会把实时 push 的向量自动合并进“可检索的” HNSW 图谱**？还是仅标记该表、
    需要另外触发一次离线 build/merge 才能检索？（这是我们怀疑的核心：向量进了正排，但 realtime 段没有合并进
    可检索的向量索引。）若需要显式触发 build/merge，正确的接口/操作是什么？
  - `linearBuildThreshold=5000` 与 `minScanDocCnt=20000` 的确切含义？docCount(3669) < linearBuildThreshold 时
    是否会改走精确/线性检索？（公开文档未定义这两个参数。）
  - `ignore_invalid_doc=true` 是否在 HNSW 构建阶段丢弃了我们的向量？后台日志里有无对应的 skip/异常记录？
  - **`proxima.hnsw.builder.enable_adsampling=true` 与 `is_embedding_saved=false` 是否冲突**？我们的 dense 索引
    开启了 adsampling 但未保存原始向量（`is_embedding_saved=false`）。adsampling 在构建/检索时若需读取原始向量
    维度，而原始向量未保存，是否会导致向量无法进入可检索的 HNSW 图谱（而仅留在正排）？**若是，正确做法是
    `is_embedding_saved=true` 还是关闭 `enable_adsampling`？是否需要重建表？**（这两个参数公开文档均无说明，故来询问。）
  注：维度、NaN/Inf、零向量、归一化、推送格式均已自查排除（见上）。
  注：不希望用 API 源「索引重建」——《表的索引重建》明确它会清空历史数据、仅回追 3 天增量。

## 问题 2：离线全量构建在本实例上从不完成
用「全量数据来源」建表做离线构建，**全部卡在 `building`、永不完成**，`stats` 持续
`errorCode:3000, errorMsg:"init gig stream failed, biz: general.<table>.default"`，docCount 始终为空：

| 表 | 数据源 | 向量编码 | 是否符合官方格式 | 结果 |
|---|---|---|---|---|
| `fuling_kb_chunks_v2` | OSS+API（被自动设 `tableFormat:paimon`） | — | 否 | 卡死 building |
| `fuling_kb_chunks_v3` | DLF Object Table | 数字数组 | 否 | 卡死，转 NOT_USE |
| `fuling_kb_chunks_v4` | OSS+API | **字符串数组（符合官方文档示例）** | **是** | **卡死 building ~40 分钟** |

**最关键证据**：连一张**全新的最小 swift（API 推送）表** `fuling_abtest_noads`（仅 3 字段 id+chunk_id+
dense_vector，无离线数据源、无 TEXT/sparse、无量化、`enable_adsampling=false`）也**卡在 `RESTORE_USE`、
errorCode 3000、200+ 秒从不到 `IN_USE`**；push 返回 200 OK，但 90 秒后自查询 0/10、查询返回空（top1=None）。
→ **本实例上「新建的任何表都无法完成构建/激活、永远到不了 IN_USE」**（OSS / DLF / 纯 swift 全部如此），
唯一 `IN_USE` 的只有早先就存在的生产表 `fuling_kb_chunks`。这是**实例级的构建/激活故障**，与 schema、数据源、
数据、向量编码、`enable_adsampling`、`is_embedding_saved`、`autoBuildIndex` 等**均无关**。同一故障也解释了生产表：
构建子系统无法把新推送的向量物化进 HNSW 段（所以 live 重推后稠密仍失效）。

已排除：数据格式（v4 用官方字符串数组格式仍失败）、数据源类型、向量编码、autoBuildIndex、维度、enable_adsampling。
**请查后端日志定位 `errorCode:3000` 真实根因**，并确认是否为**构建计算资源不足/排队阻塞**
（实例现有 1 张在用表 + 3 张卡死表，每表占 2 个更新资源，是否超额导致新构建拿不到计算资源？）。

## 问题 3：卡死的表无法删除
`fuling_kb_chunks_v4 / fuling_abtest_noads / fuling_acttest1`（及更早的 v2/v3 若仍在）均卡在 `RESTORE_USE`，删除时返回 `IndexInUse`；
`stop_table` 无法使其转为 `NOT_USE`；控制台「删除」按钮灰显。**请后台强制清理/删除这 4 张表，释放构建资源。**
（它们正是「新表无法激活」故障的样本——若能告知它们为何到不了 `IN_USE`，基本即定位根因。）

## 问题 4：方法确认
对「OSS 上 JSONL 的预计算向量（dense+sparse）离线导入向量表」，正确方法是
**DataWorks 数据集成「单表离线同步任务」**（OSS源→OpenSearch目标）还是表的「全量数据来源 OSS+API/DLF」配置？
两者区别？若是前者，需要哪种**数据集成资源组**？我们已有 DataWorks 工作空间 `default_workspace_6na2`
和一个 Serverless 通用资源组 `data_process`（CommonV2），是否可直接用于数据集成单表离线同步？

## 背景
生产 RAG 系统（钉钉机器人），目标是恢复稠密/语义混合检索。生产表 BM25 当前可用，**非紧急**。
所有向量已用 DashScope `text-embedding-v4`（1024 维 dense + sparse）预计算好，存于
`oss://fuling-knowledge-base/opensearch/…/data.jsonl`（约 3669 条），可随时重推或重新导入。

---

## When Alibaba replies — forward plan (for the next session / Claude)

Depending on the answers, the unblock path is one of:
1. **In-place fix (best):** if Q1 reveals why pushed vectors skip the HNSW (e.g. a build trigger or a config
   flag), apply it to the live table → no migration. Re-push from RDS+cache via Stage 3 (`scripts/` +
   `清理stage3`) if needed.
2. **DataWorks 数据集成 单表离线 (likely correct method):** once Q3 frees resources and Q4 confirms the resource
   group, build an OSS→OpenSearch sync task (we already have DataWorks wired). Source file already staged at
   `oss://fuling-knowledge-base/opensearch/fuling-kb-chunks-v4-strvec-…/data.jsonl` (string-encoded) and the
   numeric `…v3-…/data.jsonl`.
3. **Offline OSS/DLF build:** once Q2's root cause (likely resource starvation) is cleared, the v4 setup
   (OSS+API, string vectors, doc-correct) or a DLF Object Table should complete. Validate with
   `scripts/diag_ann_selfquery.py` (self-query must be rank1@~1.0) before any cutover via `RAG_HA3_TABLE_NAME`.

Verified facts to carry forward (don't re-derive): push format = lowercase `cmd`+`fields`+numeric arrays;
OSS-file format = uppercase `CMD`+flat+string arrays; `autoBuildIndex` lives in `dataSourceInfo` (PUT
`/indexes/{t}` minimal body, no-ops while building); DLF Object Table = managed bucket, upload via 文件列表,
相对路径 = uploaded filename; API-source `reindex` is destructive; embedding cache rebuilt locally
(`scratch/embedding_cache.json`, 3669/3669 hits, drift cos=1.0).
