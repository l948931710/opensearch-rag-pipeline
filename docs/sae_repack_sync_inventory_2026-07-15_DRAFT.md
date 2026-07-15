# SAE 重打包 main：ontology-p0 待同步功能盘点（除 agent 架构）

日期：2026-07-15 · 状态：DRAFT（待 Sam 拍板迁移名单）
目标：重打包 main 的 SAE + 去 http 化 + **上线 console 操作台**。
基线：main@69da57b · 分支 claude/ontology-p0@9975373 · 分支独有 137 提交（cherry 等价已滤）。

## 一、已在 main、无需再同步（核实过 ancestor）

- **去 http 化全套已落 main（07-14 轮）**：db416cb HTTPS 强跳+HSTS · 74c1790 生产关 /docs · 7b89154 小程序切 https 域名（0.0.6）· 375b6de 生产默认锁 rag.fulingplastics.com.cn + QA_FACT_JOIN 生产默认开 · e08fb91 钉钉验签 fail-closed + HTTP 面下线闸。
- 07-13 轮：HA3 三路融合默认开（去-sparse/w3_s10/默认开三连）· 来源 chip · 令牌撤销 · 307 token 挪 fragment · 双活 supersede（11197c0）。
- c6cec60 qwen3.7-plus 默认（qwen3.6 退役摘除完成）· efdc3ec ?token sessionStorage 续存 · 1b566ee 管理界面 tab 平移修复。
- **结论：本次部署 SAE 零新增 env 的裁决仍成立**（若下方候选全部默认 OFF 迁入亦不破坏）。

## 二、建议同步（非 agent、console 上线直接受益）

### A. 贡献/审核体验族（批次α-B + δ-3 + ε-1..ε-5）——console 上线主菜
提交（按序）：3e5ce7a(α-B) → a2a8c70(δ-3 审批归 dept_admin) → a304807(ε-1) → 096c1dd/4a5695d(ε-2) → 9ba14c1/79a74f9/73e067c(ε-3) → e701b13(ε-4 高频无人回答 Top30) → **5fa299d(漏斗只给管理员——随 ε-3 的权限收紧修复，必须同批)** → 5089a3e/be0a941(ε-5) → **92d70c4(reconcile cs=FAILED P0 根治——目标文件 main 均在)**。
- 后端 routes/contribution.py、kb_console.py、admin_notify.py 均**零 agent/ontology import**，已核实。
- 前端组件 main 已有基版（ContributionReviewQueue/GapList/HeroBoard/MyContributions/DeptTable 等），冲突点集中在 ContributeView/ManageView（分支版含 agent tab，解法=保 main tab 结构、只取非 agent 组件）。
- schema 依赖：037/038 已三环境 apply（ac59107），无新增。

### B. 缺口治理族
- 忽略缺口：ca56277（后端+**schema/041** qa_gap_dismissal）+ 0038e94 + 96ae65e（UI）。
- 语义去重：54cfd7c（**schema/039/040**，qa_logger 哈希落列+语义组归并）+ b364d02（scripts 纳管）。
- 残留：039/040/041 三迁移 staging/prod apply + backfill + flag——user-gated。

### C. 批次γ 运营指标 tab + δ-2
- e2ab134（/api/kb/ops-metrics）+ e38c599（前端三面板，新增 TrendChart.vue）+ d9b5333（δ-2 各部门 7/30 天窗）。
- ⚠️ D1「LLM 用量」查 schema/023 llm_call_log（agent 表，生产未建且 serving 热路径不写）——**已核实 fail-soft**（try/except + warning，面板显 0 不 500）。D2/D3 数据表（004/017）main 已有。迁入后 D1 空面板属预期，等 agent 平台上线自然点亮。

### D. 可选：通用能力分级开放族（总闸 RAG_GENERAL_ABILITY_MODE=off）
c57096b(核心层) → 0b09ffc(四链路接线) → 9e1b904(130 测试) → e7abcda(评测门) → 56cd474(docs) → 40e0526(console 徽标) → 39259b0(小程序徽标，需另发小程序版) → 0f1b7ea(集成缝三处)。
- 核心模块 intent_router/general_answerer/rate_limiter **零 agent import**，全 flag 默认 OFF；接线碰 api.py/dingtalk_bot（中等冲突）。
- 裁决点：这次包想灰度通用回答→迁；否则可随大合并。

### E. 配套（若执行 B 的 apply）：apply_migration 修复三件套
beb516f（prod DictCursor + --bootstrap-database）+ 9ea4017（注释内分号切分）+ d3e3d74（splitter 引号感知+可重放）。台账原判「可选未迁」，但要 apply 039/040/041 就建议先迁，避开已知坑。

## 三、维持不迁（台账既有裁决，本轮复核不变）

- agent 全族、ontology 全族（含本体工作台 52405f4/OntologyWorkbench）——随分支大合并。
- 审批中心 IA 族：4632992/94371ad/87f9bd2(α-A)/838d5e0——**α-A 已核实碰 AgentApprovalQueue/useAgentApprovals，agent 连体**。
- Agent 治理 tab（ab07c8e）、运行中心 β-2（a4d0701）、ModelGateway Tier A/B、Redis 多实例族（a3a2b41/c35c759/session_store/redis_client）、readiness 四探针（a9a29a5，依赖 agent/ontology 表）、供应链 lock（531ed31）+ trivy CI 修（5980093）、压测 harness、485 治愈运维脚本（dc3bcf5，运维时在分支跑，不进包）、docs/audit 类。

## 四、打包与部署注意

1. console 静态 = console-app `npm run build` → `opensearch_pipeline/webconsole/next-dist` 进 zip；**迁完 console 提交必须跑 vitest 而不只 pytest**（efdc3ec 教训：spec 与实现分家）。
2. 打包铁律：~/Downloads/dw_upload_<date>/opensearch_sae_rag.zip；`make release-gate` 绿 + Sam 点头双门。
3. HA3 485 全量重推与本次重打包是两件事：serving 侧融合修复已在 main 包内，部署后混合查询不再盲；索引缺行的彻底治愈（全量重推）另行排程（bc4b815 复检未治愈）。
4. schema apply 顺序建议：先迁 E（工具修复）→ apply 039/040/041 → 再部署带 B 族的包。

---

## 五、执行结果（2026-07-15，同日执行完毕）

**四族 + 工具族全部迁完，36 个新提交落 main（31 摘取 + 4 适配修复 + 1 台账；本地，未推 origin）。**

- 验证全绿：pytest **2623 passed**（迁移前 2408）· ruff 绿 · console vitest **31 文件/296 例** · vue-tsc 绿 · Playwright e2e **120 passed**（含批次γ 三条新用例）· miniapp 24/24 · `npm run build` 已重建 webconsole/next-dist。
- 摘取适配（与分支的有意差异，大合并时以分支版收敛）：
  1. 所有 P0-D 身份作用域调用（syncIdentityScope/identityFingerprint/_resetKbState/_resetContributeState）一律剥除——main 无运行期身份切换；
  2. ManageView 保 main tab 结构（dash/docs/history/ops/members），KB_ADMIN_TABS 名单门思想已采纳；
  3. ux-gate 只摘批次γ 三例；审批中心组随大合并；
  4. apply_migration `--db ontology` getattr 兜底 exit 2；测试剪 ontology 三块、032 活体样本内联；
  5. MANIFEST/schema README 只登记 039/040/041（022-038 随大合并）；
  6. api.py 补 `rate_limiter as _rate_limiter` 别名 import（patch 契约）；
  7. 红线表保 main 版式，补录 9 条 ε/07-15 拍板；
  8. d3e3d74 的 P1-15（retention_node 去模型 key）未迁（依赖分支 config 哨兵）。

## 六、剩余 user-gated（打包前）

1. **schema 039/040/041 staging/prod apply**（apply_migration 已在 main；040 归组回填脚本=scripts/build_qa_gap_semantic_groups.py）——需 Sam 授权令牌。
2. **push origin main** ——等 Sam 点头。
3. **make release-gate → 打 zip**——两道门（gate 绿 + Sam 明示部署）都在 Sam 手里；本次部署 SAE **零新增 env**（新 flag 全默认 OFF：RAG_GENERAL_ABILITY_MODE=off / RAG_QA_GAP_SEMANTIC=off）。
4. 运营指标 tab 的 LLM 用量面板在 agent 平台上线前显 0（fail-soft，预期）。
