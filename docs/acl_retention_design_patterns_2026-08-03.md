# ACL 感知检索 + 留存治理:美国企业系统设计模式对标(2026-08-03)

> **这不是产品选型报告。** 上一轮(`dms_landscape_research_2026-08-03.md`)已结论性证明无产品可替代自研控制台,本轮只做一件事:从美国企业系统里**提取可迁移的架构设计**,并逐条映射到本仓库的具体文件与已确认缺陷。
>
> 方法:5 组定向研究 → 逐组对抗核验。**153 条承重声明,136 confirmed / 2 unverified / 18 refuted**。所有结论落到一手源(官方 SDK 服务模型 JSON、官方文档 GitHub 内容源仓库、官方 proto/discovery、官方 OpenAPI)。文档站被代理 403 时改读其官方源仓库。

---

## 0. 三条改变判断的核验发现(必须先看)

对抗核验推翻了原研究的三处**陈述性框架错误**——API 事实本身无误,但若照原文做选型会严重误导:

1. **⚠️ Amazon Kendra 已进入维护模式,且对新客户关闭。** 自 **2026-06-30** 起不再开发新功能,自 **2026-07-30** 起不接受新客户,AWS 官方建议迁移至 Amazon Bedrock Managed Knowledge Base。今天是 2026-08-03,窗口**已关闭**。
   → 结论:Kendra 的 API 设计(`OrderingId`、`AccessControlConfiguration`)**仍是本轮最有价值的设计参考之一**,但绝不能作为选型候选。

2. **⚠️ Azure AI Search 的原生文档级 ACL/RBAC 能力全部是 preview**(`2026-05-01-preview`),不是 GA。涉及 `permissionFilter`、`x-ms-query-source-authorization`、`/resync {"options":["permissions"]}` 等本报告引用的全部关键机制。设计可抄,**不可假设其稳定性与 SLA**。

3. **⚠️ Google Vertex AI Search 已更名**:2026-04 起 Vertex AI 套件更名为 Gemini Enterprise Agent Platform,Vertex AI Search 产品面更名为 **Agent Search**。API/SDK/IAM 层仍是 `discoveryengine.googleapis.com`,故 proto 事实不受影响,但**检索官方文档或对外沟通必须用新名**,否则会漏掉现行文档。

另有 15 条精度性纠正(枚举漏项、"全部/永远"过度概括、把某定价档独有说成通用、把"未查到"当"不存在"),已在下文对应处以 ⚠️ 标注。

---

## 1. 对应 C3(reconcile 口径漂移 → 永久越权窗口)—— 本轮最高价值

**C3 回顾**:`allowed_depts_reconcile.py:89` 的 `_prescreen_unchanged` 只比两维(legacy want vs have),而唯一写实现 `materialize_doc_allowed_depts` 的 diff 是三维(node 投影 + owner 轴)。后果:**从未投影过的 node 文档** want=[]、have=[] → 判 unchanged → 永远跳过 materialize,越权窗口从过渡态变永久态。

**惊人的收敛**:五个互相独立的系统,都把"从未处理过"当作一等状态,**没有一个靠 diff 推断**。

### 1.1 核心洞察:`diff` 在"无上次结果"时恒为空——这是结构性缺陷,不是实现 bug

Azure 的同步矩阵逐字承认这一点:

> `ACL ingestion enabled on an existing indexer` → 自动检测:**否** → 需调 `/resync permissions` 以 **backfill ACLs for previously indexed items**

即 Azure 把"从未投影过"明确列为**增量路径在结构上无法覆盖的独立故障类**,必须有专门的 backfill 操作。这和 C3 是同一个问题,微软的处理是承认它、给专用通道,而不是指望 diff 覆盖。

### 1.2 五条可直接落地的修复模式

| # | 模式(来源) | 落到本仓库 | 优先级 |
|---|---|---|---|
| **P1** | **ACL epoch + "落后者枚举"原语**。Kendra `PutPrincipalMapping.OrderingId` 按调用方提供的单调 ID 做 last-writer-wins(不是按到达顺序),配套 `ListGroupsOlderThanOrderingId` = **官方内置的陈旧权限检测查询** | 给 `kb_doc` / `kb_doc_node_grant` 加单调 `acl_epoch`,materialize 时写进 `chunk_meta`;预筛条件改为 `chunk_meta.acl_epoch < 源表 acl_epoch OR chunk_meta.acl_epoch IS NULL`。**这个谓词天然覆盖"从未投影过",不会退化** | **最高** |
| **P2** | **NOT_UPDATED 是独立一等状态**。Coveo `SinglePermissionState` 把 `NOT_UPDATED` 与 `UP_TO_DATE`/`OUT_OF_DATE` 严格分开,支持按状态过滤列举 | `chunk_meta` 加 `acl_state` 列;预筛**仅对 `UP_TO_DATE` 生效**,`NOT_UPDATED` 永不进 unchanged 集。这是独立于 P1 的第二道防线——即使口径再次漂移也不会永久误判 | 高 |
| **P3** | **skip-unchanged 判据缺信息时默认"重跑"而非"跳过"**。Elastic 写法 `doc_not_updated = TIMESTAMP_FIELD in doc and last_ts == doc[TIMESTAMP_FIELD]` —— 源没给时间戳直接导向重写 | `_prescreen_unchanged` 改为:只在能**完整复现 materialize 全部分支**(ACL mode 判定 + `project_doc_acl` + owner 比对)时才允许判 unchanged;任何模式识别不出/字段缺失/行形状异常一律判 dirty | 高 |
| **P4** | **权限刷新拥有独立于内容变更的作用域谓词**。Azure `/resync {"options":["permissions"]}` 作用域是**整个数据源**(不是 delta),刷新内容标注为 "Only ACL/RBAC metadata (content is left untouched)",与走内容谓词的 `/resetdocs` 是两条完全分离的通道 | **修复方向不是去对齐两个谓词,而是让 permission-sweep 拥有自己的谓词,默认作用域"全库"**。这是对 C3 最根本的架构性回答 | 高 |
| **P5** | **共用同一个口径计算函数**。OpenText `rm_metadataToken`:读接口返回、写接口必传的乐观并发令牌 | 给 `document_meta` 加 `acl_projection_token`(源侧字段规范化哈希),reconcile 只比 token,NULL token 一律 dirty。**关键约束:token 计算必须由 materialize 与 reconcile 共用同一函数,否则只是把漂移换个地方** | 中 |

### 1.3 附带发现:节点级 ACL 有同类结构性漏洞

Azure SharePoint 矩阵:**父级作用域(容器/目录/站点)的权限变更不被增量路径捕获,是官方承认的设计边界而非 bug**——有唯一权限的单项变更用 change token 增量捕获,父作用域继承变更**必须**显式 `/resync permissions`。

→ 对应阶段 B 的 `kb_doc_node_grant`:**组织树节点授权变更 = 父作用域变更,增量路径必然漏**。应为"节点授权变更"单独接一条全量 permission-sweep,而不是指望文档级 outbox 事件覆盖。

### 1.4 两条运维侧配套

- **对账结果按状态分桶,不只报总数**(Coveo `numberOfEntitiesByState`)。现有 `reconcile_allowed_depts` 返回 `{approved, materialized, retracted, unchanged, ...}` —— **缺 `never_projected`,而这正是最该报警的数**。加进返回值 + `ops_monitor.py` 告警 + `eval_harness/layers/l5_permission.py` 硬断言。
- **撤销方向带占比熔断**(Glean:陈旧删除比例 >20% 时挂起删除 7 天,需显式 flag 才执行)。
  ⚠️ **重要限定**:熔断会制造"授权已撤销但索引仍可见"的机密性窗口,因此**只能用于 reconcile 全扫路径,绝不能用于 outbox 定向 drain**(后者是明确的单点撤销意图,必须无条件执行)。两条路径的写入必须在指标上可区分。

### 1.5 ⚠️ 必须先测出的一条语义(决定 C3 优先级)

> `chunk_meta.allowed_depts IS NULL` 在检索侧的**确切**语义是什么?

Elastic 的反面教材:内容文档无 access control 字段 ⇒ **无任何访问限制**;role 省略 query 参数 ⇒ 整条 index 的 DLS 被禁用;多角色中任一角色不带 DLS ⇒ DLS 全部失效。Kendra `USER_TOKEN` 模式同款:"All documents with **no access control** ... will be searchable and displayable"(无 ACL 即公开)。

**这条语义是 C3 究竟是"数据泄露"还是"数据不可见"的唯一分水岭。** 若现状是 fail-closed(NULL ⇒ 仅 owner_dept 可见),C3 后果是可用性问题(管理员勾了节点、文档永远搜不到),优先级可下调一档——但仍须修。**建议:先写测试固化这条语义,再排优先级。**

配套原则(Azure):ACL 字段用**显式三态**——`["all"]`=公开、`["none"]`/`[]`=拒绝、**禁止用"字段缺失"表达任何语义**;并**禁止用负向条件(NOT IN/!=)表达权限**,否则未投影文档会被放行(Bedrock 的 `notEquals` 就明确对"文档中不存在该 key"判为通过,是 fail-open)。

---

## 2. 对应 C5 / C6(retention 两条缺陷)

### 2.1 C5:归档 fail-closed 与调度配置冲突 → 两张关键治理表在 commit 模式必然删除失败

**OpenText 的答案:归档与删除是处置链上的不同 stage,各自独立成败。** RSI stage `action_code` 枚举(⚠️ 核验纠正:共 **11** 项,原研究漏了 `16 Make Rendition`):

```
0 None · 1 Change Status · 7 Close · 8 Finalize Record · 9 Mark Official
10 Export · 11 Update Storage Provider · 12 Delete Electronic Format
15 Purge Versions · 16 Make Rendition · 32 Destroy
```

`Export(10)` 与 `Delete Electronic Format(12)`、`Destroy(32)` 分属不同 stage,**归档失败只让对象停在归档阶段,不阻断也不牵连其他表的处置**。

→ **修复方向**:把 `retention.py` 的 `_archive_batch` 从"删除的同步前置"拆成 `*_archive` 与 `*_delete` 两个独立作业。
⚠️ 拆开后必须保证 delete 阶段**只删已登记 id**,否则失去 fail-closed 的原有保护。

**第二条**:归档目标(Location)在 OpenText 是**必须先定义、被留存计划引用的一等配置对象**——缺失属于**配置期错误**而非运行期错误。
→ 把"`RAG_RETENTION_ARCHIVE` 与 `RAG_SIMULATE_OSS` 互斥"提升为 `run_retention()` 的 **preflight 断言(dry-run 也执行)**,并在 `dataworks_nodes/retention_node.py` 顶部加启动期断言。这样 C5 会在**配置时**暴露,而不是等翻到阶段 2 才每天爆。
⚠️ preflight 会让 dry-run 在无 OSS 的本地/staging 失败,需给非生产环境显式的 `RAG_RETENTION_ARCHIVE=false` 出口。

### 2.2 C6:purge 删锚点表制造永久孤儿

**三个系统给出三条互补答案:**

1. **Laserfiche:永不删除定位锚点。** 内容销毁但**保留元数据墓碑**(metadata retained by default),官方理由是"证明记录曾被正确保留";版本清理有 `minNumVersionsToKeep >= 1` 的硬下限。
   → **修复方向**:`purge_subject` 的 `qa_session_log` 硬删改为**就地墓碑化**——`user_id` 置 `'purged:' || left(sha256(user_id),16)`、内容字段置 NULL,**保留 `message_id` 与 `created_at` 作为 `qa_retrieved_doc` 的永久锚点**。
   ⚠️ 墓碑化是否满足 PIPL 第 47 条的"删除"**需法务确认**;若必须硬删,则锚点删除降级为**独立第二轮作业**,前置条件是所有依赖表 `deleted == affected` 且 `not capped` 且无 error。

2. **Azure:删除的顺序律是硬规定。** "先运行 indexer 删索引文档,索引更新后才物理删源",否则产生 orphan documents;且**删除检测策略必须从第一次运行就启用,事后补策略救不回历史孤儿**。
   → 把顺序律从注释**升级为显式的删除拓扑声明**(表→依赖表有向图),新增关联表时强制补拓扑位置并由测试断言。同时适用于 `ha3_reconcile.py:79` 的 G3 守卫。

3. **OpenText:批处理结果七分类,不是布尔标志位。** `Abort/Fatal/Fail/Requeue/Retry/Cancel/Complete` + 明确的 retry limit(3) / requeue limit(5),逐条独立事务。
   → `retention.py` 现在的 `for _b in range(max_batches)` + `rep['capped']` / `rep['error']` **把"表不存在""归档失败""打满上限"混成同一条路径,且失败后循环继续**——这正是 C6 的直接机制。应映射为 **Fatal / Fail / Requeue 三种不同结果,并据此决定是否继续下游表**。

### 2.3 retention 的三条增量建议(非缺陷,但值得做)

- **资格判定与删除执行拆成两个阶段**(OCP 的 `qualification_date`):定资格作业把"已到期"写成对象上的持久化戳,执行作业只处理带戳对象。现在 count 与 act 共用同一 SQL 谓词、每次现算、无持久中间态。
  ⚠️ 只对真删的 `qa_rows`/`audit`/`findings` 采用;`qa_blobs` 这类"置 NULL"的轻动作不值得。
- **删除前必须有人工审批记录,且是可 GET/PUT 的独立子资源**(`{approver, approved_date, approved_state}`),不是审计流水里的一行文字。现在只有 `RAG_RETENTION_ENABLE` 环境变量级双闸,**没有任何面向人的"待处置审批"**。可直接复用钉钉审批(已有免登+组织架构)。
  ⚠️ 必须是每批次/每主体粒度,不能做成一次性开关。
- **破坏性动作要求结构化理由码 + 执行者归属**。`purge_subject(user_id, commit)` 无 `reason`/`operator`/`ticket_id` 入参,且擦除动作**本身未写入 `kb_audit_log`**。
  ⚠️ 擦除动作的审计记录必须**豁免于 audit 留存作业**——否则 24 个月后连"擦过谁"都查不到。

### 2.4 全新能力:legal hold(目标系统完全没有)

**Google Vault 的并集语义**(原文可引):对象只有在**没有任何 hold 且没有任何 retention 规则**覆盖时才可被清除(`holds.delete` 原文:"If the data is not preserved by another hold or retention rule, it might be purged")。

**OpenText/Laserfiche 的建模**:hold 是独立一等对象(带 mandate/理由/类型),多对多挂到目标上,**hold 不修改留存到期日**;删除执行时逐条查冻结、被冻结者跳过、其余继续;解冻不删 hold 行,只置 `ActiveHold=false` 并记 `DateRemoved/RemovalPatron/RemovalComment`。

→ 建议新增 `kb_retention_hold` + `kb_retention_hold_target`,在 `run_retention()` 与 `purge_subject()` 两个入口 fail-closed 检查(与既有"删前冷归档 fail-closed"同款纪律)。
⚠️ **引入 hold 会与 PIPL 个人信息删除权直接冲突,优先级必须由法务书面确认,不能由工程默认。** 冻结检查若做成 SQL JOIN 会拖慢批删;可先支持粗粒度(按 user_id / doc_id / 全局)。

⚠️ **核验纠正**:Google Vault **无 retention rule 端点**(顶层资源仅 `matters`/`operations`),但冲突解析规则有官方明文:自定义规则一律压过默认规则(**即便自定义期限更短**);多条自定义规则命中时按结束最晚者保留;Drive 另有例外——只有该条目所有者的留存规则能以 action 使其过期。

⚠️ **另一条纠正**:Laserfiche 的"删除前强制选理由码"能力属 **Advanced Audit Trail 付费档**,不是通用能力。

---

## 3. 对应 C8(签名 URL TOCTOU:审批放行的内容 ≠ 实际入库内容)

这条是本轮的**意外收获**——Google Drive 给出了教科书式的答案:

**审批即冻结内容。** `approvals.start(lockFile=true)` 在**发起审批时直接锁文件**;配套 `contentRestriction{readOnly, reason, restrictingUser, restrictionTime, systemRestricted}`,其中 **`systemRestricted` 由系统施加、用户不可移除**。Box 对应物是 `FolderLock{move, delete}`。

→ **对应 C8**:目标系统 上传→注册→审批→发布 链路中,审批与内容未绑定,审批通过后内容可被改写(30 分钟签名 PUT URL 仍有效),**发布的未必是被批的那份**。
**修复方向**:审批单固化 `version_no` + 内容哈希,审批期该版本禁写;`quarantine` 用"系统施加、管理员常规路径不可解除"的锁语义(对齐 `systemRestricted`)。
⚠️ 锁期间的紧急修订需要一条显式的"撤回审批→解锁→重新提交"路径,否则会把编辑流程卡死。

**顺带**:审批状态应是"每审批人一行 + 法定人数规则"而非单一 status 字段——Box `Task.completion_rule(all_assignees|any_assignee)` + `TaskAssignment.resolution_state`;Google `Approval.reviewerResponses[]` + `dueTime`,且 **reassign 只能新增或替换、不能移除**,所有动作(含评论)进 activity log。
→ `kb_access_request` 与文档审批当前是单一 status,无法表达会签进度。
⚠️ 钉钉审批卡片要同步支持多人独立表态;**不要在没有超期提醒的情况下引入 `all_assignees`**(会大量卡单)。

---

## 4. 其他高价值模式(按可落地性排序)

### 4.1 立刻可做(小改动、高收益)

| 模式 | 来源 | 落点 |
|---|---|---|
| **纯 ACL 变更不应触发重嵌入** | Glean `/updatepermissions`("without modifying document content");Pinecone `setMetadata`、Vectara `update_metadata`、LlamaCloud 独立 `METADATA_UPDATE` step | `materialize_doc_allowed_depts` 方向已对,**但下游 stage-3 drain 会重解析 authority → 重嵌 dense+sparse → cmd=add**。一次纯权限变更触发完整重嵌入。<br>⚠️ **必须先验证 HA3 是否支持部分字段更新**,不支持则收益归零;还要验证部分更新不会重置/丢弃向量列(Azure 有 `stored=false` 时向量被丢弃的反例) |
| **授权带 TTL** | Google `Permission.expirationTime`(仅 user/group、必须未来、最长一年)与 Box `Collaboration.expires_at`——**两家独立收敛到同一决策** | `kb_doc_node_grant` / `kb_access_request` 批准时强制写入 授予级别 + 到期时间,到期由 reconcile 自动回收(正好复用全扫兜底,回收是它最擅长的方向)<br>⚠️ 到期判定必须放在 **materialize 口径内**而非只在预筛,否则出现"DB 已过期、索引仍可检索"的反向漂移 |
| **权限解释器做成一等 API** | Glean `/checkdocumentaccess` → `{hasAccess: bool}`;`/debug/{datasource}/user` → `{isActiveUser, uploadStatus, lastUploadedAt, uploadedGroups}` | 你已有 `GET /api/kb/visibility-explain`,**方向正确**。可扩展为输入 `(user_id, doc_id)` 输出 `{allowed, 判定路径, 用户部门集来源, 文档 allowed_depts 现值, chunk 投影时间, 与 authority 是否一致}`,并喂给 `eval_harness/layers/l5_permission.py`<br>⚠️ 该端点是高价值攻击面(可探测他人权限),必须限管理员/审计角色并全量写 `audit_log.py` |
| **服务端绑定过滤器,禁止 LLM 决定权限** | Vectara Agent `EagerReference($ref: session.metadata.filters.user)` 在每轮开始、**LLM 处理之前**解析过滤器 | 钉钉机器人/小程序链路:用户部门集合应在会话开始时由免登身份解析并**冻结成本轮过滤器**,禁止让 LLM 或工具调用参数决定权限过滤(防 prompt injection 改写过滤器)<br>⚠️ 会话内调岗本轮不生效,需定义权限快照有效期 |

### 4.2 值得做(需设计)

- **越权调试通道**(Azure `x-ms-enable-elevated-read: true`,需独立原子权限):对比"某用户视角可见集"与"全量集"以定位过滤错误。→ 控制台加"以指定用户身份模拟检索"与"无过滤全量检索"的**双视图 diff 页**——这正是发现"从未投影过的文档越权可见"这类缺陷的**唯一有效手段**。必须单独授权 + 写审计。
- **关闭旁路读取通道**(Google:`acl_enabled=true` 时**直接禁用** `GetDocument`/`ListDocuments`,且 `acl_enabled` 是 **IMMUTABLE** 不允许事后降级):检索侧做了 early-binding 过滤,但若管理台/运维脚本能直接查 RDS 或 HA3 原始文档,过滤形同虚设。→ 审计所有非检索路径的文档读取入口,统一挂同一套权限判定。
- **派生物权限传播必须显式**(Box `copyInstanceOnItemCopy`;Google Labels `copyMode`):RAG 管线派生物极多(`chunk_meta`、`embedding_cache`、`card_templates`、QA 缓存)。
  ⚠️ **embedding 与向量缓存一旦跨权限复用就等于绕过 ACL**——这条风险高于收益,应做成**"派生物必须继承且不可放宽"的硬约束**,而不是可配开关。
- **留存策略的不可变等级与单向退休**(Box `retention_type=non_modifiable` 只许延长不许缩短、不许删 assignment;`status=retired` 后不可复活;Shield barrier 有 `invalid` 态即配置不一致时**显式暴露而非静默失效**)。
  → `retention.py` 的留存窗口来自 `env(_months(...))`——**改小一个环境变量即可悄悄缩短合规期且不留痕**。至少让"缩短窗口"变成需显式审批 + 审计事件的操作。
  ⚠️ 折中方案:env 仍可放宽,缩短则强制走审批 + 告警。

### 4.3 记一笔(暂不做)

- **世代化 chunk key**(Azure index projections:projected key = 随机 hash(父文档每次更新即变) + 父 key + 注解路径;父更新时存活子文档也被重写 key,消失的子文档被删)。→ 对应 2026-06-15 事故(同版本 v3→v3 重灌时旧 PK 成孤儿)。
  ⚠️ HA3 主键是 int PK 不能直接塞 hash;可行做法是 `chunk_meta` 加 `gen` 字段投影为可过滤属性,按 `(doc_id, gen != current)` 批删——**需确认 HA3 支持按非主键条件删除**。
- **LSN 读己之写**(Pinecone `x-pinecone-lsn-committed` / `x-pinecone-lsn-reconciled` + `is_reconciled(target)`)。→ 可把"越权窗口"从经验值变成**可断言、可告警的量**。⚠️ 需 HA3 侧能暴露单调水位。
- **分段配置哈希**(LlamaCloud 拆 `embedding_config_hash`/`parsing_config_hash`/`transform_config_hash` 并记到每个文件):改切分参数只重跑切分与嵌入,改解析器才回 OSS 重解析,改 embedding 模型才全量重嵌。⚠️ 需一次性回填历史文档哈希。
- **前置过滤保证 top-k**(Azure 明确 `preFilter` "guarantees that k results are returned if they exist in the index",postFilter 对高选择性过滤器产生 false negatives,官方建议避免)。→ 可作为"不得改为召回后过滤"的正式依据;对小部门 / `d:<id>` 节点授权增加**召回充足性监控**。⚠️ HA3 过滤实现与 HNSW prefilter 不完全等价,需实测。
- **审计日志工程约定**:复合去重键(Google `Activity.id = {time, uniqueQualifier, applicationName, customerId}`)、至少一次投递 + 消费端按 id 去重、**双通道读同一份数据**(Box `admin_logs` 历史 vs `admin_logs_streaming` 实时)、敏感正文默认不返回、**代理行为可识别**(`actor.applicationInfo.impersonation`)。
  → 特别是最后一条:**钉钉机器人与 RAG 服务账号代用户检索时必须落 impersonation 标记**,否则事后无法区分"用户自己查的"与"机器人代查的"。⚠️ 标记要在钉钉免登链路**最外层**注入,补在下游会漏。
  → 另:你现有的 outbox(实时)+ 全扫 reconcile(补齐)**正是 Box 双通道的同构**,应把两者语义差异写进文档而非让调用方猜。

---

## 5. 一条正面背书

Azure 明确规定:**权限元数据必须投影到每个 chunk**——"Every chunk must inherit them for query-time permission filters to apply"。且 **Kendra 的 RAG 专用形态(Gen AI Enterprise Edition)只支持 `ATTRIBUTE_FILTER`**(扁平字符串属性过滤的 early-binding),用 `USER_TOKEN` 直接报 `ValidationException`。

→ **你的 `allowed_depts` 投影到 `chunk_meta` 的设计与 Azure 同构,且是两大云在 RAG 场景下的收敛选择。架构方向是对的,问题只在收敛机制(C3),不在模型选择。**

可补齐的一点(Azure 是引擎内建,你是应用层注入):检索入口应在服务端由钉钉身份解析出部门集合并**强制注入**过滤条件,任何来自前端/LLM 的过滤参数**只能收窄不能放宽**。建议单一注入函数 + 静态检查,保证没有绕过该注入点的检索路径。

---

## 6. 建议落地顺序

**第 0 步(前置,决定后续优先级)**:写测试固化 `chunk_meta.allowed_depts IS NULL` 在检索侧的确切语义(§1.5)。这一条决定 C3 是泄露还是可用性问题。

**第 1 批(修 C3,~1 个 schema 迁移)**:P1 `acl_epoch` + P2 `acl_state` 两列一次迁移到位;预筛谓词改为覆盖 NULL/落后(P1);`never_projected` 进 reconcile 返回值 + 告警。
⚠️ 回填口径要小心,否则等于把当前错误状态固化。**建议:对 `kb_doc_node_grant` 有授权但 `allowed_depts IS NULL` 的行强制标 `NOT_UPDATED`。**

**第 2 批(修 C5/C6)**:retention 归档/删除拆作业 + preflight 断言(C5);`qa_session_log` 墓碑化或依赖门(C6,**需法务先确认墓碑化是否满足 PIPL**);批处理结果三分类。

**第 3 批(修 C8)**:审批单固化 version_no + 内容哈希 + 审批期禁写。

**第 4 批(增量)**:授权 TTL、纯 ACL 变更轻量路径(需先验证 HA3 部分更新)、越权调试双视图、legal hold(需法务)。

---

## 附录:核验状态

**136/153 条 confirmed**,来源包括:`botocore` 的 Kendra 服务模型 JSON(API Reference 的生成源)、`MicrosoftDocs/azure-ai-docs` 官方内容源仓库、`googleapis/googleapis` proto + discovery(rev 20260724)、Glean `indexing-capitalized.yaml`、`opentext/pyxecm` 源码、M-Files/Laserfiche 官方文档、Box/Google/Notion 官方 OpenAPI。

**18 条 refuted**,分四类:
1. **陈述框架错误**(3 条,见 §0):Kendra 维护模式、Azure preview、Google 更名。
2. **过度概括**:Glean"全部 delete 请求带 version"(`DeleteTeamRequest` 没有)、M-Files"已签入版本**永远**不能销毁"(原文主语是 *This endpoint*,非系统级不变量)、Bedrock"四个 API **均带** clientToken"(Get/List 没有)。
3. **漏掉定价档限定**:Laserfiche 删除理由码属 Advanced Audit Trail 付费档。
4. **把"未查到"当"不存在"**(5 条,均为可检索到却报未查到):Notion 审计日志官方开发者文档存在、版本历史保留期分档明确(Free 7 天/Plus 30 天/Business 90 天/Enterprise 不限)、Google Vault 冲突解析规则有官方明文、Box 中国区有官方替代域名方案(`boxcn.net`/`boxenterprise.net`,真实障碍是 reCAPTCHA 在华不可用)、Pinecone 最终一致性措辞在官方 SDK 集成测试中有 20+ 处一手表述。

**2 条 unverified**:Google 叙述性文档(`about-acls`、NDJSON `aclInfo` 摄取)全程 403 不可达,Google 侧结论仅覆盖 API 契约可证明的范围。
