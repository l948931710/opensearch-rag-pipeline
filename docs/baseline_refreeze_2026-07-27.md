# 基线重冻 20260727：4 个尺子改动 + 2 个 prompt flag 翻默认，一次覆盖

日期：2026-07-27 · 分支：`fix/ingest-a-tier-2026-07-25` · `code_commit=cc30033`
证据：`docs/evidence/baseline_refreeze_20260727/`（gate.log + report.md）
备份：`eval_harness/goldset/baseline.json.bak-pre-refreeze-20260727`

## 为什么重冻

本批改了 **4 把尺子** + 翻了 **2 个 prompt flag**，`--strict` 必红（regime mismatch）：

| regime 键 | 旧 | 新 | 起因 |
|---|---|---|---|
| `l6_evaluator_version` | — | 2.0.0 | mid-sentence 判据：89/91 命中是假的 |
| `l1_matcher_version` | — | 2.0.0 | 标题归一双缺陷（不剥扩展名 / 括号删内容） |
| `l4_serving_evaluator_version` | — | 2.0.0 | orphan 分母收窄到"模型看得见的图" |
| `l4_ingestion_evaluator_version` | 2.0.0 | 2.0.0 | 未变 |

flag：`RAG_IMG_RULE_REQUIRES_IMAGES` / `RAG_IMG_ID_WHITELIST` 均翻默认 ON。

## 冻前逐指标核对（**没有因为"预期会红"就直接冻**）

73 项指标，12 项变好 ≥0.01、4 项变差 ≥0.01、0 项新增/消失。

### 变好

| 指标 | 旧 | 新 |
|---|---|---|
| `l4srv.marker_validity` | 0.6667 | **1.0**（77 marker / 0 非法） |
| `l6.boundary.midsentence_cut_rate` | 0.5001 | 0.326 |
| `l4srv.orphan_rate` | 0.5921 | 0.4182 |
| `l4srv.answer_image_rate` | 0.7241 | **0.8214** |
| `l1.ranking.recall@1 / @5 / mrr / ndcg@10` | 0.8642 / 0.9506 / 0.9049 / 0.8797 | 0.8704 / 0.9568 / 0.9106 / 0.8852 |
| `judge.faithfulness` / `correctness` | 4.911 / 4.449 | 4.938 / 4.462 |
| `judge.positives_fabrication_rate` | 0.022 | 0.009 |

`answer_image_rate` 同步上升是关键佐证：**marker 合法率不是靠少出图买来的**
（调研点名的那种 gaming 风险）。

### 变差（逐条查过，无一构成阻断）

| 指标 | 旧 → 新 | 判定 |
|---|---|---|
| `judge.negatives.overall` | 4.455 → 4.293 | judge 面板波动；L3 inter-rater 0.104 通过 |
| `l4srv.marker_distinctness` | 0.9118 → 0.8312 | **advisory**。合法率到 1.0 后模型只能在白名单内复用，复用率上升是直接后果 |
| `l4srv.dangling_ref_rate` | 0.0 → 0.0286 | 硬门 ≤0.05，仍过。见下 |
| `judge.mm.image_relevance` | 3.914 → 3.886 | advisory，judge 波动 |

**`dangling` 的归因我先猜错了，纠正**：我原以为是 `#F-mm15`"宁可不插"的代价。实查唯一那条
`J-r120_31`：`n_available=0` / `strategy=text_only` / `n_markers=0` —— 上下文一张图都没有，
`#F-mm14` 已把图规则整个撤掉，模型压根没有插图选项。那句"上图"是**照抄源文档自己的措辞**
（SOP 原文写着"点击上图菜单栏上的转入按钮"）。是答案文本的 LLM 波动，35 条样本上 1 条 = 0.0286。

### 仍然红的硬门（**非本轮引入**）

`source attribution recall@1` = **0.947**（门槛 ≥0.95，n=19）—— 新旧**完全相同**，
20260726 重冻时该门就是红的。1 题之差（19×0.05≈0.95）。

## 一条必须写下来的教训

`midsentence_cut_rate` 我基于 220 条评测语料预测新口径会到 **0.077**，全库实测是 **0.326**。
方向对（0.5001 → 0.326），**量级预测错了 4 倍**。原因是评测语料（6 篇 PDF + xlsx/pptx/docx）
的形态分布与全库不同。教训：**小语料上标定的判据，不能外推它在全库上的读数**。
0.326 仍远高于 0.05 门槛（该门槛的重标本来就还挂在 Sam 名下未决）。

## 门槛随之抬高

冻进基线即成为新的回退基准。今后：`marker_validity` 掉回 0.9、`orphan_rate` 涨回 0.55、
`recall@1` 掉回 0.86 都会被差量网判红（delta=0.03）。这是重冻的意义，也是代价。

⚠️ 尤其注意：今天大部分"改善"是**尺子修正**而非行为改善。真正的行为改善只有
`#F-mm14`/`#F-mm15`（marker）与随之而来的 `answer_image_rate`。把 0.5001→0.326 之类当作
"分块变好了"是误读 —— 那是判据变对了。

## 未决

- `source attribution recall@1` 0.947 差 1 题，是否值得单独查（跨两次重冻稳定）。
- `midsentence` 门槛 0.05 的重标（口径已换，门槛还是老的）。
- 分支上 14 个未 push commit，仓库为 public，push 前需过一遍 diff。
