# D8 Chunker A/B Tier 0 — 最终 image-粒度 GT 后的复测

> 评测命令：`python -m eval_harness.chunker_ab --mode binding_only --anchor-gt gt_pdf_semantic_anchors.json ... --out eval_harness/reports/chunker_ab_d8_tier0_final`
>
> GT 变更：见 `~/Downloads/opensearch-rag-data/eval_samples/ground_truth/gt_pdf_semantic_anchors.json` 的 `_meta.gt_review_log`

## 用户第二轮裁决方法论（关键转折）

第一轮裁决修了 keywords 但保留 a5 / a2 的 chunk-级宽容化倾向。用户明确拒绝："step 1 和 step 2 不是等价落点,这会把错误路由也判成正确"。

第二轮采用 **image 粒度精确拆分**:

| 变更 | 业务真相 |
|---|---|
| `pdf_sop_a5` signals 改为 `["9", "档案查询"]` | image 9 (U8 报检界面) 应留 step 4.2 — image 9 显示填表后期阶段,D8 把它移到 step 3 (入口) 是过激 |
| **新增** `pdf_sop_a7` step 4.1, signals=`["10"]` | image 10 (手写记录表) 应移 step 4.1 — image 的 ①②③ 标号是 4.1 字段 (设备/班次/数量),④⑤ 只是辅助 |
| `pdf_xs_wi_007_a2` 改 step 1 anchor (不加 step 2) | image 1 (产品标识卡) + image 2 (手写抄录) 都属于 step 1 "按产品标识卡清点+抄录"。Step 2 用《交货单》不是这两张图。**拒绝**"acceptable 同时加 step 1+step 2"宽容化 — 那样 image 2 留 step 2 也被判对,掩盖 D8 partial 修复 |

## Review 前后对比

| 指标 | 1st review 后 (n=21) | **2nd review 后 (n=22)** | 评价 |
|---|---|---|---|
| dual_jaccard OFF | 0.9524 (20/21) | 0.8636 (19/22) | OFF 下降 — a7 新建 / a2 改 step 都让 OFF 反映"业务真相 vs 当前路由"的差 |
| dual_jaccard ON | 0.9048 (19/21) | **0.9091 (20/22)** | ON 微升 — a7 在 ON 救活 (image 10 → step 4.1) + a2 partial 救活 |
| **Δ dual (ON−OFF)** | **−0.048** | **+0.046** | **方向反转!** ON 现真正高于 OFF |
| ON win-rate (非零) | 1/3 = 33% | **2/3 = 66.7%** | 接近 70% 阈值 |
| W/T/L (semantic_anchor_dual) | 1 / 18 / 2 | **2 / 19 / 1** | 新 WIN (a7), 减 LOSS (a2 改 BOTH_LOSS, 不计 LOSS) |
| key_jaccard | 1.0 / 1.0 | 1.0 / 1.0 | 不变 |
| funnel mean_jaccard | OFF 0.839 / ON 0.933 (+0.094) | 同 | 不变 |

## 维度复判 (Plan v3.1 #1 primary metric)

| 阈值 | 1st review | **2nd review** | 状态变化 |
|---|---|---|---|
| dual ≥ 0.85 (绝对值) | ON 0.9048 ✓ | ON 0.9091 ✓ | ✓ |
| **Δ dual ≥ +0.10** | −0.048 ✗ | **+0.046 ✗** | **从负转正,但未达阈值** |
| ON win-rate ≥ 70% | 33% ✗ | **66.7% ✗** | **接近未达** |
| key ≥ 0.85 | 1.0 ✓ | 1.0 ✓ | ✓ |
| funnel 全套 | ✓ | ✓ | ✓ |

**整体 Tier 0 (硬口径): 仍 NOT PASS**, 但所有失败项都从"明显 fail"变成"边缘 miss",且方向全部正向。

## D8 改动逐 image 真相 (本次 image-粒度 GT 锁定)

| image | OFF 位置 | ON 位置 | D8 改动评价 |
|---|---|---|---|
| pdf_sop image 10 | step 4.2 | step 4.1 | ✅ **修正** — image 是 4.1 字段输入参考 |
| pdf_sop image 9 | step 4.2 | step 3 | ❌ **真退步** — image 显示填表后期,不是入口阶段 |
| xs_wi_007 image 1 | step 2 | step 1 | ✅ **修正** — step 1 文本明说用产品标识卡 |
| xs_wi_007 image 2 | step 2 | step 2 (未动) | ⚠️ **未完全修复** — image 2 是 step 1 抄录产物,应跟 image 1 一起到 step 1 |

**结论**: D8 Path A/B/C 是 **net positive 但不完美**:
- 2 个 image 改对了 (10, xs_1)
- 1 个 image 改过头了 (9)
- 1 个 image 没改到 (xs_2)

## 决策矩阵

按 Plan v3.1 硬口径 NOT PASS,但实质上证据已变:

| 选项 | 论据 | 风险 |
|---|---|---|
| **A. 切默认 ON (推荐 — 在小心样本边界下)** | Δ 已正向; dual_jaccard ON 0.91 > OFF 0.86; 2 win/1 loss/1 mixed; funnel 大涨 9.4pp | Plan v3.1 硬口径未达 Δ+0.10 与 win-rate 70%,但 n=22 样本小,门槛设计可能过严 |
| **B. 留 ON 待 chunker 修两个残余问题再切** | image 9 → step 3 是真退步; image 2 → step 1 未完全修复; 等团队修这俩再 promote | 留 ON 关闭意味着继续承担 funnel 维度 -9.4pp 损失 |
| **C. 转 Tier 1/2 扩样本** | n=22 太小, 转 60+ anchor 复测 noise 范围 | 工作量大,但 plan v3 已规划 Tier 1 (Step E) |

**我的推荐: 选 B (chunker 修后再 promote)**:
- a5 / a2 暴露的不是 GT 问题而是 chunker 设计问题 — 应修 chunker
- 具体: image 9 不该被 Path C (range-ref) 拉走,image 2 应该跟 image 1 一起 Path A (content-match)
- 修完两个 chunker 残余问题再跑 Tier 0,预期能突破 Δ+0.10 与 win-rate 70% 阈值

## 接下来

修 chunker 两个残余问题 (image 9 误移 + image 2 漏改) 后,可以:
1. 重跑 Tier 0 验证两阈值通过
2. 进入 Step D 完整 Tier 0 → Step E Tier 1 conditional gen → Step F Tier 2 双索引 e2e
3. 整体 PASS 后切 SAE 默认 RAG_IMAGE_CONTENT_OVERRIDE=1 + 启动 A 类 91 doc 生产重灌

或者按 plan v3 推进 Tier 1 扩样本,在 60+ anchor 上确认 Δ 仍正向,再切默认 ON 接受 partial 状态。

---

**框架副产品**: 本轮 review 流程 (dump → widget → 用户裁决 → image-粒度精确拆分) 形成可复用的 GT 维护标准: 不为追平指标而宽容化 anchor 定义,让评测诚实反映 partial state。这是 chunker_ab framework 的元贡献。
