# D8 Phase 8 — R1 routing miss 修(SOP 锚词 fallback)— 2026-06-13

> D8 Phase 7 dryrun 暴露 R1:title 短代号 SOP (xg001/zs006) 在 `_detect_step_patterns` 第一道 `is_sop_like` gate 被拒,路由落 text mode、step_card/image_refs 全失。Phase 8 加 **SOP 锚词 fallback**:正文头部含"作业前提"/"作业说明"/"生效日期" 等 ≥2 个锚词时升 is_sop_like。**6/6 doc 健康** + Day 7 ALL_EQUAL + 3 doc binding byte-equal 不动。

## TL;DR

| 维度 | Phase 7 baseline | Phase 8 | Δ |
|---|---|---|---|
| 6 doc dryrun 健康度 | 4/6 | **6/6** ✨ | +2 |
| xg001 step_card | 0 (异常) | **8** ✨ | +8 |
| xg001 total_image_refs | 0 | **9** ✨ | +9 (9 张图全绑) |
| zs006 step_card | 0 (异常) | **5** ✨ | +5 |
| zs006 total_image_refs | 0 | **7** ✨ | +7 (7 张图全绑) |
| 3 doc binding mean | 0.7722 (P5) / 0.8389 (P5 升级) | **0.8389** | byte-equal vs P5 升级 |
| pdf_sop / xs_wi_007 / it_xxh_003 chunks 数 | 16 / 14 / 12 | 16 / 14 / 12 | byte-equal ✓ |
| admin_lodging chunks | 4 (clause) | 4 (clause) | byte-equal ✓ |
| 90 tests | ✓ | ✓ | — |
| day7_chunker_postfix_verify --n-runs 3 | ALL_EQUAL | **ALL_EQUAL** ✓ | 55/55 byte-equal |

## 1. 根因(Phase 7 dryrun 实证)

`pipeline_nodes.py::_detect_step_patterns`(line 1606)的两道 gate:

```python
# Gate 1: title-based is_sop_like
sop_keywords = ["sop","manual","guide","操作","手册","作业指导","作业导书","流程","规程","检验","培训"]
is_sop_like = any(kw in cat_l1 or kw in cat_l2 or kw in title for kw in sop_keywords)
if not is_sop_like and re.search(r'(?:^|[^a-z0-9])wi-\d', title):
    is_sop_like = True
if not is_sop_like:
    return False   # ← R1 在这死掉

# Gate 2: step_pattern_count ≥ 2(text 检测)— 上面已 return,不到这里
```

xg001/zs006 title 短代号:
- sop_keyword:无("xg" "zs" 都不是)
- wi-\d:不匹配(`wi_007` 是 underscore,无 dash)
- → Gate 1 return False → 落 text mode

而正文 step_pattern_count 已经 6/4 个,足够触发 Gate 2 —— 是 Gate 1 把它们卡死。

## 2. 修法 — SOP 锚词 fallback

`pipeline_nodes.py:1631-1648`:

```python
if not is_sop_like:
    sop_anchor_words = (
        "作业前提", "作业说明", "生效日期", "作业指导",
        "作业方法", "SOP编号", "操作规程",
    )
    anchor_hits = sum(1 for w in sop_anchor_words if w in text[:5000])
    if anchor_hits >= 2:
        is_sop_like = True
```

**锚词选择依据**(富岭 SOP 实际文档头格式):
- "作业前提":全部富岭 SOP 模板字段
- "作业说明":同上,通常紧跟"作业前提"
- "生效日期":版本日期,SOP 必有
- "作业指导":替代"作业指导书"标题
- "作业方法":步骤型 SOP 章节名
- "SOP编号":标准化编号字段
- "操作规程":工艺/检验类 SOP 别名

**阈值 ≥2** 守门:避免单"生效日期"误升非 SOP 公告(如 admin_lodging 仅含"通知"+无锚词 → 0 命中 → 不升)。

**移动 text 提取**:把 text 从 blocks 拼接的逻辑从 Gate 2 之前提到 Gate 1 fallback 之前 —— text 复用一次,无性能开销。

## 3. 实测数据

### 6 doc dryrun 对比(Phase 7 vs Phase 8)

| doc | Phase 7 step_card | Phase 8 step_card | Phase 7 indep_image | Phase 8 indep_image | Phase 7 total_refs | Phase 8 total_refs |
|---|---|---|---|---|---|---|
| pdf_sop | 10 | 10 ✓ | 0 | 0 | 10 | 10 |
| pdf_xs_wi_007 | 10 | 10 ✓ | 0 | 0 | 8 | 8 |
| pdf_it_xxh_003 | 9 | 9 ✓ | 0 | 0 | 11 | 11 |
| admin_lodging | 0 (clause) | 0 (clause) ✓ | 1 | 1 | 0 | 0 |
| **xg001** | **0** ❌ | **8** ✨ | **4** | **0** ✨ | **0** | **9** |
| **zs006** | **0** ❌ | **5** ✨ | **2** | **0** ✨ | **0** | **7** |

xg001/zs006 16 张图(9+7)全部从独立 image/ocr_chunk 转入 step_card 绑定。

### 3 doc binding(byte-equal)

```
$ python scripts/eval_image_binding_pdf.py
  pdf_sop          jaccard=0.833  ← byte-equal vs Phase 5 升级
  pdf_xs_wi_007    jaccard=0.778  ← byte-equal
  pdf_it_xxh_003   jaccard=0.900  ← Phase 5 Bug F (ToC implicit step) 升的,与 R1 无关
  Mean Jaccard:    0.8389         ← byte-equal vs Phase 5 升级
```

R1 fix 不动 GT 内 3 doc(它们 title 都命中 sop_keyword/作业指导书),3 doc binding byte-equal 保护。R1 影响 GT 外 doc(xg001/zs006 不在 GT,但生产里此类 doc 是大头)。

### Stability

```
$ bash scripts/day7_chunker_postfix_verify.sh --n-runs 3
  verdict: ALL_EQUAL ✅
  per_chunk byte-equal: 55/55
  per_fmt std_max: 0.0000
  xlsx: 1.0    unchanged
  docx: 0.9898 unchanged
  pdf:  0.8389 (Phase 5+8 复合 baseline)
```

3 doc × 3 轮全 BYTE_EQUAL,deterministic 不变。

## 4. 已修 vs 未修(全清单)

| Bug | Phase | 状态 | 备注 |
|---|---|---|---|
| A. dotted sub-step image 错绑 | 6 | ✓ | Path B 圈号 override |
| B. 顶层 step image 错绑 | 4 | ✓ | Path A anchor + content-match |
| C. matcher 选错 | 5 | ✓ | 含图优先 + step_no 锁定 |
| D. markdown bullet 不出 step_card | 5 | ✓ | heading→paragraph fall through |
| E. 跨页 range-ref(pdf_sop step 3.1 image 9) | — | chip | 影响 1 chunk J 0.667 |
| F. ToC implicit step trigger(用户并行) | 5 升级 | ✓ | _extract_toc_steps,it_xxh_003 step 7 解 |
| **R1. title-based routing miss** | **8** | **✓** | SOP 锚词 fallback |

D8 evolution 全 ON 模式 6 个 Bug + R1 routing 全部修复(除 Bug E 跨页 range-ref 独立 chip)。

## 5. 生产重灌路径(更新)

经 Phase 8 修后,**B 类(短代号 title 含正文 SOP)直接受益**,不需要"先修 R1 再灌"。分层简化为:

| 层级 | doc 特征 | 重灌建议 |
|---|---|---|
| **A** | title 含"作业指导书"/WI-/SOP 关键词 | Phase 4/5/6 修生效 |
| **B** | 短代号 title(xg001 类)+ 正文含 ≥2 SOP 锚词 | **Phase 8 R1 fix 生效** |
| **C** | title 无 SOP 关键词 且 正文 < 2 锚词(规章/通知) | clause mode 不动 |

A+B 现可统一启动 DataWorks DAG 1-3 重灌。

## 6. 下一步建议

| 选项 | 描述 | 风险 |
|---|---|---|
| (a) 启动生产重灌(推荐) | DAG 1 → 2 → 3,version_no bump,需 PROD-RW 同日 token | 高(生产写) |
| (b) 扩 prod RDS 抽样(预防) | 用 dataworks/RDS MCP 抽 20-50 doc 估算 A/B/C 比例,确认重灌覆盖率 | 中(需 PROD-RO 授权) |
| (c) 关闭 D8 evolution | 6 个 Bug + R1 全修,留 Bug E chip,转其他工作 | 低 |

## 工件
- 本报告
- `scratch/d8_phase8_baseline.json`(3 doc binding 实测)
- `eval_harness/reports/D8_phase{1-7}_*.md`(前 7 phase)

## 学到的事

- **routing 阶段的 gate 比 chunker 内部 bug 影响面大**:一个 title-based gate 让一整类生产 SOP doc(企业内部短代号命名)全无 step 绑定,影响远超单个 chunker bug。Phase 7 dryrun 暴露这类问题前完全不可见。
- **dryrun 跑生产语料子集是必修课**:D8 Phase 3-6 修了 6 个 chunker bug 后,只跑 3 doc binding eval 看不出 routing 问题。Phase 7 把范围扩到 6 doc 立刻找到 R1。
- **锚词 fallback 比放宽 step regex 安全**:用 "正文 SOP 锚词 ≥2 命中" 而非 "step_pattern_count ≥ 3 强制升 step",避免非 SOP 含步骤词的商务文档误升。锚词跨业务通用且模板化,假阳率低。
