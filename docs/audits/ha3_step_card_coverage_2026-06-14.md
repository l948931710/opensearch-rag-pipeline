# HA3 step_card 覆盖率审计 — 2026-06-14

- **生成时间**: 2026-06-14 18:09:39
- **形态**: 只读 — `RAG_ENV=prod_ro`, `SET SESSION TRANSACTION READ ONLY`
- **RDS**: `rm-bp15j7wekd5738f093o.rwlb.rds.aliyuncs.com` / db=`fuling_knowledge`
- **HA3 table**: `fuling_kb_chunks`
- **脚本**: `scripts/audit_step_card_coverage.py`

---

## 🚨 顶层发现（先看这个，再看 D1-D7）

设计的 7 维覆盖率审计**无法按计划完成**，原因是生产 RDS `chunk_meta` 表为空。
取证如下：

### 1) chunk_meta 现状（authoritative，逐条直接 SQL 确认）

| 指标 | 值 | 来源 |
|---|---:|---|
| `SELECT COUNT(*) FROM fuling_knowledge.chunk_meta` | **0** | 直接 COUNT |
| `AUTO_INCREMENT` | **17,059** | `information_schema.TABLES` |
| `CREATE_TIME` | 2026-06-06 00:38:47 | 同上 |
| `UPDATE_TIME` | 2026-06-13 00:39:28 | 同上 |
| `__recycle_bin__.*` 有无 chunk_meta 残留 | 无 | RDS recycle bin 列表 |
| `fuling_knowledge_stg.chunk_meta` 行数 | 0 | 备用库也空 |

**解读**：`chunk_meta` 表 6/6 创建之后至少积累过 ~17,059 行，6/13 00:39:28 之后被清空（或最后一次写入即清零）。不在 RDS 回收站。

### 2) HA3 现状（reference）

| 指标 | 值 | 来源 |
|---|---:|---|
| `chunk_type="step_card"` 命中数（top_k=10000，filter） | **2,112** | retriever._get_ha3_client + QueryRequest |
| 命中是否截断 | 否（< top_k） | 单次返回 < 10000 |
| 历史累计推送 chunks（`opensearch_bulk_job` COMPLETED） | **40,648**（684 job） | RDS bulk_job 汇总 |
| 待推送（PENDING） | 4,000（65 job） | 同上 |

### 3) document_version 状态字段全坏

`document_version` 1,364 行，全部 `chunk_status='NOT_STARTED'` AND `index_status='NOT_INDEXED'`，即使 6/12 还在跑 bulk_job。
说明这两列在当前流水线里**事实上不被写**（或写了又被某个迁移重置）。
`chunk_count` 偶有真值（例：DOC_ADMIN_20260513120213_342731 = 6）— 那一列还在被写。

### 4) 生产对外服务仍能输出 step 结构与图片

`fuling_operation.qa_session_log` 近 7 天 389 条 answer：
- `answer_text LIKE '%<<IMG:%'`: **171 / 389 = 44.0%**
- `content_blocks_json LIKE '%"image"%'`: **135 / 389 = 34.7%**
- 最近一条 (2026-06-14 18:09:29) `q="新员工办理宿舍入住需要凭什么单据？"` answer 含 `**第1步** / **第2步** / **第3步** ... <<IMG:` —— 即既有 step 结构又有图片标记。

**但是** retrieved_docs_json 里不含 `"step_card"` 字面 — 说明检索侧拿到的不是 RDS 那条契约里 expand_step_context 的输出，而是另一条路径（疑似走 HA3 直接读 fields）。

---

## 推论（待用户确认）

任一可能：

- **(A) 已计划重灌**：与 D8 Tier 0 收尾 + 105 PDF 重灌的工作流相关；chunk_meta 是被有意清空、等待重灌。但 6/12 还在跑 bulk_job 与 "等重灌" 不太吻合（重灌前清空通常意味着停推）。
- **(B) 流水线 + chunk_meta 解耦**：现网 DAG 3 不再写 RDS chunk_meta，直接 OSS → HA3；chunk_meta 在 6/13 被某次 migration 顺手清空，CLAUDE.md 描述的 "RDS-first + deactivate-after-index" 安全不变量已与现网代码脱节。
- **(C) 误操作 / 静默事故**：6/13 00:39:28 有人 `TRUNCATE chunk_meta` 但回收站没记录（可能 DELETE 而非 DROP）；HA3 没动，所以服务可用，但 expand_step_context / 图片 sidecar 路径降级为只走 HA3 字段。

---

## 七维原始结果（仅作记录 — 因 RDS 空，D2-D7 全 0）

### D1 — RDS↔HA3 active step_card drift

- RDS active step_card: **0**
  - 其中 `chunk_meta.index_status='INDEXED'`: 0
  - 其中 `document_version.index_status='SUCCESS'`: 0
- HA3 unique chunk_id: **2,112**
- 对称差: **2,112** = HA3 \ RDS
- 漂移率: rds_active=0 → 无法定义比率，绝对差 = 全部 HA3 内容均"无 RDS 对应"

样本（HA3 有但 RDS 无）：
```
DOC_ADMIN_20260509102839_76AFFC_v4_c0000_4580E53C
DOC_ADMIN_20260509102839_76AFFC_v4_c0001_AD979217
...（共 2,112）
```

### D2 — image_refs 覆盖率（按 file_ext）— VOID（RDS 空）

| ext | docs (status='active') | docs_with_step | step_cards | step_cards_with_refs |
|---|---:|---:|---:|---:|
| docx | 432 | 0 | 0 | 0 |
| pdf | 105 | 0 | 0 | 0 |
| xlsx | 30 | 0 | 0 | 0 |
| png | 3 | 0 | 0 | 0 |
| pptx | 3 | 0 | 0 | 0 |
| jpg | 2 | 0 | 0 | 0 |
| jpeg | 1 | 0 | 0 | 0 |
| **TOTAL** | **576** | **0** | **0** | **0** |

> 注：D2 里 "TOTAL=576" 与 `document_meta status='ACTIVE'` 真值 **582** 不一致 — 因 D2 query 用 `WHERE dm.status='active'` 小写，生产是大写 `ACTIVE`。MySQL utf8mb4 默认 case-insensitive，但显式 BINARY collation 时会区分。差 6 doc 不影响本次结论，下版本脚本应改 `LOWER(dm.status)='active'`。

### D3 — SOP 路由命中但 0 step_card

- 路由命中 doc: **316**
- 产 ≥1 step_card: **0**（100% 的路由命中文档现在都 0 step_card — 因 chunk_meta 空）
- 候选漏 chunk 名单: 0（因 `total_chunk_count > 0` 条件过滤，且所有 doc 都 0 chunks → 0 命中）

### D4 — 孤儿 step_card

- 孤儿数: **0** —— 表为空，无法判断

### D5 — step_no 连续性

- 总 parent 数: **0** —— 同上

### D6 — image_refs JSON shape 合规

- 带 refs 的 step_card 数: **0** —— 同上

### D7 — procedure_parent 平衡

- missing/duplicate: **0/0** —— 同上

---

## 结论 & 下一步

- 原 audit 设计假设 RDS chunk_meta 是真值；现在不是。**该 audit 无法在当前状态下产出可解读的覆盖率数字。**
- 至少三种推论（A/B/C）需要你判断 — 这关系到：
  - 是否需要紧急处置（C 路径）
  - 是否要重写 audit 改为 HA3-only 真值（B 路径）
  - 是否等 D8 + 105 PDF 重灌后再跑（A 路径）

### 此次跑出来的可信副产物

1. **生产 HA3 真值**：`chunk_type='step_card'` × 2,112；与"2,337 卡/744 带图"内存对比少 225 卡（未深查原因）
2. **bulk_job 历史推送总数**: 40,648 chunks 已 COMPLETED；4,000 chunks PENDING
3. **生产 answer 仍带 image 比率**：近 7 天 44%（171/389 含 `<<IMG:>>`）
4. **document_version 状态字段全坏**（NOT_STARTED + NOT_INDEXED）— 与代码假设脱节，独立工程债

### 建议下一步（任选）

1. **HA3-only 真值审计**：改写脚本，全部 7 维改从 HA3 拉 — 但 image_refs 是 RDS-only contract 的话，D2/D6 仍无意义。需先确认 image_refs 在不在 HA3 字段里。
2. **取证脚本**：查 `kb_audit_log` 与 `opensearch_bulk_job` 在 6/12 16:00 ~ 6/13 01:00 之间的操作，定位"清空 chunk_meta"是哪个动作（migration / 手工 / DAG bug）。
3. **保留现状静观**：与 D8 105 PDF 重灌窗口耦合 — 重灌过程会重新写 chunk_meta，届时再跑此 audit 即有意义。

---

## 附：可复现命令

```bash
RAG_ENV=prod_ro RAG_READONLY=true \
RAG_ALLOW_REMOTE_DB=read_only_ack RAG_ALLOW_REMOTE_SEARCH=read_only_ack \
  python scripts/audit_step_card_coverage.py
```

输出：
- `docs/audits/ha3_step_card_coverage_2026-06-14.md`（本文件）
- `docs/audits/ha3_step_card_coverage_2026-06-14.json`（原始数据）
