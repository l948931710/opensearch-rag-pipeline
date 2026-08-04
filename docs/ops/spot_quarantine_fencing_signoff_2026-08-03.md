# spot-check 隔离 ↔ 摄取写方 栅栏协议 · 决策单（2026-08-03）

> 状态：**待 Sam 拍板**。代码侧本日已落**探测器**（`d1a5ace`），本单是**栅栏**，未动一行代码。
> 同族：仓库已登记的开放缺口「cross-cloud split-brain on irreversible HA3 deletes（RDS↔HA3 无 2PC）」。

## 1. 为什么还需要一单

`d1a5ace` 关掉的是**陈旧快照**和**能被 PK 集合看见的并发重切**。三个洞按设计留着，
因为关闭它们要改的是 stage-2 / stage-3 / spot / schema 之间的**协议**，不是某个函数：

| # | 残余洞 | 为什么探测器看不见 | 后果 |
|---|---|---|---|
| ① | **同 PK 复活** | `node_acquire_index_lock` 有 `SUCCESS→PROCESSING` 重锁支（pipeline_nodes.py:6855-6862），commit 放锁后才 embed/push。stage-3 用**同一** `rds_id` 重 `cmd=add` ⇒ PK 集合前后完全相同 | RDS 已 QUARANTINED，HA3 被重新写活，**旧权限副本**继续被检索 |
| ② | **提交后写方** | 探测是时点的。spot commit 之后才落地的 re-chunk 照样产生新 PK | 同上，窗口无上界 |
| ③ | **请求删除 ≠ 确认删除** | HA3 `push_documents` 的 2xx 响应体仍可能含逐文档错误（对比 add 路径已有的逐文档错误解析） | 以为删了，其实没删 |

①的可达面收窄一点：需该 `(doc_id,version_no)` 仍有未索引 chunk 才会进 stage-3 工作集，
即仓库已知的 partial-batch 遗留面——**不为零**。

## 2. 三个已被证伪的"省事修法"（别再走回头路）

- **加 `_lock_doc` 就行** ❌ `_lock_doc` 锁 `document_meta`，而 `node_write_chunk_meta`
  **不取该锁**（其 docstring 自己声明纪律只覆盖"同事务触碰 ≥2 张表"的写方族）。锁了也拦不住重切。
- **持 `chunk_meta` 行锁跨 HA3 删除** ❌ stage-2 的 DELETE 是整批 OR-链**一条语句**（≤100 篇），
  卡住一篇 = 卡住整个 stage-2 事务；租约开启时 stage-2 是 `dv → chunk`、spot 是 `chunk → dv`，**成环**。
- **复用 `PENDING_DELETE` 当重试通道** ❌ 两个理由：(a) 既有那支写 `index_status` **无 `_lock_doc`、
  无前态谓词**，会踩掉 console restore / set_visibility 的 `PENDING_DELETE→NOT_INDEXED` 恢复
  （kb_console.py:3034/3151 注释写明「否则下轮 reconcile 恰好撤销这次恢复」）；
  (b) `reconcile_pending_deletes` 成功后只补 chunk 停用 + `DELETED`，**不补** permission /
  publish_status / gate_status / QUARANTINED / risk ⇒ 它不是"正确重做整套隔离"。

## 3. 拟议协议（四件套，须整体成立）

1. **持久化 spot-delete intent + 代际**（新表或 `document_version` 加列 + schema 迁移）。
   与通用 `PENDING_DELETE` **分开**——后者可被 console 合法撤销，隔离意图不可。
2. **stage-2 提交前验栅**：full-replace 前检查该 (doc,version) 的隔离代际；被 fence 则放弃该篇
   （文档粒度弃单，与 PR-4 租约丢锁同款语义）。
3. **stage-3 push 后验所有权**：若期间被 spot 夺权，**必须同步补偿删除本次实际 `cmd=add` 的 PK**。
   只靠收尾 CAS 落空不够——CAS 落空时行已经进 HA3 了。
4. **spot 专用 reconciler**：成功后落**全套**隔离元数据（不是通用 reconciler 那半套）。
   并且：只有确认无在途可 add 的写方、且以 **fetch 权威读**确认目标 PK 已缺失，才允许写终态、
   才允许进 `quarantined_documents`。

## 4. 待 Sam 勾选

- [ ] **A. 做不做**：现在立项 / 挂进现有「RDS↔HA3 无 2PC」条目一起做 / 明确接受残余风险并留探测器
- [ ] **B. intent 落在哪**：新表 vs `document_version` 加列（涉 schema 063）
- [ ] **C. 隔离粒度**：`(doc_id, version_no)` 还是整个 `doc_id`
      —— 并发**升版**是另一类 TOCTOU（spot 只隔离抽样到的那一版），本单未覆盖，取决于此项
- [ ] **D. 既有 `PENDING_DELETE` 无前态谓词**（会踩 console 恢复）单独修还是并入本协议
- [ ] **E. ③ 需要 fetch 权威读**：接受额外 HA3 往返成本吗

## 5. 当前风险姿态（拍板前）

语料真空期 + spot-check 采样 5%，**今日实际暴露≈0**。风险随重灌批次和语料回填同步上升；
维护性 re-chunk（refreeze / route-v2 / C 重灌）与夜间 spot-check **同窗运行时**才是真正的触发条件。
探测器已能把该情形从"静默假成功"变成 `report["errors"]` + `ha3_containment_unconfirmed` 计数——
**先把这个计数纳入夜检关注项**，它非零即代表本单该动了。
