# 本体层 Phase 0 组织 gate ①③④ 签字包（DRAFT）

日期：2026-07-12 · 发起：Sam · 关联：`docs/ontology_p0_plan_2026-07-10.md` §3

**为什么要签**：本体层代码侧（PR0–PR14）已全部落地并锁死在三道闸后——真实播种/回填、
auto 别名激活、agent 工具面接线（`RAG_ONTOLOGY_TOOLS_ENABLE`）都处于默认关。这些闸的
解锁条件不是技术项而是组织事实：源库可不可 diff、判据谁说了算、错了多久必须改回来。
**签字前系统保持候选-only + 工作台人审形态，不产生任何自动身份事实。**
（gate ②「steward 编制与日处理量」另行推进，不在本包。）

---

## Gate ①：U8 T-1 附属库可 diff 性 —— 信息部

**要回答的问题**：U8 每日 T-1 附属只读库能否支撑「增量回填」——代码侧 `U8SnapshotSource`
是契约桩（`opensearch_pipeline/ontology/seeding.py`），列映射等本项闭合后填。

核对清单（信息部逐项确认）：

| # | 事项 | 确认内容 | 结论 |
|---|---|---|---|
| 1 | 表清单 | 存货档案/客户档案/供应商档案/BOM 对应的物理表名与只读账号 | ☐ |
| 2 | 更新语义 | 附属库是全量快照还是增量？有无可靠的行级变更时间戳（modify_time 类字段）？ | ☐ |
| 3 | 删除可见性 | 源头删档/停用在附属库如何体现（软删标志 / 行消失）？ | ☐ |
| 4 | 同步窗口 | T-1 快照的完成时间点（本体回填节点建议 04:10 调度，须晚于它） | ☐ |
| 5 | 编码字段 | 存货编码/客户编码的权威列名、是否有历史改码（同物换码）的痕迹表 | ☐ |
| 6 | 访问路径 | 只读 DSN 的申请与网络可达（经 prod_access 只读会话，不发凭据明文） | ☐ |

**签字**：信息部 ________ 日期 ________ · 结论：可 diff ☐ / 仅全量 ☐ / 不可用 ☐

---

## Gate ③：Ground-Truth 标注对（PMC 三品类，50–100 对）—— PMC

**用途**：喂 `scripts/ontology_backtest.py`——分层 precision/recall + **false-merge 率
单列硬门（零容忍）**；backtest 通过后由 Sam 按工具打印的 manifest 骨架签发
`RAG_ONTOLOGY_AUTO_ACK`（auto 别名激活的唯一合法解锁路径）。

**标注模板**：`docs/ontology_gt_template.csv`（三列，UTF-8）：

| 列 | 含义 | 填写要点 |
|---|---|---|
| namespace | 编号来源 | `u8` / `customer:<客户名>` / `supplier:<供应商名>` |
| raw_code | 原始编号/名称 | **原样抄录**（大小写/后缀/空格都不要"顺手规整"） |
| expected_ref | 期望对象展示号 | 播种后的 FLP-S-…/FLP-P-…；**留空=该编号不应有对应对象** |

抽样指引（总量 50–100，PMC 三品类各占约 1/3）：
- **必含难例**：改模后缀对（`-M/-N/-W`，期望=不同对象或人审）≥5 对；同名不同物
  （如"黑色注塑叉子/勺子"式近名）≥5 对；客户货号↔U8 货号跨命名空间 ≥10 对；
- **必含负例**（expected_ref 留空）≥10 条：作废编号、手误编号、非本域编号；
- 其余按日常高频询码自然抽样（U8 导出 + 客户往来单据），不要只挑"干净的"。

**交付验收**：CSV 交回后由 Sam 跑
`python scripts/ontology_backtest.py <gt.csv> --out report.json`——工具输出分层指标、
硬门判定（exit 2=破门）与 AUTO_ACK manifest 骨架（含 GT 文件 sha256——token 与
数据集一一绑定，换文件即失效）。

**签字**：PMC 标注负责人 ________ 日期 ________ · 对数 ____ · 三品类覆盖 ☐

---

## Gate ④：边界判据 + 纠错 SLA —— 业务侧（PMC/销售/文控会签）

**判据草案（待改待签，签后即系统裁决口径）**：

1. **Product vs Revision**：模具改模（`-M/-N/-W` 后缀类）默认判**同 Product 新
   Revision**，由人工确认——系统永不自动合并改模对（代码已锁：剥后缀候选设计置信
   0.85，恒进人审区间）。
2. **Product vs SKU**：同产品不同**包装规格/只数/客户定制印刷** = 不同 SKU；
   箱规/香规（PackingSpec/StackingSpec）**只挂 SKU**，不挂 Product。
3. **PackingSpec verified 口径**：仅**车间实测打样**后的箱规可标 verified；
   来自历史 Excel/口头/估算的一律 draft（系统对 draft 恒加"量产前须打样确认"注记，
   计算工具引用 draft 时同样带注记）。verified 的登记人=PMC steward，经工作台。
4. **纠错 SLA**：发现误配（编号指错对象）后，steward 须在 **__ 个工作日**内经工作台
   处置（repoint/retire/mark_duplicate）；处置前该编号的消解结果对下游标"治理中"。
   （SLA 数字由业务侧填，建议 2 个工作日。）

**签字**：PMC ________ · 销售 ________ · 文控 ________ · 日期 ________

---

## 签字后的放行顺序（全部就绪才翻 flag）

1. gate ③ GT 交付 → `ontology_backtest.py` 硬门绿 → Sam 签发当日
   `RAG_ONTOLOGY_AUTO_ACK`（manifest 绑定 GT sha256 + 环境）；
2. 真实播种/回填按 gate ① 的 diff 结论接 `U8SnapshotSource`（另行 PR）；
3. `RAG_ONTOLOGY_TOOLS_ENABLE` 开启三前置：本包签字 + L7 flag-on 臂重冻
   （流程=eval_harness/agent/agent_cases_ontology.json `_comment`；状态见
   `docs/audits/` 当日报告）+ `RAG_PROMPT_INJECTION_GUARD` 生产开启；
4. 开 flag 属部署动作：SAE 重打包 + env 注入，逐项 user-gated。
