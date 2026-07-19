# Agent Runtime 外审（ARR-20260718-c5776a2）核查 + R1 修复台账

日期：2026-07-18。评审对象：`~/.claude/uploads/.../Agent_Runtime_Production_Review_20260718.md`
（NO-GO，3.4/5.0，2 RB + 8 P1 + 15 P2，基线 `c5776a2`）。

## 1. 核查裁决（抽验 12/25，行号零偏差）

- **RB-RT-01 属实**：submit→bind 两步确认；`bind_and_done` docstring 自认双跑窗口
  （批次4 修法=响亮化）；043 DDL 注释「绑定前 at-least-once」的设计意图只覆盖
  「起跑前崩溃」，探针证明窗口实含已起跑 run；`agent_run` 无命令唯一约束。
- **RB-RT-02 属实**：失败/取消/预算三路 emit-first 与成功路径（P0-01 durable-first）
  不对称；`_safe_transition` 吞错。
- P1 抽验 6 条（03/05/07/09/10 属实；06 半属实半在案——零快照回退是 07-16 P1-05
  记录在案拍板）；P2 抽验 2 条（22/23 属实）。
- 定级校准三处：baseline-freshness 3 FAIL=RB-05 设计内；stress 旧 SHA=已知；
  P1-RT-08「二选一修法」之二≈现状+SLA（要求把在案取舍写成合同，非纯缺陷）。
- 暴露面：agent 架构留分支拍板 + 生产 RAG_AGENT_ENABLE 未开 ⇒ 全部发现不在现网
  执行路径上；定位=「agent 上生产之前的门」。

## 2. R1 修复 as-built（Sam 口令「修R1」，全落 ontology-p0）

### RB-RT-01 → command→run 原子绑定

- **schema/052**：`agent_run.dispatch_command_id CHAR(32) NULL + UNIQUE
  uk_run_dispatch_cmd`（fuling_operation；NULL 多值合法=非 durable 路径零影响）。
- **建行即绑**：`create_run` 读 `ctx.dispatch_command_id`（routes claim 成功后
  setattr，P1-12 message_id 同款先例）随 INSERT 原子落——绑定不再依赖 submit 后
  第二步 bind UPDATE。1054（052 未 apply）回退无锚 INSERT + 一次性 warning
  （**代码可先行**）；1062 撞 uk → 新异常 `DispatchCommandBound`（与
  uk_thread_active 的 ThreadBusy 按键名分辨）。
- **恢复先查后建**：`_dispatch_recovered_submit` 起手按
  `find_run_by_dispatch_command` 查既有 run——命中即返回其 run_id 让 dispatcher
  rebind+done 收敛（评审探针「original run 存活 + redriven=1」翻绿=零新 run）；
  submit 撞 DispatchCommandBound（并发双建输家）同样反查取赢家。快路径输家 → 409
  「已受理」。`bind_and_done` 降级为正向指针+收口（真绑定在 run 行）。
- **readiness**：durable-dispatch 契约探针加 052 列（flag 开 + 未 apply ⇒ critical）。
- **真库实证**：本地 MySQL 8 上 20 线程并发 create_run 同一命令 → 恰 1 赢家 +
  19 DispatchCommandBound；陈旧 NULL 多值不受限（`test_agent_dispatch_bind_db.py`，
  conftest 串行组，052 幂等 ALTER 就地补列）。

### RB-RT-02 → 终态 durable-first 统一协议

- 新 `executor._terminal_fail_durable(run_id, handle, error, *, to, retryable,
  notify)`：**先赢 CAS 再对外宣告**；CAS 落空 read-after-write——现状 succeeded ⇒
  闭嘴（完成侧胜者已自发）；其它终态 ⇒ 按库中事实幂等宣告（SSE 不悬挂且与库一致）；
  非终态/读不到 ⇒ **不宣告任何终态**（critical 留痕交 reaper/对账；「没消息」诚实于
  「假终态」）。
- 重接五位点：主循环 RunFailed 分支（emit 挪进 helper）/两处取消（原 `_safe_transition`
  吞错+无条件 emit）/StopIteration 协议违约/except-Exception/`_fail_over_budget`。
  审批拒绝位点（299）早已 durable-first，零改动。D3 fencing（失去所有权不落失败侧
  回调）语义保留（helper notify 门控）。
- **两处旧断言改判**（test_writetools_safety_batch3 / test_agent_fault_model）：
  「失去所有权终态帧照发」→「真相未知不发帧」——旧断言恰是评审探针抓的双真相。

### 验证

分支全量 **4241 passed + 1 skip**、全仓 ruff 绿；新测试 19 条
（test_agent_r1_atomic_terminal 17 + 真库并发 2）；评审两探针场景均有对应翻绿用例。
未验证声明：真实多副本/kill-9 矩阵（Gate C 族，B7）；052 staging/prod apply 未做
（user-gated）；main 不摘（agent 面留分支拍板）。

## 3. R2/R3 修复 as-built（Sam 口令「开始R2+R3」，2026-07-18）

**R2（P1-RT-03/04/06/07）**：gateway 每 provider attempt 前重查 deadline+HTTP 超时钳剩余
（`_transport_timeout_s` 下划线传输键约定）；计量改每实际 attempt（on_attempt 钩子，
旧桩签名探测回退不双扣）；Executor 权威预算随 suspend_run_atomic 同事务持久化
（GREATEST）+ 总墙钟 RAG_AGENT_MAX_RUN_WALL_S（默认 24h）钳段窗口；prod+agent ⇒
checkpoint 专用密钥/强制验签/静态加密三件硬断 + loop 生产加密失败不退明文。

**R3-P1（05/08/09/10）**：SSE Last-Event-ID 游标续读+trim reset 协议（帧携 _relay_id）；
relay publish 有界异步（delta 队满丢+计数、控制帧短等必达、RAG_AGENT_EVENT_RELAY_ASYNC
开关）；schema/053 对账退避/隔离（指数 30s..1h、上限 20 隔离，1054 代码可先行）；
mid-run crash 合同书面化（SLO≈20min 内收尸=修法二+SLA）+收尸 ops 告警。

**R3-P2（11..25 十五条）**：11 finish_invocation 条件 CAS（rowcount 回报）；12
consume_budget 显式 _begin；13 sanitizer 键感知+11 位数字标量掩码；14 义务链保留
artifacts；15 幂等命中写 tool_replay 审计；16 RunSuspended 内部载荷 exclude 皮带；
17 outbox drain 改 task_done 账本+agent_worker 关停排空；18 **核查结论已被复审推翻并纠正**（RR-2）：ci 的 agent 真库步骤实为**点名清单**
（非全量），已扩入 dispatch_bind_db/operation_ledger/stage_d 三文件；attestation 仍归 B7；19 DUPLICATE 重放 reason 以库内决定行为准；20 EDITED 不可
重驱→ops 告警；21 终态帧先于缓存性回调（summary 不再挡 SSE）；22 撤 sql.readonly.*
预授；23 high 档补链内 fallback（light 有意快败）；24 relay 副本工具参数脱敏；25
模型出口密级门 scaffold（RAG_AGENT_EGRESS_MAX_CLASS，tool_executor 盖章）。

验证：分支全量 4275 passed + 全仓 ruff 绿；R2/R3 新增 ~29 测试；新 env 6 个全保守默认。
未验证声明：多副本/kill-9 矩阵与当前 SHA 压测（Gate C=B7）；052/053 apply（user-gated）；
钉钉/console 真机断线续读 E2E。

## 4. 复审（ARR-ReReview @e15fa8d，3.8/5.0）核查 + RR-1/RR-2 修复 as-built

**核查裁决：七条全属实（0 纠偏）**——其中 P2-RR-04（改常量没接工厂=死代码）、
P2-RR-05（frozen setattr 被自己的 except 吞=空操作）两条抓的是 R3 修复本身；
P2-RR-06 推翻本台账 18 号核查结论（已上方纠正）。教训=验证深度：改常量必查消费方、
setattr 必查 frozen、断言 CI 行为必读 job 定义——本轮起修复验收一律探针驱动。

**RR-1（Sam 口令）**：①终态**全**收编——complete 异常/挂起失败/resume 交棒（原忽略
CAS 返回值）/complete 输家×2 统一进 `_terminal_fail_durable`（加 frm 参数）与新
`_declare_terminal_lost`（read-after-write：有事实按事实、succeeded/未知闭嘴）；
源级闸测试锁「helper 之外不得 emit 终态」。②relay 控制帧**绝不丢**（writer 存活
切片阻塞保序、死亡/60s 兜底同步直写乱序保达）+ `end()` 改 task_done 账本 flush
（与 llm_log_outbox P2-RT-17 同款修法——同一天在两处犯同一竞态）。

**RR-2**：reconciler 异常分支同退避（checker timeout=最常见查不清形态）；
default_gateway 工厂 high 档真接 fallback（env 同模型去重）；DUPLICATE 经
model_copy 按库内决定行**重建** outcome、决定行读不出**拒绝续跑**（绝不吃请求体
reason）；egress 盖章前置到写型 ctx 副本替换之前（原 ctx 可见）；CI 点名清单+3
真库文件；053 flag-conditional readiness critical（RAG_AGENT_OP_RECONCILE_ENABLE
开 ⇒ 两列必在）+ api 接线。

验证：4287 passed + 全仓 ruff 绿；12 条探针驱动新测试（复审 5 个探针场景全部翻绿）
+ 4 条旧断言改判（「真相未知仍发帧/合成作废帧」=双真相本体）。未收面（复审在案、
非本批范围）：trim 后全文快照与客户端 reset/replace 实现（console 侧工程）、双副本
断线 E2E、当前 SHA stress/attestation——均归 B7/Gate C。

## 5. 第三轮复审三条（RB-HA-01/P1-EVT-02/P2-APR-01）核查+修复 as-built

三条核查全属实。修复：
- **RB-HA-01**：`__end__` 封流资格改随「终态所有权」走——handle._relay_terminal 只在
  durable 背书终态帧写入后置位（helper CAS 胜/事实帧/完成成功/审批拒绝），finally 的
  `_finish(end_relay=...)` 按其门控；CAS 输家与真相未知路径**不封流**（消费侧靠终态帧
  或 is_terminal_fn 探针收流）。探针「__end__→run_completed 不可见」翻绿。
- **P1-EVT-02**：relay 检测到本 run 丢过 delta 时终态帧翻 `streamed=false` +
  `delta_dropped` 计数；回放端点见 delta_dropped 先发 `event: reset`（replace 语义）
  再补完整 final_text。无丢弃零改动（同实例真流式不重复答案）。console 侧 reset
  消费实现仍为待办（在案）。
- **P2-APR-01**：DUPLICATE **无条件**以库行重建（reason 含 NULL——「无理由」也是
  不可变事实；decided_by_effective 取库行原审批人）；决定行瞬时读不出 ⇒
  503+Retry-After（可重试存储语义，弃 409）。
验证：4293 passed+全仓 ruff 绿；+6 探针测试（三探针全翻绿）+源级闸（DUPLICATE 分支
禁 is-not-None 跳过）。

### RB-HA-01b（第四刀）：封流所有权收进 RunHandle._finish 本体

resume 交棒分支的裸 `_finish()`（默认 end_relay=True）在 helper CAS 输后仍封流——
同轮修复内即回归，证明调用点纪律不够。修法照评审建议：`_finish` 本体加
`_relay_terminal` 门（end_relay 之外还须所有权），不安全默认从此不存在，四个调用点
统一被护；668 行调用点条件降级为无害冗余。+3 探针测试（裸 finish 无所有权=零 __end__/
有所有权=恰一次/交棒输家场景翻绿）+1 旧协议测试改判（手工 handle 须显式持有所有权）。
验证：4296 passed+全仓 ruff 绿。

## 6. 遗留（全部 user-gated）

Gate C（当前 SHA 压测/staging/多副本演练/052+053 apply/真机断线续读 E2E）归 B7
user-gated；R1/R2/R3 代码面已全部落地。
