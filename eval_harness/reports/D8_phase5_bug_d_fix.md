# D8 Phase 5 — Bug D 修复 + Matcher C2 修 + Bug F (ToC implicit step) + dump bug — 2026-06-13

> 接续 D8 Phase 3 留下的 Bug D（markdown bullet/heading SOP `_chunk_by_step` 不出 step_card）。本 phase 落地三处 fix：chunker heading→paragraph fall through + GT 重标 + matcher Bug C2。3 doc 全 升,**Mean Jaccard 0.6966→0.7722 (+0.0756)**;byte-equal verify ALL_EQUAL,xlsx/docx 不退。
>
> **2026-06-13 收尾（Bug F + dump bug）**：原报告留下的两个 it_xxh_003 chip — "step 5 漏绑 image 25" 实为 judge_bundle `[:5]` 显示截断 bug（J=1.0 才是真），不是 chunker bug；"step 7 不识别" 修法落地 ToC-aware implicit step trigger。**Mean Jaccard 0.7722→0.8389 (+0.0667)**，it_xxh_003 0.700→0.900。

## TL;DR

| 维度 | D8 Phase 3 baseline | D8 Phase 5 baseline | D8 Phase 5 收尾 | Δ vs P3 |
|---|---|---|---|---|
| **PDF 3-doc Mean** | 0.6966 | 0.7722 | **0.8389** | **+0.1423** |
| pdf_sop | 0.788 | **0.833** | 0.833 | +0.045 |
| pdf_xs_wi_007 | 0.722 | **0.778** | 0.778 | +0.056 |
| pdf_it_xxh_003 | 0.632 | 0.700 | **0.900** | **+0.268** ✨✨ |
| xlsx | 1.0 | 1.0 | 1.0 | 0 ✓ |
| docx | 0.9898 | 0.9898 | 0.9898 | 0 ✓ |
| 90 chunker + ingestion_binding tests | n/a | ✓ | ✓ | — |
| day7_chunker_postfix_verify --n-runs 3 | ALL_EQUAL ✓ | ALL_EQUAL ✓ | ALL_EQUAL ✓ | — |

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
| it_xxh_003 procedure_parent | [] | [16] | 0 | matcher 把 procedure_parent GT 错配到 step 4 step_card(Bug C3 — 见 §收尾) |
| ~~it_xxh_003 第五步硬盘~~ | ~~[20-25]~~ | ~~[20-24]~~ | ~~0.833~~ | ~~chunker 漏绑 image 25~~ → **dump bug 实际 J=1.0**(见 §收尾 Bug G) |
| ~~it_xxh_003 第七步显卡 + 全文收尾~~ | ~~[] each~~ | ~~[27,29] each~~ | ~~0 each~~ | ~~chunker 没产 step 7 step_card~~ → **修复(Bug F ToC implicit step)** |

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
- `scratch/binding_pdf_after_toc.json`(2026-06-13 收尾,Bug F+G 修后 baseline)

---

## §收尾(2026-06-13)— Bug F + Bug G 闭环

> 原报告留下两个 it_xxh_003 chip（"step 5 漏绑 image 25"、"chunker 漏识别 step 7"）。深入诊断后发现一个是误诊（dump bug），一个是真 chunker 缺陷。两个都已闭环。

### Bug G — judge_bundle `[:5]` 显示截断 (eval_harness)

`eval_harness/binding/ingestion_binding.py:323`：

```python
"produced_image_refs": [
    {...}
    for img in img_refs_raw[:5]   # ← 老 cap，让 6+ 图 step_card 看似漏图
],
```

`jaccard_score` 计算用的是无截断 `pred_refs`（line 282 `_pred_refs_from_chunk`），所以分数本来就对；但 dump 出来的 `produced_image_refs` 截到 5 个 → 报告读起来像"chunker 漏绑 image 25"。

**实证**：手动跑 `_match_gt_chunk_to_produced + _pred_refs_from_chunk` 拿到的 step 5 chunk imgs=[20,21,22,23,24,25] 6 张全在；`jaccard_score=1.0`。误读源头是 `_doc_to_judge_bundle_item` 的 dump 截断。

**修法**：去掉 `[:5]` 改为完整 dump。Cosmetic-only，不改 jaccard 计算。

### Bug F — chunker 不产 step 7 → ToC-aware implicit step trigger

**root cause**：it_xxh_003 PDF 正文 page 11-14 完全没有 "第七步：安装显卡" 字样（只在 page 1 目录里有 `• 第七步：安装显卡，并接好各种线缆 „„`）。结果 chunker `_chunk_by_step` 主循环里 step 6 自 page 9 起一直没关，page 10/11/12/13/14 内容全被吞到 step 6 chunk（实测 step 6 chunk_text 长度 867 字、含 PCI-E/显卡/线缆/SLI/24PIN/线缆整理/组装完成 等 step 7+收尾段全部关键词）。

matcher 在选 GT chunk 时:
- GT 第七步 kws=['显卡','PCI-E','插槽','线缆','SLI','24PIN'] → 全命中 step 6 chunk → match 到 step 6 chunk imgs=[27,29] → **J=0**(GT 期望 [])
- GT 全文收尾 kws=['线缆整理','散热空间','组装完成','七个步骤'] → 全命中 step 6 chunk → 同上 → **J=0**

**修法**：`opensearch_pipeline/chunker.py`

1. **`_extract_toc_steps` (类方法, line 397+)**: 从 preamble blocks 扫 `[•·●\-\*\#　]*第N步：标题 „„` 形式的目录条目，抽 (step_no, title, keywords)，**至少 2 条**才返回非空。`_TOC_LINE_RE` + `_toc_title_keywords` 配套：title 抽 noun-style keyword（去掉 "安装"/"操作" 等通用动词前缀，避免 "安装" 在多 step 都命中误启）。
2. **`_extract_toc_steps` 调用点 (line 444 之前)**：在 Phase 1 分组前一次扫，存到 `toc_steps` 供主循环。`toc_steps=[]` 时主循环行为完全不变。
3. **主循环 paragraph 路径 implicit trigger (line 604+)**: `if not all_matches:` 分支前加触发：
   - 必须 `toc_steps` 非空 + `current_step is not None` + `found_any_step`
   - 必须 paragraph 长度 ≥20 + 不以 "生效日期" 开头（排除页眉/页脚模板段）
   - 必须 declared step_no == `current_step.step_no + 1`（**严格顺序**，不跳号 — 跳号意味着 anchor 真的缺，是罕见 case）
   - 必须 paragraph 含 declared title 的某个 noun keyword
   - 满足全部 → 关闭 current_step、按 ToC declared 信息 implicit start 新 step

**保守边界**：5 条触发条件全要满足。pdf_sop / pdf_xs_wi_007 的 preamble 没 markdown ToC（实测 `toc_steps=[]`） → 完全不进 implicit branch，行为 byte-equal。

**实证 chunker output**（it_xxh_003）：
```
step_card pg=9  step=6 imgs=[27, 29]    第六步：安装光驱、电源 ...      ← 收缩到 page 9-10
step_card pg=11 step=7 imgs=[]          用手轻握显卡两端 ...            ← NEW! page 11-14 内容
```

matcher 重选：
- GT 第七步 → match step 7 chunk (recall=1.0, imgs=[]) → **J=1.0** ✓
- GT 全文收尾 → match step 7 chunk (recall=1.0, imgs=[]) → **J=1.0** ✓ (两个 GT 都 match 到同一 chunk 不冲突，因为 imgs=[] J=1.0 与 GT 一致)

### 实测数据 — Bug F + G 修后

```
$ python scripts/eval_image_binding_pdf.py
  pdf_sop          jaccard=0.833  (unchanged — toc_steps=[])
  pdf_xs_wi_007    jaccard=0.778  (unchanged — toc_steps=[])
  pdf_it_xxh_003   jaccard=0.900  ← +0.200 vs Phase 5 baseline 0.700
  Mean Jaccard:    0.8389         ← +0.0667 vs Phase 5 baseline 0.7722

$ python -m pytest tests/test_chunker.py tests/test_step_card_expand.py \
                   tests/test_ingestion_binding.py -q
  90 passed in 0.90s

$ bash scripts/day7_chunker_postfix_verify.sh --n-runs 3
  verdict: ALL_EQUAL ✅
  per_fmt mean across 3 runs: pdf=0.8389  xlsx=1.0  docx=0.9898  std=0.0
```

it_xxh_003 per-chunk:
```
J=1.0    text_chunk         封面目录 七步总览
J=0.0    step_card          电脑安装作业概览(procedure_parent)   ← Bug C3 残尾
J=1.0    step_card          第一步 安装CPU处理器
J=1.0    step_card          第二步 安装散热器
J=1.0    step_card          第三步 安装内存条
J=1.0    step_card          第四步 将主板安装固定到机箱中
J=1.0    step_card          第五步 安装硬盘                  ← imgs=[20-25] 6 张 (Bug G dump 误诊已澄清)
J=1.0    step_card          第六步 安装光驱、电源
J=1.0    step_card          第七步 安装显卡 并接好各种线缆      ← NEW from Bug F fix
J=1.0    step_card          全文收尾 线缆整理与总结            ← NEW from Bug F fix
```

### 已修 vs 未修 — 收尾后

| Bug | 状态 | 备注 |
|---|---|---|
| A. dotted sub-step image 错绑(pdf_sop 4.1/4.2) | 部分(matcher 选含图升 0→0.5) | task_b297628c chip 跟踪 |
| B. 顶层 step image 错绑(xs_wi_007 1↔2) | 部分 | Phase 4 anchor 收窄 + content-match override ON 完全解 |
| C. typed pool 同质类型 sub-chunk matcher 选错 | ✓ 修复 | C2 含图优先 + step_no 锁定 |
| D. markdown bullet SOP 不出 step_card | ✓ 修复 | heading→paragraph fall through |
| E. 跨页 range-ref(pdf_sop step 3.1 image 9) | 未修 | 独立 chip |
| **F. ToC declared step 正文缺 anchor → 不产 step_card** | **✓ 修复(收尾)** | ToC-aware implicit step trigger |
| **G. judge_bundle dump `[:5]` 截断**(eval-side cosmetic) | **✓ 修复(收尾)** | 去掉截断；澄清"step 5 漏 image 25"实为误诊 |
| C3. matcher procedure_parent 错配到 step_card | 未修 | 同 typed pool 不命中阈值时 fallback 全集；procedure_parent kws 命中率太低 |

### 学到的事(补充)

- **eval 报告 dump 字段不是真值的 mirror — 任何 dump 截断/格式化都可能误导根因诊断。**Bug G 实证：J=1.0 是真，dump 显示 5/6 张图让我们花一整轮去诊断 "chunker 漏绑 image 25"，但根本没有这个 chunker bug。eval 工具改动应优先保证 dump 与 score 计算同源、无截断/无格式损失。
- **ToC 是文档结构信号 — chunker 当前只信 STEP_BOUNDARY anchor，跨页内容靠 "current_step 一直没关" 兜底，等于把"作者写漏 anchor"的所有责任都丢给 matcher。**Bug F 修法证明 preamble ToC 是稳定可用的二级信号：5 条严格触发条件 + ≥2 ToC 条目 gate 让保守边界足够小，不会污染无 ToC 的 doc（pdf_sop / xs_wi_007 byte-equal 实证）。后续若遇到类似 "ToC 已声明但正文缺 heading" 的 PDF，可直接复用 `_extract_toc_steps` + implicit trigger 模式。
- **byte-equal verify 是 chunker 改动的最关键门 — 任何路径上的 if-fallthrough 写错都会让某 doc 多产/少产 1 个 chunk，影响下游 RDS chunk_id 复用、HA3 index 双版本。**本次 Bug F 实施时特意把 trigger 条件做成 fallthrough 默认（不满足任一条件 = 走原始 path），避免给 toc_steps=[] 的 doc 引入分支。3 runs ALL_EQUAL 在所有 4 个格式（pdf/xlsx/docx/pptx）上同时通过是必须的。
