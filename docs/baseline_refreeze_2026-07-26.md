# 评测基线重冻记录（2026-07-26）

前基线冻于 **2026-07-20**（commit `6ba487b`，73 项）。今日一批摄取侧改动使 regime 失配、
`--strict` 必红，故重冻。备份：`eval_harness/goldset/baseline.json.bak-pre-refreeze-20260726`。

## 1. 为什么必须重冻（不是"分数掉了想洗白"）

失配的是 **regime 严格键**，不是指标：

| key | 旧基线 | 本轮 | 原因 |
|---|---|---|---|
| `l4_ingestion_evaluator_version` | 无该键 | `2.0.0` | M1–M4 匹配器换了判定口径 |
| `funnel_policy` | 无该键 | `c1` | 选项 C 翻默认 |
| `l4_gt_sha` | 无该键 | `d91855974893c86b` | PDF GT 独立重标（宽容窗口，不构成失配） |

跨判据/跨 GT 比较**必须**强制重冻——这正是这两个键刻意不进 `_LENIENT_REGIME_KEYS` 的设计意图。

## 2. 跑法（口径正确性优先）

`bash deploy/eval_release_gate.sh`，258 题 × L0–L6 全 live + 3 组 judge 面板，约 4.5 小时。

⚠️ **一个差点跑错的地方**：本机 shell 未设 `RAG_RERANK_ENABLE`，`_regime` 读出
`rerank_enable=False`，而旧基线是在 rerank **ON** 的生产姿态下冻的。若照跑，L2 会拿
rerank-OFF 的分数去对 0.9/0.8 的 rerank 档位阈值，**冻出来的基线本身就是错的**。
已用 `RAG_RERANK_ENABLE=true` 跑，报告里的 regime guard 复核为
`active fusion=weighted, rerank=True`。

跑前用 `--limit 3 --layers l0,l1,l2` 做了端到端烟测（全绿）——4 小时后才发现打不通的代价
远高于这一分钟。

## 3. 冻前逐项核对（**零回退**）

**73 → 73 项，零缺失、零新增、零回退、10 项改善、63 项持平。**

| 指标 | 旧基线 | 本轮 | Δ |
|---|---|---|---|
| `judge.negatives.overall` | 3.959 | 4.455 | **+0.496** |
| `judge.completeness` | 4.089 | 4.221 | +0.132 |
| `l4ing.jaccard.xlsx` | 0.891665 | **1.0** | +0.108 |
| `l4ing.jaccard.pdf` | 0.81111 | **0.91320** | +0.102 |
| `l6.judge_chunk.repr_pass_rate_ge4` | 0.338 | 0.432 | +0.094 |
| `judge.correctness` | 4.37 | 4.449 | +0.079 |
| `judge.faithfulness` | 4.84 | 4.911 | +0.071 |
| `judge.mm.image_relevance` | 3.848 | 3.914 | +0.066 [advisory] |
| `l4srv.answer_image_rate` | 0.68966 | 0.72414 | +0.034 [advisory] |
| `l4srv.marker_validity` | 0.63636 | 0.66667 | +0.030 |

两项 L4-ing 提升是今日全部工作的合力：选项 C + strip-stitch + GT 重写 + matcher 2.0.0。

### 3.1 三条硬失败逐条溯源 —— **全部既有，非本批引入**

闸门 exit=1（预期）。`--strict` 报的三条：

| 失败 | 判定 |
|---|---|
| `source attribution recall@1 (L1)` 0.947 | 与旧基线**逐位相同**，绝对闸既有未过 |
| `<<IMG:N>> marker validity (L4-srv)` 0.6667 | 实际**改善**（0.6364→0.6667），仍未过绝对门槛 |
| `baseline regression (regime)` | 正是本次重冻要解决的那条 |

两条 `[L6-soft]`（mid-sentence cut 0.5001 / routing-family 0.897）同样与旧基线**逐位相同**，
且均为 advisory。

⚠️ 口径：这些是**绝对闸**（固定门槛），与"相对基线是否回退"是两回事。基线冻的是**指标值**
不是判据结果——**重冻不会把它们洗白**，该红还是红。

### 3.2 2 次 rerank 超时的影响面（已量化，未重跑）

全程 2 次 `dashscope read timeout=15`（fail-open），占 258 题 ~0.8%，非系统性。指纹：

- 排序类（`l1.*` 的 recall/mrr 共 30 余项）**逐位不变** —— 回退路径仍召回正确文档；
- 仅 4 项分数档位类轻微下移：`l2.frac_at_least_med` −0.008、`l2.frac_high` −0.007、
  `l2.separation_auc_offtopic` −0.006、`l1.ranking.ndcg@10` −0.0006。

最大 0.008，远低于 delta 0.03。**未重跑的理由**：冻的是**略低**的值，方向上只会让未来正常
轮次显得更好，不会制造假回退；反之（冻略高值）才危险。

## 4. 结果

`frozen_at=20260726_185818`、`code_commit=277f740`、73 项、delta=0.03。
用同一份 report 复算差量网：**regime 零失配；69 硬指标 + 4 advisory 全 clean**。

## 5. 这次重冻锁进了什么（重要）

新基线把今日全部决定固化成了新参考系：**选项 C 默认 ON、`RAG_PDF_STRIP_STITCH` 默认 ON、
PDF GT 独立重标、L4 matcher 2.0.0、`EXTRACTOR_VERSION` 1.3.0**。

⇒ 此后**任何回退这些决定的改动，都会被差量网读成回归**。要撤其中任一项，需连同基线一起处置。
