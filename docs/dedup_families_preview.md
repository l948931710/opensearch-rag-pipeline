# 近重复家族下线预览 + Serving 风险评估

生成: 2026-06-10 13:57 · 只读分析，未做任何变更 · 执行须另行批准

风险因子: usage=近30天答案引用次数(drop侧) · coverage=drop侧文本被keep侧覆盖率 ·
imgs=图片chunk数(drop/keep) · gold=金集expected_doc命中 · ACL=跨部门(权限上线后暴露) ·
rank=高使用家族的当前检索名次(keep/drop)

| # | 规则 | 保留 (dept) | 下线 (dept) | chunks 留/撤 | usage | coverage | imgs d/k | gold | ACL | 风险 | 理由 |
|---|------|------------|------------|--------------|-------|----------|----------|------|-----|------|------|
| 1 | keep-pdf | FL-XS-WI-001《吸塑数量本填写》作业指导书-班组长.pdf (production) | FL-XS-WI-001《吸塑数量本填写》作业指导书-班组长.docx (production) | 13/2 | 1 | 68% | 1/2 | — | — | 高 | 当前检索名次 keep@1 / drop@4；keep 侧仅覆盖 drop 内容 68%（下线丢内容） |
| 2 | keep-pdf | FL-XS-WI-008《吸塑待检入库单》作业指导书-成品仓管.pdf (production) | FL-XS-WI-008《吸塑待检入库单》作业指导书-成品仓管.docx (production) | 7/2 | 0 | 94% | 0/2 | — | — | 低 | 内容全覆盖、零引用、同部门 — 安全 |
| 3 | keep-pdf | FL-XS-WI-003《吸塑到货单查询》作业指导书-班组长.pdf (production) | FL-XS-WI-003《吸塑到货单查询》作业指导书-班组长.docx (production) | 9/8 | 0 | 53% | 7/2 | — | — | 高 | keep 侧仅覆盖 drop 内容 53%（下线丢内容）；drop 侧图片更多 (7>2)，下线丢图 |
| 4 | keep-pdf | FL-XS-WI-004《吸塑不干胶领料》作业指导书-班组长.pdf (production) | FL-XS-WI-004《吸塑不干胶领料》作业指导书-班组长.docx (production) | 8/2 | 0 | 70% | 1/1 | — | — | 高 | keep 侧仅覆盖 drop 内容 70%（下线丢内容） |
| 5 | keep-pdf | FL-XS-WI-002《吸塑领料申请单查询》作业指导书-班组长.pdf (production) | FL-XS-WI-002《吸塑领料申请单查询》作业指导书-班组长.doc (production) | 10/1 | 0 | 85% | 0/2 | — | — | 高 | keep 侧仅覆盖 drop 内容 85%（下线丢内容） |
| 6 | keep-pdf | FL-XS-WI-007《吸塑扫码报检》作业指导书-成品仓管.pdf (production) | FL-XS-WI-007《吸塑扫码报检》作业指导书-成品仓管.docx (production) | 9/1 | 4 | 88% | 0/4 | — | — | 高 | 当前检索名次 keep@1 / drop@9；keep 侧仅覆盖 drop 内容 88%（下线丢内容） |
| 7 | keep-pdf | FL-XS-WI-005《吸塑领料申请单打印》作业指导书-班组长.pdf (production) | FL-XS-WI-005《吸塑领料申请单打印》作业指导书-班组长.doc (production) | 20/13 | 0 | 46% | 12/8 | — | — | 高 | keep 侧仅覆盖 drop 内容 46%（下线丢内容）；drop 侧图片更多 (12>8)，下线丢图 |
| 8 | keep-pdf | FL-XS-WI-006《吸塑交货单打印》作业指导书-班组长.pdf (production) | FL-XS-WI-006《吸塑交货单打印》作业指导书-班组长.docx (production) | 13/2 | 1 | 86% | 1/2 | — | — | 高 | 当前检索名次 keep@1 / drop@2；keep 侧仅覆盖 drop 内容 86%（下线丢内容） |
| 9 | keep-pdf | FL-XG-WI-004《吸管-待检入库单》作业指导书-成品仓管.pdf (production) | FL-XG-WI-004《吸管-待检入库单》作业指导书-成品仓管.doc (production) | 6/2 | 0 | 96% | 0/0 | — | — | 低 | 内容全覆盖、零引用、同部门 — 安全 |
| 10 | keep-pdf | FL-XG-WI-002《吸管-打印交货单》作业指导书-班组长.pdf (production) | FL-XG-WI-002《吸管-打印交货单》作业指导书-班组长.docx (production) | 12/6 | 1 | 68% | 3/3 | — | — | 高 | 当前检索名次 keep@2 / drop@1；keep 侧仅覆盖 drop 内容 68%（下线丢内容） |
| 11 | keep-pdf | FL-XS-WI-009《吸塑-产品入库打印》作业指导书-成品仓管.pd (production) | FL-XS-WI-009《吸塑-产品入库打印》作业指导书-成品仓管.do (production) | 18/18 | 0 | 55% | 13/7 | — | — | 高 | keep 侧仅覆盖 drop 内容 55%（下线丢内容）；drop 侧图片更多 (13>7)，下线丢图 |
| 12 | keep-pdf | FL-XG-WI-001《吸管-辅料领料申请单》作业指导书-班组长.pd (production) | FL-XG-WI-001《吸管-辅料领料申请单》作业指导书-班组长.do (production) | 17/8 | 1 | 72% | 5/4 | — | — | 高 | 当前检索名次 keep@1 / drop@2；keep 侧仅覆盖 drop 内容 72%（下线丢内容）；drop 侧图片更多 (5>4)，下线丢图 |
| 13 | keep-pdf | FL-XG-WI-005《吸管-产品入库打印》作业指导书-成品仓管.pd (production) | FL-XG-WI-005《吸管-产品入库打印》作业指导书-成品仓管.do (production) | 18/13 | 0 | 66% | 10/7 | — | — | 高 | keep 侧仅覆盖 drop 内容 66%（下线丢内容）；drop 侧图片更多 (10>7)，下线丢图 |
| 14 | keep-pdf | FL-XG-WI-003《吸管-扫码报检》作业指导书-成品仓管.pdf (production) | FL-XG-WI-003《吸管-扫码报检》作业指导书-成品仓管.docx (production) | 8/6 | 3 | 84% | 1/1 | — | — | 高 | 当前检索名次 keep@2 / drop@1；keep 侧仅覆盖 drop 内容 84%（下线丢内容） |
| 15 | keep-pdf | FL-XG-WI-008《吸管-纸吸管耐热测试》作业指导书-检验员.do (production) | FL-XG-WI-008《吸管-纸吸管耐热测试》作业指导书-检验员.pd (production) | 14/14 | 0 | 56% | 13/13 | — | — | 高 | keep 侧仅覆盖 drop 内容 56%（下线丢内容） |
| 16 | keep-pdf | FL-ZS-WI-009《注塑发货拖柜》作业指导书-成品仓管.pdf (production) | FL-ZS-WI-009《注塑发货拖柜》作业指导书-成品仓管.docx (production) | 18/5 | 0 | 72% | 3/6 | — | — | 高 | keep 侧仅覆盖 drop 内容 72%（下线丢内容） |
| 17 | keep-pdf | FL-ZS-WI-006《注塑核对待检入库单》作业指导书-成品仓管.pd (production) | FL-ZS-WI-006《注塑核对待检入库单》作业指导书-成品仓管.do (production) | 10/6 | 0 | 50% | 3/2 | — | — | 高 | keep 侧仅覆盖 drop 内容 50%（下线丢内容）；drop 侧图片更多 (3>2)，下线丢图 |
| 18 | keep-pdf | FL-ZS-WI-007《注塑返工货待检处理》作业指导书-成品仓管.pd (production) | FL-ZS-WI-007《注塑返工货待检处理》作业指导书-成品仓管.do (production) | 13/3 | 0 | 73% | 2/3 | — | — | 高 | keep 侧仅覆盖 drop 内容 73%（下线丢内容） |
| 19 | keep-pdf | FL-ZS-WI-008《注塑产品入库打印》作业指导书-成品仓管.pdf (production) | FL-ZS-WI-008《注塑产品入库打印》作业指导书-成品仓管.doc (production) | 23/12 | 1 | 64% | 9/9 | — | — | 高 | 当前检索名次 keep@1 / drop@4；keep 侧仅覆盖 drop 内容 64%（下线丢内容） |
| 20 | keep-pdf | FL-ZS-WI-005《注塑收货报检》作业指导书-成品仓管.pdf (production) | FL-ZS-WI-005《注塑收货报检》作业指导书-成品仓管.docx (production) | 14/8 | 0 | 64% | 3/2 | — | — | 高 | keep 侧仅覆盖 drop 内容 64%（下线丢内容）；drop 侧图片更多 (3>2)，下线丢图 |
| 21 | keep-hr | A29环境应急预案.docx (hr) | A29环境应急预案.docx (admin) | 20/20 | 0 | 100% | 0/0 | — | 是 | 中 | 跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 22 | keep-hr | A23保安管理程序.docx (hr) | A23保安管理程序.docx (admin) | 7/7 | 0 | 100% | 0/0 | — | 是 | 中 | 跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 23 | keep-hr | A24员工保安意识程序.docx (hr) | A24员工保安意识程序.docx (admin) | 3/3 | 0 | 100% | 0/0 | — | 是 | 中 | 跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 24 | keep-hr | A51保安员工作制度.docx (hr) | A51保安员工作制度.docx (admin) | 2/2 | 3 | 100% | 0/0 | 是 | 是 | 中 | 当前检索名次 keep@1 / drop@2；金集 expected_doc 命中 → 执行前需改指向 keep 侧；跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 25 | keep-hr | A21工厂进出厂、限制区域管理规定.docx (hr) | A21工厂进出厂、限制区域管理规定.docx (admin) | 4/4 | 0 | 100% | 0/0 | 是 | 是 | 中 | 金集 expected_doc 命中 → 执行前需改指向 keep 侧；跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 26 | keep-hr | A1员工行为管理标准.docx (hr) | A1员工行为管理标准.docx (admin) | 13/13 | 10 | 100% | 0/0 | 是 | 是 | 中 | 当前检索名次 keep@2 / drop@1；金集 expected_doc 命中 → 执行前需改指向 keep 侧；跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 27 | keep-hr | A52吸烟管理制度.docx (hr) | A52吸烟管理制度.docx (admin) | 6/6 | 1 | 100% | 0/0 | 是 | 是 | 中 | 当前检索名次 keep@2 / drop@1；金集 expected_doc 命中 → 执行前需改指向 keep 侧；跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 28 | keep-hr | A28宿舍管理制度.docx (hr) | 宿舍管理制度.docx (admin) | 6/6 | 2 | 98% | 0/0 | 是 | 是 | 中 | 当前检索名次 keep@1 / drop@5；金集 expected_doc 命中 → 执行前需改指向 keep 侧；跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 29 | keep-hr | A41车辆进出管理规定.docx (hr) | 车辆进出管理规定.docx (admin) | 2/2 | 0 | 70% | 0/0 | — | 是 | 高 | keep 侧仅覆盖 drop 内容 70%（下线丢内容）；跨部门 admin→hr：ACL 上线后 admin 用户可能失去访问，执行前确认归属 |
| 30 | keep-newest | FL-QC-009-015中速机安全操作规程(已受控).docx (production) | FL-QC-009-015中速机安全操作规程(5).docx (production) | 9/5 | 0 | 83% | 0/0 | — | — | 高 | keep 侧仅覆盖 drop 内容 83%（下线丢内容） |
| 31 | keep-newest | 关于外来人员来访留宿相关规定(最终版）.docx (admin) | 关于外来人员来访留宿相关规定.docx (admin) | 5/5 | 1 | 82% | 0/1 | — | — | 高 | 当前检索名次 keep@1 / drop@4；keep 侧仅覆盖 drop 内容 82%（下线丢内容） |
| 32 | keep-newest | FL-QC-009-009淋膜机操作规程(1).docx (production) | FL-QC-009-009淋膜机操作规程.docx (production) | 2/2 | 0 | 58% | 0/0 | — | — | 高 | keep 侧仅覆盖 drop 内容 58%（下线丢内容） |

## 汇总：高=22 中=8 低=2 未解析=0

## 排除项（按工单/计划）
- 场区变体 2 对（保安巡查记录表 北门/总表、外来人员入厂告知书 新/松门）— 待业务定夺
- 假阳性 5 组 — 保持不动

## 执行前置条件（本预览不执行）
1. Part A 本地 E2E 报告评审通过（keep-pdf 的选择在新管线下复核 — docx 绑定质量已大幅提升）
2. 金集命中的家族先把 golden_full.json expected_doc_ids 改指 keep 侧
3. 跨部门家族确认 ACL 归属（或等权限系统用权限解决共享，不靠重复注册）
4. 执行顺序：RDS 停用（单事务）→ HA3 同步删除（幂等）；中断残留由 清理stage3 步骤0 清扫兜底。（原 HA3-first 方案废弃：中断会留下 RDS active-INDEXED 但 HA3 已删的幽灵行）
