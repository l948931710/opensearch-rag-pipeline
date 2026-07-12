# Agent 平台生产上线压测方案（ontology-p0 → 现网）

日期：2026-07-12 · 状态：**已实现并跑通零成本基线**（工具箱 `stress_harness/`，
基线报告见 §7）· DRAFT 门 G1–G9 待批准（§4 批准提案）

---

## 1. 目标与范围

**被测系统**：`/api/agent/ask` 企业 Agent 链路（SSE 流式 agent loop + knowledge_search
工具 + ModelGateway→DashScope + RDS run-store 持久化），部署形态为 SAE 单容器
`uvicorn --workers 1 --timeout-keep-alive 65`。上线前需要回答四个问题：

1. **容量**：单实例天花板在哪（4 并发 run / 8 工具线程池 / 20 DB 连接 / 120 threadpool
   tokens），到顶时行为是否干净（429/503 而非 5xx/挂死）？
2. **韧性**：DashScope 429/5xx/时延尖峰/空补全下，重试-熔断-恢复链路是否按设计工作，
   会不会跨 run/跨路径放大？
3. **治理**：限流/深思配额/全局日熔断在 agent 流量下是否仍然成立（agent 一次 ask
   实耗 12-17 次模型调用，计费单位错配）？告警与台账是否真的触发？
4. **运维**：实例被杀/重启后，孤儿 run 收尸、容量恢复、在线客户端解挂是否符合预期？

**两档执行**：

| 档 | 成本 | 被测面 | 替身面 | 执行者 |
|---|---|---|---|---|
| local（零成本） | 0 | 真 FastAPI/uvicorn、executor/loop/gateway/tool 栈、SSE 线协议、MySQL 持久化、限流器、投机检索、reaper | DashScope chat/embedding（mock，OpenAI 兼容含流式 tool_calls 分片与 usage 终帧）、HA3（走 OpenSearch 本地回退打 mock） | 本仓 CI / 任何开发机 |
| staging（真实计费） | ~¥50-100/趟（500 模型调用硬预算） | 一切真实（qwen3.7 真时延、HA3-stg、SAE SLB 65s 空闲超时） | 无 | 你（VPC/凭据侧），见 §6 手册 |

**不在本轮范围**：HA3 引擎自身容量（共享实例，错峰观测）、多实例横向扩容
（Dockerfile 钉死 workers=1，WS0 Redis 状态外置未完成前无意义）、ontology 工作台
高并发（steward 低频面）。

## 2. 破坏假设排行（H0–H9，按预期先破顺序）

| # | 假设 | 验证场景 | 状态 |
|---|---|---|---|
| **H0** | **冷启动并发墙旁路**：`routes/agent.py::_get_runtime` 是无锁 check-then-act 单例；冷 worker 上并发首请求各建一套 executor，4-run 墙在冷窗口内完全失效 | S9 | **已证实=F6（头条）**：16 同刻首请求 16/16 接纳、0 拒绝、agent_run 实测并发 16 |
| H1 | **投机检索先于并发准入**：被 429 拒绝的 submit 仍然各烧一次 embedding+检索（`routes/agent.py` SpeculativeSearch 构造在 `executor.submit` 之前）——拒绝风暴下检索面放大 | S2 双臂 | **已证实=F1**：spec-on 臂 1.0 检索/被拒，off 臂 0.0 |
| H2 | **共享网关熔断跨路径污染**：`RAG_SERVING_MODEL_GATEWAY=true`（Tier B，默认 off）时普通流式与 agent 共用 DashScope 调用面；agent 侧 429 风暴触发的 per-model 熔断是否殃及普通问答 | S4F2 | 探针化=F2 |
| H3 | **20 连接 DB 池是共享咽喉**：agent 每 run 数十笔短写（run/step/heartbeat/llm_call_log 同步 INSERT）与读路径、live-ACL 重读同池竞争 → 503 DB_POOL_EXHAUSTED | S3/S6 | 已压：稳态未耗尽（瓶颈在 threadpool，见 F7） |
| H4 | **空补全 5× 重试燃烧**：`empty_final_retries=5` + while 语义，退化窗口内每 run 至多 1+5 次连发模型调用 | S4c | 上限已验 |
| H5 | **429 风暴 → 熔断级联**：per-`provider:model` 熔断（5 败/30s）进程级共享，打开时所有并发 run 一起 ModelUnavailable；恢复应 ≤ 冷却+1 请求 | S4b | 已压 |
| H6 | **断连 ≠ 取消**：客户端断开后 run 照常跑完（落库在 executor 完成侧）——弃单占满 4 槽 + 白烧模型调用，容量按「槽 × run 全时长」而非在线用户数 | S5 | **已证实=G8**：断连 run 照常完成落库，僵尸窗探针 429 |
| H7 | **SIGKILL 孤儿 run**：实例被杀后 running 行悬空，reaper（间隔 300s/stale 900s 生产默认）收尸前不可见于运营面 | S8 | 已压：reaper 收尸 + 新实例满额接客 |
| H8 | **全局日熔断单位错配**：cap 按 admission 记 1 次，agent 实耗 12-17 次模型调用——2000/day 账单护栏对 agent 流量等效放大同倍数 | S7 | 量化=F4；台账/告警链路已证实 |
| H9 | **SSE 静默段空闲超时**：工具段/首 token 前无帧下发，SLB 60s/keep-alive 65s 链路上任何更短的中间层空闲超时都会掐流 | staging 档观测 | 待现场 |
| — | **混合负载尾延迟竞争**：agent 顶格时普通流式 p95 劣化 | S6 | 观测=F7（本地 1.43×） |

## 3. 场景矩阵（S0–S8）

工具箱：`stress_harness/`（纯 asyncio+httpx，无 locust/k6 新依赖）。规模档
smoke（分钟级/CI）与 full（提交基线，含 30min 浸泡）。mock 模型时延为常数
（默认 0.3s）——**local 档时延门是管线开销门**，真实模型时延门在 staging 档。

| ID | 证明什么 | 负载形状（full 档） | 关键门 |
|---|---|---|---|
| S0 | 单用户基线：帧序/落库/开销 | 1 VU × 20 run（10 深思） | 帧序违规=0；run 成功=100%；每 run 落库 1 agent_run + 2 llm_call_log + 1 tool_invocation + 1 qa_session_log；管线开销 p95 < 1.5s |
| S1 | 4-run 并发墙干净性 | 开环 0.5→6 rps 爬坡 3min | 超额全部 429（0×5xx）；「Agent 并发已满」文案；被拒不落 agent_run 行 |
| S2 | **F1 投机放大（头条）** | 同刻 40 并发 × 5 波，spec on/off 双臂 | G6：被拒 submit 的 emb 调用 ≤0.05/个——**预登记 spec-on 臂 FAIL**（实测 ≈1.0） |
| S3 | 顶格浸泡泄漏/漂移 | 6 VU 闭环 × 30min | RSS 漂移<15%；线程平稳；p95 漂移<+20%；MySQL 连接≤25；llm_call_log 记账完整 |
| S4 | 依赖故障注入 | 时延尖峰 25s；429 风暴 90s→熔断→恢复；空补全 | 风暴期 run 显式 error 帧；熔断后快速失败<1.5s；恢复≤45s；空补全≤6 调用/turn |
| S4F2 | F2 跨路径污染探针 | flag-on + agent 侧风暴 + 普通流式探针 | 测量型（记录污染形态，不判 PASS/FAIL） |
| S5 | 弃单风暴（断连≠取消） | 24 客户端首帧/首 chunk 后断开 | 服务端照常完成+落库；僵尸占槽探针 429；浪费模型调用量化（G8 观测门） |
| S6 | 混合负载公平性 | 4 agent VU + 20 普通流式 VU + 10 aux GET × 10min，对照臂无 agent | 普通流式 p95 劣化 ≤1.25×；DB_POOL_EXHAUSTED=0；aux p95<0.5s |
| S7 | 限流治理 | 限流开：6/min、深思配额、全局 cap 触顶 | 第 7 次/分 429；配额后 4xx；触顶 503 + `qa_admission_reject` 台账（≤90s 批量窗）+ 钉钉告警恰 1 次（dedup） |
| S8 | 崩溃恢复钻演 | 3 弃单长 run + 1 在线挂流 run 中途 SIGKILL → 重启（reaper 5s/10s） | 孤儿=4 被看见；在线客户端 <10s 解挂；新实例 4/4 满额接客；孤儿 ≤25s 收尸 |
| S9 | **F6 冷启动竞态（头条）** | 16 同刻首请求打冷实例（不预热） | 接纳 ≤4（**预登记 FAIL**：实测 16/16，墙旁路） |

## 4. SLO 门（既定 + DRAFT 批准提案）

**既定（已批，qa_rollup/RAG_SLO_\*，对 `/api/ask*` 权威）**：answer_rate≥0.75、
no_result_rate≤0.15、p95_latency_ms≤25000、error_rate≤0.05。

**DRAFT agent 门（本方案提出，对 `model_name='agent'` 流量；staging 实测后批准数值）**：

| 门 | 判据 | 阈值（提案） |
|---|---|---|
| G1 | TTFT（请求→session 帧）p95 | ≤ 2s（local mock 档 ≤0.3s） |
| G2 | 首 token（请求→首 chunk）p95 | ≤ 15s（staging light 档） |
| G3 | 工具段（tool_call→tool_result）p95 | ≤ 12s |
| G4 | run 总时长 p95 | ≤ 60s light / ≤120s 深思；TTL(600s) 终止数=0 |
| G5 | 错误率（error 帧+AGENT_ERROR 行 / 提交） | ≤ 0.05 |
| G6 | 429 正确性：超额全 429、被拒零 DB 行、**被拒零投机检索**（≤0.05/个） | 最后一项现状 FAIL=F1 |
| G7 | 熔断恢复：打开期快速失败<1.5s；痊愈后 ≤冷却30s+1 请求恢复 | — |
| G8 | 弃单代价：浪费模型调用/弃单（观测门，取消-on-断连是产品决策） | 报告数值 |
| G9 | 浸泡稳定：RSS<+15%/30min、线程平稳、零池耗尽 | — |

**批准动作**：G2/G3/G4 数值在 staging 档首跑后按实测 p95 × 1.5 定稿；其余可直接批。

## 5. 工具与架构

```
stress_harness/
  mockend.py    单端口 mock：DashScope chat（流式 tool_calls 分片 + include_usage 终帧 +
                429/500 窗口 + 空补全 + 按 model 过滤）· embedding · OpenSearch _search ·
                钉钉告警 webhook · 计数器（放大系数真相源）
  serverctl.py  真 uvicorn 子进程（SAE 同参）+ /proc RSS/线程采样 + SIGKILL 钻演
  sse.py        SSE 逐帧打点（ttfb/session/首chunk/工具段/done/total）+ 帧序校验 + 弃单钩子
  scenarios.py  S0–S8 声明式规格（env 覆盖 + mock 剧本 + 门）
  dbprobe.py    MySQL 真相断言（agent_run/llm_call_log/tool_invocation/qa_*、连接数、孤儿）
  gates.py      GateResult/ScenarioResult + eval_harness.metrics 复用
  report.py     report.{json,md}（eval_harness/reports 同款目录约定）
  runner.py     CLI（--scenario matrix --tier local|staging --scale smoke|full
                --budget-model-calls N）
```

关键接缝（全部现有配置面，零业务代码改动）：`RAG_LLM_API_BASE_URL` /
`RAG_EMBEDDING_API_BASE_URL` 指 mock；HA3 endpoint 留空 → 检索走
`_search_chunks_opensearch` 本地回退打 mock；`RAG_SESSION_SIGNING_KEY` 共享密钥
in-process 铸合成用户 token（不走钉钉 authcode）；`RAG_MAIN_HIT_REVALIDATE=false`
（mock 命中在权威表无行）。

**mock 修真教训**（已回归单测化）：SSE `Content-Type` 不带 charset 时 requests
`iter_lines(decode_unicode=True)` 按 latin-1 解码，中文 query 乱码 → 投机检索
`query_matches_question` 永 miss。压测自身先被这个坑咬了一口——真 DashScope 带
charset，mock 必须对齐。

## 6. 执行手册

### 6.1 local 档（零成本，随时可跑）

```bash
pip install -e ".[api,dev,production]"
bash scripts/ci_load_schema.sh          # 本地 MySQL 三库（agent 落库 fail-closed 必须可达）
make stress-smoke                        # ~1min：mock 单测 + S0
make stress-local                        # ~15min：S0-S8 smoke 规模
make stress-local SCALE=full             # 提交基线（含 30min 浸泡）
```

CI：`.github/workflows/stress.yml`（workflow_dispatch，非阻断，MySQL service
container，报告传 artifact）。

### 6.2 staging 档（真实计费，错峰执行）

**前置**：staging 侧 `RAG_AGENT_ENABLE=true`、schema 022+ 已应用、限流参数临时放开
（`RAG_RATE_USER_PER_MIN=60`、`RAG_GLOBAL_DAILY_LLM_CAP` 提到 5000 或临时关）、
用 staging `RAG_SESSION_SIGNING_KEY` 铸测试 token。

```bash
STRESS_STAGING_ACK=I_UNDERSTAND_COSTS \
STRESS_TARGET_URL=https://<staging-host> \
STRESS_TARGET_TOKEN=<Bearer> \
make stress-staging STRESS_BUDGET_MODEL_CALLS=500
```

**窗口**：22:00–24:00 CST（staging 与生产共用 RDS 实例与 HA3 硬件——错峰是硬要求）。
**顺序**：S0（10 run 基线，读 G2/G3/G4 真实数值）→ S2 单波（16 并发，验证 F1 在真
HA3/embedding 上的放大）→ S4b 单轮（真 429 熔断恢复）→ S7 告警验证（staging 侧把
cap 临时压到 30，触顶看钉钉告警真的响、`qa_admission_reject` 真的落）。
**中止判据**（任一即停）：5xx 突发 >5%；生产侧 `/api/ask` p95 > 25s（既定 SLO）；
SAE 日志出现 DB_POOL_EXHAUSTED；预算耗尽（runner 自动硬停）。
**看点**：工具段静默期是否被 SLB 掐流（H9）；`llm_call_log.latency_ms` 分布；
生产库连接数（共享实例侧压）。
**复原**：跑完立刻把限流参数改回，确认告警 dedup 窗口过期后清 stat_date 当日测试行。

### 6.3 上线节奏建议

0. **F6 整改先行（阻断级）**：`_get_runtime` 加 `threading.Lock` 双检锁——冷启动并发墙
   旁路是唯一让「4 并发」容量假设在发布/重启时刻整体失效的缺陷，必须先修；修后 S9 应转绿
   （接纳 ≤4）。
1. 本方案 local 档 full 基线跑通（G6-spec-waste / F6-cold-cap 为预登记 FAIL，其余绿）；
2. F1 整改合入（一行级：SpeculativeSearch 构造挪到 `executor.submit` 成功之后）→
   S2 复跑转绿；
3. staging 档 500 调用趟：G2/G3/G4 定稿 + H9 排除 + 告警链路实证 + F7 阈值标定；
4. 灰度（试点部门）期间每日看 `qa_daily_metrics` + `llm_call_log` 对账单口径（F4）；
5. 全量前把 `RAG_GLOBAL_DAILY_LLM_CAP` 按 F4 的实测倍率重定（按模型调用数或加权）。

## 7. 基线结果与发现（local 档）

基线报告：`stress_harness/reports/`（run 目录随每次执行生成；报告含门表、
各段时延分位、浸泡时间线数组与诚实范围声明）。**首轮 full 基线的数字以该目录
下最新 run 为准**——本节固化跨轮不变的结论性发现：

- **F6（头条·已服务端证实）**：冷启动并发墙旁路。`routes/agent.py:173-219`
  的 `_get_runtime` 是无锁 check-then-act 单例（`if _RUNTIME is None: … _RUNTIME = (…)`）。
  冷实例上 16 个**同刻**首请求实测 **16/16 全部接纳、0 拒绝**，`agent_run` 表实测并发
  重叠达 16（4-run 墙本应拒绝 12 个），三次复跑稳定复现。机理：竞态线程各建一整套
  runtime（4 槽 executor + 熔断器 + reaper 线程），末位赋值成单例、先建的泄漏但其
  executor 池仍在服务。**4-run 并发墙在冷窗口内提供零保护**——而冷窗口正是 SAE 每次
  发布/扩容/重启撞上在途流量的时刻：瞬时并发无上界，×2-17 模型调用/run 直灌 DashScope
  与 20 连接 DB 池，还叠加 F1 每 run 再拉一次投机检索、每次竞态泄漏一个 reaper 线程。
  S1 的「干净 4-run 墙」仅在首请求单发预热单例后成立。**整改（一行级）**：`_RUNTIME`
  构造包 `threading.Lock` 双检锁。这是上线前第一优先修项。
- **F1（已证实，headline）**：投机检索在 `_enforce_rate_limit` 之后、
  `executor.submit` 并发准入**之前**触发——并发墙上的每个 429 拒绝仍各烧
  1 次 embedding + 1 次检索（S2 spec-on 臂实测放大 ≈1.0/被拒；off 对照臂 ≈0）。
  40 并发突发 = 36 个拒绝但 ~40 次检索面负载。**整改**：构造挪到 submit 成功后，
  或提交前预检 executor 空位；代价是接纳 run 损失几十 ms 重叠收益。
- **F2**：`RAG_SERVING_MODEL_GATEWAY=true` 时普通流式与 agent 共用 DashScope 调用面
  与 per-model 熔断（S4F2 探针记录污染形态）。翻此 flag 上线前，把跨路径熔断隔离
  （独立 gateway 实例或按 category 分 breaker key）列为前置项。
- **F3**：xhigh/max 档的 max→plus fallback 链从 `/api/agent/ask` 不可达（端点档位
  映射仅 light/high，两档路由均单模型链）——高阶档 fallback 韧性在 serving 层是
  死配置。上线前决定：暴露档位 or 收敛路由表。
- **F4**：全局日熔断按 admission 记 1 次，agent 单 ask 实耗多次模型调用
  （本地实测下界 ≈2×/ask（单工具轮），设计上限 12-17×）——2000/day 护栏对 agent
  流量的账单保护力等效除以该倍数。cap 应改按模型调用数或加权计费单位。
- **F5（压测过程捕获）**：SSE 流式响应若 `Content-Type` 缺 charset，requests 消费端
  按 latin-1 解码中文 → 投机检索 query 匹配永 miss。真 DashScope 带 charset，
  但**自托管/代理网关**若剥 charset 会静默把投机检索优化整个变成纯浪费——
  gateway 侧建议改 `resp.encoding = "utf-8"` 强钉（backlog，非上线阻断）。
- **F7（观测）**：混合负载尾延迟竞争。4 个 agent VU 顶格时，普通流式 p95 本地实测
  从 0.67s 劣化到 0.95s（1.43×，超 1.25× 提案阈值），DB 池未耗尽（无 503）——瓶颈在
  单 worker 的 threadpool/GIL 与 agent 每 run 数十笔同步 DB 写，而非连接数。绝对值是
  mock 时延产物、方向真实。staging 档用真实模型时延标定 S6-fairness 阈值，并评估是否
  给普通问答与 agent 分独立 threadpool token 配额。

## 8. 风险与后续

| 风险/欠账 | 处置 |
|---|---|
| **F6 整改前上线（阻断级）** | 冷启动并发墙旁路让发布/重启窗口内并发无上界，直接击穿 DashScope 配额与 DB 池——`_get_runtime` 加锁是上线前置硬条件 |
| F1 整改前上线 | 拒绝风暴放大检索面（embedding QPM + HA3 CU + rerank 账单）；灰度期用户少、突发概率低，可接受但应排第二批修（F6 之后） |
| workers=1 天花板 | 4 并发 run 是产品容量上限（**前提是 F6 已修**，否则冷窗口无墙）；灰度公告/排队文案按此设计；横向扩容等 WS0 Redis 状态外置 |
| local 档未覆盖 HA3 真容量 | staging 档 S2 单波在真 HA3 上复测放大系数；HA3 CU 告警阈值调低一档过灰度期 |
| 深思(thinking)真实时延 ~24s | G4 深思档 120s 提案基于 12 轮上限估算，staging 实测后定稿 |
| 浸泡 30min 只到 G9 分辨率 | 上线后首周每日 cron 跑 smoke 矩阵（stress.yml 可加 schedule），拿 7 天趋势替代长浸泡 |
