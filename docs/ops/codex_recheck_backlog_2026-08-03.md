# codex 待补评审清单（2026-08-03 附录B 批次）

> **背景**：codex 额度于本批第 3 条中途用尽（恢复时间 **2026-08-07**）。前两条走完了完整
> codex 流程，**后四条没有**。本单是恢复后要补的评审队列，按风险排序。
>
> 依据：同一批里 codex 从我的方案中抓出 **9 处真错**（含两次「我把修复写成了回退」），
> 所以"我独立核验过"不能替代评审。

## A. 走完 codex 流程的（**不需**复核）

| commit | 内容 | codex 状态 |
|---|---|---|
| `1d65994` | funnel_policy 逐文档 + stage-2 loader 补键 | APPROVE（且 loader 那半是 codex **独立发现**的） |
| `d1a5ace` | spot-check 隔离探测器 | 方案经两轮 REVISE 收窄后落地；**栅栏**部分另见 `spot_quarantine_fencing_signoff_2026-08-03.md` |

## B. 未过评审的（按风险排序）

### B1 🔴 `e5e29ce` cosurface 补图复核 —— **最该先复核**
codex 完成了缺陷核查与修法建议，**但没看到实现 diff**。两个具体待裁决点：

1. **我刻意偏离了 codex 的建议**：它主张「缺 `version_no` 时该文档不补图」（fail-closed），
   我改成「两边都已知才比对，未知则放行交 4b/4c」。
   我的依据：`parse_ha3_response` 对 `version_no` 缺省是 `0`，而**全仓没有任何一处消费
   HA3 返回的 `version_no`**（retriever 零消费；`ha3_reconcile`/`ha3_verify` 在
   `HA3_PARITY_OUTPUT_FIELDS` 里要了却从不读）。`to_ha3_doc` 确实推了该字段，但
   「HA3 加字段必须重建表」⇒ **线上表有没有这一列，仓内零证据**。
   👉 **可现网一验即决**：对生产 HA3 取一条 image chunk，看返回是否含非零 `version_no`。
   有值 ⇒ 可收紧为 fail-closed；无值 ⇒ 我的偏离是对的，且说明该字段是死的。
2. `strict_on_unavailable` 只挡「复核**没跑成**」不挡「复核**按配置不跑**」
   （`main_hit_revalidate` 关 / `simulate_db`）这个切分是否正确；`top_k` ×2→×4 的成本面。

### B2 🟠 `a61fe87` doc-status 补传 publish_status/chunk_status
风险点：新放出的「已隔离/未入索引」徽章会让**此前显示「已上线」的存量文档改变外观**
（那正是修复目的，但运营侧会看到一批文档"状态突然变了"）。需确认：
- `_KB_BADGE_CASE_SQL`（徽章的 SQL 镜像，带 parity 测试）**未动**是否确实无需同步；
- doc-status 与 my-docs/browse 现在四处口径是否真的一致（我加了 parity 测试，但只覆盖 5 组取值）。

### B3 🟠 `d2c8e12` 分页 tiebreaker（5 处）
有真库实测（8/12 → 12/12）撑着，但需复核：
- `ORDER BY` 加列对**查询计划**的影响（我判断本就走 filesort，故可忽略——需 EXPLAIN 佐证）；
- tiebreaker **方向**（DESC/ASC 跟随主排序）是否会让某些既有深链/游标语义变化；
- `_KB_MAX_OFFSET=10000` 之外，是否该借机推 keyset 分页（我明确列为"未解决"）。

### B4 🟡 `da771d8` useDialog `_supersede` + useAsk `retry` 忙时守卫
- `_supersede` 用「取消」了结旧 Promise：是否存在**依赖旧框返回值**的调用方会因此走进
  意外分支（我判断取消是安全默认，但没有全量审调用点）；
- `retry` 忙时改为 no-op（+ 按钮禁用）：是否更应「停掉当前流再重试」——这是产品语义选择。

### B5 🟡 `c5c7d59` approve/reject 测试 + 注释修正
纯测试 + 注释，风险最低。仅需确认测试断言没有把**当前实现**当成**规范**固化
（例如 `approved=0 + note` 这个返回形状是否值得钉死）。

### B6 🟢 `fecf060` 监控假绿（探针失败 ⇒ exit 3）
本条其实在额度用尽**之前**，但也没单独过评审。风险：**已部署的定时任务可能开始变红**——
那是本意（此前是假绿），但 Sam 应先知道再让它触发。

### B7 🟡 `a4f6e37`+`b8e11b4` P2-11 分页（我的贡献 / 复审任务）——**已自行量化，降级**
原记「OFFSET vs keyset 不确定」。**已用真库量清楚，不再是开放问题**：
- **默认视图零漏，且是精确自洽**：`ORDER BY created_at ASC` + 处置即**本地移除** +
  offset 传 `.length` ⇒ 本地条数与服务端前缀同步收缩。实测处置 0~4 条第 2 页恒为 T05..T08。
  ⚠️ 这条正确性**依赖前端契约**（offset=本地条数、处置即移除），不是 OFFSET 本身的性质
  —— 谁改了 `resolveReviewTask` 的本地移除，或把 offset 改成"累计已加载数"，就会破。
- **`include_closed` 视图每处置 1 条漏 1 条**（首排序键 `(open_pred) DESC` 是可变谓词，
  处置让行跨组跳走而本地不移除）。`b8e11b4` 已把该分支的翻页收掉、改为如实说明截断。
- **仍待裁决（降为设计题）**：是否把 include_closed 也做对——改排序去掉 open-first 分组，
  或对该分支上 keyset。属 UX + 设计变更，该走 ui-iterate + codex。
- 三个已踩过的坑已修并有测试（ORDER BY 补 `t.task_id`、`offset` 进 cache key、`&` 拼接），
  但请复核 `_dashboard_cache` 与分页的整体交互——缓存 TTL 内翻页看到的是不同时刻的快照。

### B8 🔴 差评复核分页 —— **本条没做，需设计裁决**
`kb_feedback_review` 是**两层静默截断**：SQL 硬 `LIMIT 300` 扫原始行 → Python 按
`message_id` 去重聚合 → 凑满 `limit`(20) 就不再收新消息。两层都不对外暴露。
⇒ 管理员看到的"差评就这些"可能只是全量的一小部分，**而且无从知道**。
直接加 `OFFSET` **不成立**：SQL 的 offset 作用在原始 join 行上，与去重后的条目不对齐
（同一 message 有多条 cited-doc 行），会漏消息/重消息。
可选方案：(a) `SELECT DISTINCT message_id ... LIMIT/OFFSET` 子查询再补 docs；
(b) keyset by `(created_at, message_id)`；(c) 先只暴露 `has_more`/`truncated` 让截断不再静默。
👉 **在拍板前，(c) 是零风险的最小改进**（符合本仓"no silent caps"纪律），但也需确认
UI 该显示什么（能"加载更多"才给按钮，否则给一句"结果已截断，请收窄时间窗/筛选"）。

## C. 复核时要一并回答的横向问题

1. **B1 的 `version_no` 现网一验**（上面已给方法）——一个查询就能定案，优先做。
2. 这批共 8 条修复，是否有**互相干扰**的面？我判断没有（分属摄取 provenance / 隔离 /
   检索补图 / 控制台读路径 / 前端状态机），但没做交叉分析。
3. **xdist flake 家族已确认不止一员**，值得单独立项（属 xdist-ontology-flake 同族）：
   - `test_stream_gate::test_flag_off_refusal_passthrough_unchanged`（三跑红一次）
   - `test_miniapp_serving::test_token_tamper_and_garbage`（2026-08-03 新见）
   共同特征：**单独跑必绿、全量并行跑偶发红、干净树亦复现**。每次只是确认"与本改动无因果"，
   根因（跨测试模块互踩 / 模块级状态未隔离）始终没查。它正在稀释 `make test` 的信号价值——
   现在每次全量红都要先花一轮排除 flake。

## D. 状态

- 附录B「值得优先自查的 7 条」——**全部完成**（其余 43 条正文没给，在工作流原始输出里，
  仓内无法获取）。
- P0/P1 里仍**待 Sam 拍板**：C9（业务裁决）、C5（合规二选一）、C4（需先查生产钉钉端点）。
- P2-11 **2/3 完成**（我的贡献 + 复审任务，`a4f6e37`）；差评复核见 **B8**（需设计裁决）。
- P2-12 / P2-13 / P2-14 / P2-15 **全部完成**（`200f850`、`c5c7d59`）。
- ⇒ **P2 批次除 B8（差评复核分页）外全清**；B8 后于 `ba338c1` 完成。
- **P3（设计债 4 项）2026-08-04 全清**：
  - P3-1 kb_admin 鉴权收敛为唯一实现 + 双向守卫 —— `c3c4101`
  - P3-2 下线 `POST /api/search`（默认 404；开回来也过敏感 guard）—— `eb1162f`
  - P3-3 六个硬 LIMIT 队列的截断改为如实告知 —— `4f8d895`
  - P3-4 决策端点形态 —— **审计后判定不改**（0 守卫缺口，改名纯破坏性），
    约定与理由见 `docs/ops/decision_endpoint_shapes_2026-08-04.md`
- ⚠️ 这 4 条**均未过 codex**，与本文件 §B 的 6 条一并等 2026-08-07 额度恢复。
