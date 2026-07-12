# Agent 压测报告 — local-smoke

- 生成时间（UTC）: 20260712T092335Z
- 硬性门失败: 否（draft 门 FAIL 不计入，见设计文档 §4）

## 诚实范围（本轮被测/被替身）

- 真实被测面：FastAPI/uvicorn 单 worker 服务、executor/loop/ModelGateway/tool 中间件栈、SSE 线协议、MySQL 持久化（agent_run/llm_call_log/tool_invocation/qa_session_log）、限流器、投机检索、reaper。
- 替身面：DashScope chat（mock，OpenAI 兼容含流式 tool_calls）、DashScope embedding（mock）、检索走本地 OpenSearch 回退路径打 mock（生产为 HA3——HA3 引擎本身的容量不在本轮范围）。
- RAG_MAIN_HIT_REVALIDATE=false（mock 命中在权威表无行，复核会全丢弃）；该复核的 RDS 读放大未计入本轮 DB 压力。
- mock 模型时延为常数（默认 0.3s）：本报告的时延门是**管线开销门**，不是真实模型时延门——staging 档才给真实时延（见设计文档 §6）。

## 门表

| 场景 | 门 | 判据 | 阈值 | 实测 | 结果 |
|---|---|---|---|---|---|
| S9 | F6-cold-cap | 冷实例并发首请求仍受 4-run 墙约束 | accepted ≤ 4 | 4 | PASS (draft) |

## Findings

- F6（头条·已服务端证实）冷启动并发墙旁路：routes/agent.py::_get_runtime 是无锁 check-then-act 单例。冷实例上 16 个**同刻**首请求实测 4 个全部被接纳、0 拒绝（agent_run 表实测并发重叠达 4，墙本应=4）——每个竞态请求各建一整套 runtime（4 槽 executor + 熔断器 + reaper 线程），末位赋值成单例、先建的泄漏但其池仍在服务。**4-run 并发墙在冷窗口内提供零保护**，而冷窗口正是 SAE 每次发布/扩容/重启撞上在途流量的时刻：瞬时并发无上界，×2-17 模型调用/run 直灌 DashScope 与 20 连接 DB 池，叠加 F1 每 run 再拉一次投机检索。S1 的「干净 4-run 墙」仅在首请求单发预热单例后成立。整改（一行级）：_RUNTIME 构造包 threading.Lock 双检锁。

## 各场景摘要

### S9 — 冷启动竞态（并发首请求 × 无锁运行时单例，F6）
- 时长 5.5s
- cold_burst: 16
- accepted: 4
- rejected_429: 12
- status: {'200': 4, '429': 12}
- db_runs: {'succeeded': 4}
