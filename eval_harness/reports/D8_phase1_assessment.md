# D8 Phase 1 评估发现 — 2026-06-13

> Phase 1 计划:**P1-a** 给 PDF `_insert_image_refs_heuristic` 策略 1b-2 主 overlap-max 循环加 5 行 `if y0 > img_y0: break`(synth 推荐解 PDF 4.1/4.2 互换)+ **P1-b** 给 xlsx P2 naturals 加 content-gate(env-gated,解 task_1e52dec0 step3 应空残留 1.5pp)。**实施后两改动都暴露 synth 漏诊,本轮零代码改动,只固化两大评估发现。**

## TL;DR

| 维度 | synth 评估推荐 | 实测结论 | 决策 |
|---|---|---|---|
| **PDF 4.1/4.2 互换** | 5 行 `if y0 > img_y0: break`(几何 reading-order) | **regression**:image 5/6 从 step 3.1 错绑到 step 2 | **回退,移入 Phase 2** |
| **xlsx step3 应空 1.5pp** | content-gate(用 `_content_match_steps`) | **content-gate 没有新信号,反而误杀 nat_si 正确命中** | **回退,延期到 chip 算法改造** |
| **xlsx Jaccard 0.8636 → 1.0(实测!)** | — | **用户并行 session 的 ref_keys.py filename 次级身份升级 + GT 标 filename 已解** | **接受新基线 1.0,可升 hard ≥0.95** |

## 发现 1:PDF 4.1/4.2 不是几何 bug,是语义关联

### synth 假设(根因猜测)
overlap-max 偏向"段落跨度大且包含图片"的下方段落(如 step 4.2 段落跨页 b_y1>>img_y1),让真该绑的上方紧邻段落(step 4.1)输掉。修法:加 `if y0 > img_y0: break` 让"块开头在图片上缘下方"的段落不参与 overlap,fallback 接手取上方最近。

### 实测真相

跑 PDF chunker 实地 dump anchors+assets:

```
image 10 (page 3) bbox y0=401.4 y1=661.6  (跨度 260pt!)
step 4(父)     paragraph y0=105 y1=116   → overlap = 116 - 401 < 0  ❌
step 4.1       paragraph y0=130 y1=162   → overlap < 0  ❌
step 4.2 标题  heading   y0=167 y1=178   → overlap < 0  ❌
step 4.2 ⑤⑥   paragraph y0=183 y1=209   → overlap < 0  ❌
⑦              paragraph y0=474 y1=485   → overlap = 485-474 = 11  ✓
"重复"         paragraph y0=682 y1=693   → overlap < 0  ❌
```

**没有任何 step 4.x 段落与 image 10 有 overlap**(所有 4.x 段落 b_y1≤209,远低于 img_y0=401)。主循环只 ⑦ 段落命中(overlap=11)。fallback 取"上方最近 y0 ≤ 401"= ⑤⑥ 段落 y0=183(归属 step 4.2)→ 实测 image 10 → step 4.2。

**GT 给 [10] → step 4.1 是基于语义**(image 10 是手写订单填写示例 = "按交货单填①设备②班次③数量"的图示,内容关联 step 4.1 而非 step 4.2 的"④根据设备带出班组人员")。**几何 break 修不了语义关联**。

### 加 break 实测 regression

```
改前: step 2=[4], step 3.1=[5,6], step 4.1=[], step 4.2=[9,10]
改后: step 2=[5,6,4] ❌, step 3.1=[] ❌, step 4.1=[], step 4.2=[9,10] 不变
```

image 5/6(page 2)的 step 3.1 段落 y0=424 b_y1=434(短段落 10pt),与 image 5/6 实际 overlap 仅 ~10pt。加 break 后 step 3.1 段落 y0=424 > img_y0(~390) → break,主循环 best_idx=None,fallback 取 ① 段落(y0=235,归属 step 2)→ image 5/6 错绑 step 2。

**结论**:PDF 这条 bug 的真实修法 = 给 `_insert_image_refs_heuristic` 加 `_content_match_steps` 内容匹配(类似 xlsx P0)。这是 Phase 2 范围,要扩 PDF GT 到 ≥3 doc 才能稳标阈值。

## 发现 2:xlsx step3 应空残留不是 chunker bug,是评测口径粗

### synth 假设
P2 naturals "位置对位"硬塞图到 step3(应空)。修法:加 content-gate 在 nat_si 命中前用 `_content_match_steps` 验证内容匹配,不达标 → skip(orphan)。

### 实测真相

跑 `_content_match_steps` dump 每个 xlsx_sop asset → step 的 score+margin:

| asset(visual_summary 摘要) | best_step | score | margin | gate ON 结果 |
|---|---|---|---|---|
| anchor=10 水平仪/传感器 | **1** ✓ | 1.00 | **0.00** | skip → orphan ❌(应进 step1) |
| anchor=11 一只手插电源 | **4** ❌(应 2) | 1.00 | 0.50 | 误派 step4 |
| anchor=12 天平归零按键 | **4** ✓ | 1.20 | 0.50 | 命中 step4 ✓ |
| anchor=12 空白称重盘 | **3** ❌(应 5) | 1.20 | 0.50 | 误派 step3 |
| anchor=14 口罩称重 3.098g | **3** ❌(应 5) | 1.20 | **0.00** | skip → step5 丢图 ❌ |
| anchor=15 另一只手插电源 | **3** ❌(应 2) | 1.00 | **0.00** | skip → orphan |

**关键 insight**:既有 P0 content-match(`score>=0.8 + margin>=0.5`)已经把"内容明确匹配"的 asset 吃完(anchor=12 归零图 → step4 命中 P0)。**P2 naturals 进来的本来就是"内容信号不够"的 asset**,再用同一算法 gate 它们,等于复用没增加新信号。

- 把"插电源"图(应 step2 仪器开启)误派 step3/4(因"电源"在 step2+step6 文本都有,IDF 抵消)
- 把 step1(水平仪)、step5(口罩称重)的图 margin=0 skip,让两个 step 丢图

**含义**:**"哪个 step 应空"是先验信息(GT 知道,chunker 不知道)**,无法从既有 visual_summary/ocr 与 step text 算出来。要修必须加新信号(如 step text 缺"图引用"标记 → 视为 noimage step)。

### 用户并行 session 已经实质解了 1.5pp 残留

D7 报告 xlsx Jaccard = 0.8636。Phase 1 实施前重新跑 verify(`bash scripts/day7_chunker_postfix_verify.sh --n-runs 3`)→ xlsx = **1.0000**!根因是用户改动:

**`eval_harness/binding/ref_keys.py`**(+44):xlsx `strict_key` fmt-gated 升级 — 当 GT 显式标 `filename` 时使用 `("xlsx", block_index, filename)`,否则退回旧 `("xlsx", block_index)` 兼容语义。

**`gt_xlsx_pptx_analysis.json`**(GT):同 anchor_row=12 的多图分别标 `filename`:
```json
step4 调零 → block_index=12 filename=xlsx_sop_sheet0_img0002.jpeg
step6 关闭 → block_index=12 filename=xlsx_sop_sheet0_img0004.jpeg
```

pred chunker 给 step4 和 step6 各分一张图(block_index 都是 12,filename 各不同)→ filename 升级 strict 后,step4 与 step6 都 ✓,xlsx_sop Jaccard 升 1.0。

**这是"同 anchor 多图消歧靠 filename 而不是 chunker 算法改造"的更经济解法**。task_1e52dec0 算法改造可关闭。

## 决策与下一步

### 立即变更(本 commit)
- **本轮 0 代码改动**:回退 P1-a + P1-b(完全 byte-equal HEAD)
- **写 D8 评估报告**(本文件)固化两大诊断
- **将 xlsx Jaccard 锁档基线从 0.8636 → 1.0**(下一轮 D8 验证报告补)
- **可升 xlsx Jaccard hard ≥0.95**(D7 升 ≥0.85 太保守,新基线 1.0 留 5pp 缓冲)

### 留 chip(下次会话或独立 session)

| Chip | 描述 | 优先级 |
|---|---|---|
| **PDF Phase 2:加 content-gate 到 `_insert_image_refs_heuristic`** | 仿 xlsx P0 把 `_content_match_steps` 应用到 PDF 路径,解 4.1/4.2 语义错位 + 3.1 跨页;需扩 PDF GT 到 ≥3 doc 才能稳阈值 | 中 — image_binding 3.412→? 提升空间 ≤0.5,ROI 不高 |
| **task_1e52dec0 关闭(已被 filename 升级替代)** | xlsx step3 应空残留靠 ref_keys.py + GT filename 升级解了 1.5pp,chunker 算法改造不必做 | **关闭** |
| **Phase 2 完整方案(range-ref + content-gate)** | PDF 3.1 "②-⑥步操作" range-ref 解析,把 image_index 2..6 都绑给 step 3.1(不仅靠几何) | 低 — 影响 1 个 chunk 5 张图,边际 ROI |

### 学到的事(本轮)
- **synth 工作流的盲点**:几何根因猜测过早,没在 dump anchors/blocks 之前固化"问题是几何还是语义"。下次类似工作流应在 synth 前先跑一轮 anchor dump,把根因层次锁住。
- **content-gate 没有 free lunch**:既有 P0 content-match 阈值已经吃完"高置信"asset,P2 进来的本质是"低置信",用同一算法 gate 它们等于自欺。
- **GT 升级是更经济的"chunker bug"消解器**:同 anchor 多图消歧不需要改 chunker,只需 GT 标 filename + ref_keys.py fmt-gated 兼容升级。

## 工件
- 本报告
- `scripts/day7_chunker_postfix_verify.sh` 3 连跑 verdict = ALL_EQUAL,xlsx=1.0/pdf=0.7273/docx=0.9847(D7→D8 verify 数据见 `scratch/day7_chunker_verify_20260613_014409/compare.md`)
- 无代码改动(`git diff HEAD opensearch_pipeline/pipeline_nodes.py` 对比用户并行 session 的 +242 行,我自己 0 行)
