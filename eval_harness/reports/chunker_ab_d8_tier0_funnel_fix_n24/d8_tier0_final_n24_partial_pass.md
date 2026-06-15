# D8 Tier 0 PDF-only n=24 — Partial PASS (5/6 primary, Δ FAIL)

> Analysis: chunker-ab Workflow (3 analyzer + 2 critic + 1 synth, agent_count=6, subagent_tokens=391k)
>
> Report scope: PDF-only Tier 0 验收。Docx/xlsx 扩样 + GT 独立化复测列为 Tier 1 准入前置。

## Executive summary

D8 chunker A/B (Path C 收紧 + Path D 实施) 在 n=24 anchor + funnel GT 业务真相对齐后**方向性通过、统计未显著**。Semantic anchor primary 6 阈值 5/6 通过 (dual ON 0.958、wins 100%、0 critical、key 1.0、n=24 floor), **唯一未达 Δ ≥ +0.10** (实测 +0.083, 缺 0.017pp); 95% CI 含 0、McNemar p ≈ 0.25, 统计不显著的根因是 n=24 样本量 (21 PDF anchor + 3 admin_lodging) 不足以让 +8pp 收益显著。Funnel regression reference 维度 4/4 阈值全 ✓ (ON 0.922、Δ +0.067、3W/0L), **但本轮 GT 做了 3 处业务真相对齐恰好覆盖 D8 ON 行为差异点, funnel 降级为 informational**, 不再作独立 regression PASS 门。

实质净收益 = **4 image 重新正确绑定** (sop image 9→4.2、image 10→4.1、xs_wi_007 image 1+2→step 1) + **1 次 Path D activation** (xs_wi_007 image 2); 0 critical regression、上轮 1 LOSS 翻 WIN。**结论:接受为 D8 Tier 0 PDF-only 验收, Tier 1 前置必须扩 docx/xlsx 池 + GT 独立化复测**。

---

## funnel 稳定性 (降级 informational)

| doc | n | OFF | ON | Δ | 上轮 ON | 变化 |
|---|---|---|---|---|---|---|
| pdf_it_xxh_003 | 10 | 0.900 | 0.900 | 0.000 | 0.900 | 0 |
| pdf_sop | 11 | 0.879 | 0.970 | +0.091 | 0.879 | +0.091 |
| pdf_xs_wi_007 | 9 | 0.778 | 0.889 | +0.111 | 0.722 | +0.167 |
| **总体** | 30 | **0.856** | **0.922** | **+0.067** | 0.839 | +0.083 |

**WIN (3)**: pdf_sop 步骤4.1 (0.5→1.0) / 步骤4.2 (0.5→1.0, 上轮 TIE 翻) / pdf_xs_wi_007 步骤2 (0.0→1.0, **上轮 LOSS 翻 WIN**)
**LOSS (0)**: 上轮 1 LOSS (xs_wi_007 步骤2) 经 GT image 2 归 step 1 后翻为 +1.0 WIN

**4 阈值全 ✓**, 但因 GT 修正与 D8 ON 行为差异点重叠, **降级 informational** 不作 PASS 门。下轮恢复 reference 地位需冻结 GT 后再跑 chunker 改动。

---

## anchor Δ 稳定性 (5/6 primary 通过, Δ FAIL)

| 指标 | n=22 (path_c_d) | **n=24 (funnel_fix)** | 变化 |
|---|---|---|---|
| dual OFF | 0.864 (19/22) | **0.875 (21/24)** | +0.011 |
| dual ON | 0.955 (21/22) | **0.958 (23/24)** | +0.004 |
| **Δ dual** | **+0.091** | **+0.083** | **−0.008** |
| W/T/L | 2/20/0 | **2/22/0** | +2 TIE |
| ON win-rate | 100% | **100%** | = |
| key_jaccard | 1.0/1.0 | 1.0/1.0 | = |

新加 a8 (step 1.2 image 2) / a9 (step 3.2 image 7) 都 OFF/ON 同绑 ✓ Path A/B/C/D 均不触发。

### Plan v3.1 #1 primary 6 阈值复判

| 阈值 | 实测 | 判定 |
|---|---|---|
| dual ON ≥ 0.85 | 0.958 | ✓ |
| **Δ ≥ +0.10** | **+0.083** | **✗ 缺 0.017pp** |
| ON wins ≥ 70% | 100% | ✓ |
| 0 critical | 0 loss | ✓ |
| key ≥ 0.85 | 1.0 | ✓ |
| n ≥ floor | 24 | ✓ (PDF-only 上限) |

**5/6 通过, 唯一 Δ FAIL**。

### 统计显著性

- 95% CI (正态近似, p=0.917): Δ ± 1.96σ = **[−0.027, +0.194], 含 0**
- McNemar exact (b=2, c=0): **p ≈ 0.25 单尾, 未达 p < 0.05**

### Δ 微降归因

a8/a9 均 dual=1, 分子分母同 +2, 净 wins 不变 (2)。Δ = 2/n: 2/22 → 2/24 = 机械稀释 −0.0076。**不是 ON 退化**, 是 baseline TIE 拉低 marginal denominator。

---

## Path C + Path D generalization (non-regression on n=24)

### 1. 4 张关键图 provenance (ON arm)

| 图 | doc | step ON | step OFF | 归因 |
|---|---|---|---|---|
| image 9 | pdf_sop | 4.2 ✓ | 4.2 ✓ | Path A weak override, **Path C 收紧后不触发** |
| image 10 | pdf_sop | 4.1 ✓ | 4.2 | Path A content-match override |
| image 1 | pdf_xs_wi_007 | 1 ✓ | 1 ✓ | **Path A strong override** (alt 21 vs geo 2, ≥10.5x) |
| image 2 | pdf_xs_wi_007 | 1 ✓ | 2 | **Path D propagation** (seed=image 1 strong, route_reason=cluster_propagation) |

### 2. Path D 触发列表 (ON arm)

| count | doc | page | img | seed_img |
|---|---|---|---|---|
| 1 | pdf_xs_wi_007 | 1 | 2 | 1 |

n=22 → n=24, Path D **仍只触发 1 次**, 无新 false positive。8 守门在扩样下未放宽。

**Non-regression on 2 new baseline anchors confirmed; Path D activation evidence remains n=1**。真正 generalization 需要在 docx/xlsx 池里看到 ≥3 个独立 Path D 触发。

---

## Critic 摘要

**Completeness (9/9 数字一致)**: per_case 总行、funnel mean、Δ、W/T/L、anchor dual、key 全对。

**Critic 修正 (已采纳)**:
1. **n=24 = 21 PDF + 3 admin** (不是"4 PDF 池硬限"); 若只看 PDF 21, Δ = 2/21 = 0.0952 (差 0.005pp)
2. **funnel GT 修正 = post-hoc aligned**: 3 处 GT 修改恰好对齐 D8 ON 差异点, 先有 chunker 行为再有 GT, confirmation-bias loop → funnel 降级 informational
3. **Δ 0.083 vs 0.10 = miss, 不是"实质 PASS"**: 三项独立证据 (Δ 缺 0.017pp、CI 含 0、p≈0.25) 都说统计未显著
4. **Path D 1 次 ≠ generalization**: a8/a9 是 baseline 同绑, 只证非回归, 不证激活泛化

---

## 下一步决策矩阵

| 选项 | 推荐 | 工作量 / 风险 | 期望产出 |
|---|---|---|---|
| **(a) 切默认 ON + 91 doc 生产重灌** | **推荐 (分批)** | Path C+D OFF byte-equal 已验, ON 守门严. **风险**: Path D activation n=1, 跨格式触发率未知. **成本**: DAG 1-3 全量, PROD-RW token, stage3 spot_checker 复核. **建议先 10 PDF 试点 → 全量 91**. | image binding 正确率 +8pp, 4 张关键图修正, xs_wi_007 step 1/2 答案恢复 |
| **(b) 扩 docx/xlsx anchor 池到 n≥60** | **推荐 (与 c 串联)** | 单扩 PDF 不可达 n=60, 需 docx/xlsx 真值 GT (~30 anchor × 5 min = 2.5h). | 若 Δ 仍 +0.08 但 n=60 → McNemar p<0.05 显著; 若 docx/xlsx 出新 LOSS 则反向证伪. **核心价值: 把 Δ 从未显著升到显著** |
| **(c) 转 Tier 1 (Plan v3 Step E) conditional gen** | **推荐 (前置 6/8 就绪)** | 前置: Tier 0 验收 (本轮 Partial PASS, 可放行) + D8 默认状态决策 (= 选项 a) + 反例语料 + judge 策略 + **GT 独立化** (Critic 4) + **docx/xlsx anchor 扩样** (= 选项 b). | step↔image bipartite F1, 引入 conditional gen 减 hallucination, D8 整体收口 |

**推荐序列**: 先 **(a) 分批生产重灌** (10 PDF 试点 → 全量 91), 并行 **(b) docx/xlsx anchor GT 扩样** 作 Tier 1 准入, **(c) Tier 1 启动前置 = funnel GT 独立化冻结 + docx/xlsx Path D ≥3 触发证据**.

---

## 工程债清单

1. **funnel GT post-hoc aligned**: 本轮 3 处对齐在看到 D8 ON 后做出, 违反 regression GT 独立性。**TODO**: Tier 1 前由独立 oracle 盲审重标
2. **PDF anchor 池上限 = 24** (21 PDF + 3 admin): plan v3.1 #1 写 n=60+ 时未预见硬限。**TODO**: 扩 docx (找有 step↔image 的 docx) + xlsx (procedure_image_guide 多图同行)
3. **Path D activation 仅 n=1**: xs_wi_007 image 2 是唯一触发, 其他 23 anchor 均不触发。**TODO**: docx/xlsx 扩样后看 ≥3 独立触发
4. **"PDF 池硬限"措辞修正**: 实际是 21 PDF + 3 admin, 措辞统一避免 Tier 1 误判 admin 类饱和
5. **statistical floor 缺失**: v3.1 #1 阈值未配 n_min, n=24 时 Δ +0.083 实质无法显著。**TODO**: 升级 plan v3 阈值, Δ 配 McNemar p<0.05 或 95% CI 下限 > 0
6. **Path C 收紧后实测 0 触发**: image 9 走 Path A weak override 锚定 4.2, Path C 二次门 ≥3 在本样本未达。**TODO**: 确认 Path C 是否真有正样本或保留作 D8 Phase 9 假阳验证
7. **生产重灌 deactivation 不变量**: 选项 (a) 分批前必须预演 spot_checker PENDING_DELETE 复核窗口
8. **report 体量 vs 信号比失衡**: 本轮 3 analyzer + 2 critic 围绕 4 image + 1 Path D trigger。**TODO**: 后续 chunker A/B 直接"单文件 ≤ 2 页 + analyzer 1 篇", critic 仅 PASS/FAIL 边缘介入
