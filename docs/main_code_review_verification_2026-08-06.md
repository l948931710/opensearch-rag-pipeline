# `main_code_review_2026-08-06.md` 逐条核查 + 修复批次拆分

> 核查日期：2026-08-06（America/Los_Angeles，跨夜）
> 被核查对象：`docs/main_code_review_2026-08-06.md`（codex ultra 评审，基准 `efac342..133ad84`）
> 核查基准：**当前 HEAD `d967bee`**（比报告基准 `133ad84` 多 4 个提交：`bb76b47` / `03df630`
> / `f786921` / `d967bee`），因此报告里的行号普遍已漂移；本文按**当前树**逐条重核。
> 核查性质：只读，不改生产、不连线上。

## 0. 总账

| | 条数 |
|---|---:|
| 核查的行动项 | **23**（§3 七条 + §4 十条 + §5 六条） |
| **属实**（证据可复核、结论成立） | **21** |
| **属实但需纠偏**（事实对、归因或范围被放大） | **2**（§3.1、§3.6） |
| **推翻** | **0** |
| 已实施待线上证明 | 1（§4.2，`133ad84`，本会话已过 codex APPROVE） |
| 非新发现（仓内已登记的开放缺口） | 1（§4.1） |
| 已被后续提交部分缓解 | 1（§5.6，`f786921` 加了前端上界，后端缓存本身未动） |

**引用错误 2 处**（不影响结论）：
- §3.4 引 `schema/001_knowledge_base_schema.sql:124` —— 该文件**不存在**，真实为
  `schema/001_opensearch_pipeline.sql:124`（内容与行号都对：`approval_status VARCHAR(32) DEFAULT 'PENDING'`）。
- §3.2 引 `config.py:305-306`、§3.1 引 `kb_console.py:3437-3453` 等多处行号已漂移；
  实体都在，位置需重新定位（见下表「当前位置」列）。

⚠️ 一条**方法学提醒**：报告 §6 自陈「当前 SHA 完整测试 = 未独立验证」「lint/typecheck/build = 未独立验证」。
本次核查同样**没有**替它补线上证据；下面每条的判定只覆盖「代码事实是否如其所述」与「推理是否成立」。

---

## 1. §3 本次 diff 的行动项

### 3.1 [P1] 审批内容与实际摄取字节可以不一致 —— ✅ 属实，但**根因比报告窄**

| 报告的证据 | 当前位置 | 判定 |
|---|---|---|
| 签名/上传 token TTL 延到 60 分钟 | `kb_upload.py:29` `UPLOAD_TOKEN_TTL = 60 * 60` | ✅ |
| 内容绑定默认关闭 | `config.py:475` / `:1072` `content_binding: bool = False` | ✅ |
| 只有 schema 探测成功才写绑定列 | `kb_console.py:3150-3151` | ✅ |
| 探测异常当「不支持」 | `kb_console.py:3458-3474` | ✅（字面属实） |
| 摄取仍可读 key 当前内容 | `pipeline_nodes.py:629-645` | ⚠️ **纠偏**，见下 |

**🔴 纠偏一：摄取侧其实是对的。** `pipeline_nodes.py:634-645` 在 `binding_mode=VERSION_ID`
而 `raw_version_id` 为空时**显式 fail-closed**（注释原话：「不带 versionId 的取件那等于绕过绑定去
摄取"当前对象"」），有值时 `params={"versionId": _bvid}` 取钉死的那一版。
所以「摄取读当前内容」不是独立缺陷，而是**行被记成 LEGACY_UNBOUND 之后的必然结果**。

**🔴 纠偏二：flag 那一侧也是 fail-closed 的。** `kb_console.py:2942-2949`：开了绑定但 OSS HEAD
没回版本号 ⇒ 直接 **503**，不退回 LEGACY。报告把探测降级笼统称作 "fail-open" 覆盖面过大。

**✅ 真正的洞（报告方向对、落点需要收窄）**：
`content_binding=True` **但 schema/064 未 apply** 时 —— `_kb_content_binding_columns()` 返回
False ⇒ `_bind_cols` 为空 ⇒ 该行**以未绑定形态落库**，而端点返回 **200**。
运维以为绑定已开、实际每一行都是 LEGACY_UNBOUND，且**全仓没有任何启动期断言**把
「flag 开」与「064 已 apply」绑在一起（`config.py` 里 `content_binding` 只有取值，无校验）。

**并且今天这条是活的**：flag 默认 `False` ⇒ 现网**所有**行都是 LEGACY_UNBOUND，
TTL 又从 30 延到 60 分钟 ⇒ 报告描述的「A 过审 → 同 key PUT 成 B → 摄取 B」窗口翻倍。

---

### 3.2 [P1] 付费扇出提高但硬成本边界未落地 —— ✅ **全部属实**（7/7）

`git diff efac342..133ad84 -- config.py` 实证三个上限确实在本批次抬高：

```
-    max_ocr_pages: int = 50            →  + 200
-    pdf_native_max_pages: int = 200    →  + 1000
-    pdf_image_max_pages: int = 20      →  + 100     ← 注释自陈「**付费路径**…主要花费项」
```

| 证据 | 当前位置 | 判定 |
|---|---|---|
| rebuild/cost breaker 默认关 | `config.py:999` `_env_bool("REBUILD_ENABLED", False)` | ✅ |
| `RAG_FUNNEL_MAX_IMAGES` 默认 0 = 不限 | `unified_extractor.py:2200` | ✅（注释自陈「默认 0 = 不限」） |
| 共享 breaker 仅开关开启才生效 | `unified_extractor.py:2221-2222` `getattr(_breaker,"enabled",False)` | ✅ |
| **page-OCR fallback 绕过 breaker** | `unified_extractor.py:2745-2753` 直调 `ocr_client.ocr_pdf/ocr_image` | ✅ |
| `.env.example` 的 `RAG_REBUILD_COST_BREAKER` 是幽灵变量 | `.env.example:131`；全仓 `.py` **零命中** | ✅ |

breaker 在该文件只有 3 个接入点（`:1187` `:1196` VLM rebuilder、`:2221` 图片漏斗），
**page OCR 完全在预算之外** —— 而 OCR 页上限刚被抬到 200。

---

### 3.3 [P2] 新旧控制台上传能力不一致 —— ✅ 属实

| 事实 | 位置 |
|---|---|
| 新控制台 300MB | `console-app/src/lib/kb.ts:8` |
| legacy 硬编码 50MB | `webconsole/console.html:211` |
| legacy 超时 25 分钟 | `webconsole/console.html:224` |
| `/console-legacy` 仍公开 | `routes/console.py:103`（注释：「保留 ≥1 发布周期，P8 退役」） |

**补充报告没说的**：legacy 里那两行注释现在都是**假的** ——
`:211` 写「与后端 kb_upload.MAX_UPLOAD_BYTES 对齐」（后端已 300MB）、
`:224` 写「略小于 upload token 30min TTL」（TTL 已 60min）。注释比数字更误导人。

---

### 3.4 [P2] 退役污染普通版本的审批状态 —— ✅ 属实

```
退役 kb_console.py:3427-3429
  UPDATE document_version SET approval_status='WITHDRAWN'
  WHERE doc_id=%s AND approval_status='PENDING'                    ← 无 cps 条件
恢复 kb_console.py:3586-3588
  ... WHERE doc_id=%s AND approval_status='WITHDRAWN'
      AND content_process_status='PENDING_APPROVAL'                ← 多一个条件
```

两边**不对称**。而 `schema/001_opensearch_pipeline.sql:124` 的默认值就是 `'PENDING'`，
管线两条写入路径（`pipeline_nodes.py`、`dataworks_nodes/register_new_files.py`）都不显式设置
⇒ 普通版本天然带 `approval_status='PENDING'` + `content_process_status` 非 PENDING_APPROVAL。
退役把它打成 WITHDRAWN，恢复**永远匹配不上** ⇒ 卡死。

**讽刺的是退役 SQL 上方 `:3425-3426` 的注释自己写着**：「与 kb_restore 的还原**必须成对**，
否则文档恢复后那一版卡在 WITHDRAWN…一个没人会发现的隐形僵尸」——意图对，谓词没配平。

---

### 3.5 [P2] 过期拒绝可覆盖 WITHDRAWN —— ✅ 属实，**且比报告写的更严重**

- `kb_console.py:3344` reject 的 WHERE **只有** `content_process_status='PENDING_APPROVAL'`，
  不带 `approval_status` 条件；
- 退役**有意不改** `content_process_status`（`:3419-3421` 注释明说）
  ⇒ 退役后该版本仍满足 reject 的谓词 ⇒ WITHDRAWN 可被改写成 REJECTED
  ⇒ 恢复的 `WITHDRAWN→PENDING` 再也匹配不上。

**报告漏说的一半（更要命）**：前端 `approve` 已经在看计数了 ——
`useKb.ts:1345-1352` 有大段注释记着 2026-08-06 现网 20 个僵尸条目就是「只看 HTTP 码」造成的，
所以现在 `if (!ar?.approved) { notice(...); loadApprovals(true) }`。
而 **`reject`（`useKb.ts:1358-1366`）完全没做这件事** —— 拿到 200 就 `removeApproval(d)`，
后端返回的 `{"rejected": 0}` 无人消费。**同一个 bug 家族，修了一半。**

---

### 3.6 [P2] 退役/恢复后审批队列陈旧 —— ✅ 属实，但**范围被放大**

- `useKb.ts:2062-2069` `retire()` / `:2077-2085` `restore()` 都只 `await loadDocs()`，
  **不调 `loadApprovals`** ✅；而退役会把整篇文档的 PENDING 打成 WITHDRAWN
  ⇒ 队列里那些单已经不该在了 ✅。
- 批量退役 `:639` 同样只 `void loadDocs()` ✅。

**⚠️ 纠偏**：报告说「同文件批量操作路径也只刷新文档列表」——**不全对**。
`:1213` 与 `:1321` 两条批量路径**已经**带 `void loadApprovers(true)`（原文 `loadApprovals(true)`，
注释：「force：批量里可能有待审批单」）。所以缺的是 **retire/restore 这一族**，不是所有批量路径。

---

### 3.7 [P2] node ACL 空共享列表回退 legacy —— ✅ 属实

`DocTable.vue:61-64`：
```ts
function sharedLabels(d: DocItem): string[] {
  if (d.shared_labels?.length) return d.shared_labels
  return grantedLabelsByDoc.value.get(d.doc_id) || []      // ← node 文档零共享时落到这
}
```
没有任何 `acl_mode` 分支。node 文档没有 node share 时会展示历史 legacy 授权。

---

## 2. §4 跨模块生产阻断项

### 4.1 [P1] 新 generation 未完成即可服务 —— ✅ 属实，但**不是新发现**

`chunker.py:154` `is_active: bool = True` ✅；
`pipeline_nodes.py:9112-9120` 尾部失败 `raise RuntimeError(... "Aborting DAG execution to
prevent deactivating older chunk versions.")` —— 中止保护的是「旧版本别消失」这个不变量，
**不回滚已推的新版本 chunk** ✅ ⇒ 新旧双版本同时可服务。

⚠️ 这条**项目根 `CLAUDE.md` 早已登记为已知开放缺口**：
「partial-batch failures strand fully-INDEXED docs with their old versions still active
(dual versions served)」。报告独立复现了它，但不应按「新阻断项」计价 —— 它是**存量架构债**。

### 4.2 图片 serving-version gate —— ✅ 代码已落地并已过评审

`133ad84` 已实施；**本会话已完成 codex 双盲评审并 APPROVE**（详见
`docs/ops/codex_recheck_backlog_2026-08-03.md` §E 第四项）。剩两项线上前置未做，
与 memory 里登记的一致。**不重复立项**。

### 4.3 [P1] 300MB 入口缺压缩容器限制 —— ✅ 属实

全仓 `magic` / `zipfile` / `compress_size` / 压缩比检查 **零命中**；
Office 容器在主抽取进程直接打开：`docx_extractor.py:149,414`、
`image_extraction_utils.py:83,517,932`、`unified_extractor.py:1244,1401`。
（`:1401` 的 `read_only=True` 只省内存，挡不住 zip bomb。）
签名只绑 `Content-Type`（见 4.7）。

### 4.4 [P1] 限流 seed 丢启动后增量 —— ✅ 属实（半径小）

`rate_limiter.py:555-573`：`_seed()` 里
```python
cur = g_cnt if g_day == day else 0
if db_cnt > cur: self._global_day = (day, db_cnt)
```
就是 `max(memory, persisted)`，且 `:571` 走 **daemon 线程**（服务已在收请求）。
报告的复现场景成立。
⚠️ **半径**：丢失量 = seed 窗口（一次 DB 读，通常亚秒）内放行的请求数，不是无界。
docstring 自陈的前提「重启后内存从 0 重积」在异步 seed 下不成立。

### 4.5 [P1] DingTalk 无界线程 + 绕过 admission —— ✅ 属实（含文档缺口）

`dingtalk_bot.py:1721` `RAG_DT_MAX_WORKERS` 默认 **"0"**；`:1726-1727`
`RAG_DT_ADMISSION_ENABLE` 默认关；`:1840` 有界分支 vs `:1842` 裸 `threading.Thread`。
`.env.example` 对这两个变量 **零命中** ⇒ 运维无从发现。
代码注释 `:1703-1708` 自陈该缺陷。

### 4.6 [P1] promotion 未绑定当前 SHA 的完整证据 —— ✅ 属实（4/4）

| 事实 | 位置 |
|---|---|
| promotion 只 `needs: build-smoke` | `.github/workflows/image.yml:133` |
| baseline freshness `continue-on-error: true` | `.github/workflows/ci.yml:259`（注释：「E2 翻硬门时删除本行」） |
| release-gate 脚本仍标 DRAFT，缺 baseline 只 WARN 跳过回归判断 | `deploy/eval_release_gate.sh:2` / `:41-42` |
| 发布证据对应的是 `efac342` 不是当前 SHA | `docs/ops/image_release_2026-08-06.md:1,19,22` |

### 4.7 [P2] 签名 PUT 无字节范围 + staging orphan 无 GC —— ✅ 属实

`oss_url.py:131` `sign_headers = {"Content-Type": content_type}` —— 只绑 Content-Type，
无 `content-length-range`。大小检查在上传**完成后**的 register 阶段。

### 4.8 [P2] ACL reconciler 扫描面 ≠ 修复面 —— ✅ 属实（且有明确时间线）

- **扫描**：`allowed_depts_reconcile.py:79-85` `WHERE cm.is_active=1`，**无版本限定**。
  注释 `:74-77` 明记 2026-08-03「B2-② 去掉 `cm.version_no=dm.current_version_no`」——
  扫描面是**被刻意放宽的**。
- **修复**：`access_grants.py:402` `materialize_doc_allowed_depts` 第 1 步就
  `SELECT dm.current_version_no`（`:429-431`），并按**该版本**的 permission_level 做 gate。

⇒ 旧 active 版本漂移会**每轮被扫出、每轮修不掉、每轮再报一次**。扫描放宽时没同步放宽修复。

### 4.9 [P2] HA3 orphan 造成 top-k 饥饿 —— ✅ 属实

`dataworks_orchestrator.py:1198-1199` `RAG_STAGE3_ORPHAN_PURGE` opt-in，未开则整块跳过。
检索侧在有限 top-k 返回**之后**才做 RDS 复核，无 overfetch/backfill。

### 4.10 [P2] 会话请求缺幂等与顺序控制 —— ✅ 属实

`api.py:381-395` `AskRequest` 有 `session_id` / `conversation_id`，**无** request/idempotency ID。

---

## 3. §5 性能与效率

| 条 | 判定 | 当前树证据 |
|---|---|---|
| **5.1** 检索重复读 RDS authority | ✅ | `retriever.py` 共 **8** 处独立 `_get_db_conn()`：`618`（node ACL）/`713`/`808`（主命中）/`1080`（图片版本门，`133ad84` 新增）/`1706,1709`（stitch-expand 共享 scope，证明模式可行）/`2643`（cosurface）/`3024`（`_attach_doc_dates`） |
| **5.2** 每请求子线程池 | ✅ | 4 处 per-request executor：`:1381`（ha3fusion，max_workers=3，**默认开**）/`:2776`（multi-query）/`:2890`（主路预取）/`:2951`（cosurface 预取） |
| **5.3** 原件二次全文件读 | ✅ | `pipeline_nodes.py:703-709` 下载后重开文件按 1MiB 块算 SHA-256；`:713` extractor 再读一遍同一文件 |
| **5.4** 红点拉完整队列 | ✅ | `App.vue:31` `if (session.canManage) { loadApprovals(); loadAccessRequests(); loadPendingContribs() }`；`ContributeView.vue` 进页再拉一次 |
| **5.5** 无差别 idle prefetch | ✅ | `router/index.ts:29-36` `prefetch = () => { QaView(); ManageView(); ContributeView() }`，**无 canManage 判断**，Safari 走 2s setTimeout |
| **5.6** TTL 缓存无淘汰无上限 | ✅（部分缓解） | `kb_console.py:728-759` 裸 dict，get 命中过期只返回 miss **不删**，put 无 maxsize。⚠️ `f786921` 已加**前端** offset 上界，但后端仍接受任意 offset、缓存本身未动 |

§5.7 两条容量边界（`--workers 1` 水平扩展上限、rerank VL 路由率）报告自己就没提为缺陷，
本次同样不立项，只作上线观测清单。

---

## 4. 与既有台账的去重

立项前必须先对齐，避免同一件事在两个台账里各修一遍：

| 本报告条目 | 已在别处登记 | 处置 |
|---|---|---|
| §4.1 双版本同时可服务 | 根 `CLAUDE.md`「Open reliability gaps」 | 合并，不新增条目 |
| §4.2 图片版本门 | backlog §E 第四项（已 APPROVE，剩 2 项线上前置） | 已闭环，只跟踪线上前置 |
| §5.6 的 offset 上界 | backlog §F 第 2 项（`_KB_MAX_OFFSET` 静默钳位，待 Sam） | 缓存淘汰**另立**；offset 协议沿用 §F |
| §4.9 HA3 orphan | memory `ha3-row-evaporation-2026-07-17` 家族 | 引用既有结论，别重跑枚举 |
| §4.6 release-gate / E2 硬门 | memory `rb05-scorecard-realignment`（E2 翻硬门） | 合并到同一批 |

---

## 4-bis. Sam 拍板（2026-08-06）与执行结果

| 问 | 拍板 | 状态 |
|---|---|---|
| 先动哪批 | **A + B 代码一起做** | ✅ 已落地（见下） |
| 批 C 成本硬边界做到哪 | **接线做完但默认仍关** | 待做（C1 page-OCR 接 breaker 仍是唯一代码洞） |
| DingTalk 无界线程 | **先在 staging 翻，观察后再定** | ✅ A2 已把两个开关暴露进 `.env.example`；staging 翻闸待 Sam |
| B5 存量回填 | **先出 prod-ro dry-run 清单** | ✅ 已跑，**结论：无对象，本项撤销**（见下） |

### 批 A 已落地
A1 `.env.example:131` 幽灵变量 `RAG_REBUILD_COST_BREAKER` → 真名 `RAG_REBUILD_ENABLED`
（并注明熔断器不覆盖 page-OCR）；A2 补 `RAG_DT_MAX_WORKERS` / `RAG_DT_ADMISSION_ENABLE`；
A3 legacy console 两处失效注释（「与后端对齐」实为 50 vs 300MB、「略小于 30min TTL」实为 60min）；
A4 报告头部加核查指引 + 订正 schema 文件名。

### 批 B 已落地（过 codex 双盲，3 轮 APPROVE）

codex 抓出 **2 BLOCKER + 3 MAJOR**，其中一条是**我方案里的假陈述**：
我写「`/api/kb/reject` 唯一消费者是 `useKb.ts`，已 grep 确认」——那次 grep 加了
`--include=*.ts --include=*.vue --include=*.py`，把小程序和 legacy 排除在外；
而 legacy 还是**动态拼路径** `this.api('/api/kb/' + kind, ...)`，字面 grep 本来也抓不到。
实际三个消费者：`useKb.ts:1361` / `fuling-rag-miniapp/utils/api.js:254` / `console.html:379`。

| 项 | 落地内容 |
|---|---|
| B1 | 退役 WHERE 加 `AND content_process_status='PENDING_APPROVAL'`，与 kb_restore 配平。**并删掉我原来给的错误理由**——「宽谓词防 stage-1 认领复活」被 codex 推翻：stage-1 谓词（`pipeline_nodes.py:178-181`）不读 `approval_status` 也不读 `document_meta.status` |
| B2 | reject WHERE 加 `AND approval_status='PENDING'`；0 行由 200 改 **409**；🔴 409 必须抛在 `try` **之外**（否则被 `except Exception` 改写成 500），并补 `except HTTPException: raise`；🔴 `write_audit` 移到 409 之后 —— 此前**无条件**执行，一次 0 行驳回照样留一条 REJECT 审计 |
| B3 | 三端都改。Vue 消费 `rejected` 计数 + 409 刷队列；legacy 在 `.catch` 补 `loadApprovals()`（它今天靠 `.then` 自我纠正，409 后会丢）；小程序 `_decide` 改一处**同时覆盖 approve 与 reject**——Vue 侧 08-06 修掉的僵尸条目 bug，小程序两条路径都还开着 |
| B4 | 四处补队列刷新：`useKb.ts` retire / restore / bulkRetire + legacy `doRetire()`。⚠️ `_bulkRun` 被 `bulkSetVisibility` 共用，刷新走 `opts.refreshApprovals` 而非无条件（codex MINOR） |

**验证**：8 条变异全红 0 存活；`make test` 4393 passed / lint 0 / typecheck 0 /
vitest 519 passed / build 0，全部显式回显退出码。
⚠️ 测试第一版是**假绿**已修：`pending-approvals` 桩原本回空集，于是「本地乐观移除」与
「拉了服务端真值」最终条数都是 0，断言等于空转。
⚠️ 实施偏离方案一处：catch 里的刷新从「无差别」收窄为「只对 409」——
无差别版本被既有契约 `useKb.spec.ts:636`（500 失败时**有意**留着单可重试）当场打红。

### 🔴 B5 撤销：prod-ro dry-run 显示**无回填对象**

`scratch/withdrawn_zombie_dryrun_20260806.py`（只读会话）：

```
① WITHDRAWN 全量分布
   cps=PENDING_APPROVAL   meta=retired   ver=retired   n=20     （合计 20 行）
② 疑似受害集（WITHDRAWN 且 cps<>PENDING_APPROVAL，kb_restore 还原不了）
   —— 合计 0 行
```

⇒ 生产 20 行 WITHDRAWN **全部**是「合法撤销、恢复能还原」的那种。
**§3.4 预测的损伤在生产尚未发生**，B1 是纯预防，**回填无对象、本项关闭**。
⚠️ 脚本里写死了 codex 给的反例：`scripts/reset_for_rechunk.py` 只改 content/chunk 状态、
不碰 `approval_status`，能把合法的 `WITHDRAWN/PENDING_APPROVAL` 变成 `WITHDRAWN/NOT_STARTED`
⇒ 该谓词**不等价于**「被退役误伤」，输出只能当人工归因候选，**永远不许直接当回填目标**。

### 顺带上报：一条独立的既存缺陷（本批不做）

codex 在核 B1 时挖出来的：退役**只改当前版本**的 `dv.status`（`kb_console.py:3409`），
而 stage-1 的认领谓词只看 `content_process_status='NOT_STARTED' AND canonical_json_key IS NULL
AND dv.status='active'` —— **不看 `document_meta.status`**。
⇒ 一篇已退役文档的**旧 active 版本**若满足那三条，**退役后仍会被 stage-1 认领摄取**。
与 approval_status 无关，属「整篇退役是否该阻止旧活跃版本继续摄取」的独立业务问题。
（本次 prod-ro 快照未查该形态的存量，如需可加一条只读查询。）

## 5. 修复批次（本文的拆分，**与报告 §8 不同**）

报告 §8 按「问题类型」分批。我按**能不能一起验、会不会互相踩、需不需要你放行**重排 ——
理由：这批里真正的瓶颈不是编码量，而是①几条改动共用同一段 SQL/同一个连接 scope，
②好几条是 user-gated 或需要 schema 变更，③有 4 条是纯注释/文档，混在代码批里会拖慢评审。

### 批 A — 零风险澄清（可立即做，不需你拍板）

不改任何运行时行为，只消除「注释在撒谎」和「文档指向幽灵」：

| # | 内容 | 位置 |
|---|---|---|
| A1 | 删/改 `.env.example:131` 的幽灵变量 `RAG_REBUILD_COST_BREAKER` → 真名 `RAG_REBUILD_ENABLED` | `.env.example` |
| A2 | `.env.example` 补上 `RAG_DT_MAX_WORKERS` / `RAG_DT_ADMISSION_ENABLE`（§4.5 的文档缺口） | `.env.example` |
| A3 | legacy console 两处失效注释（「与后端对齐」「30min TTL」）改成事实 | `webconsole/console.html:211,224` |
| A4 | 报告自身的两处引用错误（schema 文件名、漂移行号）在报告里订正 | `docs/main_code_review_2026-08-06.md` |

**验收**：`make lint` + 不需要任何测试（无行为改动）。

### 批 B — 审批状态机（一个事务面，必须一起改一起测）

§3.4 / §3.5 / §3.6 三条**咬在同一组 SQL 谓词和同一个前端刷新策略上**，分开改会互相掩盖：

| # | 内容 |
|---|---|
| B1 | 退役的 `approval_status='PENDING'` → 加 `AND content_process_status='PENDING_APPROVAL'`，与 restore 配平（§3.4） |
| B2 | reject 的 WHERE 加 `AND approval_status='PENDING'`；0 行时返回 **409** 而不是 200（§3.5） |
| B3 | 前端 `reject` 消费 `rejected` 计数 —— **照抄 `approve` 已有的那段**（`useKb.ts:1345-1352`）（§3.5） |
| B4 | `retire`/`restore`/批量退役三处补 `loadApprovals(true)`（§3.6） |
| B5 | 存量数据：`approval_status='WITHDRAWN' AND content_process_status<>'PENDING_APPROVAL'` 的行需要**受控 backfill** ⇒ 🔴 **要你放行**（prod 写） |

**验收**：真库跨状态矩阵（普通版/待审版 × 退役/恢复/驳回 × 并发）+ 前端 3 条 + 变异反证。
**依赖**：B5 必须在 B1 之后（先止血再回填）。

### 批 C — 成本与资源硬边界（钱和进程稳定性）

| # | 内容 | 备注 |
|---|---|---|
| C1 | page-OCR fallback 接入共享 breaker（§3.2 唯一的**代码**洞） | `unified_extractor.py:2745` |
| C2 | 单文档/单批次/每日预算改**默认开启且非零**，覆盖 page OCR + 漏斗 OCR + VLM | 🔴 **默认值要你定**（直接影响摄取会不会被截断） |
| C3 | DingTalk 有界 worker + admission 默认开 | 🔴 **翻默认要你拍板**（`RAG_DT_MAX_WORKERS` 取值） |
| C4 | 限流 seed 在收请求前同步完成，或改 `persisted_base + boot_delta` 原子合并（§4.4） | 纯代码，半径小 |
| C5 | Office/ZIP 魔数 + entry 数 + 展开总量 + 压缩比校验；大文件隔离 worker + 超时（§4.3） | 工作量最大的一条 |

**验收**：C1/C4 有单测；C2/C3 是 flag 翻默认 ⇒ 需 staging 压测；C5 需构造 zip bomb 样本。
**为什么和批 B 分开**：这批改的是摄取/钉钉侧，与控制台事务面零重叠，可并行推进。

### 🔴 批 D 重新定型（2026-08-06 双盲复核：报告 §5.1/§5.2 的**前提**不成立）

开工前我和 codex **各自独立**读了一遍 `retriever.py` 的调用链，两边都判定报告这两条的
事实描述对、但**前提被放大了**，其中一条让报告的目标**物理上做不到**。

#### 双方独立撞出的同一结论

| | 报告的说法 | 实际 |
|---|---|---|
| §5.2 | 「默认检索按请求创建 HA3/**多查询**/**预取**子线程池」 | 只有 HA3 fusion 是默认（`config.py:268` True）。`multi_query_mode` 默认 **"off"**（`config.py:427`）⇒ fan-out 与主路预取两个池**默认根本不建**。cosurface 预取的 `cosurface_images` 参数默认 **False**（`retriever.py:2827-2830`，`/api/ask` 传 false）⇒ 只在图文 SSE 路径。**默认纯文本查询 = 1 个池 / 3 worker，不是 4 个** |
| §5.1 | 「建立 request-scoped connection，把 8 处 checkout 降到 ≤2」 | `_conn_scope` 是 `threading.local()`（`retriever.py:1689`）。`search_chunks`（含 3 个 authority 策略）在 multi-query 开启时跑在 `_px` / fan-out 的 **worker 线程**上；pymysql 连接**非线程安全**，跨线程共享是数据损坏不是优化 ⇒ **「8 处合并」在物理上做不到** |

#### codex 另外找到、**我漏掉**的三条（都已核）

1. **8 处 checkout ≠ 每请求 8 次**：`:618`（node ACL）与 `:713`（legacy 跨部门）**互斥**
   （有 `acl_ctx` 时 `:679-680` 直接 return）；`:1706`/`:1709` 也互斥（scope active 与否）。
   我在自己的模型里**照抄了报告的「8」而没查互斥性**——这是我的错。
2. **`_get_db_conn()` 每次显式 `begin()`**（`db.py:32-55`）⇒ 请求级单连接会把本来分处
   多个时间点的读**塞进同一个事务边界**，改变 TOCTOU 的可见方式。这不是性能改动，是**语义改动**。
3. **expand 会产出原命中里不存在的 sibling chunk**（`retriever.py:2213-2229`，各带 `image_refs`）
   ⇒ 报告要的「一次批量 authority 快照」**对 expand 后的载荷不封闭**。
   现有代码在 expand 之后**再走一次**版本门（`:2988-2996`）正是为此。

另有两条边界：`AclContext` 在进入 `retrieve_and_enrich` **之前**就读 RDS
（`api.py:950-958`、`dingtalk_identity.py:973-1037`）⇒「每请求 ≤2」若指端到端则前提也不成立；
分解 timeout 8s、rerank timeout 15s ⇒ 把连接跨这些外部等待持有会**恶化**池压而非改善。

#### 重新定型后的批 D

**D0（新增，且必须最先做）—— 先量，再谈重构。**
现在**没有任何测量**：仓内零处断言每请求 checkout 数或 executor 生命周期，
上面所有"收益"都是推的。用 `db._get_db_conn` 包装器记录线程名/次数/连接对象 id，
跑六种路径（默认单查询 / 含非 public 命中 / 含图 / SSE cosurface / multi-query 开 / rerank probe）。
⚠️ SIM 模式量不出来：`_deny_stale_version_images`、`_attach_doc_dates` 等在 `simulate_db` 下直接早退。
必须用真库栈或既有测试的 `_get_db_conn` 打桩范式（`tests/test_main_hit_revalidate.py:68-100`）。

**D1（收窄）** 只合并**同一线程、顺序执行、中间无外部等待**的 authority SQL
（请求线程上的 stitch/expand + expand 后版本门 + 探测 + doc_date）。
**绝不**跨 `_px` / fan-out / `_cx` 的 worker。收益 = 少几次 checkout，**不减少 SQL 往返**。

**D2（升级为独立项，本批不做）** 报告要的「合并成一次批量 authority 查询」——
它改的是**裁决输入**不是 transport，且被上面第 3 条证明「一次快照不封闭」。
要做必须先定义每根轴独立的「成功 / 空集 / 缺行 / 失败」四态，把现有
fail-open/fail-closed 逐条锁死（node ACL 丢全部非 public / legacy 丢跨部门 /
主命中默认保留 / 版本门丢图保正文），**不能用一个空 dict 代替四态**。

**D3** executor 收敛照做，但按实际默认面定规模（1 个池 / 3 worker 是主战场）。
⚠️ 换长活池必须同时处理：删掉 `shutdown(wait=False)`（`:2898`/`:2959`，对共享池调用会毒化进程）、
每 task 的 TLS 清理（worker 跨请求复用 ⇒ 残留 scope 会泄漏连接）、队列上限与背压。

**D4** 指标（并入 D0 的探针，落成常驻断言）。

#### 🔴 D0 已跑（2026-08-06）：默认路径 **3 次**，不是 8 次

探针 `scratch/retriever_checkout_probe_20260806.py`：`simulate_db=False` + `_get_db_conn`
换计数桩（记线程名/调用者/连接对象 id/SQL），HA3 侧只桩客户端与解析，
`search_chunks` 的三个 authority 策略与融合 executor **全部真跑**。

| 路径 | checkout | 线程 | 分布 |
|---|---:|---|---|
| 默认纯文本 · 全链 | **3** | 全 MainThread | `_revalidate_main_hits` 1 + `_stitch_expand_conn` 1 + `_attach_doc_dates` 1 |
| 含图命中 · 全链 | 3 | 全 MainThread | 同上 |
| multi-query 开 · 全链 | 3 | 全 MainThread | 同上（见下方口径①） |
| 仅 `retrieve_and_enrich` 段（桩掉 search_chunks） | 2（含图 3） | 全 MainThread | stitch/expand 1 + doc_date 1（+ 版本门 1） |

⚠️ **口径与未覆盖面（不夸大）**：
① multi-query 那行仍是 3 且全在 MainThread —— SIM 下 `maybe_decompose` 不产子查询，
   **worker 线程分支没被真正走到**，"worker 上会各自 checkout" 仍是静态推断。
② 探针命中全为 `public` ⇒ `_deny_revoked_cross_dept` / node-ACL 的候选集为空、未触发。
   真实部门内查询应 **+1**（两者互斥）⇒ 现实默认约 **4**，含图再 +1 ⇒ **≈5** 封顶。
③ 桩连接不量 SQL 往返延迟与 pool wait，只量**次数**。

⇒ 现有 `_conn_scope` **已经把最大的一块合并掉了**（stitch+expand 两阶段共用 1 次）。
D1 的剩余头寸 = **3~5 → 1~2，即每请求省 2~3 次 checkout**。

#### 🔴 D0 数据订正（2026-08-06 晚，codex 指出）

第一版探针里写的 `try: get_config.cache_clear() except AttributeError: pass`
**静默什么也没做** —— `get_config` 不是 `lru_cache`，是模块级 `_config` 单例
（`config.py:1292-1300`）。于是 **`multi-query 开` 与 `rerank probe 开` 两行的环境变量从未生效**，
量到的还是默认配置。**那两行数据作废。** 这是我自己测量里的一次假绿。

改成直接 `config._config = None` 后重测，multi-query 那行**变了**：

```
【multi-query 开 · 全链】 checkout = 3
  按线程 : {'ThreadPoolExecutor-2_0': 1, 'MainThread': 2}
```

⇒ **`_revalidate_main_hits` 确实会落到 worker 线程上**（经 `_px` 主路预取，
`retriever.py:2886-2898` —— 只要 mode 是 auto/llm 就起，不要求真的产出子查询）。
原先标注的「worker 分支仍是静态推断」**现已证实**，这反过来**加强**了 D1 的撤销理由：
跨线程共享一条 pymysql 连接不安全，那部分 checkout 本来就不可合并。

#### 判据结论：D1 建议**不做**，D3/D4 保留

- **D1 收益**：省 2~3 次 checkout/请求。**代价**：`_get_db_conn()` 每次显式 `begin()`
  （`db.py:32-55`）⇒ 合并后整请求进同一事务边界（语义改动）；且 fail-closed 的
  版本门与跨部门撤销从**独立故障**变成**同时故障**。收益不抵风险。
- **D3 仍然成立且是本组真正的资源故事**：`api.py:139` 把 AnyIO 令牌调到 **120**，
  而每个 `search_chunks` 新建 3-worker 融合池（`retriever.py:1381`，`config.py:268` 默认开）
  ⇒ 理论上 **120 × 3 = 360** 个短生命周期子线程，且没有任何进程级总量闸。
  这条与 checkout 无关，不受 D0 结果影响。
- **D4 = 把这个探针变成常驻断言**：现在没有任何测试钉住"每请求 checkout 数"，
  将来有人再加一个自取连接的消费点，无人会发现。

#### 判据：D 值不值得做，取决于 D0 的数字

若默认路径实测只有 2–3 次 checkout（互斥性使然，很可能），
则 D1 的收益接近噪声，**整批应降级或取消**，只保留 D3 与 D4。这一步没有捷径。

#### D4-① 已落地：`tests/test_retrieval_checkout_budget.py`（4 条）

把 D0 的数字固化成**预算断言**（不是性能断言）：默认全链 3 次、含图 3 次、
消费点集合固定、stitch/expand 必须仍共享一条连接、默认路径全在调用线程。
变异反证：破坏 `_conn_scope` ⇒ 红；新增一个自取连接的消费点 ⇒ 红。

⚠️ **该测试第一版是假绿**：fixture 全是 `text` chunk，而 `expand_step_context:1961-1966`
的 `need_expand` 为假时**整段早退**、连 `_stitch_expand_conn` 都不调 ⇒
「stitch/expand 共享连接」那条恒为 1，把 `_conn_scope` 关掉照样绿。
加了一条 `step_card` 后变异才打红。

#### 🔴 D3 未实施：codex 找出 5 条 BLOCKER + 一条无法在本会话解决的定值依赖

| # | BLOCKER | 位置 |
|---|---|---|
| 1 | **P→P 自等待**：`_one(0)` 在 P worker 里 `primary_supplier()` 等的是**同属 P** 的 `_primary_future` ⇒ 「P→F 单向无环」不成立 | `retriever.py:2757` / `:2893` / `:2905` |
| 2 | fusion 的 fail-open **只包 `fut.result()` 不包 `ex.submit()`** ⇒ `PoolSaturated` 会在提交第一个臂时同步抛出，**落不进**既有单臂降级 | `retriever.py:1381` vs `:1385` |
| 3 | 进程级池**没有关闭点**：`api.py` lifespan 的 `yield` 后无 `finally` ⇒ 只删 retriever 里的 shutdown 会留下不可控常驻 worker | `api.py:123` / `:179` |
| 4 | 我方案里的 `as_completed` **会破坏顺序**：`ex.map` 保证 `lists` 与 query 同序，而交错合并依赖它 | `retriever.py:2776` / `:2798` |
| 5 | permit 生命周期未定义完整：submit 失败 / future 运行前取消 / 任务抛异常都不得泄漏 permit，否则「有界」会永久缩成 0；TLS 清理还必须**关掉**残留的 `_conn_scope.conn` 而不只是置空 | `retriever.py:1689` / `:1703` / `:2978` |

另有三条 REMAINING：`rejected` 单一计数不够（须按池/调用点/降级原因分类+分母+阈值）；
长活 worker **不继承 ContextVar** ⇒ 需每次 submit `copy_context()`（`request_context.py:4,41`）；
「含图=3」的 fixture 未证明图真的过了两次版本复核。

**定值依赖（本会话无法解决）**：F/P 的默认 worker 数**不能由静态代码推出**。
codex 与我独立同意：本批应只提供旋钮 + 每进程上限 + 指标，
**默认值必须由 staging 的 HA3/DashScope 配额、P95/P99、实例数与可接受降级率决定**。
在拿到这些数据前，把 24/12 当"有依据的默认值"上线是拍脑袋；
而「维持旧的每请求等效并发」又等于没限流、失去 D3 的意义。

⇒ **D3 挂起，等 staging 压测数据。** 五条 BLOCKER 的修法已明确（见 codex 输出），
不是设计不通，是**默认值缺证据**。

### 批 D — 检索热路径（原始拆分，已被上面重新定型取代）

§5.1 / §5.2 **必须一起做**：都是「请求级共享 vs 每消费点自取」的同一个问题，
分两次改会让第二次把第一次的 scope 又切开。

| # | 内容 |
|---|---|
| D1 | request-scoped authority snapshot：一次批量取 active / ACL / 物理 PK / serving version / doc date（8 处 checkout → 目标 ≤2） |
| D2 | 图片版本门并入主命中投影；expand 后版本门放进现有 stitch/expand scope |
| D3 | 进程级有界 executor 或按 HA3/DashScope 分别的 semaphore，替掉 4 处 per-request 池 |
| D4 | 指标：每请求 checkout 数、pool wait、authority latency、executor active/queued/timeout |

**🔴 硬约束（写进方案，别让重构顺手抹平）**：撤权 / 主命中 / 图片 gate 的
**fail-open vs fail-closed 语义必须各自保持原样** —— 共享的是 transport，不是安全裁决。
**验收**：并发回归 + 指标基线；这批改完必须重跑 eval（融合路径动了）。

### 批 E — 状态一致性与召回（各自独立，可按人手排）

| # | 内容 | 备注 |
|---|---|---|
| E1 | ACL reconciler：扫描面与修复面对齐（要么都只看 current，要么 materialize 覆盖全部 active 版本）（§4.8） | 建议后者，因为扫描面是 08-03 刻意放宽的 |
| E2 | node/legacy ACL 展示按 `acl_mode` 显式分支，node 模式下空 `shared_labels` 即权威（§3.7） | 纯前端，最小 |
| E3 | HA3 bounded overfetch/backfill；orphan purge 纳入常规运维（§4.9） | 🔴 purge 是不可逆 HA3 删除，**逐次授权** |
| E4 | 会话幂等 request ID + clear 的 generation/tombstone + 同 session 顺序控制（§4.10） | 需要设计，别顺手做 |
| E5 | 上传字节范围策略 + staging 配额 + orphan GC（dry-run 优先）（§4.7） | 🔴 GC 涉删对象，**逐次授权** |

### 批 F — 前端与缓存效率（纯性能，最后做）

| # | 内容 |
|---|---|
| F1 | consolidated badge-count endpoint（只回计数 + truncated），完整队列进页面再拉（§5.4） |
| F2 | `useContribute` 补 single-flight + staleness 门（`useKb` 已有现成范式）（§5.4） |
| F3 | ManageView 预取按 `canManage` + `saveData`/慢网裁剪（§5.5） |
| F4 | `_dashboard_cache` 改带 `maxsize` 的 TTL/LRU + 过期淘汰 + 容量指标（§5.6） |

**注**：F4 与 backlog §F 第 2 项（offset 钳位协议）是两件事 —— 缓存淘汰不需要你拍板，
offset 响应形态需要。

### 批 G — 发布证明（不是编码，是流程）🔴 全部 user-gated

| # | 内容 |
|---|---|
| G1 | 干净工作树冻结候选 SHA（⚠️ 现在树里还有**另一会话**未提交的 `conftest.py` / `DocTable.vue` / `tokens.css`） |
| G2 | promotion job 机器校验精确 Git SHA + 镜像 digest；把完整 CI / 安全扫描 / SBOM / live eval / canary 设为前置（§4.6） |
| G3 | 删 `ci.yml:259` 的 `continue-on-error`（= RB-05 的 E2 翻硬门，memory 里已登记待办） |
| G4 | `eval_release_gate.sh` 去 DRAFT；缺 baseline 从 WARN 改 FATAL |
| G5 | §4.2 的两项线上前置（prod-ro canary + egress 证明） |
| G6 | 254 条 Deep Scan candidates 在新冻结 SHA 上去重 / 验证 / 剔误报（§9） |

---

## 6. 建议的推进顺序与理由

```
批 A（今天就能清）
   └─ 批 B ──┐
   └─ 批 C ──┤ 三批可并行（事务面 / 摄取面 / 检索面互不重叠）
   └─ 批 D ──┘
              └─ 批 E（依赖 B 的状态机定稿、D 的连接 scope 定稿）
                    └─ 批 F（纯性能，改完不影响正确性判定）
                          └─ 批 G（必须在代码全部落定后，对最终 SHA 一次性做）
```

**不建议照报告 §8 的顺序**的两个具体理由：
1. 报告把「审批内容绑定」「generation 原子发布」「图片 gate」「ZIP 限制」放在同一批
   —— 但 §4.2 已经做完了，§4.1 是存量架构债（要单独立项、可能是季度级），
   把它们捆在「第一批」会让这一批永远交付不了。
2. 报告把 §5.1（RDS checkout）和 §5.2（线程池）拆在「第二批」的两个条目里
   —— 它们是同一次重构，拆开做第二次会撤销第一次的 scope。

## 7. 本次核查没做的事（诚实边界）

- **没有**对任何一条做线上验证（全程只读本地树，未连 prod RDS/HA3/OSS）。
- **没有**独立复跑报告 §6 里那些「已过期」的测试结果。
- **没有**核查 §9 的 254 条 Deep Scan candidates —— 那是 G6 的工作量，
  且报告自己说明它们「不是 254 个已验证漏洞」。
- §3.1 的「TTL 60 分钟是否该回退」属产品取舍（Sam 2026-08-05 拍板过 300MB 配套），
  本文只指出窗口翻倍这个事实，不代为裁决。
