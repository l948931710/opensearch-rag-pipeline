# Agent worktree 重新评估报告——独立复核备忘

复核日期：2026-07-11（America/Los_Angeles）
仓库：`l948931710/opensearch-rag-pipeline`
被复核对象：`opensearch-rag-pipeline-agent-worktree-reevaluation-2026-07-10.md`（针对 `claude/ontology-p0` @ `9ea4017` 的分支重评报告）
复核所用代码：worktree `/Users/laijunchen/Downloads/agent-v2-worktree`（HEAD `4632992` = `9ea4017` 之上一个 console UX 提交；其 Python / schema / `.github` 文件与 `9ea4017` **逐字节一致**，可代核）

> 本备忘不是又一份分支审查，而是对 2026-07-10 那份「重新评估」报告**本身是否可信**的对账。方法：git/GitHub 事实全量比对 + 六组 P0 代码逐行亲核 + 三个只读子代理分别核 agent-runtime P1、ontology P1/P2 与「已改进」项、以及全套测试计数复跑。下文所有 `file:line` 均指 `claude/ontology-p0` @ `9ea4017`（在上述 worktree 亲核），非 `main` 工作树。

---

## 1. 结论先行

**报告高度可信，核心结论（生产高风险 Agent = NO-GO，方向正确、可继续投入）成立，可以按它行动。**

在 40+ 项可独立核验的事实中，只发现 **1 条部分失准 + 1 处半句措辞无实据**，其余（六组 P0、15/17 条 P1/P2、4 项已改进、全部测试计数、全部 git/GitHub 事实）全部属实，且引用的文件行号几乎逐行精确。报告的证据纪律尤其可取：把生产库落地一律降格为「操作者陈述、未独立复核」，明示动态探针范围。

据此对本仓既有判断的校准：与 memory 的 [[agent-platform-v2-state]]（"合 main 待系统测试成熟、全链未端到端真跑"）、[[ontology-layer-p0-2026-07-10]]（"组织 gate 四项未签，签字前禁真实播种"）完全一致，无冲突。

## 2. 事实层核对（全部对上）

| 声称 | 实测 | 结论 |
|---|---|---|
| 增量 `3c339fe..9ea4017` = 10 commits / 50 files / `+3961/-470` | 逐字一致 | ✅ |
| 全量 `17fb00a..9ea4017` = 21 commits / 59 files / `+10235/-73` | 逐字一致 | ✅ |
| 基线 `17fb00a`、merge-base(main) `9fa3e07` | 一致 | ✅ |
| 相对 main ahead 30 / behind 3 | 一致；behind 3 = 0cbb0f8(serving P0 flags) + 2 console 修复 | ✅ |
| origin 头 = `9ea4017`，审查期未漂移 | `git ls-remote` 一致 | ✅ |
| 无 PR、`9ea4017` 无 workflow runs / commit statuses | `gh`：PR 列表空、runs=0、check-runs=0、status pending total=0 | ✅ |
| 生产/staging 独立库、9 表 2 视图、`fuling_ro` 直查 1142 | **仅存在于提交信息**，仓库内无独立证据 | ✅ 报告已正确降格为「操作者陈述」 |

## 3. 六组 P0 阻断——逐条亲核，全部证实

| P0 | 裁决 | 关键证据（`claude/ontology-p0` @ 9ea4017） |
|---|---|---|
| **A** 候选 / evidence / count 跨 ACL 泄露 | ✅ 证实 | `routes/ontology.py:177` `_enrich_candidates` 对不可读候选用 `{**c}` 展开 `SELECT *` 行后只清 4 个字段——`target_object_id`/`features_json`/method/confidence 原样出参（注释写「不泄 ref/title/type」，是漏改半截）；`ontology.py:287-295` 对象搜索先 `limit` 后 filter + 未授权 `count_objects()` 当 `total`；`agent_tools/ontology_resolve.py:148-153` receipt 在 candidate 分支保留 status/confidence（resolved-不可读分支已清零，恰是漏网分支） |
| **B** Agent identity 写路径缺 requester object/action ACL | ✅ 证实 | `agent_tools/ontology_identity_resolve.py:125` `run()` 只校验存在/active/version/审批 scope 漂移，**从不调 `can_mutate_identity`**；`grep` 确认该函数仅出现在 `routes/ontology.py:375,552,621`（工作台三处写路径），Agent 工具侧零调用。附带发现：`ontology_identity_resolve.py:191` 成功消息 title 被遮蔽时回退显示机密对象 `canonical_ref`——报告未抓到这层 |
| **C** 审批 expiry 与 edited decision 未绑定 | ✅ 证实（最重） | `agent_runtime/approval_store.py:182-185` 决策 CAS 条件仅 `status='pending'`、**无 `expires_at` 比较**，过期全靠 `routes/agent.py:82` 默认 300s 一轮的 reaper；`routes/agent.py:400` 已决重放只比 `status == outcome.kind`；`executor.py:172-178` 一次性 grant 的 `args_digest` 由**本次 HTTP outcome 的 `edited_args`** 计算，落库的 `approval_decision.edited_args_json` 全程无人读取。qty=1 批准、重放执行 qty=999999 在代码上必然成立 |
| **D** 前端审批状态跨身份复用 | ✅ 证实（触发面见 §5.4） | `console-app/src/composables/useAgentApprovals.ts:31-38` 模块级 singleton `items/supported` + `STALE_MS=30_000` 与用户 ID 无关；`__resetAgentApprovals()` 仅测试调用（`grep` 确认运行时无身份切换 hook）；`d4eb5d4` 的「跨身份重置」只修了 `useOntology`，恰好漏掉本文件 |
| **E** 高写崩溃 / 超时不可恢复 | ✅ 证实 | `agent_runtime/tool_executor.py:189-197` 线程池超时 `fut.cancel()` 杀不掉在跑线程（注释自认「B1 硬伤」）；`schema/022_agent_runtime.sql:98` `uk_tool_idem` 唯一键 + `tool_executor.py:109` 只复用 succeeded 行 → stale `executing` 行阻塞重试且无 fencing/对账；`executor.py:309-335` `_persist_suspend` 三段非原子 |
| **F** Agent 尚未形成用户可用链路 | ✅ 证实 | `agent_tools/__init__.py:24-28` 只注册 `KnowledgeSearchTool`；`policy.py:116-118` 基线只放 `kb.search/sql.readonly.*`；主聊天 `console-app/src/composables/useAsk.ts:412` 写死 `/api/ask/stream`，SSE 无任何 agent 帧处理。报告「不是错误的默认关闭，但不算可用产品」的表述公允 |

报告声称的 5 个「动态探针」结论（隐藏候选泄露、search count 泄露、跨部门 Agent write、过期审批放行、edited 参数替换），每一个都能从上述静态代码推出必然成立。

## 4. P1/P2 抽查：17 条中 15 实 / 1 部分 / 1 措辞

**全部证实（15）**：`tool_invocation.approval_request_id` 恒 NULL（`schema/022:91` 有列、`run_store.record_invocation` 签名无此参数）、checkpoint 明文 JSON + load 不验 `state_digest`（`loop.py:65-68` / `executor.py:165-166`）、一轮多 tool call 挂起丢调用（恢复后消息序列违反 OpenAI「每 tool_call 需 tool response」格式）、工具可见集全集暴露（`registry.py:89-96` ctx 参数未用）、obligations 零 enforcement 消费方 + output schema 不校验、self-approval 无生产启动断言（`config.py` 有现成守卫模式却没套用）、link 无 cardinality（`schema/029:21` 唯一键缺 `(src,type,active)`，同一 SKU 可挂两个 active `sku_of_product`）、store 边界可绕生命周期、provenance 可缺失/陈旧、backup steward 零消费点、coverage 分母被本批 `limit` 覆写、auto ack 只查 hash 长度 `>=8`、SQL splitter 引号内分号**沙箱实测真的切错**、owner_dept 白名单 fail-open。

**部分（1）** — P1「resume 改变原始执行上下文」：说「预算/deadline 重新给默认值」以偏概全。实际 turns/tool_calls/tokens 有 durable 恢复（`executor.py:186` `_budget_snapshot` 从 `agent_run` 累计计数播种），**只有 deadline 每次 resume 刷新 10 分钟窗**。同条的 channel 硬编码（`routes/agent.py:463` `channel="console"`）、conversation_id 可被 body 覆盖两个子点属实但当前影响有限（唯一入口本就写死 console）。

**措辞（1）** — P1「生产 apply 不可完全重放」：说「commit 记录使用 `scratch/apply_ontology_dbs_20260710.py`」——`git log` 全历史检索**未点名该脚本**（脚本确在磁盘、被 `.gitignore` 命中、不可从仓库重放，实质结论成立）。

**报告承认的 4 项改进——全部真实**（评分上调有据）：CI 零 skip 真库契约 job（`.github/workflows/ci.yml:143-151` + `test_ontology_db_isolation.py:50-53` 收集期硬 raise）、binary 身份键 + 全局唯一 ref（`schema/028:23` `COLLATE utf8mb4_bin`、`schema/027:36` `uk_ref` 全局唯一）、独立 Ontology DB（`config.py:180` `ontology_database` + `test_ontology_db_isolation.py` 真库隔离断言）、审批回链（`schema/028:35-37` + `025`，`ontology_identity_resolve.py:173-203` 落 `approval_request_id`/`confirmed_by`=真实审批人）。

## 5. 报告的失准与瑕疵（均不动摇主结论，供读者校准）

1. **一处以偏概全**：P1 resume 预算描述——见 §4「部分」。turns/tool_calls/tokens 实为 durable 恢复，只有 deadline 重置。
2. **半句无实据**：apply 脚本未被提交信息点名——见 §4「措辞」。
3. **「上次评分」基线不可追溯**：本仓 `docs/audits/` 两份文档（`full_audit_main_2026-07-10.md` 8 维口径、`ontology_p0_branch_review_2026-07-10.md`）都没有这套 16 维分数表。增量列（如 Ontology 实现 +1.8）无法独立复核；UX 维度中途改口径虽有披露，但趋势可比性已断。**分数只能当方向性参考，绝对结论以 P0 证据为准。**
4. **P0-D 触发面窄于叙述**：console 当前无应用内登出/换号流程（无 logout 函数，换 token 走整页 reload 会清模块态），「A 登出→B 登录 30 秒窗口」今天难以走到。作为审批面板的防御硬门要求合理，但定 **P0 略偏严——P1 更贴切**。
5. **轻微低估已有恢复件**：reaper 已含 B6 对账重驱（decided-but-not-resumed，`routes/agent.py:105-113`）与 stale-resuming→suspended 回边，报告未记这一笔；但其核心洞（副作用中途崩溃、幂等行无 fencing）不受影响。

## 6. 未在报告中、但值得记的交叉核验

- 2026-07-09 深度审查（[[agent-platform-v2-review-2026-07-09]]）标的**现网回归 session_store hmac 非 ASCII TypeError，在本分支已修**（`session_store.py:49-52` 先 `encode("utf-8")` 再 `compare_digest`）——报告未提不算遗漏。
- 07-09 抓的 **call_id 复用绕过在本分支也已修**（`executor.py:170-178` 一次性 `ApprovalGrant` 绑定 `(tool_name, args_digest)`）；本报告转而打审批语义的下一层（expiry / edited 重放），说明该审查系列在跟真实代码演进，非复读。
- **测试计数复跑（复现「无本地栈」环境）**：ruff All passed；全量 **2994 passed / 99 skipped**（报告 2991/99；+3 = worktree 在途未提交的新测试，`skipped` 精确一致）；ontology 专项等价文件集 **337/50 逐字**；agent 专项 **217/20 逐字**；vitest 236（报告 234；+2 = 晚于 9ea4017 的 console 提交+在途 spec）；build 通过；`npm audit` 0 高危。**所有运行零失败。**

## 7. 建议与 PR 顺序是否合理

- **GO/NO-GO 阶梯合理**：本地/simulate GO、staging 只读 conditional、高写 NO-GO 的分级与 flag 默认关闭现状匹配。
- **PR-1（审批不可变性）+ PR-2（统一授权）排最前是对的**——P0-C 与 P0-B 是本次核出最实、最险的两组，且相互独立可并行。唯一可商榷：PR-4（Agent UX canary）排在 PR-5（ontology 不变量）前，属口味。
- **Phase 0 四门「未通过」**与 memory 记录的组织 gate 四项未签一致；auto activation 保持硬关闭的要求正确。

## 8. 操作提醒（重要）

测试复跑期间发现该 worktree `/Users/laijunchen/Downloads/agent-v2-worktree` 正被**另一会话实时编辑**（未提交 diff 从 3 涨到 5 文件，是「审批历史补 agent 类」的在途工作：`routes/kb_access.py`、`tests/test_kb_endpoints.py`、`ApprovalHistory.vue`、`approval-history.spec.ts`、`useAuth.ts`）。**动手修 P0（尤其 PR-1/PR-2 会碰 `routes/agent.py`、`approval_store.py`、`executor.py`）前须先与那条线协调，避免撞同一批文件。**

---

**一句话**：这是一份罕见地经得起逐行对账的审查报告——方法克制、引用精确、改进与缺陷两头都属实。按 PR-1 / PR-2 先动手是安全的；唯一前置动作是先协调正在编辑该 worktree 的并发会话。
