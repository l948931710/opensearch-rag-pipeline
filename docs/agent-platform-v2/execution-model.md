# 执行模型（B1）· 报告 §3 补节 / 评审 §7-P0-2 gate 交付

> ⚠️ **实现状态更新（2026-07-21 迁移批C，核查 M16.1）**：本文是 2026-07-07 冻结的**设计
> 决策**文档，下列冻结正文（§4/§5「尚待实现」）已被实现追上，阅读时以代码为准：
> - `loop.py::DefaultAgentLoop`、`executor.py::ThreadedRunExecutor`（含 `drain()` 排水 E3）、
>   `event_relay.py::_RedisRelay`（SSE 跨实例中继 XADD/XREAD）、per-thread 串行化
>   （schema/037 `uk_thread_active`）**均已落地**——§5 不再是「尚待实现」。
> - §4「无独立 agent worker 层」需修正为：**默认仍是嵌入式有界执行器（选项 b）**，但另有
>   **可选的独立 worker**（`agent_worker.py`，durable dispatch）——受 `RAG_AGENT_ENABLE` +
>   `RAG_AGENT_DURABLE_DISPATCH` 双 flag（默认关）+ 独立部署 gate 约束，非默认形态、非统一
>   worker 化。冷启动 recovery owner 缺口见生产就绪评审 B9。
> 下方正文保留冻结原样以存决策脉络，不逐句改写。

> 状态：**已拍板（2026-07-07）**。这是评审要求"先于 loop 接口冻结"的运行时执行模型。
> 决策口径：plan/报告的多实例模型 + 评审推荐 + 已建底座三者收敛。
> 关联代码：`agent_runtime/{context,events,run_store}.py`（已建）；`loop.py`/`executor.py`（按本节冻结的接口后续实现）。

## 0. 决策

**B1 = 选项 (b)：每进程有界执行器（per-process bounded RunExecutor）。**

run 主体在**专用有界线程池**里执行，与 HTTP 请求生命周期解耦；SSE 只做事件消费端；
跨 worker/实例的 resume 靠 `run_store`(RDS) + Redis 事件驱动，无粘性路由。

**依据（三者收敛）**：
1. **plan 模型**（[报告 §8](富岭企业级Agent底座架构设计报告-v2.md:538) "逻辑单入口、物理多实例"；[plan WS0-4](implementation-plan.md:33) 解除 `--workers 1`→`RAG_UVICORN_WORKERS`）= 一个 app × 多 uvicorn worker × 多 SAE 实例，**无独立 agent worker 层**。选项 (b) 正是这个模型；选项 (c) 要新起 plan 从没设计的层。
2. **评审推荐**（[B1 line 66](../reviews/agent-platform-v2-architecture-review-2026-07-06.md:66)）= "run 主体走**专用有界执行器/后台 task**"——就是 (b)；"独立 worker"(c) 只是并列候选，未被推荐。
3. **多 worker 化稀释风险**：WS0 状态外置已完成 → `--workers>1` 前提满足。一个卡死 run 的爆炸半径 = 1/(worker数×实例数)，评审 B1 的"agent 拖垮存量 RAG"失败场景大半被稀释。

**唯一硬伤**：Python 线程杀不掉 → 靠协作取消（turn 边界查 cancel 标志）+ 每个工具/模型调用的**硬 socket 超时** + 看门狗（到期标 failed、停止喂 run，僵尸线程等其 I/O 超时自退）。

**到 (c) 的接缝**：`RunExecutor` 接口拓扑无关 → 日后若需硬隔离，把"进程内线程池"换成"队列+独立 worker 层"，**不碰 `loop.py` 与工具契约**。

## 1. 执行流（时序）

```
HTTP/console 请求
  → 服务端构造 ExecutionContext（frozen，身份/ACL 服务端注入）
  → RunExecutor.submit(ctx, loop, messages, tools)   [有界池；满→RunRejected→429]
        └─ 后台线程内：run_store.create_run → status=running
           while turn < budget.max_turns：
             ModelGateway.complete → 解析 tool_calls
             yield ToolCallProposed ──► Runtime 驱动器：
                 PolicyEngine.authorize_tool_call(ctx, spec, args)
                   ├ ALLOW           → Executor 执行 → gen.send(ToolResult)
                   ├ DENY            → gen.send(ToolResult.denied)
                   └ REQUIRE_APPROVAL→ 写 checkpoint + approval_request(pending)
                                        → transition(running→suspended)
                                        → yield RunSuspended；**线程结束**（不占池）
             无 tool_call → yield RunCompleted → transition(running→succeeded)
  → SSE 端点：订阅该 run 的事件流（Redis Stream，跨 worker/实例），不驱动 loop

审批回调（钉钉/console，console-first）：
  → decide() 落 approval_decision（幂等键 first-valid-wins）
  → transition(suspended→resuming)  [CAS 认领，防两回调并发重入]
  → 发布 resume 事件 → **立即 ACK**（回调不背着续跑，B3）
  → 任一实例的 RunExecutor 消费事件 → submit_resume
        └─ 重建 ExecutionContext（**重解析身份**，needs_reauth）→ 重过 Policy
           → transition(resuming→running) → gen = loop.resume(ctx, checkpoint, outcome)
           → 续跑；结果经 conversation_id 主动投递（console 通知 / 钉钉卡片）
```

## 2. 冻结的接口（loop.py / executor.py 按此实现）

```python
# loop.py —— AgentLoop 契约。B2 解法：不用 ToolResultInjector，改 Generator.send 回注结果。
class AgentLoop(Protocol):
    def run(self, ctx: ExecutionContext, messages: list[Msg],
            tools: list[ToolSpec]) -> Generator[AgentEvent, ToolResult | None, None]: ...
    def resume(self, ctx: ExecutionContext, checkpoint: RunCheckpoint,
               outcome: "ApprovalOutcome") -> Generator[AgentEvent, ToolResult | None, None]: ...
# 驱动语义（Runtime 侧，单线程驱动生成器——无跨线程回注死锁）：
#   ev = next(gen)
#   while True:
#     if isinstance(ev, ToolCallProposed):
#         result = runtime.adjudicate_and_execute(ctx, ev)   # Policy → Executor
#         ev = gen.send(result)                              # ← B2：结果回注
#     else:
#         emit_to_sse(ev)                                    # ModelDelta/RunSuspended/...
#         ev = next(gen)

# executor.py —— 每进程有界执行宿主（拓扑无关接缝 → 日后可换独立 worker 层 c）
class RunExecutor(Protocol):
    def submit(self, ctx: ExecutionContext, loop: AgentLoop,
               messages: list, tools: list) -> "RunHandle": ...
    def submit_resume(self, ctx: ExecutionContext, run_id: str,
                      outcome: "ApprovalOutcome") -> "RunHandle": ...
    # 有界：并发 run 达 RAG_AGENT_MAX_CONCURRENT_RUNS（每进程）→ raise RunRejected（HTTP→429）

class RunHandle(Protocol):
    run_id: str
    def events(self) -> "Iterator[AgentEvent]": ...   # SSE 订阅端（Redis Stream 跨实例）
    def request_cancel(self) -> None: ...             # 协作取消（线程杀不掉的兜底）

# ApprovalOutcome —— resume 回注值（B2）；判别联合（四处置）
class ApprovalOutcome(Protocol): kind: str
class Approved(ApprovalOutcome):          kind = "approved"
class Edited(ApprovalOutcome):            kind = "edited";  edited_args: dict   # 重写历史 tool_call args
class RejectedFeedback(ApprovalOutcome):  kind = "rejected_feedback"; reason: str  # 理由回喂模型续跑
class RejectedTerminate(ApprovalOutcome): kind = "rejected_terminate"           # run=cancelled
```

**RunCheckpoint 语义 schema（B4）** —— `run_store` 只存 `state_blob` 字节；loop 层按此编码：
```
{ version: int,                       # 序列化版本号（跨 CD 版本 resume 兼容，F7）
  messages: [...],                    # 完整对话历史
  turn_slots: { call_id: 裁决态 } }   # 当前 turn 内每个 tool_call 的槽位：
     裁决态 ∈ executed(result) | pending_approval | not_adjudicated
规则：① 末单裁决后才 resume（未裁决槽位阻塞）；② 首个 REJECTED_TERMINATE 即止；
      ③ EDITED 时重写该 call_id 的 args 使 messages 自洽（避免 resume 时 DashScope 400 / 重复执行）。
```

## 3. B-theme 逐条落点

| # | 问题 | 落点 |
|---|---|---|
| B1 | 无并发/执行模型 | **(b) 每进程有界执行器**；per-instance 上限 `RAG_AGENT_MAX_CONCURRENT_RUNS`，满→429 |
| B2 | ToolResultInjector 无定义 | **取消该抽象**，改 `Generator[AgentEvent, ToolResult\|None, None]` + `gen.send()` 单线程回注 |
| B3 | resume 跑在回调线程 | 回调只 `decide`+`transition(suspended→resuming)`+发事件+ACK；续跑由执行器消费。**`resuming` 态已加 run_store** |
| B4 | 同 turn 多 tool_call 挂起语义 | checkpoint `turn_slots` 按 call_id 建模；末单裁决后 resume；EDITED 重写 args |
| B5 | running 崩溃恢复 vs checkpoint 时机 | running 崩溃**无 checkpoint** → 心跳超时对账标 `failed`（不重放半执行 turn）；只有 suspended/resuming 可 resume |
| B6 | decided-but-not-resumed 卡死 | 对账扫 `resuming` 超阈值 → 重发 resume 事件（run_store 已可查 resuming） |
| B8 | frozen ctx 装不下可变 budget | **消耗落 run_store**（`turns_used/tool_calls_used/tokens_used` 列 + `consume_budget()` 已建）；caps 留 `RunBudget`；重解析阈值 `DEFAULT_REAUTH_THRESHOLD_S=300` 已定 |

## 4. 并发 / 拒绝 / 排水

- **专用池**，绝不复用 Starlette 请求线程池（复用=评审 B1 描述的"拖垮 /api/ask"本身）。
- 溢出 → `RunRejected` → 429「系统繁忙」（容量层 fail-closed）。
- **发布/缩容排水（E3）**：SIGTERM → 新 run 拒收；in-flight run 在 turn 边界主动 checkpoint 挂起（HIGH_WRITE 标"不可中断窗口"）；被杀 run 由心跳对账判 superseded。
- **per-thread run 串行化（C1）**：同 thread 并发两 run → RDS `(thread_id, active)` 唯一约束或 Redis per-thread 锁（排队 or supersede，WS1 loop 实现时定）。

## 5. 尚待实现（本节只冻结接口，实现在 WS1）

- `loop.py` 自研 DefaultAgentLoop（事件流 while 循环）+ `executor.py` 有界 RunExecutor；
- `ApprovalOutcome` 判别联合落 `agent_runtime/approval.py`（或 events 同级）；
- SSE 跨实例事件中继（Redis Stream XADD/XREAD）；
- C1 per-thread 串行化 + E3 排水协议（loop 冻结后随实现落地）。
