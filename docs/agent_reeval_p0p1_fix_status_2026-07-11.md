# Agent worktree 重评 P0+P1 修复台账（2026-07-11）

对象：`opensearch-rag-pipeline-agent-worktree-reevaluation-2026-07-10.md`（重评报告）+
`docs/audits/agent_worktree_reeval_verification_2026-07-11.md`（逐行复核备忘）确认的
**6 组 P0 + 15 条代码侧 P1 + 2 条 P2**。分支：`claude/ontology-p0`（worktree）。

状态图例：✅ 代码+测试闭环 · 🟡 代码侧闭环、留组织/运维尾巴 · ⏸ 刻意不做（写明理由）。

## P0

### P0-A 对象存在性与 evidence 跨 ACL 泄露 — ✅
- `routes/ontology.py` `_restricted_candidate_stub()`：不可读候选**白名单从零重建**常量占位行
  （只留 candidate_id/case_id 定位键），废除 `{**c}` 展开后删字段——target_object_id/
  features_json/method/confidence 不再出参。
- 对象搜索 ACL **SQL 谓词下推**：`ontology/authz.py::acl_read_predicate_sql`（与
  `can_read_object` 同模块单一语义来源）+ `store.search_objects_authorized/
  count_objects_authorized`——LIMIT 与 COUNT 只作用授权集合，total/truncated 不再泄露
  不可见对象数量，分页反饿死；路由保留行级复核当纵深防御。
- `agent_tools/ontology_resolve.py` `_unresolved_payload()`：resolved-不可读 / candidate-
  全不可读 / 真无候选三种情况**逐字节同答**；候选先过 ACL 再截断 top-3（反饿死）。
- 测试：`tests/test_ontology_acl_matrix.py` 24 例——5 角色 × 3 密级矩阵，断言不可读响应
  结构等价于不存在。

### P0-B Agent identity 写路径缺 requester object/action ACL — ✅
- `agent_tools/ontology_identity_resolve.py`：`_gate_target()` 以 ctx.acl_groups（服务端注入）
  调**工作台同一个** `authz.can_mutate_identity()`，置于 version/scope 检查之前（错误文案
  不泄露存在性）；不可见与不存在同文案同结构；approver scope 校验保留为额外条件；
  propose 侧同闸（发起人过不了闸 → scope 收敛 ''=仅 kb_admin 可审）。
- 复核追加的 canonical_ref 回退泄露：`_display_target()` 门后语义漂移兜底改用不透明
  `对象 <object_id>` 占位，绝不回退 canonical_ref。
- 刻意无 kb_admin role bypass（读写工具同 fail-closed 纪律，专测钉死）。

### P0-C 审批 expiry 与 edited decision 未绑定 — ✅
- `approval_store.decide()`：同一 FOR UPDATE 事务内比较 `expires_at`——过期 pending
  **原子转 expired** 并返回 `DECIDE_EXPIRED`（不再依赖 reaper 窗口）；CAS UPDATE 条件带
  `expires_at > NOW(3)`（读后跨界兜底）。
- **决定绑定最终参数摘要**（schema/031 `approval_decision.final_args_digest`）：
  approved=原 args_digest；edited=人工改后参数原文 sha256（digest 无 PII）。
- `routes/agent.py` 已决重放：kind 必须同向 + edited 参数 digest 必须与库内决定行一致
  （不一致/决定行缺失/031 前无 digest 的历史行 → 一律 409）；reason/decided_by 以库内
  不可变决定行为准（重放者是重驱者，不冒名决策人）——「DB 批 qty=1、重放执行 qty=999999」
  的动态探针场景关闭。
- 测试：`tests/test_agent_approval_integrity_db.py`（真 MySQL 6 例：过期原子拒、digest 绑定、
  并发双批单胜者、幂等键语义、临界时间、CSV 队列）+ `tests/test_routes_agent.py` 重放矩阵
  5 例。

### P0-D 前端审批状态跨身份复用 — ✅
- 新增 `console-app/src/composables/identityScope.ts` 身份作用域注册中心：身份键 =
  userId+role+ACL 版本+token；三层防线 = useAuth 5 个转换点 eager 同步清空 + 每个 loader
  入口 lazy 对账 + 在途请求指纹判废（A 的慢响应绝不落进 B 的视图）。
- 注册 5 个 store：agentApprovals（审计主标的）、ontology（d4eb5d4 土制重置迁移进来）、
  kb（14 个 loader + seq 计数器改递增防撞号）、contribute、ask（同人换证不打断在途问答，
  真换人才清）。
- 测试：`identityScope.spec.ts` 12 例（switch-account/logout/401 re-login/role refresh/
  在途竞态判废）；vitest 全套 251 例绿。

### P0-E 高风险写的崩溃/超时不是可恢复执行 — 🟡（代码侧闭环；durable worker=PR-3 范围）
- **uncertain 状态机**（schema/031 tool_invocation.status 增 `uncertain`）：有副作用工具
  超时 → invocation 落 uncertain（副作用不可知，**不再谎报 failed** 诱发盲重试）；模型收到
  「已标记待对账，勿假定失败后重试」。
- **stale executing 对账**：reaper 每轮 `mark_stale_invocations_uncertain`（默认 900s，
  `RAG_AGENT_INV_STALE_S`）——进程崩溃僵尸进人工对账通道。
- **同键状态机护栏**（tool_executor）：uncertain→阻断自动重试；executing 新鲜→拒双跑；
  executing 陈旧→CAS 转 uncertain；failed→CAS 回收原行重试（fencing 单胜者；修复 uk_tool_idem
  下「任何非 succeeded 残行都把后续重试撞成 IntegrityError」）。
- **人工对账闭环**：`GET /api/agent/invocations?status=uncertain` + `POST
  /api/agent/invocations/resolve`（kb_admin，note 必填，CAS 单向，入合规审计）。
- **原子挂起**（run_store.suspend_run_atomic）：checkpoint + approval_request +
  approval step + running→suspended **单事务**（原四段分事务的半态消除）。
- ⏸ **HIGH_WRITE 迁独立 durable worker（lease+fencing token+outbox）**：重评报告自己排为
  PR-3 独立项；当前无已接线的 HIGH_WRITE 业务工具（canary 只读起步），本批不做——已在
  tool_executor 模块 docstring 声明边界。
- 测试：`tests/test_agent_runtime_hardening.py` 超时/护栏/回收 8 例 + routes 对账端点 1 例。

### P0-F Agent 尚未形成用户可用链路 — ✅（canary 级；灰度开关默认关）
- 后端 run center：`GET /api/agent/runs`（我的 runs）+ `GET /api/agent/runs/{run_id}`
  （run+步骤时间线+invocations 回执状态+审批指向；他人 run 404=不可见即不存在；kb_admin 全量）
  + `tool_result` SSE 帧（driver 裁决后发射 status+耗时，无内容/参数）。
- 前端 canary（console-app）：
  - `useAgentAsk.ts`：独立 `/api/agent/ask` transport，帧协议全覆盖
    （session/chunk/tool_call/tool_result/approval/done/error/[DONE]）；能力探测
    （404/403→开关/入口零渲染，不留死入口；中途 404→自动回退旧路径重发）；
    kill switch（「Agent 模式」pill，默认关=旧 RAG；localStorage 带 uid 戳，换人作废）；
    identityScope 注册（换人清空、同人换证保留偏好只重探测）。
  - `AgentFlow.vue` 阶段化状态条：**只映射真实事件**（已提交→回答中→调用工具→工具完成→
    等待审批→完成/失败），tool_result 四态 chips（完成+耗时/失败/被策略拒绝/等待审批）。
  - `AgentRunCard.vue` 结局卡：挂起卡（工具+脱敏参数+审批单号+撤回申请+审批人可达时
    「去审批」）；**uncertain 显式「结果不确定，已进入人工对账，勿重复发起」**；failed
    给下一步操作提示；断线按 run_id 5s 轮询、终态即停（批准≠成功，回执可见）。
  - `AgentRunCenter.vue` 运行中心抽屉（问答侧，员工可达）：我的 runs + 详情
    （审批块/工具回执/步骤时间线/本人挂起可撤回）。
  - 审批卡补第四处置「修改后批准」（JSON 编辑→kind=edited，own 申请不渲染该按钮）
    + expires_at 相对有效期。
  - 验证：vitest 30 文件 277 例 + vue-tsc + build + Playwright e2e 54 passed
    （新增 agent-canary.spec 3 用例×3 视口）全绿。
  - 已知限制（如实）：agent 流式为节流全量渲染简化版（未接匀速吐字泵）；挂起后 run
    完成的最终答案不回填聊天气泡（卡片给回执+指路运行详情，不做猜测性解析）；
    kb_admin 对账操作台前端未做（后端端点已有，展示层已显式标注待对账态）。

## P1（重评报告 §6 顺序）

| # | 条目 | 状态 | 修复 |
|---|---|---|---|
| 1 | tool_invocation.approval_request_id 未填 | ✅ | record_invocation 增参；adjudicator 消费 ApprovalGrant 时注入 ctx → tool_executor 落列；invocation→approval 可直接回放 |
| 2 | checkpoint 未加密且 digest 不验证 | 🟡 | resume 前核 sha256（executor._verify_checkpoint）——篡改/损坏拒绝续跑并回滚 suspended；**加密⏸**：checkpoint 需 messages 原文往返（sanitize.py 既有说明），治理走 retention 短留存+主体擦除，落盘加密属基建项另立 |
| 3 | 一轮多 tool call 挂起丢调用 | ✅ | loop._process_calls + RunSuspended.remaining_calls 进 checkpoint；resume 处置 pending 后逐个续处理（再命中审批再挂起）；**每个 tool_call 最终都有 tool 消息**（OpenAI 消息序合法）；旧 blob 无该键→[]（形状兼容） |
| 4 | resume 改变原始执行上下文 | ✅ | channel/conversation_id 一律取 agent_run 行（body 不可覆盖）；deadline=每活跃执行段一个新窗口（**有意设计**，挂起可跨天，已写明 docstring）；耗量 durable 播种（原实现已对，复核确认） |
| 5 | 工具可见集未按 policy 收敛 | ✅ | PolicyEngine.would_grant（authorize 同规则源的 args 无关投影）+ registry.attach_visibility_filter；必然被拒的工具不进模型可见集；过滤 fail-open（调用时 Policy 仍兜底） |
| 6 | obligations 只收集不执行 + output schema 不校验 | ✅ | tool_executor 义务执行点：limit_rows/mask_output/redact_output 内置执行器，**未注册义务在副作用前 fail-closed 拒绝**；义务执行器异常→输出扣留；output_schema 校验 receipt——违约读工具 failed、写工具 uncertain（副作用已发生进对账） |
| 7 | self-approval 缺生产启动断言 | ✅ | config.py 生产守卫 hard-raise（production/staging + RAG_AGENT_ALLOW_SELF_APPROVAL）+ routes 运行时环境复核（防启动后注入，fail-closed） |
| 8 | link 语义不完整 | ✅ | LINK_TYPE_SPECS（5 型：sku_of_product/packing_spec_of_sku/stacking_spec_of_sku/mold_of_product=single，material_of_product=multi）+ add_link 重写（端点 FOR UPDATE 校验存在/active/类型匹配、自指拒、single 型锁内查重 → LinkCardinalityViolation、同三元组幂等）+ **schema/033** active_single_key 生成列 UNIQUE（DB 级拒双活，绕服务层直插也被 1062 拦，专测钉住）；已 apply 本地+幂等重放验证 |
| 9 | store 边界可绕生命周期 | ✅ | 裸写原语（mint_object/insert_identifier/add_link）加必填 keyword-only `_caller` 白名单闸（seeding/backfill/steward_workbench/governed_action/store_internal/test），非法调用 PermissionError 零副作用；033 补三 FK（merged_into RESTRICT、source_case_id/resolved_identifier_id SET NULL）；superseded_by 刻意不加 FK（repoint 事务序不容，文件头有评审记录） |
| 10 | provenance 可缺失或陈旧 | ✅ | golden 全属性须溯源覆盖否则 fail-closed（mint/with_alias/update 三挂点）；update_golden 按 diff 强制**改值必须带新溯源**、删值剪除溯源行；sem.lookup_specs 透出 source/version/as_of（缺溯源回退 updated_at，再无为 None 绝不编造） |
| 11 | backup steward 未生效 | ✅ | stewardship.effective_steward_depts；工作台三处（写侧门/case 可见性/队列粗筛）认 backup；agent 侧 approver_scope 写 CSV "steward,backup"（schema/031 加宽 160）+ 决策侧 _scope_covers CSV 拆分 + 队列 FIND_IN_SET 分量匹配；对象级读权不因 stewardship 放宽（三分授权专测钉住） |
| 12 | coverage 分母不可靠 | ✅ | population 分母=全量源计数（limit 命中后 continue 只计数不处理，不再 break 把分母覆写成批大小）；去 min(1.0) 掩蔽——超分母原样透出 + 返回结构恒带 anomaly 字段（"coverage_exceeds_population"）+ WARN；per-object-type 仍 approx（population 表加列属后续 schema 扩展，已备案） |
| 13 | auto acknowledgement 可伪造 | ✅ | ack 升级为持密钥签发的签名 manifest：`RAG_ONTOLOGY_AUTO_ACK=<manifest_path>:<hmac_hex>`，manifest 必填 op/date/docset/gt_summary/signer，HMAC-SHA256(`RAG_ONTOLOGY_ACK_HMAC_KEY`) 恒时比较；无密钥/篡改/异钥/过期/缺字段/旧格式一律拒；auto 默认关不变（真实 GT/backtest 仍是组织项，验签机制已立） |
| 14 | 生产 apply 不可完全重放 | ✅ | splitter 引号感知重写（三种 MySQL 引号/转义/双写；未闭合 fail-closed）+ DELIMITER 响亮拒绝（历史 002/003/006/016/017/018 走 mysql CLI 既定通路）+ `scripts/apply_ontology_dbs.py` 入仓（MANIFEST 自动发现 ontology 迁移、复用 apply_migration 全套守卫、默认 dry-run）；回归测试 12→49 例 |
| 15 | DataWorks 节点未调度 | 🟡 | 代码侧：`RAG_NO_MODEL_RESOLUTION=ack`（config 惰性哨兵——llm/ocr/vlm/embedding 无端点无 key，意外调用立刻失败；生产守卫豁免 key 要求、禁 Gemini 检查照跑）+ retention/ontology_backfill/ontology_invariants 三节点接线去 DASHSCOPE_API_KEY 硬要求；**调度本身（DataWorks 节点建立+dead-man）= user-gated 运维项** |

## P2

| 条目 | 状态 | 修复 |
|---|---|---|
| owner_dept 白名单 fail-open | ✅ | kb_authz 加载失败由「告警放行」改 RuntimeError fail-closed 拒写（治理控制面准入不带病放行），mint 全路径零副作用专测钉住 |
| attribute source desired state 留 ghost | ⏸ | 版本化 diff+停用审计属 desired-state 机制重构，重评报告未列 P1，不入本批 |

## UX 建议外评裁决（2026-07-11，用户转交 GPT 建议后拍板）

**采纳并执行**：①`tool_result` SSE 帧（driver 裁决后发射 status+耗时，无内容/参数——
「调用工具」阶段有明确结局，兑现「批准≠成功」最短路径；events/executor/routes 三点 +
守护测试）；②聊天阶段化状态条（**只映射真实事件**，拒绝编造「正在理解/核验结果」等
管线不存在的假阶段）；③运行中心撤回按钮（发起人自撤=既有 rejected_terminate 通道）；
④审批卡补第四处置「修改后批准」+ expires_at 展示；⑤uncertain/失败的下一步操作文案。

**裁掉（过度设计/无对象/已闭环）**：审批卡 diff/证据链/影响范围（需新数据模型，PR-4
完整版范围）；聊天内 ontology 消歧（canary 未注册 ontology 工具，不可达）；风险预览矩阵
（Policy 层已强制，低写/批量工具不存在）；steward SLA/移动端审批/通知安全跳转/批量审批
（无对应后端事实或组织项）。

## 顺手修复（不在清单但同批发现）

- deadline 真比较：RunBudget.deadline 此前全链零调用点（形同虚设）——executor 每模型轮
  比较，超时 fail-closed。
- schema/031 未登记 ci_load_schema.sh MANIFEST（E 线代补，防 loader 完整性闸红）。
- README 031 撞号勘误：原「本体事件 P2 预留」的 031 已被本批占用，本体事件顺延取新号。

## 总验证（2026-07-11 收口）

- Python 全量（xdist，本地真 MySQL 全跑）：**3237 passed / 1 skipped / 0 failed**
  （基线 2994，净增 ~240 例守护测试）；ruff `opensearch_pipeline/ tests/` 全绿。
- console：vitest **30 文件 277 例**（基线 26/236）+ vue-tsc + build + Playwright e2e
  54 passed / 24 skipped（既有 skip）全绿。
- schema/031（fuling_operation）、033（fuling_ontology）已 apply 本地 docker + 台账行 +
  ci_load_schema MANIFEST 登记 + 033 幂等重放验证；apply_ontology_dbs 自动发现验证
  （7 文件族含 033）。

## 未验证 / 遗留（如实声明）

- 真实 SAE / staging 部署、真钉钉端到端、真 DashScope 全链（本批全部单测/契约/本地真库级）。
- schema/031、033 仅 apply **本地 docker**；staging/prod apply 为 user-gated。
- `apply_ontology_dbs.py --commit` 真实执行未验证（mock 单测 + 本地 dry-run）。
- PR-3（durable worker）、PR-6（吸收 main 3 commits + 开 PR + HEAD CI 证据）、PR-7
  （Phase 0 签名 gate artifact/GT/backtest）为重评报告排期的后续独立项。
- 组织 gate 四项（U8 T-1 diff/steward 排班/PMC GT/边界签字）未签——真实播种与 auto
  activation 保持硬关闭不变。
