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

### B7 ✅ `a4f6e37`+`b8e11b4` P2-11 分页（我的贡献 / 复审任务）——**2026-08-06 补评审收官，见 §G**
⚠️ 下面这段是 2026-08-04 的自查结论，**已被 §G 的双盲对照推翻一半**：
「默认视图零漏、精确自洽」只在**没有任何东西在飞**时成立；追加在途时处置本页一条即漏行
（代跑实证 T20 消失）。原文保留作为「同一个人复查同一批代码有系统性盲区」的又一例证。

原记「OFFSET vs keyset 不确定」。~~已用真库量清楚，不再是开放问题~~：
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
| **B3** | 🔴 **本行已被 2026-08-06 实测推翻，见 §F** | ~~EXPLAIN 实测 5/5 零计划影响；tiebreaker 方向承重~~ —— 那次是**一次性人工跑**，仓里当时没有任何可执行 EXPLAIN 测试。补上可执行测试后结论反转，详见 §F |
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

### Sam 已拍板 → 已落地（`05385ab`，2026-08-06）
1. ~~**`/api/search` 去留**~~ ⇒ **彻底删除**（连同 `SearchRequest`/`SearchResponse`/
   `RAG_SEARCH_ENDPOINT_ENABLE` 总闸；守卫改向断言路由表无该 path）。
2. ~~**feedback / review-task 的并发语义**~~ ⇒ 两处 UPDATE **加前态谓词** + 0 影响行分流
   404/409，前端 `uploadErrText` 补 409 直出服务端文案。
   ⚠️ `decision_endpoint_shapes_2026-08-04.md` 宣称的「0 守卫缺口」**已确认失实**。
   `/api/kb/admin-grants` 漏审**仍开着**（跨 `user_role` + 两张 grant 表写入，前置读无
   `FOR UPDATE`）—— 未随本批处理。
3. ~~**HA3 方向二（stale/zombie）恒绿**~~ ⇒ 按子类**分级告警**（`rds_inactive` /
   `orphan_chunkid` 计入，`dup` 不计），独立标题 + 独立去重槽；**有意不动 `ok` 与退出码**
   （那属需 Sam 先知情的部署面变化，B6 同族）。

### 第四项：图片「当前版本」不变量 —— 已实施（2026-08-06，未提交）

Sam 拍板四项里范围最大的一条，`05385ab` 当时显式留作单独一批。核心是新增
`_deny_stale_version_images` + `_resolve_serving_versions`（`retriever.py`），
在 **4 个消费点**生效（主命中 4d / 本地 OS 回退 / expand 后 / cosurface 内），
`_probe_pool_image_refs` 共用同一权威解析。

✅ **权威值已回签**（Sam 2026-08-06：「图必须是当前版本就行」）——
即**拍板的是意图、不是机制**：原字面拍板「以 `document_meta.current_version_no` 为准」
被 codex 补评审证伪后，按下述口径实现，意图不变。实测证据：
- `routes/kb_console.py:3072` 在**上传登记的同一个事务里**就把指针推到 N+1（摄取一步没跑）；
  `asset_set_diff.py:185` 仓内自证「上一成功服务版本**不是** current_version_no 指针」
  ⇒ 待审批 / DAG 处理中 / 索引失败期间会**正文照常、图片全没**，可能持续数天。
- 退一步的 `MAX(version_no) WHERE is_active=1` 同样被证伪：新 chunk 在 **DAG2** 就以
  `is_active=1 + NOT_INDEXED` 落库（`chunker.py:154`），只是把回归从「上传后」挪到「DAG2 后」。

**实际采用**：该 doc 最高的「**完整 INDEXED**」active 版本；无完整版本但**只有一个** active
版本时降级取它（ACL/标题/可见性投影会把**正在服务的那一版**标成 `NOT_INDEXED` ——
`access_grants.py:507`、`kb_console.py:3782` —— 所以「无完整版本」是**正常投影更新会
产生的合法过渡态**，不是异常，也不等于生产中长期普遍存在）；
**多个** active 版本且都不完整 ⇒ 真歧义 ⇒ fail-closed。
与本仓既有惯用法同源（`spot_checker.reconcile_stranded_versions`、stage-3 停用闸）。

#### 上线前置项①：生产 `index_status` 分布 —— **已跑（2026-08-06，Sam 点名 prod-ro）**

探针：`scratch/serving_version_distribution_probe_20260806.py`（全程只读，
`get_prod_readonly_conn()` 会话级 READ ONLY；**直接调用 `_resolve_serving_versions` 本体**
而非另写等价 SQL，否则分桶结论就不是关于将上线那段代码的证据）。

⚠️ **首轮结论作废**：现网正处**语料真空期**，`chunk_meta` 只剩 **6 行 `is_active=1`**
（`is_active=0` 有 63,882 行，document_meta 3,416 篇）。对 active 面的分桶只有 1 篇 doc、
0 张图，「✅ 不会造成图消失」是**空转结论**，不足为据。改用退役 population 作**代理**测量：

| 测量 | 结果 |
|---|---|
| 全量 63,888 行 `index_status` | INDEXED 29,403 (46.0%) / NOT_INDEXED 24,425 (38.2%) / DELETED 10,060 (15.8%) —— **无 NULL、无 FAILED** |
| 版本组（doc×version，2,074 组）完整性 | 完整 1,547 (74.6%) / 不完整 527 (25.4%) |
| **每篇 doc 的最新版本组** | **完整 1,544 / 1,557 = 99.17%**；不完整仅 **13 篇 (0.83%)** |
| 「不完整」的成因构成 | 旧版：DELETED ×460 + NOT_INDEXED ×54；最新版：NOT_INDEXED ×9 + DELETED ×4 |
| 13 篇最新版不完整中**带图**的 | **8 篇**（FINANCE 1 / MARKETING 4 / PRODUCTION 2 / QUALITY 1） |

**判读**：不完整绝大多数（460/527 ≈ 87%）是**旧版退役产生的 DELETED**，那些行 `is_active=0`、
不参与 serving ⇒ 无影响。真正相关的是最新版不完整的 13 篇（其中 8 篇带图）——
只要它们重灌后**只有一个 active 版本**，就落进「单版本降级」⇒ **图照常服务**；
只有在「两个 active 版本且都不完整」的瞬时窗口才会 fail-closed。
codex 担心的 **NULL 语义在本语料里不存在**（0 行 NULL、0 行 FAILED）。

🔴 **仍须在语料重灌完成后复跑本探针**：以上是退役 population 的代理测量，
不能替代对真实 active 面的核验。重灌后 active 面成形，再跑一次即可定案。

#### 上线前置项②：生产等量 `EXPLAIN` + 实测 —— **已跑（2026-08-06）**

计划形态：外层 `type=ALL` + `Using temporary`；内层 `type=index key=idx_doc_version
rows=57,410 filtered=8.11` —— 即**优化器没有用 `doc_id IN` 去 range-seek，而是扫索引**。
现有 `idx_doc_active(doc_id,is_active)` / `idx_doc_version(doc_id,version_no)` 都不覆盖
`index_status`，确如 codex 所料。

实测（本机 → 杭州 RDS，**基线 RTT `SELECT 1` = 188–194 ms，必须先剥掉**）：

| 样本 | 端到端 | **净成本（扣 RTT）** |
|---|---|---|
| 典型池（中位规模 20 篇；中位 16 chunk/篇、P90 54） | 193–206 ms | **≈ 0–15 ms** |
| 最坏池（chunk 数最大的 20 篇，含 11,818 / 9,195 / 1,649） | 353–415 ms | ≈ 160–220 ms |

**判读**：典型候选池的净成本落在 RTT 噪声内，**不构成上线阻塞**；真正吃成本的是那几篇
万级 chunk 的巨型 doc（属离群值，中位数只有 16）。SAE 与 RDS 同 VPC，真实 RTT ~1ms，
端到端会远好于上表。
⇒ **暂不加覆盖索引** `(doc_id,is_active,version_no,index_status)`，但把它记为
「若重灌后 active 面规模显著大于 63k、或巨型 doc 进入常规候选池，则重新评估」的待办。
（⚠️ 本行数字来自退役 population 的代理基数；重灌后应随①一并复跑。）

**已知残留**（均经 codex 确认「不使本批失效」，各自独立立项）：
- gate→rerank 的窄 TOCTOU（过门时是 serving 版本、随后新版切服，仍可能签名外发）；
- HA3 `source_image` 的**字段级**投影漂移（本门只验 chunk 身份属于 serving 版本）；
- 本地 OpenSearch 回退的字符串 `id` vs 数字 `chunk_meta.id`（**既存**缺陷，在 4c 的物理
  PK 轴上；版本门走 cid 轴不受影响）。

### §C-ter 自认开放项**不完整**
上述五条它一项都没列到。这印证了 §C-bis 自己写的：同一个人复查同一批代码有系统性盲区。

### 下一轮优先级（codex 给出、我方认同）
1. ~~`e5e29ce` cosurface 补图~~ ⇒ **已收官**：图片「当前版本」不变量，见上一小节。
2. ~~`a61fe87` 徽章语义~~ ⇒ **已收官**，见下一小节。
3. 仍开着：`d2c8e12`（五处分页依赖全序与方向）、`a4f6e37`/`b8e11b4`（分页状态机）。

### 第五项：徽章 `chunk_active` 轴分叉 —— 已实施（2026-08-06，未提交）

§C-ter 把它记成「哪边语义为准，需裁决」，**但事实是两边都错**。正常升版后旧版本是
`document_version.status='superseded'` + `index_status` **仍是 SUCCESS**
（`pipeline_nodes.py:7832` 注释自陈「只写 status、不动 index_status」）+ chunk 全 `is_active=0`：
- **doc-status** 传真实 `chunk_active=0` ⇒ 落穿「已上线」⇒「处理中」。错（没有东西在处理），
  且「处理中」非终态 ⇒ 前端 `trackStatus` 空转 22 次 ×8s；
- **version-history / my-docs / browse / contribution / SQL 镜像** ⇒「已上线」。也错（搜不到）。

而权威信号 `document_version.status='superseded'` 是管线亲手写的，**读侧全仓零消费**。

**Sam 拍板**：消费 superseded，新增徽章「历史版本」（放「内容不符」之后、「已上线」之前）。
**同时整个移除 `chunk_active` 轴** —— codex 逐条枚举了所有会停用 chunk 的生产写路径，
证明「当前版本 SUCCESS + 0 活跃 chunk 且逃过全部更早分支」不可达；留着它等于永久保留一处
`_KB_BADGE_CASE_SQL` **原理上无法表达**的分叉。doc-status 仍单独返回三个计数字段。
`_kb_status_badge` 的可选轴全改 **keyword-only**（contribution 是位置调用，删第 4 位后
`gate_status` 会撞 `TypeError` 又被其 fail-open 吞掉 ⇒ **贡献列表徽章整片静默消失**）。

**顺带闭合两条既存缺陷**（同一手工同步面）：「内容不符」是后端自陈的不可自动重试终态却不在
前端 `TERMINAL_BADGES`（空转到上限）；`_KB_BAD_BADGES` 前后端不一致（服务端"异常"筛选返回的行
被前端本地再排除、计数少算）。并补了此前**不存在**的跨层词表 parity 测试。

**`kb_restore` 一并修**：retired **不是终态**，而该端点只把当前版本置回 active ⇒ 恢复后
残留 `status='active'` 的旧版本落进「处理中」这个永不前进的假进行态。同事务加归一
（`version_no<%s AND status='active'`，CAS 保证不改写已 superseded / corpus_cleanup 的 inactive）。

**存量核查（prod-ro 只读）**：老版本 status 分布 superseded 979 / **active 94** / inactive 65。
那 94 行**全部** `dm.status='retired'` ⇒ 徽章第一条分支即命中「已退役」，**当前无用户可见缺口**、
**零 prod 写**；未来经 restore 复活时由新加的归一语句兜住。

验证：`make test` 4377 passed / `make lint` / `make sim-all` 全 exit=0；vitest 496；
vue-tsc + build exit=0；**变异 21/21 全红 0 存活**。codex APPROVE（四轮，2 BLOCKER + 4 MAJOR，
其中**四条是我测试里的假绿**：trackStatus 只验成员没验状态机、appending-column 行退化成恰好
等于基础列数、裸 `"status"` 被 `*_status` 子串命中、`shared_labels` 桩恒定空）。

⚠️ **记为独立项、本批不做**：`kb_restore` 目前也能把 `corpus_cleanup` 标为 `inactive` 的
语料卫生排除件"恢复上线"（入口只区分 active/非 active）。属独立的业务授权语义，
需 Sam 裁决是「只允许 retired 的逆操作」还是「明确 inactive 的重灌/校验流程」。

## D. 状态

- 🏁 **补评审队列已跑完**（2026-08-06）：§E 七条 + §F 分页 tiebreaker 族 + §G B7 分页状态机。
  剩下的全部是 **user-gated 待 Sam 裁决**的条目（§F 四项 + §G 两项），无待评审代码。
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


## F. 2026-08-06 codex 补评审：`d2c8e12` 分页 tiebreaker 族（B3/B7）

该族过了自查与六员对抗核验，但**从未过 codex**。补评审抓出 1 BLOCKER + 4 MAJOR，
并且**可执行 EXPLAIN 一跑就推翻了 §C-bis 的 B3 结论**。

### 🔴 已实施

1. **gaps 上游两个 `LIMIT 2000` 补全序**（BLOCKER）：`_compute_open_gaps` 里
   `ORDER BY q.created_at DESC` → `, q.id DESC`；`ORDER BY t.days_ago ASC` → `, t.rid DESC`。
   依据：`q.id` 是 `qa_session_log` 的 PK（`message_id` 只有普通 `idx_message_id`，
   DDL 注释「消息唯一ID」**不是**约束）；`t.rid = MAX(q.id)` 而 `GROUP BY q.message_id`
   分组不相交 ⇒ 各组 MAX 必不同。
   此前 created_at 秒精度 / days_ago 整天粒度，在 LIMIT 边界同值时两次重算可选出不同候选，
   而下游 hash 终排序**补不回上游已漏选**的行。
2. **守卫重构**：全局裸列名白名单 `UNIQUE_COLS` → **SITES 站点契约**
   （端点 / 唯一项 / DDL 依据 / 是否方向敏感 / 是否可词法扫）；唯一项**必须收尾**；
   `_parse_term` 严格单列（`id % 2`、`COALESCE(...)` 一律不认唯一）。
   ⚠️ 我原提的「检查全部相邻方向对」被 codex 拦下——会把 browse 的
   `owner_dept ASC, updated_at DESC, doc_id DESC` 这种**合法**混向误判。
3. **可执行 EXPLAIN 进 CI**：`test_pagination_stability.py` 加入 db-integration 清单；
   MySQL 镜像由浮动 `mysql:8.0` **钉到 `8.0.46`**（浮动 tag 会让断言因非代码变化而红）。
   同时删掉「无 tiebreaker 必须实际漏行」那条脆断言——SQL 只是不保证次序，
   不保证某个计划**必然**显形。

### 🔴 §C-bis 的 B3 结论被推翻（实测，MySQL 8.0.46 / FORCE INDEX / 2000 行 / ANALYZE）

| 索引 | ORDER BY | using_filesort |
|---|---|---|
| **生产现状** `(user_id, hidden_at, last_message_at)` | `last_message_at DESC`（仅前缀） | **false**（backward index scan） |
| 同上 | `…, conversation_id DESC`（同向） | **true** |
| 同上 | `…, conversation_id ASC`（混向） | **true** |
| 显式 4 列（含 `conversation_id`） | 同向 | **false**（backward index scan） |
| 同上 | 混向 | **true** |

⇒ 在**该查询形态 + 该索引 + FORCE INDEX** 下，MySQL 8.0.46 **未**利用隐含的
`conversation_id` 后缀来消除排序。故：
- 「EXPLAIN 实测 5/5 零计划影响」对 `/api/conversations` **失实** ——
  在现状 DDL 下，**加 tiebreaker 本身**（而非方向）就让该索引失去免排序能力；
- 「方向必须跟随」**只在索引显式含 tiebreaker 列时才有适用对象**。
  SITES 里 conversations 的 `direction_sensitive` 已置 **False**（该守卫现为 dormant）。

⚠️ 措辞边界（codex 要求）：以上只支持「该形态下不消除排序」，**不**支持
「MySQL 普遍不使用隐含 PK 后缀」，也**不**等于断言生产实际选定计划已经切换
（探针用了 FORCE INDEX，不测优化器自主选路）。
⚠️ 正确性仍需要 tiebreaker，**不得因 filesort 撤回**。

### 待 Sam 裁决 / user-gated

1. **扩 `idx_user_visible_recent` 到 `(user_id, hidden_at, last_message_at, conversation_id)`**
   —— 实测这样同向即可免排序。属 schema 变更，须走 `schema/` 文件 + `schema_migrations`
   台账（F-35 纪律）。做完后 conversations 的 `direction_sensitive` 可翻回 True。
2. **`_KB_MAX_OFFSET=10000` 的静默钳位**：现为 `offset=min(requested,10000)` + HTTP 200，
   请求 10001/20000 返回同一页窗口、更深行不可达、响应不回 `effective_offset`。
   与本仓 "no silent caps" 纪律相悖（P3-3 正是为此做的），但改响应形态要动前端。
3. **gaps 的 2000 硬 cap 是否允许不完整视图**（补全序只稳定「选哪 2000 条」，
   第 2001 条起仍永不可见）。
4. **OFFSET 活数据竞态**（覆盖全部分页端点）：翻页期间的增删改/权限变化仍会漏行重行，
   彻底解法是 keyset cursor + 快照水位，属架构变更。


## G. 2026-08-06 codex 补评审：B7 分页状态机（`a4f6e37` / `b8e11b4`）—— **队列最后一项，收官**

**这是补评审队列的最后一条。** 也是第一次用**新版双盲对照**流程跑（Sam 改过 skill：
双方独立产出、互不预览，再按同一套证据标准对照；Codex 的 VERDICT 是输入不是通行证）。
框架偏差已说明：新 skill 是**实施前的方案评审**，而本条是**已提交 diff 的补评审**，
只沿用其证据纪律（双盲落盘、代跑验证、裁决表、双向记账），不套 PR 话术。

### 双盲的产出差（这次它真的起作用了）

| 类别 | 条目 |
|---|---|
| **AGREED** | fail-open 把查询失败伪装成「没有更多 / 安全网干净」 |
| **CODEX-ONLY** | ① **追加在途 × 处置交错 ⇒ 静默漏行**（我完全没看到，见下）；② `_KB_MAX_OFFSET` 静默钳位在 review-tasks 上会造成**重复追加同一页**（我只报了「口径不一致」，没推到后果） |
| **CLAUDE-ONLY** | R1 空列表 + has_more ⇒「安全网干净」与「加载更多」并存（codex 只从 DB 异常那条路径看到「安全网干净」，没看到**正常处置**也能到）；R3 `mineHasMore` 失败后不重置；R6 截断说明在 fail-open 下双重静默；R7 `contributions/mine` 无 offset 上界；R8 组件测试触发未 mock 的真实网络 |

**记账：Codex 误报 0，Claude 误驳 1。** 我那条误驳是「10,000 cap 当前规模不可达」——
我从 ~1600 篇文档外推，但任务 id 是 **per (doc, version)**（`pipeline_nodes.py:2823`
`rev_{doc_id}_v{version_no}`、`cost_breaker.py:496`），`schema/001:305` 只约束 `uk_task_id`，
且 `review_task` **无 retention** ⇒ 随升版单调增长。撤回，改口径为「真缺陷、是否已越界未举证」。

### 代跑实证（预注册判定标准写在看结果之前）

1. **后端 fail-open**：monkeypatch `cursor.execute` 抛 1146 ⇒ `offset=0` 与 `offset=20`
   **都**返回 `items=0 / has_more=False`、无任何降级标志、HTTP 200。
2. **追加在途 × 处置交错**（vitest 探针，服务端模型 T00..T40 / 每页 20）：
   `GET=/api/kb/review-tasks?offset=20`，处置 T05 后返回页从 **T21** 起 ——
   **`含 T20=false`**，尾部 `…T18,T19,T21,T22…`。**T20 永远看不到。**
   ⚠️ 这是**同一管理员单人**就能触发的，不需要并发用户：点「加载更多」后立刻处置一条即可。
3. **空列表 × has_more**：`[P1] 安全网干净=true  加载更多按钮=true` —— 两条 v-if 链
   互不排斥，同时渲染。

### 修复（M1..M11，13 条变异全红 0 存活）

| # | 位置 | 内容 |
|---|---|---|
| M1/M4 | `kb_console.py` + `useKb.ts` | `KbReviewTasksResponse` 增 `degraded`；异常分支自陈降级。**不变量：`degraded=true ⇒ items/has_more 不是业务数据`**——追加 ⇒ 不追加/不覆盖 has_more/置横幅；替换 ⇒ **清空**列表与 has_more |
| M2 | `useKb.ts` | 列表基准代际 `reviewTasksBase`；检查顺序 **① seq → ② base**（seq 变直接丢弃**不重试**）；base 失配丢弃 + **恰好重试一次**；`base++` 以「该条在当前列表里」为条件 |
| M3 | `useKb.ts` | `offset > KB_MAX_OFFSET` ⇒ notice + 零请求。**只是 UI 缓解**，后端仍静默改写 |
| M5/M8 | `useContribute.ts` / `MyContributions.vue` | 替换失败时 `mineHasMore=false`；有 LoadError 时不说「还没有贡献」 |
| M6 | `ReviewTaskQueue.vue` | 空态四分支：degraded / 默认视图还有更多（**保留按钮**）/ closed 视图还有更多（只给截断说明）/ 真空才说「安全网干净」 |
| M10 | `useKb.ts` | LoadError 重试回到失败的那一页，且 **offset 数值现取本地条数**（复用旧数值会在「失败→处置→重试」时重新漏行） |
| M11 | `useKb.ts` | 视图标签 `reviewTasksView`：换视图失败 ⇒ 清空（否则旧视图数据挂在新标题下）；同视图刷新失败仍保留 |
| — | `contribution.py:715` | **M9 已删除**：原拟给 `mine` 加客户端上限，codex 指出会把原本可达的数据变为不可达（与 no silent caps 反向）。只留注释记录口径不对称 |

**HTTP 状态码没改**（我主动收窄）：原方案是 `offset>0` 查询失败抛 500。codex 指出仓外
`/api/kb/review-tasks` 消费者无证据可排除，而查网关日志属 prod-gated ⇒ 改状态码是拿不到
证据的赌注。改为 `200 + 纯增字段`（对旧客户端严格向后兼容的超集）。
补偿：两条**互不可伪造**的断言（后端真抛异常那条 + 前端喂 payload 那条），少任一条都能造假绿。

### 三轮里 codex 抓到而我没抓到的（方案/实施阶段，不是发现阶段）

1. M2 **必须先判 seq 再判 base**——否则切视图后会用**新视图的条数**去重取旧视图那一页。
2. M6 的「空页但还有更多」**必须限定默认视图**——无条件保留按钮会把 `b8e11b4` 刻意关掉的
   closed 视图翻页重新打开。
3. M1 的 degraded **替换路径必须清空列表**——toggle 先同步翻转标题，保留旧列表 =
   把上一个视图的数据挂在新标题下。
4. M10 **重试不得复用失败时的 offset 数值**——「失败 → 处置 → 重试」时 base 检查兜不住
   （重试是在处置之后发起的，捕获的就是新 base）。
5. 成功的追加**不复位 `degraded`** ⇒ 之后处置到空列表，空态会一直说「服务端查询失败」。

⚠️ 我的变异第一轮**有 2 条存活**，都是「测试写得假」而非实现有洞：一条变异只把 seq 检查
挪过了两行**注释**（语义没变，等于没施加），另一条断言正则 `offset=(19|20)&include_closed`
太窄、真出问题时发的是 `offset=1&include_closed=true`、照样全绿。已改为钉**总请求次数**。

### 门（全部显式回显退出码）

```
MAKE_TEST_EXIT=0    4382 passed, 2 skipped
LINT_EXIT=0         TYPECHECK_EXIT=0
VITEST_EXIT=0       512 passed (53 files)
BUILD_EXIT=0        E2E_EXIT=0   402 passed (3.3m)
变异 13/13 全红 0 存活（含后端那条，已清 __pycache__）
```
e2e 首跑有 1 红：`loading-states.spec.ts:127 G4 stats 在途`。判 timing flake，依据两条：
单独重跑 3×（3 视口 = 9 次）全绿；全量重跑 402/402 全绿。且该用例走 `/api/kb/stats`，
与本批改动无交集。

### 仍未解决 / 升级给 Sam（在 §F 四项之外新增两项）

5. **PROD-RO 计数**：`SELECT COUNT(*) FROM review_task WHERE review_status='PENDING'` ——
   决定 10,000 cap 是 latent 还是**已发生**的现网漏行。user-gated。
6. **仓外 `/api/kb/review-tasks?offset=` 调用者核查**（网关/SLS 访问日志）——
   决定 fail-open 该不该从 `200 + degraded` 升级为 **500**。user-gated。
   在拿到这个证据之前，`degraded` 只保护会读该字段的客户端。

并把新证据回灌 §F 第 4 项：**OFFSET 活数据竞态不再只是「并发用户」问题**——
同一管理员单人即可触发（M2 已堵住自己那一半，**他人处置仍漏**）。
默认视图排序 `created_at ASC, task_id ASC` 是全序，对**这一个端点**做 keyset 是局部可证的，
能一次性消灭 M2/M3 和「他人处置漏行」三条，并让 B7 记的三条前端契约不再是正确性前提。
属接口变更，与 §F 第 4 项同题 ⇒ 不单方面做，请 Sam 拍板是否上调优先级。
