# D8 Phase 3 评估发现 — chunker dotted 切分 image_ref 归属 — 2026-06-13

> Phase 3 计划：用户提出 "image 10 在 chunker `_chunk_by_step` 按 dotted 子序号切分单一 multi-segment paragraph 时被默认归到后一个 segment（step 4.2）；改为按圈号/邻近性归到前一个（step 4.1）应得 +1 chunk Jaccard，PDF 0.7273 → 0.818"。本轮 dump 现状 block 流 + matcher trace 实证 **用户假设不成立**：step 4.1 / 4.2 在 raw extraction 后是 **三个独立 block**，根本没有 multi-segment paragraph；同时浮现 **Bug C** — eval matcher 把 GT 3.1 误配到 step_no=4 父（无图）而非含 [5,6] 的真 step 3.1。**本轮零代码改动**，固化三个发现 + 留 2 个独立 chip。

## TL;DR

| 维度 | 用户预期 | 实测结论 | 决策 |
|---|---|---|---|
| **Bug A 根因 = chunker multi-segment paragraph 切分** | image_ref 应跟 dotted sub_no 最近的 sub-chunk，而非默认 last | **假设不成立**：dump 显示 step 4.1 / 4.2 标题 / 4.2 body 是 **3 个独立 block**，没有多 segment 切分场景 | 撤销假设 |
| **真实根因（重新定位）** | — | image 10 几何位于 step 4.2 内容流之后（#29 在 ⑤⑥ + ⑦ 之间），current_step = 4.2 是真值；唯一可行修法是 content-match 跨 step 重派，已被 Phase 2 实证 net regression | 不再尝试 |
| **新发现：Bug C — eval matcher 误配 GT 3.1** | — | density-max 在 typed pool 内选了 step_no=4 父 (recall=0.60, d=0.457, len=43, imgs=[]) 而非 step_no=3 main (recall=0.60, d=0.161, len=349, imgs=[5,6]) | 留独立 chip — 修后 PDF Jaccard 可升 0.7273 → 0.7879 |
| **PDF GT 仅 1 doc 限制** | 用户提示 "建议先扩 ≥3 doc 后再做" | 实证三次了：Phase 1 几何 break、Phase 2 content-match、Phase 3 dotted 切分假设 — 都是 single-doc 不足以稳标 | 接受用户建议 |
| **跨页 image 9（step 3.1 缺）** | image_index 跳号 + 跨页 range-ref | image 9 几何插入位置在 page 3 ⑤⑥ paragraph 之后（#27），整体归 step 4.2；要绑回 step 3.1 必须实现 "②-⑥步操作" range-ref 解析 | 留 chip — 与 GT 扩充一起规划 |

## 实测数据

### 基线复现

```
$ RAG_ENV=prod_ro RAG_READONLY=true python scripts/eval_image_binding_pdf.py
PDF docs:                1 (degraded: 0)
Strong GT chunks (∋图):  11
Mean Jaccard:            0.7273  (std=0.4671)
逐文档:
  pdf_sop  strong=11 jaccard=0.727  step_cards=10  dup=1.00
```

逐 chunk gt vs produced：

| step | gt expected | produced | Jaccard |
|---|---|---|---|
| 步骤1.1 收取交货单 | [1] | [1] | 1.0 |
| 步骤1.2 核对错误处理 | [2] | [2] | 1.0 |
| 步骤1.3 抄录订单信息 | [3] | [3] | 1.0 |
| 步骤2 交货单分类 | [4] | [4] | 1.0 |
| **步骤3.1 U8扫码报检** | **[5, 6, 9]** | **[]** | **0.0** ← matcher 配错（Bug C） |
| 步骤3.2 扫码枪扫描 | [7] | [7] | 1.0 |
| **步骤4.1 填写设备班次** | **[10]** | **[]** | **0.0** ← chunker 几何归 4.2（Bug A） |
| **步骤4.2 班组人员** | **[]** | **[9, 10]** | **0.0** ← image 10 误归 + image 9 跨页留这（Bug A + 跨页） |
| 步骤5 群通知完成 | [11] | [11] | 1.0 |

### 关键 dump：page 3 实际 block 序列（_insert_image_refs_heuristic 之后）

```
#22  paragraph pg=3 y=95.0-102.4    | 生效日期：2022年8月25日
#23  paragraph pg=3 y=105.6-116.0   | 步骤4：报检（根据交货单信息在U8扫码报检处填相对应数据）；
#24  paragraph pg=3 y=130.4-162.8   | 4.1 按《交货单》填写①设备（G22）...②班次、③数量(备注数的产量...
#25  heading   pg=3 y=168.0-178.4   | 4.2 填写完后，依次点击"④根据设备带出班组人员"若人员不对，以交货单为准进行修改...
#26  paragraph pg=3 y=183.6-209.6   | "⑤根据交货单备注数的填写数量"、"⑥报检"完成报检；若"报检"无反映...
#27  image_ref pg=3 bbox y=218-392  | idx=9  visual: U8系统'档案查询'界面截图  ← 插入位置：⑤⑥ paragraph 之后
#28  paragraph pg=3 y=474.8-485.3   | ⑦  ← 单独的圈号标注 paragraph（无 circled_label flag）
#29  image_ref pg=3 bbox y=401-661  | idx=10 visual: 手写订单填写表单  ← 插入位置：⑦ paragraph 之后
#30  paragraph pg=3 y=682.8-693.9   |  重复步骤3.2--步骤4，直至《交货单》报检全部完成；
#31  paragraph pg=3 y=713.9-724.4   | 步骤5：发送到"注塑车间仓管"群通知统计报检完成（图⑦）。
```

`_chunk_by_step` 处理顺序：

1. **#23** 步骤4 → 新开 step_no=4 main
2. **#24** "4.1 ..." → STEP_BOUNDARY_RE 命中 "4.1" → 新开 step_no=4 sub=1 sec=4.1（含 ①②③ 文本）
3. **#25** "4.2" heading → 新开 step_no=4 sub=2 sec=4.2（heading + ④ 文本）
4. **#26** "⑤⑥..." → 无 step boundary → append 到 current_step = step 4.2 body
5. **#27** image_ref idx=9 → `current_step["image_refs"].append(extra)` → **step 4.2 拿到 image 9**
6. **#28** "⑦" paragraph → append 到 current_step = step 4.2
7. **#29** image_ref idx=10 → **step 4.2 拿到 image 10**
8. **#30** "重复步骤3.2--步骤4" → 若 STEP_BOUNDARY_RE 命中 "3.2"/"步骤4" 则新开（实测：appends 到 4.2）
9. **#31** "步骤5" → 新开 step_no=5

最终 step 4.2 image_refs = [9, 10]，step 4.1 = []。这正是当前观察到的现象。

## 发现 1：用户假设的 "multi-segment paragraph" 不成立

用户 brief：
> "step 4 文本在 raw extraction 后**很可能是单个 paragraph block**，内含 '①设备②班次③数量' + '④根据设备带出班组人员⑤⑥报检' 两组圈号。image 10 几何定位到这个 block 之后，`_chunk_by_step` 按 dotted 序号把这个 block 切成 step 4.1 / 4.2 两个子 chunk，image_ref 应该归到 step 4.1 但实测归到 step 4.2。"

实测（dump #24 / #25 / #26）：
- step 4.1 ①②③ 在 **#24 单独 paragraph y=130-162**（"按《交货单》填写①设备②班次③数量"）
- step 4.2 标题 + ④ 在 **#25 单独 heading y=168-178**（"4.2 填写完后,依次点击'④根据设备...'"）
- step 4.2 ⑤⑥ 在 **#26 单独 paragraph y=184-210**（"'⑤根据交货单备注数...'、'⑥报检'完成报检"）

三个独立 block，pdfplumber 已经按段落抽好。`_chunk_by_step` 走的是 line 530-538 的 image_ref 归 current_step 路径，不是 line 599-712 的 multi-segment paragraph 切分路径 —— **multi-segment 切分根本没参与**。

**含义**：用户 brief 里的修法 ("image_ref 在 dotted 切分时应该跟 dotted 子序号最近的那个 sub-chunk") **不适用** —— 没有 dotted 切分场景可改。

## 发现 2：真实根因 = 几何 vs 语义错位（已被 Phase 1/2 实证不可单 doc 解）

image 10 的 bbox y0=401, y1=661 完全在 step 4.2 文本（y≤209）和 "重复" 段落（y=683）之间的 **几何空隙**。pdfplumber/heuristic 把它锚定到 #28 ⑦ paragraph 之后（best_idx 来自 overlap-max：⑦ y=475-485 与图片 y=401-661 overlap=10pt 是页面唯一非负 overlap）。

要让 image 10 归到 step 4.1（GT 期望），可行手段只有：
- **(a) 重排 reading order**：Phase 1 P1-a 尝试 `if y0 > img_y0: break`，实证 step 3.1 image 5/6 反被误派 step 2 → 单 doc 阈值无解
- **(b) 跨 step content-match**：Phase 2 把 `_content_match_steps` 嵌入 `_insert_image_refs_heuristic`，实证 step 2 / 3.1 / 3.2 三处误派，net -0.0909 → cand 粒度（paragraph 级零散 vs 整段 step）不匹配
- **(c) 圈号匹配**：image 10 visual_summary="手写订单填写表单"、ocr_text="手册号:一般贸易 生产数量:450 ..."，**不含任何圈号**（①②③ 与 ④⑤⑥ 都没有）→ 无法用圈号歧义

三个手段都无单 doc 解，与 Phase 2 结论一致：**几何位置 + 内容匹配 ≠ 解决一切**，是 PDF 作者排版 vs 语义关联的根本错位，需更宽 GT 集才能稳定区分 "geometrically-after-N-yet-semantically-N-1" 模式 vs "geometrically-after-N-and-semantically-N" 模式。

## 发现 3：Bug C — eval matcher 把 GT 3.1 误配到 step_no=4 父

`_match_gt_chunk_to_produced` 在 typed pool 内用 `density = hits / sqrt(len)` 选最高分。GT step 3.1 keywords = `['U8', '扫码', '报检', '业务导航', '质量管理']`，per-chunk 排名（按 density 降序）：

```
ct=step_card  step=4    sub=None sec=None   recall=0.60 d=0.457 len= 43 imgs=[]       ← matcher pick (Bug C)
ct=step_card  step=3    sub=2    sec=3.2    recall=0.40 d=0.167 len=144 imgs=[7]
ct=step_card  step=3    sub=None sec=None   recall=0.60 d=0.161 len=349 imgs=[5, 6]   ← 真 step 3.1
ct=step_card  step=5    sub=None sec=None   recall=0.40 d=0.138 len=211 imgs=[11]
ct=step_card  step=4    sub=2    sec=4.2    recall=0.40 d=0.101 len=394 imgs=[9, 10]
```

step_no=4 父 chunk 文本只有 43 字（"步骤4：报检（根据交货单信息在U8扫码报检处填相对应数据）"），偶然包含 "U8/扫码/报检" → recall 0.60 同分但 density=0.457 远高于 step_no=3 main 的 0.161（后者 349 字含 OCR 注入文本 "业务导航/质量管理" 才命中真 keywords）。

**含义**：当前 0.7273 基线里有一部分 jaccard=0.0 是 **eval 测量偏差**，不是 chunker 真错：chunker 已经正确把 [5,6] 绑到了含 step 3.1 文本的真 chunk，matcher 没找到它。

### 假设修 Bug C 的预估增量

如果 matcher 用 GT 标签里的步骤号（"步骤3.1" → step_no=3 或 sec_no=3.1）做次级 tiebreak，GT 3.1 配回 step_no=3 main：
- 真 produced=[5, 6]，GT expected=[5, 6, 9] → Jaccard = 2/3 = 0.667
- 单 chunk 修正：0.0 → 0.667
- **PDF mean Jaccard：0.7273 → 0.7879**（+0.0606）

这是一个 **eval-only 改动**，不动 chunker，单 doc 可验证、跨 doc 不会引入回归（matcher 改动对其他 chunk 只可能更严格匹配 step_no）。

## 决策与下一步

### 立即变更（本 commit）

- **本轮零代码改动**（chunker 与 matcher 都不动）
- 写本评估报告固化三个发现
- **保持 PDF Jaccard 锁档基线 0.7273**（不升 hard）

### 留 chip

| Chip | 描述 | 优先级 | ROI |
|---|---|---|---|
| **Bug A：image 10 几何 vs 语义错位** | Phase 1/2/3 已三次实证单 doc 无解；要么扩 PDF GT ≥3 doc 后重新设计跨 step content-match（带 cand grouping），要么接受 PDF 该 chunk 永久 -0.0 | 低 | 边际 ≤+0.0909（修 4.1 + 4.2 两 chunk）|
| **Bug C：eval matcher 用 step_no/sec_no 次级 tiebreak** | 在 `_match_gt_chunk_to_produced` 的 typed pool 选择里，先按 GT label 提取的步骤号过滤（"步骤3.1" → 优先 sec_no="3.1" 或 step_no=3 的 candidate），命中后再 density-max | **中** | +0.0606（修 step 3.1 一个 chunk 0.0→0.667），eval-only 风险低 |
| **跨页 image 9（step 3.1 缺）** | image 9 在 page 3、step 3.1 在 page 2，纯几何无法绑；要 "②-⑥步操作" range-ref 解析 → 把 image_index 5/6/9 都绑给 step 3.1 | 低 | 与 Bug A 同 chunk，叠加增量 |
| **PDF GT 扩到 ≥3 doc** | Phase 1/2/3 一致结论 — 任何 PDF chunker/matcher 改动都需要跨 doc 验证；挑 2 个新 PDF SOP 标 binding GT | **中** | Phase 4+ 前置 |

### 学到的事（本轮）

- **诊断假设要先 dump 再验证**：用户 brief 说 "step 4.1/4.2 是同 paragraph 多 segment"，dump 显示是三个独立 block。如果直接基于假设动 `_chunk_by_step`（按 dotted sub_no 改归属），改动会落空（无 multi-segment 场景触发）+ 引入意外副作用
- **眼前的 Jaccard 0.0 可能是测量 bug，不是绑定 bug**：step 3.1 一直被认为是 "chunker 没绑 image 5/6"，dump 才发现 chunker 绑对了，是 matcher 选错 chunk
- **几何 vs 语义错位是真实物理现象**：作者把 "怎么填表" 的图放在 "①②③" 文字下方但跨过 "④⑤⑥" 文字 — 图与文不在 reading-order 相邻区，没有任何单 doc 启发式能定向解
- **三轮 Phase 共同结论**：D7 锁档基线 PDF 0.7273 在没有更宽 GT 集前 **不应作为待优化目标**，应作为已知上限接受

## 工件

- 本报告
- `scratch/binding_pdf_20260613_0253.json`（baseline 实测，per-chunk gt vs produced 完整）
- `scratch/d8_phase3_pdf_dump.json`（chunker 输出每个 step_card 的实际 image_refs + visual_summary）
- `scratch/dump_pdf_sop_chunker.py` / `scratch/dump_pdf_sop_blocks.py` / `scratch/verify_matcher_3_1.py`（诊断脚本，可重复跑）
- 无代码改动：`git diff HEAD opensearch_pipeline/chunker.py` = 0 行；`opensearch_pipeline/pipeline_nodes.py` = 上轮用户 xlsx +242 行未变（与本轮无关）
