# Console 管理界面（kb_admin）功能与 UX 评审 — 2026-07-14

**评审对象**：`claude/ontology-p0` @ `598018c`（console 最新状态；main 上缺审批中心 IA 重构与 Agent 运行中心等分支侧改动）。
**方法**：三路系统盘点（前端 26 组件逐个过 / 后端 6 路由文件全端点 / docs 历史决策与审计报告）+ 本地实机走查（scratchpad worktree 起 SIM API :8001 + Vite :5180，kb_admin 签名 token 真登录 + `?preview=kb` 满数据态，五个 tab、Agent 运行中心、贡献页全部过目）。
**结论先行**：管理台的**骨架和治理闭环已经相当完整**（五 tab IA、审批三合一、诚实降级、身份隔离都做得好）；剩下的高价值优化不是「再造界面」，而是三类——①把**后端已就绪但前端没接线**的能力开出来（Agent 治理三件套、本体翻页），②给 kb_admin 补**运营数据面**（成本/SLO/容量，表都建好了），③收掉**十来个交互一致性毛刺**（放行无确认、队列不自刷新、行动项埋页尾）。

---

## 1. 现状素描

### 1.1 结构（实测确认）

| Tab | 内容 | 可见角色 |
|---|---|---|
| 概览看板 dash | kb_admin：全库资产/运行健康/可用性/知识效果/反馈 + **三个行动队列**（差评复核、转人工、入库复审）；dept_admin 降级为本部门版 | canManage |
| 文档管理 docs | 上传入库 → 文档台账（服务端筛选/搜索/分页/批量退役/批量改可见）→ 授权治理（已放行跨部门授权+撤销） | canManage |
| 审批 approvals | 待办（Agent 高风险→上传入库→跨部门授权，排序即风险序）/ 历史（五类型 chips）同址 | canManage（内部再按角色分块） |
| 本体消解 ontology | 覆盖率 4 卡 + 未解析编号队列（确认/手动指定/驳回/批量驳回） | flag+steward 探测，404/403 自隐 |
| 成员管理 members | dept_admin 授予/编辑/撤销（按部门 chips），kb_admin 行受保护 | 仅 kb_admin |

角色 gating 链：`session.canManage`（whoami `can_manage_kb`）→ ManageView tab 过滤（`ManageView.vue:78-85`）；kb_admin/dept_admin 职责已拍板分工（授权审批归 dept_admin，入库审批归 kb_admin）。

### 1.2 flag 现实（谈优化前必须先谈这个）

console「还差什么」的第一层答案其实是**部署与 flag**，不是代码：

| Flag | 默认 | 关掉时 console 少什么 |
|---|---|---|
| `RAG_AGENT_ENABLE` | OFF | `/api/agent/*` 全 404 → Agent 审批块、运行中心、Agent 模式全部自隐 |
| `RAG_ONTOLOGY_ENABLE` | OFF | 本体消解 tab 整个自隐 |
| `RAG_QA_FACT_JOIN` | OFF | 台账「利用度」筛选静默失效、insights 的 cited/helped_users 缺失 |
| `RAG_ADMIN_NOTIFY` | OFF | 审批/申请无钉钉提醒（加剧下文「队列不自刷新」的痛点） |
| `RAG_ALLOWED_DEPTS_ACL` | OFF | 指定部门可见范围不进检索 ACL |

另有公共尾巴：**console 一切改动要重跑 build 并 SAE 重打包才见于生产**（`console-app/CLAUDE.md`），本报告全部建议共享这个 user-gated 尾巴。

---

## 2. 值得新增的功能

### 2.1 纯前端接线（后端端点现成，零后端改动，性价比最高）

| # | 建议 | 依据（后端已有） | 价值 | 建议优先级 |
|---|---|---|---|---|
| F1 | **Agent 工具治理页**：工具注册表 + kill switch 开关 + 代码↔DB 漂移告警 | `GET /api/agent/tools`、`POST /api/agent/tools/toggle`（`routes/agent.py:1006,1030`，kb_admin） | 现在全局停用一个失控工具只能 curl；这是 Agent 灰度前的治理刚需 | **高**（与 `RAG_AGENT_ENABLE` 灰度绑定） |
| F2 | **uncertain 工具调用对账台**：超时/崩溃后副作用不明的调用列表 + 人工核实处置 | `GET /api/agent/invocations`、`POST /api/agent/invocations/resolve`（`routes/agent.py:1251,1277`） | 重评报告里已声明「展示层已标注、操作台前端延期」——这是审批闭环最后一块 | **高**（同上绑定） |
| F3 | **Agent 运行中心「管理视角」**：现在只是问答页里「我的运行」抽屉（run id/轮次/tokens）；`runs/{id}` 其实返回完整 steps 时间线+工具回执+审批+最终答案 | `GET /api/agent/runs/{id}`（`routes/agent.py:1078`，owner-or-kb_admin） | kb_admin 审计一次运行现在无 UI 可用；顺手把抽屉里 `run_demo1 · default · 轮次 2` 这类技术黑话换成「问题摘要+可点回会话」 | 中 |
| F4 | **本体工作台翻页与筛选**：前端硬编码 `?limit=50` 无加载更多（`useOntology.ts:133`），积压 >50 静默截断 | workbench 支持 `cursor/offset/namespace/object_type/order`（`routes/ontology.py:240`）；coverage 支持 `object_type` 下钻 | 本体正式播种后队列会远超 50；这是唯一「数据一多就看不全」的硬截断 | **高**（组织 gate 签字、真实播种前做掉） |
| F5 | **org-tree 部门选择器**：上传/授权处的部门选择用现成聚合（ACL 组+钉钉部门映射+可授权范围） | `GET /api/kb/org-tree`（`routes/kb_console.py:84`，前端 0 引用） | 部门多了以后下拉列表不够用；131 部门的映射就绪 | 中 |
| F6 | ontology steward 深操作 UI：标识符停用/改指、对象退役/标记重复、对象详情 | `routes/ontology.py:562,591,628,659,334`（全无 UI） | 低频权力工具，队列态先行、详情页跟进 | 低 |

### 2.2 新数据面（数据/表已就绪，需加只读端点 + 看板卡）

| # | 建议 | 数据来源（已存在） | 价值 | 优先级 |
|---|---|---|---|---|
| D1 | **Token/成本看板**：按模型/类别(deep/default/quick)/部门归集的 token 与调用量、错误率、延迟 | `llm_call_log`（`schema/023`，含 `dept_group`「成本按部门归集」）。⚠️ `cost_estimate` 有意留 NULL（不编造单价）——初期只展示 **token 量与调用数**，配价表后再回算金额 | Sam 明确成本敏感；现在 console 对 LLM 花销完全盲 | **高** |
| D2 | **SLO 日趋势**：答问率/无答率/错误率日曲线 + SLO 违约历史 + 单聊/群聊结构 | `qa_daily_metrics`（`schema/004`，含 `slo_ok`/`slo_breaches_json`，rollup 已在写） | governance 只有 30 天快照，「上周变差了吗」答不了 | **高** |
| D3 | **限流/容量卡**：offered vs admitted、被拒原因分布（global_cap/per_min/thinking 配额…） | `qa_admission_reject`（`schema/017`） | 容量规划的缺失半边；被限流的用户投诉无处可查 | 中 |
| D4 | **入库质量哨兵**：每日无效分块率/乱码率/句中截断率趋势 | `ingest_quality_metrics`（`schema/021`） | L6 离线评测之外的轻量日常哨兵 | 中 |
| D5 | **转人工→知识沉淀闭环**：per-ticket SLA 时长（assigned/answered/closed_at）、`trigger_reason` 归因、`converted_to_faq` 转化率 | `escalation_ticket` 未 surface 字段（`schema/002:53`） | 与 P3-19「faq_review_queue 死表闭环」同一条产品拍板；先把指标晒出来能推动拍板 | 中 |
| D6 | 差评结构化归类：`user_feedback.badcase_category` 进差评复核卡 + 看板分布 | `schema/002:42` | 现在差评只有原因多选+评论，缺聚类视角 | 低 |
| D7 | 合规可见性：retention 任务上次运行/清理行数；主体删除（purge_subject）工单化 | `retention.py:346,521`（现全靠 CLI） | PIPL 审计友好；低频 | 低 |
| D8 | Agent 合规审计查询面（risk_level/decision/policy 过滤） + 抽检运行历史/权限收紧建议 | `agent_audit_log`（`schema/024`）、`spot_checker.py` | Agent GA 后追责回放 | 低 |

### 2.3 现有功能补强（多数需要前后端各一点）

| # | 建议 | 现状证据 | 优先级 |
|---|---|---|---|
| E1 | **审批队列自动刷新/新单提醒**：至少「审批 tab 停留时 60s 轮询 + 侧栏红点联动」；`RAG_ADMIN_NOTIFY` 打开做钉钉推送兜底 | 全部队列 load-once + 30s staleness 门（`useKb.ts:214`、`useAgentApprovals.ts:43`），挂着页面永远看不到新单 | **高** |
| E2 | **「待你处理」chip 聚合三队列**：差评之外把转人工、入库复审（以及待审批）计数一并入 chip 条 | chip 只做了差评（`KbAdminDashboard.vue:28-30`，注释「唯一行动区」已过时——页尾实际有三个队列） | **高**（小改） |
| E3 | **文档 360 抽屉**：单文档聚合视图（版本史+可见性解释+被引问题+差评+chunk 状态`doc-status`） | 现在分散在 4 个 modal + 看板反查；`/api/kb/doc-status` 台账内无入口 | 中 |
| E4 | **审批历史分页/搜索/时间范围**：后端固定 `_APPROVAL_HISTORY_LIMIT` 无分页参数（`routes/kb_access.py:604`），前端仅类型 chips | 审计/追溯场景一多必然要 | 中 |
| E5 | **授权到期机制**：已授权列表加有效期或到期自动进「建议复核」队列（现在永久授权 + 90 天 amber 提示） | `AccessGrantList.vue:19-27` | 中（需后端） |
| E6 | 台账行「来源新鲜度」：`sources[].doc_date` 渲染（答案侧 chip 同理） | blindspot P2-31 已备好数据、前端「后续接」 | 低 |
| E7 | 批量操作失败明细完整化（>8 条被 `slice(0,8)` 截断） | `useKb.ts:328` | 低 |

---

## 3. UX 优化点

### 3.1 已知未修（staging 2026-07-11 探索遗留，先收掉）

1. **贡献处置后兄弟面板不刷新**（驳回后「待回答」「我的贡献」不同步，重载才一致）。
2. **「我的贡献」时间戳裸 ISO**（`2026-07-11T08:57:17`），与他处 `2026-07-08 05:29` 风格不一。
3. **审批深链 URL 残留他 tab 参数**（`?tab=approvals&q=拉片机`）——`ManageView.vue:65-70` 只增删 `tab/view` 不清外来键。
4. LLM 偶发正文内联「（来源：…）」——提示词护栏遵循问题，观察项。

### 3.2 本次实测新发现

5. **「放行」类动作是审批面里唯二无确认的写操作**：上传审批「通过」（`ApprovalQueue.vue:63` 直接 `@click="approve(d)"`）与跨部门授权「授权」单击即生效；对比退役/批量/改可见/Agent 批准全都有 confirm，模式不一致。放行影响面（全公司可检索/跨部门放行）不小于退役，建议补轻量确认（或撤销窗口）。
6. **Agent 审批过期单仍呈可批态**：mock 两单都「已过期」但批准/驳回按钮照常可点（后端会拒，前端应过期即禁用 + 显示「过期视同拒绝」+ 一键清理）。
7. **看板行动项埋在 ~2900px 页尾**：dash 是资产→健康→可用性→效果→反馈→三队列的超长单页；E2 的聚合 chip 是最小解，更彻底的解法是给 dash 加 sticky 锚点段导航（资产/健康/效果/待处理），或把「待处理」整段提到首屏。
8. **上传卡永久占据文档管理首屏**：台账才是高频工作区，上传是低频动作；建议上传卡默认折叠为一行按钮（保持既定 IA 顺序不变，只降视觉权重）。
9. **管理队列全量拉取无分页**：pending-approvals/access-requests/escalations/feedback-review 等一次拉全（服务端 limit≤50 兜底），量大时既截断又无提示——与 F4 同属「数据变多就露怯」一族。
10. 小项：差评区滚动定位用 `setTimeout 400ms`（慢机可能落空，`FeedbackReviewList.vue:30`）；`aria-invalid` 挂在 div 上（`MemberRoleManager.vue:99`）；台账 6 个行操作纯图标靠 title 提示，新手可发现性弱；成员管理页在少数据时大片留白；运行中心抽屉技术黑话（见 F3）。

### 3.3 值得肯定、应保持的模式（评审对照基线）

诚实降级（404 静默/5xx 显式可重试/绝不编造数字，如 coverage 的「近似口径」直接写在卡上）；危险操作行内 loading + 定向更新；身份切换 eager-clear（identityScope）；「0 条」与「加载失败」严格区分；预览原件解「盲批」。新功能应沿用这些约定。

---

## 4. 明确不要做（已有拍板，避免回锅）

- **移动端/375px 适配**（console 定位 PC，多次 WONTFIX）。
- **聊天阶段化假状态条**（禁止编造管线不存在的阶段）。
- **审批卡内嵌 diff/证据链/影响范围**（PR-4 完整版范围，canary 不做）。
- **批量审批、聊天内本体消歧、风险预览矩阵、steward SLA**（2026-07-11 外评裁决裁掉）。
- 375px 之外的既定红线：UI 非平凡改动必须走 `/ui-iterate` 硬门（`console-app/CLAUDE.md`）。

---

## 5. 落地节奏建议

| 批次 | 内容 | 特征 |
|---|---|---|
| α（一轮 /ui-iterate 可收） | E2 聚合 chip、E1 轮询、#5 confirm 补齐、#6 过期禁用、3.1 的 1-3、#10 小项 | 纯前端、零 schema、低风险 |
| β（Agent 灰度配套，`RAG_AGENT_ENABLE` 开启前后） | F1 工具治理、F2 对账台、F3 运行中心管理版 | 纯前端接线，端点全备 |
| γ（运营数据面） | D1 token 看板、D2 SLO 趋势、D3 容量卡（+顺手 E4 历史分页） | 需新只读端点；表已存在，无 migration |
| δ（跟组织节奏） | F4 本体翻页（gate 签字前）、D5 闭环指标（推动 P3-19 拍板）、E3 文档 360 | 与业务里程碑绑定 |

公共尾巴：所有批次都要 console build + SAE 重打包才上线（user-gated）。

---

## 6. 附录：评审环境与凭据

- 实机：scratchpad worktree（detached @598018c）+ SIM API `:8001`（`RAG_SIMULATE=true RAG_ENV=local RAG_AGENT_ENABLE=true RAG_ONTOLOGY_ENABLE=true RAG_SIM_USER_ROLE=kb_admin`）+ Vite `:5180`（代理已指 8001）；`issue_session_token` 铸 kb_admin token 真登录 + `?preview=kb` 满数据 mock 双态各过一遍。预览沙箱进不了 `~/Downloads` 下的 worktree（TCC/EPERM），故落在 scratchpad——复现时注意。
- 三路盘点报告要点已内联本文并按 `file:line` 抽查核实（ontology limit=50、agent tools/invocations 端点、schema 004/017/023 表、approve 无 confirm 均已直接验证）。
- 主要引用：`console-app/src/views/ManageView.vue`、`composables/useKb.ts`（1549 行主 store）、`useOntology.ts`、`useAgentApprovals.ts`、`components/manage/*`；`opensearch_pipeline/routes/{kb_console,kb_access,contribution,agent,ontology}.py`；`schema/002,004,017,021,023,024`；`docs/audits/staging_ui_exploration_2026-07-11.md`（worktree）、`docs/agent_reeval_p0p1_fix_status_2026-07-11.md`、`docs/blindspot_audit_fix_status.md`。
