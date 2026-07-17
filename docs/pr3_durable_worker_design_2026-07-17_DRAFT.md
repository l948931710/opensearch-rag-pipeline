# PR-3 durable worker 独立立项——设计与分期（2026-07-17，Stage A 已落地）

> 立项来源：2026-07-11 Agent 底座重评 PR-3（「执行所有权迁 durable worker，lease+outbox」），
> 2026-07-16 外审 §9 目标架构与 Gate 4 同图；四个修复批次（台账
> docs/audits/enterprise_agent_review_verification_2026-07-16.md）收掉了现架构内可修的
> 语义漏洞，本项目换掉修不动的前提：**「谁在执行这个 run」只活在 Web 进程内存里**。

## 0. 目标与非目标

**目标**：run 的接受、执行归属、恢复全部成为 durable、可租约、可对账的事实——
进程崩溃/滚动发布不再丢命令；HIGH_WRITE 开闸的结构前置就位。

**非目标（本立项不做）**：多副本全量语义（distributed admission/跨实例 cancel——外审
P1-10 已按「多副本前置条件」记录）；钉钉/miniapp 通道改造（console agent 先行）；
把 LLM 流式换成轮询（SSE UX 不动）。

## 1. 关键决策（Stage A 拍板，可复议）

### D1. command-as-truth，而非 queued run 状态
外审目标图把 `queued` 画进 run 状态机；本设计改为**命令行（outbox）是唯一的
pre-dispatch 真相，agent_run 行仍在 worker 认领时才创建**（状态机不加 queued 态）。
理由：
- `agent_run.status` 是 ENUM + `active_thread` 生成列（037 uk 串行化）都要跟着改——
  对已带数据的表动生成列表达式=DROP/ADD 重建；command-as-truth 让 Stage A **纯增量**
  （一张新表，agent_run 零 ALTER）。
- 语义等价：命令行带 lease/attempts，「无人持有的 queued run」这类状态从根上不存在
  （pre-dispatch 崩溃只留命令行，扫描器重驱）；run 一旦存在即有人驱动，沿用既有
  reaper/fencing 全套。
- uk_thread_active 的 409 语义自然保留（dispatch 时 create_run 撞键=ThreadBusy）。

### D2. in-process worker 先行，进程形态可后拆
Worker 是**同进程后台线程**（dispatcher），不是新部署单元。理由：SAE 单副本 +
部署 user-gated；worker 逻辑=「读命令表→认领→驱动」，与进程形态无关，日后拆独立
容器时代码原样搬（executor.py B1 注释预留的 (c) 路径）。多副本时天然工作：
认领用 `FOR UPDATE SKIP LOCKED`，谁抢到谁跑。

### D3. fast-path inline dispatch（不牺牲 SSE 延迟）
`/api/agent/ask` 流程：**enqueue（单条 INSERT，命令即刻 durable）→ 同请求内
claim_specific + 执行**（拿到 RunHandle 照常 SSE）。代价=每问多一次 INSERT + 两次
UPDATE；收益=API 返回后的任何崩溃都有命令可恢复。容量满/线程忙沿用今天的
429/409（命令落 failed，UX 零变化）；「排队等空位」留作后续可选 knob。

### D4. 恢复语义：run 创建前 at-least-once，创建后 at-most-once
- 命令 `queued` 或 lease 过期且 **未绑 run_id** → 重驱（重建 ctx 现解 ACL——铁律 5，
  绝不用提交时快照），attempts 封顶（默认 3）落 failed；
- 已绑 run_id → **绝不重执行**：run 终态→命令 done；run 非终态→等（活 run 有心跳，
  僵尸交 reaper 收成 failed 后命令收口 done）。
- 残窗如实记录：create_run 与 bind_run 之间崩溃（毫秒级）可能双执行一次——上限
  attempts 次，与 B6 重驱同级别的已知折衷。
- 恢复驱动无 SSE 消费者：答案照常走完成事务落库（B6 同款 daemon 排空），用户从
  会话历史/运行中心取回。

### D5. flag 默认 OFF
`RAG_AGENT_DURABLE_DISPATCH`（默认 off）。off = 今日路径字节级不变；on = enqueue +
inline dispatch + 后台恢复扫描（随既有 reaper 线程节奏）。灰度顺序：staging 开 →
观察 `agent_dispatch_command` 积压/attempts → 生产随 RAG_AGENT_ENABLE 一起评审。

## 2. Stage A 交付物（本次落地）

- `schema/043_agent_dispatch_outbox.sql`（fuling_operation，纯增量）：
  `agent_dispatch_command`（command_id/kind/status/thread_id/user_id/channel/
  payload_json/run_id/attempts/lease_holder/lease_expires_at/last_error；
  `idx_claim(status, lease_expires_at)`）。payload 含用户问题原文——敏感级与
  qa_session_log 同级，retention 后续纳入 F-36 作业（Stage B 项）。
- `agent_runtime/dispatch_outbox.py`：RDSDispatchOutbox——enqueue / claim_specific /
  claim_next（SKIP LOCKED）/ renew_lease / bind_run / complete / requeue 语义，
  多语句事务全部 `_begin` 钉连接（批次1 纪律）。
- `agent_runtime/durable_dispatcher.py`：DurableDispatcher——fast-path dispatch_now +
  后台恢复循环（lease 过期重驱/终态收口/attempts 封顶），holder=进程实例 id。
- routes/agent.py：flag-gated 接线（agent_ask enqueue+bind+complete；恢复回调
  `_dispatch_recovered_submit` 以 payload 重建 ctx/callbacks，身份现解）；
  dispatcher 随 runtime 启动（agent flag 开时）。
- 回归测试 `tests/test_agent_pr3_dispatch.py`（outbox 事务形态 / dispatcher 恢复
  语义 / 路由 flag on-off 等价 / 现解 ACL）。

## 3. 后续分期

- **Stage B ✅ 已落地（2026-07-17）**：
  - schema/044 命令 kind 扩 `resume`（ENUM INSTANT 加值）；
  - `dispatch_outbox.insert_command_tx` 供 `approval_store.decide` 的 `outbox_writer`
    游标回调——**resume 命令与审批决定同事务**（决定 commit ⇒ 恢复命令 durable）；
    edited **不入队**（脱敏参数不可自动重驱，P1-07 不变）；
  - `claim_next`/`list_bound_expired` **kind-aware**（顺修 Stage A 缺口：已绑 run 的
    submit 命令绝不被 claim_next 捡走重执行=双答案；resume 命令 run_id 恒有值仍照常
    认领——重驱幂等）；
  - `durable_dispatcher.close_done`（resume fast-path 收口，无 bind 步）+ 恢复按 kind
    分流（`_dispatch_recover`）：resume 按 run 现状收敛（suspended→重驱 /
    running→retry / 终态→done）；
  - **B6 对账与 Stage B 恢复共用单引擎 `_redrive_resume_run`**（语义一致；对账保留为
    兜底——覆盖 Stage B 之前的历史决定、edited 人工流、flag off 环境）；
  - `agent_dispatch_command` 纳入 F-36 retention（终态命令 6 月删）+ purge_subject
    主体擦除（user_id 直删，payload 含问题原文）。
- **Stage C ✅ 已落地（2026-07-17）**：HIGH_WRITE tool task ledger——
  - **操作台账** schema/045 `agent_tool_operation`（fuling_operation，纯增量）：工具把
    台账行与副作用**同一事务**提交（`operation_writer` 游标座缝，形态同 Stage B
    insert_command_tx；ontology store 双后端同契约），「行存在⇔副作用已提交」成为
    可查证事实；**PK 即 fencing**——僵尸线程与对账放行后的重试并发提交至多一个
    commit，输家撞 `OperationAlreadyApplied` 整事务回滚、读现行台账回执按幂等成功收口；
  - **operation_id 透传**：executor 对副作用工具注入 `ctx.operation_id`
    （= invocation_id；reclaim 重试复用同行 ⇒ 同 id）；注入成功后回填
    `tool_invocation.operation_id` **对账资格章**（schema/046，纯增量 ALTER）——
    「台账无行 ⇒ 未提交」只对确实拿到 operation_id 的执行成立，Stage C 之前的遗留
    uncertain 行/未注入执行恒 NULL，永留人工通道，绝不误判 failed；
  - **回执查询协议 `check_operation(operation_id)`**：工具级三态（applied /
    not_applied / unknown）；`ontology_identity_resolve`（现网唯一 HIGH_WRITE 工具）
    已接线（台账后端 `operation_ledger.py`，可注入 InMemory 测试后端）；
  - **uncertain 自动对账**（`operation_reconciler.py`，随 reaper 周期）：
    applied→succeeded（台账回执补进 invocation 行 + P1-13 同事务审计
    event_type=invocation_reconcile）/ not_applied→failed（放行同键重试）/
    unknown→留人工；flag `RAG_AGENT_OP_RECONCILE_ENABLE` **默认 off**
    （RAG_AGENT_OP_RECONCILE_MIN_AGE_S=300 / RAG_AGENT_OP_RECONCILE_LIMIT=50）。
    已知残窗（如实记录）：非台账的 check_operation 实现（日后外部系统查证）下，
    not_applied→failed 后僵尸迟提交=invocation 行与事实漂移——同键重试经台账 fence
    幂等自愈，台账后端无此窗；
  - **per-tool worker 隔离**：副作用工具跑 `tool-iso-<name>` 专属线程池（容量=P1-02
    舱壁配额，舱壁 fail-fast 在前 ⇒ 池内永不排队），共享超时池归读工具——写挂死
    不再蚕食读、读洪峰占不到写额度；kill switch `RAG_AGENT_TOOL_ISOLATED_POOL`
    （默认 on，纯进程内资源编排无语义变化）；
  - **retention/purge**：`tool_operations` 作业（12 月，随 RAG_RETENTION_AGENT_TRACE_MONTHS
    与 tool_invocation 同窗）+ purge_subject 12 表链覆盖（run_id 归属链）。
- **Stage D**：多副本形态（dispatcher 拆独立部署、distributed admission、durable
  cancel、事件 durable log）——与外审 P1-10 前置条件合流。

## 4. 验收门（Stage A）

1. flag off：全量测试与行为零变化；
2. flag on：ask SSE 契约不变（session/tool_call/chunk/done 帧序）；
3. 故障注入：enqueue 后未 dispatch（模拟崩溃）→ 恢复扫描重驱且答案落库归属正确；
   已绑 run_id 的命令绝不重执行；attempts 封顶落 failed；
4. `make test` + `make lint` 绿。
