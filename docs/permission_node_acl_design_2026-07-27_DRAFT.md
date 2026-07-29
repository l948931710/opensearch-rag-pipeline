# 权限系统重设计:组织树节点 ACL(node-ACL)设计稿 _DRAFT

> 2026-07-27 · 状态:codex-review 已达成设计共识,待 Sam 裁决业务项· 决策人 Sam
> 目标形态:**管理员在管理台直接勾选钉钉组织树节点**决定文档可见度,取代手工维护的粗粒度组码。

---

## 0. 目标与非目标

**目标**
1. 文档可见度由管理员在**钉钉组织树**上勾选节点表达(子树语义:授权节点 = 该节点及其全部后代可见)。
2. 组织漂移自愈:新建叶子部门自动继承、部门改名免疫、员工转岗次日生效,**无需重新物化任何文档**。
3. 跨部门共享成为一等公民(勾多个节点),消灭 `marketing-shared` 这类硬编码特例组。
4. 保持现有安全不变量:fail-closed、单一 ACL 边界、权威/投影分离、撤销可收敛。

**非目标(本轮明确不做)**
- 不改敏感级轴(`public`/`dept_internal`/`restricted` 的路径启发式判定保持不变)。
- 不做"授权子树但排除某节点"的排除语义(理由见 §3.1)。
- 不做大爆炸迁移(理由见 §5)。

**Sam 2026-07-28 裁决(T3/T4 已定,并连带迫使 T5 进入范围)**
- **T4 归属 = 单节点**:一篇文档只属于一个部门,选叶子/子部门;UI 必须是**从中心逐级选进去的级联选择器**,不能平铺(131+ 部门无法筛)。
- **T3 伞形/共享特例取消**:`production` 伞形与 `marketing → production-family` 非对称共享**不再需要任何特例映射** —— 可见度多选到中心级节点即等价。⇒ `_PRODUCTION_UMBRELLA_OWNERS`(9 子线)与 `_DEPT_OWNER_EXPANSION` 在 node 模式下**是被删除、而不是被迁移**。

  ⚠️ **但"只勾两个中心"不等价于现网读者面**(已核验,待 Sam 复核):现行策略里 production-family 文档还有**两类中心外读者**——
  1. **海外系叠加了 `production` 读权**(2026-07-03 拍板):海外中心 / 印尼公司 / 获胜工厂 / 墨西哥公司 / 海外生产中心国内办公室,均为 `["overseas","production"]`。
  2. **总经办 = 全库可读**(`["*"]` 全组哨兵;非 kb_admin,无写权/管理台)。

  ⚠️ **引证以 LIVE 口径为准**:`RAG_ACL_ANCESTRY` 现网 **OFF**(Sam 2026-07-28 确认)⇒ 活的映射是**名字表** `dingtalk_identity._DEPT_NAME_TO_GROUPS`(海外系 `:94-100`、总经办 `:101`),**不是** `dept_ancestry.py` 的锚点表(那张表至今 dark)。两张表此处语义一致,故结论不变。

  **本稿采用的解法(等待 Sam 确认)**:
  - 海外系 → production-family 文档的默认预勾集改为 **[生产中心, 营销中心, 海外中心]**(镜像今天的真实读者面),管理员可逐篇取消;
  - 总经办 → **保持在用户侧**作为"全库读"角色(与今天一致),**不做逐文档预勾**(给每篇文档都勾总经办既荒谬又易漏)。`AclContext` 携带 `org_wide_reader` 标记。

    ⚠️ **仅靠 `can_read_doc` 放行不够——它是后置判定,而 HA3 主过滤器根本不会把 node 文档召回来**(node 文档 owner 是哨兵、且总经办的祖先链不含被勾节点)⇒ 必须**同时给 org_wide_reader 一条 HA3 候选分支**:过滤器对该角色直接放行全部 `dept_internal`(不加 owner/节点约束),召回后再统一走权威复核。
    ⚠️ `GRANT=false` 时**仍然无条件拒绝 node 文档**(不因 org_wide_reader 例外);legacy 文档继续按现有 15 组语义复核,**不得**把未知 legacy owner 顺手放宽成可见。

  ✅ **Sam 2026-07-28 确认**:采用上述解法 —— 生产类文档默认预勾 **[生产中心, 营销中心, 海外中心]**,总经办保持用户侧全库读角色。**这是等价迁移,不是收权**。
- **T5 被迫进入范围**(见 §3.5):`_kb_owner_scope_sql`(`api.py:2731-2743`)按 `owner_dept IN (组码)` 过滤,归属一旦变节点,dept_admin 管辖轴不同步改就会失效。

---

## 1. 现状(已核验,含文件:行号)

### 1.1 端到端链路

| 环节 | 实现 | 证据 |
|---|---|---|
| 身份→组码 | `_resolve_user_dept` 优先 RDS `user_role`,回退钉钉 API | `dingtalk_identity.py:293` |
| 最近祖先制 | `resolve_dept_ids` 沿父链找最近锚 → **仍只返回组码,祖先链被丢弃** | `dept_ancestry.py:82` |
| flag | `RAG_ACL_ANCESTRY` **默认关** | `dingtalk_identity.py:48` |
| 令牌 | 写 `acl_groups` JSON + legacy `dept` CSV | `auth_token.py:145` |
| 主检索过滤 | `_build_permission_filter` = 单一 ACL 边界 | `retriever.py:458-486` |
| 组→owner 展开 | production 伞形 9 子线 + marketing 非对称共享(硬编码) | `retriever.py:363-393` |
| 文档侧字段 | `chunk_meta.owner_dept`(单值组码)+ `allowed_depts`(JSON 组码列表) | `schema/001:64,204` |
| 跨部门权威 | `kb_access_request(status='approved')` | `schema/008:7` |
| 唯一注入点 | `access_grants.resolve_allowed_depts` | `access_grants.py:31` |
| 投影/撤销 | decide 同事务 enqueue outbox → stage-3 drain → reconcile 双向兜底 | `access_grants.py:205,237`、`allowed_depts_reconcile.py` |

### 1.2 HA3 字段类型(决定性约束)

| 字段 | HA3 类型 | 含义 |
|---|---|---|
| `permission_level` | **STRING**(单值) | public / dept_internal / restricted |
| `owner_dept` | **STRING**(单值) | 内容属主,保留 production 子线粒度 |
| `allowed_depts` | **MULTI_STRING**(多值) | 跨部门授权组码集;`field="x"` = **数组成员匹配** |

证据(⚠️ 分清代码契约与实机 schema):
- `permission_level` / `owner_dept` = STRING:`docs/ha3_stg_table_spec.md:17`(2026-06-10 从生产表导出的 23 字段快照)。
- `allowed_depts` = MULTI_STRING:**仅有代码契约证据**——`pipeline_nodes.py:7611-7614`(按数组推送)、`retriever.py:482`(成员匹配)。⚠️ **该字段不在上述 23 字段快照里**(快照第 38 行是 `chunk_type MULTI_STRING`,不是 allowed_depts);它是快照导出**之后**才加的。仓库内**没有**受版本控制的现网 `get_table` 实机证据 ⇒ 见 T2,**上线前必须实查**,不得从仓库文件外推。

⚠️ **`owner_dept` 是单值 STRING,无法承载多节点集合** —— 多节点授权只能落在 MULTI_STRING 字段上。

### 1.3 三个必须同时处理的既有事实

1. **org 树快照在现网不存在**。`/api/kb/org-tree` 的 `org_tree` 来自 `scratch/dingtalk_org_tree.json`(`api.py:2796`),而 `scratch/` 同时被 `.gitignore:22` 与 `.dockerignore:8` 排除、Dockerfile 只 COPY `opensearch_pipeline/` → **07-23 起的镜像应用里该文件不存在,生产恒返回 null**。本地文件停留在 2026-06-19。⇒ **组织目录服务是本设计的硬前置**。
2. **二次取回有独立权限判定点**,不走主过滤器:`_same_permission` 只比 `(permission_level, owner_dept)`、**不比 allowed_depts**(`retriever.py:1189`);cosurface(`retriever.py:1898`)与图片签名(`api.py:1934`)各有一套判定。盲区审计已记载"4+ 份手工同步副本跨 2 存储"(`architecture_blindspot_audit_2026-07-05.md:410`)。
3. **`document_acl_rule` 是死表**(`schema/001:35`,全仓零 .py 引用),不得当作现成权威表复用。

---

## 2. 核心设计:读侧展开(read-side expansion)

**判定式**:`可见 ⟺ 用户祖先链 ∩ 文档授权节点集 ≠ ∅`

关键取舍——**在读侧展开用户祖先链,而不是在写侧展开授权节点的后代**:

| 方案 | 写入 | 组织新增叶子 | 过滤项数 |
|---|---|---|---|
| 写侧展开(存全部后代 id) | 每次组织变动要重算并**重推全部文档** | 需重新物化才可见 | doc 侧数百项 |
| **读侧展开(存被勾节点,查询发祖先链)** ✅ | 文档静态、组织变动零重推 | **自动继承** | 用户侧 ≈ 树深(≤5) |

读侧展开是唯一能兑现"组织漂移自愈"目标的方案,且过滤项数由树深(现网 ≤5,`_MAX_HOPS_DEFAULT=15` 兜底)决定,而非部门规模。

### 2.1 值域命名空间:`d:<dept_id>`

节点授权值写成 `d:599318766`。现有组码全部**不含冒号**(`retriever.py:337`,注意 `corn_eco` 含下划线、并非纯字母),故两个值域**不相交**,因此:
- **复用现有 `allowed_depts` MULTI_STRING 字段,HA3 零 schema 变更、零重建表**;
- 新旧两套授权可在**同一字段的不同文档**中共存 = 天然双栈(§5);⚠️ 同一文档**不得**混投两种值(见 §2.1b);
- 过滤器只需在现有 OR 分支里多加节点项。

过滤表达式(flag 开时):
```
(permission_level="public")
 OR (permission_level="dept_internal" AND (owner_dept="..." OR ...))          ← 现状不变
 OR (permission_level="dept_internal" AND (allowed_depts="g1" OR ...          ← 现状(组码)
                                        OR allowed_depts="d:1001" OR ...))   ← 新增(祖先链)
```

### 2.1a 文档 ACL 模式:`legacy` / `node`(修正 —— 解决"叠加≠替换")

⚠️ **仅追加 OR 分支无法实现目标语义**。owner 分支(含 production 伞形 9 子线、marketing 非对称共享)恒放行,管理员在树上**取消**节点无法收回 owner 组带来的可见性 —— 那只是"增量共享",不是"勾选决定可见度"。

**解法(无需 HA3 加字段)**:引入 per-doc ACL 模式,并利用一个已存在的物理事实——**归属轴与检索投影轴本来就是两列**:

| 列 | 语义 | 消费方 | node 模式下 |
|---|---|---|---|
| `document_meta.owner_dept` | 归属/管理轴 | 看板 `kb_insights`(`kb_console.py:618`)、dept_admin 作用域 | **保持真实属主不变** |
| `chunk_meta.owner_dept` + HA3 `owner_dept` | 检索投影轴 | `_build_permission_filter`、`_revalidate_main_hits`(读 `chunk_meta`,`retriever.py:653-694`) | 写**哨兵值 `__acl_node_mode_v1__`** |

因为没有任何用户组会展开出该保留值(`_expand_groups_to_owners` 的 taxonomy 常量表里不存在),owner 分支对 node 模式文档**永不命中** ⇒ 可见性完全由节点授权决定 = **真替换语义**。且哨兵同时写 `chunk_meta` 与 HA3,两侧一致 ⇒ `_revalidate_main_hits` 的 ACL 轴比对照常通过(已核验它读 `chunk_meta` 而非 `document_meta`)。

模式存 `document_meta.acl_mode`(`legacy` 默认 / `node`),**不进 HA3**(HA3 侧由哨兵隐式表达)。

哨兵取值 = **`__acl_node_mode_v1__`**(不用短的 `_node`):净化器允许下划线故可安全通过(`retriever.py:314-325`),用户侧又因组码白名单无法伪造(`retriever.py:431-455`);但 RDS 该列**无 CHECK 约束**(`schema/001:204`)、且 owner 可从任意 `raw/<dept>/…` 路径直接提取、无白名单(`pipeline_nodes.py:1536-1547`)⇒ 必须配套:①代码常量单一来源 ②上传/注册路径 guard 拒绝该保留值 ③上线前对 RDS+HA3 现网值域做**碰撞查询**(T11)。

### 2.1b 投影是模式互斥的,且必须收敛到单一函数(修正 —— 堵住"升版复活 owner")

⚠️ **原稿漏了写入侧闭环**。chunk 的 owner 继承自 `document_meta.owner_dept`(`pipeline_nodes.py:5391-5397`)并原样写入 chunk_meta(`:6127-6133`);stage-2 只重算 `allowed_depts`(`:6022-6039`)、materialize 也只改 `allowed_depts`(`access_grants.py:188-200`)。⇒ **一篇 node 文档一旦升版 / re-chunk / 重新 ingest,真实 owner 会被写回检索投影轴,legacy owner 分支静默复活 = 权限重开**。

**修正**:新增唯一投影函数 `project_doc_acl(mode, real_owner, groups, node_ids) -> (chunk_owner, allowed_depts)`,与 `access_grants` 现有"单一注入点"纪律同型,**所有写/重建路径必须调用**:普通 ingestion、升版、re-chunk、ACL materialize、stage-3 reload(`dataworks_orchestrator.py:549-565`)、reconcile、全量导出。

**投影不变量(模式互斥,不得混投)**:

| mode | `chunk_meta.owner_dept` + HA3 | `allowed_depts` |
|---|---|---|
| `legacy` | 真实 owner | 组码集(现状) |
| `node` | **哨兵** | **仅 `d:<id>`,绝不含组码** |

⚠️ node 文档若同时投影组码,`retriever.py:477-486` 的组码 OR 分支仍会放行 —— 哨兵就白设了。"双栈"**只表示不同文档可分处两种模式**,不表示同一文档同时投两种值。

### 2.2 权威表与投影

新增权威表 `kb_doc_node_grant`(doc_id, dept_id, granted_by, granted_at, revoked_at),**复用现有权威/投影模式**:

```
kb_doc_node_grant(active) ─┐
                           ├→ access_grants.resolve_allowed_depts(唯一注入点)→ DocAcl(groups,node_ids,mode)
                           │   ⚠️ 按 mode **互斥**取值,非混合并集——以 §2.1b 投影表为准
kb_access_request(approved)┘        │
                                    ├→ chunk_meta.allowed_depts(RDS 投影)
                                    ├→ outbox(009/049 generation CAS)
                                    └→ stage-3 drain → HA3(检索投影)
                                       + allowed_depts_reconcile 双向兜底
```
撤销收敛链路的**机制**复用,但有三处**必须改造**(不是"原样复用"):

1. **权威解析不得先混合再净化**。`resolve_allowed_depts` 现把所有值送进组码白名单(`access_grants.py:41` → `sanitize_owner_depts`),`d:` 会被静默丢弃。改为返回结构化 `DocAcl(groups, node_ids, mode)`,两个权威源**分别解析、各自校验**,只在 RDS/HA3 **投影边界**才编码成混合集合。
2. **查询侧撤销复核必须节点感知**。`_deny_revoked_cross_dept`(`retriever.py:581-584`)现做纯组码相交 `groups & authorized`;节点授权命中会被判"无授权"而**误丢**。若为可用性绕开这层,陈旧节点授权又会在撤销窗口内**泄露**。⇒ 必须扩成 `AclContext × DocAcl` 判定。
3. **reconcile 候选集必须纳入节点授权**。全扫对账现只扫 `kb_access_request` + 已有投影(`allowed_depts_reconcile.py:117`),"只有节点授权、尚无投影"的文档**不可被全扫发现**,只能靠 outbox —— 兜底腿断了。

### 2.2a 前置重构:统一读判定 `can_read_doc(acl_ctx, doc_acl)`

ACL 判定现散落在**至少 5 处**独立实现(盲区审计已记载"4+ 份手工同步副本跨 2 存储",`architecture_blindspot_audit_2026-07-05.md:410`):主检索过滤 → 撤销复核(`retriever.py:581`)→ 邻居/step(`retriever.py:1189`)→ cosurface(`retriever.py:1898`)→ 图片签名(`api.py:1943,1972`)。**在新增第 6 种语义之前必须先收敛**:抽出单一纯函数 `can_read_doc(acl_ctx, doc_acl) -> bool`,各点统一调用。这是本轮的**前置重构,不是可选项**。

### 2.2b 上传后随时改可见范围(Sam 2026-07-28 提问确认)

**支持,且与首次设置是同一个入口**。节点授权存在权威表 `kb_doc_node_grant`,**不烘焙进上传动作** ⇒ 文档上线后任何时候都能重开组织树多选、保存。

**今天的对应能力(粒度不同)**:`POST /api/kb/access-grants`(`kb_access.py:354`,"Owner 侧主动共享")已允许属主部门管理员/kb_admin 在上传后直接放行指定部门、无需对方申请。差别在:①粒度是**15 个固定组码**,不是组织树节点;②语义是**追加放行**,不是"重设可见范围";③一次点一批组码,不是在树上多选。

**⚠️ 生效延迟是不对称的(必须在 UI 上如实告知)**:

| 方向 | 生效时机 | 原因 |
|---|---|---|
| **收窄**(取消某节点) | **立即** | 查询侧 authority recheck 当场拦截(`_deny_revoked_cross_dept` + 提交后 `invalidate_deny_cache`),不等投影 |
| **放宽**(新增某节点) | **等下一次 stage-3**(日 baseline ~24h) | HA3 服务端过滤是召回闸门,投影没到就根本召不回来 |

这个不对称是 HA3 作为召回闸门的**固有性质**,不是实现缺陷 —— 而且方向正确(收权即时、放权滞后 = fail-safe)。

**建议配套「立即生效」按钮** —— ⚠️ 但**不得移植 `scratch/repush_doc_phase_d.py` 的实现形态**:该脚本自己声明"无 DAG locks / 无 deactivate / 无 claim"(`:1-12`)且不支持图片/step-card 完整载荷,而正式 stage-3 有版本 claim、PROCESSING 失效接管、批边界完整性闸、失败阻止旧版本停用、状态 CAS(`pipeline_nodes.py:6410-6520,6619-6750,8042-8284`)。正确形态:

1. 保存权威 + enqueue(带 generation CAS,`schema/049`);
2. **受控 worker 定向 materialize**;
3. 走**标准 stage-3 状态机**(claim/fencing/parity),不是 HTTP 线程直推;
4. UI 分三态:排队 / 处理中 / **已在 HA3 验证** —— 只有验证成功才显示"已生效"。

或在迁移窗口内临时提高 stage-3 频次作为过渡。

### 2.3 用户祖先链从哪来

**不放进令牌**(令牌签发后缓存,组织调动会留下陈旧特权),而是**服务端按 staffId 实时解析**、复用现有 ~45s ACL 缓存(`api.py:524`):
`staffId → dept_dim(本地 RDS 组织快照) → 祖先链 [leaf, …, root]`
- `dept_dim` 不可达/快照超期 → **fail-closed 退回仅 public**(与现有 partial 语义一致)。
- 多直属部门 → 各自祖先链取并集,总项数设硬上限(见 §3.3)。

---

## 3. 关键语义裁决

### 3.1 纯子树继承,不支持"排除"

授权节点 N ⇒ N 的整棵子树可见,**不提供"授权 N 但排除 N 下某节点"**。理由:
1. 排除会让判定从"交集非空"变成非单调谓词,fail-closed 推理与 HA3 过滤表达都显著复杂化;
2. 任何排除需求都可用"改勾更具体的子节点"表达 —— ⚠️ 但**表达力并非严格无损**:枚举 N 的现有子节点后,将来 N 下**新增**的直属子节点不会自动继承(这正是我们要的自愈特性的反面)。作为非目标可接受,但不得声称等价;
3. ⚠️ **锚点表的显式 `[]` 不是节点通道的收紧手段**。`dept_ancestry.py:30` 的 `[]` 语义是"**组码解析到此为止**",它**不得**用来截断物理祖先链 —— 否则授权根节点将覆盖不到该部门,直接违反纯子树语义。两条通道的"停止"语义必须严格分开实现。

### 3.1a 不限层级 + 「仅本节点」语义(Sam 2026-07-29 两问)

**问一:节点只下放到二级吗?** → **不限层级,开放全树**。限制层级**没有安全收益、只有表达力损失** —— 子树语义下,深层节点是**更窄**的授权而非更宽。实测支撑(2026-07-29):

```
员工挂载层级:L1 97人(8.3%) · L2 273人(累计31.5%) · L3 600人(累计82.6%) · L4 129人 · L5 76人
各层部门/直属人数:L1 17个/111人 · L2 28个/288人 · L3 57个/615人 · L4 11个/129人 · L5 6个/77人
深层(≥L3)节点 74 个中 62 个在生产中心下
```
⇒ 只到二级会让 **68.5% 的员工所在层级无法被精确收窄**(仍能被上层子树覆盖,不会漏人)。现状 18504 条 chunk 全挂笼统 `production` 说明今天没有车间级隔离;开放全树是为将来留出表达力,零成本。
**UI**:默认展开到二级、更深按需展开(119 节点不一次全铺);归属单选与可见度多选共用同一棵树。
⚠️ **每个节点必须显示子树实际人数** —— 授权到比员工挂载点更深的节点 = 没人能看到(例:某车间的人都挂在车间层,却授权给其下班组)。选完即见"这会给 X 人看",选到 0 人当场发现。数据由同步 job 顺手物化。

**问二:直挂在上级、没进下级部门的人怎么办?** → 先澄清:**选父节点 = 父节点直挂人员 + 整棵子树**,直挂人员**已被覆盖**。真正表达不了的是"要部分子部门 + 直挂人员,但不要其他子部门"。

实测这个缺口不小:**26 个节点有子部门却仍有直挂人员,合计 172 人(全员 14.6%)** —— 获胜生产中心 38 · 国际贸易部 28 · 国内营销部 20 · 获胜包装 14 · 注塑事业部 8 …

⇒ 引入**第二种节点值**(值域仍不含组码冲突):

| 值 | 语义 | 用户侧匹配 |
|---|---|---|
| `d:<id>` | 该节点 **+ 整棵子树** | 用户**祖先链** ∩ 该集合 |
| `dx:<id>` | **仅直挂本节点**的人 | 用户**直属部门** ∩ 该集合 |

⇒ `AclContext` 须同时携带 `ancestor_dept_ids`(祖先链)与 `direct_dept_ids`(直属部门)。
判定:`(祖先链 ∩ 子树节点) ∪ (直属部门 ∩ 仅本节点)` 非空。
**UI**:每个节点一个勾选 + 「含下级」开关(默认开);关掉即 `dx:`。

### 3.2 ACL 权威是文档级;投影是最终一致(修正措辞)

**权威层**:同一 doc+version 的 ACL 是**文档级**的 —— RDS 正常写路径确实整体一致(`materialize_doc_allowed_depts` 按 `doc_id+version_no+is_active=1` 整体 UPDATE,`access_grants.py:196-200`;首写同事务全删全插,`pipeline_nodes.py:6043,6136`)。

**投影层不是原子的**,原稿"天然保证"的措辞过强,已核验两处反例:
- **对账用并集比较会漏判**:`current_allowed_for_doc` 把各 chunk 的值 union 后与 want 比(`access_grants.py:92-108`)。若 chunk A=`["d:1"]`、chunk B=NULL,并集仍等于 want → 判 `unchanged` → **B 永远不会被修好**。⇒ 对账须改为**逐行等于 canonical 值**,而非比并集。
- **HA3 投影跨批非原子**:stage-3 装载 `LIMIT 1000` 不按文档分组、**明确可能把一个文档切成两批**(`dataworks_orchestrator.py:476` 原注释),HA3 再按 ≤100/子批并行 upsert(`pipeline_nodes.py:7596`)。版本切换的安全由 `node_deactivate_old_chunks` 的边界完整性闸兜底,但**ACL 投影在批间存在可见的不一致窗口**。

⇒ 撤销窗口的机密性**必须靠文档级 authority recheck**(§2.2a 的 `can_read_doc`),不能依赖"投影不变量"。`_same_permission`(`retriever.py:1189`)只比 `(permission_level, owner_dept)`、不比 allowed_depts —— 在 node 模式下哨兵值使其仍然成立(同 doc 全 chunk 同为哨兵),但**必须加负例测试**锁死。

### 3.3 注入与规模上限

- dept_id 一律**严格解析为正整数**(`int()` + 范围校验),**不沿用"净化非法字符后接受"策略**;非法 → 丢弃该支并告警。
- ⚠️ 节点值**绝不可流经 `_sanitize_ha3_filter_value`**(`retriever.py:314`)—— 该净化器会**删掉冒号**,`d:123` 会被打成 `d123`。正确做法:服务端严格数字解析后**自行拼接** `"d:" + str(int(id))`,净化器只作用于组码。
- **写侧超限 = 拒绝,不截断**:管理员提交超过上限的节点集 → 返回 422,绝不静默截断后让 UI 以为已完整保存。
- **读侧超限 = 丢弃整个节点通道并告警**,不做"取前 N 项"(顺序不稳定),**且绝不回退到 legacy 名字/组码口径**(现有 `dept_ancestry` 的 partial 语义要求调用方回退名字口径,`dept_ancestry.py:91` —— 节点解析器**不得继承**该行为,否则超限反而变成换一条更宽的通道)。
- 截断本身**不会放宽权限**:判定式是纯正向的"交集非空",祖先集合被截成子集只可能少命中;前提正是上一条(不回退 legacy 通道)。
- ✅ **上限已按 T8 实测校准(2026-07-28,见 §9.0)**。实测:树深最大 **5**、单用户直属部门数最大 **5**、**祖先链并集(= 节点 OR 项数)最大 8 / P99 5 / 平均 2.9**。据此锁定:

  | 项 | 上限 | 依据 |
  |---|---|---|
  | 单用户直属部门数 | **8** | 实测最大 5,留 60% 余量 |
  | 单链深度 | **15**(沿用 `_MAX_HOPS_DEFAULT`) | 实测最大 5,防环足够 |
  | 过滤器节点 OR 项数 | **32** | 实测最大 8 → 4× 余量(原拟 64 属过度保守) |
  | 单文档授权节点数 | **32** | 一级部门仅 17 个,管理员实际只会勾少数中心 |

  ⚠️ 三者不再自相矛盾(8 个部门的链并集实测就是 8 项,远低于 32)。**T1 的风险实质消失**:节点分支最多给过滤器加 8 项,与现有 owner 分支(production 伞形展开已达 ~12 项)同量级,不构成新的表达式规模风险 —— 但 T1 仍须实测确认冒号值 `d:123` 的写入与过滤解析行为。

### 3.5 归属改单节点 + 管理轴同步迁移(Sam 2026-07-28 裁决的展开)

**归属(owner)= 单个组织节点**,新列 `document_meta.owner_dept_id`(与 legacy 的 `owner_dept` 组码**并存**,由 `acl_mode` 决定谁权威)。为什么不复用同一列:过渡期两类文档必须同时可被 dept_admin 作用域命中,合并到一列会逼出字符串前缀判别,脆。

**归属节点默认预勾进可见集**(可取消,取消时 UI 明确警示"属主部门将看不到本文档")。理由:现行 owner 分支天然给属主读权,不预勾会造成大量"自己传的文档自己看不到"。⚠️ 这**只是 UI 的显式默认值**——后端绝不得在保存时偷偷补回属主节点(否则"取消勾选"变成谎言);且 `restricted` 仍然压过节点可见集(§3.4 的 AND-bind 不变)。

**UI = 级联选择器**(中心 → 部门 → 车间/班组),归属单选、可见度多选,两者共用同一棵 `dept_dim` 树;必须支持按名搜索(131+ 部门纯靠展开找不现实)。

**管理轴(T5)同步迁移**:新增**独立表 `dept_admin_node_grant(user_id, managed_dept_id, source, granted_by, is_active, …)`** —— **不**在 `dept_admin_grant` 上加列(该表 `managed_owner_dept` 是 NOT NULL + 唯一键 `(user_id, managed_owner_dept)`,`schema/006:34-37`,加 nullable 列既表达不了 node-only 授权、也挡不住一行同时授两条轴)。作用域 SQL 改为:

```sql
-- ⚠️ 必须按 mode 隔离,不能无条件 OR
AND (
      (m.acl_mode = 'legacy' AND m.owner_dept    IN (<该 admin 的 legacy 组码集>))
   OR (m.acl_mode = 'node'   AND m.owner_dept_id IN (<managed_dept_id 的全部后代 id,含根自身>))
)
```

⚠️ **无条件 OR 是越权洞**:node 文档的 `owner_dept` 残值会继续命中 legacy 管理员 —— 归属已迁走,旧属主部门管理员却仍能管。
**fail-closed 三态**:未知 mode / node 模式缺 `owner_dept_id` / 组织快照读取失败 ⇒ **只允许 kb_admin**,dept_admin 一律不放行。

**新增文档专用 helper,不改既有通用 helper 语义**:`_kb_can_manage`(`api.py:2746`)当前只收单个组码,而**知识贡献审核仍用它判 `category_dept` 组码**并复用 `_kb_owner_scope_sql`(`contribution.py:711,849`)⇒ 直接改会误伤贡献流。新增:
- `can_manage_doc(kb, acl_mode, legacy_owner, owner_dept_id)`
- `doc_owner_scope_sql(mode_col, legacy_col, node_col)`

所有**文档写端点**必须在锁内读齐三列再裁决。⚠️ 现状两个已知缺口:直接共享与 visibility-explain 只读 `owner_dept`(`kb_access.py:395,502`);审批只信 `kb_access_request.owner_dept`(`:940`)—— 归属迁移后**旧属主管理员仍能操作**,须一并修。

**须同步扩** `KbIdentity`(现只存 `granted_owner_depts`,`kb_authz.py:135`)与权威现查(现只读 `managed_owner_dept`,`dingtalk_identity.py:897`)。

**管辖根 = 自动派生 + 手动覆盖(Sam 2026-07-28)**,但自动派生**必须锁死这五条**,否则调岗即静默提权:
1. **只有 active `dept_admin` 角色**才派生管辖根 —— 普通员工绝不因组织归属获得任何管理权;
2. 手动覆盖行是**替换**自动根,**不是取并集**(`source` 列区分 auto/manual,manual 存在时 auto 失效);
3. **无部门或多直属部门**不得自动取并集 ⇒ **fail-closed,转人工指定**;
4. **自动根发生变更、或根是中心级、或后代规模超阈值** ⇒ 告警并要求人工确认后才生效(现网大量员工挂在**中心**而非叶子,中心级自动根 = 一次性拿到整个中心的管理权);
5. 组织快照 **>48h** 时,自动根与后代展开**同时失效**,只保留 `kb_admin`。

**后代集缓存**:必须**含根自身**;缓存键带**组织快照 revision/更新时间** —— 否则组织同步后未失效的进程缓存会继续把已移出子树的部门交给旧管理员。读取失败/环/超规模/快照过期 ⇒ **不得回退 legacy 管辖**(fail-closed)。

后代集**每次查询时从 `dept_dim` 现算**(小表 + 进程缓存),**不做物化路径** —— 与本设计"读侧展开"的一致哲学:组织调整后管辖范围自动跟随,无需回填任何文档。生产中心这类大子树约 85-100 个后代,`IN` 列表规模可接受。

⚠️ **三分授权原则不变**:`managed_dept_id` 只表示"能管这些文档"(上传/升版/退役),**不代表能读全部**,也不代表能授权给别人 —— 读组仍由用户自己的祖先链决定,授权面另算。

**存量归属迁移**:现有 `owner_dept` 是 **OSS 路径派生的组码**(`raw/production_mold/internal/…`),不是钉钉 dept_id,**无法自动翻译**。阶段 B 由管理员在级联选择器里重选真实节点时逐篇完成;未重选的文档保持 legacy 模式、行为不变。

### 3.4 保留 public / restricted

三态敏感级不变,节点授权仍**AND-bind 到 `dept_internal`**(沿用 `gate_by_permission`,`access_grants.py:111`):public 全员可见、restricted 永不放行。这保持了现有纵深防御,也让 flag 关闭时行为逐字节不变。

---

## 4. 改动范围

| 文件 | 改动 |
|---|---|
| `dept_ancestry.py` | 新增 `resolve_ancestor_chains()`——返回**祖先链 id 列表**(现有 `resolve_dept_ids` 返回组码的行为不动) |
| `dingtalk_identity.py` | 身份解析附带 `dept_chain`;新增 `dept_dim` 读取 |
| **`acl_policy.py`(新)** | **前置重构**:`AclContext(groups, ancestor_ids)` / `DocAcl(groups, node_ids, mode)` / `can_read_doc()` 单一判定(§2.2a) |
| **`api.py`** | `Identity` 增 `ancestor_dept_ids`;`/api/ask`·`/stream` 传 `AclContext` 而非裸组列表(`api.py:515,580,889,940`);图片签名(`:1943`)改调 `can_read_doc`;history/conversation 回放策略(§7 T9) |
| **`dingtalk_bot.py`** | 机器人入口同样构造 `AclContext`(`dingtalk_bot.py:1247`);卡片重建图片重签(`:2118,2161`)走统一判定 |
| `retriever.py` | `_build_permission_filter` 追加节点 OR 分支(flag 门控)+ node 模式哨兵;`_deny_revoked_cross_dept` 改节点感知;cosurface 同步 |
| `access_grants.py` | `resolve_allowed_depts` → 返回结构化 `DocAcl`,两权威源分别解析、仅在投影边界编码(**不得先混合再过组码白名单**) |
| `allowed_depts_reconcile.py` | 候选集纳入 `kb_doc_node_grant`;一致性判定改**逐行等于 canonical**(不比并集) |
| `routes/kb_access.py` | `visibility-explain`(`:517,545`)须能解释节点授权;保存端点加**文档行锁/`acl_revision`**(现 direct-grant 只普通读 `:395-401`、审批只锁 request 行 `:940-955` → 并发整体替换会撕裂);node 文档上的 legacy grant/approve 须**显式拒绝或标记 inert**(否则 node 期间新批的隐形组码会在回滚时突然复活) |
| **`pipeline_nodes.py`** | 普通 ingestion / 升版 / re-chunk 全部改调 `project_doc_acl`(`:5391-5397,6022-6039,6127-6133`)—— **不改则升版即重开权限**(§2.1b) |
| **`dataworks_orchestrator.py`** | stage-3 reload 按 mode 重算投影(`:549-565`),不只重解析 allowed_depts |
| **`routes/kb_console.py`** | `kb_stats` 的 dept_admin chunk 数按 `chunk_meta.owner_dept IN (…)` 统计(`:404-412,437-444`;通用 scope SQL `api.py:2731-2743`)→ node 文档会**从真实归属部门的统计里消失**;改为 JOIN `document_meta` 按 `m.owner_dept` 作用域(`kb_insights` 本就走 document_meta,安全) |
| **`ha3_verify.py`** | 自查探针把 owner 当"模拟用户组"发 self-query(`:29-40,67-77`),哨兵不在组码白名单 → 归一为空 → **node 文档被误报不可检索**;须改收真实 `AclContext`/祖先链 |
| `routes/kb_console.py` | `/api/kb/org-tree` 改读 `dept_dim`(不再读 scratch 文件);可见度编辑端点接受节点集 |
| `routes/kb_access.py` | 新增节点授权/撤销端点(复用 decide 同事务 enqueue 模式) |
| `schema/0XX_node_acl.sql` | ①`document_meta.acl_mode NOT NULL DEFAULT 'legacy'` ②`document_meta.acl_revision NOT NULL DEFAULT 0`(并发整体替换的 CAS)③`kb_doc_node_grant` + **`UNIQUE(doc_id, dept_id)`** + active/revoked 查询索引 + soft-revoke/reactivate 语义 ④`dept_dim`/`staff_dim` 组织快照表 |
| **`chunker.py` / `clients.py`** | 本地 OpenSearch 回退链:`to_opensearch_doc()` 不输出 `allowed_depts`(`chunker.py:199-234`)、index mapping 也未声明(`clients.py:433-457`)⇒ **node 文档在本地 E2E/灾备回退中全部不可见**(不越权,但功能缺失) |
| **`scripts/export_full_to_oss_for_v2.py`** | 全量离线导出直接复制 `cm.owner_dept` 且**根本不输出 `allowed_depts`**(`:53-58,78-104`)⇒ 必须改调 `project_doc_acl` |
| `scripts/sync_dingtalk_org.py` | **新增**:钉钉组织同步 job(只读钉钉 → 写 dept_dim/staff_dim) |
| `console-app/` | 组织树多选控件替换现有 `GROUP_LABEL` 硬编码分享目标(`useKb.ts:175`) |

HA3:**无 schema 变更、无重建表**(§2.1)。

---

## 5. 迁移:双栈,不做大爆炸

**核心性质**:因为新旧值在同一 MULTI_STRING 字段命名空间不相交,**存量文档可以完全不动**。

- 阶段 A(本轮):前置重构 `can_read_doc` + 组织同步 job + 权威表 + 过滤器节点分支 + 哨兵机制(双 flag:`RAG_NODE_ACL_GRANT` 默认关 / `RAG_NODE_ACL_ENFORCE` 默认开,见 §5 真值表;**不得再造第三个开关**)。存量文档全部 `acl_mode='legacy'`,行为逐字节不变。
- 阶段 B:Sam 2026-07-28 决定**清空存量语料 + 各部门按规范命名重传** ⇒ 逐文档归属迁移取消。但"清空"的**执行协议必须重写**(codex 增量评审 4 个 blocker,已逐条核验):

### 5.1 「清空」= 软退役 + manifest,**严禁硬删**

⚠️ **硬删 `chunk_meta` 会致盲唯一的 HA3 孤儿清理工具**:`ha3_reconcile` 的扫描上界取自 `SELECT MAX(id) FROM chunk_meta`(`ha3_reconcile.py:187`,注释明写"孤儿 PK 可能大于 max(active id),扫描上界必须覆盖 inactive 行")⇒ 先清表则 MAX(id) 失效,**~27k 条遗留 HA3 行再也枚举不出来**。同理 `spot_checker` 依赖 `document_meta` 行取锁、依赖 `chunk_meta` 枚举 HA3 PK(`spot_checker.py:223-245,332-391`)。

⇒ **禁止 `DELETE/TRUNCATE document_meta / document_version / chunk_meta`**。清空只能走**有 manifest 的逐文档软退役**(`kb_console.py:2520-2589` 现成路径:锁 document_meta → 标 retired → 停用全部 chunk → 全版本置 PENDING_DELETE,保留审计与恢复入口)。RDS 元数据 + OSS raw/canonical/rag-ready **保留到新语料验收与回滚窗口结束**。

### 5.2 文档身份:优先保住逻辑 doc_id

金集**双锚都会断**:`golden_full.json` 225 条正例全有标题锚、其中 165 条另有 `expected_doc_ids`,匹配只认 doc_id 或标题相似度(`eval_harness/matching.py:89-107`)⇒ 换 doc_id + 规范化改名 = 两个锚同时失效。更隐蔽的是:**release regime 记录了模型/题集/评测器版本,却没有 corpus 指纹**(`run_eval.py:206-248`)⇒ 不改金集也会**静默拿新语料分数比旧基线**;L6 算了 `chunk_id_set_hash` 但 baseline 不消费(`l6_chunk_quality.py:578-596`)。

⇒ 二选一:**①受控"认领/替换版本"路径保住逻辑 doc_id**(优先);②建不可变 `old_doc_id → new_doc_id` 映射,并重写金集、贡献引用、看板历史口径。⚠️ 保 doc_id 也**不能直接复用现有升版**:退役文档禁止升版(`kb_console.py:2113-2116`),且正文相同会被 unchanged gate 跳过并回退旧版(`pipeline_nodes.py:1129-1201`,生产 `RAG_SKIP_UNCHANGED_REINGEST=true`)⇒ 需迁移专用受控路径。**并给 release regime 补 corpus snapshot 指纹**。

### 5.2a 迁移路径定案(Sam 2026-07-28):**按内容是否变化分三类,事后重绑金集**

放弃"逐篇受控升版配对" —— Sam 指出改版后名字也变了,管理员无从知道旧版叫什么,配对成本不可接受。改为:

**① 文档按"内容是否变化"分三类走不同路**

| 情况 | 做法 | 成本 |
|---|---|---|
| 内容不变、名字也不用改 | **什么都不做** | 零。doc_id 天然保住,金集不受影响 |
| 内容不变、只需规范化名字/分类/归属/可见范围 | **改元数据**(不重传) | 标脏重推 HA3(title 进 chunk→HA3,`chunker.py:182,210,256`);**零抽取、零 embedding API**(chunk_text 未变、缓存全命中) |
| 内容确实变了 | 正常重传 | `canonical_sha256` 自然不同,去重不误伤 |

⚠️ **改元数据端点当前不存在**:`routes/kb_console.py` 的写端点只有 approve/reject/register/restore/retire/set-visibility/upload-url/review-tasks/feedback-review —— **没有任何改标题/分类/归属的路径**。这是本轮必须新建的能力(改标题 + 改分类 + 改归属节点 + 改节点可见集,一个端点四件事,同事务 + 审计 + 标脏重推)。

**② 跨文档去重的处置(比原判断更乐观)**

先前判定"必须迁移期关掉 `RAG_DEDUP_CROSS_DOC`"**过度**了。按上表分流后:内容变了的 sha 不同不触发;内容没变的根本不重传也不触发。**唯一残留**=管理员不知道内容没变、仍重传了一遍 —— 此时去重跳过**是正确行为**,只需把提示改准:"与现有《XXX》内容完全相同,已跳过;若只想改名请用『编辑信息』"。

✅ **且系统已能在上传时就发现**:`document_version.etag` 有专用跨库查重索引(`schema/007`,现为 advisory 提示不拦截)⇒ 升级为**上传即引导**:etag 命中 → 直接跳转"编辑信息",不让他传完再被静默跳过。

⚠️ 仍需注意:`_xd_covers`(`pipeline_nodes.py:811-848`)读的是 **`document_meta.owner_dept`**(真实 owner),不是检索投影轴 ⇒ **node 模式的哨兵救不了它**,别指望哨兵自动豁免去重。

**③ 金集:事后按题目重绑,不做事前配对**

语料换新后,"这道题该命中哪篇文档"的正确答案**本来就是新文档** —— 保旧 doc_id 反而是在保过期映射。⇒ 重传完成后统一重绑,**管理员零负担**。三条硬约束:

1. ⛔ **绝不采纳检索 top 命中当标准答案** —— 循环论证会让 recall 假绿。仓库已踩过同型坑(PDF GT 循环性,同一改动分数从 -0.133 假翻成 +0.064,见 `docs/` 相关记录)。必须**读新文档内容判断是否真回答了该问题**(LLM 辅助 + 人工抽查)。
2. **必须给 release regime 补 corpus 指纹**:现只记模型/题集/评测器版本(`run_eval.py:206-248`),换语料后会**静默拿新分数比旧基线**;L6 已算 `chunk_id_set_hash` 但 baseline 不消费(`l6_chunk_quality.py:578-596`)。补上后系统才能自己拒绝跨语料比较。
3. **接受评测真空期**:重绑完成前 release-gate 不可用,期间发布门禁改走**绝对门**而非相对基线;完成后显式冻结新 baseline。

✅ **可先干跑省一大截**:金集匹配接受 **doc_id 或标题相似度任一**(`matching.py:89-107`)⇒ 规范化若保留主题词,标题锚会自己存活。重传后先跑一次 eval,**只重绑真正断掉的题**(金集共 258 题、引用 200 篇文档,实际断裂面预计远小于此)。

### 5.2c 全量重传定案(Sam 2026-07-28:归属/名字/内容都会大改)

分三类的优化在"三样都改"面前基本失效 ⇒ **全量重传成立**。但"清空"必须严格区分两种含义:

| | 含义 | 判定 |
|---|---|---|
| **逻辑清空** | 软退役:`status='retired'` + `is_active=0`,**保留** document_meta / document_version / chunk_meta 行 | ✅ **只能这样** |
| **物理清空** | `DELETE/TRUNCATE` RDS 表 + 清 HA3 | ⛔ **禁止**(§5.1 三个 blocker:孤儿枚举致盲、看板断链、不可逆) |

**两笔容易被漏算的成本**:

**① 图片重判风险 —— ⚠️ 本条 2026-07-29 已修正,原判断过时**

~~原写"VLM 缓存全 miss + 6.5% 单向翻 DISCARD"~~ —— 该 6.5% 实测(2026-07-25)**早于** 2026-07-26 的针对性修复,两个前提都不成立:

1. **主路径已从结构上堵死**:选项 C(`RAG_FUNNEL_DISCARD_REQUIRES_CATEGORY`,commit `8f1d977`+`97871ac`,**默认 ON**)规定**只有 VLM 自己断言 `decorative`/`logo_header` 才弃图**,`unknown` 一律救回 ROUTE_TO_VECTOR(`image_funnel_processor.py:152-175`)。实测样本里 32 张 LOW_RELEVANCE 有 **26 张(81%)是 `unknown`**(模型在"拒绝归类"而非断言装饰),这批现在全部走 rescue。
2. **"缓存全 miss"不必然**:VLM 缓存按**图片 MD5** 键控,不是按文档。原文档编辑另存通常**保留内嵌图片字节** → 缓存命中、根本不重判;只有重新导出/换工具重做才重压缩。

**残留风险**(量级远小于原判、未实测):VLM 重跑时**明确断言** `decorative` 的那一小部分仍会被弃(原样本 6/32)。

**对策(实测而非预设)**:试点部门量三个数 —— **VLM 缓存命中率 · DISCARD 率 · 重传前后图覆盖率对比**。
**可选加固**:开**选项 E**(`b5aae22`,漏斗判决落 RDS、已判过的图免疫 VLM 制度漂移)—— 正是大批量重跑的对症药。⚠️ 前置:`schema/059_image_funnel_verdict.sql` **生产尚未 apply**(2026-07-28 查 `schema_migrations` 确认),且默认 OFF。

**② 金集断裂面会远大于预估**
名字也改 ⇒ **标题锚同时失效**(先前"标题锚可能自己活下来"的乐观假设不成立)⇒ 258 题里断裂的比例会很高,基本等于**重标一遍金集**。这是全量重传的固有代价,须计入排期,并按 §5.2a 三条硬约束执行(不采纳检索 top 命中 / 补 corpus 指纹 / 真空期改绝对门)。

**执行姿态:按部门分批,先传后退**

```
每个部门:上传新文档 → 验证可检索 → 退役该部门全部旧文档 → 下一个部门
```
- **中断窗口 = 单部门的短暂重叠期**,而不是全公司停摆几天/几周(1562 篇的重新入库周期取决于各部门配合速度,不可控)。
- 过渡期同部门新旧并存 ⇒ 答案可能引用旧版,但"有旧答案"优于"无答案";并存期以小时计。
- 旧文档全部退役后不再是 active incumbent ⇒ **跨文档去重天然不触发**。
- ⚠️ **不可逆的 HA3 删除排到最后**:全部部门验收完成后才 drain PENDING_DELETE,要求 `pending=0` 且 RDS↔HA3 missing/extra=0。

**前置条件更硬了**:既然**归属也要改**,重传时管理员就要在组织树上选归属节点 + 可见节点集 ⇒ **node-ACL 必须先上线**,否则这批文档要重做一遍归属。

### 5.3 顺序改为「预装 — 验证 — 切换 — 延迟清理」

原稿"开 GRANT → 清空 → 重传"**不是最优也不必要**。真正的硬约束只是:**node schema / 上传写契约 / projector / ENFORCE 必须先上线**;`GRANT` **不必先开** —— `GRANT=false` 时预装的 node 文档 fail-closed 不可见,验收后再切换,期间**旧语料持续服务、零知识中断**。

```
① 部署 node schema/projector/ENFORCE(GRANT=false)
② 冻结旧语料 → 不可变 manifest(doc/version/chunk PK、raw/rag-ready key、checksum、旧标题/owner、金集与贡献引用)
③ 修跨文档去重的 mode/cohort 语义(见下)→ 预装并索引 node 文档
④ 重绑定金集 + 给 regime 补 corpus 指纹 → 过绝对门 → 显式冻结新 baseline
⑤ 切换:软退役旧文档 → 开 GRANT(短维护窗,避免旧 legacy ACL 比新 ACL 更宽时继续放行)
⑥ 验收通过后才 drain 不可逆 HA3 删除,要求 pending=0 且 RDS↔HA3 missing/extra=0
```

⚠️ **预装期的阻断项:跨文档去重会杀掉新文档**。生产 stage-1 显式开启 `RAG_DEDUP_CROSS_DOC=true`(`dataworks_nodes/stage1_node.py:40`,代码默认 OFF、节点 setdefault 开启),受众覆盖判断走 legacy `(permission_level, owner_dept)`(`pipeline_nodes.py:811-845,1221-1255`)。旧文档仍 active 且正文相同时,新 node 文档可能被标 `SKIPPED_DUPLICATE` → 旧文档随后退役 ⇒ **两边都不可用**。必须先让去重理解 `acl_mode`/迁移 cohort,或在受控迁移任务中关闭 skip 并**核实 DataWorks 侧实际生效值**(不能按代码默认外推)。

**最安全形态**:独立 HA3 表/语料 generation 做**蓝绿切换**;若坚持同表,则按上述短维护窗。

双栈仍需保留(预装与切换期间系统要持续服务),但生命周期从"长期共存"缩短为**一次性过渡**。

**"保存"是替换而非追加**(明确裁决):管理员点保存 = 该文档进入 `node` 模式,**同事务内**完成 ①`acl_mode` 切换 ②节点授权集整体替换 ③既有 `kb_access_request` 组码授权对检索**失效**(保留行做审计,不参与投影)④enqueue outbox。UI 必须展示**真实有效读者**(节点子树展开后的部门列表),而不是仅回显勾选项 —— 否则管理员看到的和实际生效的不是一回事。

**回滚:两个开关,不可合并**(修正)。原稿"关 flag 即 fail-closed"**不成立** —— 投影未收敛时,HA3 里可能仍是旧真实 owner、legacy `approved` 组码也可能仍在,关掉正向分支反而让这些**旧通道复活**。故拆成:

| 开关 | 作用 | 可否关闭 |
|---|---|---|
| `RAG_NODE_ACL_GRANT` | 正向:节点祖先链 OR 分支放行 | 可关(关 = 不再产生新的节点命中) |
| `RAG_NODE_ACL_ENFORCE` | 强制:**所有 node 模式的非 public 命中一律按当前 `acl_mode` + `kb_doc_node_grant` 权威复核** | **常开,不随正向开关一起关** |

⚠️ **"强制复核"推不出"全部拒绝"** —— 若用户仍被节点权威授权,`can_read_doc` 会返回 true。故判定必须写成**显式真值表**,`GRANT=false` 是**无条件 DENY**,不是"再去复核一次":

```
命中 = node 模式 且 非 public:
  ENFORCE=false              → 非法运行姿态,启动即失败(禁止 GRANT=true && ENFORCE=false)
  GRANT=false                → DENY(无条件,不调 can_read_doc)
  GRANT=true                 → can_read_doc(当前权威)
读 acl_mode / 节点权威失败    → 只留 public(fail-closed)
```

⚠️ **过渡期不能靠哨兵识别 node 命中**:`acl_mode` 不进 HA3,而未收敛的投影里旧 owner 还在、哨兵尚未写下 ⇒ 必须对**全部非 public 命中**批量查当前 `acl_mode`(可搭 `_revalidate_main_hits` 已有的 chunk_meta 批查一起做,不额外加往返)。
⚠️ node 模式启用后**禁用按日 legacy escape**(`config.py:1085-1115`),并要求 `RAG_ACL_FAIL_CLOSED=true`(`retriever.py:590-607`)—— 否则权威 DB 故障时不是只留 public。
**计划性回滚**顺序:逐文档切回 `legacy` → 恢复真实 chunk owner + legacy 投影 → 验证 HA3 收敛 → 最后才关强制复核。

⚠️ 强制复核**不能只覆盖 cross-dept 命中**。现状 `_deny_revoked_cross_dept` 仅挑 `owner_dept not in owner_set` 的命中复核(`retriever.py:537-543`),而主命中复核不比 allowed_depts(`:610-620,688-695`)、cosurface 补图独立查询后**根本不过主命中复核**(`:1898-1937,1989-2023`)。node 模式必须对**全部非 public 命中**(含邻居/step/cosurface/图片重签/本地 OpenSearch 回退)统一走 §2.2a 的 `can_read_doc`。

⚠️ **不做全量翻译**。理由:任一文档 ACL 投影变更 → 该文档全部 active chunk 标 `NOT_INDEXED` → stage-3 **全量经过 embedding 阶段并整文档重推**(HA3 同 PK = 整文档替换,不能局部 patch,`ha3-sdk-semantics` 第 2 点)。⚠️ 措辞精确化:**并非必然重新调用 embedding API** —— 命中 embedding cache 可复用向量(`pipeline_nodes.py:7200-7206,7231-7234`),只有 cache miss 才真调模型。但"全量重推 HA3"这部分不可避免,对 ~18k chunks 全量翻译仍是一次全量重建级操作。

**伞形策略不可自动翻译**(Codex 正确指出):`production` 伞形(9 子线)与 `marketing → production-family` 非对称共享是**业务策略**,不是组织结构的函数。双栈让它们保持现状即可;若将来要用节点表达,须业务显式给出映射(§7 待办 T3)。

---

## 6. 测试与验证

**必须新增**
- 祖先链解析:多直属部门并集、环/超深、部分失败 fail-closed、空输入。
- 过滤器:节点分支 flag 关时输出**逐字节等于历史**(沿用 Phase D 的等价性断言方式,`test_multi_dept_acl.py`)。
- 严格数字解析:注入串/超长/负数/非数字全部丢弃。
- 文档级不变量:同 doc 全 active chunk allowed_depts 一致(§3.2)——含邻居拼接/step 扩展的负例。
- cosurface + 图片签名路径的节点 ACL 正反例(`api.py:1934` 独立判定点)。
- 权威/投影:节点授权 → 物化 → 撤销 → reconcile 双向收敛;乱序 outbox + generation CAS。
- 组织变更:新增叶子自动继承(**零重推**即可见)、节点删除 → 孤儿授权检测。

**验证顺序**:`make test` + `make lint` → simulate(`make sim-all`)→ staging 实测(HA3 表达式上限 T1、真实成员匹配)→ 生产 flag 灰度。

---

## 7. 待办与未决(需 Sam 或实测裁决)

| ID | 事项 | 类型 |
|---|---|---|
| T1 | HA3 过滤表达式长度/OR 项/MULTI_STRING 元素上限 —— staging 实测锁定 §3.3 数值 | 实测 |
| ~~T2~~ | ✅ **2026-07-28 实测通过**(prod_ro 只读):`fuling_kb_chunks` IN_USE / **24 字段** / docCount **27484**;`allowed_depts` = **MULTI_STRING** ✔、`owner_dept` = STRING ✔ ⇒ **HA3 零 schema 变更的前提成立** | CLOSED |
| ~~T3~~ | ✅ **已裁决 2026-07-28**:取消特例,靠可见度多选中心级节点表达(生产类默认预勾**三个**中心:生产/营销/海外,见 §0);伞形/expansion 表在 node 模式下删除而非迁移(§0) | CLOSED |
| ~~T4~~ | ✅ **已裁决 2026-07-28**:归属=单节点、级联选择器、默认预勾进可见集(§3.5) | CLOSED |
| T5 | ⚠️ **由 T4 连带进入范围**(非可选),见 §3.5。✅ **管辖根已裁决 2026-07-28**:**按管理员自己所在部门自动取**(从 `staff_dim` 取其部门节点=管辖子树根,管自己部门及下属),Sam 只在例外时手动覆盖 ⇒ 数据模型须支持「自动派生根 + 手动覆盖行」两来源 | 实施项 |
| ~~T6~~ | ✅ **已裁决 2026-07-28**:**每日一次**同步(对齐夜间作业);快照 **>48h 告警并 fail-closed**(不再沿用陈旧祖先链);节点删除 → 孤儿授权**告警不自动删** | CLOSED |
| ~~T7~~ | ✅ **2026-07-28 完成**:schema **049 已 apply**(07-17 23:54);Sam 控制台确认 **`RAG_ALLOWED_DEPTS_ACL` = ON**、**`RAG_ACL_ANCESTRY` = OFF**。残留一条:DataWorks stage-3 节点侧是否同为 ON 待顺手复核(serving 管查得出、stage-3 管推得进,少一边即半条链) | CLOSED* |
| ~~T8~~ | ✅ **2026-07-28 实测通过**(钉钉只读):119 部门 / 树深 5 / 直属部门数最大 5 / **祖先链并集最大 8、P99 5、平均 2.9** ⇒ §3.3 上限已锁定为 8/15/32/32 | CLOSED |
| ~~T9~~ | ✅ **已裁决 2026-07-28**:**文字保留、图片拦截** —— 历史文字答案撤权后仍可见(用户本就已读过);但**图片重签 + 系统重投旧答案必须过当前 ACL**(T9-a 已是上线硬门槛)。涉及 `api.py:2175`→`content_blocks_builder.py:725`、`dingtalk_bot.py:2118,2161,1646-1676` | CLOSED |
| T10 | 聚合类派生数据按组码 cohort 分桶(热门问题 `api.py:2349`、改写池 `:2430`、知识缺口 `contribution.py:1165,1291,1460`)——节点 ACL 是**逐文档**的,同组码用户不再必然同可见范围 ⇒ 分桶口径需重定义 | 业务裁决 |

---

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 组织目录不可用/陈旧 → 误判可见性 | fail-closed 仅 public + 快照超期告警 + 同步 job 幂等重跑 |
| 过滤表达式超 HA3 上限 → 查询失败 | §3.3:**写侧 422 拒绝、读侧丢弃整个节点通道并告警**(均非"截断");T1 实测前不开 flag |
| **升版/re-chunk 复活 legacy owner** | §2.1b 单一 `project_doc_acl` + 所有写路径强制调用 + 升版后投影断言测试 |
| **哨兵值碰撞**(RDS 无 CHECK、owner 可从任意 raw 路径提取) | 保留值 `__acl_node_mode_v1__` + 上传/注册 guard + 上线前现网碰撞查询(T11) |
| 二次取回泄露 | §3.2 文档级不变量 + 负例测试;cosurface/图片签名同步改 |
| 节点删除 → 孤儿授权静默不可见 | 同步 job 检测孤儿 dept_id 并告警(不自动删授权) |
| 新旧双栈期语义混淆 | 两套值命名空间不相交 + 看板分别统计 + 逐文档迁移可回滚 |

**回滚**:见 §5 的**双开关**表 —— 关 `RAG_NODE_ACL_GRANT` 只停正向放行,`RAG_NODE_ACL_ENFORCE` **保持常开**使 node 文档非 public 命中全部被拒 = 真 public-only。legacy 文档全程不受影响(它们的组码通道未动)。⚠️ 不可只关一个总开关就宣称回滚安全 —— 那会让未收敛投影里的旧 owner / 旧组码通道复活。

---

## 9. 上线硬门槛(全部为 user-gated,缺一不可开 flag)

| 门 | 内容 |
|---|---|
| T1 | ⚠️ **只读半全绿 + 写入半 2026-07-29 部分完成**:冒号解析 ✅、OR 项 **≥4096 / 127KB 未见上限** ✅、32节点+10owner 组合形态 ✅ ⇒ **规模风险消除**;剩写入半(冒号值成员匹配),脚本 `scratch/probe_node_acl_t1_20260728.py` 待 Sam 授当日 ack |
| ~~T2~~ | ✅ 2026-07-28 通过(见 §9.0) |
| ~~T7~~ | ✅ 049 已 apply;`ALLOWED_DEPTS_ACL`=ON、`ACL_ANCESTRY`=OFF(见 §9.0d) |
| ~~T8~~ | ✅ 2026-07-28 通过(见 §9.0b):树深 5 · 直属部门数 ≤5 · **节点 OR 项数最大 8 / P99 5** ⇒ §3.3 上限已锁定 |
| ~~T11~~ | ✅ 2026-07-28 通过:现网 owner_dept 值域 13 个全为组码,`__acl_node_mode_v1__` 无碰撞 |
| — | staging 实证 `allowed_depts="d:123"` 中**冒号**的写入/查询/过滤解析(不能假定与纯字母同行为) |
| **T12** | **旧运维写工具封禁**(见下) |
| **T9-a** | **图片重签 + 系统重投旧答案**必须先过当前 ACL(业务裁决只保留"历史文字是否永久可见"这一项) |

### 9.0 2026-07-28 现网只读实测结果(prod_ro)

```
HA3 fuling_kb_chunks : IN_USE · 24 字段 · docCount 27484 · 分区 2
  allowed_depts    = MULTI_STRING   ✅ 设计承重假设成立
  owner_dept       = STRING(单值)   ✅
  permission_level = STRING          ✅
  多值字段全集 = allowed_depts / chunk_type / dense_vector / sparse_vector_indices / sparse_vector_values
RDS fuling_knowledge :
  schema/049 generation 列  ✅ 已 apply(2026-07-17 23:54)
  kb_access_request         空表(Phase D 至今零真实授权)
  chunk_meta 活跃 27484,allowed_depts 非空 0
  owner_dept 值域(13):production 18504 · hr 2145 · rd 2074 · marketing 1426 · it 934 ·
    quality 674 · finance 590 · admin 403 · pmc 385 · supply 249 ·
    production_paper_cup 52 · production_straw 27 · production_thermoforming 21
```

**三条对设计的直接含义**:
1. **HA3/RDS 完全对齐**(27484 = 27484),`allowed_depts` 已是 MULTI_STRING ⇒ §2.1「零 schema 变更、零重建表」**实证成立**。
2. **存量跨部门授权为 0** ⇒ 阶段 A 双栈**几乎零迁移负担**,没有历史组码授权要兼容。
3. ⚠️ **归属迁移成本高于预期**:伞形表列了 9 条子线,现网 chunk 只用到 4 条,**18504/27484(67%)挂在笼统的 `production` 上**;`production_injection`/`_mold`/`_blown_film`/`_carton`/`_pulp_molding` **一条 chunk 都没有**。⇒ 这 18504 条**无法自动推断真实车间部门**,归属改节点只能:①逐篇重选,或 ②**接受"归属=生产中心"粗粒度**(可见度不受影响——勾生产中心本就覆盖整棵子树)。**建议默认取 ②**,需要精确归属的再逐篇细化。

### 9.0b T8 组织规模实测(2026-07-28,钉钉只读)

```
部门总数 119(不含虚拟根)   一级部门 17
树深分布: L1=17 · L2=28 · L3=57 · L4=11 · L5=6      >>> 最大树深 5
一级部门子树规模(= dept_admin 管辖 IN 列表长度,含根):
  生产中心 73 · 综合管理中心 11 · 营销中心 8 · 品技中心 4 · 获胜包装 4 ·
  财务中心 3 · 研发中心 3 · 审计部 3 · 海外中心 2 · 其余 9 个各 1
                                                    >>> 最大子树 73 节点
员工去重 1175 人
直属部门数: 1个=1141人 · 2个=27人 · 3个=5人 · 5个=2人   >>> 最大 5
★ 祖先链并集大小(= HA3 过滤器节点 OR 项数):
   1项 97人 · 2项 266人 · 3项 585人 · 4项 142人 · 5项 83人 · 6项 1人 · 8项 1人
                          >>> 最大 8 · P99 5 · 平均 2.9
```

**四条含义**:
1. **过滤器规模不是问题**:节点分支最多加 8 项(P99 只有 5),与现有 owner 伞形展开(~12 项)同量级 ⇒ §3.3 上限敲定为 32,T1 的规模风险实质消失。
2. **dept_admin 管辖 IN 列表最坏 73 项**(生产中心),SQL `IN` 完全可接受,§3.5"查询时现算后代集、不做物化路径"成立。
3. ⚠️ **97 人(8%)直接挂在一级中心上**(祖先链只有 1 项)。这正是 §3.5 担心的场景 —— 若其中有人是 `dept_admin`,自动派生管辖根会**一次性给出整个中心的管理权**(生产中心 = 73 个节点)。§3.5 第 4 条"中心级根必须人工确认"由此获得实测支撑,**不是假想风险**。
4. ⚠️ **硬编码快照已漂移**:`_PRODUCTION_WORKSHOP_DEPTS` 存的是 85 个车间名,而生产中心子树实测只有 **73 个节点** —— 组织已变、快照没跟上。这恰恰是 node-ACL"改名免疫 + 新叶子自动继承"要解决的问题,也说明**继续维护名字快照的路走不通**。

### 9.0c T1 过滤表达式实测(2026-07-28,staging 只读)

```
表 fuling_kb_chunks_s,纯 query 零写入
[冒号解析]  allowed_depts="d:123"                    ✅ 解析通过
            多冒号值 OR / 冒号+组码混合                ✅ 解析通过
            完整形态(public + owner + 节点三分支)     ✅ 通过,正常命中
[规模]      8→4096 项 全部 ✅;4096 项 = 126974 字节仍通过 ⇒ **未见上限**
[设计形态]  32 节点项 + 10 owner 项 = 1446 字节        ✅ 通过
```

⇒ **§3.3 的 32 项上限有约 128× 余量**,过滤表达式规模**不构成任何风险**;T1 从"可能否决方案的门槛"降级为"确认一个字符能否存储"。

**写入半实测(2026-07-29,Sam 授权,staging 自清理,零残留)**:

| 断言 | 结果 |
|---|---|
| 冒号值**写入并读回**(fetch) | ✅ `stored allowed_depts = ['d:599318766','hr']` **逐字节保真** |
| 负例:前缀截断 `d:59931876` | ✅ 不误命中 |
| 负例:去冒号 `599318766` | ✅ 不误命中 |
| 负例:整串 `d:599318766,hr` | ✅ 不误命中(证明是成员匹配非整串) |
| 负例:未授权节点 | ✅ 不误命中 |
| **正例:`allowed_depts="d:599318766"` 命中** | ❌ **staging 上测不出** |

⚠️ **但正例失败与冒号无关** —— 决定性对照:**同一文档里的纯组码 `hr` 同样 0 命中**,而同表另一个 MULTI_STRING 字段 `chunk_type="step_card"` 过滤**正常返回 20 条**。settle 到 ~180s 仍不变。
⇒ 锁定为 **staging 表 `allowed_depts` 字段的索引状态问题**(Phase D Step 0 用 modify 加字段后未做索引重建;与"live sparse 倒排从未物理构建"同类),**不是值内容问题**。
✅ **生产表该字段可过滤有既有实证**:Phase D Step 3 端到端 round-trip(授权 marketing 读 quality 属主文档 → marketing 可见 / hr 不可见 / 撤销后收回),用的同样是实时 push。

**⇒ 2026-07-29 生产 disposable round-trip(Sam 授权 + 当日 PROD-RW)= 判定探针法到此为止**

先后在 staging(索引重建后)与**生产**各做一次性 round-trip,结果一致:`fetch` 保真、
**全部负例通过**、**全部正例 0 —— 包括同文档里的纯组码对照 `hr`**。

排查过程连错三轮,依次被排除:①冒号 ②staging 字段索引 ③`order="DESC"` 缺失
(F-20/G29,`retriever.py:1214` 有注释;补上后结果不变)。**最终只读隔离实验定位**:

```
生产真实文档(读):chunk_type="step_card"=20 · "procedure_parent"=20 · owner_dept="hr"=20
                  doc_id="<7-16 的真实 chunk>"=2/25 · fetch ✅
                  ⇒ MULTI_STRING 成员匹配与属性过滤在生产【完全正常】
刚推的探针      :fetch ✅(doc store)但任何 filter 皆 0
```

⇒ **根因 = HA3 实时推送的"可查询"延迟**:文档立即进 doc store(fetch 可见),但进入
**可过滤索引**有滞后(仓库既有记载:"freshly-pushed not yet queryable,需 settle ~120s +
double-pass";实测 90–180s 仍不可见,真实周期未知、可能以小时计)。**与冒号、与
`allowed_depts`、与索引重建全都无关。**

⇒ **探针法无法在分钟级闭环**,不宜继续做生产 round-trip。**T1 按分解证据结案**:

| 子命题 | 状态 |
|---|---|
| 冒号值可写入 MULTI_STRING 并逐字节读回 | ✅ 两表实证 |
| MULTI_STRING 成员匹配在生产可用 | ✅ 真实文档实证(chunk_type) |
| 过滤表达式接受冒号、规模无上限风险 | ✅ §9.0c |
| 三类误命中负例(前缀/去冒号/整串)+ `d:` 不串 `dx:` | ✅ 全绿 |
| **含冒号值的正例命中** | ⏳ 受实时索引延迟阻碍,**留待灰度金丝雀观测** |

**收口方案(取代继续探针)**:GRANT 灰度前,用**第一篇真实 node 授权文档**走正常 stage-3
管线,次日观测其可检索性 —— 这本来就是灰度必做的一步,把 T1 最后一环并入即可,零额外成本。
**若届时不命中**,退路已备且廉价:组码是固定 15 项白名单,换任何不碰撞的无冒号前缀
(如 `dept599318766`)只需改 `acl_policy.py` 两个常量,判定/投影/测试均不受影响。

**清理**:两次生产探针 fetch + `owner_dept="__zz_probe__"` 过滤双向确认**零残留**;
docCount 27486 vs 基线 27484 系 HA3 逻辑删计数滞后(下次 merge 收敛),属已知正常行为。

### 9.0f schema 059 + 060 已 apply 生产(2026-07-29,Sam 授权 + 当日 PROD-RW)

```
staging fuling_knowledge_stg : 059(1 条) + 060(24 条) ✅  存量 566 篇 acl_mode 全 legacy
生产    fuling_knowledge     : 059(1 条) + 060(24 条) ✅  台账 14:35:24 / 14:35:41
  document_meta 新列  acl_mode='legacy'(NOT NULL) · owner_dept_id=NULL · acl_revision=0
  ★ 存量 1938 篇文档 acl_mode 全为 legacy   kb_doc_node_grant 行数 0
  新表 dept_admin_node_grant / dept_dim / kb_doc_node_grant / staff_dim / image_funnel_verdict
  生产实跑 resolve_doc_acl → 全 legacy、节点集空 ⇒ 检索行为与 apply 前逐字节一致 ✅
```

⚠️ **apply 时遇到的两个门与正确处置**:
1. 本机不带 `RAG_ENV` 会解析到 **localhost**,必须 `RAG_ENV=production` 才指向生产。
2. production 安全守卫要求 `RAG_REQUIRE_AUTH`/`RAG_ACL_FAIL_CLOSED`(SAE 注入,本机
   `.env.production` 未声明)。正确处置 = **显式声明这两个安全值为 true**(与现网 07-23
   镜像实际姿态一致);**不得**用 `RAG_ALLOW_LEGACY_OPEN_PROD=ack:<date>` 豁免 —— 那是
   放宽姿态的令牌且属 Sam 专有。

### 9.1 T12 —— 可写生产投影的旧工具必须先封禁/迁移

这些工具**不在镜像里**(`.gitignore:22`/`.dockerignore:8`),但可从运维机持生产 RW token 直接写投影,**不能因"不在镜像中"而忽略**:

| 工具 | 危害 |
|---|---|
| `scratch/backfill_allowed_depts.py:59-69,71-101,115-130` | 按 legacy authority 把**组码**写回所有 approved 文档 ⇒ node 文档保留的 approved 审计行会被**重新投影成组码**,组码 OR 分支复活 |
| `scratch/repush_doc_phase_d.py:57-66,68-107,118-127` | 按 legacy authority 重算 allowed_depts 并**直推 HA3** |
| `scratch/export_jsonl_v2.py:62-87`、`scratch/tier3_sandbox.py:63-87,117-126` | 手工构造 owner、缺 allowed_depts |

处置:**删除、或加永久拒绝执行的 tombstone、或改调受控 `project_doc_acl`**。同时加 **CI 扫描**:任何新增的"直接写 `chunk_meta.owner_dept`/`allowed_depts`"或"直接构造 HA3 `cmd:add`"入口必须进 allowlist。

已核验**安全**的路径(只标脏/只删,不生成投影):`scripts/rebuild_from_rds.py:163-181`(标 NOT_INDEXED 后走 stage-3)、`scripts/reset_for_rechunk.py:89-104`(复位后走 stage-2)、`ha3_reconcile.py:272-276`(只删 orphan)。

### 9.2 DocAcl 正授权缓存

**默认不缓存正授权**(或跨进程版本号/极短 TTL)。仅"提交后本进程失效"覆盖不了多 worker + DataWorks 写入;沿用现有 deny cache 的保守姿态(默认 TTL=0 + 写端主动失效,`retriever.py:489-513`),并对撤销 SLA 做测试。
