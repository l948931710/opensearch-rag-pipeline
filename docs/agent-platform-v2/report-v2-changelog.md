# v1 → v2 结论变更清单（report-v2-changelog.md）

> 逐条对照 PDF v1（2026-07-02）原结论。变更类型：确认 / 推翻 / 重写 / 新增 / 删除 / 弱化。
> 证据列给锚点，全文见 open-source-code-review.md（开源）与 repo-architecture-map.md（Repo）。开源 commit：见 review 总表。

## 一、前提与选型

| # | v1 原结论 | v2 新结论 | 类型 | 变更原因 | 富岭 Repo 证据 | 开源源码证据 |
|---|---|---|---|---|---|---|
| 1 | 约束 C1「Qwen-only 硬禁非 Qwen」为第一评级标尺 | provider 无关、仅限境内、DashScope 默认；allowlist 是配置决策 | 重写 | 业主 V2 硬约束更新；凡仅以 C1 论证的评级失去唯一性依据 | 现有代码本无 provider 抽象（4 处手写 HTTP），"换 provider"瓶颈在自身 | — |
| 2 | 约束 C2「钉钉唯一入口」 | 钉钉主要入口、不被框住、性能优先 | 重写 | 业主更新；且 Repo 实际已有三入口 | console-app/ + fuling-rag-miniapp/ 实存 | — |
| 3 | 主循环选型 Qwen-Agent（唯一零适配，BaseTool/并行/MCP 齐备） | 自研事件流可挂起 AgentLoop 为默认；Qwen-Agent 降为可选 Adapter 对象+源码借鉴库 | 重写 | ①前提 C1 已变；②"并行调用"实为顺序 for 循环且默认关；③无任何 HITL 挂点；④dashscope 硬依赖+模型名含 qwen 默认路由公有云 | Qwen-Agent 未在用（依赖/import 零命中）——本就是空地规划 | `fncall_agent.py:94-105`（顺序 for）；`function_calling.py:63`（默认 False）；`llm/__init__.py:95-98`（默认公有云）；`setup.py`（硬依赖） |
| 4 | 待建工具全部注册为 Qwen-Agent BaseTool（"L3 全部注册为 BaseTool"） | EnterpriseTool 自有契约；BaseTool 仅经 <100 行可选 Adapter | 推翻 | 违反"工具协议独立于框架"硬约束；接缝已探明成本极低 | 工具层从零建，无迁移包袱 | `agent.py:212-237`（实例注入通道） |
| 5 | 单 Agent + 工具优先，多 Agent 仅问数候选/长报告局部 | 维持；且当前阶段多 Agent 一律不建 | 确认（收紧） | Repo 无多 Agent 需求证据；首批场景单上下文全覆盖 | tools.json 场景扫描 | — |
| 6 | 借鉴 LangGraph 语义自建，不整套引入 | 维持；明确采纳面=checkpoint 三表数据模型+interrupt/resume 协议+Store 接口语义 | 确认（具体化） | 源码证实运行时复杂度与耦合 | — | `pregel/_algo.py`（1300+ 行）；`checkpoint-postgres/base.py:43-91` |
| 7 | 交互网关借鉴 Hermes 单进程形态+钉钉 Stream 反向 WSS | Stream 属实且 Repo 已实现双模接入；"单进程"解释为逻辑单入口、物理多实例 | 重写 | v1 把已有当待建；单 worker 是要拆的形态 | `dingtalk_stream_runner.py`（官方 SDK 双模已实现）；`Dockerfile:42-48` | Hermes `plugins/platforms/dingtalk/adapter.py:153`（1707 行实存） |
| 8 | 百炼不能替代自建网关（钉钉仅 HTTP、无 HITL 节点） | 方向维持，但降级为非承重依据（文档级未重验） | 弱化 | 本次不重验官方文档；主链路自建已由其它证据独立支撑 | — | — |

## 二、HITL / 状态 / 记忆

| # | v1 原结论 | v2 新结论 | 类型 | 变更原因 | Repo 证据 | 开源证据 |
|---|---|---|---|---|---|---|
| 9 | 会话历史+HITL 检查点统一落 Redis（thread_id=钉钉会话） | RDS=durable 真相源（run/checkpoint/审批/审计）；Redis=新增基建，只承载可重建热态 | **推翻** | 三重证据：Repo 零 Redis；LangGraph 官方无 Redis checkpointer 且 Postgres 才是生产级；跨天审批要求持久+可审计 | `grep import redis` 零命中；`session_store.py:6` 仅注释 | langgraph libs/ 仅 InMemory/Postgres/Sqlite Saver；`cache/redis` 是节点缓存 |
| 10 | LangGraph interrupt()/Command(resume)，resume 值回注 | 确认，并升级为自研协议的复刻蓝本（含崩溃后可重复 resume） | 确认 | 全链路源码闭合 | — | `types.py:811-934`；`_io.py:74`；`_algo.py:1280-1345` |
| 11 | 恢复时节点从头重跑，interrupt 前副作用须幂等；外部写置于中断后 | 确认（铁律 4/6 的源码依据） | 确认 | — | — | `_runner.py:574-613`（常规写丢弃） |
| 12 | AsyncPostgresSaver 在 async 方法内用 threading.Lock，高并发隐患 | 实为 asyncio.Lock；实例级串行化与吞吐瓶颈成立，锁类型与机理描述错误 | 重写 | 源码核验 | — | `aio.py:43,59,363-403`；threading.Lock 仅同步版 `__init__.py:43,59` |
| 13 | OpenAI SDK：needsApproval → result.state.interruptions → approve/reject 粘性（引 JS 文档） | Python 版能力完整、API 形态不同：needs_approval / RunResult.interruptions / RunState.approve-reject(always_*) / RunState JSON 序列化恢复 | 重写 | PDF 引的是 JS；Python 源码另有形态且更完整（schema 版本化序列化） | — | `tool.py:1300`；`result.py:367,393`；`run_state.py:323-379,657`；`run.py:453-508` |
| 14 | Spring AI Alibaba 三态 APPROVED/REJECTED/MODIFIED | 实名 EDITED；EDITED=改参重建 tool_call（采纳）；REJECTED=理由回喂模型（采纳+另设硬终止）；新发现漏答默认放行风险（反向 fail-closed）与审批传输层为半成品 | 重写 | 源码核验 | — | `InterruptionMetadata.java:285-289`；`HumanInTheLoopHook.java:102-115`；`ConsoleInteractionHandler.java` |
| 15 | ADK state 四级作用域/Plugin 全局门控（仅设计灵感，本体绑 GCP 被排除） | 四级作用域与 Plugin 确认；**"绑 GCP"推翻**——核心零 google-cloud 依赖、DatabaseSessionService 支持 MySQL；升级为可移植参照（拆桶合并 30 行+三表 schema+PluginManager） | 推翻（部分） | 源码核验自托管路径 | — | `pyproject.toml`（无 google-cloud）；`database_session_service.py:88-93`；`schemas/v1.py`；`plugin_manager.py:275-322` |
| 16 | 记忆=Redis 会话 + state 作用域两笔（v1 最薄处） | 五层记忆模型+StateStore/MemoryService 接口+治理字段全量设计（v2 §6） | 新增 | 任务要求 P0 接缝；v1 空白 | session.json 全组缺口证据 | ADK 三表/LangGraph 三表/AgentScope AgentState/Hermes 压缩 |
| 17 | 上下文压缩五级 shaper+auto-compact（仿 Claude Code） | 保留方向，P2 按需；P1 先做 rolling summary；实现参照改为可读源码的 Hermes（防注入分隔符）+Qwen-Agent（分级截断） | 弱化（参照替换） | Claude Code 为 PDF 二手转述不可核；两个可核源码已覆盖需求 | 现状 10 轮硬截断 | `context_compressor.py:662+`；`llm/base.py:602-804` |
| 18 | 子 Agent 摘要回传/sidechain | 方向确认；当前不建，触发条件留档 | 弱化 | 无场景证据 | — | — |

## 三、工具与专项能力

| # | v1 原结论 | v2 新结论 | 类型 | 变更原因 | Repo 证据 | 开源证据 |
|---|---|---|---|---|---|---|
| 19 | Qwen-Agent 自带沙箱不达生产 | 确认（比 v1 更具体：容器无资源/网络限制、内核端口发布 0.0.0.0） | 确认 | 源码核验 | — | `python_executor.py:95-98`；`code_interpreter.py:259-264` |
| 20 | XiYan-SQL pipeline（多候选+selection）可复用 | 该仓库无任何代码（README/论文导航页）；仅论文方法论 | 重写 | 源码核验 | — | xiyan-sql 仓库文件清单（零 .py） |
| 21 | M-Schema 可直接采用 | 确认，附条件：vendoring（无 __init__.py 不能规范引入）+ Examples 抽真实列值须加脱敏开关 | 确认（加条件） | 源码核验 | RDS 双库地基现成 | `schema_engine.py fectch_distinct_values`；`utils.py examples_to_str` |
| 22 | xiyan_mcp_server 支持本地/DashScope 双模式，可作现成组件 | 双模式属实；"现成组件"推翻——无只读守卫（任意 SQL 自动 COMMIT）、resource SQL 注入、无鉴权、HITL 名不副实；仅借 get_data 契约+sql_fix 重试 | 重写 | 源码核验四项安全缺陷 | — | `db_source.py`（engine.begin()）；`server.py:98-108`（f-string 注入） |
| 23 | Text-to-SQL 只读守卫=AST 校验+SELECT-only+白名单+部门 WHERE | 升级为 8 层语义层守卫栈（物理只读账号/语义视图/脱敏 Schema/AST/SQL-HARDLINE/行级权限/预算/结果校验） | 重写（升级） | xiyan-mcp 反例证明单层不够；Repo 四账号体系可承载物理层 | `prod_access.py:79-115`；`environment_design.md:216-220` | 同上反例 |
| 24 | XiYanSQL-QwenCoder 可境内部署、SQL 环节优先专项模型 | 方向保留；新增前置条件：**仓库无 LICENSE 文件，权重许可以模型页为准，采用前核实** | 确认（加条件） | 供应链检查 | — | xiyan-qwencoder 仓库无 LICENSE |
| 25 | KIE：Qwen-VL 自定义 schema / DocMind 兜底 | 保留为设计推断（文档级未重验）；新增 Repo 复用点：vlm_endpoint/ocr_client/vlm_retry 即 KIE 工具底座 | 确认（弱化+落地） | DocMind 能力未重验；Repo 素材 v1 未发现 | `vlm_endpoint.py:22-103`；`vlm_retry.py:36-80` | — |
| 26 | DashScope 仅 json_object 无 json_schema，靠 prompt+后置校验 | 保留设计（prompt+pydantic 校验+重试），证据降级 🟡 待落地前复核 | 弱化 | API 行为超出静态审查范围 | `query_decomposer.py:112`（既有先例） | — |
| 27 | 模型多档路由：类别→Qwen Max/Plus/Turbo（仿 OmO，"用户可配 provider 优先级链"） | 类别→境内模型链（不限 Qwen 档位）；OmO 细节修正：用户层配不了 provider 优先级链（仅内置表有）；**license 非 OSI 禁源码搬运+默认 PostHog 遥测**——只借数据结构自写 | 重写 | 业主约束更新+源码/许可核验 | 模型名散于 env，无路由 | `model-requirement-types.ts`；`fallback-models.ts`；`telemetry-core/constants.ts`；LICENSE.md |
| 28 | OmO Team Mode 12 工具/4 并发/8 成员/500 轮/120 分钟硬限额 | 12 工具/4 并发/8 成员属实；500 轮/120 分钟是可调默认值，500 轮无运行时强制点 | 重写 | 源码核验 | —（多 Agent 本就不建） | `team-core/config.ts`；`create.ts:161-175` |

## 四、安全与网关

| # | v1 原结论 | v2 新结论 | 类型 | 变更原因 | Repo 证据 | 开源证据 |
|---|---|---|---|---|---|---|
| 29 | OpenClaw：审批同信道 CVE + 修复=移出信道+早退守卫 | 确认；补充修复形态细节（专用方法族+operator.admin scope+baseHash CAS+回归测试） | 确认 | 源码核验 | — | `nodes.ts:1319-1330`；`exec-approvals.ts:29-74,128-196` |
| 30 | OpenClaw 沙箱修复=208 行校验文件 | 文件现 435 行，范围扩至 network/seccomp/apparmor/保留路径 | 重写（细节） | 源码核验 | — | `validate-sandbox-security.ts`（435 行） |
| 31 | OpenClaw 信任决策分散在各层调用点 | 核心已收敛到三个统一边界（方法授权/工具策略管道/沙箱校验）；channel 侧审批鉴权仍分散 | 重写 | 对当前源码不再整体成立 | — | `server-methods.ts:234-271`；`tool-policy-pipeline.ts:127-215` |
| 32 | Hermes：DingTalk 渠道/fail-closed 审批/审批沙箱替代/DashScope 一等公民 | 四项确认；新增两坑：非交互上下文 AUTO-APPROVED fail-open 分支（须封死）；DashScope profile 默认国际站 endpoint（境内须覆写） | 确认（加警示） | 源码核验 | — | `approval.py:2003-2022`；`alibaba/__init__.py:10` |
| 33 | 钉钉 Stream 多副本分发 P0 必解（粘性/一致性哈希路由） | 问题范围更大（全部热态进程内、单 worker 硬约束）；解法=状态外置 Redis+连接分担天然均衡，**无需粘性路由/leader election** | 重写 | Repo 证据 | `Dockerfile:36-46`；`dingtalk_stream_runner.py:20-23`；session/限流/去重全内存 | — |
| 34 | Redis 会话后端（OpenAI SDK Redis sessions 印证） | Session Protocol 四方法语义采纳；RedisSession 对照自写；tracing 默认境外上传+敏感数据缺省 true 列为其依赖否决项 | 确认（加否决项） | 源码核验 | — | `redis_session.py`；`tracing/processors.py:34`；`run_config.py:41-44` |
| 35 | Gajae action_needed/reply 审批协议传输无关（v1 自标 🟡） | 确认并升级：协议+ActionRegistry（幂等键/first-valid-wins/迟到拒绝）为钉钉审批协议主要数据模型参照 | 确认（升级） | 源码核验超预期 | 现有卡片回调无此语义 | `protocol.rs:2-7`；`actions.rs` |
| 36 | Claw Code ~88 行极简循环/append-only/serve 无守护 | 最精简 207 行（88 行无出处）；append-only 基本成立（auto-compaction 会重写）；serve 属实；新增可借项 ProviderCapabilityReport | 重写 | 源码核验 | — | `claw-analog/lib.rs:1220-1425`；`providers/mod.rs` |
| 37 | 第一轮被 0-3 否决的论断「gajae 面向 Anthropic/OpenAI/Gemini/Grok、会话存 .gjc/」（v1：勿引用） | **该论断为真**（且实际支持面更广） | 推翻（对 v1 的否决的推翻） | 源码核验；v1 对抗验证误杀真论断——其否决结论同样需要源码复核 | — | `packages/ai/src/providers/`；`dirs.ts:23` |
| 38 | AgentScope 1.0 ReAct 主推、原生并行工具（secondary 源提取） | 主线 2.0.4dev、ReActAgent 已删；并行=按 is_concurrency_safe 条件分批；**地位上调**：工具契约/权限状态机/单对象状态为 EnterpriseTool 与自研 Loop 的主要参照 | 重写（升级） | 源码核验 | — | `_version.py`；`_agent.py:1148-1590`；`tool/_base.py` |

## 五、落地路径与评测

| # | v1 原结论 | v2 新结论 | 类型 | 变更原因 | Repo 证据 | 开源证据 |
|---|---|---|---|---|---|---|
| 39 | P0（1-2 周）=Qwen-Agent 起循环+RAG 首工具+Redis 会话+钉钉 Stream 接入 | P0（3-4 周）=状态外置多实例化+四件套接口+durable 表族+ModelGateway+CD；Stream 已有无需"接入"；循环自研 | 重写 | P0 重心从"壳"改为"接缝"；工期按实际改造量修正 | Stream 已实现；Redis 零基建 | — |
| 40 | P1 问数+抽取 → P2 HITL 写回 → P3 生成/子 Agent → P4 闭环观测 | 顺序确认；P2 明确"模拟执行不接真实写"，P3 才接 U8 staging+kill switch+canary；P4 并入治理/自助注册 | 确认（细化） | 与业主 §9 路线对齐 | — | — |
| 41 | Feedback 闭环复用「251 金标集+release gate」 | 金标集实存（76/251/338 三档）；**release gate 自标 DRAFT 未接 CI**——先闭环门禁再谈复用；评测新增 Agent 四维度 | 确认（加修正） | Repo 核验 | `eval_harness/goldset/`；`deploy/eval_release_gate.sh:1`；`ci.yml:7-8` | — |
| 42 | （v1 未涉及）迁移/部署治理、审计可见性、token 成本、治理边界、Prompt 版本、可靠性中间件 | 新增 M/N/G/H/I 五个模块设计（v2 §9-10） | 新增 | Repo 缺口证据驱动 | db.json/obs.json/console.json 组证据 | — |

**统计**：确认 11 · 重写 17 · 推翻 4（含对 v1 一项否决结论的推翻）· 弱化/降级 5 · 新增 5。
**元结论**：v1 的外部调研框架事实大体可靠（确认+细节重写为主），但（a）凡涉及"拿来即用"的可用性判断（xiyan-mcp、Redis checkpointer、BaseTool 直挂、ADK 排除）错误率高——**可用性必须源码级验证**；（b）v1 完全缺失 Repo 视角，把已有当待建、把待建当可用；（c）v1 自己的证据分级（✅/🟡/0-3 否决）不能直接采信，本次复核既推翻过它的 ✅ 细节也推翻过它的否决。
