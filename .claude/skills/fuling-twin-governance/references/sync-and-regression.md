# 周边同步与回归验证 — 金集、注册防重、回归快测口径陷阱

## 金集（goldset）同步

- **时机**：每批退役 commit **之前**，核对本批将退役 doc_id 与
  `eval_harness/goldset/golden_full.json` / `golden_50.json` 的交集；命中的把
  `expected_doc_ids` 与 `resolution[].doc_id/owner_dept` 一并改指 keep 侧，改前
  `cp` 出 `.bak_twin_retire` 备份。
- **翻转后必须重核**：改向基于"当时的 retire 侧"。后续裁决把 keep/retire 翻转后，
  新的退役侧（原 keep）可能恰是金集目标——每次翻转后重跑交集核对，别信上一轮结论。
- 金集匹配走 `eval_harness/matching.py` 归一，**裸子串匹配会假阴**：已知别名漂移
  "财务操作手册"（金集写法）vs"财务部操作手册"（实际标题）。

## 注册侧防重（防复发）

- 正本 `opensearch_pipeline/ingest_policy.py`：`raw_key_stem`（basename 去最后一层
  扩展名）+ `stem_twin_action`（同部门同 stem→skip+告警；跨部门→仅 warn——拦不拦
  是 ACL 归属问题）。
- `dataworks_nodes/register_new_files.py` 持有**内联副本**，AST parity test
  （tests/test_ingest_generalization.py）对拍，单边改动挂 CI。改完必须在发布窗口
  **重新粘贴节点**（顶部凭证注入块 5 行从旧节点/清理stage3 节点抄），跑一次
  DRY_RUN 看日志：`rev <日期>` + 防重映射条数 = 生效证明。
- 防复活已内建：注册查重 `SELECT raw_key FROM document_version` 不过滤 status，
  退役保留的 inactive 行挡住同 raw_key 重注册——所以**退役只置 inactive、永不删行**。

## 回归快测（每役收尾）

形态：本地 serving :8001（`RAG_ENV=test` + `RAG_RERANK_ENABLE=true` 指生产只读）
跑金集 25 题（`scratch/prod_retest_collect.py`）+ 11 题大手册专项（golden_full 中
resolution/expected_docs 含"手册"且非员工手册），对照最近基线答案集。

四个已踩实的口径陷阱，逐个核对后才能下"回归/零回归"结论：

1. **退役→keep 归一映射**：期望命中与 sources 对比前，把全部已退役 doc_id 经
   journal 映射到 keep 侧（采集脚本内置的 TWIN map 只有历史条目，要按 journal
   动态扩展），否则命中全是假阴。
2. **基线环境一致性**：复测 serving 的环境变量要和基线采集时一致（实测翻车点：
   `RAG_MAX_CONTEXT_CHARS` 默认 6000 vs 基线 10000 → 尾部带图 chunk 被截，图数
   "回归"是环境差不是退役差）。同样核对**基线采集时间 vs serving 代码提交时间线**
   （`git log --format='%h %ci'` vs 基线文件 mtime）——基线之后落库的 serving
   prompt 变更造成的行为差，不能记到本次治理头上。
3. **expected_doc_ids 为空的题**（BIND 系）是图绑定验收题不算 rank，两轮同 None
   属口径不是回归。
4. **图数下降先判随机性**：sources 与基线逐字一致 + ×2/×3 探针图数随机恢复 =
   LLM 引用倾向噪声；探针稳定为 0 才继续深挖（文档是否被回灌/退役 → 环境差 →
   serving 代码时间线，按此序归因）。

判定模板：rank 全持平/更好 ∧ 拒答不增 ∧ sources 变化均为良性（孪生消失、keep
身份替换）∧ 图数差异全部归因为噪声或非本役因素 ⇒ 零回归。结论落
`scratch/twin_retire_regression_<date>.md` 存档。
