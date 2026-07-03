# V2 变更日志（逐条对照 PDF 原结论）

> 变更类型:确认 / 推翻 / 重写 / 新增 / 删除 / 弱化。每条附富岭 Repo 证据 + 开源源码证据。
> 依据文件:`repo-architecture-map.md`、`report-gap-analysis.md`、`open-source-code-review.md`、`borrowing-matrix.md`。

## A. 被推翻 / 重写的原结论（重点）

| # | PDF 原结论 | V2 新结论 | 变更类型 | 变更原因 | 富岭 Repo 证据 | 开源源码证据 |
|---|---|---|---|---|---|---|
| P26 | 会话历史 + HITL 检查点**统一落 Redis** | 临时会话/缓存/锁落 Redis;**durable checkpoint/审批/audit 必须落 RDS** | **推翻** | 跨天审批 + Redis 易失 + 已有 RDS 事务/outbox 资产;违反分层原则 | Redis 0 接入(`session_store.py:6`/`r03`§6);outbox 事务模式成熟(`schema/009`) | LangGraph 生产 checkpointer 仅 Postgres(`checkpoint-postgres`),Redis 无官方实现 |
| P4(部分) | HITL 检查点**存 Redis**(thread_id=钉钉会话) | interrupt/resume 语义借 LangGraph,**检查点落 RDS**;钉钉卡片底座已存在仅需扩审批 dispatch | **重写** | 同上 + 卡片框架已备 | 卡片回调框架齐备(`dingtalk_bot.py:1012`,双通道) | LangGraph `interrupt()` 从节点入口重放(`types.py:811`) |
| P1(部分) | Qwen-Agent 支持**并行调用** | 模型单轮可发多 tool_call,但**执行层同步串行 for 循环,无并发原语** | **重写** | 源码验证执行层无并发 | ❌无 agent loop(`r05`) | `fncall_agent.py:94-105` 串行;`agents/` grep ThreadPool/gather 0 命中 |
| P8 | LangGraph AsyncPostgresSaver 用 **threading.Lock** 串行化 | ❌threading.Lock 只在同步类;async 类是 **asyncio.Lock**;但确有实例级串行化(连接池被架空) | **推翻(机制)/保留(现象)** | 源码机制说错,现象方向对 | — | `aio.py:43` asyncio.Lock;`aio.py:374` 单实例共享锁(对照 Store 已修) |
| P10 | Spring 三态 APPROVED/REJECTED/**MODIFIED** | 第三态是 **EDITED** 非 MODIFIED(传 MODIFIED 抛异常) | **重写** | 源码枚举名不符 | ❌无审批三态 | `InterruptionMetadata.java:285` `enum{APPROVED,REJECTED,EDITED}`;grep MODIFIED 0 命中 |
| P15(部分) | ADK **Sequential/Parallel/Loop** workflow agents | 三件套本版本**全部 @deprecated**,官方指向新 Workflow 图引擎;部署非强绑 GCP | **重写** | 源码已弃用 | 无 workflow 需求 | `sequential_agent.py:49` @deprecated;Database 后端可自托管 |
| AgentScope | 1.0 ReAct 范式 + asyncio.gather 分发 | 本 commit 是 **v2 重写版**,ReActAgent/MsgHub 删除,改为**可挂起状态机 + 工具级权限内核** | **重写** | PDF 描述 1.x,现版本 v2 | ❌无 agent loop | `_agent.py:94` 可挂起状态机;`tool/_base.py:94` ToolBase 超集 |
| P13(部分) | 审批与沙箱是**叠加**关系 | 仅 **docker-带宿主挂载**场景叠加;modal/daytona/singularity/纯 docker 是**替代**(隔离沙箱内 HARDLINE 都被短路) | **重写** | 源码分场景 | U8 写回须叠加(既要幂等又要人审) | Hermes `approval.py:1939-1950` 分场景短路 |
| P11 | OpenClaw CVE-2026-28466:execApprovals.set **在 SYSTEM_COMMANDS** | 攻击结构(审批写与工具调用同转发原语)真实、修复可验证;但**CVE 无 ID 落地、措辞与源码不符**(execApprovals 从不在 SYSTEM_COMMANDS,真实面是 node.invoke 透传) | **重写** | 源码措辞不符 | 尚无 Agent | `nodes.ts:1319` 转发前硬拦截;`node-command-policy.ts:55` SYSTEM_COMMANDS 无 execApprovals |
| P16 | OmO 任务类别→模型路由可原样映射到 Qwen 多档 | 借数据结构设计,但**类别由 LLM 声明非代码推断**,三处消费;**LICENSE(Sustainable Use)禁商用=硬否决,绝不引依赖** | **重写** | 裸调无 provider 抽象 | ❌裸 HTTP DashScope(`r05`) | `category-model-requirements.ts` 表;`LICENSE.md` Sustainable Use;PostHog 默认开 |
| P19 | XiYan-SQL 架构可借鉴 | xiyan-sql 是**论文仓 0 行代码**;M-Schema 可即取;MCP **无只读守卫**(安全关键);安全三件套四仓全为零须 100% 自建 | **重写** | 源码定性 | ❌无 T2SQL | xiyan-sql 仅 README+PDF;`db_source.py:73` engine.begin() 直执行无 is_select 校验 |
| P12 | Qwen-Agent 自带沙箱**不达生产** | 从"无沙箱"改为"有基础 Docker 沙箱但隔离不足"(无 network 隔离/端口 0.0.0.0/无资源限额/协作式超时可绕过) | **重写** | 源码当前 HEAD 已 Docker | 尚无沙箱需求 | `code_interpreter.py:257` Docker;`python_executor.py:98` 自认 not sandboxed |

## B. 被确认的原结论

| # | PDF 原结论 | 变更类型 | 证据 |
|---|---|---|---|
| P2 | 单 Agent + 工具优先,不建重型多 Agent | **确认** | 写型任务需单一连续上下文;Cognition/Gajae 固定角色论证 |
| P3 | 钉钉 Stream 反向连接零公网 | **确认(加注)** | 富岭已用 Stream(`dingtalk_stream_runner.py`);Hermes 单机需补多副本层 |
| P6 | 百炼/Coze/Dify 不能替代自建编排层 | **确认** | 与约束一致;百炼钉钉仅 HTTP |
| P7 | DashScope 仅 json_object 无 json_schema | **确认** | 已复核;Qwen-Agent 源码不冲突 |
| P14 | OpenAI SDK needsApproval+interruptions+粘性 | **确认** | `tool.py:426`/`run_state.py:332`;后端不用 |
| P17 | Gajae action_needed/reply 传输无关 | **确认(高可借鉴)** | `gjc-notifications/protocol.rs` token+幂等契约;唯一落地 loopback WS |
| P18 | Claw ~88 行/append-only harness | **确认(仅设计)** | `conversation.rs:325` ~208行 run_turn;仓库自称展品不可采用 |
| P20 | KIE DocMind 通用 KV / Qwen-VL schema | **确认** | 已复核;Repo 无 KIE(全新域) |
| P21 | 钉钉 Stream 多副本需自建粘性/分发 | **确认(强化)** | 无锁无租约无粘性(`r04`§5);Hermes D1-D9 |
| P22 | 底座现状 | **确认** | 全部 Repo 证实 |
| P23 | ACL 透传复用 retriever 不统迁 | **确认(强化)** | ACL 成熟 fail-closed(`r02`);无 primary_dept(是 acl_groups) |
| P24 | RAG 封装为首个 BaseTool | **确认** | 契约从 answer_flow/extraction 生长 |
| P37 | U8 写回经中间表 + 钉钉审批流 | **确认** | Repo 无 U8 接入(仅语料);复用 outbox 补偿 |
| P9 | LangGraph 官方 Qwen 路径/checkpointer 仅 Postgres | **确认** | 源码证实 |

## C. 弱化的原结论

| # | PDF 原结论 | V2 弱化 | 原因 |
|---|---|---|---|
| P1(主体) | Qwen-Agent 为"唯一零适配核心选型" | Qwen-Agent 是 AgentLoop **可替换实现之一**(经 Adapter);AgentScope v2 可挂起状态机是更贴企业 HITL 的备选 | 任务书改"provider 无关";长期能力须独立于框架(评审原则 7) |
| P5 | 吸取 OpenClaw 教训 | 确认两条设计规则,但补:tool policy 角色 deny 可被 allow 覆盖(不变量偏弱)、per-agent auth 隔离是运维边界不可照搬多租户 | 源码保留意见 |
| P25 | 251 金标集可复用 | 部分确认(eval_harness/tests/eval 存在 golden set);具体条数/judge/A-B 完整度需细查 | 未逐项核实 |

## D. 新增结论（PDF 未覆盖,V2 补齐）

| 新增 | 内容 | 证据 |
|---|---|---|
| 分层记忆数据模型 | request/session/**dept-scope 长期**/durable run/archive/knowledge 六层 + StateStore/Memory Service 接口 + 完整表结构 | ADK 作用域 + 富岭 work-queue + LangGraph checkpoint |
| durable 执行复用现有 | 移植摄取侧 SKIP LOCKED/2h stale/retry≤3 为 agent_run 执行语义,不引 Temporal/LangGraph 引擎 | `dataworks_orchestrator.py:181`(`r03`§4.3) |
| EnterpriseTool 从现有模式生长 | I/O 契约←extraction/schema + answer_flow;幂等←kb_console;补偿←outbox;审批←kb_access | `r05`§8 八条生长点 |
| ModelProvider 消除裸调 | 境内 provider 池抽象,业务不得直调任一 SDK | `llm_generator.py` 裸 HTTP |
| 无自动迁移执行器风险 | schema 靠 gitignored scratch 脚本人肉 apply,须补自动化 | `r06`§3 |
| 钉钉卡片 dispatch 需扩审批 + fail-closed | 归属校验改 fail-closed,审批走 Stream 通道 | `r04`§9 |
| M-Schema 唯一即取组件 | Text-to-SQL 四仓仅 M-Schema 可拿代码,安全三件套 100% 自建 | XiYan 审计 |

## E. 删除的原结论

| 删除 | 原因 |
|---|---|
| "检查点存 Redis(thread_id=钉钉会话)"作为落地方案 | 见 P26 推翻;改 RDS |
| OmO 作可移植选型 | LICENSE 硬否决;降为纯理念参考 |
| ADK Sequential/Parallel/Loop 作编排参照 | 已 deprecated;改指 Workflow 图引擎 |
