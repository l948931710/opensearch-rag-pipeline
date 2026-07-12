# Agent 压测报告 — local-smoke

- 生成时间（UTC）: 20260712T053548Z
- 硬性门失败: 否（draft 门 FAIL 不计入，见设计文档 §4）

## 诚实范围（本轮被测/被替身）

- 真实被测面：FastAPI/uvicorn 单 worker 服务、executor/loop/ModelGateway/tool 中间件栈、SSE 线协议、MySQL 持久化（agent_run/llm_call_log/tool_invocation/qa_session_log）、限流器、投机检索、reaper。
- 替身面：DashScope chat（mock，OpenAI 兼容含流式 tool_calls）、DashScope embedding（mock）、检索走本地 OpenSearch 回退路径打 mock（生产为 HA3——HA3 引擎本身的容量不在本轮范围）。
- RAG_MAIN_HIT_REVALIDATE=false（mock 命中在权威表无行，复核会全丢弃）；该复核的 RDS 读放大未计入本轮 DB 压力。
- mock 模型时延为常数（默认 0.3s）：本报告的时延门是**管线开销门**，不是真实模型时延门——staging 档才给真实时延（见设计文档 §6）。

## 门表

| 场景 | 门 | 判据 | 阈值 | 实测 | 结果 |
|---|---|---|---|---|---|
| S0 | S0-frames | 帧序违规数 | == 0 | 0 | PASS |
| S0 | S0-success | run 成功率 | == 100% | 6/6 | PASS |
| S0 | G1 | TTFT(session 帧) p95 | ≤ 2s（本地 mock ≤ 0.3s） | 0.184 | PASS (draft) |
| S0 | S0-overhead | 管线开销 p95（扣除 mock 已知耗时） | < 1.5s | 0.190s | PASS |
| S0 | S0-db-runs | agent_run succeeded 行数 | == 6 | 6 | PASS |
| S0 | S0-db-llm | llm_call_log 行数 | == 12 | 12 | PASS |
| S0 | S0-db-tools | tool_invocation succeeded | == 6 | 6 | PASS |
| S0 | S0-db-qa | qa_session_log SUCCESS(model=agent) | == 6 | 6 | PASS |
| S1 | S1-clean-429 | 超额一律 429（无 5xx/传输错） | other == 0 | 0 | PASS |
| S1 | S1-wall-hit | 确实到达并发墙（有 429） | > 0 | 51 | PASS |
| S1 | S1-accepted-complete | 被接纳的 run 全部完成 | == accepted | 36/36 | PASS |
| S1 | S1-reject-msg | 429 文案 = Agent 并发已满 | 命中 | True | PASS |
| S1 | S1-db-no-orphan | 拒绝请求不落 agent_run 行 | rows == 36 | 36 | PASS |
| S2-on | S2-clean | 无 5xx/传输错 | == 0 | 0 | PASS |
| S2-on | G6-spec-waste | 被拒 submit 的投机检索代价（检索次数/被拒） | ≤ 0.05 | 1.0 | **FAIL** (draft) |
| S2-off | S2-clean | 无 5xx/传输错 | == 0 | 0 | PASS |
| S2-off | G6-spec-waste | 被拒 submit 的投机检索代价（检索次数/被拒） | ≤ 0.05 | 0.0 | PASS (draft) |
| S3 | G9-rss | RSS 漂移（首/末四分位均值） | < 15% | 1.7% | PASS (draft) |
| S3 | G9-threads | 线程数平稳 | |Δ| ≤ 8 | 21.0→20.0 | PASS (draft) |
| S3 | G9-p95-drift | p95 漂移（前半 vs 后半） | < +20% | 8.5% | PASS (draft) |
| S3 | S3-errors | 全程无 5xx/传输错（429 背压不计） | == 0 | 0 | PASS |
| S3 | S3-db-conns | MySQL 连接数峰值（服务池上限 20 + 探针） | ≤ 25 | 6 | PASS |
| S3 | S3-llm-account | llm_call_log 记账完整（2×成功 run） | ≥ 626 | 628 | PASS |
| S4 | S4a-survive | 模型 8.0s 尖峰下 run 正常完成 | done 且无 error | True | PASS |
| S4 | S4b-all-fail-loud | 风暴期 run 显式失败（error 帧） | == 6 | 6 | PASS |
| S4 | G7-fastfail | 熔断打开后失败为快速失败 | < 1.5s | [0.03, 0.02] | PASS (draft) |
| S4 | G7-recovery | 故障恢复后首个 run 成功 | ≤ 45s | 31.5s（ok=True） | PASS (draft) |
| S4 | S4c-empty-burn | 空补全重试上限（1 + 5 兜底） | ≤ 6 次/turn | 6 | PASS |
| S4F2 | F2-baseline | flag-on 下普通流式基线可用 | ok | True | PASS |
| S4F2 | F2-observe | 污染观测（测量型，不判 PASS/FAIL） | 记录 | failed=True fast=True | PASS (draft) |
| S5 | S5-zombie-slots | 弃单后槽位仍占用（僵尸窗内探针 429） | == 429（设计现状的量化） | 429 | PASS (draft) |
| S5 | S5-complete-anyway | 断连 run 服务端照常完成 | succeeded == 4 | 4 | PASS |
| S5 | S5-durable | 断连 run 答案照常落 qa_session_log | SUCCESS == 4 | 4 | PASS |
| S6 | S6-fairness | 普通流式 p95 劣化（mixed/control） | ≤ 1.25× | 1.131 | PASS (draft) |
| S6 | S6-db-pool | DB_POOL_EXHAUSTED 503 | == 0 | 0 | PASS |
| S6 | S6-aux | 辅助 GET p95 | < 0.5s | 0.018 | PASS (draft) |
| S7 | S7-per-min | 第 7+ 次/分钟被 429 | 前6=200 后2=429 | [200, 200, 200, 200, 200, 200, 429, 429] | PASS |
| S7 | S7-thinking-quota | 深思第 4+ 次被拒（配额 3） | 前3=200 后2=4xx | [200, 200, 200, 503, 503] | PASS |
| S7 | S7-global-cap | 触顶后 503（服务对外全拒） | 出现 503 | True | PASS |
| S7 | S7-alert-once | 全局熔断钉钉告警恰好 1 次（dedup） | == 1 | 1 | PASS |
| S7 | S7-ledger | qa_admission_reject 台账落行（≤90s 批量窗） | global-cap 行存在 | True | PASS |
| S8 | S8-orphans-seen | 被杀时留下孤儿 running 行 | == 4 | 4 | PASS |
| S8 | S8-client-unblocked | 在线 SSE 客户端被杀后快速断流（不挂死） | < 10s | 2.315 | PASS |
| S8 | S8-fresh-capacity | 新实例立即满额接客（4/4 成功） | == 4 | 4 | PASS |
| S8 | S8-reaper | 孤儿 run ≤25s 内被收尸（running→failed） | == 0 | 0 | PASS |
| S9 | F6-cold-cap | 冷实例并发首请求仍受 4-run 墙约束 | accepted ≤ 4 | 16 | **FAIL** (draft) |

## Findings

- F1 投机检索先于并发准入：spec-on 臂 24 个被拒 submit 共触发 32 次检索（1.0/被拒）；spec-off 对照臂 0.0/被拒。同题 embedding 被 query-LRU 抹平，真实混合流量下 embedding 面同倍放大。整改选项：把 SpeculativeSearch 构造移到 executor.submit 成功之后，或提交前预检 executor 空位。
- F3 xhigh/max 档的 max→plus fallback 链从 /api/agent/ask 不可达（端点档位映射仅 light/high，且两档路由均为单模型链）——高阶档 fallback 韧性在 serving 层无法演练，属死配置；上线前应决定：暴露档位 or 收敛路由表。
- F2 RAG_SERVING_MODEL_GATEWAY=true 时普通流式与 agent 共用 DashScope 调用面：agent 侧 429 风暴后普通路径探针 status=200 error='回答生成失败，请联系管理员 (trace: 9689010e)' total=0.07368561500061332s fast_fail=True——上线若翻此 flag，需把跨路径熔断隔离（独立 gateway 实例或按 category 分 breaker key）列入前置项。
- G8 弃单代价量化：4 个被接纳客户端在首帧后断开，服务端仍完成全部 run 并消耗 8 次模型调用（≈2.0 次/弃单）；僵尸窗内探针得 429（429=占满）。取消-on-断连是产品决策，上线容量按「4 槽 × run 全时长」而非「在看用户数」估算。
- F4 全局日熔断单位错配：cap 按 admission 记 1，本轮 9 次被接纳 ask 实际产生 9 次模型调用（1.0×/ask；本场景 tool_turns=0 为下界，真实 agent 12 轮上限时可达 12-17×）——2000/day 的账单护栏对 agent 流量按次计等效放大同倍数，上线前应按「模型调用数」或加权计费单位重定 cap。
- F6（头条·已服务端证实）冷启动并发墙旁路：routes/agent.py::_get_runtime 是无锁 check-then-act 单例。冷实例上 16 个**同刻**首请求实测 16 个全部被接纳、0 拒绝（agent_run 表实测并发重叠达 16，墙本应=4）——每个竞态请求各建一整套 runtime（4 槽 executor + 熔断器 + reaper 线程），末位赋值成单例、先建的泄漏但其池仍在服务。**4-run 并发墙在冷窗口内提供零保护**，而冷窗口正是 SAE 每次发布/扩容/重启撞上在途流量的时刻：瞬时并发无上界，×2-17 模型调用/run 直灌 DashScope 与 20 连接 DB 池，叠加 F1 每 run 再拉一次投机检索。S1 的「干净 4-run 墙」仅在首请求单发预热单例后成立。整改（一行级）：_RUNTIME 构造包 threading.Lock 双检锁。

## 各场景摘要

### S0 — 单用户基线（正确性 + 各段时延）
- 时长 4.7s
- runs: 6
- ok: 6
- status: {'200': 6}
- ttfb_p95_s: 0.1844585509998069
- session_p95_s: 0.1844585509998069
- first_chunk_p95_s: 0.8272055269999328
- first_tool_result_p95_s: 0.5248395259995959
- total_p95_s: 0.8600548639997214
- overhead_p95_s: 0.19005486399972138
- tool_elapsed_ms: [10, 5, 5, 7, 6, 5]
- db_runs: {'succeeded': 6}
- db_llm: {'count': 12, 'avg_ms': 333.9167, 'max_ms': 346, 'errors': 0, 'by_model': {'qwen3.7-plus': 12}}
- db_tools: {'succeeded': 6}
- db_qa: {'SUCCESS': 6}

### S1 — 并发爬坡到 4-run 墙（429 干净性）
- 时长 47.5s
- submitted: 87
- accepted: 36
- rejected_429: 51
- other_status: {}
- accepted_ok: 36
- accepted_total_p95_s: 4.188760574000298
- reject_body_sample: {"detail":"Agent 并发已满，请稍后再试"}
- db_runs: {'succeeded': 36}

### S2-on — 突发拒绝风暴（投机检索 on）
- 时长 12.0s
- submitted: 32
- accepted: 8
- rejected_429: 24
- other: 0
- emb_calls_lru_flattened: 0
- search_calls: 32
- search_per_submit: 1.0
- search_per_rejected_submit: 1.0

### S2-off — 突发拒绝风暴（投机检索 off）
- 时长 12.0s
- submitted: 32
- accepted: 8
- rejected_429: 24
- other: 0
- emb_calls_lru_flattened: 0
- search_calls: 8
- search_per_submit: 0.25
- search_per_rejected_submit: 0.0

### S3 — 顶格浸泡（RSS/线程/时延漂移 + DB 连接上限）
- 时长 243.1s
- runs: 313
- ok: 313
- hard_err_5xx_transport: 0
- backpressure_429: 0
- p95_first_half_s: 1.1769639149997602
- p95_second_half_s: 1.2769299920000776
- p95_drift: 8.5%
- rss_first_kb: 102589
- rss_last_kb: 104339
- rss_drift: 1.7%
- threads_first: 21.0
- threads_last: 20.0
- mysql_conn_max: 6
- rss_timeline_kb: （数组/明细见 report.json）
- threads_timeline: （数组/明细见 report.json）
- db_llm: {'count': 628, 'avg_ms': 522.5605, 'max_ms': 681, 'errors': 0, 'by_model': {'qwen3.7-plus': 628}}

### S4 — 依赖故障注入（尖峰 / 429 风暴熔断 / 空补全燃烧）
- 时长 52.8s
- a_spike_latency_s: 8.0
- a_total_s: 16.304294473000482
- b_storm_runs: 6
- b_errored: 6
- b_last2_total_s: [0.03, 0.02]
- b_recovery_s: 31.5
- c_llm_calls_one_run: 6
- c_run_ok: True

### S4F2 — 共享 ModelGateway 熔断跨路径污染（F2 探针）
- 时长 4.1s
- baseline_ok: True
- probe_status: 200
- probe_error: 回答生成失败，请联系管理员 (trace: 9689010e)
- probe_total_s: 0.07368561500061332
- probe_fast_fail: True

### S5 — 客户端弃单风暴（断连不取消 → 僵尸槽位 + 浪费额度）
- 时长 13.1s
- burst: 8
- accepted: 4
- rejected_429: 4
- aborted_confirmed: 4
- probe_status_during_zombies: 429
- llm_calls_after_abandon_window: 8
- db_runs: {'succeeded': 4}
- db_qa: {'SUCCESS': 4}

### S6 — 混合负载（agent 顶格时普通问答/辅助面不劣化）
- 时长 94.4s
- plain_ctl_n: 232
- plain_mix_n: 231
- plain_p95_control_s: 0.6355059309998978
- plain_p95_mixed_s: 0.7186120369997298
- plain_p95_ratio: 1.131
- aux_p95_mixed_s: 0.0180780489999961
- db_pool_503: 0

### S7 — 限流治理（6/min、深思配额、全局日熔断 → 告警/台账）
- 时长 66.9s
- per_min_statuses: [200, 200, 200, 200, 200, 200, 429, 429]
- thinking_statuses: [200, 200, 200, 503, 503]
- cap_statuses_tail: [503]
- admission_rejects: [{'date': '2026-07-12', 'reason': '__admitted__', 'count': 30}, {'date': '2026-07-12', 'reason': 'global_cap', 'count': 16}, {'date': '2026-07-12', 'reason': 'per_min', 'count': 2}]
- llm_calls_per_admitted_ask: 1.0

### S8 — 崩溃恢复（SIGKILL 中途杀 → 孤儿 run 收尸 → 立即可服务）
- 时长 14.6s
- orphans_right_after_kill: 4
- attached_client_total_s: 2.3146938499994576
- attached_transport_error: RemoteProtocolError: peer closed connection without sending complete message bod
- fresh_ok: 4
- orphans_after_reaper: 0

### S9 — 冷启动竞态（并发首请求 × 无锁运行时单例，F6）
- 时长 7.2s
- cold_burst: 16
- accepted: 16
- rejected_429: 0
- status: {'200': 16}
- db_runs: {'succeeded': 16}
