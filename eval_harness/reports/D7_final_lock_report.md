# L4 Day 7 锁档报告 — 2026-06-13

> chunker 修复(task_29a70891 + task_b889bddc)+ DOCX 接入(用户独立完成)+ L4-serving 25 题脱 N/A 后的最终锁档。**核心成就:5 轮 byte-equal,chunker 非确定性已消;3 个 L4-ing 闸 deterministic PASS。**

## Day 5 → Day 6 → Day 7 三轮 baseline 对比

| 闸 | D5 | D6 | D7 | 状态 | 决策 |
|---|---|---|---|---|---|
| **L4-ing PDF Jaccard** | 0.7273 | 0.7273 | **0.7273** | ✅ deterministic | 留 soft(样本 1 doc 11 chunks) |
| **L4-ing XLSX Jaccard** | 0.6818 | 0.7727 | **0.8636** | ✅ deterministic +9.1pp | **可升 hard ≥0.85(1pp 缓冲)** |
| **L4-ing DOCX Jaccard** | n/a | n/a | **0.9847** | ✅ deterministic | **保 hard ≥0.95**(已是 hard,3.5pp 缓冲) |
| **img_dup_factor p95** | 1.48 | 1.15 | **1.15** | ✅ hard PASS | 留 hard ≤1.20 |
| **L4-srv marker_validity** | (N=2) | (N=2) | **0.7541** | ❌ FAIL N=25 | 留 soft + 报 prompt 修复 chip |
| **L4-srv dangling_ref** | (N=2) | (N=2) | **0.0** | ✅ PASS | 升 hard ≤0.05(已 PASS) |
| **L4-srv orphan_rate** | (N=2) | (N=2) | **0.6** | ❌ FAIL N=25 | 留 soft + builder 行为 |
| **image_binding (Claude)** | 3.343 | 3.314 | **3.412** | ❌ FAIL +0.10 | 留 soft(step3/PDF 3.1/4.1/4.2 真 bug 待 chip) |
| L3 Claude 4 维度 | — | — | — | ➖ N/A(L4-only) | — |

## 三大核心发现

### 1. chunker 非确定性消除 — ALL_EQUAL ✅

5 次连跑 `scripts/day7_chunker_postfix_verify.sh`:
- **per_chunk 36/36 byte-equal**
- per_fmt mean std = **0.0000**(across 5 runs)
- img_dup_factor p95 全 1.15 一致

用户在 task_29a70891 + task_b889bddc 修了 ThreadPoolExecutor 完成顺序 → `r.assets` 列表的稳定 key 排序。`scripts/day7_chunker_postfix_verify.sh` 是 plan 升 hard 的工具(verdict=ALL_EQUAL/STD_OK/DRIFT),已落地可复用。

### 2. XLSX +9.1pp 大涨,step5/6 互换 bug 修复实证

| xlsx_sop 题 | D6 image_binding | D7 image_binding | Δ | 解读 |
|---|---|---|---|---|
| 步骤4-天平调零 | 5.0 | 5.0 | 0 | 一直对 |
| 步骤5-样品称重 | 5.0 | 5.0 | 0 | D6 蒙对,D7 真稳 |
| **步骤6-仪器关闭** | **1.33** | **4.0** | **+2.7** | **task chip 修了** |
| 步骤3-试样准备 | 1.0 | 1.0 | 0 | task_1e52dec0 待修(算法局限) |
| 步骤2-仪器开启 | 3.0 | 2.67 | -0.3 | 双图算法局限 |

xlsx Jaccard 0.6818 (D5) → 0.7727 (D6,chunker race 蒙对) → **0.8636 (D7,真修)**。

### 3. DOCX 接入实测 0.9847 — 接近 strict baseline 98.6%

用户独立实施的 `_run_docx_strict_path` + `_is_sop_docx` SOP 启发式(排除 admin_/hr_/eval_*_faq)产出:
- `binding_jaccard_docx = 0.9847` per_fmt['docx'] 来源标 `strict_fixture`(report.py 已加 notes 字段)
- 43 个 fuling_chunk_exp/docx 中筛 SOP 跑 production-faithful chunker,与 `scripts/eval_image_binding_accuracy.py --strict` 98.6% 高度吻合
- 入 hard 闸 ≥0.95 PASS,3.5pp 缓冲

## 升 hard 决策与变更

### 立即升 hard(本 commit 包含)

| 闸 | 旧阈值 | 新阈值 | 余地 |
|---|---|---|---|
| **XLSX Jaccard** | ≥0.65 soft | **≥0.85 hard** | 1.4pp |
| **dangling_ref_rate** | ≤0.05 hard(已是)| 保 hard | 5pp(0.0 实测) |
| DOCX Jaccard | ≥0.95 hard(D6 设定)| **保 hard** | 3.5pp(实测 0.9847) |
| img_dup p95 | ≤1.20 hard(D6 设定)| **保 hard** | 5pp(实测 1.15) |

### 保留 soft 的理由

- **PDF Jaccard 0.7273**:1 doc 11 chunks 样本太小,step3.1/4.1/4.2 三个 PDF chunker bug 未修,deterministic 但脆。需要 ≥3 doc PDF 样本 + chunker bug 修后再升
- **marker_validity 0.7541 / orphan 0.6**:LLM/prompt 行为(builder 兜底设计上就让 orphan 高),不是 binding 失败。建议保 soft + 报 prompt 优化 chip(下方)
- **image_binding 3.412**:step3 + PDF 3.1/4.1/4.2 真 bug + `task_1e52dec0` 算法局限。chip 修后预期升,不该升 hard

## 应继续治理的真信号(留 chip / 后续工作)

| 真信号 | Chip / Task | 形态 |
|---|---|---|
| xlsx step3 应空 + step2 双图(算法 unsupervised 局限) | `task_1e52dec0` 已开 | binding policy 改造,二选一方案 A/B |
| PDF step 3.1/4.1/4.2 image_refs 全空 / cross-bind | 待评估开新 chip | pdf chunker 同款 anchor 顺序 bug 的 PDF 版本? |
| LLM `<<IMG:N>>` marker_validity 0.75 | 待开 chip | prompt 优化 — 让 LLM 不编造越界图号 |
| orphan 0.6(builder 兜底) | 设计如此 | 不是 bug:builder 设计就是 unreferenced 图末尾追加,plan 已说"不该升 hard,仅 trend" |

## 整个 D5→D7 学到的 4 件事

1. **deterministic 比 std 重要** — chunker 非确定时,样本小的 std 数字毫无意义。D7 5 连跑 byte-equal 才是真 baseline
2. **DOCX 走 strict_fixture 路径成功** — GT 标 expected_image_refs 工作量大时,用现有 strict 邻近文本匹配做 micro-accuracy 包装入 Jaccard 是务实 ROI 选择
3. **L4-srv 闸真值 N=25 才有意义** — N=2 时所有 srv 闸自动降级 N/A 正确,扩 cases 后 marker_validity/orphan 真值暴露真实 LLM 行为(都是 prompt/builder 设计内,不是 bug)
4. **判 image_binding 维度有效** — 17 个含图 case 中 step6 D6→D7 +2.7pp 精准对应 chunker bug 修复,Claude 评委一致性高(stdev 0.055)

## 工件

- `eval_harness/reports/D7_final_lock_report.md`(本报告)
- `eval_harness/reports/D7_chunker_postfix_report.md`(verify 脚本自动生成)
- `scratch/day7_chunker_verify_20260613_001953/run_{1..5}/`(deterministic 验证)
- `eval_harness/reports/run_l4_serving_d7/`(L4-srv 25 题 baseline)
- `scripts/day7_chunker_postfix_verify.sh` + `day7_chunker_postfix_compare.py`(可复用)
- `eval_harness/goldset/golden_l4_serving.json`(25 题 L4-srv 题集)
