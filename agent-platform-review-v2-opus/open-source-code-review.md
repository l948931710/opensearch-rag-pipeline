# 开源源码审查（V2）

> 按 §5.6 格式:仓库 · commit · 访问状态 · 关键文件/符号 · 真实实现(基于源码非 README) · 可借鉴 · 不适合部分 · 对 PDF 原结论的确认/推翻/重写。
> 环境可达性:GitHub 出口放行,全部 `git clone --depth 1` 成功读**真实源码**。以下均为【代码存在】级(库代码无线上部署面)。第三方仓库全程只读静态审查,未运行任何代码。

## 访问状态总表

| 仓库 | commit / tag | 状态 | LICENSE | 遥测 |
|---|---|---|---|---|
| QwenLM/Qwen-Agent | `31a4d36` (2026-03-04) | ✅ 已读源码 | Apache-2.0 | ❌ 无 |
| langchain-ai/langgraph | `5931a5f` (v1.2.7) | ✅ 已读源码 | MIT | 核心库无;CLI 有(可关) |
| agentscope-ai/agentscope | `922e673` (2.0.4dev) | ✅ 已读源码 | Apache-2.0 | ❌ 无(默认 no-op) |
| openai/openai-agents-python | `2afb6e1` (0.17.7) | ✅ 已读源码 | MIT | ⚠️ tracing 默认上报 OpenAI |
| google/adk-python | `e6df097` (2.3.0) | ✅ 已读源码 | Apache-2.0 | OTel,env-gated 默认 no-op |
| alibaba/spring-ai-alibaba | `e1b1482` (1.1.2.2) | ✅ 已读源码 | (Java, Apache 系) | Micrometer,需引 starter |
| NousResearch/hermes-agent | `26dca5e` | ✅ 已读源码 | MIT | ❌ 无(仅版本检查) |
| openclaw/openclaw | `6011c9e1` | 🔄 补审中 | 待补 | 待补 |
| XGenerationLab/XiYan-SQL 系列 | `603deda`等 | 🔄 补审中 | 待补 | 待补 |
| code-yeongyu/oh-my-openagent | `3f51596` | 🔄 补审中 | 待补 | 待补 |
| Yeachan-Heo/gajae-code + ultraworkers/claw-code | `7b4ea98`/`4ea31c1` | 🔄 补审中 | 待补 | 待补 |

---

## 1. Qwen-Agent — AgentLoop 首选候选(经 Adapter 接入)

- **主循环**:`qwen_agent/agents/fncall_agent.py:73-108` · `FnCallAgent._run` · ReAct while 循环,上限 `MAX_LLM_CALL_PER_RUN=20`;`agent.py:178-210` · `_call_tool` 按名查 `function_map`,普通异常被**吞成 traceback 文本回喂模型**(需硬失败须抛 `ToolServiceError`)。
- **工具体系**:`tools/base.py:24` `TOOL_REGISTRY`(模块级全局 dict,无多租户隔离)+ `:44` `@register_tool`(import 时副作用注册);`BaseTool` 契约面仅 `name/description/parameters/call`;`is_tool_schema`(:62-106)对 parameters 关键字**白名单过严**(多一字段即非法,`additionalProperties/$defs` 不容)。
- **DashScope**:`llm/qwen_dashscope.py:49` dashscope SDK 原生;`llm/base.py:92-96` `model_type=='qwen_dashscope'` **默认强制 use_raw_api=True** 走原生 tool_calls;raw 路径仅支持 stream、绕过 retry/cache。
- **MCP**:`tools/mcp_manager.py` 进程级单例 + 独立线程 asyncio loop;stdio/SSE/streamable-http;**仅提取文本结果,图像/blob 丢弃**;`json.loads` 严格解析(传 dict 崩)。
- **可借鉴**:Adapter 成本极低——继承 BaseTool 覆写 4 属性+call,可**以实例形式直接塞 function_list,完全绕开全局 TOOL_REGISTRY**(`agent.py:212-217`)——正是 EnterpriseTool→Qwen-Agent Adapter 的落点。
- **不适合**:全局单例(TOOL_REGISTRY/MCPManager)不利多租户;沙箱不达生产(见下);异常吞噬语义与企业硬失败冲突。
- **PDF 裁决**:P1 **重写**——BaseTool/register/MCP/DashScope 零适配 ✅ 成立,但"**并行调用**"失实(执行层同步串行 for 循环,`agents/` grep ThreadPool/gather 0 命中)。P12 **重写**——不是"无沙箱"而是"有基础 Docker 沙箱但隔离不足"(`code_interpreter.py:257`:无 `--network none`、kernel 端口发布 0.0.0.0、无资源限额、协作式超时可绕过;`python_executor.py:98` 源码自认 "Not sandboxed. Do not use for production")。

## 2. LangGraph — HITL/持久化事实标准（借语义,不整体引擎化）

- **Checkpointer**:`checkpoint/base/__init__.py:176` `BaseCheckpointSaver`;Postgres 实现三表 `checkpoints`(JSONB)/`checkpoint_blobs`(按版本去重)/`checkpoint_writes`(pending/interrupt/resume 落盘,负 idx 约定 `WRITES_IDX_MAP`);**迁移仅在显式 `setup()` 时执行,库不自动 apply**。
- **interrupt/resume 语义(经源码精确验证)**:`types.py:811-934` `interrupt()` —— resume 时**节点从入口整体重跑**,已答复的 interrupt 靠 `scratchpad.resume` 回放跳过(`:915-918`);被中断 task 的**普通 channel 写入不落盘、整体丢弃**(`_runner.py:585-591`),只存 INTERRUPT/RESUME。确定性 task_id = `hash(checkpoint_id, ns, step, node_name, path)` → resume 无需持久化"哪个任务中断"。
- **Store(长期记忆)**:`store/base` `BaseStore`,namespace 元组 + key + value;PostgresStore 落 `store(prefix,key,value jsonb)` + 可选 pgvector;与 checkpointer 完全正交。
- **可借鉴**:P4 铁律"interrupt 前副作用须幂等"**源码证实为真**;不引整库时最小照抄数据模型:两张表 + 负 idx 特殊通道 + 确定性 task_id + NULL_TASK_ID 哨兵 + per-task resume 列表(见 borrowing-matrix)。
- **不适合**:整套引擎绑 channel/pregel 模型,与富岭 RDS 状态机范式不同;Redis checkpointer 无官方实现。
- **PDF 裁决**:P8 **推翻(机制)/保留(现象)**——❌ AsyncPostgresSaver 用 `asyncio.Lock`(`aio.py:43`)不是 threading.Lock(后者只在同步类);✅ 但确有实例级串行化(`aio.py:374` 单实例所有 DB 操作共享一把 asyncio.Lock,连接池被架空;对照 Store 侧已修)。**PDF 机制说错,结论方向对**。P9 **支持**——生产 checkpointer 仅 Postgres,Redis 无官方实现 → 印证"durable checkpoint 落 RDS 非 Redis"。

## 3. AgentScope — v2 重写版，可挂起状态机（AgentLoop 强候选）

- ⚠️ **重大发现:本 commit 是 v2 重写版(2.0.4dev)**,`ReActAgent`/`MsgHub`/`pipeline` **全部删除**,PDF 描述的是 1.x,已过时。
- **主循环**:统一 `agent/_agent.py:94` `class Agent`;`_reply_impl` while 循环 + **tool call 五态状态机(PENDING/ALLOWED/ASKING/SUBMITTED/FINISHED)**,遇确认/外部执行事件 `return` 退出,下次以结果事件续跑——**循环可挂起、可整体序列化(Pydantic AgentState)、跨进程恢复**。
- **工具协议**:`tool/_base.py:94` `ToolBase` = JSON Schema + 执行元数据(`is_concurrency_safe/is_read_only/is_external_tool/is_state_injected`)+ **权限四方法(check_permissions/check_read_only/match_rule/generate_suggestions)** + 流式 ToolChunk——是 Qwen-Agent BaseTool 的严格超集。
- **异步并行**:`_agent.py:1148` `_batch_tool_calls` 按 `is_concurrency_safe` 自动切 sequential/concurrent 批,`asyncio.gather(return_exceptions=True)`——**真并发执行**(与 Qwen-Agent 的串行对照鲜明)。
- **HITL**:`permission/_engine.py` 权限引擎(5 模式,deny→ask→工具自查→allow→默认 ASK)+ 事件协议(RequireUserConfirm/ExternalExecution)是**一等公民、内核而非外挂**。
- **可借鉴**:可挂起状态机 + 全量 Pydantic 状态 + 工具协议自带并发/只读/权限元数据 + 权限引擎——这些是 EnterpriseTool 契约与 AgentLoop Adapter 的**最佳设计参照**(比 Qwen-Agent 更贴企业 HITL)。
- **不适合**:多 Agent 强绑 app 层(FastAPI+Redis+MessageBus);单文件 2612 行核心循环不可组合替换;`count_tokens` bytes/4 粗估;2.0.4dev 活跃重写、API 稳定性风险高;`ExceptionGroup` 需 Python≥3.11。
- **PDF 裁决**:PDF 对 AgentScope 的描述(ReAct/asyncio.gather 分发)**已过时,须重写**——现版本是可挂起状态机 + 工具级权限内核,作为 AgentLoop 候选比 PDF 认知强得多,但成熟度/稳定性风险也更高。

## 4. OpenAI Agents SDK — HITL 工具级 API 的最佳形状参照

- **HITL API**:`tool.py:426` `FunctionTool.needs_approval: bool | Callable[(ctx, params, call_id)->bool]`(**支持按参数动态判定**);`result.py:334` `RunResult.interruptions: list[ToolApprovalItem]`;`run_state.py:332` `approve(item, always_approve)` / `reject(item, always_reject, rejection_message)`。
- **状态机**:`run_context.py:29` `_ApprovalRecord{approved: bool|list, rejected: bool|list}` —— **bool=永久粘性,list[str]=按 call_id 一次性**;批准优先于拒绝;可改判。
- **跨天恢复**:`run_state.py:657` `to_json()`(不序列化 agent 定义,仅名字引用)/ `from_json()`(按 agent.name 遍历 handoff 图重建);schema 版本前向 fail-fast。
- **可借鉴**:`needsApproval + interruptions + 粘性批准`的 API 形状 → 映射钉钉审批卡片(interruptions[i].name/arguments/call_id → 卡片字段;state.to_json → 存 DB;回调后 from_json + approve/reject → 续跑),**适配成本低-中**,无超时逻辑(跨天挂起零成本)。
- **不适合**:① agent 定义不入快照,发版改名使旧快照不可恢复;② schema 前向 fail-fast 需版本治理;③ **tracing 默认上报 api.openai.com(含默认开启的敏感入出参)**——企业落地须强制 `OPENAI_AGENTS_DISABLE_TRACING=1`;④ 后端本体境外(P14 的"后端不可用"成立)。
- **PDF 裁决**:P14 **支持**——needsApproval/interruptions/粘性批准的 API 形状与 LangGraph interrupt/resume 互印证,作 HITL 工具级 API 设计参考;后端不用。

## 5. Google ADK — state 作用域分层的设计蓝图

- **四级作用域**:`sessions/state.py:64` `APP_PREFIX/USER_PREFIX/TEMP_PREFIX`(session/user:/app:/temp:);唯一路由 `_session_util.py:37` `extract_state_delta` 按前缀切三桶,**temp: 静默丢弃**;`base_session_service.py:154-202` 统一实现"temp 只存活当前进程内 session、user/app 落独立表"。
- **持久化边界**:Database 后端三表 `sessions`/`app_states`/`user_states`;⚠️user/app 合并是 `dict |`(**无删除语义、last-write-wins**);VertexAi 后端 `get_user_state` 直接 `NotImplementedError`。
- **Plugin 全局横切**:`plugins/base_plugin.py` 13 钩子,Runner 级注册(全局)先于 agent callback;⚠️首个非 None 即短路 + 任一异常即 RuntimeError 全链中断(不如中间件洋葱模型可组合)。
- **可借鉴**:**temp 统一剪除机制是最值得直接照抄的一条**(工作记忆/长期记忆硬边界);user: 三处落点(路由/存储表/读合并)是新增 `dept:` 层的精确模板(见 borrowing-matrix)。
- **不适合**:❌无 dept/tenant 层(硬编码三前缀,dept_id 需自方注入);前缀键绕过 schema 校验;user/app 无删除语义/无版本;语义检索记忆在独立 MemoryService,与 state 分开。
- **PDF 裁决**:P15 **重写**——四级作用域机制 ✅ 真实干净;但 ⚠️**Sequential/Parallel/Loop 三件套本版本全部 @deprecated**(`sequential_agent.py:49`),官方指向新 `Workflow` 图引擎;部署**非强绑 GCP**(Database/SQLAlchemy 后端可自托管境内)。

## 6. Spring AI Alibaba — HITL 三态审批的主要参照

- **HumanInTheLoopHook**:`.../hook/hip/HumanInTheLoopHook.java:47` 是**核心库真实类**(不是示例),`ReactAgent` 主流程特判接线。
- **三态**:`InterruptionMetadata.java:285` `enum FeedbackResult{APPROVED, REJECTED, EDITED}`——⚠️**第三态是 `EDITED` 不是 MODIFIED**(grep MODIFIED 0 命中,传 MODIFIED 抛 IllegalArgumentException)。
- **三态数据流**:APPROVED 原样放行;**EDITED** 用人工 arguments **重建 ToolCall(保原 id/name)替换最后一条 assistant 消息**(⚠️对编辑后 JSON 无 schema 校验);**REJECTED** 保留 toolCall + **预插带拒绝理由的 ToolResponse,执行器按 id 去重跳过**(而非删除,保 tool_call/response 配对);⚠️**无反馈的需审批工具默认放行**(应改 fail-closed)。
- **Checkpoint**:`Checkpoint.java:30` `{id, state 全量快照, nodeId, nextNodeId}`;Saver SPI 有 memory/file/h2/jdbc/mysql/oracle/postgresql/mongo/redis 实现;⚠️人工反馈是**进程内 Java 对象,不经 saver 持久化**,跨进程恢复须调用方重建。
- **可借鉴**:三态语义(尤其 EDITED 改参重建 + REJECTED 预插响应去重)可**直接移植到任何 OpenAI/Anthropic 风格消息协议**;恢复协议 = threadId + 最新 checkpoint + 一次性 feedback。
- **不适合**:Java 栈(富岭是 FastAPI/Python);审批策略仅按工具名精确匹配;无 HITL 专属埋点;feedback 不持久化。
- **PDF 裁决**:P10 **重写**——原生 HumanInTheLoopHook 三态 ✅ 成立,但第三态名 **EDITED 非 MODIFIED**;需修补的原实现弱点(缺反馈默认放行/EDITED 无校验)Python 自研须改 fail-closed + 加校验。

## 7. Hermes Agent — 交互网关形态最同构参照（接口/算法可搬，状态层不可搬）

- **单进程网关**:`gateway/run.py` 单 asyncio loop + 硬编码 `ThreadPoolExecutor(max_workers=10)`;`platforms/base.py:2253` `BasePlatformAdapter` 抽象仅 connect/disconnect/send + 声明式 capability flags。
- **DingTalk 适配器真实存在**:`plugins/platforms/dingtalk/adapter.py`(1707 行)基于 `dingtalk-stream` SDK **Stream Mode**(无需公网 webhook),指数退避重连 `[2,5,10,30,60]`、session_webhook 缓存(5min 过期边际)+ URL 域名白名单、AI Card 流式 + finalize、结构化 `is_in_at_list` 提及门 + 中文唤醒词。
- **session_key 设计**:`gateway/session.py:822` 确定性 `agent:profile:platform:chat_type:chat_id[:thread][:user]`——群聊默认按用户隔离、线程默认共享、DM 无 chat_id 回退 sender 防串味。**天然适合作一致性哈希分片键**。
- **审批 fail-closed**:`tools/approval.py` HARDLINE 硬阻断清单(rm -rf 系统目录/mkfs/dd/fork bomb/shutdown,含绕过拼写)+ manual/smart/off(未知回落 manual)+ **"超时=拒绝、沉默非同意"**(300s 默认)+ once/session/always 三级 + import 时冻结 yolo。
- **上下文压缩五阶段**:无 LLM 裁剪 → 头保护 → token 预算尾保护 → 结构化 LLM 摘要 → 迭代更新;`compression_locks` 表已是 DB 级带过期分布式锁。
- **可借鉴(接口/算法层,与部署形态无关)**:DingTalk 适配全套工程 know-how、session_key 规则(可作 SAE 分片键)、审批 fail-closed 状态机 + HARDLINE 清单、五阶段压缩、`parent_session_id + end_reason` 血缘 schema、ProviderProfile 声明式 provider 层。
- **⚠️ 必须点名的单机假设(SAE 多副本直接踩坑,共 9 处 D1–D9)**:进程内会话互斥、agent 缓存进程内存、**审批阻塞在线程 Event 上(/approve 必须落发起副本)**、session_webhook/去重全进程字典、平台锁是本机文件锁、SQLite 文件库、硬编码 10 worker、无外部控制面、scale_to_zero 是 Fly suspend 语义。**"钉钉 Stream 多副本同 appKey 由钉钉侧投递分配,消息级不保证会话亲和 —— 这正是 Hermes 完全没有的层。"**
- **PDF 裁决**:P3 **支持(需加注)**——单进程 IM 网关 + Qwen 一等公民 ✅;但 Hermes 是**单机单进程**,SAE 多副本的会话亲和/分发是 Hermes 未解决的,不能照搬状态层。P13 **重写**——审批 fail-closed ✅;但"审批与沙箱叠加"**仅在 docker-带宿主挂载场景成立**;对 modal/daytona/singularity/纯 docker,`approval.py:1939-1950` 源码是明确的**替代**关系(隔离沙箱内连 HARDLINE 都被短路)。

---

> **补审仓库(OpenClaw 安全 / XiYan-SQL 系列 / OmO 模型路由 / Gajae-Claw)见本文件末尾"补充审查"章节——由后续 Opus 工作流完成后追加,并同步进 borrowing-matrix。**

## 8. OpenClaw — 执行面隔离与沙箱安全的反面教材（专项对照）

- **审批信道带外隔离(正例)**:`gateway/server-methods/nodes.ts:1319-1330` 在 `node.invoke` handler 顶部、allowlist 解析**之前**硬编码字符串拦截 `system.execApprovals.*`,强制改走 `exec.approvals.node.set`(`operator.admin` scope,`core-descriptors.ts:57`)+ base-hash 乐观锁;回归测试固化 `server.node-invoke-approval-bypass.test.ts:529-545`。反例结构:节点落地端 `node-host/invoke.ts:531` 自身**无 scope 校验**("信任上游"),故上游任一可透传路径都是单点失效。
- **沙箱框架级显式校验(正例)**:`agents/sandbox/validate-sandbox-security.ts` 真实 denylist(`BLOCKED_HOST_PATHS` /etc /proc /sys /dev /root + docker.sock 族;`BLOCKED_HOME_SUBPATHS` .aws/.ssh/.docker...);**bind 祖先/后代双向覆盖检查**(`getBlockedReasonForSourcePath:139-158`——挂 `/home` 会暴露 `/home/.ssh` 判 `covers`);symlink 逃逸硬化(沿已存在祖先 realpath 二次校验);框架级强制点 `docker.ts:413`(容器创建入口 fail-closed,危险绕过仅经命名 `dangerously*` 开关)。
- **Tool Policy 逐层只减不增**:`agents/tool-policy-pipeline.ts:147-213` filter-only 流水线(global→agent→sandbox→subagent),每步在上一步输出上 `Array.filter`,后层无法加回前层已删工具;deny 优先。⚠️但 `agent-tools.policy.ts:105-110` 角色 deny 可被 operator 的 explicitAllow 覆盖——**富岭若要更强隔离,安全基线 deny 应不可被下层 allow 覆盖**。
- **per-agent auth 隔离**:`SECURITY.md:53` 明确把"同 gateway 多互不信任用户 + 期望隔离"列为 **out-of-scope**,一用户信任模型,隔离靠 OS/host 级拆分。**多租户场景不可照搬**;可借鉴其 scope 分级 + per-agent 工作区 + 沙箱 env 白名单。
- **PDF 裁决**:P11 **重写**——攻击结构(审批写与工具调用同转发原语)真实、修复可验证,但 ⚠️**CVE-2026-28466 无 ID 落地(仓内 grep 0 命中)、无 commit 可追溯,且"execApprovals 在 SYSTEM_COMMANDS"措辞与源码不符**(execApprovals 从不在 SYSTEM_COMMANDS,真实风险面是 node.invoke 透传)。引用须以源码行为为准。P5 **支持**——两条设计规则(审批带外隔离 / 沙箱框架级校验)均有正反例源码支撑。LICENSE MIT。

## 9. XiYan-SQL 系列 — Text-to-SQL 专项（拿走什么 / 必须自建什么）

- **四仓定性**:xiyan-sql=**论文仓,0 行可运行代码**(仅 README + PDF + png);xiyan-qwencoder=**权重发布仓,0 代码**(⚠️仓内**无 LICENSE**,权重 license 须到 HF/ModelScope 单独确认);m-schema=**✅ 真实可运行**(Apache-2.0);xiyan-mcp=**✅ 可运行 MCP server**(Apache-2.0)。
- **M-Schema(唯一即取组件)**:`schema_engine.py:10` `SchemaEngine`(继承 llama_index SQLDatabase,传 SQLAlchemy Engine)反射表/主外键/列类型 + 抽样 distinct 值 → `m_schema.py:125` `to_mschema()` 产结构化 prompt 文本。⚠️**抽样会读真实数据行进 prompt**(`schema_engine.py:135`),企业用须自加列级脱敏。
- **xiyan-mcp 只读守卫 = ❌ 完全没有(安全关键)**:`db_source.py:73-85` `HITLSQLDatabase.fetch()` 用 `engine.begin()`(**自动提交事务**)`execute(text(sql))` 直接执行 LLM 生成的任意 SQL,**无 is_select 校验**,INSERT/UPDATE/DELETE/DROP 会真实落库;类名 `HITLSQLDatabase` **名不副实**(无任何人工确认闸门);`read_resource` f-string 拼 `SELECT * FROM {table_name}` 存表名注入面;无 ACL/行级/租户过滤。
- **可拿走**:M-Schema 生成器(即取,补脱敏)、QwenCoder 权重(拿权重不拿代码,BIRD 69.03% 是官方自报单模型分)、MCP server 骨架(作 PoC 参考:OpenAI 兼容调用 + schema 反射 + sql_fix 三次纠错)。**必须 100% 自建**:只读/SELECT-only 守卫、ACL/行级/租户注入、**语义层**(指标/维度/口径,四仓完全没有)、真正 HITL 闸门、审计/超时/资源限额。
- **PDF 裁决**:P19 **重写**——QwenCoder 作现成组件 ✅(仅模型层,零封装代码);MCP ⚠️半成立(可跑但缺安全层,不可直接生产);XiYan-SQL 框架 ❌(只有论文没有代码,多候选/选择/Refiner 编排 0 行开源)。**安全三件套(只读守卫/ACL 注入/语义层)在四仓全部为零。**

## 10. OmO — 模型路由设计（借设计,不引依赖）

- **映射**:`model-core/src/category-model-requirements.ts` `CATEGORY_MODEL_REQUIREMENTS`(8 类别静态表)+ `agent-model-requirements.ts` `AGENT_MODEL_REQUIREMENTS`(12 具名 agent);数据结构 `ModelRequirement{fallbackChain: FallbackEntry[]}`,`FallbackEntry{providers: string[](有序候选), model, variant, reasoningEffort...}` + 门控 `requiresModel/requiresAnyModel/requiresProvider`。
- **类别非代码推断**:类别由**上游 LLM 在 delegate-task 显式传 `category` 参数**声明(`tools/delegate-task/types.ts:58`),`<Selection_Gate>` 只是 prompt 文本门控,**无关键词/嵌入分类器**。
- **⚠️ 三处消费点**:两套并行 resolver(规范版 `model-resolution-pipeline.ts:75` + 增强版 `delegate-core/model-selection.ts:75`,后者多 cross-provider/explicit-high)+ 运行时失败重试链(`runtime-fallback-error-classifier.ts` 已内建**中文额度/限流正则**,对境内有利)。用户可配置覆盖(每类别 model/fallback_models/variant/... + 可新增自定义类别;`disabled-providers.ts` 可屏蔽境外 provider)。
- **境内移植**:数据结构天然多 provider,链中已内建境内 provider(bailian-coding-plan=DashScope、qwen3.5-plus、kimi/minimax/zai);须改造:重写两张表候选顺序(解 hephaestus 硬钉 openai)、同步类别默认模型第二源、补 provider 变换分支、connectedProviders 缓存含 DashScope、两套 resolver 回归。**无权重/轮询/健康度 LB**(顺序取第一个可达),多档 DashScope 限流须自建 LB。
- **硬阻塞(非代码)**:⚠️**LICENSE = Sustainable Use License v1.0(n8n 系非 OSI,禁商用/禁有偿分发)——境内商用移植违约,须重授权或洁净室重写**;⚠️PostHog 遥测默认开启(须强制关闭)。
- **PDF 裁决**:P16 **重写**——"任务类别→模型路由用户可配置"✅ 但**类别由上游 LLM 声明非代码推断**,且实为初始解析(两套 resolver)+ 运行时重试三处消费,比"单一有序链"复杂。**只借设计思想(理念参考),绝不引依赖(LICENSE 硬否决)。**

## 11. Gajae-Code / Claw Code — 低优先级设计参考（均 MIT）

- **Gajae action_needed/reply 协议(P17)**:✅ 真实、高可借鉴。独立 Rust crate `crates/gjc-notifications/`(~3984 行)——`protocol.rs` 传输无关 JSON 契约:`ActionNeeded{id,kind(Ask/Idle),sessionId,question,options}` / `Reply{id,answer,token,idempotencyKey}`(**携 per-session token + 幂等键**),answer 三形(Index/Text/Structured),六种 RejectReason(AlreadyAnswered/Unauthorized/IdempotencyConflict...),终态帧 `ActionResolved{resolvedBy: Local/Client/Timeout}`。⚠️**唯一落地传输是 loopback WebSocket**(`server.rs`),"传输无关"指开放客户端契约,非可插拔 transport。
- **Claw Code(P18)**:设计成立但**仓库不可采用**——`README.md:57` 自称 "not the serious production project... museum exhibit",导向 gajae/lazycodex。技术主张全部源码确证:append-only 消息(`session.rs:279` push→append-JSONL→失败 pop 回滚)、全历史重建请求(`conversation.rs:365` 每轮 `messages.clone()`,请求是历史纯函数)、Provider trait 双实现、~208 行纯函数式 `run_turn`;但 `main.rs` 是 **19831 行巨石**,agent 自动维护痕迹,工程可采用性低。
- **PDF 裁决**:P17 **支持(高可借鉴)**——action_needed/reply 传输无关契约 + token/幂等/replay,用钉钉互动卡片重实现(富岭卡片双通道底座已备);`gjc-notifications` 的 protocol 契约是跨两仓最值得直接移植的单块(理念参考,重写非引依赖)。P18 **支持(仅设计)**——append-only + 全历史重建是干净 harness 模式,但仓库自我否定,借模式勿采仓库。
