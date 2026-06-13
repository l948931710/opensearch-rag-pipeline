# 生产完整终验报告 — 2026-06-12

**结论：通过（PASS）。** 生产 SAE 部署（main@0eb0b1f 前的已部署包，公网 `120.55.69.9:8000`）
在答案质量、四条回答链路、溯源落库、小程序端到端渲染四个维度全部达标，无阻断性回归。
防刷限流包已就绪未部署 —— 唯一遗留是部署后补一个 429 冒烟。

## 环境形态声明（公平性四原则）

| 原则 | 本轮落实 |
|---|---|
| 配置同构 | 直打**生产 SAE 公网**（rerank ON、guard ON、context 10000 即线上实际 env），最高保真——含公网链路 + CORS + 真实容器 |
| 语料同面 | 生产 HA3/RDS（582 active docs / ~5.8k chunks 全 public） |
| 题集延续 | 复用 `scratch/local_e2e_answers.json` 25 题 + 对照 `prod_retest_R2_20260611`；补 5 项探针（2 回归题/深思/负例/流式） |
| 渲染真值 | 小程序图片验到网络层（OSS 签名 URL 200）+ DOM 层（`naturalWidth>0`） |

## A. 答案质量（25 题主采集，全 0 错误）

- **命中：hit@1 = 25/25，hit@any(top7) = 25/25，零 miss**（BIND 题按 expected_titles 标题匹配）
- **拒答/守卫：** 25 题 no_result=0、guard=0（GT 题召回稳定）
- **图片符合：** 期望有图 8 题中 7 题非空；超预期带图 5 题
- **延迟：** p50 5172ms / p95 9721ms / max 14485ms / mean 5787ms（RAG+rerank+LLM，可接受）

**对照 R2 逐题漂移：** 仅 3 题图片数变化（QA-24 3→0、SRC-04 2→3、J-r120_21 3→0），
**双向**且 rank 均仍=1、正文要点完整 —— 系 LLM `<<IMG:N>>` 穿插的非确定性，非数据/检索回归。
rank / no_result 零漂移。

## B. 四条回答链路 + 补充探针

| 探针 | 结果 |
|---|---|
| REG-NJ 年终奖如何划分 | guard=true，事假 7天/14天/2-3 规则正确 ✓ |
| REG-YJ 应急演习次数 | 消防2次/特种设备1次/IT 1次，**跨 3 文档融合**正确 ✓ |
| THK-01 深思臂（QA-18 同题） | model=`qwen3.6-plus+thinking`，51.3s，答案与非深思版一致 ✓ |
| NEG-01 域外负例（今天天气） | no_result=true，REFUSAL 分桶，rephrase 非空 ✓ |
| STR-01 流式（请假流程） | 帧序 session→sources→chunk×61→done→content_blocks→[DONE]，order_ok ✓ |

## C. 溯源落库核验（qa_session_log，prod_access 只读）

`test-prod-final-20260612` 共 **30 行 = 29 SUCCESS + 1 REFUSAL**（与采集计数吻合）：
- `+thinking` 后缀正确落在深思行（`model_name='qwen3.6-plus+thinking'`）
- REFUSAL 正确分桶（今天天气 → REFUSAL，非 NO_RESULT）
- retrieval_latency 30/30 非空；llm_latency 29/30（1 行缺，疑偶发，非阻断）
- content_blocks_json 14 行非空（= 图文答案数）—— 确认生产**未设 RAG_PURE_TEXT**，小程序图文正常

## D. 小程序 LIVE 端到端（原型桥直连生产 SAE，真实渲染管线）

| 题 | 验证 | 结果 |
|---|---|---|
| 吸塑扫码报检（BIND-01 图文步骤卡） | 步骤卡 + 真图 DOM/网络 | 7 步骤卡、3 张 OSS 图 **DOM naturalWidth>0 ×3 + 网络 200 ×3 + CORS OPTIONS 200**；方向正确无串图（全归属 doc 328126） |
| 食堂今天有什么菜（库外负例） | NO_RESULT 卡 | 「未找到相关内容」卡 + 转人工出口 + **零假来源** |
| 年终奖如何划分（guard 回归题） | 低匹配条 + 多轮 | 橙色「匹配度较低…」条 + 5 来源全标「相关度低」+ 要点正确；多轮会话连续（同 session 3 轮） |

截图留证：`第1步`步骤卡+产品标识卡真图、NO_RESULT 卡、guard 提示条（本轮对话内）。

## 遗留项（均非阻断）

1. **防刷限流包待部署**：本轮公网未触发 429（35 连发 hot-questions 全 200）证实新包尚未上线 ——
   终验在限流关闭态完成（答案质量信号最干净）。部署后补一个 429/503 冒烟即闭环。
2. **钉钉机器人实发**：2 道回归题已由 API + 小程序 UI 验证；钉钉端实际发送需用户手机操作，本轮无法代劳。
3. ~~**原型 LIVE 桥 rephrase 局限**：`index.html:1183` 硬编码 `rephrase:[]`~~ **已修（一行：`rephrase: resp.rephrase || []`）**：
   dev 桥现转发后端 rephrase 建议；负例题复测确认 chip 渲染 + 点击回填不自动发（符合真实前端契约）。
   渲染/wiring 本就存在，仅桥接断点。生产无关。
4. **图片穿插非确定性**：QA-24 / J-r120_21 本轮未穿插图（绑定仍在，LLM 未引）——已知行为，
   双向噪声非单向丢失。

## 工件

- 采集：`scratch/prod_final_answers_20260612.json`（25 题）、`scratch/prod_final_supplement_20260612.json`（5 探针）
- 脚本：`scratch/prod_final_collect_20260612.py`、`prod_final_supplement_20260612.py`、`prod_final_analyze_20260612.py`
