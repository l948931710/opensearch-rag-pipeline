# D8 Phase 4 — Bug B 修复（image 错绑相邻 step）— 2026-06-13

> 接续 D8 Phase 3 finding：xs_wi_007 顶层 step 1↔2 image 错绑实证为「chunker step boundary 与 image anchor 冲突」通病（不限 dotted child step）。本 phase 落地两处 fix：anchor 结构性过滤（always-on）+ content-match 覆写（env-gated）。**Bug B 完全修复**，xs_wi_007 +0.167；OFF byte-equal；pdf_sop/xlsx/docx 不退。

## TL;DR

| 指标 | OFF（默认） | ON `RAG_IMAGE_CONTENT_OVERRIDE=1` |
|---|---|---|
| PDF 3-doc mean | **0.6966**（byte-equal vs Phase 3） | **0.7350**（+0.0384）|
| pdf_sop | 0.788 unchanged | 0.788 |
| **pdf_xs_wi_007** | 0.722 unchanged | **0.889（+0.167）** |
| pdf_it_xxh_003 | 0.632 unchanged | 0.632 |
| xlsx | 1.0 ✓ | 1.0 |
| docx | 0.9898（D7 lock 0.9847）| 0.9898 |
| 36 chunker tests | ✓ | — |
| `day7_chunker_postfix_verify --n-runs 3` | ALL_EQUAL ✓ | — |

## 1. 根因诊断（3 doc 实测 + instrument）

**xs_wi_007 image 1（产品标识卡）geo vs content 决策表**：

| block (page 1) | y0 | y1 | overlap | bigram∩ | 备注 |
|---|---|---|---|---|---|
| i=0 preamble | 95 | 132 | -84 | 4 | |
| i=1 **步骤1** "按《产品标识卡》清点" | 152 | 178 | -37 | **21** | ◀ content max |
| i=2 步骤2 "向各区班长收集交货单" | 184 | 196 | -20 | 0 | |
| i=3 sub of 步骤2 | 266 | 277 | +10 | 6 | |
| i=4 sub of 步骤2 "机台上尾数" | 321 | 367 | **+46** | 2 | ◀ geo max |
| image 1 bbox y | 216 | 395 | — | — | |

→ 几何 overlap-max 选 i=4（step 2 延续段），与图片语义 step 1 显著矛盾（10x bigram 差距）。

**xs_wi_007 image 30 反例**（GT step 5.1 主流程）：

geo overlap-max 选 i=16 `'①  ②\n④  ⑤'`（multi-char circled overlay，不是 step 段也不是 step boundary）。若仅按"非 step 起始块即 override"会被语义最匹配的 sub-bullet 3「3）假如点击带不出人员」吸走 → 错绑 5.2 异常流程。

**关键洞察**：page_block_anchors 把 **table 块（pdf_extractor 同页 table 先扫，y0 落 step 之间，块流早于 step paragraph）** + **circled overlay 块（单字符 cl 或多字符 ①②④⑤ 浮标）** 都当 anchor，制造了两类系统性几何错位。

## 2. 修法（两层）

### Fix 1（结构性，always-on）— `_insert_image_refs_heuristic` anchor 候选收窄

`opensearch_pipeline/pipeline_nodes.py:2065-2095` 把 page_block_anchors 构造从「排除 ocr_text/image_ref」改为「**只允许 paragraph/heading**，且非 circled_label 单字符块，且非"圈号+空白+双击"组成的多字符 overlay 块」。

排除依据：
- **table**：`pdf_extractor` 把同页 table 先入 blocks（pdf_sop i=7/8 page 2 table），随后才是页内 paragraph（i=9+）。table y0 落在 step 之间（i=7 y0=134 在 step 2 y0=121 与 step 3 y0=424 之间）。geo "above" 规则选中 i=8（U8路径表）→ image_ref 插入位置在 step 2 paragraph **之前** → `_chunk_by_step` 缓存为 pending_images → 归到下一个新 step。pdf_sop image 5/6 page 2 错绑 step 2 实证。tables 不代表 step 起始文本，不该当 anchor。
- **circled overlay**：单字符 cl 段（pdf_sop i=11 "①"、i=13 "⑧"）是几何标注层；多字符段（xs_wi_007 i=16 "①  ②\n④  ⑤"）是 2D 浮标。两者都不是 step 文本。

OFF 状态 byte-equal：anchor 候选变了但实际 best_idx 落到等价 step 块（因 chunker `_chunk_by_step` 本就 skip 这些）— 3 doc 39 GT 每一条 per_chunk 完全一致。

### Fix 2（语义，env-gated `RAG_IMAGE_CONTENT_OVERRIDE`）— content-match 覆写

`opensearch_pipeline/pipeline_nodes.py:2126-2202` 几何 best_idx 选定后，若：
1. geo pick 自身不是 step 起始块（不含 `STEP_BOUNDARY` 标记）
2. 图片 visual_summary + ocr_text 与同页其他 **step 起始块** 的 bigram 重叠 ≥10 且 ≥3× geo_score

→ 覆写 best_idx 到该 step 块。

守门"geo 已是 step 起始块即不覆写"防止：同 step 多子条目（`1）.../2）.../3）...`）都匹配 STEP_BOUNDARY，几何选中后 content match 把图错移到关键词更多的子条目。

阈值保守（MIN_ABS=10, RATIO=3.0）：
- xs_wi_007 image 1：bg=21 vs geo bg=2 → 触发 ✓
- pdf_sop image 9/10：bg=1/7 信号弱不触发（pdf_sop 0.788 不变）
- it_xxh_003：无 step_card 不影响

Env-gate 默认 OFF：生产保守不开，评测开。3 doc 实测稳定后再考虑默认 ON。

## 3. 测试矩阵

```
$ python -m pytest tests/test_chunker.py tests/test_step_card_expand.py -q
36 passed in 0.57s

$ python scripts/eval_image_binding_pdf.py                    # OFF
  pdf_sop          jaccard=0.788  ← byte-equal vs Phase 3
  pdf_xs_wi_007    jaccard=0.722  ← byte-equal
  pdf_it_xxh_003   jaccard=0.632  ← byte-equal
  Mean Jaccard:    0.6966         ← byte-equal

$ RAG_IMAGE_CONTENT_OVERRIDE=1 python scripts/eval_image_binding_pdf.py    # ON
  pdf_sop          jaccard=0.788  ← 不变
  pdf_xs_wi_007    jaccard=0.889  ← +0.167 (步骤1 0.0→1.0, 步骤2 0.5→1.0)
  pdf_it_xxh_003   jaccard=0.632  ← 不变
  Mean Jaccard:    0.7350         ← +0.0384

$ bash scripts/day7_chunker_postfix_verify.sh --n-runs 3
  verdict: ALL_EQUAL ✅
  per_fmt std_max: 0.0000
  per_chunk byte-equal: 64/64
  xlsx: 1.0 unchanged
  docx: 0.9898 (D7 lock 0.9847 不退，略升)
```

## 4. 已修 vs 未修

| Bug（D8 Phase 3 报告 4 类）| 状态 | 备注 |
|---|---|---|
| A. dotted sub-step image 错绑（pdf_sop 4.1↔4.2）| **未修** | content match bg=7 信号不足；提阈值会引入其他文档退步 |
| **B. 顶层 step image 错绑（xs_wi_007 1↔2）** | **✓ 修复** | bg=21 vs 2 强信号，env-gated ON 触发 |
| C. step 多 sub-chunk matcher 选错 | 未修 | 不在 chunker 层 — 是 ingestion_binding matcher 选 chunk 策略 |
| D. markdown bullet SOP 不出 step_card | 未修 | 单独 chip（it_xxh_003 0.632→可能 0.85+）|

## 5. 决策与下一步

### 立即变更（本 commit）
- `opensearch_pipeline/pipeline_nodes.py`：
  - anchor 候选从 "ocr_text/image_ref 排除" 改为 "paragraph/heading 白名单 + circled 排除"
  - geo best_idx 后加 env-gated content-match 覆写
- 本报告

### 留 chip（精简版）
| Chip | 描述 | 优先级 |
|---|---|---|
| pdf_sop Bug A（4.1/4.2 dotted child step swap）| content-match 信号太弱，需更激进策略（layout-aware step segmentation 或 fig-ref 标注扫描）| 中 |
| Bug D markdown bullet SOP 不出 step_card | `_chunk_by_step` 内部 step block 边界识别 | 中 — it_xxh_003 0.632 → 可能 0.85+ |
| 默认 ON `RAG_IMAGE_CONTENT_OVERRIDE` | 跑 ≥3 doc 多次确认稳定后切默认 ON | 低 |

### 已知不修
- Bug E 跨页 range-ref（pdf_sop step 3.1 image 9）：需 _chunk_by_step 跨页 step 续连机制，独立工作

## 6. 学到的事

- **pdf_extractor block 顺序 ≠ 几何顺序**：tables 与 paragraphs 不按 y-position 排列，几何"上方块"规则有系统性陷阱。anchor 候选必须收窄到 step 文本性质的块。
- **circled overlay 不只是单字符**：`pdf_extractor` 给 `①` 单字符标 `circled_label`，但 `①  ②\n④  ⑤` 多字符整段不标 — 需用结构性 regex 兜底。
- **content match 守门很关键**：geo_is_step 守门避免同 step 多子条目互相吸引，是 Path A 能稳定 ON 的关键。
- **三方控制论**：anchor 收窄（结构）+ 几何（位置）+ content match（语义）形成三层防御，单一层不够。

## 工件清单
- 本报告
- `eval_harness/reports/D8_phase3_pdf_gt_expansion.md`（前一 phase 评估）
- `scratch/d8_phase3/baseline_off.json`（OFF 基线）
- `scratch/d8_phase3/baseline_on_v5.json`（ON 最终结果）
- `scratch/d8_phase3/instrument_all3.py`（几何/content 对比 instrument）
- `scratch/d8_phase3/dump_chunks_after_fix.py`、`trace_enriched.py`、`trace_best_idx.py`（诊断脚本）
