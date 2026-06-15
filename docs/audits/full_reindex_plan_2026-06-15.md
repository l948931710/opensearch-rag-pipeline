# 富岭 HA3 全量重索引 — 详细执行策略（review 稿）

> 状态：**待 review，未执行**。这是 prod 高风险操作，所有写步骤需用户逐次授权。
> 生成：2026-06-15。综合本会话实测 + rebuild skill + 用户提供的阿里云文档调和。

## 0. 一句话策略 — 用户已拍板（2026-06-15）

**push-then-purge + laptop orchestrator + 先 10-20 doc 小批走完 A-F 验证序再全量。dense 确认一直正常（G21 误判已澄清，真因是 G29 order=DESC 已修）。**

push-then-purge：先把 579 个 active doc 全量重 chunk + embed + 推到现有 HA3 表（新 chunk 与旧的并存，旧的当安全网），确认新数据落地后，再删预先枚举的旧 4,896 个 chunk。**近零停服 + 旧数据可回滚**。

**绝不**用阿里云控制台的"全量索引重建/全量切换"——在我们这张 Swift 喂数据、无离线源的表上，那是 G20 停服陷阱（重放过期 Swift log → 空索引 → 搜索全挂）。我们走 `push_documents` realtime 重推。

---

## 1. 当前精确状态（本会话实测，非估算）

| 项 | 值 | 来源 |
|---|---|---|
| RDS `chunk_meta` | **0 行**（6/13 fixture 清空） | 直接 COUNT |
| HA3 当前 active chunk | **4,896**（step_card 2112 / procedure_parent 155 / text_chunk 2396 / image 233） | 零向量+filter 实测 |
| HA3 doc 主键 = `chunk_meta.id`(int) | id 范围 **8923-13669**，全 < 新 AUTO_INC 17059 → 全是 6/13 前的"孤儿" | 实测 |
| `document_meta` ACTIVE | **579 doc**（inactive 288） | 实测 |
| `document_version` active+canonical | 1053 版本行（含多版本；重索引按 current_version 取 579 doc） | 实测 |
| chunker 漂移率 | **36%**（N=25：16 精确 / 9 漂移；双向：切更细 or 更粗） | 实测 A/B |
| bulk_job 历史 | 684 COMPLETED(40,648 chunks) / 65 PENDING(4,000) | 实测 |
| staging dry-run | stage 1/2/3 机制全验证（10 doc，RDS↔HA3 139=139） | 已跑 |

**关键纠正**：purge 目标是 **4,896**（当前 active），不是 40k（那是历史累计推送量）。删除是分钟级，不是 10-15 分钟。

---

## 2. 阿里云文档逐条调和（用户提供的 doc vs 我们实际）

| 阿里文档说 | 我们的实际 | 判定 |
|---|---|---|
| 触发"全量索引重建"（控制台/API） | 我们表 dataSource=swift + 无离线源 → 全量重建会重放过期 log → **空索引停服**（G20 实战发生过） | ❌ **陷阱，禁用** |
| "全量切换"上线新索引版本 | Swift 表无独立离线版本可切；新表构建后端坏（v2/v3/v4 卡 building，G22-27） | ❌ **不可用** |
| 推 `doc_id`/`split_content`/`split_content_embedding` 三字段 | 我们 `to_ha3_doc` 推的是 chunk_id/chunk_text/dense_vector/sparse_* 等一套自有 schema（字段名不同但等价） | ⚠️ **改造**：用我们现有 to_ha3_doc，不照搬文档字段名 |
| 外部完成切片+向量化再推 | 正是我们 stage 2(切片) + stage 3(embed+push) 做的 | ✅ 适用 |
| 向量维度一致(1024) | 我们 dimension=1024，distance=InnerProduct，查询 order=DESC | ✅ 适用（注意 DESC，G29）|
| 单批不宜过大、分批推 | stage 3 已 LIMIT 100 doc/1000 chunk 分批 | ✅ 适用 |
| 规格计算器评估扩容 | 4,896 → 重 chunk 后总量预计相近（579 doc × 平均 ~17 chunk ≈ 似），无需扩容 | ✅ 留意但大概率不需 |
| dense 向量检索 | **dense 正常工作**。G21（2026-06-07"dense 从没建起来"）是早期误判，真因是查询没 set `order=DESC`（G29），已修复。dense 一直好的，realtime push 照常喂 dense_vector → 重索引后 dense 仍正常 | ✅ 适用（DESC 已在 retriever 生效）|

**底线**：阿里文档描述的是通用 happy-path；我们这张表的 Swift+无离线源约束让"控制台全量重建"成陷阱。我们用 **realtime push + 删旧** 实现等效的"全量替换"。

---

## 3. 执行序：push-then-purge（推荐）

```
Step A  枚举旧 HA3 id（4,896）→ 存盘（这是删除目标 + 安全网清单）
Step B  全量 stage 2（579 doc，从现存 canonical 重 chunk + 写 chunk_meta，含 PII redact）
Step C  全量 stage 3（embed + push 新 chunk 到现表，新 rds_id；旧的仍在 → 并存）
Step D  验证新 chunk 落地（chunk_meta 数 ≈ HA3 新增数，抽 query 看召回）
Step E  删除 Step A 枚举的旧 4,896 id（push_documents cmd=delete，批 100）
Step F  修 index_status + 最终验证（RDS↔HA3 对齐、ANN 自查、答案质量）
```

### 为什么 push-then-purge 优于 purge-then-push

| | purge-then-push | **push-then-purge** ✓ |
|---|---|---|
| 停服窗口 | 删完到推完 = **数小时**（stage2 3-4h + stage3） | **无**（旧数据全程在服务）|
| 失败回滚 | 旧数据已删，push 挂了 = 长时间全挂 | 旧数据是安全网，push 挂了照常服务，不 purge 即回滚 |
| 过渡期副作用 | 搜索全空 | 重复结果（旧+新 chunking 并存），有噪声但可用 |

用户"接受短暂停服"——但 push-then-purge 连短暂都不用，只是过渡期有重复噪声（Step C→E 之间，可压到最短）。

---

## 4. 逐步细节

### Step A — 枚举旧 id（只读）
- 零向量 + top_k=10000（4,896 < 10000，一次取全；验证返回数=4896 无截断）
- 收集每个 doc 的 HA3 `id` 字段 → 存 `scratch/ha3_old_ids_20260615.json`
- ⚠️ 若单次 top_k 截断，按 chunk_type 分查（各类型均 < 10000）

### Step B — 全量 stage 2（prod 写）
- **必须走完整 stage 2**（classify→PII→redact→publish→chunk→write），不能 chunk-only：
  `node_redact_or_quarantine` 原地脱敏 blocks，跳过它 = 未脱敏 PII 写进 chunk_meta（已验证 pipeline_nodes.py:1585）
- 输入：579 active doc 的 current version（**不 bump version** → chunk_id 的 version 段与 HA3 一致）
- 现存 canonical 复用（免 re-OCR）
- `RAG_IMAGE_CONTENT_OVERRIDE` **关**（D8 默认关；开了让少数 doc chunk 偏离，且 D8 已证 1/10 低激活）
- 执行路径：laptop orchestrator（staging 已证）或 DataWorks（in-VPC）。laptop 需 RW 账号(.env.production) + 挂 ~3-4h

### Step C — 全量 stage 3（prod 写 + 碰 HA3）
- embed（有 cache；36% 漂移 doc 的 chunk_text 变了 → 这部分 cache miss 重算）+ push 到现表
- 新 chunk 新 rds_id（≥17059）→ 与旧 4896 并存
- ⚠️ stage 3 的 `node_deactivate_old_chunks` 会查 chunk_meta 找旧版本删——但旧的 chunk_meta 已空，它删不到旧 4896（这正是为什么要 Step E 手动删）

### Step D — 验证新数据（只读）
- chunk_meta active 数 ≈ HA3 总数 - 4896（新增量）
- 抽 10 query，确认新 chunk 可召回 + 内容对

### Step E — 删旧 4896（prod 写 + 碰 HA3）
- 读 Step A 的 id 清单 → push_documents cmd=delete，批 100 → 删完 stats() 验 docCount
- 删的是旧 chunking；新 chunking 已在（Step C）→ 搜索切换到新版本

### Step F — 收尾（prod 写）
```sql
UPDATE chunk_meta SET index_status='INDEXED' WHERE is_active=1 AND index_status='NOT_INDEXED';
UPDATE document_version SET index_status='SUCCESS' WHERE status='active' AND chunk_count IS NOT NULL;
```
- 跑 `scripts/audit_step_card_coverage.py`：RDS↔HA3 step_card 对齐
- ANN 自查（G18，`diag_ann_selfquery.py`）：注意 dense 可能仍弱（G21，非本次能修）
- 真实 query 端到端（bot/api）

---

## 5. 成本 + 时间

| 项 | 估算 |
|---|---|
| Step B stage 2（579 doc classify+chunk）| ~3-4h（staging 外推：10 doc=126s）+ classify LLM ~579 次（几 ¥）|
| Step C embedding | 64% doc cache 命中（chunk_text 没变）；36% 漂移 doc 重算 embed。~17k chunk 总量，估 ¥10-30 |
| Step E 删 4896 | 批 100 × ~49 轮 ≈ 几分钟 |
| **总时间** | **半天级**（主要在 stage 2/3 跑批）|
| **停服** | **0**（push-then-purge）；过渡期重复噪声 Step C→E |

DashScope 配额（30K RPM）远够。

---

## 6. 风险 + 回滚（我的对抗式分析；独立 agent 审查因 529 待补）

| 风险 | L×I | 缓解 |
|---|---|---|
| Step C push 失败 | 中×中 | 旧 4896 还在 → 照常服务；不执行 Step E 即"回滚"。修复后重跑 C |
| Step E 删一半失败 | 低×中 | 幂等（id 清单可重跑）；残留旧 chunk = 重复噪声，补删即可 |
| 枚举漏 id（top_k 截断）| 中×中 | 验证返回数=4896；不足则按 chunk_type 分查 |
| 36% 漂移 → 新旧并存期重复 | 高×低 | 已知；Step C→E 窗口压最短；rerank 会部分去重 |
| embedding 超预算 | 低×低 | 有 cache；设 DashScope 消费告警 |
| dense/ANN（G21 旧担忧）| — | **已澄清非问题**：dense 一直正常，G21 是误判，真因 G29 order=DESC 已修。重索引后 Step F 自查复核确认即可 |
| 6/13 类 sim→prod 误触发 | 低×高 | 5 层 guard 已装（本会话 commit）+ 重索引期间暂停周期实例 |
| 控制台有人手滑点"全量重建" | 低×高 | runbook 红字警示；操作期间不碰控制台索引功能 |

**回滚核心**：push-then-purge 的安全网就是"旧 4896 直到 Step E 才删"。Step A-D 任何失败都不影响线上（旧数据在）。只有 Step E 之后才是新世界，而那时新数据已 Step D 验证过。

---

## 7. Blockers（开工前必须解决）

1. **暂停 DataWorks 周期实例 + spot_checker**（重索引窗口内防误触发/误判）
2. **确认枚举机制**（Step A top_k=10000 是否取全 4896 无截断；不行则分 chunk_type）
3. **执行路径定**（laptop RW vs DataWorks）+ 对应凭证就绪
4. **小批验证**（先 10-20 doc 走完 A-F 全流程，验证序正确，再全量）

## 8. 用户决策（已定，2026-06-15）

1. **执行序**：✅ push-then-purge（无停服 + 安全网）
2. **执行路径**：✅ laptop orchestrator（staging 已证）
3. **小批先行**：✅ 先 10-20 doc 走完 A-F 验证序，通过再全量
4. **dense**：✅ 一直正常（G21 误判，G29 DESC 已修）；本次重索引会正常带 dense，Step F 自查复核即可，无需单独工单

---

## 附：与之前方案的关系
- 否决了"建新表切换"（后端坏，G22-27）
- 否决了"纯 stage2-only 恢复 chunk_meta"（36% 漂移 → 新文本配旧 embedding，不一致）
- 否决了"控制台全量重建"（Swift 表 G20 停服陷阱）
- 采用"realtime push 新 + 删旧"（push-then-purge），等效全量替换 + 无停服 + 安全网
