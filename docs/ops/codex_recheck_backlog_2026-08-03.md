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
   - `test_reconcile::test_scan_ha3_pks_bucket_clamped_and_half_open`（**2026-08-04 新见**：
     全量并行一次红；随后 5 次全绿——2 次带同一改动 + 3 次干净树。**不归因、未解决**）
   共同特征：**单独跑必绿、全量并行跑偶发红、干净树亦复现**。每次只是确认"与本改动无因果"，
   根因（跨测试模块互踩 / 模块级状态未隔离）始终没查。它正在稀释 `make test` 的信号价值——
   现在每次全量红都要先花一轮排除 flake。

## C-bis. 2026-08-04 自查（**不替代 codex**，但把"没人看过"降级为"我逐条查过并留了证据"）

Sam 要求「验证到 100% 确认为止」。逐条实测结论：

| 条 | 判定 | 证据 |
|---|---|---|
| **B1** | 🔴 **我错了，已修** | 我偏离 codex 的 fail-closed 建议，理由是「HA3 是否回填 version_no 无法验证」——**那条理由错的，证据一直在仓里**：`docs/ha3_stg_table_spec.md` 是生产表实时导出、含 `version_no INT64`；`stitch_neighbor_chunks` 自 `16eb40b` 起就拿它建 WHERE（不回该字段的话邻居拼接会静默全空）。改回 fail-closed（`f9e59ea`）。**顺带查出独立既存 bug**：本地 OS 回退路径读侧漏取 version_no ⇒ 该路径邻居拼接自 2026-06-28 起一直静默全空 |
| **B2** | ✅ 问一成立；问二有隐雷已拆 | 真库**全交叉积 20160 组 0 不一致**（`5566f1f`）。但 gate 轴此前只在 doc-status/版本历史的 `_is_q` 外挂里，SQL 镜像只看 publish ⇒ gate-only 隔离在列表侧会显「已上线」。**当前不可达**（三个 gate_status 写方里两个 quarantined 都与 publish 同 UPDATE），已两侧补齐 |
| **B3** | ✅ 结论对、理由曾错 | EXPLAIN 实测 5/5 零计划影响（`858f515`）。但 `/api/conversations` **改前就没走 filesort**（"本就 filesort"在那处不成立）。**新发现**：tiebreaker 方向承重 —— 逆向 ASC 会从 Backward index scan 掉成 PRIMARY + filesort。已加守卫 |
| **B4** | ✅ 无问题 | **全量审 19 个调用点**（7 promptText + 9 confirm + notice）：取消值一律落到中止分支。加守卫（`922c9f1`/`f32e255`）。retry 语义属产品取舍，留 Sam |
| **B5** | ✅ 不改 | 断言全是安全性质；唯一"精确形状"那条**必须保留** —— `/api/kb/approve` 无 `response_model`，它是唯一形状契约 |
| **B6** | ✅ 属实且已在跑；**顺带查出 C1 同族漏修** | launchd `com.fuling.ops-monitor` 上次退出码正是 3（根因 DNS，被正确归 error 而非 drift）。但**告警标题层**没跟上：探针失败顶着「parity drift」+critical 发。三处已修 + AST 守卫（`9cd9436`） |
| **B7** | ✅ 逐端点核过 | `_dashboard_cache` 7 个使用者里**只有 review-tasks 带 offset**；后果已写进调用点 + 守卫（`de119a1`） |
| **P3-1~4** | ✅ 守卫重跑反证仍咬 | 逐条把修复改坏 ⇒ 对应测试红；还原 ⇒ 绿 |

🔴 **需要 Sam 知道的现网状态**：ops-monitor 日志里 **24 条 CRITICAL 全部被抑制**
（`RAG_OPS_ALERT_WEBHOOK` 未配）。其中的 RDS↔HA3 drift 很可能是语料真空期的预期结果，
但**重灌开始前必须配好 webhook**，否则重灌期的真 drift 同样静默。

⚠️ 自查**不等于**外部评审：本次找到的 3 处真问题（B1 的错误依据、B2 的 gate 隐雷、
B6 的告警标题）都是**我自己写的代码**里的，同一个人复查同一批代码有系统性盲区。
08-07 仍须送 codex。

## C-ter. 2026-08-04 六员对抗性核验（codex 额度未复，Sam 授权的替代深核）

方法：6 个互相独立的全新上下文核验员（B1/B2/B3/B4B5B7B8/B6/交叉面），对抗性框架
（以推翻为目标）+ 变异测试（修复改回必红）+ 反锚定（结论落定后才准读 §C-bis 并报分歧）。
**总裁决：8 条修复的行为性主张全部立住、0 条被推翻**；但挖出 **12 处新缺陷/宣称失实**，
已全部修复并逐条过反证（5 个提交）：

| 修复提交 | 内容 |
|---|---|
| `cc7ceaf` | B6 族两缺陷：qa_rollup 探针失败误顶「SLO breach」标题（本机 14 条被抑制 CRITICAL 实证）；reconcile `complete=False` 无-error 家族误顶「drift」标题/占真 drift 去重槽（含 ok=True+enum_invisible 的 07-21 历史形态）→ 三态分流（missing_confirmed>0 例外仍按 drift）。AST 守卫闭三旁路（关键字/属性调用/命名域扩 qa_rollup），raw/ha3/qa_rollup 补双向行为测试 |
| `307f98d` | B2 族三尾巴：my-docs/browse/contribution **渲染侧**补 gate 轴（此前筛选认 gate、渲染不认，gate-only 行自相矛盾）；reset_for_rechunk 隔离行硬拒（掐断 gate-only 铸造残余链）；不可达守卫从 3 文件+单引号拓宽到全仓+引号两态 |
| `df4580a` | B3 族三尾巴：方向守卫查窗口内**全部** ORDER BY 臂（分支构造此前只查最后一臂，变异实证 22 绿）；UNIQUE_COLS 去 message_id（非唯一键）；kb_gaps 终排序补 hash tiebreaker（切片分页潜伏面） |
| `1b662ae` | B1 族三处守卫缺失补钉（整删 4b 曾 4181 全绿；strict 出口②③改回 fail-open 曾全绿）+ 订正靠巧合续绿的过期测试文档 |
| `f8334dc` | B8×视图交叉缺陷（**两名核验员独立撞出同一条**）：筛选视图丢截断标志→静默截断复活+误报横幅，已接线；「收窄时间窗」死胡同文案（端点无时间窗参数）改真实出路；「加载更多」双击重复追加补追加对追加闸（第一版全路径置忙被既有契约测试抓出，收窄后落地） |

（`fd516e3` 跟改 contribution 徽章桩 7 列——307f98d 落地时漏跑该测试面的补课。）

**§C-bis 需更正的陈述**（账本错、代码对/已修）：
- **B5 行已过时**：c5c7d59 原文有一条把「省略 version_no 放行全部 pending」钉成规范的断言，3 小时后被 a951b9b 以安全理由反转（其 docstring 自认"把漏洞写成了规范"）。「✅ 不改」判定不再成立——现树行为正确，本行是陈旧结论。
- **数字更正**：B4「19 个调用点」算术不可复现（实为 7 promptText + 9 confirm = 16，notice 27 处全 fire-and-forget）；B2「20160 组」不可复现（入库测试自始是六轴 105,840，a951b9b 后 129,360；"0 不一致"本身属实）；「三个写方」实为四个（register_new_files.py:434 + DDL 默认值）；被抑制 CRITICAL 全机 **38** 条（ops-monitor 24 + qa-rollup 14），非 24。
- **fecf060 提交信息**「此前这类失败会静默 exit 0」对 launchd 排程作业失实（`_job_exit` error→3 自初版即有；其修的两个作业不在任何调度里）。
- **守卫≠不会再犯**：各守卫的词法边界已在对应测试注释里写明（B4 反转门/跨行赋值不可见、B7 cache 守卫认"offset"字面量、徽章类文本守卫抓删除不抓等价改写）。

**核验后仍开着的**（未随手修，各有原因）：
- ✅ **B1 现网一验已封口**（2026-08-04，Sam 授权 prod-ro；`scratch/b1_versionno_probe_20260804.py`）：
  300 行 `fuling_operation.qa_session_log`（窗口 06-20..07-29，语料真空期前）共 4752 个
  retrieved-doc 条目——现代血缘格式下 **4699/4699 = 100% 非零 version_no、0 个零值、
  0 个与 chunk_id 内嵌版本不匹配**。53 个 None 全部落在 06-20 01:09-01:52 一个 44 分钟
  窗口、键形态为旧序列化（连 chunk_id 都没有）= c605761 血缘字段上线**前**的旧进程残影，
  与 HA3 回填无关。⇒ HA3 回填 version_no 坐实，fail-closed 依据从「仓内 spec 文档间接
  证明」升级为现网实证。
- chunk_active 轴分叉（doc-status 传真值、其余四处传 None，升版残留 SUCCESS 场景两端点同行不同徽章；锁测试 counts=(5,5,5) 测不到）——修向需裁决（哪边语义为准），记设计题。
- 主命中图无版本轴（图直接命中 top-k 时 4c 不比版本，双活窗口可投旧版图）——在 B1 两 commit 声明范围外，属独立改进项。
- B7 include_closed 做对（keyset/去 open-first）仍属设计变更；后端 include_closed 分支仍收 offset（前端已收口）。
- SIM 模式 mock 行不带 version_no 时补图全拦（仅影响演示观感）。

**08-07 codex 交接物**：本节 + 六份核验报告结论 + 上表 5 提交的 diff。codex 复核重点可收窄到：三态分流的语义边界（incomplete vs drift 的判据）、渲染侧 gate 轴的列序改动、两个在途闸的收窄语义。

## E. 2026-08-06 codex 补评审第一轮（额度已恢复）

范围按 §C-ter 自己写的交接重点收窄：**C-ter 五提交的三个语义问题 + P3 四项**（后者从未过任何评审）。
codex 裁决 **REVISE**：1 BLOCKER + 5 MAJOR。我方逐条核验 **7/7 属实、0 推翻**
（上一轮我推翻过 codex 一条，本轮没有）。

### 通过的
- `307f98d` gate 列序：三处 unpack 与 SQL 列**均对齐**（my-docs 13/14/15、browse 12/13/14、contribution 6）
- `c3c4101` P3-1 鉴权收敛：**没有变松**，事务内 helper 与旧内联判断等价
- `cc7ceaf` 三态分流本身正确：已确认的 recall drift 不会被误分到 incomplete

### 已修（`3075012`，四条不需业务裁决的）
| | 缺陷 |
|---|---|
| 🔴 | `/api/search` 调 `search_chunks` **不传 `acl_ctx`** ⇒ node-ACL 读侧 fail-closed 复核整段被跳过（该复核门控在 `acl_ctx is not None`）。默认 404 不可达，但它是文档化的 break-glass 开关 |
| 🔴 | `eb1162f` 宣称「开回来也过敏感 guard」**只对一半**：guard 受它自己的 `RAG_SENSITIVE_QUERY_GUARD` 门控，默认关时恒 None。旧测试把 guard patch 成命中，从未覆盖真实配置组合 |
| 🟠 | `loadReviewTasks` / `loadMine` 的 **append-vs-replace 竞态**：忙闸只挡追加对追加，挡不住追加在途时列表被替换。正确范式在同文件 850 行外（`docsSeq`），只是没照抄 |
| 🟠 | P3-3 的第六个队列 `my-access-requests`：truncated 后端在回、前端在存、**全前端零消费** ⇒ 六队列实为五个 |
| 🟡 | `get_conversation`：`LIMIT 200` + 无条件 `has_more=False`，docstring 自称「全部消息」。不在 P3-3 的六队列定义内，当时漏网 |

### 待 Sam 拍板（未修）
1. **`/api/search` 去留**：保留为正式 break-glass（还需强制 guard + 审计）还是彻底删除。
   我倾向删——仓内 console / 小程序 / 钉钉 / eval 四类调用方都不依赖它。
2. **feedback / review-task 的并发语义**：两者更新**都没有前态谓词**
   （`WHERE message_id=%s AND feedback_type='downvote'` / `WHERE task_id=%s`）⇒ 静默
   last-writer-wins。⚠️ **`decision_endpoint_shapes_2026-08-04.md` 宣称的「0 守卫缺口」
   不成立**；仓里只对 gaps 做过 last-action-wins 的明确裁决，这两处没有。
   另 `/api/kb/admin-grants` 完全漏审（跨 `user_role` + 两张 grant 表写入，前置读无 `FOR UPDATE`）。
3. **HA3 方向二（stale/zombie）恒绿**：`ok = not rds_active_missing and not vanished_docs`。
   设计上有意，但 docstring 只论了**召回**（"harmless to recall"）、没论**机密性** ——
   已退役文档的 chunk 残留在 HA3 是「本该消失的内容仍可检索」，不是清理滞后。

### §C-ter 自认开放项**不完整**
上述五条它一项都没列到。这印证了 §C-bis 自己写的：同一个人复查同一批代码有系统性盲区。

### 下一轮优先级（codex 给出、我方认同）
`e5e29ce` cosurface 补图 —— 图片是最高 PII 风险模态，涉及版本 / 物理 PK / ACL / TOCTOU 四轴。
其后依次：`a61fe87`（徽章语义，chunk_active 轴仍在 doc-status 与列表间分叉）、
`d2c8e12`（五处分页依赖全序与方向）、`a4f6e37`/`b8e11b4`（分页状态机，本轮竞态的根表面）。

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
