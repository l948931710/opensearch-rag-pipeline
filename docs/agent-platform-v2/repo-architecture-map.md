# 富岭 Repo 架构地图（repo-architecture-map.md）

> Phase 0 事实扫描产物 · 基线：`opensearch-rag-pipeline` @ `7c704ce`（branch `claude/agent-platform-v2-audit-jtu5ha`，与 main 同源）· 2026-07-03
> 证据纪律：所有 ✅ 均区分 `[代码存在]` / `[线上在跑]`。**本仓库无部署流水线与生产环境变量文件，全部结论最高只能定级 `[代码存在]`**；线上在跑仅有间接证据（生产实例指纹硬编码 `config.py:27-31`、运营周报 PDF、注释中的生产事故记录），单列于 §9 待确认清单。

---

## 1. 总览：这是什么系统

单体 monorepo 的**纯 RAG 问答流水线**——没有任何 Agent/工具调用层。规模实测（wc）：

| 目录 | 职责 | 规模 |
|---|---|---|
| `opensearch_pipeline/` | 后端主包（摄取 DAG + 检索生成 + 钉钉机器人 + 控制台 API），**扁平结构** 60+ 模块同级，仅 `routes/` `extraction/` `webconsole/` 三个子目录 | 79 py · 41,629 行（最大热点 `pipeline_nodes.py` 7,215 行） |
| `tests/` | pytest（CI 阻塞门，simulate 模式） | 162 py · 44,589 行（**超过主包**） |
| `eval_harness/` | L0–L6 七层评测 + golden set + judge | 42 py · 8,958 行 |
| `scripts/` / `dataworks_nodes/` | 运维审计脚本 / DataWorks 批处理节点 ×8 | 27 py + 8 py |
| `console-app/` | Vue3+Vite+Pinia 控制台 SPA | 88 ts/vue · 8,863 行 |
| `fuling-rag-miniapp/` | 钉钉小程序（员工问答第二入口） | ~2,000 行 js |
| `schema/` | RDS DDL 手写迁移 001–016（19 个 SQL，含 3 对冲突编号） | — |
| `deploy/` | macOS launchd plist + 监控 Dockerfile（占位符未完成） | — |

**运行时进程拓扑（实测，仅两类半）：**

1. **SAE 在线服务**：`Dockerfile:42-48` `uvicorn opensearch_pipeline.api:app --workers 1`——**单 worker 是硬编码约束**（注释明示：session_store/限流为进程内内存，禁多 worker）。钉钉 Stream 客户端是**同一进程内的守护线程**（`api.py:112-113` → `dingtalk_stream_runner.py:49-58`），不是独立网关进程。
2. **DataWorks 批处理**：`dataworks_nodes/` 8 个节点脚本（stage1-3 摄取 + 注册/扫描/保留/健康监控），主入口 `dataworks_orchestrator.py`（836 行，`--stage/--bizdate/--resume`）。调度配置在阿里云控制台，仓内不可验证。
3. **⚠️ 运维定时任务跑在开发者个人 Mac 的 launchd 上**：`deploy/com.fuling.{ops-monitor,qa-rollup,qa-weekly-report}.plist`——生产对账/日指标/周报依赖个人笔记本在线（plist 注释自认"laptop sleeps → 次日唤醒补跑"）；DataWorks 化镜像 `deploy/dataworks_monitor.Dockerfile:18` BASE 还是 `<FILL_EXACT_TAG_FROM_CONSOLE>` 占位符。

**外部依赖（全部阿里云栈 + 钉钉）**：HA3 向量引擎（`alibabacloud-ha3engine-vector`）、RDS MySQL（pymysql+DBUtils）、OSS（oss2）、DashScope（LLM/embedding/VL/rerank，**全部手写 HTTP，SDK 声明了但零 import**）、钉钉（dingtalk-stream WSS + REST 卡片 API）。**无 Redis、无消息队列**（`grep import redis` 零命中）。

---

## 2. 核心调用链

### 2.1 问答链路（API 侧）

```
POST /api/ask(/stream)  api.py:612/778
  → current_identity()  api.py:321-363   Bearer HMAC 令牌解析；RAG_LIVE_ACL_REREAD 默认 on：读时实时重查 DB user_role 覆盖令牌内嵌 acl_groups
  → _enforce_rate_limit  api.py:389-421  进程内四层限流（ask 成本路径 fail-CLOSED 503）
  → _prepare_ask  api.py:561             会话合并（客户端 history 优先）+ user_dept=identity.acl_groups
  → retrieve_and_enrich  retriever.py:1866   【统一检索入口，可直接工具化】
      ├ get_query_embedding      retriever.py:90    DashScope native（dense+sparse），LRU 128
      ├ search_chunks            retriever.py:692   HA3 三路混合：kNN(dense+sparse) + BM25 TextQuery + weighted/rrf 融合
      │   └ _build_permission_filter  retriever.py:399  【ACL 单点】public OR (dept_internal AND owner_dept∈展开组)；空组→仅 public（fail-closed）
      ├ _deny_revoked_cross_dept retriever.py:456   Phase D 跨部门授权读侧实时复核（flag 默认关）
      ├ rerank_chunks            reranker.py:117    DashScope qwen3-rerank/vl-rerank 路由（默认关，fail-open）
      ├ stitch_neighbor_chunks   retriever.py:843   RDS chunk_meta ±1 邻居拼接（同权限校验 H4）
      ├ expand_step_context      retriever.py:1043  step-card/procedure_parent 四意图家族展开
      └ cosurface_doc_images     retriever.py:1553  补图 + image_refs（<<IMG:N>> 协议）
  → generate_answer(_stream)  llm_generator.py:684/750   HTTP 直拼 DashScope compatible-mode /chat/completions
      payload 仅 model/messages/max_tokens/temperature/enable_thinking —— 从不传 tools 字段
  → BackgroundTasks → log_qa_session  qa_logger.py:142   落 RDS qa_session_log（PII 掩码，失败吞异常不阻断）
```

### 2.2 钉钉链路（双模共核）

```
钉钉消息 → [HTTP] POST /dingtalk/webhook (dingtalk_bot.py:793, HMAC 验签 timestamp-only 300s 窗)
        → [Stream] dingtalk_stream_runner.py (官方 dingtalk-stream SDK 出站 WSS，DINGTALK_STREAM_MODE 默认关)
        两模共用 _process_webhook_body  dingtalk_bot.py:836
  → _is_duplicate_msg  :132   msgId 进程内 TTL dict 去重（多副本即失效，注释自认）
  → ack「正在查询」→ threading.Thread(_process_rag_query)   裸 daemon 线程，无队列无并发上限
  → _resolve_user_dept(sender_staff_id)  dingtalk_identity.py:261   逐消息服务端解析部门→组码（90s 缓存）
  → retrieve_and_enrich → 三级降级回复：流式 AI 卡片(PUT /card/streaming 打字机) → 成品卡片 → Markdown
  → log_qa_session 落库
卡片回调 → /dingtalk/card/callback  dingtalk_bot.py:923   ⚠️ 不验 apiSecret 签名（注释自认），靠 message_id 归属校验兜底（DB 异常 fail-open）
```

### 2.3 摄取链路（离线）

OSS 上传 → `register_new_files` → DataWorks stage1-3（`dataworks_orchestrator.py` 幂等重入，`dag_engine.py` 249 行 Kahn 拓扑仅本地模拟用）→ chunker/extraction（OCR/VLM，vlm_retry compress-on-retry + cost_breaker 三道闸）→ embedding（native dense+sparse，指数退避）→ HA3 + RDS chunk_meta → `ha3_reconcile`/`reconcile` 只读对账。

---

## 3. 身份 / ACL 链路（成熟度最高的横切层）

| 环节 | 实现 | 状态 |
|---|---|---|
| 登录 | `POST /api/auth/dingtalk`（api.py:506）authCode→userid→服务端解析组→签发 HMAC 令牌（`auth_token.py:145-209`，typ=session/2h TTL/生产缺密钥直接 RuntimeError） | ✅[代码存在] |
| 身份→组映射 | `dingtalk_identity.py`：叶子部门名→组码硬编码快照（85 生产叶子，2026-06-21）+ user_role 缓存优先 + 钉钉 API 回退；未知部门 **fail-closed 仅 public** | ✅[代码存在]（快照需人工回灌 ⚠️） |
| acl_groups 生成 | **一律服务端**；请求体 `dept` 字段标注`[已废弃·服务端忽略]`（api.py:204-228）；落库 uid 绝不采信请求体（api.py:377-386） | ✅[代码存在] |
| 检索边界 | **单点** `retriever._build_permission_filter`（白名单归一 `_VALID_ACL_GROUPS`→空即仅 public）；HA3 filter 服务端拼接 + `_sanitize_ha3_filter_value` 防注入 | ✅[代码存在] |
| 撤销收敛 | 读时 live-reread（默认 on，45s TTL）+ 跨部门命中权威表实时复核 `_deny_revoked_cross_dept`（DB 异常丢弃全部跨部门命中，机密性优先） | ✅[代码存在]（**总开关 RAG_ALLOWED_DEPTS_ACL 代码默认 False**，线上取值不可证 ⚠️） |
| 写授权 | `kb_authz.py` 纯函数裁决（三角色 employee/dept_admin/kb_admin），**与读扩展结构性隔离**（模块注释:5-10 明示绝不 import 读侧推导写权）；管理判定 DB 现查不信令牌 role | ✅[代码存在] |
| 跨部门授权 | `kb_access_request`（pending/approved/rejected/revoked 单向状态机 + FOR UPDATE 幂等）→ `kb_acl_projection_outbox` → stage-3 drain 物化 allowed_depts 进 HA3 | ✅[代码存在] |
| 会话恢复权限 | **每请求/每消息重解析**（会话不冻结权限）——已符合 V2 评审原则 5 | ✅[代码存在] |

**关键判定：文档 ACL 与"工具/操作级 ACL"不是统一模型。** 检索走单点 filter；其余操作（resign-images、history、kb console 写、审批）是 **20+ 端点逐个手写守卫**，无集中式"操作×资源"策略引擎，新增端点漏加守卫无框架级保护。`primary_dept` 概念不存在（全仓零命中），多部门以组列表并集承载。

---

## 4. 会话 / 记忆链路（双轨设计，Agent 底座最大短板之一）

```
轨道一（LLM 上下文）：session_store.py 进程内存 LRU
  500 会话上限 / 30min TTL / 最近 10 轮滑窗 / RLock
  ⚠️ 重启即丢、多实例互不可见 —— Dockerfile 因此钉死 --workers 1
轨道二（审计/展示）：RDS fuling_operation.qa_session_log（append-only）
  conversation_id = 客户端逻辑会话 ID，仅归并展示用；qa_conversation.hidden_at 软删除
  ⚠️ 恢复旧会话【不】回读 RDS 重建 LLM 上下文（useAsk.ts:386-389 只传 id）——重启即失忆
```

- 无会话摘要/压缩（全仓无 summarize/compaction），超 10 轮直接截断
- 无 durable run/checkpoint（`ingestion_resume.py` 仅摄取管线幂等重跑，明言"does NOT resume a specific historical run"）；流式中断=整轮消失
- 无用户级长期记忆（preference/画像/user_profile 全仓零命中）；唯一"个性化"是全局热门问题聚合
- 服务端会话历史三端点存在但 flag `RAG_CONVERSATION_HISTORY` 代码默认 False，线上开启无证据
- 留存策略存在：`retention.py`（blob 6 月置 NULL / 行 18 月删，默认 dry-run）

---

## 5. 工具 / LLM 接入现状（Agent 底座从零起步的证据）

| 检查项 | 结论 | 证据 |
|---|---|---|
| 工具抽象 / Registry / function-calling | ❌ **全无** | 全库 Grep BaseTool/ToolRegistry/register_tool/function_call/tool_call 仅命中一个归档脚本；LLM payload 从不带 tools |
| Qwen-Agent / LangChain / LangGraph / MCP | ❌ **零依赖零 import** | requirements/pyproject 无；import 级 Grep 0 命中 |
| Text-to-SQL / U8 / 库存 / 订单 / KIE 工具 | ❌ 无代码无占位 | 相关词仅出现在语料标题与测试 fixture；**U8 现状=把操作手册当文档检索** |
| ModelProvider 抽象 | ❌ 无 | chat 至少 4 处各自手写 HTTP（llm_generator/query_decomposer/spot_checker/pipeline_nodes）；`dashscope` SDK 是**死依赖**（声明未 import）；config 残留 Gemini 模型名与 GEMINI_API_KEY 回退（config.py:244,664 ⚠️） |
| 工具化雏形（可复用素材） | ✅ 分散存在 | `vlm_endpoint.py`（VLM 端点路由收敛，最接近抽象层）；`embedding_client.py`（统一客户端+指数退避）；`reranker.py`（fail-open 降级）；`http_session.py`（共享连接池） |
| 幂等/重试/熔断/审批"素材件" | ✅ 成熟但分散 | contribution/kb_access 幂等状态机+outbox（"绝不出现权威已改而无 outbox 行"）；vlm_retry compress-on-retry；cost_breaker 三道闸；rate_limiter 四层+全局日 LLM 熔断；多个只读对账器 |
| 类工具组件接口统一性 | ⚪ 不统一 | 检索=模块函数 / 解析=UnifiedExtractor 类 / OCR=OCRClient 类 / 生成=模块函数——三种形态混用，无共同 schema 与错误契约 |

---

## 6. 数据库与迁移

**RDS 单实例双生产库 + 双 staging**：`fuling_knowledge`（文档/chunk/ACL，18 表）+ `fuling_operation`（QA 日志/反馈/贡献/指标）；四账号体系（fuling_admin/fuling_ro/fuling_stg/fuling_metrics）+ 应用层双保险（`prod_access.py` 会话只读 + 当日 PROD-RW 令牌；`db.py` GuardedDBConnection 写守卫 + staging 双库同 `_stg` 校验）。

**已有表族**（✅ 代码存在，写入方均可定位）：document_meta/version/chunk_meta · qa_session_log/qa_conversation · user_feedback/escalation_ticket · kb_audit_log（**fail-open 写入，只写不读** ⚠️）· user_role/document_acl_rule/dept_admin_grant/kb_access_request/kb_acl_projection_outbox · kb_contribution · pipeline_run/qa_daily_metrics/qa_retrieved_doc。

**Agent 运行时表：一张都没有**——agent_run/agent_step/tool_invocation/approval_request/approval_decision/execution_receipt/event_log 全仓零命中（❌）。最接近的参照物是 `kb_access_request` 的人审状态机与 outbox 投影模式。

**迁移机制 ⚠️**：无 Alembic/Flyway；规程=手写 `schema/NNN_*.sql` + gitignored 的 `scratch/apply_migration_NNN.py`（不入库，git 无记录）+ 生产库 `schema_migrations` 台账（011 建，起因是 010 列漂移事故）。CI 零迁移步骤；README 编号台账已过期（称下一号 015 但 016 已存在）；012–016 是否已 apply 生产仓内不可查。

---

## 7. 可观测 / 测试 / 评测

- **日志**：stdlib logging 非结构化；自研 `X-Request-Id` ContextVar 中间件（RequestIdLogFilter 未在仓内安装，"留给部署侧"）
- **指标**：❌ 无 Prometheus/OTel/ARMS——"MySQL 表即指标"（qa_daily_metrics 日粒度）；**LLM token 用量解析后仅 logger.info，不落库**（schema 无 token 列）——成本无法按用户/部门归集 ⚠️
- **评测资产（高度可复用 ✅）**：L0–L6 七层评测编排（索引健康→检索排名 recall@k/MRR/nDCG→校准→答案质量→多模态→权限→延迟）；golden set 三档 76/**251**/338 例（PDF 所称"251 金标集"实存：`eval_harness/goldset/golden_full.json`）；Qwen 生成 × Claude 独立面板 judge（防自评）+ 校准；冻结基线回归门 + 5 套离线 A/B 脚本
- **⚠️ 但发布门禁未闭环**：`deploy/eval_release_gate.sh` 自标 DRAFT、需 VPC runner，**未接 CI**
- **CI**（`.github/workflows/ci.yml`）：test（全量 pytest simulate，阻塞）+ security（gitleaks 阻塞 + pip-audit 非阻塞）+ frontend（vitest+vue-tsc 阻塞）；**无 CD**——部署=手工双 zip 上传 SAE（README 自述有"选错包静默部署旧版"风险）

---

## 8. 前端 / 控制台

- `console-app/`：3 路由（问答 / `/manage` 四 tab / `/contribute`）。**已有三类审批队列**（文档版本审批 ApprovalQueue、跨部门访问 AccessRequestQueue、贡献审核 ContributionReviewQueue）+ 审批历史 + 授权/管理员任免——**Agent 工具审批队列可直接挂进 ManageView tab 骨架复用交互范式**
- ❌ 无审计页（kb_audit_log 只写无读 API）、无 Agent 运行状态页、无工具调用记录页、无反馈逐条工作台、无系统配置页
- 前端鉴权=展示门控（路由不拦截），enforcement 全在后端
- `fuling-rag-miniapp`：员工问答第二入口（chat/history/kb-docs），上传经 web-view 深链回 /console

---

## 9. 关键文件与符号索引（Agent 底座接缝点）

| 能力 | 文件 · 符号 | 接缝价值 |
|---|---|---|
| 检索工具化 | `retriever.py:1866 retrieve_and_enrich(query, top_k, user_dept, ...)` | 签名纯函数式，可直接封装为首个 EnterpriseTool；**user_dept 必须由可信侧注入，绝不可暴露给 LLM** |
| ACL 单点 | `retriever.py:399 _build_permission_filter` + `:372 _normalize_acl_groups` | Policy Engine 的数据面复用点，不重建第二套 |
| 写授权纯函数 | `kb_authz.py:208 authorize_upload` | 工具级 authorize_* 裁决函数的范式 |
| 身份解析 | `api.py:321 current_identity` / `dingtalk_identity.py:261 _resolve_user_dept` | ExecutionContext 的构造来源 |
| 审批状态机参照 | `routes/kb_access.py:679 _kb_access_decide`（FOR UPDATE + from_status 幂等 + 同事务 outbox） | approval_request 表与决策事务的现成模式 |
| 幂等/outbox | `access_grants.py:138 materialize_doc_allowed_depts` / `:203 enqueue/drain` | tool_invocation 幂等与副作用投影参照 |
| LLM 调用收敛起点 | `vlm_endpoint.py:22` / `embedding_client.py:55` / `http_session.py:46` | ModelProvider 网关的既有素材 |
| 会话存储替换点 | `session_store.py:44 _LRUSessionStore`（注释自认"生产可替换为 Redis"） | Session Memory 外置的唯一改造点 |
| 审计写入 | `audit_log.py:46 write_audit`（fail-open ⚠️） | Agent 审计需改 fail-closed 并补读取 API |
| 钉钉双模核 | `dingtalk_bot.py:836 _process_webhook_body` / `dingtalk_stream_runner.py:87` | Gateway 层复用点；Stream 是"连接分担"模型（同 clientId 多连接分摊消息） |
| 环境/生产守卫 | `env_guard.py` / `prod_access.py` / `config.py:27 PROD_FINGERPRINTS` | 工具执行环境限制的既有基建 |
| 限流/熔断 | `rate_limiter.py:1 LIMITER` / `extraction/cost_breaker.py` | token budget/工具配额的素材（均进程内，需外置） |

## 10. 待确认清单（仓内无法验证的线上状态）

1. SAE 应用配置/环境变量实值：`RAG_ALLOWED_DEPTS_ACL`（代码默认关——**若线上未开，审批通过≠检索放行**）、`RAG_CONVERSATION_HISTORY`、`RAG_RERANK_ENABLE`、`DINGTALK_STREAM_MODE`、`RAG_DINGTALK_STREAMING`
2. schema 012–016 是否已 apply 生产（台账在生产库内，仓内只有注释性基线）
3. DataWorks 各节点（含 retention）实际调度挂载状态
4. fuling_ro/fuling_stg 等账号是否已按 checklist 在控制台真实建立（文档 checklist 未勾选）
5. 小程序发布状态（README PROD_BASE_URL 为 TODO）
6. GitHub Actions 实际运行历史（仓内仅配置声明）
