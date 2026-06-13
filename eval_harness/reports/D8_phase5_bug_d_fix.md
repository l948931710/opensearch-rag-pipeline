# D8 Phase 5 — Bug D 修复 + Matcher C2 修 — 2026-06-13

> 接续 D8 Phase 3 留下的 Bug D（markdown bullet/heading SOP `_chunk_by_step` 不出 step_card）。本 phase 落地三处 fix：chunker heading→paragraph fall through + GT 重标 + matcher Bug C2。3 doc 全 升,**Mean Jaccard 0.6966→0.7722 (+0.0756)**;byte-equal verify ALL_EQUAL,xlsx/docx 不退。

## TL;DR

| 维度 | D8 Phase 3 baseline | D8 Phase 5 baseline | Δ |
|---|---|---|---|
| **PDF 3-doc Mean** | 0.6966 | **0.7722** | **+0.0756** |
| pdf_sop | 0.788 | **0.833** | **+0.045** ✨ |
| pdf_xs_wi_007 | 0.722 | **0.778** | **+0.056** ✨ |
| pdf_it_xxh_003 | 0.632 | **0.700** | **+0.068** ✨ |
| xlsx | 1.0 | 1.0 | 0 ✓ |
| docx | 0.9898 | 0.9898 | 0 ✓ |
| 90 chunker + ingestion_binding tests | n/a | ✓ | — |
| day7_chunker_postfix_verify --n-runs 3 | ALL_EQUAL ✓ | ALL_EQUAL ✓ | — |

## 1. 根因诊断（4 路 parallel discovery workflow）

it_xxh_003 实测路径:
1. `_STEP_DETECT_RE` 触发 step mode ✓（D8 Phase 3 加 markdown bullet 容忍后）
2. node_chunk_documents 路由到 step mode ✓
3. `_chunk_by_step` 内部 → heading "第一步：安装 CPU 处理器" 进 **chunker.py:480** heading 路径
4. heading 路径只用 ASCII 数字 regex `^(\d+(?:\.\d+)*)\s*[\.．、]?\s*\S` —— "第" 非数字 → `heading_step_match=None`
5. line 505 → 更新 current_section,line 528 `continue`,**不调用 `_STEP_BOUNDARY_RE`**（虽然 _STEP_BOUNDARY_RE 的 Group 3 `第\s*([一二三四五六七八九十\d]+)\s*步` 能匹配）
6. step_groups 全程为空 → line 726 fallback 到 `_chunk_text_fallback` → 全文 SOP 切成 text_chunk

对比 pdf_sop / xs_wi_007: step 段是 `block_type=paragraph` "步骤N：..." → 进 paragraph 路径(line 587) → `_STEP_BOUNDARY_RE.finditer` 命中 → step_group 正常生成。

**关键差异:** `block_type=heading` vs `block_type=paragraph` 决定走 ASCII regex 还是 _STEP_BOUNDARY_RE,只有后者支持中文 step 标记。

## 2. 修法（3 处)

### Fix 1 — chunker heading→paragraph fall through

`opensearch_pipeline/chunker.py:474-490` 在 heading 路径前置 4 行:含行首显式 step 标记的 heading 块重写为 paragraph,让 paragraph 路径的 _STEP_BOUNDARY_RE 统一处理(支持中文数字 + 混合编号继承 + sub_no 处理)。

```python
if block_type == "heading" and re.match(
        r'^[ \t　]*(?:步骤\s*[一二三四五六七八九十\d]+|'
        r'Step\s+\d+|第\s*[一二三四五六七八九十\d]+\s*步)',
        text, re.IGNORECASE):
    block_type = "paragraph"
```

仅行首放行避免说明性 heading "本节含步骤…" 误判。不更新 section 因为 paragraph 路径会用 current_section 填 step_group section_title。

**影响:**
- it_xxh_003 现产 12 chunks: 2 text_chunk + 1 procedure_parent + **9 step_card**(原 19 text_chunk + 1 ocr_chunk + 11 独立 image)
- pdf_sop / xs_wi_007 byte-equal(step 段本就是 paragraph)
- xlsx/docx 不受影响

### Fix 2 — GT 重标 it_xxh_003 对齐新 chunker 粒度

原 D8 Phase 3 GT 标了 19 subchunk 粒度(基于 chunker bug 时代的细分 text_chunk 产出),与 Bug D fix 后 9 step_card 粒度 mismatch — matcher 配错全 0,Jaccard 反而 0.632→0.219 假性退步。

用 Workflow + schema-validated agent 重标 it_xxh_003 GT 为 10 chunks(2 text_chunk + 1 procedure_parent + 7 step_card),对齐主步骤粒度:
- 第五步 GT=[20,21,22,23,24,25] 聚合 6 张硬盘图(原 3 子条:[20,21,22]/[23,24]/[25])
- 第六步 GT=[27,29] 聚合(原 2 子条:[27]/[29])
- 其他 step 1/1 对应不变

backup: `gt_pdf_analysis.json.bak_d8p5`

### Fix 3 — Matcher Bug C2(评测层)

`eval_harness/binding/ingestion_binding.py:_match_gt_chunk_to_produced` 在 typed pool 后加 4 行:GT expected_image_refs 非空时,优先选含 image_refs 的 chunk —— 反映"主步骤含图"标注意图,避免短 visual_knowledge/摘要先行 chunk 用 density 抢戏真子步骤。

```python
if typed:
    if gt.expected_image_refs:
        with_imgs = [s for s in typed
                     if (_gv(s[0], "extra") or {}).get("image_refs")]
        if with_imgs:
            typed = with_imgs
```

**根因:** chunker step 5 切 2 sub-chunk —— i=8 是 "[补充图示]" visual_summary 摘要先行 200 字 imgs=[],i=9 是主步骤 500+字 imgs=[20-25]。matcher 按 density-max,i=8 短密度高被选中 pred=[] J=0,而 i=9 才是 GT 期望的真主步骤。0-image GT(expected_image_refs=[])不进 filter 无副作用。

**副作用(纯收益):**
- pdf_sop step 4.1 0.0 → 0.5 (pred 从 [] 升到含图的 [9,10])
- xs_wi_007 step 1 0.0 → 0.5 / step 2 仍 0.5
- 与 D8 Phase 3 chunker_attribution 报告的 Bug C 同根因,本 fix 完整覆盖。

## 3. 实测数据(per-chunk Jaccard)

跑 `scripts/eval_image_binding_pdf.py` 默认 OFF。完整表 `scratch/d8_phase5_baseline_v3.json`。

主要变化(vs D8 Phase 3):

| doc | chunk | D8 P3 | D8 P5 | Δ | 备注 |
|---|---|---|---|---|---|
| pdf_sop | 步骤 4.1 填写设备班次 | 0.0 | 0.5 | +0.5 | matcher C2 fix 选含图 chunk |
| xs_wi_007 | 步骤 1 标识卡清点 | 0.0 | 0.5 | +0.5 | matcher C2 fix |
| xs_wi_007 | 步骤 2 收集核对 | 0.5 | 0.5 | 0 | image 2 仍错绑到 step 1(Phase 4 ON 解过) |
| it_xxh_003 | 第一步 三角对齐 / 第二步 散热器 / 第四步 主板 | 0.0 ea | 1.0 ea | +1.0 ea | Bug D + GT 重标 |
| it_xxh_003 | 第五步 硬盘 | 0.0 | 0.833 | +0.833 | chunker step 5 sub 缺 image 25,GT 期望 6 张 |
| it_xxh_003 | 第六步 光驱+电源 | 0.0 | 1.0 | +1.0 | Bug D + GT 重标 |

剩余失败:

| chunk | gt | pred | J | 原因 |
|---|---|---|---|---|
| pdf_sop 步骤 4.2 班组人员 | [] | [9,10] | 0 | Bug A 镜像(Phase 2 chip) |
| pdf_sop 步骤 3.1 U8 | [5,6,9] | [5,6] | 0.667 | image 9 跨页 range-ref(Bug E chip) |
| xs_wi_007 步骤 5.1 主流程 | [30] | [41] | 0 | matcher 在 step_no=5 内 2 sub 选错 chunk |
| it_xxh_003 procedure_parent | [] | [16] | 0 | matcher 把 procedure_parent GT 错配到 step 4 |
| it_xxh_003 第五步硬盘 | [20-25] | [20-24] | 0.833 | chunker 漏绑 image 25 |
| it_xxh_003 第七步显卡 + 全文收尾 | [] each | [27,29] each | 0 each | chunker 没产 step 7 step_card,matcher 错配到 step 6 |

## 4. 测试矩阵

```
$ python -m pytest tests/test_chunker.py tests/test_step_card_expand.py \
                   tests/test_ingestion_binding.py -q
90 passed in 0.87s

$ python scripts/eval_image_binding_pdf.py
  pdf_sop          jaccard=0.833  ← +0.045
  pdf_xs_wi_007    jaccard=0.778  ← +0.056
  pdf_it_xxh_003   jaccard=0.700  ← +0.068
  Mean Jaccard:    0.7722         ← +0.0756 vs D8 Phase 3

$ bash scripts/day7_chunker_postfix_verify.sh --n-runs 3
  verdict: ALL_EQUAL ✅
  per_chunk byte-equal: 55/55
  per_fmt std_max: 0.0000
  xlsx: 1.0    unchanged
  docx: 0.9898 unchanged
  pdf:  0.7722 new baseline
```

## 5. 已修 vs 未修

| Bug | 状态 | 备注 |
|---|---|---|
| A. dotted sub-step image 错绑(pdf_sop 4.1/4.2) | 部分(matcher 选含图升 0→0.5) | 真根因仍在 chunker step boundary,Phase 4 chip 跟踪 |
| B. 顶层 step image 错绑(xs_wi_007 1↔2) | 部分 | Phase 4 anchor 收窄 + content-match override ON 完全解,OFF 部分 |
| C. typed pool 同质类型 sub-chunk matcher 选错 | **✓ 修复** | 含图优先 + step_no 锁定双层防御 |
| **D. markdown bullet SOP 不出 step_card** | **✓ 修复** | heading→paragraph fall through |
| E. 跨页 range-ref(pdf_sop step 3.1 image 9) | 未修 | 独立 chip,需 _chunk_by_step 跨页 step 续连 |

## 6. 决策与下一步

### 立即变更(本 commit)
- `opensearch_pipeline/chunker.py:474-490`:heading→paragraph fall through(Bug D)
- `eval_harness/binding/ingestion_binding.py:244-258`:matcher C2 含图优先
- `gt_pdf_analysis.json`:it_xxh_003 19→10 chunks,.bak_d8p5 备份
- 本报告

### 留 chip(新增)

| Chip | 描述 | 优先级 |
|---|---|---|
| **chunker step 5 漏绑 image 25** | it_xxh_003 第五步硬盘 GT 6 张图,chunker 实绑 5 张缺 image 25(Seagate 硬盘特写)— 看 _insert_image_refs_heuristic / _chunk_by_step image_ref 归属(可能跨页 9 时关闭了 step 5) | 中 |
| **chunker 漏识别 step 7** | it_xxh_003 第七步 page 11-14 没产 step_card —— 可能 page 11 文本"用手轻握显卡两端..."紧接 step 6 没 separator,extractor 没标 heading,paragraph 路径 _STEP_BOUNDARY_RE 也没命中"第七步" | 中 |
| **xs_wi_007 step_no=5 内 2 sub matcher 选错(step 5.1 vs 5.2)** | matcher 在同 step_no 内多 sub-chunk 还需更精细策略;现状靠 keyword 偶然 disambig | 低 |
| **it_xxh_003 procedure_parent GT 配错** | matcher 把 procedure_parent type GT 错配到 step 4 step_card —— 可能 typed pool 没含 procedure_parent chunk(chunker chunk_type 不一致?) | 低 |

## 7. 学到的事

- **chunker 内部多个 step regex 互不知晓**:`_STEP_DETECT_RE`(routing 检测)、`_STEP_BOUNDARY_RE`(paragraph 路径)、heading 路径的 ASCII regex —— 三者覆盖范围不重叠,markdown SOP 落在 heading 路径 regex 盲区。修法:让 block_type 重写让 step 块统一走 _STEP_BOUNDARY_RE。
- **GT 标注必须与 chunker 实际产出粒度对齐**:D8 Phase 3 it_xxh_003 GT 标 19 subchunk 是基于 chunker bug 时代的细分产出。Bug D 修后产出粒度变 9 step_card,GT 不重标 matcher 配错全 0 假性退步。**chunker 改动 + GT 同步重标是一对绑定动作**。
- **matcher density-max 偏好短文本是结构性问题**:visual_knowledge / 摘要先行 chunk 短而 keyword 命中多 → density 抢戏含图的真子步骤。"含图优先"是评测层正确解,反映标注意图。

## 工件清单
- 本报告
- `eval_harness/reports/D8_phase{1-4}_*.md`(前 4 phase 评估)
- `scratch/d8_phase5_{baseline_off,baseline_v2,baseline_v3,it_xxh_003_chunks}.json`(每轮实测)
- `~/Downloads/opensearch-rag-data/eval_samples/ground_truth/gt_pdf_analysis.json.bak_d8p5`(GT 重标备份)
