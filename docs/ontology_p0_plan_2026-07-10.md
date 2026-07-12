# 本体(Ontology)层 P0–P1 实施计划 —— 身份脊柱 + PMC-1 箱规/香规垂直切片

> 版本：v2（2026-07-10，已并入外评收紧 S1–S9）。
> 上游设计：《富岭企业智能化方案_基础分析总纲 v1.1》《富岭企业Agent本体层设计 v1.1》《富岭本体P0-P1落地细化》（白领痛点整理/）。
> 分支：`claude/ontology-p0`（切自 `claude/enterprise-agent-plan-6e9072`），PR 小步合回 agent 分支，不直接上 main。

## 1. 定位与勘误

**本体层 = 身份、语义关系与来源治理的控制面**：管 ①对象身份（canonical + 别名脊柱）②关系（link）③每属性权威来源（attribute_source）④受治理动作路由。**不是万物 SoT**——库存/订单/金额/完工状态仍以各 SoR（U8/Max/钉钉表单）为准，本体只登记"该属性听谁的、多新鲜、谁负责"。

设计文档 → 实施的勘误（以 `schema/` 目录实况为准）：

| 文档所写 | 实况 |
|---|---|
| 迁移编号 024–028 | 已被 agent v2 占用（022–026 已存在）；本体表族重编 **027 core / 028 identity / 029 link / 030 sem_views / 031 event(P2 预留不写文件)** |
| "018 审批""023 预留 v2 P4" | 018=gen_meta、023=llm_call_log；审批=025 |
| "sem_ 021 vs 028 归属" | 不存在——021=ingest_quality_metrics，sem_* 由 030 独占 |
| 钉钉问答 demo / 钉钉 HITL 卡片 | console-first 既定拍板：PMC-1 走 `/api/agent/ask`，钉钉侧降 P2+ |

**审批双门**（对文档的显式偏离）：会话内提案走 v2 approval_request 原样（门 A）；steward 工作台批量处置**不写 approval_request**，在 resolution_case/identifier 自身状态机上处置（kb_access 范式，门 B）——approval_request.run_id 锚定 agent_run，且 B6 对账 reaper 会重驱 decided-but-not-resumed，无 run 的合成审批单会被误重驱。

## 2. 设计收紧（外评采纳，S1–S9）

- **S1 在线读写彻底分离**：`resolve()` 纯读、永不落库、永不 auto-activate（否则 READ_ONLY 工具带写副作用，撕毁 v2 ToolSpec 风险分级契约）。持久化仅四条路径：播种、离线回填 worker、steward 工作台决策、受治理 Action（`ontology.identity.resolve` 经审批）。
- **S2 候选承载层**：`uk_ns_norm` 装不下"一个未解析编号 × N 候选目标"→ 新增 `ontology_resolution_case`（观测聚合+evidence_json 证据快照+seen_count）+ `ontology_resolution_candidate`（case_id/target/method/confidence）。工作台以 case 为单位批量处置；积压/人工审核率统计由此出。
- **S3 P0 最小纠错**：deactivate_identifier / repoint_identifier（原子）/ retire_object / mark_duplicate（仅标记 merged_into，不做传播）。全量 merge/split 仍 P2。杜绝手改 SQL。
- **S4 identifier 唯一性**：业务 scope 唯一由 namespace 编码（`customer:KFC`）承载；唯一约束="至多一行 active"——`uk(namespace, norm_value, active_key)`，active_key 生成列（active→1，其余→NULL）。有效期不进 P0。
- **S5 stewardship 独立化**：`ontology_stewardship`（scope_type object_type|namespace|attribute → steward_dept）承载授权；attribute_source 回归纯来源治理。
- **S6 阈值分层机制**：τ 查表 (namespace, method) 带全局默认 0.95/0.70；逐层校准数字随分层 GT 积累再填。embedding 恒只召回候选、构造性无 auto 通路。
- **S7 sem_* 授权收紧**：不授通用 `fuling_ro`；`ontology/sem.py` 服务层是唯一读取口；未来 readonly_sql 接 sem_* 必须走服务层部门 WHERE 注入。
- **S8 packing 参数配置化**：`packing_math.py` 纯数学内核；折边余量/柜容/取整/适用品类/生效期/来源=版本化参数对象（object_type='calc_rule'，steward=PMC），输出引用参数版本。
- **S9 验收分层**：namespace×method 分层 precision/recall + **false-merge 率单列硬门** + false-split/unresolved/人工审核率/auto 占比 + packing 数值/单位/取整/规则版本 + anchor flag ON/OFF 延迟差 + 非 PMC 零退化。

## 3. go/no-go 前置（Phase 0，组织侧，四项签字前禁止真实播种）

① U8 T-1 附属库可 diff 性（信息部）② steward 编制与日处理量（业主 HR；冷启动工期 `≈(N×覆盖率)/(人数×D)`）③ ground-truth 50–100 标注对（PMC 三品类）④ 边界签字+纠错 SLA（Product/Revision/SKU 判据、PackingSpec verified 口径、误配纠正时限）。

豁免并行：normalize 纯函数与 027–029 DDL 可先写；播种/阈值/验收不得先行。

## 4. 工单（PR0–PR14）

**Phase 1 空脊柱**：PR0 本文档+README 勘误 · PR1 schema 027/028/029+ci_load_schema · PR2 `ontology/ids.py`+`normalize.py` · PR3 `ontology/store.py`(含纠错四操作)+`attribute_source.py`+`stewardship.py`+Fake+真库测试 · PR4 `ontology/resolve.py`+`agent_tools/ontology_resolve.py`（纯读；不接 registry）· PR5 `ontology/seeding.py`+`scripts/ontology_seed.py`（--dry-run 默认；真播种 user-gated）。

**Phase 2 HITL 闭环**：PR6 `routes/ontology.py`（flag `RAG_ONTOLOGY_ENABLE` off→404；case 队列/批量处置/纠错/统计）· PR7 console `OntologyWorkbench.vue`+ManageView tab · PR8 门 A `agent_tools/ontology_identity_resolve.py`（LOW_WRITE always-approval）+approval_store approver_scope seam · PR9 DataWorks 回填 worker。

**Phase 3 PMC-1 切片**：PR10 schema/030 sem 视图+`ontology/sem.py`（服务层 ACL 行过滤 fail-closed；未消解回落原值）· PR11 `packing_math.py`+calc_rule 参数对象+`agent_tools/packing_calc.py` · PR12 `packing_lookup.py`+knowledge_search 实体锚定（flag `RAG_ONTOLOGY_ANCHOR_ENABLE` off；不动 retriever.py）· PR13 唯一接线+L7 重冻（4 工具进 registry+prompt+增族）· PR14 staging 全链+`scripts/ontology_backtest.py`+验收报告。

**明确不做（P2+）**：U8 写回全链、事件闭环(031/outbox)、merge/split 全量传播、identifier 有效期、Max 集成、KIE 候选生产线、HA3 chunk 打 object_id 标签、「人」的脊柱（待命名会签）、钉钉 HITL 卡片、active_entities 改造。

## 5. 验证

每 PR `make test`+`make lint`；真库走 host-pin 本地 MySQL（ci_load_schema 灌 027–030）；每 Phase 收口跑 `make release-gate`（251 零退化）+`make agent-eval-gate`（L7）；M3 验收=S9 分层指标 + PMC-1 Top-3≥90% + 覆盖率≥70% + 越权 100% 拦截。

所有 staging/prod apply、真播种、DataWorks 部署、SAE deploy 逐项 user-gated。
