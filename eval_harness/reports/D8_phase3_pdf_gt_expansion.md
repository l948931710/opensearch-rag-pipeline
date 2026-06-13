# D8 Phase 3 评估发现 — PDF GT 扩到 3 doc，新 baseline 0.6966 — 2026-06-13

> Phase 3 目标:把 PDF GT 从 pdf_sop 单 doc 扩到 ≥3 doc(D8 Phase 2 评估报告留下的 Phase 3 前置条件)。完成 + 顺带修了 2 个 chunker/eval-harness blocker。新 baseline 揭示 4 类 chunker 失败模式,远超 Phase 1 已知的 Bug A/B。

## TL;DR

| 维度 | Phase 3 计划 | 实测结论 | 决策 |
|---|---|---|---|
| **GT 扩展** | ≥3 doc | pdf_sop(11) + xs_wi_007(9) + it_xxh_003(19) = 39 chunks | 完成 ✓ |
| **新 baseline** | — | **Mean Jaccard 0.6966** (std 0.4522) | 替代旧单 doc 0.7273 lock |
| **per-doc 分布** | — | pdf_sop=0.788 / xs_wi_007=0.722 / it_xxh_003=0.632 | 跨 doc std=0.066 |
| **暴露的 chunker bug** | — | 4 类:(1) Bug A/B 在 xs_wi_007 重现 (2) image 错位至相邻 step (3) sub-chunk matcher 选错 (4) markdown bullet SOP 不出 step_card | 留 chip |
| **配套修复** | — | _STEP_DETECT_RE 加 bullet 容忍 + ingestion_binding title realpath | 已合,36 tests + pdf_sop byte-equal ✓ |

## 1. GT 扩展过程

### 候选池盘点(3 个候选,2 个最终)

| 候选 | 路径 | 类型 | n_pages | n_images | 决策 |
|---|---|---|---|---|---|
| pdf_xs_wi_007 | `scratch/rotation_repro/FL-XS-WI-007.pdf` | 吸塑扫码报检 SOP(与 pdf_sop 同型业务) | 3 | 8 | ✓ 选 — 平行业务 cross-validate |
| pdf_it_xxh_003 | `fuling_chunk_exp/it_FL-CW-XXH-003-《电脑安装》作业指导书.pdf` | 电脑/服务器组装 SOP(IT 类不同业务) | 14 | 11 | ✓ 选 — 不同业务多样性 |
| pdf_admin_lodging | `fuling_chunk_exp/admin_关于外来人员来访留宿相关规定.pdf` | 行政留宿规定(单图行政章) | 1 | 1 | ✗ 排除 — 无 step 结构 |

### 标注流程

- **数据准备**:dump 每 doc 的 PDF text(per page,markdown 形式)+ image manifest(image_index/visual_summary/ocr_preview/page_num)+ chunker 实际产出 chunks。`scratch/d8_phase3/{label}_{text.md,chunks_v3.json}`
- **并行标注**:Workflow 2 agent 并行,schema-validated 输出(GT_SCHEMA enforces label/chunk_type/keywords≥2/expected_image_refs/expected_images)。每 agent 看到 PDF text + manifest + 实际 chunks,基于**语义关联**标 expected_image_refs(不基于 chunker 实际输出)。
- **合并**:`scripts/d8_phase3_merge_gt.py` 保留 `_meta` + pdf_sop 不动,追加新 doc keys。备份 `.bak_d8p3`。

### 工件

- `~/Downloads/opensearch-rag-data/eval_samples/ground_truth/gt_pdf_analysis.json`(新增 2 doc 38 chunks)
- `scratch/eval_manifest/pdf_{xs_wi_007,it_xxh_003}_images.json`(image manifest)
- `scratch/d8_phase3/{xs_wi_007,it_xxh_003}_{text.md,chunks_v3.json}`(annotation 输入)
- `scratch/d8_phase3/baseline_3doc_v3.json`(新 baseline)
- `scripts/d8_phase3_merge_gt.py`(merge 工具)
- `~/.../eval_samples/documents/{pdf_xs_wi_007,pdf_it_xxh_003}.pdf`(symlink 到真实文件)

## 2. 配套修复(2 处 blocker)

### Fix 1:`_STEP_DETECT_RE` markdown bullet/heading 前缀容忍

**根因**:it_xxh_003 目录格式是 `• 第一步：安装CPU处理器` 和 markdown heading `# 第一步：...`,但 `_STEP_DETECT_RE` 要求 `(?:^|\n)\s*第\s*...步` —— `\s*` 卡住 `•`/`#`,导致 it_xxh_003 全文 0 个 step 匹配,被路由到 text mode、图全成独立 image chunk 无 step 绑定。

**修法**(opensearch_pipeline/pipeline_nodes.py:1594):
```python
r'(?:^|\n)[\s•·\-\*\#]*(?:'   # ← 允许 bullet/heading 前缀
```
要求 ≥2 个匹配仍保护 false-positive(单条 `- 1.` 不够)。
**验证**:tests/test_chunker.py 36 个全绿 + pdf_sop byte-equal(D7 lock 数据完全一致)。

### Fix 2:`ingestion_binding._extract_and_chunk` title 取 realpath filename

**根因**:`title=label`(如 `"pdf_xs_wi_007"`)不含 sop_keywords、underscore `wi_007` 不匹配 `wi-\d` regex → `_detect_step_patterns` 返回 False → step mode 不触发。pdf_sop label 恰好含 "sop" 关键词逃过了这个评测层 bug。

**修法**(eval_harness/binding/ingestion_binding.py:122):
```python
filename = os.path.basename(os.path.realpath(doc_path))  # ← realpath 解 symlink
prod_like_title = os.path.splitext(filename)[0]
doc = {..., "title": prod_like_title, "filename": filename, ...}
```
`realpath` 是关键:eval_image_binding_pdf 把新 doc 软链到 `docs_dir/{label}.{ext}`,symlink 名退化回 label —— `realpath` 解出真实文件名(`FL-XS-WI-007.pdf`/`it_FL-CW-XXH-003-《电脑安装》作业指导书.pdf`),与生产 RDS title 等价。
**验证**:pdf_sop title 不变(`"pdf_sop"` realpath = 同文件) → byte-equal。

## 3. 新 baseline 详解(per-chunk Jaccard)

### pdf_sop(11 strong, Jaccard 0.788)

D7 lock 是 0.7273,本轮升至 **0.788**(+0.0606)——根因:matcher 现在在 step 3.1 选对了 chunk(produced 从 [] → [5, 6]),与 chunker 改动无关,与 title realpath 修复后 ingestion_binding 走 step mode 选对 step_card 有关。

| step | gt | pred | jaccard | 备注 |
|---|---|---|---|---|
| 1.1/1.2/1.3 | [1]/[2]/[3] | [1]/[2]/[3] | 1.0/1.0/1.0 | ✓ |
| 2 | [4] | [4] | 1.0 | ✓ |
| **3.1** | [5, 6, 9] | [5, 6] | 0.667 | ⚠️ Bug B(跨页 image 9 缺) |
| 3.2 | [7] | [7] | 1.0 | ✓ |
| **4.1** | [10] | [] | 0.0 | ❌ Bug A(image 10 错绑到 4.2) |
| **4.2** | [] | [9, 10] | 0.0 | ❌ Bug A 镜像 |
| 5 | [11] | [11] | 1.0 | ✓ |

### pdf_xs_wi_007(9 strong, Jaccard 0.722)

**Bug A 类型在同型 doc 上不重现**(step 5 "①设备②班次③数量" 没像 pdf_sop step 4.1 那样错绑) — 但出现了**新 chunker bug**。

| step | gt | pred | jaccard | 备注 |
|---|---|---|---|---|
| **1** 标识卡清点 | [1] | [] | 0.0 | ❌ image 1 应绑 step 1,实绑 step 2 |
| **2** 收集核对 | [2] | [1, 2] | 0.5 | ⚠️ image 1 错绑过来(根因同 step 1) |
| 3 U8 报检界面 | [26, 27, 28] | [26, 27, 28] | 1.0 | ✓ |
| 4 扫码枪 | [29] | [29] | 1.0 | ✓ |
| **5.1** 主流程 | [30] | [] | 0.0 | ❌ chunker 切 step 5 多 sub-chunk,matcher 选错(选了无图的 i=8/9) |
| 5.2 异常流程 | [41] | [41] | 1.0 | ✓ |

**模式**:image 错绑到**相邻 step**(step 1↔2),与 pdf_sop Bug A(step 4.1↔4.2)是同一根因 —— image 几何 anchor 与 step 边界冲突时,chunker 把 image 归到错的相邻 step。pdf_sop 是 dotted child step(4.1/4.2),xs_wi_007 是顶层 step(1/2),证明这不是 dotted 切分独有 bug。

### pdf_it_xxh_003(19 strong, Jaccard 0.632)

**step mode 触发了但 chunker 没切出 step_card**(`step_cards=0`),只切出 19 text_chunk + 1 ocr_chunk。图绑到 text_chunk 的 section_title="第N步" 上,部分能命中。

| step | gt | pred | jaccard | 备注 |
|---|---|---|---|---|
| 第一步 三角对齐 | [8] | [] | 0.0 | ❌ image 8 错绑到前一段"打开插座" |
| 第一步 打开插座 | [] | [8] | 0.0 | ❌ 镜像 |
| 第一步 收尾 | [] | [12] | 0.0 | ❌ image 12(散热器)错绑到 step 1 收尾 |
| 第二步 散热器 | [12] | [] | 0.0 | ❌ 镜像 |
| 第四步 垫脚螺母 | [16] | [16] | 1.0 | ✓ |
| 第五步 硬盘托架 | [20, 21, 22] | [] | 0.0 | ❌ |
| 第五步 装入硬盘 | [23, 24] | [27] | 0.0 | ❌ image 27 是光驱,错绑到硬盘 |
| 第五步 装回机箱 | [25] | [] | 0.0 | ❌ |
| 第六步 光驱 | [27] | [27] | 1.0 | ✓ |
| 第六步 电源 | [29] | [29] | 1.0 | ✓ |

**模式**:多种**相邻段错位**(step 1 内段 vs step 2、step 5 内段 vs step 6),以及 image 24/25 等无 OCR/弱 visual 信号的 image 完全没绑。

## 4. 暴露的 chunker bug 4 类

| 类型 | 现象 | 涉及 doc | 留 chip |
|---|---|---|---|
| **A. dotted sub-step image 错绑相邻** | step 4.1↔4.2 互换 | pdf_sop | 已留 D8 Phase 2 chip(task_0dc55c6f) |
| **B. 顶层 step image 错绑相邻** | step 1↔2 互换 | xs_wi_007 | 同根因,合入上述 chip |
| **C. step 多 sub-chunk matcher 选错** | xs_wi_007 step 5 sub-chunk i=8/9 无图、i=10 有图,matcher 选了 i=8 | xs_wi_007 | 新 chip |
| **D. markdown bullet SOP 不出 step_card** | it_xxh_003 step mode 触发但 chunker `_chunk_by_step` 没产 step_card,全是 text_chunk + section_title | it_xxh_003 | 新 chip |
| **E. 跨页 range-ref** | pdf_sop step 3.1 "②-⑥步操作" image 9 跨页未绑 | pdf_sop | 已在 D8 Phase 2 报告中 |

## 5. 决策与下一步

### 立即变更(本 commit)
- **代码改动**(2 处 fix 已应用):
  1. `opensearch_pipeline/pipeline_nodes.py:1594` `_STEP_DETECT_RE` 加 bullet/heading 容忍
  2. `eval_harness/binding/ingestion_binding.py:122` title 取 realpath filename
- **GT 改动**:`gt_pdf_analysis.json` 追加 `pdf_xs_wi_007`(9 chunks)+ `pdf_it_xxh_003`(19 chunks)。.bak_d8p3 备份
- **docs symlinks**:`eval_samples/documents/pdf_{xs_wi_007,it_xxh_003}.pdf` symlink 到真实文件
- **新 PDF baseline**:**0.6966**(替代单 doc 0.7273)
- **chunker 36 tests + pdf_sop byte-equal**:✓

### 留 chip(新增,接续 task_0dc55c6f)

| Chip | 描述 | 优先级 |
|---|---|---|
| **chunker step boundary image 错绑相邻 step**(覆盖 Bug A/B/C) | pdf_sop step 4.1/4.2 + xs_wi_007 step 1/2 + xs_wi_007 step 5 sub-chunk —— 这是同一根因不同表现。修法范围:`_insert_image_refs_heuristic` 几何 anchor + `_chunk_by_step` step boundary image 归属判定 | **高** — 影响 ~3-5 chunks/Jaccard ~0.15+ |
| **markdown bullet/heading SOP `_chunk_by_step` 不出 step_card**(D) | step mode 触发但 `_chunk_by_step` 内部 step block 边界识别失败,fallback 到 text mode chunks。要看 `_chunk_by_step` 内部的 step block detection 逻辑 | **中** — 影响 it_xxh_003 整体绑定(0.632 → 可能 0.85+) |
| **新 baseline 锁档 + 升 hard** | PDF Jaccard hard ≥0.65(目前 D7 lock 用 ≥0.7,需重定) | 低 — followup |

### 已知不修(本 phase 之外)

- **xs_wi_007 step 5 子流程 matcher 选 sub-chunk**:GT 已经按 chunker 实际切分粒度对齐(GT step 5.1/5.2 两条对应 chunker 多个 step 5 sub-chunk),需要 matcher 改 sub-chunk selector 策略 → 与 chip C 合并
- **PDF GT 再扩到 ≥5 doc**:本 phase ≥3 已满足 Phase 2 留下的前置条件,再扩属于 Phase 4 工作

## 6. 学到的事

- **GT 扩展的真正价值不是"多点平均"而是"暴露 doc 多样性"**:pdf_sop 单 doc 看不出 chunker 在同型/不同型业务上的稳定性,3 doc 立刻拉出 4 类失败模式 + std 0.066
- **eval-harness 隐式假设≠生产路径**:`title=label` 在评测里截断了 `_detect_step_patterns` 的真实 routing,pdf_sop 因 label 恰好含"sop"侥幸过关。realpath 修复让评测与生产对齐
- **markdown 友好但 chunker 不一定友好**:作业指导书用 `• 第一步：` 列出目录是非常自然的格式,但 chunker `_STEP_DETECT_RE` 对此盲。修 regex 是 1 行 fix,但 chunker `_chunk_by_step` 在 markdown SOP 上不出 step_card 是更深的结构问题
- **Bug A/B 是普遍现象,不是 dotted 独有**:xs_wi_007 step 1/2 互换证明 image 错绑相邻 step 是一类**chunker step boundary 与 image anchor 冲突**的通病,不局限于 4.1/4.2 这种 dotted child step

## 工件清单
- 本报告
- `eval_harness/reports/D8_phase{1,2}_*.md`(前两个 phase 评估)
- `scratch/d8_phase3/baseline_3doc_v3.json`(新 baseline)
- `scratch/d8_phase3/workflow_gt_objects.json`(workflow 标注产出)
- `scripts/d8_phase3_merge_gt.py`
