# Staging UI 交互式探索测试报告（agent-v2-worktree）

- **日期**: 2026-07-11
- **被测对象**: worktree `/Users/laijunchen/Downloads/agent-v2-worktree`，分支 `claude/ontology-p0` @ `b2e4823`（含审批中心 IA 4632992/94371ad + 重评 P0/P1 修复批 + Agent canary 30e67df）
- **环境**: STAGING 真栈——RDS `fuling_knowledge_stg`/`fuling_operation_stg`/`fuling_ontology_stg`（rm-bp15j7…）· HA3 `ha-cn-kgl4slr1n01.public`（staging 表）· OSS staging（含 7-07 同步的 1867 图）· 真 DashScope（rerank 开启，qwen3-vl-rerank 实跑）
- **方法**: 以真实用户身份在浏览器逐界面交互探索（点击/输入/弹窗/滚动/主题/视口/身份切换），4+1 个 seed 身份（employee×2 / dept_admin / kb_admin / 匿名），关键写路径落库后 SQL 回验。共 5 次真实问答（2 普通 + 1 深度思考 + 1 无结果 + 1 agent）。
- **boot 配方**: worktree 内 `npm run build`（next-dist）→ `RAG_ENV=staging RAG_SESSION_SIGNING_KEY=<k> RAG_AGENT_ENABLE=true RAG_ONTOLOGY_ENABLE=true RAG_ALLOWED_DEPTS_ACL=true RAG_STREAM_REASONING=true PYTHONPATH=<worktree> stack-test/python -m uvicorn opensearch_pipeline.api:app --port 8000`；token 经 `auth_token.issue_session_token` 铸造、`?token=` 进入。seed/teardown 脚本：`scratch/staging_uix_seed_20260711.py`。

## TL;DR

**结论：整体健康度高。** 26 个功能面 25 个按设计工作；全程唯一真 500 = **`/api/kb/feedback-review` 的 SQL 列名错误（P1，main 同病）**，暴露于 staging 真库，单测 mock 掩盖了它。Agent canary 全链在 staging 真跑通且五张 trace 表落库全对。分支缺 main 最新 3 commits（含思考计时）属已知合并债非新缺陷。

---

## 1. 系统真实功能清单（探索所得）

**路由**: `/`（问答）· `/manage`（知识库管理，requiresManage）· `/contribute`（知识贡献）；未知路径重定向 `/`。

**问答页**: 问候语+身份名 · composer（Enter 发送/Shift+Enter 换行/停止键）· 深度思考 toggle（逐问生效）· Agent 模式 toggle（试点，与深度思考互斥）· Agent 运行中心按钮 · 热门问题 3 条 · 会话侧栏（新会话/搜索对话[内容级匹配]/会话列表/逐会话删除）· 悬停展开侧栏（知识贡献/知识库/主题切换/身份徽标）· 流式答案（检索态→有据等待→墙钟匀速吐字+光标→定稿）· 来源 chips（文件名+章节+引文）· 内联图（OSS 签名 URL + figcaption 摘要 clamp）· 操作条（复制/转人工/有用/没用[流式中置灰，NO_RESULT 不显]）· 低匹配度警示条 · 「回到最新」pill · 亮暗主题 · 375px 可用（chips 换行，已知 WONTFIX 方向）。

**Agent canary**: 能力探测（GET /api/agent/runs，404/403 → 零死入口）· 阶段条只映射真实事件带分段计时 · tool_result 回执帧（状态+耗时，无内容）· 运行完成卡（工具回执计数+运行详情入口）· 运行中心抽屉（我的 runs 列表[轮次/工具/tokens] + 详情[run_id/起止/tier/工具回执/步骤时间线/载荷已脱敏] + 5s 轮询提示）。

**知识库管理（角色分面）**:
- employee：只读概览（我的可检索范围[部门 chip]/热门问题/去问答/去知识贡献）
- dept_admin：概览看板（本部门资产 4 卡+状态分布+使用成效近30天+转人工工单，StatCard 骨架屏）· 文档管理（上传 UI[格式/50MB/OCR/.xls 提示+归属+可见范围]、台账[我的/本部门/全部门 scope chips+状态 chips+搜索+归属/范围/利用度三筛选]、行操作[预览原始上传文件(签名)/退役下线]）· 审批（待办/历史 chips 同址）
- kb_admin：+ 全库治理看板（资产 4 卡/部门分布图/类型分布/运行健康[入库成功率/一致性/嵌入失败率/延迟 p50/p95]/部门覆盖失衡表/治理风险[PII 脱敏/隔离/转人工]/服务可用性/知识效果/最常用知识）· 成员管理（dept_admin 授予/编辑/撤销，kb_admin 受保护）· 本体消解 tab（能力探测出现，RAG_ONTOLOGY_ENABLE）
- 审批中心：待办聚合空态（按角色措辞不同）+ 知识贡献待审指路行（去处理）+ 历史（类型 chips：访问授权/知识贡献/上传审批/成员授权/Agent 操作，含决策人/理由/时间）

**知识贡献**: 统计卡（待回答/本月贡献/已采纳/贡献者）· 待回答缺口列表（检索未命中/低置信度聚合，带部门归因+答案不够好徽标+N 天未回答）· 贡献知识弹窗（问题预填+归属分类预选+提交）· 已有贡献·待入库徽标 · 我的贡献（待审核/已驳回+理由回显）· 审核区（kb_admin/dept_admin：可见范围下拉+采纳/驳回[理由 0/500 弹窗]）· 侧栏贡献图标待办红点。

**本体消解工作台**: 空队列态（「检索/播种/回填遇到无法自动确认的编号时会挂到这里」）+ 治理脚注（确认=铸正式别名/理由必填/auto 通道抽检）。staging 本体库为空（组织 gate 未签、播种禁行——符合预期）。

## 2. 关键验证结果（全部 PASS 项）

| 链路 | 证据 |
|---|---|
| 流式问答 E2E | 请假流程：结构化分步 + 2 来源 chips + **内联流程图真实渲染**（staging OSS 图打通）+ 反馈 POST /api/feedback 200 + user_feedback upvote 落库 |
| 深度思考 | RAG_STREAM_REASONING 下 reasoning 流入 → 「思考过程 · 29.2s」折叠条 → 点击展开完整 CoT；思考独占阶段无空答案泄漏（本分支未含 6740338 修复但未观察到该 bug 触发） |
| NO_RESULT 降级 | WiFi 题（staging 缺该文档）：低匹配度警示条 + 「未找到相关信息」+ 最接近来源 + 转人工 + **不显示反馈拇指** |
| Agent 全链 | 阶段条 `已提交7.5s›回答中5.9s›调用工具22s›工具完成3.2s›回答中20s›完成`；DB 回验 agent_run=succeeded/turns=2/tool_calls=1/tokens=8336（与运行中心显示一致）、agent_step=model_call×2+tool_call×1、llm_call_log 2 行 **run_id 全非 NULL**、agent_audit_log 1 行、qa_session_log agent 合流 1 行；载荷脱敏只露 `{final, turn_index}` |
| 缺口挖掘实时性 | 我的 WiFi 无结果提问 **数分钟内**出现在 kb_admin 的待回答列表（1次询问·行政·答案不够好） |
| 贡献闭环 | 提交→统计卡即时 +1→缺口徽标翻转→审批中心指路行+侧栏红点→审核区驳回（理由弹窗）→我的贡献显「已驳回+理由」（重载后） |
| 审批中心 IA | 待办/历史同址 chips；历史类型筛选含 Agent 操作；旧深链 tab=agent/history 兼容逻辑在码内已核 |
| doc-preview 签名原件 | GET /api/kb/doc-preview?doc_id=… 200（window.open 防拦截模式） |
| 权限矩阵 | employee /manage=只读概览（范围「生产」实时 RDS 解析）；kb_admin 检索过滤日志 `public OR (dept_internal AND owner/allowed=hr)`（服务端 HA3 谓词）；匿名=品牌化免登失败页零泄漏 |
| 身份隔离（P0-D） | kb_admin→财务员工切换：**会话列表零残留**、问候语/范围即时切换（identityScope eager-clear 生效） |
| token 续存（94371ad） | 无 `?token` 直 navigate `/console/manage` 登录态保持（sessionStorage 续存 + URL 抹除） |
| 本体 tab fail-closed | 无 flag→404→tab 隐藏；开 flag→能力探测→chip 出现+深链高亮正确 |
| 基建细节 | 全局熔断计数跨重启回种（1→10）；封面降权 4 chunks；rerank 实跑（in=20 out=20）；AppShell 切 tab 不再重挂（治理接口无重复风暴，抓包确认） |

## 3. 缺陷与观察

### P1 — `/api/kb/feedback-review` 真库必 500（差评复核完全不可用）
- **现象**: kb_admin/dept_admin 看板加载时该接口连续 500；前端区块**静默整体隐藏**（无错误态、无「待你处理」chip）→ 差评在治理面完全不可见，管理员无从知晓差评存在或功能已坏。
- **根因**: [kb_console.py:735](opensearch_pipeline/routes/kb_console.py) SELECT `q.question`，而 `qa_session_log` 列名为 `query_text`（DDL 与 staging/prod 实表一致；同文件 642 行用法正确）。pymysql 1054。
- **溯源**: 引入于 `aadaf34`（差评复核功能首 commit，随 7-04 批合入 **main**，两分支同病）。单测 mock DB 掩盖；prod 未部署该批所以未爆。
- **已行动**: 已挂后台修复任务 chip（task_a5a384d6：改 `q.query_text AS question` + 补真库契约回归）。
- **连带建议**: 差评区块对 500 应显式降级（错误占位或告警），而非静默消失。

### P3 / 打磨项
1. **贡献处置后兄弟面板不刷新**: 驳回后「待回答」徽标仍显「已有贡献·待入库」、「我的贡献」仍显「待审核」，重载才一致——决策动作未失效兄弟查询缓存。
2. **我的贡献时间戳显示原始 ISO**（`2026-07-11T08:57:17` 带 T），与他处 `2026-07-08 05:29` 风格不一。
3. **Agent 答案无来源 chips**: 普通问答有来源引用，agent 路径知识经工具回执呈现但正文无 grounding 引用——员工视角两种答案可信度呈现不一致（产品取舍，建议显式拍板）。
4. **LLM 偶发内联「（来源：…）」**: 试用期答案正文内联来源标注，违反提示词「不在正文列来源」规则（4 答案中 1 次；深度思考 CoT 显示模型自检该规则通常有效）——非确定性遵循，量级待评。
5. **审批中心 URL 残留他 tab 参数**（`?tab=approvals&q=拉片机`）：无功能影响，深链分享略脏。
6. 375px composer chips 换行（「深度思/考」断词）——与既有 WONTFIX 拍板一致，仅记录。

### 环境/数据观察（非代码缺陷）
- **staging 语料缺口**: 热门问题「访客 WiFi 密码」在 staging 566 篇快照中无对应文档（prod 有）→ 正好验证了优雅降级；staging **全部 public、零 dept_internal** → ACL 差异化检索/跨部门授权闭环无法在 UI 层真跑。
- **staging bucket 无 CORS** → 浏览器直传上传不可真测（已知，6-30 曾临时加规则后拆除）。
- **7-08 会话遗留测试数据未清**: 身份 `STAGING_DEPTADMIN`/`STAGING_KBADMIN`（user_role+grant，成员管理可见）、1 条贡献驳回历史、2 条旧 agent_run（1 succeeded/1 failed）+5 条 null-run_id llm_call_log。建议专项清理或保留为演示数据的决定入档。
- **分支合并债**（预期内，PR-6 范畴）: 本分支缺 main `0cbb0f8`（现网 P0 加固）/`1e488c0`（深度思考实时计时——**本分支思考中无 Ns 计时**）/`6740338`（空态泄漏修复）。

### 自动化怪癖（供后续 UI 测试会话复用）
- 嵌入式浏览器合成滚动不触发原生 scroll 事件（「回到最新」pill 需显式 dispatch 才现身，产品逻辑无恙）；`computer scroll` 偶发 30s 超时。
- composer 的 Enter 合成按键不触发发送（真实键盘正常），需点发送钮。
- ContributionReviewQueue 与「去处理」按钮需受信点击（合成 .click() 无效/仅聚焦）——与 6-29 已知怪癖一致；聚焦后 Return 可激活。

## 4. 覆盖边界（未测到的面及原因）

| 未覆盖 | 原因 | 风险敞口 |
|---|---|---|
| Agent 审批挂起卡/四处置/撤回 | 默认注册表仅 knowledge_search（READ_ONLY），无写型工具可触发挂起 | UI 存在但仅单测覆盖；接真写工具时须联调 |
| 跨部门授权申请→审批→放行闭环 | staging 全 public + 分类器拦截共享文档权限翻转（合理） | 6-30 L4 曾全链 UI 验证过；本轮仅验空队列/历史渲染 |
| 上传真实 PUT / 升版 / 退役-恢复 / 成员授予-撤销 | CORS 缺失；对共享文档破坏性；L4 已有记录 | UI 渲染已验，写路径回归依赖 L4 基线 |
| 本体工作台 case 处置 | staging 本体库空（组织 gate 未签，播种禁行） | 仅空态+治理文案验证 |
| 工具治理 kill switch / uncertain 对账操作台 | 无前端（API-only，「仍开」清单内） | — |
| 钉钉/小程序端 | 本轮范围=console | — |

## 5. staging 遗留数据台账（本轮产生）

- **已清**: `STAGING_UIX_*` user_role ×4、dept_admin_grant ×1（脚本 `--teardown`）。
- **留存**（op_stg，正常使用痕迹，均可按 `user_id LIKE 'STAGING_UIX_%'` 定位）: qa_session_log ×5 · user_feedback ×2（upvote+downvote）· kb_contribution ×1（rejected，含决策理由）· agent_run `33558831e897…` + agent_step ×3 + tool_invocation ×1 + llm_call_log ×2 + agent_audit_log ×1。热门问题/看板聚合含这些痕迹（30 天窗口后自然滚出）。
- 服务器已停（:8000 释放）；worktree git 状态除本报告与 seed 脚本外无改动。

## 6. 建议下一步（按优先级）

1. 修 P1 列名 bug（chip 已挂）+ 给差评区块加显式错误态；为 kb_console 系 SQL 补真库契约测试（CI db-integration job 已有基座）。
2. 吸收 main 3 commits（PR-6 release evidence 流程内）。
3. 拍板 agent 答案的来源呈现策略；观察内联来源违规频次，必要时提示词加护栏。
4. 贡献决策后失效兄弟查询缓存；ISO 时间戳格式化。
5. staging 卫生：清 7-08 遗留身份/agent 行；建 1-2 篇 dept_internal fixture 文档（使 ACL 差异化与授权闭环可在 staging UI 层回归）；决定是否给 staging bucket 加固定 localhost CORS 规则。
