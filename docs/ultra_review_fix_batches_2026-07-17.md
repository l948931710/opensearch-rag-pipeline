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

## 批次4 — flag 翻开前置(agent/durable/general-ability;flag 默认 off 但在铺开路径)
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ⬜ | P1 旧审批决定被 reconcile 重放到新挂起调用 | `agent_runtime/approval_store.py:488` | 扫描限最新 request;_verify_persisted_decision 加 call_id 匹配 |
| ⬜ | P2 恢复扫描与快路径竞态→双跑 | `agent_runtime/dispatch_outbox.py:134` | claim_next 加最小年龄宽限;快路径失 claim 不再直通执行 |
| ⬜ | P2 attempts 耗尽的 resume 命令三个 closer 都够不着(永久僵尸) | `agent_runtime/dispatch_outbox.py:242` | 恢复扫描补 resume 分支,按 run 终态收口 |
| ⬜ | P2 bind_and_done 吞 CAS 失败(被偷 lease 双跑无日志) | `agent_runtime/durable_dispatcher.py:73` | 检查 bind_run 返回值,CAS miss 记 ERROR 并跳过 complete |
| ⬜ | P2 pool.submit 失败但已入队→双 _release 侵蚀并发闸 | `agent_runtime/executor.py:231` | dispatch_maybe_scheduled 时 except 路径不 _release |
| ⬜ | P2 suspend 落库失败路径无 D3 归属栅栏 | `agent_runtime/executor.py:531` | _notify_failure 门在 running→failed CAS 成功上,对齐 567-571 |
| ⬜ | P2 空 state_digest 绕过 REQUIRE_HMAC 校验 | `agent_runtime/executor.py:410` | 有 key/REQUIRE_HMAC 时缺 digest 直接拒绝 |
| ⬜ | P2 崩溃收割的 run 不关事件流,跨副本 SSE 挂 30 分钟 | `agent_runtime/event_relay.py:132` | 终态 durable status 时限界 tail(replay-only/短宽限) |
| ⬜ | P1 RedisRateLimiter 缺 admit_general→redis 后端 500 | `rate_limiter.py:718` | 实现 admit_general(Lua INCR+北京午夜 TTL),对齐 memory 语义 |
| ⬜ | P2 /api/agent/approve 授权前泄露 run 状态与决定 | `routes/agent.py:1005` | 挂起分支前先过 owner-or-kb_admin 门,未授权 404 |
| ⬜ | P2 确定性计算免配额有文档无实现 | `general_answerer.py:203` | calc 先行判定再 admit_general,或 model=="calc"/异常时退还 |

## 批次5 — 摄取/编排 P2 同族
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ⬜ | P2 DAG-3 失败回滚无 lease 栅栏且不清租约列 | `dataworks_orchestrator.py:587` | 回滚 WHERE 加 fence_where_sql,SET 加 clear_set_sql |
| ⬜ | P2 分类成功路径落库无 lease 栅栏 | `pipeline_nodes.py:1878` | CONTENT_CLASSIFIED/冻结维护/贡献三路径补 fenced-write |
| ⬜ | P2 HA3 delete 把真 SDK 2xx 当成功不解析 per-doc 错误 | `pipeline_nodes.py:6690` | 对齐 _push_chunks_to_ha3 的 str body 解析;幂等匹配收紧到精确码 |
| ⬜ | P2 parity 失败后 dv 卡 PROCESSING 2 小时 | `pipeline_nodes.py:7544` | raise 前同事务 CAS PROCESSING→FAILED+清租约 |
| ⬜ | P3 stage-1 no-progress 计数含 _quarantine 行误中止 | `dataworks_orchestrator.py:661` | drain 计数排除 scanner 不认领的行 |

## 批次6 — 抽取/多模态 P2
| 状态 | 条目 | 位置 | 修法 |
|---|---|---|---|
| ⬜ | P2 部分页 OCR 失败仍报 DONE,页面内容静默丢失 | `extraction/ocr_client.py:298` | 记 failed page 清单/计数,门 DONE |
| ⬜ | P2 200 无法解析的 OCR 响应返 "" 并永久缓存 | `extraction/ocr_client.py:477` | 解析失败 raise(页 FAILED 不缓存);空结果不入缓存 |
| ⬜ | P2 预算 cap/DENY 跳过的图片无 NEEDS_REVIEW 信号 | `extraction/unified_extractor.py:1945` | 计入 vlm_degraded_count(或专用 skipped 计数)驱动 NEEDS_REVIEW |
| ⬜ | P2 XLSX/PPTX 整层 except 把部分抽取定稿为 DONE | `extraction/unified_extractor.py:1343` | 中途异常打 partial 标记→NEEDS_REVIEW |
| ⬜ | P2 refund() 让真实计费逃出预算上限 | `extraction/vlm_rebuilder.py:472` | 仅未发生计费调用的页可退款,按 doc 记 billed 计数 |
| ⬜ | P2 _vlm_reconstruct_page 裸 requests.post 无重试 | `extraction/vlm_rebuilder.py:149` | 走 post_json_with_retry(对齐 ocr_client) |
| ⬜ | P2 XLSX drawingN.xml 假定映射 sheet N,批注绑定静默丢失 | `extraction/image_extraction_utils.py:672` | 经 worksheet .rels 解析真实 sheet↔drawing 映射 |
| ⬜ | P2 clause_chunk 丢 page_num/source 溯源 | `chunker.py:1847` | 按段落跟踪 (offset,page_num,source),对齐 step 路径 |

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
