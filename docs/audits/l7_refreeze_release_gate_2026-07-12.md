# L7 flag-on 臂重冻 + release-gate refreeze — 2026-07-12

授权：Sam 本轮明示（"L7 flag-on 臂付费重冻 + release-gate，live 评测你机器跑"）。
全程只读（envboot=.env+.env.prod_ro；L7 零 DB）。关联：gate 签字包
`docs/ontology_gate_signoff_2026-07-12_DRAFT.md`。

## L7（agent 行为门）

### RUN 记录

| RUN | 臂 | cases | 结果 | 报告 |
|---|---|---|---|---|
| A0 | off（默认） --gate | 主 31 | 硬不变量全 1.0；**软破门 write_propose_rate 0.5 vs 基线 0.6667−δ0.1** | agent_eval_20260712T010100.json |
| A | off 复跑 | 主 31 | write_propose_rate **0.5 稳定复现**（非抖动）；其余全 1.0 | agent_eval_20260712T010504.json |
| B | on（工具全族可见+原后缀） | 主 31+onto 5 | write_propose_rate **0.1667**；onto trigger 1.0 / args_rel 0.4 | agent_eval_20260712T010919.json |
| C | on（修正后缀+链路感知判分） | 主 31+onto 5 | write **回升 0.5**（=off 臂同水位）；onto trigger 1.0 / args_rel 0.8；其余全 1.0 | agent_eval_20260712T011535.json |
| D | on --gate（对新冻基线） | 主 31+onto 5 | **✅ 过闸 exit=0**，指标与 C 逐项一致 | agent_eval_20260712T012024.json |

### 法证结论（B 轮，判据=逐例 tools/final_snippet）

1. **off 臂 0.5 vs 昨日基线 0.6667 = 模型日漂移，代码无罪**：off 臂 registry/prompt 与
   基线冻结时逐字节同源（守护单测锁死）；两次独立复现同值。6 例族单例翻转=±0.167>δ0.1
   ——该软指标在 n=6 下天然脆，任何单例波动即破门（gate 设计观察，随新基线一并记录）。
2. **on 臂 write 掉到 1/6 = 提示词后缀真回归（本轮抓到的头号收获）**：原后缀"先用
   ontology_resolve 解析身份"被模型执行为**写提案的前置闸**——空本体店（未播种=生产
   现状）解析必失败 → w01/02/03/05/06 全部"编号未解析，无法建单"拒提案。这违背
   write_approval 族语义（写意图必须提案、由审批环节把关身份）。
   **修复**：后缀显式解耦——"编号解析不到**不阻塞**业务单据操作…把『编号未解析，待
   审批人核对』写进说明即可"。
3. **onto args_rel 0.4 = 判分过严非行为错误**：o03/o04 是 resolve(编号)→packing_calc
   (object_id) 的**理想接力链**，判分只查末端工具参数 → 误判负。修复=链路感知（期望
   实体出现在任一调用参数即算落地）+ score 带 args_sample（判负可诊断）。o02 待 C 轮
   args_sample 定性。

### 重冻裁决（已执行）

- **freeze**：baseline.json ← RUN C（agent_eval_20260712T011535.json，frozen 2026-07-12）。
  新基线：tool_trigger/query_rel/no_tool/grounded/approval_suspend 全 1.0；
  write_propose_rate **0.5**（=当日 off 臂双复现水位，非本分支引入——off 臂结构与昨日
  基线冻结时同源）；**新增 ontology_tool_trigger_rate 1.0 / ontology_args_relevance 0.8
  （o02=模型把 KH-A88 错拆 namespace 的轻档行为边界，如实入册；软指标未进闸）**。
- **RUN D 出闸绿（exit=0）**——on 臂对新基线全指标达标。
- ⚠️ **基线口径=on 臂（工具全族可见 + 修正后缀 + 合并金集 36 例）**：此后跑
  `make agent-eval-gate` 须 `RAG_ONTOLOGY_TOOLS_ENABLE=true` + `--cases` 指向合并金集
  （或翻 flag 后的生产默认态），off 臂裸跑会因口径不一致产生假信号。
- o02 类 namespace 错拆的改善路径（P2，不阻塞）：ontology_resolve 工具描述里枚举
  合法 namespace 形态 / 金集加 few-shot。

## release-gate（RAG 服务门）

前置事实：
- 冻结基线 `eval_harness/goldset/baseline.json` regime.eval_set_sha=3bed9881eefaf0ee，
  与现 golden_50（14d996432c09b1a5）不匹配——QA-02（宿舍孪生错标→1B0B2E）与 QA-95
  （067E33 误标删除）修正后未 refreeze；且 regime 记录 llm_model=qwen3.6-plus（已退役）
  ——**refreeze 是硬前置**（脚本 preflight 会直接 FATAL）。
- 既定路径（7-06 法证）：无基线全链跑（run+judge）→ 人工裁决 L1 diff（对照已知 3 题
  环境性翻转）→ freeze → 重跑 gate 出绿。
- 与 L7 错峰串行（共享 DashScope 配额，避免 429 污染基线）。

【待填：run#1 结果与 diff 裁决 · freeze · run#2 gate 出口】

## 计费口径

L7 每轮 31–36 例 × light 档若干模型调用；release-gate 每轮 golden_50（76 题）全链
（检索+生成+3 面板 Claude judge）×2 轮。均为 Sam 本轮明示授权的付费评测。
