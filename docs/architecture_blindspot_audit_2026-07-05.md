# 架构盲区审计报告 — Unknown Unknowns (2026-07-05)

## 1. 总述

本轮审计与 2026-07-01 的《逐文件质量评审》互补而非重叠。前一轮以**单文件正确性**为透镜，逐个模块验证"这段代码本身对不对";其产出(F-1…F-43 及基线限制)几乎全部是"某一行/某一函数内部的缺陷"。本轮换用**系统级 / 跨切面 / 涌现式**透镜,专门捕捉那些**每个文件单独看都正确、缺陷只存在于文件之间的关系里**的问题(unknown-unknowns):写入者与消费者跨越两个独立部署平面(DataWorks 摄取 vs SAE 服务)时的失配、一个能力在 schema 中声明却无人实现、一个安全网被写入却从不被出队、一个不变量(如"查询向量的模型必须等于索引向量的模型")不归任何单一文件所有。

因此本轮的典型证据形态不是"第 X 行写错了",而是"A 文件承诺的下游消费者在 B、C、D 文件里都不存在",或"两个各自正确的守卫用了不同的信号键,恰好在某条路径上分叉"。所有 57 条发现均已对抗性复核,并逐条确认相对逐文件审计为**新增(NOVEL)**;去重合并后本报告呈现 **55 条**。凡证据偏薄者已下调置信度或降级严重度,并如实标注「已确认 CONFIRMED」/「较可信 PLAUSIBLE」。

## 2. 盲区维度地图(覆盖面)

| 维度 | 是否含 critic 追加 | 是否浮现实质风险 |
|---|---|---|
| 单写者不变量 / 跨平面写一致性 | 是 | ⚠️ 有(P2×2, P3×2) |
| 故障域与灾备(DR) | | ⚠️ 有(P2×2) |
| 数据治理 — PIPL 删除权/告知同意/目的限制 | 是 | ⚠️ 有(P2×1, P3×2) |
| 数据治理 — 审计链防篡改 | 是 | ⚠️ 有(P3) |
| 数据驻留 — 跨境传输(PIPL Ch.3) | | ⚠️ 有(P2×2) |
| 部门隔离作为系统属性(非检索过滤) | | ⚠️ 有(P2×2, P3×2) |
| 索引与嵌入模型版本演化生命周期 | | ⚠️ 有(P2×2, P3×3) |
| 经济失控 / 滥用经济学 | | ⚠️ 有(P2×3) |
| 可观测性 — 假健康 / 无信号 / 请求关联 | | ⚠️ 有(P1×1, P2×3, P3×1) |
| 对抗性 LLM 原生威胁面(标记投毒 / 内容供应链) | | ⚠️ 有(P2×1, P3×1) |
| 时间 / 时区 / 日历分桶 | 是 | ⚠️ 有(P2×2, P3×1) |
| 可复现性与可审计性(争议/合规复现) | | ⚠️ 有(P2×3) |
| 回归安全网 / 发布门 / judge 漂移 | | ⚠️ 有(P2×4, P3×1) |
| 供应链 / 依赖治理 | 是 | ⚠️ 有(P3) |
| 配置地雷(安全开关被静默关闭) | | ⚠️ 有(P2×2) |
| 容量 / 无界增长时间炸弹 | | ⚠️ 有(P2×2, P3×1) |
| 语料时效 / 时间治理(过期/矛盾/复审) | 是 | ⚠️ 有(P2×1, P3×3) |
| 派生数据可恢复性(向量/审计流) | | ⚠️ 有(P3×2) |
| 人机协同队列的所有权(转人工/复审) | 是 | ⚠️ 有(P1×1, P2×2, P3×1) |
| 嵌入共享缓存投毒 | 是 | ✅ 清白(前轮已证伪:仅理论) |
| VLM JSON 失败 fail-open | | ✅ 清白(_quarantine 钩子未接线,不触发) |
| prod+simulate 无守卫 | | ✅ 清白(serving 忽略 simulate 标志) |
| spot_checker 删除无状态校验 | | ✅ 清白(SDK 抛 TeaException) |

> 清白维度同样是产出:它们证明对抗面已被主动排查并排除,而非遗漏。

## 3. 确认发现

### P1 —— 最高优先级

**P1-1. 全局日熔断触顶=静默全站宕机,且让 SLO 看起来更"绿"**
- **维度**:可观测性 — 假健康信号
- **触发场景**:扫描器/预算日突增/重试风暴把当日准入问答数推到 `RAG_GLOBAL_DAILY_LLM_CAP`(默认 2000),此后至次日全部 503。
- **影响**:`rate_limiter.py:295-302` 触顶仅 `logger.error`,**从不调用 send_ops_alert**(其调用者集合={alerting, dataworks_orchestrator, qa_rollup, reconcile},rate_limiter 不在内),无人被 page。更糟:`api.py:389-414 _enforce_rate_limit` 在 `log_qa_session`(639/693/…)**之前**就抛 503,被拒请求从不落 `qa_session_log`;于是 `qa_rollup` 分母只剩触顶前的成功量,answer_rate/error_rate 全绿——宕机日在 SLO 看板上被报成 HEALTHY。
- **为何被逐文件漏掉**:rate_limiter 的日志行、qa_rollup 的聚合各自都正确;缺陷只在"拒绝发生在 A 文件、日志发生在 B 文件"的交互里,没有文件拥有"offered vs admitted load"这个量。
- **修复**:触顶边沿 `send_ops_alert(severity=critical, dedup_key='global-cap')`;把被拒准入计入 `rejected_count` 并让 qa_rollup 把它纳入分母。
- **与已知项区别**:F-32 是 send_ops_alert 把 HTTP200 当已投递;本条是热路径事件**根本不调用**告警,且被结构性地从唯一的 serving 指标里抹除。
- 已确认。

**P1-2. 转人工是"只写不读":按钮插入 PENDING 工单却无人出队,同时骗用户说有人会跟进**
- **维度**:人机协同队列所有权 / 死胡同交互
- **触发场景**:用户得到错误答案点「转人工」,`feedback_handler.py:239-311 _create_escalation` 仅 `INSERT ... ticket_status='PENDING'` 后 commit 返回,**不发任何通知**;`dingtalk_bot.py:1085` 却回「已为你转人工,相关同事会尽快跟进~」,控制台 `MessageBubble.vue` 渲染「已转交管理员」。
- **影响**:全仓对 `escalation_ticket` 的读只有两处:`kb_console.py:1109` 的累计 `COUNT(*)`(看板虚荣数)与 `feedback_miner.py:73` 的只读 markdown 统计;`assigned_user_id/expert_answer/closed_at/ticket_status` 从无任何文件 UPDATE。全产品的核心信任恢复机制是彻底 no-op,每一次升级都被静默丢弃且用户被误导——正是事后复盘"转人工路由到虚空一年"的场景。
- **为何被漏掉**:`_create_escalation` 单看是一次正确的 INSERT+return;只有做**跨文件消费者普查**才发现这次写入没有任何消费者。
- **修复**:`_create_escalation` 内真正通知(钉钉群机器人/责任部门);新增管理台队列端点(按龄列 PENDING、分派、作答→expert_answer、关闭)并设 owner 与 SLA;在有消费者之前不要对用户承诺"同事会尽快跟进"。
- **与已知项区别**:F-5/F-6/F-7 关注反馈**写入**的安全/PII;无一发现该写入**无消费者**;F-32 至少尝试了通知,本条连尝试都没有。
- 已确认。

### P2 —— 高优先级

**P2-1. kb_retire 在 RDS 下线文档,却把其向量永久留在 HA3 可检索——status='retired' 无任何自动清除路径**
- **维度**:单写者不变量 / 服务写 vs 批清除接缝
- **触发场景**:管理员在控制台下线一份作废/有合规错误的文档,得到 200「retired=true」。
- **影响**:`kb_console.py:1962-1970` 仅写 RDS 且**从不置** `index_status='PENDING_DELETE'`;而 `reconcile_pending_deletes` 只选 `PENDING_DELETE`、`reconcile_stranded_versions` 要求存在新 INDEXED 版本(retire 已把 is_active 全清零故子查询为空)、CS3 只读且把残留 HA3 行归为良性 `ha3_stale`。默认配置下,被撤销/含合规错误内容持续被检索并作为权威来源引用,直至人工执行带 prod token 的删除。响应文案还在主动误导管理员。
- **为何被漏掉**:单看该端点是一次干净的事务写+审计行,docstring 承诺"由 gated 运维完成";看不到那个下游消费者不存在——需跨"服务→批"接缝枚举每个 reconciler 证明无人以 status='retired' 为键。
- **修复**:让 retire 复用兄弟端点 `kb_set_visibility→restricted` 的握手(置 `PENDING_DELETE`),或新增以 `status='retired'` 为键的调度清除器;检索主命中增加 RDS is_active/status 复核(纵深防御)。
- **与已知项区别**:F-37 是相反方向(retired 文档被复活);基线"PENDING_DELETE+reconcile 缓解"在此不适用,因为 retire 根本不进入该路径。
- 已确认。

**P2-2. 批处理 node_deactivate_old_chunks 盲目把 index_status 重置为 SUCCESS,覆盖服务平面写入的 PENDING_DELETE 握手 → 受限文档被永久公开投放**
- **维度**:单写者不变量 / index_status 列被两平面共写
- **触发场景**:文档 D 当前版本正处于 stage-3 工作集(有 is_active=1 NOT_INDEXED chunk,ACL 物化后的常态);stage-3 取锁(PROCESSING)开始推送时,管理员将 D 设为 restricted(PROCESSING→PENDING_DELETE、permission 收紧)。stage-3 完成后以**旧的宽松 permission_level** 推入 HA3,随后 `pipeline_nodes.py:5643-5664` 把 `PENDING_DELETE→SUCCESS` 无条件覆盖。
- **影响**:管理员标记为 restricted 的文档以旧 permission 永久留在 HA3 被越权投放,无 reconciler 修复、无告警。竞态触发(故 P2),但后果是持久的 ACL 破坏。
- **为何被漏掉**:两个写者各自看都对(控制台 `NOT IN (DELETED,PENDING_DELETE)` 安全,批处理置 SUCCESS 像正常完工);只有把 index_status 当作两平面共有的锁/握手令牌,丢失更新才可见——逐文件透镜无法同时握住两个写者。
- **修复**:`node_deactivate_old_chunks`/`node_update_index_status` 改为 CAS:`... SET index_status='SUCCESS' WHERE ... AND index_status='PROCESSING'`(仓内 `dataworks_orchestrator.py:551` 已有此范式);控制台侧对称地不覆盖 PROCESSING。
- **与已知项区别**:F-38/F-40 均为平面内竞态;本条是**批写者摧毁服务平面写入的状态令牌**,击穿了基线赖以缓解的 PENDING_DELETE+reconcile。
- 已确认。

**P2-3. HA3 批量数据丢失有检测无补救:reconcile.py 能发现消失的 chunk,却无任何代码路径能重建索引,也无重建 runner**
- **维度**:故障域与 DR
- **触发场景**:HA3 索引/集群被删或向量丢失,而 RDS 仍报 INDEXED。
- **影响**:`reconcile.py:2-19` 明言检测"从检索彻底消失"方向但"NEVER deletes or deactivates";唯一写者 stage-3 只重选 `index_status IN (NOT_INDEXED,FAILED)`(`dataworks_orchestrator.py:421`),卡在 INDEXED 的 chunk 永不被认领;文档中引用的全量重建工具(`ha3_verify.py:17`、`access_grants.py:6` 假定存在)grep 无果;唯一复位工具 `reset_for_rechunk.py` 需显式 `--docs` 且做重量级全量重切。恢复=人工逐 doc 拼 JSON 批量重跑。
- **为何被漏掉**:reconcile 是正确的只读探针、orchestrator 的认领查询是正确的增量过滤、reset 工具是正确的定向复位;缺陷是它们之间**缺失的边**——无模块拥有"检测到缺失→重建"。
- **修复**:让 reconcile 输出零-HA3-行文档的 chunk 集,新增 `rebuild_from_rds.py` 将其 `index_status→NOT_INDEXED` 使 stage-3 重嵌入重推;加周期性 parity+repair 作业与 DR 演练。
- **与已知项区别**:F-34 是相反方向(删除 RDS 不再认识的 HA3 行);本条是"从 HA3 消失"方向有检测器却无任何写者。
- 已确认(降级自 P1:reconcile 已排 02:30 日跑并发 OBS-4 CRITICAL 告警,恢复非纯人工;真实缺口是"检测到补救"无自动桥+无 bulk one-shot)。

**P2-4. DashScope 是横跨两平面的单一故障域,且无降级读:嵌入服务中断时 /api/search(号称"无 LLM")与全部检索硬失败,尽管 HA3 的 BM25 chunk_text 可纯文本兜底**
- **维度**:故障域与 DR
- **触发场景**:DashScope 嵌入端点中断而 HA3/RDS 健康。
- **影响**:每个请求都算新嵌入无兜底(`retriever.py:161` raise、search_chunks/retrieve_and_enrich 无 except),`/api/search`、`/api/ask`、钉钉全 500。docstring 自述有 BM25 文本路,但只在同一 hybrid 查询内(仍需向量),无纯文本路径。摄取侧同样依赖该供应商,爆炸半径覆盖两平面。
- **为何被漏掉**:每个调用点都正确地把错误传播为 500;缺失属性是架构级——无跨 retriever+api 的隔板/兜底层,无人拥有"嵌入供应商宕机时产品做什么"。
- **修复**:嵌入失败时降级为纯 BM25(chunk_text)查询并标 degraded;加 (question,dept) 短 TTL 答案缓存;熔断器跳到文本模式而非全部 500。
- **与已知项区别**:基线仅提限流与 ~11% 错误率,无一涉及 DashScope 宕机下的**可用性**或降级读缺失。
- 已确认。

**P2-5. 不存在数据主体擦除路径——离职员工/删除请求者的个人数据无法清除(违反 PIPL 第15/47条)**
- **维度**:数据治理 — PIPL 删除权
- **触发场景**:员工离职或数据主体行使 PIPL 撤回/删除权。
- **影响**:唯一保留机制 `retention.py` 纯按龄(`created_at < DATE_SUB(...)`),`_JOB_NAMES` 无 user_id 维度;`user_feedback`/`escalation_ticket`(均含 user_id/query/answer)甚至不在其作业集内→无限保留。`delete_conversation` 是软删且注释明言审计行「不动」,`clear_session` 仅逐内存 LRU。无任何 API/CLI/作业能按主体清除。
- **为何被漏掉**:retention/delete_conversation/clear_session 各自都工作;只有全系统视图才看到无文件拥有"擦除用户 X 的一切",能力落在五张表两库+内存的缝隙里。
- **修复**:新增 `retention.purge_subject(user_id)` 跨 `qa_session_log/qa_conversation/qa_retrieved_doc_fact/user_feedback/escalation_ticket` 硬删+逐 session_store,受审计管理员 token 门控;挂离职钩子。
- **与已知项区别**:F-36 是无界增长/时窗批删,与按主体擦除结构正交(retention 无 user_id 轴,且两表根本不在其内)。
- 已确认。

**P2-6. 跨境驻留守卫键于环境**标签**而非物理**目标**:生产员工 PII 可经"只读远程"合规路径外泄到 Google**
- **维度**:数据驻留 — 跨境传输
- **触发场景**:工程师用 dev 标签开 PROD-RO 会话(`RAG_ENVIRONMENT=development` + `RAG_ALLOW_REMOTE_DB/SEARCH=read_only_ack`),笔记本只配 `GEMINI_API_KEY`。
- **影响**:Gemini/驻留硬守卫只对 `environment in (production,staging)` 触发(`config.py:869`);但数据访问守卫 `_validate_environment_target_consistency` 明确允许 dev 标签凭 ack 读生产 RDS+HA3;无 DashScope key 时模型解析全路由到 `generativelanguage.googleapis.com`(`config.py:677/694/705`)。查询嵌入与检出的生产 chunk_text(含身份证/手机号/薪资)被 POST 到 Google。
- **为何被漏掉**:两个守卫各自看都严密;只有把它们组合,才发现驻留键于标签、访问键于物理指纹,恰在 ack'd 只读远程路径上分叉。
- **修复**:驻留守卫改为按**解析后的目标指纹**(复用 `is_prod_target`)触发,与标签无关;至少在 `read_only_ack` 置位时禁止 Google base_url。
- **与已知项区别**:前轮证伪的"prod+simulate"/"缓存投毒"均不同;本条是两个各自健全的守卫的**组合缺口**。
- 已确认。

**P2-7. 由查询日志派生的 UX 特征(热门问题/改写建议)跨全部门聚合 query_text 且零 ACL,把他部门查询意图泄露给每个用户**
- **维度**:部门隔离作为系统属性 — 独立于 HA3 过滤的泄露通道
- **触发场景**:HR/财务用户以 ≤30 字问一个部门受限主题且成功两次,一小时内被聚合为主页「示例问题」推给他部门产线工人。
- **影响**:`api.py:1499-1513 _compute_hot_questions` 与 `1569-1589 _compute_success_pool` 仅有 status/长度/测试账号谓词,**无 user_dept 过滤**(该列存在,`qa_logger.py:235`),经 `/api/hot-questions` 逐字下发。查询文本本身(揭示他部门在问什么、内部术语)越界,尽管每次检索都正确按部门过滤。
- **为何被漏掉**:该聚合查询看是正确、防注入的;只有握住"部门界定可见范围"这一全系统不变量,才注意到该面派生自跨部门 qa_session_log。
- **修复**:两个聚合查询加 `AND user_dept = %s`(或成员集 IN),或维护独立策展的公共建议池。
- **与已知项区别**:F-36 是同表的增长/保留;本条是用户面特征无部门谓词地读取它,且泄露的是问题文本本身(非答案 PII)。
- 已确认(泄露量中等:热门仅 top-6 高频;改写池需 bigram 重叠;但违反了明示的隔离不变量)。

**P2-8. 摄取与服务两平面之间不存在嵌入模型契约:HA3 文档无模型判别符、查询时无 ingest↔serve 模型守卫,同维度升级(v4→v5)静默返回垃圾相似度**(合并 #14+#51)
- **维度**:索引与模型版本生命周期 / 单供应商模型漂移
- **触发场景**:`text-embedding-v4→v5`(或任意同 1024 维替换)在 SAE 服务容器经 `RAG_EMBEDDING_MODEL` 上线,而 DataWorks 摄取仍跑 v4(或别名被供应商重映射)。
- **影响**:`chunker.py:249-286 to_ha3_doc` 不写 embedding_model/version/dimension;查询过滤器只按权限(`retriever.py:637`);两平面各自从 env 独立解析模型(`config.py:693-695`),维度都是固定默认 1024 故 HA3 不报错;v5 查询向量与 v4 文档向量的相似度语义无意义。全站召回崩塌,无 500、无告警,唯一症状是慢慢变差的点踩。
- **为何被漏掉**:无文件拥有"查询模型必须等于文档模型"这一不变量,它横跨 config(每部署解析)、to_ha3_doc(定 schema 却省戳)、retriever(建查询却不校验)与两个物理分离的部署。
- **修复**:把 embedding_model+version+dimension 戳入每个 HA3 文档;服务启动/readiness 断言 `config.embedding.(model,dimension)` 等于索引内探测到的模型,失配则 fail-closed(503);`/api/version` 返回运行时模型而非常量。
- **与已知项区别**:F-3/F-20 是 kNN order=DESC(排序方向);证伪的"缓存投毒"是缓存;无一涉及 HA3 文档缺模型判别符或 ingest/serve 配置漂移无交叉校验。
- 已确认(降级自 P1:属迁移期潜在,需运维动作触发,非稳态每查询必发;但触发即静默全站崩塌)。

**P2-9. 摄取嵌入缓存键省略 dimension(且共享单个跨环境 OSS 镜像),同模型改维度会静默返回旧的错维度向量**
- **维度**:索引与模型版本生命周期
- **触发场景**:运维改 `RAG_EMBEDDING_DIMENSION`(如 1024→512,v4 支持 Matryoshka 多维),或 A/B overlay 仅维度不同。
- **影响**:缓存键仅 model+text(`embedding_cache.py:24`、`pipeline_nodes.py:5770`),而查询侧键含 dimension(`retriever.py:121`)——两缓存不对称。命中的 chunk 以旧维重推:若 HA3 表已重建为新维则被拒并永久 FAILED 循环;若未重建则对缓存文本静默 no-op,产出混维语料/无效 A/B。OSS 镜像单对象无 env/dim 命名空间(`OSS_MIRROR_KEY` 固定),跨环境串味。
- **为何被漏掉**:键格式在缓存文件、维度是一等可配旋钮在 config、两缓存键不对称——只有同时读两个缓存才见失配。
- **修复**:键改为 `md5(f"{model}_{dimension}_{text}")`;OSS 镜像按 env/model/dim 命名空间。
- **与已知项区别**:与证伪的"缓存投毒(理论,值投毒)"不同,本条是键完整性缺陷(维度缺席)且有支持的真实触发。
- 已确认。

**P2-10. CostBreaker 运行级预算每个 drain 批次都重新实例化,唯一的聚合支出上限从不累加,被 N 倍静默击穿**
- **维度**:经济失控 / 滥用经济学
- **触发场景**:运维开启熔断并设 `run_budget_rmb=200`;大/图密 backlog 分 K 批 drain。
- **影响**:`run_stage_drained`(`opensearch_pipeline/dataworks_orchestrator.py:656`)在 drain 循环内调 `run_stage`(:768,最多 100000 迭代),而 `run_stage:58-59` 每次都 `CostBreaker(config)`,`__init__` 重置 `_run_total_rmb=0`。唯一跨文档的 Gate 3 每批归零,总花费上界=K×200 而非 200。docstring 声称的"一次运行一个实例"不变量被违反。
- **为何被漏掉**:cost_breaker 类内部正确、run_stage 内部正确;缺陷只在对象预期寿命(每次运行一个)与调用者(每批一个)的跨文件矛盾里。
- **修复**:在 `run_stage_drained` 内实例化一次并注入;以 run_id 键的进程级单例;理想以按 bizdate 的持久台账支撑(可跨 DataWorks 节点重试)。
- **与已知项区别**:基线只提 cost_breaker 默认关/仅 VLM;未发现即便开启,run_budget 门也被跨文件寿命失配击穿。
- 已确认。

**P2-11. 公网端点的全局日 LLM 熔断器仅在内存,每次 SAE 重启归零,且计"准入请求"而非 DashScope 调用**
- **维度**:经济失控
- **触发场景**:扫描器猛打 `/api/ask` 时并发 SAE 重部署/OOM/自动伸缩替换。
- **影响**:`rate_limiter.py:183` 全局上限状态是进程内元组,重启即重初始化为 0,攻击中崩溃循环则反复清空 2000/日 账户保护顶;且计准入请求 1 次,而一个 thinking+多子查询请求耗约 10× token——同一顶下真实账单差一个数量级。
- **为何被漏掉**:计数器在本文件内看是权威("workers=1 即全局");缺口是进程寿命 vs 它要执行的 24h 预算窗,及"准入请求"与"计费调用"的阻抗失配——无文件拥有"重启后什么幸存"。
- **修复**:计数器(及估算花费)持久化到 RDS 按北京日;按估算调用/token 权重递增;加 DB 读的硬管理员 kill 开关。
- **与已知项区别**:已知限流项(F-5/F-42)关身份/鉴权;基线"workers=1 无 Redis"针对会话,未提计费熔断器继承同等易失性。
- 已确认。

**P2-12. 无组件拥有"今日 DashScope 总花费":摄取嵌入及全部辅助 LLM 路径完全无门,三个现有控制彼此不相交且各有逃逸口**
- **维度**:经济失控
- **触发场景**:任何批量弄脏 chunk 的事件(ACL reconcile 重置 NOT_INDEXED、模型/缓存键变更、批量重注册)经嵌入节点,而该节点无支出上限。
- **影响**:成本模型只知 OCR_PAGE/VLM_IMAGE 两类(`cost_breaker.py:31-88`);摄取嵌入(最大单次调用量)、query 分解、rerank、spot/reconcile LLM 调用全无成本门。cost_breaker(默认关/仅 VLM/每批归零)与 rate_limiter(仅 serving/内存)各守一隅。无单一 kill 开关能封当日总账。
- **为何被漏掉**:各控制在本文件内合理甚至被文档标注为有意窄化;只有把全部支出源与全部控制叠加才见未覆盖面。
- **修复**:引入单一持久支出台账(RDS 按北京日),每个 DashScope 调用者预留额度并设全局硬 kill 标志;把嵌入与辅助 LLM 纳入成本模型;批与 serving 挂同一台账。
- **与已知项区别**:基线把 cost_breaker 窄化与"无 Redis"各自当作可接受设计点;未识别"无组件聚合/约束当日总支出"这一涌现缺口。
- 已确认。

**P2-13. 零/塌缩流量产生假绿 SLO——死掉的前端与健康前端无法区分**
- **维度**:可观测性 — 看板绿而服务已死
- **触发场景**:钉钉 webhook 签名坏/认证失败/单容器崩溃循环,当日 qa_session_log 零行。
- **影响**:`qa_rollup.py:92-94 evaluate_slos` 明言"零流量不违约",各检查 `if x is not None`;total=0 时 `slo_ok=1` 且无告警,全站宕机显示绿;甚至 70% 流量塌缩(半个 bot 坏)在幸存流量上也正常。
- **为何被漏掉**:"None 不违约"在单元层可辩护;系统洞是无组件拥有"流量缺席本身是否事件",该不变量落在 bot 存活与 rollup 阈值之间。
- **修复**:加流量下限 SLO:total_queries 为 0 或较该周日近 N 日中位数下跌 >X% 即告警。
- **与已知项区别**:F-33 是 reconcile 空首轮假稳(reconciler bug);本条是 SLO 评估器对全 serving 平面的数据缺席定义盲。
- 已确认。

**P2-14. 无死人开关:整条监控+告警链是"仅沉默"式且在生产中并未真正被调度**
- **维度**:可观测性 — 告警本身无人监控
- **触发场景**:笔记本 crontab 关/VPN 失败、DataWorks 节点仍暂停、凭据粘贴过期、`RAG_OPS_ALERT_WEBHOOK` 未设/过期。
- **影响**:`ops_monitor.py:16-19` 自述尚未在 DataWorks 部署、需笔记本 crontab;`ops_health_monitor_node.py:8-11` 复用**已暂停**节点;`alerting.py:47-77` webhook 未设/发送失败均 fail-open。无任何心跳或 qa_daily_metrics 新鲜度检查。任务不跑或 webhook 死,沉默即被当健康。
- **为何被漏掉**:各模块"设计上 fail-open"且单独正确;涌现属性(fail-open 作业之并 + 未调度 runner = 零保证信号)只在端到端追踪投递链时可见。
- **修复**:每次 ops_monitor 写 heartbeat 行;独立廉价检查(或外部 cron-ping 死人开关)在 heartbeat/metrics 超 ~26h 时告警;定期验 webhook 可达。
- **与已知项区别**:F-32 是单条告警的投递确认 bug;本条是监控层整体无存活证明。
- 已确认。

**P2-15. 无摄取漏斗/完整性监控:静默丢弃(SKIPPED_DUPLICATE、卡 LOADING、classify FAILED、误隔离)到不了任何告警,因为 reconciler 只检查已激活 chunk**
- **维度**:可观测性 — 对所有绿信号不可见的数据丢失
- **触发场景**:文档被 SKIPPED_DUPLICATE(F-16)/classify FAILED(F-14)/误隔离/stage-1-2 崩后卡 LOADING,永不成为 active-INDEXED chunk。
- **影响**:所有 reconciler 都从 active-INDEXED 起算(`reconcile.py:101/511`),从未到达该态的文档整体在 parity 宇宙之外——RDS↔HA3 干净、raw↔OSS 干净、qa_rollup 只量已答问。一份静默从未入索引的文档零漂移、零违约、零告警,与"文档不存在"无从区分。
- **为何被漏掉**:每个 reconciler 对其切片都健全;盲区是它们全锚定在成功终态,故障群被定义在范围外——需无人拥有的全系统漏斗视图。
- **修复**:加漏斗/老化 reconciler:对注册 >Nh 仍未 INDEXED、content_process_status 卡 LOADING/PROCESSING 超阈、每日 raw-in vs indexed-out 吞吐差 告警。
- **与已知项区别**:F-14/F-16 是具体丢弃 bug;本条是整类无任何检测器(无漏斗指标、无卡行老化监控)。
- 已确认(注:`_reset_stale_stage2_locks` 有 2h 复位但仅 print,且只是把卡行移入另一无告警终态 FAILED)。

**P2-16. 单个部门经理即可无二次审批、无自动对抗筛查地发布全公司"public"投稿内容**
- **维度**:对抗性 LLM 原生威胁面 — 内容供应链的治理/权限
- **触发场景**:一个被盗或恶意的部门经理账号以 `permission_level='public'` 接受投稿。
- **影响**:`contribution.py:549-559` 内注明"部门领导直接定——public 只校验 allowed,不转审批",一个 token 既审又提升到全公司 public,随后物化为下一批 DAG 注册,无进一步人工或自动注入检查。结合 P2-17 的标记注入,一次审批即让载荷可被全公司检索。
- **为何被漏掉**:接受端点的授权检查单看正确(确实调 authorize_upload);系统风险是**缺失第二控制**(无职责分离/无 public 范围的 kb_admin 联签/无自动筛查)——逐文件正确性审计不标记"缺失"。
- **修复**:public 投稿要求第二审批人(kb_admin);accept 与 materialize 之间插入自动注入/PII 复扫;把 accept 记为区别于部门内的特权审计事件。
- **与已知项区别**:F-40 关跨部门读**授权**去重;本条关**发布**全公司内容的权柄——不同控制面。
- 已确认。

**P2-17. `<<IMG:N>>` 多模态标记是第二注入通道:投稿内容可把受害文档的图片贴到攻击者答案上(视觉引用伪造)**
- **维度**:对抗性 LLM 原生威胁面 — 标记投毒
- **触发场景**:攻击者投稿正文含字面 `<<IMG:2>>`,其 chunk 与合法带图文档在检索位 2 共现时,LLM 回显该标记。
- **影响**:摄取从不剥离 `<<IMG:N>>`;服务侧图片挂载纯由 LLM 答案里的标记按检索位 N 驱动(`content_blocks_builder.py:430-520`),无任何校验 N 对应文本实际来源的 chunk。他部门 SOP 的官方截图被渲染为攻击者文本的视觉证据。
- **为何被漏掉**:content_blocks_builder 是正确的标记解析器、contribution 是纯文本提交;滥用需把"投稿文本从不剥标记"与"标记在渲染时跨索引"这两文件两阶段串起来。
- **修复**:摄取时剥离/转义投稿正文的标记;渲染时仅当 chunk[N-1] 确实向该段贡献了文本才生效(标记绑定到源 chunk 而非裸检索位)。
- **与已知项区别**:F-24 是标记从**会话历史**跨轮泄露(源自过往答案);本条是同轮经**已索引 chunk**(投稿流)投毒——不同源、不同信任边界。
- 已确认(影响上限被略微夸大:图片已过检索 ACL,无跨租户泄露;但结构缺陷真实)。

**P2-18. qa_session_log.created_at 由 RDS DEFAULT CURRENT_TIMESTAMP 写入(非 SAE 容器),而夜间 rollup 假设它是太平洋时钟,导致每一行都被错桶约 15h 到错误的北京业务日**
- **维度**:时间/时区 — 存储时区 vs 分桶时区跨 writer/schema/连接/分析失配
- **触发场景**:生产 RDS 位于非 America/Los_Angeles 时区(浙江企业几乎必然);夜间 `run_rollup` 用 `CONVERT_TZ(created_at,'America/Los_Angeles','Asia/Shanghai')` 分桶。
- **影响**:写路径不拥有时间戳(`qa_logger.py:234-260` base_cols 无 created_at),连接池不 pin 时区(`db.py:192-212` 无 time_zone init),故 created_at 实为 RDS 服务器时区(≈+08:00);而 `qa_rollup.py:17-18` 断言它是太平洋。LA→Shanghai 转换把每行前移 ~15-16h,7月4日的 rollup 实选约 7月3日09:00→7月4日08:00 的行。每日 QA 指标与 SLO 判决都算在错的 ~15h 切片上,在 LOCAL-DEV(开发者 Mac 是太平洋)上隐形。
- **为何被漏掉**:qa_rollup 的 DST 正确转换、qa_logger 的普通 INSERT、schema 的普通 DEFAULT、db 的普通池——各自自洽;矛盾只存在于 writer+schema+连接+分析的连接处。
- **修复**:停止推断存储时区:pin 池会话时区(`SET time_zone`)并让 rollup 从该固定时区转换,或让 qa_logger 显式写 created_at;加启动断言记录 `@@session.time_zone`。
- **与已知项区别**:无已知项触及时区/日分桶;最近的 F-31/F-35 是单文件配置/DDL。
- 已确认(降级自 P1:限于 QA 指标/SLO 治理,非用户答案正确性;同类可观测性 bug F-32/F-33 为 P2)。

**P2-19. 夜间 SLO rollup 与周报用两套互不可调和的公式分桶业务日,且周报用的正是 qa_rollup 已标注为有 bug 的 +15h 公式**
- **维度**:时间/时区 — 两条报告路径日分桶分叉
- **触发场景**:任何落在 DST 分歧窗(周报固定 +15h vs 夜间 DST 正确 +16h,美国冬令时)或北京日首尾小时的查询。
- **影响**:`weekly_qa_report.py:25/89-91` 用固定 `DATE_ADD(created_at, INTERVAL 15 HOUR)`,而 `qa_rollup.py:20` 明言该法有 bug(冬令时 PST=+16h)并已改用 CONVERT_TZ。同一份周报的定性样本(+15h 拉取)与定量总量(来自 CONVERT_TZ 分桶的 qa_daily_metrics)用**两种日定义**,边界查询在一周被重复计一周被丢。
- **为何被漏掉**:两份是独立 LaunchAgent 部署物,各自内部正常;缺陷是它们必须对"这行属哪天"达成一致却没有——无文件断言该不变量。
- **修复**:抽出单一 `beijing_business_day(created_at)` 助手供两者调用,删除 `RAG_TZ_SHIFT_HOURS`;或让周报总量与样本都只读 qa_daily_metrics。
- **与已知项区别**:与 P2-18(存储时区前提)不同,本条是跨报告不一致;无已知项涉及报告日算术。
- 已确认(季节性:仅美国标准时约 11-3 月分歧,夏令时重合)。

**P2-20. 产生任何答案的确切 prompt 不可重建——qa_session_log 未捕获 prompt 模板版本或装配标志状态**
- **维度**:可复现性与可审计性
- **触发场景**:员工争议 3 个月前的安全流程答案;其间 `RAG_PROMPT_INJECTION_GUARD` 被切换、规则4/8/9 被改写(有记录的 A/B)。
- **影响**:系统 prompt 是可变模块常量且代码自认已漂移(`llm_generator.py:81-82`);`_build_messages` 按运行时标志条件追加多条规则(注入/图片子索引/低置信度/纯文本 swap),这些身份均不落库(`schema/002:81-109` 无 prompt_version/hash,`build_qa_log_kwargs` 无 prompt 参);全仓 grep `prompt_version|prompt_hash` 零命中。合规争议中组织无法复现或描述当时给 LLM 的指令集。
- **为何被漏掉**:llm_generator(prompt)与 qa_logger(列)各自单独看都没错;缺失不变量"落库日志必须钉住 prompt 身份"落在 prompt 模块、config 标志、日志 schema 的缝隙。
- **修复**:给 qa_session_log 加 prompt_version(或完整装配后系统 prompt 的内容 hash),在 `_build_messages` 后计算并经 build_qa_log_kwargs 串入;prompt 常量冻结在显式版本标签后。
- **与已知项区别**:F-23 是 stream 路径不落 cited_docs(数据管道遗漏);无已知项涉及 prompt 溯源。
- 已确认(降级自 P1:可审计性缺口,不产生错误答案)。

**P2-21. LLM 采样不确定且未钉住——temperature 默认 0.1(非零)、top_p 从不发送、每请求 temperature 不落库**
- **维度**:可复现性与可审计性
- **触发场景**:为抗辩争议答案尝试重跑,即便检索相同也得不同答案且无法复现采样制度。
- **影响**:`DEFAULT_TEMPERATURE=0.1`(`answer_flow.py:52`)非零故采样不确定;payload(`llm_generator.py:737-746/850-862`)从不发 top_p/seed,依赖 DashScope 未文档化服务端默认;`build_qa_log_kwargs` 无 temperature/top_p 参、schema 无该列,采样参数从不记录;model_name 存的是可变别名(`qwen3.6-plus`)。
- **为何被漏掉**:各调用点正确传 temperature;系统缺口是不确定性在 llm_generator 引入、api 可覆写、却从不汇入 answer_flow/qa_logger 的日志载荷。
- **修复**:审计关键路径设 temperature=0(或固定 seed),显式发 top_p,给 qa_session_log 加 temperature/top_p/seed 列;记录具体解析后模型 build 而非别名。
- **与已知项区别**:无已知项涉及采样确定性或生成参数记录。
- 已确认(注:即便 temp=0 也非位级可复现,真正可修的核心是**记录**缺口)。

**P2-22. 检索制度(融合模式/权重/阈值/rerank 开关/top_k)未被捕获,使落库的 score/top_score 在任何配置变更后不可解释**
- **维度**:可复现性与可审计性
- **触发场景**:争议后分析师读半年前 top_score=6.2 的行,其间 `RAG_RERANK_ENABLE` 被开启、阈值切到 rerank 尺度或融合切 RRF。
- **影响**:hybrid_fusion/knn_weight/阈值均 env 驱动可变(`config.py:209-318/757`),代码明言切融合"score 分布完全不同,必须重标定";但 qa_session_log 仅存裸 `top_score`,不记产生它的融合模式/权重/阈值/rerank/top_k;`score_level` 按**当前**阈值重算。落库的 6.2 再也无法分级 high/mid/low。
- **为何被漏掉**:config 在一处、打分在 llm_generator、日志在 qa_logger;"落库分数必须携带定义其含义的制度"横跨三处,无人拥有。
- **修复**:每行落一份紧凑检索配置指纹(融合/权重/阈值/rerank+model/top_k),或引用版本化检索配置注册表。
- **与已知项区别**:基线提 7.7/5.8 阈值与融合耦合仅作实时行为注意;无已知项观察到审计日志未捕获制度使历史分数不可解释。
- 已确认。

**P2-23. 发布门默认 goldset(golden_full,251)与冻结基线的 goldset(golden_50,76)不匹配 → 每次默认门跑都把逐层/子集回归网静默降级为非阻断 expected_na**
- **维度**:回归网被禁用
- **触发场景**:运维在发版前跑 `make release-gate`(默认脚本)。
- **影响**:`deploy/eval_release_gate.sh:25` 默认 `golden_full.json`(sha 32074b...),但 `baseline.json` 冻结在 `golden_50`(sha ab7e33...,匹配);regime 键含 `eval_set_sha`,`baseline.compare()` 遇 regime 差返回单个 `pass=None, na_reason="expected_na"`;`_strict_failures` 只对 `not_executed` 失败,expected_na 不阻断。于是唯一能在绝对阈值仍通过时抓到指标下降的机制被静默关闭,`merge --strict` 仍退出 0。
- **为何被漏掉**:bug 活在三个产物(shell 默认 goldset、run_eval 默认、冻结 baseline 制度)的关系里,无单一文件拥有。
- **修复**:加一行守卫,断言 run 的 eval_set_sha == baseline 的 goldset sha 则硬失败(而非 expected_na),或把 shell GOLDSET 默认改为 golden_50。
- **与已知项区别**:F-31/F-33/F-35 无关;本条是 deploy 脚本与提交产物的跨文件失配。
- 已确认(缓解:L1/L2/L3 绝对地板门仍跑仍阻断;被静默禁用的是 baseline 差量网)。

**P2-24. 门控跨运行比较的制度指纹省略了 LLM-judge 模型与 rubric_version,Claude judge 升级会静默重基四个答案质量硬门且校准过期**
- **维度**:judge 漂移无门控
- **触发场景**:runner 上 `claude` CLI 自动升级到打分偏移 ~0.3 的新模型。
- **影响**:`run_eval.py _regime():81-91` 与 `baseline.py _REGIME_KEYS` 均无 judge_model/rubric_version;judge 是环境里解析到的任意二进制(`eval_release_gate.sh:38`)。而 faithfulness/correctness/completeness/fabrication 四硬门全由 judge 产生。`regime_matches()` 仍返回 True,跨两个不同 judge 比较 correctness 如同同制度,无 refreeze 信号。
- **为何被漏掉**:需交叉引用"被基线化的指标(report.py judge 门)由制度指纹(run_eval/baseline)缺席的组件产生"——跨三文件。
- **修复**:在 `run_judge.py:70` pin `--model`,把 judge_model+rubric_version 加入 `_regime()`/`_REGIME_KEYS`。
- **与已知项区别**:基线把跨制度拒绝当可用特性;盲区是 judge 被结构性排除出制度。
- 已确认(承重腿是 judge 模型漂移;rubric 是仓内常量,变更会改 code_commit 故可被抓)。

**P2-25. 冻结基线只捕获 L1/L3 确定项/L4/judge 正例的指标,结构性省略 L0 索引健康、L2 校准与离题 AUC、L5 权限、全部 L6 chunk 质量、负例造假率——这些族即便门控在线也零回归覆盖**
- **维度**:基线覆盖洞
- **触发场景**:某变更把 L2 离题 AUC 0.90→0.86(仍≥0.85 地板)或负例造假 0.02→0.09(仍≤0.10),全过绝对门但无基线指标故差量门无法触发。
- **影响**:`baseline.py extract_metrics():55-97` 只 put `l1.*`/`l3.{over_refusal,source_leak,keyword_cov}`/`l4*`/`judge.{faith,correct,complete}`+`judge.mm.image_relevance`,无任何 `l0/l2/l5/l6/judge.negatives`。五整族的回归网退化为单一静态绝对阈值,无趋势/子集敏感度——正是基线要闭合的缺口。
- **为何被漏掉**:extract_metrics 单看正确;缺陷是它相对 run_eval 实跑的层**没枚举什么**——全系统完整性属性。
- **修复**:把 l0/l2/l5/l6/judge.negatives 关键指标纳入 extract_metrics 与 freeze。
- **与已知项区别**:与 P2-23(制度失配禁用全部回归)不同,本条是即便制度匹配基线覆盖仍按构造部分;F-43 是 reranker 无单测(不同产物)。
- 已确认。

**P2-26. 发布门无独立的源级完整性 oracle,故 chunker/dedup 的静默丢弃(F-16 图片被忽略的去重、F-29 FAQ 丢图块、F-30 clause 超预算被丢)在任何被 eval 检查的产物里都不留痕,无法被抓**
- **维度**:完整性盲区
- **触发场景**:重切回归丢掉真实内容(F-30 丢整个 clause chunk、F-16 忽略图片的哈希去重)。
- **影响**:L6 读**摄取后**产物(`l6_chunk_quality.py:698` 从 chunk_meta,`family_idset_reconciliation` 以 `rds - ha3` 为数据丢失),预设 RDS 是 ground truth;边界族只查已存在 chunk。无层把源文档与期望 chunk/图片数比较。chunker 从未产出的 chunk 在 RDS 与 HA3 都缺,idset Jaccard 仍 1.0,无门观察到缺席。
- **为何被漏掉**:每个 eval 层单看有效;只有全系统视图才见每层都从流水线输出取 ground truth,无独立完整性锚。
- **修复**:加独立源→期望产物 oracle:从源文档(或 L4 再抽取)导出期望 chunk/图片数并与落库比对。
- **与已知项区别**:F-16/F-17/F-29/F-30 是摄取 bug;本条解释**发布门为何结构上抓不到这整族**。
- 已确认(注:纯文本 chunk 丢弃 F-30 完全命中;L4 binding 会抓到带图 GT 样本的丢弃)。

**P2-27. load_config 启动校验覆盖物理目标与 LLM 供应商,却从不检查安全姿态标志——`RAG_QA_LOG_PII_REDACT=false` 在生产关闭不可逆的 QA 日志 PII 掩码且无报错**
- **维度**:配置地雷 — 安全控制被静默关闭
- **触发场景**:运维在 SAE 控制台设 `RAG_QA_LOG_PII_REDACT=false`(该配置注释自称"仅用于本地调试取证")忘了改回,或把调试 env 拷进生产。
- **影响**:两个启动校验器(Gemini 守卫、指纹一致性)都不看安全标志;`config.py:837` 该标志是普通覆写,`qa_logger.py:47` 关闭时 query_text/answer_text/content_blocks_json 逐字写入 qa_session_log。持续的 PII-at-rest 泄露,零启动信号;同结构也让 prompt_injection_guard/low_confidence_guard 在生产关闭而不被察觉。
- **为何被漏掉**:qa_logger 正确遵从标志、config 正确解析——各自正确;缺陷是缺失跨模块不变量"生产必须以安全姿态运行",校验层有意止步于物理+供应商。
- **修复**:load_config 供应商守卫后加生产姿态断言:environment==production 时要求 qa_log_pii_redact 为真,否则抛 EnvironmentMismatchError。
- **与已知项区别**:F-7/F-8/F-9/F-10 是掩码**跑起来时**漏了什么;本条是掩码被一个 env 整体关掉且无护栏。
- 已确认。

**P2-28. 反 Gemini 生产守卫信任自报的 RAG_ENVIRONMENT 标签(默认 'development'),与目标守卫用的指纹检测脱钩——真实生产内容可到 Google**
- **维度**:跨供应商风险 / 守卫键于错误信号
- **触发场景**:诊断/eval 或误配部署跑在生产 RDS/OSS/HA3 端点(指纹匹配)但 `RAG_ENVIRONMENT` 未设(→'development')且只配 Gemini key。
- **影响**:供应商守卫只在自报标签 production/staging 触发(`config.py:869`,默认 development);可靠的 `PROD_FINGERPRINTS/is_prod_target` 只被目标一致性守卫用于 rds/search、从不用于供应商守卫、也不管 OSS 读。默认 dataclass 供应商全指向 Google;env-target 守卫强制 RDS/search 的 ack 却不管 OSS 读或 LLM/嵌入供应商。生产文档文本+用户 PII 被发往 Google 做 OCR/VLM/嵌入/生成。
- **为何被漏掉**:config 单看有防御(有响亮 Gemini 守卫);只有交叉引用"模块维护两种'我在不在生产'概念(指纹用于存储、标签用于供应商)且可分歧"才见洞。
- **修复**:供应商守卫也在 `is_prod_target()` 匹配任一配置的 RDS/HA3/OSS 端点时触发(与标签 OR);加 OSS 读指纹门。
- **与已知项区别**:F-31 是标签未白名单化(拼写);本条是标签是供应商决策的**错误依据**,而同模块已有指纹检测器用于别处。
- 已确认。

**P2-29. 每日 HA3 reconcile 扫描跨越整个 AUTO_INCREMENT id 空间,该空间随累积重切churn增长(非随活跃语料),故扫描成本是无界时间炸弹且无时长告警**
- **维度**:容量 / 无界增长时间炸弹
- **触发场景**:全量维护/route-v2 重切(`RAG_MAINTENANCE_ROUTING`)每次 DELETE→INSERT 每个 chunk,使 MAX(id) 永久跳增约一倍活跃量;数次迁移+10× 语料后 MAX(id)≫活跃 chunk。
- **影响**:两个生产调度的 reconciler 从 id 0 起 500 宽枚举整个 PK 空间——`ha3_reconcile.py:83`(串行,由 orchestrator 每次 stage-3 drain 前 dry-run 调)与 `reconcile.py:228`(02:30 日跑)。id 上限因 `node_write_chunk_meta`(`pipeline_nodes.py:4806`)每次重切全 DELETE→INSERT 重分配 id 而单调永增。每日 reconcile 墙钟随历史写入总量增长,占用越来越大的 DataWorks 窗口与 HA3 QPS,无任何桶数/时长指标或告警。
- **为何被漏掉**:id-churn 写者、扫描器、日调度器各自本地正确;只有全系统视图见扫描成本经 AUTO_INCREMENT 与生命周期写入量耦合。
- **修复**:把扫描界定到活跃 id 集(按 chunk_meta 现存 id 分桶),或全量迁移后周期性 RESET AUTO_INCREMENT;加 reconcile 桶数/耗时对预算的告警。
- **与已知项区别**:F-34 说该 reconciler 无生产调用者——现已有两个日调用者,缺陷是 O(MAX(id)) 扫描成本;F-36 是日志表增长,本条是扫描范围增长。
- 已确认。

**P2-30. 健康监控 parity 检查把整张 chunk_meta 与整个 HA3 索引无分页地读进 Python 3.7 PyODPS pod 内存——召回丢失安全网上的涌现 OOM 天花板**
- **维度**:容量 / 内存天花板(单 pod)
- **触发场景**:10× 语料(数百万 chunk 行)时单次作业内 `fetchall()` 构建千万级 dict 列表+完整 HA3 map。
- **影响**:`reconcile.py:310-313` 无 WHERE/LIMIT 读全表并物化为 dict 列表,`_scan_ha3_pks` 同时建全索引 map,`compute_parity` 同时持有两者,`collect_referenced_image_keys` 再遍历每活跃行 extra_json——全在受限的 3.7 监控 pod 堆里。`run_parity_check` fail-open 且是主要静默召回丢失检测器,OOM 使其在系统最可能有漂移时恰好失效。
- **为何被漏掉**:正确的 SELECT + 正确的 diff;天花板只在(无界全表读)+(并发全索引枚举)+(受限 pod)三事实叠加时出现,分散在两文件。
- **修复**:流式/分页:按 id 桶 diff RDS vs HA3(两侧已按 PK 桶),每桶用后释放,峰值 O(桶)非 O(语料);全表读用服务端游标。
- **与已知项区别**:F-36 关表增长需保留;本条是监控进程从整表+整索引读的内存峰值,与保留无关。
- 已确认(前瞻性:需 ~10×/百万行;真 OOM-kill 更可能是非零作业退出而非静默 fail-open)。

**P2-31. 无时间元数据到达 HA3、LLM 上下文或来源面板——排序、模型、用户都无法区分 2023 版 SOP 与 2025 版**
- **维度**:语料时效 / 时间治理
- **触发场景**:两个 SOP 版本或 2023 与 2025 的流程在同一 top-7 共现。
- **影响**:`chunker.py:249-268 to_ha3_doc` 不发任何日期字段;`_chunk_header`(`llm_generator.py:228-273`)只给模型 `[文档N] 标题>章节 (相关度)`——无日期;`_extract_sources`(:458-469)返回项无日期;而系统 prompt 规则5 明确要求"多文档冲突时同时说明并注明各自来源...由用户判断",却零时间信号让模型说哪边是当前。排序也时效盲(仅融合分+封面降权+多样性)。用户可能照 2023 上锁步骤操作。
- **为何被漏掉**:各文件都正确透传收到的字段,无文件"错";缺陷是共享 chunk→HA3→sources 契约里**应存在却不存在**的字段——需沿"文档年龄"这一逻辑属性跨四模块追踪并发现它在源头(to_ha3_doc)就被丢。
- **修复**:把文档日期(effective_date 或版本 published_at)串入 to_ha3_doc(可索引)、`_chunk_header`(供模型推理时效)、`_extract_sources`(供卡片渲染"文档日期:2023-06");可加时效 tiebreaker。
- **与已知项区别**:F-3/F-20 是 kNN 排序方向 bug;F-23 是日志遗漏;本条是整条 索引→上下文→sources→卡片 契约缺任何日期属性——共享载荷链缺字段。
- 已确认(降级自 P1:系统性特征缺失而非离散正确性 bug,且真实文档日期信号连捕获都没有)。

**P2-32. VLM"降级"兜底(供应商限流/弃用)从不传播到文档状态——一次瞬时 DashScope 中断被永久烘进终态 INDEXED 索引且无重处理路径**
- **维度**:单供应商模型漂移
- **触发场景**:DataWorks 日批期间 DashScope VLM 返回 429 风暴或模型弃用错误一段时间。
- **影响**:funnel 对任何 VLM 失败返回 degraded(`image_funnel_processor.py:430-439`:非公开→SENSITIVE 批量误隔离,公开→"[VLM 降级]"占位 caption)。该 degraded 标志仅在一处被遵从——持久缓存写跳过(`unified_extractor.py:1609`);无处标记文档本身,状态机也无"因供应商宕机重处理"触发。受影响文档仍完成 DAG1→2→3 到 INDEXED。VLM 恢复后无物重扫这些"DONE"文档,损坏持久到人工发现;行被误标完成。
- **为何被漏掉**:funnel 逐图 fail-open、缓存正确跳过 degraded、DAG-3 推送成功即正确标 INDEXED——各层"优雅";缺陷跨 funnel→extractor→DAG-3 涌现(瞬时供应商失败不得到达终态),无单一文件负责。
- **修复**:任何 degraded funnel 裁决上传为文档级标志;置位时阻止到达 INDEXED(或写 `content_process_status='DEGRADED_RETRY'`)使下批在供应商恢复后重处理,镜像已有的嵌入-FAILED 重处理路径(`pipeline_nodes.py:5844`)。
- **与已知项区别**:前轮证伪的"VLM JSON-fail fail-open"是未接线的 _quarantine 钩子;本条 degraded 裁决**已**接入路由与完成,缺口是无文档级传播/自愈。
- 已确认(范围限于中断窗口的文档)。

**P2-33. review_task 在生产中只写不读:三个生产者(含 spot_checker 的权限泄露安全网)注册 PENDING 人工复审任务,却无生产读者出队或告警**
- **维度**:人工复审队列所有权 / 无人值守安全兜底
- **触发场景**:spot_checker 发现某文档实时 permission 比 LLM 建议更宽松,`spot_checker.py:630` 有意**不**自动隔离("绝不谎报已隔离,只标 PENDING_DELETE + 登记 review_task 供人工")。
- **影响**:三生产者 INSERT(`cost_breaker.py:325`、`pipeline_nodes.py:1576`、`spot_checker.py:610/721`);全仓对该表的读除单测外无任何控制台端点/API/worker/ops 作业 SELECT 或 UPDATE `review_status/reviewer_user_id`。设计的人工安全网无人值守,被标为风险的过宽权限文档持续被越权投放,无出队、无老化、无 owner。
- **为何被漏掉**:逐文件只能审存在的文件;需跨文件消费者普查才发现队列无消费者。
- **修复**:加管理复审队列端点+控制台视图,按龄列 PENDING、分派/批准/拒绝并写 reviewer_*/review_status;spot_check 不匹配任务超 SLA 告警。
- **与已知项区别**:F-11 是 spot_checker 在**运行**路径写**错**值;本条是"登记 review_task 代替行动"的正确兜底因队列无消费者而失效;前轮证伪的"spot_checker 删除无状态校验"无关。
- 已确认(注:许多不匹配确会先删 HA3 再提交 RDS,故非普遍常开泄露;但兜底安全网确无人值守)。

**P2-34. 任何人工复审队列都无积压/老化/SLA 监控:ops_monitor 只覆盖 reconcile+qa_rollup,故一堆未处理的升级/review_task/踩不触发任何告警**
- **维度**:跨队列治理 — 出队率/老化 SLA 可观测性缺失
- **触发场景**:escalation 与 spot_checker review_task 累积为 PENDING。
- **影响**:`ops_monitor.py:32-48 run_all` 只接 reconcile_ha3/oss/raw + qa_rollup,grep escalation|review_task|backlog|aging|SLA 零命中;唯一有出队路径的 user_feedback(控制台手动)也无最老龄/积压指标,openCount 是客户端从至多 20 行算。无告警在"N 工单 PENDING >7 天"或"踩积压增长"时触发;能处理的管理员无信号,无人担责。结合 P1-2/P2-33,整个人机协同面可静默停摆数月而 ops 看板全绿。
- **为何被漏掉**:只有 index/OSS parity 与 QA rollup 被监控;需注意 ops_monitor 的作业集结构性省略每个人工队列。
- **修复**:加 ops_monitor 作业,对 escalation_ticket/review_task/未处理 user_feedback 的最老 PENDING 龄与积压大小告警并设 owner 与 SLA;把 feedback_miner 排真实周期。
- **与已知项区别**:F-36 是行增长/保留(数据量);本条是同表上出队率/老化/SLA 信号缺失(小队列也可能致命地陈旧)。
- 已确认(review_task 含安全关键 spot_check_unsafe 路径,停摆有真实安全后果)。

### P3 —— 中优先级

**P3-1. 权限执行不对称:邻居/扩展行经 RDS 复核(is_active+同权限),而**主命中** HA3 行不复核——任何 RDS→HA3 投影延迟按旧 ACL 逐字投放**
- **维度**:跨平面读写一致 / 缺读侧复核
- **触发场景**:任何 ACL 变更与 HA3 重投影之间的延迟,尤其 public→restricted 收紧(同部门或普遍)。
- **影响**:主命中(`retriever.py:715-754`)直接返回,唯一后过滤 `_deny_revoked_cross_dept` 只针对跨部门 dept_internal,从不复核 is_active/status/权限降级;而邻居路径过滤 `is_active=1`(:896/1126/1674)并 `_same_permission`。收紧未删除的延迟窗内基础权限降级不在读路径执行。
- **为何被漏掉**:retriever "有"权限复核,逐文件读作已防御;缺口是这些守卫只接入次级/扩展查询,不接入到达 LLM 的主结果列表。
- **修复**:把主命中在交给生成前按 RDS chunk_meta(is_active + 当前 permission/owner_dept)水合/复核,复用邻居路径逻辑。
- **与已知项区别**:F-5/F-8/F-9/F-22 均不涉及主 HA3 命中绕过 RDS 复核的结构不对称。
- 较可信(建议重限于 permission-tightening-without-delete 延迟,去掉隔离与降级触发)。

**P3-2. 架构不变量"摄取平面是 HA3/RDS 知识表的唯一写者"实质为假——服务平面写 8+ 张知识表,这正是 reconciler 被误设计为只读单写者探针的根因**
- **维度**:单写者不变量(文档 vs 现实)
- **触发场景**:任何运维/审计从架构文档推理一致性,会假设知识表状态变更经日 DAG 串行化,不去找产生 P2-1…P2-3 的服务侧写者。
- **影响**:`docs/architecture.md:136` 称"摄取平面是唯一写入者";实则服务路由直接写 chunk_meta(is_active/index_status/permission/allowed_depts)、document_meta/version(status/current_version_no)、user_role、dept_admin_grant、kb_access_request、kb_acl_projection_outbox(`kb_console.py`/`access_grants.py`/`kb_access.py`)。`reconcile.py:4-13` 的设计契约按单写者世界写("NEVER deletes")。假不变量是上游根因。
- **为何被漏掉**:逐文件从不把整系统所有权声明与所有写点之并对照;矛盾只在 grep 每个服务模块对知识库的 INSERT/UPDATE 并与"唯一写者"比对时浮现。
- **修复**:更正文档为服务平面是特定列(ACL/status/index_status)的共写者,定义每列所有权与写协议(index_status CAS、下线/降级必走 PENDING_DELETE 握手);重定 reconciler 为主动修复服务诱发的漂移。
- **与已知项区别**:不在已知列表;是解释诸具体 bug 共同根因的跨切面模型错误,与基线"无跨云 2PC"(关存储原子性)不同。
- 已确认。

**P3-3. kb_audit_log 仅靠命名约定"仅追加",无防篡改,且应用自身持有对它的 DELETE**
- **维度**:数据治理 — 审计链完整性
- **触发场景**:持 env_guard PROD-RW token 的管理员/DBA 一条 `UPDATE/DELETE FROM kb_audit_log` 抹除不当跨部门授权或越权下线的证据。
- **影响**:`schema/001:245` 是普通可变 InnoDB 表,无 prev_hash/hmac/签名/序列锚;`audit_log.py` 只 INSERT 并自称 append-only,而 `retention.py` 的 audit 作业按龄整行 DELETE——证明应用角色可变更审计行。特权操作历史可被静默改写,无链可断。
- **为何被漏掉**:audit_log 只插、retention 按龄清理——各自正确;缺陷是跨文件组合(写者承诺仅追加而兄弟模块持有并行使 DELETE)且 schema 无加密锚。
- **修复**:加 hash 链(prev_row_hash+HMAC,密钥不在 RDS 角色)由 write_audit 写、周期校验;审计清除移到单独授权角色或离线归档,serving/pipeline 角色仅 INSERT。
- **与已知项区别**:F-36 关同表容量/保留;本条关完整性/不可变。
- 较可信(唯一触发是带外特权内部人,无应用可达利用)。

**P3-4. 任何地方都无同意记录、目的限制或合法性依据捕获——QA 日志被静默复用于语料/模型优化**
- **维度**:数据治理 — 同意与目的限制
- **触发场景**:PIPL 合规审计要求富岭出示收集员工问题及二次使用(语料/eval 挖掘)的合法依据与同意证据。
- **影响**:全仓搜 consent/同意/隐私/注销 只命中无关项;schema 无同意表,钉钉认证无告知流,qa_session_log 无 purpose/legal_basis 列;而 `feedback_miner.py`/`weekly_qa_report.py`/`eval_harness` 确将 qa_session_log/user_feedback 复用于超出"回答问题"的目的。PIPL 第13/14/6条无技术支撑。
- **为何被漏掉**:同意/目的是无自然归属文件的全系统属性,逐文件正确性扫从不标记其缺席——无可指的 bug 行,只有跨 schema+认证+挖掘脚本的缺失能力。
- **修复**:建同意/目的记录子系统(记录合法依据与使用范围);二次使用前校验目的。
- **与已知项区别**:F-7/F-8 关日志 PII,F-36 关保留;本条是同意/目的子系统缺席。
- 已确认(注:PIPL 第13(2)条用工场景依据是法律论点,不构成技术记录支撑)。

**P3-5. 部门 ACL 无单一咽喉:在读两个不同后端存储的 ≥4 条代码路径中被独立重导,且只有检索读路径跑 fail-closed 撤销复核**
- **维度**:部门隔离作为系统属性 — 缺权威执行点
- **触发场景**:HA3 索引 ACL 字段与 RDS document_meta 漂移(代码自认"字段漂移"),或未来 ACL 语义变更只改一处。
- **影响**:public/owner 扩展/allowed_depts/restricted 逻辑在 `retriever.py:358`(HA3)、`:516-524`(OpenSearch DSL)、`:415-487 _deny_revoked_cross_dept`(唯一 fail-closed 复核)、`api.py:1098-1145 _resign_visible_doc_ids`(读 RDS document_meta,**不同存储**)独立重构;各处注释自称"单一来源"却被彼此存在证伪。正确性依赖 4+ 份手工同步副本跨 2 存储,一处单边编辑或存储漂移静默开/关部门边界。
- **为何被漏掉**:每份重构本地正确且自述权威,逐文件全部盖章通过;只有并排比较才见它们是跨不同存储、复核覆盖分歧的独立重导。
- **修复**:引入单一权威 `is_doc_visible(doc_id, acl_groups)`/`permission_filter(acl_groups)` 模块,由检索/本地兜底/resign-images/日志派生面共用,单一存储来源,内置撤销复核。
- **与已知项区别**:F-5/F-19/F-40 是单点 bug;本条是咽喉缺失+两存储(HA3 vs document_meta)重构分裂的系统缺口。
- 较可信(当前无可达可见性 bug;resign 读权威存储,故非更弱路径)。

**P3-6. 内存会话存储无身份/部门绑定且跨 API 与钉钉两平面共享;结构化钉钉 session key 可被已认证 API 调用者伪造,窃取他人(可能他部门)会话历史**
- **维度**:部门隔离 — 绕过检索 ACL 的跨平面会话通道
- **触发场景**:已认证营销用户获知/猜出财务用户钉钉 conversationId,构造 `<cid>:<financeStaffId>` 作 session_id 打 `/api/ask`。
- **影响**:`session_store.py:95` 单进程全局单例被 api.py 与 dingtalk_bot.py 共导,条目无 owner 字段;`api.py:576` 仅对 `miniapp:` 前缀绑所有权,其余"持有即所有";钉钉 key 是结构化 `conversation_id:sender_staff_id`(staffId 半段低熵可枚举)。get_or_create_session 返回受害者活历史,追问"总结刚才"使 LLM 复述其受限答案,不重跑 HA3 故检索 ACL 不介入。
- **为何被漏掉**:session_store 单看是正确的线程安全 LRU、api 的 miniapp 检查对其命名空间够用;缺口只在注意到同一单例服务两平面两种 key 方案且钉钉方案是结构化时可见。
- **修复**:每条目绑解析后身份(建时存 owner user_id/acl_groups,取时验);或 API 与 bot 存储命名空间隔离;拒绝 bound owner≠caller 的客户端 session_id。
- **与已知项区别**:基线仅提会话在内存,F-5 是伪造 user_id 到 /api/history;无一提会话存储本身无身份/部门绑定、跨平面共享、结构化可猜 key。
- 已确认(利用需在 30min TTL 内取得活 conversationId;结构性授权缺口已由代码证实)。

**P3-7. chunk_meta.embedding_version 是硬编码常量、与运行时解析的模型/维度脱钩,且从不被读回——被称为"重索引范围推导基石"的字段无法检测模型/维度变更**(合并 #15+#52)
- **维度**:索引与模型版本生命周期 / 溯源
- **触发场景**:运维设 `RAG_EMBEDDING_MODEL=v5`(或改维度)而不改 `versions.py`。
- **影响**:`versions.py:25 EMBEDDING_MODEL_VERSION="text-embedding-v4"` 是手工字面量;写路径(`pipeline_nodes.py:6633-6635/6671-6673`)把运行时 `chunk.embedding_model` 与该常量并列写入;docstring 称该字段是"重索引范围/受影响文档集差量所依赖的基石",但全仓 grep 显示 embedding_version/dimension **只写不读**(无 SELECT/WHERE)。模型换后新 chunk embedding_model='v5' 而 version 仍 'v4';维度变更时 version 完全不动。溯源/lineage/审计元数据静默失真,任何将来基于它的重索引范围诊断都返回错误范围。
- **为何被漏掉**:versions.py 是看似合理的常量+好 docstring、pipeline_nodes 是正常列写;缺陷是 versions.py **承诺**该字段所能启用之事 与 (a) 它不源自实时 config、(b) 无代码读它 的跨文件矛盾——需全生命周期透镜。对比 `versions.py:31 acl_policy_version()` 是内容 hash(自动变),嵌入版本却未获此待遇。
- **修复**:从实时 config 派生(如 `f"{model}@{dimension}"` 或内容 hash,仿 acl_policy_version);并真正消费它——加 `WHERE embedding_version <> :current` 的重索引范围选择器。
- **与已知项区别**:F-35 是缺 DDL 列;本条是列存在但值是与 config 脱钩的手工常量且只写不读——语义/溯源缺陷。
- 已确认(现纯潜在:无代码读它,今日无运行时错误行为)。

**P3-8. Gemini 嵌入兜底产生维度/制度不兼容的向量(无 sparse、原生维度不同),在 EMBEDDING_DIMENSION 默认不变下与 dense+sparse HA3 制度不兼容**
- **维度**:索引与模型版本生命周期
- **触发场景**:非守卫环境(SIM/LOCAL-DEV/LOCAL-EVAL)去掉 DashScope key,模型静默兜底到 `gemini-embedding-2` 而 `EMBEDDING_DIMENSION` 仍 1024。
- **影响**:`config.py:694` 兜底模型换但维度默认(:775)不变;Gemini 路径无 sparse(`pipeline_nodes.py:5798/5888-5924` 仅 dense),而整个检索制度假设 dense+sparse(`embedding_client.py:14`、`retriever.py:626-632`)。该态下建的索引/eval 持无 sparse、可能错维度的向量,与生产制度不兼容,eval 数悄然失效。爆炸半径限非生产(生产/staging 硬 raise),故 P3。
- **为何被漏掉**:config 选兜底模型、pipeline_nodes 另一分支丢 sparse、retriever 假设 sparse 存在——不兼容只在三文件作为一个制度同读时可见。
- **修复**:把嵌入维度(及 sparse 能力)绑到解析后模型而非独立 env 默认;兜底模型无法产配置维度/所需 sparse 时即便 dev 也 fail-fast,或强制 RAG_SIMULATE 哈希。
- **与已知项区别**:与证伪的 VLM/Gemini fail-open 项(关 VLM JSON/simulate 守卫)不同,本条是模型↔维度↔sparse 制度耦合。
- 已确认。

**P3-9. "统一 trace"能力未接线:钉钉(主渠道)与核心 RAG 模块不发关联 id——失败请求无法端到端重建**
- **维度**:可观测性 — 请求关联/可追溯
- **触发场景**:用户报告钉钉 bot 的错误/失败答案,运维尝试跨 bot→retriever→reranker→llm→log 重建。
- **影响**:`request_context.py:41-48 RequestIdLogFilter` 定义但**无处安装**(无 addFilter、无 `%(request_id)s` formatter);retriever/llm_generator/reranker/embedding_client 零处引用 get_request_id(grep=0);`dingtalk_bot.py:547/768` 仅在 except 内铸本地 `uuid.uuid4().hex[:8]`,与 ContextVar 分歧;stream runner 从不 set ContextVar;qa_logger base_cols 无 request_id 列,trace 仅在失败路径作 error_message 自由文本。请求中检索/生成日志携 request_id='-',无法 grep 关联;成功但错的答案完全无 id。
- **为何被漏掉**:request_context 单看干净、dingtalk_bot 的本地 trace_id "看着没事";缺口(filter 从不挂载、两 id 跨模块分歧)只在检查谁真正消费 get_request_id() 时可见。
- **修复**:部署时给 root/uvicorn handler 挂 RequestIdLogFilter 与 formatter;bot/stream 入口在请求起始 set_request_id();qa_session_log 加 request_id 列(成功失败都写)。
- **与已知项区别**:F-6/F-7 关 webhook 签名/PII 日志;无已知项涉及 trace 基建定义却未接入承载真实负载的模块与渠道。
- 已确认(失败 bot 请求确携分歧的本地 trace_id;仅成功但错的答案完全无迹)。

**P3-10. --bizdate 是名义标签,对各阶段处理哪些行毫无影响——DataWorks 无法重处理/回填指定业务日,且阶段节点兜底从容器本地 datetime.now() 推日**
- **维度**:时间/日历 — 调度器 bizdate vs 基于状态的选择;缺按日重处理能力
- **触发场景**:运维跑 `--stage 2 --bizdate 20260701` 想回填 7月1日。
- **影响**:`dataworks_orchestrator.py:52` 仅把 bizdate 塞入 env;各阶段全按状态选(stage-2 `WHERE content_process_status IN (NOT_STARTED,FAILED...)`、stage-3 `WHERE index_status IN(...)`),bizdate 从不进 WHERE,唯一消费者是溯源标注(`pipeline_nodes.py:4835`)。省略时兜底 `bizdate=(datetime.now()-1天)`(容器本地=UTC,其翻日时刻异于北京业务日)。重跑不重处理 7月1日文档,只 drain 当前 NOT_STARTED/FAILED;lineage 行携不对应文档实际日的 bizdate。
- **为何被漏掉**:各阶段"用了 bizdate"(传递、记录、入溯源),逐文件读作参数被遵从;只有端到端追踪才见它从不到达 WHERE。
- **修复**:要么让 bizdate 真实(按注册/created_at 日窗界定选择,显式时区),要么从 CLI/溯源契约移除并声明摄取纯状态 drain;若保留兜底,用固定北京偏移(UTC+8)算 T-1。
- **与已知项区别**:不在已知;F-14 关状态转换,无已知项涉及 bizdate 名义或容器本地兜底。
- 已确认。

**P3-11. judge 校准门(judge vs 人工效度)从不接入自动发布路径——即便有人工标注,results['judge_calibration'] 也只被手动文档片段填充,故每次门跑 auto-judge 的绝对效度无锚**
- **维度**:judge 未校准
- **触发场景**:任何自动门跑:面板打分,四 judge 门在 Claude 认同 Claude 上通过,无校准门抓统一宽松的面板。
- **影响**:`eval_release_gate.sh` 无 judge_calibration 调用;run_eval 从不设 `results['judge_calibration']`;`report.py:347-350` 仅 `if cal := r.get("judge_calibration")` 才发门。唯一填充路径是 `judge_calibration_DRAFT.md:16-33` 的手动三步片段。任何 `make release-gate` 跑该门从不出现;整个发布依赖的正确性认证建于未验证的 judge 上,激活需手改 report.json。
- **为何被漏掉**:需追踪 report.py 定义的门依赖一个无编排器(run_eval/门脚本)写入的 results 键——跨文档+代码的悬空能力。
- **修复**:门脚本接入 build_template/compare;或至少显式记录该门在自动路径永不激活。
- **与已知项区别**:DRAFT 状态被承认,但接线缺口(build_template/compare 只活在文档、门脚本无钩子)是新增跨切面事实。
- 已确认(团队部分知晓的治理缺口)。

**P3-12. Python 依赖在四份互异、非权威清单间浮动且无 lockfile——pip-audit CI 门认证的解析没有任何生产运行时能复现**
- **维度**:供应链 / 依赖治理
- **触发场景**:任一 SAE/监控镜像重建在某上游发布后解析到比 2026-07-04 pip-audit 基线更新(或被撤/被投毒)的传递版本;CI 仍绿(它审自己的新解析,非镜像的)。
- **影响**:无 Python lockfile,全部清单 `>=` 浮动:`pyproject.toml`、自称已死的 `requirements.txt`、`Dockerfile:23` 构建时解析最新、`dataworks_monitor.Dockerfile` 注释谎称 pinned 实则浮动;`dataworks_deployment.md:15-19` 记录的 DataWorks 包集**遗漏** dashscope/oss2/ha3 等核心 SDK。安全门提供虚假保证,运维照 runbook 重建资源组会 ImportError。
- **为何被漏掉**:每份清单本地合理(`>=` 惯用);缺陷只在把四个面+CI 门排齐、观察它们从不收敛到 pin 集且成员分歧时可见。
- **修复**:引入单一 lockfile(pip-tools `--generate-hashes` 或 uv.lock),每个运行时 `--require-hashes` 安装,pip-audit 指向 lock,更正 DataWorks 包清单为完整生产集。
- **与已知项区别**:前轮供应链项为空;F-31 与 P3 prod_access 项关配置值,非依赖清单系统。
- 已确认(降级自 P2:CI 新解析设计上对新公告变红,真正未覆盖窗较窄;最强具体危害是 runbook 遗漏核心 SDK)。

**P3-13. 摄取嵌入缓存硬上限 20000 条,语料超此后全量重嵌入抖动缓存,跨运行 OSS 镜像停止摊薄 DashScope 成本——规模上超线性成本增长**
- **维度**:容量 / 成本天花板
- **触发场景**:活跃语料/全量 route-v2 重切超 20k chunk(每 chunk 占 dense+sparse 两条,故 20k 上限仅容 ~10k chunk)。
- **影响**:`pipeline_nodes.py:5764 _CACHE_MAX_ENTRIES=20000`,`embedding_cache.py:178-206` 保留最新 N 条并把整个 sqlite 推 OSS 供 serverless 复用。语料超上限时下次 serverless 运行拉到的镜像仅覆盖最后 20k,其余重付 DashScope;每次仍全量往返被削 sqlite,收益递减。仅成本无正确性,故 P3,但是无信号的规模悬崖。
- **为何被漏掉**:上限在缓存文件内看合理;其不足只在对照语料规模轨迹与 OSS 镜像摊薄目标时可见——缓存模块、摄取调用者、预期数据量的跨关注。
- **修复**:上限随语料规模缩放(或驱逐跳过属当前活跃语料的 id);每次运行记缓存命中率,命中率崩塌时在成本爆前告警。
- **与已知项区别**:与证伪的"缓存投毒(理论)"不同(那是值);与 F-27(VLM 缓存 last-write-wins)不同存储不同失效模式。
- 较可信(驱逐在 finalize 无 drain 内抖动;真实缺陷是持久 OSS 镜像被封顶,>20k 语料重载时跨运行摊薄退化)。

**P3-14. document_meta.effective_date / expiry_date 是死 schema——DDL 声明却无任何摄取路径写、无任何检索过滤读,故任何文档永不过期**
- **维度**:语料时效 / 时间治理
- **触发场景**:行政上传一份封面写"有效期至 2024-12-31"的安全作业指导书,摄取把两列留 NULL;2026 年员工问上锁流程,过期 SOP 仍被检索并作权威回答。
- **影响**:`schema/001:80-81` 声明这两列;全仓 grep 仅命中这两行 DDL,零读写。每条注册路径(`pipeline_nodes.py:258`、`register_new_files.py:398`、`sample_corpus.py:104`)都省略它们;`_build_permission_filter`(`retriever.py:358-385`)仅发 permission/owner_dept/allowed_depts,无日期谓词。语料无任何按日期过期机制。
- **为何被漏掉**:逐文件见 DDL 就当有意/被用,见 INSERT 省列就当"默认 NULL,没事";只有跨文件追踪(DDL 声明→无写者→无读者→无过滤)才见整个能力是残桩。
- **修复**:要么注册时填这两列并给检索过滤/is_active 停用加 `(expiry_date IS NULL OR expiry_date >= CURDATE())`;要么若过期不在范围内则删列以免暗示不存在的治理保证。
- **与已知项区别**:F-37 关按 status 显式 retire 的文档、基线#3 关取代 version;本条正交——完全无任何按日期过期机制。
- 已确认(降级自 P1:无运行时误动作,是残桩/理想化 schema;无任何 populate 路径或需求佐证,是范围/特征缺口而非活缺陷)。

**P3-15. 无跨文档矛盾/取代检测——唯一跨文档机制是精确哈希去重,故两份非同一但矛盾的活跃流程都保持可检索,无物标记冲突**
- **维度**:语料时效 / 时间治理
- **触发场景**:部门以**新 doc_id** 发 2025 修订浸泡时间 SOP(重传而非改版常见),2023 旧 doc_id 从未 retire,两者共现于同一 top-7。
- **影响**:唯一跨文档一致性机制是精确内容哈希去重(`schema/005` 索引 `canonical_sha256`),只对字节相同触发;无语义/主题级冲突检查(grep re-review/contradiction/supersession 无果)。"锁定后等5分钟"vs"等15分钟"哈希不同、都 is_active=1、都进 top-7,摄取或服务时无物标记它们在安全参数上矛盾。
- **为何被漏掉**:逐文件验证去重索引与哈希比较正确;看不到"两活跃文档互相矛盾"这类问题根本无 owner——缺口由模块缺席定义。
- **修复**:加取代策略:摄取时按标题/类别/嵌入相似聚类,同主题近重复注册时要求审阅者 retire/关联旧文档;top-k 含高相似但关键参数不同的文档时提示"可能冲突来源"。
- **与已知项区别**:F-16/F-17 是精确哈希去重**机制内**的 bug;本条是"精确哈希是唯一跨文档守卫且对时效问题是错工具"——缺失能力。
- 已确认(降级自 P2:缺失能力设计缺口,已有离线手动缓解 corpus_cleanup_worklist.md;真实语料以良性格式重复为主,价值级矛盾未被数据佐证)。

**P3-16. 不存在周期性复审/文档老化作业——review_task 在分类时一次性生成,无任何 DAG 或 cron 标记老化的安全/HR SOP 复验**
- **维度**:语料时效 / 时间治理
- **触发场景**:2023 年索引的安全 SOP 从未被再触碰,无任何调度进程每季度选 N 月以上的安全/制度类文档登记 review_task。
- **影响**:`review_task`(`schema/001:285-307`)仅摄取分类时填(`pipeline_nodes.py:1578-1585` 低置信度插入),有 reviewed_at 但无 review_due/next_review 列或老化触发;`docs/ops_monitoring_schedule.md` 只排 CS3/CS4+qa_rollup,无老化文档复审作业;DAG1-4 无一计算文档龄。ISO/制度管理要求的受控流程周期复验既无数据(effective_date NULL)也无作业。
- **为何被漏掉**:这是缺失的调度组件;逐文件只能审存在的文件,缺失复审 DAG 正是此类全系统缺口——需问"什么作业应存在而不存在"。
- **修复**:加调度 DataWorks 节点,选超每类别复审间隔的受控类文档(用 effective_date)插入新 `PERIODIC_REVIEW` 型 review_task 供部门管理台;可对逾期文档降权/打标。
- **与已知项区别**:F-36 是删旧运营行;本条是相反——缺失复审老化内容的作业。
- 已确认(降级自 P2:缺失生命周期特征而非活缺陷;无规范强制周期复审)。

**P3-17. 向量嵌入无任何权威存储——只在 HA3 与一个建议性、最旧先驱逐、小到装不下语料的 SQLite 缓存里,故任何 HA3 丢失都强制全语料重嵌入并重付 DashScope**
- **维度**:派生数据可恢复性
- **触发场景**:HA3 索引损坏/误删(正是 ha3-dense-fix 事件),而缓存已驱逐旧 chunk 或在 serverless pod 上降级为不持久。
- **影响**:chunk_meta 只存嵌入**元数据**从不存向量(schema 无 dense/sparse 列);唯一另一份是显式**建议性**缓存(`_CACHE_MAX_ENTRIES=20000`,最旧先驱逐,任何 SQLite/文件系统失败降级为进程内 dict 不持久);每 chunk 占两条,20k 仅容 ~10k(计划文档称 3669 文档时无损)。重建计划自认缓存陈旧强制全语料重嵌入。重建成本无界且与语料规模成正比,且原始与重建向量代际间可能模型漂移,无权威向量可差分。
- **为何被漏掉**:embedding_cache 是正确良性降级的缓存、chunk_meta 是正确元数据表;只有跨文件问"HA3 消失后向量从哪来"才见答案是"无持久处"。
- **修复**:持久化 dense+sparse 向量(或按 model+text hash 的内容寻址向量 blob 存储)作记录源,或契约性把缓存 pin 到全语料容量+完整性溯源使重建零成本无漂移。
- **与已知项区别**:证伪的"缓存投毒"是活命中正确性;本条是持久/可恢复缺口——缓存是事实备份却容量驱逐、可降级、欠容,且根本无持久向量存储。
- 较可信(影响被夸大;至多值 DR runbook 一条:全 HA3 丢失+驱逐/降级缓存需从 chunk_meta.chunk_text 重嵌入缺失 chunk)。

**P3-18. retention.py 硬删 qa_session_log(架构文档声明的"所有问答唯一审计流水"),删前无归档/导出,一旦启用即不可逆不可恢复**
- **维度**:治理 / 审计数据删除的不可逆性
- **触发场景**:retention 进入 stage-2(DRY_RUN=False),18 月龄 qa_session_log 行被删;此后合规/法务请求某历史对话只找到聚合 rollup。
- **影响**:架构 §8.1 指定 qa_session_log 为唯一审计流;retention 的 qa_rows 作业整行物理删(`DELETE ... WHERE created_at < DATE_SUB(...)`),删前唯一守卫是 rollup 存活检查——但 qa_daily_metrics 是**聚合**非行级审计留存,无导出到 OSS/冷存的步骤。`RAG_RETENTION_ENABLE=true`+--commit 后原始审计行永久消失(叠加无 RDS PITR/版本化归档)。
- **为何被漏掉**:retention 单看是谨慎的治理模块;只有交叉引用架构"唯一审计流"指定与无归档层才见删除不可恢复。
- **修复**:qa_rows DELETE 前把行导出到 WORM/冷 OSS 归档(或分区 Parquet),仅热行受 DELETE。
- **与已知项区别**:F-36 是"无保留→无界增长",retention 是其修复;本条是该修复删**唯一审计记录**且无归档层,把增长问题转为不可逆/合规问题——相反失效模式。
- 较可信(应作治理建议:qa_rows DELETE 前加冷/WORM 归档,并承认无限保留是它本要解决的对立风险)。

**P3-19. faq_review_queue 是完全死表(零生产者、零消费者):schema 为其设计的 升级→专家答→FAQ→语料 闭环从未被构建**
- **维度**:缺失能力 / 未构建的升级↔语料闭环
- **触发场景**:即便有人手动在 DB 里答了升级,也无路径把该答案提升进语料,故同一问题对下个用户继续失败。
- **影响**:`schema/001:309 faq_review_queue` 定义完整人审+发布工作流(source_ticket_id/review_status/published_rag_key/bailian_file_id/published_at),全仓 grep 仅两处命中(CREATE TABLE 与 `architecture.md:382` 列举),无文件 INSERT/SELECT/UPDATE;`escalation_ticket.converted_to_faq` 从不被置。语料改进那半闭环不存在,KB 结构上无法从人工处理过的案例自愈。
- **为何被漏掉**:逐文件只能审存在的文件;整张无运行时足迹的表是被证实的缺席——需问"该有的能力有没有"。
- **修复**:要么实现 升级→FAQ→语料 发布器(从已答工单填 faq_review_queue、审阅 UI、发布到 HA3/Bailian、置 converted_to_faq),要么删死表并从架构文档移除该闭环暗示。
- **与已知项区别**:F-37/F-38/F-39 关**运行中**路径的正确性;本条是整张零运行时足迹的表——被证实的缺席;与 P1-2(升级死胡同)不同,因为即便升级有人值守也仍撞这堵墙(语料发布阶段另行未建)。
- 已确认(注:架构文档未真声称闭环工作,仅列举该表;宜表述为"schema 定义了从未实现的理想反馈闭环")。

## 4. 主题与系统性建议

**主题一:"写入却无人消费"是本轮最反复的结构缺陷。** 转人工工单(P1-2)、review_task(P2-33)、faq_review_queue(P3-19)、kb_retire 的下线意图(P2-1)、reconcile 检测到的丢失(P2-3)——五处都是"某文件正确地写入一条待办/状态,而承诺的下游消费者在全仓不存在"。逐文件审计对每一次写入都盖章通过,因为缺陷是消费者的缺席,不是写入的错误。**最高杠杆结构动作:对每张状态/队列表(escalation_ticket、review_task、faq_review_queue、document_version.index_status='PENDING_DELETE'/'retired')建立"生产者→消费者"契约清单与积压/老化告警(P2-34),把"有 owner 出队"设为不变量。**

**主题二:关键不变量横跨两个独立部署平面(SAE serving vs DataWorks ingest),却不归任何单一文件所有。** "查询模型=索引模型"(P2-8)、index_status 的 CAS(P2-2)、"生产必须安全姿态"(P2-27/P2-28)、跨境守卫键于目标而非标签(P2-6/P2-28)——全是两平面各自正确、组合处分叉。**结构动作:把这些跨平面契约固化为可断言的启动/readiness 检查(索引级模型指纹、姿态断言、指纹驱动的供应商守卫),并在 `docs/architecture.md` 更正"唯一写者"谬误、明确每列的所有权与写协议(P3-2)。**

**主题三:整个类别的能力从不存在。** 无按主体擦除(P2-5)、无同意/目的记录(P3-4)、无审计防篡改(P3-3)、无按日期过期(P3-14)、无跨文档矛盾检测(P3-15)、无周期复审(P3-16)、无权威向量存储(P3-17)、无 prompt/采样/检索制度的溯源(P2-20/P2-21/P2-22)。这些不是 bug,是缺失的子系统,PIPL 合规面(擦除/同意/驻留/审计)尤其集中。**结构动作:把"数据治理与可复现性"作为一个独立工作流立项,而非散落修补——一次性补齐 溯源列(prompt_hash/sampling/retrieval-regime/embedding contract)、主体擦除作业、同意记录、冷归档。**

**主题四:可观测性存在系统性"假绿"偏置——最坏的日子看板最健康。** 全局熔断把宕机报成 HEALTHY(P1-1)、零流量假绿(P2-13)、监控链本身未被调度且 fail-open(P2-14)、摄取漏斗丢失不可见(P2-15)、时区错桶使 SLO 算错日(P2-18/P2-19)。**结构动作:引入"流量下限 SLO + 死人开关心跳 + offered-vs-admitted 分母 + 摄取漏斗完整性",并把 send_ops_alert 接到每个热路径的边沿事件(熔断触顶、队列积压)。**

**主题五:回归安全网在默认调用下大面积失效或有系统性盲区。** 默认 goldset 失配使 baseline 差量网静默 expected_na(P2-23)、judge 不在制度指纹(P2-24)、五个 eval 族无基线覆盖(P2-25)、完整性 oracle 自我参照抓不到 chunker 丢弃(P2-26)、judge 校准从不接线(P3-11)。**结构动作:加一行 goldset-sha 一致性硬断言,把 judge_model/rubric 纳入制度,补齐 L0/L2/L5/L6/negatives 基线,引入独立源级完整性 oracle。**

**主题六:成本无单一账本、无单一 kill 开关。** run_budget 每批归零(P2-10)、全局熔断内存易失(P2-11)、嵌入/分解/rerank 完全无门(P2-12),叠加 reconcile 扫描随历史 churn 无界增长(P2-29)与监控 pod OOM(P2-30)。**结构动作:建单一持久支出台账(RDS 按北京日,全部 DashScope 调用者预留额度,含硬 kill 标志),并把 reconcile 扫描界定到活跃 id 集。**

## 5. 方法论脚注

本轮以系统级/跨切面/涌现透镜复核了 20 个盲区维度(含 4 个 critic 追加),其中 4 个维度经对抗验证清白(嵌入缓存投毒、VLM fail-open、prod+simulate、spot_checker 删除),16 个浮现实质风险;收到 57 条对抗验证发现,去重合并 2 对(嵌入模型契约、embedding_version 溯源)后呈现 **55 条**:P1×2、P2×33、P3×20;置信度 **CONFIRMED 49 条、PLAUSIBLE 6 条**(P3-1、P3-3、P3-5、P3-13、P3-17、P3-18)。每条均已核对相对 2026-07-01 逐文件审计为新增,并在证据偏薄时主动降级严重度或置信度;凡属已知项变体者已在成文前剔除。