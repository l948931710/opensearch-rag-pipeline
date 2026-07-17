# 外部企业 Agent production 评审逐条核查 + 修复批次划分（2026-07-16）

> 核查对象：`~/Downloads/enterprise_agent_production_review_2026-07-16.md`（外部评审，审 `claude/ontology-p0@7041eba` = 当前分支 tip）
> 核查方法：22 个独立核查 agent —— 4 个 P0 每项双查（故障注入实际复现 + 反驳式精读），14 个 P1 每项一个反驳式核查；R0-01 由 git 直接验证。全部钉在 7041eba 的独立 worktree 上。
> 复现脚本存档：`scratch/agent_review_repro_20260716/`（可直接改造成 batch-1 的 fault-model 回归测试）

## 总判定

**19/19 项事实层全部属实，0 项被推翻**——评审者读代码是准确的，引用行号全部对得上，4 个 P0 的故障注入全部被我方独立复现。**但定级系统性偏高**：4 个 P0 全部应降级（双查两两一致），多个 P1 是本 SHA 已拍板并写入代码注释/审计台账的 known-deferred 决定，评审把其中至少 2 项"刚落地的加固"倒读成了"遗留缺陷"（P1-14 LEGACY ack、P1-10 拓扑守卫均是 733d2c4 批次5 刚上的）。

评审 NO-GO 结论的实际含义：对 HIGH_WRITE / 多副本形态 NO-GO——与项目现有姿态本来就一致（写工具 flag 默认 OFF、单副本 `--workers 1`）；对"只读灰度"则过严。**唯一真正的发布级阻断是 R0-01（主干分叉），已实证确认。**

关键的共同背景（评审未充分计入）：agent runtime 在现网是 flag 门控全暗的；所有 4 个 P0 都有 reaper（300s 间隔 + 900s stale → failed）在 ≤20 分钟内自愈；P0-01 的触发条件在 shipped `DefaultAgentLoop` 下不可达（所有退出路径都发终态事件），需要未来 loop bug 或第三方 loop 才触发。

## 逐条判定表

| 项 | 评审级 | 核查判定 | 复核级 | 实际复现 | 真实内核（一句话） | 修复量 |
| --- | --- | --- | --- | --- | --- | --- |
| R0-01 主干不可合 | 阻断 | **确认** | 阻断（user-gated） | ✅ git 实证：62 落后/172 超前、merge-tree 18 冲突、冲突清单吻合 | 合 main 前置，按 Sam 拍板节奏走 | L |
| P0-01 StopIteration 吞终态 | P0 | PARTIAL | **P1**（灰度前必修） | ✅ 双复现 | driver 缺"流结束⇒必有 durable 终态"不变量；shipped loop 不可触发、reaper 自愈 | S |
| P0-02 drain/submit 竞态 | P0 | PARTIAL | **P2** | ✅ 复现 | 窗口只在 ASGI shutdown/atexit 出现；pool.submit 失败须诚实转 failed，drain 应等 `_active` 计数而非只看 `_live` | S |
| P0-03 commit ACK 丢失分裂 | P0 | PARTIAL | **P2** | ✅ 双复现 | 分裂方向是安全侧（客户端见 failed、DB 已 succeeded）；commit 异常应 read-after-write 判定 | S |
| P0-04 拒绝终止忽略 CAS | P0 | PARTIAL | **P2** | ✅ 复现 | 终态事件须以 CAS 成功为前置；reconciler 已存在可兜底 | S |
| P1-01 队列 max=1 吞终态 | P1 | PARTIAL | P3 | ✅ 复现 | 仅退化配置可达（默认 10000，无处设 1）；clamp 正值 ≥2 一行修 | S |
| P1-02 HIGH_WRITE 共享线程池 | P1 | PARTIAL | P2=known 债 | — | 即 PR-3（durable worker），代码 docstring 已明示 deferred；过渡可加 per-tool 信号量 | S 过渡 / L 全量 |
| P1-03 普通异常误判确定失败 | P1 | PARTIAL | P2/P3 | — | 属实但今日不可达（唯一副作用工具 flag OFF）；side-effect 工具异常耗尽应默认 uncertain | S |
| P1-04 幂等命中绕 obligations | P1 | PARTIAL | P2/P3 | — | 属实；修法应 apply-on-hit（读取时按当前策略施加 obligations），劣于评审的存脱敏回执方案；派生 key 须重查 | S |
| P1-05 预算非硬上限 | P1 | PARTIAL | P2 | — | resume 归零回退是本 SHA 有意决定（防双计）；真实内核=调用前 `max_tokens=min(cap, remaining-reserve)` | S |
| P1-06 审批前后 provenance 断裂 | P1 | PARTIAL | P2 | — | 属实且代码注释已自认；最省修法=completion 事务内从 `tool_invocation.receipt_json` 并集 doc_ids | S |
| P1-07 审批幂等/edited 恢复 | P1 | PARTIAL | P2/P3 | — | ledger+幂等键已存在（schema/025 `uk_req_idem`）；真实缺口仅"同幂等键重试应回放决定而非 409"；edited envelope 建议应拒绝（逆转有意决定） | S |
| P1-08 resume 身份 fail-open | P1 | PARTIAL | P2 | ✅ 部分复现 | (a) 定性错误——回退身份是最小权限（空组=public-only）；真实内核=HIGH_WRITE resume 身份不可解析应 fail-closed + 账号 status/tombstone | M |
| P1-09 readiness false-green | P1 | PARTIAL | P2 | — | 承重通道已被 733d2c4 关闭（评审部分过时）；剩余真实内核=逐库 manifest 校验、`_AGENT_TABLES` 补 llm_call_log/agent_audit_log、uk_thread_active 索引探针 | M |
| P1-10 多副本拓扑自报 | P1 | PARTIAL | P3=多副本前置 | — | 拓扑守卫本身就是本 SHA 刚落的 P1-10 修复；增量=守卫清单补 event relay；downward-API 建议在 SAE 上不成立 | S 增量 |
| P1-11 session_id 未按用户命名空间 | P1 | PARTIAL | P3 | — | 属实但所有严重后果被独立 user_id 门挡住；修=仅 console session_id-only 分支改 `sid:{user_id}:{session_id}`（钉钉/小程序命名空间勿动） | S |
| P1-12 message_id 后置回填 | P1 | PARTIAL | P3 | — | "极快完成"半个触发条件是错的（set_message_id 是无条件 UPDATE）；真实窗口仅进程崩溃亚毫秒级；修=create_run 同事务写入（可选 kwarg 保 stub 兼容） | S |
| P1-13 治理动作与审计非原子 | P1 | PARTIAL | P2 | — | 字面属实（四处均 post-hoc fail-open 且有意注释）；修=同库同事务折入（kill switch 例外：紧急关闸不应被审计表故障阻塞，用 durable pending 折衷） | M |
| P1-14 生产姿态可绕过默认 | P1 | PARTIAL | P2 | — | LEGACY ack 被倒读——它是 733d2c4 刚落的迁移期加固而非遗留逃生口；真实内核=ack 应仿 env_guard 惯例绑日期 `ack:<YYYY-MM-DD>` 到期失效 | S |

## 修复批次划分（全部落 ontology-p0）

### 批次 1 — Executor 终态真值（P0 内核全量 + P1-01），全 S，集中在 executor.py/run_store.py
1. **P0-01**：driver 加 `terminal_seen` 判别器；裸 StopIteration 且非 suspended/terminal 时经 `_transition_checked(running→failed)` 发 `RunFailed(retryable=true)`——必须走既有 D3 fencing 语义，CAS false 不发重复失败帧。
2. **P0-02**：`pool.submit` 包 try，shutdown RuntimeError 时 submit 侧 running→failed / resume 侧回边 suspended；`drain()` 等待既有 `_active` 准入计数归零而非只快照 `_live`（零新增状态）。
3. **P0-03**：`complete_run_atomic` 把 `conn.commit()` 单独隔离 try 域；commit 异常时开新连接 read-after-write：succeeded→照常发 done、running→维持现失败语义、无法判定→completion_uncertain。
4. **P0-04**：RejectedTerminate 的终态事件以 `_transition_checked(resuming→cancelled)` 成功为前置；CAS false 读 durable 现状——注意保住 relay `__end__` 帧（跨实例 replayer 不阻塞）；routes/agent.py 停止硬编码 `status="cancelled"` 回包。
5. **P1-01**：事件队列 maxsize 正值 clamp ≥2（保留 ≤0=无界语义）。
6. **验收**：把 `scratch/agent_review_repro_20260716/` 五个注入脚本改造成 `tests/` 内 blocking 回归测试（评审 §8 的建议此条采纳）。

### 批次 2 — ToolExecutor 副作用语义（P1-03、P1-04、P1-02 过渡加固），全 S
1. **P1-03**：`side_effects or risk_level != READ_ONLY` 的工具，重试耗尽后的任何异常默认 `uncertain`（不再仅 timeout/schema 违约）；预边界错误由工具用 `ToolResult.fail` 表达。
2. **P1-04**：幂等命中路径统一走 output validation + obligations pipeline（apply-on-hit，按当前决策的 obligations）；同键不同参派生新 key 后对派生 key 重新 lookup。
3. **P1-02 过渡**：per-tool 并发信号量 + 池大小 env 可配（`_ToolBreaker` 熔断已有）；durable worker 全量迁移维持 PR-3 排期不并入本批。

### 批次 3 — 预算 / provenance / 审批与身份（P1-05、P1-06、P1-07、P1-08）
1. **P1-05**：每次模型调用前设 `max_tokens=min(profile_cap, remaining-reserve)` 并为最终回答留额；resume 预算读取区分"异常"（fail-closed/重试）与"合法全零快照"（保留回退，防重演双计问题）。
2. **P1-06**：completion 事务的 extra_writer 内，从本 run 的 `tool_invocation.receipt_json` 并集 doc_id 级来源后再 `_flatten_retrieved`（fail-open、同连接、不新增表）。
3. **P1-07**：`/approve` 在 run 已 resuming/running/终态且携带相同 idempotency_key（或同向 outcome）时回放已记录决定（200/202）而非 409；**不做** edited executable envelope。
4. **P1-08**（M）：pending call 为 HIGH_WRITE 时身份不可解析 → 重挂起（fail-closed with retry），只读 run 维持最小权限继续；用户表加 account status/tombstone，token 带 authz version、实时不一致即拒。

### 批次 4 — Readiness / 治理审计 / 配置姿态（P1-09、P1-13、P1-14、P1-10 增量、P1-11、P1-12）
1. **P1-09**（M）：`scripts/ci_load_schema.sh` 的 file→DB manifest 抽成 schema/ 下机器可读单一来源（注意 Dockerfile 只 COPY schema/ 不 COPY scripts/），readiness 逐库校验 applied 台账；`_AGENT_TABLES` 补 `llm_call_log`+`agent_audit_log`；加 `uk_thread_active` 唯一索引探针。
2. **P1-13**（M）：审批 decide / uncertain 人工对账的审计 INSERT 折入同一 MySQL 事务（同在 fuling_operation 库，零跨库成本）；kill switch 保持"先关闸"，审计失败落 durable pending 记录而非阻塞。
3. **P1-14**：`RAG_ALLOW_LEGACY_OPEN_PROD` → `ack:<YYYY-MM-DD>` 日期绑定、午夜过期、启用即打审计日志（仿 `env_guard.py` 既有惯例）。
4. **P1-10 增量**：多副本守卫清单（config.py）补 `RAG_AGENT_EVENT_RELAY`；其余维持"多副本前置条件"文档化，不做 downward-API。
5. **P1-11**：console session_id-only 分支 thread key 改 `sid:{user_id}:{session_id}`；钉钉 `conversationId:staffId` / `miniapp:<staffId>` 命名空间保持不动；注意换 key 会孤儿化在存会话（部署窗口选择）。
6. **P1-12**：`message_id` 经 create_run 同事务写入（RunStore Protocol 加可选 kwarg，`set_message_id` 保留为兼容 no-op）。

### user-gated（不排入代码批次）
- **R0-01 主干合并**：18 冲突（认证/限流/迁移工具/console），须整段解冲突 + 对新 SHA 重跑全部 gate + 批次1 fault suite。属 Sam 拍板节奏（agent 底座合 main 一直是 user-gated）。
- **PR-3 durable worker / HIGH_WRITE 执行宿主迁移**（评审 §9 目标架构）：方向与项目既有计划一致，维持独立立项，HIGH_WRITE flag 在此之前保持 OFF（现状即如此）。

## 执行状态

- **批次 1：✅ 已落地（2026-07-16，ontology-p0）**。五项全修 + `tests/test_agent_fault_model.py` 21 项故障注入回归（test-quality 复核实证其中 13+ 项在旧代码上必红）；全量 3824 绿 + lint 绿。四人对抗复查（concurrency/transaction/callers/test-quality）另揪出四点并同批修正：
  1. **（升 P1）SteadyDB 劈事务**：池化连接不 `begin()` 时断连触发 DBUtils 透明重连+单句重试，`complete_run_atomic`/`suspend_run_atomic` 的多语句事务被劈成两半（复现实证 status=succeeded 落地而答案行丢失）——`run_store._begin()` 钉连接，五个事务性方法全部前置（仓内先例 spot_checker/cost_breaker）；
  2. `pool.submit` **入队后**才抛（线程创建失败类）时条目仍会被 warm worker 驱动——P0-02 的 CAS 反标改为仅在必未入队（`_shutdown/_broken`）时执行；
  3. P0-04 的 CAS 改回裸 `transition`：DB 异常 ≠ CAS False，异常时立即回边 resuming→suspended（撤回无决定行子场景 B6 无从收敛，不能钉死 resuming 等 reaper）；
  4. 消歧读对 running 有界重试（3 次/~1s）收窄「commit 慢收尾被误判失败」残窗；两处 mutation 缺口补 spy 帧观测断言。
  遗留不修（评级 nit/pre-existing，已记录）：`_put_local` 挤位与消费者的极窄竞态窗（durable 轮询兜底）；submit 异常与驱动器的双 `_release`（预先存在，负计数仅放宽上限，随 PR-3 一并收）。
- **批次 2：✅ 已落地（2026-07-16，ontology-p0）**。三项全修 + `tests/test_agent_tool_executor_batch2.py` 13 项回归（8 项在旧代码上必红，5 项为不变语义守卫）；全量 3837 绿 + lint 绿：
  1. **P1-03**：副作用工具**任何**耗尽异常收 uncertain（不只 timeout/schema 违约——requests.ReadTimeout/connection reset 正是「下游已提交、响应阶段抛」形态），并禁 in-loop 自动重试（核查发现的加码项：可重试白名单恰与已提交形态重合）；`ToolResult.fail` 维持 failed（工具作者预边界声明，有意不变）。
  2. **P1-04**：幂等命中 apply-on-hit——回执渲染进模型可见文本前过当前决策 obligations（策略收紧后回放按新姿态，不固化写入时姿态）+ 当前 output_schema 复验（版本漂移拒绝复用并告警；无回执历史行跳过不误伤）；派生键真重放复用回执（不再误报「同幂等键并发执行冲突」）；义务强制点抽 `_apply_obligations_safe` 两路共用。
  3. **P1-02 过渡**：per-tool 并发舱壁（`RAG_AGENT_TOOL_MAX_CONCURRENCY` 默认 4，满则 fail-fast=确定无副作用绝不进对账；配额随 future 真正完成才释放——挂死线程持续占本工具配额不外溢）+ 超时池大小可配（`RAG_AGENT_TOOL_TIMEOUT_POOL_SIZE` 默认 8）。durable worker 全量迁移维持 PR-3。
- **批次 3：✅ 已落地（2026-07-17，ontology-p0）**。四项全修 + `tests/test_agent_batch3_fixes.py` 17 项回归（16 项在旧代码上必红，1 项为零快照回退守卫）；全量 3854 绿 + lint 绿：
  1. **P1-05**：model_fn pre-call 预算闸（种子+段内已耗+本轮 prompt 估算 ≥ token_budget → BudgetExceeded 费用未发生；估算刻意低估导向 chars//4，post-call 复判仍是权威兜底）；临近耗尽（余额<16384）才给 max_tokens 设余额上限（FLOOR 1024 防截碎答案，正常调用行为零变化）；resume 段种子从 agent_run.tokens_used 播种；`_budget_snapshot` 读取异常 fail-closed（RunRejected→回滚 suspended 可重试，且移到接手 running 之前），合法全零快照维持回退（核查拍板：防重演双计+瞬断钉死）。
  2. **P1-06**：`_union_invocation_doc_ids`——completion 事务同游标把本 run 全部成功检索回执的 doc_ids 并进 retrieved_docs（只补 doc_id 级条目不复制 chunk 载荷，checkpoint 不存 chunk 的 P0-A 决定不动；fail-open）。已知残留：回执补差条目无 title（历史 UI 渲染朴素）。
  3. **P1-07**：`/approve` 已受理命令的 HTTP 重试幂等回放——run 已离开 suspended 且库内决定同 idempotency_key 或同向 → 202 回放（get_decision 补返 idempotency_key），其余维持 409；**不做** edited executable envelope（维持拒绝）。
  4. **P1-08**：Approved/Edited 续跑身份 fail-closed——解析异常→503 可重试（decided 重放接住）、墓碑→403 终局（留 suspended 等 TTL）；对账同语义（approved 向跳过+告警）；**墓碑用既有 user_role.is_active 列零 schema 变更**（有行且全 0=显式停用）：api strict 分支令牌立即失效（此前只能等 2h TTL），真无行维持文档化保留令牌组语义。authz-version-in-token 按核查建议不做（TTL+墓碑+跨部门实时拒已覆盖撤销目标）。已知残留：停用发起人的 decided run 在 TTL 前每轮对账各一条 error 日志（可告警特征，有意保留）。
- 批次 4：未动工。

## 评审建议中被拒绝/修正的部分（核查结论）
- "不可绕过的 production profile"（P1-14）：**拒绝**——ack 存在正是因为现网 SAE 包先于 flag 存在，硬断言会 brick 重部署/回滚；改为日期绑定。
- "edited 决定存加密 executable envelope"（P1-07）：**拒绝**——逆转本 SHA 的有意决定（edited 只存脱敏参数、拒绝自动重驱）。
- "从部署平台 downward API 感知拓扑"（P1-10）：**不成立**——SAE 不向实例暴露副本数；K8s downward API 也只给 pod 元数据。
- "存 typed/sanitized receipt"（P1-04）：**改良**——写入时固化会冻结策略姿态，应读取时按当前 obligations 施加。
- 评分卡与 NO-GO：事实成立但语义上是"HIGH_WRITE/多副本 NO-GO"，与现有 flag-OFF/单副本姿态一致；不构成对只读灰度路线的新阻断（灰度前置=批次1）。
