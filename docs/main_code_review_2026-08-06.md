# Main vs origin/main 生产级代码审查

> 日期：2026-08-06（America/Los_Angeles）  
> 审查性质：只读、独立代码审查  
> 审查结论：**REVISE / NO-GO**  
> 文档状态：审查快照；不代表生产发布批准

> **📌 2026-08-06 逐条核查已完成** —— 结论与修复批次见
> [`main_code_review_verification_2026-08-06.md`](main_code_review_verification_2026-08-06.md)。
> 23 条：**属实 21 / 属实但需纠偏 2（§3.1、§3.6）/ 推翻 0**。
> 引用订正：§3.4 的 `schema/001_knowledge_base_schema.sql` **不存在**，真实文件是
> `schema/001_opensearch_pipeline.sql`（行号 124 与内容都对）；本报告基准是 `133ad84`，
> 而 HEAD 已到 `d967bee`（多 4 个提交），**其余行号普遍已漂移**，核查文档给了当前位置。

## 1. 执行摘要

不建议将当前 `main` 直接提升到生产。

本次冻结审查目标为：

- 审查提交：`133ad84af743fe293b95ae985a46dcdc4eec0c6c`
- 对比基线：`efac34260d3a6fbfc11dd443583e87df5978e947`
- `origin/main`：`efac34260d3a6fbfc11dd443583e87df5978e947`
- 分支状态：`main` 比 `origin/main` 超前 12 个提交
- Diff 规模：43 个文件，新增 3024 行，删除 380 行

确认的主要风险包括：

- 审批对象与最终摄取字节之间缺少默认开启、失败关闭的不可变绑定。
- OCR/VLM 付费调用上限大幅提高，但硬预算、熔断和所有调用路径尚未闭合。
- 新旧控制台的 300MB 上传能力发生漂移。
- 退役、恢复、拒绝三个操作之间存在审批状态机竞争和状态污染。
- 新一代 chunk 不是原子发布，中途失败会形成新旧版本混合服务。
- 图片 serving-version gate 已在 `133ad84` 落地并通过 65 个相关测试，但尚缺生产
  RDS/HA3 只读验证，并新增了可合并的 RDS authority 往返。
- 300MB 上传入口缺少压缩容器资源限制，存在压缩炸弹和进程资源耗尽风险。
- 全局 LLM 限额的异步恢复会丢失启动后增量。
- DingTalk 默认模式仍可能创建无界线程并绕过统一 admission。
- 默认检索会按请求创建多组子线程池，并在同一请求中多次独立 checkout RDS 连接。
- Stage-1 对每个已下载原件额外执行一次全文件 SHA-256 磁盘读取。
- 前端为侧栏红点拉取完整审核队列，贡献页可重复发起同一请求；路由空闲预取又会让普通员工下载管理页代码。
- 看板 TTL 缓存不淘汰过期条目且没有容量上限。
- 镜像 promotion 没有绑定当前精确 SHA 的完整 CI、安全、live-eval 和恢复证据。

深度安全扫描的结构化中间产物已经恢复：26 份 discovery worker 结果、20 份去重结果，以及
254 条 canonical candidate。扫描仍未生成最终验证报告或完成清单，而且扫描快照
`001fe9c` 比本报告审查提交早 5 个提交；因此这些记录只能作为待验证候选，不能视为安全通过。

## 2. 审查范围与证据边界

### 2.1 已覆盖

- `efac342..133ad84` 的完整 Git diff。
- API、认证、限流和 session 管理。
- KB 控制台、审批、退役、恢复和 ACL 管理。
- 上传、OSS 签名、抽取、OCR/VLM 漏斗和 DataWorks 摄取。
- chunk 版本发布、HA3 写入、RDS 权威状态和 reconciliation。
- 文本、图片和 cosurface 检索链路。
- Docker、CI、安全扫描、镜像 promotion 和 release-gate。
- 前端路由分包、预取、审核队列加载、流式渲染和当前构建产物大小。
- 在线检索的 RDS checkout、线程池、缓存，以及摄取原件的磁盘 I/O 热路径。
- 与上述路径相关的代码测试和发布文档。
- 深度安全扫描的工作者结果、去重结果、最终候选账本及会话完成状态；详见 §9。

### 2.2 未能证明

- 真实生产 RDS、HA3、OSS、DataWorks 和 SAE 的运行状态。
- 生产环境中各安全开关的最终取值。
- 当前精确 SHA 的认证 canary、故障恢复和回滚演练。
- 当前精确 SHA 的完整、干净工作树测试结果。
- 深度安全扫描的完整收敛结果。
- 生产流量下的 P50/P95/P99、线程数、RDS pool wait、HA3 arm latency、磁盘吞吐和前端网络 trace。

### 2.3 工作区污染边界

最终快照时 tracked 产品代码和测试已干净；审查过程中原先未提交的图片版本门已由外部流程
提交为 `133ad84`。工作区仍存在非本次审查产生的未跟踪文件，包括：

- `docs/evidence/rerank_subject_drift_20260727/`
- 两份 `baseline.json` 备份
- `test-results/`
- 本报告 `docs/main_code_review_2026-08-06.md`

这些未跟踪证据、备份和测试产物不进入审查提交；本报告自身也尚未提交。

## 3. 本次 diff 的行动项

### 3.1 [P1] 审批内容与实际摄取字节可以不一致

证据：

- `opensearch_pipeline/kb_upload.py:23-29`：签名和上传 token TTL 延长到 60 分钟。
- `opensearch_pipeline/config.py:470-475`：内容绑定默认关闭。
- `opensearch_pipeline/routes/kb_console.py:3129-3144`：只有 schema 探测成功才写入绑定列。
- `opensearch_pipeline/routes/kb_console.py:3437-3453`：schema 探测异常被转换为“不支持”，即 fail-open。
- `opensearch_pipeline/pipeline_nodes.py:152-166,653-655`：摄取路径仍可读取 key 的当前内容。
- `docs/ops/c8_approval_content_binding_signoff_2026-08-03.md:38-54`：生产 OSS versioning/lifecycle 尚未完成证明，并记录了 A→审批→覆盖为 B 的场景。

影响：

审批人确认的对象 A 可以在 TTL 内被同 key 的对象 B 覆盖，DataWorks 随后摄取 B。结果是审批审计与实际知识内容不一致。

要求：

- 审批记录绑定不可变 OSS version ID、ETag 或最终对象地址。
- 服务启动时强制验证绑定 schema 和 OSS 能力，不可把探测错误当成“不支持”。
- 在生产证明完成前，不应仅通过延长 TTL 扩大可变窗口。

### 3.2 [P1] 付费抽取扇出提高，但硬成本边界没有同步落地

证据：

- `opensearch_pipeline/config.py:305-306`：OCR 最大页数从 50 提高到 200。
- `opensearch_pipeline/config.py:659-662`：PDF native 页上限提高到 1000，图片页上限从 20 提高到 100。
- `opensearch_pipeline/config.py:317,999`：rebuild/cost breaker 默认关闭。
- `opensearch_pipeline/extraction/unified_extractor.py:2195-2213`：`RAG_FUNNEL_MAX_IMAGES` 默认为 0，即不设上限。
- `opensearch_pipeline/extraction/unified_extractor.py:2215-2237`：共享 breaker 仅在开关开启时生效。
- `opensearch_pipeline/extraction/unified_extractor.py:2747-2749`：page-OCR fallback 直接调用 OCR client，没有经过共享 breaker。
- `.env.example:131`：文档提到代码并不读取的 `RAG_REBUILD_COST_BREAKER`；代码实际读取 `RAG_REBUILD_ENABLED`。

影响：

批量摄取时单个文档可产生大量 OCR/VLM 调用，多个文档并行后会形成成本、延迟和上游配额事故。现有配置说明还可能让运营人员误以为 breaker 已启用。

要求：

- 增加默认开启且非零的单文档、单批次和每日预算。
- 同一预算必须覆盖 page OCR、图片漏斗 OCR 和 VLM 调用。
- 预算扣减必须在付费调用之前完成，并支持并发原子性。
- 修正环境变量文档；在硬边界落地前保留旧上限。

### 3.3 [P2] 新旧控制台上传能力不一致

证据：

- `console-app/src/lib/kb.ts:8`：新控制台上限为 300MB。
- `opensearch_pipeline/webconsole/console.html:211,438-439`：legacy 控制台仍硬限制 50MB。
- `opensearch_pipeline/webconsole/console.html:224`：legacy 请求超时仍为 25 分钟。
- `opensearch_pipeline/routes/console.py:103` 附近：`/console-legacy` 仍被公开提供。

影响：

从 legacy 入口进入的用户无法上传合法的 50–300MB 文件，并会收到与后端能力矛盾的错误信息。

要求：

- 两个客户端统一从 `/api/kb/config` 获取运行时上限和超时；或正式退役 legacy 路由。
- 增加跨客户端能力一致性测试。

### 3.4 [P2] 退役会污染普通版本的审批状态

证据：

- `schema/001_opensearch_pipeline.sql:124`：`approval_status` 默认 `PENDING`。
  （原文误写为 `001_knowledge_base_schema.sql`，该文件不存在；2026-08-06 核查订正。）
- `opensearch_pipeline/pipeline_nodes.py:475-482`：普通版本写入未显式设置审批状态。
- `opensearch_pipeline/dataworks_nodes/register_new_files.py:430-441`：另一普通写入路径也未显式设置审批状态。
- `opensearch_pipeline/routes/kb_console.py:3406-3408`：退役把所有 `PENDING` 改为 `WITHDRAWN`。
- `opensearch_pipeline/routes/kb_console.py:3542-3550`：恢复只重新打开 `PENDING_APPROVAL` 文档。

影响：

普通版本退役再恢复后可能永久保留错误的 `WITHDRAWN` 审批状态，导致 UI、审计或后续状态判断失真。

要求：

- 退役审批状态更新增加 `content_process_status='PENDING_APPROVAL'` 条件。
- 或为不需要审批的版本定义显式终态，并对历史数据执行受控 backfill。

### 3.5 [P2] 过期拒绝请求可以覆盖 WITHDRAWN

证据：

- `opensearch_pipeline/routes/kb_console.py:3319-3324`：reject 只以 `content_process_status=PENDING_APPROVAL` 为条件。
- 退役会更新 `approval_status`，但保留该 content status。
- `console-app/src/composables/useKb.ts:1336-1342`：客户端没有根据 `rejected: 0` 判断竞争失败。

影响：

旧页面或并发 reject 请求可以把已经撤回的审批改成 `REJECTED`。恢复逻辑随后无法按 WITHDRAWN 路径处理。

要求：

- reject SQL 同时要求 `approval_status='PENDING'`。
- 使用行版本或其他 optimistic lock。
- 更新行为 0 行时返回 409，客户端必须刷新服务端真值。

### 3.6 [P2] 退役和恢复后审批队列保持陈旧

证据：

- `console-app/src/composables/useKb.ts:1983-2006`：退役/恢复只调用 `loadDocs()`。
- 同文件批量操作路径也只刷新文档列表。
- `console-app/src/components/manage/ManageView.vue:113-121`：审批队列明确不轮询。

影响：

操作完成后，用户仍会看到已经处理的审批单，直到手工刷新或缓存失效，容易触发重复或过期操作。

要求：

- 所有改变审批可见性的写操作完成后调用 `loadApprovals(true)`。
- 单条和批量操作使用同一个刷新策略。

### 3.7 [P2] node ACL 的空共享列表错误回退到历史 legacy grants

证据：

- `console-app/src/components/manage/DocTable.vue:60-62`：仅当 `shared_labels.length > 0` 时使用 node 标签，否则回退 legacy grants。
- `opensearch_pipeline/routes/kb_access.py:532-588`：legacy grant 查询没有按当前 `acl_mode` 过滤。
- `opensearch_pipeline/routes/kb_access.py:153-158`：历史 legacy 行会保留作审计。

影响：

node 文档没有任何 node share 时，界面可能展示历史、仅供审计的 legacy 授权，使管理员误判当前共享范围。

要求：

- 显式按 `acl_mode` 分支。
- node 模式下将空 `shared_labels` 视为权威结果，不得回退 legacy 数据。

## 4. 跨模块生产阻断项

### 4.1 [P1] 新 generation 在完整完成前已经可服务

证据：

- `opensearch_pipeline/chunker.py:154`：新 chunk 默认 active。
- `opensearch_pipeline/pipeline_nodes.py:6700-6715`：写入时携带 active 状态。
- `opensearch_pipeline/dataworks_orchestrator.py:495-532`：Stage 3 按有限 chunk 批次推进，而非按完整文档 generation 原子处理。
- `opensearch_pipeline/pipeline_nodes.py:7355-7358`：完整性检查主要延迟旧版本停用。
- `opensearch_pipeline/pipeline_nodes.py:9112-9119`：尾部失败会标记 FAILED，但已经 active 的部分新 chunk 不会整体撤销。

影响：

摄取失败期间，查询会同时看到完整旧版本和部分新版本，导致引用、回答和版本审计不一致。

要求：

- 新 generation 以 staging/non-serving 状态写入。
- 完成 chunk、embedding、HA3 推送和数量校验后，再原子切换文档 serving pointer。
- 失败 generation 必须整体不可见且可安全重放。

### 4.2 [原 P1，`133ad84` 已实现；待线上证明] 图片 serving-version gate

证据：

- `opensearch_pipeline/retriever.py:966-1022`：`_resolve_serving_versions` 以最高完整
  INDEXED active 版本为权威，并对多 active、无完整版本的歧义状态 fail-closed。
- `opensearch_pipeline/retriever.py:1025-1113`：`_deny_stale_version_images` 对纯图片行丢行，
  对带图正文剥图保正文，并以 RDS chunk/version 数据裁决。
- 同文件 `:1522-1525,1626-1628,2538-2542,2994-2996`：本地 OS 回退、主命中、
  cosurface 和 expand 后四个消费点均接入版本门。
- `tests/test_image_version_invariant.py` 以及关联 cosurface/probe 测试已纳入提交；本次独立定向执行
  `tests/test_image_version_invariant.py tests/test_extract_tmp_lifecycle.py` 为 65 passed。

影响：

原缺陷会让旧版本中的敏感图片、已删除内容或错误版本图片出现在答案中，或者先发送到外部模型。
`133ad84` 已在代码层堵住四个已知消费点；剩余风险是生产 schema/数据形态、性能和真实 HA3/RDS
时序尚未验证，不能仅凭本地单测视为生产关闭。

要求：

- 对 `133ad84` 做生产只读 canary：覆盖正常单版本、双 active、部分 INDEXED、ACL 重投影和 retire 场景。
- 验证旧图既不会展示，也不会进入 VL rerank 的外部 egress payload。
- 采集新增版本门的 RDS query latency、pool wait 和每请求 checkout 数；按 §5.1 合并 authority reads。
- 完成线上证据前，本项状态保持“代码已实现、生产未证明”。

### 4.3 [P1] 300MB 上传入口缺少压缩容器资源限制

证据：

- `opensearch_pipeline/routes/kb_console.py:2800-2805`：签名只绑定调用者提供的 `Content-Type`。
- 没有发现 magic-byte、ZIP entry 数、展开后总大小或压缩比限制。
- `opensearch_pipeline/extraction/unified_extractor.py:1244,1401-1411,1756`：DOCX/XLSX/PPTX 在主抽取进程内直接打开。

影响：

授权上传者或取得签名 URL 的主体可以上传伪装成 Office 文件的压缩炸弹，引发 DataWorks/抽取进程 OOM、CPU 或临时磁盘耗尽。

要求：

- 在解析前验证文件魔数和容器结构。
- 限制 entry 数、单 entry 大小、总展开大小和压缩比。
- 对大文件解析使用隔离 worker、wall-clock timeout 和内存/磁盘资源限制。

### 4.4 [P1] 全局 LLM 限额异步恢复会丢失启动后增量

证据：

- `opensearch_pipeline/rate_limiter.py:367-373`：持久计数异步 seed，服务已可接收请求。
- `opensearch_pipeline/rate_limiter.py:555-573`：seed 通过 `max(memory, persisted)` 合并。

复现场景：

数据库已有 9 次、上限 10 次；进程启动后 seed 完成前接受 2 次请求，内存为 2；seed 得到 9 后合并为 9，第三次仍会被放行，而实际总量已经达到 12。

要求：

- 服务接收请求前同步完成 seed；或
- 独立保存 boot delta，并以 `persisted_base + boot_delta` 原子合并。

### 4.5 [P1] DingTalk 默认可创建无界线程并绕过 admission

证据：

- `opensearch_pipeline/dingtalk_bot.py:1703-1710`：代码注释明确记录无界 daemon thread 和 admission bypass。
- `opensearch_pipeline/dingtalk_bot.py:1719-1728`：worker 默认 0，admission 默认关闭。
- `opensearch_pipeline/dingtalk_bot.py:1815-1848`：默认分支为每个请求创建线程。
- `.env.example` 没有完整暴露对应生产安全开关。

影响：

认证用户突发请求可以耗尽线程、内存和 CPU，并绕过全局 LLM 成本限制。

要求：

- 生产默认使用有界 worker pool 和有限队列。
- admission 必须默认启用并作为启动检查项。
- 队列满时返回可观测的拒绝结果，而不是继续创建线程。

### 4.6 [P1] promotion 没有绑定当前 SHA 的完整发布证据

证据：

- `.github/workflows/image.yml:130-136`：promotion 只依赖 `build-smoke`。
- `.github/workflows/ci.yml:245-252`：baseline freshness 是 `continue-on-error`。
- `deploy/eval_release_gate.sh:2,41-42`：脚本仍标为 DRAFT，缺 baseline 时会跳过回归判断。
- `docs/ops/image_release_2026-08-06.md:1,19-25`：证明的是 `efac342` 镜像，而不是当前 `133ad84`。
- 同文档 `:6,69-76,88`：release-gate 被豁免或仍待补验。

影响：

人工批准 promotion 时，系统无法保证被提升镜像对应的精确 SHA 已通过完整 CI、安全、前端、live evaluation、canary 和恢复验证。

要求：

- promotion job 必须验证精确 Git SHA 和镜像 digest。
- 将完整 CI、前端、安全扫描、SBOM/Trivy、live eval、认证 canary 和恢复证据设为机器可验证前置条件。
- 任何豁免必须有明确责任人、风险边界、有效期和补验截止时间。

### 4.7 [P2] 签名 PUT 没有字节范围限制，staging orphan 没有清理闭环

证据：

- `opensearch_pipeline/oss_url.py:130-135`：签名只约束 Content-Type。
- `opensearch_pipeline/routes/kb_console.py:2804`：生成普通 presigned PUT。
- `opensearch_pipeline/routes/kb_console.py:2904`：大小在上传完成后的 register 阶段才检查。
- `opensearch_pipeline/dataworks_nodes/register_new_files.py:301-305`：自助上传 orphan 被跳过。
- `opensearch_pipeline/raw_inventory.py:7-25`：当前工具是只读 inventory，不是定时 GC。

影响：

用户可以上传超大对象并永不注册，造成 OSS 成本和 staging 存储增长。

要求：

- 使用支持 content-length-range 的上传策略或受控 multipart session。
- 对用户、部门和时间窗口设置 staging 配额。
- 为未注册对象建立带审计、dry-run 和恢复窗口的定时 GC。

### 4.8 [P2] ACL reconciler 扫描范围与修复范围不一致

证据：

- `opensearch_pipeline/allowed_depts_reconcile.py:74-90`：扫描所有 active 版本。
- `opensearch_pipeline/access_grants.py:402-446`：解析和更新聚焦 current 版本。
- `opensearch_pipeline/access_grants.py:480-564`：最终修复也只覆盖 current。

影响：

旧 active 版本发生组织映射漂移时会持续保持 dirty。查询侧撤权可降低泄漏概率，但会造成错误缺失、重复告警和难以收敛的状态。

要求：

- 要么只扫描 current serving 版本；要么对所有扫描到的 active 版本执行一致修复。
- 为旧 active/current 不一致增加明确指标和修复 SLA。

### 4.9 [P2] HA3 orphan 可造成 top-k 饥饿

证据：

- `opensearch_pipeline/dataworks_orchestrator.py:1186-1211`：orphan purge 是 opt-in。
- `opensearch_pipeline/retriever.py:1295-1325`：有限 top-k 返回后才进行 RDS revalidation。
- 被删除的候选没有 bounded overfetch 或 backfill。

影响：

高分 orphan 占满 HA3 top-k 后被 RDS 丢弃，即使后续存在有效文档，用户也可能得到空结果。新增告警只能发现问题，不能恢复召回。

要求：

- 实现有界 overfetch/backfill，直到获得足量有效候选或达到安全上限。
- 将 orphan purge 纳入 canary 后的常规运维流程。

### 4.10 [P2] 会话请求缺少幂等和顺序控制

证据：

- `opensearch_pipeline/api.py:381-392`：AskRequest 有 session ID，但没有稳定 request/idempotency ID。
- `opensearch_pipeline/session_store.py:153-187`：clear 删除条目，迟到 append 会重新创建。
- history 读取、模型生成和 append 之间没有同 session 的序列化或 CAS。

影响：

- 客户端超时重试会重复调用 LLM 和重复计费。
- clear 后仍在运行的旧请求可以复活会话。
- 同一 session 的快速并发提问读取相同历史，并按完成顺序而非提交顺序写回。

要求：

- 引入稳定 request ID 和结果重放表。
- clear 时增加 session generation/tombstone，旧 generation 不得 append。
- 为同 session 请求增加序号、CAS 或受控串行化。

## 5. 性能与效率专项审查

本节是对全仓 backend/frontend 热路径的补充审查，不把“代码上可达的浪费”冒充“已经由线上指标
证明的瓶颈”。优先级依据资源耗尽半径、默认路径覆盖率和修复收益确定：DingTalk 无界线程的
P1 证据和要求已在 §4.5；其余 4 个 P2、2 个 P3 如下。

| 优先级 | 发现 | 归属 |
|---|---|---|
| P1 | DingTalk 默认无界线程并绕过 admission | 当前 HEAD；见 §4.5 |
| P2 | 同一检索请求多次独立 RDS checkout，图片版本门进一步放大 | 当前 HEAD；`133ad84` 新增部分成本 |
| P2 | 默认检索按请求创建 HA3/多查询/预取子线程池 | 当前 HEAD |
| P2 | Stage-1 对原件执行下载后的第二次全文件读取 | 当前 HEAD |
| P2 | 前端为红点拉完整队列，贡献页可重复发同一请求 | 当前 HEAD |
| P3 | 所有用户空闲时无条件预取管理页代码 | 当前 HEAD |
| P3 | 看板 TTL 缓存无容量上限且过期条目不淘汰 | 当前 HEAD |

### 5.1 [P2] 检索请求重复读取 RDS authority

证据：

- `opensearch_pipeline/retriever.py:586-625`：node ACL 复核自行 checkout 连接。
- 同文件 `:761-826`：主命中 active/ACL/PK 复核再次独立 checkout。
- 同文件 `:1025-1099`：`133ad84` 的图片版本门又自行 checkout，并在主命中、cosurface、
  expand 后多处调用。
- 同文件 `:1683-1709,2964-2987`：stitch/expand 已共享一个 checkout，证明请求级共享模式可行；
  但该 scope 在图片 expand 后版本门和文档日期查询之前关闭。
- 同文件 `:3008-3035`：`_attach_doc_dates` 再开独立连接。
- cosurface 结果还会依次执行版本、授权和主命中三套复核（`:2538-2542`）。

影响：

普通 node-ACL 命中路径已经可能支付授权、主命中、stitch/expand 和日期四轮连接获取；图片路径
还会增加版本门和 cosurface 复核。部分查询可并行预取，但都会竞争默认 20 连接的进程级池，
在并发请求下放大 pool wait、查询 RTT 和尾延迟。

要求：

- 建立 request-scoped authority snapshot/connection，一次批量返回 active、ACL、物理 PK、serving
  version 和 doc date，再由各策略独立裁决。
- 将图片版本列并入主命中复核投影；将 expand 后版本门放进现有 stitch/expand connection scope。
- 共享 transport 不得合并安全语义：撤权、主命中和图片 gate 仍分别保持既有 fail-open/fail-closed 策略。
- 增加每请求 DB checkout 数、pool wait、authority query latency 指标和并发回归测试。

### 5.2 [P2] 每请求子线程池造成跨请求并发放大

证据：

- `opensearch_pipeline/api.py:133-142`：AnyIO 默认允许 120 个同步请求线程。
- `opensearch_pipeline/retriever.py:1291-1390`：默认开启的三路客户端融合为每个查询创建
  `ThreadPoolExecutor(max_workers=3)`。
- 同文件 `:2775-2777,2888-2897,2949-2960`：multi-query、decompose 主路预取和 cosurface
  预取还会创建额外的每请求 executor。

影响：

并发流量下，请求线程会继续孵化短生命周期子线程；即使大部分时间在等待网络，也会增加线程栈
虚拟内存、调度和上下文切换，并使 HA3/DashScope 的真实并发脱离单一总预算。单 worker 并不会限制
这些子线程。

要求：

- 改为进程级、生命周期受控且总量有界的 executor，或按 HA3/DashScope 分别设置共享 semaphore。
- 队列必须有上限和超时，不能把每请求线程池替换成无界全局队列。
- 记录 active、queued、queue-wait、timeout、429 和单臂降级指标，再用 staging 压测确定上限。

### 5.3 [P2] 原件 SHA-256 产生第二次全文件磁盘读取

证据：

- `opensearch_pipeline/pipeline_nodes.py:585-677`：原件先由 OSS 下载到本地文件。
- 同文件 `:701-711`：下载完成后重新打开文件，以 1MiB block 计算 SHA-256。
- 同文件 `:713`：随后 extractor 再读取同一原件。
- 同文件 `:183`：单批最多 100 篇；`opensearch_pipeline/kb_upload.py:51`：自助上传上限 300MB。

影响：

自助上传极端批次可额外产生约 30GB 本地磁盘读取；直投路径受 400MB extract cap 约束，理论值更高。
checksum 对内容失效和 asset diff 有价值，问题不是“应不应该算”，而是下载后又完整扫一遍。

要求：

- 用 OSS download stream 同时写文件并更新 SHA-256，继续核对 version ID/content binding。
- 或在可信上传链记录原始字节 digest，摄取侧只验证而不重新全量读取。
- 增加原件字节数、checksum wall time、parser wall time 和临时盘吞吐指标。

### 5.4 [P2] 侧栏红点触发完整队列和重复贡献请求

证据：

- `console-app/src/App.vue:28-33`：管理员 session ready 后，为侧栏红点预载 approvals、
  access requests 和 contributions pending 三份完整队列。
- `console-app/src/composables/useContribute.ts:239-252`：`loadPending` 拉取最多 50 条完整 DTO，
  但没有 `useKb` 已具备的 single-flight 和 30 秒 staleness 门。
- `console-app/src/views/ContributeView.vue:18-22`：进入贡献页时再次调用 `loadPending()`，可与 App
  预载并发重复。
- `opensearch_pipeline/routes/contribution.py:46-49,812-845`：pending 响应包含 question、content、
  author 等完整字段，并额外计算 asks 热度；侧栏实际只消费数量。
- accept/reject 后 `useContribute.ts:331,345` 会同时重拉 pending、mine、gaps 三个完整列表。

影响：

每个管理员登录都为一个数字传输完整审核内容并占用 aux 限流、RDS 查询和聚合预算；直接进入贡献页
时还可能双拉。审核操作又通过整表重拉更新可局部确定的状态。

要求：

- 提供按身份裁剪的 consolidated badge-count endpoint，只返回各入口计数和 truncated 信号。
- 完整队列进入对应页面后再加载；为 `useContribute` 增加 keyed single-flight/staleness。
- 写操作先进行安全的本地移除/更新，再按需要后台刷新权威真值。

### 5.5 [P3] 路由懒加载被无权限区分的 idle prefetch 抵消

证据：

- `console-app/src/router/index.ts:3-6`：三个视图采用动态 import 分包。
- 同文件 `:26-35`：router ready 后对所有用户无条件 import QaView、ManageView、ContributeView；
  Safari 等无 `requestIdleCallback` 环境在 2 秒后执行。
- 当前 checked-in 产物实测：ManageView 207,492 bytes raw / 58,129 bytes gzip，ContributeView
  37,495 bytes raw / 11,239 bytes gzip。

影响：

普通员工没有管理权限也会下载、解析并执行管理页模块；路由分包只把成本延后，没有真正避免。

要求：

- 身份 ready 后仅对 `canManage` 用户预取 ManageView。
- 尊重 `navigator.connection.saveData`/慢网络；优先采用导航 hover/focus 等意图预取。
- 用浏览器 network trace 分别验证普通员工和管理员的首次加载字节及 route transition latency。

### 5.6 [P3] TTL 缓存不会回收过期页且没有容量上限

证据：

- `opensearch_pipeline/routes/kb_console.py:721-752`：`_dashboard_cache` 是普通 dict；get 对过期
  条目只返回 miss，不删除，put 也没有 max size。
- 同文件 `:1367-1392`：review-tasks cache key 包含 `limit/offset/include_closed`；offset 可达 10,000，
  使认证管理员能够产生大量不同页面键。

影响：

60 秒 TTL 只控制“是否命中”，不控制对象生命周期。长时间运行且访问不同分页组合时，过期响应会
一直占内存，直到写操作清空缓存或进程重启。

要求：

- 使用带 `maxsize` 的 TTL/LRU，或在 get/put 时清理过期条目并执行容量淘汰。
- 对分页端点优先改 keyset pagination；避免缓存任意 offset 页面。
- 增加 cache entries、estimated bytes、hit/miss/eviction 指标。

### 5.7 尚需线上定量的容量边界

- `Dockerfile:55-67` 固定 `--workers 1`，原因是 session、限流、去重和 token cache 仍在进程内。
  这是明确的水平扩展上限，不等于当前 QPS 已经不足；必须先外置状态并做双实例演练，不能直接调 workers。
- `.env.production.template:34-38` 建议开启 rerank 和 VL routing；`reranker.py:176-193` 默认一条
  带图候选即可让整个候选池走 VL。该行为有 A/B 质量依据，且真实 SAE env 未核验，因此未提升为
  缺陷；上线应按图片数统计 VL 路由率、P95/P99、模型成本和 recall，再决定最小图片数或意图路由。

## 6. 验证结果

| 检查项 | 状态 | 说明 |
|---|---|---|
| HEAD/base/merge-base | 已验证 | `HEAD=133ad84`，`origin/main` 与 merge-base 均为 `efac342` |
| Diff 范围 | 已验证 | 43 files，`+3024/-380` |
| `git diff --check` | 有轻微问题 | `tests/test_rag_api.py:407` EOF 多余空行；非阻断 |
| 效率补充定向测试 | 已验证 | 图片版本门 + 临时盘生命周期 65 passed；0.28s |
| 前端构建产物测量 | 已验证 | checked-in assets 静态测量；ManageView 207,492 raw / 58,129 gzip bytes |
| 较早冻结点后端测试 | 已验证但已过期 | 一组 `145 passed, 1 skipped`；另一组 198 backend tests |
| 较早冻结点前端测试 | 已验证但已过期 | 51 frontend tests |
| 当前 SHA 完整测试 | 未独立验证 | 未执行完整矩阵；定向通过不等于全仓通过，提交信息不能替代独立验证 |
| 当前 SHA lint/typecheck/build | 未独立验证 | 本轮未执行完整 lint/typecheck/build |
| 深度安全扫描 | 部分产出/未收敛 | 运行约 5 小时 34 分钟；恢复 46 份结构化阶段结果和 254 条候选，但没有最终验证报告 |
| 真实 RDS/HA3/OSS/DataWorks/SAE | 未验证 | 本地环境无法替代线上证明 |
| 当前 SHA release-gate | 未通过证明 | 现有发布证据对应 `efac342` |
| 认证 canary/恢复演练 | 未验证 | 必须由部署环境补充 |

## 7. 已有正向基础

以下工程基础是有价值的，但不足以抵消上述阻断项：

- 镜像基础依赖使用 digest pin。
- 运行容器采用非 root 用户。
- 正式镜像安装使用 hash-locked dependency path。
- CI 已包含 gitleaks、pip-audit、SBOM 和 Trivy 等检查。
- auth/ACL 生产启动守卫比基线更严格。
- 新增了一批 append race、predecessor predicate、HA3 告警和大文件不变量测试。
- 流式 Markdown 已采用增量 strip/render 缓存，避免逐帧全文重渲染的 O(n²) 放大。
- QaView 已把 deep-watch 全消息树收窄到最后一条消息的可见信号。
- ManageView 已按 tab 惰性加载，后端 stats/insights/governance 也已有短 TTL 聚合缓存。
- stitch/expand 已共享一次 RDS checkout，为 §5.1 的 request-scoped authority 模式提供了现成接缝。

## 8. 建议修复顺序

### 第一批：数据和安全不变量

1. 审批内容不可变绑定，并改为 fail-closed。
2. generation staging + 文档级原子 serving pointer。
3. 图片 current-version gate，覆盖展示与所有外部 egress。
4. Office/ZIP 资源限制和隔离解析。

### 第二批：成本和运行稳定性

1. OCR/VLM 统一硬预算。
2. 全局限流启动恢复修复。
3. DingTalk 有界队列和默认 admission。
4. 检索 request-scoped authority snapshot + 上游全局有界 executor/semaphore。
5. 下载流同步计算原件 SHA-256，消除第二次全文件读取。
6. 上传字节策略、staging 配额和 orphan GC。

### 第三批：状态机和召回一致性

1. 审批退役/拒绝/恢复的 optimistic locking。
2. 前端审批队列强制刷新。
3. node/legacy ACL 展示隔离。
4. ACL reconciliation 范围统一。
5. HA3 bounded refill。
6. session 幂等、generation 和顺序控制。

### 第四批：前端和缓存效率收口

1. consolidated badge-count endpoint，完整队列按页面惰性加载。
2. `useContribute` 请求合流/staleness 和写后局部更新。
3. ManageView 按权限、网络和导航意图预取。
4. 看板缓存增加 TTL 淘汰、容量上限和内存指标。

### 第五批：发布证明

1. 在干净工作树中冻结新的候选 SHA。
2. 对该 SHA 重跑完整后端、前端、lint、typecheck、build 和安全扫描。
3. 生成并验证 SBOM、镜像 digest 和 provenance。
4. 运行真实认证 canary、live evaluation、故障注入和恢复演练。
5. 让 promotion workflow 机器绑定上述精确 SHA 证据。
6. 对最终解析后的 merge SHA 再做一次独立审查。

## 9. 深度安全扫描产物回收

### 9.1 已恢复的产物

本次回收核对到的扫描快照为 `001fe9c10f2d856c66239b09e9d02c8a94945438`。该提交是本报告
审查提交 `133ad84af743fe293b95ae985a46dcdc4eec0c6c` 的祖先，中间相隔 5 个提交。因此，候选中
已经被后续提交修复、改变或移除的路径，必须重新检查，不能直接沿用扫描结论。

结构化产物包括：

- 26 份 discovery worker `result.json`，每份关联独立 worker、威胁模型、范围文件和候选账本。
- 20 份 dedup reducer `result.json`，记录被消费的 worker 和 remediation-subsumption 归并关系。
- 根候选账本与最后一轮 canonical candidate ledger 逐字节一致，共 254 行、254 个唯一
  `candidate_id`；每行都包含 summary、evidence、locations 和至少一个 CWE 标签。
- 会话日志共识别 166 个 Deep Scan 会话，其中 148 个以 `task_complete` 结束，18 个停止在
  `token_count`；最后一次活动为 2026-08-06 13:32:41（America/Los_Angeles）。回收时任务登记
  为空，日志也没有继续追加。

### 9.2 候选分布

下列数字是 CWE 标签出现次数；一条 candidate 可以带多个 CWE，因此不能相加后解释为漏洞数：

| CWE | 标签次数 | 候选主题 |
|---|---:|---|
| CWE-200 | 71 | 信息暴露 |
| CWE-863 | 37 | 授权不正确 |
| CWE-862 | 30 | 缺少授权 |
| CWE-359 | 26 | 隐私信息暴露 |
| CWE-284 | 26 | 访问控制不当 |
| CWE-693 | 25 | 保护机制失效 |
| CWE-345 | 23 | 数据真实性验证不足 |
| CWE-400 | 22 | 不受控资源消耗 |
| CWE-494 | 21 | 下载代码缺少完整性检查 |
| CWE-829 | 20 | 引入不受信任功能 |

候选 location 引用最集中的文件是 `opensearch_pipeline/dingtalk_bot.py`（132 次）、
`opensearch_pipeline/api.py`（105 次）、`opensearch_pipeline/pipeline_nodes.py`（96 次）、
`opensearch_pipeline/routes/kb_console.py`（89 次）、`opensearch_pipeline/retriever.py`（87 次）和
`opensearch_pipeline/extraction/unified_extractor.py`（80 次）。这些是位置引用次数，不是独立漏洞数；
同一根因可能引用多个位置，同一文件也可能被多个高度重叠的候选反复引用。

### 9.3 证据边界和后续处理

这批产物已经证明扫描工作者确实完成了大量代码分析和候选归并，修正了此前“没有任何标准
discovery 产物”的判断。但是，它们尚未达到可直接进入发布判定的 finding 标准：

- 没有最终 scan manifest、完成清单、验证报告或面向读者的最终汇总。
- candidate schema 没有 severity 或 validation status；254 条记录不是 254 个已验证漏洞。
- 18 个会话没有正常完成事件，最后一轮 reducer 也没有对应的结构化 `result.json`。
- 扫描基于较早提交；其后的 5 个提交包含 ACL、追加竞态、HA3 残留、图片 serving-version gate
  等修复，可能直接改变部分候选的可达性或结论。
- 候选中包含 prototype、测试生成器、评测脚本和 goldset 等非生产路径；必须先区分生产可达问题、
  运维/开发工具风险、测试数据问题和误报。

建议按以下顺序收口：

1. 以 candidate 根因和真实 source/control/sink 去重，并映射到本报告已有 P1/P2 项。
2. 对剩余候选在新的冻结 SHA 上重新检查入口、守卫、配置前提、实际 sink 和负向控制。
3. 只有完成源码复核或授权的可重复验证后，才分配严重度并升级为正式 finding。
4. 输出包含确认项、推翻项、重复项和未决项的最终验证报告，再运行针对最终 diff 的安全复查。

## 10. 发布判定

当前状态：**REVISE / NO-GO**。

至少在以下条件满足前，不应给出生产批准：

- [ ] 审批对象与摄取字节不可变绑定且 fail-closed。
- [ ] 新 generation 完整完成前不可服务。
- [ ] 图片 current serving-version gate 已在代码落地；补齐真实 RDS/HA3 canary 与 egress 证明。
- [ ] 300MB 文件具备容器展开限制、隔离和超时。
- [ ] OCR/VLM 所有付费路径纳入硬预算。
- [ ] 限流 seed 和 DingTalk admission 不再 fail-open。
- [ ] 检索并发有统一总预算，RDS authority reads 不再按消费点重复 checkout。
- [ ] 原件 checksum 不再触发下载后的第二次全文件读取。
- [ ] 红点计数不再拉完整队列，前端预取按权限和网络条件裁剪。
- [ ] 进程内 TTL 缓存具备过期淘汰和容量上限。
- [ ] 当前精确 SHA 在干净工作树完成全部本地门。
- [ ] 当前精确 SHA 完成线上 canary、恢复和 release-gate。
- [ ] promotion 与精确 SHA、镜像 digest 和全部证据绑定。
- [ ] 254 条 Deep Scan candidates 已在冻结 SHA 上完成去重、验证和误报剔除。
- [ ] 深度安全扫描已生成最终验证报告和可核对的完成清单。

## 附录 A：本次 Codex 用量说明

账户显示的原始 token 数不等于用户可见输出。Codex 当前按输入 token、缓存输入 token 和输出 token 分别记录并折算额度；长线程、全仓代码、工具结果、重复上下文和子代理都会显著放大累计处理量。

本次审查确实形成了上述代码证据和行动项。深度安全扫描产生了 46 份结构化阶段结果和 254 条
canonical candidates，但没有完成最终验证和报告阶段。账户侧本地会话记录合计约 2.83B token，
其中约 94.7% 是缓存输入；这不能解释为 2.83B 的新内容或有效报告输出。

判断计量是否异常时，应查看 Usage 明细：

- 大头为 cached input：与长仓库审查和重复上下文相符。
- 大头为 ordinary input：需要检查任务、子代理和上下文重复次数。
- 大头为 output，或单个任务独占接近全部当日用量：应导出用量明细并联系 OpenAI 支持核查。

官方参考：

- <https://help.openai.com/zh-hans-cn/articles/20001106>
- <https://help.openai.com/en/articles/12289294-global-admin-console>
