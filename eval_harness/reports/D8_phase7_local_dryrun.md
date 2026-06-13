# D8 Phase 7 — 本地 6 PDF 池 dryrun + 多轮稳定性测试 — 2026-06-13

> 用户授权"先预演,更多 doc 多轮稳定测试"。Phase 6 完成后,**生产侧重灌前**先在本地 6 doc 池跑新 chunker(D8 Phase 3-6 改动)产出 dryrun + 3 轮 byte-equal 稳定性。3/3 stability BYTE_EQUAL ✓,4/6 doc 健康,**2 doc 暴露新 routing miss bug (R1)** — 重灌前必修。

## TL;DR

| 维度 | 结果 |
|---|---|
| 6 doc dryrun 健康度 | 4/6 健康(pdf_sop / pdf_xs_wi_007 / pdf_it_xxh_003 / admin_lodging),2/6 异常(xg001 / zs006) |
| 3 doc × 3 轮 stability | **3/3 BYTE_EQUAL** ✓(确定性确认) |
| 新发现 routing miss bug | **R1 (Medium)** — title-based routing miss 让 ERP/U8 短代号 SOP 漏触发 step mode |
| 生产重灌建议 | **先修 R1 + 扩 prod RDS 10-20 doc 抽样** 再启动重灌 |

## 1. 6 doc dryrun 数据

| doc_label | title | m_mode | step_card | indep_image | chunks_w_img | total_refs | 健康 |
|---|---|---|---|---|---|---|---|
| pdf_sop | pdf_sop | step | 10 | 0 | 8 | 10 | ✓ |
| pdf_xs_wi_007 | FL-XS-WI-007 | step | 10 | 0 | 5 | 8 | ✓ |
| pdf_it_xxh_003 | it_FL-CW-XXH-003-《电脑安装》作业指导书 | step | 9 | 0 | 5 | 11 | ✓ |
| admin_lodging | admin_关于外来人员来访留宿相关规定 | clause | 0 | 1 | 0 | 0 | ✓ (符合规定文件预期) |
| **xg001** | **xg001** | **text** | **0** | **4** | **0** | **0** | ❌ |
| **zs006** | **zs006** | **text** | **0** | **2** | **0** | **0** | ❌ |

### 异常 doc 详情

**xg001**:U8 ERP 领料申请 SOP,正文含「步骤1...步骤6」共 6 处 step 边界 + 9 张 funnel-kept 图(U8 截图)。但 `title="xg001"` 短代号:
- sop_keyword_match=False(无 sop/manual/作业指导书 等)
- wi_dash_match=False(无 `wi-\d` 形式)
- step_pattern_count=6 但不足以触发 fallback

→ m_mode=text → 产出 1 table_chunk + 3 text_chunk + **9 ocr_chunk + 4 独立 image chunk**,**0 image_refs 绑定**。

**zs006**:同型问题 — 4 step patterns + 7 funnel-kept 图,title="zs006" 短代号无 SOP 关键词 → text mode → 11 chunks 全无 step_card / image_refs。

**生产影响**:这两个 doc 在生产侧也会同样误路由,16 张图(9+7)全部无 step 锚定。Phase 3-6 改动**对其无效**——routing 决策在 chunker 进入前已经发生。

## 2. 3 doc × 3 轮 stability

| doc | runs | n_chunks | sha256 一致 | verdict |
|---|---|---|---|---|
| pdf_sop | 3 | 16 | ba179ceecb007e45 ×3 | **BYTE_EQUAL** ✓ |
| pdf_it_xxh_003 | 3 | 12 | 3793a0bccd7bfa7a ×3 | **BYTE_EQUAL** ✓ |
| admin_lodging | 3 | 4 | b3cf8f8b04d42402 ×3 | **BYTE_EQUAL** ✓ |

覆盖 step + clause 两种模式,确定性确认。与 day7_chunker_postfix_verify ALL_EQUAL 结论一致。

## 3. 新发现 — R1 title-based routing miss

### 根因

`pipeline_nodes.py:_detect_step_patterns`(line 1606)要求:
```python
sop_keywords = ["sop","manual","guide","操作","手册","作业指导","作业导书","流程","规程","检验","培训"]
is_sop_like = any(kw in cat_l1 or kw in cat_l2 or kw in title for kw in sop_keywords)
if not is_sop_like and re.search(r'(?:^|[^a-z0-9])wi-\d', title):
    is_sop_like = True
if not is_sop_like:
    return False
# ... step regex 检测 step_pattern_count ≥ 2 ...
```

`is_sop_like` 是**第一道 gate**,title 不命中直接返回 False,正文 step 模式不论多少都不检测。xg001/zs006 短代号 title 通过不了这一关。

### 影响范围(估)

生产 RDS 里有多少 doc 像 xg001/zs006 这样:
- 文件名是企业内部短代号(无 SOP/WI 关键词)
- 正文是真 SOP(含步骤N)

未知 — 需要扩 prod RDS 抽样统计。估算下限:Fuling 工厂 SOP 文件命名习惯(`xg001` "新工序 001"、`zs006` "注塑 006")很常见,**估计 5-15% 的 SOP doc 受影响**。

## 4. 修法建议(Phase 8 候选)

**R1 修法 — text-fallback 触发条件放宽**(`pipeline_nodes.py:_detect_step_patterns`):

```python
# 放宽 is_sop_like:title 不命中关键词时,看正文 SOP 锚词
# (作业前提/作业说明/生效日期/作业指导)+ step_pattern_count 联合触发
if not is_sop_like:
    text = doc.get("text", "") or ""
    sop_anchor_words = ["作业前提","作业说明","生效日期","作业指导","SOP编号"]
    anchor_hits = sum(1 for w in sop_anchor_words if w in text[:5000])
    step_count = len(_STEP_DETECT_RE.findall(text[:10000]))
    # 任一条件触发: 锚词 ≥2 个 OR step 模式 ≥3 + 图片 ≥5
    if anchor_hits >= 2 or (step_count >= 3 and n_images_kept >= 5):
        is_sop_like = True
```

**收益**:xg001 含 "作业说明" + "生效日期" 2 锚词 + 6 step 模式 + 9 图 → 双重命中升 step mode → 产 step_card + image 绑定。
**风险**:可能误升非 SOP 含步骤词的 doc(如"操作流程说明" 类商务文档),需 dryrun 复测。

## 5. 生产重灌建议(分层)

| 层级 | doc 特征 | 建议 |
|---|---|---|
| **A 立即可灌** | title 含 `作业指导书`/`WI-`/`FL-XS-WI-`/`SOP` 关键词 | Phase 3-6 修复直接生效;预期 image↔step 绑定显著提升;可启动 DataWorks DAG 1-3 重灌 |
| **B 待 R1 修后灌** | 短代号 title(`xg001`/`zs006`/`ms*`)+ 正文是 SOP | 当前重灌无效(routing 落 text);**先 Phase 8 修 R1 再灌** |
| **C 不动** | 规定/通知/手册 clause 模式 doc | byte-equal 保证不动安全;无需操作 |

### 重灌前置 checklist

- [ ] **Phase 8**: 修 R1 routing miss + 36 chunker tests 全绿 + 6 doc dryrun 复测健康度从 4/6 → ≥6/6
- [ ] **prod RDS 抽样**: 列 100-200 doc 抽样,统计 A/B/C 层比例,确认 B 类比例(若 >10% 优先修 R1)
- [ ] **dry-run prod 子集**: 用 PROD-RO 模式下载抽样 doc → 跑新 chunker → dump chunks 看与生产 RDS 现有 chunks diff(估算重灌影响 chunk 数)
- [ ] **version_no bump 策略**: 决定整批 bump 还是分批(分 dept / 分文件类型)
- [ ] **PROD-RW 授权**: 同日 `PROD-RW:<date>` token 准备(用户逐次)
- [ ] **DAG 监控**: DAG 1 (extraction) → 2 (chunk) → 3 (push HA3 + deactivate) 全程跟踪,确保 `node_deactivate_old_chunks` 在新 chunks 全部 indexed 后才执行(never disappear from index 不变量)

## 6. 决策与下一步

### 立即变更(本 commit)
- 本报告
- `scratch/d8_phase7_workflow_output.json`(workflow raw output 备份)

### 推荐路径

| 选项 | 描述 | 风险/成本 |
|---|---|---|
| **(a) 修 R1 + 扩 prod 抽样**(推荐) | Phase 8 修 _detect_step_patterns + 用 dataworks/RDS MCP 抽 10-20 prod doc 跑 dryrun | 中风险,中成本(数小时) |
| (b) A 类立刻重灌,B 类延后 | 跳过 R1,先灌 title 命中关键词的 SOP | 漏 5-15% doc,但已修部分立即上生产 |
| (c) 等 prod RDS 抽样优先 | 不动代码,先量化 B 类占比再决策 | 最保守,但需 prod 访问 |

## 工件清单
- 本报告
- `scratch/d8_phase7_workflow_output.json`(workflow raw output)
- `eval_harness/reports/D8_phase{1-6}_*.md`(前 6 phase)
