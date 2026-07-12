# Agent 压测报告 — local-smoke

- 生成时间（UTC）: 20260712T092444Z
- 硬性门失败: 否（draft 门 FAIL 不计入，见设计文档 §4）

## 诚实范围（本轮被测/被替身）

- 真实被测面：FastAPI/uvicorn 单 worker 服务、executor/loop/ModelGateway/tool 中间件栈、SSE 线协议、MySQL 持久化（agent_run/llm_call_log/tool_invocation/qa_session_log）、限流器、投机检索、reaper。
- 替身面：DashScope chat（mock，OpenAI 兼容含流式 tool_calls）、DashScope embedding（mock）、检索走本地 OpenSearch 回退路径打 mock（生产为 HA3——HA3 引擎本身的容量不在本轮范围）。
- RAG_MAIN_HIT_REVALIDATE=false（mock 命中在权威表无行，复核会全丢弃）；该复核的 RDS 读放大未计入本轮 DB 压力。
- mock 模型时延为常数（默认 0.3s）：本报告的时延门是**管线开销门**，不是真实模型时延门——staging 档才给真实时延（见设计文档 §6）。

## 门表

| 场景 | 门 | 判据 | 阈值 | 实测 | 结果 |
|---|---|---|---|---|---|
| S2-on | S2-clean | 无 5xx/传输错 | == 0 | 0 | PASS |
| S2-on | G6-spec-waste | 被拒 submit 的投机检索代价（检索次数/被拒） | ≤ 0.05 | 0.0 | PASS (draft) |
| S2-off | S2-clean | 无 5xx/传输错 | == 0 | 0 | PASS |
| S2-off | G6-spec-waste | 被拒 submit 的投机检索代价（检索次数/被拒） | ≤ 0.05 | 0.0 | PASS (draft) |

## Findings

- F1 投机检索先于并发准入：spec-on 臂 24 个被拒 submit 共触发 8 次检索（0.0/被拒）；spec-off 对照臂 0.0/被拒。同题 embedding 被 query-LRU 抹平，真实混合流量下 embedding 面同倍放大。整改选项：把 SpeculativeSearch 构造移到 executor.submit 成功之后，或提交前预检 executor 空位。

## 各场景摘要

### S2-on — 突发拒绝风暴（投机检索 on）
- 时长 12.0s
- submitted: 32
- accepted: 8
- rejected_429: 24
- other: 0
- emb_calls_lru_flattened: 0
- search_calls: 8
- search_per_submit: 0.25
- search_per_rejected_submit: 0.0

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
