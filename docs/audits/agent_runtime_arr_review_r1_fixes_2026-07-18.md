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

## 3. 未修清单（R2/R3 待口令）

R2=P1-RT-03/04/06/07（gateway deadline 每 attempt 重查、按 provider attempt 计量、
resume 权威预算、checkpoint 生产硬门）；R3=P1-RT-05（多副本前提 SSE cursor）/08/09/10
+ P2 台账 15 条。Gate C（当前 SHA 压测/staging/多副本演练）归 B7 user-gated。
