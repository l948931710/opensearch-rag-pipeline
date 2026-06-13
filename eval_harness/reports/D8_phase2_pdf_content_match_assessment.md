# D8 Phase 2 评估发现 — PDF content-match 实证 net regression — 2026-06-13

> Phase 2 计划:在 `_insert_image_refs_heuristic` PDF 路径（策略 1b-2 几何 overlap-max 之后）加 `_content_match_steps` 校正，仿 xlsx `_bind_pool` P0 模式，解 D8 Phase 1 留下的 Bug A（pdf_sop step 4.1/4.2 互换）。实施后单 doc 实测**净 regression 0.7273 → 0.6364**，本轮零代码改动落盘，固化三大评估发现。

## TL;DR

| 维度 | Phase 2 推荐 | 实测结论 | 决策 |
|---|---|---|---|
| **PDF Jaccard 升幅** | env-gated content-match 校正，预期 +0.3~0.5 | **0.7273 → 0.6364**（-0.0909） | **回退,移入 Phase 3** |
| **Bug A 解了吗** | step 4.1/4.2 互换应被矫正 | step 4.1=[], step 4.2=[9,10] 与 OFF 完全相同 | 未触发矫正 |
| **副作用** | xlsx/docx 不退；其他 PDF chunk 不退 | step 2=[4]→[4,5] / step 3.2=[7]→[6,7] / step 3.1=[5,6]→[] **三处误派** | content-gate 无 free lunch |

## 实测数据

env=OFF（默认）byte-equal vs HEAD（commit ceeef32）`scripts/eval_image_binding_pdf.py`：

| step | gt | OFF | ON | OFF Jaccard | ON Jaccard | Δ |
|---|---|---|---|---|---|---|
| 1.1 收取交货单 | [1] | [1] | [1] | 1.000 | 1.000 | — |
| 1.2 核对错误处理 | [2] | [2] | [2] | 1.000 | 1.000 | — |
| 1.3 抄录订单信息 | [3] | [3] | [3] | 1.000 | 1.000 | — |
| **2 交货单分类** | [4] | [4] | [4,5] | 1.000 | 0.500 | **-0.500** |
| 3.1 U8扫码报检 | [5,6,9] | [5,6] | [] | 0.667 | 0.000 | **-0.667** |
| **3.2 扫码枪扫描** | [7] | [7] | [6,7] | 1.000 | 0.500 | **-0.500** |
| 4.1 填写设备班次 | [10] | [] | [] | 0.000 | 0.000 | — |
| 4.2 班组人员 | [] | [9,10] | [9,10] | 0.000 | 0.000 | — |
| 5 群通知完成 | [11] | [11] | [11] | 1.000 | 1.000 | — |
| **mean** |   |   |   | **0.7273** | **0.6364** | **-0.0909** |

（前言/步骤 4 父说明等 0-image GT 项省略，与 D8 Phase 1 计法一致：strong=11）

## 发现 1：Bug A 真实根因不在 `_insert_image_refs_heuristic`

Phase 1 评估已经诊断:image 10 (page 3 bbox y0=401.4 y1=661.6,跨度 260pt) 与所有 step 4.x 段落 (b_y1≤209) 没有任何 y 重叠,主循环 None → fallback 取上方最近段落 = step 4.2 的 ⑤⑥ 段落 (y0=183)。

Phase 2 加 content-match 后,**`best_idx` 在 content-match 校正后仍指向同一 block**。原因:

- step 4.1 的关键词 "①设备②班次③数量" 与 step 4.2 的关键词 "④根据设备带出班组人员⑤⑥报检" 在 PDF text extraction 后**很可能落在同一个 raw paragraph block** 里（pdfplumber 按段落抽取,圈号 ①②③④⑤⑥ 是同一段连续文本）。
- 那么 image 10 几何 best_idx = 这个 block,content-match 也指向这个 block (因为 cand 里它是 page 3 唯一含 "设备/班次/数量/报检" 的块) → no-op,best_idx 不变。
- chunker 后续 `_chunk_by_step` 把这个 block 按 dotted 序号切成 step 4.1 / step 4.2 两个 chunk,image_ref 跟到哪一边由切分顺序决定 (实测落到 4.2)。

**含义**:Bug A 真实根因 = chunker `_chunk_by_step` 的 image_ref 归属判定（dotted 切分时,image_ref block 应该跟到 dotted 序号在它**之前**的那个子 chunk,而不是默认就近）。`_insert_image_refs_heuristic` 改不动这个。

## 发现 2：raw-block content-match cand 粒度不对,xlsx 的成功不可平移

`_content_match_steps(img_text, candidates)` 在 xlsx 路径上 work 是因为 cand=`(step_no, step_card.chunk_text)`——**整段 step 文本聚合**,稀有词权重稳定。

PDF 路径下我把 cand 改成 `(block_idx, block_text)`——**paragraph 级零散**:

- step 3.1 文本在 PDF raw blocks 里可能是 heading "3.1 U8 扫码报检" + paragraph "如下图②-⑥步操作 1)开启 U8 2)业务导航 3)质量管理..." 两个独立 block,稀有词 "U8/导航/质量" 被打散到两个 cand 里
- step 2 单 paragraph "按厂家、班次、货号分别堆放（如图①）" 包含 "班次" 与 image 5/6 的 OCR/visual_summary 部分重合
- IDF 加权后,**step 2 的单 paragraph cand 反而比 step 3.1 的拆开两 cand 更胜过**——image 5/6 被 content-match 误派到 step 2/3.2

实测 `step 3.1 OFF=[5,6] ON=[]`——content-match 把原本几何正确绑的两张图全拉走,造成单点损失 -0.667。

**含义**:要让 content-match 在 PDF 路径 work,必须先做 **block → step grouping**(按 heading 序号 1.1/1.2/2/3.1 把同 step 内 paragraph 聚合成 cand),但这相当于在 `_insert_image_refs_heuristic` 里复现 chunker 的 step 边界识别,复杂度大幅升高且与 `_chunk_by_step` 逻辑冗余。

## 发现 3：单 doc GT 不足以稳定标定阈值

xlsx P0 阈值 `score≥0.8 + margin≥0.5` 是在 7 个 xlsx_sop GT chunk 上调出来的。PDF 直接搬这个阈值到单 doc(pdf_sop, 11 strong chunks)实测:

- step 4.1 case (期望矫正): 阈值过严,content-match 没触发
- step 2 / 3.2 case (期望保持): 阈值过松,误派激活

**单 doc 标阈值的本质问题**:Bug A (期望矫正) 与 Bug B (期望保持) 都在 pdf_sop 一个文档里,任何阈值选择都不可能同时满足两者(0.7273 → 0.6364 实证了这一点)。要稳定标定**至少需要 3 doc** (1 用作 cross-validation),才能区分"算法没收敛" vs "doc 特性导致阈值有冲突"。

## 决策与下一步

### 立即变更（本 commit）
- **本轮零代码改动**:`opensearch_pipeline/pipeline_nodes.py` 在 D8 Phase 2 后已 byte-equal 回到 +242（用户并行 session 的 xlsx `_bind_pool` 改动,不属本轮）
- **写本评估报告**固化三大诊断
- **保持 PDF Jaccard 锁档基线 0.7273**(无 D8 Phase 2 升级)
- **不升 PDF Jaccard hard**(D7 ≥0.65 已经覆盖)

### 留 chip（下次会话或独立 session）

| Chip | 描述 | 优先级 |
|---|---|---|
| **Bug A 真实根因排查:chunker `_chunk_by_step` dotted 切分时 image_ref 归属** | image_ref block 在 dotted 序号切分时落到 step 4.2 而非 4.1。要看 `_chunk_by_step` 的 image_ref boundary 判定逻辑(应该跟 dotted 子序号或文本邻近性) | 中 — 影响 1 chunk 1 张图,边际 ROI ≤0.1 |
| **PDF GT 扩到 ≥3 doc** | 当前只 pdf_sop 1 个,任何阈值标定都不可靠。挑 2 个新 PDF SOP (有 step + 有图)作为 cross-validation,再考虑 chunker 改动 | 中 — Phase 3 前置 |
| **Bug B (step 3.1 跨页 image 9) range-ref 解析** | 跨页绑定打破 page_num 是真值假设,风险高;且 image_index 不连续(8 弃),"②-⑥步操作"字面区间不直接映射到 image_index 区间。**延期到 Phase 3**,与 chunker 改动一起做 | 低 |

### 学到的事（本轮）
- **content-match 不是免费的**:cand 粒度必须与算法假设匹配(整段 step text vs paragraph 级零散),搬不动就别搬
- **单 doc 标阈值的本质矛盾**:Bug A 与 Bug B 在同一 doc 里冲突时,无单阈值解,**先扩 GT 再调算法**比"加 env-gate 试错"更经济
- **几何位置 + 内容匹配 ≠ 解决一切**:同 raw block 里多 step 字符串(①②③④⑤⑥)是 PDF/chunker 边界问题,不是 image binding 问题——选错抽象层会越改越糟

## 工件
- 本报告
- `scratch/d8_phase2_pdf_off.json`(env OFF 实测)+ `scratch/d8_phase2_pdf_on.json`(env ON 实测)
- `scripts/day7_chunker_postfix_verify.sh` 未跑(已通过 `eval_image_binding_pdf.py` 单 doc 验证 byte-equal,跳 5 连跑节省 ~30min)
- 无代码改动(`git diff --stat opensearch_pipeline/pipeline_nodes.py` = +242 与本轮开始一致,本轮零行)
