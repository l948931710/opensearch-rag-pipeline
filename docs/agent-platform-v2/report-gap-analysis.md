# 报告—Repo 差距分析（report-gap-analysis.md）

> Phase 3 裁决产物。输入：PDF《富岭企业级 Agent 底座架构设计报告》v1（2026-07-02）38 条主张 + 富岭 Repo 事实扫描（@7c704ce）+ 15 个开源仓库源码审查。
> 裁决值：**支持 / 推翻 / 重写 / 证据不足**。开源侧细节证据见 `open-source-code-review.md`，采纳决策见 `borrowing-matrix.md`。

---

## 0. 前提级裁决：PDF 的评级标尺本身已变

| # | PDF 前提 | 裁决 | 说明 |
|---|---|---|---|
| C-1 | 「Qwen-only：生产硬禁非 Qwen」是全部选型的第一标尺 | **重写（业主更新）** | V2 硬约束已改为「provider 无关、仅限境内、DashScope 默认」。凡**仅以 C1 为依据**的评级（尤其"Qwen-Agent 唯一零适配"）失去唯一性论据，须按新标尺重评。Repo 侧佐证：现有代码本就无任何 provider 抽象（4 处手写 HTTP），"换 provider 成本"的真实瓶颈在自身而非框架 |
| C-2 | 「钉钉唯一入口」 | **重写（业主更新）** | V2 改为「钉钉主要入口，不必被框住，性能优先」。且 Repo 事实上已有三个入口：钉钉 bot、console SPA、小程序（PDF 未提后两者） |
| C-3~C-6 | 数据留境内 / SAE / 多部门 ACL / 高风险 HITL | 支持 | 与 V2 硬约束一致 |

---

## 1. PDF 核心主张逐条裁决

### 1.1 选型与架构主张

| # | PDF 主张 | 裁决 | 证据要点（Repo 证据 · 开源证据） |
|---|---|---|---|
| A-1 | 主循环选 Qwen-Agent：唯一零适配、BaseTool/并行调用/MCP 齐备 | **重写** | 三项能力两真一虚：BaseTool 契约与 MCP 属实（`tools/base.py`、`mcp_manager.py`）；**"并行函数调用"是协议层能力且默认关，执行侧是顺序 for 循环**（`fncall_agent.py:94-105`，无任何并发）。另两条 PDF 未提的硬伤：**全框架无 HITL 挂点**（`FnCallAgent._run` 检测到 function_call 即无条件执行）；dashscope 是不可剥离硬依赖且模型名含 qwen 时**默认路由公有云**（`llm/__init__.py:95-98`）。"唯一零适配"的前提 C1 已变更。结论改为：Qwen-Agent 是**可选的 AgentLoop 适配对象与源码借鉴库**，不是骨架宿主 |
| A-2 | 单 Agent + 工具优先，多 Agent 仅局部 | 支持 | Repo 无任何多 Agent 需求证据；写型任务单一上下文与 V2 评审原则 2 一致。维持，且首批场景（RAG/只读 SQL/KIE）全部单 Agent 可覆盖 |
| A-3 | 网关借鉴 Hermes 单进程形态 + 钉钉 Stream | **重写** | Stream 反向 WSS 属实且 **Repo 已实现**（`dingtalk_stream_runner.py`，官方 SDK，PDF 未发现这一点——报告把"待建"当成了现状缺口）。Hermes 借鉴对象成立（DingTalk 适配器 1,707 行真实存在）。但"单进程网关"须按 V2 约束解释为**逻辑单入口**：Repo 当前是 API+Stream 同进程单 worker，这是要拆掉的形态而非要保持的形态 |
| A-4 | HITL 借 LangGraph interrupt/resume；**checkpoint 存 Redis** | **重写（Redis 部分推翻）** | interrupt/resume 语义源码闭合（支持）。但「检查点统一落 Redis」被三重证据推翻：① Repo **零 Redis 接入**（grep 零命中），无运维经验；② LangGraph 官方仓库**无 Redis checkpointer**（仅 InMemory/Postgres/Sqlite，Redis 只是节点缓存 `cache/redis`）且 Postgres 才是生产级；③ 审批跨天 + 审计留痕 + 恢复语义要求 durable 与可查询——应落 **RDS**（已有成熟守卫/账号体系），Redis 只作会话热缓存。详见 borrowing-matrix B 组 |
| A-5 | OpenClaw 两条安全教训 | 支持（细节重写） | CVE 修复痕迹源码可证（`nodes.ts:1319` 早退守卫 + `exec-approvals.ts` 专用方法族 + operator.admin scope + baseHash CAS）；沙箱校验文件实存但已 435 行非 208 行、范围扩至 network/seccomp/apparmor。「信任分散」表述过时：核心决策已收敛到三个统一边界，残留在 channel 侧 |
| A-6 | 百炼不能替代自建网关（钉钉仅 HTTP） | 证据不足（维持 🟡） | 文档级主张，本次未重验官方文档；与架构决策兼容（主链路自建已由其它证据支撑），不作为承重依据 |
| A-7 | 业务工具直接注册为 Qwen-Agent BaseTool（"L3 全部注册为 BaseTool"） | **推翻** | 与 V2 硬约束（工具协议不得依赖具体框架）直接冲突。源码加固了这个判断：BaseTool 契约极小（3 属性 1 方法）且 Agent 支持**实例直接注入绕过全局注册表**（`agent.py:212-237`）——正确形态是 EnterpriseTool 自有契约 + 薄 Adapter 按需转 BaseTool，成本一个适配类 |
| A-8 | 借 LangGraph 语义自建而非整套引入 | 支持 | 与源码复杂度证据一致（Pregel 运行时 1300+ 行、langchain_core 深耦合）；checkpoint 三表结构、interrupt 协议、Store 接口按"数据模型/API 语义"采纳 |

### 1.2 框架事实主张（开源源码裁决，摘要）

| # | 主张 | 裁决 | 一句话证据 |
|---|---|---|---|
| B-1 | Qwen-Agent 沙箱不达生产 | 支持 | `python_executor.py:95` "Not sandboxed...not for production"；code_interpreter 容器无 network/memory 限制、内核端口发布到 0.0.0.0 |
| B-2 | LangGraph interrupt→resume 值回注 | 支持 | `types.py:811-934` + `_io.py:74` + `_algo.py:1280` 全链路闭合 |
| B-3 | 恢复时节点从头重跑、前置副作用须幂等 | 支持 | `_runner.py:574-613`：GraphInterrupt 只落 INTERRUPT/RESUME 写，常规写全部丢弃 |
| B-4 | AsyncPostgresSaver 用 threading.Lock 串行化 | **重写** | 实为 **asyncio.Lock**（`aio.py:43,59`）；实例级串行化与吞吐瓶颈结论成立，但锁类型与"阻塞事件循环"机理错误 |
| B-5 | 官方无 Redis checkpointer | 支持 | libs/ 下仅 InMemory/Postgres/Sqlite 三种 Saver |
| B-6 | OpenAI SDK needsApproval/interruptions（JS 文档） | **重写** | Python 版能力完整但 API 形态不同：`needs_approval` + `RunResult.interruptions`（非 result.state）+ `RunState.approve/reject(always_*)` + RunState JSON 序列化恢复 |
| B-7 | Sessions 多后端（含 Redis） | 支持 | `memory/session.py` Protocol + extensions 下 Redis/SQLAlchemy/Mongo/Dapr/加密全实存 |
| B-8 | ADK state 四级作用域 + Plugin 全局门控 | 支持 | `state.py:64-66` 前缀 + 三表分层持久化（app_states/user_states/sessions）；PluginManager 12 回调点 early-exit fail-closed |
| B-9 | ADK 部署强绑 GCP、本体不可用 | **推翻** | pyproject 核心依赖**零 google-cloud 包**；DatabaseSessionService 支持 sqlite/pg/mysql/mariadb；LiteLlm 接任意兼容端点；仅语义 MemoryService 生产实现是 Vertex 系（该能力自建即可）。ADK 的 state/plugin 设计从"仅设计灵感"升级为**可直接对标的自托管参照** |
| B-10 | Hermes：DingTalk 渠道/fail-closed 审批/审批沙箱替代/DashScope 一等公民 | 支持（补两条 PDF 未提的坑） | 四项全部源码证实。**新发现①**：存在 fail-open 分支——非交互非网关非 cron 上下文危险命令 AUTO-APPROVED（`approval.py:2003-2022`），强制 HITL 必须封死；**新发现②**：DashScope profile 默认指向**国际站** endpoint（dashscope-intl），境内需覆写 |
| B-11 | Spring AI Alibaba：HumanInTheLoopHook 三态 APPROVED/REJECTED/**MODIFIED** | **重写** | Hook 与时序属实（AFTER_MODEL 拦 tool_calls）。第三态实名 **EDITED**（`InterruptionMetadata.java:285-289`，全仓无 MODIFIED）；EDITED=用人工改写参数重建 ToolCall 放行（干净，值得采纳）；REJECTED=拒绝理由回喂模型而非终止。**新发现两个坑**：漏答反馈的 tool call 按 APPROVED 放行（`HumanInTheLoopHook.java:112-115`，合规场景必须改 fail-closed）；HumanInteractionHandler 只有控制台半成品——**审批传输层（钉钉卡片）无现成可复用，必须自建** |
| B-12 | OpenClaw CVE/沙箱校验/信任分散 | 支持/重写/重写 | 见 A-5 |
| B-13 | OmO 类别路由/Team Mode 限额 | **重写** | 委派是 category+subagent_type **双轨**非只按类别；用户可配的是模型序列**不含 provider 优先级链**（该形态仅内置硬编码表）；"500 轮/120 分钟硬限额"是 zod 默认值，**500 轮未定位到运行时强制点**。**新发现（PDF 完全未提，硬否决级）**：默认开启 PostHog 遥测（硬编码 API key 上报 us.i.posthog.com）；LICENSE 是 **Sustainable Use License（非 OSI）禁商业分发**——只能借数据模型/理念，禁止源码搬运 |
| B-14 | Gajae 传输无关审批协议（PDF 自标 🟡）/ 第一轮被 0-3 否决的论断 | **支持 / 支持（被否决论断实为真）** | 协议自证传输无关（`protocol.rs:2-7` "transport-agnostic JSON contract"），且 ActionRegistry 状态机（幂等键/first-valid-wins/迟到拒绝）是**钉钉审批协议的最佳数据模型参照**——该库贡献超 PDF 预期。**元发现**：第一轮被 0-3 票否决的「gajae 面向 Anthropic/OpenAI/Gemini/Grok、会话存 .gjc/」经源码核验**为真**（providers 目录四家俱全、CONFIG_DIR_NAME=".gjc"）——PDF 的对抗验证误杀过真论断，其否决结论与肯定结论同样需要源码复核 |
| B-15 | Claw Code ~88 行极简循环/append-only/serve 无守护进程 | **重写** | 最精简循环 207 行（`claw-analog/src/lib.rs:1220-1425`），"88 行"仓库内无出处；append-only 基本成立（但 auto-compaction 会重写历史）；serve 无守护属实。仅 ProviderCapabilityReport 能力矩阵（API 语义）可借 |
| B-16 | DashScope 仅 json_object 无 json_schema | 证据不足（维持 🟡） | 官方 API 行为，本次静态审查范围外；设计上按"prompt+后置校验"兜底不变，落地前以当期官方文档复核 |
| B-17 | XiYan-SQL 路线/M-Schema/QwenCoder | **重写（组合）** | ① XiYan-SQL 仓库**无任何代码**（纯 README/论文导航页，pipeline 只是论文方法论）；② M-Schema 支持（330 行可 vendoring，但无 __init__.py 不能规范 pip 引入，且 **Examples 抽 5 个真实列值进 prompt 无脱敏**，须加列级开关）；③ xiyan_mcp_server「可作现成组件」被推翻：**无只读守卫（engine.begin() 任意 SQL 自动 COMMIT）、resource 存在 f-string SQL 注入、全源码无鉴权、类名 HITLSQLDatabase 名不副实无任何人工确认**——仅 get_data 契约与 sql_fix 重试循环可借鉴 |
| B-18 | ChatQwen（langchain-qwq）适配路径 | 证据不足（且随决策失效） | 文档级；V2 决策不引 LangGraph 运行时，该适配路径不再承重 |
| B-19 | AgentScope 1.0 ReAct/并行工具 | **重写** | 主线已是 **2.0.4dev，ReActAgent 类已删除**（统一 Agent 类）；并行是**条件性分批**（按 is_concurrency_safe 切 concurrent/sequential）。**新发现（升级其地位）**：AgentScope 2.0 的工具契约（ToolBase 带 input_schema/is_concurrency_safe/**is_read_only**/is_external_tool 元数据）、PermissionEngine + 工具调用状态机（ASKING 挂起→确认可改参→规则沉淀）、单一 pydantic AgentState——**恰是企业需要的三块，比 Qwen-Agent 契约强**；但 2.0.4dev 迭代期 + 捆绑 anthropic/dashscope/openai 硬依赖 → 只借契约与实现，不引依赖 |

### 1.3 落地设计主张

| # | 主张 | 裁决 | 说明 |
|---|---|---|---|
| C-10 | 会话历史 + HITL 检查点统一落 Redis | **推翻** | 见 A-4。V2 分工：Redis=会话热态/限流/去重/锁（**新增基建**）；RDS=durable run/审批/审计/长期记忆 |
| C-11 | 钉钉 Stream 多副本分发 P0 必解 | **重写（问题比 PDF 大）** | Stream 是"连接分担"模型（同 clientId 多连接分摊消息，`dingtalk_stream_runner.py:20-23` 注释明证）。但 Repo 的单实例假设**不止 Stream**：msgId 去重、session_store、AWAITING_COMMENT、access_token 缓存、四层限流、成本熔断**全部进程内内存**，Dockerfile 钉死 --workers 1。解法不是"粘性路由"而是**状态外置（Redis）后无粘性多实例**——状态外置了，连接分担反而是天然的负载均衡 |
| C-12 | 模型多档路由（类别→Qwen 三档） | 支持（扩展） | 扩展为境内多 provider 路由；OmO fallbackChain 双层结构只借数据模型（license 限制） |
| C-13/14 | 上下文压缩五级管线 / 子 Agent 摘要回传 | 支持（降级为 P2 按需） | Repo 当前 10 轮滑窗，先补结构化摘要即可；五级 shaper 是长任务优化非 P0。压缩实现参照 Hermes ContextCompressor（防注入边界分隔符）与 Qwen-Agent 分级截断 |
| C-15 | state 作用域 user:/dept:/temp:（仿 ADK） | 支持（须补完整数据模型） | PDF 停在一行提及；V2 交付分层记忆数据模型（B 模块），ADK 拆桶/合并 30 行可移植 + 三表分层 schema 直接参照 |
| C-16 | deny-first / 授权隔离正交 / 审批+沙箱叠加 | 支持 | 与 Repo fail-closed 纪律同构；落到 Policy Engine 设计 |
| C-17 | HITL 三态卡片 | 支持（语义修正） | 三态=APPROVED/REJECTED/**EDITED**（改参重建 tool_call）；补第四种处置：REJECTED-终止 与 REJECTED-回喂模型 分开建模 |
| C-18 | U8 写回经附属库/中间表 + 钉钉审批流；外部写在 interrupt 之后 | 支持（U8 无 API 属业务口径，Repo 无反证） | Repo 佐证 U8 目前只是文档语料；"写在中断后+幂等"与 LangGraph 源码语义一致 |
| C-19 | Text-to-SQL 只读守卫（AST/SELECT-only/白名单/部门 WHERE） | 支持（升级为 K 模块语义层） | xiyan-mcp 反例证明"没有守卫的 NL2SQL 组件不可接入"；V2 要求语义层：只读账号（Repo 四账号体系现成）+ 视图白名单 + 行级部门过滤 + EXPLAIN/LIMIT 预算 |
| C-20 | KIE：Qwen-VL 自定义 schema / DocMind 兜底 | 证据不足（文档级，维持设计推断） | Repo 已有 VLM 链路素材（vlm_endpoint/ocr_client/vlm_retry）可复用为 KIE 工具底座 |
| C-21 | 复用「251 金标集 + release gate」 | 支持（附带线上状态修正） | `eval_harness/goldset/golden_full.json` 251 例实存（另有 76/338 两档）；**但 release gate 自标 DRAFT 未接 CI**——"复用"前先把门禁闭环 |
| C-22 | P0 编排壳→P1 只读→P2 HITL→P3 生成→P4 闭环 | 支持（重排内容） | 顺序正确；但 P0 内容重写：不是"Qwen-Agent 起循环"，而是**接口边界 + Redis 基建 + durable 表**（详见 v2 §9） |

---

## 2. 模块级差距表（§4 A–N，含七选一状态判定）

图例：报告状态=PDF 覆盖程度；Repo 状态=✅代码存在/⚪推断/❌缺失。

| 模块 | 报告状态 | Repo 状态 | 关键证据 | 缺口 | 优先级 | **状态判定（七选一）** |
|---|---|---|---|---|---|---|
| A Runtime 边界 | 有方向无接缝（推荐 Qwen-Agent 为核心，未定义 Adapter 边界） | ❌ 无任何 Agent 层；Qwen-Agent **未在用**（依赖/import 零命中） | tools.json：LLM payload 无 tools 字段 | AgentLoop/ExecutionContext/EnterpriseTool 接口全缺 | **P0** | **新建独立模块**（自研轻量 AgentLoop + AgentLoop Adapter 接口；Qwen-Agent 降为可选适配对象） |
| B Memory/State/Durable | **最薄**（一行 Redis + 一行 state 作用域） | 会话=进程内 LRU；durable run ❌；长期记忆 ❌；Redis ❌ | session.json 全组证据 | 分层记忆数据模型、durable 表族、Redis 基建全缺 | **P0** | **新建独立模块**（Session Memory 迁 Redis=对 session_store 的替换性小改；durable/审批表族全新建；长期记忆仅定接口延后实现） |
| C Tool Contract/Registry | 仅"注册为 BaseTool"一句 | ❌ 从零；素材件分散成熟（幂等状态机/重试/熔断） | tools.json | 契约字段（risk/permission/idempotency/approval/owner）全缺 | **P0** | **新建独立模块**（契约参照 AgentScope ToolBase 元数据 + LangGraph/OpenAI 语义；禁直接依赖 BaseTool） |
| D Policy Engine | 只有 deny-first 理念 | 检索 ACL 单点成熟；操作守卫 20+ 端点手写分散 | authz.json | 统一"操作×资源×风险"裁决点缺失 | **P0** | **新建独立模块**（数据面复用 retriever ACL + kb_authz 白名单单一来源，不建第二套 ACL 数据） |
| E Durable Workflow | 未覆盖 | 摄取侧有幂等重入+outbox 模式；在线侧无状态机 | tools.json/db.json | 确定性流程（对账/补偿/多级审批）宿主缺失 | P1 | **小幅扩展现有实现**（沉淀 outbox/幂等重入为通用件；**不引 Temporal**——当前规模自研状态机+DataWorks 已够） |
| F Model Gateway | 有路由理念，无接口 | ❌ 4+ 处手写 HTTP；dashscope SDK 死依赖；config 残留 Gemini | tools.json/rag.json | ModelProvider 接口、路由、熔断、token 记账全缺 | **P0** | **新建独立模块**（收敛既有 vlm_endpoint/embedding_client/http_session 素材） |
| G Prompt/配置版本 | 未覆盖 | prompt 硬编码于 llm_generator 常量；~240 个环境变量读取分裂 | rag.json/inventory.json | 版本/回滚/运行重现全缺 | P2 | **延后建设**（P0 只做最小闭环：prompt 常量外置 + 版本号写入 agent_run 记录） |
| H Observability/Eval | 只提"复用金标集" | 评测资产厚（L0-L6/251 金标/judge/基线门）；token 不落库；无 OTel | obs.json | run/tool/approval trace 表、token 记账、门禁闭环 | P1 | **小幅扩展现有实现**（评测复用为主 + 新增 Agent trace 表族；release gate 接入发布流程） |
| I Reliability | 零散提及 | 素材分散成熟（vlm_retry/embedding 退避/cost_breaker/对账器）；钉钉出站零重试；无队列/死信 | dingtalk.json/tools.json | 统一执行中间件（超时/重试/幂等/熔断）缺失 | P1 | **小幅扩展现有实现**（把分散素材沉淀为工具执行中间件） |
| J 安全/Secrets | OpenClaw 教训层面 | env_guard/prod_access/账号体系/gitleaks 成熟；审计 fail-open；卡片回调无签名 | authz.json/db.json/dingtalk.json | 审批信道隔离、审计 fail-closed、回调加固 | P0（随 D） | **小幅扩展现有实现** |
| K 数据契约/语义层 | 停在"AST 校验" | ❌ 无任何 Text-to-SQL 代码；四账号体系与只读会话守卫是现成地基 | tools.json/db.json | 语义层（视图/指标/RLS/预算）全缺 | P1 | **新建独立模块**（M-Schema vendoring；xiyan-mcp 不采用，仅借 tool 契约） |
| L 部署/扩缩容 | 提出 Stream 分发问题 | 单实例单 worker 硬约束；无 CD；运维任务在个人 Mac | inventory.json/api.json | 状态外置→多实例、部署流水线 | **P0（前置）** | **小幅扩展现有实现**（Redis 外置状态 + 镜像 CD；不需要 leader election/粘性路由） |
| M DevEx/CI-CD | 未覆盖 | CI 测试门健康（三阻塞 job）；迁移无工具、apply 不入库；无 CD | db.json/obs.json | 迁移工具化、发布流水线、canary | P1 | **小幅扩展现有实现** |
| N 治理/责任边界 | 未覆盖 | kb 域三角色+dept_admin_grant 已有；工具治理零 | authz.json/console.json | 工具 owner/风险等级变更/管理面分离 | P1 | **新建独立模块**（轻量：Registry 元数据 + 管理面独立路由与角色，复用控制台骨架） |

**明确"现在不建设"清单**：多 Agent 编排/GroupChat（无场景证据）；MCP Server 对外暴露（无消费方）；代码解释器/沙箱执行（首批工具无代码执行需求，沙箱推迟到有此类工具时再按 OpenClaw 校验清单建）；向量化长期记忆（先结构化，B 模块治理字段就位后再评估）；Temporal/工作流引擎（规模不匹配）；线上 A/B 流量分桶框架（先离线 A/B + 灰度发布）。

---

## 3. Top 10 缺口（全部映射到真实 Repo 证据）

| # | 缺口 | Repo 证据锚点 | 为什么挡住 Agent 底座 |
|---|---|---|---|
| 1 | **水平扩展硬瓶颈：全部热状态进程内、强制单 worker** | `Dockerfile:36-46`；session_store/rate_limiter/_seen_msg_ids/AWAITING_COMMENT/token 缓存全内存；`grep redis` 零命中 | Agent 长任务+审批回调要求多实例高可用；这是所有 P0 的前置条件 |
| 2 | **无任何工具层**：无抽象/Registry/function-calling | tools.json：LLM payload 从不带 tools；框架依赖零 | EnterpriseTool/Registry 从零设计（好处：无历史包袱，接缝干净） |
| 3 | **无 ModelProvider 抽象**：4+ 处手写 HTTP、dashscope SDK 死依赖、Gemini 残留配置 | `llm_generator.py:698`/`query_decomposer.py:97`/`spot_checker.py:114`/`pipeline_nodes.py:1158`；`config.py:244,664` | V2 硬约束 1（provider 无关仅境内）当前不满足；换模型/统一重试/审计逐处改 |
| 4 | **无 durable run/审批数据模型**：agent_run/agent_step/tool_invocation/approval_request/execution_receipt 零命中 | db.json 全仓 grep 证据 | HITL 跨天审批、崩溃恢复、审计追溯全部无处落 |
| 5 | **记忆断层**：上下文重启即失忆、无摘要、无长期记忆、conversation_id 与 session_id 双轨脱节 | `session_store.py`（30min TTL）；`useAsk.ts:386`（恢复不回传历史）；RDS 有全量历史但不用于重建 | Agent 多轮/长任务的地基；PDF 最薄处 |
| 6 | **Policy Engine 缺失**：检索 ACL 单点之外，操作守卫 20+ 端点手写 | authz.json："文档 ACL 与工具 ACL 是否统一模型 ⚪" | 工具调用的 deny-first 统一裁决无处挂；新端点漏防无框架保护 |
| 7 | **审计不可靠且不可见**：fail-open 写入、只写不读、token 成本不落库 | `audit_log.py:13`（PROD-RO 被拦即吞）；无审计读 API/页面；schema 无 token 列 | 高风险工具执行的审计必须 fail-closed 且可查；成本无法按部门归集 |
| 8 | **可靠性中间件缺失**：钉钉出站零重试零熔断、问答链路无幂等键、裸 daemon 线程无恢复 | dingtalk.json 超时/重试组证据；`_process_rag_query` threading.Thread | 工具执行的超时/重试/幂等/补偿要统一，不能延续手写散布 |
| 9 | **迁移与部署治理**：迁移 apply 脚本 gitignored 不可审计、CI 无 CD、部署手工 zip、运维任务在个人 Mac launchd | `schema/README.md:9-13`；`.gitignore:22`；`deploy/*.plist` | Agent 表族要上线，迁移与发布不先工具化就是在沙地上盖楼 |
| 10 | **评测门禁未闭环**：资产厚但 release gate DRAFT 未接 CI、无 Agent 维度评测 | `deploy/eval_release_gate.sh:1`（DRAFT）；`ci.yml:7-8` 排除 live 层 | P1 只读 Agent 上线需要"工具选择/参数/权限正确性"评测维度与挡板 |

---

## 4. 报告—代码不一致项（PDF 视角错位汇总）

1. **把已有当缺失**：钉钉 Stream 接入（PDF P0 要"接入"，Repo 已实现双模共核）；251 金标集与评测（PDF 当"复用点"一笔带过，实际是全 Repo 最厚资产）。
2. **把缺失当可用**：Redis 会话后端（PDF 直接设定为 L1 组件，Repo 零 Redis）；"工具按 BaseTool 注册即可"（Repo 连 function-calling 都没有，且该设计违反 V2 约束）。
3. **未见 Repo 的三入口现状**（钉钉 bot/console/小程序）与**单 worker 硬约束**——后者使"SAE 多副本分发"问题的范围远大于 Stream 一项。
4. **通篇零 Repo 引用**：PDF 是纯外部调研，所有"待建六模块"未对照现有代码的复用点（如审批状态机、outbox、四账号体系、fail-closed 纪律）。
