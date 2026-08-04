# C3′ ACL 投影收敛机制 拍板单（2026-08-03）

> **背景**：2026-08-03 ultra 评审确认 **C3**（`_prescreen_unchanged` 与唯一写实现
> `materialize_doc_allowed_depts` 的 diff 口径漂移）。修复过程中 codex 四轮评审又暴露出一条
> **更根本、且不在评审 11 条内**的缺陷 —— 记为 **C3′：ACL 是文档级权威，实现却是版本限定**。
>
> 同日的设计模式对标研究（`docs/acl_retention_design_patterns_2026-08-03.md`，**在 remote 分支 `claude/code-review-ultra-console-74ccdf`（`c92fed6`），未合 main**；5 组研究 ×
> 对抗核验，153 条承重声明，136 confirmed / 2 unverified / 18 refuted —— ⚠️ 原文这几个数**不自洽**（136+2+18=156≠153），此处如实转述，引用前以原文为准）为 C3 给出了**五个独立系统收敛的同一结论**，
> 其 P1/P2 两条比已落地的修法更根本。本单把两者合并成一次拍板。
>
> **已落码但未提交**（工作区，`make test` 4015 绿 / lint 绿 / +23 测试逐条反证）：C3 的
> 四步安全增量（node 不进预筛、legacy 预筛加 owner 维、owner 集合语义、current-version
> stale-owner 候选源）。**本单的所有选项都是在此基础上的增量，不推翻已落码部分。**

---

## 0-pre. 🔴 现网实测（2026-08-03，prod_ro 只读）—— 记忆已过时，以此为准

**生产语料已清空完毕**：`document_meta` 1938 行全部非 active（307 inactive / 1562 retired /
69 superseded）；`chunk_meta` **63,882 行全部 `is_active=0`**；**node 文档 0 / 生效 node 授权 0 /
approved 跨部门授权 0**；`RAG_ALLOWED_DEPTS_ACL` env 未设 ⇒ 默认 **False（关）**。

⇒ **epoch 没有任何存量要收敛，本单 §④「回填」整节对当前语料是空转**；062 的价值全在前方：
让**重灌进来的新语料从出生就带正确 epoch**。这也是姿态选 A 的决定性依据（见 ②）。

---

## 0. 一条必须先纠正的判断（我核验后与研究文档不同）

研究文档 §1.5 把「`chunk_meta.allowed_depts IS NULL` 的检索语义」列为决定 C3 优先级的前置问题，
并倾向：*若是 fail-closed，则 C3 只是可用性问题，优先级可下调一档*。

**核验结论：前半句对，后半句不成立。**

- `_build_permission_filter`（[retriever.py:489-525](../../opensearch_pipeline/retriever.py)）是**正向匹配**：
  `allowed_depts="<组码>"` 无值即不召回。所以 NULL **确实 fail-closed**，不是 Elastic/Kendra
  那种「无 ACL 即公开」的 fail-open。
- **但泄露不来自 NULL，来自没翻的 owner 轴。** 未投影的 node 文档，其
  `chunk_meta.owner_dept` 还挂着**旧真实 owner**，于是过滤器的
  `(permission_level="dept_internal" AND owner_dept="<旧owner>")` 分支**照常命中** ——
  正确投影后这里本该是 `NODE_OWNER_SENTINEL`（[acl_policy.py:217](../../opensearch_pipeline/acl_policy.py)），
  而任何真实用户的 owners 集都不含该哨兵值。

⇒ **C3/C3′ 是可用性与机密性两个后果同时成立**：新授权部门搜不到 **且** 旧 owner 组持续可读。
研究文档只盯 NULL 一维，恰好漏掉真正漏的那一维。**优先级不下调。**

---

## 1. C3′ 本体：版本轴（这是新缺陷，评审 11 条里没有）

**证据**：`materialize_doc_allowed_depts` 只解析并只更新 `current_version_no`
（[access_grants.py:382-395](../../opensearch_pipeline/access_grants.py) 的 SELECT、
`WHERE ... AND version_no=%s` 的 UPDATE）。而：

- 升版**注册时**就把 `document_meta.current_version_no` 推进（[kb_console.py:2641](../../opensearch_pipeline/routes/kb_console.py)）；
- 驳回**只改 `document_version`，不回拨指针**（[kb_console.py:2848](../../opensearch_pipeline/routes/kb_console.py)）；
- 旧版本 chunk 要等新版本成功收尾才停用（DAG-3 的 deactivate 不变量）。

⇒ `{current_version_no=2, v2 REJECTED/无 chunk, v1 仍 active 且 owner 非哨兵}` 是**可永久存在的真实态**，
而 materializer 永远够不到 v1。**已落码的 stale-owner 候选源刻意用 INNER JOIN 限定 current 版**
（避免零行假漂移挤占写预算），所以**这条盲区仍然开着**。

---

## 2. 待拍板六项（v2：codex 两轮 REVISE 后重构；v1 的 P2 `acl_state` 已删除）

> **v1→v2 变更摘要**（避免你按旧稿拍板）：❌ 删 P2 `acl_state`（三态皆可由 epoch 推导，且"忘记 bump"时 state 同样不变 ⇒ 不构成独立防线）；❌ 删索引（待生产 EXPLAIN 后另取号）；✏️ epoch 范围**收窄**为 `(owner_dept, allowed_depts)`，permission_level 移出（归 C9）；✏️ 原 ③"排在 ①② 之后"改为 **③ 必须与「全版本 epoch sweep / 消费启用」同批**（不要求与纯 DDL apply 同批）；✏️ 回填从"全量重投影"改为 **certify/changed 二分**。

### ① 采纳 `acl_epoch` 投影失效代次（schema 062 v2）

| 项 | 内容 | 状态 |
|---|---|---|
| ✅ 已 apply | `document_meta.acl_epoch` + `chunk_meta.acl_epoch` —— **2026-08-03 已 apply staging+生产**（`PROD-RW:2026-08-03`，台账 checksum d98971f6） | ✅ |
| 三态判定（**含不变量破坏态**） | `cm.acl_epoch = dm.acl_epoch` ⇒ clean；`IS NULL` 或 `<` ⇒ dirty；**`>` ⇒ 不变量破坏**（正常路径不可能，但错误 writer / 局部恢复 / 人工 SQL 可造成）⇒ **阻断告警，不得判 unchanged、不得 certify**。无需改 DDL，但实施协议必须覆盖 | ☐ |
| 证据 | Kendra `PutPrincipalMapping.OrderingId` 单调 last-writer-wins + `ListGroupsOlderThanOrderingId`（官方内置的陈旧权限检测）。⚠️ Kendra 2026-06-30 维护模式、07-30 起不接新客户 —— **设计可参考，不可选型** | |
| 建议 | **采纳。** 唯一「谓词天然覆盖『从未投影过』且不会退化」的方案；现落码的 stale-owner 候选源只覆盖 owner 一轴、且只覆盖 current 版 | |
| **范围：认证输出 vs 失效输入（务必分清）** | **认证输出**严格 = `project_doc_acl` 产出的 `(owner_dept, allowed_depts)`；`permission_level` **不在认证范围**（epoch 相等不证明 permission 已同步，其同步权威仍归 C9 —— 让 certify 按 dm 修 chunk 等于抢先替 C9 裁决"RDS 赢"）。<br>🔴 **但「不认证」≠「不 bump」**：materializer 先按**每版本** permission 做 gate 再产 `allowed_depts`，且 set-visibility 的重投影**受 flag 门控**（`kb_console.py:3160`）⇒ flag 关时 public→dept_internal 只改 chunk permission 不投影；**若此时不 bump，开 flag 后 epoch 相等、sweep 跳过、已批准的跨部门授权永不投影**。⇒ **有效每版本 permission 变化必须 bump**（只是让二元投影重算，不是让 RDS permission 赢） | ☐ |
| 偏离原研究 | 研究文档写"两张授权表各加 epoch"，DDL 改为 **document_meta 单一水位**：①避免谓词按 acl_mode 分叉（那正是 C3 根因，不该在修它的迁移里重造）②chunk 侧逐版本**表达能力**。⚠️ v1 曾用"两表 MAX 必然 N+1"作论据，**codex 校准为不严格成立**（可两条批量 GROUP BY），已从理由中删除 | ☐ |
| 已验证 | 本地 MySQL 8.0.46 一次性 scratch 库：v2 连跑两次 **exit 0**（幂等守卫生效），两列类型/默认值正确；scratch 已 DROP，`fuling_knowledge` 零触碰 | |

### ② bump 完备性：单一入口 + CI writer 守卫 + 语义 fingerprint 兜底

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 采用 **(b)+(c)**：`record_acl_projection_invalidation(cursor, doc_id, reason)` 单一入口（同事务 epoch+1 + ACL outbox enqueue）+ 扩 `tests/test_acl_projection_writers.py` 的 allowlist 守卫 + **低频全量语义 fingerprint 审计**。**trigger 另立后续条目，不进本批** | ☐ |
| 为何必须有 fingerprint（不是可选） | 投影结果还受**代码/配置语义**影响：`resolve_allowed_depts` 读运行时组码白名单、`project_doc_acl` 的哨兵/规范化规则也可能改 —— 这类变化**不触发任何 DB writer**，epoch 不动而谓词照判 clean。**trigger 同样抓不到**。且静态 allowlist 只能强制"新增写点被审阅"，测试自己就承认 `join/format` 动态构造会漏报 | |
| fingerprint 的硬要求 | 必须**完全绕过 epoch 候选集**做全量；发现"epoch 相等但 fingerprint 不等"须至少 bump+enqueue 或**阻断告警**，**不得只记日志后继续宣称收敛**；投影规则变更须配套全局 invalidate（或引入 policy version） | ☐ |
| 为何不上 trigger | 仓库**零 trigger 先例**；且可能破坏既有 meta-first 锁序纪律（spot_checker 明确要求）。生产 RDS 的 TRIGGER 权限/迁移工具兼容/锁序**均未实测** | |
| ✅ 硬约束（Sam 2026-08-03 已确认） | bump **不得受 `RAG_ALLOWED_DEPTS_ACL` 门控** —— flag 关闭期的权威变化若不 bump，水位永久丢失，开 flag 后这批文档永远判 unchanged | ☐ |
| stamp 点（codex 穷举确认） | 只有三处写 RDS 投影：materializer 的 node UPDATE、materializer 的 legacy UPDATE、**stage-2/re-chunk 共用的 `node_write_chunk_meta` INSERT**。⚠️ 漏第三处 ⇒ **每篇新摄取文档的 chunk 天生 `acl_epoch=NULL`、永久 dirty，sweep 对新文档永不收敛**。stage-3 reload 只改内存后推 HA3、spot_checker 只停用 chunk、rebuild 脚本只改 index_status —— **均不是 stamp 点** | |
| ✅ **`projection_complete` 不变量（Sam 2026-08-03 已确认）** | **`chunk_meta.acl_epoch=E` 当且仅当 `(owner_dept, allowed_depts)` 已由 strict authority snapshot 完整成功计算。任何门控跳过、capability/mode/grant 读取失败或 fallback 都不得 stamp。** 适用于 materializer / certify / stage-2 **三类** stamp | ☐ |
| 为何这条是必须的（已核验） | 当前 stage-2 **三处都不满足**：①legacy `allowed_depts` 受默认关闭的 `RAG_ALLOWED_DEPTS_ACL` 门控，注释明写"flag 关 → 不查、写 NULL"（`pipeline_nodes.py:6251`）；②即使 flag 开，解析失败也只 `print` 后继续写空投影（`pipeline_nodes.py:6304`）；③node 模式识别仍走 fail-loose 的 `resolve_acl_modes`（`pipeline_nodes.py:6265`）。⇒ 若无条件 stamp，新 chunk 会被"认证为最新"而其 `allowed_depts` **根本没算过**；日后开 flag 时 epoch 相等、sweep 不修 —— **正好重造"从未投影却被判 clean"** | |
| ✅ **姿态：Sam 2026-08-03 拍板选 A** | **A（已选）**：RDS 的 owner/allowed 投影**始终计算**，`RAG_ALLOWED_DEPTS_ACL` 只控制 **HA3 字段推送与检索消费**，解析失败则不 stamp 或中止该文档。<br>**依据**：重灌在即而 flag 为关 —— 选 B 会让新语料每个 chunk 都带 `acl_epoch=NULL`，开 flag 时必须对整个新语料全量回填。<br>**安全性已核实**：`to_ha3_doc(include_allowed_depts=False)` 默认不推 `allowed_depts`，推送由调用点单独门控 ⇒ **A 不改变进 HA3 的载荷**，serving 行为逐字节不变。 | ✅ |
| 待实现方案选择（不阻塞拍板） | strict authority 读取失败时是"整篇中止"还是"写 chunk 但 epoch 留 NULL" —— 留到实现评审；Sam 现在只需拍**"失败不得 stamp"这个不变量** | |

### ③ C3′ 多版本 materializer —— **必须与「全版本 epoch sweep / 消费启用」同批**（不要求与纯 DDL apply 同批）

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | materializer 从「只认 `current_version_no`」改为处理该文档**所有 `is_active=1` 服务版本**，每版本各自保留 `PROCESSING` 互斥、按版本算 gate。⚠️ 精确边界：**062 的纯 DDL apply 可独立先做（第 0 步）**；本项只与**全版本 sweep / 消费启用**绑定 | ☐ |
| **v1 错误已纠正** | v1 写"排在 ①② 之后，epoch 可能顺带覆盖版本轴" —— **不成立**。schema 只给逐版本**表达能力**，不改 current-only 的 writer（只解析 current、只更新该版本）。若 sweep 先于 C3′ 启用：旧 active v1 **每轮进 targets、每轮写不到、每轮占预算**，且 outbox 对非 `skipped_locked` 仍标 done ⇒ 投影意图被永久终结 | |
| 折中（若必须先观测） | sweep **明确限定 current version**，且**不得宣称已覆盖 C3′** | ☐ |
| ⚠️ 终态语义 | 只有「整篇文档完全没有 active chunk」才可终态 `skipped`；「current 无 chunk、旧版仍 active」绝不能终态跳过 | |

### ④ 回填：certify-only / projection-changed 二分（**不是**全量重推）

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | **certify-only**：重跑权威解析，owner/allowed_depts 与现存投影一致 ⇒ **只 stamp epoch，不动 index_status**；**projection-changed**：投影值确实变了 ⇒ 才写 ACL 字段 + 置 `NOT_INDEXED`。`verified_docs`/`changed_docs`/`reset_chunks` **三预算分开**，以 stage-3 backlog 为暂停门，稳定 keyset 分页 | ☐ |
| **v1 错误已纠正** | v1 称"存量全部 dirty，`_LIMIT` 会自然摊平" —— **不成立**：`_LIMIT` 限的是**文档数**（`materialized+retracted`），不是 chunk 数/`NOT_INDEXED` 数/stage-3 推送能力。200 篇大文档可一次产生数千 chunk 重推 | |
| certify 的 fail-closed 要求 | **不能直接复用现有 fail-loose 解析**：`resolve_acl_modes` 在 capability 探测/查询失败时**静默按 legacy 返回**，一次瞬时读失败即可把 node 文档"认证"成 legacy。certify 须用 strict resolver + 同一致快照（meta-first 锁序），读失败/未知 mode/混合值/epoch 竞争**一律不 stamp** | ☐ |
| 残留前提（无法消除） | certify 仍依赖"权威解析器本身正确"。缓解：生产只保留**一个** canonical projector，另用独立 golden oracle、模式互斥不变量、零 grant node 哨兵等测试验证 —— **不要再写第二套完整生产解析器** | |
| 首轮盘点须输出 | active distinct docs / active versions / active chunks / 每文档 chunk 分布 / 当前 `NOT_INDEXED` backlog。**规模一律现查，不按记忆数字排期** | |

### ⑤ `never_projected` 进 reconcile 返回值 + 告警

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 返回值加 `never_projected`（现有键：`approved/materialized/retracted/unchanged/reset_chunks/capped/skipped/errors`）；接 `ops_monitor` 告警 + `eval_harness/layers/l5_permission.py` 硬断言 | ☐ |
| 证据 | Coveo `numberOfEntitiesByState`：结果按状态分桶。**当前最该报警的那个数恰好没被报出来** | |
| 建议 | **采纳，成本最低、与 ① 同批。** 没有这个数，①②③④ 做完也无法证明收敛 | |

### ⑥ P4：节点授权变更单接全量 permission-sweep

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 「节点授权变更」不指望文档级 outbox 覆盖，单接一条**作用域=全库**的 permission-sweep | ☐ |
| 证据 | Azure SharePoint：**父作用域权限变更不被增量路径捕获，是官方承认的设计边界**。组织树节点授权 = 父作用域变更，增量路径必然漏 | |
| 建议 | **原则采纳，排最后。** ①⑤ 落地后先看 `never_projected` 真实量级再定 sweep 频率 | |

---

## 3. 不在本单、但相邻的两条

- **纯 ACL 变更触发全量重嵌入**（研究 §4.1）：`materialize` 标脏 `NOT_INDEXED` 后，stage-3 drain 会重解析 authority → 重嵌 dense+sparse → `cmd=add`。一次纯权限变更付一次完整嵌入成本。
  ⚠️ **本条尚未核验**：修复前提是 HA3 支持部分字段更新（且部分更新不会重置/丢弃向量列）。**需先实测再立项**，不要写进本单的验收。
- **C9**（set-visibility 被 stage-2 按路径覆盖）：与 C3′ 正交，走它自己的决策单（版本级 `permission_override` + 双锁 + generation fence）。

---

## 4. 建议落地顺序

> ⚠️ **v1 的顺序（①②④ 先上 → 观测一周 → 再决定 ③）已被 codex 推翻,不要照它排期**：sweep 先于 C3′ 启用会让旧 active 版本每轮进 targets、每轮写不到、每轮占预算，且 outbox 标 done 永久终结投影意图。

1. **第 0 步（可独立做，零风险）**：apply schema **062**（纯 additive，旧代码不读不写，行为逐字节不变）。**sweep flag 保持关闭。**
2. **第 1 步**：部署**写方** —— bump 单一入口（②）+ **三处 stamp 点全部落地**（含 `node_write_chunk_meta`，漏它则新文档永不收敛）。此时 epoch 开始积累，但没有任何消费方。
3. **第 2 步（必须同批）**：**C3′ 多版本 materializer（③）+ 全版本 dirty sweep + certify/changed 回填（④）+ `never_projected` 计数（⑤）一起上**。先跑 `commit=False` 只读预览拿真实规模，再定窗口。
4. **第 3 步**：生产规模 `EXPLAIN` 定索引（另取号）；观测 `never_projected` 量级后决定 ⑥（sweep 频率）。
5. **并行（限定）**：语义 fingerprint 审计（②的 c 部分）——**检测**可与上述并行，但**自动 bump+enqueue 修复依赖第 0/1 步已部署**；在此之前只能产出**阻断告警**。它是 epoch 覆盖不到的那类失效的唯一防线。

**回滚**：第 0 步纯加列，无回滚需求。第 1 步（bump/stamp）最坏是多标 dirty，**不会少标** ⇒ 无机密性回滚风险。第 2 步动 ACL 唯一写实现，**需独立回滚预案**，且 sweep 挂 `RAG_ACL_EPOCH_SWEEP` 默认关、可秒级关停。

---

## 5. 🔴 第 2 步（C3′ 多版本 materializer + 全版本 sweep）——**2026-08-03 立项拆分,未实施**

第 1 步（写方）已落码（commit `72c9e22`）：bump 单一入口 + 7 个权威写点 + 三处 stamp +
`projection_complete` + 姿态 A ⇒ **epoch 已在正常积累**。

第 2 步经 codex 实现评审后确认**不是"再走一步"，而是三块独立工程**，Sam 2026-08-03 拍板
**在此打住、分别立项**。三条 blocker 逐条记录（均已核实到代码行）：

### ⛔ B1 · 两个调用面的事务语义不一致（返回契约要重构）

- outbox drain 对 `skipped_locked` **仍 commit**，只是不标 done（`access_grants.py:606/630`）
- reconcile **只在 `materialized/retracted` 时 commit**（`allowed_depts_reconcile.py:226/230`）

⇒ "部分版本写成功 + 聚合报 `skipped_locked`" 会让 reconcile **不提交已经发生的写** ——
要么丢，要么被后续文档的 commit 意外带上。
**修法**：返回值加显式控制字段 `complete` / `wrote_projection` / `locked_versions` / `versions[]`。
**不能靠单一 status 表达两个正交维度。**

### ⛔ B2 · certify 未闭环 —— **其中一条是第 1 步已落码的潜伏缺口**

- ✅ **已修（2026-08-03，同会话）**：`materialize` 值相等时不再直接 `return unchanged`，而是先走
  **certify-only**（只写 `acl_epoch`、**不动 `index_status`**、不重推 HA3），两个分支都有；
  `reconcile` 增加 `certified` 提交分支（漏掉它 epoch 会丢或被下一篇 commit 意外带上）；
  outbox drain 的 else 支已覆盖 `certified`（投影意图确实已落实，刻意归此支）。
- ✅ **已修**：新增 `projection_rows_all_match()` **逐行**校验替代并集口径作为**认证判据**。
  分工写死：`current_allowed_for_doc`（并集）判**要不要重投影**（少计只朝重投影自愈，安全）；
  新 helper（逐行全等）判**能不能盖章**（宁可不盖，绝不误认证）。坏 JSON/任一行不符 ⇒ 拒绝。
- reconcile 候选仍是旧来源，**没有 `acl_epoch IS NULL/<` 这一路**（`allowed_depts_reconcile.py:203`）
- legacy 预筛仍 current-only（`:74`）⇒「current 干净、旧 active 版 dirty」会被提前判 unchanged
- 缺 `chunk.acl_epoch > dm.acl_epoch` 的**不变量破坏阻断告警**

### ⛔ B3 · stage-3 的「先读 ACL、后抢锁」TOCTOU —— **改管线，不是改 materializer**

stage-3 先把 chunk 读进内存（`FROM chunk_meta cm`）并在 `:603-650` **重解析** allowed_depts
（那段刻意不信 chunk_meta 投影、直读权威授权表），而抢 `PROCESSING` 是 `dag.run(ctx)`（`:663`）
里的首节点 `node_acquire_index_lock` ⇒ **即使 materializer 锁了并提交，已读到旧 ACL 的
stage-3 仍会把旧值推回 HA3**。按 `version_no` 排序**解决不了**。
（行号为 2026-08-03 e6ca2f4 之后的当前值——该文件当日因 C9/B′ 加过 capability 探测，
原稿的 `:472/:627` 已漂移，勿按旧号定位。）
**修法**：stage-3 改 **claim-before-read**，或抢锁后重读重算 ACL 再进 embedding/push。
另：materializer 不应继续硬编码 2h（`access_grants.py:387`），应复用 `ingest_lease.takeover_where_sql()`。

### 已达成共识、实施时直接采用的设计点

| 项 | 结论 |
|---|---|
| 选版 | `DISTINCT version_no FROM chunk_meta WHERE doc_id=? AND is_active=1`，**按 version_no 升序**；零 active chunk 才终态 `skipped` |
| 预算 | `reset_chunks` 求和后 `_LIMIT` 仍只限**文档数** ⇒ 须加独立 `reset_chunks` 预算 + stage-3 `NOT_INDEXED` backlog 暂停门；certify-only 另计 |
| 锁序 | 统一 `document_meta → document_version(version_no 升序) → chunk_meta`，并同步修订 `spot_checker.py:23` 的既有声明 |
| **两 flag 分离** | `RAG_ACL_MULTIVERSION`（唯一写实现）与 `RAG_ACL_EPOCH_SWEEP`（全扫候选）分开；运行时不变量 **`SWEEP=true 且 MULTIVERSION=false` 必须拒绝启动**。启用序 multiversion→sweep，回滚序相反 |
| outbox 队头饥饿 | `ORDER BY enqueued_at LIMIT 200`（`:581`），locked 只累加 attempts 不出队 ⇒ 200 条持续锁定即饿死新撤权意图。**不得自动标 done**；需退避/到期调度或配额分离 + 按 attempts/年龄告警 |
| `apply=False` | preview 整轮不逐文档 rollback ⇒ **不得无条件加 `FOR UPDATE`**（会持锁到连接关闭）。走非锁定严格快照或每文档显式 rollback |

### ⚠️ 实施前必须补的证据

- 「stage-3 已预读旧 ACL → 权威变更 → stage-3 抢锁」的**确定性并发测试**
- 多版本**逐行混合投影**测试（`["finance"]`/`NULL` 并存但并集恰好等于 want 的反例）
- staging 合成大文档实测：多版本单文档的事务耗时、锁等待、`NOT_INDEXED` 增量
  （生产当前零 active 数据，给不出可靠预算）

---

## 附：本单引用的研究文档自带的三条限定

1. **Amazon Kendra** 2026-06-30 起维护模式、07-30 起不接新客户 —— 设计可参考，**不可选型**。
2. **Azure AI Search** 的文档级 ACL/RBAC 全部是 `2026-05-01-preview`，**非 GA** —— 设计可抄，不可假设 SLA。
3. **Google Vertex AI Search** 已更名 Agent Search（API 层仍 `discoveryengine`）—— 检索官方文档需用新名。

> **行号基线**：本单全部 `file:line` 引用基于 **2026-08-03 工作区**（main `de9ff6e` + C7/C1/C2/C6/C10/C3 六项未提交改动）。C7 的归一块在 `kb_console.py` 约 2399 行插入 12 行，**该文件 2399 行之后的行号相对 main 有偏移**。
