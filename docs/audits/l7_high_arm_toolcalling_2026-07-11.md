# L7 high 臂对照：深思对工具调用行为的影响（2026-07-11）

**问题**：深思→high 档（本周接入 console）对工具调用准确率有无影响？L7 基线只冻在 light 臂。

**方法**：runner 加 `--tier`（默认 light 零行为变化；`--gate` 仅限 light 臂防错比基线），
31 例双臂各一跑 + 异常例复跑 ×3 + complete_stream 终值 spy 法证 ×4。

## 结果

| 指标 | light | high | 判定 |
|---|---|---|---|
| tool_trigger_rate (10) | 1.0 | 1.0 | 深思不压制工具触发 ✅ |
| tool_query_relevance (10) | 1.0 | 1.0 | 参数质量不变 ✅ |
| no_tool_rate (6) | 1.0 | 1.0 | 也不引入过度调用 ✅ |
| approval_suspend_rate | 1.0 | 1.0 | 硬不变量在 high 臂成立 ✅ |
| write_propose_rate (6) | 0.5 | **0.8333** | 深思**改善**写意图提案（5/6 vs 3/6）——超出已知 0.5↔0.667 震荡带，单跑信号非证明 |
| grounded_rate (9) | 1.0 | 0.8889 | **非落地质量回归**，见下 |

## grounded 走低的真因：high 档退化空响应（真发现）

`g-low-1` 在 high 臂 `final_snippet=""`。复跑 ×3：过/过/**再挂**（失败恒伴随短延迟
4.5-4.8s，通过则 9-11s）→ 非一次性瞬时。spy 法证抓到现行（run2 首轮）：

```
finish='stop'  text=0ch  reason=0ch  completion_tokens=2   ← DashScope 退化响应
finish='tool_calls' …                                      ← loop 兜底重试（1bfade7）救回
finish='stop'  text=136ch …                                → grounded=True
```

**签名与 2026-07-11 早间瞬时空答完全一致（completion=2）**，只出现在思考臂；正文一旦产出，
落地与引用全对。综合采样发生率 ~20-30%/run（8 跑 2 挂 + 1 次被重试救回）。现有兜底
（`empty_final_retries=1`）救单发、救不了连发或重试额度已耗后的晚发。

**加固选项（未实施，待拍板）**：
1. high 臂 `empty_final_retries` 提到 2（改动最小，概率性覆盖连发）；
2. 重试轮强制 `enable_thinking:False`（退化只见于思考臂，无思考重试近乎确定出正文；
   需 make_model_fn 重试变体的管道改动）；
3. 用 llm_call_log 监控发生率（`completion_tokens<=2 AND status='ok'`），并作为
   DashScope 侧问题跟进证据。

## 判定

1. **工具调用四个准确率面在 high 臂零回归**（触发/参数/克制/审批挂起全 1.0）——
   深思开关对工具调用行为安全。
2. write_propose 的改善信号值得在接真写工具、重造提示词时带上（届时 n 也该扩）。
3. 唯一负项是 provider 层退化空响应，与工具调用无关；已定位签名与发生率，
   兜底半覆盖，加固三选项待拍板。

## 交付物

- runner `--tier` 参数（永久对照能力，`--gate` 守 light 臂）
- 报告 JSON：eval_harness/reports/agent_eval_20260711T191705.json（light）/
  agent_eval_20260711T192109.json（high）
- spy 脚本：scratchpad spy_glow1.py（会话产物）
