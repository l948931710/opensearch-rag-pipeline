# PR-4 摄取台账 lease/fencing 立项 scope

> 日期：2026-07-17
> 基线：`claude/ontology-p0` @ `1109f51`（PR-3 四期已全落）
> 来源：效率评审核查（docs/audits/perf_efficiency_review_verification_2026-07-17.md）三项 NECESSARY 之首；CLAUDE.md gotchas 自认 open gap「no lease/heartbeat *during* a live run」
> 定位：**并发开关（RAG_EXTRACT/LOADER_FETCH/PUBLISH/HA3_PUSH/EMBED_CONCURRENCY，perf backlog user-gated 尾项）开启前的正确性前置**。单实例日跑+默认并发 1 的现状下风险可容忍——本项是「开并发之前必须先落」的事，不是现网急修。
> 状态：**已拍板并落地**（2026-07-17 Sam「go, defaults as proposed」→ tracking ID=PR-4，默认全按本 scope：TTL/RENEW=900/300、丢锁=弃单文档、stage-1 维持 non-goal）。W1-W4 全部完成，as-built 差异见 §6。

---

## 1. 问题陈述（现状与危害）

摄取两本台账（`document_version.content_process_status`＝stage-1/2、`document_version.index_status`＋`chunk_meta`＝stage-3）的互斥全靠**一次性 CAS 认领 + 2 小时年龄式失效接管**，没有运行中续租、没有持有者身份、没有写回栅栏：

| # | 现状 | 危害（并发>1 或 DataWorks 重试重叠时） |
|---|---|---|
| H1 | 认领后无续租：`_reset_stale_stage2_locks`（`dataworks_orchestrator.py:594-619`）与 stage-3 takeover（`pipeline_nodes.py:5513-5533`）都按 `updated_at < NOW()-2h` 判死 | **>2h 的存活运行被当尸体接管**——大 PDF 的 OCR/VLM 漏斗、大批 chunk 的 LLM 分类都可能超 2h。接管后两个写者同时在跑同一 (doc_id,version) |
| H2 | 被接管的僵尸不知道自己已丢锁，继续写回 | **写回无栅栏**：僵尸的 `node_write_chunk_meta` DELETE→INSERT 与新持有者交错＝撕裂 chunk 集；僵尸的终态写（DONE/FAILED/SUCCESS/NOT_INDEXED）晚到覆盖新持有者状态＝台账翻转；最坏是僵尸基于陈旧视图跑 `node_deactivate_old_chunks`（is_active=0 + **HA3 不可逆删除**） |
| H3 | 崩溃恢复延迟恒为 2h（与真实死亡时刻无关） | 当日批次窗口内文档卡死 2h 才能重捡 |
| H4 | 裸跑（非 orchestrator 的 `run_simulation`/手工 DAG）不带 stage-2 清扫 | 裸跑崩溃残留的 LOADING/PROCESSING 要等下次 orchestrator stage-2 才回收（既有已知项，本项顺带改善恢复速度但不改变「谁来扫」） |

**为什么现在立项**：效率评审 P1 主推的就是拉高摄取并发；并发>1（或多 DataWorks 实例重叠、自动重试）一旦发生，H1/H2 从理论变成必然。PR-3 的 lease 只覆盖 agent 命令面（`agent_dispatch_command`），**不覆盖摄取台账**——这是核查确认过的边界。

## 2. Goals / Non-goals

**Goals**
1. 认领带租约（holder + 到期 + epoch），运行中续租——存活持有者不再被接管（消 H1）。
2. 台账终态写与破坏性写带栅栏——丢锁僵尸的写回原子性失败（消 H2），`node_deactivate_old_chunks` 升级为「同事务验租后才允许 is_active=0」。
3. 失效接管从固定 2h 变为「租约到期即接管」（TTL 级恢复，消 H3；对无租约旧行保留 2h 兜底）。
4. 全程 flag 门控默认 off，off 臂行为与现状逐字节等价；additive schema，可先 apply 后部署。

**Non-goals**（明确不做，防 scope 蔓延）
- 不做 stage-1 认领（stage-1 无锁属既有设计，双跑=幂等浪费不腐蚀数据；`_count_pending_rows` docstring 明示 no-progress 守卫为此而在。若并发灰度后实测浪费显著再单独立项）。
- 不做 RDS↔HA3 2PC（既有 open gap，PENDING_DELETE 对账是既定模式，本项不碰）。
- 不做全局 DashScope 并发预算（核查的另一项 NECESSARY，**独立姊妹立项**——本项管正确性，它管配额）。
- 不改 DAG-3 节点顺序、不动 never-disappear 不变量（本项只让 deactivate **更难**误触发）。
- 不引入外部组件（无 Redis/ZK——租约就在 RDS 行上，沿 PR-3 043 模式）。

## 3. 设计

### 3.1 Schema：`schema/048_ingest_lease.sql`（additive）

`document_version` 加三列（一套租约列服务两本台账——同一 dv 行同一时刻只处于一个 stage）：

```sql
ALTER TABLE document_version
  ADD COLUMN lease_holder     VARCHAR(64)  DEFAULT NULL COMMENT '认领方实例 id（host-pid-uuid8），语义同 agent_dispatch_command.lease_holder',
  ADD COLUMN lease_expires_at DATETIME(3)  DEFAULT NULL COMMENT '租约到期（NULL=旧包/未启用租约的认领，走 2h 年龄兜底）',
  ADD COLUMN lease_epoch      INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'fencing token：每次认领/接管 +1；写回带 epoch 谓词';
-- 观测/接管扫描辅助（谓词仍以 status 列打头，选择性来自既有 idx）：
ALTER TABLE document_version ADD KEY idx_lease_expiry (lease_expires_at);
```

`chunk_meta` **不加列**——认领单位是 (doc_id, version_no)，dv 级租约覆盖其下所有 chunk 写。
入 `MIGRATION_MANIFEST.tsv` + `schema_migrations` 台账行（F-35 纪律）；fuling_knowledge 库（staging/prod apply 为 user-gated 尾项，同既有惯例）。

### 3.2 租约协议（新模块 `opensearch_pipeline/ingest_lease.py`，~150 行）

- **holder id**：进程启动铸一次 `{hostname[:24]}-{pid}-{uuid8}`（模块级单例，同 PR-3 worker id 风格）。
- **时钟纪律**：所有租约时间算术在 SQL 侧（`NOW(3)`），Python 永不比较墙钟——多机部署无时钟偏斜问题。
- **claim**：在三个既有认领 UPDATE 的 SET 中追加 `lease_holder=%s, lease_expires_at=NOW(3)+INTERVAL %s SECOND, lease_epoch=lease_epoch+1`（谓词不变）。认领成功后回读本行 epoch（或 `SELECT LAST_INSERT_ID` 不可用则单查——批量认领时按 (doc,ver) IN 一次回读）。
- **renew**：`UPDATE ... SET lease_expires_at=NOW(3)+INTERVAL ttl WHERE doc_id=%s AND version_no=%s AND lease_holder=%s AND lease_epoch=%s`；rowcount==0 ⇒ `LeaseLost`。renew 恒改 lease_expires_at，**天然规避 changed-rows 陷阱**（连接池未开 CLIENT_FOUND_ROWS，同值 UPDATE rowcount=0——stage-3 takeover 注释里已埋过的坑，实现时所有栅栏语句必须保证至少一列真实变化或显式带 `updated_at=NOW()`）。
- **fenced write**：终态/破坏性写回的 WHERE 追加 `AND lease_holder=%s AND lease_epoch=%s`；rowcount==0 ⇒ `LeaseLost`。多语句写回（见 3.4 的 write_chunk_meta / deactivate）改为**同事务先 `SELECT ... FOR UPDATE` 验租再写**——「验租与副作用同事务」，语义同 045「台账行与副作用同一事务」。
- **takeover**：接管谓词从 `updated_at < NOW()-2h` 改为
  `(lease_expires_at IS NOT NULL AND lease_expires_at < NOW(3)) OR (lease_holder IS NULL AND updated_at < NOW()-INTERVAL 2 HOUR)`
  ——新行按租约、旧包/裸跑遗留的无租约行按 2h 兜底，无缝混跑。接管本身也是一次 claim（epoch+1），旧持有者从此所有 renew/fenced write 原子失败。
- **丢锁策略（僵尸侧）**：`LeaseLost` ⇒ **放弃该 doc 剩余写回、记 WARNING、继续批内其他文档**（该 doc 由新持有者或下轮 drain 收口）。不因单文档丢锁把整 stage 打红——与 graceful-degradation 惯例一致；丢锁计数进 run metrics，全批丢锁时 no-progress 守卫自然兜底。

### 3.3 参数与 flag

| env | 默认 | 说明 |
|---|---|---|
| `RAG_INGEST_LEASE_ENABLE` | `false` | 总闸。off＝不读不写租约列，与现状逐字节等价（含 2h 接管原样）；on＝全协议 |
| `RAG_INGEST_LEASE_TTL_S` | `900` | 租约时长（15min） |
| `RAG_INGEST_LEASE_RENEW_S` | `300` | 续租间隔（TTL/3；按文档/批粒度在既有循环里顺手续，不开后台线程） |

不开心跳线程：续租挂在既有循环的天然节拍上（见 3.4 各续租点）——循环本就逐文档/逐批打 DB，加一条微 UPDATE 即可；两次节拍间隔理论上可能超 TTL 的极端场景（单文档单阶段 >15min 无任何 DB 触点，如超大 PDF 的纯 OCR 段）在续租点选择上覆盖（OCR/VLM 逐页/逐图回调处按 RENEW_S 节流续租）。

### 3.4 接线清单（touch points，行号为 1109f51 基线）

**W2 — stage-2（content_process_status 台账）**
| 位置 | 动作 |
|---|---|
| loader 认领（`dataworks_orchestrator.py:219-241`，SKIP LOCKED→LOADING） | claim 加租约戳 |
| classify 认领（`pipeline_nodes.py:1532/1554`，→PROCESSING） | claim 加租约戳（LOADING→PROCESSING 属同 holder 交接：holder 不变、epoch 不变、只刷 expiry；跨进程场景不存在——同一 stage-2 运行内） |
| 续租点 | classify 逐文档循环、chunker 逐文档、publish 上传循环（`pipeline_nodes.py:4974-4990`）、`node_write_chunk_meta` 逐文档——按 RENEW_S 节流 |
| 终态写 fenced | DONE（`:5377`）、NEEDS_REVIEW（`:5429`）、FAILED（`:1781/:5331`） |
| `node_write_chunk_meta` DELETE→INSERT | 同事务 FOR UPDATE 验租后执行（撕裂 chunk 集的根治点） |
| 清扫（`dataworks_orchestrator.py:594-619`） | 谓词升级为 3.2 takeover 形态 |

**W3 — stage-3（index_status + chunk_meta 台账）**
| 位置 | 动作 |
|---|---|
| 认领三支（`pipeline_nodes.py:5484-5533`：批量 CAS / SUCCESS-relock / stale-takeover） | 三支全加租约戳；takeover 支改 3.2 谓词 |
| 续租点 | embed 逐批（`:6264` 循环）、HA3 push 逐子批（`:6712` 循环） |
| chunk_meta index_status 批量回写 | fenced（dv 级验租一次 + 批量 UPDATE 同事务） |
| dv 终态（SUCCESS / 边界复位 NOT_INDEXED，`:6027` 一带） | fenced |
| **`node_deactivate_old_chunks`（`:5746-5791` 完整性闸一带）** | 完整性闸之外再加「同事务验租」硬门：验租失败＝整个 deactivate 跳过（连 HA3 删除一起不发）。HA3 删除仍在 RDS commit 之后、失败走既有 PENDING_DELETE 对账 |
| `_count_pending_rows(3)` 谓词（`dataworks_orchestrator.py:658-666`「非 PROCESSING 或已过 2h」） | 与新 takeover 谓词对齐（否则计数集≠可认领集，no-progress 守卫误报）；stage-3 loader 的同款过滤同步改 |

**W1 — schema + 模块**：048 + manifest + `ingest_lease.py`（helper 全部走 `db._get_db_conn`，**保住既有 patch 契约**——tests 打 `db._get_db_conn` 一处即 mock 全部租约 SQL）。

**W4 — 测试/文档/收口**
- 故障注入回归（新文件 `tests/test_ingest_lease.py`，真库族 ⇒ **必须进 conftest 串行组**，xdist 惯犯教训）：≥12 项——持有者死亡后 TTL 接管；>TTL 存活但按时续租不被接管；僵尸终态写 fenced-out；僵尸 write_chunk_meta fenced-out；僵尸 deactivate fenced-out（连 HA3 mock 断言零调用）；epoch 复用拒绝；无租约旧行走 2h 兜底；混合批部分丢锁只弃单文档；flag off 臂逐字节现状（既有 stage2/3 回归全绿即证）；`_count_pending_rows` 对齐；同值 UPDATE changed-rows 陷阱回归；renew 节流。
- `make sim-all` + `make test` + `make lint` 全绿（sim 模式租约整体 no-op——RAG_SIMULATE 无 RDS，节点既有分支即天然跳过）。
- CLAUDE.md gotchas 更新（open gap → 「已闭合，flag 默认 off」）；perf backlog 并发尾项处加前置引用；部署说明：**apply 048 → 重打 DataWorks 包 → DataWorks 节点 env 开 flag（与并发开关同一次灰度批）**。

### 3.5 兼容性与风险

| 风险 | 处置 |
|---|---|
| 新旧混跑（048 已 apply、旧包还在跑） | 旧包无视新列（additive）；旧包认领的行 lease 为 NULL → 新清扫按 2h 兜底处理。**残留窗口**：旧包的 2h 清扫仍可能接管新包 >2h 存活持有者——单日单实例现状下窗口为理论值，灰度期以「先包后 flag」顺序压缩 |
| 认领批量化路径（E#46 集合式 UPDATE） | 集合式 SET 对全批统一戳 holder/expiry；epoch+1 逐行自增语义在集合式 UPDATE 下天然成立；回读 epoch 一次 IN 查询 |
| 续租/栅栏新增 SQL 量 | 每文档每 5min 一条微 UPDATE + 每终态写一个谓词——相对批内 embedding/push 可忽略（同 COUNT(*) 核查结论的量级逻辑） |
| ALTER 大表 | document_version 行数千级，DATETIME(3)/INT 列 INSTANT/INPLACE，秒级 |
| 语义回退 | kill switch＝flag off 即回现状；048 列留存无害 |

## 4. 工作量与顺序

W1 schema+模块 ~0.5d → W2 stage-2 ~1d → W3 stage-3 ~1d → W4 测试/文档 ~0.5-1d：**合计 ~3-3.5 天**（单人，全程 SIM/本地真库可验，不需要碰 staging/prod；staging/prod 048 apply 与 flag 灰度是既有 user-gated 尾项的一部分）。W2 与 W3 可独立提交（各自 flag 臂内自洽），沿「一批一提交、老代码必红回归」惯例。

## 5. 待拍板（Sam）

1. **立项确认与 tracking ID**（沿 PR-N？还是并入批次7 之后的下一批？）。
2. TTL/RENEW 默认 900s/300s 是否可接受（更保守可 1800s/600s——恢复慢一点、误接管更远）。
3. 丢锁策略确认：弃单文档继续批（本 scope 推荐）vs 整 stage 打红。
4. stage-1 无锁维持现状（本 scope 推荐 non-goal）是否同意。
5. 姊妹立项「全局 DashScope 并发预算」是否同窗启动（本项不依赖它，但两者都是并发开关的前置，同一次灰度收口最省事）。

---

## 6. 拍板与 as-built 记录（2026-07-17 落地）

**拍板**：Sam「go, defaults as proposed」——tracking ID=**PR-4**；TTL/RENEW=900s/300s；
丢锁=弃单文档继续批；stage-1 维持 non-goal。姊妹项「全局 DashScope 并发预算」未同窗启动（另行拍板）。

**落地提交**（`claude/ontology-p0`）：W1=schema/048+`ingest_lease.py`+15 单测；W2=stage-2 接线；
W3=stage-3 接线（+2 源检守卫改 seam）；W4=真库故障注入 9 项（conftest 串行组）+本文档+CLAUDE.md/
perf backlog 收口。全套 `make test` 3979 绿 + `make lint` 绿 + `make sim-all` 绿；048 已 apply
本地 fuling_knowledge（staging/prod apply=user-gated 尾项）。

**as-built 与 scope 的差异**（实现中修正的设计点）：

1. **续租改批语义**（§3.2 修正）：per-key 续租护不住还在队尾排队的文档（批量认领 100 篇、
   classify 是最长阶段）→ 实现为 `LeaseSet.renew_all`（谓词只按 holder；rowcount 短缺时回读
   holder=me 存活集对账丢弃）+ 全局节流 `should_renew`（免 DB 预判）；触点=classify
   as_completed/publish 上传/write_chunk_meta 收尾/embed 批/push 批五处 `_lease_renew_tick`
   （用 pipeline_nodes._get_db_conn——patch 契约保持）。per-key `maybe_renew` 保留备用。
2. **每次认领恒 epoch+1**（§3.4 W2「LOADING→PROCESSING 交接 epoch 不变」修正）：loader 认领与
   classify 认领之间没有任何栅栏写，epoch 恒 +1 更简单且严格更安全；顺带天然规避同值 UPDATE
   changed-rows 陷阱（认领必改行）。
3. **deactivate 双重验租**（§3.4 W3 单点「同事务验租」细化）：不可逆 HA3 删除前用**无锁快照**
   `verify_still_held`（不能抱行锁跨网络调用；验租通道故障 fail-closed 拒删），RDS 收尾事务首
   再 FOR UPDATE 验租（行锁持至 commit）。快照后-删除中被接管的残余窗口＝双方删同一批旧 PK，
   幂等无害。丢锁按 doc 粒度剔除（删除集/收尾集/审计行三处同步），批内其他文档照常。
4. **终态写=释放**：所有 fenced 终态写顺带 `lease_holder=NULL, lease_expires_at=NULL` 并本地
   `discard`——「租约列非空 ⇔ 有人自认在跑」，后续同 doc 辅助写（如 vlm NEEDS_REVIEW 覆写）
   走无栅栏旧语义（自有终态后的良性覆写）。
5. **off 臂零足迹**：`get_lease_set` 在 flag off 时返回模块级空实例且不写 ctx（LeaseSet 含
   threading.Lock，进 ctx 会破坏测试对 ctx 的 deepcopy）；`LeaseSet.__deepcopy__` 返回自身
   （进程域资源句柄语义）。
6. **协议模块 cursor 传入制**（§3.4 W1「helper 走 db._get_db_conn」加强）：`ingest_lease.py`
   完全不管理连接——所有函数在调用方事务的 cursor 上执行（「验租与副作用同事务」由构造保证）；
   唯一开池连接的是 pipeline_nodes 的续租触点。
7. **update_index_status 的 dv FAILED 收尾**不拼栅栏谓词只清租约：该写已有 PROCESSING CAS 且
   事务首验租+行锁在手，再拼 epoch 谓词会与 CAS 的 rowcount 判读打架（复位型终态，语义同 3）。

**测试对照**（scope §W4 清单 → 实测落点）：TTL 接管/存活续租免接管/2h 兜底=真库 T2/T2L/T3；
僵尸终态 fenced-out=T4；验租-写回零窗口=T5（行锁阻塞实测）；部分丢锁对账=T6；off 臂=T7+全量
既有回归（3979 绿）；changed-rows 陷阱=既有 H2/Q 行为测试照过+epoch 恒增设计消解；epoch 复用
拒绝=T4（epoch 谓词）；「僵尸 write_chunk_meta/deactivate fenced-out」在节点层为同事务验租+
显式剔除路径（W2/W3 代码审读+T4/T5 协议级证明），节点级端到端注入留给 staging 灰度探针。

**部署顺序**（进 user-gated 尾项清单）：staging/prod apply 048 →（重打 DataWorks 包）→
DataWorks 节点 env 开 `RAG_INGEST_LEASE_ENABLE=true`（与并发开关同一次灰度批，先 flag 后并发）。
新旧混跑窗口语义见 §3.5。

---

## 附录 A：main 移植版差异与启用前置 runbook（2026-08-02，codex 六阶段共识）

本文档正文以 `claude/ontology-p0`（基线 1109f51）为准；2026-08-02 起 DataWorks 生产包改从
main 构建，PR-4 全套随之移植回 main（基线 b31f776）。移植版与正文的**刻意差异**：

1. **台账登记**（修正 §3.1）：main 无 `MIGRATION_MANIFEST.tsv`——048 逐字节复制
   （checksum 台账约束，`schema/README.md` 铁律 2），登记走 `scripts/ci_load_schema.sh`
   MANIFEST + `schema/README.md` 矩阵。**staging/prod/CI 三环境已 apply**（正文 §W1
   「user-gated 尾项」已完成，2026-07 期间执行）。
2. **续租触点如实声明**（修正 §3.2 括注）：实际接线为 classify 循环尾/publish 上传/
   write_chunk_meta 收尾/embedding 批/HA3 push 批——**没有** OCR/VLM 逐页回调续租点。
   单文档单阶段 >TTL 无 DB 触点的极端场景（超大 PDF 纯 OCR 段）仍可能被接管，
   属已接受的稀疏续租设计；启用后如实测命中，调 `RAG_INGEST_LEASE_TTL_S` 而非加线程。
3. **失锁墓碑**（强化 §3.2）：非自愿失锁在 LeaseSet 留墓碑，该 key 后续 fence/verify
   一律拒绝（op0 版 discard 后栅栏退化 no-op 的洞已闭）；自愿释放（终态后 discard）
   语义不变。
4. **分类持久化不以 rowcount 判租**（修正 as-built 点 7 同族）：frozen/contribution/
   LLM-success 三路径的 dv SET 可能全同值（frozen 路径是确定性字面量，重试必
   changed-rows=0），改走事务内 `verify_for_update`（行锁+holder/epoch 相等判定），
   语句序保持 main 的 meta→dv 全局纪律。
5. **parity 的 dv FAILED 回滚块整体挂 flag 门**：off 臂与 main 现状逐字节一致（不新增
   任何写）；on 臂 ownership 前置过滤，全丢锁不写不抛（弃单归新持有者，下游
   deactivate 的 verify_still_held 兜底）。
6. **deactivate HA3 失败事务逐 doc 前置验租**：闭「快照验证后、网络删除期间被接管」
   窗口（正文 §3.5 已知残余，移植版收窄到 HA3 删除本身的幂等无害域）。

### 启用前置 runbook（user-gated，逐步授权）

背景：2026-07-22~08-01 十天 op0 包窗口内 DW 节点 `setdefault RAG_INGEST_LEASE_ENABLE=true`
**实际生效过**（b31f776 留碑）；flag-off 认领不清租约列——非终态行可能残留过期
`lease_holder/lease_expires_at`，而 flag-on 接管谓词按到期即放行 → 残留列是接管地雷。
启用顺序（缺一不可，顺序不可换）：

1. 停 stage-1/2/3 调度，确认无在飞运行（DW 实例列表 + `GET_LOCK` 探测）；
2. 一次性清残留：非终态行（content_process_status ∉ 终态 或 index_status='PROCESSING'）
   `SET lease_holder=NULL, lease_expires_at=NULL`——**不动 lease_epoch**（单调性不可回退）；
3. 验证：`SELECT COUNT(*) ... WHERE lease_expires_at IS NOT NULL AND <非终态谓词>` = 0；
4. DW 节点 env 开 `RAG_INGEST_LEASE_ENABLE=true`（正式节点重贴必须用 main 生成器版）；
5. 观察一个完整 drain 周期后，才允许提高摄取并发（设计顺序约束：先租约后并发）。
