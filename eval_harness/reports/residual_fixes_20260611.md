# 复测三残留修复 — 实施与验证报告（2026-06-11 晚）

承接 `prod_retest_20260611.md` 的残留清单与已批计划（插桩 → P2 → P3 → P1 重定向）。
验证形态统一：本地 serving `RAG_ENV=prod_ro` + rerank ON + 守卫 ON 指生产 HA3/RDS 只读。

## 改动清单（3 代码 + 1 配置）

| # | 文件 | 内容 | 类型 |
|---|---|---|---|
| 1 | `llm_generator.py` `_format_context` step_card 分支 | 带图步骤卡补 `[📷 图片]` 标签（与 image/text_chunk 分支对齐） | 代码 1 行 |
| 2 | `llm_generator.py` 规则 10 | 步骤类回答默认引用所属文档的图 + 严禁引用无关文档的图 | prompt |
| 3 | `retriever.py` `expand_step_context` + `config.py` | **超大家族防洪**：意图筛选结果 > `RAG_STEP_EXPAND_FAMILY_CAP`(默认12) 时收缩为「命中卡+同小节伙伴+文档序±2窗口」；≤cap 正常 SOP 行为不变 | 代码 |
| 4 | SAE 环境变量 | `RAG_MAX_CONTEXT_CHARS=10000`（budget_ab 既有数据支持：多题实需 7.9-9.2k） | 配置 |

## 根因实证（阶段0插桩，修正了勘察初判）

- **J-r120_23**：目标小节裸混检排 17（在 rerank 池内）且 rerank 后其 step1 卡已进 top7——
  病灶不是"池未命中"，而是人事手册 48 卡共享一个 mega parent、41 个 step_no=0 使意图区间
  筛选退化成全家族扩展（53 chunks ≈ 15k 字），组内 step_no 升序让目标垫底被 6000 字预算截断。
  **原方案 A（rerank 池 20→50）对此无效，已按用户决策跳过，省下 251 题 A/B 与 rerank ×2.5 费用。**
- **图题**：image_refs 完好到达 `_format_context`，断点 = LLM 引用倾向（×3 探针：water_soak 仅 1/3 引用）。
- **guard=False 矛盾解除**：rerank 分 52/53 在场、max 0.861≥0.8，行为正确（复测看到的 0.698
  是 sources 展示的兄弟继承分）。无需修复。

## 验证结果

| 验收项 | 结果 |
|---|---|
| 单元测试 | 107/107（新增防洪结构测试 ×2 + 步骤卡标签测试 ×1；全套 24 个失败经 stash 对比证实为既有问题，已立独立任务） |
| J-r120_23 端到端 | **拒答 → 转正**：53→17 chunks、目标小节登顶 [文档1]、答案 5 步金标准全要点（F2 模糊查询/选中修改/保存/手机号）、3 张精准截图（查询条件框/人员列表/档案表单） |
| J-water_soak | ×3 探针 3/3 引用 → 2 图（泡水+毛巾擦拭） |
| QA-24 | ×3 探针 3/3 引用 → 3 图全中金标，噪声文档（打机工资统计）0 引用——禁令生效 |
| 25 题全集（R2) | **25/25 零拒答**（基线 1 拒答）；rank 全稳；图片只增不减，所有增量（J-r120_21/22 各 3 图、BIND-04 0→3）人工看图全部强相关 |
| 大手册专项（金集 11 题） | **11/11 rank=1 非拒答**（含 25 题集外的财务手册 2 题；初报 2 миss 为脚本标题匹配 artifact——金集写"财务操作手册"实际为"财务**部**操作手册"，按 harness matching 归一后命中） |
| 为什么不跑全量 251 L0-L6 | 改动在排序之后的上下文组装层，`search_chunks`/rerank 输出可证明性不变；受影响人群（步骤卡/大手册）已被 25 题 + 11 题专项全覆盖。全量 harness 留给 SAE 发布前的最终保险（可选） |

## 遗留

- 金集 expected_docs 与实际标题存在"财务操作手册 vs 财务部操作手册"类别名漂移——评测时务必走 `eval_harness/matching.py`，裸子串匹配会假阴。
- 方案 D（人员档案 FAQ 语料补强）继续挂起：防洪修复后词汇断层不再致命（rank=1 实证）。
- 全套 24 个既有测试失败：独立任务卡排查（疑似测试环境 config 解析问题）。

## 工件

- R0/R1/R1b/R2 答案集: `scratch/prod_retest_{answers,R1,R1b,R2}_20260611.json`
- 插桩: `scratch/diag_image_inject.py` · ×3 探针: `scratch/diag_img_citation_x3.py`
- 发布配套: SAE 环境变量新增 `RAG_MAX_CONTEXT_CHARS=10000`；`RAG_STEP_EXPAND_FAMILY_CAP` 代码默认 12 无需配置
