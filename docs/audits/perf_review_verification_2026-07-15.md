# 性能架构评审逐条核查报告

> 核查对象:`opensearch-agent-performance-architecture-review-ontology-p0.md`(外部性能架构评审)
> 评审声称基线:`a2a8c708ebbcc8153b3926097ea70bbce840a8c9`(`claude/ontology-p0`)
> 核查基线:同一 commit(在其上拉独立 worktree 逐行核对)
> 核查方法:5 个独立子代理分域核对,每条断言对着真实代码数清 SQL/事务/轮询次数,指令为"核对或推翻算术",非附和
> 日期:2026-07-15

---

## 0. 总判决

**17 条可核验的事实性断言中,16 条完全属实,1 条(§4.6 压测数字)部分属实。**

- 代码级事实扎实、算术精确,这份评审可信,可以照它排 P0。
- **6 处报告实际低估了问题严重度**(见 §3),批次 A 的收益只会比承诺更高。
- 只有 **§4.6 的经验数字 1.43× p95 劣化没有仓库产物支撑,且被仓库里唯一一次实测(1.131× PASS)反证**——这是唯一需要打折扣的结论(见 §2)。
- 2 处轻微措辞高估(§4.8 spec_hit、§6.4 QaView),不影响结论。

> 注:下列 `file:line` 均在 `claude/ontology-p0` 分支;`main` 上多数文件不存在,查看需先 `git checkout claude/ontology-p0`。

---

## 1. 逐条核对表

| # | 章节 / 问题 | 判决 | 决定性证据 |
|---|---|---|---|
| 1 | §4.1 workbench N+1(251/302 SQL) | ✅ 属实(**低估**) | `routes/ontology.py:256` 逐 case 裸循环,无 batch/join;每个 store 调用一次往返(`store.py:286/1274/1375` 均单 SQL 无缓存)。算术精确:kb_admin `1+50×(1+3+1)=251`、dept_admin `1+1+50×(1+1+3+1)=302`。stewardship **逐 case 重读**:kb_admin 50 次全表扫、dept_admin **101 次**、零去重;`limit=200`(`store.py:1222` 允许)时 kb_admin ~1001 |
| 2 | §4.2 `packing_calc._select_rule`(201 SQL) | ✅ 属实(worst-case) | 无 `rule_ref` 分支 `packing_calc.py:205-223`:`find_objects(...,limit=200)` 取≤200 条(`:206`),再逐条 `get_object`(`:209`);首投影缺 `golden_json`(`store.py:306-308` + 注释)。201 = 1+200 是理论上限;实际 = 1+N(active `calc_rule` 很少,pilot 多为 public) |
| 3 | §4.3后端 `/api/agent/runs/{id}` 5读 / 0.8 QPS | ✅ 属实(精确) | `routes/agent.py:1090-1122` 五个独立查询(get_run/list_steps/list_invocations/get_latest_by_run/fetch_answer),各自开 `_get_db_conn`。活跃/挂起 run = 4读÷5s = **正好 0.8 QPS**;第 5 读确为 `succeeded` 条件触发(succeeded 瞬时 1.0,随即终态停轮询) |
| 4 | §4.6 单 worker 争用 / **p95 劣化 1.43×** | ⚠️ **部分属实** | 见 §2 专项 |
| 5 | §4.4 delta/SSE/relay 无合并 | ✅ 属实 | 三跳零合并:`model_gateway.py:236`(每 provider 帧一个 StreamDelta)→`loop.py:275`(1:1 ModelDelta)→`routes/agent.py:430`(逐帧 `json.dumps` SSE);`event_relay.py:78-80` 每事件 `XADD`+`EXPIRE`(TTL 每事件重置);relay 默认关(`RAG_AGENT_EVENT_RELAY=="redis"` 才启,`:46-54`) |
| 6 | §4.5 每模型轮 4 提交 / 6 SQL | ✅ 属实(**低估**) | `executor.py:353-355` 三个独立 commit(heartbeat / append_step=SELECT MAX+INSERT / consume_budget=UPDATE+SELECT) + LLM-call 日志(`model_gateway.py` → `run_store.py:723`)= **4 commit / 6 SQL**,均独立事务未批。工具轮多 1 commit(`:376`),另有 30s 心跳 ticker(`:622`) |
| 7 | §6.2 Agent 全量重渲染 O(n²) | ✅ 属实 | `useAgentAsk.ts:234` `scheduleAgentRender` 每 80ms 对**全量** `ai.raw` 重跑 `renderMd(stripImg())`;`:230` 注释明说不复用增量缓存。O(n²) 定性公允(80ms 节流只降常数,不改量级) |
| 8 | §6.3 `_pollRunOnce` 全扫 + 无变更持久化 | ✅ 属实 | `useAgentAsk.ts:550-560` 双层遍历全部 conversations×messages 定位 run,无 early break;`:544` 浅拷贝整个 details map;`:561` 每次成功轮询**无条件** `schedulePersist()`(400ms debounce 仅缓和不消除,单发 5s 轮询仍触发一次全量 `JSON.stringify`+`localStorage.setItem`) |
| 9 | §4.8 推测检索浪费 + 缺指标 | ✅ 属实(轻微高估) | admission 后才启(`executor.py:141` `_acquire` 先于 `:147` `spec.start()`,设计正确);`knowledge_search.py:290-303` 每 admitted run 一次全量 `retrieve_and_enrich`,**无 cancel 路径**(grep `.cancel()` 无),模型走别的工具则 future 空跑丢弃;fusion 默认开(`config.py:243`)→`retriever.py:859` `ThreadPoolExecutor(max_workers=3)` 2-3 路 HA3。**轻微高估**:命中有 `receipt["speculative"]=True`(`knowledge_search.py:89`)可事后计数,确无 started/miss/wasted-ms 指标 |
| 10 | §4.7 embedding 池 800 / ~80 批 | ✅ 属实(冷缓存) | `resolve.py:57` 4 类 × `:331` 200 = 800(上界,先过机密过滤+标题去重);DashScope batch=10(`config.py:874`)→ ≤200/10×4 ≈ **80 批** + 1 query embedding。持久(SQLite WAL 单例)+ 内存(cap 4096)缓存确实存在,报告已正确限定冷缓存/P2 |
| 11 | §6.5 小程序非流式 + ~7s 打字机 | ✅ 属实 | `fuling-rag-miniapp/utils/api.js:112` 缓冲式 `/api/ask`(`:7-8` 注释 "dd.httpRequest BUFFERED ONLY");`answer-bubble.js:152-158` 自适应封顶 ~7s(`charsPerTick = ceil(totalChars×24/7000)`)。主要延迟是首屏等待非动画 |
| 12 | §5 `agent_run(user_id,started_at)` 缺索引 | ✅ 属实(**低估**) | `run_store.py:663-669` `WHERE user_id=%s ORDER BY started_at DESC`;`schema/022_agent_runtime.sql:48-50` 仅 PK(run_id)/idx_thread/idx_status_hb——**连 `user_id` 单列索引都没有**,036/037 ALTER 也未加 → 全扫 + filesort,比"缺复合索引"更糟 |
| 13 | §5 ontology 翻页缺复合索引 | ✅ 属实 | `store.py:1205-1270` `list_open_cases` keyset 排序 `seen_count DESC,last_seen_at DESC,case_id DESC`;`schema/028_ontology_identity.sql:69-72` 只有 2 列前缀 `idx_status_freq(status,seen_count)`,不覆盖 `last_seen_at`/`case_id` tie-breaker |
| 14 | §5 `SUBSTRING_INDEX(namespace,':',1)` 非 sargable | ✅ 属实 | `store.py:1258` 在 `scope_filter` WHERE 的 OR 子句里对列包函数,废掉任何 namespace 索引 |
| 15 | §6.1 前端构建体积 | ✅ 属实(几乎逐字节) | 实跑 `npm ci` + `vite build`:**250 模块(精确)**;ManageView 197.34/54.99KB、vendor 104.38/40.55、index 80.54/28.72、Qa 47.13/14.48、useAgentAsk 12.97/5.62——**全部精确吻合**;构建耗时 618ms vs 报告 747ms(机器相关,不重要)。ManageView 是最大业务 chunk 且懒加载(`router/index.ts:5`) |
| 16 | §6.2 useAsk 增量 vs Agent 全量 | ✅ 属实 | `useAsk.ts:230-266` 增量 strip/render 缓存;`:194` 自注释旧法为 O(n²)。与 #7 互证 |
| 17 | §6.4 router 无差别预取三视图 | ✅ 属实(轻微) | `router/index.ts:30` 无角色/连接判断预取 Qa/Manage/Contribute;grep `saveData`/`effectiveType`/`navigator.connection` 无。**轻微**:QaView 是默认路由 `/` 通常已加载,`import()` 幂等,实际增量只有 Manage+Contribute |

---

## 2. 专项:§4.6 那个 1.43× 压测数字(唯一打折扣项)

拆两半看:

**代码事实全真:**
- `Dockerfile:76` 确实钉死 `uvicorn --workers 1`,原因在 `:65-69` 写明(session_store + AWAITING_COMMENT 进程内内存)。
- Agent 默认并发 4:`agent_runtime/executor.py:107` `max_concurrent=4`(背后 `ThreadPoolExecutor(max_workers=4)`),`routes/agent.py:298` `RAG_AGENT_MAX_CONCURRENT_RUNS` 默认 `"4"`。
- 仓库里**真有**一条正好测这个的混合压测线:`stress_harness/scenarios.py` 的 **S6(混合负载公平性)**,驱动 4 agent VU 对普通流式 + aux GET,算 `plain_p95_ratio = mixed/control`,gate 卡 **≤1.25×**(`:636-638`),跟踪 `db_pool_503`(`:634`),超阈值报 "F7" finding,模板措辞与报告几乎一字不差。

**但那组具体数字没有仓库产物支撑,且被唯一一次实测反证:**
- 报告的 **0.67→0.95s / 1.43× / "已超过 1.25× 门槛"** 在任何已提交产物里都搜不到(grep `0.95`/`1.43` 近 p95 无命中)。
- 仓库里**唯一**一次真实 S6 跑分 `stress_harness/reports/run_20260712T053548Z_local-smoke/report.{json,md}`:`plain_p95_control_s=0.636`、`plain_p95_mixed_s=0.719`、`plain_p95_ratio=1.131` → **PASS、低于门槛**、`db_pool_503=0`(另 4 个 report 目录只跑了 S9/空,无 S6)。

**结论:** "连接池没打满 → 瓶颈是单进程/GIL/同步写争用"这个**方向性判断**被 `db_pool_503=0` 佐证,站得住;但 **"1.43×、已超发布门槛"这个量级无来源,且与仓库唯一实测(1.131× 通过)相反**。大概率是评审者本机的 local-mock 数(机器相关、不可从仓库复现)。**别当成"已压穿门槛"的既成事实**——要拿它决策须在 staging 用真实依赖重跑 S6 定量。这也呼应报告 §1/§10 自己的免责声明("未用真实 RDS/HA3/DashScope 做等价压测")。

---

## 3. 报告"低估"清单(实际比写的更严重)

1. **§4.1 workbench**:stewardship 逐 case 重读、零去重,dept_admin 达 101 次全表扫;`limit=200` 时 kb_admin ~1001 SQL。
2. **§4.5 每轮写**:工具轮再多 1 commit + `record_invocation`/`finish_invocation`,另有 30s 后台心跳 ticker,实际写压高于 4 commit/6 SQL。
3. **§5 agent_run 索引**:连 `user_id` 单列索引都没有,不止"缺复合索引"——用户 run 列表是全扫 + filesort。

## 4. 报告"轻微高估"清单(不影响结论)

1. **§4.8**:命中有 `receipt["speculative"]=True` 可事后统计,缺的是 started/miss/wasted-ms,不是"完全无命中指标"。
2. **§6.4**:QaView 通常已加载,实际预取浪费只有 Manage+Contribute。

---

## 5. 建议(落到执行)

1. **报告可信,可照它排 P0**。§4.1/§4.2/§4.3 的 N+1 与轮询放大是确定性、算术精确的真问题;§4.1/§4.5/§5 实际更严重,批次 A 收益更高、风险更低。
2. **唯一别照单全收的是 §4.6 的 1.43×**——量级无来源、被 1.131× 实测反证;用它当"已超门槛"结论前先在真实依赖环境重跑 S6。
3. 回签评审时,把 §4.8 / §6.4 两处措辞按 §4 修正。

---

## 附:核查方法

- 在评审基线 commit `a2a8c70` 上 `git worktree add` 拉独立工作区,避免污染 `main`。
- 5 个独立子代理分域(ontology 后端 / agent 后端 / agent 前端 / 流式管线 / 基建·构建·小程序),各自读真实代码、数 SQL/事务/轮询、跑 `vite build` 核对体积,指令为"核对或推翻算术"。
- 全程只读源码;仅子代理 5 在 worktree 内 `npm ci` + `vite build`(构建产物,无源码/git 变更)。核查后 worktree 已移除。
