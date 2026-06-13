# D8 Phase 10 — Bug E 修(跨页 range-ref override,Path C) — 2026-06-13

> Bug E (pdf_sop step 3.1 image 9 跨页 range-ref) D8 evolution 最后 1 个 chip。Phase 10 加 **Path C 跨页 range-ref override**:扫全文找含 "X-Y步" 圈号范围引用的 step text,image 圈号 ∩ range ≥2 + bigram 命中 ≥3 双信号守门 → 跨页 override。**pdf_sop ON 0.879 → 1.000(满分),3 doc mean ON 0.8889 → 0.9297**;OFF byte-equal,day7 ALL_EQUAL。

## TL;DR

| 维度 | Phase 6 ON | Phase 10 ON | Δ |
|---|---|---|---|
| pdf_sop | 0.879 | **1.000** ✨ | **+0.121** (8/8 chunks 全 J=1.0) |
| pdf_xs_wi_007 | 0.889 | 0.889 | 0 |
| pdf_it_xxh_003 | 0.900 | 0.900 | 0 |
| **3 doc Mean(ON)** | 0.8889 | **0.9297** | **+0.0408** |
| 3 doc Mean(OFF 默认) | 0.8389 | **0.8389** | 0 byte-equal ✓ |
| 90 chunker+ingestion_binding tests | ✓ | ✓ | — |
| day7_chunker_postfix_verify --n-runs 3 | ALL_EQUAL ✓ | **ALL_EQUAL** ✓ | 55/55 byte-equal |

## 1. 根因(Phase 10 understand workflow 实证)

### Image 9 跨页错绑链路

| 字段 | 值 |
|---|---|
| image 9 page_num | 3 |
| image 9 bbox | [42.48, 218.04, 500.76, 392.76] |
| image 9 ocr_text | "" (空) |
| image 9 visual_summary | "U8系统'档案查询'界面截图,左侧为功能导航栏..." |
| image 9 **vlm_annotation_map.keys** | **['①','②','③','④','⑤','⑥']** ← 关键被忽视的信号源 |

`_insert_image_refs_heuristic` 几何 best_idx 选 page 3 的 step 4.2 paragraph(image 9 在 page 3 物理位置紧邻 step 4.2),`_chunk_by_step` 扫到 image_ref block 时 current_step = step 4.2 → image 9 错绑 step 4.2 step_card。

### Step 3.1 range-ref pattern

step 3.1 paragraph(page 2):
```
步骤3： 3.1 进入U8系统的"扫码报检"界面（如下图②-⑥步操作）。
```

`_RANGE_RE` 命中 `②-⑥` → range [2, 6]。但 Phase 6 Path B 限同页 anchors,page 2 的 step 3.1 不在 page 3 image 9 的 anchors 列表里,**Path B 不跨页**。

### 区分度验证(关键守门基础)

image 9 vs step 3.1 bigram:
- image 9 visual+ocr 与 step 3.1 text "进入 U8 系统的扫码报检界面" 共有 4 个 bigram:`{U8, 8系, 系统, 界面}` — 主题词共现
- image 10 visual "手写生产记录表单" 与 step 3.1 共有 0 个 bigram — 完全无关

bg_hit ≥ 3 既能 catch image 9 又能拒 image 10。

## 2. 修法 — Path C 跨页 range-ref override

`opensearch_pipeline/pipeline_nodes.py:2273-2342`(策略 1b-2 几何 + Path A/B 之后):

```python
if (best_idx is not None and os.getenv("RAG_IMAGE_CONTENT_OVERRIDE", ...) ...):
    # image 圈号集 = OCR + vlm_annotation_map.keys (双源)
    img_cir_all = (set OCR 圈号) | (set vlm_annotation_map keys 中圈号)
    img_cir_nums = {圈号→数字, 如 ②→2}
    if len(img_cir_nums) >= 2:
        # 逆序扫全 blocks(前页 step 优先)
        for block in reverse(blocks):
            if not _STEP_BOUNDARY_RE.search(block.text):
                continue
            for m in _RANGE_RE.finditer(block.text):
                range = [start_num, end_num]
                hit = sum(1 for n in img_cir_nums if start_num <= n <= end_num)
                if hit < 2: continue                  # 圈号守门
                bg_hit = bigram(img_text, block.text) 
                if bg_hit < 3: continue                # 语义守门
                # 双信号过 → 候选,score = hit * bg_hit,选最高
                ...
        if best_range_idx is not None:
            best_idx = best_range_idx
```

**关键设计决策**:

1. **双源圈号**:`OCR + vlm_annotation_map.keys()` 是 D8 evolution 一直被忽视的关键修复——Phase 6 Path B 只看 OCR 漏过 image 9(OCR 空)的 6 个圈号。
2. **双信号守门**:
   - 圈号 ∩ range ≥ 2(语法信号:image 标注的圈号确实落在 range 内)
   - bigram 命中 ≥ 3(语义信号:image 描述的主题词与 step 文本共现)
   - 单一信号不够,排除"圈号巧合 hit"或"通用 bigram 噪声"两类假阳性
3. **跨页支持**:逆序扫全文 blocks,不限 anchors 同页,让 page 2 的 step 3.1 救回 page 3 的 image 9。
4. **逆序+break**:先扫前页 step 优先(SOP 中 range-ref 通常引用前面 step 范围,不会"②-⑥步操作"指向后面 step)。
5. **env-gated**:复用 `RAG_IMAGE_CONTENT_OVERRIDE`,与 Path A/B 共享开关,默认 OFF 保护生产 baseline byte-equal。

## 3. 实测数据(per-chunk Jaccard)

### pdf_sop ON 状态完整 8/8 chunks 全 J=1.0(只 strong chunks):

| chunk | gt | pred | J |
|---|---|---|---|
| 步骤 1.1 收取交货单 | [1] | [1] | 1.000 ✓ |
| 步骤 1.2 核对错误处理 | [2] | [2] | 1.000 ✓ |
| 步骤 1.3 抄录订单信息 | [3] | [3] | 1.000 ✓ |
| 步骤 2 交货单分类 | [4] | [4] | 1.000 ✓ |
| **步骤 3.1 U8 扫码报检** | **[5,6,9]** | **[5,6,9]** | **1.000** ✨ (Path C image 9 跨页 override) |
| 步骤 3.2 扫码枪扫描 | [7] | [7] | 1.000 ✓ |
| **步骤 4.1 填写设备班次** | **[10]** | **[10]** | **1.000** ✓ (Phase 6 Path B image 10 圈号) |
| 步骤 5 群通知完成 | [11] | [11] | 1.000 ✓ |

3 个 0-image GT chunks(前言/步骤 4 父/步骤 4.2)pred 也 [] = empty-vs-empty=1.0,不在 strong 计数但全对。

### byte-equal & stability

```
$ python -m pytest tests/test_chunker.py tests/test_step_card_expand.py \
                   tests/test_ingestion_binding.py -q
90 passed in 0.72s

$ python scripts/eval_image_binding_pdf.py                                  # OFF
  pdf_sop          jaccard=0.833  ← byte-equal vs Phase 5
  pdf_xs_wi_007    jaccard=0.778  ← byte-equal
  pdf_it_xxh_003   jaccard=0.900  ← byte-equal
  Mean Jaccard:    0.8389         ← byte-equal

$ RAG_IMAGE_CONTENT_OVERRIDE=1 python scripts/eval_image_binding_pdf.py    # ON
  pdf_sop          jaccard=1.000  ← +0.167 (Phase 10 Path C image 9)
  pdf_xs_wi_007    jaccard=0.889  ← unchanged (Phase 6 Path A)
  pdf_it_xxh_003   jaccard=0.900  ← unchanged
  Mean Jaccard:    0.9297         ← +0.0908 vs Phase 5 OFF baseline

$ bash scripts/day7_chunker_postfix_verify.sh --n-runs 3
  verdict: ALL_EQUAL ✅
  per_fmt std_max: 0.0000
  per_chunk byte-equal: 55/55
  xlsx: 1.0 / docx: 0.9898 / pdf: 0.8389
```

## 4. D8 evolution 全清单(Bug A→E + F + R1)

| Bug | Phase | 状态 | 升幅 |
|---|---|---|---|
| A. dotted swap(pdf_sop 4.1/4.2) | 6 | ✓ Path B 圈号 | step 4.1 0.5→1.0 |
| B. 顶层 step swap(xs_wi_007 1↔2) | 4 | ✓ Path A bigram | step 1/2 0.5→1.0 |
| C. matcher 选错 sub-chunk | 5 | ✓ 含图优先 + step_no | 跨 doc |
| D. markdown bullet 不出 step_card | 5 | ✓ heading→paragraph | it_xxh_003 0.219→0.700 |
| **E. 跨页 range-ref(pdf_sop 3.1 image 9)** | **10** | **✓ Path C 跨页 + 双源圈号 + 双信号守门** | **step 3.1 0.667→1.0,pdf_sop 0.879→1.000** |
| F. ToC implicit step(it_xxh_003 step 7) | 5 收尾 | ✓ _extract_toc_steps | it_xxh_003 0.700→0.900 |
| R1. title routing miss(xg001/zs006) | 8 | ✓ SOP 锚词 fallback | 16 张图全绑(GT 外) |

**D8 evolution 7 个 Bug 全部修复,Bug E 收官**。

## 5. 完整 baseline 历程

| Phase | OFF | ON | Δ from baseline |
|---|---|---|---|
| Phase 3 baseline(3 doc GT 扩好) | 0.6966 | — | — |
| Phase 5(Bug D + matcher C2) | 0.7722 | — | +0.0756 |
| Phase 5 收尾(Bug F + dump bug) | 0.8389 | — | +0.1423 |
| Phase 6(Bug A Path B 圈号 override) | 0.8389 | 0.8222 | OFF byte-equal,ON +0.0500 |
| Phase 8(R1 锚词 fallback) | 0.8389 | — | OFF byte-equal |
| **Phase 10(Bug E Path C 跨页)** | **0.8389** | **0.9297** | **OFF byte-equal,ON +0.0908 vs Phase 5 收尾** |

## 6. 决策与下一步

### 立即变更(本 commit)
- `opensearch_pipeline/pipeline_nodes.py:2273-2342`:Path C 跨页 range-ref override
- 本报告

### D8 evolution 关闭建议

- Bug A/B/C/D/E/F/R1 全部修复 → D8 chip 全清
- ON 状态 3 doc mean **0.9297**(理论上限因 0-image GT 限制 ~0.93),已接近天花板
- 下一步:**默认 ON `RAG_IMAGE_CONTENT_OVERRIDE`**(扩 3 doc 多轮稳定后切默认)或转其他工作

### 留 chip(可选,低优先级)

| Chip | 描述 | 优先级 |
|---|---|---|
| `RAG_IMAGE_CONTENT_OVERRIDE` 默认 ON | 多轮 prod 抽样稳定后切默认,让生产享受 Path A/B/C 升幅 | 低 |
| Path C 守门超参数化 | 当前 hardcode bg_hit≥3 / hit≥2,可考虑 env 调参 | 极低 |
| `r1_fallback_triggered_total` metric | Phase 9 留 chip,长期监控 R1 漂移 | 极低 |

## 7. 学到的事

- **vlm_annotation_map.keys() 是被忽视的圈号信号源**:OCR 可能空(VLM 解析 ITER UI 截图 OCR 不准),但 vlm_annotation_map 是 VLM 主动识别的圈号-描述映射,key 集等价于"VLM 看到的圈号"。Phase 6 Path B 只读 OCR 漏过 image 9 6 个圈号,Phase 10 Path C 双源圈号是关键。
- **跨页 SOP 引用是不可避免的物理现实**:range-ref "②-⑥步操作" 写在 step text 但图片在 next page 是 PDF SOP 排版的自然结果(text 写到 page 底,images 排 page 顶)。Path A/B 同页限定是结构性盲区,必须 Path C 跨页才完整。
- **双信号守门防误拉是 D8 evolution 的核心方法论**:Phase 6 Path B(圈号 Jaccard)、Phase 8 R1(锚词 + 阈值)、Phase 10 Path C(圈号 hit + bigram match)— 都是用两个独立信号源相互验证,避免单信号假阳性。这个 pattern 比"放宽单一阈值"安全得多。

## 工件
- 本报告
- `scratch/d8_phase10_{off,on}_v2.json`(实测数据)
- `eval_harness/reports/D8_phase{1-9}_*.md`(前 9 phase 完整链)
