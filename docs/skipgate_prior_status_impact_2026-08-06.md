# skip-gate prior-version status 影响面核查（2026-08-06）

**结论：现网影响面 = 0，存疑关闭。** 但核查过程推翻了一条被广泛引用的注释，见 §4。

- 复跑脚本：`scratch/probe_skipgate_impact_20260806.py`（纯 SELECT，prod_access 只读会话）
  ⚠️ `scratch/` 是 gitignore 的（`.gitignore:22`），该脚本**只在本机**、不随仓库分发；异地复跑请按下面 §3 的三层查询自行重建，判定标准见各层说明
- 行为侧回归门：`tests/test_skip_gate_prior_version_status_db.py`（真库，含对照组 + 反证锚）
- 核查数据快照时间：2026-08-06

## 1. 被查的行为

L2 skip-gate 选 prior version 的语句（`opensearch_pipeline/pipeline_nodes.py:1459`）：

```sql
SELECT version_no, canonical_sha256 FROM document_version
WHERE doc_id=%s AND version_no<%s AND canonical_sha256 IS NOT NULL
ORDER BY version_no DESC LIMIT 1
```

**没有 `status` 谓词** —— `retired` / `rejected` 的旧版本照样参与 canonical_sha256 比对。
命中即把新版本置 `SKIPPED_DUPLICATE`，并把 `document_meta.current_version_no`
**回退**到那个 prior。若 prior 恰好是个不在服务的版本，指针就被拨到了死版本上。

这是**代码事实**（读码即可确认），本次要回答的是它在现网**造成过什么后果**。

## 2. 行为侧：先确认"如果发生会怎样"

`tests/test_skip_gate_prior_version_status_db.py` 在真库上做对照实验：同一份 seed
只改 prior 的 `status`（`active` vs `rejected`），两组结果必须相同。

| 组 | v2 content_process_status | current_version_no |
|---|---|---|
| prior=`active` | `SKIPPED_DUPLICATE` | 回退到 1 |
| prior=`rejected` | `SKIPPED_DUPLICATE` | 回退到 1 |

**变异验证**：给那句 SELECT 加上 `AND status='active'` 后两组立刻分叉
（rejected 组变回 `NOT_STARTED`、指针不回退）⇒ 证明测试确实在测 status 这一维度，
不是"两组都没命中"的空相等。反证锚（hash 不同则不 skip）同时成立。

结论：**行为确证 —— 现状确实不看 status。**

## 3. 影响侧：现网数据（三层）

### L1 危害直查（不依赖成因归因）

「文档 `status='active'` 但 `current_version_no` 指向一个不可服务的版本
（版本 status 非 active，或 index_status ∈ {DELETED, PENDING_DELETE}）」

**0 条。** 无论成因是不是 skip-gate，这类危害当前不存在。

### L2 是否发生过

| 指标 | 值 |
|---|---|
| `SKIPPED_DUPLICATE` 版本总数 | **0** |
| 被 `retired`/`rejected` 的 prior 挡掉的版本 | 0（分母为 0，本身说明不了问题） |

分母为 0 时「没发生过」与「这道门没生效过」不可区分 —— 必须看 L3。

### L3 分母校准

找出**本该被 skip 的**版本（`version_no>1` 且与 skip-gate 会选中的 prior 同 hash）：

| 处理日 | 条数 | content_process_status | chunk_status |
|---|---|---|---|
| 2026-07-06 | 68 | `DONE` | `DONE` / `EMPTY` 混合 |
| 2026-07-07 | 4 | `DONE` | `DONE` |

**72 条，全部 `DONE`，一条都没被 skip。**

排除替代解释「prior 的 hash 是事后回填、skip-gate 当时读不到」：

| prior.processed_at 早于 v2 | 时间未知 | prior 晚于 v2 | 总数 |
|---|---|---|---|
| **72** | 0 | 0 | 72 |

72/72 零例外（prior 处理于 6-22 12:27，v2 处理于 7-06 16:29）。而 skip-gate 代码
2026-06-16 就已落地（`bc970dd`）—— 上线三周后，这道门在这批重传上**没有生效**。

佐证：版本 status 分布为 retired 1822 / active 1453 / superseded 998 / inactive 359，
L2b 为空不是因为库里没有非 active 的版本行。

## 4. 副产品：flag 的生效范围与注释不符（**本次最值得记的一条**）

`pipeline_nodes.py:1467` 的注释写着：

> 生产默认 `RAG_SKIP_UNCHANGED_REINGEST=true`

全仓库 grep 的事实是：该 flag **只在一处**被设成 true ——
`dataworks_nodes/stage1_node.py:41` 的 `os.environ.setdefault`，
**没有任何 `.env` 文件设它**。

所以准确表述是：

- 走 **DataWorks stage-1 节点**（日常调度、正常上传的必经路径）→ skip-gate **开**
- 笔记本 / 本地 CLI / 直调 orchestrator → **关**

7-06、7-07 那两批正是手工重灌路径，因此没命中。这与
`flag-propagation-is-execution-path-dependent-2026-07-26` 记录的判例是同一机制。

**引用那条注释时要带上执行路径限定**，否则会同时得出两个错误结论：
以为手工重灌会被 skip（不会），以及以为日常调度不会被 skip（会）。

## 5. 裁决

| 项 | 结论 |
|---|---|
| 「skip-gate 不过滤旧版本 status」是否属实 | **属实**（代码 + 真库对照实验双证） |
| 是否在现网造成过后果 | **没有**（L1=0、L2a=0，且 L3 证明这道门在相关重传上未生效） |
| 是否需要修 | **不需要**。可达路径被 F-37 挡住（退役文档禁止升版，`kb_console.py:2769`），且 rejected 版本通常没跑过 stage-2 ⇒ `canonical_sha256` 为 NULL ⇒ 被 `IS NOT NULL` 自动滤掉 |
| 遗留 | 72 个版本内容未变却重跑了整套摄取（重抽取/嵌入/入索引），是 skip-gate 本该省下的成本；其中部分产出 `chunk_status='EMPTY'`，与既有「EMPTY 文档」待办可能有交集 —— 未深挖 |

回归门已留：行为一旦改变（比如有人给那句 SELECT 加上 status 过滤），
`test_skip_gate_prior_version_status_db.py` 的对照组会立刻红。
