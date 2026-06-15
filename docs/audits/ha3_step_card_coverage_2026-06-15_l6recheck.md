# HA3 step_card 覆盖率审计

- **生成时间**: 2026-06-15 12:37:43
- **形态**: 只读 — `RAG_ENV=prod_ro`, `SET SESSION TRANSACTION READ ONLY`
- **RDS**: `(see banner)` / db=`fuling_knowledge`
- **HA3 table**: `fuling_kb_chunks`


## D1 — RDS↔HA3 active step_card drift

- RDS active step_card 数: **3078**
  - 其中 `chunk_meta.index_status='INDEXED'`: **3078**
  - 其中 `document_version.index_status='SUCCESS'`: **3078**
- HA3 返回 chunk_id（filter chunk_type='step_card'）: **3078**
- HA3 unique chunk_id: **3078**
- **RDS \ HA3**（RDS 有但 HA3 缺）: **0**
- **HA3 \ RDS**（HA3 有但 RDS 已停）: **0**
- 交集: **3078** / 对称差: **0**
- 漂移率 = sym_diff / rds_active = **0.00%**

## D2 — image_refs 覆盖率（按 file_ext）

| ext | docs | docs_with_step | step_cards | step_cards_with_refs | step 覆盖率 | image 覆盖率 |
|---|---:|---:|---:|---:|---:|---:|
| docx | 429 | 133 | 2373 | 410 | 31.00% | 17.28% |
| pdf | 98 | 66 | 682 | 302 | 67.35% | 44.28% |
| xlsx | 30 | 5 | 23 | 12 | 16.67% | 52.17% |
| png | 3 | 0 | 0 | 0 | 0.00% | n/a |
| pptx | 3 | 0 | 0 | 0 | 0.00% | n/a |
| jpg | 2 | 0 | 0 | 0 | 0.00% | n/a |
| jpeg | 1 | 0 | 0 | 0 | 0.00% | n/a |
| **TOTAL** | **566** | **204** | **3078** | **724** | **36.04%** | **23.52%** |

## D3 — SOP 路由命中但 0 step_card 的候选漏 chunk

- **限制**：SQL 只复刻了 `_detect_step_patterns` 的 cat/title 关键字侧，未复刻 `_STEP_DETECT_RE >=2` 文本侧检查 → 候选名单 ≠ 定罪名单。
- 路由命中: **306** doc
- 其中产 ≥1 step_card: **201**（65.69%）
- 候选漏 chunk: **103**

| doc_id | file_ext | title | chunks |
|---|---|---|---:|
| `DOC_ADMIN_20260513120214_6469A` | docx | 《结算水电房费》作业指导书.docx | 2 |
| `DOC_ADMIN_20260513120216_0F780` | docx | 宿舍水电工的工作范围.docx | 1 |
| `DOC_IT_20260513120633_039B24` | xlsx | 外贸发票操作流程.xlsx | 7 |
| `DOC_IT_20260513120634_2947B1` | docx | 富岭U8+品质部操作手册.docx | 37 |
| `DOC_IT_20260513120634_8549A8` | docx | 富岭U8+资材部操作手册.docx | 50 |
| `DOC_IT_20260513120634_5E462A` | docx | 富岭U8+车间操作手册.docx | 57 |
| `DOC_IT_20260513120634_70759C` | docx | 调拨单简易操作手册.docx | 2 |
| `DOC_IT_20260513120634_6B0EAA` | docx | 辅料赠送入库操手册.docx | 1 |
| `DOC_PRODUCTION_20260513120634_` | xlsx | D区、C区、F区(12oz仿PET杯)作业指导书.xlsx | 17 |
| `DOC_PRODUCTION_20260513120636_` | xlsx | 设备清扫基准书-成型机（阿德沃）.xlsx | 60 |
| `DOC_PRODUCTION_20260513120637_` | docx | 合模机操作流程.docx | 5 |
| `DOC_PRODUCTION_20260513120637_` | png | 磨床操作流程.png | 1 |
| `DOC_PRODUCTION_20260513120642_` | xlsx | 纸杯设备清扫基准书-内贴机(2).xlsx | 64 |
| `DOC_HR_20260514123016_A91D6E` | docx | A01安全控制程序.docx | 7 |
| `DOC_HR_20260514123016_7DEAF4` | docx | A03安全培训管理程序.docx | 11 |
| `DOC_HR_20260514123016_846E49` | docx | A04反恐意识培训管理程序.docx | 3 |
| `DOC_HR_20260514123016_EDDC3B` | docx | A08反恐日常检查及汇报程序.docx | 1 |
| `DOC_HR_20260514123016_AC62F5` | docx | A10危险源辨识、风险评价管理程序.docx | 7 |
| `DOC_HR_20260514123017_4AB5A8` | docx | A15背景调查与核实程序.docx | 5 |
| `DOC_HR_20260514123017_A6F6F4` | docx | A16噪声管理程序.docx | 5 |
| `DOC_HR_20260514123018_3E6647` | docx | A17离职、转岗员工及终止合作客商管理程序.docx | 4 |
| `DOC_HR_20260514123018_6F270E` | docx | A19供应商（服务商）选择和评估程序.docx | 5 |
| `DOC_HR_20260514123018_DDE426` | docx | A1环境和职业健康安全运行控制程序.docx | 9 |
| `DOC_HR_20260514123018_018298` | docx | A20怀孕女工和新生妈妈岗位风险评估程序.docx | 7 |
| `DOC_HR_20260514123019_9C6165` | docx | A22处理访客程序.docx | 1 |
| `DOC_HR_20260514123019_8D8871` | docx | A23保安管理程序.docx | 7 |
| `DOC_HR_20260514123019_126688` | docx | A24温度与湿度管理程序.docx | 3 |
| `DOC_HR_20260514123020_416528` | docx | A28废水废气管理控制程序.docx | 8 |
| `DOC_HR_20260514123020_008F61` | docx | A2EHS环境健康管理控制程序.docx | 8 |
| `DOC_HR_20260514123020_EA1870` | docx | A30反偷窃程序.docx | 4 |

_…还有 73 个未列出_

## D4 — 孤儿 step_card

- 孤儿数: **0**（pass gate = 0）

## D5 — step_no 连续性

- 总 parent 数（active step_card 有 step_no 且有 parent_chunk_id）: **204**
- 有 gap 或 min_step≠1 的 parent: **59**（28.92%）
- step_no IS NULL 的 active step_card: **0**
- **解读**：子步 3.1/3.2 都映射 step_no=3 是合法的；这里用 `max(step_no) - count(distinct step_no)` 捕获真正的 gap。

| doc_id | parent_chunk_id | min~max | distinct | total | missing |
|---|---|---|---:|---:|---:|
| `DOC_FINANCE_20260611201418_0E0` | `DOC_FINANCE_202606112014` | 0~46 | 26 | 54 | 20 |
| `DOC_HR_20260514123022_0959E5` | `DOC_HR_20260514123022_09` | 0~116 | 105 | 116 | 11 |
| `DOC_HR_20260514123024_9A9DB9` | `DOC_HR_20260514123024_9A` | 1~60 | 54 | 61 | 6 |
| `DOC_IT_20260513120634_C6FD16` | `DOC_IT_20260513120634_C6` | 0~27 | 21 | 184 | 6 |
| `DOC_IT_20260513120632_52BD41` | `DOC_IT_20260513120632_52` | 6~6 | 1 | 3 | 5 |
| `DOC_FINANCE_20260611201418_8D4` | `DOC_FINANCE_202606112014` | 5~5 | 1 | 1 | 4 |
| `DOC_PRODUCTION_20260513120642_` | `DOC_PRODUCTION_202605131` | 0~28 | 24 | 28 | 4 |
| `DOC_HR_20260514123020_815CD9` | `DOC_HR_20260514123020_81` | 0~14 | 11 | 14 | 3 |
| `DOC_RD_20260611201420_858258` | `DOC_RD_20260611201420_85` | 4~5 | 2 | 2 | 3 |
| `DOC_HR_20260514123023_76760A` | `DOC_HR_20260514123023_76` | 0~8 | 6 | 8 | 2 |
| `DOC_HR_20260514123026_B63FA8` | `DOC_HR_20260514123026_B6` | 0~23 | 21 | 24 | 2 |
| `DOC_MARKETING_20260611201418_0` | `DOC_MARKETING_2026061120` | 1~8 | 6 | 16 | 2 |
| `DOC_PRODUCTION_20260513120639_` | `DOC_PRODUCTION_202605131` | 2~4 | 2 | 2 | 2 |
| `DOC_PRODUCTION_20260514123027_` | `DOC_PRODUCTION_202605141` | 2~5 | 3 | 3 | 2 |
| `DOC_ADMIN_20260513120213_18543` | `DOC_ADMIN_20260513120213` | 1~21 | 20 | 28 | 1 |
| `DOC_ADMIN_20260513120214_39A37` | `DOC_ADMIN_20260513120214` | 1~8 | 7 | 11 | 1 |
| `DOC_ADMIN_20260513120214_61F67` | `DOC_ADMIN_20260513120214` | 1~3 | 2 | 6 | 1 |
| `DOC_ADMIN_20260513120214_E32FC` | `DOC_ADMIN_20260513120214` | 2~3 | 2 | 4 | 1 |
| `DOC_FINANCE_20260611201418_752` | `DOC_FINANCE_202606112014` | 0~4 | 3 | 18 | 1 |
| `DOC_FINANCE_20260611201418_C77` | `DOC_FINANCE_202606112014` | 0~5 | 4 | 25 | 1 |

## D6 — image_refs JSON shape 合规

- 带 refs 的 step_card 总数: **724**
- 总 ref entry 数: **1706**
- JSON parse 失败的 chunk: **0**（pass gate = 0）
- 整 chunk 全部 entry 合规（oss_key + image_index + xlsx anchor）: **712** = 98.34%
- **逐字段 entry 级覆盖率**：
  - oss_key 非空：1662/1706 = 97.42%
  - image_index 为 int：1706/1706 = 100.00%
  - source_image 非空：1685/1706 = 98.77%
  - visual_summary 非空：1681/1706 = 98.53%
- **xlsx 子集**（14 entry）：
  - filename 非空：10/14 = 71.43%
  - anchor_row 非空：10/14 = 71.43%
- 逐 ext compliant chunk:
  - docx: 400/410 = 97.56%
  - pdf: 302/302 = 100.00%
  - xlsx: 10/12 = 83.33%

<details><summary>不合规样本前 10</summary>

- `DOC_IT_20260514123026_DB87F9_v2_c0000_1C17D6B6` (docx) missing: oss_key
- `DOC_IT_20260514123026_DB87F9_v2_c0001_E1547ED3` (docx) missing: oss_key
- `DOC_IT_20260514123026_DB87F9_v2_c0005_DF886BCB` (docx) missing: oss_key
- `DOC_IT_20260513120633_ACFD0F_v2_c0000_24FCB00E` (docx) missing: oss_key
- `DOC_IT_20260513120633_ACFD0F_v2_c0002_BBE1D93C` (docx) missing: oss_key
- `DOC_IT_20260513120633_ACFD0F_v2_c0004_5A60F584` (docx) missing: oss_key
- `DOC_RD_20260611201420_94D82A_v1_c0006_2348BFFC` (docx) missing: oss_key
- `DOC_RD_20260611201420_94D82A_v1_c0008_56B8F873` (docx) missing: oss_key
- `DOC_PRODUCTION_20260513120642_FDAF42_v3_c0003_8D6CF573` (docx) missing: oss_key
- `DOC_PRODUCTION_20260513120642_FDAF42_v3_c0018_66ED3989` (docx) missing: oss_key
</details>

## D7 — procedure_parent 平衡

- 有 step_card 但 0 procedure_parent 的 doc: **0**（pass gate = 0）
- procedure_parent > 1 的 doc: **0**

## 结论 & 建议

- ✅ **D1 PASS**: RDS↔HA3 drift 0.00% < 0.5%
- ✅ **D4 PASS**: 无孤儿 step_card
- ✅ **D7 PASS**: 每 doc 恰好 1 个 procedure_parent
- D5 gap 率: 28.92% （子步合并导致的非 gap 不计）
- D6 chunk 全合规率: 98.34%
