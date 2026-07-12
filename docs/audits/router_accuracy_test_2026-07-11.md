# Router 准确性系统测试报告（2026-07-11）

**问题**：思考分档路由（light/high/xhigh/max）——接线是否准确？档位升级是否真买到准确率？

**结论先行**：接线 100% 准确（守护测试钉死全矩阵）；**在封闭式问题上档位升级买到的准确率
= 0**——29 题跨三个难度带，四档全部 29/29，而 light 只用了思考档 ~1% 的 completion token
和 ~1/5 的延迟。**default-light + 用户自选深思的现行策略被强验证**；思考档的价值论证必须
建立在开放式企业工作负载（多文档综合/含糊问题）上，封闭式准确率不是它的战场。

## 第 1 层：接线矩阵（确定性，进守护）

`tests/test_router_wiring_matrix.py`（4 例，全绿）把 7 个生产调用点的路由钉成一张表：

| 调用点 | category | 解析结果 |
|---|---|---|
| routes/agent.py submit/resume | `model_profile or "light"` | light/high（thinking→high 由 test_routes_agent 守护） |
| compaction.summarize | `"light"`（签名默认，测试锚定） | qwen3.7-plus 不思考 |
| query_decomposer | 自建 `"decompose"` 单链 | config 模型，等值门钉线上行为 |
| llm_generator serving ×2 | 自建 `"serving"` 单链 | config 模型、max_retries=0、tier_params={}（测试锚定） |
| default_gateway 4 档 | light/high/xhigh/max | 链序+thinking 参数全量断言（xhigh/max 带 →plus fallback 项） |

任何路由漂移（换模型/动链序/改 thinking 预算）一处变红。

## 第 2 层：分档策略有效性（staging 真 DashScope，116 调用）

两轮递进难度，判分=answer-only 指令 + 变体子串 + 排除串（碰撞校验过）：
- **基础集 18 题**（6 易 + 12 难：鸡兔/工程/浓度/利润/日历/唯一真话/数列/相遇/阶梯计价/陷阱）
- **地狱集 11 题**（专打非思考失误面：847×396/概率分数/7^2026 个位/容斥/跨年日历/蜗牛爬井/
  斐波那契第 15 项/三盒唯一真话/单位换算链/中文数数/优先级陷阱）

| tier | 准确率 | 中位延迟 | completion tok 合计(29 题) |
|---|---|---|---|
| light | **29/29** | **1.1-1.3s** | **64** |
| high | 29/29 | 6.1-7.5s | 6,608 |
| xhigh | 29/29 | 4.1-6.5s | 5,848 |
| max | 29/29 | 3.6-5.1s | 5,204 |

（总费：116 调用，prompt 4.2k + completion 17.7k tok，一次性。）

## 判定

1. **接线准确性 = 100%**，且已进守护（漂移即红）。
2. **qwen3.7-plus 不思考在封闭式问题上已饱和**：直到"地狱集"难度（经典非思考失误面）
   light 仍全对——档位升级在此类问题上是纯成本（+4-6s 延迟、~100× completion token），
   零准确率收益。**default-light 策略正确**；深思计入配额（10/日）也正确——它买的是
   token 不是对错。
3. **思考档的辩护责任转移**：其价值假设只剩开放式工作负载（多文档综合、含糊企业问题、
   agent 多步规划）——已有的间接证据是 reasoning 长度随 budget 递增（539/636/802，
   2026-07-09 难 prompt）与深思 run 的行为差异；**封闭式对错不是它的评估维度**（本报告
   即为证据）。若要正面论证，需在 RAG 真实工作负载上做答案质量盲评（不同于本探针）。

## 如实声明的边界

- 只测了封闭式可判定问题；开放式综合质量未测（是另一种评测，见判定 3）。
- 判分为子串匹配（answer-only 指令把碰撞风险压到最低，个别单字答案仍有理论 FP 面）。
- 未追问"交叉点在哪"（竞赛数学难度）——那是模型能力问题，不是本系统路由策略问题：
  企业 KB 工作负载不会出现比地狱集更难的封闭式问题。
- 未测 xhigh/max 真 429 时退 plus 的现场表现（单元已覆盖，现场造不出）。

## 交付物

- `tests/test_router_wiring_matrix.py`（4 守护例，并入全量）
- `scratch/probe_router_tier_accuracy_20260711.py` / `probe_router_tier_hell_20260711.py`（可复跑）
- 原始数据：scratchpad `router_tier_accuracy_20260711.json` / `router_tier_hell_20260711.json`
