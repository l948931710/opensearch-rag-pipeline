# Unknown-unknowns 全架构外审——逐条核查 + 修复批次台账

- 日期：2026-07-16
- 外审文件：`~/Downloads/opensearch-rag-pipeline_unknown-unknowns_architecture_review_2026-07-16.md`
- 外审基线：`a2a8c708`（claude/ontology-p0，2026-07-14）——**落后核查时 HEAD 46 个提交**
- 核查基线：`8273bc4`（claude/ontology-p0 HEAD，含 perf 批次A/B/C、通用能力分级开放、ε 批次、CI 两修）
- 核查方式：6 路子代理静态核查（每条 claim 在 HEAD 按 symbol 重定位取证 + `git diff a2a8c708..HEAD` 判 ALREADY_FIXED）+ HEAD 全量 pytest 实测
- 裁决词表：CONFIRMED（HEAD 仍成立）/ PARTIAL（部分成立或影响面高估）/ ALREADY_FIXED / REFUTED

## 1. 总裁决

外审代码层准确率很高：**22 个 claim 家族无一整条被推翻**——17 条 CONFIRMED、5 条 PARTIAL（多为影响面高估、或最尖锐形态已被 46 个新提交钝化），仅 P1-04b 的「EXPIRE 逐帧」半项已被 3a519f8 修掉。「Agent / Ontology 面生产 NO-GO」的结论**维持**。

但要按面拆开读：全部 P0 集中在**默认 OFF 的 agent/ontology 新面**（`RAG_AGENT_ENABLE` / `RAG_ONTOLOGY_TOOLS_ENABLE` / relay 均关）+ readiness/部署证据；**旧只读 RAG 主链路本次未新增代码级阻断项**，其「条件 GO」前置＝批次7 的部署证据（与既有台账一致，无新增）。

## 2. 逐条核查总表

| # | claim | 裁决 | HEAD 关键证据 | 备注/纠偏 |
|---|---|---|---|---|
| P0-01 | run 未持久化答案即 succeeded | CONFIRMED (5/5) | executor.py:443-445 先 succeeded CAS 后回调；:521-523 回调异常全吞；routes/agent.py:577-592 memory 先于 qa_session_log | 4c878a3 只合并了记账事务，未把答案并入 succeeded 事务；CAS 先行是有意的 fencing——修法是「答案入同一事务」而非换序 |
| P0-02 | 最终模型轮绕过预算/deadline | CONFIRMED (3/3) | 预算三检仅在 ToolCallProposed 分支（executor.py:348-365）；RunCompleted 只记账不检查（:432-453）；gateway 仅 pre-call 检 deadline（model_gateway.py:392/457） | token_budget=10、末轮 usage=16 → 记 16 且 succeeded，复现成立 |
| P0-03 | 轮询自我限流 + 恢复不了答案 | b/c/d/e/f CONFIRMED；a/g PARTIAL | 轮询端点全部 `scope="ask"`（routes/agent.py:1106/1136）；beb9905 aux 桶（120/min）未覆盖 agent 端点；气泡不水合 final.answer_text（useAgentAsk.ts:633-644） | 数字过时：27f08e2/3a519f8 已加 suspended 45s 慢车道+指数退避+隐藏页暂停+1-SQL /status 探针。running 车道仍 <1min 自我 429；suspended 车道无分钟级 429，但 ~3h45m 耗尽 300/day 且与真实提问同桶互挤 |
| P0-04 | 纠错 UI / auto 抽检闭环不存在 | CONFIRMED (4/4) | 后端 4 纠错路由在（ontology.py:605/634/671/702），console-app 0 处调用；OntologyWorkbench.vue:350 文案许诺不存在能力；无任何 review_status/reviewed_at 数据模型；4 路由 note 均可空（对比 confirm/dismiss 强制非空） | 后端 409 文案也在指路「对象详情纠错」这个不存在的页面（ontology.py:475） |
| P0-05 | coverage 分母可被局部 backfill 覆盖 | CONFIRMED (5/5) | 027:73-79 `PRIMARY KEY (namespace)`；store.py:1485-1490 `ON DUPLICATE KEY UPDATE records=VALUES(records)`；10,000→100 覆盖成立；分品类恒走 active/(active+open)（store.py:1543-1576） | 已有缓释被外审漏记：payload 自标 `denominator:"approx"`，只是 `_print_coverage` 打印时丢掉了口径字段；P1-12 已防 `--limit` 截断分母（防不了局部源文件） |
| P0-06 | readiness 缺迁移/缺列/kill-switch 不可读仍绿 | a/c/d/e CONFIRMED；b PARTIAL | 只查表存在性（readiness.py:64-83）；台账只比已记文件（:139 自述）；strict 默认 off（:193），api.py:694-695 只拒 `"drift"` 且 `unavailable` 恒绿；normalized_title 回填 0 处检查 | 有界：`rds`/`agent_tables` 是 critical 探针，全库不可达仍会红；漏的是「表在、契约不在」与 kill-switch 读退化 |
| P0-07 | 单 flag 同开 READ+HIGH_WRITE；安全前置仅告警 | CONFIRMED (4/4) | agent_tools/__init__.py:37-44 一 flag 注册 ontology_resolve+packing_calc+HIGH_WRITE identity_resolve；policy.py:135-140 同 flag 授三 scope；injection guard off 仅 warning（routes/agent.py:264-268）；REQUIRE_AUTH/ACL_FAIL_CLOSED 默认 off 且 production 不硬断言 | 两处是记录在案的决策而非疏漏：TLS 告警不阻断（config.py:686「用户选先不强制」）、单 flag=docstring 自记「计划内中间态」；且 HIGH_WRITE 结构性 REQUIRE_APPROVAL（policy.py:99-101）——授予≠免批执行 |
| P0-08 | 组织 gate 未签 + U8 源是桩 | CONFIRMED (2/2) | seeding.py:104-118 显式 NotImplementedError（有意契约桩）；signoff 文档 DRAFT、六个 ☐ 全空、签字行全空 | 与既有台账一致：签字前禁真实播种/回填正是这个桩在执行的栅栏；纯 user-gated |
| P1-01 | 播种 alias+case 愈合非原子 | CONFIRMED | seeding.py:296-316 两事务；:348-355 close 失败显式 fail-open；原子 API `insert_identifier_closing_case`（store.py:854）已存在且 agent 工具在用，seeding 未用 | mint 路径已原子（mint_object_with_alias 一个事务）；heal_if_stale 只在同记录再现时兜底，非扫描器 |
| P1-02 | 迁移 runner 不可真重放 | CONFIRMED | apply_ontology_dbs.py:241-247 全量重执行、无台账跳过；038:18-22 是全家**唯一**未守卫的裸 ALTER（011/013/027/028/029/030/032/033/034 全有守卫） | 修复须走 038a 修订纪律：改 038 本体会触 runner 自己的 checksum drift exit-4 |
| P1-03 | kill switch 冷实例/DB 故障 fail-open | a CONFIRMED；b/c PARTIAL | registry_store.py:105-108 冷实例无缓存→空禁用集（自记「fail-open」设计） | 影响面高估：warm 实例沿用上次快照；重现的 HIGH_WRITE 工具仍被审批门拦执行；真洞=冷启动×DB 故障叠加窗 |
| P1-04 | relay 无 resume 且拖慢流 | a/c CONFIRMED；b PARTIAL | 0-0 回放（event_relay.py:123-128）、无 Last-Event-ID、SSE 无 id: 帧；publish 在驱动线程同步 XADD（executor.py:87-90）；默认 OFF（RAG_AGENT_EVENT_RELAY≠"redis" 即关） | EXPIRE 逐帧已被 3a519f8 修掉（model_delta 不续 TTL）；且现无任何前端消费 /events——重复回放是潜伏面不是现网面 |
| P1-05 | 本地事件队列无界 | CONFIRMED | executor.py:63 `queue.Queue()` 无 maxsize；断连后 `_client_disconnected_at` 全仓只有写点无读点，delta 照常入队 | 自然上界=run 自身预算/deadline × max_concurrent=4，故为放大器而非无限泄漏 |
| P1-06 | Agent 真库契约未进 CI | CONFIRMED | 4 个 agent DB 测试文件在 CI 零点名（db-integration 只跑 pipeline 5 件 + ontology 4 件）；零跳过硬化（REQUIRE_RDS）只有 ontology 家族有 | CI 基建已够（ci_load_schema.sh 已灌 022-025/031/035/036/037/042 全 DDL）——缺的只是点名+串行组+硬化 env；另见 §3-4 对「无 check」的解读纠正 |
| P1-07 | read-trace 异步与终态竞态 | CONFIRMED | 全局单线程 trace 池默认异步（tool_executor.py:67-82）；_drain_runtime/shutdown 不调 drain_read_trace（routes/agent.py:208-242）；flake 用例=tests/test_agent_runtime_integration.py:125（断言前未 drain） | 本轮 HEAD 全量实测未复现（外审自测 1/10 概率）；竞态从代码坐实，与 xdist flake 惯犯记录一致 |
| P1-08 | 双 TTL 隐式耦合 | CONFIRMED | RAG_AGENT_APPROVAL_TTL_S（approval_store.py:138）与 RAG_AGENT_SUSPENDED_TTL_S（routes/agent.py:161）独立、均默认 259200、零校验；reaper 的 resuming→suspended 回边重置 heartbeat——**即便默认相等时钟也会漂移** | 两个半状态都不 cross-heal：审批先过期→run 占 active_thread 到自身 TTL；run 先过期→pending 审批留队（执行时 409 安全兜底） |
| P1-09 | 不变量扫描面过窄 | CONFIRMED | invariants.py 恰好 4 项扫描；外审列的 9 缺口全部坐实（其中 5 项有写侧/DB 约束补偿但无漂移检测） | 高价值缺口：superseded_by 断链/环（033 有意不加 FK）、active link→非 active 端点、normalized_title NULL 簇、population snapshot 陈旧 |
| P1-10 | 单 worker+内存态是隐藏拓扑约束 | CONFIRMED | Dockerfile:76 `--workers 1`；session/rate-limit/dedup/token cache 四态全默认 memory；无副本数强制校验 | 原语已在但 opt-in：RAG_REQUIRE_REDIS（默认 off）+ redis_client.py「三件同出」清单 |
| §5 | HTTP hardening 对未配置 Host/缺 XFP 放行 | CONFIRMED | http_hardening.py:51-54、:85-86 两个放行分支 | 记录在案的承重设计（直连 IP 小程序/钉钉回调无 XFP，猜 scheme 会造重定向环）；真敞口=旧 EIP:8000 明文入口未关（批次7） |
| §5 其余 | 部署证据类 8 项 | 不可仓内证明 | — | 全部归批次7 attestation |
| §6 | 前端性能建议 | 与 P0-03/P1-04/05 重叠 | run detail 仍 4-5 查询，但 3a519f8 已把每拍热路径换成 1-SQL /status 探针 | 「立即优化」表中 relay/队列/state backend 三行成立；ManageView 拆包 9ba0f90 已做惰性加载 |
| §7 | 测试数字 | 环境相对 | 外审 3436/128skip/1fail = a2a8c708+无本地 DB；HEAD 本机（本地 MySQL 接线）**3761 passed / 1 skipped / 0 failed（37.57s）** | flake 未现但竞态代码坐实（P1-07）；128 个 skip 的主体正是 CI 也不跑的真库契约（P1-06） |

## 3. 对外审的 7 处纠偏

1. **P0-03 数字过时**：27f08e2/3a519f8 已落 suspended 45s 慢车道、指数退避、隐藏页暂停、1-SQL /status 探针。「~30 秒自我 429 / ~50 分钟耗尽日额」只对 running 车道成立；suspended（审批等待）车道无分钟级 429，日额约 3h45m 耗尽。核心命题（轮询计 ask 配额、与真实提问同桶互挤）仍成立。
2. **P0-03「恢复不了最终答案」过重**：运行中心 detail 面板一直渲染 final.answer_text（AgentRunCenter.vue:195-197，审基线时已在）。缺的是聊天气泡水合——一次点击可达，不是不可恢复。
3. **P1-04b 半项已修**：EXPIRE 逐帧续期被 3a519f8 修掉；「同步 XADD 在模型驱动线程上」仍成立。且 /events 现无任何前端消费——重复回放是潜伏缺陷。
4. **P1-06「没有可见 check」解读要纠正**：真因=分支从未开 PR + ci.yml/frontend.yml 只 trigger main/PR；07-12~07-15 每 push 0 秒 startup failure（未引号 `fs:`）已被 5980093 修复。main 上 CI+Frontend 全绿。不是「CI 不存在/从不跑」——但「分支提交无 check」这个事实本身两个 commit 都成立。
5. **P1-03b 影响高估**：warm 实例保留上次禁用快照；重现的 HIGH_WRITE 工具仍被结构性审批门拦住执行。真洞=冷启动×DB 故障叠加。
6. **P0-06b 有界**：`rds`/`agent_tables` 是 critical 探针，全库不可达仍红。漏的是「表在、契约不在」与 kill-switch 读退化这个中间态。
7. **两处是拍板项不是补丁项**：RDS TLS 告警不阻断（config.py:686 记录「用户选先不强制」）、单 flag 一杆两类（docstring 自记「计划内中间态」）。升级它们需要 Sam 拍板，不能当 bug 顺手修。

## 4. 修复批次

划分原则：同文件族一个批次一次评审收口；全部先 SIM/本地验证；生效侧（部署/apply/签字/演练）单列批次7 user-gated。规模：S=<50 行、M=50-300、L=300+。

### 批次1 — Agent run 完成真值与预算硬上限（P0-01 / P0-02 / P1-07 / P1-05）
Scope：`agent_runtime/{executor,run_store,tool_executor}.py`、`routes/agent.py`、tests。

1. `complete_run_atomic`：qa_session_log 落库与 running→succeeded CAS 同一 operation 库事务（复用 `suspend_run_atomic` 的 `extra_writer` 缝，run_store.py:242）；session memory 降为 commit 后缓存副作用；持久化失败 → failed/uncertain，绝不发 done。（M）
2. RunCompleted 分支补 post-call token/turn/deadline 检查（镜像 ToolCallProposed 块）；超限落 budget_exceeded——费用已发生，诚实失败、记账保留。（S）
3. `_drain_runtime`/shutdown 补 `drain_read_trace()`；`test_shadow_link_end_to_end` 断言前 drain（根治 flake）。（S）
4. 事件队列有界 + 断连宽限后丢 ModelDelta（终态帧永不丢；`_client_disconnected_at` 从只写变可用）。（S-M）

验收：故障注入 4 组（memory 失败 / qa log 失败 / commit 失败 / 两步间进程退出）+ 预算边界 4 组（首轮即 final / 工具后 final / 空答案重试 final / resume final）。

### 批次2 — 前端恢复协议 + 轮询配额（P0-03）
Scope：`routes/agent.py`（3 个 `_enforce_rate_limit` 调用点）、`console-app/src/composables/useAgentAsk.ts`。

1. `/runs`、`/runs/{id}`、`/runs/{id}/status` 改 `scope="aux"`（beb9905 的 120/min 登录车道、无日额）；approve/cancel 是动作、维持 ask。（S）
2. 终态帧跟踪：clean EOF 无终态帧 → disconnected + ensureRunPolling（03f）；askAgent catch 且已有 runId → 同样入恢复而非报错卡（03e，镜像 stopAgent 现成写法）。（S）
3. 轮询发现 succeeded → 气泡水合 `final.answer_text`（renderMd/stripImg 现成管道）；sources 需 detail 出参补 retrieved_docs，可选后做。（S；含 sources 则 M）

验收：挂起 10 分钟 / 断流恢复 / 审批后完成 三条 vitest+e2e；轮询不再挤占真实提问额度。

### 批次3 — Ontology 治理真值（P0-04 / P1-09）
- **3a 立即（S）**：文案诚实化——OntologyWorkbench.vue:350 与后端 409 提示（ontology.py:475）不再许诺不存在的「对象详情纠错/抽检队列」；四个纠错路由 note 强制非空（对齐 confirm/dismiss 现成写法）。
- **3b 结构（L，workbench 本就 staging-only）**：对象详情抽屉 + 挂 4 个既有纠错端点（useOntology.ts + workbench + 新组件）；schema/043 给 ontology_identifier 加 review_status/reviewed_at/reviewed_by（或独立抽样表）+ auto 待抽检列表 API + workbench 抽检 tab；manual_review_rate 改真实口径（审核完成率，不再是非-auto 占比）。
- **3c invariants 扩面（M）**：superseded_by 断链/环、active link→非 active 端点、normalized_title NULL/重复簇、population snapshot_at 陈旧；auto 抽检超 SLA 待 3b 数据模型落地后加。

验收：新增扫描在 staging 全 0；UI 词表进 e2e 锁（沿 be0a941 的 seam 锁模式）。

### 批次4 — Coverage 分母 + 播种原子 + 迁移可重放（P0-05 / P1-01 / P1-02）
1. `record_population_snapshot` 只在 master/全量语义登记（`mint_new=True` 或显式 authoritative 标志）；mention backfill 永不覆盖 master 分母；`_print_coverage` 逐行打印 `denominator` 口径。（M；RDS+Memory 双实现同步）
2. `_Sink.alias()` 改走 `insert_identifier_closing_case` 原子 API（heal_if_stale 保留为兜底，不替代事务）。（S）
3. runner 按台账 checksum 跳过已应用文件 + **038a 守卫化修订**（032/033 同款 information_schema+PREPARE；注意改 038 本体会触 runner drift exit-4，必须走 NNNa 纪律 + schema/README 台账行）。（S+S）
4. 新增回归：局部 backfill 分母覆盖（10,000→100）、apply 脚本「连续执行两次」真 MySQL 用例。

### 批次5 — Readiness / flag 分离 / kill-switch / 拓扑防呆（P0-06 / P0-07 / P1-03 / P1-08 / P1-10）
1. readiness：本地未 apply 迁移反向 diff→新状态词；031-038 关键列契约探针；kill-switch 读探针（uncached `disabled_names()`）；strict 下 `unavailable` 不再绿；normalized_title 未回填 report-only 字段。（M）
2. flag 分离：新 `RAG_ONTOLOGY_WRITE_TOOLS_ENABLE` 单列 HIGH_WRITE 工具；写工具开启且 injection guard off → 硬失败（现 warning 的理由「只读窗口」在写工具下不成立）。（S）
3. production 姿态断言：REQUIRE_AUTH / ACL_FAIL_CLOSED 缺失→启动失败（带显式 ack 逃生口，风格对齐既有 guard）；**TLS 升硬断言=拍板项**（CA 到位后）。（S）
4. kill-switch：无可信快照时 HIGH_WRITE fail-closed（READ_ONLY 维持 fail-open）± 快照本地落盘续命。（S-M）
5. TTL 单源派生（一个 env + 可选覆盖告警）+ reaper 双向 cross-heal（审批过期→挂起 run 过期；run 终态→pending 审批过期）。（S-M）
6. 拓扑：`RAG_EXPECTED_REPLICAS>1` 或 production overlay 强制 `RAG_REQUIRE_REDIS`。（S）

验收：readiness 在「表在契约不在 / kill-switch 不可读 / 迁移未 apply」矩阵下 503；冷启动×DB 故障注入下写工具不可解析。

### 批次6 — CI 契约门（P1-06）
1. db-integration job 点名 4 个 agent 真库测试 + `tests/conftest.py::_LOCAL_STACK_SERIAL_MODULES` 登记 + 新 `RAG_AGENT_TESTS_REQUIRE_RDS` 零跳过硬化（对齐 ontology 家族 REQUIRE_RDS 模式；DDL 基建 ci_load_schema.sh 已齐、纯点名工作）。（S）
2. **分支可见性（拍板）**：给 ontology-p0 开 PR 或加分支 push trigger——否则分支上永远无 check（现状是 trigger 设计使然，main 全绿）。

验收：任一 agent DB 测试 skip 即 CI 红。

### 批次7 — user-gated（代码之外/生效侧，Sam/组织）
- Gate ①③④ 签字包走完（P0-08；签字前维持禁真实播种/回填的代码栅栏不动）。
- U8SnapshotSource 真实现——阻塞于 gate ①（U8 T-1 可 diff 判定），签后才是代码活。
- 部署证据 attestation（外审 §5）：SAE 环境变量四件套实开（REQUIRE_AUTH / ACL_FAIL_CLOSED / PROMPT_INJECTION_GUARD / RDS_SSL_CA）、旧 EIP:8000 明文入口关闭、仓库转私有、双库 PITR 联合恢复演练、DataWorks backfill/invariants 节点部署+告警接收人、信息分级审查。
- 既有尾巴不变（不因本审重排）：SAE/DataWorks 重打包、refreeze、staging/prod 未 apply schema、金集复标降阈。
- P1-04 relay：**维持 OFF 即是当前正解**；Last-Event-ID/id: 帧 resume + 异步批量 publish（M）仅在决定启用 relay 时做——现无前端消费 /events，优先级最低。

## 5. 建议执行顺序

**批次1 → 批次2 →（3a + 4 + 6 并行）→ 批次5 → 3b/3c**；批次7 与代码批次解耦、随时可推进。理由：批次1 消灭「错误的成功状态」这一最大真值风险；批次2 是用户可感知面；3a/4/6 全是小而独立的收口；批次5 动 config/readiness 语义面最广、放后集中评审；3b 是唯一 L 级、且 workbench 本就 staging-only 不阻塞其他面。

外审「0-24h 保持关闭」项核对：RAG_ONTOLOGY_TOOLS_ENABLE、ontology auto ack、Redis relay 在 HEAD 均默认 OFF——**现状已满足，无需动作**。
