# 审计复核修复批次规划 — 2026-07-12

**验证基线**：对 17 项既往审计问题（A1-A7 / B1-B3 / C1-C4 / D1-D3）在 tip `5f561cb`（分支
`claude/ontology-p0`）上的逐项代码复核（每项一核查 + 一对抗复核；C4/D3 为单方核查）。
结论：**8 项仍在 / 7 项部分修复 / 2 项已修复（A6 心跳、D2 retention 节点）**。
本文档把 15 个未闭合项按**上线闸门**整理成修复批次：批内代码落点相邻、可独立测试合入；
批与批之间除标注外无代码依赖，可并行开发、按闸门顺序合入。

## 复核状态速览

| 状态 | 项 |
|---|---|
| 🔴 仍存在 | A1 串行化、A2 计费、A4 幂等、A7 前端、B2 Redis SPOF、C1 目标锁、C2 归一召回、C3 先截断后 ACL |
| 🟡 部分修复（残留面见各批次） | A3 演练、A5（drain 已修/cancel 缺）、B1（后端已备/默认 memory）、B3（中继已实现/挂起-续跑断流）、C4（schema 已落/签字 0/4）、D1（GA 已证/251 未重冻）、D3（成功侧已 fence/失败侧盲） |
| 🟢 已修复 | A6（后台心跳 ticker）、D2（retention 节点关归档；现网生效待重粘贴 → 批次 7） |

复核中的新发现（原清单外）：
- **B3 断流**：审批挂起时 `RunHandle._finish` 无条件 `relay.end()` 写 `__end__` 哨兵，读侧遇之即收流——续跑段全部事件（含最终答案帧）经 `/api/agent/runs/{id}/events` 永不可达。
- **A2 加重**：`/approve` 续跑以 `count_llm=False` 准入且空轮 retries 重置为 5，续跑段模型调用对全局熔断零扣减。
- **C2 覆盖缺口**：S9 false-merge 硬闸（backtest）走 resolver 路径，不覆盖播种侧 `title_matches` 召回。

## 依赖图（哪个闸门卡哪批）

```
批次1（准入+计费）──┐
批次2（生命周期+流）─┴─→ 开 RAG_AGENT_ENABLE 灰度
批次3（写工具安全线）───→ 注册任何 HIGH_WRITE 工具 / 开 RAG_ONTOLOGY_TOOLS_ENABLE → 之后才能做 A3 staging 演练
批次4（前端小修）───────→ 无闸门，随时可合（建议随批次2 一起，stop 按钮要接 cancel 路由）
批次5（多实例配套）─────→ --workers>1 / 多 SAE 实例
批次6（ontology 治理）──→ 真实 seeding / 任何 auto 路径开启
批次7（流程/user-gated）─→ 穿插进行，多数依赖 Sam
```

---

## 批次 1：Agent 准入与计费（开闸 P0）——A1 + A2

**闸门**：`RAG_AGENT_ENABLE` 灰度前必须合入。两项都在 ask 准入层，一起改一起测。

> **状态（2026-07-12）：✅ 代码侧完成**（落 claude/ontology-p0；全量 3466+ 绿 + 真库
> e2e 19 绿 + lint 绿）。schema/037 已 apply **本地**+记台账；staging/prod apply 留批次 7。
> 两处与原案的实现偏差（均更贴合验收标准）：
> ① **A2 挂点在 `make_model_fn`（model_gateway.py）而非 executor._drive_gen**——空终轮
>   重试发生在 loop._drive_model 内部、不产生独立事件，executor 只能看到轮边界；
>   model_fn 每次调用恰好一次 gateway 调用，多轮/空轮重试/续跑段天然全覆盖。先扣后调
>   （超帽的那次调用不发出去），超帽 → BudgetExceeded → run 诚实落 failed。
> ② **/approve 取括号里的备选**：保持 count_llm=False + 显式豁免注释——审批是治理动作
>   （撤回/拒绝须恒可达，触顶 503 审批=制造审批黑洞），续跑消耗由逐调用计费覆盖。
> 另：eval_harness/agent 传 `charge_llm=False`（本地跑批不烧准入配额，否则 251 题×多轮
> 中途撞 user_per_day 帽）；thinking 档按 thinking_cap_weight 加权（对齐准入侧 P2-11）。

### A1 per-thread run 串行化 【M】
- `schema/037_agent_run_serialization.sql`：MySQL 不支持部分唯一索引 → 生成列
  `active_thread VARCHAR(128) GENERATED ALWAYS AS (IF(status IN ('running','suspended','resuming'), thread_id, NULL))`
  + `UNIQUE KEY uk_thread_active (active_thread)`（MySQL UNIQUE 允许多 NULL，终态行不占坑）。
- `run_store.create_run`（run_store.py:101）：捕获 1062 → 抛 `ThreadBusy`。
- `routes/agent.py` ask 入口：`ThreadBusy` → **409**「该会话已有回答在进行中」。
- 语义决策：thread 有 **suspended**（等审批）run 时新 ask 也 409（non-terminal 一律互斥）。
- 测试：同 thread 并发双 submit → 恰一个 409；审批挂起中再 ask → 409；终态后可再 ask。
- ⚠️ 迁移需三环境 apply（进批次 7）。

### A2 逐模型调用计费 【M】
- `rate_limiter.py`：新增 `charge_llm_call(user_id, weight=1)`（memory + Redis/Lua 两后端），
  只扣全局/每用户日计数、不做准入判定。
- `executor._drive_gen`：每次模型调用返回后（executor.py:347 附近，**含空终轮重试**）调 charge；
  超帽 → run 诚实以 `failed(budget_exceeded)` 收尾（复用 _fail_over_budget 模式）。
- `routes/agent.py:685`：`/approve` 续跑改 `count_llm=True`（或由逐调用计费覆盖后显式注释
  豁免准入、保留逐调用扣减）。
- 运营配套（进批次 7）：`RAG_GLOBAL_DAILY_LLM_CAP` 语义从「次/ask」变「模型调用数」，
  阈值按实测倍率重定并公告。
- 测试：一个 3 轮 run 扣 3+；空轮重试也扣；续跑段扣；超帽 run 中断且状态诚实。

---

## 批次 2：Run 生命周期与事件流（开闸 P0）——A5 残留 + B3 断流

**闸门**：同批次 1。都改 `executor.py`/`routes/agent.py`，与批次 1 有文件重叠 →
**批次 1 合入后再开这批**（或同一人连做）。

> **状态（2026-07-13）：✅ 代码侧完成**（落 claude/ontology-p0；全量 3490 绿 + lint 绿；
> 测试 tests/test_agent_cancel_and_relay_continuity.py 10 例，含台账验收用例
> 「FakeRedis 下 suspend→resume→replay 全帧可达」与「cancel 后 cancelled 终态/槽位释放/
> 中继终态帧」）。与原案的实现说明：
> ① B3 修在**写侧** `RunHandle._finish(end_relay=...)`——本地队列恒投哨兵（原 /ask SSE
>   在挂起帧后照常收流），仅真终态写中继 `__end__`；读侧 `stream_run_events` 无需改
>   （`_TERMINAL_TYPES` 本就不含 run_suspended，不会在挂起帧 return——复核时的读侧
>   改动项经核实为已然成立）。RejectedTerminate 裸 handle 已补 `_attach_relay`。
> ② A5 语义分支：running+本实例句柄→202；suspended→409（走审批拒绝/撤回）；
>   resuming→409 瞬态；终态→409；无本实例句柄→501（跨实例标记留 v2，由 reaper 收尸）。
>   准入 count_llm=False（治理动作恒可达，同 /approve 豁免理由）。
> ③ SSE 断连不自动 cancel：GeneratorExit 只记 `handle._client_disconnected_at`（grace-cancel
>   迭代的输入），显式取消走新端点。cancel 在轮边界生效（阻塞中的模型/工具调用不中断）。
> ④ 前端 stop 按钮接本端点 = 批次 4 载荷（A7），本批未动前端。

### B3 挂起-续跑中继断流（复核新发现）【S】
- 最小修：`RunHandle._finish`（executor.py:94-95）只在**真终态**（succeeded/failed/cancelled）
  时 `relay.end()`；RunSuspended 路径不写 `__end__`。
- 读侧 `stream_run_events`（event_relay.py:137-138）：遇 `run_suspended` 帧不 return，
  继续 XREAD 等续跑段（总超时 1800s 已有兜底）。
- 顺手修：RejectedTerminate 裸建 RunHandle（executor.py:196-198）补 `_attach_relay`。
- 测试：FakeRedis 下 suspend→approve→resume→replay 全帧可达（当前测试矩阵缺这条）。

### A5 服务端 cancel 【S-M】
- `POST /api/agent/runs/{run_id}/cancel`：门禁同 run 详情（本人或 kb_admin，他人 404）→
  找到 handle 调 `request_cancel()`（协作检查点 executor.py:361-365/451-455 已存在，接上即活）；
  跨实例场景本实例无 handle → 501/提示（写 Redis cancel 标记留 v2）。
- SSE 断连策略决策：默认**不**自动 cancel（防移动网闪断误杀），但 `GeneratorExit` 时记录
  disconnect 时间，供后续 grace-cancel 迭代。
- 文档写明：cancel 在轮边界生效（阻塞中的模型调用不中断）。
- 测试：cancel 后 run 落 `cancelled` 终态、槽位释放、relay 收到终态帧。

---

## 批次 3：写工具安全线——A4 + D3 残留 + C1

**闸门**：注册任何 HIGH_WRITE 工具进默认 registry / 开 `RAG_ONTOLOGY_TOOLS_ENABLE` 之前。
A3 的 staging 演练排在这批合入之后。

> **状态（2026-07-13）：✅ 代码侧完成**（落 claude/ontology-p0；全量 3510 绿 + lint 绿；
> 测试 test_writetools_safety_batch3.py 7 例 + test_subject_purge 新 3 例 +
> test_ontology_store C1 5 例×memory/rds 双后端）。与原案的实现说明：
> ① A4 幂等复核：find_succeeded_invocation 增返 args_digest；命中不匹配 → 拒绝复用 +
>   告警 + 派生键 `{key}:a{digest(args)[:16]}` 真执行；历史行无摘要沿用复用（fail-open）。
>   键格式改 `run_id:t{turn}:call_id`（policy 层统一，同轮重放稳定/跨轮不撞）+ gateway
>   兜底 call_id 加 uuid 片段（`call_{i}_{hex8}`，审批 grant 键同免撞）——采用原案「或」
>   后方案并两者都做。
> ② D3：失败侧三处（RunFailed 分支/异常兜底/_fail_over_budget）改 checked CAS——
>   失去所有权（purge 删行/收尸/取消抢先）不再调 _notify_failure，本地 SSE 事件照发；
>   purge_subject 前置查非终态 agent_run（running/suspended/resuming），存在即 fail-closed
>   拒绝（dry-run 报 blocked_by_runs，commit raise）；agent_run 表缺失（1146，未铺 022）
>   视为无 in-flight。处置顺序=先 cancel（批次2 端点）/处置审批/等收尸再擦除。
> ③ C1：RDS `_lock_active_target`（FOR UPDATE + status='active' 断言，复用 mark_duplicate
>   模式）挂 insert_identifier / repoint（新 target）/ confirm_case / closing_case 四路径，
>   锁序统一「case → object」防死锁；mint_object_with_alias 不需要（target=同事务新建对象，
>   已注释说明）；memory 后端 `_assert_active_target` 同语义（repoint 经内部 insert 同享）。

### A4 幂等复核 args + 弃位置序号键 【M】
- `run_store.find_succeeded_invocation`（run_store.py:468）：SELECT 增返 `args_digest`。
- `tool_executor`（tool_executor.py:240-249）：命中时比对 args_digest，不匹配 → **拒绝复用**，
  告警 + 按内容派生新键执行。
- `model_gateway.py:570`：兜底键至少含轮次消碰撞（`f"call_t{turn}_{i}"`），并修正 policy.py:237
  与 gateway 行为矛盾的注释；或统一改为 policy 层 `digest(tool_name + canonical_args + turn)` 派生。
- 测试：同键不同 args → 不复用；无 provider id 的两轮同工具调用 → 键不碰撞。

### D3 失败侧 fencing + purge quiesce 【S-M】
- `executor.py:448/459-462/662-670`：失败侧回调改 checked CAS gate——`_safe_transition`
  返回 False（行已被 purge 删/失去所有权）则**不调** `_notify_failure`（与完成侧 :435 对齐）。
- `retention.purge_subject`（retention.py:521-612）：前置查该 user_id 的非终态 agent_run，
  存在则 fail-closed 拒绝并列出 run_id（批次 2 的 cancel 落地后可升级为「先 cancel+等待再 purge」）。
- 测试：purge 后 RunFailed 不再 INSERT qa_session_log；有 in-flight run 时 purge 被拒。

### C1 别名写锁定目标对象 【M】
- `ontology/store.py`：`confirm_case_with_identifier`(:751)、`repoint_identifier`(:617)、
  `insert_identifier`、`insert_identifier_closing_case`(:810)、`mint_object_with_alias`(:703)
  事务内对目标 object `SELECT ... FOR UPDATE` + 断言 `status='active'`
  （直接复用同文件 `mark_duplicate` :481-490 的现成模式）。
- 测试：memory 后端加语义断言；RDS 侧至少单测锁 SQL 形状（含 FOR UPDATE + active 断言路径）。

---

## 批次 4：前端小修——A7 【S，无闸门，建议与批次 2 同车】

- `useAgentAsk.ts:464`：stopAgent 按 `streamConvId` 反查 `conversations` 取消息列表，
  不用 activeId 派生的 `messages.value`；顺修 :475-478 误标「已取消本次提问。」的误伤路径。
- 空 chunk 帧双端 guard：后端 `routes/agent.py:431` 与回放端点 :1147 加 `if ev.text` guard；
  前端 chunk 分支（useAgentAsk.ts:263-268）空 content 直接 return。
- stop 按钮接批次 2 的 cancel 路由（当前只断视图不停服务端）。
- spec 补两用例：流中切会话、纯 reasoning 空 chunk。

---

## 批次 5：多实例配套——B1 残留 + B2

**闸门**：`--workers>1` 或多 SAE 实例部署前。

### B1 强制 Redis + fail-loud 【S】
- 新 env `RAG_REQUIRE_REDIS=true`（生产部署模板默认带）：置位时 `_make_limiter`
  （rate_limiter.py:781-791）init 失败 **raise 拒绝起服**，不再静默回退 memory；session_store 同。
- 部署文档：多实例清单必含两个 backend flag + REQUIRE_REDIS。

### B2 ready 解耦 + fail-open-with-alarm 【M + infra】
- `/api/ready`（api.py:586-603）：限流 Redis 检查降为非 critical + 响亮告警（或加
  `RAG_READY_REDIS_STRICT` 开关，默认宽松）——防 30s failover 摘掉整个集群；
  ask 路径 fail-closed 503 保留（成本护栏）但补 Retry-After。
- `redis_client.py:72`：支持 Sentinel/Cluster 接入（redis-py 原生）。
- Redis HA 部署（双副本/自动切换）→ 批次 7（infra，user-gated）。

---

## 批次 6：Ontology 数据治理——C2 + C3

**闸门**：真实 seeding / 任何 auto 路径开启前（与批次 7 的 gate 签字并行推进，
但 auto 开启必须两者都齐——C2 正是 S9 硬闸测不到的盲区）。

### C2 归一化感知召回 【M-L】
- `schema/03x`：`ontology_object` 加 `normalized_title` 列 + 索引，含存量 backfill 迁移。
- `store.find_objects` 增归一键等值查询；`seeding.title_matches`（seeding.py:197-207）改等值召回，
  废弃 raw LIKE 预过滤；召回超 LIMIT 时告警（截断可观测）。
- `scripts/ontology_backtest.py` 增**播种路径 would-auto 仿真**——当前 S9 硬闸只测 resolver 路径，
  拦不住播种侧误合并（复核确认的覆盖缺口）。

### C3 先 ACL 后截断 【S】
- `routes/ontology.py:209`：`_enrich_candidates` 改授权集合上取 top-N（对齐同文件 :323
  `search_objects_authorized` 及 agent 工具侧已修模式），另返 `hidden_count`。
- 若团队决定保留 stub 展示（HITL 告警价值），必须补显式决策记录——二选一，不能维持现状。
- 更新 `test_ontology_acl_matrix.py`（当前把反模式锁成契约）。

---

## 批次 7：流程 / user-gated 清单（穿插，多数依赖 Sam）

| 项 | 内容 | 依赖 |
|---|---|---|
| C4-签字 | 补 gate ② 模板；①③④+② 四个 org gate 真签 | Sam/信息部 |
| C4-台账 | 033 状态裁决：只读会话查三库 schema_migrations，改正 README 或补 apply | .env.production 只读 |
| D1-重冻 | 3.7 上重冻 release-gate（先决策口径：251 题金集 vs golden_50=76）+ answer-tier 重标定；refreeze run#2 前置：Sam `/login` 恢复 judge + rerank 开启 | Sam |
| D1-GA | prod VPC 端点（vpc-cn-beijing）对 qwen3.7-plus 一次直接探测 | 部署窗口 |
| A2-配套 | RAG_GLOBAL_DAILY_LLM_CAP 按新计费单位重定 + 公告 | 批次 1 合入后 |
| A3-演练 | staging 真实 identity_resolve 全链路：suspend→approve/edit/reject→resume→uncertain→reconcile | 批次 3 + gate 签字 + SAE 重打包 |
| D2-粘贴 | retention 节点新脚本重粘贴到 DataWorks 控制台 | DataWorks 控制台权限 |
| B2-HA | Redis 双副本/自动切换落地 | infra |
| 迁移 apply | 批次 1（037，本地已 apply+台账 2026-07-12）/批次 6（03x）——staging/prod apply | Sam 授权链 |

---

## 建议执行顺序

单人串行：1 → 2（+4 同车）→ 3 → 6 → 5，批次 7 穿插。
两人并行：一人 1→2→4，一人 3→6；批次 5 谁先空谁做。

**规模合计**：批次 1 M+M，批次 2 S+SM，批次 3 M+SM+M，批次 4 S，批次 5 S+M，批次 6 ML+S——
纯代码面约 6 个 M 当量，参照「压测批次1」（7c0a02c）节奏约 3-4 个工作批次可清完代码侧；
流程侧取决于签字/登录/授权节奏。
