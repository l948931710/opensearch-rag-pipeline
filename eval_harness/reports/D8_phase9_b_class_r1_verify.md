# D8 Phase 9 — B 类 3 doc R1 触发率验证(脱敏) — 2026-06-13

> Phase 8.5 PROD-RO 全量 PDF 分类发现 B 类(短代号 title)= 3 doc(2.7%)。本 phase 把 3 doc OSS 下载到 scratch/d8_phase9_b_class/(脱敏文件名 b_1.pdf/b_2.pdf/b_3.pdf),Workflow 3 parallel agent 跑 dryrun + R1 触发判定。**0/3 R1 真触发,0/3 R1 救活,3/3 真假阳性 — R1 守门完美,0 误升 step mode**。

## TL;DR

| 指标 | 结果 |
|---|---|
| 验证 doc 数 | 3 (B 类全集) |
| R1_triggered = true | **0/3 (0%)** |
| R1_helped = true | **0/3 (0%)** |
| 真假阳性(短代号但非 SOP) | **3/3 (100%)** |
| 重灌批次入列 | **0 doc** |
| R1 假阳性数 | **0** |
| R1 假阴性数 | **0** (3 doc 均非 SOP,正确 ROUTE_TO_TEXT) |

## 1. 验证方法

**Step 1**: `prod_access.py::get_prod_readonly_conn()` PROD-RO 拉 `document_meta` ⋈ `document_version` 抽 B 类 3 doc(title clean 长度 ≤8 + 无 sop_keyword/wi-\d 命中),取 raw_key。

**Step 2**: `get_prod_oss_bucket()` 只读句柄 → `bucket.get_object_to_file(raw_key, scratch/d8_phase9_b_class/b_<idx>.pdf)`。文件名脱敏(用 idx 1/2/3),secret mapping(doc_id ↔ b_idx)仅本地 scratch 持久化。

**Step 3**: Workflow 3 parallel agent(schema-validated),per-doc:
- 跑 `UnifiedExtractor` 抽 text + assets
- 算 sop_kw_hit / wi_dash_hit / sop_anchor_count / step_pattern_count
- 算 R1_triggered = (not sop_kw_hit) and (not wi_hit) and (anchor_count ≥ 2)
- 跑 `_extract_and_chunk` 看 chunk 分布
- 算 R1_helped = R1_triggered and step_card > 0
- classification enum 三选一

**Step 4**: synthesize agent 汇总,生成重灌决策。

**脱敏保护**:transcript 不允许 dump 任何 title 全文 / 正文摘要 / image visual_summary / OCR 片段。schema 强制只 dump 聚合 stats + 锚词命中**计数**(不含锚词文本/位置)。

## 2. 实测数据(3 doc 聚合,脱敏)

| 字段 | b_1 | b_2 | b_3 |
|---|---|---|---|
| n_pages | 1 | 20 | 5 |
| n_images_funnel_kept | 1 | 20 | 5 |
| sop_keyword_match_title | False | False | False |
| wi_dash_match_title | False | False | False |
| sop_anchor_count | 0 | 0 | 0 |
| step_pattern_count | 5 | 70 | 2 |
| **R1_triggered** | **False** | **False** | **False** |
| m_mode_inferred | text | text | text |
| chunks total | 2 | 48 | 20 |
| chunks types | text_chunk + image | ocr_chunk × 48 | ocr_chunk × 20 |
| step_card_count | 0 | 0 | 0 |
| chunks_with_image_refs | 0 | 0 | 0 |
| classification | 真假阳性 | 真假阳性 | 真假阳性 |

### 单 doc 路径诊断(脱敏)

- **b_1**: 单页低密度内容,5 step pattern + 0 锚词 → R1 未触发 ✓;text + 1 image chunk;判定**非 SOP 工序文档**(通知/简短公告类),正确 ROUTE。
- **b_2**: 20 页扫描表单,**70 step pattern**(列表式编号噪声)+ 0 锚词 → R1 未触发 ✓;funnel 20 图全 ROUTE_TO_TEXT(OCR 入文)+ chunker 切 48 个 ocr_chunk;判定**表单/扫描件**,非工序型 SOP,正确 ROUTE。这是 R1 守门的最关键 case:**高 step_pattern_count 但 0 锚词** → 不升,避免把表单误判为 SOP。
- **b_3**: 5 页扫描型 PDF,2 step pattern + 0 锚词 → R1 未触发 ✓;OCR-only 路径,判定**非工序型 SOP**(可能是规章/合同/记录),正确 ROUTE。

## 3. R1 守门表现

R1 fallback 设计原意:`title 短代号 + 正文 SOP 锚词 ≥ 2 → 升 step mode`。

| 守门表现 | 实测 |
|---|---|
| 假阳性(误升 step mode) | 0/3 ✓ |
| 假阴性(漏救真 SOP) | 0/3 ✓(3 doc 均非真 SOP,无需救) |
| **关键 case**:b_2 step_pattern=70 但锚词=0 | **守门成功**,未误判为 SOP |

**结论**:阈值 ≥2 + 7 个锚词(作业前提/作业说明/生效日期/作业指导/作业方法/SOP编号/操作规程)与 B 类语料分布匹配良好。降到 ≥1 仍不会误救活(3 doc 都是 0 命中),提到 ≥3 也不会漏(0 命中 < 任何阈值)。

## 4. R1 修法实际价值评估

| 维度 | 评估 |
|---|---|
| 假阳性风险 | 0(本批 3 doc 实证) |
| 假阴性风险 | 不可测(3 doc 均非真 SOP) |
| 净 R1_helped 收益(本批) | 0 |
| 修法复杂度 | ~20 行 + 7 锚词列表 |
| 运行时开销 | 0(text 已抽,锚词查找 O(L)) |
| 未来语料漂移保险 | **高** — 若未来接入"xg001+作业说明"类 SOP,自动捕获 |

**净判**:R1 fallback **保留**,**不再为单独 R1 调参/扩样投入**。它是低成本兜底,守门表现完美,但本批 B 类 0 真 SOP 救活说明生产侧 SOP 标准化命名已较成熟。

## 5. 生产重灌决策(B 类)

**0 doc 入重灌批次**。

A 类 91 doc(82%)仍按 Phase 8.5 策略分批重灌:A-wi 27 → A-kw 64。B 类 0 doc 加入,C 类 17 doc 不动。

## 6. 后续 chip 建议

| chip | 优先级 | 描述 |
|---|---|---|
| **r1_fallback_triggered_total metric**(可选) | 低 | 在 ingestion `node_chunk_documents` 加一行 metric 统计 R1 命中次数;长期监控若从 0 抬头(语料漂移)即触发回看锚词集 |
| **不再扩 B 类样本调参** | — | 当前阈值已在 3 doc 上 0 假阳,继续扩样边际收益低 |

## 7. 学到的事

- **守门设计要看反例**:b_2 是绝佳反例 — step_pattern_count=70(表单列表噪声)但锚词=0。如果 R1 仅看 step_pattern,b_2 会被误升 step mode 产假 step_card。锚词 ≥2 的"必须项"清除了这类陷阱。
- **生产语料标准化降低 routing miss 风险**:A 类 82% + C 类 15.3% 合计 97.3% 被现有 title gate 正确覆盖,生产 SOP 命名规范度高(全部 ≥9 字符,无裸代号)。R1 是面向未来/新接入的保险,而非修当前洞。
- **脱敏 workflow 设计可复用**:secret mapping 仅本地 scratch + transcript 强制聚合 stats + schema 不允许字段含正文摘要 = 三层保护。后续涉及生产数据的 dryrun 可走同模式。

## 工件
- 本报告
- `scratch/d8_phase9_b_class/_secret_mapping.json`(本地,不入 git)
- `scratch/d8_phase9_b_class/b_{1,2,3}.pdf`(本地,scratch/ 已 gitignore)
- `eval_harness/reports/D8_phase{1-8.5}_*.md`(前 8 phase)
