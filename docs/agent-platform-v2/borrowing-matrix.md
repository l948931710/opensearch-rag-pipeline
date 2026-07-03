# 模块级借鉴矩阵（borrowing-matrix.md）

> V2 采纳决策的证据底座。七级采纳度：理念参考 / API 语义 / 数据模型 / 核心源码实现 / 适配器实现 / 完整依赖引入 / 不采用。
> **双证据纪律**：每项决策同时给出①富岭 Repo 证据（为什么需要/不需要，附文件·符号）②开源源码证据（真实实现，附仓库@commit·文件·符号）。`核心源码实现`/`完整依赖引入` 必须 ✅ 源码已验证 + 明确 Repo 缺口，并过供应链检查（§末）。
> Repo 基线 @7c704ce；开源 commit 见 open-source-code-review.md 总表。

## 图例与总原则

经 15 仓源码审查，**没有任何一家框架进入"完整依赖引入"**。全部采纳落在 数据模型 / API 语义 / 核心源码实现（局部摘抄改写）/ 适配器实现 四档——这由证据决定（各家硬伤见 review §12），也与硬约束"长期能力独立于具体 Agent 框架"一致。

---

## A. AgentLoop 与 Runtime 边界

| 目标模块 | 来源库 | 采纳度 | 富岭 Repo 证据 | 开源源码证据 | 不采用的部分 | 最终决策理由 |
|---|---|---|---|---|---|---|
| AgentLoop（自研主循环） | Qwen-Agent | **理念参考 + 核心源码实现（骨架参照）** | ❌ Repo 无任何循环/编排（api.py 固定 RAG 链路；tools 字段全库零命中）——从零建，需最小可靠骨架 | ✅ `fncall_agent.py:73-108` _run：~35 行 while 循环，_detect_tool/_call_tool/function_map 三个清晰 override 点；`agent.py:193-203` 工具异常转文本回灌 | 其循环无 HITL 挂点（检测即执行）、无事件流、无挂起态——不能直接用 | 循环本体简单（<500 行自研），难点在挂起/恢复与 durable 记录，Qwen-Agent 恰好没有这两样 |
| AgentLoop（挂起/恢复语义） | AgentScope 2.0 | **核心源码实现（参照改写）** | 同上；且钉钉审批回调天然异步，循环必须可挂起 | ✅ `agent/_agent.py:575-732` _reply_impl：事件流循环遇 RequireUserConfirmEvent 即 return 挂起、下次 reply 带确认续跑；`:1339-1590` 工具调用状态机（ASKING/ALLOWED/SUBMITTED/FINISHED） | 不引其依赖（2.0.4dev + anthropic/dashscope/openai 硬捆绑）；app 层 Redis 全家桶 | "事件流 + 可挂起状态机"是四家中唯一与钉钉异步审批同构的循环形态 |
| AgentLoop Adapter 接口 | （自研，语义对齐三家） | API 语义 | Repo 无框架依赖=零迁移包袱，正好把 Adapter 定成第一层接口 | ✅ Qwen-Agent `agent.py:212-237`（BaseTool 实例直接注入，绕过全局注册表）证明适配缝极小；AgentScope/OpenAI SDK loop 形态各异但都可折叠为 run(messages, tools, ctx)→events | — | 保证换 Loop 不动业务：工具/权限/记忆/审批/审计全部不进 Loop |
| 子 Agent（当前不建） | Claude Code 理念（PDF 转述） | 不采用（现阶段） | Repo 首批场景（RAG/只读 SQL/KIE/测算）全部单上下文可覆盖，无并行/隔离需求证据 | —（未读其源码，PDF 二手） | 全部 | 评审原则 2：无三条件之一不引入多 Agent |

## B. Memory / State / Durable（P0 接缝）

| 目标模块 | 来源库 | 采纳度 | 富岭 Repo 证据 | 开源源码证据 | 不采用的部分 | 最终决策理由 |
|---|---|---|---|---|---|---|
| durable checkpoint/run 表结构 | LangGraph | **数据模型** | ❌ agent_run/agent_step/tool_invocation 等表全仓零命中（db.json）；摄取侧 pipeline_run 仅批处理粒度 | ✅ `libs/checkpoint-postgres/.../base.py:43-91` 三表 MIGRATIONS：checkpoints(PK thread_id+ns+id)/checkpoint_blobs(按 channel,version 去重)/checkpoint_writes(负索引 INTERRUPT/RESUME 特殊写)；`base/__init__.py:92-123` Checkpoint TypedDict | 不引 Pregel 运行时（1300+ 行、langchain_core 耦合）；不用其 AsyncPostgresSaver（实例级 asyncio.Lock 串行化，`aio.py:43,363-403`） | 表结构直接翻译为 RDS MySQL DDL（Repo 已有 schema/ 迁移惯例与四账号体系可复用） |
| interrupt→resume 协议 | LangGraph | **API 语义 + 核心源码实现（协议复刻）** | ❌ 对话链路无任何 checkpoint（`ingestion_resume.py:1-17` 明言只服务摄取）；HITL 跨天审批无处挂起 | ✅ `types.py:811-934` interrupt 按调用序号取 resume；`_runner.py:574-613` 中断只落特殊写、常规写丢弃→节点重跑；`_io.py:74`+`_algo.py:1280-1345` resume 值持久化后重放注入（崩溃后可重复 resume） | 节点整体重跑语义带来的副作用重复——须以"外部写在审批后+幂等键"消化（V2 铁律 6） | 协议闭合且抗崩溃；富岭以"工具为粒度"而非"图节点"复刻，更简单 |
| 审批挂起-恢复状态机 | OpenAI Agents SDK | **API 语义 + 数据模型** | ❌ Repo 审批仅 KB 权限域（`kb_access_request`），无工具执行审批 | ✅ `tool.py:1300` needs_approval(bool|回调)；`result.py:367` interruptions；`run_state.py:323-379` approve/reject(always_*) 三态粘性 + 批准记录随 RunState JSON 序列化（含 schema 版本）；`run.py:453-508` RunState 作 input 恢复 | 不引其依赖（默认 tracing 上传 api.openai.com 且敏感数据缺省 true，`tracing/processors.py:34`+`run_config.py:41-44`）；needs_approval 默认 False 的 opt-in 语义（富岭须 Registry 层强制） | "先解析参数→按参数定审批→挂为可序列化 item→粘性批准"语义链最完整 |
| 审批三态与改参放行 | Spring AI Alibaba | **API 语义 +（EDITED 机制）核心源码语义** | ❌ Repo 审批只有 approve/reject 两态（`kb_access.py:679`），无改参概念；钉钉卡片需三态 | ✅ `InterruptionMetadata.java:285-289` enum{APPROVED,REJECTED,**EDITED**}；`HumanInTheLoopHook.java:102-105` EDITED=人工改写参数重建 ToolCall 后放行；`:106-110` REJECTED=理由回喂模型 | ⚠️ 漏答反馈按 APPROVED 放行（`:112-115`）——**必须反向为 fail-closed**；HumanInteractionHandler 半成品；Java 栈不引入 | EDITED 重建 tool_call 是改参放行最干净实现；富岭补"REJECTED-硬终止"第四处置 |
| 审批线协议（钉钉卡片桥接） | Gajae-Code | **API 语义 + 数据模型 + 核心源码实现（ActionRegistry 移植）** | ❌ Repo 卡片回调是赞踩反馈（`dingtalk_bot.py:1012`），无审批路由/幂等/迟到处理 | ✅ `gjc-notifications/src/protocol.rs` ActionNeeded/Reply{**idempotencyKey**,token}/ActionResolved{resolvedBy}/ReplyRejected{already_answered\|idempotency_conflict\|unauthorized}+版本协商；`actions.rs` ActionRegistry：first-valid-reply-wins、幂等重试 DuplicateAccepted、迟到拒绝、重连重放（纯内存 <200 行） | Telegram 客户端与 loopback WS 传输层；kind 枚举硬编码 | 各家中唯一把"挂起审批→IM→回注"做成传输无关合约的；钉钉 adapter=render/mapInbound 两方法 |
| Session Memory 接口 | OpenAI Agents SDK | API 语义 | ⚠️ `session_store.py:44` 进程内 LRU（注释自认"生产可替换为 Redis"）——迁移点唯一且清晰 | ✅ `memory/session.py:14` 四方法 Protocol（get_items/add_items/pop_item/clear_session）；`extensions/memory/redis_session.py` RedisSession（TTL/key_prefix/注入客户端） | 不引依赖；RedisSession 代码可对照自写 | 极简协议够用；Repo 的 session_key 语义（会话×人）保留 |
| 长期记忆分层 schema（P2 实现，P0 定接口） | Google ADK | **数据模型 + 核心源码实现（拆桶/合并 30 行移植）** | ❌ 用户级长期记忆零命中（session.json）；多部门场景天然需要 user/dept/app 三层 | ✅ `sessions/state.py:64-66` 三前缀；`_session_util.py:37-50` extract_state_delta 拆桶（temp: 丢弃）+`database_session_service.py:176-187` _merge_state；`schemas/v1.py` app_states(PK app)/user_states(PK app+user)/sessions 三表主键强制隔离；并发三层防护（进程锁+行锁+乐观 marker） | 不引 ADK 依赖（自带 Runner 等于换架构；workflow 引擎 experimental）；Vertex 系 memory | 富岭映射：user:/dept:/app: 三 scope（dept 是富岭特有，加一桶即可）；**不默认向量化**，结构化存 RDS |
| Memory 与 Session 边界 | Google ADK | 理念参考 + API 语义 | Repo 已有"审计流水(qa_session_log) vs 上下文(session_store)"双轨，恰与该边界同构 | ✅ `memory/base_memory_service.py` 三方法（add_session_to_memory/add_events_to_memory/search_memory）；Runner 从不自动写 memory（写入必须显式） | Vertex 实现 | "session=事实流水、memory=蒸馏知识、写入显式"直接约束长期记忆治理（写入条件/确认） |
| 上下文压缩 | Hermes + Qwen-Agent | **核心源码实现（两处摘抄改写）** | ❌ 无任何摘要/压缩（全仓无 summarize/compaction），10 轮硬截断 | ✅ Hermes `agent/context_compressor.py:662+`：50% 阈值/保头护尾/廉价辅模型结构化摘要/**"respond to the message below, not the summary above" 边界分隔符防摘要注入**（踩坑修复）；Qwen-Agent `llm/base.py:602-804` 分级截断（先压 FUNCTION 结果→删中间步→截首尾） | Hermes 的 SQLite 血缘轮换（富岭用 RDS 表达）；整仓引入 | P1 实装 rolling summary；两段源码均自包含可摘 |
| 会话血缘 | Hermes | 数据模型 | qa_conversation 已有（PK user+conv_id），Agent run 需父子关系（压缩轮换/子任务） | ✅ `hermes_state.py:695` sessions.parent_session_id 自引用 FK + end_reason 边类型 + `:2521` 递归 CTE 双向血缘 | SQLite 本体 | agent_run.parent_run_id + reason 一列表达，低成本 |

## C. Tool Contract / Registry

| 目标模块 | 来源库 | 采纳度 | 富岭 Repo 证据 | 开源源码证据 | 不采用的部分 | 最终决策理由 |
|---|---|---|---|---|---|---|
| EnterpriseTool 契约字段 | AgentScope 2.0 | **API 语义 + 数据模型** | ❌ 工具抽象零命中；现有类工具组件三种形态混用（函数/类/生成器，tools.json）需统一 schema | ✅ `tool/_base.py` ToolBase：input_schema + **is_read_only / is_concurrency_safe** / is_external_tool / is_state_injected 元数据；`tool/_toolkit.py:225-388` 统一流式契约（ToolChunk/ToolResponse、异常回灌、取消=INTERRUPTED 终态） | 不引依赖；schema 自动生成的 docstring 魔法（企业侧显式声明更可审计） | 元数据驱动调度与权限（is_read_only 直接映射风险等级）是四家最强契约；富岭再加 risk_level/permission_scope/idempotency/approval_policy/data_classification/owner/deprecation 企业字段 |
| Tool Registry | Qwen-Agent | **核心源码实现（~60 行改写）** | ❌ 无注册表；但 Repo 有"白名单单一来源"惯例（`kb_authz.py:59-67` 写白名单=retriever._VALID_ACL_GROUPS）可延续 | ✅ `tools/base.py:24,44-59` TOOL_REGISTRY + register_tool（重名抛错/显式覆盖）；`agent.py:212-237` **实例注入优先、注册表兜底双通道** | 进程级全局 dict 的多租户问题（改实例级 Registry + DB 元数据表） | 模式简单成熟；企业版注册表落 DB（版本/风险/owner），进程内只是缓存 |
| EnterpriseTool→Qwen-Agent BaseTool Adapter | Qwen-Agent | **适配器实现（可选，非 P0）** | Repo 未用 Qwen-Agent；若未来选它作某场景 Loop，业务工具不得依赖 BaseTool（硬约束） | ✅ `tools/base.py:109-190` BaseTool=3 属性 1 方法，`is_tool_schema` jsonschema 校验——Adapter 是一个 <100 行的包装类 | 直接继承 BaseTool（协议反向依赖） | 接缝已探明成本极低，保留选项即可 |
| MCP 工具归一化（当前不建） | Qwen-Agent | 理念参考 | ❌ Repo 无 MCP 且无消费方需求 | ✅ `tools/mcp_manager.py:31-310` MCP 工具动态生成 BaseTool 子类（stdio/sse/streamable-http、断线重连、atexit 清理） | 单例全局事件循环线程（高并发瓶颈） | "外部协议→内部工具契约 Adapter"的范式记录在案，待有 MCP 需求再启用 |
| 声明式工具白名单分权 | Gajae-Code | 核心源码语义 | Repo 角色（employee/dept_admin/kb_admin）只管 KB 域；Agent 需"角色→可用工具集"声明 | ✅ `prompts/agents/*.md` frontmatter tools 白名单（读角色物理无写工具）+ `tools/bash.ts:535-575` 运行时硬 enforcement（堵 cd/env 绕过） | 提示词层约定部分 | "分权靠装配时白名单而非提示词"——Tool Registry 按角色/场景过滤的实现原则 |

## D. Policy Engine / 安全

| 目标模块 | 来源库 | 采纳度 | 富岭 Repo 证据 | 开源源码证据 | 不采用的部分 | 最终决策理由 |
|---|---|---|---|---|---|---|
| Policy 挂点布局 | Google ADK | **核心源码实现（参照，~300 行 PluginManager 可移植）** | ⚠️ Repo 守卫逐端点手写 20+ 处（authz.json），需统一挂点 | ✅ `plugins/plugin_manager.py:42-55,275-322` 12 回调点、按序执行、首个非 None early-exit、异常 fail-closed；`functions.py:569` before_tool 返回 dict 即替代执行（拦截）；`base_agent.py:473-506` plugin 优先于 agent callbacks | ADK 本体 | before_tool/after_tool/before_model 三挂点起步；fail-closed 语义与 Repo 纪律一致 |
| 策略管道形态（只减不增） | OpenClaw | 核心源码实现（参照） | Repo ACL 是"白名单归一+fail-closed"单层；工具策略需 全局→部门→角色→会话 分层 | ✅ `tool-policy-pipeline.ts:127-215` 层化单调递减过滤（任何层只能减不能加回）+ 逐层 audit label；`agent-tools.policy.ts:50-74` 子代理硬拒绝清单 | 其 RPC 全栈 | "只减不增+逐层留痕"是策略可审计性的关键形态 |
| 策略键服务端背书 | OpenClaw | 理念参考 | ✅ Repo 已同构：acl_groups 服务端生成、请求体 dept 废弃（`api.py:204-228`）、落库 uid 不信请求体 | ✅ `agent-tools.policy.ts:266-308` groupId 必须被服务端派生上下文背书否则置 null | — | 互证：V2 评审原则 4（模型不得提供身份/ACL）在两侧都有实现先例 |
| 审批管理面带外隔离 | OpenClaw | **API 语义 + 理念参考** | ❌ Repo 无工具审批面；新建时必须一步到位 | ✅ CVE-2026-28466 修复形态：`nodes.ts:1319-1330` 工具信道无条件早退 + `exec-approvals.ts:128-196` 专用方法族 + `core-descriptors.ts:54-57` operator.admin scope + `exec-approvals.ts:29-74` baseHash CAS 防 TOCTOU + 回归测试 | 单文件本地持久化 | 铁律落地：审批策略读写走独立管理 API+管理员角色，绝不注册为工具 |
| 钉钉回调加固 | OpenClaw | **核心源码实现（参照改写）** | ⚠️ `dingtalk_bot.py` 卡片回调不验 apiSecret（注释自认）、归属校验 DB 异常 fail-open（`:989-991`） | ✅ `hooks-request-handler.ts` 五件套：header-only token、常时比较（safeEqualSecret）、认证失败限流、目标白名单、token+scope+idempotencyKey 幂等指纹 | — | 审批卡片回调是高风险入口，五件套逐条补齐 |
| 灾难操作硬阻断 | Hermes | **核心源码实现（清单+去混淆管线摘抄）** | ❌ Repo 无工具执行故无此层；U8 写回/SQL 工具上线即需要 | ✅ `tools/approval.py:365` HARDLINE 12 条正则（高于一切模式）+ `:1160-1337` shell 去混淆（引号剥离/命令替换展开/起始锚定）；`:213-218` 安全配置文件自保护（防 agent 自改 approvals.mode） | ⚠️ **fail-open 分支必须封死**（`:2003-2022` 非交互上下文 AUTO-APPROVED）；容器后端跳过审批的替代语义（富岭=叠加） | SQL 版 HARDLINE（DROP/TRUNCATE/DELETE 无 WHERE 等）+ 配置自保护直接适用 |
| 审批 fail-closed 语义 | Hermes | API 语义 | Repo 限流已有 fail-CLOSED 先例（ask 成本路径 503，`api.py:389-421`） | ✅ `approval.py:1693-1697,2484-2513` 超时即拒 + "Silence is not consent" + 禁止换皮重试同类命令 | — | 超时=拒绝 + 拒绝后禁改写重试，写进 Approval Engine 规格 |
| 沙箱校验（延后，随代码执行工具启用） | OpenClaw | 核心源码实现（届时摘抄） | ❌ 首批工具无代码执行需求——沙箱整层延后 | ✅ `validate-sandbox-security.ts`（435 行自包含纯函数）：BLOCKED_HOST_PATHS+HOME 凭据子路径+双向祖先检查+realpath 复检+network/seccomp/apparmor 校验，`docker.ts:413` 创建前强制调用 | 现阶段全部 | 记录在案；Qwen-Agent 沙箱已证不达生产（`code_interpreter.py` 端口 0.0.0.0、无资源限制），届时不用它 |
| Guardrails 并行护栏 | OpenAI Agents SDK | 核心源码实现（参照） | Repo 已有输出侧软护栏（低置信 prompt 注入，`llm_generator.py:92`），无输入/工具级护栏 | ✅ `run.py:1194-1247` 护栏与模型调用 asyncio.gather 并行、tripwire cancel；`guardrail.py:100` run_in_parallel=False 可改前置强拦截；工具级三处置 allow/reject_content/raise | 不引依赖 | 高风险工具用前置串行强拦截，低风险用并行省时延——二档语义照抄 |

## E/F. Model Gateway / 路由

| 目标模块 | 来源库 | 采纳度 | 富岭 Repo 证据 | 开源源码证据 | 不采用的部分 | 最终决策理由 |
|---|---|---|---|---|---|---|
| ModelProvider 接口 | OpenAI Agents SDK + Qwen-Agent | **API 语义** | ❌ 4+ 处手写 HTTP（`llm_generator.py:698`/`query_decomposer.py:97`/`spot_checker.py:114`/`pipeline_nodes.py:1158`）；dashscope SDK 死依赖；Gemini 残留（`config.py:244,664`） | ✅ OpenAI SDK `models/interface.py:37,127` Model 两方法+ModelProvider.get_model+MultiProvider 前缀路由；Qwen-Agent `llm/base.py:61-533` 基类管缓存/截断/退避/停止词、叶子类只管 HTTP 的分层 | 两家依赖本体 | 接口=get_response/stream_response 两方法；富岭叶子类首个实现即 DashScope compatible-mode（现有 http_session/vlm_endpoint 素材收敛） |
| provider 能力矩阵 | Claw Code | API 语义 | 多 provider 路由需知道谁支持 tool_calls/结构化输出/思维链 | ✅ `rust/crates/api/src/providers/mod.rs` ProviderCapabilityReport 逐能力枚举 Supported/Unsupported/PassthroughAsTool | Rust 本体 | 能力矩阵进 provider 配置表，路由与降级决策据此 |
| task category→模型档路由 | oh-my-openagent | **数据模型 + 理念参考（⚠️ 禁源码搬运）** | ❌ 模型名散在 env（LLM_MODEL 默认 qwen3.6-plus），无按任务分档 | ✅ `model-core/src/{category-model-requirements,model-requirement-types}.ts` fallbackChain 双层结构（模型偏好序×provider 优先序×每级推理参数）；`delegate-core/src/model-selection.ts` 五级解析优先级；"解析期选链+运行期沿链重试"闭环 | **License：Sustainable Use License 非 OSI 禁商业分发——只借数据结构自写实现**；默认 PostHog 遥测；海外模型链 | 类别→境内模型链（DashScope Max/Plus/Turbo + 可配自托管/其它境内 provider）；数据结构照其形自写 |
| 提示词模拟 function-calling 兜底 | Qwen-Agent | 核心源码实现（可选摘抄） | 境内 provider 池中可能有不支持原生 tool_calls 的模型/自托管端点 | ✅ `llm/function_calling.py:23-136` + `fncall_prompts/nous_fncall_prompt.py`：functions 注入 system + `<tool_call>` 文本解析还原 | 默认模板的 Qwen 偏置（换模型需实测） | 作为 capability=Unsupported 时的降级通道，非主路径 |
| 结构化输出兜底 | （自研；PDF 结论沿用 🟡） | 理念参考 | Repo 已有先例：query_decomposer temperature=0 严格 JSON+失败回退（`query_decomposer.py:112`） | —（DashScope json_object 限制为文档级结论，未重验） | — | prompt 约束+pydantic 后置校验+重试，落地前复核当期 API |

## K. Text-to-SQL / 语义层

| 目标模块 | 来源库 | 采纳度 | 富岭 Repo 证据 | 开源源码证据 | 不采用的部分 | 最终决策理由 |
|---|---|---|---|---|---|---|
| Schema 表示（M-Schema） | M-Schema | **核心源码实现（vendoring ~330 行 + 脱敏改造）** | ❌ 无 Text-to-SQL 代码；RDS 双库+四账号体系是现成地基（`prod_access.py:79` 只读会话强制） | ✅ `schema_engine.py`（SQLAlchemy Engine→反射，mysql 特判）+ `m_schema.py`（to_mschema 文本/表列裁剪/save-load） | ⚠️ 无 __init__.py 不能规范 pip 引入（只能 vendoring）；`fectch_distinct_values` 抽 5 个真实列值仅滤 email/URL——**必须加列级脱敏开关** | 表示法成熟且 QwenCoder 系模型按此格式训练；vendoring 后归自有包管理 |
| NL2SQL 工具契约 | xiyan_mcp_server | **API 语义（仅契约）；组件本体不采用** | ❌ 同上 | ✅ `server.py` get_data(query)→SQL→执行→sql_fix 重试 3 次→markdown ≤100 行 | **本体四项否决**：`db_source.py` engine.begin() 任意 SQL 自动 COMMIT 无只读守卫；`server.py:98-108` resource f-string SQL 注入；全源码无鉴权；HITLSQLDatabase 名不副实无人工确认 | 契约与重试循环可借；执行层自建：只读账号+AST SELECT-only+视图白名单+部门行级过滤+LIMIT/EXPLAIN 预算 |
| SQL 专项模型 | XiYanSQL-QwenCoder | 理念参考（模型选型，非代码） | 只读问数场景 P1 启动时评估 | ✅ 仓库为权重发布页：nl2sqlite prompt 模板 + M-Schema 输入约定 + vLLM 片段；**仓库无 LICENSE 文件——权重许可以模型页为准，采用前核实** | 训练侧 | 经 ModelProvider 接入（DashScope 或自托管 vLLM 皆为一个 provider 实现），不影响骨架 |
| 多候选+selection pipeline | XiYan-SQL | 理念参考 | P1 单候选+校验够用；精度不足再引入多候选 | ✅ 仓库**无任何代码**（README/论文导航页）——只能作为论文方法论 | "可复用 pipeline"的预期 | PDF 该项已重写；需要时评估另一仓库 alibaba/XiYan-SQL |

## G/H/L. 其它

| 目标模块 | 来源库 | 采纳度 | 富岭 Repo 证据 | 开源源码证据 | 不采用的部分 | 最终决策理由 |
|---|---|---|---|---|---|---|
| 网关渠道适配层 | Hermes | 数据模型 + 核心源码实现（哨兵模式） | ✅ Repo 钉钉双模共核已成熟（`dingtalk_bot.py:836` + `dingtalk_stream_runner.py`）——**复用而非新建**；缺并发正确性与多渠道抽象 | ✅ `platform_registry.py` PlatformEntry（依赖检查/required_env/独立发送函数）；`run.py:9869-9886` **每会话哨兵占位防双 agent 撕裂** + 全局并发上限拒绝 | 2 万行 gateway 全家桶；国际站默认 endpoint | Repo 裸 daemon 线程无并发上限（dingtalk.json）——哨兵+上限直接补 |
| Trace/Observability | （自研，扩展现有） | 理念参考 | ✅ Repo 已有 request_id ContextVar 中间件+qa_session_log 落库惯例；❌ token 不落库、无工具 trace | ✅ 反面：openai-agents 默认上传境外+敏感数据缺省 true；正面：ADK 导出显式开启+消息正文开关 | OTel 全家桶（现阶段） | Agent trace 沿用"RDS 表即 trace"路线（tool_invocation/agent_step 表），量大后再评估 OTel |
| 评测 harness | （复用 Repo 自有） | — （Repo 复用） | ✅ `eval_harness/` L0-L6 + golden 76/251/338 + Claude judge + 冻结基线门——**全场最厚资产，直接扩展 Agent 维度**（工具选择/参数/权限/SQL 正确性） | — | — | 无需外借；缺口只在门禁接入 CI（`eval_release_gate.sh` DRAFT） |
| 百炼/Coze/Dify 平台承载主链路 | — | 不采用 | Repo 主链路（检索/ACL/审计）全部自有且成熟，平台托管无法复用这些资产 | —（文档级结论 🟡 未重验） | 全部 | 维持 PDF 方向：不承载主链路；决策不依赖其钉钉 HTTP 细节是否仍准确 |

---

## 供应链检查（凡 核心源码实现 / 适配器实现 及以上）

| 对象 | LICENSE | 遥测 | 维护/版本风险 | 结论 |
|---|---|---|---|---|
| Qwen-Agent（循环骨架/截断/注册表/提示词 FC 摘抄；可选 Adapter） | Apache-2.0 ✅ | 未发现 ✅ | 活跃；若作依赖须锁版本并强制显式 model_server（防默认公有云路由）；核心依赖 11 包较轻但含 dashscope 硬依赖 | 摘抄√；Adapter 可选√；不整库引入 |
| LangGraph（表结构/协议复刻） | MIT ✅ | 核心无 ✅（CLI 有 Supabase 上报——不引 CLI） | 只借 DDL 与协议语义，零运行时依赖 | √ |
| AgentScope（循环/状态机/压缩参照） | Apache-2.0 ✅ | 未发现 ✅ | **2.0.4dev 快速迭代、1.x→2.x 已推倒一次**——禁作依赖，只参照实现 | 参照√，依赖✗ |
| OpenAI Agents SDK（审批状态机/护栏参照） | MIT ✅ | **默认上传境外+敏感数据缺省 true** ⚠️ | 只借语义与局部代码，不装包——遥测风险即消除 | 参照√，依赖✗ |
| Google ADK（拆桶合并/PluginManager/表结构） | Apache-2.0 ✅ | 默认不导出 ✅（开启后消息正文默认进 span，需关） | workflow 引擎 experimental；只移植自包含函数 | √ |
| Hermes（压缩/HARDLINE/哨兵/血缘摘抄） | MIT ✅ | 未发现 ✅ | 单文件巨型模块，摘抄自包含段落；**fail-open 分支反向改造** | 摘抄√ |
| OpenClaw（回调五件套/沙箱校验/策略管道参照） | MIT ✅ | 两处 opt-out 型 ⚠️（摘抄纯函数不涉及） | 摘抄自包含纯函数 | 摘抄√ |
| Gajae-Code（审批协议/ActionRegistry/白名单分权移植） | MIT ✅ | 未发现（OTel opt-in）✅ | 个人项目迭代快——移植而非依赖，协议版本自管 | 移植√ |
| M-Schema（vendoring） | Apache-2.0 ✅ | 未发现 ✅ | 打包不规范（无 build 配置）——vendoring 进自有包+补脱敏 | vendoring√ |
| oh-my-openagent（路由数据结构） | **Sustainable Use License（非 OSI）** ⛔ | **默认 PostHog** ⚠️ | **禁止源码复制**；只按其数据结构形状自写 | 仅数据模型/理念 |
| xiyan_mcp_server | Apache-2.0 ✅ | 未发现 | demo 级工程质量+四项安全缺陷 | 仅 API 语义，本体✗ |
| XiYanSQL-QwenCoder（模型权重） | **仓库无 LICENSE** ⚠️ | — | 权重许可以 HF/ModelScope 模型页为准，采用前核实 | 待核实后按 provider 接入 |
