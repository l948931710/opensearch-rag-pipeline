# 《富岭企业级 Agent 底座架构设计报告 v2》实施前架构评审

> 评审对象：`docs/agent-platform-v2/` 全套（主报告 v2 + 6 份配套文件）
> 评审口径：生产级企业 Agent 架构 go/no-go gate review
> 评审基线：报告写作 commit `7c704ce`（2026-07-03）· 当前仓库 HEAD `646d709`（2026-07-05，领先基线 116 commit）
> 方法：8 维度独立评审（证据核查 / 安全 / 运行时架构 / 分布式一致性 / 实施计划 / 运维就绪 / 设计尺度合规 / 红队）→ 每条发现对抗性核实（尝试推翻）→ 完备性批判。71 条原始发现经核实 64 条存活，加完备性补充共 **69 条确认发现**，7 条被推翻剔除。

---

## 0. 总判断

**方案的架构方向是对的，但当前形态尚未达到"可照工单开工"，判定为有条件 NO-GO。**

报告在两件事上做得比绝大多数同类设计好，必须先肯定：

1. **证据纪律真实存在且大体扎实**。核心事实主张抽查基本成立——`retrieve_and_enrich` 确为统一检索入口、`_build_permission_filter` 确为 fail-closed ACL 单点、function-calling/Redis 确实全库零命中、四账号+outbox+审批状态机确为可复用素材。"在现有底座上薄层构建、不推倒重建"的基调正确。
2. **横切层不进 AgentLoop、工具契约不依赖具体框架、deny-first、外部写在审批之后、resume 重解析权限**——这几条边界铁律是企业 Agent 的正确骨架，且都能映射到仓库已有教训。

但评审发现了 **3 类会直接导致返工或事故的问题**，它们决定了"不能现在照 implementation-plan 拆 issue 开工"：

- **A｜事实基线已过时**：报告规划的迁移编号、大量 file:line 锚点、以及至少一处安全修复前提，在写作后三天内已被主线覆盖。照抄开工会撞号、会踩空锚点、会把一天前刚修的越权漏洞在新链路上复活。**这是方法论问题**——"证据重建"的报告有 3 天保质期，而计划却要在 3 天后执行。
- **B｜P0 接口冻结前缺一块地基**：报告把"接口边界"定为 P0 核心，并反复用"接缝错了全盘返工"论证其重要性——但**恰恰漏掉了决定接口形态的最底层输入：并发/执行模型**。同步 `Iterator` 的 Loop、`ToolResultInjector` 无定义、长时 run 没有执行宿主、resume 跑在钉钉回调线程里——这些不补齐，`loop.py`/`run_store.py` 接口一旦冻结就是错的，正中报告自己的判词。
- **C｜运维与合规地基缺席**：审批超时对账、心跳扫描、死信补偿全押在一个**当前不存在**的调度器上（DataWorks 生产不可用、运维还在个人 Mac launchd）；staging 运行环境不存在但 WS0 验收全靠它；机密数据出境 DashScope 无闸门、评测 judge 用的是境外 `claude-opus-4-8`（与硬约束 3 正面冲突）。这些不是"实施细节"，是 gate 项。

**结论**：先做一个 **WS0-Pre 修正批次**（见 §6），把上述 A/B/C 的阻塞项补进报告与计划，并对 HEAD 做一次事实 re-baseline，之后即可开工。方案不需要重写，需要**补齐地基 + 校准基线**。

严重度分布：27 high / 37 medium / 5 low（全部 CONFIRMED）。下文按主题合并同类项陈述（同一问题被多个维度独立发现的，合并计一条并标注）。完整分级索引见 §7。

---

## 1. 主题 A · 事实基线已过时（方法论级，最高优先）

报告扉页写"全部结论由证据重建 @ `7c704ce`"，但 implementation-plan 是要在 HEAD 上执行的工单来源。基线与执行点之间的 3 天 116 commit 落差制造了以下确定性故障：

### A1. 迁移编号 017–021 已被主线全部占用（7 个维度独立命中 · high）
- 报告 §4/§5/§6/§7 与 plan 6 处硬编码 `017_agent_runtime`…`020_llm_call_log`，§11 续规划 `021/022/023`。
- **写作时正确**（`git ls-tree 7c704ce schema/` 最高号 016），**现已冲突**：HEAD 已存在 `017_qa_admission_reject`、`018_gen_meta_runtime_contract`、`019_chunk_meta_index_retry`、`020_document_version_simhash`、`021_ingest_quality_metrics`，`schema/README.md` 台账均已登记。
- 该仓库有前科：`repo-architecture-map.md` 自记现存 3 对冲突编号（002/003/006），011 台账机制本身即因 010 列漂移事故而建。重号会污染 `schema_migrations` 生产变更追溯。
- **修**：四段 DDL 文件头 + §11 迁移列 + plan 六处编号一律改占位符 `NNN_*`；plan 加纪律"开工时按 `schema/README.md` 台账现值取号"；WS0 第 0 步做 HEAD re-baseline。

### A2. session_store 已加会话归属绑定（P3-6），报告设计会把它复活（2 维度命中 · high · 安全）
- HEAD commit `d72ef63`（2026-07-05）给 `session_store.py` 加了 `_verify_owner` + `SessionOwnershipError`，修复注释原文：**"钉钉会话 key 是结构化可构造的 conversationId:staffId，没有这层校验时任何已认证 API 调用者都能伪造 key 窃取他人多轮上下文"**。独立核实：基线该文件 0 处 owner 校验，HEAD 9 处。
- 但报告 §6 `SessionMemory` 四方法（`get_snapshot/append/set_summary/clear`）只收 `thread_id`、键设计 `sess:{thread_id}:msgs` 无 owner 位；plan WS0-2 还写"现签名不变"。**照此实现 RedisSessionStore，一天前刚修的越权在 Agent 新链路（且 WS0-2 迁移的正是现网 RAG 会话）上原样复活**：任一有效令牌用户构造他人 `conversationId:staffId` 即可读/污染其上下文，含权限过滤后的部门数据。
- **修**：`SessionMemory` 全部方法签名加 `ctx`/`owner`，Redis 侧存 owner 并常量时间比对、不符 fail-closed；plan WS0-2 以 HEAD 签名（含 owner/trusted）为准；补 Redis auth/TLS/VPC 设计（§13 仅提 AOF，全文无 Redis 认证）。

### A3. 报告 §8 对 AWAITING_COMMENT 的现状判定是事实错误（medium）
- §8 把 `AWAITING_COMMENT` 列入"全进程内、需外置"清单，WS0-3 计划把它迁 Redis。
- 但 `feedback_handler.py:325` 注释明写**"状态存 RDS（handled_status='AWAITING_COMMENT'），多 worker 安全"**——它早已是 RDS 状态、多实例安全。把它迁 Redis 是**负收益改造**（把一个 durable 事实降级为热态）。
- **修**：从 WS0-3 移除该项；核对 §8"进程内状态"清单其余项是否也已被后续修复改写。

### A4. file:line 锚点大面积失效、无对照机制（medium）
- 全套文档以精确 `file:line` 为证据锚，但 116 commit（含盲区审计大批量修复）后锚点已大面积漂移；除报告头一句"@7c704ce"外无任何"锚点以基线为准"的对照说明。plan 是逐文件工单，锚点错位会让实施者改错位置。
- **修**：plan 顶部加一句"所有 file:line 为 `7c704ce` 快照，开工前对 HEAD 逐一 re-anchor"；WS0-0 产出一份锚点漂移对照表。

> **主题 A 的根因不是某个数字写错，而是"证据重建型报告"与"三天后执行"之间没有 re-baseline 关卡。** 这是最该先补的一条流程。

---

## 2. 主题 B · P0 接口冻结前的运行时地基缺失（返工级）

报告把接口边界定为 P0 并称"接缝错了全盘返工"，但接口设计缺了决定其形态的最底层输入。以下每条都在 `loop.py`/`run_store.py` 冻结前必须补。

### B1. 没有并发/执行模型（high · 根本性）
- §3 的 `AgentLoop.run` 返回**同步** `Iterator[AgentEvent]`，而宿主是 FastAPI + 钉钉裸 daemon 线程。全文无一句并发模型（asyncio / 线程池 / 独立 worker）。
- **失败场景**：P1 灰度一个部门，每个 run 占死一个线程 1–3 分钟，线程池被占满后 `/api/ask`、`/api/auth`、钉钉 webhook 全部排队超时——**agent 灰度直接拖垮存量 RAG**；钉钉入口则无上限线程膨胀。
- **修**：§3 增设"执行模型"节并作为 P0 交付——run 主体走专用有界执行器/后台 task，与 HTTP 请求生命周期解耦，SSE 只做事件消费端；给出 per-instance 最大并发 run 数与拒绝策略。此决策必须先于 loop 接口冻结。

### B2. `ToolResultInjector` 出现在核心签名但全文无定义（high）
- 它是 Loop↔Runtime "唯一通信协议"的另一半。缺失导致：§3⑩ 承诺的 Adapter 契约测试无法编写；若实现为同线程阻塞取结果会与消费 iterator 的 Runtime **直接死锁**；每个实现者各猜一种回注方式，P0 结束接口即分叉。
- **修**：冻结前补全 `ToolResultInjector` 完整 Protocol（或改用 `Generator.send(ToolResult)` 并把返回类型改为 `Generator[AgentEvent, ToolResult|None, None]`，与 B1 联动）；连带补 `RunCheckpoint` 字段级 schema（含序列化版本号）与 `ApprovalOutcome` 判别联合。

### B3. resume 续跑跑在钉钉审批回调线程内（high）
- 审批人点 EDITED → 回调 handler 背着整个剩余 run 跑 2 分钟 → 钉钉 ACK 超时重投 → 第二次回调并发进入 resume（decide 幂等只保护 decision 落库，续跑重入需 run 状态机 CAS，图三未画）；`run_in_executor(None)` 默认执行器几个并发审批即占满，殃及全部 Stream 消息。续跑完成后**结果无投递通道**——用户视角"审批通过了但没有下文"。
- **修**：回调 handler 只做 `decide` 落库 + `transition(suspended→resuming)` CAS + 发布 resume 事件后立即 ACK；续跑由 B1 的执行宿主消费；补 resume 结果主动投递（钉钉走 conversation_id 主动卡片）。

### B4. 同 turn 多 tool_call 部分执行的挂起/恢复语义未定义（high）
- §3 仅"messages+待执行调用序列化"一句。按字面只存待执行调用 → resume 时 messages 缺第 1 个工具结果 → DashScope 400；保守重放整 turn → 第 1 个工具重复执行（计费/延迟，HIGH_WRITE 靠幂等键兜底但那是最后防线）。
- **修**：`RunCheckpoint` 显式建模 turn 内每个 `call_id` 的槽位（executed(result) / pending_approval / not_adjudicated），规定末单裁决后才 resume、首个 REJECTED_TERMINATE 即止；EDITED 时重写历史该 tool_call 的 args 使 messages 自洽。列入回放测试。

### B5. 崩溃恢复主张与 checkpoint 写入时机自相矛盾（medium）
- §3⑧ 说进程崩溃后 running 态 run 可由 resume 恢复，但 checkpoint 只在"挂起"时写——running 态 run 没有 checkpoint 可 resume，恢复路径不可实现。需明确 running 崩溃后是从最近 step 边界重放还是标记失败。

### B6. approved-but-not-resumed 的 run 永久卡死（medium）
- decision 落库与 resume 跨进程，唯一对账任务只扫 `pending→expired`。审批已批但 resume 未触达（进程重启/pub-sub 丢事件）→ run 永远 suspended，expiry 不会捞它。需加"decided 但仍 suspended 超阈值 → 重放 resume"对账。

### B7. 审批卡片投递无 outbox/补投（medium）
- `approval_request` 落库（图三步7）与投递钉钉（步9）非同事务，投递失败=审批单静默等死到 expired。仓库已有 kb_access outbox 范式却没用在这里。

### B8. ExecutionContext frozen 与 budget 扣减矛盾（medium）
- `@dataclass(frozen=True)` 装不下可变的 `RunBudget` 扣减；且预算消耗跨 suspend/resume 不持久——resume 重建 ctx 即预算清零、`deadline`/`auth_resolved_at` 阈值未定值。需把预算移出 frozen ctx 落 run_store，定义重解析阈值默认值。

### B9. agent 会话与 qa_session_log/qa_conversation 双轨无合流（medium）
- 一次 agent 对话写哪几张表、L2 重建从哪读、console 历史页读哪个——全文无合流设计。L2 从 `qa_session_log` 回读重建会缺 agent 轮次；console 历史看不到 agent 对话。

---

## 3. 主题 C · 分布式状态与一致性（多实例正确性）

WS0 状态外置是全项目回归风险最高的改动，其正确性有以下缺口：

- **C1（high）per-thread run 串行化缺失**：同用户同会话并发两条消息 → 两个 run 交错写 `sess:{thread}:msgs`，历史轮次错位、双 compaction 竞态、预算翻倍。报告称"不需要粘性路由"在会话**写路径**上不成立。需 RDS `(thread_id, active)` 唯一约束或 Redis per-thread 锁，明确排队串行 or supersede。
- **C2（high）pub/sub 承载审批 resume 通知**：Redis pub/sub 是 at-most-once 无持久化，订阅实例正重启即丢事件 → 审批已批用户永远收不到结果。SSE 中继同理丢帧。改 Redis Stream（XADD/XREAD 断线续读）或断线从 run_store 补发，resume 一律由处理决策的实例同步驱动、pub/sub 仅加速。
- **C3（high）Redis 故障降级矩阵只写了 session 一项**：限流 fail-closed 使单档 Redis 成为**全站问答新单点**（改造后可用性反而低于现在的零依赖）。去重/锁/pub-sub 故障行为未定义。需逐组件降级矩阵 + Redis 双副本/自动切换 + `/api/ready` 仅在 ask 强依赖层参与摘流。
- **C4（medium）msgId 去重 SETNX 先占位后处理**：处理崩溃后钉钉重投被去重键吞掉，消息永久丢失，比现有内存版更差（丢消息 > 重复处理）。需"处理完成才确认"语义。
- **C5（medium）幂等回路不完整**：`uk_tool_idem` 撞键后读回执路径、`status=executing` 的 in-doubt 处置、EDITED 改参后幂等键语义均未定义；且报告与 plan 对幂等键派生规则互相矛盾（`run_id+step_no` vs 业务单据号）。
- **C6（medium）僵尸 run 回收任务无落点**：`heartbeat_at` 建了列和索引，但阈值/动作/由谁扫/单例保证在报告与 plan 都没有（与主题 E 的"调度器不存在"叠加）。
- **C7（medium）会话写路径无并发控制**：LIST append + LTRIM + summary STRING 三键无原子性，rolling summary 的 `upto_msg_id` 与 LTRIM 边界不一致会丢摘要。
- **C8（medium）单 worker 隐式依赖有漏网项**：`retriever._deny_cache` 的跨模块主动失效在多实例下失灵，威胁"授权撤销即时生效"；身份/ACL 三个 TTL 缓存也未入降级矩阵。除报告点名五项外需再 grep 一遍 module-level 可变状态。

---

## 4. 主题 D · 安全与合规（gate 级）

- **D1（high）审批回调只证来源、不证点按人身份**：五件套解决"回调是否来自钉钉"，不解决"点按钮的人是否 ∈ approver_scope"。钉钉卡片可转发/存在于多人群，任何收到者点"通过"，回调 userId 即成 `decided_by`。`decide()` 必须校验 `decided_by ∈ approver_scope`、非成员 Unauthorized 落审计；卡片单人定向禁转发；归属校验改 fail-closed。
- **D2（high）机密数据出境 DashScope 无闸门**：`knowledge_search`/`readonly_sql` 命中 `confidential` 文档或经营数据后，内容作为 prompt 发往阿里云托管的 DashScope——**境内 ≠ 富岭企业边界内**。研发配方/合同/财报进第三方模型，涉阿里云训练使用条款，报告零讨论。修：Policy/Gateway 加 `data_classification × provider` allowlist，confidential 默认禁送外部 provider 或强制自托管/脱敏；DashScope 数据处理与不用于训练承诺列入 §13 作 P1 前置。**代码量约一天（字段已有）**。
- **D3（high）评测 judge 用境外模型，与硬约束 3 正面冲突**：`eval_harness/run_judge.py:41` `JUDGE_MODEL="claude-opus-4-8"`。报告把"Claude judge"当核心复用资产并接入阻断式发布门。严格执行硬约束 3 则业务数据不能进 judge、P1"权限维度 100% 硬门"失去裁判而返工；默许出境则合规叙事被内控一票否决。修：L7 及含业务数据的评测层换境内 judge 面板（保留反自评），judge 服务不可用要有人工兜底而非直接阻断。
- **D4（medium）prompt injection → tool_call 无机器可执行防线**：§9.4"高风险工具参数不采信自由文本来源"没有落地机制，且 LOW_WRITE 默认不过审批。需在 ToolSpec/Executor 层把"参数是否源自检索文本"变成可判定标记。
- **D5（medium）M-Schema Examples 默认抽真实值送 DashScope**：脱敏开关默认态未定，商密数值列不在 PII 脱敏覆盖内。需默认 `masked`、商密列纳入脱敏。
- **D6（medium）checkpoint 加密密钥管理 + 留存缺失**：`state_blob` 长期留存全量对话（含机密检索内容），密钥管理未提、无过期策略。confidential 数据 checkpoint 应强制加密而非"可插拔"。
- **D7（medium）L4 dept 记忆是持久化注入通道**：模型可写 dept scope、`search` 未排除未确认项 → 投毒事实被后续 run 当上下文。写入需人工确认门 + search 排除未确认项。

---

## 5. 主题 E · 运维就绪与计划优先级（落地级）

### 运维地基缺席
- **E1（high）审批对账/心跳扫描/死信补偿全押在不存在的调度器**：DataWorks 生产实际不可用（仅 VIRTUAL 空节点、代码包未部署），运维还在个人 Mac launchd，而计划把"迁 DataWorks"排在 WS5（P4），**晚于依赖它的 WS3（P2）**。P2 上线后审批 `expires_at` 到期无人置 expired，"超时=拒绝"承诺落空、suspended run 永久悬挂。修：把"可靠调度地基"提为 WS0——应用内后台扫描线程（Redis SETNX 锁互斥，分钟级扫 `idx_status_expiry`/`idx_status_hb`），DataWorks 只作日级兜底；审批过期改**读时惰性判定**（decide/查询路径即判 expires_at），定时对账降级为兜底。
- **E2（high）staging 运行环境不存在但 WS0 验收全靠它**：WS0 五条验收全要"双实例 + 真钉钉 + Redis"的 staging 形态，今天不存在、计划里没有建它的工时。结果只能生产上验收（拿存量用户当小白鼠）或验收名存实亡。修：插入 WS0-0 建 staging SAE 应用 + staging 钉钉机器人 + staging Redis + CI staging 通道；无 staging 时的替代验收（生产影子实例）作显式降级。
- **E3（high）发布/缩容对 in-flight run 无排水**：`/api/ready` 只摘新流量，正在执行的 turn（尤其 HIGH_WRITE）和 SSE 被直接杀掉，而 WS0 引入 CD 后发布是高频事件。P3 真实写回阶段这是生产事故路径。需 SIGTERM 后 turn 边界主动 checkpoint 挂起、HIGH_WRITE 标"不可中断窗口"、被杀 run 由心跳任务判 superseded。

### 可观测 / 成本 / 容量
- **E4（high）无 agent SLO / 无审批默认窗口 / 无新告警阈值**：现有 SLO 只覆盖 RAG 四指标。P1 灰度没有量化"健康/回滚"依据、审批 fail-closed 的"超时"业务上不可预期。需定义 run 成功率/p95、suspended>24h 计数、审批过期率等并挂告警。
- **E5（high）成本防线缺口**：现有账单保护按"请求数"计，而 agent 使单请求成本 ×10–20。全局日帽 2000 请求不变即可让日账单放大 10 倍不触任何闸；跑飞循环当天烧穿。需 Gateway 加日级/部门级 RMB spend 闸（fail-closed），报告补"单次对话成本量级估算"。
- **E6（medium）告警通道承接不了新告警族**：`alerting.py` 去重是进程内存（多实例后同告警 ×N），severity 纯装饰无分级路由，报告让死信/熔断/漂移/审批过期全倒进一个钉钉 webhook。
- **E7（medium）新表族零留存设计**：`retention.py` 作业清单与 PIPL 主体擦除链都不覆盖 step/checkpoint/llm_call_log/approval，DDL 无 TTL/分区。
- **E8（medium）Redis/Tair 运维自相矛盾**：AOF 建议与"可容忍丢失"矛盾；内存淘汰策略未定（LRU 逐出=静默失忆/重复消费，noeviction=限流 fail-closed 全站 503）。
- **E9（medium）"部门级灰度"无实现机制**：`RAG_AGENT_ENABLE` 是全局环境变量，SAE 改环境变量=重启=杀 in-flight run，P1"灰度部门"验收按现设计无法执行。需运行时可切的灰度开关（DB/Redis 标志位 + 部门白名单）。
- **E10（medium）readonly_sql 打在唯一生产 RDS 实例**：LLM 生成的查询无实例级负载隔离，一条合法慢查询可拖垮现有 RAG + 审批状态机 + agent 平台。需只读副本或资源隔离。

### 计划与优先级
- **E11（high）golden 251 基线门没有执行场所**：脚本自述 DRAFT、未接任何 CI，接入排在 WS2-4，但 WS0（最高回归风险阶段）验收与横切纪律#3 都以它为门。修：gate 去 DRAFT + VPC runner 建设并入 WS0-4；per-merge 跑 golden_50 smoke、发布前跑 251 全量。
- **E12（medium）P1 出口验收无量化线**："E2E 完成率与人工纠正率达标"全文无数字，"按业务基线"而产品未上线不存在基线；l7 冷启动只解决题源，参数正确性/多步轨迹 ground truth 与人工标注流程无 owner。
- **E13（medium）双轨（agent/rag 链路）无收敛里程碑**：`_process_agent_query` 与 `_process_rag_query` 长期并列，灰度期两张回归网都不覆盖试点部门答案质量，谁维护两套、何时收敛未定。
- **E14（medium）WS0 灰度只有 flag 单切**：无双写/影子读阶段；关键缓解"回读重建"依赖 §13.1 未确认的生产 flag；最小档 Redis 成 ask 路径新硬依赖。
- **E15（medium/low）两文档工期与阶段归属互相矛盾**：报告 P0=3-4 周 vs plan WS0+WS1=3.5-5 周；§7 把"4 处 chat 收敛"标 P0 而 plan 放 WS2（P1）；"13-18 周"是多人口径而 git 证据显示有效开发者 1 人。

---

## 6. 主题 F · 组织 / UX / 尺度 / 红队（unknown unknowns）

- **F1（high）巴士系数 = 1**：全仓 149 commit 实质一人 + AI。"WS2/WS3 双线并行压缩 3 周"对单人不成立；P3 后系统持有 U8 写通道，kill switch、补偿"人工触发"、审批告警响应全部只有一个可用人；且**评审者 = 被评者 = 实施者，无内生纠错回路**（本次外部评审正是补这个）。修：把"第二操作员 + runbook"设为 P2→P3 go/no-go 前置；WS3/WS4 各加"非作者按 runbook 独立完成崩溃恢复/kill switch 演练"；工期按串行口径重报业主；P3 前引一次外部独立验收。
- **F2（high）SQL 语义层只解了技术层、没解组织层**：指标口径谁定义、口径变更如何管理、答错数责任边界全缺。财务与生产"产量"口径不一致 → 试点部门拿到的数与 ERP 对不上 → "Agent 算错了"无法反驳 → 问数场景死于第一个月。修：每个 `sem_*` 视图强制业务 owner + 口径描述 + 版本号，DDL 评审需口径 owner 签署（可复用 kb_access 审批状态机）；首批问数域口径清单列入 §13 作 WS2-1 前置；SQL golden 由口径 owner 验收。
- **F3（medium）用户体验层零覆盖**：多轮澄清、执行中进度反馈（尤其钉钉主入口）、失败文案、SQL 答数溯源展示全缺，但 P1 验收却以"试点部门 E2E 完成率/人工纠正率"为标准——UX 直接决定这两个数。
- **F4（medium）审批人是活人，无代理/升级/催办/业务日历**：管理员休假一周 = 该部门写操作全线 expired-terminate，HITL 组织层面不可用（与"无审批人可达=拒绝"叠加即业务停摆）。
- **F5（medium）EDITED 交互不存在设计**：四处置中最复杂的"钉钉卡片上改嵌套 JSON 参数"作为 P2 验收项，但钉钉卡片交互能力撑不起，按图实施必然卡壳。需重新设计（小程序编辑页 or 结构化字段卡片）或降级 EDITED 范围。
- **F6（medium）Agent 产出回灌知识库的自引用回路**：现成入口 `kb_contribution`，摄取侧无 AI 来源标记/过滤，报告对 L5 只写"不动"。AI 答案被存回库 → 被索引 → 成为未来检索证据。
- **F7（medium）checkpoint 跨代码版本 resume 无兼容策略**：挂起 run 可存活 3 天、横跨多次 CD 发布，blob 无版本字段，审批通过时代码/ToolSpec/消息格式可能已变。
- **F8（medium）审批-审计证据链自断**：`approval_request`/`tool_invocation` 只存"脱敏后"参数 + digest，审批人批的是脱敏视图、执行的是 checkpoint 原文，而脱敏规则本身全文未定义。
- **F9（medium）kie_extract 输入通道在钉钉主入口不存在**：现有 bot 显式丢弃图片/附件，且文件引用类工具的数据面授权（用户是否可读该 OSS 对象）在 Policy 两层模型中完全没有。
- **F10（medium）许可义务无落地动作**：Apache-2.0/MIT 的版权声明保留、M-Schema vendoring 的 LICENSE 随附、OmO clean-room 边界——摘抄/移植条目的许可义务无任何交付项承接。
- **F11（low）服务模型无版本 pin 与漂移应对**：DashScope 强制升级/下线 `qwen3.6-*` 时，提示词模拟 function-calling 通道与冻结评测基线双双失守（仓库对 judge 已学过这课）。
- **F12（low）§13 待确认清单无 SAE 平台侧能力项**：P0 验收直接押注 SSE 长连接、多实例灰度、Redis 连接等 SAE 未验证假设。
- **F13（low）retrieve_and_enrich "纯函数式"仅签名成立**：行为受十余 env flag 与内部 LLM 子调用支配，直接工具化会破坏 ToolSpec 的确定性与超时假设；HEAD 起 `RAG_MAIN_HIT_REVALIDATE` 默认开，依赖足迹与报告描述已不一致。

---

## 7. 开工前必须完成的修正批次（WS0-Pre）

建议在拆 issue 之前，先交付一个把上述 gate 项补进报告与计划的修正批次，再进 WS0：

**P0 — 不做不能开工（阻塞项）**
1. **HEAD re-baseline**：迁移编号全改占位符 + 台账取号纪律（A1）；file:line 锚点漂移对照表（A4）；核对 §8 现状清单（A3）。
2. **补运行时执行模型节**，先于 loop 接口冻结：并发/执行宿主 + `ToolResultInjector` 定义 + `RunCheckpoint`/`ApprovalOutcome` schema（B1/B2/B4）。
3. **SessionMemory 加 owner 归属校验 + Redis auth/TLS**（A2/D1 会话侧），修复被吞的 P3-6。
4. **可靠调度地基提到 WS0**：应用内后台扫描线程 + 审批读时惰性过期（E1）；撤 Mac launchd。
5. **staging 环境建设 WS0-0**（E2）；golden gate 去 DRAFT 并入 WS0-4（E11）。
6. **数据出境闸门 + judge 境内化裁决**（D2/D3）——含业务数据进模型前的合规前置。
7. **成本日级/部门级 spend 闸**（E5）。

**P1 — 进 WS1 前补设计**
- per-thread 串行化（C1）、pub/sub 换 Redis Stream + decided-but-suspended 对账（C2/B6）、Redis 全组件降级矩阵（C3）、发布排水协议（E3）、agent SLO 族与告警阈值（E4）、审批回调 `decided_by ∈ approver_scope` 校验（D1）。

**P2 前补组织/交互设计**
- SQL 口径 owner 流程（F2）、第二操作员 + runbook（F1）、EDITED 交互重设计（F5）、UX 层最小闭环（F3）、审批人代理/升级链（F4）。

**纳入 §13 未确认清单（挂 owner + 截止）**
- DashScope 企业数据条款、staging/SAE 平台能力、Redis/Tair 规格、首批问数域口径、U8 对接口径。

---

## 8. 附：被对抗核实推翻的发现（不纳入结论，供参考）

以下 7 条初评发现在对抗核实阶段被推翻或证据不足，特此透明记录：审批 render_summary 由 LLM 生成的假设（未确证生成方）、READ_ONLY 审计 fail-open 缺检测面（场景内影响不足）、§13 缺 owner 的两条重复项（与 E12/E15 合并）、Approval 接口 P0 定型 P2 消费的"矛盾"（接口先行有合理性）、DB 版工具注册表过度设计（治理需求成立）、"3-5 千行"低估（口径不明无法定论）。

---

*本评审由 8 维度并行 + 逐条对抗核实的多智能体工作流生成（85 agent），所有存活发现均标注 CONFIRMED 并附独立复现证据。结论仅覆盖架构与实施就绪度，不含具体代码实现。*
