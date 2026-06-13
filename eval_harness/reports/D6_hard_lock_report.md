# L4 Day 6 锁档报告 — 2026-06-12

> 对比 D5 baseline,Day 6 第二轮跑 + 两个 RUBRIC/聚合改进,**结论:无闸升 hard**,reasons 与证据链如下。

## D5 vs D6 两轮对比(同 git commit, 同 GT, 同 LLM 配置)

| 闸 | D5 | D6 | Δ | 决策 |
|---|---|---|---|---|
| **PDF Jaccard** | 0.7273 | 0.7273 | 0 | 留 soft(不 deterministic 但本轮巧合相同 — 仅 1 文档 11 chunks) |
| **XLSX Jaccard** | 0.6818 | **0.7727** | +0.091 | 留 soft — **chunker 非确定性**,xlsx_sop step5 jaccard D5=0 / D6=1.0 |
| **img_dup p95** | 1.48 | **1.15** | −0.33 | **改聚合后 PASS** — degraded doc 不入 p95(xlsx_inspect 同行多图是文档形态非 bug) |
| **image_binding** (Claude) | 3.343 (N=36) | 3.314 (N=17) | −0.029 | 留 soft — **3.3 是真值**,真 chunker bug 6/17 拖低,RUBRIC 改"只评含图 case"不挽救 |
| L4-srv 3 闸 | (N=2 自动降级) | 同 | — | N/A 不变 |

## 三个关键诚实发现

### 1. PDF/XLSX Jaccard 非确定性 — chunker 内 ThreadPoolExecutor 竞态

D5 vs D6 同代码同 GT 同 cache,xlsx_sop 步骤5-样品称重 jaccard 在 D5 是 0(chunker 把 anchor_row=14 该绑给 step5 的图绑给了 step6,把 anchor_row=15 该绑给 step2 候选的图绑给了 step5),D6 两次跑都是 1.0(正确绑定)。已开 task chip `task_b889bddc` 补诊断,根因怀疑在 `_process_embedded_images` 的 ThreadPoolExecutor 完成顺序影响 `r.assets` 列表次序,chunker 据该次序 + 内容匹配 tiebreaker 分配,顺序变化导致结果变化。

**含义:** PDF 1 doc / XLSX 1 doc 是过小样本,std 看不出真分布。**不该升 hard,等 chunker 修非确定性 + GT 扩容(至少 5 doc/format)再考虑。**

### 2. image_binding 3.3 是真值,RUBRIC 改"只评含图 case"不改善

D5 评 36 case(含 15 个显式负例 ib=3 中性) mean 3.343;D6 改 RUBRIC 只评 17 含图 case mean **3.314 (Δ −0.03)**。**显式负例 RUBRIC 不是拖低均值的原因。**

3.3 反映 17 含图 case 里 **6 个真 chunker bug**(占 35%):
- PDF: step 3.1=1(U8 多截图全 miss)、4.1=1(图被绑到 4.2)、4.2=2(cross-bound)
- XLSX_sop: step 3=1(显式负例 chunker 多绑)、step 6=1(图被绑错)
- XLSX_spec: 流程图=1, 产品照片=1(独立 image chunk 形态局限,non-bug 但度量不适用)

**含义:** ≥4.0 soft 闸是 chunker bug 修后可达,**现 3.3 FAIL 是真信号**。inter-rater stdev D5 0.274 → D6 0.055(更紧),说明评委一致认为 6 个 case 真错。

### 3. dup p95 修聚合后从 1.48 → 1.15 ✅

`xlsx_inspect` GT degraded(schema 简化没标 expected_image_refs),其 dup=1.67(5 step_card 共享 3 unique 图,同行多图设计内行为)。D6 改聚合排除 degraded doc 的 dup,主 p95 仅看 `pdf_sop`(1.00)+ `xlsx_sop`(1.20),p95=1.15 ≤ 1.20 hard 闸 PASS。

**含义:** `img_dup_factor p95` hard 闸继续保持(0.05pp 缓冲);仍诚实在 per_doc 列出 degraded doc 的 dup 供 trend 监控。

## 改进点本轮落地

| 改 | 文件 | Why |
|---|---|---|
| `_doc_to_judge_bundle_item` 过滤显式负例 | ingestion_binding.py | 减小 panel 成本(36→17),不影响 mean |
| dup_p95 排除 degraded doc | ingestion_binding.py | xlsx_inspect 同行多图非 bug,入主闸误报 |

## Day 7+ 接力(留下一轮)

1. **修 chunker 非确定性** — task chip `task_b889bddc` + `task_29a70891`,根因可能在 `_process_embedded_images` ThreadPoolExecutor 完成顺序
2. **GT 扩容** — 至少 5 doc/format 才能让 std 有意义、升 hard 不假绿不假红
3. **DOCX 接入** ingestion_binding — 现 GT 4 doc 全没标 expected_image_refs(`scripts/eval_image_binding_accuracy.py --strict` 跑过 98.6%,接进度量需把 text-proximity 包装成 Jaccard)
4. **L4-serving 扩 cases** — golden_50 只 2 题 expect_images=True,需要补到 ≥ 20 题 marker_validity 等闸才能脱离 N/A

## 锁档闸总结(本轮终态)

| 闸 | Final value | 状态 | 阈值 |
|---|---|---|---|
| binding pdf Jaccard | 0.7273 | ✅ PASS soft | ≥0.70(留 soft,样本小) |
| binding xlsx Jaccard | 0.7727 (D6) / 0.6818 (D5) | ✅ PASS soft | ≥0.65(留 soft,非确定性) |
| img_dup p95 (excl degraded) | 1.15 | ✅ PASS hard | ≤1.20 hard |
| image_binding (Claude) | 3.314 | ❌ FAIL soft | ≥4.0 soft(真信号 — chunker bug 6/17) |
| L4-srv 3 闸 (marker/dangling/orphan) | (N=2) | ➖ N/A 自动降级 | 待样本 ≥ 5 |
| L3 Claude 4 维度 | (无 positive case) | ➖ N/A | — |

## 工件

- `eval_harness/reports/run_l4_baseline_d5/`(首轮)
- `eval_harness/reports/run_l4_baseline_d6/`(本轮,改进后) — report.{md,json}、judge_bundle_binding.json(17 items)、judge_verdicts.json(3 评委 × 17 = 51 verdicts)、shards_binding/
- `D5_baseline_summary.md`(D5 自报告)
- `D6_hard_lock_report.md`(本文件)
