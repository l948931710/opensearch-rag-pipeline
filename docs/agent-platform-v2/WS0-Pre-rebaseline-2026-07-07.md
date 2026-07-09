# WS0-Pre · HEAD Re-baseline 可执行清单（2026-07-07）

> 触发：实施前架构评审判 **有条件 NO-GO**（`docs/reviews/agent-platform-v2-architecture-review-2026-07-06.md`），§7 要求"拆 issue 前先交付把 gate 项补进报告与计划的修正批次 + 对 HEAD 做一次事实 re-baseline"。
> 本文只做 **主题 A（事实基线过时）+ 主题 B（运行时执行模型）** 的 re-baseline，**纯梳理、零生产写、零代码改动**。C/D/E/F 类 gate 项另行成篇。
> 校验方式：报告/计划锚点（`@7c704ce`）逐一比对 HEAD 现值（`@9fa3e07`），命令可复现。

## 0. 基线漂移事实（先锁定）

| 项 | 值 |
|---|---|
| 报告基线 | `7c704ce`（2026-07-03，报告扉页"证据重建"点） |
| 评审基线 | `646d709`（2026-07-05） |
| 当前 HEAD | `9fa3e07`（2026-07-07，本次 re-baseline 点） |
| HEAD 领先评审基线 | **19 commit** |
| HEAD 领先报告基线 | **135 commit** |

**关键结论**：`git diff --name-only 646d709..HEAD` 对本批锚点文件**零改动**——评审（07-06）的锚点结论至今仍准；但 `7c704ce..HEAD` 有 **6 个锚点文件已变**（api / config / dingtalk_bot / feedback_handler / rate_limiter / session_store）。**即：需要 re-anchor 的是"报告/计划的 7c704ce 锚点"，评审文档本身可直接采信。**

---

## ① 迁移编号 re-baseline + 取号纪律

### 冲突事实
报告 §4/§5/§6/§7 与 plan WS1-2/WS2/WS4 硬编码 `017`–`023`。写作时（`7c704ce` 最高号 016）正确，**现已全部撞号**——`schema/` 现存且 **016–021 已 apply 生产**（见 MEMORY「schema 016–021 已 apply 生产 2026-07-06」）：

```
017_qa_admission_reject.sql       018_gen_meta_runtime_contract.sql
019_chunk_meta_index_retry.sql    020_document_version_simhash.sql
021_ingest_quality_metrics.sql
```

`schema/README.md:15` 权威声明：**"编号严格单调递增，下一个可用号 = 022"**，并记录历史三对冲突（002/003/006）——重号会污染生产 `schema_migrations` 台账追溯（011 台账机制本身即因 010 列漂移事故而建）。

### 纪律（写进 plan 顶部与每处 DDL 文件头）
1. **报告/计划所有迁移号一律改占位符 `NNN_*`**（不再硬编码具体数字）。
2. **开工时按 `schema/README.md` 台账现值取号**（当前 022；WS1 启动前可能再有摄取侧迁移落地，故绝不预先硬编码）。
3. **报告 §4/§5/§6/§7 与 plan 六处编号一并改占位符**；连带修正报告与计划**互相矛盾的号→内容映射**（报告 §6 称 `019_agent_run_store`，plan WS1-2 称 `019_llm_call_log`——同号不同表，必须统一）。

### 指示性分配（取号时现取，**勿硬编码**，仅示范连续性）
| plan 原号 | 内容 | 指示性新号 |
|---|---|---|
| 017_agent_runtime | tool_registry + agent_run + agent_step + agent_checkpoint + tool_invocation | `NNN`（≈022） |
| 018_approval_engine | approval_request + approval_decision | `NNN+1`（≈023） |
| 019_llm_call_log | llm_call_log | `NNN+2`（≈024） |
| 020_agent_audit_log | agent_audit_log | `NNN+3`（≈025） |
| 021_semantic_views | sem_* 视图族（WS2） | `NNN+4`（≈026） |
| 023_u8_staging | U8 staging + 对账（WS4） | 取号时现取 |

---

## ② file:line 锚点漂移对照表（本仓锚点，`7c704ce → 9fa3e07`）

> 开源仓库锚点（Qwen-Agent `fncall_agent.py` / AgentScope `_agent.py` 等）pin 在各自外部 commit，非本仓、不在此表；见 `open-source-code-review.md`。

| # | 报告/计划锚点 @7c704ce | HEAD @9fa3e07 现值 | 漂移 | 动作 |
|---|---|---|---|---|
| 1 | `api.py:449-503` `/api/ready` | `@app.get("/api/ready")` **@481**；降级 503 逻辑 481–540 | +32 | plan WS0-1 改指 **481** |
| 2 | `api.py:579` 会话调用点 | `_get_or_create_session(...)` **@624**；import@72；alias@335；`_append_to_history` **@761/@1007**（两处） | +45 | plan WS0-2 改指 **624**，另标 761/1007 两处 append |
| 3 | `dingtalk_bot.py:132-156` `_is_duplicate_msg` | def **@137**；调用@882；`_seen_msg_lock`@133 | +5 | 微漂移→ **137** |
| 4 | `dingtalk_card.py:35-79` `_get_access_token` | def **@40**；`_cached_token`@36；`_token_lock`@35；`_token_expires_at`@46 | +5 | 微漂移→ **40** |
| 5 | `Dockerfile:42-48` `--workers` | CMD@42；`--workers 1` **@46**；注释@35–40 | 稳定 | ✅ 保留；⚠️ 注释@36 含**过时** AWAITING_COMMENT 理由（见 §③） |
| 6 | `config.py:244,664` Gemini 残留 | 默认名@244/@275；`GEMINI_API_KEY` 回退 **@688**；解析链 695/698/702/705/708/713/720；**生产禁 Gemini 守卫 @890–952** | 664→688 且**面扩大** | ⚠️ 见 §附 A：WS1-3"删 Gemini 残留"必须**保留守卫 890–952** |
| 7 | `feedback_handler.py:325` AWAITING_COMMENT | 注释"状态存 RDS（handled_status='AWAITING_COMMENT'），多 worker 安全" **@325 精确命中** | 命中 | 评审 A3 成立→ WS0-3 **删除此迁移项**（见 §③） |
| 8 | `retriever.py:1866` `retrieve_and_enrich` | `def retrieve_and_enrich(` **@2020** | **+154** | 大漂移→ plan WS1-3 改指 **2020** |
| 9 | `session_store.py:6,44` 会话存储 | `SessionOwnershipError`@27；`_LRUSessionStore`@51；`_verify_owner`@105；`get_or_create_session(…, owner=)` **@119**；`append_to_history(…, owner=)` **@170**；`clear_session(…, owner=)` **@153** | **签名已变** | 评审 A2 成立→ WS0-2"现签名不变"作废（见 §③） |
| 10 | `prod_access.py:79-115` 只读账号 | `get_prod_readonly_conn` **@79**；`get_prod_rw_conn`@86 | 稳定 | ✅ WS2-1 直接采信 |
| 11 | `run_judge.py:41` JUDGE_MODEL | `JUDGE_MODEL = os.environ.get("RAG_EVAL_JUDGE_MODEL", "claude-opus-4-8")` **@41 命中**（可 env 覆写） | 命中 | 评审 D3 成立→ L7 换境内 judge（见 §附 B） |
| 12 | `rate_limiter.py` 四层计数 | `Limits`@210；`global_daily_llm_cap: int = 2000` **@219** | — | WS0-3 迁 Redis；**E5 成本闸锚点 = 219** |

---

## ③ §8「进程内状态」现状核对（含 C8 完备性补漏）

### 报告 §8 原列 5 项复核
| 项 | 锚点 @HEAD | 判定 | 动作 |
|---|---|---|---|
| 会话 LRU | `session_store.py:51` `_LRUSessionStore`；`_sessions`@102 | 真，**但签名已带 `owner`** | 迁 Redis 须透传 owner（A2） |
| 限流四层 | `rate_limiter.py:210` `Limits`；进程内计数 | 真 | WS0-3 迁 Redis（fail-closed 语义保留） |
| msgId 去重 | `dingtalk_bot.py:133` `_seen_msg_lock` + `_is_duplicate_msg@137` | 真 | WS0-3 迁 Redis（注意 C4：先占位后处理会吞消息） |
| **AWAITING_COMMENT** | `feedback_handler.py:325` 明注 **RDS 多 worker 安全** | **假（已外置）** | **从 WS0-3 删除**——迁 Redis 是把 durable 事实降级为热态，负收益（A3） |
| token 缓存 | `dingtalk_card.py:36` `_cached_token` + `_token_expires_at@46` | 真 | WS0-3 加 Redis 共享刷新（SETNX 防惊群，进程内保留为 L1） |

### C8 完备性补漏（评审要求"再 grep 一遍 module-level 可变状态"→本次全包扫描新增）
| 新增项 | 锚点 @HEAD | 风险 |
|---|---|---|
| ACL 拒绝缓存 | `retriever.py:481` `_deny_cache` + `invalidate_deny_cache@493` | 跨模块**主动失效**，多实例下失灵→**威胁"授权撤销即时生效"** |
| live ACL 缓存 | `dingtalk_identity.py:445` `_live_acl_cache` | 同上，撤权延迟 |
| bot 部门缓存 | `dingtalk_identity.py:277` `_bot_dept_cache` | 部门解析陈旧 |
| 告警去重 | `alerting.py:28` `_LAST_SENT`（**自注 "process-local; fine for single-instance"**） | 多实例后同告警 **×N**（评审 E6） |
| SWR 刷新 | `api.py:1527` `_swr_refresh_lock` 守护的 stale-while-revalidate | 每实例各刷一次 |

> 结论：`RAG_ALLOWED_DEPTS_ACL` 依赖的 `_deny_cache`/`_live_acl_cache` 必须进 WS0 降级矩阵与失效广播设计（否则多实例后撤权不即时生效——安全回退）。

### Dockerfile 注释矛盾（连带修正）
`Dockerfile:36-37` 注释把 AWAITING_COMMENT 列为单 worker 理由，与 `feedback_handler.py:325`「RDS 多 worker 安全」**直接矛盾**。→ WS0-4 解除 `--workers 1` 时**修正该注释**；真实单 worker 阻塞项 = 会话 + 限流 + msgId 去重 + token 缓存 + 上表 5 项 C8 缓存，**不含 AWAITING_COMMENT**。

---

## ④ 运行时执行模型 · 冻结 loop.py / run_store.py 接口前的待决设计点

> 评审主题 B：报告把"接口边界"定为 P0 核心并称"接缝错了全盘返工"，却漏了决定接口形态的最底层输入。以下 3 条（B1/B2/B4）是 §7 P0 阻塞项，**必须先于 loop 接口冻结拍板**；B5/B8 连带、C1/B3 在 WS1 loop 实现前收口。

### B1 · 并发 / 执行宿主（根本性，先决）
- **现状**：报告 §3 `AgentLoop.run(...) -> Iterator[AgentEvent]` 是**同步迭代器**，宿主 = FastAPI 线程池 + 钉钉裸 daemon 线程；全文无并发模型。
- **失败场景**：P1 灰度一个部门，每 run 占死一线程 1–3 分钟→线程池占满→`/api/ask`、`/api/auth`、钉钉 webhook 全排队超时——**agent 灰度直接拖垮存量 RAG**。
- **待决（三选一 + 硬约束）**：run 主体走 (a) asyncio task / (b) 专用**有界**线程池 executor / (c) 独立 worker 进程+队列。硬约束：与 HTTP 请求生命周期解耦，SSE 只作事件消费端；给出 **per-instance 最大并发 run 数 + 拒绝策略**。
- **交付物**：报告 §3 增设"执行模型"节，作为 P0 交付，**先于 loop 接口冻结**。

### B2 · `ToolResultInjector` 全文无定义（Loop↔Runtime 协议另一半）
- **现状**：出现在 §3 核心签名，但无 Protocol 定义；同线程阻塞取结果会与消费 iterator 的 Runtime **死锁**；各实现者各猜一种回注方式→P0 结束接口即分叉。
- **待决**：补全 `ToolResultInjector` 完整 Protocol，**或**改用 `Generator.send(ToolResult)` 并把返回类型改为 `Generator[AgentEvent, ToolResult | None, None]`（与 B1 执行模型联动）。
- **连带**：定义 `RunCheckpoint` **字段级 schema（含序列化版本号）** + `ApprovalOutcome` 判别联合。

### B4 · 同 turn 多 tool_call 部分执行的挂起/恢复语义
- **现状**：报告 §3 仅"messages + 待执行调用序列化"一句。按字面存待执行调用→resume 时 messages 缺第 1 个工具结果→DashScope 400；保守重放整 turn→第 1 个工具重复执行（HIGH_WRITE 靠幂等键兜底=最后防线）。
- **待决**：`RunCheckpoint` 显式建模 turn 内每个 `call_id` 槽位：`executed(result)` / `pending_approval` / `not_adjudicated`；规定**末单裁决后才 resume**、**首个 REJECTED_TERMINATE 即止**；EDITED 时重写历史该 tool_call 的 args 使 messages 自洽。列入回放测试。

### B5 · 崩溃恢复主张与 checkpoint 写入时机矛盾（连带收口）
- §3⑧ 称进程崩溃后 `running` 态 run 可 resume，但 checkpoint 只在"挂起"时写→`running` 态无 checkpoint 可恢复。**待决**：`running` 崩溃后从最近 step 边界重放，还是标 `failed`（明确二选一）。

### B8 · `ExecutionContext frozen` 与 budget 扣减矛盾（连带收口）
- `@dataclass(frozen=True)` 装不下可变 `RunBudget` 扣减；且预算跨 suspend/resume 不持久→resume 重建 ctx 即预算清零。**待决**：预算移出 frozen ctx 落 `run_store`；定义 `auth_resolved_at` 重解析阈值默认值。

### 联动（WS1 loop 实现前收口，非本次 P0 阻塞）
- **C1** per-thread run 串行化：同用户同会话并发两消息→两 run 交错写 `sess:{thread}:msgs`。需 RDS `(thread_id, active)` 唯一约束或 Redis per-thread 锁。
- **B3** resume 不得跑在钉钉回调线程：回调只做 `decide` 落库 + `transition(suspended→resuming)` CAS + 发 resume 事件后立即 ACK，续跑交 B1 执行宿主。

---

## 附：本次 re-baseline 连带发现（超出 A/B，供 gate 批次消化）

**A. `config.py` Gemini 守卫禁删（WS1-3 scope 修正）** — plan WS1-3「删除 Gemini 残留（:244,664）」是**欠范围**：Gemini 已织入配置解析链（688/695/698/702/705/708/713/720），且 **890–952 是有意的生产禁 Gemini 安全护栏**（"falling back to Google Gemini is strictly forbidden in production"）。→ 只清**默认值残留**，**守卫必须保留**；作独立小 PR 时须显式圈定不动 890–952。

**B. `run_judge.py:41` D3 成立但可 env 覆写** — 默认 `claude-opus-4-8`（境外）违反硬约束 3；但 37–41 注释（P2-24）说明 `RAG_EVAL_JUDGE_MODEL` 可覆写。→ 修法 = L7 及含业务数据的评测层换**境内 judge 面板**（保留反自评）+ 改默认值，judge 不可用要有人工兜底而非直接阻断发布。

**C. `rate_limiter.py:219` = E5 成本闸落点** — `global_daily_llm_cap = 2000` 按**请求数**计；agent 使单请求成本 ×10–20，此帽不变即可让日账单放大 10 倍不触闸。→ Gateway 加**日级/部门级 RMB spend 闸（fail-closed）**，锚点即此处。

---

## 复现命令

```bash
# 基线漂移
git rev-list --count 646d709..HEAD          # → 19
git diff --name-only 646d709..HEAD -- opensearch_pipeline/{api,session_store,rate_limiter,dingtalk_bot,dingtalk_card,feedback_handler,config}.py   # → 空（评审锚点仍准）
# 取号
grep -n '下一个可用号' schema/README.md      # → :15 = 022
ls schema/0{17,18,19,20,21}_*.sql            # → 已占用
# A2 / A3 命中
grep -n 'owner\|SessionOwnershipError' opensearch_pipeline/session_store.py | head
grep -n '多 worker 安全' opensearch_pipeline/feedback_handler.py            # → :325
# C8 补漏
grep -rn '_deny_cache\|_live_acl_cache\|_LAST_SENT' opensearch_pipeline/*.py
```

---

## 完成判据（本清单交付即达成）

- [x] 迁移号冲突取证 + 占位符化纪律 + 022 取号规则（①）
- [x] 12 处本仓锚点漂移对照 + 逐条动作（②）
- [x] §8 五项复核（AWAITING_COMMENT 判负剔除）+ C8 五项补漏 + Dockerfile 注释矛盾（③）
- [x] B1/B2/B4 执行模型待决点成文 + B5/B8/C1/B3 收口清单（④）
- [ ] **下一步（需拍板，非本文范围）**：把①②③的改动落回 `implementation-plan.md` 与报告正文；④的三条设计决策由你拍板后补进报告 §3。
