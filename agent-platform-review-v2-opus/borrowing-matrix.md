# 模块级借鉴矩阵（V2 · 证据底座）

> 七级采纳度:`理念参考` < `API 语义` < `数据模型` < `核心源码实现` < `适配器实现` < `完整依赖引入` < `不采用`。
> 每项双证据(缺一不可):**富岭 Repo 证据**(为何需要/不需要,附文件符号)+ **开源源码证据**(源码真实实现,附仓库/文件/符号)。
> `完整依赖引入`/`核心源码实现` 必须 ✅ 源码已验证 + 明确 Repo 缺口,并过供应链检查(LICENSE/遥测/维护/可锁版本)。

## 主矩阵

| 目标模块 | 来源库 | 采纳度 | 富岭 Repo 证据 | 开源源码证据 | 不采用的部分 | 最终决策理由 |
|---|---|---|---|---|---|---|
| **主循环 + 工具注册** | Qwen-Agent | **适配器实现** | ❌ 无 agent loop/registry/function-calling(`api.py:126` grep agent/tool 0 命中;`r05`) | ✅ `tools/base.py:24` TOOL_REGISTRY + `:44` register_tool;`fncall_agent.py:73` _run;可绕开全局 registry 以实例塞 function_list(`agent.py:212-217`) | 全局单例 TOOL_REGISTRY(多租户不友好)、异常吞噬语义、is_tool_schema 过严 | Qwen-only 约束下零适配命中 DashScope;业务工具经 EnterpriseTool→BaseTool Adapter 接入,不直接依赖。LICENSE Apache-2.0 ✅ 无遥测 ✅ |
| **主循环设计哲学(备选 AgentLoop)** | AgentScope v2 | **理念参考** | 同上缺口 | ✅ `agent/_agent.py:94` 可挂起状态机(tool call 五态)+ 全量 Pydantic AgentState + 工具协议自带并发/只读/权限元数据(`tool/_base.py:94`) | 多 Agent 强绑 app 层(FastAPI+Redis);2612 行单文件核心不可组合;2.0.4dev API 不稳定 | 其"可挂起状态机 + 工具级权限内核"是 EnterpriseTool/ExecutionContext 契约的最佳设计参照;但成熟度风险高,借设计不引依赖。Apache-2.0 ✅ |
| **HITL 中断/恢复语义** | LangGraph | **API 语义 + 数据模型** | ❌ 无 durable checkpoint 问答侧(摄取侧 RDS 状态机 `dataworks_orchestrator.py:181` 可移植) | ✅ `types.py:811` interrupt() 从节点入口重放;`checkpoint-postgres/base.py:47-76` 两表结构 + `WRITES_IDX_MAP` 负 idx;确定性 task_id(`_algo.py:834`) | 整套 pregel/channel 引擎(与富岭 RDS 范式不同);Redis checkpointer 无官方实现 | 抄 interrupt/resume 语义 + checkpoint 数据模型(两表+负idx特殊通道+确定性task_id),落 RDS 自研,不整库引入。MIT ✅ 核心库无遥测 ✅ |
| **HITL 工具级 API 形状** | OpenAI Agents SDK | **API 语义** | ❌ 无工具审批 API;钉钉卡片回调框架已备(`dingtalk_bot.py:1012`) | ✅ `tool.py:426` needs_approval(bool\|callable);`result.py:334` interruptions;`run_state.py:332` approve/reject 粘性(`run_context.py:29` bool=永久/list=一次性) | 后端本体(境外);tracing 默认上报 OpenAI(含敏感入出参) | needsApproval+interruptions+粘性批准 API 形状照搬为 HITL 装饰器语义;后端不用。MIT ✅ |
| **HITL 三态审批** | Spring AI Alibaba | **核心源码实现(算法移植)** | ❌ 无审批三态;卡片回调 dispatch 仅反馈域(`dingtalk_bot.py:1109` 未识别 action 丢弃) | ✅ `HumanInTheLoopHook.java:67-145` 三态:EDITED 改参重建 ToolCall 保 id/name;REJECTED 预插拒绝响应+执行器 id 去重跳过 | Java 栈;审批策略仅按工具名;缺反馈默认放行(须改 fail-closed);feedback 不持久化 | EDITED/REJECTED 的消息协议操作可直接移植到 Python OpenAI/Anthropic 风格协议;⚠️**第三态是 EDITED 非 MODIFIED**(PDF 错) |
| **HITL 协议传输无关** | Gajae-Code | **API 语义(理念参考)** | 钉钉互动卡片双通道已备(`dingtalk_bot.py:923`/`dingtalk_stream_runner.py:118`) | ✅ `gjc-notifications/protocol.rs` ActionNeeded/Reply 传输无关 JSON 契约 + token/幂等/6 拒因/3 answer 形 | ⚠️唯一落地传输是 loopback WS(非可插拔 transport);Rust | action_needed/reply 契约(token+幂等键+终态帧)用钉钉互动卡片重实现,Python 重写。MIT ✅ |
| **harness 极简哲学** | Claw Code | **理念参考** | 会话状态进程内 LRU,无从历史重建 | ✅ `conversation.rs:325` ~208 行纯函数 run_turn + append-only 全历史重建请求 | ⚠️仓库自称博物馆展品;`main.rs` 19831 行巨石 | append-only + 全历史重建请求是干净 harness 模式,借设计勿采仓库。MIT ✅ |
| **交互网关形态** | Hermes | **核心源码实现(接口/算法层)** | ✅ 钉钉双通道已实现但单进程假设(`dingtalk_bot.py:130` "多副本需迁 Redis") | ✅ `plugins/platforms/dingtalk/adapter.py` Stream 全套;`gateway/session.py:822` session_key 确定性设计;审批 fail-closed + HARDLINE 清单 | ⚠️ D1–D9 单机假设(agent 缓存/审批队列/webhook/去重/锁全进程内);SQLite 文件库 | 接口/算法层高保真迁移(适配器 ABC + session_key + 审批 fail-closed + 五阶段压缩);状态层必须整体外置化(SAE 多副本)。MIT ✅ |
| **钉钉零公网接入** | 钉钉 Stream + Hermes DingTalk adapter | **核心源码实现** | ✅ `dingtalk_stream_runner.py` 已用 Stream WSS;但无守护重启/无多副本粘性 | ✅ Hermes `adapter.py` 指数退避重连[2,5,10,30,60]、session_webhook 缓存+过期边际、AI Card finalize、结构化提及门 | Hermes 单机文件锁(status.py:933) | 复用富岭现有 Stream 接入 + 补 Hermes 的工程细节(重连/webhook 过期/finalize) |
| **模型多档路由** | OmO | **理念参考(强制不引依赖)** | ❌ 裸 HTTP 直调 DashScope(`llm_generator.py`),无 provider 抽象 | ✅ `category-model-requirements.ts` 类别→fallbackChain 表;`FallbackEntry{providers[], model, variant}`;中文额度/限流正则内建 | ⚠️**LICENSE Sustainable Use License 禁商用(硬否决)**;PostHog 遥测;类别由 LLM 声明非推断;两套 resolver | 借"任务类别→模型 profile + fallback chain"数据结构设计,自研 ModelProvider 接口;**LICENSE 硬否决,绝不引依赖/抄源码**。境内 provider 池(DashScope 多档 + 自托管) |
| **上下文压缩** | Hermes / Claude Code / AgentScope | **核心源码实现** | ❌ 会话截断=丢弃最旧非摘要(`session_store.py:138`;`r03`) | ✅ Hermes `context_compressor.py:2670` 五阶段(裁剪→头保护→token预算尾保护→LLM摘要→迭代更新)+ 反抖动 + 静态兜底 | Hermes 压缩即会话分裂(可选) | 五阶段压缩算法移植,防注入摘要前缀;auto-compact 兜底。MIT ✅ |
| **state 多级作用域** | Google ADK | **数据模型** | ❌ 无 state 作用域;身份是 acl_groups 多组(无 primary_dept) | ✅ `sessions/state.py:64` user:/app:/temp: 前缀;`base_session_service.py:187` temp 统一剪除;Database 后端三表 | ⚠️ 无 dept 层(硬编码三前缀);user/app 合并 dict\| 无删除语义;Sequential/Parallel/Loop 已 deprecated | 抄四级作用域数据模型 + **temp 剪除机制(最值得直接照抄)**;新增 dept: 层仿 user: 三处落点(路由/表/读合并);dept_id 由富岭 ACL 注入。Apache-2.0 ✅ |
| **全局横切门控** | Google ADK | **理念参考** | ✅ 已有 request_context/audit_log/rate_limiter 横切件 | ✅ `plugins/base_plugin.py` 13 钩子 Runner 级全局注册,先于 agent callback | ⚠️首个非None即短路+异常即RuntimeError全链中断(不如中间件洋葱可组合) | 借"Plugin 全局横切一次注册全局生效"理念;实现用富岭现有中间件模式(更可组合) |
| **会话持久化后端** | OpenAI SDK / AgentScope | **数据模型** | ❌ Redis 0 接入;会话进程内 LRU | ✅ OpenAI `sqlite_session.py` 两表 sessions/messages;AgentScope 写锁内 upsert(`_chat.py:441`) | 各自绑定其框架类型 | 会话历史/临时态可 Redis,但 **durable checkpoint/审批/audit 落 RDS**(推翻 PDF Redis 统一) |
| **安全反面教材** | OpenClaw | **理念参考 + 核心源码实现(校验逻辑)** | ✅ 已有 env_guard 写守卫/GuardedDBConnection(`db.py:52`);但审批与工具信道未分离(尚无 Agent) | ✅ `nodes.ts:1319` 审批命令转发前硬拦截 + scope 分级;`validate-sandbox-security.ts` 双向覆盖+symlink 规范化 | per-agent auth 隔离是运维边界(多租户不可照搬);tool policy 角色 deny 可被 allow 覆盖 | 审批信道带外隔离(method+scope+转发前拦截)理念参考;沙箱双向覆盖+symlink 校验逻辑可移植。MIT ✅ |
| **durable 执行状态机** | 富岭自有 + LangGraph | **复用现有 + 数据模型** | ✅ RDS 行级状态机(SKIP LOCKED 认领/2h stale/retry≤3/drain)现成(`dataworks_orchestrator.py:181-223`) | ✅ LangGraph checkpoint 数据模型作补充 | LangGraph 整引擎;Temporal(过度) | **主体复用富岭现有摄取侧 work-queue 模式**移植为 agent_run/step 表,checkpoint 语义借 LangGraph 数据模型 |
| **Text-to-SQL** | XiYan(M-Schema) + QwenCoder | **核心源码实现(仅 M-Schema)+ 完整依赖(仅权重)** | ❌ 无任何 T2SQL(`r05`);但 ACL 注入模式成熟(`retriever.py:399` 可复用为 WHERE 注入) | ✅ M-Schema `schema_engine.py:10` 可即取;⚠️MCP `db_source.py:73` **无只读守卫** | ⚠️xiyan-sql 框架 0 代码;MCP 无 ACL/只读;QwenCoder 权重 license 须单独确认 | 拿 M-Schema 生成器(补脱敏)+ QwenCoder 权重;**只读守卫/ACL 注入/语义层 100% 自建**(复用富岭 ACL 模式) |
| **Agent 平台整体替代** | 百炼/Coze/Dify | **不采用** | ✅ 钉钉 Stream 唯一入口 + 原生 HITL + ACL 透传 + 数据留境内约束 | 百炼钉钉通道仅 HTTP、Stream 无法返回消息(PDF 已复核) | 全部 | 不承载主链路;局部可作试点(P6) |
| **EnterpriseTool I/O 契约** | 富岭自有 | **复用现有** | ✅ `extraction/schema.py` ExtractionResult + `answer_flow.build_qa_log_kwargs`(全字段单点+状态词表 SUCCESS/REFUSAL/NO_RESULT/ERROR) | — | — | 契约从现有成熟模式生长,不外部引入 |
| **幂等/补偿/审批钩子** | 富岭自有 | **复用现有** | ✅ 固定键幂等(`kb_console.py:1274`)+ outbox 同事务(`schema/009`+`access_grants.py:203`)+ 审批状态机(`kb_access.py:679`) | — | — | 三套成熟模式收编为 tool 级 policy,写外部系统(U8)复用 outbox 语义 |

## 供应链检查(建议 `核心源码实现`/`完整依赖引入` 的仓库)

| 仓库 | LICENSE | 商用兼容 | 遥测 | 版本可锁 | 结论 |
|---|---|---|---|---|---|
| Qwen-Agent | Apache-2.0 | ✅ | ❌ 无 | ✅ | 可适配器引入 |
| LangGraph | MIT | ✅ | 核心库无(CLI 可关) | ✅ | 数据模型/语义借鉴,不整库引入 |
| Hermes | MIT | ✅ | ❌ 无(仅版本检查) | ✅ | 接口/算法层核心源码移植 |
| Spring AI Alibaba | Apache 系(Java) | ✅ | Micrometer(需引 starter) | ✅ | 算法移植(Python 重写,不引 Java 依赖) |
| Google ADK | Apache-2.0 | ✅ | OTel env-gated 默认 no-op | ✅ | 数据模型借鉴 |
| OpenAI Agents SDK | MIT | ✅ | ⚠️ tracing 默认上报,须 `OPENAI_AGENTS_DISABLE_TRACING=1` | ✅ | API 语义借鉴,后端不用 |
| AgentScope v2 | Apache-2.0 | ✅ | ❌ 无(默认 no-op) | ⚠️ 2.0.4dev 不稳定 | 理念参考,不引依赖 |
| OpenClaw | MIT | ✅ | 待补审确认 | ✅ | 校验逻辑移植 |
| M-Schema | Apache-2.0 | ✅ | ❌ 无 | ✅ | 核心源码引入(补脱敏) |
| XiYanSQL-QwenCoder | ⚠️ 仓无 LICENSE,权重 license 须 HF/ModelScope 确认 | 待确认 | — | ✅(权重版本) | 权重引入前须核 license |
| **OmO** | ⚠️ **Sustainable Use License(禁商用)** | ❌ | PostHog 默认开 | — | **硬否决:仅理念参考,绝不引依赖/抄源码** |

## 供应链补充（补审仓库）

| 仓库 | LICENSE | 商用兼容 | 遥测 | 结论 |
|---|---|---|---|---|
| OpenClaw | MIT | ✅ | 未见第一方遥测 | 校验逻辑移植 ✅ |
| M-Schema / XiYan-MCP | Apache-2.0 | ✅ | ❌ 无 | M-Schema 引入;MCP 仅参考 |
| Gajae-Code | MIT | ✅ | 未见 | protocol 契约理念参考 ✅ |
| Claw Code | MIT | ✅ | 未见 | 借设计勿采仓库(自称展品) |

## 结论

- 唯一 `硬否决(不采用/绝不引依赖)`:**OmO**(Sustainable Use License 禁商用)、**百炼/Coze/Dify** 整体替代(约束冲突)。
- `完整依赖引入` 仅一处:**XiYanSQL-QwenCoder 权重**(且须先核 HF/ModelScope 权重 license)。
- 其余全部收敛为 `理念参考 / API 语义 / 数据模型 / 核心源码实现(算法移植,Python 重写)/ 适配器实现`——**无一处需整库引入第三方 Agent 框架作运行时宿主**,符合"长期架构能力独立于具体 Agent 框架"原则。
- **富岭自有资产(ACL / 幂等 / outbox 补偿 / 审批状态机 / RDS work-queue / 审计)是最高复用优先级**,多处判定为"复用现有"而非外部借鉴。
