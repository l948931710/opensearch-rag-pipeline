# 文档升版可见范围提醒(doc-update-notify)设计稿 _DRAFT · rev3

> 2026-08-04 · 分支 `feature/doc-version-notify` · 决策人 Sam
> 状态:**Claude 侧补审两轮完成(评审记录 §12);codex 跨模型终审待 8/7(Sam 可裁决是否需要);实施前须过 §11 拍板项**
> 需求一句话:**文档升级到新 version 并在检索侧生效后,提醒所有"对该文档有可见权限"的用户。**
> ⚠️ 行号锚定基线:main@4e79f16;main 当晚已推进(撞号修复等),**实施前全稿按 main 现头重锚**。

---

## 0. 摘要(TL;DR)

- **触发 = 日调状态扫描**,摄取路径几乎零改动(**唯一豁免**:复活死列 `activated_at`,见 D1c)。谓词排除追溯隔离件(NULL 安全 SQL);"current 停在失败版但更早版本已生效"有二次谓词兜底。
- **内容门 = 双指纹**:正文 `canonical_sha256` + 原始字节 `checksum_sha256`——正文变=text 更新;正文同而字节变=图集/格式更新(**管线自己的 skip 撤销语义**,P1-5);两者全同(重建 seed 复制同一 OSS 对象)才静音。
- **受众 = 权威判定离线枚举** + **姿态见证**:serving 把 (GRANT/ENFORCE/ANCESTRY/TTL) 写 `rag_runtime_contract` 见证行,节点每轮对照,缺失/过期/不符 → 整轮 HOLD。`resolve_doc_acl(strict=True)`;groups 镜像逐条对齐在线优先级(墓碑/seeded 永久权威/employee 行过 TTL 弃行/多部门并集)。
- **状态机**:PENDING / HOLD(可回放) / SUPPRESSED(语义终态);事件表=**防重放台账,永不删除**;显式 `--baseline` 在**开闸前一刻**执行。
- **发送 = 原子认领 + 同步有界**(PENDING→SENDING claim,杜绝双实例双发);发送前逐批 ACL 复核;每人每日 ≤1 条摘要。
- **通道 = console 站内 + 钉钉工作通知**(钉钉即触达移动端;小程序站内 inbox 留 v2,§11 拍板)。全 flag 默认关。

## 1. 目标与非目标

**目标**:①新版本检索侧生效后通知当时有可见权限的用户;②同一 (doc,version,user) 各通道最多一次(含并发/重跑);③通知面 ⊆ 检索面(函数同源+姿态见证;数据面残差有界并申报 §9);④绝不影响摄取/问答主流程。
**非目标(v1)**:diff 摘要;订阅偏好;"你新获得可见"通知;退役通知;实时推送;**小程序站内 inbox**(钉钉工作通知已覆盖移动端触达;inbox 留 v2)。

## 2. 现状与缺口(全部核验;行号=main@4e79f16 基线)

| 事实 | 证据 |
|---|---|
| 版本模型:`current_version_no` 指针 + `document_version` 每版一行;切换=CAS `index_status='SUCCESS'`+旧行 `superseded` 同事务 | schema/001:79,107-179、pipeline_nodes.py:7607-7644 |
| reconcile 会把搁浅切换收敛到 SUCCESS+supersede | spot_checker.py:655-680 |
| `document_version.updated_at` ON UPDATE 随任何列写刷新;`activated_at` 全仓零写方(死列) | schema/001:144-146 |
| version+1 ≠ 内容变:重建 seed 原样复制 raw_key(同一 OSS 对象 ⇒ checksum/etag 同) | scratch/rebuild_seed_versions.sql:39-56、scratch/seed_versions.py:58-67 |
| skip-gate:正文 sha 同但**资产集有新增 → 撤销 skip 照常升版**(管线权威的"内容变了"含图集维度) | pipeline_nodes.py:877,1412-1420 |
| 内容指纹双列都在:`canonical_sha256`(正文)/`checksum_sha256`+`etag`(原始字节) | schema/003:20-22、schema/001:118-119 |
| `can_read_doc(ctx, doc, *, grant_enabled, enforce_enabled)` 必填姿态;GRANT=false 对 node 无条件 DENY | acl_policy.py:243-290 |
| `resolve_doc_acl(strict)` 双语义;宽松回落=node 按真实 owner 超发;`resolve_acl_modes` **只有宽松语义** | access_grants.py:169-203,130-135,142-166 |
| 在线 groups:墓碑=权威空组;user_role 行 TTL 内权威;**employee 行过 TTL(默认 6h)穿透 API 不采信**;`_normalize_dept_to_codes`(:226-264) deny 哨兵→[];多部门并集 | dingtalk_identity.py:340-380,33-40 |
| org_wide 只在 node 分支消费;legacy 分支只看 groups | acl_policy.py:285-286,228-240 |
| 隔离唯一权威 `_kb_version_quarantined`(OR 语义);"隔离件 index_status 可残留 SUCCESS"documented | kb_console.py:3308-3313、api.py:2745-2749 |
| `staff_dim`(schema/060:139-149,无姓名)/`dept_dim`(:126-137);快照>48h fail-closed | dingtalk_identity.py:954-990 |
| `rag_runtime_contract` 运行时 KV 已存在(双库;knowledge=ops 心跳先例) | schema/018 |
| 工作通知通道 `admin_notify`(asyncsend_v2;100 人=截断非分页;fire-and-forget 是 serving 纪律非批处理纪律) | admin_notify.py:35,66-112 |
| DW env=粘贴时静态注入;`.env`/`.env.production` **均无** NODE_ACL/ANCESTRY 键(grep 计数 0);ENV_KEYS/NODE_FILES 见生成器 | scratch/gen_dataworks_paste_20260721.py:21-43 |
| retention 粒度=月(`_months`);归档族 RAG_RETENTION_ARCHIVE 默认 true;主体擦除=`_purge_jobs`(+归档侧 `_purge_archives_for_subject`) | retention.py:87-105,166-168,368-397,406+ |
| `user_role` 覆盖极小(07-28 prod-RO 实测约 57 行/1175 人;实施前重查) | 数据面事实 |
| schema 取号 **065**(main 至 064/op0 至 058);README「下一号」注过时(本次一并改) | schema/ 目录、git ls-tree |

## 3. 核心设计决策

### D1. 触发 = 状态扫描

**a) 主谓词**(fuling_knowledge 只读;跨库反连接在 Python 侧两查一比,不写跨库 SQL):

```sql
dm.status='active' AND dm.current_version_no > 1
AND dv_new.version_no = dm.current_version_no
AND dv_new.status='active' AND dv_new.index_status='SUCCESS'
AND NOT (UPPER(COALESCE(dv_new.publish_status,''))='QUARANTINED'
      OR LOWER(COALESCE(dv_new.gate_status,''))='quarantined')     -- NULL 安全三值逻辑:正常行含 NULL 列不丢行
AND EXISTS (旧版本行 status='superseded')
-- 反连接(无事件行)在 Python 侧完成;INSERT 用 INSERT IGNORE(uk_doc_version),双实例并发幂等
```

**b) 二次谓词**(补审B minor-6):对 `current_version_no` 指向的版本**非** SUCCESS 的文档(升版失败/隔离停滞),取其 `status='active' AND index_status='SUCCESS'` 且有 superseded 兄弟的**最高版本**建事件——覆盖"v(N) 已生效、当日 v(N+1) 注册后终态失败"导致 (doc,N) 永不建事件的边界。
**c) 切换时点锚 = `activated_at`(摄取路径唯一豁免)**:`updated_at` 被 ACL 投影/epoch/隔离等无关写刷新,不能当切换时点。复活 001:144 死列:`node_deactivate_old_chunks` 的 index_status CAS 与 `reconcile_stranded_versions` 的收敛 UPDATE 两处 SET 子句**补写 `activated_at=NOW()`**(纯增列写、零读方、失败无影响);discover 对 `activated_at IS NULL` 的行保守 SUPPRESSED(stale_switch)。新近度:`activated_at` 距发现 >7 天 → SUPPRESSED(stale_switch)。
**d) 隔离/退役是动态态**:发送前对每事件再读 dm.status + 真 helper 隔离判定,命中 → SUPPRESSED(quarantined_or_retired) + 全部 notice SKIPPED。
- 否掉的备选:挂 orchestrator 成功块 / 挂 kb_console 升版事务(理由同 rev1/rev2:双路径、生效前时刻、摄取安全面)。

### D2. 内容门 = 双指纹(rev3,补审B B-2)

前版选取 = "version_no<current 且 canonical_sha256 IS NOT NULL 的最新行"(skip-gate 同款,pipeline_nodes.py:1360-1362)。判定:

| canonical_sha256(正文) | checksum_sha256/etag(原始字节) | 判定 |
|---|---|---|
| 不同 | — | content_changed='text' → 通知 |
| 相同 | 不同 | content_changed='assets' → **通知**(正文没变但文件变了=图集/格式更新;与 skip-gate 的资产撤销语义对齐 P1-5) |
| 相同 | 相同 | SUPPRESSED(unchanged)——重建 seed 复制同一 OSS 对象必落此格 |
| 任一缺失 | — | SUPPRESSED(missing_sha)(保守) |

注:`RAG_SKIP_UNCHANGED_REINGEST` 只在 DW 节点 setdefault,非 DW 执行路径不生效——本门是对所有路径成立的保险。

### D3. 受众 = 权威判定离线枚举

```
audience(doc) = { s ∈ staff_dim(is_active=1) :
                  can_read_doc(ctx(s), acl(doc), grant_enabled=G, enforce_enabled=E) }
```

**a) 姿态见证(rev3,替代"env 对齐"愿望;补审A-1c/B-M3/M4)**:
- serving 在 config 加载与心跳时把 **(node_acl_grant, node_acl_enforce, acl_ancestry, acl_cache_ttl, stamped_at)** 写入 `rag_runtime_contract`(knowledge 侧,ops 心跳 P2-14 同款先例,schema/018);
- 节点**每轮启动(含纯发送轮)**读见证行:缺失/超 24h/与自身解析所需不符 → **整轮 HOLD(posture_unknown/posture_mismatch) + exit 3 + 告警**;判定用的 G/E/ancestry/TTL **一律取见证值**,不取节点自身 env ⇒ 正反两个方向的漂移(节点比 serving 严=漏发;节点比 serving 宽=超发)都被机制杜绝,SAE 控制台改 flag 无需重贴节点。
- "存在 node 候选"断言用 **strict 探测**(`_node_acl_columns_present(cursor, strict=True)` + 显式 acl_mode 扫描;`resolve_acl_modes` 只有宽松语义不可用于断言,access_grants.py:142-166);探测失败=整轮 HOLD(posture_unknown)。
- 发送前复核同样 `resolve_doc_acl(strict=True)` + 见证姿态。

**b) strict 权威**:`NodeAclAuthorityUnavailable`/未知 mode → HOLD(authority_unavailable);strict 结果缺 doc → 直查 document_meta 区分:行在 → HOLD;行真不在 → SUPPRESSED(meta_missing)(生产无删行路径,仅手工手术会触发,独立分词便于分诊)。
**c) groups 镜像(与在线逐条对齐;补审A-3/B-M2)**:
1. `user_role` 墓碑行(is_active=0)→ 权威空组,排除一切受众;
2. **seeded 行(role≠employee)不论年龄永久权威**(在线 H3 同款);**employee 行 age>TTL(TTL 取见证值,默认 6h)→ 弃行**,落第 3 条(近似在线穿透 API 后的结果);TTL 内 employee 行按 `_normalize_dept_to_codes` 读回(deny 哨兵→仅 public);
3. 无行/弃行 → `staff_dim.dept_ids`(**全部直属部门并集**)→ dept_dim.name → 名字表/生产中心子树集(复用 dingtalk_identity 同一常量,抽纯函数);
4. ancestry 姿态按见证:OFF=名字口径;ON=dept_ancestry 锚表走 dept_dim 物理链;见证与预期不符 → 整轮 HOLD。
**d) 总经办/org_wide**:排除 = "groups ⊇ 全量合法组集"的用户按 knob 剔除(两轨都生效)。
**e) SUPPRESSED(audience_zero) 前置条件**:快照 fresh + strict 成功 + 姿态见证通过,三者全立才允许落;否则 HOLD(分词 stale_snapshot/authority_unavailable/posture_*)。**HOLD 不建任何 notice 行。**
**f) 规模护栏**:`bulk_guard` 计数基准**唯一定义 = 本轮进入 resolve 的事件数(含回放)**,超 `MAX_EVENTS_PER_RUN` 的部分重新 HOLD(bulk_guard);`audience > MAX_AUDIENCE` → HOLD(audience_cap)。CLI:`--requeue doc_id[:version] | --requeue --all --limit N`(分批放行)、`--suppress doc_id[:version]`(人工止损,单向落 SUPPRESSED(manual))。public → SUPPRESSED(public_policy);restricted → audience_zero。

### D4. 存储(fuling_operation,schema/065;DDL 同 rev2 增两列语义)

两表结构同 rev2(ENGINE/COLLATE 显式、doc_id VARCHAR(100)、updated_at、idx_channel_state、last_error),增量修订:
- notice.state 增 **SENDING**(认领态,见 D5);event.reason 增 meta_missing/manual;
- **`doc_update_event` = 防重放台账,永不进留存删除、永不进归档**(反连接依赖行存在;≤50 事件/日 ⇒ 年 ~1.8 万行,体量无虞)——这同时消解"retention 清事件行 → 远古重发现"的回路;
- `doc_update_notice` 留存 **6 个月**(retention `_months` 粒度对齐),**不进 `_ARCHIVE_TABLES`**(user_id 个人数据不落 OSS 冷归档,免扩 `_purge_archives_for_subject`),**进 `_purge_jobs`**;
- unread 口径:**只数 console 通道**;
- 钉钉新近度锚 = **notice 建行时的 `event.resolved_at`**(回放重解析会刷新)→ 与 HOLD 回放不打架;它只封"落行后 >3 天仍未发出"的投递失败堆积(SKIPPED(stale)),不误杀回放。

### D5. 通道

- **console**:notice 行即站内信;读时逐行复核 = can_read_doc(当前身份/权威/见证姿态) **+ dm.status='active' + 隔离排除**(退役/隔离件的历史通知同样隐藏);unread_count = 复核通过的 PENDING 计数,**复核集截断为最近 50 行、显示 99+ 封顶**。
- **钉钉**:出队 = **原子认领**(`UPDATE ... SET state='SENDING', attempts=attempts+1, claim_ts=NOW() WHERE state IN ('PENDING','FAILED') AND channel='dingtalk' ... LIMIT n`;SENDING 超 30min 视为死认领可回收)——双实例(手动重跑×日调重叠)不双发;每用户每轮一条摘要(≤10 篇+"等 K 篇"),按渲染文本分组、分批 ≤100 循环;HTTP 前逐批以当前权威+见证姿态重跑 can_read_doc,deny → SKIPPED(acl_revoked);**同步有界发送**(timeout=5s/批,逐批落 SENT/FAILED+last_error;**每轮发送墙钟预算 30min**,余量留 PENDING 下轮续投);attempts≥3 → SKIPPED(attempts_exhausted)+告警。
- resolve 只为**当时开启的通道**建行;子闸后开不补历史行。
- 文案/日志/告警三面纪律:digest 标题**发送时现查**(改名文档以新名呈现,console 与钉钉可能异名——申报);日志与 **ops 告警只带 doc_id/reason/计数,绝不带标题**。
- `_kb_version_quarantined` **下沉到无 FastAPI 依赖的共享模块**(kb_console re-export 保持旧 import 面)——DW 节点不能 import routes/kb_console(拖 FastAPI)。

### D6. 执行体 + 配置

同 rev2(七步骨架/py3.7/dry-run 双闸/simulate 先于 flag/显式 env_guard),增量:
- ENV_KEYS 增补:`RAG_DOC_NOTIFY`、`RAG_DOC_NOTIFY_DINGTALK`、`RAG_DINGTALK_AGENT_ID`、`RAG_NODE_ACL_GRANT`、`RAG_NODE_ACL_ENFORCE`、`RAG_ACL_ANCESTRY`(节点自身解析途径需要;**判定姿态一律以见证行为准**,env 仅兜底);**NODE_FILES 增补 `doc_update_notify_node.py`**;
- **`.env.production` 补值并与 SAE 控制台 env 现值核对**是上线步骤(§10),不是隐含前提(两文件现均无这些键,grep 计数 0)。
- flags 同 rev2 五个;调度 07:30(当日晚间收敛顺延一天)。

## 4. 数据流全景

```
serving(SAE) ──心跳──▶ rag_runtime_contract(姿态见证 G/E/ancestry/TTL)◀──每轮断言── DW 节点
[钉钉组织] ─org_sync─▶ dept_dim/staff_dim ─┐(离线 AclContext:墓碑/seeded 永久/employee TTL/并集)
document_meta × document_version           │
  ①discover:主+二次谓词/NULL安全隔离排除/双指纹门/activated_at 新近度
  ▼                                        ▼
doc_update_event(PENDING|HOLD|SUPPRESSED)  ②resolve:strict+见证姿态+can_read_doc → notice(仅开启通道)
  ▼ ③send:原子认领(SENDING)→发送前复核→分批≤100→同步落状态(30min 预算)
console:GET /api/kb/notices(读复核+退役隔离排除+限频)     (①②③=DW 日调,dry-run 默认)
```

## 5. 接口(serving)

同 rev2(routes/notices.py 两端点、无条件 401、_build_acl_ctx 同源、1146 降级、title 二次单查),增量:**两端点挂 `_enforce_rate_limit`**(api.py:648;kb 端点全挂先例);unread 截断/封顶见 D5;前端轮询=导航切换时 + ≥60s 间隔;读复核加退役/隔离排除。

## 6. 修改范围(rev2 基础上增量)

| 文件 | 增量 |
|---|---|
| opensearch_pipeline/pipeline_nodes.py | **activated_at 补写一处**(deactivate CAS SET 子句) |
| opensearch_pipeline/spot_checker.py | **activated_at 补写一处**(reconcile 收敛 UPDATE) |
| serving config 加载/心跳 | 姿态见证写入(rag_runtime_contract,018 已有表零 DDL) |
| 共享模块(如 doc_state.py) | `_kb_version_quarantined` 下沉;kb_console re-export |
| opensearch_pipeline/retention.py | notice 6 个月窗 + 不进归档 + _purge_jobs;event 显式豁免 |
| scratch/gen_dataworks_paste | ENV_KEYS 六键 + NODE_FILES 增补 |
| CLI | --baseline/--requeue(单个+--all --limit)/--suppress |
| 其余 | 同 rev2(065/ci_load_schema/config/doc_update_notify.py/notify 节点/routes/notices.py/console-app/README/conftest/apply 脚本) |

## 7. 边界情况(rev2 全表仍有效,增改)

| 场景 | 行为 |
|---|---|
| 正文没变但图集/文件变(P1-5 族) | **通知**(content_changed='assets';rev2 会误静音) |
| 重建 seed(同一 OSS 对象) | 双指纹全同 → SUPPRESSED(unchanged) |
| 双实例并发(手动重跑×日调) | discover INSERT IGNORE 幂等;send 原子认领不双发 |
| current 停在失败版但更早版已生效 | 二次谓词兜底建事件 |
| HOLD 回放 | 重解析刷新 resolved_at → 钉钉不被 stale 误杀;回放量入 bulk_guard 计数 |
| retention 清理 | event 永不删(台账);notice 6 个月+purge_jobs+不归档 |
| 正常行 publish/gate 列为 NULL | COALESCE 谓词不丢行(三值逻辑) |
| 隔离/退役后的历史 notice | console 读侧同样隐藏(dm.status+隔离排除) |
| serving 在 SAE 改 ACL flag | 见证行次轮生效,节点自动 HOLD/放行,无需重贴 |
| (其余同 rev2 表:入库前隔离/连升两版/离职/调动窗/dm↔chunk 漂移/表未 apply/SIM 等) | |

## 8. 测试与验证(rev2 基础上增)

并发双实例(认领互斥+INSERT IGNORE);sha 同+字节异 → 通知;双同 → 抑制;过期 employee 行降级/seeded 永久/多部门并集;见证缺失/过期/不符 → HOLD;NULL 列不丢行;回放后钉钉可达(resolved_at 锚);退役件读侧隐藏;unread 截断口径;--suppress/--requeue --limit;activated_at NULL → 保守抑制;限频生效。**预期未验证项**:同 rev2 + SAE env 三值现查 + 见证行现网首写。

## 9. 风险与回滚(rev2 基础上修订申报)

- 误发残余:staff_dim 日批陈旧窗(调动,≤~31h)+ **"从未再触发在线解析的用户"在 TTL 窗内的镜像残差**(过 TTL 弃行修法已把无上界漂移收敛到 TTL 窗);反向(节点比 serving 宽)已被见证机制杜绝。
- 基线损失:`--baseline` 在开闸前一刻执行,损失窗=基线前 ≤7 天的真实升版(显式申报;量化见 §11 数据附件)。
- 回滚:全 flag 关=行为归零;activated_at 补写为纯增列写(失败不影响主流程,回滚即删两行代码);见证写入 fail-open;表纯增量。

## 10. 上线次序(rev3)

1. 代码合 main(全 flag 关,三绿)→ 2. staging 065 apply + dry-run 演练(输出受众差异计数)→ 3. **数据附件三查+切换时点直方图**(prod-RO,user-gated)定 MAX_AUDIENCE 等默认 → 4. 生产 065 apply → 5. SAE 重打包部署(端点+UI+**姿态见证写入**)并确认见证行落库 → 6. **`.env.production` 补 NODE_ACL/ANCESTRY/AGENT_ID 值并与 SAE 控制台现值核对** → 7. DW 节点铸造/粘贴/发布/调度 → 8. **开闸前一刻 `--baseline --commit`** → 9. 语料重传收口后开 `RAG_DOC_NOTIFY`(console 先行)→ 10. 观察后开 `RAG_DOC_NOTIFY_DINGTALK`。

## 11. 尚未确定(Sam 拍板项)

1. public 文档是否通知(默认不)。2. 总经办是否收(默认不)。
3. MAX_AUDIENCE 默认值 vs production 伞形(默认 300 恐 HOLD 最大部门;分层上限备选)——**数据附件**:(a) 前版 sha NULL 占比 (b) 首启命中数 (c) 按 owner 受众分布 (d) 近 30-60 天切换时点直方图(基线损失量化)。
4. 钉钉通道节奏/调度时刻(建议 07:30)。5. 文案与深链(待确认文档详情路由)。
6. 重传波期间姿态(bulk_guard HOLD 后人工 --requeue --limit 分批 or --suppress)。
7. **小程序触达面**:v1 移动端靠钉钉工作通知(开闸后);console-only 观察窗内多数员工零触达——窗长与是否接受,Sam 定;miniapp 站内 inbox 列 v2。
8. **与 C3′ 版本轴方案的共同依赖声明**:两稿都写 document_version(C3′ 触碰 updated_at 正是 D1c 弃用它的原因);8/7 codex 同批审时互相引用。

## 12. 评审记录

- **R1(2026-08-04,替代评审)**:两路红队(权限泄露面/数据流运维)REVISE;6 blocker 全核验属实,修订入 rev2。证据纠偏一处(spot_checker 抽检路径实置 DELETED)。
- **R2(2026-08-04,Claude 侧补审,Sam 拍板执行)**:补审A(闭合性核验)= 2 项 CLOSED、5 项残洞(姿态见证缺失/探测 strict 化/employee TTL/回放×新近度冲突/bulk_guard 定义矛盾/NULL 三值逻辑/基线步序);补审B(零先验全稿)= 2 blocker(发送无并发互斥;内容门漏图集维度)+ 6 major(updated_at 污染×retention 回路/env 值缺失/小程序缺位/unread 成本等)。**全部经主工程师逐条开源码核验属实**(含 `_asset_additions_block_skip`:877/:1413、activated_at 死列 001:144、env 键计数 0),修订入本 rev3;无一项驳回,一项降格(strict 缺行=生产无触发路径,配 meta_missing 分诊分词)。
- 顺产缺陷两枚均已由 Sam 开工的芯片会话修复落 main(api.py can_read_doc 姿态参数;skip-gate 撞号双修)。
- **codex 跨模型终审:8/7 额度恢复后可选**——Claude 侧同模型盲区(如有)非自审可覆盖,是否加审 Sam 裁决;届时与 C3′ 稿同批。
