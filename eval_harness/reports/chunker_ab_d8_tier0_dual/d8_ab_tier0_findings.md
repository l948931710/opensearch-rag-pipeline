# D8 Chunker A/B Tier 0 评测最终报告

> 评测命令：`python -m eval_harness.chunker_ab --mode binding_only --arm off:'' --arm on:RAG_IMAGE_CONTENT_OVERRIDE=1 --gt-file gt_pdf_analysis.json --anchor-gt gt_pdf_semantic_anchors.json --out eval_harness/reports/chunker_ab_d8_tier0_dual`
>
> Analysis: chunker-ab-tier0-multidim-analysis Workflow (4 analyzer + 2 critic + 1 synth)

## Executive summary

D8 image override (Path A/B/C) 的 Tier 0 评测出现 **维度分歧**：funnel regression-ref 维度全部达标（mean_jaccard_pdf 0.839 → 0.933，Δ +0.094，5/30 WIN、0 LOSS），但 plan v3.1 #1 指定的 primary metric **semantic anchor dual_jaccard 未通过**（0.857 → 0.810，Δ −0.048，绝对值与 Δ 均跌破 ≥0.85 / ≥+0.10 门槛，ON win-rate 1/3=33% < 70%）。key_jaccard OFF=ON=1.0 表明 chunk 边界/路由无改动，dual 退步完全来自 image_hit 翻转。

按硬口径，**Tier 0 NOT PASS**。但 0 个 anchor 出现 chunk 路由级 critical regression（key_hit_on 全 True），dual 退步的 2 个 case (`pdf_sop_a5`、`pdf_xs_wi_007_a2`) 与 D8 Phase 4 / Bug A 的 design intent 直接冲突 — 用户 GT 标注与 chunker 设计意图（image 应绑到哪个 step）存在权威性争议，**不建议**就此放弃 ON，但**也不能 promote**，须先 review 4 个 GT 失配 anchor。

---

## 维度 1 — Per-doc 健康度

| Doc | funnel n_strong_gt | funnel mean_jaccard off / on / Δ | anchors | anchor key off / on / Δ | anchor dual off / on / Δ | 结论 |
|---|---|---|---|---|---|---|
| `pdf_sop` | 11 | 0.833 / **1.000** / **+0.167** | 6 | 1.000 / 1.000 / 0 | 0.667 / 0.667 / 0 | 维度分歧（a4 救 a5 跌，dual 持平）|
| `pdf_xs_wi_007` | 9 | 0.778 / **0.889** / **+0.111** | 6 | 1.000 / 1.000 / 0 | 0.833 / 0.667 / **−0.167** | 维度对撞（funnel 涨、anchor a2 退）|
| `pdf_it_xxh_003` | 10 | 0.900 / 0.900 / 0 | 6 | 1.000 / 1.000 / 0 | 1.000 / 1.000 / 0 | byte-equal |
| `admin_lodging` | — (clause) | — | 3 | 1.000 / 1.000 / 0 | 1.000 / 1.000 / 0 | byte-equal |

`pdf_it_xxh_003` 与 `admin_lodging` ON/OFF 双臂 byte-equal，与 override 只在 step_card 路径触发的预期一致。`pdf_sop` 与 `pdf_xs_wi_007` 是全部争议来源，且 funnel 与 anchor 维度方向相反。

---

## 维度 2 — 16 image-bearing anchor W/T/L

- **WIN** (off=F→on=T): **1** — `pdf_sop_a4`（步骤 3 进入 U8 扫码报检界面）
- **TIE**: **12**（10 双 T + 2 双 F：`pdf_sop_a1`、`pdf_xs_wi_007_a3`）
- **LOSS** (off=T→on=F): **2** — `pdf_sop_a5`（步骤 4.2 填表完成）、`pdf_xs_wi_007_a2`（步骤 2 收集核对交货单）
- Net = **−1 dual_hit**（与 dual_jaccard −0.0476 = −1/21 一致）

所有 5 个非 TIE case 的 `key_hit_off=key_hit_on=True`，说明 chunk 切分与 step 边界完全没动，**LOSS 源头只能是 `image_index` 真实改路**（D8 Path A/B/C 改 `best_idx`）。`pdf_sop` 一文出现"a4 救活以 a5 牺牲为代价"的 zero-sum trade — image 10 被 Path B 圈号信号从 step 4.2 移到 step 4.1，命中 a4 GT 期望但偏离 a5 GT 期望。`pdf_xs_wi_007_a2` 同理，images 1/2 被 Path A content-match 从 step 2 移到 step 1。

---

## 维度 3 — Jaccard 深度（primary metric）

| metric | n | OFF | ON | Δ | 门槛 | 判定 |
|---|---|---|---|---|---|---|
| semantic_anchor_key_jaccard | 21 | 1.000 | 1.000 | 0 | ≥0.85 | ✓ |
| **semantic_anchor_dual_jaccard** | 21 | 0.857 | **0.810** | **−0.048** | ≥0.85 & Δ≥+0.10 | **✗** |
| dual_jaccard_image_only | 16 | 0.813 | 0.750 | −0.063 | — | 退步集中在 image 子集 |
| funnel mean_jaccard_pdf | 30 | 0.839 | 0.933 | +0.094 | ON≥0.90 & Δ≥0.05 | ✓ |
| anchor ON win-rate (非零) | 3 | — | 1/3=33% | — | ≥70% | ✗ |
| funnel ON win-rate (非零) | 5 | — | 5/5=100% | — | ≥60% | ✓ |

doc-clustered bootstrap（n_doc=3，N=10000）：Δ dual mean ≈ −0.056，95% CI ≈ [−0.167, +0.000] — CI 包含 0，统计上不可拒绝 Δ=0，但**同样不可声称 ON 改进**。注意 n 过小，noise framing 须双向适用，不能只对 ON 不利方向喊 noise、对有利方向不喊。

按 plan v3.1 #1 的 primary metric 定义，**semantic anchor 维度 NOT PASS**（绝对值、Δ、win-rate 三项全部不达标）。funnel 维度全过，但不是 primary，无权反向救场。

---

## 维度 4 — Critical regression 严格判定

`dual_hit_off=True → dual_hit_on=False` 且 `key_hit_on=True` 的 anchor：

| anchor_id | step | expected signals | OFF/ON dual | 路由证据 |
|---|---|---|---|---|
| `pdf_sop_a5` | 步骤 4.2 填表完成报检 | `["9","10","档案查询"]` | T → F | Path B 圈号信号将 image 10 从 step 4.2 移至 step 4.1（`chunker.py:2226-2228` 注释自述："J=0.6 vs step 4.1 / J=0.2 vs step 4.2 — 清晰指向 step 4.1"）；Path C range-ref 可能将 image 9 移至 step 3.1 |
| `pdf_xs_wi_007_a2` | 步骤 2 收集交货单核对 | `["1","2","产品标识卡"]` | T → F | D8 Phase 4 Bug B fix 将 images 1/2 从 step 2 移回 step 1（Phase 4 memo 记 "xs_wi_007 +0.167"，但仅 funnel 维度复核） |

**严格按 spec（GT 即真值）：TRUE_CRITICAL = 2，Tier 0 critical-regression gate 不通过。**
**按 D8 design intent + 代码注释 + Phase 4 memo：intentional behavior，net 0。**

二者结论冲突的根因是 **GT 权威性 vs design intent 权威性** 之争 — 由设计实现者自己的注释/memo 推翻用户标注的 GT 属循环论证，且 Phase 4 memo 当时仅以 funnel 维度收尾、未用 anchor 维度复核。结论是 4 个 anchor（含 2 个 BOTH_LOSS：`pdf_sop_a1`、`pdf_xs_wi_007_a3`）须进入用户 GT review 队列，review 完成前不得以"intentional"为由清零 critical 计数。

---

## Critic 摘要

**Completeness critic**：13 项独立 recount 与 4 个 analyzer 顶层数字 byte-exact 一致（51 行 per_case 全部被引用、3 个 dual 翻转 anchor 与独立 scan 完全吻合）— 无遗漏、无错算，强化结论可信度。

**Cherry-pick critic**（弱化原 analyzer 框架）：(1) ANCHOR_WTL 的"假退步=visual_summary 重写"归因被 CRITICAL 反证 — override 改 `best_idx` 不改文本，image 是真实 rerouted，"假退步"措辞应删除；(2) PER_DOC 用 funnel +9.4pp 反推 anchor 退步无效属维度倒挂，anchor GT 是用户标的、funnel GT 是团队建的，前者权威性更高；(3) CRITICAL 用代码注释 + Phase 4 memo 推翻用户 GT 属让被告自证清白；(4) n=21 noise framing 须双向适用，不能选择性使用；(5) "GT 太严"≠"ON 改动对" — 即便 GT 有口径问题，是 dataset 缺陷而非 ON 通过 Tier 0 的理由。整体上 **弱化** 原 4 analyzer 中倾向"ON 实质 OK"的叙事，**强化** 应按 primary metric 硬口径判 NOT PASS。

---

## 决策建议

**Tier 0 判定（硬口径）：NOT PASS** — primary semantic anchor dual_jaccard 三项全不达标，funnel 维度反向证据不足以救场。可行下一步：

1. **GT review 优先（推荐）** — 把 `pdf_sop_a5`、`pdf_sop_a1`、`pdf_xs_wi_007_a2`、`pdf_xs_wi_007_a3` 4 个 anchor 的 expected_image_signals 拉出来与用户当面复核，确认 image 10 究竟应绑 step 4.1 还是 4.2、xs_wi_007 images 1/2 应绑 step 1 还是 2。GT 修正后重跑 Tier 0；若修正后 ON pass，再 promote。这条路同时清掉 funnel/anchor 维度对撞的真正根因。
2. **扩 Tier 1 样本（保底）** — 维持 ON 关闭，把 anchor 样本从 21 扩到 ≥60（n_doc ≥10），用 doc-clustered bootstrap 复核 Δ −4.76pp 是否仍在 noise 内；GT 不动。适用于 GT review 暂无人力的情况，但延后决策。
3. **分桶放行（不推荐）** — 仅在 funnel 强涨的 pdf 子集开 ON、其他保持 OFF。代价是引入 doc-level 开关复杂度，且未解决 GT 与 design intent 冲突。

**推荐选项 1**。这是唯一既尊重用户 GT 权威性、又给 D8 Path A/B/C 设计意图一个公平复核机会的路径，且 n=4 review 工作量可控。
