# 全本地受控 A/B：新管线 vs 生产旧 chunks（同环境同配置盲评）

生成: 2026-06-10 晚 · 设计与执行回应"上轮跨环境对比不公平"的问题（见 `local_e2e_20260610.md` 三个 artifact）
· 25 题（19 金集单文档题 + 6 手工绑定题）· 3 独立盲评（A/B 随机化 seed=20260610、qid 级密封映射）

## 结论（先看这个）

**在消除全部环境不对称后，新管线多数票 10 : 6 反超旧管线（tie 8）——上轮 4 : 16 的总分劣势被证实主要是环境 artifact，
而非管线回退。** 绑定修复在 5 道 BIND 题上 4 : 1 占优，且评审在不知道哪侧是新管线的情况下，
原话引用了图-步骤精确对应的细节（红箭头连字段、红框标注③）。文本维度旧管线仍略高（粗粒度 chunk 在 U8
手册深层小节上覆盖更稳），这是细粒度步骤卡对 rerank 命中小节的依赖，指向两个具体残余问题（见下）。

## 实验设计（与上轮的本质区别）

| | 实验组（new） | 对照组（old） |
|---|---|---|
| chunks | 本地新管线回灌产物（2,074 条，1,348 step_card） | **生产 RDS active 旧版本镜像**（1,758 条，0 step_card，411 image） |
| 索引 | `locale2e_v1` | `locale2e_old_v1` |
| 服务 | :8001 | :8002 |
| 其余 | **完全相同**：同 serving 代码 · 同本地 OpenSearch 两路检索(dense0.7+BM25 0.3) · 同 mapping（含 chunk_id/chunk_index/source_image/visual_summary）· 两侧 `RAG_RERANK_ENABLE=true`（pool=20→top7，标签 0.9/0.8 量纲）· 同 LLM(qwen3.6-plus) · 同日采集 | 同左 |

对称性的直接证据：双侧延迟中位数 **7089 vs 7087 ms**。
对照组用生产 RDS 当前 active chunks（含真实 VLM caption / extra_json）只读镜像构建（`scratch/local_e2e_old_mirror.py`），
不靠本地重跑旧代码复刻——对照的是"线上正在服务的真实产物"。

上轮三个 artifact 的消除方式：
1. **rerank 不对称** → 两侧同开（qwen3-rerank / qwen3-vl-rerank 路由，重排日志确认生效）。
2. **VLM 缓存中毒** → 缓存已清理（本轮复查 1,900 条 0 污染），且 simulate 结果不再入持久缓存。
3. **本地索引缺字段** → `to_opensearch_doc` 补发 `chunk_id`/`chunk_index`，索引 mapping 显式声明
   `source_image`/`visual_summary`，两索引全量重建（实验组 112 条、对照组 411 条顶层带图 chunk 均可达）。
4. （附带）**sparse 通道**：两侧对称缺失（标准 OpenSearch 无 sparse），不再构成偏置。

## 盲评聚合（24 题，BIND-06 双侧 retrieval-miss 剔除）

| 维度 | old_pipeline | new_pipeline |
|---|---|---|
| correctness | **4.90** | 4.75 |
| completeness | **4.53** | 4.36 |
| relevance | **4.88** | 4.65 |
| image_quality | 3.26 | **3.53** |
| overall | 4.32 | **4.40** |

**题级多数票：new 10 / old 6 / tie 8**（3 评审独立 preference 分布：9/6/9、9/8/7、9/7/8，无单评审主导）。

解读：旧管线文本维度略高的主因是 6 道 U8 深层小节题（J-r120_21/22/23 等）——旧的粗粒度 chunk 单条即含完整小节，
新管线把 211-chunk 手册切成细步骤卡后，个别题 rerank 后仍未把正确小节排进 top-7。但题级胜负上，
新管线靠图文绑定与流程结构在更多题上"更可用"（overall 与多数票均占优）。

### 绑定修复的盲评原话（评审不知道哪侧是新管线）

- **BIND-01 吸塑扫码报检（3/3 一致选新）**：旧链路在对称环境下 0 图；新链路"产品标识卡图与第1步清点实货、
  手写记录图与抄录步骤对应良好"。
- **BIND-04 注塑收货报检（3/3 一致选新）**："三张图分别精确对应核对标识卡与记录单（红箭头连字段）、
  错误时竖起一箱（红框标注③）、抄录时间/机台/数量/货号"——绑定几何修复的直接证据。
- **J-itin_socket（新胜）**：旧链路所配"CPU三角缺口对位图"与"打开LGA775插座"无关，被评审判为**错绑**；
  新链路无图反而不失分。上轮该题旧链路因"有图"得分，本轮在金标准对照下错绑被抓出。
- **BIND-05（旧胜，真实输点）**：新链路自述只展示 12 步中前 6 步，缺装柜/监装后段——步骤卡截断问题，
  且混入一张与步骤无关的微信聊天截图（噪音图）。非缓存中毒（上轮该题输在 [Simulated] 占位图，已消除）。

### 上轮 7 个本地拒答题的走向（rerank 对称后）

| qid | 上轮（本地无 rerank） | 本轮 |
|---|---|---|
| J-r120_21 | 拒答 | 作答但漏具体步骤，**old 胜**（另见"标记泄漏"残余问题） |
| J-r120_23 | 拒答 | **新侧仍拒答**（检索 rank=1 命中文档，LLM 判上下文不足）→ old 胜 |
| J-r120_30 | 拒答 | 作答完整，**new 胜** |
| J-r120_31 | 拒答 | 作答，tie |
| J-r120_36 | 拒答 | 作答，**new 胜** |
| J-r120_37 | 拒答 | 作答，tie |
| J-itin_socket | 拒答 | 作答 + 抓出旧链路错绑，**new 胜** |

7 题中 6 题恢复作答（净胜 3 / 平 2 / 负 2），确证上轮拒答群主要是 rerank 缺位的 artifact。

## 逐题明细

| qid | old rank/img | new rank/img | 多数票 | 评审要点 |
|---|---|---|---|---|
| QA-18 | 1/1 | 1/2 | tie | 文本几乎一致，新侧多一张支撑图，未拉开差距 |
| QA-23 | 1/0 | 1/0 | new | 新侧把水电费计算规则归入独立清单步骤，照做更不易混淆 |
| QA-24 | 1/1 | 1/3 | old | 新侧花整步讲相机安装参数偏离"客户端主要功能"主线，删除车辆一笔带过 |
| QA-43 | 1/0 | 1/0 | tie | 十不吊内容一致；新侧把禁令清单标成"第N步"格式略不当（上轮同样发现） |
| SRC-04 | 1/1 | 1/2 | tie | 双方均未点名金标准来源文档 |
| BIND-01 | 1/0 | 1/3 | **new (3/3)** | 新侧三图逐步对应；旧侧对称环境下无图 |
| BIND-02 | 1/3 | 1/3 | new | 双方均 3 图，新侧步骤-图对应更准 |
| BIND-03 | 1/3 | 1/3 | new | 新侧给出关键导出导航路径 |
| BIND-04 | 1/0 | 1/3 | **new (3/3)** | 新侧全要点 + 三图精确绑定；旧侧漏"交货单分类放置"且无图 |
| BIND-05 | 1/3 | 1/3 | old | 新侧只覆盖前 6/12 步缺装柜监装段 + 一张无关聊天截图 |
| J-r120_21 | 1/1 | 1/0 | old | 新侧缺全部操作要点并泄漏 `[文档5]` 内部引用标记 |
| J-r120_22 | 1/0 | 1/0 | old | 新侧漏"手工修改现部门编码"金标准步骤（与上轮同模式） |
| J-r120_23 | 1/0 | 1/0 | old | **新侧仍拒答**（唯一残余拒答） |
| J-r120_30 | 1/0 | 1/0 | new | 新侧覆盖审批表全链路 |
| J-r120_31 | 2/0 | 2/0 | tie | 双侧均 rank=2，作答质量接近 |
| J-r120_32 | 1/0 | 1/0 | new | 商检号要点覆盖更全 |
| J-r120_35 | 1/0 | 1/0 | tie | 实质相同 |
| J-r120_36 | 1/0 | 1/0 | new | 转入供应商填制界面路径直击问题 |
| J-r120_37 | 1/0 | 1/0 | tie | 双方均命中默认业务类型与取价要点 |
| J-itin_step1 | 1/0 | 1/0 | tie | 均正确 |
| J-itin_steps | 1/0 | 1/0 | tie | 七步骤一致 |
| J-water_seal | 1/2 | 1/3 | old | 新侧多一张与密封步骤无关的揭膜图（与上轮同发现） |
| J-water_soak | 1/1 | 1/2 | new | 新侧毛巾擦拭实拍图支撑擦干步骤 |
| J-itin_socket | 1/1 | 1/0 | new | 旧侧配图错绑（CPU 对位图 ≠ 开插座） |
| BIND-06 | None | None | 剔除 | 双侧 retrieval-miss（与上轮一致，该文档疑似不在批内/未被索引覆盖） |

## 残余问题（按优先级，转入 worklist）

1. **J-r120_23 仍拒答**：rank=1 命中文档但步骤卡粒度下正确小节未进上下文（员工档案修改）。复查该题的
   rerank 池与 expand_step_context 行为；属"细粒度 + 多小节手册"模式的尾部 case。
2. **`[文档N]` 内部引用标记泄漏到答案正文**（J-r120_21 新侧，2/3 评审点名）：生成层 prompt 或
   marker 清洗对该 pattern 的覆盖缺口；serving 端 marker 泄漏修复轮（commit 6aed0ca 系列）未覆盖此变体。
3. **BIND-05 步骤截断**：12 步只渲染前 6 步（procedure 上下文长度上限）+ 无关聊天截图入选（图池准入）。
4. **U8 深层小节题文本覆盖**（J-r120_22 漏"手工修改现部门编码"）：与上轮同模式，步骤卡切分时该句
   是否被并入相邻卡，需查该文档的卡边界。
5. QA-43"十不吊"被标成步骤格式：清单类条款不应走 step 模板（小）。

## 效度边界（结论的适用范围）

- **内部效度（本实验测什么）**：管线产物（抽取/绑定/分块）的净效应——同引擎、同 rerank、同字段、同日。
  结论"新管线产物在同等服务条件下整体更可用、绑定显著更准"可信。
- **外部效度（不直接外推什么）**：本地为 dense+BM25 两路、无 sparse 通道；生产 HA3 三路的排序分布可能不同。
  **生产回灌后仍需用同题集复测**（原回灌验证清单不变），本报告将基线从"跨环境 4:16"修正为"同环境 10:6"。

## 本轮顺带修复的工程问题（已进工作区）

1. **摄取推送索引名硬编码 bug**（`pipeline_nodes.py` 三处 `ctx.get("opensearch_index", "fuling_knowledge_v1")`）：
   ctx 从未被任何调用方设置，导致推送永远落到硬编码索引、无视 `RAG_OPENSEARCH_INDEX` 配置（serving 检索却读配置）
   ——本轮第一次重建时 2,074 条被误推进 `fuling_knowledge_v1`（已清理还原至 55 条）。修复为回退到
   `get_config().opensearch.index_name`。
2. `to_opensearch_doc` 补发 `chunk_id`/`chunk_index`（serving `_source` 契约字段，邻居拼接依赖）；
   `_ensure_opensearch_index` mapping 显式声明 4 个字段。单测：`tests/test_chunker.py::TestOpenSearchDocServingContract`。

## 数据与工件

- 采集: `scratch/local_ab_answers.json` · 盲评包: `scratch/local_ab_bundle.json`（sealed 含映射）
  · 评审输入（无映射）: `scratch/local_ab_judge_input.json`
- 裁决: `scratch/local_ab_judgments.json`（3×24）· 解盲聚合: `scratch/local_ab_report_data.json`（`scratch/local_ab_report.py` 生成）
- 对照组镜像: `scratch/local_e2e_old_mirror.py`（--preview/--register/--status/--wipe，生产只读）
- 索引: `locale2e_v1`（2,074）/ `locale2e_old_v1`（1,758）· 服务: :8001 / :8002（env overlay `.env.local_ab_{new,old}`，已加 .gitignore）
