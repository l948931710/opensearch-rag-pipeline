# 贡献域迁到 node 轴 — 实施方案（终版，2026-08-06）

**状态**：已过 Claude-Codex 双盲评审四轮，`VERDICT: APPROVE` / `REMAINING BLOCKERS: NONE` / `ESCALATE: NONE`。

**实施状态（2026-08-07）**：M1-M11 代码全部落地，`make test`（4450 passed，连续 5 轮）+ `make lint` + console `npm run test/typecheck/build` 全绿。三件 user-gated 的事里 **①② 已于 2026-08-07 收口，只剩发镜像**：

| 事项 | 现状 | 谁来做 |
|---|---|---|
| `schema/067` apply 生产 + staging | ✅ **2026-08-07 02:57 已 apply**（Sam 当日授权 `PROD-RW:2026-08-07`）。台账 checksum `ea3b9440`；生产 5 行 / staging 2 行存量 `category_dept_id` 全 NULL ⇒ 行为不变 | 已完成 |
| M8 存量迁移 | ✅ **2026-08-07 已执行**（Sam 亲手跑）：4 条白名单行 → `category_dept_id=34265162`（人力资源部），第 5 行 finance/rejected 未动，白名单外误伤 0 —— 事后 prod-ro 独立复核，非采信脚本自报。组码列 `category_dept='hr'` 保留作审计留痕 | 已完成 |
| 发新镜像 / 开 `RAG_NODE_ACL_GRANT` | ⏳ **唯一剩余项**。现网 flag 本就是 on（2026-08-03 起，现查：27 active 管辖根 / 2228 篇 node 文档 / 16757 条有效 kb_doc_node_grant），缺的只是带新代码的镜像。⚠️ ①② 做完对现网**零效果**——老镜像读不到 `category_dept_id` 列 | Sam |

实施期新增的三处（方案未写、按仓规补的，均为收紧方向）：
1. **067 capability 探测**（`_contrib_axis_present`，正向缓存 + 探测失败按未 apply）——未 apply 的环境全域逐字节回到组码行为，**先部署后 apply 也安全**（方案原文只承诺了「先 apply 后部署安全」）。
2. **node 贡献行的 `category_dept` 落空串哨兵**——理由同 060 给 node 文档写 `owner_dept=NULL`：留组码残值 = node 行会被旧组码管理员的作用域命中。M8 迁移的 4 行是唯一例外（保留 `hr` 留痕），其降级受众恰是它们今天的管理员，无扩权。
3. **跨轴改归属显式 400**（node 行不支持在采纳时改回组码分类）——静默保留旧轴会让审核人以为改成功了。

**评审记账**：Codex 提出 17 条，全部经 `文件:行` 核验 CONFIRMED，0 条 REFUTED；Claude 独有 5 条（均源自 prod-ro 现网核查，Codex 在 read-only 沙箱不可见）全部成立。Codex 误报 0 / Claude 误驳 0。

其中两条是真 BLOCKER——若按初版实施，node-only 管理员会从「看不见」变成「看得见、点采纳报 403」，比现状更糟：
- accept 的 `authorize_upload` 仍走组码轴（`contribution.py:935`→`kb_authz.py:373,330,212`）
- retry 整条路径遗漏（`contribution.py:1076,1103`）

演进：v1（范围假设被推翻）→ v2（吸收首轮 6 条）→ v3（次轮 5 条 + Sam 四项裁决）→ **v4 = 本文**（三轮 2 条新 BLOCKER + Sam 裁决 F + M11）。

## Sam 裁决（2026-08-06，本方案的设计前提）

| 项 | 裁决 |
|---|---|
| A 归属节点来源 | **提交人选（默认=其所在部门），审核人在采纳前可改** |
| D 默认可见范围 | **归属节点 subtree**（与摄取侧 node 默认授权一致） |
| E flag 关闭时 | **拒绝提交 node 贡献**，对齐 `kb_console.py:2658-2663` 的上传行为，不静默回退组码轴 |
| L1 直挂 depth1 | **给指定负责人显式授予该 depth1 根**（`dept_admin_node_grant`），代码侧维持 fail-closed 落兜底 |
| F 提交端归属来源 | **只回提交人自己的部门**（新增极小只读接口，不暴露整树、不含管辖字段） |

⚠️ **A 与 L1 是配套的**：默认值取提交人部门 ⇒ 直挂 depth1 的人默认落在 depth1 节点上 ⇒ 仅当该 depth1 根被显式授权才有人管，否则仍 fail-closed 落 kb_admin 兜底。代码不做任何向上/向下推导。

## 目标 / 非目标

**目标**：`dept_admin_node_grant` 授权的管理员在贡献域**看得到、审得了、采纳得成、重试得动**，通知发对人。

**非目标**：不改组码轴既有语义（`_kb_owner_scope_sql` / `_kb_can_manage` / `authorize_upload` 逐字不动）；不迁移已有 legacy 文档；不启用 `RAG_ACL_ANCESTRY`。

## 八个消费点（v1 只覆盖 4 个）

| # | 消费点 | 文件:行 | 处理 |
|---|---|---|---|
| 1 | 队列作用域 | `contribution.py:776` | M2 |
| 2 | kb_admin 孤儿兜底 | `contribution.py:774` | M2+M3 |
| 3 | accept 归属鉴权 | `contribution.py:895` | M2 |
| 4 | **accept 入库授权** | `contribution.py:935`→`kb_authz.py:373` | **M4**（Codex C1） |
| 5 | reject 鉴权 | `contribution.py:1017` | M2 |
| 6 | **retry 鉴权** | `contribution.py:1076` | M2 |
| 7 | **retry 入库授权** | `contribution.py:1103` | **M4**（Codex C2） |
| 8 | 提交通知收件人 | `admin_notify.py:193` | M6 |

## 修改范围

### M1 DDL（ADDITIVE，走 schema/ + 台账 + CI 清单）
```sql
ALTER TABLE kb_contribution ADD COLUMN category_dept_id BIGINT NULL
  COMMENT '归属组织节点（node 轴权威）。NULL=组码轴行，判定回落 category_dept';
ALTER TABLE kb_contribution ADD INDEX idx_dept_id_status (category_dept_id, review_status);
```
复合索引对齐现有 `(category_dept, review_status)`（Codex C6）。

### M2 单一判定入口（贡献域专属，**绝不改** `api.py:2889/2902`）
```python
_contrib_can_manage(kb, category_dept_id, category_dept, *, cur) -> bool
_contrib_scope_sql(kb) -> (clause, params)
_contrib_orphan_sql(cur) -> (clause, params)
```
**轴隔离**（Codex C3 + `api.py:2950-2960` 范式）：`category_dept_id IS NOT NULL` ⇒ **只**走 node；`IS NULL` ⇒ 只走组码。两支互斥，不 OR 交叉。孤儿判定同样按分支各自算。

### M3 全局覆盖集（Codex C4/C9）
```python
_all_managed_node_descendants(cur) -> Optional[Set[int]]
```
`_kb_managed_descendants` 是 per-identity，不可复用。
快照不可用（`None`）时：**仅 node 分支** fail-open 取全集（kb_admin 宁多看不丢件），**legacy 分支保持原组码孤儿规则不变**（Codex C9）。dept_admin 侧一律 fail-closed。记 WARN。

### M4 node 落库契约（Codex C7 BLOCKER，v1/v2 均缺）

现有物化 `contribution.py:450-453` 只写 legacy `owner_dept`。新增**独立 node materializer**，与 legacy 版并列、后者逐字不动：

同一事务内原子完成：
1. `document_meta`：`owner_dept=NULL`、`acl_mode='node'`、`owner_dept_id=<category_dept_id>`、`status='active'`、`current_version_no=1`
2. `document_version`：与 legacy 版同形（`cps`/`appr` 沿用现有 public 审批语义）
3. **`kb_doc_node_grant`：`(doc_id, category_dept_id, scope='subtree')`**（Sam 裁决 D）—— 缺这条则可见集为空 ⇒ 文档谁都搜不到
4. `record_acl_projection_invalidation`（投影失效登记）
5. raw_key 走 `node_storage_segment`（防 stage-1 按路径段重派归属）

失败 ⇒ **数据库侧**整笔回滚，`ingestion_status` 保持可重试态。

⚠️ **不是跨 OSS+DB 的原子事务**（Codex 三轮 NEW CONCERN）：现有物化 `contribution.py:424-438` 先写 OSS 再开 DB 写入，DB 回滚撤不回已落的对象。清理沿用既有的「同 raw_key 重试覆盖」策略——raw_key 由 `(doc_id, upload_id)` 固定，重试写同一对象，不产生孤儿。

新增并列授权函数（**不改** `authorize_upload`）：
```python
authorize_upload_node(identity, owner_dept_id, permission_level, *, descendants) -> AuthzDecision
```
判定复用 `kb_authz.can_manage_doc(identity, "node", None, owner_dept_id, descendants)`；`public` 仍 `requires_kb_admin_approval`。

### M5 active 校验进最终授权路径（Codex C8）

`can_manage_doc`（`kb_authz.py:275-281`）**不校验节点 active** —— 它只做 `owner_dept_id in descendant_ids`；active 校验是 `kb_console.py:2681` 在上传路径上单独做的。

因此 **三处都要查** `dept_dim WHERE is_active=1 AND dept_id=%s`：

1. **提交入口**（服务端，不信客户端传值）—— 否则会存下指向已停用/不存在节点的 pending 行，只能等 accept 时才失败（Codex 三轮 B2；v2 有此条，v3 改写时**被我弄丢**，此处恢复）
2. **accept 最终授权处** —— 节点可能在提交后被停用
3. **retry 最终授权处** —— 同上

三处缺一不可：提交入口挡的是脏数据落库，accept/retry 挡的是时间差。
accept 改归属（Sam 裁决 A）⇒ **双端管辖**（源与目标都在管辖内，kb_admin 除外），对齐 `kb_console.py:4182` 的 D6。

### M6 通知（Codex MAJOR）
`admin_notify._dept_admin_ids` 现仅接组码、只查 `dept_admin_grant`。改为按 contribution 的 `category_dept_id` 查覆盖它的管辖根持有者；快照不可用时与 M3 一致地通知 kb_admin（否则队列可见但无人被叫）。

### M7 文案按轴分支（Codex C10 推翻 v2 的全局文案）
- node 行未被任何 grant 覆盖 → 「归属节点不在任何部门管理员的管辖范围内」
- legacy 行 → 保留原组码语义文案
- 通用兜底 → 「该贡献当前未匹配到可审核的部门管理员」（Codex 建议措辞）

### M8 存量迁移：ID 白名单 + 数量预检（Codex C11）

**已执行 Codex 预注册验证**（prod-ro 只读，判定标准：≠4 即中止）：结果 **COUNT=4** ✅
```
CONTRIB_01KZAY128NS7RZ9186D7044PVP
CONTRIB_01KZB2FT6F6V4WF6SDR8CYY7VB
CONTRIB_01KZB2PKKD7HC3JA5J7Y3Z3SHN
CONTRIB_01KZB2R7Y6BPDAD2C0XJDHVRTB
```
迁移 UPDATE 必须同时限定 `contribution_id IN (上述4个)` **AND** `review_status='pending'` **AND** `category_dept_id IS NULL`，且执行前重新预检数量=4。
**禁止**任何形如 `WHERE category_dept='hr'` 的推断性 UPDATE（Claude L3 + Codex 加强）：组码→dept_id 是一对多，本次可映射仅因存量恰好全是 `hr`，不可推广。

### M9 DTO / 前端 + 提交端归属来源（Codex MINOR + 三轮 B1，Sam 裁决 F）

DTO 层：提交与审核的请求/响应、列表项、前端类型都显式带 `category_dept_id`。

**提交端不可复用 `OrgTreeSelect`**（Codex 三轮 B1，已核验）：
- `contribution.py:585-589` 提交只要求登录，**employee 可提交**
- `kb_console.py:104` 的 `/api/kb/org-tree` 有 `_require_kb_console` ⇒ employee **403**
- `OrgTreeSelect.vue:5` → `useOrgSnapshot.ts:90` 固定请求该接口
- 且该响应含 `my_managed_owner_depts` / `my_managed_node_roots`，放开会泄露管理结构

**解法（Sam 裁决 F）**：新增极小只读接口 `GET /api/kb/my-depts`
- 鉴权：仅要求登录（与提交端同档），**不要求管理员**
- 数据源=`dingtalk_identity` 已有的 `dept_ids`（`dingtalk_identity.py:643`）
- **不返回整棵树，不返回任何管辖/授权字段**
- 前端：1 个 ⇒ 只读展示；多个 ⇒ 下拉选

**返回规则（Sam 2026-08-06 补充裁决 G —— 直挂上级节点者可选下级）**：

对调用者所在的每个节点 N：
- N **无**活跃子节点（叶子）⇒ 返回 `N` 自己
- N **有**活跃子节点 ⇒ 返回 **N 的直接子节点集**（不含 N 自己）

这条直接消解 L1 盲区的主体：直挂 depth1 的人不再被迫把贡献挂在无人管辖的中心节点上，而是自己选一个有管理员的二级部门。

现网验证：陈吉连（`095406500537675325`）直挂营销中心（`599986031`，depth1），其 4 个直接子部门 **全部已有管理员**——国内营销部/王佳琪、国际贸易部/陈雨露、电子商务部/黄阿伟、计划部/冯连青 ⇒ 他选任意一个都有人审。

⚠️ **残余盲区（fail-closed，不消除）**：若某 depth1 节点**本身就是叶子**（无子部门）、又无管理员、且有人直挂，其贡献仍落 kb_admin 兜底。现网 depth1 的 17 个节点中，法务部属此形态但**有**管理员（王紫阳管 `44083880`）⇒ 当前无实例。此形态出现时的长期解法仍是显式授根（裁决 L1）。

审核端改归属仍用 `OrgTreeSelect`（管理员身份，鉴权本就满足）。

### M11 驳回→修改重交链路的 node 适配（Sam 2026-08-06 认可）

**现状已闭环，不新建功能**：驳回填 `review_note`（`contribution.py:1027`）→ 钉钉通知提交人（`contribution.py:1041`，rejected 独立文案分支）→「我的贡献」对 `state==='rejected'` 显示「修改重交」，带**旧稿 + 原归属**重开提交弹窗（`MyContributions.vue:47-53`），驳回理由显示在行上（`:92`）。

本方案需要改的**只有一处**：重开弹窗预填的"原归属"从组码改为 `category_dept_id`；员工端仍受 M9 约束（只能在自己部门中选）。否则重交会丢归属或带回旧组码。

**配套规则（Sam 认可）：归属错误不作为驳回理由。**

理由：员工端归属已收窄为「只有自己部门」（Sam 裁决 F），若因"挂错部门"驳回，员工改不了归属，重交回来还是同一个部门 ⇒ **死循环**。

- **归属挂错** ⇒ 审核人在采纳前**直接改归属**（M5 的双端管辖路径），不驳回
- **驳回**留给只有作者能修的问题：内容质量、信息过时、答非所问

这条要落进驳回弹窗的提示文案，否则规则只存在于文档里。

**Codex 四轮补充（已核验，纳入）**：

- 重开弹窗的 `question` / `content` / `source_message_id` / `gap_query` 已完整透传（`MyContributions.vue:55-65`、`useContribute.ts:282-311`），与 node 轴无关，无需新增持久字段。
- 「只有一处」指**只有一个轴相关业务字段**，非字面一处代码：实施时连带改 `ContributionItem` 类型、`openModal` prefill 与表单状态、POST payload 里的组码字段（已在 M9 的 DTO 范围内）。
- 🔴 **GapList 的 `g.dept` 降为展示性建议**（`GapList.vue:39-41`）：它现在会作为归属 prefill 传进弹窗，改造后**不得**再用它决定 node 归属——员工归属只能来自 `my-depts`。
- 🔴 **legacy rejected 行重开时**（`category_dept_id IS NULL`）应回退到 `my-depts` 的当前默认项，**绝不可从旧 `category_dept` 反推 node**——组码→dept_id 是一对多（Claude L3），反推会把 M8 明令禁止的推断性映射从前端绕回来。

### M10 flag 关闭行为（Sam 裁决 E）
`RAG_NODE_ACL_GRANT` 关闭时**拒绝提交 node 贡献**（400，文案对齐 `kb_console.py:2658-2663`），不静默回退组码轴。已存在的 node 行仍按 node 判定（不因关 flag 变成无人可管）。

## 边界

| 情形 | 处置 |
|---|---|
| 提交人直挂**有子部门**的上级节点 | `my-depts` 返回其**直接子节点集**，提交人自己选一个（裁决 G）⇒ 不再落兜底 |
| 归属节点是未授权的 depth1 **叶子** | fail-closed 落 kb_admin 兜底；长期解法=显式授予该根（裁决 L1）。现网无实例 |
| 提交人挂不上树 | 无法形成有效 `category_dept_id` ⇒ 走 legacy 组码轴或拒绝提交（见 M10） |
| 节点提交后被停用 | accept/retry 现查 active ⇒ 403（M5） |
| 组织快照失效 | dept_admin fail-closed；kb_admin **仅 node 分支** fail-open（M3） |
| 存量 `category_dept_id IS NULL` | 判定与改动前逐字节一致 |

## 测试

真库集成（进 `conftest._LOCAL_STACK_SERIAL_MODULES`）：
1. 管辖根 depth2 + 贡献挂 depth3 后代 ⇒ 可见 + accept 成功 + **落 node 文档三字段正确 + 默认 subtree grant 存在**
2. 贡献挂未授权 depth1 ⇒ 不可见 + accept 403 + 落 kb_admin 兜底
3. `category_dept_id IS NULL` ⇒ 与改动前逐字节一致
4. 快照失效 ⇒ dept_admin 全 False；kb_admin **仅 node 全集**，legacy 孤儿规则不变
5. **retry 与 accept 结论一致**
6. 节点提交后停用 ⇒ accept/retry 403
7. flag 关闭 ⇒ 提交 node 贡献 400
- **反证锚**：强制关掉 node 腿，用例 1 必须回到全落兜底
- **一致性用例**：构造"列表可见但 accept 403"，单一入口成立则必红

## 风险与回滚
- ADDITIVE DDL，先 apply 后部署安全
- 新增函数而非改既有 ⇒ 组码路径零回归面
- 回滚：关 `RAG_NODE_ACL_GRANT` ⇒ 新 node 贡献被拒；已存 node 行仍按 node 判定（刻意，防止变成无人可管）
