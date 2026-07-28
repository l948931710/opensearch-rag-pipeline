# 「企业 Agent 性能与处理效率补充审查」逐条核查台账

> 日期：2026-07-17
> 核查对象：`~/Downloads/enterprise_agent_performance_efficiency_review_2026-07-16.md`（外部评审，基线 `7041eba`，分支 `claude/ontology-p0`）
> 核查基准：分支 HEAD `e1211cd`（评审基线之后另有 15 个提交：外审修复批次 1-4 `b74958a/8ae61cf/68bf603/d97f964`、PR-3 Stage A+B `29e9124/e4abd26` 等）
> 方法：29 个子代理分 6 簇对代码逐条核查 + 全部关键/非 CONFIRMED 结论对抗复核 + 补漏扫描；本台账中的行号/默认值均经主会话独立抽查确认（并发默认值、投机指标、预算闸、LIMIT 1000、COUNT(*)、fusion 默认开、线程池位置）。

---

## 一、总判决

**事实层：高度可信。** 42 项可证伪断言中 0 项被推翻——并发默认值表（§3.1）逐项精确、Stage 3 `LIMIT 1000`/每轮 `COUNT(*)`/嵌套 VLM 并发/每请求线程池/单 worker 约束/`max_tokens=None`（评审时点）全部属实，§14 证据文件清单 10 条全部存在且角色相符。

**优先级与必要性层：显著高估。** 三类系统性折扣：

1. **评审基线上就已存在的东西被当成缺口**（评审自己的 SHA 上即可证伪）：
   - §5.2「必须增加」的投机检索指标，5 项中 4 项（started/hit(consumed)/miss/wasted_ms/HA3 臂数）在 `7041eba` 上就有（perf 批次 A §4.8，`27f08e2`，`knowledge_search.py:364` finalize + `executor.py:612` run 末必打）。真实残余仅 `latency_saved_ms` 派生指标与按意图门控。
   - §3.5/§3.7 的「近期」观测项（embedding 缓存命中率打印+容量告警、reconcile `buckets_scanned/elapsed_s`+时长告警）在基线前即已落地（P3-13/P2-9/P2-29）。
   - **§5.1 评审第一优先建议「知识问答默认走普通 RAG」描述的就是现状**：钉钉 bot 直调 `retrieve_and_enrich`（`dingtalk_bot.py:909`，全文件无 agent 路径）、小程序只调 `/api/ask`、console 问答默认 `/api/ask/stream`；agent 是双重 opt-in（`RAG_AGENT_ENABLE` 默认 off→端点 404 + 前端 agentMode 默认 false 且 404 自动回退）。「控制台显式模式」子项已存在，「钉钉轻量路由」的对象不存在。该建议真实增量≈0。

2. **评审基线之后（07-16/17）已落地的项**：
   - §5.3 预算/`max_tokens`：`68bf603`（批次 3 P1-05）落地 pre-call 预算闸（费用未发生即拦）+ 临近耗尽 `max_tokens=max(FLOOR, remaining)` + resume durable 种子 + 逐调用记账（schema/023 早已有）。与评审建议的唯一差异是有意改良（正常调用维持 None=行为零变化、FLOOR 防截碎）。残余：tool-planning 单独小上限（NICE_TO_HAVE）。
   - §5.4 P0 前置：四个 Agent 运行态 P0 已全部在 `b74958a` 修复（且外审核查台账已将四项全部降级 P1/P2——现网 flag 全暗+reaper 自愈）；per-tool 并发舱壁（评审建议的过渡形态）已随批次 2 落地（`tool_executor.py:237`）。
   - HIGH_WRITE durable worker（§5.4/第四阶段）：与仓库既有 **PR-3 立项完全同向**，Stage A+B（outbox+lease，`29e9124/e4abd26`，flag 默认 off）已落地；剩 Stage C（HIGH_WRITE tool ledger）/D（多副本）——按既有台账推进即可，无需按评审另立项。

3. **前提被反证的建议**（2 项，UNNECESSARY）：
   - **§3.4 `COUNT(*)`→`EXISTS`**：成本前提错误——三个 stage 的 COUNT 谓词全部有索引支撑（`idx_index_status` schema/001:236、`idx_content_process`:178、`uk_doc_version`:174），成本随 **pending 行数**增长而非全表；每 1000-chunk 批才跑一次，相对批内 embedding/push 可忽略。且精确计数是 **no-progress 守卫的单调信号**（`dataworks_orchestrator.py:788-794`；stage-1 无原子认领，文档串明示该守卫「必需」）——换 EXISTS 会削弱一个在用的安全机制。
   - **§3.6 分类结果缓存**：主场景（维护重切）已被更强机制覆盖——`frozen_routing` 是强制、fail-closed、零 LLM 的确定性复用；sha 键缓存会与 `RAG_ALLOW_UNFROZEN_RECHUNK`「有意重掷」token 语义冲突（授权重分类时静默返回旧 category），且前置的 prompt/taxonomy 版本纪律仓库不存在（taxonomy 是节点内硬编码 dict），漏 bump 即静默钉死。评审自设的落地条件（生产日志证明重复分类量足够）也无证据。

另有一处**与现网现实矛盾**：§4.3「reranker 不应全局开启」——现网 rerank 已全局 ON（`.env.production.template:29-41`，有意决策 +10.5pp recall@1）。困难度触发是合理的省成本思路，但当前 rerank 线的真问题是阈值重标定（「匹配度低」banner，已立项待金集复标），先动阈值再谈触发策略。

---

## 二、真正立得住的增量（按价值排序）

| 项 | 裁决 | 说明 |
|---|---|---|
| **摄取写侧 lease/fencing 先于扩并发**（§2 P0 前置#1 / §10） | **NECESSARY** | 与 CLAUDE.md 自认 open gap 一致：stage-1/2/3 只有 2h 年龄式失效锁、无运行中续租（全仓无 heartbeat/续租代码），>2h 存活运行可被接管。**注意 PR-3 的 lease 只覆盖 agent 命令面，不覆盖摄取两本台账**。当前默认并发 1+单实例日跑风险可容忍；一旦启用并发开关，此项变硬前置。评审最有价值的一条顺序约束。 |
| **全局模型调用并发预算**（§3.2/A10） | NECESSARY（与上同批） | 嵌套乘法属实且比评审说的更深：extract × VLM(8) 之外还有 **OCR 页并发(4)** 也共享同一 DashScope 账号（`ocr_client.py:357`，评审漏了这层）。现无任何跨文档全局 in-flight cap（cost_breaker 是金额熔断非并发闸）。默认 extract=1 时峰值仅 8 路，风险未现实化——作为「开并发前的前置」成立，作为「现在就做」不成立。 |
| **Agent 执行所有权按进程分裂**（§6.1/G4） | NECESSARY＝PR-3 Stage D | 事实属实（`_RUNTIME` 每进程单例、4-run 墙每进程一份）；是扩 replicas 的真前置，但就是在途 PR-3 的既定内容，重名不重工。 |
| Stage 3 按 (doc_id,version_no) 组批（§3.3/B5） | NICE_TO_HAVE | 正确性已由边界完整性闸全保（残留未索引即推迟停用+版本复位 NOT_INDEXED，`pipeline_nodes.py:5746-6027`）；效率代价有界：已 INDEXED 的头部**不会**重复 embed/push（重选谓词只挑 NOT_INDEXED/FAILED），每批至多 1 个跨批文档，代价=状态多翻几次+停用推迟一轮。值得做但收益中等。 |
| 请求级权威快照（§4.1/D9） | NICE_TO_HAVE | 碎片化属实：典型 3-4 次 checkout（deny/revalidate/stitch+expand 共享域/日期），multi-query 开启时上限超评审的 4（每臂各 revalidate）。但池层已缓解（perf#13/14 pool 扩容+非阻塞逃生）、QA 写已后台化、三类读语义各异（fail-closed/fail-open/fail-open）不宜硬拼 JOIN。顺手做：先把 `_attach_doc_dates` 并进既有 F#60 thread-local 域。 |
| 模块级有界池替代每请求池（§4.2/D6/D10） | NICE_TO_HAVE | 属实：`retriever.py:865/2174/2287/2347` 四处 with-块每请求建池。**其中 fusion 三臂默认 ON**（`config.py:248 client_fusion_enable=True`）=每个默认路径请求建销一个 3 线程池；其余三处 flag 默认 off。agent 侧投机池已是模块级共享（`routes/agent.py:109-123`）。低 QPS 下影响小，做的话优先 fusion 池。 |
| 观测补缺（§8/F5） | NICE_TO_HAVE | 已有覆盖率比评审呈现的高得多：serving 三段耗时/日 p50-p95+SLO/429 拒绝台账/agent tokens+步耗时+投机 hit-waste/缓存命中率/request_id 全链路皆在。真缺：TTFT、HA3 逐臂耗时、rerank 耗时、pool wait、峰值内存、cost/run 价表（价表+llm_call_log 生产建表 user-gated——不编造单价）。 |
| Embedding 缓存容量核对（§3.5/C1） | 事实属实,观测已有 | 默认 20k 条、dense+sparse 分开占位≈1 万 chunk 属实（现网 ~1.8 万 chunk 若一次全量重灌会驱逐）；但命中率打印+容量压力 ops 告警已有，评审「按指标调」的前提已满足,真需要时调 env 即可。 |
| HA3 稳定 projection key + tombstone/outbox（§3.7/C6） | NICE_TO_HAVE(远期) | 「近期」三条基线前已全做；「全扫降为安全网」很大程度已是现状（orphan 全扫默认 dry-run,真删走 PENDING_DELETE 定向对账）。真残余=同版本重切孤儿的定向 tombstone——比换 PK 便宜得多,应先做;换 PK 语义=HA3 重建表级动作,触发条件（1800s 扫描告警）从未命中。 |

## 三、USER_GATED（评审 P1 主推,但本就是等拍板的既定尾项）

摄取并发开关全套（§3.1）：`RAG_EXTRACT/LOADER_FETCH/PUBLISH/HA3_PUSH/EMBED_CONCURRENCY` 保守默认是**有意设计**,`docs/perf_optimization_backlog.md` 明示「DataWorks 节点自行开启」为 user-gated 尾项,当前无任何 DataWorks 节点脚本设这些变量。评审的灰度序列（一次一维、先 2 后 4、盯 429）与仓库自身框架一致,可作为开启时的操作剧本——但**开启顺序上应先补摄取侧 lease/fencing（上表第 1 项）**,这点评审排序正确。

多实例（§6.1）：单 worker 枚举属实,但 Redis session/限流后端（WS0,含 Lua 原子+fail-closed）已建成仅默认关——「先外置再扩」的剩余工作主要是部署决策+双实例演练,非新代码。

## 四、逐条 verdict 汇总

CONFIRMED 30 / PARTIAL 10（均为「事实对、定性或前提有折扣」）/ STALE_ALREADY_DONE 2（E5 预算闸、E7 四 P0 前置）/ REFUTED 0。
必要性分布：NECESSARY 3（G5 摄取 lease、A8+A10 并发预算——均为「开并发前置」而非立即项;G4=PR-3 既定）/ ALREADY_DONE 11 / USER_GATED 7 / NICE_TO_HAVE 12 / UNNECESSARY 2（B6 EXISTS 换 COUNT、C3 分类缓存）/ 其余 N_A（纯事实项）。

§11「明确不建议的过度设计」清单与仓库既有拍板全部一致（不上 Kafka/Temporal/async 重写/不缓存最终回答/不直接加 worker）,无异议。

## 五、评审自身的方法论问题（供下次对外审校准）

1. 未核对评审基线上已存在的机制就下「必须增加」（投机指标、缓存观测、reconcile 观测三处),也未发现第一建议描述的就是现状——**对 flag 门控的暗线一律当成了默认路径**。
2. 两处成本推断未查 schema 索引/守卫用途即下结论（COUNT(*)、分类缓存）。
3. 与部署现实脱节一处（rerank 全局 ON）。
4. 但相对 07-15 那份性能评审（16/17 属实、1 处编造数字）,这份**没有编造数字**,量级判断均带条件句,结构上可信度更高。
