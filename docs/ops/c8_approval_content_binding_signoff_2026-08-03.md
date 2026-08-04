# C8 审批内容绑定（签名 PUT URL TOCTOU）拍板单（2026-08-03）

> **背景**：2026-08-03 ultra 评审 C8 —— **审批放行的内容 ≠ 实际入库内容**。
> 加同日设计模式对标研究（`docs/acl_retention_design_patterns_2026-08-03.md`，在 remote 分支
> `claude/code-review-ultra-console-74ccdf@c92fed6`，**未合 main**）§3 的 Google Drive 范式。
> codex 评审 2026-08-03（第三批）。**本单只出决策，代码零改动。**

---

## 1. 缺陷（已核验）

- `UPLOAD_TOKEN_TTL = 30*60` —— **upload token 与签名 PUT URL 共用这 30 分钟**（`kb_upload.py:23`）。
- register 在**当下时点** `head_object(raw_key)`（`kb_console.py:2543`）取 `etag_val`（:2552）落
  `document_version`（:2698/2702）。
- **摄取路径全域零 etag 复核**（`pipeline_nodes` / `dataworks_orchestrator` / `dataworks_nodes` grep 确认）
  ⇒ stage-1/2 摄取的是**拉取时点**的字节，不是 register 时 HEAD 到的那份。
- 审批人预览（`doc-preview`，`kb_console.py:2143`）是**即时签名 GET** ⇒ 预览与最终入库实物可以是两份。

⇒ 上传合规文件 A → register/审批放行 → 30 分钟内用**同一 put_url** 重 PUT 任意字节 B
（可超 register 校验过的大小上限）→ DAG 摄取 B。`etag/file_size` 与实物永久失真，连带查重失效。

---

## 2. ⚠️ 两条对评审报告修法的纠正

### 2.1 「register 成功后作废 upload token」——**不成立**

upload token 是**本服务的 HMAC 令牌**（`kb_upload.py:201`）；PUT URL 是 **OSS AK 独立签发的预签名 URL**
（`oss_url.py:129`）。预签名 URL **按时间过期，服务端无法逐个撤销** —— 作废我们自己的 token
**完全不影响那条 put_url 继续可用**，只挡住走我们端点的路径。
（轮换 AK / 改 bucket policy 只能粗粒度影响整批访问，不是逐 URL 撤销机制。）

### 2.2 「摄取入口 ETag 复核」——**是止血，不是完整修复**（codex BLOCKER）

我原以为摄取侧按 register 的 etag 复核即可闭合。**反例不需要 ETag 碰撞**：

```
register 时对象 = B，登记 ETag(B)
审批预览前重 PUT A     → 审批人看到 A
点击审批后重 PUT B     → stage-1 If-Match ETag(B) 成功
最终入库 B             → 审批人批的是 A
```

⇒ 必须**同时**固定两处的内容身份：**①审批预览读的** ②**最终摄取读的**。裸 ETag 复核只解决 ②。

---

## 3. 🔴 终局方案：**V 或 F 二选一**（Sam 拍板）；E 仅临时缓解、**不关闭 C8**

> ⚠️ **治理边界（codex BLOCKER）**：E **不是**第三个终局选项。勾了 E **C8 仍保持 OPEN**，
> 必须同时指定终局方案的 **owner + 期限**，否则会出现"勾 E ⇒ C8 被当作已修复关闭"的误读。

### 方案 V（终局首选）：version-id 固化

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | register 保存 `raw_version_id + etag + file_size`；`doc-preview` 按保存的 `versionId` 签 GET；scanner 携带保存的内容身份；stage-1 在**真实 GET** 上带 `params={"versionId": expected}` 并核返回身份/大小 | ☐ |
| 为何首选 | 重 PUT 会产生**新 version**，不影响已固化的那个 ⇒ 这才是真正的"内容冻结"，同时闭合预览与摄取两处 | |
| ✅ 前置**已核实**（Sam 2026-08-04 告知） | **生产 OSS 已开 version control** ⇒ 原「不得为 C8 直接无评估开启 versioning」的阻塞**解除**（bucket 级姿态已是既成事实，不需为本项再做开关决策）。⚠️ 仍需确认的是**姿态细节**：Enabled 还是 Suspended、以及生命周期规则是否会删历史版本（若会，`raw_version_id` 绑定的对象可能在保留期后消失，`VERSION_ID` 模式需配套保留策略）| ✅ |
| SDK 可行性 | 仓库钉 OSS SDK **2.19.1**（`requirements-dataworks-py37.txt:17`），HEAD/GET 结果带 `versionid`，GET 与签名 URL 均接受 `params`。⚠️ 但当前 `head_object()` 包装器**把 version-id 丢掉**，只返回 size/content_type/etag（`oss_url.py:187-193`）—— 需先补 | |
| 🔴 **binding mode + fail-closed 契约（必须写死）** | 只靠 `raw_version_id IS NULL` **无法区分"存量"与"新写失败"** ⇒ 加显式 `content_binding_mode`：<br>`LEGACY_UNBOUND`（存量，允许兼容读）/ `VERSION_ID` / `FROZEN_KEY`。<br>**`binding_mode=VERSION_ID` ⇒ `raw_version_id` 必须非空；缺失或返回身份不符 ⇒ register/预览/审批/摄取全部 fail-closed，不得回退 ETag 或 legacy** | ☐ |
| 为何必须 | 生产若处于 Suspended、包装器漏字段或配置漂移，实施者可能把 NULL 当 legacy 兼容继续放行 ⇒ **V 静默退化回未绑定状态** | |

### 方案 F（后备）：不可变 final object

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 客户端 PUT **只写 staging key**；register 以**条件复制**生成一个**从未暴露 PUT 权限**的 final key；`document_version`、预览、摄取**全部指向 final key** | ☐ |
| 适用 | versioning 未启用且不打算开启时的完整后备（同样闭合预览与摄取两处） | |
| 纵深 | 缩短 TTL、禁止覆盖 PUT、摄取 `If-Match` | |
| 🔴 **必须先解决 register 幂等（codex BLOCKER）** | 现 register 幂等是**按 token 里的 `raw_key` 查 `document_version.raw_key`**（`kb_console.py:2575-2578`）。F 让 token 带 staging key、库里存 final key ⇒ **客户端因响应丢失重试时查不到已写的行** ⇒ 走新建路径撞 `uk_doc_version` 后仍按 staging key 查赢家 → 抛错（:2697/:2705）；**升版路径甚至可能再分配一个版本号** | ☐ |
| 🔴 幂等修法 | 定义**稳定的上传意图 ID**：token 同时携带 `upload_id / staging_key / final_key`，RDS 持久化 `upload_id`（或 staging-key hash）作幂等键；「复制成功、DB 失败」后的重试须**验证既有 final 对象并续写原版本**，**不得新建下一版本** | ☐ |
| 🔴 条件复制的可验证契约（不能只留四个字） | ①source copy 必须**绑定 register 所见身份**；②target 必须用**确定性、不可覆盖**的 final key；③copy 成功后**验证 final 对象**再提交 RDS；④任一步失败**不得退化为普通 copy 或继续登记** | ☐ |
| 🔴 staging 命名空间 | 必须放在 **`raw/` 摄取命名空间之外**（建议独立 `upload-staging/` 前缀 + TTL GC）——`register_new_files.py:231` 以 `OSS_RAW_PREFIX` 全扫 `raw/`，新 staging 形状若不匹配孤儿排除规则**会被批量注册器收编** | ☐ |
| 🔴 final key 必须保留权限路径契约 | `raw/<owner_dept>[/<perm_seg>]/<doc_id>/<upload_id>/<filename>`（`kb_upload.py:113-116`）被 stage-2 的路径权限解析依赖 ⇒ **不能改成不兼容的 `frozen/...` 后还指望按路径解析权限** | ☐ |
| ⚠️ staging 清理 | 条件是「**超过 PUT TTL 且没有【仍有效或持有活跃租约的】注册意图**」——⚠️ 若写成没有未完成意图，**已过期但从未完成的 abandoned intent 会让 staging 永久无法 GC**；**不能复制完成即删** —— 仍有效的 PUT URL 会重建同名 staging，造成库存噪声 | |

### 方案 E（仅止血，**不得宣称完整修复**）

| 项 | 内容 | 状态 |
|---|---|---|
| 内容 | 只在 stage-1 真实 GET 上加 `If-Match`（按 register 落库的 etag） | ☐ |
| ⚠️ 边界 | **没有固定审批预览内容** ⇒ 2.2 的反例仍然成立 | |
| 🔴 治理 | 启用 E **不关闭 C8**；必须同时登记终局方案（V 或 F）的 **owner + 期限** | ☐ |

---

## 4. 无论选哪个方案都必须一并定的四条

### 4.0 🔴 保护**不得只挂在审批分支**

即使**无需审批的自助上传**，重 PUT 同样能绕过 register 的**大小、ETag、查重**校验。
⇒ V/F 的内容绑定必须作用于**全部上传**，**不能只在 `PENDING_APPROVAL` 分支生效**。

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 内容绑定对免审批上传同样生效 | ☐ |

### 4.1 🔴 审批必须精确到单个 `version_no`

`KbApprovalRequest.version_no` 现为 **`Optional[int] = None`**（`kb_console.py:2219`），而
`kb_approve` 的 `vfilter = "AND version_no=%s" if req.version_no else ""`（:2803）
⇒ **省略即批准该文档【全部】pending 版本**。前端虽传具体版本（`useKb.ts:1002`），
但**API 安全边界不能依赖前端**。

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 审批/驳回**强制**单个 `version_no`，并绑定该版本的不可变内容标识 | ☐ |

### 4.2 🔴 内容不匹配的终态：**不可自动重试**（状态名须实施评审前定）

现有下载异常统一进 `_mark_extraction_failed`，其 docstring 明写「keys 保持 NULL ⇒
下一次 stage-1 按既有扫描谓词**自动重捡自愈**」（`pipeline_nodes.py:983-987`）。
**内容不匹配是安全不变量破坏，不是 OSS 瞬断** —— 复用该分支会**永久自动重试并反复占队头**。

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | mismatch ⇒ 进入**不可自动重试**的安全状态 + 审计 + 告警；要求**撤回/重新上传形成新版本**；**绝不静默改用现场对象** | ☐ |
| ⚠️ 命名 | 具体状态名及其在**待审批队列 / 状态徽章 / 告警**中的呈现须实施评审前定 —— **不要借用既有 `NEEDS_REVIEW`**，否则会与"可服务但内容不完整"那类语义混在一起 | ☐ |

### 4.3 🔴 `scan_oss_sync_keys.py` 必须同步约束

该工具可直接改写既有 `document_version.raw_key`，**却不更新 ETag/version-id**
（`dataworks_nodes/scan_oss_sync_keys.py:293`）。内容绑定上线后必须二选一：
**排除已绑定版本**，或**迁移时生成新版本并重新审批**。
**绝不能把同一审批版本重新指向另一份对象。**

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 选"排除已绑定" 还是 "迁移即新版本+重审批" | ☐ |

---

## 5. 产品决策（Sam 定，工程不替）

**紧急修订路径**：研究文档警告「锁期间的紧急修订需要显式的**撤回审批→解锁→重新提交**路径，
否则会把编辑流程卡死」。

| 项 | 归属 |
|---|---|
| 谁能撤回、状态叫什么、是否需要通知 | **Sam 定** ☐ |
| ⚠️ **但工程不变量不开放选择** | **登记/进入审批后的同一版本内容不得原地修改；修订必须生成新版本 + 新内容身份** |

---

## 6. 本批**不做**的：会签模型

`schema/001:124` 确认 `approval_status VARCHAR(32) DEFAULT 'PENDING'` —— **单一字段**，无法表达会签进度。
研究文档建议改「每审批人一行 + 法定人数」（Box `Task.completion_rule` / Google `reviewerResponses[]` + `dueTime`，
且 **reassign 只能新增或替换、不能移除**），但它自己也警告**"不要在没有超期提醒的情况下引入
`all_assignees`（会大量卡单）"**。

⇒ 现网是**单审批人（kb_admin）模式**，无已证会签需求；引入 quorum 是**产品能力扩展，不是 C8 修复**。
**本批只记录为未来设计项。**（codex 同意）

---

## 附：本单未核实项（拍板/实施前置）

- **生产 bucket versioning 状态**（Enabled / Suspended / 未启用）—— 决定方案 V 是否可行
- bucket 生命周期规则对历史 versions / delete markers 的处理及**成本影响**
- 是否允许条件 PUT / `x-oss-forbid-overwrite`，以及**生产 CORS 是否放行该头**（仓库当前无此实现）
- **生产是否有仓库之外的 writer** 会覆盖或移动 `raw/` 对象
- **OSS 是否支持所需的「source 条件复制 + target 禁止覆盖」组合**，失败时的**具体错误码**（决定 F 可行性）
- F 的「对象复制成功但 RDS 事务失败」时，幂等协议**如何恢复**

> ⚠️ 另一条口径纠正：ETag 在这里是**单次 PUT 的 MD5**（`kb_console.py:2551`）。它适合作 OSS 条件请求标识，
> **但不应在安全设计中称为强内容哈希**。仓库虽有 raw SHA-256，但在 **stage-1 下载后才计算、且失败时放行**
> （`pipeline_nodes.py:548`），**无法证明审批时的内容**。
