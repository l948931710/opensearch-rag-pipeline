# 决策类端点形态盘点与约定（P3-4，2026-08-04）

> 结论先行：**不改现有 URL**。本单是「审计结果 + 新端点的约定」，不是重构方案。
> 触发：2026-08-03 ultra 评审 P3「决策动作 REST 形态不统一」。

## 1. 先查有没有实质缺陷（有则先修，没有才谈形态）

「决策动作」的实质风险是**重复提交/并发决策**：没有前态守卫时，双击 = 双次生效
（例如把一条已驳回的申请再"通过"一次）。逐个核过 17 个决策端点：

| 端点 | 互斥/前态机制 |
|---|---|
| `/api/kb/approve` · `/reject` | `FOR UPDATE` + 前态谓词 + env_guard |
| `/api/kb/retire` · `/restore` | `FOR UPDATE` + 前态谓词 + env_guard |
| `/api/kb/feedback-review/resolve` · `/review-tasks/resolve` | 前态谓词 + env_guard |
| `/api/kb/access-requests/approve` · `/reject` · `/revoke` | 委托 `_kb_access_decide` ⇒ `FOR UPDATE` + `from_status` |
| `/api/kb/admin-node-candidates/decide` | `FOR UPDATE` + env_guard |
| `/api/kb/contributions/{cid}/accept` · `/reject` · `/retry-ingestion` | `FOR UPDATE` + 前态谓词 |
| `/api/kb/admin-grants/revoke` | 谓词式幂等：`SET is_active=0 … WHERE is_active=1` |
| `/api/kb/gaps/dismiss` | 谓词式幂等：`INSERT … ON DUPLICATE KEY UPDATE`（last action wins，有意） |
| `/api/kb/gaps/restore` | 谓词式幂等：`UPDATE … WHERE revoked_at IS NULL` |

⇒ **0 缺口**。全部要么持行锁 + 显式前态，要么把前态写进 `WHERE`（同样安全，且天然幂等）。

⚠️ 方法论留痕：初筛用「端点装饰器之间的代码块里有没有 `FOR UPDATE`/前态谓词」，
把 `access-requests/approve|reject` 报成「无锁无前态」——**假阳性**，它们是三行委托，
守卫在被委托的 `_kb_access_decide` 里。**别据此写静态守卫测试**：委托一层就漏报，
再包一层就误报。这一层只能靠人读 + 本单记录。

## 2. 现存的四种形态

| 形态 | 例子 | 计数 |
|---|---|---|
| (a) 根级动词，id 在 body | `POST /api/kb/approve` · `/reject` · `/retire` · `/restore` | 4 |
| (b) `资源/动词`，id 在 body | `/api/kb/access-requests/approve` · `/review-tasks/resolve` · `/gaps/dismiss` | 7 |
| (c) `资源/{id}/动词` | `/api/kb/contributions/{cid}/accept` · `/reject` | 3 |
| (d) `资源/decide` + body 里 `action` | `/api/kb/admin-node-candidates/decide` | 1 |

最扎眼的是 (a)：`/api/kb/approve` **没说批的是什么**。它批的是「上传的新版本」，
而 `/api/kb/access-requests/approve` 批的是「跨部门授权申请」——两个域共用一个动词，
根级那个还占了通名。

## 3. 为什么不改

- **纯命名收益，零功能收益**：四种形态都能正确表达决策，第 1 节已证守卫齐备。
- **破坏性**：URL 是发布接口。console 好办（同仓同发），但**钉钉小程序是已发布的
  独立客户端**，其版本节奏与后端不同步 ⇒ 改名要么留双写别名（把 4 种形态变成 8 个 URL，
  比现状更糟），要么承担一段时间的客户端 404。
- **回归面**：这些是审批/授权/退役路径，全是权限与状态机代码。为了美观去动它们，
  风险收益比是负的。

## 4. 约定（对**新增**端点生效）

新的决策端点一律用 **(c) `资源/{id}/动词`**：

    POST /api/kb/<资源复数>/{id}/<动词>

理由：id 在路径上 ⇒ 审计日志与网关访问日志天然带对象标识（body 不会进访问日志）；
动词独立成路径 ⇒ 每个动作可单独做限频与鉴权，不必解 body 才知道要做什么
（(d) 的 `action` 字段就必须解 body 才能判断）。

不为此改动既有端点。若将来某个既有端点**本来就要**破坏性改造（换语义、换请求体），
顺手迁到 (c) —— 但那要单独拍板，不属于本单。

## 5. 与本批其它单的关系

- 队列**读**侧的截断问题（同一批端点）已在 P3-3 修完，见 `4f8d895`。
- kb_admin 鉴权的多套实现已收敛，见 `c3c4101`（那个是真缺陷：多套判定=漂移即越权；
  本单的多种 URL 形态**不是**同类问题，别混为一谈）。
