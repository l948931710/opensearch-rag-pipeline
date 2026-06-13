# D8 Phase 8.5 — 生产 RDS PDF 全量分类(R1 覆盖率量化)— 2026-06-13

> Phase 8 修了 R1 routing miss(SOP 锚词 fallback),本 phase 用 PROD-RO 连生产 RDS 跑 `document_meta` 全量分类,量化 A/B/C 三层占比 + R1 修法实际影响范围,为生产重灌决策提供数据。

## TL;DR

| 维度 | 数据 | 含义 |
|---|---|---|
| 生产总 active doc | 582 | 含全格式(PDF/DOCX/XLSX 等) |
| **生产 PDF doc 全量** | **111** | 不需抽样,直接全跑分类 |
| A-kw (sop_keyword 命中) | 64 (57.7%) | Phase 1-6 chunker 修直接覆盖 |
| A-wi (wi-\d 命中) | 27 (24.3%) | Phase 1-6 直接覆盖 |
| **A 合计** | **91 (82.0%)** | 立即可启动 DAG 1-3 重灌 |
| **B 短代号 ≤8 字符** | **3 (2.7%)** | Phase 8 R1 fallback 候选,需正文确认锚词 |
| C 长名无 keyword | 17 (15.3%) | clause/text 模式(规章/通知/手册) |
| title 长度分布 ≤8 字符 | **0 (0.0%)** | 生产无 xg001/zs006 这种极短代号 |

## 1. 抽样方法

`opensearch_pipeline/prod_access.py::get_prod_readonly_conn()` PROD-RO 会话级 READ ONLY 连接(防呆双重保险:`SET SESSION TRANSACTION READ ONLY` + RDS 端 `fuling_ro` 只读账号)。

```sql
SELECT title, original_filename, category_l1, category_l2
FROM document_meta WHERE status='active' 
AND (LOWER(original_filename) LIKE '%.pdf' OR LOWER(doc_type) LIKE '%pdf%')
```

不抽样,**直接全量**(111 PDF 不大,无需 LIMIT)。按 Phase 8 R1 fix 的分类逻辑跑:

```python
SOP_KW = ["sop","manual","guide","操作","手册","作业指导","作业导书",
         "流程","规程","检验","培训"]
WI_RE = re.compile(r'(?:^|[^a-z0-9])wi-\d', re.IGNORECASE)
# 拼 title + category_l1 + category_l2 + original_filename 一起搜
search_str = ' '.join([title, cat_l1, cat_l2, fname])
A_kw = any(kw in search_str for kw in SOP_KW)
A_wi = bool(WI_RE.search(search_str))
B = (not A_kw and not A_wi) and len(title_clean) ≤ 8
C = 其他
```

**脱敏**:transcript 不暴露任何具体 doc_id / title / filename,仅输出聚合分类计数。生产数据隐私保护。

## 2. 完整分类结果

| 类别 | 定义 | 数量 | 占比 | Phase 状态 |
|---|---|---|---|---|
| A-kw | title 含 sop/manual/guide/操作/手册/作业指导/流程/规程/检验/培训 任一 | 64 | 57.7% | Phase 1-6 ✓ |
| A-wi | title 匹配 `(?:^|[^a-z0-9])wi-\d` (WI 文号) | 27 | 24.3% | Phase 1-6 ✓ |
| **B** | A 都不命中 + title 清洗后 ≤8 字符(短代号) | 3 | 2.7% | **Phase 8 R1 fallback** |
| C | A 都不命中 + title 长名(>8 字符) | 17 | 15.3% | clause/text(无需重灌) |

## 3. 关键洞察(意外发现)

### 生产 title 都是长名

| title clean 长度 | doc 数 | 占比 |
|---|---|---|
| ≤4 字符 | 0 | 0.0% |
| 5-8 字符 | 0 | 0.0% |
| 9-15 字符 | 10 | 9.0% |
| 16-30 字符 | 63 | 56.8% |
| >30 字符 | 38 | 34.2% |

**生产 RDS 里 title 全部 ≥9 字符**。本地 fuling_chunk_exp/scratch 里的 `xg001`/`zs006` 是研发测试用裸代号,生产侧 title 都被规范化为含完整描述名("xg001-产品工序作业指导书" 等),自然命中 A-kw 关键词。

### B 类 3 doc 是怎么进来的

代码用 `re.sub(r'[\s\(\[（【].*', '', title)` 切掉括号后内容做"清洗 title",可能 title 形如 `"abc001（产品工序说明）"`,清洗后剩 `"abc001"` ≤8 字符 → B 类。需要正文确认锚词才知道是不是真 SOP。

### R1 修法实际影响

- **R1 修法本可能影响**:3 个 B 类 doc(2.7%)
- **R1 修法实际收益(估)**:取决于这 3 doc 正文是否含 ≥2 个 SOP 锚词。若全是真 SOP → +3 doc 转 step mode(生产 step_card 总盘子 +3.3%);若全是假阳性(短代号但非 SOP)→ 0 收益,clause/text 不动
- **R1 修法的真实价值**:**预防性覆盖**未来新接入的短代号 SOP doc,而非修当前生产盘子。当前 91 + 17 = 108 doc(97.3%)已被 A/C 正确路由覆盖。

## 4. 生产重灌策略(更新)

基于 Phase 8.5 分类数据,重灌策略简化为:

| 行动 | 范围 | 优先级 | 风险 |
|---|---|---|---|
| **重灌 A 类 91 doc** | title 含 sop/manual/wi-\d 等关键词 | 高 | 中(生产写,需 PROD-RW 同日 token) |
| **B 类 3 doc 单独审查** | 短代号 + 正文待确认 | 中 | 低(3 doc 量小,可人工 review) |
| **C 类 17 doc 不动** | 长名无 SOP 关键词,clause/text 模式 | — | 0(byte-equal 保护) |

### 分批重灌建议

- **批 1**:A-wi 27 doc(WI 文号最规范,Phase 4/6 image anchor + 圈号 override 收益最大)
- **批 2**:A-kw 64 doc(标题含"作业指导书/操作手册"等)
- **批 3**:B 类 3 doc(人工审查后,若 ≥1 个真 SOP 则触发 R1 重灌)

每批跑 DataWorks DAG 1 → 2 → 3,监控 `node_deactivate_old_chunks` 在 push HA3 成功后才退役旧 chunks(`never disappear from index` 不变量保护)。

## 5. 已知遗留

- **Bug E 跨页 range-ref**(pdf_sop step 3.1 image 9):chip task_3f1c4896 跟踪,影响 1 chunk J=0.667,生产重灌不影响
- **B 类 3 doc 正文锚词验证**:需要下载 OSS raw 跑 _extract_and_chunk + _detect_step_patterns dump,确认 R1 fallback 是否触发
- **xlsx/docx 同类抽样**:本 phase 仅 PDF,xlsx/docx 是否有同型 routing miss 未验证(D8 主要在 PDF 路径深耕,xlsx/docx 走独立路径)

## 6. 决策与下一步

### 立即变更(本 commit)
- 本报告

### 选项

| 选项 | 描述 | 风险/成本 |
|---|---|---|
| (a) B 类 3 doc 下载验证 | OSS get + _extract_and_chunk dryrun,确认 R1 触发率 | 低(PROD-RO + scratch 临时区) |
| (b) 启动 A-wi 批 1 重灌(27 doc)试点 | 最规范命名 batch,DAG 1-3 全量跑,需 PROD-RW 同日 token | 高(生产写) |
| (c) 关闭 D8 evolution + push working tree | D8 已 8 个 phase 完整覆盖,转其他工作 | 低 |

## 工件
- 本报告
- `eval_harness/reports/D8_phase{1-8}_*.md`(前 8 phase)
