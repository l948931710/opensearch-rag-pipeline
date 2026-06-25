# 检索一致性 / 宽问题欠召回 — 诊断结论 + 改点 plan

> 触发：同一问题「U8+成品仓库怎么操作」run-to-run 有时 12 步+3 图、有时 5 步+0 图。
> 全程生产只读诊断（`RAG_ENV=prod_ro`）。**本文件只列改点 + 验收,不含已落地代码。**
> 任何检索改动都受三条硬约束(见 §4),逐项过金集再上。

## §1 P0 诊断结论(已用全精度原始响应证实)

**探针**：`scratch/diag_rerank_raw_20260624.py`（monkeypatch `reranker._call_rerank`,不改生产代码,跑真实 `retrieve_and_enrich`,落原始全精度 `relevance_score`）。

**结论(实测)**：
1. **rerank 没有饱和** —— 原始分**全部 distinct,spread≈0.10**(0.8626→0.9677)。先前 diag 里"全 0.931"是 **3 位四舍五入显示假象 + 扩展兄弟继承分**(retriever.py L884-957)+ 该次 final-10 恰好聚集。`reranker.py` 取原始 float、无 rounding/clip/sigmoid、默认 0.0 非 0.931、无缓存 → **排除工程假饱和**。
2. **病灶在 stage-1 候选生成,不在 rerank** —— FL-ZS-WI-009《注塑发货拖柜》在出现于候选池时 rerank 给 **#3 / 0.9547**(高于多数 U8 手册段);另一次它**根本没进候选池**。即:带图明细 / 拖柜内容是**边界候选**,在 dense/sparse/BM25 over-fetch 池里**时进时出** → 答案图文 run-to-run 抖动。
3. **推论**：宽意图("U8+成品仓库怎么操作")语义最匹配手册**概览段**;明细 + 拖柜是边界匹配 → 欠召回。**rerank 模型/阈值不要动**(它健康)。

> ⇒ 原 roadmap 的 B(查 rerank 0.931)**已闭环:不是 rerank 问题**。重心转向 **候选覆盖 + 全链路确定性 + 图绑定浮现**。

## §2 改点一:全链路确定性 tie-break(P0,工程正确性,可单独上)

并列分下 order=DESC 非确定(已知 G29 陷阱)是抖动放大器。**每一个排序/合并/截断阶段都要有稳定次序键**(统一 `score DESC, 然后 chunk_id ASC`),否则修了一处,抖动会从下游的 `set()/dict.values()/并行 future 完成序`重新冒出。

需加稳定键的阶段(文件/函数):
| 阶段 | 位置 | 现状风险 |
|---|---|---|
| HA3 dense/sparse/BM25 召回 | `retriever.py` 混合检索(order=DESC) | 并列分顺序非确定(G29) |
| 多路融合 / RRF 合并 | `retriever.py` 候选合并 | `set()`去重后顺序不定 |
| rerank 返回重排 | `reranker.py::rerank_chunks` 按 index 重排 | rerank 分并列时无次序键 |
| 邻居拼接 / step 兄弟展开 | `retriever.py` L884-957 | dict 分组 + 兄弟列表顺序 |
| 图片挑选(配额轮转) | `content_blocks_builder.py` | `max_images` 轮转顺序 |
| 上下文 token 截断前排序 | `retriever.py`/`llm_generator.py` | 截断点取决于稳定排序 |

**验收**(比"三次相同"更严):同 query ×30,以下全部逐字一致 ——
`retrieved_chunk_ids` ∧ `reranked_ids` ∧ `expanded_context` ∧ `image_refs` ∧ `final_prompt_hash`。
指标:`RetrievalStability@30 = 1.0`、`ContextHashStability@30 = 1.0`、`ImageSetStability@30 = 1.0`。

## §3 改点二:候选覆盖 + 图绑定浮现(P1,结构性,逐个过金集)

rerank 既健康,真修法是**让明细/带图内容稳定进候选池 + 让图靠绑定浮现**。优先用**已有结构**,不重建索引:

1. **procedure 意图路由(先用规则,不上 LLM classifier)**：query 含「怎么操作/如何操作/流程/步骤/怎么做/系统里怎么/单据怎么」→ 进 procedure 检索路由,保障召回 `procedure_parent` + `step_card` + 带图 step。
2. **parent→child 展开,而非 step_card 平面竞争**：命中 `procedure_parent`(语义) → 展开其 `step_card`(具体步骤+图);step_card 不必独立击败概览段。**本系统已有 procedure_parent/step_card/兄弟展开**(retriever.py L810-957)→ 复用。
3. **图靠 image_refs 确定性浮现**：定位到 step → 按 `step_card.image_refs` 拉绑定图。**本系统已有确定性图↔步绑定** → 图从"碰巧进 top-k 的概率事件"变"结构化事件"。直接治本次抖动。
4. **候选生成配额(非最终配额!)**：保证 dense/BM25/sparse/procedure/step_card/带图 各路最低覆盖,合并去重后再 rerank 50-80 个。⚠️ 仅 candidate-gen 配额;**最终结果不强制 per-doc 配额**(孪生治理已证实最终配额有害)。
5. **概览近重抑制**：同 `procedure_parent` 最多 N 个候选 / heading+content hash 去重,防概览段淹没明细。

> ⚠️ 查询改写/分解(原 B2)**本系统已 A/B 过、被搁置("dark",见 multi-doc-and-guard A/B)**——重提须先回答"上次为何没赢"(候选噪声+延迟)。不盲目重加。
> ⚠️ ColBERT/late-interaction = 中长期,索引/延迟成本高,不作当前修复。

## §4 硬约束(任何检索改动都受约束)

1. 🔴 **ACL fail-closed 不可破**：多路 lane / 改写 / procedure route **必须仍走** `_build_permission_filter`(dept_internal 服务端过滤,空组→仅 public)。**绝不绕过权限过滤拼候选**。
2. 🔴 **金集回归门**：每个改动过 251 题金集 + `make release-gate`,零回归才上(对照历史轮次,注意标题别名漂移)。
3. ⚠️ **阈值是标定死的**(7.7/5.8、rerank 高0.9/中0.8):任何 rerank/融合改动会错位 高/中/低 标签,须连标签重标。本计划**不动 rerank**,故标签不受影响。

## §5 落地顺序

```
P0(今天可动、零风险)：①全精度 0.931 捕获=已完成(结论:rerank 健康) ②全链路 tie-break(§2)→ Stability@30=1.0
P1(逐个立项过金集)：procedure route + parent→child + image_refs 确定性浮现(§3.1-3.3)→ 直接治抖动+欠召回
P2：候选生成配额 + 概览近重抑制(§3.4-3.5)
P3(评估)：宽问题 progressive disclosure(先给业务导航+最常用流程,允许用户续选);late-interaction(中长期)
```

**一句话**：rerank 没病(已证实);病在"宽问题只召回概览、带图明细停在候选边界 + 边界非确定"。治法 = **procedure 路由召回 parent 展开绑定步骤与图 + 全链路确定性**;概览定位、明细回答、图靠绑定浮现。

---

## §6 更正(2026-06-24,P0-A 执行后):前提基本被推翻

`scratch/diag_tiebreak_locate_20260624.py` ×20 实测:`retrieve_and_enrich`(cosurface=True)**7 层全确定(distinct=1)**——嵌入/HA3成员/HA3顺序/rerank输入/rerank输出/最终chunk/最终图。**检索无不确定性。**

`/api/ask` 干净 ×3(**unique user_id,无会话历史**):**图数 3/3 恒=3**;步数 6-11 仅长度波动。
> 先前"有时 0 图"= **测试用同一 user_id → 共享 session → merged_history 喂 LLM → 压缩答案丢拖柜步**(api.py `/api/ask` 实锤喂 history)。**是测试设计缺陷,非系统抖动。**

**结论修正**:无 L2 检索不确定、无 rerank 饱和(早证)、无随机丢图。**§2 tie-break 降级为非紧急防御性卫生;§3 procedure 路由治的是"检索深度/概览偏好"另一独立话题,非本"一致性"症状。** 唯一真实残留 = **多轮会话历史压缩可能丢带图步骤**(by-design history 行为,值得单独瞄)。教训:instrument-before-fix 拦下了一次修不存在的问题(用户坚持 P0-A 先定位)。

## §7 唯一确认的真实残留(2026-06-24,矩阵+×4+prompt 定位)

`scratch/probe_multiturn_20260624.py` + ×4 一致性实测(直接走 generate_answer,自构 history)：
- 基线(无历史) img标记 [2,4,4,4] 丢图 **0/4**;
- **无关历史(食堂refusal)** [4,0,0,3] 丢图 **2/4**、avg 3.5→1.8;
- 相关历史(U8登录) [4,4,0,4] 丢图 1/4。

**结论**：history(尤其无关/refusal 前轮)**概率性**让完整新问题被压缩 → 少发 `<<IMG:N>>` → 少图。非确定,但真实(基线从不丢、无关历史 2/4 丢)。

**机制定位(用户三选)**：
- **#3 图仅跟 LLM `<<IMG:N>>` 引用 = 结构性放大器**(llm_generator 规则10:图靠 LLM 发标记才浮现 → 图被 LLM 啰嗦度挟持)。**根因。**
- **#1 history selection = 触发器**:refusal/无关前轮进 history 诱导更简短答案(代码 L311 已知"编号引用进历史诱导模仿")。
- **#2 简洁 prompt(规则3) = 背景偏置。**

**修复方向(不实现,先 instrument)**：
- 治本(#3)：操作步骤类答案,图按答案**已覆盖步骤**的 `step_card.image_refs` **确定性绑定浮现**,不再 gated on LLM 发 `<<IMG:N>>`(=用户点10)。需 golden-set + **ImagePrecision/IrrelevantImageRate** 护栏(防过度出图)。
- 缓解(#1)：refusal/无关前轮不进下轮 history(history selection);复用现有 `strip_doc_citations`-before-history(L310)卫生。

**优先级=中**:首问(最常见"怎么操作")不受影响;仅"先问无关再问目标"多轮场景丢图。非紧急,但是这条线唯一真实问题。

## §7.1 立项决策(用户定稿,2026-06-24)

**定性**:不是紧急生产故障,**也不只是普通 backlog**——无关前轮 2/4 丢图说明机制有结构性脆弱点,且影响**后续所有带图 SOP/操作类回答**。立为**独立 P2「多轮体验改进」**,**不再触碰 L2 / reranker / procedure route**。

**治本设计(精确版,防过度出图)**:
1. **保留 LLM `<<IMG:N>>`** 作为显式图片定位(主路径,不替换);
2. **结构化 fallback**:当最终答案**实际覆盖**某个带图 step-card、但 LLM **未生成**该图标记时,按该 step 的 `image_refs` **自动补图**;
3. 补图**按 `step_no` 排序 + asset 去重 + 受 `max_images` 限制**,并**重新校验 ACL + 文档版本 + active 状态**(不信任检索时的旧判定);
4. ⚠️ **关键防过度出图**:**只对"答案实际使用的步骤"补图**——**仅进入 context 但最终答案未覆盖的步骤,其图不展示**(答案覆盖的 step ≠ 检索到的 step)。

**上线门槛**:过 251 金集 + 新增评估 **BoundImageRecall / ImagePrecision / IrrelevantImageRate / 无关历史前后图片一致率 / 图片数量分布**。

**工程方法论结论(本线最重要的保留项)**:**instrument-before-fix 连续拦下两次错误修复**(L2不确定、rerank饱和均为测试假象)。后续类似"答案/检索不一致"问题,**先做 session 隔离 + 逐层 ×N 观测,再进代码**——别凭症状指层。

## §7.2 P2 实现状态(2026-06-24)

**核心 fallback 已实现 + 单测**(`content_blocks_builder.py`,flag `RAG_KB_IMAGE_FALLBACK` 默认 OFF):
- `_inject_fallback_markers`:答案覆盖带图 step-card(`section_title` 核心短语在答案出现)但 LLM 漏发 `<<IMG:N>>` → 在该步行末补插标记 → 下游既有 签名/近重抑制/max_images/穿插 照常;
- 防过度出图:仅 step_card · 仅 `is_active!=False` · 仅 `section_title` 核心(≥4字)在答案出现 · 不重复补;
- ACL 天然满足(只用已过 `_build_permission_filter` 的 chunks,无新增暴露);answer 本地副本不影响落库/历史。
- 验收:8 单测 + 229 content-blocks 回归全过(flag OFF 零变化),ruff 绿。设计源自 map workflow(4 agent)。

**上线门槛(未完成,§7.1 要求)**：
1. ⚠️ **金集图标注短板**：`golden_full.json` 仅 ~2 个 `expect_images` 案例(map 实测 "L4-srv gate not_executed when N<5")→ **BoundImageRecall/ImagePrecision 无法有意义计算,需先标注 ~10-20 个带图 step 操作类金集案例**(expected_images + 绑定步骤)。
2. 加 5 指标到 harness:BoundImageRecall(L3,`layers/l3_answer.py`)、ImagePrecision+IrrelevantImageRate(`mm_answer_metrics.py`)、无关历史前后图一致率、图数分布;接 `make release-gate`(`deploy/eval_release_gate.sh` / dim9)。
3. 251 金集 + judge 面板零回归 → 方可把 flag 打开上线。

## §7.3 效力验证的诚实caveat(2026-06-24)

**直接复现效力 smoke 不可信(已发现 bug)**:`generate_answer` 内部按 `max_context_chars` 截断 chunks 并对**截断后**的集合编 `[文档N]`,而我把**完整 CH** 传给 `build_content_blocks` → `<<IMG:N>>` 的 N 错位 → 图块恒 0。**绝对数无效**(API 路径两侧 chunk 对齐才正确)。教训(再次):复现harness本身要先验对齐。

**未证实的关键点**:fallback 治的是"**答案覆盖了该步、但 LLM 漏发 `<<IMG:N>>`**"(真实失效模式)。但多轮压缩可能**整步丢弃**(连步文本一起没)——那种情况答案不再提该步,fallback 按设计(§7.1 防过度)**不补**,**故不一定能治本次多轮症状**。fallback 对目标症状的实际效力**需走 API 路径(两侧 chunk 对齐)+ 金集**验证,direct replication 不行。

**净状态**:P2 代码**已实现+单测+零回归(flag OFF)**,机制对"marker-miss"成立;**对"多轮整步丢弃"是否有效=未证实**。上线前必须:① API 路径效力测(flag ON backend,多轮场景,数图块);② 金集图标注补足 + 5 指标 + release-gate。

## §7.4 API 路径效力测结果(2026-06-24)—— 结论:NOT 情况A,不进金集

**装置**(`scratch/eff_backend_launcher.py` + `eff_ab_driver.py`):两个受插桩 prod_ro 后端 OFF(:8011)/ON(:8012),走**真实 /api/ask 非流式**(=小程序实际路径,`dd.httpRequest` 仅缓冲、无流式)。历史用请求体 `history` 显式注入(与 session 同一条 `merged_history`,api.py:470-472),OFF/ON **逐字节一致**;每试唯一 session_id 不复用;关限流(LIMITER 旁路)避免靠复用省请求。插桩旁路落 JSONL:`q / hist / 完整answer / raw <<IMG:N>> / fallback_injected / 最终 oss_key / 全 chunk id+type+section+有图+active`。2 query × 4 历史 × 4 试 × {OFF,ON} = 64 轮。

**铁证 1 — fallback 32/32 ON 轮 `fallback_injected=0`(一次都没触发)**。原因已在 RDS 核实:命中检索的带图 step-card 是 `FL-ZS-WI-009《注塑发货拖柜》` 的 step_no 1–5,**`section_title=None`**(步文在 `chunk_text`:"步骤1:把《拖柜安排》…")。`_inject_fallback_markers` 锚 `_title_core(section_title)`(需≥4字)→ 空 → 跳过。**即:fallback 对 DOCX/PDF 这一**最常见 step-card 形态**是结构性 no-op(锚错字段)。**

**铁证 2 — flag 对图片无任何因果作用**。flag **只在 LLM 生成之后**于 `build_content_blocks` 后补标记,**不改 prompt/生成**。故 OFF↔ON 的 `rawIMG#` 差异**全是 LLM 采样噪声**。唯一非零 cell(U8查询×U8登录历史:OFF mean 0 → ON mean 3)其 `inj#=[0,0,0,0]` → 证明是采样(OFF 四试恰好抽到 0 个 marker、ON 抽到 7/4),**不是 fallback**。

**铁证 3 — 目标症状"多轮历史抑制绑定图"在 /api/ask 上不复现**。对**答案真正覆盖该图步**的查询(注塑发货拖柜步骤):图块在 基线/无关闲聊/拒答数据/相关U8 四种历史 × OFF+ON **共 32 轮恒为 3**(rawIMG#恒 7)。补充直测两regime确认:`cosurface=True`(stream/原 probe regime)raw_marker 恒 4、`cosurface=False`(小程序)恒 7,**三种历史均无抑制**。原 probe 的 `[4,0,0,3]` 抑制是**用 U8 查询(答案讲的是 U8 手册,另一篇文档,拖柜图只是 cosurface 外挂/边缘项)**测出的**度量假象**,不是真实绑定图回归。

**判定(对照 §7.1 情况树)**:**非情况A**(ON 未经 fallback 恢复任何图)。本质 = **情况C(step-mapping 锚错字段)+ 前提修正(症状在正确取查询下根本不复现)**。
→ **不进金集图标注 + release-gate**(§7.1 中"情况A 才投入"的门槛未达)。
→ 决策(交用户):(a) 若仍要 fallback 当保险——**改锚 `chunk_text`/`step_no` 而非 `section_title`**,再重测;但即便修好,本轮 32 试 LLM 始终引足(7→capped 3),**修好的 fallback 也补不了**(已封顶),其唯一价值场景(答案描述了步却 0 marker)在拖柜查询 32+ 试未出现一次;(b) **或直接撤回 P2**(症状是 U8查询/cosurface 假象,无真实路径受害)。flag 当前默认 OFF,两条路都零线上风险。
→ 方法论(再次印证 instrument-before-fix):**连"已实现的修复"也要在真实路径证伪**——P2 通过单测+零回归,但真实 /api/ask 上是 no-op,且所治症状不复现。

**决策已执行(2026-06-24):撤回 P2。** 删除 `content_blocks_builder.py` 的 `_inject_fallback_markers`/`_title_core`/`_img_fallback_enabled`/`_TITLE_NUM_PREFIX`/`import os` + `build_content_blocks` 里的 fallback 钩子 + `RAG_KB_IMAGE_FALLBACK` flag;删 `tests/test_kb_image_fallback.py`。回到纯 LLM-citation 出图。验证:ruff 绿,content-blocks 204 测全过,**全套 1408 passed**(= 加 P2 前基线,干净回滚)。理由:症状是 U8查询/cosurface 度量假象、无真实受害路径;且 LLM 本就引足(capped),fallback 即便改锚也补不了。装置 `scratch/eff_backend_launcher.py`+`eff_ab_driver.py` 留作以后真有用户实证"该出图却没出"时复用。
