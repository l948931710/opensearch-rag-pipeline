# D8 Chunker A/B Tier 0 — GT Review 后复测

> 评测命令：`python -m eval_harness.chunker_ab --mode binding_only --anchor-gt gt_pdf_semantic_anchors.json ... --out eval_harness/reports/chunker_ab_d8_tier0_post_review`
>
> GT 变更：见 `~/Downloads/opensearch-rag-data/eval_samples/ground_truth/gt_pdf_semantic_anchors.json` 的 `_meta.gt_review_log`

## 用户裁决（2026-06-14 20:50 UTC）

| anchor | verdict | 说明 |
|---|---|---|
| `pdf_sop_a1` | 🟡 fix_keywords | `['1', '三联', '检验单']` → `['1', '检验单']`（删 "三联"，visual_summary 里没有） |
| `pdf_sop_a5` | ⏭ skip | 留 D8 真改路由的争议状态待后续决断 |
| `pdf_xs_wi_007_a2` | ⏭ skip | 同上 |
| `pdf_xs_wi_007_a3` | 🟡 fix_keywords | `['26', '27', '28', '用友 U8', '扫码报检']` → `['26', '27', '28', '扫码报检']`（删 "用友 U8" 多空格问题） |

## Review 前后对比

| 指标 | Review 前 | Review 后 | Δ |
|---|---|---|---|
| dual_jaccard OFF | 0.857 (18/21) | **0.952 (20/21)** | +0.095 |
| dual_jaccard ON | 0.810 (17/21) | **0.905 (19/21)** | +0.095 |
| Δ dual (ON−OFF) | −0.048 | −0.048 | 0 |
| ON win-rate (非零) | 1/3 = 33% | 1/3 = 33% | 0 |
| key_jaccard | 1.0 / 1.0 | 1.0 / 1.0 | 0 |
| funnel mean_jaccard | 0.839 / 0.933 (+0.094) | 同 | 同 |

## 维度复判（Plan v3.1 #1 primary metric）

| 阈值 | Review 前 | Review 后 | 状态 |
|---|---|---|---|
| dual ≥ 0.85（绝对值） | ON 0.810 ✗ | **ON 0.905 ✓** | **从 ✗ → ✓** |
| Δ dual ≥ +0.10 | −0.048 ✗ | −0.048 ✗ | ✗（不变） |
| ON win-rate ≥ 70% | 33% ✗ | 33% ✗ | ✗（不变） |
| key ≥ 0.85 | 1.0 ✓ | 1.0 ✓ | ✓ |
| funnel ON ≥ 0.90 + Δ ≥ +0.05 + wins ≥ 60% + 0 critical | ✓ | ✓ | ✓ |

**整体 Tier 0 判定（硬口径）：仍 NOT PASS** —— ON dual_jaccard 通过绝对值阈值但 Δ 与 win-rate 仍不达。

## 关键发现

**fix_keywords 对 Δ 无影响**。a1 + a3 修 keywords 同时改善 OFF 和 ON（因 keyword fail 在两 arm 同时发生），对称 +2 → Δ 不变。这进一步**确认**：剩余的 Δ −4.76pp **全部** 来自 `pdf_sop_a5` + `pdf_xs_wi_007_a2` 这 2 条 D8 真改路由 anchor。

| anchor | OFF dual | ON dual | 路由变化 |
|---|---|---|---|
| `pdf_sop_a5` | T | F | image 9 → step 3；image 10 → step 4.1（D8 Phase 6 Bug A fix） |
| `pdf_xs_wi_007_a2` | T | F | image 1 → step 1（D8 Phase 4 Bug B fix） |

## 决策矩阵

剩余 2 条 skip 的 anchor 是 Tier 0 闭环的唯一阻塞：

| 路线 | 实施 | Δ dual 后果 | Tier 0 |
|---|---|---|---|
| **A. GT 对**（D8 是 bug） | a5/a2 留 anchor 不动，回滚 D8 Path A/B/C 的 Bug A/B fix | 回滚后 ON 会重新 dual hit → Δ → 0 | PASS |
| **B. 代码意图对** | 把 a5/a2 acceptable 改为 ON 实际绑的 chunk（step 4.1/step 1） | 修后 ON dual hit 救活 → Δ → 0 | PASS |
| **C. 继续 skip** | 不动，承认 Tier 0 NOT PASS，转 Tier 1 扩样本复核 | Δ −0.048 持续 | NOT PASS |

## 下一步

剩余 2 条争议的本质是 **"image 应该绑哪个 step"** 的业务判断 —— 只有真正了解 SOP 流程的人能定（用户）。

- `pdf_sop_a5`：image 10（手写生产记录表）该绑 step 4.2"填表完成"还是 step 4.1"填写设备/班次/数量"？
- `pdf_xs_wi_007_a2`：image 1（产品标识卡）该绑 step 2"收集核对交货单"还是 step 1"按标识卡清点实货"？

建议直接看本地图（`~/Downloads/gt_review_key_images/`）+ chunk text 后再做一次裁决。或者保留 skip 状态接受 Tier 0 NOT PASS，转下一阶段。
