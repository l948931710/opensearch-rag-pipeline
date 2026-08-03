# 全仓库 Ultra Code Review + Console 功能完整性分析(2026-08-03)

> 方法:多智能体分工评审(10 个子系统深度评审员 → 逐条对抗性验证 → console 六路完整性分析 → 后端/前端测试实跑)。
> 基线:`main` HEAD `6bce699`,评审分支 `claude/code-review-ultra-console-74ccdf`。
> 对抗验证口径:每条 critical/high 由独立怀疑者读完整调用链、查守卫/flag/测试后尝试推翻,存活者才进本报告(宁可漏报不误报)。

---

## 0. 结论速览

| 维度 | 结果 |
|---|---|
| 确认缺陷(critical/high) | **11 条**(原始 61 条发现去重后 11 条进对抗验证,**0 条被推翻**;3 条严重度经验证下调) |
| 待评 medium/low | 50 条(未过对抗验证,附录 B,建议按域自查) |
| 后端测试 | **3911 通过 / 0 失败 / 51 跳过**(裸环境补齐可选依赖后全绿) |
| 前端测试 | typecheck 0 错;**343 通过 / 2 失败**(DocTable 翻页器 2 例超时,系测试缺 router 注入,非功能缺陷) |
| Console 前后端契合 | 58 个 `/api/kb/*` 端点**全部有前端生产调用,零悬空调用**;缺口集中在"分页能力闲置 / 静默失败态 / 权限按钮未前置禁用" |
| Console 后端零测试端点 | 5 个(`approve`/`reject`/`whoami`/`doc-status`/`config`)——审批放行这条权限提升链**完全无测试** |

**最需要立刻看的 3 条**:
1. **C7(数据越权,原判 critical)**:legacy 上传用 `permission_level:"private"` 别名会绕过 kb_admin 审批,静默全员公开。
2. **C4(身份冒充越权)**:钉钉 webhook 签名只绑 timestamp 不绑 body,可伪造 `senderStaffId` 冒充他人读其部门文档。
3. **C0(数据静默丢失)**:DOCX 表格单元格内嵌图片(工厂 SOP 最常见版式)付费 OCR/VLM 处理后永不绑定 chunk,答案里静默消失且零告警。

---

## 1. 确认缺陷清单(11 条,已过对抗验证)

严重度标注:`原判 → 验证后`。`file:line` 为准确锚点。

### C7 · CRITICAL → HIGH · `routes/kb_console.py:2280`
**`permission_level` 别名 "private" 走 legacy upload-url 未归一化 → 绕过审批的静默全员公开**

legacy 上传分支把客户端原值 `perm = req.permission_level` 直接透传给 `build_raw_key` 和 upload token,全程不归一化。`authorize_upload` 只校验**别名归一后的副本**(`_PERMISSION_ALIAS` 把 `internal/private → dept_internal`),所以 `"private"` 被判 `dept_internal`、`requires_approval=False` 放行;但 `kb_upload._PERM_PATH_SEG` 只认 `dept_internal/internal/restricted`,`"private"` 查不到 → 生成**无权限段的扁平 raw_key**。系统权威设计是"可见范围由路径定",`resolve_permission_level` 对扁平路径返回 `public` 并在 stage-2 覆盖回写 `document_meta/chunk_meta`。node 上传、set-visibility、contribution accept 都做了 `{internal,private}→dept_internal` 归一,**唯独这条最常用的 legacy 分支漏掉**。
**触发**:dept_admin 调 `POST /api/kb/upload-url {action:"new", owner_dept:"finance", permission_level:"private"}` → 放行且免审批 → 落 `raw/finance/DOC/UL/f.pdf`(无 internal 段)→ stage-2 解析为 public 覆盖回写 → 本意仅本部门可见的文档**未经 kb_admin 审批对全公司可检索**,台账 badge 一路正常,无告警。
**验证**:调用链逐环成立,无上游守卫。与 2026-07-17 尾斜杠 P0(validate-one/use-another)同一机制根。

### C4 · HIGH → MEDIUM · `dingtalk_bot.py:521`
**Webhook 签名只绑 timestamp 不绑 body → 伪造 senderStaffId 冒充他人越权读取其部门文档**

`_verify_signature` 的 `string_to_sign = f"{timestamp}\n{app_secret}"`,HMAC 完全不覆盖 body。下游 `_process_claimed_body` 直接信任 `body["senderStaffId"]`,`_resolve_user_dept` 用它解析 ACL 组,`retriever._build_permission_filter` 用该组决定可检索 chunk。现有 `#F-dingtalk-ssrf` 注释只给 sessionWebhook 加了钉钉域名白名单,但**挡不住此攻击**:攻击者用自己与机器人 DM 的合法 `*.dingtalk.com` sessionWebhook 即可收到回投;msgId 去重也无效(body 由攻击者构造,换新 msgId 绕过)。
**触发**:300s 窗口内捕获任意一对合法 `(timestamp,sign)` → POST 伪造 body:`senderStaffId=财务部员工`、`sessionWebhook=攻击者自己的 DM 回投地址`、`text=敏感问题` → 机器人以受害者 finance 组身份检索 dept_internal 文档并回投到攻击者 DM。跨部门数据外泄 + 身份冒充。
**验证**:代码链逐环证实。降为 medium 的唯一理由是需先捕获一对窗口内合法签名(威胁模型代码注释自己也承认此前提)。**修复方向**:签名纳入 body 摘要,或对高敏检索强制二次身份校验(钉钉 API 反查 staffId ↔ 请求来源)。

### C0 · HIGH · `extraction/docx_extractor.py:537`
**DOCX 表格单元格内嵌图片不产 image_ref/inline asset → 混合版式步骤截图付费处理后永不绑定、静默消失**

`_walk` 对非 1×1 表格只走 else 分支产 markdown table block(`_cell_text_with_textboxes` 只抽文字),不扫描单元格内 `<w:drawing>/<w:pict>`,表内图既不产 image_ref 块也不计入 `inline_image_assets`。但 `extract_images_from_docx` 按 `document.part.rels` 会把它们全部导出;`_extract_docx` 对齐逻辑把它们当 leftover 追加、赋 `max(inline_index)+1` 的新 index——**这些 index 在 blocks 中无任何 image_ref 块对应**。下游 `_inject_image_ref_blocks` 因 blocks 已含 image_ref(正文任一内联图如 logo)走 enrich-only 路径,leftover 资产永不注入任何块;同时这些图照常过 OCR+VLM 付费漏斗、进 canonical assets,但没有 chunk 携带其 `image_refs`,serving 端永远渲染不出,`vlm_degraded_count/partial_loss_notes` 均为 0。
**触发**:典型中文工厂 SOP —— 正文一张 logo + 步骤放在"步骤|操作说明|操作示图"多行表格(截图嵌在示图列)。抽取后 blocks 只有 logo 一个 image_ref,N 张步骤截图成 leftover。step 模式下独立 image chunk 兜底三条件全 False(`RAG_IMG_CHUNK_FALLBACK_V2` 默认 OFF),N 张截图全部 serving-dead、付费白花、文档以 DONE 定稿、零可排查信号。
**验证**:六环节逐一坐实,tests 对表内图片零覆盖。唯一削弱因素(非 step 的 text/clause 文档会被兜底救成独立 image chunk)不适用于本仓库核心的 SOP step 场景。

### C1 · HIGH · `raw_inventory.py:66`
**`run_raw_inventory` 把 `_get_oss_bucket()` 二元组当 bucket 用 → 盘点探针在任何环境必然失败**

`clients._get_oss_bucket()` 所有返回路径都是二元组 `(bucket_or_None, is_simulated)`,而此处 `bucket = _get_oss_bucket()` 不解包:`if bucket is None` 恒 False(元组恒 truthy),`oss2.ObjectIterator(bucket, ...)` 拿到元组,迭代时 `tuple.list_objects` 抛 `AttributeError` → 被函数级 broad except 吞掉 → 恒返回 `ok=False`、四桶计数恒 0。**这个为"自助上传孤儿/未注册文件"建的唯一探针在真实和 simulate 环境下都 100% 产不出数据**。`tests/test_ingest_b_raw_inventory.py` 把 `_get_oss_bucket` 打桩成裸 `object()` 且整体 stub 掉 ObjectIterator,恰好掩盖 bug。次生:`ops_monitor._job_exit` 只认单数 `error` 键而本函数写 `errors` 列表,失败被归为 exit 2(drift)而非 3(error);`alert` 参数除签名外零使用。
**验证**:仓库其余 12 处调用方全部正确解包,唯此一处笔误。当前未接现网调度(现网只跑 `reconcile_ha3/reconcile_oss`),不影响生产数据面,但一旦按 ops_monitor 阶段 2 上线即失效。**修复即一行**:`bucket, _ = _get_oss_bucket()` + 处理 simulate。

### C3 · HIGH · `allowed_depts_reconcile.py:89`
**reconcile 预筛与 materialize 的 diff 口径漂移 → 零投影 node 文档与 owner 漂移被永久误判 unchanged**

`_prescreen_unchanged`(perf E#36)只比两维:legacy 权威 `resolve_allowed_depts` 的 want vs `chunk_meta.allowed_depts` 的 have。但唯一写实现 `materialize_doc_allowed_depts` 的 diff 已扩为三维:① node 文档的 want 来自 `kb_doc_node_grant` 投影的 `d:/dx:` 值;② `owner_dept` 投影轴必须一并比较(codex 阶段 B BLOCKER 修复)。预筛既不查 `acl_mode` 也不查 `owner_dept`,导致这条"authority↔投影漂移全扫兜底"对两类漂移完全失明:(a) 从未投影过的 node 文档(legacy want=[]、have=[])被判 unchanged 直接跳过,materialize 永不执行;(b) `allowed_depts` 一致但 chunk `owner_dept` 与应投影值不一致的文档。
**触发**:管理员勾选组织节点授权但 decide 端点 outbox 入队被降级吞掉(reconcile 自述要兜的场景)→ 下轮全扫,文档进 targets 但预筛判 unchanged 跳过 → `d:<id>` 永不落 chunk_meta/HA3,被授权部门员工永远搜不到;若 chunk 仍留真实 owner,旧 owner 组持续可读——**stale-owner 越权窗口由"过渡态"变"永久态",最后一道自愈防线失效**。
**验证**:`test_reconcile_covers_never_projected_node_doc` 之所以过,是桩 cursor 把预筛 JOIN 误路由到别的分支触发 `except: return set()` 整体放弃,并未覆盖真实预筛路径。

### C9 · HIGH · `routes/kb_console.py:3110`
**对尚未入库版本执行 set-visibility → 改动被 stage-2 按路径权威覆盖回写**

`kb_set_visibility` 只改 RDS(`document_meta/chunk_meta.permission_level` + 标脏重推),但 raw_key 权限段是 stage-2 的权威来源。对**已入库**文档没问题(stage-2 不再跑);对 `content_process_status` 仍为 `PENDING_APPROVAL/NOT_STARTED` 的版本,管线首次跑 stage-2 时会用上传时定死的路径段覆盖掉管理员刚改的级别。端点无"当前版本未走完管线"的守卫。
**触发**:public 文档 v1 进 PENDING_APPROVAL → kb_admin 先 set-visibility 收窄为 dept_internal(返回 `changed=true`)→ 队列放行 → stage-2 按扁平 public 路径覆盖回写 `permission_level='public'` → **管理员显式收窄的决定被静默还原,文档全员可检索,审计日志却留着一条 "public→dept_internal" 成功记录**。
**验证**:全链路逐环成立。与 C7 同源(路径即权威 vs RDS 意图不同步)。

### C8 · HIGH → MEDIUM · `routes/kb_console.py:2531`
**签名 PUT URL 在 register/审批后仍有效可复用,管线从不复核 ETag → 30 分钟窗口内可替换已登记/已审批文档实物(TOCTOU)**

register 用 `head_object` 在**当下时点**校验并落 `etag/size`,但签名 PUT URL 有效期是完整 `UPLOAD_TOKEN_TTL=30min` 且 OSS 预签 URL 可重复使用。全仓 grep 确认摄取路径(orchestrator/pipeline_nodes/dataworks_nodes)**无任何 ETag 复核**:stage-1/2 摄取的是拉取时点的字节,而非 register 时 HEAD 到的那份。审批人预览(`doc-preview`)也是即时签名 GET,预览与最终入库实物可不同。
**触发**:上传合规文件 A → register(或 public 场景 kb_admin 预览 A 后放行)→ 30 分钟内用同一 put_url 重新 PUT 任意字节 B(可超 register 校验过的 50MB 上限)→ DAG 摄取 B → **审批放行的内容 ≠ 实际入库/可检索内容**,`etag/file_size` 与实物永久失真,连带查重全失效。
**验证**:TOCTOU 无守卫成立。降 medium 因需 upload token 未过期 + 有效 put_url。**修复方向**:register 成功后作废 upload token / 摄取入口 `If-Match` 复核 register 落库的 etag。

### C5 · HIGH → MEDIUM · `retention.py:180`
**生产 retention 调度节点与删前归档 fail-closed 冲突 → qa_rows/audit 两作业在 commit 模式必然失败**

P3-18 删前冷归档默认开启(`_archive_enabled` 默认 true)且 fail-closed(`_get_oss_bucket()` 返回 sim/None 即 raise 拒删)。但 docstring 指定的调度器 `dataworks_nodes/retention_node.py:65` 显式设 `RAG_SIMULATE_OSS=true`、不装 oss2、也未按 blindspot 要求设 `RAG_RETENTION_ARCHIVE=false` → 归档路径必 raise。dry-run 阶段不走归档,观察期全绿,翻到阶段 2(`DRY_RUN=False`)才爆。
**触发**:上线阶段 2 → `qa_blobs/pipeline_run/findings/qa_facts` 正常删,但 `qa_rows`(问答流水)与 `audit`(特权操作审计)两张最关键治理表每天在第一批就抛"OSS 不可用……拒绝删除",节点 exit 3。两张表永不清理、DataWorks 每日报错;运维若为止血设 `RAG_RETENTION_ARCHIVE=false` 则静默放弃删前归档保证——**两个方向都是缺陷,该配置组合从未被真跑验证**。

### C6 · HIGH → MEDIUM · `retention.py:398`
**purge_subject 在 qa_retrieved_doc 删除失败/打满上限后仍继续删 qa_session_log → 制造永久不可定位的个人数据孤儿行**

`_purge_jobs` docstring 明确:`qa_retrieved_doc` 无 `user_id` 列,必须经 `qa_session_log.message_id` 关联删除且**必须先于** qa_session_log,"先删日志则事实行成永久孤儿"。但 `purge_subject` 主循环对单表失败/capped(打满 `max_batches`)都只记 error 后**继续删清单最后一项 qa_session_log**,销毁残余事实行唯一的 message_id 锚点。
**触发**:`--purge-user --commit` 时 `qa_retrieved_doc` 撞锁等待超时 1205/死锁 1213(与每日 retention 批删并发时完全现实)→ 该表 error,但循环继续删完 qa_session_log → 次日重跑 count 子查询返 0、报全表 ok —— **PIPL 擦除被报告为完成,而该主体的检索/引用轨迹永久留库且再也无法归属清除**,与本函数目的直接相悖。**修复方向**:qa_session_log 删除前置依赖门(前序关联表未全成功则不删锚点表)。

### C2 · HIGH → MEDIUM · `ha3_verify.py:334`
**ha3_verify self-query 被 expand_step_context 的 chunk_id 替换污染 → 产生假绿(missing 判成 present)**

命中判据含 `r.get('chunk_id') == c.get('chunk_id')`,而生产 `retrieve_and_enrich` 内部 `expand_step_context` 会把命中 step_card 的兄弟/子步骤从 **RDS** 拉出,并把展开行 `chunk_id` 覆写为兄弟的 chunk_id(`id/doc_id` 仍继承命中行)。于是一个 HA3 里根本不存在的 step_card,只要任一同家族兄弟被索引,self-query 就命中兄弟、展开出携带缺失卡 chunk_id 的 RDS 行 → 判 present。**探针把"从 RDS 展开取回"误当"HA3 可召回"**,正是模块自述要消灭的假绿("假绿比误报更坏")。展开行自带 `is_expanded/_stitched` 标记但探针未过滤。
**触发**:Stage-3 部分失败,某 SOP 12 张卡只 6 张进 HA3;核查跑 `verify_chunks_present` → 缺失卡文本命中相邻兄弟、展开出自身 RDS 行 → `missing_ids=[]`、`ok=True` → 修复被跳过,用户对这 6 步持续召回失败而报告全绿。
**验证**:机制成立。降 medium 因 `verify_chunks_present` 目前全仓无生产调用方(仅测试/脚本引用),是"埋着的雷"而非"正在爆的雷"。**修复即一行**:命中判据过滤 `not r.get('is_expanded')`。

### C10 · HIGH → MEDIUM · `fuling-rag-miniapp/utils/api.js:198`
**小程序 hot-questions 永不带 Bearer → 服务端动态"猜你想问"对小程序结构性失效**

`getHotQuestions()` 调 `request('/api/hot-questions', {})` 不带 `auth:true`,永不携带 Bearer。而服务端 `/api/hot-questions`(P2-7)规定:identity 为空/无部门 cohort 时只返回静态兜底,真实"近 30 天部门高频问题"只对已认证请求下发。**整条 P2-7 动态热门问题链路(部门 cohort 缓存、SWR 计算)对其主要消费方永久 inert,零报错零日志**。对比同批 `resignImages`(注释 P1-10 补了 `auth:true`),本接口漏改。
**修复即一行**:该请求加 `auth:true`(或登录完成后再拉)。

---

## 2. Console 功能完整性分析

### 2.1 前后端契合度:优秀

- **58 个 `/api/kb/*` 端点全部有 console-app 生产调用,零悬空调用**(前端所有 API 字面量逐一比对后端路由均命中)。
- **孤儿端点仅 1 个真孤儿**:`POST /api/search`(console/legacy/小程序/钉钉全无调用,仅 tests + rate_limiter 引用)——建议下线或标注内部调试。其余"孤儿"实为小程序专用(`/api/ask` 非流式、`/api/history`、`/api/session/clear`)或基础设施探针(`/api/version`、`/health`、`/ready`)。
- 核心业务流验证为**通**:上传→register→审批→放行→台账可见 ✓;授权申请→审批→撤销 ✓;贡献→采纳→入库→重试 ✓;隔离文档恢复(脱敏后传新版本)UI 有明示 ✓。

### 2.2 真正的缺口(按严重度)

**A. 分页能力闲置 —— 后端已就绪,前端不消费(数据不可达)**

| 流程 | 严重度 | 证据 |
|---|---|---|
| 差评复核队列 20 条即截断、无翻页,积压不可见 | **P1** | `kb_console.py:877`(limit=20)vs `useKb.ts:1145`(不传 limit、无翻页 UI) |
| 入库复审任务队列同款 20 条截断 | **P1** | `kb_console.py:1088` vs `useKb.ts:1213` |
| "我的贡献"第 51 条起永不可见(丢弃 has_more) | **P1** | `contribution.py:699` vs `useContribute.ts:206`(固定 limit=50,无"加载更多") |
| 会话详情固定 LIMIT 200 无翻页,超长会话回灌静默不全 | P2 | `api.py:2246` vs `useAsk.ts:746` |
| 缺口列表固定 30 条,offset 能力闲置(第 31+ 名无入口) | P3 | `contribution.py:1320` vs `useContribute.ts:174` |

> 这三条 P1 修复成本极低:前端 1 行 `qs` + 消费 `has_more`,后端已就绪。

**B. 静默失败态 —— 与本仓库自己的"显式降级"范式不一致**

| 缺口 | 严重度 | 证据 |
|---|---|---|
| HeroBoard 拉失败静默清空 → 整卡自隐,"加载失败"与"真无数据"不可分 | P2 | `useContribute.ts:238`(对比同文件 `noteLoadError` 范本 172/179) |
| 删除会话无确认框 + 服务端 DELETE 失败被 `.catch(()=>{})` 吞 → 换端登录会"复活" | P2 | `Sidebar.vue:45`、`useAsk.ts:509` |
| 管辖根候选队列加载失败静默空 → kb_admin 无法区分"拉失败"与"无候选" | P2 | `useKb.ts:1465` |
| 点赞失败乐观回滚无提示(点踩有兜底,点赞没有) | P3 | `useAsk.ts:557,563` |
| 图片重签失败无错误文案;VersionHistoryModal/OrgTreePicker 错误无重试按钮 | P3 | `useAsk.ts:591`、`VersionHistoryModal.vue:43`、`OrgTreePicker.vue:200` |
| 贡献归属部门下拉硬编码 10 组码 → 组织新增部门时前端漂移选不到 | P3 | `useContribute.ts:84` |

**C. 权限态 UI 未前置禁用(后端拦得住,无安全问题,是体验/一致性缺口)**

- dept_admin 管理的 public 文档,DocTable 行菜单仍显示"退役下线",点击后才吃后端 403。对比 ShareDocModal 已正确前置禁用 public 档、批量改可见范围也已限 kb_admin ——**退役/恢复按钮是漏网之鱼**。证据:`DocTable.vue:429-434` vs `kb_console.py:2894`,范式对照 `ShareDocModal.vue:73`。

### 2.3 鉴权/命名一致性(设计债,非缺陷)

- **同为 kb_admin-only 却两种实现**:一部分走 `_require_kb_admin`,`approve/reject/pending-approvals` 走 `_require_kb_console` + 手动 role 检查——行为等价、写法分叉,审计易漏。
- **决策动作四种 REST 形态**:入库审批 `POST /api/kb/approve`(无资源名);授权 `POST /access-requests/approve`;贡献 `POST /contributions/{cid}/accept`(用 accept 不用 approve);差评 `POST .../resolve` + body action。
- **分页三种姿态**:limit+offset+has_more / 仅 limit / 固定 LIMIT 不分页,混用。
- **同路径双语义**:`GET/POST /api/kb/access-requests`(待办 vs 提交)、`access-grants`(清单 vs 共享)、`doc-meta`(读 vs 写)——靠 method 区分,资源名不自释。

### 2.4 Legacy console 对比(`console.html` vs Vue 新版)

新版对 legacy 功能覆盖**基本完整**(登录双路/审批/上传三步流+批量/查重/轮询/我的文档/升版深链/?debug 均有 parity),且新增问答、知识贡献、五 tab 管理台三大业务面。真正退化只有微项:

1. 文件名查重提示丢失"一键改为升版"链接(legacy 有,新版纯文本)。
2. `?corpID`(大写)变体不再识别(新版只认 `corpId/corpid`)。
3. 轮询终态文案变弱(legacy "内容未变/已隔离/已驳回"各有专门文案,新版静默停轮询)。
4. 升版请求不再回传原文档标题(依赖后端"缺省保留原值")。

**行为不一致需注意**:`/console-legacy` 不走 token→fragment 重定向,继续用 legacy 即继续在访问日志里记 token(新版已抹除)。计数口径也不同(legacy 状态 chips 纯客户端只统计已加载 ≤50 条,>50 篇时必然偏小)。

### 2.5 测试覆盖矩阵

**后端 console 路由 59 端点,零覆盖 5 个**(最高风险):
1. `POST /api/kb/approve` —— 审批放行是 `PENDING_APPROVAL→NOT_STARTED` 唯一入口、决定公开文档能否入库,**权限提升路径零测试 = 最高风险**。
2. `POST /api/kb/reject` —— 完全零测试。
3. `GET /api/kb/whoami` —— 前端一切权限门(canManage、upload_target_depts)的单一数据源,无直测。
4. `GET /api/kb/doc-status` —— 上传后唯一进度反馈链路,跨部门查他人状态与轮询不终止都无守卫测试。
5. `GET /api/kb/config` —— 驱动前端 feature flag,坏了整页降级。

其余端点覆盖厚实:`test_kb_endpoints.py`(135 测)、`test_contribution.py`(88 测)、`test_kb_upload/register/retire/set_visibility/doc_meta/node_grants/admin_node_axis` 均有专测。

**前端**:33 个 vitest spec + 6 个 Playwright e2e(`ux-gate.spec.ts` 17 测三视口硬门)。**零覆盖关键流程**:上传成功全流程 e2e(upload-url→OSS PUT→register→轮询)、ApprovalQueue 审批通过/驳回、ReviewTaskQueue、`resolveFeedback`/`rejectAccess` 动作、Node-ACL 管理 UI(OrgTreePicker/saveNodeGrants)。

**Top 建议补测**(按风险):`approve`/`reject` 后端路由 → 审批 e2e → `doc-status`+trackStatus → 上传全流程 e2e → `whoami` 对齐断言。

### 2.6 规划文档对账(docs → 实现)

07-19 重设计规格(方案 B)**逐条落地质量高**(五 tab、队列分页、DocTable 三控件收敛、下载原件语义、Playwright 硬门均可在代码找到对应)。未落地项大多有明确归因(Agent/ontology 三件套留 `ontology-p0` 分支未迁 main;转人工 D5 因功能下线失效;#8 上传卡折叠被 07-19 终稿否决;E4 后端分页 redlines 有意出批)。

**真正"无归因悬空"的遗留 + 一处注释虚指**:
- ⚠️ **`ManageView.vue:116` 注释称审批角标走"60s pollQueues 三队列",但全仓无任何 setInterval 轮询实现**——E1 自动刷新实际未做,注释会误导维护者。当前只有 30s staleness **抑制重拉**门(与"自动刷新"相反)。
- ❌ **#5 "通过/授权"仍单击即生效无确认**——07-14 列为 α 批高价值一致性修复,至今未落(`ApprovalQueue.vue:86`、`AccessRequestQueue.vue:82`),与退役/批量/驳回全有确认不一致。
- 其余悬空:E3 文档 360 抽屉未做、E6 doc_date 新鲜度、D4 入库质量哨兵、D6 badcase 结构化归类、D7 retention 合规可见性、3.1-3 深链残留他 tab 参数、部门 chips 容器 div 残留 `aria-invalid`。

---

## 3. 测试健康

**后端**:`python -m pytest tests -q` → **3911 通过 / 0 失败 / 51 跳过**(约 38s)。裸环境首轮 22 失败全部是可选依赖缺失(`fitz`/`oss2`/`Tea`),补装后全绿,**非代码缺陷**。唯一环境坑:Ubuntu setuptools 68.1.2 构建 sdist 报 `install_layout`,`pip install -U --ignore-installed setuptools wheel` + `--no-build-isolation` 绕过。

**前端**:`npm ci` OK(1 个 high 漏洞提示);`vue-tsc` typecheck **0 错**;`vitest` **343 通过 / 2 失败**。2 个失败均在 `browse.spec.ts` 的 DocTable 翻页器 describe,系 5000ms 超时——运行时 stderr 报 `injection "Symbol(router)" not found`(DocTable 挂载时缺 router 注入,组件等一个永不 resolve 的异步路径),**是测试脚手架缺注入,非功能 bug**。建议在该 spec 补 `router` mock provide。

---

## 4. 建议优先级(可直接排期)

**P0(安全/数据正确性,应尽快修)**
1. **C7** legacy upload-url 对 `permission_level` 做与 node 分支一致的 `{internal,private}→dept_internal` 归一(一处对齐即堵)。
2. **C9** set-visibility 增加"当前版本 `content_process_status` 未终态则拒绝/警示"守卫,或改 raw_key 权限段。
3. **C4** 钉钉签名纳入 body 摘要,或高敏检索强制二次身份校验。
4. **C0** DOCX 表格单元格分支补 `<w:drawing>/<w:pict>` 扫描 → 产 image_ref 块(或至少纳入 leftback 兜底 + 告警)。

**P1(可靠性/合规,一行~小改)**
5. **C1** `raw_inventory.py:66` 解包二元组(+ ops_monitor 认 `errors` 键)。
6. **C6** purge_subject 对 qa_session_log 加前序依赖门。
7. **C2** `ha3_verify` 命中判据过滤 `is_expanded`(埋雷,趁无调用方先修)。
8. **C10** 小程序 hot-questions 加 `auth:true`。
9. **C3** reconcile 预筛纳入 `acl_mode`/`owner_dept` 维度,或对 node 文档不走预筛快路径。
10. **C5** retention_node 明确设 `RAG_RETENTION_ARCHIVE=false` 或装 oss2 打通归档——二选一并真跑阶段 2。

**P2(Console 完整性)**
11. 差评复核/复审任务/我的贡献三处加"加载更多"(前端 1 行,后端已就绪)。
12. DocTable 退役/恢复按钮按 `perm==='public' && !isKbAdmin` 前置禁用(照抄 ShareDocModal 范式)。
13. HeroBoard/候选队列接入 `noteLoadError`;删除会话加 ConfirmDialog。
14. 补 `approve`/`reject` 后端路由测试 + 审批 e2e(封住权限提升零测试缺口)。
15. 修正 `ManageView.vue:116` 虚指注释(要么实现 60s 轮询,要么改注释)。

**P3(设计债)**:统一 kb_admin 鉴权实现、决策动作 REST 形态、分页姿态;下线 `POST /api/search`。

---

## 附录 A:严重度下调说明

3 条经对抗验证从 high 降级,均因"机制成立但当前不可达/需额外前提":
- **C4** high→medium:需先捕获窗口内合法签名。
- **C8** high→medium:需 upload token 未过期 + 有效 put_url。
- **C5/C6** high→medium:配置/并发前提。
- **C2** high→medium:`verify_chunks_present` 当前无生产调用方。

C7 从 critical→high(仍是本轮最高实际风险):越权成立但需 dept_admin 主动构造别名请求。

## 附录 B:50 条待评 medium/low 发现(未过对抗验证)

按子系统分布:extraction 5、ingest-orch 5、retrieval 4、security-authz 5、api-layer 2、dingtalk 4、data-layer 6、console-backend 5、console-frontend 7、client-apps-ops 7。完整清单见工作流原始输出。**值得优先自查的几条**:

- `pipeline_nodes.py:6377` chunk provenance 的 `funnel_policy` 读泄漏循环变量 `doc`(最后一篇 canonical)→ 全批 chunk 写成同一策略标签。
- `ops_monitor.py:70/84` P2-14 心跳在默认 `prod_ro` 下写不进 → 监控死人开关失效;子作业单探针 SQL 失败被静默吞(exit 0)→ 监控假绿。
- `retriever.py:2199` cosurface 补图绕过主命中 RDS 复核 → 陈旧/孤儿 image 行直接投放。
- `spot_checker.py:909` 隔离路径在陈旧快照上枚举 PK 不取锁,并发重分块时 HA3 漏删新 PK → 文档标记已隔离却仍可检索。
- `kb_console.py:308` my-docs/browse 分页 ORDER BY 无唯一 tiebreaker(updated_at 秒级非唯一)→ OFFSET 翻页丢行/重行。
- `kb_console.py:2061` doc-status 漏传 publish_status/chunk_status → 隔离/0-chunk 版本永远显示"处理中",前端轮询等不到终态。
- `useAsk.ts:473` retry() 在另一次流式进行中会静默删错误卡且不重发;`useDialog.ts:38` 单 _resolve 槽位,并发弹窗前一个 Promise 永久悬挂。
