# 语料清理工单 — 近重复文档家族 + RDS 卫生（只读分析，待文档负责人执行）

**日期:** 2026-06-09 · **范围:** 396 个活跃索引文档（read-only RDS `document_meta⋈chunk_meta`）· **方法:** 归一化标题相似度 ≥0.75 聚类 + 人工分类

**动机:** multi_doc_ab 评测证实近重复家族直接挤占 top-7 上下文名额（如 QA-91 的 top-7 被《外来人员入厂告知书（新）》《（松门）》两个 doc_id 占 4 席），是跨文档覆盖的第一可修因子；文档级多样性限额因此失效（实测反而有害）。

**总量:** 39 个家族 / 80 个文档 / 709 个活跃 chunk；其中可去重家族 ≈ 32 个（每家族下线 1 份 → 释放约 32 个文档、显著减少重复 chunk 占位）。


## 1. docx+pdf 双格式重复（同一 SOP 两种格式都入了库）— 最大类，建议优先

20 对（FL-XS/XG/ZS-WI 系列作业指导书）。两格式抽取质量差异极大（如 FL-ZS-WI-008：docx 12 chunk vs pdf 23 chunk）。**建议：保留 chunk 数多的一份（抽取更完整，通常是 pdf——截图类 SOP 的图片绑定也以 pdf 路径为准），下线另一份。**

| 保留 | 下线 | 部门 | chunks(留/撤) |
|---|---|---|---|
| FL-XS-WI-001《吸塑数量本填写》作业指导书-班组长.pdf | FL-XS-WI-001《吸塑数量本填写》作业指导书-班组长.docx | production/production | 13/2 |
| FL-XS-WI-008《吸塑待检入库单》作业指导书-成品仓管.pdf | FL-XS-WI-008《吸塑待检入库单》作业指导书-成品仓管.docx | production/production | 7/2 |
| FL-XS-WI-003《吸塑到货单查询》作业指导书-班组长.pdf | FL-XS-WI-003《吸塑到货单查询》作业指导书-班组长.docx | production/production | 9/8 |
| FL-XS-WI-004《吸塑不干胶领料》作业指导书-班组长.pdf | FL-XS-WI-004《吸塑不干胶领料》作业指导书-班组长.docx | production/production | 8/2 |
| FL-XS-WI-002《吸塑领料申请单查询》作业指导书-班组长.pdf | FL-XS-WI-002《吸塑领料申请单查询》作业指导书-班组长.docx | production/production | 10/1 |
| FL-XS-WI-007《吸塑扫码报检》作业指导书-成品仓管.pdf | FL-XS-WI-007《吸塑扫码报检》作业指导书-成品仓管.docx | production/production | 9/1 |
| FL-XS-WI-005《吸塑领料申请单打印》作业指导书-班组长.pdf | FL-XS-WI-005《吸塑领料申请单打印》作业指导书-班组长.docx | production/production | 20/13 |
| FL-XS-WI-006《吸塑交货单打印》作业指导书-班组长.pdf | FL-XS-WI-006《吸塑交货单打印》作业指导书-班组长.docx | production/production | 13/2 |
| FL-XG-WI-004《吸管-待检入库单》作业指导书-成品仓管.pdf | FL-XG-WI-004《吸管-待检入库单》作业指导书-成品仓管.docx | production/production | 6/2 |
| FL-XG-WI-002《吸管-打印交货单》作业指导书-班组长.pdf | FL-XG-WI-002《吸管-打印交货单》作业指导书-班组长.docx | production/production | 12/6 |
| FL-XS-WI-009《吸塑-产品入库打印》作业指导书-成品仓管.pdf | FL-XS-WI-009《吸塑-产品入库打印》作业指导书-成品仓管.docx | production/production | 18/18 |
| FL-XG-WI-001《吸管-辅料领料申请单》作业指导书-班组长.pdf | FL-XG-WI-001《吸管-辅料领料申请单》作业指导书-班组长.docx | production/production | 17/8 |
| FL-XG-WI-005《吸管-产品入库打印》作业指导书-成品仓管.pdf | FL-XG-WI-005《吸管-产品入库打印》作业指导书-成品仓管.docx | production/production | 18/13 |
| FL-XG-WI-003《吸管-扫码报检》作业指导书-成品仓管.pdf | FL-XG-WI-003《吸管-扫码报检》作业指导书-成品仓管.docx | production/production | 8/6 |
| FL-XG-WI-008《吸管-纸吸管耐热测试》作业指导书-检验员.docx | FL-XG-WI-008《吸管-纸吸管耐热测试》作业指导书-检验员.pdf | production/production | 14/14 |
| FL-ZS-WI-009《注塑发货拖柜》作业指导书-成品仓管.pdf | FL-ZS-WI-009《注塑发货拖柜》作业指导书-成品仓管.docx | production/production | 18/5 |
| FL-ZS-WI-006《注塑核对待检入库单》作业指导书-成品仓管.pdf | FL-ZS-WI-006《注塑核对待检入库单》作业指导书-成品仓管.docx | production/production | 10/6 |
| FL-ZS-WI-007《注塑返工货待检处理》作业指导书-成品仓管.pdf | FL-ZS-WI-007《注塑返工货待检处理》作业指导书-成品仓管.docx | production/production | 13/3 |
| FL-ZS-WI-008《注塑产品入库打印》作业指导书-成品仓管.pdf | FL-ZS-WI-008《注塑产品入库打印》作业指导书-成品仓管.docx | production/production | 23/12 |
| FL-ZS-WI-005《注塑收货报检》作业指导书-成品仓管.pdf | FL-ZS-WI-005《注塑收货报检》作业指导书-成品仓管.docx | production/production | 14/8 |


## 2. 跨部门同文重复（admin 与 hr 各注册一份同名文档）

9 对（A 系列合规制度为主，含 A##前缀变体：宿舍管理制度/A28、车辆进出/A41、A09/A9）。同内容双 doc_id 重复索引。**建议：每对保留一份（归属建议 hr——A 系列合规文档的主责部门），下线另一份；两部门都需访问的，等权限/ACL 系统上线后用权限解决，不要靠重复注册。**

| 保留 | 下线 | 部门 | chunks(留/撤) |
|---|---|---|---|
| A29环境应急预案.docx | A29环境应急预案.docx | hr/admin | 20/20 |
| A23保安管理程序.docx | A23保安管理程序.docx | hr/admin | 7/7 |
| A24员工保安意识程序.docx | A24员工保安意识程序.docx | hr/admin | 3/3 |
| A51保安员工作制度.docx | A51保安员工作制度.docx | hr/admin | 2/2 |
| A21工厂进出厂、限制区域管理规定.docx | A21工厂进出厂、限制区域管理规定.docx | hr/admin | 4/4 |
| A1员工行为管理标准.docx | A1员工行为管理标准.docx | hr/admin | 13/13 |
| A52吸烟管理制度.docx | A52吸烟管理制度.docx | hr/admin | 6/6 |
| A28宿舍管理制度.docx | 宿舍管理制度.docx | hr/admin | 6/6 |
| A41车辆进出管理规定.docx | 车辆进出管理规定.docx | hr/admin | 2/2 |


## 3. 版本变体（旧版未随新版下线）

3 组。**建议：保留 已受控/最终版/更高版本，下线旧版。**

| 保留 | 下线 | 部门 | chunks(留/撤) |
|---|---|---|---|
| FL-QC-009-015中速机安全操作规程(已受控).docx | FL-QC-009-015中速机安全操作规程(5).docx | production/production | 9/5 |
| 关于外来人员来访留宿相关规定(最终版）.docx | 关于外来人员来访留宿相关规定.docx | admin/admin | 5/5 |
| FL-QC-009-009淋膜机操作规程(1).docx | FL-QC-009-009淋膜机操作规程.docx | production/production | 2/2 |


## 4. 场区变体（业务上确为不同场区/门岗）— 待业务定夺

- 保安巡查记录表（北门）.xlsx vs 保安巡查记录表.xlsx
- 外来人员入厂告知书 （新）.docx vs 外来人员入厂告知书 （松门）.docx

检索侧两份都会召回并占位。**选项 A（推荐）:** 合并为一份带场区小节的文档重新入库；**选项 B:** 维持现状，接受占位（已证实文档级限额方案有害，不再考虑）。


## 5. 假阳性（仅标题相似，实为不同实体）— 保持不动

- 保留全部: 关于饭卡充值的通知（有图）.pdf / 通知（进出厂规定）.pdf / 关于2021年秋季上下班时间调整通知.pdf
- 保留全部: 002《新员工住宿安排》作业指导书.pdf / 002《新员工住宿安排》作业指导书.pdf
- 保留全部: A09安全隐患报告和举报奖励制度.docx / A9安全隐患报告和举报奖励制度.docx
- 保留全部: 车床操作流程.png / 磨床操作流程.png
- 保留全部: 纸杯设备清扫基准书-外贴机(2).xlsx / 纸杯设备清扫基准书-内贴机(2).xlsx


## 6. 既有 RDS 卫生项（沿用 coverage_gap_findings.md 的结论）

- **249 行已被取代的旧格式注册**（05-12 批，无活跃 chunk，转换孪生已索引）：标记 `status=superseded` 或迁出 `document_meta`，**严禁重新跑管道**（会产生真重复）。
- **2 行 `~$` Office 临时文件**：直接删除。
- **~7 份确缺文档**（动火/油库/路费补贴/注塑修模/FL-QC-015-035/036/037）：拿到源文件后走 DAG1→3 正常入库。

## 执行注意（给执行人）

1. 下线 = 按 doc_id 走与 `node_deactivate_old_chunks` 相同的 `chunk_meta.is_active=0` + HA3 删除流程；**先确认保留版可检索（自查询）再撤旧**——遵守"先索引后下线"的安全不变量。
2. 全部动作按 doc_id 操作（本工单已列），不要按标题模糊匹配。
3. 完成后跑 `eval_harness` L0/L1 验证 docCount 与 recall 无回归；QA-91/QA-112/RAG-07 三个 case 应直接受益。

## 7. 双向独有内容孪生组 — 2026-06-12 诊断细化后的处置（原"4 组全走文控"修正）

41 组同名异扩展孪生已退役 37 组（三批 324 chunks，双侧验证全过）。剩 4 组经
源文件级回查（独有内容拿到对方源文件 XML/全文中搜索，scratch/twin_image_audit /
twin_content_coverage 系列），裂成三种情况：

1. **FL-XS-WI-005《吸塑领料申请单打印》= docx 抽取器缺口（已修复）**：
   72 个 Word 文本框装着关键步骤文字，python-docx 不读 `w:txbxContent` 整段丢失
   → "pdf 独有"是假象。抽取器已补（docx_extractor `_textbox_texts`，
   25 条独有覆盖 23 条）。**待办：部署新 zip → docx 侧单文档回灌 → 翻转 keep→docx
   → 退役 pdf。**
2. **FL-PMC-WI-010 / FL-PMC-WI-013 = pdf 是更全的修订版**（无文本框无嵌入对象，
   填写说明/操作步骤真不在 docx 源里）；docx 独有仅 1-2 行表单示例数据。
   **待办：退役 docx（keep=pdf 维持现状），损失可忽略。**
3. **FL-ZS-WI-009《注塑发货拖柜》= 真双向版本漂移**（pdf 有装箱单证表格、
   docx 有监装毛重±5% 责任条款，互不包含且均不在对方源文件中）。
   **唯一真正需要文控的：请文档责任人出合并完整版，入库后退役两旧身份。**
   在此之前双身份并行服务（serving 端有标题折叠，答案层无重复来源）。

## §8 孪生退役补记（2026-06-24）
- scan: active docs 1621，剩 4 活跃孪生组（58 组已往轮解决）。
- 退役：FL-ZS-WI-009《注塑发货拖柜》作业指导书-成品仓管 **docx `DOC_PRODUCTION_20260513120638_3282C7`**（keep pdf `…326895`）。同部门 public 转换对，docx 仅多一张微信传输噪声图，pdf 覆盖全部实质图。RDS 14 停用 + HA3 14 删；双侧验证 RDS/HA3 残留=0、keep 在线。令牌 PROD-RW:2026-06-24。
- **待办（跨部门 ACL 决策）**：杯盖黑/粗吸管/试饮杯 产品规格书（marketing↔production，dept_internal）。ACL 铁律 = **keep production**（marketing 能读 production，反之不能；keep marketing 会切断 production 访问）。脚本 chunk-count 自动选对杯盖黑/试饮杯选错（选了 marketing）。试饮杯两份文本差 390 字需先核漂移。journal: scratch/twin_retire_journal_twin_retire_manifest_20260624.json

### §8 续：2026-06-24 本轮 4 组孪生全清（收官）
- 共退役 4 份 / RDS 停用 70 chunk + HA3 删 70，全部双侧验证（退役侧 RDS active=0 ∧ HA3=0；keep 侧 active 在线 ∧ HA3 在线）。
- 跨部门产品规格书（杯盖黑/粗吸管/试饮杯）：**一律 keep production**（ACL 铁律：marketing 经展开可读 production，反之不能；若 keep marketing 会切断 production 访问）。
- ⚠️ 覆盖核查假象记录：杯盖黑 初判 production 仅覆盖 marketing 87%、13 句"独有"全是实质规格字段——**实为格式差异假象**（marketing 纯文本句 vs production 表格+图片描述）。字段级复核 production 含 10/10 关键字段 → 安全。教训：exact-substring + 剥 [图片描述] 会把"表格 vs 句子"误判为内容缺失，须字段级复核兜底。
- 令牌 PROD-RW:2026-06-24；journal: scratch/twin_retire_journal_twin_retire_manifest_20260624.json
