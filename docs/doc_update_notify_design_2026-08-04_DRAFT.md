# 文档升版可见范围提醒(doc-update-notify)设计稿 _DRAFT

> 2026-08-04 · 分支 `feature/doc-version-notify` · 决策人 Sam
> 状态:**待评审**(codex MCP 额度 8/7 恢复,评审路径待 Sam 拍板;本稿未经六阶段评审,不得据此实施)
> 需求一句话:**文档升级到新 version 并在检索侧真正生效后,提醒所有"对该文档有可见权限"的用户。**

---

## 0. 摘要(TL;DR)

- **触发 = 状态扫描,不挂摄取路径**:每日任务扫 `document_meta × document_version`,发现"版本切换已完成但尚无事件行"的文档 → 写事件台账。摄取管线(DAG1-3/orchestrator)**零改动**。
- **"内容真变了"门**:新旧版本 `canonical_sha256` 相等或缺失 → 抑制(SUPPRESSED)。维护性重灌/全量重建(version+1 但内容不变,`rebuild_seed_versions` 族)天然静音。
- **受众 = 权威判定离线枚举**:对 `staff_dim` 全员逐人构造 AclContext,用与检索面同源的 `acl_policy.can_read_doc` 判定 → **通知面 ⊆ 检索面**,legacy/node 双轨、总经办、撤销全部继承权威语义,零平行实现。
- **存储 = 双表 outbox**(`fuling_operation`,schema/065):`doc_update_event`(每 doc×version 一行,UNIQUE 幂等)+ `doc_update_notice`(每 用户×事件×通道 一行,状态机)。
- **通道 = 钉钉工作通知(按人日摘要) + console 站内通知**,复用 `admin_notify` 的 asyncsend_v2 通道与 fire-and-forget 纪律,补齐其 >100 人截断缺口(分批循环)。
- **执行体 = 新 DataWorks 日调节点**(`retention_node`/`org_sync_node` 同款七步骨架,DRY_RUN 默认)。
- **全部 flag 默认关**;建议语料重传收口后再启用。

---

## 1. 目标与非目标

**目标**
1. 文档新版本**在检索侧生效后**(不是上传时),通知当时对它有可见权限的用户"该文档已更新"。
2. 同一 (doc, version) 对同一用户**最多打扰一次**;重跑/重建/对账路径不产生重复或虚假通知。
3. 通知面永远 ⊆ 检索面(不能通过通知泄露标题/存在性给无权限者);解析失败宁可不发(fail-closed)。
4. 通知失败绝不影响摄取与问答主流程(全程 fail-open on 主链路视角)。

**非目标(v1 明确不做)**
- 不做"变更内容摘要/diff"(v1 只报"《标题》已更新至 vN")。
- 不做用户订阅/关注/免打扰偏好(全员按权限收,通道级开关兜底)。
- 不做"权限变更→你新获得了可见"通知(挂点已知:`access_grants.record_acl_projection_invalidation`,留 v2)。
- 不做文档退役/下线通知。
- 不做实时推送(时效 = 日调批;与摄取本身的日批节奏一致)。

---

## 2. 现状与缺口(已核验,file:line)

| 事实 | 证据 |
|---|---|
| 版本模型:`document_meta.current_version_no` 指针 + `document_version` 每版一行,新旧同表多行 | schema/001:79,107-179 |
| 切换完成时刻:DAG3 `node_deactivate_old_chunks` 同事务做 index_status CAS `PROCESSING→'SUCCESS'` + 旧行 `status='superseded'` | pipeline_nodes.py:7607-7644 |
| DAG3 之后无任何节点;"切换成功 doc 集合" `_sp_dvs` 是纯局部变量,ctx 不可见 | dag_definitions.py:199-207、pipeline_nodes.py:7628 |
| 第二条切换收尾路径:`spot_checker.reconcile_stranded_versions`(stage-3 pre-drain,fail-open) | dataworks_orchestrator.py:1164-1173 |
| **version+1 ≠ 内容变了**:重建 seed 原样复制 raw_key 只递增版本号 | scratch/rebuild_seed_versions.sql:39-56、skills/fuling-ha3-rebuild/scripts/seed_versions.py:59-71 |
| 内容指纹已双侧在库:`document_version.canonical_sha256`(003);skip-gate 已用它判"同正文" | schema/003:20-22、pipeline_nodes.py:1359-1400 |
| 升版强制继承原 permission_level(升版不得改可见范围) | routes/kb_console.py:2660 |
| 权威读判定已统一:`can_read_doc(AclContext, DocAcl)`;文档侧权威解析 `resolve_doc_acl` | acl_policy.py:243-290、access_grants.py:169 |
| 全员花名册:`staff_dim(staff_id, dept_ids CSV, is_active)`(org_sync 每日刷新,**无姓名**);组织树 `dept_dim(parent_id 邻接表)` | schema/060:126-149、org_sync.py:79-190 |
| legacy 轴"组码→人"**无权威表**:`user_role` 是惰性缓存+人工 seed(57 行/1175 人),按它枚举系统性漏人 | dingtalk_identity.py:317-410 |
| 文档→组码反查已有钦定函数 `groups_that_can_read_owner` | retriever.py:411-431 |
| 钉钉工作通知通道已存在:`admin_notify.py`(asyncsend_v2,RAG_ADMIN_NOTIFY 默认关,fire-and-forget,收件人只解析**管理员**) | admin_notify.py:66-88,115-137 |
| ⚠️ `_MAX_RECIPIENTS=100` 是**单批截断非分页**,群发会静默丢人 | admin_notify.py:35,80 |
| console 通知呈现:全仓无 inbox/站内信表或端点;审批红点=纯拉取派生 | schema/README.md 全表、Sidebar.vue:24-25 |
| 日调节点先例(七步骨架/py3.7 钉版/DRY_RUN 双闸/退出码 0-2-3) | dataworks_nodes/retention_node.py:36-205、org_sync_node.py:35-190 |
| 组织快照保鲜:>48h 节点通道 fail-closed | dingtalk_identity.py:955-990 |
| schema 取号:main 最大 064、分支侧最大 058 ⇒ **本功能取 065**;README「下一号 064」已过时 | schema/ 目录 + git ls-tree claude/ontology-p0 |

**缺口小结**:事件无台账(切换成功只有 per-chunk DEACTIVATE 审计行)、"部门→全员 staffId"展开零代码、通知无落库/去重/重试、console 无站内通知面。

---

## 3. 核心设计决策

### D1. 触发 = 状态扫描(scan-based),不挂摄取路径

每日扫描谓词(fuling_knowledge 侧只读):

```sql
dm.status='active' AND dm.current_version_no > 1
AND dv_new.version_no = dm.current_version_no
AND dv_new.status='active' AND dv_new.index_status='SUCCESS'
AND EXISTS (旧版本行 status='superseded')
AND NOT EXISTS (doc_update_event WHERE doc_id=dm.doc_id AND version_no=dm.current_version_no)
```

**为什么不挂 DAG3/orchestrator 成功块**(备选曾考虑:orchestrator:1316-1326 有 `complete_meta_projection` 同款 fail-open 先例):
1. 切换收尾有**两条路径**(DAG3 正常路径 + reconcile_stranded_versions 修复路径),挂点要挂两处且 `_sp_dvs` 需新增 ctx 透传;扫描按状态收敛,**天然全覆盖**。
2. 摄取路径是本仓库安全不变量最密集的地方,零改动 = 零新增失败面。
3. 时效上无损失:通知本来就是日批摘要。
- 事件表自身即水位(UNIQUE(doc_id, version_no)),无需时间戳水位,也不依赖 `activated_at` 死列。

**为什么不挂 kb_console.py:2924 升版事务**(探索期另一备选):那是"升版意图"时刻,文档尚未可检索,且可能中途隔离/失败/被 skip-gate 回退;通知"已更新"必须锚在**生效**时刻。

### D2. 内容变更门:canonical_sha256 相等或缺失 → 抑制

- 新版 `canonical_sha256` vs 被 supersede 的最近前版:**双方都在且不等 → PENDING;相等 → SUPPRESSED(unchanged_sha);任一缺失 → SUPPRESSED(missing_sha)**(保守:证明不了"变了"就不打扰)。
- 这层单独兜住全量重建/维护重灌(seed 族 version+1 内容不变)。`RAG_SKIP_UNCHANGED_REINGEST`(生产默认 true,stage1_node.py:41)已在上游把"同正文重传"挡在 DAG3 之外(版本指针回退,pipeline_nodes.py:1392-1400),本门是第二道保险。
- 纯 re-chunk/re-index 不升版(reset_for_rechunk 严格 version_no=current,scripts/reset_for_rechunk.py:49-53),对本功能天然不可见。

### D3. 受众 = `can_read_doc` 权威离线枚举(通知面≡检索面)

```
audience(doc) = { s ∈ staff_dim(is_active=1) : can_read_doc(ctx(s), resolve_doc_acl(doc)) }
```

ctx(s) 离线构造,**与 serving 同源**:
- groups:seeded `user_role` 行优先(与在线优先级一致);否则 `staff_dim.dept_ids → dept_dim.name → _DEPT_NAME_TO_GROUPS`/生产中心子树集(dingtalk_identity 现有纯结构,需抽一个可离线复用的纯函数,不复制表)。
- ancestor_chains/direct_dept_ids:`dept_ancestry.resolve_ancestor_chains` + `staff_dim.dept_ids`(与 `resolve_acl_context` 同料,dingtalk_identity.py:1021-1052)。
- 组织快照 >48h:**事件保持 PENDING 不解析**,告警,下轮重试(不发错人,也不丢事件)。

**刻意排除(默认,均为 Sam 可翻的 knob)**:
- `permission_level='public'` 文档 → SUPPRESSED(public_policy)。public 受众=全员,每次更新全公司推送是骚扰;v1 只通知 dept_internal。
- 仅凭总经办 `*` 通配可见(org_wide_reader)的用户不进受众(判定时置 org_wide=False 再跑 can_read_doc)。总经办对全库可见,逐篇打扰荒谬。
- restricted 文档:can_read_doc 对任何人不放行 → 受众恒空 → SUPPRESSED(audience_zero),自然排除(测试钉住)。

**规模护栏**:`audience > RAG_DOC_NOTIFY_MAX_AUDIENCE`(默认 300)→ SUPPRESSED(audience_cap) + 告警;单轮新事件 > `RAG_DOC_NOTIFY_MAX_EVENTS_PER_RUN`(默认 50)→ 全批 SUPPRESSED(bulk_guard) + 告警——**同一机制覆盖首启基线、重建波、失控三种场景**(首次启用时的存量积压会被它整批静音,即隐式 baseline;告警文案指明原因)。

### D4. 存储 = 双表 outbox,`fuling_operation`,schema/065

落 operation 库(交互运营域惯例:user_feedback/qa_retrieved_doc/dingtalk_msg_dedup 同域);解析在 Python 侧完成,无跨库 SQL JOIN,event+notice 同库单事务。

```sql
-- schema/065_doc_update_notify.sql → fuling_operation(均显式 COLLATE utf8mb4_unicode_ci)
CREATE TABLE doc_update_event (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  doc_id VARCHAR(128) NOT NULL,
  version_no INT NOT NULL,
  prev_version_no INT DEFAULT NULL,
  content_changed TINYINT(1) DEFAULT NULL,        -- sha 对比结果;NULL=未判定
  status VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- PENDING|RESOLVED|SUPPRESSED
  suppress_reason VARCHAR(32) DEFAULT NULL,       -- unchanged_sha|missing_sha|public_policy|audience_zero|audience_cap|bulk_guard
  audience_count INT DEFAULT NULL,
  resolved_at DATETIME DEFAULT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_doc_version (doc_id, version_no),
  KEY idx_status_created (status, created_at)
);
CREATE TABLE doc_update_notice (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  event_id BIGINT UNSIGNED NOT NULL,
  doc_id VARCHAR(128) NOT NULL,                   -- 冗余,console 读侧免 JOIN event
  user_id VARCHAR(128) NOT NULL,                  -- 钉钉 staffId(与 qa_session_log.user_id 同口径;不存姓名)
  channel VARCHAR(16) NOT NULL,                   -- console|dingtalk
  state VARCHAR(16) NOT NULL DEFAULT 'PENDING',   -- PENDING|SENT|READ|FAILED|SKIPPED
  attempts INT NOT NULL DEFAULT 0,
  sent_at DATETIME DEFAULT NULL,
  read_at DATETIME DEFAULT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_event_user_channel (event_id, user_id, channel),
  KEY idx_user_state (user_id, channel, state, created_at)
);
```

- console 通道:notice 行本身就是站内信(state PENDING=未读,READ=已读);无需发送动作。
- dingtalk 通道:PENDING→SENT/FAILED;FAILED 下轮重试,attempts ≥3 → SKIPPED + 告警。
- 配套:`scripts/ci_load_schema.sh` 登记(否则 manifest 测试红)、`tests/test_schema_ddl_parity.py` 契约、`schema_migrations` 台账、`retention.py` 加留存窗(event 365d / notice 180d,拟)。

### D5. 通道:钉钉工作通知(按人日摘要) + console 站内

- **钉钉**:每用户每轮**一条**摘要文本:「【富岭知识库】你可见的 N 篇文档有更新:《A》→v3、《B》→v2 …(≤10 篇列名,余显"等 K 篇");详情见知识库控制台」。按**渲染文本相同**分组合并 userid_list,**分批循环 ≤100 人/次**(修复 admin_notify 截断模式)。复用 `dingtalk_card._get_access_token` + `RAG_DINGTALK_AGENT_ID`;HTTP 走本模块 `_http_post` seam + `_SEND_ASYNC`(tests monkeypatch,admin_notify 同款,tests/test_admin_notify.py:13-17 范式)。发送量=每人每日≤1 条,远低于工作通知任何配额档(官方配额以现值为准,不在此复述数字)。
- **console**:见 §5 接口。红点/列表读 notice 表;**读时用当前身份重跑 can_read_doc,失败或 deny 即隐藏该行**(fail-closed;组织变动/撤销后不再展示历史通知标题)。钉钉侧已发文本无法撤回——文案只含标题+版本号,收件人发送时点均有权限,残余风险见 §9。
- 文案不带深链(console 文档详情路由是否存在待确认,见 §10);引导语固定"到知识库控制台查看"。

### D6. 执行体 = 新 DataWorks 日调节点 + 业务模块

- `opensearch_pipeline/doc_update_notify.py`:`discover_events()` → `resolve_audience()` → `render_and_send()`,`main(argv)` 支持 `--commit`(默认 dry-run 全链只读打印,retention.py 同款);simulate 短路在 flag 检查**之前**(retention.py:236-239 纪律)。
- `dataworks_nodes/doc_update_notify_node.py`:七步骨架照抄 retention_node/org_sync_node(py3.7 钉版依赖仅 PyMySQL/DBUtils/requests;env 守卫先于 import;`RAG_SIMULATE_OPENSEARCH=true` 短路 R5 完整性守卫;DRY_RUN 常量双闸;退出码 0/2/3)。建议调度 **每日 07:30**(在 org_sync 00:15 与夜间 stage-3 之后,上班前送达)。正式版走 `scratch/gen_dataworks_paste` 生成器铸造(ENV_KEYS 补 `RAG_DOC_NOTIFY`/`RAG_DINGTALK_AGENT_ID`/`DINGTALK_CLIENT_ID/SECRET`——生成器键清单=节点 env 全量真相)。
- 写库守卫:节点硬设 `RAG_ENVIRONMENT=production` → env_guard 第 2 级放行;业务模块入口仍显式 `assert_destructive_write_allowed("doc_update_notify", ...)` 早失败早响亮(retention.py:246-247 同款)。

### 配置(config.py dataclass + `_env_bool`/int 解析,content_binding 同款成对写法)

| flag | 默认 | 语义 |
|---|---|---|
| `RAG_DOC_NOTIFY` | **False** | 总闸(发现+解析+落表)。**独立新闸,不复用 RAG_ADMIN_NOTIFY**(retention/purge 双闸纪律:开审批通知不应连带开升版群发) |
| `RAG_DOC_NOTIFY_DINGTALK` | **False** | 钉钉通道子闸(console 站内可先行) |
| `RAG_DOC_NOTIFY_INCLUDE_PUBLIC` | False | public 文档是否通知 |
| `RAG_DOC_NOTIFY_MAX_AUDIENCE` | 300 | 单事件受众上限 |
| `RAG_DOC_NOTIFY_MAX_EVENTS_PER_RUN` | 50 | 单轮新事件上限(bulk_guard) |

---

## 4. 数据流全景

```
[钉钉组织] ──org_sync(日00:15)──▶ dept_dim / staff_dim ─┐
                                                        │(离线构造 AclContext)
document_meta × document_version ──①discover(扫描谓词+sha门)──▶ doc_update_event
        ▲                                                           │
        │(权威 DocAcl: resolve_doc_acl)                             ②resolve(can_read_doc 逐人判定)
        └───────────────────────────────────────────────────────────┤
                                                     doc_update_notice(console|dingtalk × user)
                                                                    │
                            ③send: dingtalk=asyncsend_v2 分批摘要   │   console=API 拉取(读时 ACL 复核)
                                        (①②③ = doc_update_notify_node 日调,dry-run 默认)
```

## 5. 接口变化(serving)

新增 `routes/notices.py`(cold domain,遵守 routes/__init__.py 铁律:不 define/shadow 被 monkeypatch 的 api 属性):

- `GET /api/kb/notices?limit=20` → `{items:[{id,doc_id,title,version_no,created_at,state}], unread_count}`。身份取自现有 console 免登/token(user_id=staffId,与 whoami 同源);逐行 can_read_doc 复核;表不存在(1146)→ 空列表降级(先部署后 apply 安全,qa_logger 负缓存同款)。
- `POST /api/kb/notices/read` `{ids:[...]}` 或 `{all:true}` → 置 READ(仅限本人行,幂等)。
- console-app:顶栏铃铛红点(unread_count)+ 下拉列表 + 全部已读;新 e2e spec(Playwright 硬门惯例);加载态遵守四态收口惯例。

## 6. 修改范围(文件清单)

| 文件 | 动作 | 内容 |
|---|---|---|
| `schema/065_doc_update_notify.sql` | 新增 | §D4 双表(fuling_operation) |
| `scripts/ci_load_schema.sh` | 改 | 登记 065→operation |
| `opensearch_pipeline/config.py` | 改 | 5 个 flag(dataclass+解析成对) |
| `opensearch_pipeline/doc_update_notify.py` | 新增 | discover/resolve/send/main;`_http_post` seam;dry-run |
| `opensearch_pipeline/dingtalk_identity.py` | 微改 | 抽离线可复用的 groups 纯函数(不动在线行为) |
| `dataworks_nodes/doc_update_notify_node.py` | 新增 | 七步骨架日调节点 |
| `opensearch_pipeline/routes/notices.py` + `api.py` 挂载 | 新增 | §5 两端点 |
| `opensearch_pipeline/retention.py` | 改 | 两表留存窗 |
| `console-app`(Bell+面板+store+e2e) | 新增 | §5 前端 |
| `tests/test_doc_update_notify.py` 等 | 新增 | §8 矩阵 |
| `scratch/apply_migration_065.py` | 落库时新增 | 幂等 apply+台账(当日 RW token) |

**不改**:pipeline_nodes.py、dag_definitions.py、dataworks_orchestrator.py、retriever.py、acl_policy.py、access_grants.py(只读消费)。

## 7. 边界情况

| 场景 | 行为 |
|---|---|
| 全量重建/维护重灌(version+1 内容同) | sha 门 SUPPRESSED(unchanged_sha);量大时 bulk_guard 双保险 |
| 同正文重传 | skip-gate 上游回退版本指针,扫描根本看不见 |
| 纯 re-chunk/re-index | 不升版,不可见 |
| 新版被 PII 隔离/失败/LIMIT 推迟 | index_status ≠ SUCCESS,不触发;修复后自然补触发一次 |
| 两次运行之间连升两版(v3→v5) | 只对 current(v5) 建事件,通知一次(中间版不打扰) |
| retire/restore | dm.status≠active 排除;PENDING_DELETE 握手不受影响(扫描只读) |
| reconcile 修复的搁浅切换 | 状态收敛后照常发现(挂点方案会漏,本方案不漏) |
| 组织快照 >48h | 事件留 PENDING+告警,不解析不发送 |
| 员工离职/移出 | staff_dim.is_active=0 发送侧排除;console 读时复核隐藏 |
| 事件后 ACL 变更 | 受众按解析时点权威表现算(升版本身不改可见范围,kb_console.py:2660) |
| 发送中途失败 | notice 行级状态机,下轮只重试 FAILED;attempts≥3 → SKIPPED+告警 |
| 表未 apply / flag 关 | 节点 exit 2;serving 端点 1146 降级空列表;全链行为与今天逐字节一致 |
| SIM | simulate 短路,不触外网不落真库 |

## 8. 测试与验证

- **受众真值表**(核心):legacy 组码/伞形/marketing 共享/approved 跨部门授权/node subtree+exact/public/restricted/org_wide/撤销/快照过期/seeded user_role 优先——逐项断言"通知面=检索面"(与 acl_policy 测试同料构造)。
- 抑制矩阵:unchanged_sha/missing_sha/public_policy/audience_cap/bulk_guard/audience_zero。
- 幂等:同库重跑 discover 零新事件;notice UNIQUE 冲突安全。
- 发送:>100 人分批、同文本合并、FAILED 重试、attempts 封顶;`_http_post` monkeypatch 断言载荷。
- routes:未认证 401、只见本人行、READ 幂等、1146 降级、ACL 复核隐藏。
- DDL parity + ci_load_schema manifest + `make test`/`make lint`/`make sim` 全绿。
- **预期未验证项(照例声明)**:真实钉钉工作通知送达(需现网 AgentId+flag)、DataWorks 节点粘贴/发布/调度(Sam 操作)、生产 065 apply。

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 误发给无权限者(唯一不可撤回伤害) | 单一权威 can_read_doc + 真值表测试 + 文案仅标题/版本 + cap;console 侧读时二次复核 |
| 群发骚扰 | 日摘要 1 条/人 + public 默认排除 + 双上限 + 双 flag 渐进(console 先行) |
| 与检索面漂移(平行实现腐化) | 不写第二套判定,只调 acl_policy/access_grants;离线 groups 抽自在线同一纯结构并加对照测试 |
| DataWorks 节点故障 | 退出码 2/3 + ops 告警;事件台账幂等,恢复后自动补投 |
| 回滚 | 全 flag 关=行为归零;表为纯增量,serving 探测降级 ⇒ 先部署后 apply、先关后拆均安全;无不可逆步骤 |

## 10. 上线次序(建议)

1. 代码合 main(全 flag 关,`make test`/`lint`/`sim` 绿)→ 2. staging 065 apply + 节点 dry-run 演练 → 3. 生产 065 apply(台账)→ 4. SAE 重打包部署(console 端点+UI)→ 5. DW 节点粘贴/发布/调度(paste 生成器铸造)→ 6. **语料重传收口后** Sam 开 `RAG_DOC_NOTIFY`(console 站内先行观察)→ 7. 观察 ≥1 周后开 `RAG_DOC_NOTIFY_DINGTALK`。

## 11. 尚未确定(Sam 拍板项)

1. public 文档更新是否通知(默认不;若要,受众=全员,建议维持摘要+上限)。
2. 总经办(org_wide)是否收(默认不收)。
3. 两个上限默认值(300/50)与钉钉通道开启节奏。
4. 调度时刻(建议 07:30)。
5. 文案与是否带 console 深链(需先确认文档详情路由)。
6. 启用时机与重传波期间姿态(建议真空期收口后再开;若重传走"认领旧 doc_id 升版"路径,bulk_guard 会整批静音并告警,是否人工放行由 Sam 定)。
