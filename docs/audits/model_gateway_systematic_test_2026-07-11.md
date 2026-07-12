# ModelGateway 系统性测试报告（2026-07-11）

**对象**：`opensearch_pipeline/agent_runtime/model_gateway.py`（644 行）——DashScopeProvider ·
ModelGateway（complete / complete_stream）· _Breaker · 思考分档路由 · make_model_fn 适配 ·
default_gateway 工厂 · usage_raw（Tier B 新增）。

**方法**：契约面盘点 → 对照存量 30 例（`test_agent_runtime_model_gateway.py`）标盲区 →
补 24 例对抗性测试（`test_model_gateway_systematic.py`）→ staging 真 DashScope 矩阵
（`scratch/probe_gateway_matrix_20260711.py`）→ 并发真跑。

**结论先行**：⛔ 0 个新 P0/P1。单元盲区 24 例一次全绿（行为均符合契约）；真实矩阵
4 档模型解析/思考梯度/流式重组/function-calling/并发全通。6 条行为记档（F1–F6，
非缺陷，防将来误判）。测试全量 3297 绿。

## 1. 覆盖映射（契约面 × 测试）

| 契约面 | 存量 30 例 | 本批 24 例补的盲区 |
|---|---|---|
| category 路由/未知档回退 | ✅ | 空路由 → ModelUnavailable |
| 沿链 fallback（可重试/非可重试/耗尽） | ✅ | provider 未注册跳过、全缺失 fail-loud |
| 同 provider 退避重试 | ✅ | **泛异常**（非 ModelError）同样重试+沿链 |
| deadline fail-closed | ✅ 进门 | **重试间隙过期截停**（门在环内也生效） |
| 熔断 | ✅ 会打开 | **开了之后链还活着**（跳到下一项）、冷却重闭、成功清零、8 线程并发冒烟 |
| tier_params 合并/extra 优先 | ✅ | **tier_params={} 零注入**（serving 收敛语义） |
| 流式首增量边界（前可 retry/后必抛） | ✅ | 泛异常已下发后同样直抛；**缺终值 ChatResponse=非可重试**（不掩盖契约破损） |
| 流式退化同步/记账 | ✅ | 流式成功记账带终帧 usage；**预增量失败计入熔断** |
| 消费者挂断 | — | **GeneratorExit 安静收尾**：不触熔断、不记 error |
| DashScope 线协议 | ✅ 正常解析/429 | **坏帧跳过 + 无 [DONE] 收尾**；**双工具分片交织重组** + 坏 args→{}；中断也 close（连接池卫生） |
| usage_raw（Tier B 新增） | 间接（serving 等值门） | chat/chat_stream 直通单测（含 cached_tokens 明细） |
| _body 构造 | 间接 | max_tokens=0 丢弃、tools→tool_choice、extra 覆盖顶层参数（F2/F3 记档） |
| default_gateway env | — | RAG_AGENT_MODEL_*/THINK_*（坏 int→默认）、allowlist 排除 → fail-loud |
| 适配层 | ✅ | tool_call 缺 id 合成 call_N |

## 2. staging 真实矩阵（真 DashScope，2026-07-11）

```
complete × 4 tiers（同问 9.9 vs 9.11）
light  2.0s qwen3.7-plus            reason=0ch    ✅ 答对   ← 显式关思考真省
high   5.6s qwen3.7-plus            reason=314ch  ✅ 答对
xhigh  6.0s qwen3.7-max-2026-06-08  reason=348ch  ✅ 答对
max    5.9s qwen3.7-max-2026-06-08  reason=305ch  ✅ 答对
（4 档 usage_raw 齐全；思考档额外带 completion_tokens_details）

complete_stream
light 1.3s  4 增量  重组==终值全文 ✅  reason_deltas=0
max   6.5s 75 增量  重组==终值全文 ✅  reason_deltas=72  usage 终帧 28/264 + 明细

function-calling（light）2.4s
finish=tool_calls → knowledge_search{"query": "公司请假制度"} ✅

并发（4 线程共享单 gateway）total 2.2s ≈ 最慢单次（真并行），4/4 答对 0 异常 ✅
```

未真跑项（只有单元证据）：xhigh/max 的 max→plus 沿链 fallback（无法现场制造 429）；
多 provider 场景（当前 allowlist 只有 dashscope）。

## 3. 行为记档（非缺陷，防误判）

- **F1 _Breaker 无锁**：纯 dict，依赖 GIL 原子性。8 线程 × 300 混合操作冒烟无异常；
  最坏情形=丢一次失败计数（阈值略钝化），不会崩/死锁。接受，不加锁。
- **F2 extra 合并在 _body 最后**：可覆盖顶层采样参数（temperature 等）。这是特性——
  tier_params 注入与 serving 收敛的 stream:False 都靠它；已用断言记档。
- **F3 max_tokens=0 falsy 丢弃**：`if req.max_tokens:` 语义，0 视为未设。现有调用方
  均传正数；记档防误用。
- **F4 chat_stream 非 200 且 resp.text 本身抛异常**：原始异常裸抛（未包 ModelError），
  被 complete_stream 的 except Exception 兜住照常重试——边角自洽，不修。
- **F5 思考档 reasoning 在简单问题上无梯度**（314/348/305）：thinking_budget 是**上限
  不是目标**；难题上梯度已验（2026-07-09 真调 539/636/802 递增）。评估思考档效果须用
  难题。
- **F6 消费者挂断无「abandoned」记账行**：GeneratorExit 安静收尾（正确：不是 provider
  故障），代价是 llm_call_log 对被挂断调用无记录——serving/agent 各自台账（qa_session_log
  / agent_run）已覆盖会话级可见性，接受。

## 4. 交付物

- `tests/test_model_gateway_systematic.py`：24 例并入守护（全量 3297 绿）。
- `scratch/probe_gateway_matrix_20260711.py`：真实矩阵探针（可复跑）。
- 本报告。
