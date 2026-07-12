# stress_harness — agent 平台生产上线压测工具箱

设计文档（场景矩阵/门/执行手册/发现）：`docs/agent_stress_test_launch_plan_2026-07-12.md`

## 快速开始（零成本本地档）

```bash
pip install -e ".[api,dev,production]"
# 本地 MySQL（三库 + 全量 schema；agent run 落库 fail-closed，必须可达）
bash scripts/ci_load_schema.sh

make stress-smoke                 # mock 单测 + S0 基线（~1 分钟）
make stress-local                 # 全矩阵 S0-S9 smoke 规模（~12 分钟）
make stress-local SCALE=full      # 提交基线用（含 30 分钟浸泡）
```

报告落 `stress_harness/reports/run_<ts>_<tag>/report.{json,md}`（同目录 `logs/` 存各场景
服务端日志）。退出码：0=非 draft 门全绿；2=任一非 draft 门 FAIL。

## 架构（一图）

```
runner ──┬─ MockBackend（单端口线程服务器）
         │    ├ /…/chat/completions   mock DashScope（流式 tool_calls + usage 终帧 + 故障注入）
         │    ├ /…/services/embeddings mock 原生 embedding（dense+sparse）
         │    ├ /{index}/_search       mock OpenSearch（本地回退检索路径）
         │    └ /robot/send            mock 钉钉告警 webhook
         ├─ ServerCtl：真 uvicorn 子进程（--workers 1 --timeout-keep-alive 65 = SAE 同参）
         │    └ /proc 采样（RSS/线程，S3 泄漏门）
         ├─ DBProbe：直连 MySQL 读 agent_run/llm_call_log/tool_invocation/qa_* 断言
         └─ scenarios S0-S9（asyncio VU 调度 + sse.py 逐帧打点/帧序校验）
              S0 基线 · S1 并发墙 · S2 投机放大(F1) · S3 浸泡 · S4 故障注入 ·
              S4F2 共享网关污染(F2) · S5 弃单(G8) · S6 混合负载(F7) · S7 限流治理 ·
              S8 崩溃恢复 · S9 冷启动竞态(F6 头条)
```

被测的是**真代码**：FastAPI 路由、ThreadedRunExecutor、DefaultAgentLoop、ModelGateway
（重试/退避/熔断）、ToolExecutor 中间件栈、投机检索、限流器、reaper、MySQL 持久化、
SSE 线协议。被替身的只有付费外呼（DashScope chat/embedding）与 HA3（走 OpenSearch
本地回退打 mock）。

## staging 真实计费档

```bash
STRESS_STAGING_ACK=I_UNDERSTAND_COSTS \
STRESS_TARGET_URL=https://<staging-host> \
STRESS_TARGET_TOKEN=<staging 签名密钥铸的 Bearer> \
make stress-staging STRESS_BUDGET_MODEL_CALLS=500
```

硬预算中止 + 拒绝回环地址；窗口/中止判据/看点见设计文档 §6（staging 与生产共用
RDS/HA3 实例，只允许错峰执行）。

## mock 修真教训（写给下一个改 mock 的人）

- SSE 响应 `Content-Type` **必须带 `charset=utf-8`**：requests 的
  `iter_lines(decode_unicode=True)` 对无 charset 的 `text/*` 按 latin-1 解码，
  中文 query 会变乱码——投机检索的 `query_matches_question` 因此永 miss
  （回归测试 `tests/test_stress_harness_smoke.py::test_mock_llm_stream_frames_and_usage`）。
- 流式 tool_calls 必须按 DashScope 语义分片（`index` + `arguments` 字符串片段 +
  `include_usage` 的 usage-only 终帧），gateway 端按 index 累积重组。
- mock 命中在权威表无行 → 本地档 `RAG_MAIN_HIT_REVALIDATE=false`（诚实范围段已声明）。
