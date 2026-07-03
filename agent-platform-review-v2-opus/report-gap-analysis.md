# 报告 — Repo 缺口分析（V2）

> 以富岭真实代码为基线,逐条裁决 PDF 第一版主张,并映射到 §4 模块 A–N 的状态判定。
> 状态判定七选一:`复用现有` / `小幅扩展` / `新建独立模块` / `引入完整依赖` / `延后建设` / `当前不建设` / `证据不足`。

## 1. PDF 核心主张裁决表(支持 / 推翻 / 重写 / 证据不足)

| # | PDF 主张 | 裁决 | 证据 |
|---|---|---|---|
| P1 | 主循环选 Qwen-Agent,BaseTool+@register_tool+并行调用+MCP 齐备,"零适配命中 DashScope" | **重写** | ✅ BaseTool/register_tool/MCP/DashScope 原生均成立(`qwen_agent/tools/base.py:24,44`,`llm/qwen_dashscope.py:49`);⚠️**"并行调用"失实**——模型单轮可发多个 tool_call,但执行层是同步串行 for 循环,`agents/` grep ThreadPool/asyncio.gather **0 命中**(`fncall_agent.py:94-105`)。Adapter 成本确实极低(继承 BaseTool 覆写 4 属性+call,可绕开全局 registry) |
| P2 | 单 Agent + 工具优先,不建重型多 Agent | **支持** | Repo 无多 Agent 需求;写型任务(单据/U8写回)需单一连续上下文。判定不变。 |
| P3 | 交互网关借鉴 Hermes 单进程 IM 网关 + 钉钉 Stream 反向连接 | **支持(需加注)** | 富岭钉钉双通道已实现;Hermes 是**单机单进程**,富岭要面对 **SAE 多副本**——差异点(会话粘性/分发)是 Hermes 未解决的,不能照搬(见 `open-source-code-review` Hermes) |
| P4 | HITL 借鉴 LangGraph interrupt/resume;检查点存 **Redis**;审批经钉钉互动卡片 | **重写** | interrupt/resume 语义 ✅ 值得抄;但**检查点存 Redis 是错的**:跨天审批 + Redis 易失 + LangGraph 官方 checkpointer 生产级仅 Postgres → durable checkpoint 必须落 **RDS**(见 P8/B 模块)。钉钉卡片底座 ✅ 已存在(`dingtalk_bot.py:1012`),仅需扩审批 dispatch |
| P5 | 安全吸取 OpenClaw 教训:审批信道带外 + 沙箱框架级校验 | 证据不足(OSS 补审中) | 设计规则方向正确;OpenClaw 源码级论证在补跑工作流,`borrowing-matrix` 完成后闭环 |
| P6 | 国内平台(百炼/Coze/Dify)不能替代自建编排层 | **支持** | 与"钉钉 Stream 唯一入口 + 原生 HITL + ACL 透传"约束一致 |
| P7 | DashScope 结构化输出仅 json_object、无 json_schema | 支持(PDF 已复核) | 与 Qwen-Agent 源码不冲突;Text-to-SQL/KIE 须 prompt + 后置 AST/字段校验兜底 |
| P8 | LangGraph AsyncPostgresSaver 用 **threading.Lock** 串行化并发 | **推翻(机制)/ 保留(现象)** | ❌ 是 **asyncio.Lock**(`checkpoint-postgres/.../aio.py:43,59`),threading.Lock 只在同步类;✅ 但确有实例级串行化:`aio.py:374` 单实例所有 DB 操作持同一 asyncio.Lock,连接池被架空(对照 Store 侧已修:池化连接不共享锁)。**PDF 机制说错,结论方向对** |
| P9 | LangGraph 官方 Qwen 路径 langchain-qwq/ChatQwen;checkpointer 生产级仅 Postgres | 支持 | LangGraph 源码证实 Postgres 是唯一生产 checkpointer(`checkpoint-postgres`),Redis 无官方实现;Store 长期记忆同样 Postgres+pgvector |
| P10 | Spring AI Alibaba 原生 HumanInTheLoopHook,三态 **APPROVED/REJECTED/MODIFIED** | **重写** | ✅ HumanInTheLoopHook 是核心库真实类(`.../hook/hip/HumanInTheLoopHook.java:47`);⚠️**第三态叫 `EDITED` 不是 MODIFIED**(`InterruptionMetadata.java:285` `enum FeedbackResult{APPROVED,REJECTED,EDITED}`,grep MODIFIED 0 命中)。传 "MODIFIED" 会 `IllegalArgumentException`。能力在,名称错 |
| P12 | Qwen-Agent 自带沙箱不达生产 | **重写** | 应从"无沙箱"改为"有基础 Docker 沙箱但隔离不足":当前 HEAD 已是 Docker(`code_interpreter.py:257`),但❌无 `--network none`、❌ kernel 端口发布 0.0.0.0、❌ 无资源限额、协作式超时可绕过;`python_executor` 源码自认 "Not sandboxed. Do not use for production"(`:98`) |
| P13 | Hermes 审批 fail-closed;⚠️审批与沙箱是替代关系(陷阱) | 证据不足(OSS 补审中) | Hermes 单进程网关 ✅;审批/沙箱替代语义待 borrowing-matrix 闭环 |
| P14 | OpenAI Agents SDK needsApproval+interruptions+粘性批准;Sessions 多后端(含 Redis) | 支持 | 见 `open-source-code-review` OpenAI-Agents;API 形状与 LangGraph interrupt/resume 互印证,作 HITL 装饰器 API 设计参考 |
| P15 | Google ADK state 四级作用域 + Plugin 全局横切;部署强绑 GCP | **重写** | ✅ 四级作用域(session/user:/app:/temp:)机制真实且干净,temp 统一剪除是最值得抄的一条;⚠️**Sequential/Parallel/Loop 三件套本版本全部 @deprecated**,官方指向新 `Workflow` 图引擎(`sequential_agent.py:49`);⚠️无 dept/tenant 层(硬编码三前缀),user/app 合并是 `dict\|` 无删除语义。部署非强绑 GCP:Database 后端(SQLAlchemy)可自托管境内 |
| P16 | OmO 任务类别→模型路由用户可配置 | 证据不足(OSS 补审中) | model routing 设计待 borrowing-matrix 闭环 |
| P19 | XiYan-SQL 分数/QwenCoder 可境内部署/mcp 可作现成组件 | 证据不足(OSS 补审中) | Text-to-SQL 可拿走什么、必须自建什么(语义层/只读守卫/ACL 注入)待闭环 |
| P20 | KIE:DocMind 仅通用 KV / Qwen-VL 自定义 schema | 支持(PDF 已复核) | 与 Repo 无冲突(Repo 无 KIE 代码,全新域) |
| P21 | 钉钉 Stream 多副本需自建会话粘性/分发 | **支持(强化)** | Repo 证实**无锁/无租约/无粘性**(`dingtalk_stream_runner.py` 无 leader election);多副本=事实多活消费+状态错乱,是故障形态。**这是 P0 硬约束** |
| P22 | 底座现状(HA3三路/DataWorks/SAE+FastAPI/钉钉唯一入口/多部门ACL已生效/数据留境内) | 支持 | 全部由 Repo 证实(见 repo-architecture-map) |
| P23 | ACL 透传"复用现有 retriever.py,不统迁 ACL" | **支持(强化)** | ✅ ACL 成熟、fail-closed、服务端生成、读写分离(kb_authz H1);但注意无 primary_dept(是 acl_groups 列表),且 Phase D flag 默认 off。Agent Runtime **必须复用此授权入口,不得重建第二套** |
| P24 | "现有 RAG 封装为首个 BaseTool"是 P0 起步 | 支持 | Repo 确无工具骨架,RAG 是最自然的首个工具;I/O 契约可从 `answer_flow.build_qa_log_kwargs` + `extraction/schema.py` 生长 |
| P25 | 已有 251 金标集可复用为评测底座 | 证据不足→部分支持 | eval_harness/ + tests/eval/ 存在 golden set;具体是否 251 条、judge/A-B harness 完整度需细查,但方向支持"复用现有评测底座" |
| P26 | Redis 会话后端:会话历史 + HITL 检查点统一落 Redis | **推翻** | ❌ Redis 0 接入;⚠️**"统一落 Redis"违反分层原则**:临时会话/缓存/锁可 Redis,但 durable checkpoint / 审批记录 / audit 必须落 **RDS**(跨天审批 + 易失性 + 已有 RDS 事务/outbox 资产)。这是 PDF 记忆模型最薄处 |
| P37 | U8 写回不走 API(U8 不开放)→ 经信息部附属库/中间表 + 钉钉审批流 | 支持(企业约束) | Repo 无 U8 代码接入(仅作 RAG 语料);写回域是全新建 EnterpriseTool,须复用 outbox+reconcile 补偿模式 |

---

## 2. §4 模块 A–N 状态判定(七选一)

| 模块 | 判定 | 依据(Repo 证据 + PDF 关系) |
|---|---|---|
| **A Runtime 边界** | **新建独立模块** | Repo 无任何 Runtime/Adapter 分层。须建 ExecutionContext/EnterpriseTool/AgentLoop Adapter/Policy Engine/StateStore/ModelProvider/ApprovalProvider/Audit 接口边界。Qwen-Agent 只经 AgentLoop Adapter 接入(P1) |
| **B Memory/State/Durable** | **新建独立模块(P0 接缝)** | ⚠️PDF 最薄。Repo:会话=进程内 LRU、durable=仅摄取侧 RDS 状态机、Redis 0 接入。须交完整分层记忆数据模型 + StateStore/Memory Service 接口。durable checkpoint 落 RDS(推翻 P26/P4 的 Redis) |
| **C Tool Contract/Registry** | **新建(从现有模式生长)** | Repo 无 registry,但 `extraction/unified_extractor`(按 key 分发)+ `DAGNode`(status/duration/error/result)+ `answer_flow`(全字段单点+状态词表)是现成生长点。业务工具经 Adapter 转 Qwen-Agent BaseTool,不直接依赖 |
| **D Policy Engine** | **小幅扩展现有** | ACL/deny-first 已在 retriever+kb_authz 成熟(11 条 fail-closed 路径);Policy Engine 应封装现有授权入口为统一决策点,**不重建**。工具权限/风险等级/是否需审批为新增维度 |
| **E Durable Workflow** | **复用现有 + 小幅扩展** | RDS 行级状态机(SKIP LOCKED 认领/2h stale/retry≤3/drain 循环)是现成"至少一次+幂等重入",直接移植为 agent_run/agent_step 执行语义。**不引 Temporal**(过度);LLM 负责意图,确定性流程用现有状态机模式 |
| **F Model Gateway** | **新建独立模块** | Repo 裸 HTTP 直调 DashScope(`llm_generator.py`),无 provider 抽象。须建 ModelProvider 接口(境内池:DashScope 默认 + 可配其余境内 provider/自托管);task→profile 路由、fallback、结构化输出后置校验 |
| **G Prompt/配置版本** | **新建(轻量)** | Repo prompt 为手写字符串常量;无版本/rollout/A-B。须建 prompt version + 与 tool version 关联 + 运行记录版本重现 |
| **H Observability/Eval** | **小幅扩展现有** | trace(X-Request-Id)/audit(kb_audit_log)/metrics(qa_daily_metrics)/eval(golden set)/feedback 均在。扩:工具调用 trace、审批 trace、E2E 完成率、按 run 的 token/成本归集 |
| **I Reliability** | **复用现有模式** | 幂等键/超时重试/熔断/outbox补偿/去重 均有成熟模式(散落),收敛为 tool 级 policy。kill switch/circuit breaker 部分已有(rate_limiter 全局日熔断) |
| **J 安全/Secrets** | **小幅扩展 + 吸取 OpenClaw** | env_guard 写守卫/GuardedDBConnection/三层写守卫/最小权限已有;须补:审批信道带外(OpenClaw 教训)、SQL allowlist、tool result injection、MCP trust |
| **K 数据契约/语义层(T2SQL)** | **新建独立模块** | Repo 无 T2SQL。须建业务语义层/只读视图/join allowlist/row-level security/EXPLAIN/result validation,不能简化为"模型生成 SQL+AST 校验"(P34) |
| **L 部署/扩缩容** | **新建(P0 约束)** | 当前 `--workers 1` 单活由部署约定强制。SAE 多副本须解:钉钉 Stream 分发/会话粘性/分布式锁/message dedup 迁 Redis。"逻辑单入口"≠物理单实例 |
| **M DevEx/CI-CD** | **小幅扩展现有** | tests/ 覆盖广、有 prod-guard/parity 测试;但**无自动迁移执行器**(人肉 scratch 脚本),CI 现状需核实。补:contract test、mock model/tool、schema migration 自动化 |
| **N 治理/责任边界** | **延后建设(P4 阶段)** | 谁能注册工具/改风险/发布 prompt/调 ACL 的治理面,平台化阶段建;管理面须与工具调用信道分离(OpenClaw 教训) |

---

## 3. Top 10 缺口(映射真实 Repo,按优先级)

| # | 缺口 | Repo 现状证据 | 优先级 | 建议 |
|---|---|---|---|---|
| 1 | **无 Agent Runtime 边界与工具契约** | 全仓无 registry/function-calling/agent loop(`r05`) | P0 | 定义 ExecutionContext + EnterpriseTool + Tool Registry + AgentLoop Adapter,从 extraction/answer_flow 模式生长 |
| 2 | **分层记忆模型缺失** | 会话=进程内 LRU,无 summary/长期记忆(`r03`) | P0 | 新建 request/session/long-term 三层 + StateStore/Memory Service 接口;不默认全向量化 |
| 3 | **durable checkpoint 无问答侧实现** | RDS 状态机仅摄取侧;无 agent_run/step/checkpoint 表(`r03`/`r06`) | P0 | 移植 SKIP LOCKED 认领模式建 agent_run/agent_step/tool_invocation/approval_request/execution_receipt 表,落 RDS 非 Redis |
| 4 | **钉钉 Stream 多副本无粘性/分发** | 无锁/租约/粘性,--workers 1 约定单活(`r04`) | P0 | 会话粘性(thread_id 一致性哈希路由)+ 去重迁 Redis + 消息分发方案;逻辑单入口≠物理单实例 |
| 5 | **Model Gateway/provider 抽象缺失** | 裸 HTTP 直调 DashScope(`r05`) | P0 | ModelProvider 接口 + 境内 provider 池路由 + 结构化输出后置校验兜底 |
| 6 | **Policy Engine 未收敛为统一决策点** | ACL 成熟但分散在 retriever/kb_authz;工具权限维度缺 | P1 | 封装现有授权入口为 deny-first 决策点,新增工具权限/风险/审批维度,不重建 ACL |
| 7 | **HITL 审批 dispatch 未覆盖审批动作** | 卡片回调框架齐备但仅反馈域(`r04`) | P1(P2实施) | 扩 dispatch 审批分支(三态 APPROVED/REJECTED/EDITED)+ 归属校验改 fail-closed + 审批走 Stream 通道 |
| 8 | **Text-to-SQL 语义层/只读守卫全缺** | 无任何 T2SQL 代码(`r05`) | P1 | 语义层+SELECT-only AST+表列白名单+部门 ACL 注入 WHERE;评估 XiYan 系列可拿走的组件 |
| 9 | **可观测/评测未覆盖 Agent 维度** | 无工具/审批 trace、无 run 级成本归集(`r07`) | P1 | 扩现有 trace/audit/eval:工具选择/参数正确性/权限正确性/HITL 正确性分离评测 |
| 10 | **prompt/tool 版本与配置治理缺失** | prompt 为手写常量,无版本(`r05`) | P2 | prompt version + rollout/rollback + 与 tool version 关联 + 运行记录版本重现 |

---

## 4. 报告 — Repo 冲突项(单列)

| 冲突 | PDF 说法 | Repo/源码事实 |
|---|---|---|
| HITL 检查点存储 | "检查点存 Redis" | Redis 0 接入;跨天审批 + LangGraph 生产 checkpointer 仅 Postgres → 必须 RDS |
| 记忆统一后端 | "会话历史+HITL 检查点统一落 Redis" | 违反分层:临时态可 Redis,durable/审批/audit 必须 RDS |
| Spring 三态名 | "MODIFIED" | 源码是 `EDITED`(传 MODIFIED 抛异常) |
| LangGraph 锁 | "threading.Lock 串行化" | async 类是 asyncio.Lock;现象对、机制错 |
| Qwen 并行工具 | "并行调用" | 执行层同步串行 for 循环,无并发原语 |
| ADK 编排 | Sequential/Parallel/Loop | 三者本版本全部 deprecated,指向 Workflow 图引擎 |
| primary_dept | 隐含"主部门" | 无此概念,一律 acl_groups 多组列表 |

---

## 5. 建议章节目录(v2 报告)

1. 执行摘要(证据重建的五点核心判断) 2. Repo 事实基线(引 architecture-map) 3. 目标架构边界与数据模型(A Runtime) 4. 分层记忆与 Durable State(B,P0 重点,含表结构) 5. 工具契约与 Registry(C) 6. Policy Engine(D) 7. Durable Workflow(E) 8. Model Gateway(F) 9. HITL 审批(卡片扩展 + 三态) 10. Text-to-SQL 语义层(K) 11. 可观测/评测(H) 12. 安全/部署/多副本(J/L) 13. 三张架构图 14. 分阶段路线(P0–P4) 15. 未决问题 + PoC 清单。
