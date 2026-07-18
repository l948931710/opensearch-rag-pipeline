# Ultra 全仓评审修复分批台账
**日期:** 2026-07-17 · **分支:** claude/ontology-p0 · **来源:** `docs/audits/ultra_repo_review_2026-07-17.md`

盘点:1 P0(+1 根因 P2)· 11 独立 P1(6082 两条重复计一;dispatch_outbox:134 降 P2;ingest_lease:107 改判 P3 前置)· 39 CONFIRMED P2 · 49 P3 · 1 PLAUSIBLE(SAE 安装路径待确认)。

状态图例:⬜ 未动工 · 🔧 进行中 · ✅ 已修(make test+lint 绿) · ⏭️ 有意不修(注明) · 🔒 user-gated

---

## 批次1 — P0 权限逃逸根治(现网、无 flag、最高优先)
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ✅ | P0 尾斜杠 category_dept 绕过 kb_admin 审批发全员 public | `routes/contribution.py:851` | final_dept 用 sanitize 后的规范值,同一值喂 authorize_upload/build_raw_key/落库 |
| ✅ | P0根因 perm_from_raw_key 失效开放 public;build_raw_key 不验段 | `kb_upload.py:108/123` | build_raw_key 拒绝空/含 `/` 的段;perm_from_raw_key 畸形 key 返 restricted(fail-closed) |
| ✅ | P2 owner_dept 授权用净值、落库/编路径用原值 | `routes/kb_console.py:2255` | 顶部 sanitize 一次,授权/raw_key/token/INSERT 全用同一规范值 |

**批次1 落地记录(2026-07-17)**:如修法落地;新增 `kb_authz.sanitize_owner_dept`(单值净化,与 authorize_upload 内部规则同源);`perm_from_raw_key` 收紧为「仅 5/6 段且无空段的合法结构」——扁平 5 段→public 原语义保留,其余一律 restricted。回归:尾斜杠采纳直发 e2e(test_contribution)+段注入/畸形 key fail-closed(test_kb_upload)共 3 条新用例。调用面核查:perm_from_raw_key 仅 contribution.py 两处消费(均为 build_raw_key 产出键),管线 raw/ 扫描的 permission_level 路径启发式为独立函数不受影响。

## 批次2 — 现网安全面 P1/P2(无 flag 门控)
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ✅ | P1 卡片回调 apiSecret 硬编码默认值(仓库曾公开) | `dingtalk_bot.py:295` + `dingtalk_card.py:200` | 未设 env 时拒绝注册/验签(不再回退字面量);部署侧轮换 secret(🔒生效) |
| ✅ | P1 每次发卡 print 明文问答(违反 _q_for_log 红线) | `dingtalk_card.py:416` | 过 redact_query_text 或删掉 cardParamMap dump |
| ✅ | P1 GuardedBucket 漏 copy_object(prod-write 防线缺口) | `env_guard.py:258` | _WRITE_METHODS 补 copy_object+multipart 写法 |
| ✅ | P1 session_id 无长度上限→审计行静默丢失(可自选逃审计) | `api.py:290` | AskRequest.session_id max_length=128;build_qa_log_kwargs 防御性截断 |
| ✅ | P2 ACL 归一化 "*" 直通扩全组(fail-closed 边界破洞) | `dingtalk_identity.py:257` | 仅信任内部 sentinel 的 "*";外部来源 "*" 按未知丢弃 |

**批次2 落地记录(2026-07-17)**,as-built 与修法的差异点:
- **apiSecret**:验签未配 secret 返 None(shadow 记日志放行、required 一律 403);注册未配 secret 拒绝且不发网络请求(先于 token 获取)。**🔒生效前置**:SAE 环境变量设新 `DINGTALK_CARD_CALLBACK_API_SECRET` + `DINGTALK_CARD_CALLBACK_FORCE_UPDATE=true` 轮换注册——只部署代码不配 secret 时,反馈按钮回调注册会被拒(答案/打字机不受影响)。
- **发卡日志**:取「删 dump」路线,改打 cardParamMap **键名**(排障看模板字段齐不齐够用,值一概不出)。
- **GuardedBucket**:除 copy_object 外一并拦 multipart 五法;copy 族 op 标签取目标 key(第 3 位参),ack 命名与 put/delete 同构。
- **session_id**:入口闸 AskRequest.session_id **及 user_id** 均 max_length=128(检索请求模型 user_id 同步);build_qa_log_kwargs 按 schema/001 列宽防御截断 session/message/user_id/user_name/conversation_id=128、**user_dept=64**——后者顺带修复全组用户(总经办 15 组 CSV=104 字符)在 strict 模式整行丢审计的同机制问题。
- **"*" 哨兵**:修在 `_resolve_user_dept` 的钉钉 API 入口(星号项按未知丢弃、不落缓存),而非 `_normalize_dept_to_codes` 内——后者的裸 "*" 展开是**缓存行契约**(seed 行+祖先压缩回写依赖),已加测试锁定不回归。

每批验证:`make test` 3993 passed/1 skipped + `make lint` 绿(2026-07-17)。未验证声明:真实钉钉回调链路(secret 轮换后需真机点反馈按钮)、SAE 部署生效、真实 RDS strict 模式下超长 id 的 1406 行为(截断逻辑为纯函数已单测)。

## 批次3 — 摄取链路 P1(现网 correctness/concurrency)
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ✅ | P1 超长 step_card 不切分被验证器整块丢弃 | `chunker.py:1197` | step_text 本体过 _split_long_text(is_step_continuation),对齐 1800-token parent 预算 |
| ✅ | P1 池连接不进事务模式→死锁中 SteadyDB 单句重放撕裂账本 | `db.py:207` | _get_db_conn 调 begin()(对齐 agent_runtime `_begin()` 已有模式) |
| ✅ | P1 deactivate 失败路径无 CAS 清掉 PENDING_DELETE 握手(两条重复计一) | `pipeline_nodes.py:6082` | 失败 UPDATE 补 `AND index_status='PROCESSING'` + lease clear,对齐 6208/7340 |
| ✅ | P1 chunk_meta 提交与状态收口间崩溃→unfrozen-rechunk 守卫楔死整批 | `pipeline_nodes.py:5472` | crash-resume 豁免:_chunk_set_hash 相同自动放行或按 doc 跳过而非整批 raise |
| ✅ | P1 run/daily 预算瞬态耗尽→健康文档被终态隔离 | `extraction/cost_breaker.py:463` | 仅 doc 自身原因(gate1/2/2b)才 quarantine;run/daily 预算拒绝保持可重捡 |

**批次3 落地记录(2026-07-17)**,as-built 与修法的差异及关键决策:
- **超长 step_card**:分治两形态——(a) step_text 本体单独超长(单步吞并多页,parts 仅 `[step_text]`)时把本体过 `_split_long_text`,首段作主块、其余正文段+补充降级续接块;(b) step_text 可容纳、仅叠加补充超长时**完全保留原贪心塞补充行为**(零回归)。max_chunk_chars 默认 800≈533 token,切后远低于 2000-token 校验上限。
- **db.py begin()**:未采「无条件 begin() 改所有路径」的字面最简法。新增防御式 `_begin_txn`(对齐 `run_store._begin`:getattr 桩跳过 + **try/except 保读路径可用性**——begin 抛错不阻断取连接),在 `_get_db_conn` **事务开头**调用(保 FOR UPDATE 锁语义)。**读路径代价**:单句 SELECT 中途断连不再 SteadyDB 静默重放,改抛错(serving 映射 503 快速失败);ping=1 取连接重连不受影响。**kill switch `RAG_DB_TXN_BEGIN=false`** 恢复旧无-begin 语义(逃生口)。
- **deactivate 失败 CAS**:失败路径 `SET index_status='FAILED'{clear_set_sql()}` + `AND index_status='PROCESSING'`,逐字对齐 7340(FAILED 是复位型终态,只清租约不拼 epoch 栅栏)。clear_set_sql() 现网 flag off 为空串→纯 PENDING_DELETE 保护、零租约副作用。
- **crash-resume 豁免**:采「精准区分」而非「全 _prior 放行」。关键区分点=**状态互斥**:crash-resume(sweep 转 `FAILED`+`retry>0`)vs deliberate reset_for_rechunk(`NOT_STARTED`+`retry=0`)。新增 `_partition_prior_rechunk`(JOIN document_version+document_meta):crash-resume 目标 **auto-freeze 复用 document_meta 存储分类**续跑(zero-LLM,family 保持),category 丢失则降级 deliberate(fail-closed);deliberate 仍整批 raise。override(`RAG_ALLOW_UNFROZEN_RECHUNK`)docset_hash 现只按 deliberate 子集算(crash-resume 不需 override)。**kill switch `RAG_CRASH_RESUME_AUTOFREEZE=false`** 回退旧整批 raise。**行为变化**:此前有 chunk 的目标一律整批 raise,现 crash-resume 自动续跑——请 review。
- **cost_breaker 瞬态**:新增 `_is_transient_cost_deny`(短语契约:`RUN budget exhausted`/`would exceed RUN budget`/`DAILY budget exhausted`=瞬态);gate_vlm_rebuild 仅对非瞬态(doc-intrinsic 闸1/2/2b)才 quarantine,RUN/DAILY 预算瞬态耗尽保持文档可重认领。

每批验证:`make test` 4008 passed/1 skipped + `make lint` 绿(2026-07-17,一处测试断言因 6082 SQL 换行而更新,已加强验证 CAS)。未验证声明:真实 RDS 死锁下 begin() 的重放阻断(纯机制,单测覆盖 helper)、真实 crash 窗口的 stage-2 续跑(状态机+分区逻辑单测覆盖)、真实 HA3/DataWorks 端到端。两个新 kill switch(`RAG_DB_TXN_BEGIN`/`RAG_CRASH_RESUME_AUTOFREEZE`)默认 on=修复生效。

**批次3 独立核查(2026-07-17,第二会话)**:五项逐项对照周边代码复核。**3 项通过**:chunker 分治(续接块机制/图只绑主块契约/`_split_long_text` 每段≤max 均验证)、db begin(转发链 GuardedDBConnection→PooledDB→SteadyDB 成立、池 autocommit=False 确认、READ ONLY 走建连期 init_command 无 1568 冲突)、6082 CAS(deactivate 失败点新版本行确在 PROCESSING,PENDING_DELETE 握手被保住)。**2 项核查纠偏**:
- **crash-resume 分区生产路径失效**:分区只认 `FAILED`,但 orchestrator 的 stage-2 loader 在 DAG-2 启动前已把认领行 FAILED→**LOADING**(retry_count 保留)——guard 时看到 LOADING+retry>0,分区永不命中、整批照旧 raise,恰好在真正发生楔死的生产路径上白修(原 5 条测试全部直喂 FAILED,未建模认领后状态)。纠偏:状态列接受 `('FAILED','LOADING')`,互斥判据不变(retry_count;deliberate 认领后=LOADING+retry=0),补 LOADING 两态回归测试。
- **cost 瞬态拒绝残余链路**:瞬态虽不再写 RDS 隔离,但 `cost_quarantined=True` 仍烙进 canonical JSON → stage-2 按 QUARANTINE 跳过 → 0-chunk 收口落 **EMPTY+DONE 静默终态**(且 stage-1 因 keys 已写永不重跑)——「保持可重认领」名存实亡,还比旧行为少 review_task 可见性。纠偏:`gate_vlm_rebuild` 加 `deny_out` 出参暴露瞬态定性;瞬态改标新字段 `cost_deferred`(schema);`node_build_canonical` 增 **COST-DEFER 守卫**(与 ENV-DEP 同型:不写 canonical keys、extraction_status='FAILED' 留痕、NOT_STARTED 保持 → 下一 run/次日预算滚动后 stage-1 按既有谓词自动重捡重过闸;循环后统一 raise 炸红——预算耗尽必须可见,并终止 drain-loop 无进展空转)。doc-intrinsic 路径(quarantine+review_task)不变;可选表格精修路径(quarantine_on_deny=False)不变。新回归 5 条(deny_out 契约/rebuilder 分流/守卫扣稿/健康同批不楔死/LOADING 分区)。

## 批次4 — flag 翻开前置(agent/durable/general-ability;flag 默认 off 但在铺开路径)
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ✅ | P1 旧审批决定被 reconcile 重放到新挂起调用 | `agent_runtime/approval_store.py:488` | 扫描限最新 request;_verify_persisted_decision 加 call_id 匹配 |
| ✅ | P2 恢复扫描与快路径竞态→双跑 | `agent_runtime/dispatch_outbox.py:134` | claim_next 加最小年龄宽限;快路径失 claim 不再直通执行 |
| ✅ | P2 attempts 耗尽的 resume 命令三个 closer 都够不着(永久僵尸) | `agent_runtime/dispatch_outbox.py:242` | 恢复扫描补 resume 分支,按 run 终态收口 |
| ✅ | P2 bind_and_done 吞 CAS 失败(被偷 lease 双跑无日志) | `agent_runtime/durable_dispatcher.py:73` | 检查 bind_run 返回值,CAS miss 记 ERROR 并跳过 complete |
| ✅ | P2 pool.submit 失败但已入队→双 _release 侵蚀并发闸 | `agent_runtime/executor.py:231` | dispatch_maybe_scheduled 时 except 路径不 _release |
| ✅ | P2 suspend 落库失败路径无 D3 归属栅栏 | `agent_runtime/executor.py:531` | _notify_failure 门在 running→failed CAS 成功上,对齐 567-571 |
| ✅ | P2 空 state_digest 绕过 REQUIRE_HMAC 校验 | `agent_runtime/executor.py:410` | 有 key/REQUIRE_HMAC 时缺 digest 直接拒绝 |
| ✅ | P2 崩溃收割的 run 不关事件流,跨副本 SSE 挂 30 分钟 | `agent_runtime/event_relay.py:132` | 终态 durable status 时限界 tail(replay-only/短宽限) |
| ✅ | P1 RedisRateLimiter 缺 admit_general→redis 后端 500 | `rate_limiter.py:718` | 实现 admit_general(Lua INCR+北京午夜 TTL),对齐 memory 语义 |
| ✅ | P2 /api/agent/approve 授权前泄露 run 状态与决定 | `routes/agent.py:1005` | 挂起分支前先过 owner-or-kb_admin 门,未授权 404 |
| ✅ | P2 确定性计算免配额有文档无实现 | `general_answerer.py:203` | calc 先行判定再 admit_general,或 model=="calc"/异常时退还 |

**批次4 落地记录(2026-07-17)**,as-built 与修法的差异及关键决策:
- **approval_store:488 双保险**:①扫描 NOT EXISTS 排除「存在更新请求」的行(按 created_at+request_id 排序,executor 侧无关);②`_verify_persisted_decision` 新增 `pending_call_id` 锚定——请求行 call_id ≡ checkpoint pending_call.call_id,不符即 RunRejected(锚定行优先取 latest 捷径,**行内无 call_id 键的简化测试桩沿用旧语义跳过**——真实存储行恒含 call_id/schema NOT NULL,生产不可绕)。
- **dispatch_outbox:134**:①`claim_next` 加 `min_age_s` 参数(queued 臂 `created_at < NOW-宽限`;=0 字节级旧 SQL),dispatcher 经 **`RAG_AGENT_DISPATCH_CLAIM_GRACE_S`(默认 30s)** 传入——未采审计建议的 lease_s(120s):防护窗口是毫秒级,30s 余量巨大且孤儿恢复少等一拍;②fast path 失认领**改 409**(「已受理正在处理中」),不再直通执行——命令归持有方恰一次执行;enqueue 成功但 claim 抛异常的子角落维持「可用性优先直通」旧档(有意,台账留痕)。
- **dispatch_outbox:242**:新增 `sweep_exhausted_resumes`(单条 UPDATE LEFT JOIN agent_run):run 仍 suspended→failed 留因、run 行已清除→failed、其余(running/终态)→done;tick 经 getattr 接线(桩无此法跳过),计数入返回值 `resume_closed`。
- **durable_dispatcher:73**:bind_run CAS 落空→ERROR+**跳过 complete**(命令归窃取方收敛);complete 落空→ERROR(恢复扫描按已绑收口)。审计建议的「recover_fn 前后续租」**有意不做**:claim_next 同事务刚铸新租约,再续租是毫秒级冗余;租约不足的真对策是调 RAG_AGENT_DISPATCH_LEASE_S(日志已指路)。
- **executor 三修**:231/361 双 release 按 `dispatch_maybe_scheduled` 门控(可能已入队→槽随条目移交驱动器;宁漏一槽自限容量,绝不欠计放大并发);531 失败侧回调补 D3 栅栏(running→failed CAS 成立才 _notify_failure,对齐 567-571,RunFailed 帧照发);410 缺 digest 且(密钥在场 或 REQUIRE_HMAC 开)→拒绝续跑(防「置空 digest 列」绕过——正是 HMAC 威胁模型),无密钥环境零回归。
- **event_relay:132**:`stream_run_events` 加可选 `is_terminal_fn` 探针(routes 注入 run_store 读,模块不 import serving)——**只在 XREAD 空转时**查终态(活跃流零额外 DB 读),终态则 500ms 短读 drain 赛跑帧后收流;/events 端点已接线。中继降级(_dead)与崩溃收尸两形态都被兜住。
- **rate_limiter:718**:`RedisRateLimiter.admit_general` 落地(_GEN_LUA:INCR-then-check 原子+首建键 EXPIRE 到北京次日零点;>quota 的 INCR 不回退——只作 >配额 比较,与 memory「恰放行 quota 次」等价);anon/off 判定在 Python 对齐 memory 版。**fail 姿态=fail-open**(对齐 aux/charge:请求已过 ask 主闸的 fail-closed,配额层抖动不该打死通用问答;charge 全局帽后判兜底)。
- **routes/agent:1005**:approve 在任何状态分支前过**可见性门**——发起人/kb_admin/快照 scope 覆盖的 dept_admin 可见,其余 404(不可见==不存在,对齐 run 详情);裁决权(职责分离 403)仍归 _authorize_approver。**行为变化**:无关员工/scope 不覆盖的 dept_admin 从 403 改 404(两条既有测试相应更名改断言)。
- **general_answerer:203**:calc **先于** admit_general(api 与 dingtalk_bot 两调用点同源修)——算术命中零 LLM 零配额;LLM 异常仍耗一格的次要面**有意不做退还**(审计"或"语义,退还机制两后端新增面不成比例)。

每批验证:新回归 19 条(pr3 dispatch 6+hardening 7+cancel/relay 2+redis limiter 4+general 3+approval 真库 1,含两条 403→404 改判);`make test` 全量 + `make lint` 绿(2026-07-17)。未验证声明:真实 RDS 上 NOT EXISTS 扫描与 LEFT JOIN 收口的执行计划(本地 MySQL 8 真库测试绿)、真实 Redis 的 _GEN_LUA(fakeredis+lupa 覆盖)、多副本真实竞态窗口、SAE 部署。所有修复都在默认 off 的 flag 后(除 approve 404 门与 calc 免配额——前者现网 RAG_AGENT_ENABLE off 不可达,后者 RAG_GENERAL_ABILITY_MODE off 不可达)。

## 批次5 — 摄取/编排 P2 同族
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ✅ | P2 DAG-3 失败回滚无 lease 栅栏且不清租约列 | `dataworks_orchestrator.py:587` | 回滚 WHERE 加 fence_where_sql,SET 加 clear_set_sql |
| ✅ | P2 分类成功路径落库无 lease 栅栏 | `pipeline_nodes.py:1878` | CONTENT_CLASSIFIED/冻结维护/贡献三路径补 fenced-write |
| ✅ | P2 HA3 delete 把真 SDK 2xx 当成功不解析 per-doc 错误 | `pipeline_nodes.py:6690` | 对齐 _push_chunks_to_ha3 的 str body 解析;幂等匹配收紧到精确码 |
| ✅ | P2 parity 失败后 dv 卡 PROCESSING 2 小时 | `pipeline_nodes.py:7544` | raise 前同事务 CAS PROCESSING→FAILED+清租约 |
| ✅ | P3 stage-1 no-progress 计数含 _quarantine 行误中止 | `dataworks_orchestrator.py:661` | drain 计数排除 scanner 不认领的行 |

**批次5 落地记录(2026-07-17)**,as-built 与修法的差异及关键决策:
- **DAG-3 回滚**:epoch 栅栏+clear_set_sql 逐字对齐节点内 FAILED 路径;**节点内已判 LeaseLost 的 key(epoch 已丢弃)整个跳过**——文档归新持有者收敛,绝不无栅栏覆盖其在跑 PROCESSING 锁。LeaseSet 经 result_ctx 取(dag.run 浅拷贝共享对象,与 preempted 集同源)。flag off 时栅栏/清列均空串=字节级旧 SQL。
- **分类三路径 fenced-write**:**dv 先写带栅栏并 check_fenced_write,过验才写 document_meta**(同事务;此前 meta 先写,栅栏挡不住 meta 分道扬镳)。LeaseLost ⇒ **弃单文档继续批**(return False→failed_doc_ids→canonicals 剔除,PR-4 语义),不 abort 节点;LeaseLost 显式 except 置于泛 except **之前**(LeaseLost 是 RuntimeError 子类,否则会被成功路径的 DB-error re-raise 误吞成整节点 abort)。冻结维护/贡献 FAQ 两路径同款。
- **HA3 delete 2xx 解析**:对齐 push 侧(str body→json.loads,失败保守按无 doc 级错误);doc 级错误**逐条**判幂等——精确码 DocumentNotFound/7504 或该条 message 含 not_found/no_op,其余 raise 交调用方失败路径(FAILED CAS+旧版本排 PENDING_DELETE)。**非 2xx 分支(仅 mock/sim 可达)的宽底 combined_msg 扫描有意保留**——既有测试钉住其语义,真实 SDK 永不走它;收紧只做在真正的生产可达面(2xx per-doc)。
- **parity dv CAS**:受影响 (doc_id,version_no) 去重后同事务 CAS PROCESSING→FAILED+清租约;CAS miss(已 FAILED/PENDING_DELETE)合法故**不 check_fenced_write**;**不 discard** epoch——随后 orchestrator 回滚对同 key 的栅栏 CAS 因行已离开 PROCESSING 安静 no-op(discard 反而触发误导性 skip 日志)。
- **stage-1 隔离行**:谓词收进 ingest_policy 单一来源(`stage1_quarantine_like_pattern`,'\_' 转义——LIKE 里 '_' 是单字符通配),scanner 认领 SELECT 与 `_count_pending_rows(1)` 同步启用(计数↔认领镜像纪律);顺带修隔离行占 LIMIT 100 名额的队头挤占。`process_quarantine=True` 未来通道保留(SQL 谓词随之关闭,Python 过滤同门)。

每批验证:新回归 12 条(HA3 2xx 解析 4+新文件 test_batch5_ingest_fences.py 8:谓词/scanner/计数/回滚源检/classify 栅栏×2/parity CAS);`make test` 全量+`make lint` 绿(2026-07-17)。未验证声明:真实 HA3 SDK 的 2xx body 形态(按 _push_chunks_to_ha3 的 96 例实测形态对齐)、真实 RDS 上 NOT LIKE 转义行为(标准 MySQL 8 语义)、真实 TTL 接管窗口的栅栏竞态(PR-4 故障注入族已覆盖同机制)。生效前置:lease 相关三项仅 RAG_INGEST_LEASE_ENABLE=on 时改变行为(off=字节级现状);HA3 解析与 stage-1 计数无 flag、随下一次 DataWorks 包部署生效。

## 批次6 — 抽取/多模态 P2
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ✅ | P2 部分页 OCR 失败仍报 DONE,页面内容静默丢失 | `extraction/ocr_client.py:298` | 记 failed page 清单/计数,门 DONE |
| ✅ | P2 200 无法解析的 OCR 响应返 "" 并永久缓存 | `extraction/ocr_client.py:477` | 解析失败 raise(页 FAILED 不缓存);空结果不入缓存 |
| ✅ | P2 预算 cap/DENY 跳过的图片无 NEEDS_REVIEW 信号 | `extraction/unified_extractor.py:1945` | 计入 vlm_degraded_count(或专用 skipped 计数)驱动 NEEDS_REVIEW |
| ✅ | P2 XLSX/PPTX 整层 except 把部分抽取定稿为 DONE | `extraction/unified_extractor.py:1343` | 中途异常打 partial 标记→NEEDS_REVIEW |
| ✅ | P2 refund() 让真实计费逃出预算上限 | `extraction/vlm_rebuilder.py:472` | 仅未发生计费调用的页可退款,按 doc 记 billed 计数 |
| ✅ | P2 _vlm_reconstruct_page 裸 requests.post 无重试 | `extraction/vlm_rebuilder.py:149` | 走 post_json_with_retry(对齐 ocr_client) |
| ✅ | P2 XLSX drawingN.xml 假定映射 sheet N,批注绑定静默丢失 | `extraction/image_extraction_utils.py:672` | 经 worksheet .rels 解析真实 sheet↔drawing 映射 |
| ✅ | P2 clause_chunk 丢 page_num/source 溯源 | `chunker.py:1847` | 按段落跟踪 (offset,page_num,source),对齐 step 路径 |

**批次6 落地记录(2026-07-17)**,as-built 与修法的差异及关键决策:
- **统一「不完整不定稿」通道**:新增 `ExtractionResult.partial_loss_notes`(schema→canonical→orchestrator 回读→closure 三跳与 P2-32 同型),closure 的 NEEDS_REVIEW 门扩为 `vlm_degraded_count>0 或 partial_notes 非空`(chunk/索引照常,graceful degradation 不破)。OCR 部分页失败(消费侧从 OCRResult.pages 派生失败页清单,聚合仍 DONE 保留成功页文本)与 XLSX/PPTX 中途异常(有产出时)走此通道;**零产出仍归 0-chunk 疑似失败守卫**(FAILED+retry,更强,互不干扰)。
- **cap/DENY 跳过**:按审计原建议计入 `vlm_degraded_count`(同一 NEEDS_REVIEW 自愈通道,print 注明 budget skipped 分量)——预算调高后重扫即自愈。
- **OCR 200 不可解析**:DashScope 分支 raise RuntimeError(页 FAILED、raise 天然跳过缓存写);**空文本页不入页缓存**(真空白页重付一次 OCR 的代价换掉「一次形态异常固化为永久空页」;Gemini dev 分支未动)。
- **refund 计费泄漏**:两路径(rebuild/refine)按 `_billed_pages` 只退未发请求页的份额(`dataclasses.replace(est, est_cost_rmb=share)`);**「请求已发出即计费」保守判定**(连接级失败不可区分,账本只多不少);refined>0 时照旧不退(维持保守超记)。**行为更替**:零产出但已计费不再全额退款——既有测试 test_rebuild_refunds_when_no_blocks_produced 改判为两条新语义用例。
- **重建重试**:`_vlm_reconstruct_page` 改走 `post_json_with_retry`(label=VLM(rebuild),timeout 元组透传);外层 catch→[] 的最终失败语义不变(有界重试后仍失败才丢页)。
- **xlsx drawing↔sheet**:新增 `_drawing_to_sheet_order`(workbook.xml sheet 顺序→r:id→workbook rels→worksheet rels→drawing,与资产 page_num 的 openpyxl wb.worksheets 同源);**任何一环缺失/解析失败回退旧 drawingN→sheetN 假定**(fail-open,常规文件两者一致零变化;封面页工作簿从「标注全丢」变为正确绑定)。
- **clause 溯源**:逐段落记 `(offset,page_num,source)`(与 #F-clause-img 图片偏移同一坐标系),clause_chunk 按 seg_start 覆盖块盖章;无边界 fallback 路径用游标 find 精确定位(sub 是 full_text 连续窗口子串,find 必中)。合并段跨页时取起始块溯源(审计原建议粒度)。

每批验证:新回归 15 条(batch6 文件 10+degraded 传播文件 3+dormant 改判 2);`make test` 全量+`make lint` 绿(2026-07-17)。未验证声明:真实 DashScope 429 重试行为(共享 helper 已有生产案底)、真实封面页 xlsx 工作簿语料(夹具为手工最小 OOXML)、NEEDS_REVIEW 状态在真实 RDS 的运维查询面。生效面:全部无 flag、随下一次 DataWorks 包部署生效;**行为变化两处需知**——①部分丢失文档从静默 DONE 改 NEEDS_REVIEW(存量已定稿的不回溯,重灌时生效);②rebuild/refine 零产出不再全额退款(预算消耗会更快到帽,这正是修复目的)。

## 批次7 — 服务/配置/告警 P2
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ⬜ | P2 send_ops_alert 只看 HTTP 200,errcode 失败静默丢告警 | `alerting.py:73` | 解析 body,errcode!=0 记 error 返 False |
| ⬜ | P2 /api/ready 无认证无限流却每次打真 RDS+HA3 | `api.py:593` | 探针结果短 TTL 缓存(single-flight)±aux IP 限流 |
| ⬜ | P2 环境标签开放集+大小写归一不一致,未知标签跳过全部交叉校验 | `config.py:632` | load_config 归一(strip+lower)一次,未知标签 fail-fast |
| ⬜ | P2 maxcached=5 vs maxconnections=20,TLS 后重连抖动放大 | `db.py:195` | maxcached 对齐上限(或随 RAG_DB_POOL_MAX 暴露) |
| ⬜ | P2 is_stream_active 报线程活性非 WSS 连通,重连窗丢点击 | `dingtalk_stream_runner.py:140` | 用 SDK 连接回调置/清 active 标志 |

## 批次8 — 前端/小程序/schema/CI/测试门(续跑新增 P2)
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ⬜ | P2 阻断 CI 从不在发布分支跑(gitleaks/CVE 全豁免) | `.github/workflows/ci.yml:12` | push 触发加 `claude/ontology-p0`(或 `claude/**`) |
| ⬜ | P2 prod-write 测试门漏 OSS bucket/远程 OpenSearch | `tests/conftest.py:34` | _prod_target_violations 补 OSS bucket+opensearch host 检查 |
| ⬜ | P2 改参并批准把服务端脱敏值当真参执行 | `console-app/.../AgentApprovalQueue.vue:73` | 编辑值与 checkpoint 原参合并/或含掩码占位即拒绝 |
| ⬜ | P2 401 reauth 风暴(无 in-flight 去重) | `console-app/src/composables/useAuth.ts:229` | reauth 单飞(共享 in-flight promise) |
| ⬜ | P2 畸形 deep-link decodeURIComponent 顶层抛→白屏 | `console-app/src/composables/useAuth.ts:20` | try/catch 包 decode,坏参降级忽略 |
| ⬜ | P2 会话切换调 legacy stop() 误终结 agent 消息→重试双跑 | `console-app/src/composables/useAsk.ts:433` | stop() 加 m.agent 守卫走 agent 取消路径 |
| ⬜ | P2 共享设备会话串号(登录前渲染他人缓存) | `fuling-rag-miniapp/pages/chat/chat.js:166` | _restoreLast 前先 ensureOwner;_ask/抽屉/设置登录路径补 owner 复查 |
| ⬜ | P2 「清空会话」后每次冷启动都静默清空恢复 | `fuling-rag-miniapp/pages/chat/chat.js:232` | reset 处理后持久化 lastResetAt(或清 marker) |
| ⬜ | P2 ACL 撤销可被 drain 竞态静默吞掉(无 generation 列) | `schema/009` + `access_grants.py:273` | outbox 加 generation/epoch 列(新 schema 文件🔒apply),drain 按代次 CAS 收口 |
| ⬜ | P2 ontology DataWorks 节点 py3.7 装不上 requests==2.32.3 | `dataworks_nodes/ontology_backfill_node.py:41`(×2 文件) | 补 sys.version_info 钉版分支(对齐 07-17 stage 节点) |
| ⬜ | P2 dedup fix 模式 status-only 退役留双活文档 | `dataworks_nodes/scan_oss_sync_keys.py:253` | 退役同时灭活 chunk_meta+排 HA3 PENDING_DELETE(复用 spot_checker 模式) |
| ⬜ | PLAUSIBLE SAE 安装路径到底走 requirements.txt 还是 lock | `requirements.txt:13` | 查 SAE 部署配置定性;若 zip/buildpack 路径→给 requirements.txt 上 hash 或改指 lock |

## 批次9 — P3 清扫(49 项,选择性;单独排期)
- run-1 30 项 + 续跑 16 项 + 改判 3 项(`packing_math:245`/`store:1469`/`spot_checker:686`),台账见审计文档 P3 表。
- 原则:security 类(auth_token typ 未强制、gap dismiss 无 owner 域、intent_router 提示注入边界、openDocPreview tabnabbing、legacy cleanup 裸 pip)优先;纯 maintainability 按顺路修。

## 有意不修 / 移交
| 条目 | 处置 |
|---|---|
| `ingest_lease.py:107` takeover 信残留租约 | ⏭️ 改判 P3:已是 `docs/ingest_lease_fencing_scope_2026-07-17.md` §3.5 记录在案的 workers>1 前置,归 PR-4 尾项,不在本轮 |
| REFUTED 3 项(access_grants:249 · requirements.txt:15 · corpus_cleanup:39) | ⏭️ 评审已推翻,不修(access_grants:249 的 head-of-line клог留 P3 备注) |
| DingTalk secret 轮换、schema/009 变更 apply、CI 分支保护 UI 设置 | 🔒 user-gated 生效项,代码侧先落 |

## 验证纪律
每批:`make test` + `make lint` 绿才标 ✅;涉及 serving 行为的补回归测试;未验证项(真实 HA3/RDS、钉钉端到端、SAE 包)逐批声明。
