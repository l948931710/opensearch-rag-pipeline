# retention 归档与批次状态 拍板单（2026-08-03）

> 🟢 **Sam 2026-08-03 已拍板：选 A（打通删前冷归档）**。
> 实施进度：`4898e44`（run id 抗碰撞 + 启动即 preflight + 节点装 oss2 + 钉版镜像 parity）
> · `cbe0ec0`（主体擦除覆盖 OSS 归档面 —— 选 A 引入的 PIPL 缺口，同批堵上）。
> **仍待 Sam 执行的 ops 动作**（代码已就绪、我不能代做）：
>   1. DataWorks retention 节点「凭据」段补 **OSS 四件套**（endpoint/bucket/AK/SK）——
>      缺任一项节点启动即失败（preflight 有意 fail-closed）。
>   2. 首跑保持 `DRY_RUN=True` 观察，再翻阶段 2。
>   3. ⚠️ **未实测**：生产 OSS 账号是否真具备该 bucket 的 `PutObject` 权限。
> 本单其余未勾选项（六分类状态机等）仍待拍板。

> **背景**：2026-08-03 ultra 评审的 C5/C6 两条，加同日设计模式对标研究
> （`docs/acl_retention_design_patterns_2026-08-03.md`，在 remote 分支
> `claude/code-review-ultra-console-74ccdf@c92fed6`，**未合 main**）的建议。
> codex 评审 2026-08-03（第二批）。**本单只出决策，不落代码。**
>
> **已落码但未提交**（上一批）：C6 的锚点前序依赖门（`needs_anchor`/`is_anchor` +
> `blocked_by`），`make test` 4015 绿、4 例回归逐条反证过。本单是在其上的增量。
>
> **不在本单**：C6 的「锚点墓碑化」（Laserfiche 范式）—— 是否满足 **PIPL 第 47 条的"删除"
> 需法务书面确认**，不由工程默认。

---

## 1. C5 · 归档 fail-closed 与调度配置冲突

### 1.1 缺陷（已核验）

- `_archive_enabled()` 默认 **true**（`os.environ.get("RAG_RETENTION_ARCHIVE","true")`）；
  `_archive_batch` 在 OSS 不可用（simulate/占位凭据）时 **raise 拒删**（fail-closed）。
- 但 `dataworks_nodes/retention_node.py:65` 显式设 `RAG_SIMULATE_OSS=true`，
  且第 32 行注释明写"retention 是**纯 RDS 作业，不碰检索后端/OSS**"、依赖里**不装 oss2**。
- ⇒ `DRY_RUN` 现为 `True`（阶段 1，不走归档路径，所以观察期全绿）；**一旦翻到阶段 2
  （`DRY_RUN=False`），`qa_rows`（问答流水）与 `audit`（特权操作审计）两张最关键的治理表
  每天在第一批就抛"OSS 不可用……拒绝删除"，节点 exit 3**。两张表永不清理。

### 1.2 ⚠️ 我不采纳研究文档的"拆成独立 stage"（codex 条件同意）

研究文档建议照 OpenText 把归档与删除拆成各自独立成败的 stage。**核查后不采纳**：

当前形状是 `select_rows → _archive_batch → act_by_ids(按刚归档的 id 删)`（`retention.py:268/277/280`），
**"删的是刚归档那批"这个性质，是同批耦合白送的**。拆开后 delete 作业必须知道"哪些 id 已归档"：
要么新建一张归档 id 台账（新持久状态 + 它自己的一致性问题），要么每次回查 OSS（**把 OSS 变成
删除路径的同步依赖**——恰恰是本条要摆脱的东西）。研究文档自己标了 ⚠️「拆开后必须保证 delete
只删已登记 id」，但没说这个"登记"存哪。

⇒ **本批不拆**。独立 stage 的收益（独立重试、削峰、审计）记录在案；**将来若拆，前置条件是
先有持久化 manifest/receipt，让 delete 只消费"已确认归档"的 id**。

### 1.3 🔴 但当前保证比我原先说的弱 —— 必须补一条（codex BLOCKER）

我原先表述为"删除集合**恒等于**归档集合"，**过强**。准确表述是：

> 成功删除的 id ⊆ 本批曾成功上传的行，**且以该 OSS 对象未被后续覆盖为前提**。

**漏洞**：归档 key = `archive/retention/{table}/{run_ts}/batch-{batch_no:04d}.jsonl.gz`，
而 `_run_ts = time.strftime("%Y%m%dT%H%M%S")` —— **秒级**。两个同秒启动的实例会向**同一个 key**
写不同批次，后写覆盖前写 ⇒ 已删除的数据**失去归档副本**，fail-closed 名存实亡。

| 项 | 内容 | 状态 |
|---|---|---|
| 🔴 待拍板（主修复） | 归档 key 改用**碰撞不可行的 run ID**（UUID / 数据库执行 ID）—— **这是主修复** | ☐ |
| 纵深防御（非替代） | 条件写入 / 禁止覆盖 —— 是**纵深**，不是主修复的替代 | ☐ |
| ⚠️ receipt 的边界 | **归档 receipt 不能单独替代不可覆盖性**：只有当它指向**不可变或内容寻址**对象时才构成保护；否则它只能**检测**覆盖，**不能恢复**已丢失的归档 | |
| 前置未知 | 生产调度**是否可能重入或并发**运行 retention？无全局 lease 的情况下**应按"可能并发"设计** | ☐ |
| ⚠️ 语义限定 | OSS 上传成功但 RDS 删除回滚时，重试会**重复归档**。这是可接受的 **at-least-once**，但**不得宣称 archive/delete 跨系统原子** | |

### 1.4 preflight 断言（作用域必须精确）

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | `run_retention()` 加 preflight：`RAG_RETENTION_ARCHIVE=true` 且 OSS 不可用 ⇒ **启动即失败**，不等阶段 2 每天在第一批爆 | ☐ |
| 🔴 作用域（codex BLOCKER，含 window 条件） | 精确谓词：<br>`archive_required = _archive_enabled() and any(j in _ARCHIVE_TABLES and windows[j] > 0 for j in jobs)`<br>即**三个条件同时成立**才 preflight：①本次选中了归档表（`qa_rows`/`audit`，6 个作业里只有这 2 个）②`archive=true` ③**该作业 retention window > 0**。只跑 `findings` 等非归档作业时**不得**因缺 OSS 失败 | ☐ |
| 🔴 为何必须带 window 条件 | `months <= 0` 是**合法 skip**（`retention.py:234`）。若漏掉该条件，`qa_rows window=0 + OSS 不可用` 会从应有的 `SKIPPED` **变成 preflight `FATAL`** —— 与本单 2.2 对 `SKIPPED` 的定义直接矛盾 | |
| 说明 | 不要求 `affected > 0` —— preflight 本就是**配置姿态检查**，不是数据量检查 | |
| 🔴 位置 | 必须在**全局 simulation/simulate-db 短路之后**；"dry-run 也执行"指的是**连真实 RDS 的 `commit=False` 预演**，**不是**推翻现有 simulation 纯跳过契约 | ☐ |
| ⚠️ 能力边界（不可夸大） | `_get_oss_bucket()` 能查配置/simulation 姿态/`oss2` 依赖，**但不写对象就不能证明 bucket 存在或账号有 `PutObject` 权限**。preflight **不是**端到端 OSS 可写性证明 | |
| 非生产出口 | **不设**通用"跳过 preflight 但保留 archive=true"的旁路。非生产应显式选 `RAG_RETENTION_ARCHIVE=false` 或只跑非归档作业；若确需 preview-only 旁路，须限定 `commit=False + 非生产` 并报告 `preflight_unverified` | ☐ |

### 1.5 🔴 DataWorks 节点二选一（Sam 拍板）

| 选项 | 内容 | 状态 |
|---|---|---|
| **A** | 装 `oss2` + 去掉 `RAG_SIMULATE_OSS=true` + **注入完整 OSS 配置**，打通删前归档。⚠️ `retention_node.py` **两处都要改**：依赖数组（第 **36/38** 行，按 py 版本分两支）与 `RAG_SIMULATE_OSS=true`（第 **65** 行） | ☐ |
| **B** | 显式设 `RAG_RETENTION_ARCHIVE=false`，**书面接受"放弃删前冷归档、不可逆直删"** | ☐ |
| 前置未知 | 生产 OSS 账号**是否实际具备 bucket 访问与 `PutObject` 权限**（未实测） | ☐ |
| ⚠️ 归属 | **B 是产品/合规决定，不能凭工程便利选**（两张表是问答流水与特权操作审计） | |
| 权威边界 | **库级 preflight 是权威校验**；node 启动断言只作"提前报错"，**不得复制出第二套判定** | |

---

## 2. C6 · 批处理状态模型

### 2.1 不照搬 OpenText 七分类（codex 同意）

OpenText 的 `Abort/Fatal/Fail/Requeue/Retry/Cancel/Complete` 里，`Cancel`（人工取消）、
`Abort`（整批中止）、`Retry` vs `Requeue` 的区分在本仓库**没有语义载体**——没有人工干预面、
没有跨日队列。

### 2.2 🔴 但我提的"四分类"不足 —— 采纳 codex 的**六分类**

我原提 `Complete/Capped/Fail/Fatal` 四类，**漏了仓库里已经真实存在的两个正交状态**：

- **`BLOCKED`**：锚点表因前序失败/capped 而**刻意不执行**，带 `blocked_by`（我上一批实现的依赖门）。
  它**不是 `FATAL`** —— 保护机制正常生效，上游解除后即可重跑。
- **`SKIPPED`**：全局 simulation、窗口关闭（`months=0`）、optional table 不存在（1146）**都是合法跳过**。
  归入 `COMPLETE` 会**谎报"已执行完成"**，归入失败类又是误报。

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | 落 **六分类**：`COMPLETE` / `SKIPPED` / `CAPPED` / `RETRYABLE_FAIL` / `FATAL` / `BLOCKED` | ☐ |
| 总体状态派生 | 由逐表状态派生，优先级 **`FATAL > RETRYABLE_FAIL > BLOCKED > CAPPED > COMPLETE/SKIPPED`** | ☐ |
| 🔴 末级混合规则（写死，不留歧义） | **全部 `SKIPPED` ⇒ 总体 `SKIPPED`**；**至少一个 `COMPLETE`、其余均 `SKIPPED` ⇒ 总体 `COMPLETE`** | ☐ |
| 待补（实施评审前） | **`RETRYABLE_FAIL` 判定表**：锁超时(1205)/死锁(1213)/临时网络错误 ⇒ retryable；凭据、权限、必需 schema 缺失、不变量破坏 ⇒ `FATAL`。**六态名称定了但没有这张表，分类实现仍会漂移** | ☐ |
| `FATAL` 的定义 | 需**人工处理**的配置错误、必需 schema 缺失、不变量破坏。⚠️ 因 FATAL 而未执行的下游项标 **`BLOCKED`**，两者不可混用 | |
| preview vs 真删 | 用独立 `mode=preview\|commit` 表示，**不要都叫 `COMPLETE`** 而丢掉操作语义 | ☐ |
| 退出码策略 | 定期 retention 允许 `CAPPED` 退 0 与否可单独定；**但见下方 2.3** | ☐ |

### 2.3 🔴 附带发现：`purge_subject` 的 capped 会谎报成功（codex MAJOR，已核验）

非锚点表（如 `user_feedback`）打满 `max_batches` 时，`rep["capped"]=True` 之后紧接 `rep["ok"]=True`，
而 `result["ok"]` 不受影响 ⇒ **整体报告 `ok=True`，但该主体的行还没删完**。

对**定期 retention** 这是可接受的（次日续跑）；对 **`purge_subject`（PIPL 主体擦除）不可接受**——
**只要任何必需表 capped，主体删除就没有完成，不应最终返回成功**。

| 项 | 内容 | 状态 |
|---|---|---|
| 待拍板 | `purge_subject` 的任一必需表 `CAPPED` ⇒ 总体**不得**返回成功（`ok=False` 或独立的 `incomplete` 标记） | ☐ |
| 说明 | 这是**上一批已落码范围之外的既有行为**（不是新引入），但与 C6 同族，一并提请拍板 | |

---

## 3. 建议落地顺序

1. **立刻（不需拍板，纯止血）**：`retention_node.py` 现为 `DRY_RUN=True`，**翻阶段 2 之前必须先解决 1.5 的 A/B**，否则第一天就爆。
2. **第 1 批**：1.3 归档 key 碰撞修复 + 1.4 preflight（作用域精确）。
3. **第 2 批**：2.2 六分类 + 2.3 `purge_subject` capped 语义。
4. **待法务**：C6 锚点墓碑化（PIPL 第 47 条）。

**回滚**：1.3/1.4 均为收紧（更容易失败、不会更容易删），无机密性回滚风险；2.2/2.3 改的是报告语义与退出码，需同步核对 DataWorks 节点对 exit code 的处理。

---

## 附：本单未核实项（不得当已知）

- 生产调度是否可能重入/并发跑 retention（决定 1.3 的紧迫性）
- 生产 OSS 账号是否具备 bucket 访问与 `PutObject` 权限（决定 1.5-A 可行性）
- Sam/法务是否接受 1.5-B 的不可逆直删
