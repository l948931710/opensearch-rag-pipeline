# 文档升版可见范围提醒(doc-update-notify)设计稿 _DRAFT · rev2

> 2026-08-04 · 分支 `feature/doc-version-notify` · 决策人 Sam
> 状态:**替代评审已过一轮(两名独立红队 REVISE→修订入本稿),codex 补审待 8/7 额度恢复;未实施**
> 需求一句话:**文档升级到新 version 并在检索侧生效后,提醒所有"对该文档有可见权限"的用户。**

---

## 0. 摘要(TL;DR)

- **触发 = 日调状态扫描,不挂摄取路径**:发现"版本切换已完成但无事件行"的文档 → 写事件台账。摄取管线零改动;reconcile 修复路径天然覆盖。**谓词按 `_kb_version_quarantined`(OR 语义唯一权威)排除追溯隔离件,且发送前对隔离/退役/权限再核一遍。**
- **"内容真变了"门**:新版 vs **最新非 NULL 前版**(skip-gate 同款选取)的 `canonical_sha256`;相等/缺失即抑制 → 维护性重灌(version+1 内容不变)静音。
- **受众 = 权威判定离线枚举**:对 `staff_dim` 全员构造 AclContext,跑 `can_read_doc(ctx, acl, grant_enabled=G, enforce_enabled=E)`(**姿态与 serving 对齐并前置断言**),`resolve_doc_acl(strict=True)`;任何降级输入 → 事件 **HOLD**,绝不解析成"没人可见"。
- **状态机三分**:PENDING(待解析)/ **HOLD(可回放,运维性拦截)** / SUPPRESSED(语义性终态)。首启走**显式基线**(`--baseline` 全量盖章),不靠 bulk_guard 撞大运。
- **通道 = console 站内 + 钉钉工作通知按人日摘要**(独立子闸;解析时只为已开启通道建行;发送前逐批 ACL 复核;**同步有界发送**回写状态,不用 fire-and-forget)。
- **执行体 = 新 DataWorks 日调节点**(retention_node 七步骨架);全 flag 默认关。

---

## 1. 目标与非目标

**目标**
1. 新版本**在检索侧生效后**(非上传时)通知当时有可见权限的用户。
2. 同一 (doc, version) 对同一用户最多打扰一次;重跑/重建/对账不产生重复或虚假通知。
3. **通知面 ⊆ 检索面**(判定函数同源;数据面差异有界且方向已申报,见 §9);解析/发送任何降级 → 宁可不发或迟发(HOLD),绝不多发。
4. 通知失败绝不影响摄取与问答主流程。

**非目标(v1)**:变更 diff 摘要;订阅/免打扰偏好;"你新获得可见"通知(挂点 `access_grants.record_acl_projection_invalidation`,留 v2);退役/下线通知;实时推送。

---

## 2. 现状与缺口(已核验;⚠=rev2 修正过的引用)

| 事实 | 证据 |
|---|---|
| 版本模型:`document_meta.current_version_no` 指针 + `document_version` 每版一行 | schema/001:79,107-179 |
| 切换完成:DAG3 deactivate 同事务 CAS `index_status='SUCCESS'` + 旧行 `status='superseded'` | pipeline_nodes.py:7607-7644 |
| DAG3 后无节点;`_sp_dvs` 纯局部;reconcile 会把搁浅切换收敛到 SUCCESS+supersede | dag_definitions.py:185-207、spot_checker.py:655-680 |
| version+1 ≠ 内容变:重建 seed 原样复制 raw_key ⚠(路径修正) | scratch/rebuild_seed_versions.sql:39-56、scratch/seed_versions.py:58-67 |
| 内容指纹:`canonical_sha256`;skip-gate 取"最新非 NULL 前版"比对 | schema/003:20-22、pipeline_nodes.py:1358-1400 |
| ⚠ 升版继承 permission_level **仅 console 升版端点**;每版本有效权限权威=stage-2(`permission_override` 优先→raw_key 路径段) | kb_console.py:2660,2909-2910、dataworks_orchestrator.py:216-221、schema/063 |
| ⚠ 统一读判定真实签名 `can_read_doc(ctx, doc, *, grant_enabled, enforce_enabled)`(必填);GRANT=false 对 node 文档**无条件 DENY** | acl_policy.py:243-290 |
| `resolve_doc_acl(..., strict)` 宽松/strict 双语义表;宽松下探测异常→全 legacy(node 文档会按真实 owner 超发) | access_grants.py:169-203,130-135 |
| `user_role` 墓碑行=权威空组;**任何** user_role 行(含自动缓存 employee)TTL 内优先于名字口径 | dingtalk_identity.py:340-380 |
| org_wide(总经办 `*`)只在 node 分支消费;legacy 分支只看 groups | acl_policy.py:285-286,228-240 |
| ⚠ 隔离判定唯一权威=`_kb_version_quarantined`(publish/gate **OR** 语义);"隔离件 index_status 可能残留 'SUCCESS'"为现网documented 状态(spot_checker 抽检路径本身置 DELETED,但残留态存在) | kb_console.py:3283-3286、api.py:2745-2749、spot_checker.py:1046-1055 |
| 全员花名册 `staff_dim`(⚠ schema/060:**139-149**,无姓名);组织树 `dept_dim`(:126-137);快照>48h fail-closed | schema/060、dingtalk_identity.py:954-990 |
| ⚠ `user_role` 覆盖极小(2026-07-28 prod-RO 实测约 57 行/1175 人;**实施前需重查现值**),按它枚举系统性漏人 | 数据面事实,仓库不可证 |
| 钉钉工作通知通道已存在(`admin_notify`,asyncsend_v2,默认关);⚠ 100 人上限=截断非分页 | admin_notify.py:35,66-88 |
| console 无 inbox;审批红点纯拉取 | schema/README 全表、console-app/src/components/shell/Sidebar.vue |
| 日调节点骨架/py3.7 钉版/双闸/退出码 | dataworks_nodes/retention_node.py:36-205、org_sync_node.py:35-190 |
| ⚠ paste 生成器 ENV_KEYS **无任何 NODE_ACL 键**;DINGTALK_CLIENT_ID/SECRET **已在**清单 | scratch/gen_dataworks_paste_20260721.py:29-43 |
| 个人数据擦除唯一口径 `retention.py::_purge_jobs` | retention.py:368-397,495-536 |
| schema 取号:main 至 064、op0 分支至 058 ⇒ 取 **065**;README「下一号 064」行已过时(本次一并改) | schema/ 目录、git ls-tree |

---

## 3. 核心设计决策

### D1. 触发 = 状态扫描(摄取路径零改动)

```sql
dm.status='active' AND dm.current_version_no > 1
AND dv_new.version_no = dm.current_version_no
AND dv_new.status='active' AND dv_new.index_status='SUCCESS'
AND NOT _kb_version_quarantined(dv_new.publish_status, dv_new.gate_status)   -- rev2:追溯隔离排除(OR 语义,复用唯一权威 helper 的语义;SQL 内联同款条件)
AND EXISTS (旧版本行 status='superseded')
AND NOT EXISTS (事件行(doc_id, version_no))          -- rev2:跨库反连接在 Python 侧两查一比,不写跨库 SQL(1267 面)
```

- 否掉的备选:挂 orchestrator 成功块(要挂两处+ctx 透传,漏 reconcile 路径)、挂 kb_console 升版事务(生效前时刻,会对隔离/失败件误报)。
- **rev2:隔离/退役是动态态**——事件建行与发送隔天,发送前对每个事件**再读一次** dm.status + 隔离态,命中 → 事件转 SUPPRESSED(quarantined_or_retired),该事件全部 notice 置 SKIPPED。

### D2. 内容门:canonical_sha256(前版=最新非 NULL 行)

- 前版选取与 skip-gate 逐字同款:`version_no<current AND canonical_sha256 IS NOT NULL ORDER BY version_no DESC LIMIT 1`(pipeline_nodes.py:1360-1362)。双方在且不等 → 进入解析;相等 → SUPPRESSED(unchanged_sha);新版缺失或无非 NULL 前版 → SUPPRESSED(missing_sha)。
- rev2 注:`RAG_SKIP_UNCHANGED_REINGEST` 只在 DW 节点 setdefault(stage1_node.py:41),笔记本/手工重跑不带——第一道防线是执行路径相关的,**sha 门才是对所有路径成立的保险**。
- 实施前 prod-RO 数据附件(见 §11):前版 sha NULL 占比(missing_sha 的真实杀伤面)、首启命中谓词计数。

### D3. 受众 = 权威判定离线枚举(rev2 全面收紧)

```
audience(doc) = { s ∈ staff_dim(is_active=1) :
                  can_read_doc(ctx(s), acl(doc), grant_enabled=G, enforce_enabled=E) }
```

**a) 姿态对齐(rev2,评审 B1)**:G/E = `cfg.node_acl_grant / cfg.node_acl_enforce`,**必须与 serving 现值一致**;两键进 DW 节点 env(生成器 ENV_KEYS 补 `RAG_NODE_ACL_GRANT`/`RAG_NODE_ACL_ENFORCE`)。运行前置断言:存在 node 模式候选文档而 G=false → **整轮 HOLD + exit 3 + 告警**,绝不把 flag 缺失解析成 audience_zero。(同型现网 bug api.py:2034 已另行立项修复。)

**b) strict 权威(rev2,评审 B2)**:`resolve_doc_acl(doc_ids, cur, strict=True)`;`NodeAclAuthorityUnavailable` / 缺 meta 行 / 未知 mode → 该事件 HOLD。禁用宽松回落(宽松下 node 文档按真实 owner 组码超发)。

**c) groups 镜像优先级与在线逐条对齐(rev2,评审 B3)**:
1. `user_role` 行 `is_active=0`(墓碑)→ **权威空组,排除出一切受众**;
2. 任何 `user_role` 行(seeded 或自动缓存,不分 role)→ 按 `_normalize_dept_to_codes` 读回口径取组(白名单丢弃未知项,deny 哨兵 `__public_only__` 即仅 public);
3. 无行 → `staff_dim.dept_ids → dept_dim.name → _DEPT_NAME_TO_GROUPS`/生产中心子树集(复用 dingtalk_identity 的同一常量与函数,抽纯函数不复制表);
4. 运行前断言 `RAG_ACL_ANCESTRY` 与 serving 一致:现网 OFF 即镜像用名字口径;若开启,镜像改走 dept_ancestry 锚表(dept_dim.parents 物理链),不一致 → 整轮 HOLD。
- 残余数据面差异(TTL 窗内 user_role 陈旧行、staff_dim 日批陈旧)**有界且已申报**(§9);dry-run 报告输出"若干抽样用户离线组 vs 在线组"差异计数供观察。

**d) 总经办/org_wide(rev2,评审 M1)**:排除机制改为"**groups ⊇ 全量合法组集** 的用户按 knob 剔除"(对 legacy/node 两轨都生效;原"org_wide=False"仅 node 分支有效,对 legacy 形同虚设)。

**e) SUPPRESSED(audience_zero) 的前置条件(rev2,评审 M3)**:仅当【快照 fresh + strict 解析成功 + 姿态断言通过】全部成立才允许落;任一不成立 → HOLD(hold_reason 分词:stale_snapshot / authority_unavailable / posture_mismatch)。"这篇没人可读"与"我读不到权威"严格分开。

**f) 规模护栏(rev2 改 HOLD,评审 M3/B3)**:`audience > MAX_AUDIENCE` → HOLD(audience_cap);单轮新事件 > `MAX_EVENTS_PER_RUN` → 全批 HOLD(bulk_guard)。均可回放(`--requeue`),不再是终态。public 文档 → SUPPRESSED(public_policy)(语义性,默认不通知,knob);restricted → can_read_doc 恒拒 → audience_zero。

### D4. 存储 = 双表 outbox(fuling_operation,schema/065;rev2 修 DDL)

```sql
-- 065_doc_update_notify.sql → fuling_operation
CREATE TABLE IF NOT EXISTS doc_update_event (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  doc_id VARCHAR(100) NOT NULL,                   -- ↔ document_meta.doc_id 口径(013 惯例;跨库仅 Python 侧比对)
  version_no INT NOT NULL,
  prev_version_no INT DEFAULT NULL,
  content_changed TINYINT(1) DEFAULT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- PENDING|HOLD|RESOLVED|SUPPRESSED
  reason VARCHAR(32) DEFAULT NULL,                -- HOLD:stale_snapshot|authority_unavailable|posture_mismatch|audience_cap|bulk_guard
                                                  -- SUPPRESSED:unchanged_sha|missing_sha|public_policy|audience_zero|stale_switch|quarantined_or_retired|baseline
  audience_count INT DEFAULT NULL,
  resolved_at DATETIME DEFAULT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_doc_version (doc_id, version_no),
  KEY idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS doc_update_notice (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  event_id BIGINT UNSIGNED NOT NULL,
  doc_id VARCHAR(100) NOT NULL,
  user_id VARCHAR(128) NOT NULL,                  -- staffId(↔ qa_session_log.user_id 口径;不存姓名)
  channel VARCHAR(16) NOT NULL,                   -- console|dingtalk
  state VARCHAR(16) NOT NULL DEFAULT 'PENDING',   -- PENDING|SENT|READ|FAILED|SKIPPED
  reason VARCHAR(32) DEFAULT NULL,                -- SKIPPED:acl_revoked|quarantined_or_retired|stale|attempts_exhausted
  attempts INT NOT NULL DEFAULT 0,
  last_error VARCHAR(200) DEFAULT NULL,
  sent_at DATETIME DEFAULT NULL,
  read_at DATETIME DEFAULT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_event_user_channel (event_id, user_id, channel),
  KEY idx_user_state (user_id, channel, state, created_at),
  KEY idx_channel_state (channel, state, created_at)              -- 发送/重试扫描
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

- **rev2 通道行时机(评审 B4)**:resolve 只为**当时开启的通道**建行(console 恒建;dingtalk 仅子闸开时建)。子闸后开不补历史行——杜绝"console 先行一周后开钉钉,首轮把积压打包爆发"。另加**新近度规则**:dingtalk 发送时事件年龄 >3 天 → SKIPPED(stale)。
- 事件侧新近度(评审 M6):切换时点(dv_new.updated_at)距发现 >7 天 → SUPPRESSED(stale_switch)(restore/迟到收敛不冒充"更新")。
- 量级上限自算:50 事件 × 300 人 × 2 通道 = **3 万行/轮上限**(护栏值域内);留存窗 event 365d / notice 180d 自洽。
- 配套:ci_load_schema.sh 登记、DDL parity、schema_migrations 台账、retention.py 留存窗 + **`_purge_jobs` 增 notice 表**(user_id 键控个人数据,主体擦除必须覆盖)、schema/README.md 矩阵行+下一号修正。

### D5. 通道(rev2:同步有界发送 + 发送前复核)

- **console**:notice 行即站内信。端点见 §5;读时逐行 can_read_doc 复核(当前身份、当前权威),deny/异常即隐藏;unread_count = **复核通过后**的 PENDING 计数(不给被撤权用户留"红点=1 列表为空"的存在性信号)。
- **钉钉**:每用户每轮一条摘要(≤10 篇列名+"等 K 篇"),按渲染文本分组、**分批 ≤100 人循环**(修 admin_notify 截断模式)。**发送批出队后、HTTP 前,以当前权威对该批 (user,event) 重跑 can_read_doc**,deny → SKIPPED(acl_revoked)——把撤销/隔离窗从"天"压到"分钟"。**节点侧同步有界发送**(超时+errcode 判定,逐批落 SENT/FAILED+last_error);不用 admin_notify 的 daemon fire-and-forget(那是 serving 请求路径纪律;批处理 outbox 需要结果回写,且节点主进程退出会杀后台线程)。attempts≥3 → SKIPPED(attempts_exhausted)+告警。
- 复用 `dingtalk_card._get_access_token` + `RAG_DINGTALK_AGENT_ID`;`_http_post` seam + `_SEND_ASYNC=False`(tests monkeypatch,tests/test_admin_notify.py:13-17 范式)。**日志纪律:只记计数与 errcode,绝不记标题/文案正文**(admin_notify.py:84-87 同款)。
- 文案不带深链(文档详情路由待确认,§11);发送量每人每日 ≤1 条,远低于官方配额(以现值为准,不复述数字)。

### D6. 执行体 = DW 日调节点 + 业务模块

- `opensearch_pipeline/doc_update_notify.py`:`discover → resolve → send`,`main(argv)` 支持 `--commit`(默认 dry-run)、`--baseline`(见 §10)、`--requeue doc_id[:version]`(HOLD→PENDING 回放)。simulate 短路在 flag 之前(retention.py:236-239);入口显式 `assert_destructive_write_allowed("doc_update_notify", ...)`。
- `dataworks_nodes/doc_update_notify_node.py`:七步骨架照抄(py3.7 钉版仅 PyMySQL/DBUtils/requests——评审已 ast 验证 import 链 py3.7 干净;env 守卫先于 import;`RAG_SIMULATE_OPENSEARCH=true` 短路 R5;DRY_RUN 双闸;退出码 0/2/3)。调度建议每日 07:30(覆盖昨夜已收敛的切换;当日晚间收敛顺延一天)。
- paste 生成器 ENV_KEYS 增补:`RAG_DOC_NOTIFY`、`RAG_DOC_NOTIFY_DINGTALK`、`RAG_DINGTALK_AGENT_ID`、**`RAG_NODE_ACL_GRANT`、`RAG_NODE_ACL_ENFORCE`**(DINGTALK_CLIENT_ID/SECRET 已在清单)。

### 配置(config.py dataclass 成对写法,content_binding 同款)

| flag | 默认 | 语义 |
|---|---|---|
| `RAG_DOC_NOTIFY` | False | 总闸(独立新闸,不复用 RAG_ADMIN_NOTIFY——双闸纪律) |
| `RAG_DOC_NOTIFY_DINGTALK` | False | 钉钉通道子闸(console 先行) |
| `RAG_DOC_NOTIFY_INCLUDE_PUBLIC` | False | public 文档是否通知 |
| `RAG_DOC_NOTIFY_MAX_AUDIENCE` | 300(暂) | 单事件受众上限(→HOLD;**默认值待 §11 数据附件后定**——production 伞形受众很可能恒超 300) |
| `RAG_DOC_NOTIFY_MAX_EVENTS_PER_RUN` | 50 | 单轮新事件上限(→HOLD) |

## 4. 数据流全景

```
[钉钉组织] ─org_sync(日00:15)─▶ dept_dim/staff_dim ─┐(离线 AclContext;墓碑/缓存行优先级=在线同源)
document_meta × document_version                    │
   │ ①discover:谓词+隔离排除+sha门+新近度  ┌────────┴─────────┐
   ▼                                      ▼                  │
doc_update_event(PENDING|HOLD|SUPPRESSED) ②resolve:strict 权威+姿态断言+can_read_doc
   │                                       → doc_update_notice(仅已开启通道)
   ▼ ③send(dingtalk):发送前复核 → 分批 ≤100 → 同步落 SENT/FAILED
console:GET /api/kb/notices(读时复核)                (①②③=DW 日调节点,dry-run 默认)
```

## 5. 接口变化(serving)

新增 `routes/notices.py`(遵守 routes/__init__.py 铁律):
- `GET /api/kb/notices?limit=20` → `{items, unread_count}`。**identity 为 None 无条件 401**(不随 RAG_REQUIRE_AUTH);ctx 用 `_build_acl_ctx`(api.py:527)同源构造;逐行复核;title 从 fuling_knowledge 二次单查(不跨库 JOIN);表不存在(1146)→ 空列表降级(先部署后 apply 安全)。
- `POST /api/kb/notices/read` `{ids}|{all:true}` → 仅本人行,幂等。
- console-app:铃铛红点+列表+全部已读;新 e2e spec;加载四态惯例。

## 6. 修改范围

| 文件 | 动作 |
|---|---|
| schema/065_doc_update_notify.sql | 新增(§D4) |
| **schema/README.md** | 矩阵加 065 行 + 修"下一号"注 |
| scripts/ci_load_schema.sh | 登记 065→operation |
| opensearch_pipeline/config.py | 5 flag |
| opensearch_pipeline/doc_update_notify.py | 新增(discover/resolve/send/main/--baseline/--requeue) |
| opensearch_pipeline/dingtalk_identity.py | 微改:抽离线 groups 纯函数(在线行为零变化) |
| dataworks_nodes/doc_update_notify_node.py | 新增 |
| scratch/gen_dataworks_paste ENV_KEYS | 增补(§D6,含 NODE_ACL 两键) |
| opensearch_pipeline/routes/notices.py + api.py 挂载 | 新增 |
| opensearch_pipeline/retention.py | 留存窗 + **_purge_jobs 增表** |
| console-app(Bell/面板/store/e2e) | 新增 |
| tests/test_doc_update_notify.py 等 + **tests/conftest 串行组登记** | 新增 |
| scratch/apply_migration_065.py | 落库时新增 |

**不改**:pipeline_nodes / dag_definitions / dataworks_orchestrator / retriever / acl_policy / access_grants(只读消费)。

## 7. 边界情况

| 场景 | 行为 |
|---|---|
| 全量重建/维护重灌 | sha 门 SUPPRESSED;量大另有 bulk_guard(HOLD) |
| 同正文重传 | skip-gate 上游回退(仅 DW 路径);sha 门兜所有路径 |
| 纯 re-chunk / 贡献入库(恒 v1,contribution.py:453,467) | 不触发 |
| 新版入库前隔离/失败/LIMIT 推迟 | index_status≠SUCCESS 不触发;修复后自然补触发 |
| **发布后追溯隔离** | 谓词按 `_kb_version_quarantined` OR 语义排除;发送前再核,命中 → SUPPRESSED+SKIPPED |
| 连升两版(v3→v5) | 只对 current 建事件,通知一次 |
| retire/restore | dm.status≠active 排除;restore 后若首次满足谓词但切换时点 >7 天 → stale_switch 抑制 |
| reconcile 修复的搁浅切换 | 收敛后照常发现(spot_checker.py:655-680 置 SUCCESS+supersede) |
| 组织快照 >48h / 权威不可达 / 姿态不符 | 事件 HOLD(分词 reason)+告警,回放不丢 |
| org_sync 断供多日恢复 | HOLD 积压回放受 bulk_guard 节流(仍 HOLD,人工 --requeue 分批放) |
| 员工离职 | staff_dim.is_active=0 排除;console 读复核隐藏 |
| **员工部门调动** | staff_dim 陈旧 ≤~31h(00:15 同步+07:30 发送)→ 有界超发窗,§9 申报;发送前复核不闭合此窗(同源数据) |
| 事件后 ACL 变更/撤销 | 解析时点权威计算 + **发送前复核**(分钟级)+ console 读时复核 |
| dm↔chunk 权限漂移窗(C9 放宽延迟) | 受众按 dm 权威 ⊆ 授权面,无越权;可能通知"暂检索不到"的文档(方向安全) |
| 发送中途失败/节点被杀 | 同步发送逐批落状态;残留 PENDING 下轮续投;attempts 封顶 |
| 表未 apply / flag 关 | 节点 exit 2;serving 1146 降级;行为与今天逐字节一致 |
| SIM | simulate 短路,不触外网不落真库 |

## 8. 测试与验证

- **受众真值表**:legacy(owner/伞形/marketing/approved 授权)、node(subtree/exact/GRANT 关断言)、public/restricted、**墓碑行、deny 哨兵、自动缓存 employee 行优先、ancestry 姿态断言**、org_wide 剔除 knob、快照过期→HOLD——逐项断言与 can_read_doc 在线判定一致。
- 抑制/HOLD 矩阵:全部 reason 分词各一条;**audience_zero 的前置条件**(健康不成立时必须 HOLD 不得 SUPPRESSED)。
- 幂等/回放:重跑 discover 零新事件;--requeue 仅 HOLD 可回放;--baseline 全量盖章。
- 发送:>100 分批、同文本合并、发送前复核 deny→SKIPPED、FAILED 重试、attempts 封顶、**同步状态回写**;_http_post monkeypatch。
- routes:无条件 401、只见本人、READ 幂等、1146 降级、复核隐藏、unread_count 口径。
- DDL parity + ci manifest + conftest 串行组 + make test/lint/sim 绿。
- dry-run 报告:输出事件清单、各 reason 计数、抽样用户离线组 vs 在线组差异计数。
- **预期未验证项**:真实工作通知送达、DW 节点粘贴/发布/调度、生产 065 apply、§11 数据附件三查(均 user-gated)。

## 9. 风险与回滚

| 风险 | 缓解/申报 |
|---|---|
| 误发无权限者 | 权威函数同源 + strict + 姿态断言 + 墓碑/哨兵对齐 + 发送前复核 + 读时复核;**残余=staff_dim 日批陈旧窗(部门调动,≤~31h,方向超发但对象是昨日仍有权者)与 user_role TTL 窗——有界、已申报** |
| 广播已隔离文档标题 | 谓词排除 + 发送前再核(残余窗=复核到 HTTP 间秒级) |
| 静默漏通知 | HOLD 可回放 + reason 分词 + 告警;审计:事件台账全量留痕 |
| 群发骚扰 | 日摘要 + public 默认排除 + 双上限 + 通道渐进 + 新近度双规则 |
| 通知面↔检索面漂移 | 判定函数同源;数据面差异 dry-run 持续观测;不写第二套判定 |
| 回滚 | 全 flag 关=行为归零;表纯增量;serving 探测降级;无不可逆步骤 |

## 10. 上线次序

1. 代码合 main(全 flag 关,三绿)→ 2. staging 065 apply + 节点 dry-run 演练 → 3. §11 数据附件三查(prod-RO)定默认值 → 4. 生产 065 apply → 5. **`--baseline --commit` 显式基线**(对**全部** current>1 的 (doc,version) 无过滤插 SUPPRESSED(baseline)——把 retired/restricted/FAILED/隔离存量全部盖住) → 6. SAE 重打包(端点+UI) → 7. DW 节点铸造/粘贴/发布/调度 → 8. 语料重传收口后开 `RAG_DOC_NOTIFY`(console 先行) → 9. 观察 ≥1 周开 `RAG_DOC_NOTIFY_DINGTALK`(积压由通道行时机+stale 规则免疫)。

## 11. 尚未确定(Sam 拍板项)

1. public 文档是否通知(默认不)。
2. 总经办/org_wide 是否收(默认不)。
3. **MAX_AUDIENCE 默认值**:production 伞形文档受众大概率恒超 300 ⇒ 默认值=事实上静音最大部门;备选:按 owner 族分层上限,或放到 500+。**待数据附件**:(a) 前版 sha NULL 占比 (b) 首启命中谓词计数 (c) 按 owner_dept 的受众分布(均 prod-RO 只读)。
4. 钉钉通道开启节奏与调度时刻(建议 07:30)。
5. 文案与深链(需先确认 console 文档详情路由)。
6. 重传波期间姿态:若走"认领旧 doc_id 升版",bulk_guard 会 HOLD+告警,人工 --requeue 分批放行或整批 SUPPRESSED,由 Sam 定。

## 12. 评审记录

- 2026-08-04 替代评审(codex 额度受限,Sam 拍板先行):两名独立红队(权限泄露面 / 数据流运维)各自 REVISE;**全部 blocker 经主工程师逐条开源码核验属实并已修订入本稿**(B:姿态脱钩/strict 回落/groups 镜像优先级/追溯隔离/终态不可回放/通道积压爆发;M:总经办 legacy 无效/发送前复核/HOLD 语义/同步发送/purge_jobs/新近度)。一处评审证据纠偏:spot_checker 抽检路径本身置 index_status=DELETED(spot_checker.py:1046-1055),但"SUCCESS 残留"为 api.py:2745-2749 documented 现网态,修复方向不变。
- 顺产两个现网缺陷立项(与本设计独立):api.py:2034 can_read_doc 缺参、skip-gate 残留行升版 1062→500。
- **codex 补审:待 8/7 额度恢复后走 Phase 4-5 完整流程,通过前不实施。**
