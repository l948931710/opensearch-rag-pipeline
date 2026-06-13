# qwen3.7-plus vs qwen3.6-plus — L3 答案质量对照（2026-06-12）

**结论：质量上是统计平局、数值微正、零维度恶化。是否换用取决于成本/价格,不取决于质量。**

## 形态（同题集,公平对照）

| | 基线 | 新臂 |
|---|---|---|
| run | `run_predeploy_q36` | `run_q37_thinkoff` |
| 模型 | qwen3.6-plus | **qwen3.7-plus** |
| 题集 | golden_50（38 正 + 12 负） | **同一 golden_50** |
| thinking | OFF（0 leak） | OFF（**0 leak ✓**——3.7 尊重 enable_thinking=False） |
| 评委 | Claude 3 评委盲评 | 同款 Claude 3 评委盲评 |
| 环境 | prod_ro 只读 / 同 HA3 / 同 rerank | 同上 |

唯一变量 = LLM 模型。满足 same-question-set + same-environment 两条铁律。

## Claude 面板（正例 n=38，3 评委均值 + bootstrap CI）

| 维度 | 3.6 基线 | 3.7 | Δ | CI |
|---|---|---|---|---|
| faithfulness | 4.956 | 4.965 | +0.009 | 重叠 |
| correctness | 4.368 | **4.491** | +0.123 | 重叠 |
| completeness | 4.290 | **4.333** | +0.043 | 重叠 |
| relevance | 4.781 | 4.781 | 0.000 | 重叠 |
| overall | 4.298 | **4.491** | +0.193 | 重叠 |
| overall≥4 通过率 | 0.711 | **0.816** | +10.5pp | — |

**全维度 CI 重叠 → 统计上不可区分。** 3.7 五维全不低于 3.6,overall/correctness/通过率数值上更好,但落在噪声带内。

## 其它信号

- **fabrication 0.0 → 0.026**：仅 SRC-13 **单评委(1/3 少数票)** 的 `<<IMG:N>>` 图标记质疑,数字事实全部 grounded(消防每年≥2次/IT预演≥1次)——**非事实幻觉**,faithfulness 仍 4.965。
- **负例拦截率(规则) 0.4167 → 0.4167**：完全一致（检索/拒答行为同源）。负例 overall 4.389→4.389,fabrication 双零。
- **延迟 4830ms → 5067ms**：慢约 5%,可忽略;答案略长(345.7→356.6 字)。
- **评委间 stdev 0.085 → 0.139**：分歧略增,仍极紧(远 < 0.5)。

## 唯一的「FAIL」是确定性代理的假阳

`keyword-coverage 0.6972 < 0.70` 闸 FAIL（基线 0.7232 PASS）。但：
- Claude **completeness 维度反而升了**（4.290→4.333）——直接矛盾;
- `mean_chars` **升了**（345.7→356.6）——不是截断,是**换措辞**导致命中的精确 gold 关键词变少;
- 关键词覆盖是 exact-match,对措辞敏感。**这是粗指标的假阳,不是真漏要点。**

若采用 3.7,该闸阈值(在 3.6 上校准的)需重校准,否则会一直误报。

## 几乎所有弱题都是检索缺口,与模型无关

overall<4 的正例（QA-06/J-r120_56/_96/_72/_55、QA-106、SRC-13）评委理由几乎都是
「gold 内容不在检索 context 里 = retrieval miss」——这些对 3.6 一样会扣分,**非 3.7 回归**。

## 决策建议

- **质量层面：3.7 过线,可换。** 它在每个维度都 ≥ 3.6,correctness/completeness/通过率略优,零恶化,thinking-off 干净。
- **但质量增益在噪声带内,不构成「非换不可」的质量理由。** 决策真正的支点是 **DashScope 每 token 价格**：
  - 3.7-plus 与 3.6-plus **价格相当** → 换（取新模型、微优、零风险）；
  - 3.7-plus **明显更贵** → 不值（质量增益落在 CI 内,成本敏感场景不划算）。
- 换用是**部署决定**（改 SAE 的 `RAG_LLM_MODEL` + 重校 kw 闸），未自动执行。

## 仍待评（worklist 第 4 项残尾）

**thinking 臂未评**（本轮只跑了 thinking-off 首测）。深思质量收益 vs ~8x token 成本的性价比仍是开放问题——
若要评,同款 golden_50 + thinking-on 臂 + 同评委面板,再与本表对照。

## 工件

- 新 run：`eval_harness/reports/run_q37_thinkoff/`（report.md/json、judge_bundle、judge_verdicts、shards/）
- 基线：`eval_harness/reports/run_predeploy_q36/`
- 冒烟脚本：`scratch/probe_qwen37.py`
