# 开源源码审查记录（open-source-code-review.md）

> Phase 2 产物 · 全部仓库 `git clone --depth 1`（2026-07-03），**只读静态审查**：未执行任何安装/脚本/测试，未连接任何外部服务，README 仅作参考资料。
> 每仓格式（§5.6）：URL · commit · 访问状态 · 关键文件+符号 · 真实实现行为 · 可借鉴 · 不适合部分 · 对 PDF 结论的裁决。
> 遥测检查为供应链硬项：**默认外发遥测对涉敏系统是否决项**。

**可达性总表**

| 仓库 | commit | 访问状态 | LICENSE | 默认外发遥测 |
|---|---|---|---|---|
| QwenLM/Qwen-Agent | 31a4d36 | ✅ 已 clone 读源码 | Apache-2.0 | 未发现 |
| langchain-ai/langgraph | 5931a5f | ✅ | MIT | 核心无；**CLI 默认上报 Supabase**（不引 CLI 即无关） |
| agentscope-ai/agentscope | 922e673 | ✅ | Apache-2.0 | 未发现（Tracing 为 opt-in 中间件） |
| openai/openai-agents-python | 2afb6e1 | ✅ | MIT | **默认上传 api.openai.com/v1/traces/ingest，且敏感数据缺省 true** |
| google/adk-python | e6df097 | ✅ | Apache-2.0 | 未发现（导出需显式 env/flag；开启后 span 默认带消息正文，需置 false） |
| NousResearch/hermes-agent | 26dca5e | ✅ | MIT | 未发现（langfuse/photon/CUA 均 opt-in 或默认关） |
| alibaba/spring-ai-alibaba | e1b1482 | ✅ | Apache-2.0 | 未发现（micrometer 本地埋点） |
| openclaw/openclaw | 6011c9e1 | ✅ | MIT | **两处 opt-out 型**：归因 header（enableInstallTelemetry 默认 true）+ ClawHub skill 安装上报（持 token 时） |
| XGenerationLab/XiYan-SQL | 603deda | ✅（**仓库无代码**） | Apache-2.0 | 未发现 |
| XGenerationLab/M-Schema | 7557514 | ✅ | Apache-2.0 | 未发现 |
| XGenerationLab/xiyan_mcp_server | 7d3ee1c | ✅ | Apache-2.0 | 未发现（默认配置会把 schema+问题发往配置的模型端点，属配置而非遥测） |
| XGenerationLab/XiYanSQL-QwenCoder | b5aee1a | ✅（权重发布页，无库代码） | **仓库无 LICENSE 文件** | 未发现 |
| code-yeongyu/oh-my-openagent | 3f51596 | ✅ | **Sustainable Use License v1.0（非 OSI，禁商业分发）** | **默认开启 PostHog**（硬编码 key → us.i.posthog.com，opt-out 环境变量） |
| Yeachan-Heo/gajae-code | 7b4ea98 | ✅ | MIT | 未发现（OTel 为 opt-in，无 SDK 时 no-op） |
| ultraworkers/claw-code | 4ea31c1 | ✅ | MIT | 未发现（telemetry crate 仅内存/本地 JSONL sink，无网络上报） |

---

## 1. Qwen-Agent（github.com/QwenLM/Qwen-Agent @ 31a4d36）

**关键文件**：`qwen_agent/tools/base.py`（BaseTool L109-190 / TOOL_REGISTRY L24 / register_tool L44 / is_tool_schema L62）；`qwen_agent/agent.py`（_init_tool L212-237 / _call_tool L188-203）；`qwen_agent/agents/fncall_agent.py`（_run L73-108）；`qwen_agent/llm/{base,oai,qwen_dashscope,function_calling}.py`；`qwen_agent/tools/mcp_manager.py`；`tools/code_interpreter.py`、`tools/python_executor.py`。

**真实实现行为（源码而非 README）**：
- BaseTool 契约=3 类属性（name/description/parameters）+1 方法 call()，parameters 支持 legacy list 或 OpenAI JSON Schema（jsonschema 严格校验）。
- **最关键接缝**：Agent 不强依赖全局注册表——`_init_tool` 对 BaseTool **实例直接注入** `function_map`，只有字符串名才查 TOOL_REGISTRY。企业 Adapter 的最小成本=实现一个 BaseTool 子类并按实例传入。
- 主循环 ~35 行 while：`_call_llm` → `_detect_tool`（只认 message.function_call）→ `_call_tool` → FUNCTION 消息回灌；工具异常转 traceback 文本回灌不中断。上限 MAX_LLM_CALL_PER_RUN=20。
- **"并行函数调用"真相**：仅协议层（单轮多 tool_call），默认关（`function_calling.py:63`），开关只切提示词模板；执行侧是**顺序 for 循环同步执行**。仓库唯一线程池 `utils/parallel_executor.py` 只服务 ParallelDocQA。
- 函数调用默认是**提示词模拟**（nous `<tool_call>` 文本协议），不依赖服务端原生 tool_calls——任何"只会聊天"的境内模型都能带工具跑（脆弱但是有价值的兜底）；原生通道走 use_raw_api（qwen_dashscope 默认强制开）。
- LLM 抽象分层良好（BaseChatModel 管缓存/截断/退避重试/停止词，叶子类只管 HTTP），换 provider≈200 行子类+一行注册；`oai.py` TextChatAtOAI 可接任意 OpenAI 兼容 base_url（api_key 缺省 'EMPTY' 适配 vLLM）。
- **DashScope 耦合四处**：setup.py 硬依赖 dashscope；`llm/__init__.py:22` 顶层无条件 import；基类内嵌 dashscope 特判；**模型名含 'qwen' 且未配 model_server 时默认路由 DashScope 公有云**（误配置即数据出企业边界）。
- 沙箱：python_executor 自述 "Not sandboxed...not for production"；code_interpreter 已是 Docker+ipykernel，但无 --network/--memory/--cpus/只读根，5 个内核端口 `-p p:p` 发布到宿主 0.0.0.0。
- **无任何 HITL 挂点**：检测到 function_call 即无条件执行。
- token 分级截断算法（`base.py:602-804`：先压 FUNCTION 结果→删中间步→截首尾）是踩过坑的实现。

**可借鉴**：BaseTool 契约（API 语义）；注册表双通道（核心源码 ~60 行）；主循环骨架与异常回灌（核心源码参照）；分级截断（核心源码）；提示词模拟 function-calling 兜底（核心源码）；TextChatAtOAI 流式 tool_calls 增量归并（适配器实现）。
**不适合**：默认公有云路由；全局单例态（TOOL_REGISTRY/MCPManager/LLM_REGISTRY）；沙箱整层；无 HITL；假并行；gradio GUI（==锁版）。

**PDF 裁决**：①契约/MCP/并行 → **重写**（并行是协议非执行）；②沙箱不达生产 → 支持；③DashScope 零适配 → 支持；④（新增裁决点）provider 无关 → **重写**（OpenAI 兼容层可用，但带 Qwen 偏置与 dashscope 硬依赖）。

---

## 2. LangGraph（github.com/langchain-ai/langgraph @ 5931a5f）

**关键文件**：`libs/checkpoint/.../base/__init__.py`（Checkpoint TypedDict L92-123 / BaseCheckpointSaver L176-415 / WRITES_IDX_MAP）；`libs/checkpoint-postgres/.../base.py`（MIGRATIONS 三表 L43-91）与 `aio.py`（AsyncPostgresSaver L43,59,363-403）；`libs/langgraph/langgraph/types.py`（interrupt L811-934 / Interrupt.id=xxh3(ns) L576）；`pregel/_runner.py`（commit L574-613）；`pregel/_io.py`、`pregel/_loop.py`、`pregel/_algo.py`；`libs/checkpoint/.../store/base/__init__.py`（BaseStore L700-846）；`serde/{jsonplus,encrypted}.py`。

**真实实现行为**：
- Checkpoint=channel 化状态快照（uuid6 单调 id + channel_versions + versions_seen 版本向量）；Postgres 三表：checkpoints（PK thread_id+ns+id）/ checkpoint_blobs（按 channel,version 去重存大对象）/ checkpoint_writes（中间写，负索引区分 ERROR/INTERRUPT/RESUME 特殊写）。
- interrupt()：scratchpad 计数器按**调用序号**匹配 resume 值，有值直接返回，无值 raise GraphInterrupt；中断 id=命名空间哈希（幂等可寻址）。
- 中断持久化：**只落 INTERRUPT/RESUME 写，节点常规写全部丢弃**→恢复时该节点从头重跑（触发条件靠版本向量仍成立）。"interrupt 前副作用须幂等"是源码语义的直接推论。
- Command(resume)：先持久化为特殊 write 再在重放时注入 scratchpad——天然支持崩溃后重复 resume；多 pending 中断必须用 id map 否则 RuntimeError。
- interrupt/resume **强依赖 checkpointer**（无则 RuntimeError 快速失败）。
- AsyncPostgresSaver：**asyncio.Lock（非 threading.Lock）**实例级串行化全部 DB 操作（连接池也被串行）；threading.Lock 只在同步版。
- 官方仓库**无 Redis checkpointer**（cache/redis 是节点结果缓存）；InMemorySaver 自述仅测试用。
- BaseStore：namespace 元组 + key + value dict，batch 为唯一抽象核心，search 可选语义检索（默认关）；与 checkpointer 完全分离的两层记忆。
- 加密序列化钩子（EncryptedSerializer）+ msgpack allowlist 反序列化（防 pickle 类攻击）——境内加密存储合规直接可参照。

**可借鉴**：checkpoint 三表结构（数据模型，直接参照建 RDS 表）；Saver 接口语义（put/get_tuple/list/put_writes+负索引）；interrupt/resume 协议（核心机制自研复刻）；BaseStore 接口（API 语义）；加密+allowlist serde（适配器参照）。
**不适合**：整套 Pregel 运行时（1300+ 行、langchain_core 深耦合，违反不推倒重建）；LangSmith/Platform 生态（境外 SaaS）；CLI（Supabase 遥测）；节点整体重跑语义直接照搬会重复执行外部副作用（必须把写移到 interrupt 之后——与 V2 铁律一致）。

**PDF 裁决**：①interrupt/resume 回注 → 支持；②节点重跑幂等 → 支持；③threading.Lock → **重写**（asyncio.Lock，瓶颈结论成立机理错误）；④无 Redis checkpointer → 支持；⑤两层记忆 → 支持。

---

## 3. AgentScope（github.com/agentscope-ai/agentscope @ 922e673）

**关键文件**：`src/agentscope/_version.py`（**2.0.4dev**）；`agent/_agent.py`（_reply_impl L575-732 / _batch_tool_calls L1148 / _execute_concurrent_tool_calls L1234 / _execute_tool_call L1339-1590 / compress_context L259-521 / _call_model L2086）；`tool/_base.py`（ToolBase 元数据）；`tool/_toolkit.py`（call_tool L225-388）；`permission/_engine.py`；`state/_state.py`（AgentState）；`model/_dashscope/_model.py`；`app/storage/_redis_storage.py`。

**真实实现行为**：
- 1.x 的 ReActAgent/MsgHub/pipeline **已删除**；2.0 统一 Agent 类=事件流式 reasoning-acting 循环，**可挂起可续跑**（RequireUserConfirmEvent/RequireExternalExecutionEvent 即 return，下次 reply 带确认结果续跑）。
- 并行工具=条件性分批：按 `ToolBase.is_concurrency_safe` 切 concurrent（asyncio.gather+Queue+sentinel）/sequential 批。
- **工具契约是四家里最强**：input_schema 自动生成 + is_concurrency_safe / **is_read_only** / is_external_tool / is_state_injected 元数据；工具执行统一流式契约（ToolChunk/ToolResponse，异常回灌、取消=INTERRUPTED 终态）。
- **HITL 原生**：jsonschema 入参校验 → PermissionEngine.check_permission（规则匹配）→ ASK 时置 ToolCallState.ASKING 挂起；用户确认可**改写工具入参**、可沉淀 PermissionRule。
- AgentState=单一 pydantic 对象（context/summary/cur_iter/permission_context/tool_context...），序列化即持久化。
- 模型容错：max_retries+fallback_model 链在 Agent 层实现。
- app 服务层 StorageBase 唯一实现是 Redis；多 Agent 上移到 app 层（team 工具+MessageBus）。

**可借鉴**：工具元数据契约（API 语义/数据模型——EnterpriseTool 的主要参照）；可挂起事件流循环（核心源码实现参照——自研 AgentLoop 的主要参照）；权限状态机与规则沉淀（核心源码参照）；上下文压缩+Offload（核心源码）；重试+fallback（核心源码）。
**不适合**：完整依赖引入（2.0.4dev 快速迭代、1.x→2.x 已推倒一次；核心硬依赖捆绑 anthropic/dashscope/openai/mcp/tree_sitter）；app 层全家桶（强依赖 Redis+FastAPI+socketio）；E2B 境外沙箱。

**PDF 裁决**：①1.0 ReAct/并行 → **重写**（版本过时；并行是条件性分批）；②次选骨架 → 支持（且工具契约/HITL/状态管理**优于 Qwen-Agent**，地位应上调为契约主参照）。

---

## 4. OpenAI Agents SDK Python（github.com/openai/openai-agents-python @ 2afb6e1）

**关键文件**：`src/agents/tool.py`（needs_approval L1300/1949）；`result.py`（interruptions L367 / to_state L393）；`run_state.py`（approve/reject L323-357 / _serialize_approvals L359 / to_json L657）；`run_context.py`（is_tool_approved 三态 L178-198）；`run.py`（RunState 恢复 L453-508）；`run_internal/tool_execution.py`（L1071-1201）；`memory/session.py` + `extensions/memory/*`；`run_internal/guardrails.py`（L116-148）；`tracing/processors.py`（**L34 硬编码 api.openai.com ingest**）；`models/{interface,multi_provider}.py`。

**真实实现行为**：
- **Python 版 HITL 完整存在**（关键裁决）：needs_approval（bool 或按参数回调）→ 先解析参数再评估 → 未决挂为可序列化 ToolApprovalItem → `RunResult.interruptions` → to_state() 得 RunState（JSON 可持久化，含 schema 版本）→ approve/reject（支持 always_* 粘性，tool 级永久/call 级单次三态）→ RunState 直接作为 input 传回 Runner.run 恢复。
- Guardrails：默认与首轮模型调用**真并行**（asyncio.gather，tripwire 即 cancel），可选 run_in_parallel=False 前置串行强拦截；工具级护栏三种处置（allow/reject_content/raise）。
- Sessions：4 方法 Protocol（get_items/add_items/pop_item/clear_session）；Redis/SQLAlchemy/Mongo/Dapr/加密后端全在 extensions（可选依赖）。
- **遥测硬项**：tracing 默认注册 exporter 上传 api.openai.com，且 `trace_include_sensitive_data` 缺省 **true**（prompt/工具入出参进 trace）；可 DISABLE_TRACING/替换 processors 完全关闭。
- Model/ModelProvider 双接口 + MultiProvider 前缀路由，ChatCompletions 兼容层完整（不锁死 Responses API）。

**可借鉴**：审批中断-恢复状态机与 RunState 序列化（核心源码实现参照——approval_request 数据模型的主要参照之一）；粘性批准三态（API 语义）；Session Protocol（API 语义）；并行护栏+短路取消（核心源码参照）；护栏触发时的会话选择性持久化（理念）。
**不适合**：默认境外遥测（不引依赖则无关）；深绑 openai SDK 数据模型；HITL 是 opt-in 非强制（needs_approval 默认 False——强制须在自己 Registry 层统一包）。

**PDF 裁决**：①JS needsApproval API → **重写**（Python 等价物 API 形态不同）；②Sessions 多后端 → 支持；③guardrails 并行 tripwire → 支持；④tracing 默认境外 → 支持（PDF 未提敏感数据缺省 true，已补）。

---

## 5. Google ADK Python（github.com/google/adk-python @ e6df097）

**关键文件**：`sessions/state.py`（三前缀 L64-66）；`sessions/_session_util.py`（extract_state_delta L37-50）；`sessions/database_session_service.py`（_merge_state L176-187 / append_event L741-875）；`sessions/schemas/v1.py`（**app_states/user_states/sessions/events 三+1 表**）；`sessions/base_session_service.py`（temp: 生命周期 L174-202）；`plugins/plugin_manager.py`（12 回调 L42-55 / early-exit L275-322）；`agents/base_agent.py`（plugin 优先 L473-506）；`flows/llm_flows/functions.py:569`（before_tool 拦截）；`memory/base_memory_service.py`。

**真实实现行为**：
- state 四级作用域：前缀即路由；写时 extract_state_delta 拆三桶（temp: 丢弃）、读时 _merge_state 合并——**30 行可移植**；持久层按主键强制隔离（app_states PK=app_name；user_states PK=(app_name,user_id)；sessions PK=(app,user,id)）。
- temp: 先应用到内存再从持久化 delta 剔除（顺序是关键细节）。
- 并发三层防护：进程内 per-session asyncio.Lock + DB 行锁（with_for_update）+ 乐观 marker。
- Plugin：Runner 注册一次全局生效，12 个回调点，首个非 None 返回值 early-exit（before_tool 返回 dict 即**替代工具执行**=拦截点），异常 fail-closed。
- MemoryService 与 SessionService 边界：session=事实流水，memory=蒸馏可检索知识，**写入必须显式**（Runner 从不自动写 memory）。
- **GCP 耦合被证伪**：核心依赖零 google-cloud 包；DatabaseSessionService 支持 sqlite/pg/mysql/mariadb；LiteLlm 接任意兼容端点；仅语义 MemoryService 生产实现是 Vertex 系。
- Sequential/Parallel/Loop workflow agents 已被标 @deprecated（转向仍 experimental 的图式 Workflow）——不宜按其 API 对齐。
- HITL：require_confirmation 默认 False，无强制开关。

**可借鉴**：state 前缀+拆桶合并（**核心源码实现**，直接移植）；三表分层 schema（**数据模型**，长期记忆表结构直接参照）；PluginManager 挂点布局（核心源码参照——Policy Engine 的挂点清单）；Memory/Session 边界（理念+API 语义）；并发三层防护（核心源码参照）。
**不适合**：完整依赖引入（自带 Runner/事件循环等于换架构；workflow 引擎 experimental）；Vertex 系 memory；开启遥测导出时消息正文默认进 span（需显式关）。

**PDF 裁决**：①四级作用域 → 支持；②Plugin 全局门控 → 支持；③强绑 GCP → **推翻**（自托管路径源码真实可用）。

---

## 6. Hermes Agent（github.com/NousResearch/hermes-agent @ 26dca5e）

**关键文件**：`gateway/platforms/base.py`（BasePlatformAdapter）；`gateway/platform_registry.py`（PlatformEntry）；`plugins/platforms/dingtalk/adapter.py`（**1,707 行，dingtalk-stream Stream 模式**）+ feishu/wecom（含 wecom_crypto）；`gateway/run.py`（哨兵占位 L9869 / 线程池 L14361 / busy 三策略）；`agent/context_compressor.py`（L662+）；`hermes_state.py`（血缘 schema L695 / compression_locks L772）；`tools/approval.py`（fail-closed L1693/2484 / HARDLINE L365 / 容器跳过 L1939-2230 / **fail-open 分支 L2003-2022** / 配置自保护 L213）。

**真实实现行为**：
- 渠道插件注册表（PlatformEntry：adapter_factory/依赖检查/required_env/独立发送函数）；DingTalk/飞书/企微适配器**内置完整源码**（非 README 宣称）。
- 单进程网关并发：一个 asyncio loop + 10 线程池跑阻塞轮次；**处理消息前先占会话哨兵**防同会话双 agent 撕裂 transcript；忙时输入三策略 interrupt/queue/steer。
- 上下文压缩：50% 阈值触发，保头护尾，中段廉价辅模型结构化摘要，**"respond to the message below, not the summary above" 边界分隔符防摘要被当指令**（踩坑修复）；压缩并发用 SQLite 租约锁表+续租线程。
- SQLite 会话血缘：sessions.parent_session_id 自引用 FK + end_reason 边类型 + 递归 CTE 双向遍历。
- 审批：manual 默认、CLI 60s/网关 300s 超时即拒（"Silence is not consent"+禁止换皮重试）；HARDLINE 12 条灾难命令正则+去混淆管线，高于 yolo/off；**config.yaml/.env 自身写保护**防 agent 自我解禁。
- **两个 PDF 未提的坑**：非交互非网关非 cron 上下文危险命令 **AUTO-APPROVED fail-open**；容器后端跳过**包括 HARDLINE 在内的全部审批**（唯 bind-mount 宿主时恢复）。
- Provider：ProviderProfile 钩子干净；**alibaba/dashscope profile 默认 base_url 是国际站**（dashscope-intl），境内须覆写。

**可借鉴**：PlatformEntry（数据模型）；会话哨兵占位（核心源码——网关并发正确性）；压缩边界分隔符与租约锁（核心源码）；血缘 schema（数据模型）；fail-closed 文案与 HARDLINE 清单+去混淆（核心源码）；配置自保护（核心源码）；busy 三策略（API 语义）。
**不适合**：审批/沙箱替代语义与 fail-open 分支（须反向：叠加+封死）；2 万行单文件 gateway 全家桶整体引入；国际站默认 endpoint。

**PDF 裁决**：①渠道含钉/飞/企微 → 支持；②fail-closed 超时即拒 → 支持（补 fail-open 例外）；③容器后端跳过审批 → 支持（比 PDF 更严重：连 HARDLINE 也跳）；④DashScope 一等公民 → 支持（补国际站警示）。

---

## 7. Spring AI Alibaba（github.com/alibaba/spring-ai-alibaba @ e1b1482）

**关键文件**：`agent-framework/.../hook/hip/HumanInTheLoopHook.java`（@HookPositions(AFTER_MODEL)，approvalOn(toolName)）；`graph-core/.../action/InterruptionMetadata.java`（ToolFeedback L195-290，**enum {APPROVED, REJECTED, EDITED}** L285-289）；`graph-core/.../executor/NodeExecutor.java`（interrupt-before L107-120）；`GraphRunnerContext.java`（HUMAN_FEEDBACK resume L86-122）；`checkpoint/Checkpoint.java`（id/state/nodeId/nextNodeId）+ savers 多后端（含 Redis/MySQL）；`AgentToolNode.java`（ToolContext 注入 L561-572）；`HumanInteractionHandler/ConsoleInteractionHandler.java`（**半成品**）。

**真实实现行为**：
- HITL 时序=模型产出 tool_calls 后、工具执行前，按工具名白名单声明审批；中断即整图停止并返回 InterruptionMetadata（含工具名/参数/文案/state 快照/自动放行清单）。
- **EDITED**：用人工改写参数重建 AssistantMessage.ToolCall（RemoveByHash 替换原消息）后正常执行——改参放行的最干净实现。
- **REJECTED**：保留调用、注入拒绝理由 ToolResponseMessage 回喂模型换方案（非终止）。
- 恢复=同 threadId + HUMAN_FEEDBACK metadata 二次 invoke，从 checkpoint.nextNodeId 续跑；resume 可附带 state patch。
- **两个风险**：漏答反馈的 tool call 在 afterModel 分支按 APPROVED 放行（与 interrupt 侧 validateFeedback 语义不一致）；审批传输层只有控制台半成品（钉钉卡片桥接必须自建）。
- ToolContext 无身份/租户一等字段。

**可借鉴**：ToolFeedback 数据结构+EDITED 重建机制（核心源码语义——审批卡片三态的主要参照）；REJECTED 回喂语义（API 语义，另补硬终止分支）；interrupt-before/after 双钩子（API 语义）；checkpoint 四元组（数据模型）。
**不适合**：Java/Spring 栈整体引入；漏答默认放行语义（必须改 fail-closed）；无审批传输层可复用。

**PDF 裁决**：①原生 HITL Hook → 支持；②三态含 MODIFIED → **重写**（实名 EDITED；REJECTED 是回喂非终止；漏答放行风险 PDF 未提）。

---

## 8. OpenClaw（github.com/openclaw/openclaw @ 6011c9e1）——安全对照专用

**关键文件**：`src/gateway/server-methods/nodes.ts:1319-1330`（node.invoke 早退守卫）；`server-methods/exec-approvals.ts:128-196` + `methods/core-descriptors.ts:54-57`（operator.admin scope）；`exec-approvals.ts:29-74` / `node-host/invoke.ts:252-269`（baseHash 乐观锁）；`agents/sandbox/validate-sandbox-security.ts`（**435 行**：BLOCKED_HOST_PATHS L23-38 / HOME 凭据子路径 L40-49 / 双向祖先检查 L139-158 / validateBindMounts+realpath 复检 L323-373 / network host 与 container:* 阻断、seccomp/apparmor unconfined 阻断 L375-435；`docker.ts:413` 创建容器前强制调用）；`agents/tool-policy-pipeline.ts:127-215`（**只减不增**过滤管道，层序 profile→provider→allow→agent→group→sender→sandbox→subagent→inherited，逐层 audit）；`agents/agent-tools.policy.ts`（SUBAGENT_TOOL_DENY_ALWAYS L50-74 / groupId 服务端背书 L266-308）；`gateway/hooks-request-handler.ts`（webhook 鉴权五件套）；`server-methods.ts:234-271`（方法级统一授权单点）。

**真实实现行为**：CVE-2026-28466 修复后形态完整可见（审批读写整体移出工具信道 + 无条件早退 + 最高 scope + 回归测试 + 内容 hash CAS 防 TOCTOU）。沙箱校验为自包含纯函数、容器创建路径强制调用。工具策略是层化单调递减管道，任何层只能减不能加回。"信任分散"已收敛到三个统一边界（方法授权/工具策略/沙箱校验），残留在 channel 侧审批鉴权。

**可借鉴**：审批信道带外+早退守卫+CAS（理念/API 语义——Approval 管理面设计铁律）；validate-sandbox-security 纯函数（**核心源码**——将来做代码执行沙箱时直接摘抄）；只减不增策略管道+逐层 audit（核心源码参照——Policy Engine 的形态样板）；策略键服务端背书、拒绝调用方自报（理念——与 Repo "服务端生成 acl_groups" 同构）；webhook 鉴权五件套（核心源码参照——**钉钉卡片回调加固**直接可用：header-only、常时比较、限流、白名单、幂等指纹）。
**不适合**：整体架构（自有全栈 RPC/node 配对）；审批策略单文件本地持久化（无集中审计）；opt-out 型遥测习惯（境内须默认关）。

**PDF 裁决**：①CVE 同信道自授权 → 支持；②208 行沙箱校验 → **重写**（现 435 行、范围已扩）；③信任决策分散 → **重写**（核心已收敛、channel 侧残留）。

---

## 9. XiYan 系列（4 仓库）

**XiYan-SQL @ 603deda**：仓库=README+论文 PDF+图片，**零 .py**；训练框架在另一仓库 alibaba/XiYan-SQL（本次未评）。→ 多候选+selection 只是**论文方法论**，无可复用 pipeline 代码。

**M-Schema @ 7557514**：`schema_engine.py`（SchemaEngine 输入 SQLAlchemy Engine，mysql/pg/sqlite 特判，其余靠 inspector 兜底）+ `m_schema.py`（to_mschema() 输出【DB_ID】【Schema】【Foreign keys】文本，支持表/列裁剪、save/load）。约 330 行、无训练依赖。**两个工程刺**：无 __init__.py/平铺导入/pyproject 无 build 配置——只能 vendoring；`fectch_distinct_values()` 每列 SELECT DISTINCT LIMIT 5 抽**真实业务数据**进 prompt，脱敏仅过滤 email/URL——企业接入必须加列级开关与脱敏白名单。

**xiyan_mcp_server @ 7d3ee1c**：tool 面=一个 `get_data(query)`（生成 SQL→执行→失败 sql_fix 重试 3 次→markdown ≤100 行）+2 个 resource。**生产不可用四证**：① `db_source.py` fetch/execute 全部 `engine.begin()`（成功即 COMMIT）执行任意 LLM 生成 SQL，**无 SELECT-only/AST/黑名单**；② `server.py:98-108` resource `f"SELECT * FROM {table_name}"` **直接 SQL 注入**；③ 全源码无鉴权（sse/http 裸暴露，DB 密码明文 yaml）；④ 类名 HITLSQLDatabase **无任何人工确认环节**。本地模式=Flask 单线程 CPU float32 demo（0.0.0.0:5090 无鉴权）。工程卫生差（"README 2.md" 等 macOS 复制残留、入口脚本名 mysql_mcp_server）。

**XiYanSQL-QwenCoder @ b5aee1a**：权重发布页（README 207 行），含 nl2sqlite prompt 模板与 M-Schema 输入约定、HF/ModelScope 链接、vLLM 片段。**仓库无 LICENSE 文件**——权重许可需以模型页为准，采用前必须核实。

**可借鉴**：M-Schema 表示法与 SchemaEngine（核心源码 vendoring+脱敏改造）；get_data 契约+sql_fix 重试（API 语义）；QwenCoder prompt 模板（API 语义）。
**不适合**：xiyan_mcp_server 直接接入（等同重写）；XiYan-SQL 仓库（无代码可接）。

**PDF 裁决**：①pipeline 可复用 → **重写**（论文方法论，无代码入口）；②M-Schema 可采 → 支持（附 vendoring+脱敏条件）；③mcp server 可作现成组件 → **重写**（模式支持属实，"现成组件"被四项安全缺陷推翻）。

---

## 10. oh-my-openagent（github.com/code-yeongyu/oh-my-openagent @ 3f51596）

**关键文件**：`packages/model-core/src/category-model-requirements.ts`（8 类别）与 `model-requirement-types.ts`（**FallbackEntry{providers[], model, variant?, reasoningEffort?...} 双层结构**）；`agent-model-requirements.ts`（11 个 agent 的第二张路由表 + requiresAnyModel/requiresProvider 注册期 gating）；`delegate-core/src/model-selection.ts`（五级优先级解析+模糊匹配）；`omo-opencode/src/config/schema/{categories,fallback-models}.ts`（用户覆盖层）；`tools/delegate-task/category-resolver.ts`（"类别=通用 agent×模型链×prompt 增量" L251）；`team-core/src/config.ts` + `team-runtime/create.ts`（worker 池并发）；`packages/telemetry-core/src/constants.ts`（**硬编码 PostHog key**）。

**真实实现行为**：类别→fallbackChain（外层模型偏好序、内层 provider 优先序、每级可带推理参数）；解析优先级 userModel > userFallback > categoryDefault > 内置链 > systemDefault；**解析期选链+运行期沿链重试**闭环（model-error-classifier 等）；类别按目标模型族动态追加 prompt。委派 category 与 subagent_type 双轨；用户可配层是模型序列（无 providers 数组）。Team Mode 12 工具、4 并发/8 成员上限真实强制；**500 轮/120 分钟只是配置默认值**（500 轮未见运行时强制点）。**默认 PostHog 遥测**（机器指纹+DAU 心跳，opt-out）；**Sustainable Use License 非 OSI 禁商业分发**。

**可借鉴**：fallbackChain 双层数据结构 + 五级解析优先级 + 运行期沿链重试（**数据模型/理念——因 license 禁止源码搬运，须自写实现**）；"类别=通用执行体×模型链×prompt 增量"分解（理念）；PROMPT_METADATA（何时用/何时别用）作委派依据（理念）。
**不适合**：任何源码复制（license）；telemetry-core；自主哲学（无人工审批点、禁止成员提问）；tmux/邮箱/worktree 运行时；海外 provider 链。

**PDF 裁决**：①按类别委派+用户可配 provider 链 → **重写**（双轨入口；用户配不了 provider 优先级链）；②Team Mode 限额 → **重写**（12 工具/4 并发/8 成员属实；500 轮/120 分钟非硬限额）。

---

## 11. Gajae-Code（@ 7b4ea98）+ Claw Code（@ 4ea31c1）

PDF 标"URL 待核实"，实际均可达且为 MIT。低优先级参照，但 gajae 的审批协议贡献超出预期。

**Gajae-Code 关键文件**：`crates/gjc-notifications/src/protocol.rs`（**传输无关审批线协议**：ActionNeeded{id,kind,sessionId,question,options,summary} / Reply{id,answer,token,**idempotencyKey**} / ActionResolved{resolvedBy:local|client|timeout} / ReplyRejected{already_answered|idempotency_conflict|unauthorized...} + Hello 版本协商）；`actions.rs`（**ActionRegistry**："transport-independent heart"——pending→resolved 状态机、幂等重试 DuplicateAccepted、first-valid-reply-wins、迟到回复拒绝、迟连客户端重放，纯内存可测 <200 行可移植）；`notifications/engine.ts`（IM 适配器=render/mapInbound 两方法）；`task/agents.ts:42` + `prompts/agents/*.md`（**四角色分权=frontmatter 声明式工具白名单**，读角色物理上没有 write 工具）；`tools/bash.ts:535-575`（bashAllowedPrefixes **运行时硬 enforcement**，堵 cd/env 绕过）；`tools/plan-mode-guard.ts`（plan 模式=工具层硬约束只许写计划文件 + 子代理工具降级）。
**注意**：ralplan 的"计划审批"是 skill 层约定非服务端硬 gate；全功能通知客户端只有 Telegram（境内不可用，借协议自建钉钉客户端）。

**Claw Code 关键文件**：`rust/crates/claw-analog/src/lib.rs:1220-1425`（最精简循环 **~207 行**非 88 行；append-only messages Vec + 每 turn 落盘 + 从 session 文件重建再追加）；`rust/crates/api/src/providers/mod.rs`（Provider trait + **ProviderCapabilityReport 逐能力枚举** Supported/Unsupported/PassthroughAsTool——能力矩阵比布尔散字段更适合路由决策）；`main.rs:2545`（serve 仅报状态无守护进程，属实）。Python `src/` 层是"porting workspace"占位，无生产价值。

**可借鉴**：gajae 审批线协议+ActionRegistry（**API 语义+数据模型，强烈建议采纳**——钉钉审批协议照此定义，幂等键/令牌/迟到拒绝语义齐全）；声明式工具白名单分权 + 运行时硬 enforcement（核心源码语义——只读 Agent/角色分权直接参照）；plan-mode guard（理念——"审批前只读组装"的工具层强制实现法）；claw ProviderCapabilityReport（API 语义——ModelProvider 能力矩阵）。
**不适合**：两仓整体（完整编码代理/演示项目）；Telegram 系客户端；ralplan 软审批。

**PDF 裁决**：①gajae 审批协议传输无关 → **支持**（protocol.rs:2-7 原文自证）；②claw ~88 行循环/append-only/serve 无守护 → **重写**（207 行起；append-only 基本成立但 auto-compaction 会重写历史；serve 属实）；③**第一轮被 0-3 否决的论断「gajae 面向 Anthropic/OpenAI/Gemini/Grok、会话存 .gjc/」→ 经源码核验为真（支持）**——providers 目录四家俱全（xai=Grok），CONFIG_DIR_NAME=".gjc"。**元发现：PDF 的三票对抗验证机制误杀过真论断，其"0-3 否决"结论同样需要源码复核而非直接采信。**

---

## 12. 跨仓库结论（对 V2 架构的直接输入）

1. **没有任何一家可以整库引入**：四个候选运行时（Qwen-Agent/AgentScope/LangGraph/ADK）分别败在 假并行+无 HITL+公有云默认路由 / dev 版不稳+重依赖 / 运行时复杂+生态境外 / 换架构级引入+workflow experimental。**自研轻量 AgentLoop + 各家"数据模型/API 语义/局部源码"拼装**是证据指向的答案——这与 V2 硬约束（骨架独立于框架）天然一致。
2. **HITL 没有现成的生产级"审批传输层"**：Spring AI Alibaba 是半成品控制台、OpenAI SDK 是 opt-in、Hermes 有 fail-open 旁路、ADK 默认 False——**钉钉审批卡片桥接层必须自建**，各家贡献的是状态机语义（LangGraph resume 协议 + OpenAI RunState 序列化/粘性 + Spring EDITED 重建 + Hermes fail-closed 文案）。
3. **记忆分层的最佳参照组合**：ADK 三表分层（app/user/session）+ LangGraph checkpoint 三表 + BaseStore namespace 语义 + AgentScope AgentState 单对象——全部是"数据模型/API 语义"级采纳，无一需要引运行时。
4. **遥测格局**：openai-agents（默认上传+敏感数据 true）、OmO（默认 PostHog）、OpenClaw（两处 opt-out 型）为反面；Qwen-Agent/AgentScope/ADK/Spring/Hermes/XiYan 干净。凡建议"核心源码实现/完整依赖引入"的对象均已过此检查。
5. **license 红线**：OmO 的 Sustainable Use License 禁止把其源码抄进商业分发物——其 model routing 只能借数据结构自写；XiYanSQL-QwenCoder 权重许可待核实。其余均为 Apache-2.0/MIT。
