# AI 参与奖励管理办法 —— 评分统计口径

**状态**：口径已由 Sam 于 **2026-07-27** 通过逐项问答拍板；**实现尚未开始**（见 §4）。
**落盘日期**：2026-07-31（此前仅存在于会话记录，无仓库留档 —— 这本身是个风险，见 §0）。
**适用文本**：《AI参与奖励管理办法》F1 试行稿（综合管理中心）。

---

## 0. 为什么必须有这份文档

办法**附则 4** 指定「数据由 AI 工程师规划、统计、汇总」= Sam 本人，即**唯一统计方**。
这些口径直接决定全员奖金归属，因此：

- 数字必须**可审计、可复现** —— 任何人 challenge 时能指到一行确定的定义；
- 口径一旦用于发奖就**不能随意变更**，改动需留修订记录；
- 2026-07-31 前这 8 条只活在一次会话里。换台机器、或助手记忆失效就没了 —— 这类拍板必须落进仓库。

背景：原办法用「人均 token」作指标。分析结论是该指标**衡量的是消耗而非价值**（复制粘贴长文本即可刷高），
应换成 **周活跃使用率 + 有效问答占比 + 建议采纳数**。以下口径即围绕这三个新指标展开。

---

## 1. 八条口径（Sam 2026-07-27 拍板）

下表每条都标注了**当前代码/表结构的落点**，2026-07-31 逐条核验过。

### ① 渠道范围 = 仅 console + 小程序

```sql
WHERE conversation_type IS NULL          -- 钉钉单聊'1' / 群聊'2' 全排除
```

- 列定义：[schema/002_feedback_system.sql:103](../schema/002_feedback_system.sql) `conversation_type VARCHAR(8) COMMENT '1=单聊 2=群聊'`
- 小程序可再拆：`session_id LIKE 'miniapp:%'`（`api.py::_prepare_session` 固定命名空间），其余 NULL 行 = console。
- **理由**：群聊无 dept/role 归属（见 `qa-log-analytics-gotchas`），无法归到人，计分会失真。

### ② 周活跃 = 当周提问 ≥ 3 次

- 周边界 = **北京自然周**。⚠️ `created_at` 存的是 SAE 容器的 **Pacific 墙钟**，需 +15h 平移到北京。
- 复用既有口径：[deploy/weekly_qa_report.py:44](../deploy/weekly_qa_report.py) 的 `_week_pacific_bounds`（`qa_rollup.py` 同款 DST 正确分桶）。
- **不要自己重算时区** —— 这是 `qa-log-analytics-gotchas` 里记过的坑。

### ③ 有效问答 = `answer_status='SUCCESS'` 且该 `message_id` 无 downvote

```sql
LEFT JOIN user_feedback f
       ON f.message_id = q.message_id AND f.feedback_type = 'downvote'
WHERE q.answer_status = 'SUCCESS' AND f.message_id IS NULL
```

- `feedback_type` 词表：`upvote / downvote`（[schema/002_feedback_system.sql:39](../schema/002_feedback_system.sql)）。

### ④ 有效反馈 = downvote **且** `handled_status='RESOLVED'`

即「用户点了踩」**并且**「后台在 console 点了已解决」才算一次有效反馈。

- `handled_status` 词表：`PENDING / AWAITING_COMMENT / RESOLVED / DISMISSED`
- 处置端点写 `handled_status / handled_by / handled_at`：[routes/kb_console.py:905](../opensearch_pipeline/routes/kb_console.py)
- **理由**：只算「点踩」会奖励乱踩；必须经人工确认这条反馈确有价值。

### ⑤ 首报去重：按 `message_id` 去重，`created_at` 最早者得分

处置动作是**按 `message_id` 批量**置 RESOLVED（一次点击把该回答下全部差评行同置），
所以计分必须按 `message_id` 去重，**只有最早那条得分**。

- **理由**：防组团踩同一个回答刷分。
- SLA 计时、同部门自证标记**暂不启用**（当前 Sam 一人处置，加了只是噪声）。

### ⑥ 周期归属 = `handled_at`（不是 `created_at`）

处置完成才计入该周期，**已发布的数字不回改**。

- **理由**：若按点踩时间归属，上周的数字会因本周的处置动作而变化 —— 发过奖的周期数字必须冻结。

### ⑦ 采纳计数 = `review_status='accepted'` **AND** `ingestion_status='searchable'`

按 `searchable_at` 落周期。

- 双生命周期定义：[schema/010_kb_contribution.sql:12-14](../schema/010_kb_contribution.sql)
  - `review_status`：`pending → accepted | rejected`（管理员决策）
  - `ingestion_status`：`none → registering → registered → searchable | failed`（物化进度）
- **理由**：采纳但入库失败**不发分** —— 与「积分挂钩入库质量校验」原则一致。只有真正能被搜到才算贡献。

### ⑧ 部门排名门槛 + 活跃率分母

- **门槛**：部门当周提问数 ≥ **部门人数 × 2** 才参与「有效问答占比」排名。
  **理由**：防「少问保比率」—— 一个部门只问 2 次且都成功，占比 100%，不应压过问 200 次的部门。
- **分母**：活跃率分母 = **钉钉通讯录在册人数**（不是提过问的人数）。
  `staffId` 与 `qa_session_log.user_id` 直接对齐。

---

## 2. 口径之间的关系（三个指标怎么算出来）

```
周活跃使用率 = 当周活跃人数(②) / 部门在册人数(⑧)              ← 分子分母都按部门
有效问答占比 = 有效问答数(③) / 当周提问数                     ← 受门槛(⑧)约束才进排名
建议采纳数   = 采纳且可搜索的贡献数(⑦)                        ← 按 searchable_at 落周期
有效反馈数   = 去重后的已解决差评数(④⑤)，按 handled_at 落周期(⑥)
```

全部限定在渠道范围①内。

---

## 3. 当前实现状态（2026-07-31 核验）

| 依赖物 | 状态 |
|---|---|
| `qa_session_log`（含 `conversation_type` / `answer_status`） | ✅ 在用 |
| `user_feedback`（`feedback_type` / `handled_status` / `handled_at`） | ✅ 在用 |
| `kb_contribution`（`review_status` / `ingestion_status` / `searchable_at`） | ✅ 在用 |
| `_week_pacific_bounds` 时区口径 | ✅ 已有（`deploy/weekly_qa_report.py`） |
| **`staff_dim` / `dept_dim` 维表** | ⚠️ **表已建**（`schema/060_node_acl.sql`，生产已 apply）**但生产 0 行** —— 等组织同步 job 灌数据 |
| **部门级周聚合 job** | ❌ **未建** |
| **`kb_contribution.contribution_type`** | ❌ **未建**（列不存在） |
| **奖励统计模块 / 看板** | ❌ **未建**（仓库零代码） |

⚠️ 与 2026-07-27 记录的差异：当时 `staff_dim` 记为「待建」，现在**表已建好且生产已 apply**，
缺的只是数据（`org_sync.sync(commit=True)` 一次即可，dry-run 实测 119 部门 / 1173 员工）。

---

## 4. 待建的三件实现物

1. **`staff_dim` 灌数据** —— 钉钉通讯录同步（`opensearch_pipeline/org_sync.py` 已写好，接 DataWorks 每日跑）。
   顺带修 **26/131 部门映射缺口**（`dept-mapping-gap-scan-2026-07-03`）。
2. **部门级周聚合 job** —— 照抄 `weekly_qa_report` / `qa_rollup` 的 **tz 口径 + metrics 只读账号**模式。
   ⚠️ 别新起一套时区逻辑。
3. **`kb_contribution.contribution_type`（`knowledge` / `scenario`）** —— 用于收编办法里的「应用场景建议」。
   目前**线上无该通道**，短期走线下台账。

---

## 5. 尚未解决的问题（不属于统计口径，属于办法文本）

F1 试行稿本身有 **13 处问题**（等级 15 分重叠、月结与季结算矛盾等），已反馈，
**等综合管理中心出二稿**。本文件只固定「怎么统计」，不固定「怎么换算成钱」——
后者以办法正式稿为准。

---

## 修订记录

| 日期 | 变更 |
|---|---|
| 2026-07-27 | Sam 逐项问答拍板 8 条口径 |
| 2026-07-31 | 落盘成本文件；逐条核验代码落点；更正 `staff_dim` 状态（已建未灌） |

> ⚠️ **口径一旦用于发奖即冻结**。后续任何修改都必须在此表登记，并说明是否影响已发布周期的数字。
