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

### run#1（无基线全链，golden_50=76 题，exit=1）法证

**（勘误）**：本报告初版曾写"79 项聚合零超δ回退"——那次比对读的是 run 报告（merge
前）不存在的扁平 metrics 键，比了空字典，结论空洞作废。真实对照见 run#2 节。

**strict 失败清单分桶**（11 项）：
1. **跑法问题（本轮已定位）**：
   - `fusion/calibration regime` FAIL——**run#1 rerank 没开**（envboot 不管
     RAG_RERANK_ENABLE；6-19 基线制度=weighted+rerank）→ run#1 的 L1/L2 数字
     **不代表生产臂，不可用于 refreeze**。→ run#2 已带 RAG_RERANK_ENABLE=true 重跑。
   - judge 面板全批 `claude rc=1`——**本机 claude CLI 掉登录**（"Not logged in"）。
     judge 是 strict 的必要件（answer-correctness must be judged）。**只有 Sam 能
     `/login`**，恢复前任何 run 都到不了绿。
2. **语料/基建（治愈=生产写，Sam-gated）**：`[L6-hard] RDS↔HA3 missing=85` +
   `l6:NO_GO_DEFECT`——HA3 静默蒸发复发（7-06 治愈 64 个同款；本次仍集中 6-21/22
   批、多为单 chunk 文档）。**治愈包已备**：`rebuild_from_rds.py --probe` 只读清单
   =scratchpad/heal85_dryrun.log；commit 需当日 `PROD-RW:<date>` + stage-3 重推
   （家庭网须 RAG_HA3_PUSH_BATCH_SIZE=8）。根因侧「提阿里工单 + 周期 reconcile 自动
   闭环」仍是 7-06 遗留待办。
3. **既有绝对阈值缺口（先于本分支，flag-off/未部署/待拍板）**：xlsx 检索三连
   （recall@5 0.5455 / 来源标注 0.6 / clean-gold 0.72）、over-refusal 0.2456、
   keyword-coverage 0.396、marker validity 0.6863（修复已合 main 但 RAG_IMG_SUBINDEX
   等 flag 未部署——与 7-06 记录一致「不是回退」）、docx binding not_executed
   （GT 在、16 docs 跑了但 strong_chunks=0——L4 条件问题待查）。
   **这些不闭，strict 永远 exit 1**——refreeze 解决不了绝对门。

### run#2（rerank on，只 run 不 judge）→ 引出现网 P0

regime guard 转绿（weighted+rerank ✓）。但 L1 **recall@5=0.5636 / found_rate=0.6
vs 基线 0.9273**——40% 正例的 gold 文档 top-10 完全缺席（能命中的都在前 3）=
"不在可检索索引"签名，非排序问题。独立重跑逐字节复现（确定性，非抖动）。

**逐题法证（vs 6-19 基线 run per_query）**：22 题 found→miss（S5 部门内探针全族 7 题
+ json_text 12 题 + QA-02/06/SRC-02），2 题 miss→found。miss 的 gold 文档在 RDS
全部 active+public+chunk 在册——但 `updated_at` 高度集中 **2026-07-06 17:00–17:37 与
07-07 10:37**（= 7-06「64 drops 治愈重推」与 7-07 wave-v2 的操作窗口）。

**自查询实锤（现网 P0）**：miss 文档取自身 chunk 原文作查询，4/4 不返回自身；对整个
7-06/07 窗口人群（**485 docs / 10,366 active chunks，约占语料 37%**）随机抽 15 docs
自查询 **15/15 全盲**。reconcile 点查 missing 仅 85（且与 gold 零交集）——**doc store
在、可检索索引无**，与 docs/ha3-doc-evaporation-incident-2026-07-06.md 的引擎段合并
吃索引同签名，这次吃掉的正是 7-06 治愈重推批本身 + 7-07 批。

**结论修正链**：
- 7-06「点查是唯一可靠存在性判定」需修正——**点查只证 doc store 存在；可检索性必须
  抽样自查询**。周期 reconcile 需加自查询抽样通道，否则这种模式永远漏检。
- 现网用户自 7-07 起对这 485 篇文档检索失明（约 5 天）——L1 坍塌是它的测量像，
  refreeze 在治愈前无意义（会把盲态冻进基线）。

### release-gate 当前出口（blocked，三件 Sam-gated）

1. **治愈 485 盲文档**（生产写）：`scripts/rebuild_from_rds.py --docs <485 列表>`
   --commit（当日 PROD-RW token）→ laptop stage-3 重推（RAG_HA3_PUSH_BATCH_SIZE=8）
   → **重推后自查询抽样复检**（点查对账不再是终验）。doc 清单已备
   （scratchpad/blind485_docids.txt）。⚠️ 引擎根因未除（阿里工单仍未提，7-06 遗留）
   ——重推可能再被吃，工单+周期自查询监控是根治面。
2. **claude CLI /login**（judge 面板恢复）。
3. 治愈+judge 后：重跑 run → 裁决 → freeze → gate；既有绝对阈值缺口
   （xlsx 三连/over-refusal/keyword-coverage/marker validity/docx strong-chunks=0）
   另案拍板（部署 flag 修复 or 调门）。

## 计费口径

L7 每轮 31–36 例 × light 档若干模型调用；release-gate 每轮 golden_50（76 题）全链
（检索+生成+3 面板 Claude judge）×2 轮。均为 Sam 本轮明示授权的付费评测。
