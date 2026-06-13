# D8 Phase 6 — Bug A 修复（圈号 sub-step override）— 2026-06-13

> Bug A (pdf_sop step 4.1/4.2 dotted swap, image 10 错绑) D8 Phase 1/2/4 三度试错失败:几何 break 引入 regression / content-match bigram 信号弱 (bg=1 vs 7) / Phase 4 守门排除 dotted child step 不动。Phase 6 引入 **圈号集 Jaccard override (Path B)** 作为第三层信号:image OCR 含的圈号字符集 vs step block 圈号集精确匹配 → image 10 OCR {①②③④⑤} ∩ step 4.1 {①②③} J=0.6 触发 override。env-gated 默认 OFF byte-equal,ON 升 +0.0500。

## TL;DR

| 指标 | OFF (默认) | ON `RAG_IMAGE_CONTENT_OVERRIDE=1` |
|---|---|---|
| PDF 3-doc Mean | **0.7722** (byte-equal vs Phase 5) | **0.8222** (+0.0500) |
| pdf_sop | 0.833 unchanged | **0.879** (+0.046) ← Bug A Path B |
| pdf_xs_wi_007 | 0.778 unchanged | **0.889** (+0.111) ← Path A Phase 4 |
| pdf_it_xxh_003 | 0.700 unchanged | 0.700 (无圈号,Path B 不触发) |
| xlsx | 1.0 | 1.0 |
| docx | 0.9898 | 0.9898 |
| 90 chunker + ingestion_binding tests | ✓ | — |
| day7_chunker_postfix_verify --n-runs 3 | ALL_EQUAL ✓ (pdf 0.7722 byte-equal) | — |

## 1. 根因诊断 (3 路 parallel understand workflow)

### Image 10 几何错位

image 10 bbox y0=401 y1=661 (page 3, 跨度 260pt) 落在**所有 step 4.x 段落之下**:

| block | type | y0~y1 | 圈号集 |
|---|---|---|---|
| [16] step4 paragraph "步骤4:报检..." | paragraph | 105~116 | [] |
| [17] step4.1 paragraph "4.1 按《交货单》填①设备②班次③数量" | paragraph | 130~162 | {①,②,③} |
| [18] step4.2 heading "4.2 填写完后,...④根据..." | heading | 167~178 | {④} |
| [19] step4.2 paragraph "⑤根据交货单备注数...⑥报检" | paragraph | 183~209 | {⑤,⑥} |
| image 10 | — | **401~661** | OCR={①,②,③,④,⑤} |

主循环 overlap-max best_overlap=0 best_idx=None → fallback "上方最近 y0 ≤ img_y0+1.0" 取 [16,17,18,19] 中**最后一个 = [19]** → image 10 inject 到 [19] 后 → chunker `_chunk_by_step` 扫到时 current_step=step 4.2 sub_no=2 → image 10 错绑 step 4.2。

### 3 度试错失败

| Phase | 试法 | 结果 | 原因 |
|---|---|---|---|
| 1 | 加 `if y0 > img_y0: break` 让"块开头在图上缘下方"段不参与 overlap | regression (image 5/6 错绑 step 2) | step 3.1 短段 10pt 被 break 误伤 |
| 2 | content-match `_content_match_steps` 在 raw block 粒度做 IDF 加权稀有词匹配 | regression (step 2/3.2 误派) | raw-block cand 噪声大,与 xlsx step-level cand 不同 |
| 4 | Path A bigram override (MIN_ABS=10, RATIO=3.0) | 不触发 (Phase 4 报告实证) | image 10 visual_summary 通用词 bigram vs step 起始块 alt bg=1 < 10 |

### Path B 圈号信号

| 候选 step | 圈号集 | ∩ image OCR | Jaccard |
|---|---|---|---|
| step 4.1 (block 17) | {①,②,③} | {①,②,③} | **0.60** ✓ |
| step 4.2 heading (block 18) | {④} | {④} | 0.20 |
| step 4.2 paragraph (block 19, geo pick) | {⑤,⑥} | {⑤} | 0.17 |

image 10 OCR 含的 ①②③④⑤ 是用户在表单上手写填进去的编号示例,step 4.1 文本"按《交货单》填①设备②班次③数量"的 ①②③ 是该 step 的填写指示——**两者圈号集匹配 = 该图正是该 step 的填写示例**。这是 PDF SOP 的特定语义信号,比 visual_summary bigram 更精确。

## 2. 修法 — Path B 圈号集 Jaccard override

`opensearch_pipeline/pipeline_nodes.py:2202-2255`(策略 1b-2 几何 best_idx 选定后,Path A 之后):

```python
if (best_idx is not None and os.getenv("RAG_IMAGE_CONTENT_OVERRIDE", ...) ...):
    _CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    img_circled = set(c for c in (va.get("ocr_text") or "") if c in _CIRCLED)
    if len(img_circled) >= 2:
        # 算 geo block 圈号 Jaccard,扫同页其他 anchor 找更强匹配
        ...
        if (best_cir_idx != best_idx
                and best_cir_jacc >= 0.5
                and best_cir_jacc >= max(geo_jacc, 0.01) * 1.5):
            best_idx = best_cir_idx
```

**守门设计:**
- `len(img_circled) >= 2`:单圈号 OCR 噪声大(可能是 image edge 误读出"·"被识为"③"),要求至少 2 个圈号才有信号
- `best_cir_jacc >= 0.5`:绝对匹配阈值,过滤"image 含 ①②,step 含 ⑤⑥⑦⑧"这种交集=0 的 case
- `1.5x geo_jacc`:相对显著性,避免在 geo Jaccard 已经 0.4 时被 0.5 抢戏
- `for _y0, bidx in anchors`:同页限定(anchors 已是同页 paragraph/heading 候选)
- 共享 `RAG_IMAGE_CONTENT_OVERRIDE` env 与 Path A 复合启用

## 3. 实测数据(per-chunk diff,OFF vs ON)

跑 `scripts/eval_image_binding_pdf.py`,对比 Path A+B 改动:

| doc | chunk | gt | OFF pred | ON pred | OFF J | ON J | 修法 |
|---|---|---|---|---|---|---|---|
| pdf_sop | 步骤 4.1 填写设备班次 | [10] | [9,10] | **[10]** | 0.500 | **1.000** | Path B 圈号 |
| pdf_sop | 步骤 4.2 班组人员 | [] | [9,10] | [9] | 0.000 | 0.000 | image 9 跨页未解(Bug E) |
| xs_wi_007 | 步骤 1 标识卡清点 | [1] | [1,2] | **[1]** | 0.500 | **1.000** | Path A 已加(Phase 4) |
| xs_wi_007 | 步骤 2 收集核对 | [2] | [1,2] | **[2]** | 0.500 | **1.000** | Path A 已加(Phase 4) |
| 其他 chunks | — | — | — | unchanged | — | — | — |

Phase 6 净新增升幅:pdf_sop step 4.1 0.5 → 1.0 (Path B 圈号)。pdf_xs_wi_007 step 1/2 升幅是 Path A (Phase 4) 启用本身的功劳(Path B 在 xs_wi_007 不触发——image 1/2 OCR 含 0 圈号字符)。

## 4. 测试矩阵

```
$ python -m pytest tests/test_chunker.py tests/test_step_card_expand.py \
                   tests/test_ingestion_binding.py -q
90 passed in 0.89s

$ python scripts/eval_image_binding_pdf.py                                  # OFF
  pdf_sop          jaccard=0.833  ← byte-equal vs Phase 5
  pdf_xs_wi_007    jaccard=0.778  ← byte-equal
  pdf_it_xxh_003   jaccard=0.700  ← byte-equal
  Mean Jaccard:    0.7722         ← byte-equal

$ RAG_IMAGE_CONTENT_OVERRIDE=1 python scripts/eval_image_binding_pdf.py    # ON
  pdf_sop          jaccard=0.879  ← +0.046 (Path B image 10)
  pdf_xs_wi_007    jaccard=0.889  ← +0.111 (Path A image 2, Phase 4)
  pdf_it_xxh_003   jaccard=0.700  ← 无圈号 OCR 不触发
  Mean Jaccard:    0.8222         ← +0.0500

$ bash scripts/day7_chunker_postfix_verify.sh --n-runs 3
  verdict: ALL_EQUAL ✅
  per_fmt std_max: 0.0000
  per_chunk byte-equal: 55/55
  xlsx: 1.0 / docx: 0.9898 / pdf: 0.7722  (3 run 完全一致)
```

## 5. 已修 vs 未修

| Bug | 状态 | 备注 |
|---|---|---|
| **A. dotted sub-step image 错绑(pdf_sop 4.1/4.2)** | **✓ 修复** | Path B 圈号 J=0.6 触发,step 4.1 0.5→1.0 |
| B. 顶层 step image 错绑(xs_wi_007 1↔2) | ✓ 修复(Phase 4) | Path A bg=21 vs 2 触发 |
| C. typed pool 同质类型 sub-chunk matcher 选错 | ✓ 修复(Phase 5) | 含图优先 + step_no 锁定 |
| D. markdown bullet SOP 不出 step_card | ✓ 修复(Phase 5) | heading→paragraph fall through |
| **E. 跨页 range-ref(pdf_sop step 3.1 image 9 + step 4.2 image 9 错绑)** | **未修** | 需 `_chunk_by_step` 跨页 step 续连机制 |

## 6. 决策与下一步

### 立即变更(本 commit)
- `opensearch_pipeline/pipeline_nodes.py:2202-2255`:Path B 圈号 override(env-gated,与 Path A 共享 `RAG_IMAGE_CONTENT_OVERRIDE`)
- 本报告

### 留 chip(精简)

| Chip | 描述 | 优先级 |
|---|---|---|
| **Bug E 跨页 range-ref(pdf_sop step 3.1 image 9)** | step 3.1 "②-⑥步操作" image 9 在 page 3 但 step 3.1 文本在 page 2,需 `_insert_image_refs_heuristic` 跨页 range-ref 解析(image 9 OCR 圈号集 = [] 无圈号信号,Path B 不能解;靠 step text "②-⑥" 范围引用) | 低 — 影响 1 chunk |
| **默认 ON `RAG_IMAGE_CONTENT_OVERRIDE`** | OFF 0.7722 / ON 0.8222 净升 +0.0500;3 doc 多轮稳定后切默认 ON | 中 |
| **it_xxh_003 step 7 漏识别 + image 25 漏绑**(D8 Phase 5 留) | chunker step boundary 边界识别问题 | 中 |
| **task_b297628c**(D8 Phase 3 + Phase 4 chip,覆盖 Bug A/B/C/D 全集) | Bug A/B/C/D 都已修,可关闭 | 关闭建议 |

## 7. 学到的事

- **PDF SOP 圈号 OCR 是低维但高精度的语义信号**:visual_summary bigram 含通用词命中弱,但 image OCR 圈号集 vs step text 圈号集是"填写示例 vs 填写指示"的精确对应,Jaccard 0.6 远超 bigram 0.7 噪声门槛。一类被忽视的 cheap signal。
- **多层 override 复合启用**:Path A (bigram 通用语义) 覆盖 xs_wi_007 顶层 step 互换;Path B (圈号集精确语义) 覆盖 pdf_sop dotted child step 互换。两个独立信号源,守门正交,启用同一 env。
- **Bug A 三度试错的教训**:几何/通用 bigram/dotted-aware fix 都失败,Path B 成功是因为发现"image OCR 圈号集"这个 image 内含的语义元数据,不再依赖几何或全文文本相似度。**找对信号源 > 调阈值**。

## 工件
- 本报告
- `eval_harness/reports/D8_phase{1-5}_*.md`(前 5 phase)
- `scratch/d8_phase6_{off,on}.json`(OFF/ON 实测数据)
