# C9 可见范围意图持久化 + R1 acl_revision CAS 域 拍板单（2026-08-03）

> **背景**：2026-08-03 ultra 评审 **C9** —— `kb_set_visibility` 改的可见范围会被 stage-2
> 按 raw_key 路径**覆盖回写**；连带 **R1** —— set-visibility 在 `acl_revision` CAS 并发域之外。
> 两者都动 set-visibility，合为一单。codex 第一批（4 轮）已给出 B′ 完整协议规格。
> **本单只出决策，代码零改动。**
>
> 🔴 **前提变更（2026-08-03 Sam 口头）：语料将清空重灌，历史数据不作数。**
> 本单已按「无存量」定稿 —— 所有"存量污染探测 / 回填口径"类条目**已删除**，
> 但**缺陷本身与数据无关**（是代码路径的覆盖顺序），清空后照样成立，仍须拍板。

---

## 1. C9 缺陷（已核验）

`kb_set_visibility`（`kb_console.py:3054`）守卫齐全（授权 / public 不对称 / `status='active'` /
quarantine），但**无 `content_process_status` 守卫**；而 raw_key 的权限段才是 stage-2 的权威
（`resolve_permission_level` 路径解析）。

**关键证据**：真正跨 DAG 构造 stage-2 canonical doc 的查询在
`dataworks_orchestrator.py:225`（认领）→ `:344`（构造 `canonical_doc`），
其构造结果**不含 `permission_level`** ⇒ `resolve_permission_level` 的优先级 1（显式值）
**恒不命中**，必然落到路径解析 ⇒ 扁平即 `public`。

**触发**：public 文档 v1 进 `PENDING_APPROVAL` → kb_admin 先 set-visibility 收窄为 `dept_internal`
（返回 `changed=true`）→ 队列放行 → stage-2 按扁平 public 路径覆盖回写
⇒ **管理员显式收窄的决定被静默还原，文档全员可检索，审计日志却留着一条成功记录**。

⚠️ 与 C7 同源（路径即权威 vs RDS 意图不同步），但 C7 已修（入口归一），C9 是**另一半**。

---

## 2. 🟢 三选一 —— **Sam 2026-08-03 已拍板：B′**

> 🟢 **Sam 2026-08-03 已拍板：方案 B′**（版本级 `permission_override` + 双锁 + fence）。
> 业务含义：可见范围变更**只对可安全接管的版本立即生效**，正在被 stage-2 认领/处理的
> 版本一律 409，不做 last-writer-wins。
> **尚未实施**——B′ 需要 schema 迁移（`document_version.permission_override`）+ 改真
> loader（`dataworks_orchestrator.py:225/344`）+ 并发协议，属跨模块协议变更；
> 且 §4 的**状态矩阵仍须填满**（逐格「允许 / 409 / 允许但不 override」）才能动工。


### 方案 B′（codex 推荐）：版本级 `permission_override` + 双锁 + fence

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | `document_version.permission_override` 存 **nullable canonical 值**（**版本级**，不是 document 级布尔位）；stage-2 loader **SELECT 该列并放进 `canonical_doc["permission_level"]`** ⇒ 命中 `resolve_permission_level` 优先级 1 | ☐ |
| 🔴 必须改**真** loader | 是 `dataworks_orchestrator.py:225/344`，**不是** `pipeline_nodes.py:182`（那是 stage-1 raw scanner）。改错地方=零效果 | ☐ |
| 🔴 并发协议 | set-visibility **同时锁 `document_meta` + 当前 `document_version`**；**仅 pre-claim 状态可写 override**；`LOADING`/`PROCESSING` ⇒ **409** | ☐ |
| 🔴 为何必须有互斥 | stage-2 是**认领→置 `LOADING`→commit（释放行锁）→之后才从内存 rows 构造 canonical_doc**（`dataworks_orchestrator.py:248` → `:344`）⇒ **只加列不加互斥仍是 last-writer-wins**：loader 快照取完后管理员再写 override，本轮照样消费旧快照 | |
| 若 Sam 要求"处理中也能改" | **必须上 generation fence**（代次变化即整轮 stage-2 放弃），**不能只 re-read 一次** | ☐ |
| 为何不是"让 RDS 全局赢" | stage-1 自动注册的 INSERT **根本不含 `permission_level` 列**（`pipeline_nodes.py:347-350`）⇒ 落 schema 默认 `public`（`001_opensearch_pipeline.sql:76`）；DataWorks 批量注册则**硬编码** `VALUES (..., 'public', 'public')`（`register_new_files.py:426`） ⇒ 全局让 RDS 赢会**确定性破坏** `raw/.../internal/` 与 `restricted` 文件。override 只由**显式管理员动作**写，自动注册不置 ⇒ 绕开该反例 | |

### 方案 A″（低改动后备）：非终态版本拒绝 + 紧急下线走专用语义

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 当前版本 `content_process_status` 非终态 ⇒ **409**；**紧急整篇下线不靠 set-visibility 兼职**，走 `→restricted` 专门语义或现有 `retire` | ☐ |
| ⚠️ v1 的一刀切 409 已被推翻 | 升版**注册时**就推进 `current_version_no`（`kb_console.py:2641`）、驳回**不回拨指针**（`:2848`）⇒ 一刀切会连带堵死"把**已在线**的 public 文档紧急下线" | |
| 若仍需在线判据 | 用 **`chunk_meta`** 版本：`EXISTS(SELECT 1 FROM chunk_meta WHERE doc_id=%s AND version_no<>%s AND is_active=1 AND index_status='INDEXED')`。<br>🔴 **绝不可用 `document_version.index_status='INDEXED'`** —— 该值**从未写入本列**（`reindex_states.py:65-70` 明写，且挂 2026-06-15 canary 事故），查之**永远空集**、守卫成静默 no-op | ☐ |
| ⚠️ 标注 | 这是 **RDS 在线代理，非 HA3 物理存在证明** | |

### 方案 C′（告知不拦）—— **不推荐**

放行 + 返回 note + 审计"该文档有待入库版本，入库后可见范围将按上传路径重置"。
零风险但**不修复缺陷**，只是把脚枪写进文案。codex 明确不建议作为推荐项。

---

## 3. 🔴 业务裁决（只有 Sam 能定）

> **可见范围变更应「立即作用于在线旧版」，还是「仅作用于待处理新版本」？**

这条决定 B′ 的 override 语义边界，工程不替你定。

⚠️ 相关事实：set-visibility 现在**只改当前版本**的 chunk 权限
（`kb_console.py` 的 `UPDATE chunk_meta ... WHERE doc_id=%s AND version_no=%s`）；
只有 `→restricted` 会停用**全部**活跃版本。所以"当前版本"与"在线旧版"本就是两件事。

| 裁决 | 状态 |
|---|---|
| 立即作用于在线旧版 | ☐ |
| 仅作用于待处理新版本 | ☐ |

---

## 4. 状态矩阵 —— 🟢 **Sam 2026-08-03 已勾（三行见 §4.3）**

> 说明：矩阵不该逐格手填 —— 大部分格子是**机制强制**的（没有可选项），真正需要拍板的
> 只有 §4.3 那三行。先把两类分开，免得把力气花在没有自由度的格子上。

### 4.0 三条机制事实（先核过代码，带行号）

| # | 事实 | 证据 |
|---|---|---|
| F1 | stage-1/2 的认领谓词**完全相同**：`content_process_status='NOT_STARTED' OR (='FAILED' AND retry_count<3)` | `dataworks_orchestrator.py:225-226` / `:761-762` |
| F2 | 认领 SELECT 是 **`FOR UPDATE OF dv SKIP LOCKED`**，与置 `LOADING` 在**同一事务**内 | `dataworks_orchestrator.py:234`、`:248` |
| F3 | **两个写方今天锁的是不相交的行**：`kb_set_visibility` 只锁 `document_meta`（`kb_console.py:3299`），stage-2 只锁 `document_version` ⇒ **彼此零互斥**。这就是 C9 覆盖的机械根因 | 同上 |

🔴 **F2 收窄了 B′ 原本的 409 面**：拍板单 §2 担心「只加列不加互斥仍是 last-writer-wins」。
但只要 set-visibility **也对当前 `document_version` 行取 `FOR UPDATE`**，`SKIP LOCKED` 就让
两者干净互斥：
- set-visibility 先拿到锁 ⇒ stage-2 本轮 **SKIP** 掉这篇（不认领），下轮带着 override 处理；
- stage-2 先拿到锁 ⇒ set-visibility **阻塞到它 commit**，醒来重读即见 `LOADING` ⇒ 409。
⇒ **`NOT_STARTED` 与 `FAILED&retry<3` 是安全可写的**，不必像 B′ 初稿那样一并 409。

### 4.1 机制强制格（无自由度，不需要拍板）

| `content_process_status` | 是否会被认领 | 写 override | 依据 |
|---|---|---|---|
| `PENDING_APPROVAL` | 否（不在谓词内） | ✅ 允许 | F1 |
| `NOT_STARTED` | 是 | ✅ 允许（dv 锁互斥） | F1+F2 |
| `FAILED` & `retry_count<3` | 是 | ✅ 允许（同上） | F1+F2 |
| `FAILED` & `retry_count>=3` | 否（已出谓词） | ✅ 允许 | F1 |
| `LOADING` / `PROCESSING` | **已认领、在途** | ⛔ **409** | 该轮已持快照，拦不住 |
| `REJECTED` / `SKIPPED_DUPLICATE` | 否 | ✅ 允许 | 终态 |
| `DONE` | 否 | ✅ 允许 | 终态；见 §4.3 |

> ⚠️ `LOADING`/`PROCESSING` 的 409 **不是永久拒绝**：>2h 无进展会被 stale-lock 接管重置成
> `FAILED`+`retry_count++`（`dataworks_orchestrator.py:711-719`）⇒ 回到可写。409 文案应
> 明确「稍后重试」，不要让管理员以为这篇永远改不了。

### 4.2 `document_version.index_status`（维度四）

| 取值 | 处置 | 理由 |
|---|---|---|
| `PENDING_DELETE` | ✅ 允许写 override，但**不撤销**该握手 | 退役/收紧握手在途；stage-3 收尾 CAS 已保住它（`pipeline_nodes.py:7477`） |
| `PROCESSING` | ⛔ **409** | stage-3 正在推送，推的行带旧 permission；与 `LOADING` 同理 |
| `SUCCESS` / 其它 | ✅ 允许 | —— |

> 🔴 提醒：成功值是 **`SUCCESS`**，**不是 `INDEXED`**（`reindex_states.py:65-70`，2026-06-15
> canary 事故）。任何守卫写 `index_status='INDEXED'` 都是**永远空集**的静默 no-op。

### 4.3 🔴 真正待 Sam 拍板的三行（业务裁决）

维度一（新文档 / 有在线旧版本的升版）× 维度三（权限变更方向）：

| # | 场景 | 选项 | 我的建议 | 状态 |
|---|---|---|---|---|
| a | **升版中**（有在线旧版本），目标 = `restricted`（收紧/紧急下线） | (i) 立即作用于在线旧版本 　(ii) 只对新版本生效 | **(i) 立即** —— 这是紧急下线的**唯一**语义；若只对新版本生效，管理员点完「受限」而文档仍在被检索，是最坏的安全错觉 | ✅ **Sam 2026-08-03 选 (i)** |
| b | **升版中**，`public → dept_internal`（收紧一档） | 同上 | **(i) 立即** —— 同为收紧方向，安全侧一致 | ✅ **Sam 2026-08-03 选 (i)** |
| c | **升版中**，`dept_internal → public`（**放宽**到全公司） | 同上 | **(ii) 只对新版本** —— 放宽是不可逆的暴露面扩大；在线旧版本的内容尚未按「全公司」口径复核过 | ✅ **Sam 2026-08-03 选 (ii)**（接受与 a/b 的不对称，需 UI 文案说明） |

> **新文档**（无在线旧版本）三种方向都无歧义：override 落到待处理版本即可，不涉及"在线的谁"。
>
> 若 Sam 对 c 选 (i)（放宽也立即），则三行统一为「可见范围是**文档级意图**，恒立即生效」——
> 实现更简单、心智更一致，但要接受"放宽即刻扩大暴露面"。**这是产品决定，不是工程决定。**

## 5. R1 · `acl_revision` CAS 域

**事实**：doc-meta 编辑端点**要求** `expected_acl_revision`（`kb_console.py:3289/3401/3430`，
缺省即 400、比较后递增）；**`kb_set_visibility` 完全在该 CAS 域之外**。

**与 C9 的关系**：**正交**。CAS 解的是 **client↔client 陈旧意图**（两个管理员并发改）；
C9 是 **admin↔管线覆盖**。codex 确认这个切分正确 —— 但**不能因为正交就让它无主消失**。

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | set-visibility 是否纳入 `acl_revision` CAS 域（要求 `expected_acl_revision`，变更后递增） | ☐ |
| 支持 | 与 doc-meta 端点口径一致；061 已把 `acl_revision` 注释定义为「文档管理面编辑 CAS」，可见范围替换本就在其语义内 | |
| 反对 | 前端要多读一次 revision；且现网单管理员场景并发极少 | |
| ⚠️ 若采纳 | 必须同时确认**是否与 schema/062 的 `acl_epoch` 混淆** —— 两者**语义正交、绝不可互相复用**（`acl_revision`=管理面编辑 CAS，含 title/category；`acl_epoch`=投影失效代次）。但**set-visibility 改级别时两者都要动**：CAS 校验 + `acl_epoch` bump（因 permission 是 `allowed_depts` 的 gate 输入） | ☐ |

---

## 6. 与其他单子的交叉（避免重复拍板）

| 事项 | 归属 |
|---|---|
| `permission_level` 的 **RDS↔chunk 同步权威** | **本单**（C9）。`schema/062` 明确**不碰**，其 epoch 不认证 permission |
| `permission_level` 变化**必须 bump `acl_epoch`** | **062 单**已定（它是 `allowed_depts` 的 gate 输入）。本单采纳的方案不得与之冲突 |
| 多版本 materializer | **C3′ 单**（`c3prime_...signoff`），与本单正交 |

---

## 7. 🔴 清空重灌带来的次序问题（本单最紧的一条）

语料清空后会**重新全量摄取**。若 C9 未修就重灌：

- 重灌期间任何"上传→审批前收窄可见范围"的操作**照样会被 stage-2 覆盖**；
- 且这次是**全量**，命中面比平时大得多。

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | C9 的选定方案是否**必须在重灌开始前落地** | ☐ |
| 我的建议 | **是**。B′ 的改动面（一个 nullable 列 + loader SELECT + 双锁）小于"重灌后再逐篇纠正可见范围" | |

---

## 附：本单未核实项

- 生产事务隔离级别（连接池只显式 `autocommit=False`，仓库无显式 isolation 设置）—— B′ 的 stamp/锁协议**不应依赖默认隔离级别猜测**
- 重灌期间是否会有并发的 set-visibility 操作（决定 §7 的紧迫度）
