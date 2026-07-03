# 富岭 RAG / Agent 底座 · Repo 架构地图（V2 · 证据基线）

> 本文件为 Phase 0 事实扫描的结构化产物。所有结论均标注 `文件:行 · 符号 · 行为 · 限制`，并区分 `[代码存在]` / `[线上在跑]`。扫描全程只读,未修改任何富岭代码。
> 证据分级:✅ 源码已验证 · 🟡 官方文档 · ⚪ 工程推断 · ❌ 当前缺失 · ⚠️ 风险/冲突。

## 0. 一句话结论

富岭已是一套**成熟的企业级 RAG 服务**(FastAPI + HA3 混合检索 + 钉钉双通道 + 多部门 ACL + 摄取 DAG),但**没有任何 Agent/工具调用骨架**:无工具抽象、无 Registry、无 function-calling、无 agent loop、无运行时 MCP、Redis 0 接入。会话上下文是进程内 LRU(`--workers 1` 单点),不能支撑多步长任务。**横切件(ACL / 幂等 / outbox 补偿 / 审批状态机 / 审计 / 限流)非常齐全,Agent Runtime 应在其上"收编生长",而非另起炉灶。**

---

## 1. 模块总图

```
钉钉(Stream WSS / HTTP webhook) ┐                      ┌─ HA3/OpenSearch (混合检索)
小程序 / PC 控制台(Vite SPA) ────┼─→ FastAPI app ──────┼─ RDS MySQL ×2 (fuling_knowledge / fuling_operation)
                                 │   (api.py, 单例)     ├─ OSS (原文/图片)
                                 │                      ├─ DashScope (Qwen, 裸 HTTP)
                                 │                      └─ DataWorks (离线摄取调度)
        ┌────────────────────────┴───────────────────────┐
        │ serving 热路径: ask/stream/search/feedback      │  offline 摄取: dag_engine + dataworks_orchestrator
        │  → retriever(ACL filter) → llm_generator(RAG)   │   → RDS 行级状态机(SKIP LOCKED 认领/2h stale/retry≤3)
        │  → session_store(进程内 LRU) → qa_logger(审计)  │   → HA3 bulk 推送 + parity 对账
        └─────────────────────────────────────────────────┘
横切: current_identity(ACL) · rate_limiter(4层) · request_context(X-Request-Id) · audit_log(append-only) · env_guard(写守卫)
```

- ✅ 唯一 FastAPI app:`opensearch_pipeline/api.py:123` · `app = FastAPI(..., lifespan=_lifespan)`;其余均 `APIRouter`(`dingtalk_bot.py:76`、`routes/{kb_console,kb_access,contribution,console}.py`)。
- ✅ 启动:`Dockerfile:42-48` · uvicorn `--workers 1 --timeout-keep-alive 65`;`api.py:1826` · `main()` 本地 reload。**`--workers 1` 是硬约束**(注释明示:session_store / 去重 / AWAITING_COMMENT 均进程内内存,横扩需先迁 Redis)。[代码存在]
- ✅ Lifespan(`api.py:84-120`):调大 AnyIO 线程池 → 注册钉钉卡片 HTTP 回调 → 按 `DINGTALK_STREAM_MODE` 起 Stream WSS 客户端;均 fail-open 不阻断启动。
- **合计 58 条 HTTP 路由**:api.py 15 + dingtalk 2 + kb_console 17 + kb_access 11 + contribution 8 + console 5。

---

## 2. 核心调用链

### 2.1 问答链(serving 热路径)
```
POST /api/ask(/stream)  api.py:612 / :778
 → current_identity (Depends, api.py:321-363; 可选 Bearer, 匿名=仅 public)
 → _prepare_ask: session_store 取内存历史 + 客户端 req.history(显式则整体覆盖) api.py:579-588
 → retriever.retrieve_and_enrich:
     _build_permission_filter (ACL, retriever.py:399-427)
     → HA3 dense/sparse/BM25 混合 + rerank
     → _deny_revoked_cross_dept (跨部门命中回查 kb_access_request, retriever.py:456-528)
     → parent retrieval / step-card / image_refs 富化
 → llm_generator.generate_answer(_stream): 裸 HTTP → DashScope, 无 tools 字段
 → content_blocks_builder 图文定稿
 → session_store.append(SUCCESS 才入史) + qa_logger.log_qa_session(审计, PII 掩码)
```
- ⚠️ **LLM 仅作文本生成器**:`llm_generator.py` 请求体只有 messages/max_tokens/temperature,**无 tools/function**(`r05`);无"由模型决定调用什么"的分支。最接近"LLM 决策"的是 `query_decomposer.py`(启发式触发→LLM 严格 JSON 拆子查询,8s 超时失败即回退,`config.rag.multi_query_mode` 默认 off)——**受控子例程,非工具调用**。

### 2.2 身份 / ACL 链(读授权)
```
钉钉 userid ─→ user_role(SELECT, seeded 行优先) ─miss/过期─→ 钉钉 user/get + dept_id_list 全遍历
   → 部门中文名 → _DEPT_NAME_TO_GROUPS / _PRODUCTION_WORKSHOP_DEPTS(85节点快照→'production'伞组)
   → _normalize_dept_to_codes → 过 _VALID_ACL_GROUPS 白名单(10组) → acl_groups 组码列表
   ├─[小程序] issue_session_token(HMAC-SHA256, uid+acl_groups, 2h TTL)
   │     每请求 RAG_LIVE_ACL_REREAD 默认 on → DB 现查覆盖令牌组
   └─[机器人] 直接作 user_dept 传检索
读过滤: acl_groups → _build_permission_filter:
   public OR (dept_internal AND owner_dept∈owners)  [+flag] OR (dept_internal AND allowed_depts∈groups)
   → HA3 → _deny_revoked_cross_dept 实时复核 → 结果
```
- ✅ ACL 服务端生成,**绝不采信请求体身份字段**(`api.py:544-545`, `380-386`)。fail-closed 拒绝路径共 11 条(见 `r02` §7)。
- ✅ 读/写授权**结构性隔离**:`kb_authz.py:5-15` H1 三分铁律(read_groups / managed_owner_depts / grantable_owner_depts),写授权绝不 import 读扩展。
- ❌ `primary_dept` **全仓零命中**——无"主部门"概念,一律多组列表 `acl_groups`。
- ❌ 工具 ACL 不存在(无 function-calling ACL 概念);ACL 仅覆盖文档检索 + 知识库写。
- ⚠️ `document_acl_rule`(schema/001:35) 是**零代码引用死表**,docs 自认"长得像 ACL 权威但真权威在别处"——审计陷阱。
- ⚠️ Phase D 跨部门 ACL 全链受 `RAG_ALLOWED_DEPTS_ACL` 控制,**代码默认 off**(`config.py:307`);deploy/ 无该 env → 线上是否启用不可知。[待确认]

### 2.3 写授权 + 跨部门审批链
```
特权写接口 → _require_kb_console → resolve_kb_identity(DB现查 user_role.role + dept_admin_grant)
   → authorize_upload(硬拒 / 转 kb_admin 审批, kb_authz.py:208-266) → upload_token(uid绑定)
   → register 再次现查裁决(绝不信旧 token, kb_console.py:1247)
跨部门: dept_admin 申请(kb_access_request pending) → owner/kb_admin decide(FOR UPDATE + 单向状态机)
   ├ 同事务 enqueue_acl_projection(outbox, 失败整笔回滚) + materialize(best-effort) + 审计
   ├ 提交后 invalidate_deny_cache
   → 投影三层收敛: decide内联标脏 → outbox 定向drain → allowed_depts_reconcile 全扫兜底(200/轮双向)
   → 读侧: 授权放行等投影; 撤销拒绝不等投影(_deny_revoked_cross_dept 实时)
```

### 2.4 会话 / 状态链 ⚠️ P0 缺口区
```
真正喂 LLM 的上下文 = session_store.py (_LRUSessionStore: 进程内 OrderedDict + RLock)
   MAX_SESSIONS=500(LRU淘汰) · SESSION_TIMEOUT=1800s · MAX_HISTORY_TURNS=10
   截断=丢弃最旧(非摘要) · 重启即失忆 · --workers 1 单点
持久层 = qa_session_log / qa_conversation → append-only 审计+UI展示, 无读回→LLM 通路, 落库文本已 PII 掩码
```
- ❌ 无 conversation summary(LLM 摘要压缩)、❌ 无用户级长期记忆、❌ Redis 完全未接入(0 依赖/0 client,仅"将来迁 Redis"注释)。
- ⚠️ **当前会话历史不能支撑 Agent 多步长任务**:进程内 + 30min TTL + 500 上限 + 单点,任何重启/发布/扩容即断;无 tool-call/中间步骤的持久载体。

### 2.5 摄取 durable 链(仅服务摄取,不服务问答)
```
dag_engine(内存 DAG, 无持久化) + dag_definitions(4条摄取 DAG)
   真正持久状态机 = RDS 行级状态列:
     Stage-2 认领: SELECT ... FOR UPDATE OF dv SKIP LOCKED (dataworks_orchestrator.py:181-223)
     失效锁接管: LOADING/PROCESSING >2h → FAILED+retry_count+1, 毒文档 3 次停 FAILED
     Stage-3 乐观锁三步兜底(pipeline_nodes.py:5051-5161) + drain 循环 no-progress 守卫
   pipeline_run: run 级 header(provenance, 非 checkpoint, 无中间产物指针)
   ingestion_resume: 只读恢复报告 —— "从当前 RDS 状态重跑", 非 run 级 checkpoint 恢复
```
- ✅ **这是现成的"至少一次 + 幂等重入"work-queue 模式**,可直接移植为 Agent 步骤/任务表的执行语义(见 `report-gap-analysis` B 模块)。

---

## 3. 钉钉接入(HITL 卡片底座已存在)

- ✅ 双入口收敛同一同步核心:HTTP `POST /dingtalk/webhook`(签名校验,`dingtalk_bot.py:793`)+ Stream `_BotMessageHandler`(`dingtalk_stream_runner.py:108`)→ `_process_webhook_body`(`dingtalk_bot.py:836`)。
- ✅ **互动卡片回调框架已齐备**(HITL 审批卡的底座):HTTP `POST /dingtalk/card/callback`(`dingtalk_bot.py:923`)+ Stream `Card_Callback_Router_Topic`,收敛 `_process_card_callback_body`(`dingtalk_bot.py:1012-1110`);含回调地址注册、callbackType 动态路由(Stream/HTTP)、message_id 归属防伪造校验、ACK-only 铁律(响应不带 cardData)。
- ⚠️ **但 dispatch 仅覆盖反馈域动作**(赞/踩/转人工/补充原因),未识别 action 直接 ACK 丢弃(`:1109`)。做 HITL 审批卡需:① dispatch 增审批分支;② HTTP 通道无签名校验(apiSecret 未验证)→ 审批写操作应走 Stream 或补签名;③ 归属校验 fail-open 对审批需改 fail-closed。
- ⚠️ **多副本无锁/无租约/无粘性**(❌ 未找到):Stream 同 clientId 所有连接分担消息 → 起 N 实例=事实多活消费,但进程内会话/去重/AWAITING_COMMENT 互不可见 → 错乱。**"单活"是部署约定(`--workers 1` + README 宣称 SAE 单实例),非代码保证。** [待确认:SAE 实际副本数、`DINGTALK_STREAM_MODE` 生产值]

---

## 4. 数据库与外部依赖

- **两库同实例**(跨库 JOIN):`fuling_knowledge`(知识/管线)+ `fuling_operation`(问答运营);staging 强制 `_stg` 后缀(`config.py:607`)。
- **迁移机制 = 人肉纪律,非自动框架**:`schema/011_schema_migrations.sql` 台账表两库各一,但 `apply_migration_*.py` 全在 gitignored `scratch/`,**运行时零引用、无启动自动 apply**;迁移执行历史不可从仓库复现。多个文件"已 apply"仅注释宣称(007/015),005 文件内标注与 011 基线**自相矛盾**。[待确认:012/013/014/016 apply 状态需连生产查 schema_migrations]
- **连接池**:`db.py` 单池单例,serving/ingest 共用;autocommit=False 手动 commit(88 处)/rollback(55 处);三层写守卫(sim→prod 拒建池 / 声明式只读 / `GuardedDBConnection` 拦写)。
- **Agent 相关表全部 ❌ 未找到**:agent_run / agent_step / tool_invocation / approval_request 均无。语义最近者:`kb_access_request`(跨部门审批)、`review_task`(文档审核)、`kb_audit_log`(审计)、`qa_conversation`(会话元数据)。
- **外部依赖**:HA3/OpenSearch(混合检索)、DashScope(Qwen,裸 HTTP)、OSS(原文/图)、DataWorks(离线调度)、钉钉 OpenAPI。

---

## 5. 可观测 / 测试 / 评测底座(摘要,详见 `r07`/`r08`)

- ✅ trace:`request_context.py:51` 纯 ASGI X-Request-Id + ContextVar;审计:`audit_log.py:46` append-only kb_audit_log(fail-open,独立连接,trace_id=git_commit:bizdate)。
- ✅ 指标:`schema/004_observability_metrics.sql` pipeline_run 扩展 + qa_daily_metrics(SLO);qa_session_log 全字段落 retrieval/llm latency、model_name、content_blocks_json。
- ✅ 评测:eval_harness/ + tests/eval/(golden set);tests/ 覆盖广(80+ 测试文件,含 ACL/chunker/concurrency/prod-guard)。
- ⚠️ 缺 Agent 场景:工具调用 trace、审批 trace、E2E 完成率、token/成本按 run 归集均无(现有 cost_breaker 是 VLM 摄取预算,非 serving)。

---

## 6. 关键文件 · 符号索引

| 领域 | 文件 · 符号 |
|---|---|
| 服务入口 | `api.py:123` app · `:321` current_identity · `:389` _enforce_rate_limit · `:778` ask_stream(SSE) |
| RAG 检索 | `retriever.py:399` _build_permission_filter · `:456` _deny_revoked_cross_dept · `answer_flow.py:97` build_qa_log_kwargs |
| 生成 | `llm_generator.py`(裸 HTTP DashScope, 无 tools) · `query_decomposer.py`(受控 LLM 子例程) |
| 身份/ACL | `dingtalk_identity.py:284` _resolve_user_dept_live · `:691` resolve_kb_identity · `kb_authz.py:208` authorize_upload · `access_grants.py:29` resolve_allowed_depts · `auth_token.py:145` issue_session_token |
| 会话/状态 | `session_store.py:44` _LRUSessionStore · `qa_logger.py:142` log_qa_session |
| durable 摄取 | `dataworks_orchestrator.py:181` SKIP LOCKED 认领 · `:564` _reset_stale_stage2_locks · `pipeline_nodes.py:5051` node_acquire_index_lock · `pipeline_run.py:26` run_start · `ingestion_resume.py:29` build_resume_report |
| 钉钉 | `dingtalk_stream_runner.py:77` start_stream_client · `dingtalk_bot.py:1012` _process_card_callback_body · `dingtalk_card.py:299` callbackType 路由 |
| 工具接入(雏形) | `extraction/unified_extractor.py`(按 key 分发,最像 tool dispatch) · `dag_engine.py:31` DAGNode(status/duration/error/result) · `extraction/schema.py` ExtractionResult |
| 横切 | `rate_limiter.py:170` ServingRateLimiter(4层) · `request_context.py:51` RequestIdMiddleware · `audit_log.py:46` · `env_guard.py:173` assert_destructive_write_allowed · schema/009 outbox |

---

## 7. 待确认清单(线上状态无法从仓库验证)

1. flag 生产值:`RAG_ALLOWED_DEPTS_ACL`(默认 off,Phase D 全链)、`RAG_CONVERSATION_HISTORY`(默认 off)、`RAG_QA_FACT_JOIN`(off)、`RAG_LIVE_ACL_REREAD`(默认 on)、`DINGTALK_STREAM_MODE`(默认 off)。
2. schema 迁移 apply 状态:006/008/009/012/013/014/016 是否已 apply 至生产(需连库查 schema_migrations)。
3. SAE 实际实例数(README 宣称单实例,非源码验证)、SLB 前置、灰度路由。
4. HA3 是否已加 `allowed_depts` MULTI_STRING 字段(引擎侧变更不在仓内)。
5. `dept_admin_grant`/`user_role.role` seed 数据(谁是 kb_admin/dept_admin,在 DB 不在仓)。
6. 钉钉开发者控制台推送模式当前是 HTTP 还是 Stream;`dingtalk-stream` SDK 实际版本与重连行为。
