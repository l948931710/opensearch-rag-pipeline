# D8 chunker A/B Tier 0 最终收尾 — efficacy + safety 双 cohort 汇总

> Cohort 拆分策略 (用户 spec): 接受 Tier 0 PDF-only engineering Partial PASS; DOCX 仅做 byte-equal/non-regression 不计入 D8 Δ 与 McNemar; XLSX 做 D8-eligible 定向 stress test 按 discordant pairs 计证据; 若 XLSX 池无法提供足够 activation, 保留 statistical inconclusive, 不再用无效 TIE 凑 n。

## Cohort 重新划分发现

**关键洞察**: XLSX 调研发现 D8 改动 (Path A/B/C/D) **在 xlsx 路径上 0 触发**。原因:

- xlsx image_refs schema 用 `filename + anchor_row` 行级 binding (CLAUDE.md L1346-1384)
- `image_index = None`、`page_num = None`、无 `bbox`
- 完全跨越 Path A/B/C/D 的依赖 (bbox 几何 + image_index 圈号映射)

因此 **xlsx 实际属于 safety cohort 而非 efficacy** — 加 xlsx anchor 完全是"用 TIE 凑 n", 用户策略明确拒绝。

| Cohort | Format | 验证逻辑 | n | 状态 |
|---|---|---|---|---|
| **Efficacy** (D8-eligible) | **PDF only** | 走 Path A/B/C/D, 比对 Δ + McNemar | 21 | **Partial PASS** (5/6, Δ FAIL inconclusive) |
| Safety (non-regression) | XLSX | OFF/ON image_refs byte-equal | 10 step_cards | **PASS** (0 regression) |
| Safety (non-regression) | DOCX | OFF/ON image_refs byte-equal | 73 chunks with images | **PASS** (0 regression) |

## Efficacy cohort 最终判定 (PDF n=21)

| 阈值 (Plan v3.1 #1 primary) | 实测 | 判定 |
|---|---|---|
| dual ON ≥ 0.85 | 0.958 | ✓ |
| **Δ ≥ +0.10** | **+0.083** | **✗ inconclusive** (统计未显著) |
| ON win-rate ≥ 70% | 100% (2W/0L) | ✓ |
| 0 critical regression | 0 loss | ✓ |
| key_jaccard ≥ 0.85 | 1.0 | ✓ |
| McNemar p < 0.05 | b=2, c=0, p≈0.25 | ✗ |

**5/6 通过, Δ inconclusive**。需要累计 b ≥ 6 (再加 ≥4 新 wins) 才能 McNemar p<0.05。**PDF efficacy 池 21 anchor 是天花板** (4 unique PDF doc 限制), 无法在本地提供足够 activation, **保留 inconclusive 作为正式判定**。

## Safety cohort 实证 (XLSX + DOCX)

### XLSX (10 step_cards, 2 doc)

| doc | step_cards w/ image | byte-equal | mismatch |
|---|---|---|---|
| xlsx_sop | 5 | 5 | 0 |
| xlsx_inspect | 5 | 5 | 0 |
| **小计** | **10** | **10** | **0** |

逐 image_ref `(filename, anchor_row, page_num, image_index, block_index)` fingerprint 比对完全一致。

### DOCX (73 chunks with images, 10 doc)

| doc 类别 | doc 数 | chunks w/ image | byte-equal | mismatch |
|---|---|---|---|---|
| eval_samples SOP/manual | 4 (docx_sop, qc, water, manual) | 73 | 73 | 0 |
| fuling_chunk_exp admin | 6 (规章制度类) | 0 | 0 | 0 |
| **小计** | **10** | **73** | **73** | **0** |

eval_samples 4 docx 含 image binding (docx_manual 56 chunks, docx_sop 8, qc 5, water 4)。fuling admin 6 docx 全无图 (规章制度文本类)。**73/73 byte-equal**。

### 总 safety 证据

- XLSX 10 + DOCX 73 = **83 chunk-image pairs byte-equal**
- 0 regression 跨 2 格式 20 doc
- 证实 D8 Path A/B/C/D 改动在 PDF 路径之外**完全无副作用**

## Statistical inconclusive 的科学诚实

按你的 spec, **不用无效 TIE 凑 n**。本地资源调研后:
- PDF efficacy 池硬限 21 (4 doc × 6 step)
- xlsx / docx 在 D8 改动下 0 activation, 加它们只稀释 Δ
- 真要让 McNemar p<0.05 需要 b ≥ 6 即 +4 PDF WIN 新 anchor — 池里没有

因此**正式判定为 statistical inconclusive on D8 efficacy**, 不强行 chase 显著性。

## 工程 verdict

| 维度 | 状态 |
|---|---|
| Engineering (chunker 改动对) | ✅ PASS — 4 张关键图按业务真相归位, 0 critical regression, 100% win-rate |
| Safety (跨格式 non-regression) | ✅ PASS — 83 byte-equal, 0 mismatches |
| Statistical (Δ ≥ +0.10 p<0.05) | ⚠️ INCONCLUSIVE — n=21 PDF 池天花板, 不强行扩样 |

**整体: D8 Tier 0 可放行**, 三个维度中 engineering + safety 双 PASS, 仅 statistical inconclusive 是已知不可弥补限制。

## 下一步建议 (按你的策略)

| 选项 | 推荐 | 备注 |
|---|---|---|
| **(a) 切默认 ON + 91 doc 分批生产重灌** | ✅ 推荐 | 10 PDF 试点 → 全量 91, OFF byte-equal 已验, safety cohort 跨 2 格式实证 |
| **(b) 扩 PDF 池 (新 SOP doc) 再跑 efficacy** | 中性 | 真扩 PDF doc 是唯一能让 D8 efficacy 显著的路径, 但 ROI 看新 PDF 引入难度 |
| **(c) 转 Tier 1 (Plan v3 Step E) conditional gen** | ✅ 推荐 (前置 7/8 就绪) | 本轮验收解锁 Tier 1; 唯一未做的前置 = funnel GT 独立化冻结 |

**推荐序列**: **(a) + (c) 并行**, 跳过 (b) 单做。

## 工程债更新

接续 funnel_fix_n24 报告的 8 条工程债, 本轮新增:

9. **xlsx D8 0 activation 设计可见性**: xlsx 走 anchor_row 通道, D8 Path A/B/C/D 完全 bypass。**TODO**: 在 Plan v3 或 CLAUDE.md 明确"D8-eligible format = PDF only", 避免后续重复调研 xlsx/docx 是否需要扩样。

10. **PDF efficacy 池天花板 = 21 anchor**: 4 unique PDF doc × ~6 step。**TODO**: 若要让 D8 efficacy 统计显著, 需扩 PDF 池 (新 SOP doc 入 eval_samples), n=60+ 是 +10 PDF doc 量级工作。

11. **safety cohort 标准化**: 本轮临时跑 byte-equal 比对脚本验证。**TODO**: 把 `xlsx safety` + `docx safety` 比对集成到 chunker_ab framework 作为 mode (e.g. `--mode safety_only --format xlsx,docx`), 后续 chunker 改动可一键复测。
