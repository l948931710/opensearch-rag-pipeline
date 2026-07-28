# `claude/ontology-p0` 补充代码与系统审查

审查日期：2026-07-10（America/Los_Angeles）  
仓库：`l948931710/opensearch-rag-pipeline`  
目标提交：[`3c339fe`](https://github.com/l948931710/opensearch-rag-pipeline/commit/3c339fe136cd1834ce3735d7219a88bd3292215f)  
正确父分支：`claude/enterprise-agent-plan-6e9072`，基线 `17fb00a`  
差异：11 commits、39 files、`+6679/-8`；[compare](https://github.com/l948931710/opensearch-rag-pipeline/compare/claude/enterprise-agent-plan-6e9072...claude/ontology-p0)

> 本报告是此前 `main` 全面审查的分支补充。既有系统级 P0（公开仓库中的内部数据/拓扑、生产链路明文 HTTP、匿名检索、ACL 权威读取 fail-open、HA3 文档蒸发监控未调度）并未被本分支关闭，故不在下文重复展开，但仍然计入最终生产结论。

## 1. 结论先行

**结论：NO-GO。** 当前提交不应合并到父 Agent 分支，不应应用到共享 staging/production，不应开启 `RAG_ONTOLOGY_ENABLE`，不应执行真实播种/回填，也不应把两个 Ontology 工具接入默认 Agent registry。

可接受的范围只有：继续本地开发、空库/一次性本地 MySQL 验证、默认 dry-run、保持所有 flag 为 off。

| 决策面 | 结论 | 原因 |
|---|---|---|
| 继续本地开发、纯 dry-run | **GO** | `resolve()` 纯读、工具尚未接线、flag 默认关闭，安全边界尚可控 |
| 合入 `claude/enterprise-agent-plan-6e9072` | **NO-GO** | 对象 ACL、身份生命周期、事务一致性、审计链均存在 P0 |
| 直接合入 `main` | **NO-GO** | 分支设计本就声明不直上 main；相对最新 main 仍 diverged（ahead 20 / behind 2） |
| 027–030 应用共享 staging | **CONDITIONAL NO-GO** | 仅一次性隔离库可试；迁移工具、授权模型、真库 CI 尚未过门 |
| 生产 apply / 开工作台 | **NO-GO** | `fuling_ro` 可绕过所谓唯一读取口；工作台跨部门泄露与越权写成立 |
| 真实播种 / DataWorks 回填 | **NO-GO** | Phase 0 四项签字无证据，auto 阈值可被错误配置放开，写入非原子 |
| Agent 工具注册 | **NO-GO** | approval/audit 不能回链到实际 identifier，目标对象 ACL 与版本未绑定 |

## 2. 已确认的优点

这些方向是对的，应保留：

- `OntologyResolver.resolve()` 做到了纯读，在线 miss 不建 case、不落 alias；持久化入口被限定为播种、回填、工作台、受治理 Action。
- `ontology_identifier` 用生成列唯一键表达“同 namespace + norm 至多一个 active”，`resolution_case/candidate` 把候选与正式映射分开。
- embedding 被构造性禁止 auto-activate；改模后缀默认落 HITL 区间。
- 工作台、回填、Agent 工具均默认关闭；两个 Agent 工具刻意未接入默认 registry。
- 播种与回填默认 dry-run；`U8SnapshotSource` 在 T-1 diff 结论缺失时显式 `NotImplementedError`，没有伪造 U8 表结构。
- Agent 写工具声明 `approval_policy="always"`，审批请求持久化与职责分离框架已经存在。
- 前端实现可构建，单测完整通过；下文问题主要是后端授权语义和治理闭环，而不是页面无法运行。

## 3. 验证证据与限制

### 实际完成

- 通过 GitHub 读取并逐文件审查正确父分支上的 39 文件差异，同时按远端提交内容重建临时审查副本。
- Python `compileall`：通过；仅出现一条本分支外既存的正则转义 `SyntaxWarning`。
- 自定义动态对抗探针复现 5 个失效：
  1. 对象退役后，active identifier 仍被 resolver 返回为 `resolved`；
  2. `product` 可以被标记为合并到 `material`；
  3. 空 ACL 仍收到 internal SKU 的 object_id、canonical_ref 和标题；
  4. 负数 τ 阈值被接受并允许 auto；
  5. 并发 alias 赢家出现后，本次已铸对象成为永久孤儿，重跑只会 skip active alias。
- 前端全量 Vitest：**26 files / 226 tests passed**；Ontology Workbench 专项 **13/13 passed**。
- 前端生产构建：通过；`npm audit`：0 vulnerabilities。
- 简单敏感信息模式扫描：只命中测试合成 AccessKey，无新明文密钥证据。

### 未形成的证据

- 目标 HEAD 没有 PR，也没有 GitHub Actions workflow run / commit status。
- 当前审查运行时没有 pytest，外网安装被沙箱策略阻止，因此本地后端 pytest 未执行。不能用 `compileall` 替代 pytest 结论。
- 更关键的是，即使开 PR，现有 `db-integration` job 只跑 4 个旧模块，并不运行 `test_ontology_store.py`、`test_ontology_sem.py` 或 `test_routes_ontology.py`；这些测试的 RDS 参数在普通 simulate job 会 self-skip。因此当前 CI 设计也不能证明 Ontology 的 MySQL 事务、collation、视图与并发语义。
- Phase 0 的组织签字、GT 数据集、steward 排班/SLA、U8 T-1 diff 结论均不在仓库证据中。

## 4. P0 阻断项

### P0-01 — 工作台没有对象级读 ACL，且写授权只校验一侧 scope

证据：`routes/ontology.py:74-106,119-187,198-237,273-382`。

- 任意一个 `dept_admin` 通过 `_require_reader()` 后即可读取全局 workbench、任意 case 详情、全部对象搜索和对象详情。
- 返回内容包含 `evidence_json`、候选标题、`golden_json`、完整 identifier 列表；未按 `managed_owner_depts`、对象 `owner_dept` 或 `data_classification` 过滤。
- confirm/repoint 只按 case/旧 identifier 的 namespace 或 object_type 找 steward，不要求调用者同时有权读取、管理目标对象。
- confirm 不验证 `object_type_hint == target.object_type`；mark-duplicate 不验证源/目标同类型，也不验证目标部门授权。营销 steward 因而可以把客户编号映射到供应链 confidential material，或把 product 合并到 material。

影响：跨部门数据泄露、语义脊柱污染、目标对象越权写。feature flag 只能隐藏入口，不能在开启后提供隔离。

验收：

1. 建立唯一的 `can_read_object(ctx, object)` 与 `can_mutate_identity(ctx, source_scope, target)`，供 workbench、tools、sem 共用；默认 fail-closed。
2. public 可见；internal/confidential 仅对象归属 ACL 或显式 break-glass `kb_admin` 可见，break-glass 必须强审计。
3. 队列在 SQL 层按可管理 scope 过滤；case evidence、候选、对象搜索、详情全部执行同一过滤/脱敏。
4. confirm/repoint/merge 同时校验来源 scope、目标对象 ACL、对象类型、状态和允许的关系；跨类型默认拒绝。
5. 加入“存在性不可泄露”矩阵测试：employee、无关 dept_admin、相关 dept_admin、kb_admin × public/internal/confidential × source/target 两侧。

### P0-02 — `sem.py` “唯一读取口”在真实 DB 授权下不成立，且空 ACL 仍泄露 SKU 元数据

证据：`schema/030_sem_views.sql:7-10,26-88`、`docs/environment_design.md:220`、`ontology/sem.py:55-70,111-154`。

- 030 只写注释“sem_* 不授 fuling_ro”，没有任何可执行的 REVOKE/GRANT 或独立 schema。
- 环境规范明确给 `fuling_ro`：`GRANT SELECT ON fuling_operation.*`。库级通配 SELECT 会自然覆盖新建的 sem 视图和 ontology 基表，任何持有该账号的脚本/NL2SQL 都可绕过 `sem.py` 行过滤。
- `lookup_specs()` 只过滤 spec 行；对象已解析时，无 ACL 仍返回 internal SKU 的 object_id、canonical_ref、object_type 和标题。工具层同样只遮 confidential 标题，并把 canonical_ref/object_id 当“无害代理号”。这不满足对象级 ACL。

验收：

1. 把受限语义投影放到未授 `fuling_ro` 的独立 schema，或取消 DB 级 wildcard grant，改为逐表最小权限账号。
2. 建立独立 `ontology_reader` / `ontology_writer` 账号；读取服务不能直查未过滤基表，写服务不复用通用 admin 连接。
3. `SHOW GRANTS` 自动化测试证明 `fuling_ro` 对 sem_* 与 ontology_* 直接 SELECT 得到 1142。
4. 在任何对象字段出参前先做对象 ACL；拒绝时不返回 ID/ref/title/type，也不区分不存在与不可见。

### P0-03 — active identifier 可以指向缺失、retired 或 merged 对象，生命周期不闭环

证据：`schema/027-029` 无外键；`ontology/resolve.py:201-218,240-257`；`ontology/store.py:171-179,223-249`。

- exact resolve 看到 active identifier 后直接返回 `resolved`，不检查目标对象是否存在、是否 active、是否已 merged。
- retire/mark_duplicate 只改对象状态，不处理 active identifiers、links、open cases，也不提供受治理的 redirect 语义。
- 表之间没有 FK；insert_identifier/add_link 不验证目标存在和类型。store 层 mark_duplicate 仅禁止自指，不防跨类型、环或悬空目标。
- 动态探针已复现：退役产品仍被解析为“置信 1.00 正式映射”。

验收：

1. resolver 必须通过 active object join；目标缺失/非 active 时 fail-closed 为 unresolved 或显式 `superseded/redirect`，绝不能 `resolved`。
2. retire/merge/repoint 制定并实现同事务状态机：identifier、link、case 的处置规则明确；merge redirect 防环、同类型、目标 active。
3. 同库引用优先加 FK；若因运维原因不加 FK，必须用事务内锁定验证、周期 invariant checker 和阻断级告警实现等价保证。
4. 增加故障注入与并发测试：缺对象、退役、合并链、环、并发 retire/resolve/repoint。

### P0-04 — 归一规则与 MySQL collation 互相矛盾，会产生静默 false-merge

证据：`ontology/normalize.py:11-13,60-62` 与 `schema/028_ontology_identity.sql:33-37`。

代码明确把 customer 编号大小写视为可能有语义并原样保留；唯一键列却继承 `utf8mb4_unicode_ci`（case-insensitive）。因此 `A` 与 `a` 在 Python/MemoryStore 中是两个键，在真实 MySQL unique index 中会冲突或被视作同一身份。MemoryStore 合同测试无法揭示该生产差异。

同类缺口还包括：`canonical_ref` 只在 `(object_type, canonical_ref)` 上唯一，而 `type_code` 本身无 unique；两个类型可误配相同 type_code，`get_object_by_ref(... LIMIT 1)` 将产生歧义。

验收：

1. 给 namespace/norm 建语义明确的 binary/case-sensitive key（如 VARBINARY 或规范化哈希），不要依赖库默认 collation。
2. 用真实 MySQL 测试 customer 大小写、重音、全半角、中文括号、尾空格与 namespace 前缀。
3. `ontology_ref_seq.type_code` 全局 unique，`ontology_object.canonical_ref` 全局 unique；type_code 格式与 `_REF_RE` 一致。
4. migration 必须提供存量冲突预检，发现碰撞即中止，不得自动择一。

### P0-05 — 关键写入跨事务，崩溃或并发会留下永久半状态

证据：`ontology/seeding.py:195-231,340-354`、`routes/ontology.py:198-237`、`agent_tools/ontology_identity_resolve.py:137-161`。

- 播种先 `mint_object()`（内部已 commit），再独立 `insert_identifier()`；alias 冲突或进程崩溃会留下无别名对象。
- 分支自测只覆盖“下次同名重跑可偶然愈合”。动态并发探针证明：另一方先赢得 active alias 后，本次对象永久孤立，后续重跑只会 skip active。
- 工作台 confirm 先提交 identifier，再独立 resolve case；只有 `resolve_case()` 返回 False 时做补偿。若调用抛异常、进程在两步间退出、补偿失败，active alias 与 open/closed case 会不一致。
- Agent Action 将 case closure 定义为 best-effort，正式 identifier 与治理 case 天然可分叉。

验收：

1. 提供事务服务：`mint + canonical alias`、`confirm identifier + resolve case + audit outbox` 各在一个 DB 事务内。
2. 使用 `SELECT ... FOR UPDATE` / 唯一键后的确定性重查，所有重试绑定 idempotency key 与 args digest。
3. 对不可原子化的外部副作用使用 durable outbox/saga，不能依赖进程内补偿。
4. invariant reaper 覆盖 orphan object、active alias + open case、resolved case 无 identifier、link 悬空，并将异常作为 release blocker。

### P0-06 — 审计与审批链不能证明“谁批准了什么版本”，而且工作台审计 fail-open

证据：`routes/ontology.py:14-16,50-57`、`agent_tools/ontology_identity_resolve.py:80-85,102-111,137-161`、`agent_runtime/tool_executor.py:129-143`、`routes/agent.py:311-415`。

- 所有工作台变更在 mutation 之后调用 `_audit()`，异常被吞。会出现已改身份脊柱但完全无审计行的生产事实。
- Ontology Agent 写工具被标 LOW_WRITE，所以通用 ToolExecutor 明确让审计 fail-open。身份映射会改变检索和计算依据，不应按普通低风险偏好写处理。
- `ontology_identifier.approval_request_id` 已有列，但工具写入时不传；`confirmed_by` 记录发起人 `ctx.user_id`，不是实际 `decided_by` steward。
- approval scope 在提案时快照，批准和真正执行前不重算当前 stewardship，也不绑定 target object version。审批期间目标对象、scope 或分类改变后，旧批准仍可能落到新事实上。
- 工作台 UI 不展示 `evidence_json`/属性差异；候选确认不要求理由。手动搜索直接取 `hits[0]`，而后端只是按 canonical_ref 排序，并非“最匹配”。这会把 HITL 降格为误导性的确认按钮。

验收：

1. 所有 Ontology mutation 审计 fail-closed，并与事实变更同事务写 audit outbox；审计不可用时零副作用。
2. 将身份确认设为 `HIGH_WRITE` 或引入独立 `audit_required=true` 强约束。
3. 执行上下文携带 approval_request_id、decision_id、decided_by；identifier、invocation、audit 可双向回链。
4. 审批前和落库前重读 stewardship/object ACL，并校验 object version、status、classification、args digest；变更则批准失效、重新提案。
5. 审计最少含 actor/requester/approver、时间、source/target before-after version、tool/function version、审批理由、写回结果、request/run/call/idempotency key。
6. UI 展示证据来源、属性 diff、对象类型/归属/密级/版本；人工显式选择目标并强制理由，禁止自动取第一条。

### P0-07 — auto-merge 阈值无数值域保护，Phase 0 组织门只存在于文档

证据：`ontology/resolve.py:76-110,158-178`、`ontology/seeding.py:42,302-329`、`backfill.py:19-23,75-93`。

- τ 只检查 `high >= low`，不检查 `0 <= low <= high <= 1`。动态探针证明负数阈值可使 0.01 置信规则候选自动激活；小数点 typo 同样可把改模规则放过。
- exact-title 置信固定 0.96，默认越过 0.95；在 GT 尚未建立时已经存在 auto 通路。
- “四项签字前禁止真实播种”没有机器可验证的审批 artifact/gate。staging seed 只需 `--commit`；production backfill 只需把源码常量 `DRY_RUN=False` 并设置 env；`--mode master` 还可铸新对象。
- `U8SnapshotSource` 仍是 stub，说明 Phase 0①并未闭合；③ GT 与④边界签字亦无仓内证据。

验收：

1. τ 配置启动时严格校验数值域；非法配置必须禁用 auto 并阻断写 worker，不能回落到可 auto 的默认值。
2. 在 ground-truth 分层标定签字前，所有 auto activation 硬关闭；代码默认应为“候选-only”。
3. 真实 seeding/backfill 要求不可伪造的 gate artifact/version + 环境级审批，不以代码布尔量或普通 env 充当签字。
4. 完成 U8 T-1 diff 能力、50–100 对分层 GT、Product/Revision/SKU/PackingSpec 边界签字、steward SLA 后，才允许 staging 小流量。
5. false-merge 设硬门，且任何 false-merge 都能追溯到阈值、候选证据和批准人。

### P0-08 — 目标提交没有 CI，且现有 CI 会让 Ontology 真库测试静默缺席

证据：`.github/workflows/ci.yml:12-13,81-142` 与 Ontology 测试中的 `_RDS_OK` self-skip。

- workflow 只在 main push / PR 触发；该分支无 PR，HEAD workflow_runs 为空。
- db-integration 虽加载 027–030，却只运行旧的 pipeline/classification/concurrency/image_funnel 四模块。
- 新的 RDS store/sem 测试因此不会在 DB job 执行；simulate job 无 MySQL，会只跑 Memory 参数并跳过 RDS。
- 这正好漏掉本审查发现的 collation、外键、事务、视图授权和 Memory/RDS 语义差异。

验收：

1. 开 draft PR，base 指向 `claude/enterprise-agent-plan-6e9072`，先获得目标 HEAD 的全绿状态再谈合并。
2. db-integration 明确加入 Ontology store/sem/routes/backfill/seeding/identity tests，并在 job 末断言 `skipped == 0`（至少对真库契约族）。
3. 增加 MySQL 8 对抗测试：collation、FK/悬空、事务崩溃、并发 confirm/repoint/retire、视图与 inline parity、SHOW GRANTS。
4. 增加 ACL/audit outage/审批期间 scope 与 object version 改变的端到端测试。

### P0-09（继承依赖）— 生产迁移/回填路径不满足可审计、最小权限和可重放要求

证据：`scripts/apply_migration.py:29-39,76-104,123-140`、`dataworks_nodes/ontology_backfill_node.py:26-40,58-76,93`。

- migration 把 `environment in (development,test)` 直接判为 local，即使 host 是远程生产；只要配置层用 `read_only_ack` 放过远程连接，脚本即可绕过 `prod_access` token 路由并用普通 pymysql 连接。
- DDL 成功、ledger 写失败只 warning 且进程成功退出，允许“结构已变、台账无记录”。无 migration checksum、目标库/文件映射校验、已应用同版本内容校验或 rollback plan。
- dry-run 对 production 仍索取 RW token/连接，违背最小权限。
- DataWorks 节点运行时从公网安装未锁版本依赖，直接 `extractall` 资源 ZIP，并建议把 RDS/DashScope 凭据粘进节点源码；构建不可复现且扩大凭据暴露/供应链面。

验收：remote/prod 物理指纹优先于自报 environment；生产 apply 必经专用 RW token。DDL 与 ledger 失败必须非零退出；记录 SHA-256；文件→DB 白名单；staging 演练、备份/回滚、SHOW GRANTS 证据齐全。DataWorks 使用预构建签名制品、hash-locked 依赖、安全解压、平台 secret 注入，且移除不需要的 DashScope 密钥。

## 5. P1 / P2 问题

| 等级 | 问题 | 证据/影响 | 验收方向 |
|---|---|---|---|
| P1 | Ontology 仍可能变成业务事实副本 | `golden_json` 收 U8/手工事实，但 attribute_source 仅到 type.attribute，没有 source row/version/as-of/watermark；甚至把“采购价”暂定为 ontology manual | 每个属性值带 SoR key/version/as-of/transform/version/freshness；采购价等事实仍从 SoR 读取，答案回传 provenance |
| P1 | stewardship 不是安全的 desired state | `ensure_seeds()` 只 upsert，不删除/失效已移除 scope；旧授权会永久残留，且 seed 变更无审批版本 | scope 有状态/有效期/版本/审计；移除代码声明能显式撤权；发布前 diff + 双人批准 |
| P1 | 覆盖率指标可被“播种别名”虚高 | `active/(active+open case)` 混合全量 active alias 与去重后的 open case，忽略 source population/seen_count；不是真实覆盖率 | 以固定源快照分母计算，按 namespace×method×品类输出 precision/recall、false merge/split、unresolved、人工率 |
| P1 | 搜索/候选不可扩展且结果有偏 | title LIKE 最多 50；embedding 每类型只取 canonical_ref 最早 200；UI 又取第一条 | 精确索引优先、分页召回、稳定 rank、显式选择；报告 recall 截断率 |
| P1 | sem 缺迁移时静默 inline fallback | 030 缺失/查询失败时用 admin 连接内联同形 JOIN，掩盖 schema drift 与授权失败 | shared staging/prod 对 schema 缺失 fail-closed；仅本地兼容模式可 fallback，并打阻断告警 |
| P1 | 数据与证据无保留/删除策略 | case evidence、identifier 历史、golden JSON、候选 features 未进入 retention/subject purge 设计 | 明确保留窗口、法律依据、擦除/匿名化、legal hold 和审计留存分层 |
| P1 | DDL 约束不足 | confidence 无 [0,1] check、lifecycle_state/link_type/object_type 自由文本、owner_dept 未验证、JSON 无 schema | DB check/registry + 服务验证；schema parity 测试扩展到 027–030 |
| P1 | embedding 对所有候选标题调用外部 provider | resolver 会对对象池标题逐个 embedding，未先按对象 ACL/密级筛选 | 密级路由与 provider policy；confidential 默认不出域；离线预嵌入并按 ACL 过滤 |
| P1 | 可观测闭环不完整 | 无 orphan/invariant、steward SLA、audit failure、auto 抽检逾期、grant drift 的阻断告警 | 指标、告警、runbook、责任人、SLO 与演练证据 |
| P2 | 工作台没有计划所称批量处置，只有单条 offset 队列 | 大积压效率与并发稳定性不足 | cursor 分页、选择集、批量 preview、每条独立结果与幂等键 |
| P2 | 请求字段缺少长度/格式约束 | note/revision/namespace 等可到 DB 错误，部分工具把 `str(e)` 返回模型 | Pydantic/JSON schema 限长、枚举、revision 格式；外部错误统一映射 |
| P2 | 前端全局 singleton 状态可能跨身份残留 | `supported/coverage` 在非管理员分支不总是清空 | 身份变化时 reset，敏感状态按 user/session 隔离 |

## 6. Phase 0 四项 gate 状态

| Gate | 当前证据 | 结论 |
|---|---|---|
| ① U8 T-1 附属库可 diff | `U8SnapshotSource.iter_records()` 明确抛 `NotImplementedError` | **未通过** |
| ② steward 编制、日处理量与 SLA | 只有代码 seed scope，没有实名排班、容量、代理、响应/纠错时限证据 | **未通过 / 缺证据** |
| ③ PMC 三品类 50–100 对 ground truth | 仓库无该 GT、backtest 报告或分层阈值签字 | **未通过** |
| ④ Product/Revision/SKU/PackingSpec 边界与纠错 SLA | `target_revision` 仍是自由字符串；无 Revision 对象/规则版本/签字 artifact | **未通过** |

所以，即使代码 P0 全修，也只能先进入 staging shadow/read-only 验证，不能直接真实播种。

## 7. 建议修复顺序

1. **PR-A：身份不变量** — binary norm key、全局 ref/type_code unique、目标 active join、同类型 merge、防环、生命周期事务。
2. **PR-B：ACL 与 DB 隔离** — 对象/动作 ACL 单一实现、工作台 SQL 过滤、sem 独立 schema/账号、SHOW GRANTS 测试。
3. **PR-C：事务与审计** — atomic confirm/mint、audit outbox、approval/decision/identifier 全链、执行前版本重验。
4. **PR-D：CI 与迁移** — Ontology 真 MySQL job 零 skip、迁移 checksum/ledger fail-closed、staging 演练。
5. **PR-E：Phase 0 与 auto** — 机器 gate、τ 数值域、auto 默认硬关、GT/backtest/false-merge 门。
6. **PR-F：HITL 与指标** — 证据 diff、显式目标选择、强制理由、真实 population coverage、SLA/抽检告警。

每个 PR 都以 `claude/enterprise-agent-plan-6e9072` 的最新可合并层为 base；不要把当前 6679 行整体一次性审批，也不要绕过既定的 PR0–PR14 分层。

## 8. 允许合并前的 Definition of Done

- 所有 P0 acceptance tests 自动化且通过；目标 HEAD 有 GitHub CI 证据。
- Ontology 真 MySQL 契约族零 skip，Memory/RDS 对大小写、事务和并发语义一致。
- 无关 dept_admin 无法观察 case/object 是否存在；跨部门/跨类型写 100% 拦截。
- `fuling_ro` 直接 SELECT ontology/sem 受限对象得到权限拒绝，服务层 ACL 测试 100% 拦截越权。
- 退役/合并/缺失目标永不返回 `resolved`；无 orphan、悬空 link、case/identifier 分叉。
- 每次写都能从 identifier 追到 request→approval→decision→invocation→audit，并含 before/after version 与真实 approver。
- audit/authorization/ledger 故障时零副作用；迁移与 ledger 不允许半成功。
- auto 默认禁用；Phase 0 四项签字和 GT/backtest artifact 版本化后才可在 staging 小流量启用。
- frontend 展示证据与 diff，不自动取第一条，确认理由必填；226 个现有前端测试继续全绿并新增对抗测试。
- 父分支与最新 main 完成重放/冲突检查；此前系统级 5 个 P0 另行关闭后，才可讨论生产 GO。

## 9. 给后续 coding agent 的硬约束

- 不得把 `resolve()`、sem lookup 或任何 READ_ONLY 工具改成隐式写入。
- 不得注册 Ontology 工具、开启 flag、apply 共享库、部署 DataWorks 或真实播种，除非上述 gate 有可验证证据。
- 不得让模型或请求体提供 ACL、bypass、confirmed_by、approval ID、阈值或环境身份。
- 不得直接写 U8/RDS 业务事实表；所有生产写只走受治理 Action，U8/高风险写必须 HITL。
- 任何授权/审计/迁移台账不确定都 fail-closed；不能用“best effort”描述安全事实。
- Ontology 只做身份、语义、来源和动作路由控制面；库存、订单、金额、采购价、完工状态仍以 SoR 为准。

---

最终判断：这批代码把 Ontology 从“概念文档”推进到了可讨论的控制面原型，读写分离、case 模型、默认关闭等方向正确；但它还不是可受信任的身份脊柱。当前最危险的不是算法精度，而是**跨部门可见、跨域可写、生命周期仍解析为真、写审计可丢、DB 授权可绕过服务层**。这些必须先于 PMC-1 功能扩展修复。
