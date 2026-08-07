# C3′ ACL 投影版本轴 · 实施方案（2026-08-04，_DRAFT）

> **状态（2026-08-07 更新）**：✅ **评审完成 + 已实施**。codex 三轮（误报 0 / 12 条主张全部成立），
> Claude 误驳 3 条已记账；Sam 拍板 **政策1**（ACL 刷新可顺带退役旧 active 版本）与 **§8.1 方案 A**
> （逐版本 gate）。相对本稿的全部变更见 §10「实施记录」——**§10 与本稿正文冲突时以 §10 为准**。
> 原状态：待 codex 评审。代码一行未动。
> 上游：`docs/ops/c3prime_acl_projection_convergence_signoff_2026-08-03.md`（拍板单）。
> 本单只覆盖拍板单 §3「多版本 materializer」那一格，**不含** certify 的 strict resolver、
> 全量 fingerprint 审计、`never_projected` 指标、全库 permission-sweep（各自另立）。

---

## 0. 当前行为：**实测**，不是读码推断

复现脚本已落 `scratch/c3prime_version_axis_{repro,outbox,retract}_20260804.py`
（本地 docker MySQL 的独立 scratch 库 `c3p_knowledge`，用**真函数**跑，未 mock）。

场景：一篇文档有两个 `chunk_meta.is_active=1` 的服务版本，v2 = current 且投影正确，
v1 = 旧的仍在服务版本。

### 实测一 · 可用性方向（授权批了，旧版拿不到）

```
初始   v1: allowed_depts=None       acl_epoch=None      ← 从未投影
       v2: allowed_depts=["rd"]     acl_epoch=7
A. _prescreen_unchanged            → 空集（✅ 正确拒绝跳过，交 materialize）
B. materialize_doc_allowed_depts   → {'status':'unchanged','version_no':2,'wrote':True}
C. reconcile ×3                    → 每轮 unchanged=1 / materialized=0 / retracted=0
结果   v1 三轮后仍是 None、acl_epoch 仍是 None      🔴 永不收敛
```

### 实测二 · 机密性方向（授权撤了，旧版仍开放）

```
初始   授权已 revoked；v2 已收回(NULL)，v1 仍挂 ["finance"]
reconcile ×3 → 每轮 unchanged=1，retracted=0
结果   v1 仍是 ["finance"]      🔴 授权撤销后，旧服务版本对 finance 永久开放
```

### 实测三 · 持久意图被终结（最坏的一条）

```
record_acl_projection_invalidation → document_meta.acl_epoch 7→8，outbox 入队
drain_acl_projection_outbox        → {'processed':1,'done':1,'locked':0,'failed':0}
outbox 行 done_at=2026-08-04 14:15:02
v1 仍是 allowed_depts=None
⇒ 🔴 outbox 认为「投影意图已落实」，v1 却从没投影过。
   在下一次授权变更把它重新入队之前，这是**永久态**。
```

### 实测四 · 只修 materializer 是**无效的**（口径差实测，`…gate_conflict_20260804.py`）

混级场景：`v1.permission_level='dept_internal'`（应得 `["rd"]`）、
`v2(current).permission_level='public'`（应得空）；权威批了 rd。

```
权威 raw_want                      = ['rd']
逐版本 gate(v1=dept_internal) 应得 = ['rd']
_prescreen_unchanged               = {'C3P_DOC_1'}      ← 判 unchanged
```

`_prescreen_unchanged` 的 gate 用的是**跨版本 permission 集合**
（`allowed_depts_reconcile.py:131`：`perm_map[d] == {"dept_internal"}`），混级时集合是
`{"dept_internal","public"}` ⇒ 整篇 `want=[]` ⇒ 两版都是 NULL ⇒ **判 unchanged 直接跳过**。

⇒ 🔴 **若只把 materializer 改成逐版本，这类文档根本进不到 materializer** —— 修了不生效。
预筛必须同批改成逐版本 gate。（这条我最初写进"尚未确定"，实测后升级为 **blocker**。）

### 由此确认的四件事（都不是推断）

1. **`unchanged` 是假绿**。最后一道全扫防线报「一致」，而一个在服版本根本没投影。
   `reconcile` 的统计因此**不能**作为收敛证据。
2. **每轮都在空转且取锁**。`wrote=True`（certify 的 `UPDATE … SET acl_epoch` 对
   current 版发出了语句，`rowcount=0`）⇒ 这类文档每轮都取一次 X 锁 + commit，
   却永远不会收敛。预筛（B2-② 已是全版本口径）**每轮都正确地拒绝跳过**它，
   于是每轮都付 4×SQL 的逐 doc 复核。
3. **outbox 的持久保证在这条路径上失效**：`materialize` 返回 `unchanged`，
   drain 归入「意图已落实」标 done。
4. **预筛的跨版本 gate 会拦住修复**（实测四）⇒ 本批范围必须含预筛，否则等于没修。

---

## 0.5 生产实测（2026-08-07 00:37 PDT，prod-ro 只读）——补 §6.1 那一验

§6.1 原文「本会话在 SIM，未查；需要 Sam 给只读一验」。已查，**答案改变了 §6 的前提**：

| 查项 | 实测 |
|---|---|
| `chunk_meta.acl_epoch` / `document_meta.acl_epoch`（062） | ✅ **两列都在 = prod 已 apply** |
| `document_meta.acl_mode`（060） | ✅ 在 |
| `chunk_meta` active / 总 | **6 / 63,888** |
| C3′ population：有 ≥2 个 `is_active=1` 版本的文档 | **0** |
| 有 `is_active=1` 且 `version_no <> current_version_no` 的文档 | **0** |
| §8.1 混级 population（多版本 且 跨版本 permission 混级） | **0** |
| legacy 权威 `kb_access_request status='approved'` | **0** 篇 |

**两条结论，方向相反：**

1. 🟢 **今天影响面为 0** —— 不是缺陷不成立（§0 的夹具复现照样成立），而是
   **语料真空期**（2026-08-03 软退役 1562 篇 + HA3 清除）把 population 清空了。
   ⇒ C3′ 现在**不产生任何线上损害**，也不 page。「E1 最紧急因为它每天 page」这个
   说法（我 08-07 早先的判断）**不成立**，据此更正。
2. 🔴 **§6.1 的窗口已经错过** —— 062 **已 apply**，所以 `epoch_dirty` 候选源的闸
   **已经通电**，只是库里现在没东西喂它。原文「若尚未 apply —— 那么在 C3′ 落地前
   不要 apply」已无从执行。
   ⚠️ 本稿原来的补救是 §6.2 的 G4（把候选源收进同一 flag）——**G4 已撤销**，见 §10.2：
   它零收益（同类文档也命中 `have_ad` 候选）且会让退役→恢复链路静默失去自愈。

⇒ **落地时机的真正理由换了**（结论不变、更强）：不是「止血」，而是
**必须早于语料重灌**。下一幕正是「部门组织树重传 + 重灌 + 金集重标」，population
一回来，空转取锁 + `unchanged` 假绿 + outbox 误标 done 三件事同时成立。
而**现在是唯一能以零爆炸半径翻 flag 验证的窗口**（active chunk 只有 6 条）。

⚠️ 附带影响 §8.1：混级 population = 0 ⇒ 那条语义裁决**当前无实例**，
不构成本批的阻塞项（但 A 方案一旦落地，重灌后会对新产生的混级文档生效 ⇒ 仍需 Sam 点头，
只是可以与实施并行、不必前置）。

---

## 1. 根因（一句话 + 精确坐标）

`materialize_doc_allowed_depts` 把「文档级权威」写成了「**单版本**投影」：
`ver` 恒等于 `document_meta.current_version_no`（`access_grants.py:428-446`），
其后**每一条**读与写都带 `AND version_no=%s`：

| 坐标 | 作用 |
|---|---|
| `access_grants.py:428-438` | 只取 current version；PROCESSING 反抢锁也只查 current 那一行 |
| `:480 / :536` `current_allowed_for_doc(…, ver)` | diff 的 have 只看 current |
| `:482 / :537` `_current_owner_set_for_doc(…, ver)` | owner 轴同上 |
| `:489 / :544` `projection_rows_all_match(…, ver, …)` | certify 判据同上 |
| `:490 / :545`、`:506 / :558` `UPDATE … WHERE version_no=%s` | 两个分支的四条写语句全部版本限定 |
| `:516-519` `GROUP_CONCAT(permission_level) … version_no=%s` | gate 输入同上 |

**权威侧本来就是文档级**：legacy 的 `kb_access_request`、node 的 `kb_doc_node_grant`
都不带 version。**唯一的每版本输入是 `permission_level` 的 gate**。
所以版本轴不是「权威要按版本拆」，而是「同一份权威要投到每个在服版本上」。

### 双活版本不是异常，是**设计内的安全失败态**

`node_deactivate_old_chunks`（`pipeline_nodes.py:7023-7049`）的 docstring 写明：
04b parity 任一未愈合即 raise ⇒ 05 不执行 ⇒ **旧版本保留**（「宁可双版本并存，
也绝不让新旧都不可检索」）；另有 stage-3 loader `LIMIT 1000` 切批导致
「同版本有残留未 INDEXED chunk ⇒ 推迟停用」。

⇒ C3′ 的影响面 = **每一篇曾经历 partial push / parity 失败且此后未重灌的文档**，
与 CLAUDE.md 已登记的开放缺口「partial-batch failures … dual versions served」同一population。
**不是罕见边角。**

---

## 2. 目标 / 非目标

**目标**
- G1 `materialize_doc_allowed_depts` 对该文档**全部 `is_active=1` 版本**投影，每版本各自
  取 PROCESSING 反抢锁、各自按本版本 `permission_level` 做 gate。
- G2 outbox 的 done 判据修正：**任一版本被锁 ⇒ 不标 done**（意图保留）。
- G3 `reconcile` 的统计不再把「有版本没做」计成 `unchanged`。
- G4 `epoch_dirty` 候选源与本改动**同闸**，消除「apply 062 早于 C3′」的顺序陷阱（见 §6）。
- G5 🔴 `_prescreen_unchanged` 的 gate 改为**逐版本**，与 materializer 同口径 ——
  否则混级文档被预筛判 unchanged、根本进不到 materializer（实测四）。**没有 G5 就没有 G1。**

**非目标（明确不在本批）**
- certify 的 strict resolver（拍板单 §111：`resolve_acl_modes` fail-loose 会把 node 认证成 legacy）
- 全量语义 fingerprint 审计、`never_projected` 指标、全库 permission-sweep
- `verified_docs / changed_docs / reset_chunks` 三预算拆分（本批沿用单预算，风险见 §7）
- C9（permission 的 RDS↔raw_key 权威之争）—— 正交，走它自己的单

---

## 3. 修改范围（文件 · 接口 · 数据流）

### 3.1 `access_grants.py::materialize_doc_allowed_depts` —— 拆成「壳 + 单版本核」

把现函数体几乎原样下沉为 `_materialize_one_version(cursor, doc_id, ver, *, apply, auth_epoch, mode_certain)`,
外壳负责枚举版本 + 聚合。**单版本核内部的判定逻辑逐字不变**（这是刻意的：
本批只动版本轴，不借机改 gate/diff/certify 语义，否则回归面无法归因）。

版本集：

```sql
SELECT DISTINCT version_no FROM chunk_meta
 WHERE doc_id=%s AND is_active=1 ORDER BY version_no
```

用 `chunk_meta.is_active` 而非 `document_version.status`：投影的**写入对象**就是
chunk_meta，且 `is_active=1` 正是 stage-3 deactivate 的翻转位、与 HA3 在服集对齐
（modulo 已知的 RDS↔HA3 无 2PC 缺口）。

PROCESSING 反抢锁**逐版本**判：

```sql
SELECT dv.version_no FROM document_version dv
 WHERE dv.doc_id=%s AND dv.version_no=%s
   AND (dv.index_status IS NULL OR dv.index_status!='PROCESSING'
        OR dv.updated_at < NOW() - INTERVAL 2 HOUR)
```

查不到 ⇒ 该版本 `skipped_locked`，**只跳这一版**，其余照做。

⚠️ 一处**有意的行为变化**：现实现在 current 版**没有 document_version 行**时
（`LEFT JOIN` + `dv.index_status IS NULL` 命中）仍然继续投影。新的逐版本查询若写成
`INNER JOIN` 会把这类版本判成 locked ⇒ 必须保留 `LEFT JOIN` 语义（写成对
`document_version` 的 LEFT JOIN 或"查不到行则视为不锁"），否则是**静默回退**。

### 3.2 返回契约（向后兼容 + 新增一维）

```python
{
  "status": <聚合态>,            # 既有键，语义见下
  "reset_chunks": int,           # 各版本之和
  "version_no": int|None,        # 既有键：保持 = current_version_no（老调用方/测试不破）
  "wrote": bool,                 # 各版本 OR
  "per_version": [               # 🆕 {version_no, status, reset_chunks, wrote}
      {"version_no": 1, "status": "materialized", "reset_chunks": 3, "wrote": True}, …
  ],
}
```

聚合优先级（**保守优先**）：

| 条件 | 聚合 status | 理由 |
|---|---|---|
| 任一版本 `skipped_locked` | `skipped_locked` | outbox 据此**不标 done**（修 G2）。宁可重试。 |
| 否则任一 `materialized`/`retracted` | 该值（两者都有 ⇒ `materialized`） | reconcile 计数从 `per_version` 取真实分布 |
| 否则任一 `certified` | `certified` | |
| 否则 | `unchanged` | 只有**全部**版本都对，才准报 unchanged（修 G3 的假绿） |

无 `is_active=1` 版本（文档全下线）⇒ `{"status": "skipped", "per_version": []}`，
与现实现「无 chunk 可投」的 `skipped` 同义。

### 3.3 `allowed_depts_reconcile.py::reconcile_allowed_depts`

- 计数改为遍历 `outcome["per_version"]`（缺该键则退回按 `status` 计 —— 兼容老桩）
- `_LIMIT` 写预算：仍按**文档**计（一篇多版本算一次），保持"漂移文档不被一致文档挤出"
  的既有性质；`reset_chunks` 反映真实行数
- `epoch_dirty` 候选源（`:224-240`）移到与本改动同一 flag 之下（G4）
- **新增统计** `partially_locked`：本轮有多少文档因某版本被锁而未整篇完成 —— 这是
  「看起来收敛了但其实没有」的唯一可观测信号，接 ops_monitor

### 3.6 🔴 `allowed_depts_reconcile.py::_prescreen_unchanged` —— gate 改逐版本（G5）

现状（`:105-107`、`:131`）：`perm_map[d]` 是**跨版本**的 permission 集合，
`want = raw_want if perm_map[d] == {"dept_internal"} else []`。混级 ⇒ 整篇 want=[]。

改为：`perm_map[(d, ver)]`，逐 (doc, version) 各自 gate 出 `want_v`，再拿该版本的
`have` 行与 `want_v` 比。owner 轴已是集合比较，逐版本化后同理按 (doc, ver) 分组。

⚠️ **这不只是重构，它改变了"应有值"本身**：混级文档的旧 dept_internal 版本，
从「应有 []」变成「应有 approved groups」。语义裁决见 §8.1。

### 3.4 `drain_acl_projection_outbox`

无需改代码：`status == "skipped_locked"` 分支已有，聚合态修正后自动获得正确行为。
**但要加测试钉死**：任一版本被锁 ⇒ `done_at` 保持 NULL、`attempts+1`。

### 3.5 flag

`RAG_ACL_VERSION_AXIS`（`config.rag.acl_version_axis`），**默认 off**。
off ⇒ 版本集恒 `[current_version_no]` 且 `epoch_dirty` 恒空 ⇒ **逐字节等价于今天**。

> 🔶 **这一项我请评审方特别裁决**：默认 off 符合本仓惯例，但本条修的是**已证实的
> 机密性缺陷**，off 意味着缺陷继续在线。备选：默认 on + 保留关闭开关。
> 我倾向默认 off + 与 062 apply 同批开启（见 §6），理由是可回滚性；但这是取舍不是定论。

---

## 4. 边界情况（逐条给处置）

| # | 情况 | 处置 |
|---|---|---|
| E1 | 某版本无 `document_version` 行 | 视为**不锁**（保留现 LEFT JOIN 语义）。写成 INNER JOIN = 静默回退 |
| E2 | 某版本长期 PROCESSING（>2h 才可抢） | 2h 内该版本每轮 `skipped_locked`；文档聚合亦 locked ⇒ outbox 保持未 done。**不会**因此把已能做的版本饿死（其余版本照做） |
| E3 | 版本很多（重灌历史长） | 版本集来自 `is_active=1`，正常 ≤2；异常态可能更多。**加上限**（建议 `_MAX_VERSIONS_PER_DOC=20`）：超限只处理最新 N 个并计 `capped_versions` **告警**，绝不静默丢 |
| E4 | node 模式 | `project_doc_acl` 的 node 输入（node_ids/exact_node_ids）是文档级 ⇒ 每版本 want 相同，只有 owner 哨兵与 gate 按版本判。逻辑不分叉 |
| E5 | 某版本 `permission_level` 混级（同版本内多值） | 现有 `GROUP_CONCAT` gate 已处理（非单一 `dept_internal` ⇒ want=[]），逐版本各自判，不合并 |
| E6 | 062 未 apply | `_stamp` 恒空（现逻辑），各版本照常投影、不盖章 ⇒ 下轮仍 dirty。与今天同型 |
| E7 | 部分版本写成功、后续版本抛异常 | **单事务**内做全部版本；异常由 reconcile 的逐文档 `rollback` 整篇回滚。⚠️ 不做「逐版本提交」——那会造出「投影做了一半」的中间态，而 `_prescreen` 的逐行严格比较会把它判 dirty、下轮重做，安全但更慢；整篇回滚语义更简单。**请评审方确认此取舍** |
| E8 | `wrote` 语义 | 任一版本发出过写语句即 True。调用方 commit/rollback 逻辑不变（B1 的结论继续成立） |

---

## 5. 测试与验证

### 5.1 新增真库测试（`tests/test_acl_version_axis_db.py`，进 conftest 串行组）

把 §0 的三个实测固化成测试，**每条都要能被反证**：

| 用例 | 断言 | 反证（改坏什么会红） |
|---|---|---|
| T1 可用性 | 双活版本、v1 未投影 ⇒ materialize 后 v1.allowed_depts=["rd"]、acl_epoch 盖章 | 把版本集退回 `[current]` ⇒ 红 |
| T2 机密性 | 授权撤销 ⇒ v1 的 `["finance"]` 被清 NULL | 同上 |
| T3 outbox | v1 被 PROCESSING 锁 ⇒ drain 后 `done_at IS NULL` 且 `attempts=1` | 聚合态写成"取 current 的 status" ⇒ 红 |
| T4 不报假绿 | 有版本没做完时 `status != "unchanged"` | 聚合优先级里漏了 skipped_locked ⇒ 红 |
| T5 全对才 unchanged | 两版都正确 ⇒ `unchanged`，且**不重复写** | 聚合写成"任一 unchanged 即 unchanged" ⇒ 红 |
| T6 flag off 等价 | flag off 时对同一夹具的行为与改动前**逐字节相同** | 版本集没受 flag 门控 ⇒ 红 |
| T7 E1 无 dv 行 | 该版本仍被投影（不判 locked） | 写成 INNER JOIN ⇒ 红 |
| T8 E3 版本上限 | 超限时 `capped_versions>0` 且**进 errors** | 静默截断 ⇒ 红 |
| T9 🔴 G5 混级 | v1=dept_internal / v2=public、权威批 rd ⇒ **预筛不判 unchanged** 且 v1 得 `["rd"]` | 只改 materializer 不改预筛 ⇒ 红（即实测四） |

### 5.2 既有面

- `tests/test_acl_projection_writers.py` 的 allowlist 守卫必须继续绿
- `_prescreen_unchanged` 的既有测试不动（它本来就是全版本口径，本批不碰）
- `make test` / `make lint` 全绿；flag off 下**全量测试必须与改动前同结果**

### 5.3 验证顺序

1. flag off 跑全量 ⇒ 证明零行为变化
2. flag on 跑 T1-T8
3. 用 scratch 复现脚本跑 flag on，把 §0 的三段输出翻成绿

---

## 6. 🔴 部署顺序（本方案最重要的一条）

`epoch_dirty` 候选源（`allowed_depts_reconcile.py:224-240`）**已在 main 里，且只受
capability 探测门控**。它一旦生效（= 062 apply 后），会把「从未投影过的旧版本」
喂进 targets，而今天的 materializer 修不了它们 ⇒ 每轮进候选、每轮空转取锁、
每轮报 `unchanged` 假绿。这正是拍板单 §101 预警的顺序陷阱。

⇒ **两条硬约束**：

1. **确认生产/staging 的 062 是否已 apply**（本会话在 SIM，未查；需要 Sam 给只读一验）。
   若尚未 apply —— 那么在 C3′ 落地前**不要 apply**。
2. G4 把 `epoch_dirty` 收进 `RAG_ACL_VERSION_AXIS` 同一闸，让 apply 顺序不再是陷阱
   （apply 了但 flag 没开 ⇒ 候选源也不生效）。**这是我对拍板单的一处主动加固。**

---

## 7. 风险与回滚

| 风险 | 评估 | 缓解 |
|---|---|---|
| 写放大：一篇文档从改 1 版变成改 N 版 | 正常 N≤2；`reset_chunks` 会明显上升 —— 那是**在补此前没做的功课**，不是回归 | E3 版本上限 + `reset_chunks` 观察；必要时下调 `_LIMIT` |
| 单事务变长（多版本同事务） | 每版本 ~4 条 SQL，N≤2 ⇒ 约翻倍；仍远小于 `pre_drain` 的 ≤200 篇窗口 | 单预算沿用；E7 取舍待评审确认 |
| `index_status='NOT_INDEXED'` 标脏面扩大 ⇒ stage-3 backlog 变大 | 旧版本本来就该重推 | 分批放量：先 flag on 跑 `commit=False` 只读预览，看 drift 规模再决定何时真开 |
| 聚合 status 改变了 outbox 行为 | 有意（G2）。副作用：锁期内 outbox 行会累积 attempts | `attempts` 阈值告警另立；本批只加 `partially_locked` 统计 |

**回滚**：`RAG_ACL_VERSION_AXIS=false` 即刻回到今天的行为（无 schema 变更、无数据迁移、
无不可逆写）。已被本改动修正的旧版本投影**不会**因回滚而回退（它们只是变正确了）。

---

## 8. 尚未确定 / 请评审方裁决

1. 🔴 **混级文档的旧版本到底该不该拿 allowed_depts**（本方案唯一的**语义**裁决，其余都是工程取舍）。

   实测四已确定**机制**：预筛的跨版本 gate 会把这类文档判 unchanged。剩下的是**该怎么办**：

   - **A（我倾向）预筛与 materializer 都改逐版本** ⇒ v1(dept_internal) 拿到 `["rd"]`。
     理由：gate 的立意是「不让 allowed_depts 落到 restricted/public 的 chunk 上」，
     那本就是**逐行/逐版本**语义；retriever 也是把 allowed_depts 与
     `permission_level='dept_internal'` AND-bind 后使用。跨版本集合是当年
     materializer 只管 current 时的**过近似**，不是设计意图。
   - **B 维持跨版本保守** ⇒ 混级文档整篇不投影。代价：C3′ 的可用性缺口在混级文档上仍在。

   ⚠️ A **会扩大可见面**：rd 将能检索到一个它今天检索不到的旧版本。虽然那正是
   owner 当初批准的授权，但「批准的是文档还是当前内容」是**业务裁决**，
   我不替 Sam 拍。**A 落地前需要 Sam 明确点头。**

2. **flag 默认值**（§3.5）：off + 与 062 同批开 vs 默认 on。修的是机密性缺陷，取舍不对称。
2. **E7 事务粒度**：整篇单事务回滚 vs 逐版本提交。我倾向前者，但后者在版本多时更抗长事务。
3. **E3 版本上限取值**：20 是拍的；是否该按 `reset_chunks` 而不是版本数封顶？
4. **`version_no` 键的去留**：我保留它 = current，以免破坏既有调用方；但它现在**有歧义**
   （聚合结果配单版本号）。是否该改成 `versions: [..]` 并同步改调用方？
5. （原第 5 条「预筛口径差」实测后已升为第 1 条 + G5；此处留痕，免得评审方对不上号。）

---

## 9. 与其它单的关系

- **不含**拍板单 §109/§111 的 certify 强化（strict resolver / 三预算拆分）—— 另立
- **C9** 正交（permission 的权威之争）；但 §8.5 那条口径差与 C9 的 permission 同步问题相邻，
  若 C9 先落地会改变 `permission_level` 的可信度，届时需复核本单 §3.2 的 gate 假设
- **C3**（`c3prime` 的前身，已修）留下的 `:201` 注释「非 current 的旧服务版本…那是
  materializer 版本轴问题（C3′，另立）」正是本单要销掉的那行

---

## 10. 实施记录（2026-08-07）——与本稿正文冲突时以本节为准

### 10.1 双盲评审结果

`.claude-review/{claude-model,plan}.md` 是过程留痕。三轮下来：

| 来源 | 主张 | 结论 |
|---|---|---|
| Codex | 预筛不读 epoch ⇒ G4 承诺的收敛到不了（`allowed_depts_reconcile.py:78-85,124-143`） | CONFIRMED → **G6** |
| Codex | 版本上限截断破坏 outbox 必达（`access_grants.py:693-710` 只有 `skipped_locked` 留 pending） | CONFIRMED → **移除上限** |
| Codex | `commit=False` 不是只读预览（certify 写在 `if not apply` 之前） | CONFIRMED → **G8**（**当前 main 的既有缺陷**） |
| Codex | flag-off「逐字节等价」措辞不成立（062 已 apply） | CONFIRMED → 措辞作废 |
| Codex | 上限无版本游标 ⇒ 重试永远重复命中同一批（`schema/009` 只有 doc_id） | CONFIRMED → **移除上限**而非加游标 |
| Codex | `partially_locked` 接不到 ops_monitor（`ops_monitor.py:29-35` 无 ACL job） | CONFIRMED → **G10** 纯 SQL job |
| Claude | §7「无不可逆写」不成立（标脏旧版本 → loader 不限版本装载 → 按 `max(批内版本)` 退役 + HA3 删除） | CONFIRMED；**机制被 Codex 纠正为 PK 制 `cmd:delete`**（`pipeline_nodes.py:7250-7252`），我引的 `:7360-7364` docstring 与实现不符 |
| Claude | decide 端点是第二调用点、事务放大未评估 | CONFIRMED → **G12** |
| Claude | 无 dv 行的版本标脏后无人 drain（loader 是 INNER JOIN） | CONFIRMED → **G11**；Codex 指出**current 版本那一路今天就成立** |
| Claude | epoch 候选可覆盖 `node_stale_owner` 缺口 | **REFUTED**（equal-epoch stale-owner 无候选路径；`schema/062:81-86` 自己承认） |
| Claude | 「装载旧版本会导致停用更新版本」 | **自我推翻**（`:7507` 严格小于） |

**Codex 误报 0 / Claude 误驳 3。**

### 10.2 相对本稿的变更

- **G9 撤销**（Codex 驳回 + Sam 政策1）：非 current 版本**照常**标 `NOT_INDEXED`。
  退役副作用是**被授权的行为**，安全性依赖 04b parity 闸。
  ⇒ §7 的「无不可逆写」改为：**配置可回滚，但已发生的旧版本退役不可逆**。
  ⚠️ 留痕：04b 的 drift 检查只比 `chunk_text`、**不比 ACL 字段**（`pipeline_nodes.py:9631-9653`）。
- **E3 版本上限撤销**：`_MAX_VERSIONS_ALARM=20` 只作**告警**，全部版本照常处理。
  连带撤销 `capped_incomplete` 状态与 §3.2 里的相应一格。
- **G4 撤销**（被 `make test` 推翻）：一度把 `epoch_dirty` 收进版本轴 flag，实测两点不成立 ——
  ①零收益：它想挡的"非 current 版本每轮空转"同时命中 `have_ad` 候选，关掉 epoch 源挡不住；
  ②有副作用：退役→改归属→恢复→收敛链路（`test_retire_owner_change_convergence_db.py`）
  的自愈**正是靠这一路**，gate 掉它 = 该链路在默认 flag off 时静默失去自愈。
  ⇒ 候选源保持不受版本轴门控。**这条是我的方案缺陷，由测试而非评审抓出。**
- 新增 **G6**（预筛读 epoch）/ **G8**（预览零写）/ **G11**（`missing_version`）/
  **G12**（decide 只做 current）/ **G10**（ops_monitor `acl_projection` job）。
- 写预算改用独立文档级计数器 `_docs_written`（`materialized`/`retracted` 现在是版本数）。
- `config.py` 进修改范围：`RAGConfig.acl_version_axis` / `RAG_ACL_VERSION_AXIS`，默认 **off**。

### 10.3 落地文件

`config.py`（flag）· `access_grants.py`（壳+核拆分、`_certify`、`_version_processing_gate`、
`_aggregate_versions`）· `allowed_depts_reconcile.py`（预筛分组键+epoch 门、per_version 计数、
`_docs_written`、`partially_locked`/`missing_version`）· `queue_monitor.py`（`run_acl_projection_check`）·
`ops_monitor.py`（作业集）· `routes/kb_access.py` + `routes/kb_console.py`（G12，5 处）·
`tests/test_acl_version_axis_db.py`（新，9 用例）+ 4 个既有测试面适配。

### 10.4 验证

- `tests/test_acl_version_axis_db.py` **9/9 绿**（真库；缺 062 则 skip 不假绿）
- **变异反证 5/5 转红**：版本集退回 `[current]` / 预筛不读 epoch / certify 无视 `apply` /
  `missing` 当 `ok` / 预算按版本计。⚠️ 其中「预算」那条**第一版断言是空转的**——
  单篇文档时预算检查只在任何写之前跑一次，两种实现都不 capped；改成两篇文档夹具后才抓得住。
- `tests/test_access_grants.py` + `tests/test_allowed_depts_reconcile.py` **56/56 绿**
- 我改动面的 `ruff` 全绿；`make test` 4420 passed，**8 条失败全部落在另一 session
  正在重写的 `routes/contribution.py` 及其测试上**（当前带 F821），与本批无关。

### 10.5 仍待办（user-gated）

1. **翻 `RAG_ACL_VERSION_AXIS=true`** —— 现在是零爆炸半径窗口（active chunk 仅 6 条、
   C3′ population 0）。**必须早于语料重灌**，否则空转+假绿+outbox 误标 done 三件事同时成立。
2. **G10 的调度与告警路由**：`acl_projection` job 已实现但**未接调度**（现网节点仍只跑
   `--only reconcile_ha3 reconcile_oss`）。
3. **独立立单**：`kb_acl_projection_outbox` 未 done **2201 行**、最老 30h、`attempts` 全 0
   且一小时内从 2182 涨上来 ⇒ 形态是"有人入队、没人 drain"。成因未定（Codex 正确指出
   我"与 C3′ 无因果"的断言属 UNPROVEN）。新加的龄期探针 48h 阈值会在约 18 小时后 page。
4. **残留盲区（本批不修）**：`node_stale_owner` 的 equal-epoch stale-owner（`:205-211` 仍
   current-only）；`schema/062:81-86` 要求的语义 fingerprint 审计。
