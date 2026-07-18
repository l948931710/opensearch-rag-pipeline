# 企业 Agent 全维度 Production Review(2026-07-17)逐条核查台账 + 修复批次

> 核查日期:2026-07-18
> 评审原文:`~/Downloads/enterprise_agent_production_review_2026-07-17.md`(审 `claude/ontology-p0@c34b1da`)
> 核查基线:本地 `claude/ontology-p0@ee6946d`(评审基线后 11 提交,含 RB-05 修复 a2277fb/b9424f7/cb0fb9e、转人工下线、gap-ops/followup-rewrite+schema/050)
> 核查方式:13 路只读子代理分域逐条核查(worktree detached @ee6946d)+ 亲手复算 RB-03 合并冲突与 P2-16 全仓 lint + 承重引用抽查(routes/agent.py enqueue 块、qa_logger.py 掩码块逐字复核)。所有 file:line 以 ee6946d 为准。

## 0. 总裁决

**44 条 claim:属实 31 / 部分属实(纠偏)13 / 整条推翻 0。**

评审读码质量高(多处行号在 11 提交后仍精确命中,P3-02 四个行数、P2-17 的 346、P2-16 的 85 全部逐位复现),NO-GO 总裁决与"分能力放行"框架站得住。但存在三类系统性偏差,核查时须区分「评审事实」与「评审定级/归因」:

1. **把已在其自审基线(c34b1da)之内的修复当遗留缺陷**(≥6 处):当日制 LEGACY ack、typ 双向拒斥、`RAG_SESSION_TOKEN_MIN_IAT`、DataWorks py3.7 钉版(9 脚本)、requirements.txt 顶层钉版+redis、认领失败改 409。与 07-16 外审同款毛病(倒读刚落的加固)。
2. **把记录在案的拍板/有意设计当疏漏**(≥6 处):未配 CA 显式明文(ec3ca8a 修复本体)、TLS 告警不阻断(用户拍板"CA 到位再升硬断")、不可绕过 production profile(07-16 台账明确拒绝,替代=当日 ack)、Stream 异常 ACK OK(模块头文档化取舍)、成本诚实 NULL(schema/023 拍板"不编造单价")、coverage 先看后守(ci.yml 注释明示)。
3. **四处子事实错误**(均不推翻所在条目主体):P2-02"无 min-issued-at"(撤销杠杆已存在)、P2-23"估算为 0"(实为诚实 NULL,且价表机制根本不存在)、P2-18"api.py 等"(api.py 从未用过 on_event,仅剩 routes/agent.py:344 一处)、P2-15"无 schedule/dead-man"(launchd 日跑+心跳代码俱在——但见下:实测比评审说的更糟)。

**核查副产品(评审未发现、本次实测新增):**
- **告警链端到端断裂(升级 P2-15)**:launchd ops-monitor 当天(07-18 02:31)实跑,但 `RAG_OPS_ALERT_WEBHOOK` 未配 → 当天 critical(RDS↔HA3 parity drift)被 `SUPPRESSED-CRITICAL`;且 PROD-RO 只读守卫挡掉心跳写 → `read_heartbeat_age_hours` 恒 None → governance 的 >26h 红灯判定**永不触发**,被动 dead-man 事实失效。
- **gap_groups_node.py(评审后 d98d45a 新增)仍用裸 `extractall`**——P1-02 的不统一面在评审后继续扩大。
- `ontology/store.py::record_population_snapshot` 与 P3-01 三 API 同样无 caller/审计(同罪面+1)。
- RB-03 冲突数从 25(@c34b1da,亲手复算精确吻合)涨到 **38**(@ee6946d)——大合并越拖越贵。

## 1. 逐条裁决表

裁决:✅=属实 ✅🔧=属实但已修复(修复 commit 在评审基线后) ⚠️=部分属实/纠偏 ☁️=本地不可验证(云端/运行态)。批次列见 §2。

### Release Blockers

| ID | 裁决 | 关键证据/纠偏 | 批次 |
|---|---|---|---|
| RB-01 公网明文入口 | ✅(5/5 子证据成立) | http_hardening.py:10-18/52-86 IP 面放行系**文档化过渡设计**(等 [CARD-CB-HTTP] 存量归零关 HTTP 回调,dingtalk_bot.py:128 注释);sae_canary_deploy.py:32 明文 IP 默认属实;全仓 0 安全响应头 | B5(头)+B7(云侧收口) |
| RB-02 RDS TLS 明文 | ⚠️(事实属实,归因过时) | "未配 CA 显式明文"**就是** ec3ca8a 修复本体(pymysql 2.x PREFERRED 坑);CA 已双分支落包(certs/aliyun-rds-ca.pem,评审说"未配置 CA"的前提已变);真残留=告警未升硬断(拍板前提已满足)+dataworks 3 节点/5 scripts 未接 pymysql_ssl_args+无 Ssl_cipher 验证代码 | B3+B7 |
| RB-03 25 冲突不可合 | ✅(亲手复算精确 25) | 现涨到 38(origin/main vs ee6946d);大合并为既定计划([[main-sync-cherrypicks]]) | B7 |
| RB-04 readiness 契约缺口 | ✅(1 处小纠偏) | 探针层止步 agent-037/全局-038(readiness.py:275-288),**043-050 零探针**;台账层经 MIGRATION_MANIFEST.tsv 覆盖到 050 但默认 report-only;strict 默认关(readiness.py:264-269);flag 条件探针**机制存在**(agent/ontology 总闸)只是未延伸到 durable-dispatch/ingest-lease | B2 |
| RB-05 无当前 HEAD 质量/压测证据 | ✅🔧(a2277fb/b9424f7/cb0fb9e) | 评审时点属实;现 freshness 门恰 3 FAIL=有意红灯可见化、agent gate 对旧格式基线 fail-closed exit 2、压测报告绑 git 指纹且 b9424f7 提交线 full 档全绿(非 draft 零失败);剩 A2/B2 refreeze+E2 翻硬门 user-gated(runbook 已有) | B7 |
| RB-06 密钥姿态无 attestation | ⚠️ | 当日制 ack(config.py:1061-1082)、typ 拒斥、MIN_IAT 全在基线内;真残留=①security_posture 只报 legacy-open 一项(readiness.py:433-434)②生产不强制独立 upload key(=P2-01)③轮换/branch-protection/SAE 生效 ☁️ | B2+B7 |

### P1

| ID | 裁决 | 关键证据/纠偏 | 批次 |
|---|---|---|---|
| P1-01 enqueue fail-open | ✅ | routes/agent.py:835-836 捕获全异常回退直通(逐字复核);认领失败已 409(3fc0032,基线内);与 dispatch_outbox.py:13-15 fail-closed docstring 冲突属实;系 PR-3 D5 有意决策,但评审方向正确——flag 开启时应 fail-closed | **B1** |
| P1-02 DataWorks 制品不可变性 | ✅ | 8/11 节点固定名下载零校验;裸 extractall 6+1 处 vs 安全解压仅 ontology 2 节点;全部无尺寸预算;**评审后新增 gap_groups_node 仍裸 extractall** | B4 |
| P1-03 ZIP/XML 资源耗尽 | ✅ | image_extraction_utils.py:674-732 裸 ET.fromstring(全仓唯一生产点);defusedxml 零依赖零 import;**50MB 只挡自助上传,DataWorks 摄取路径零大小闸**(MAX_UPLOAD_BYTES 在 pipeline_nodes 零引用) | B4 |
| P1-04 单实例/拓扑声明 | ✅ | Dockerfile:76 workers 1;:66-69 注释已过时(AWAITING_COMMENT 实已外置 RDS,feedback_handler.py:241);RAG_EXPECTED_REPLICAS 纯手工(config.py:1087 自认"无法自动感知"),零 SAE API 核验;拓扑守卫存在但被手工声明门控 | B7(演练)+B5(注释) |
| P1-05 灾备演练缺失 | ✅ | 全仓 RPO/RTO/PITR 仅 2 处且均为"缺失/待办"性质;ha3_rebuild_recovery_plan 草案关键未知项属实(:73-75 工单必问);6-28 事故恢复实录≠演练制度 | B7 |
| P1-06 guard 默认可关 | ✅ | config.py:550 默认 False;生产姿态断言组(1028-1075)无 guard 条目;只读注册仅 warning(routes/agent.py:374-378),唯 HIGH_WRITE 注册硬断(agent_tools/__init__.py:62-66) | **B1** |
| P1-07 buildpack 旁路 | ✅ | requirements.txt:15 自认"生产 SAE 真实安装源";顶层==钉版+redis==8.0.1 已在基线内(3983e44);无 hash、阿里镜像、CI 零消费 requirements.txt(对齐仅靠注释约定);完整 hash 锁已记录为"随下次 SAE 重打包" | B7(随重打包) |
| P1-08 无 policy-as-code | ⚠️ | 命令式姿态断言**成套存在**(auth/ACL/PII/self-approval/多副本组合/禁 Gemini,config.py:1026-1119)——"无安全 flag 组合校验"不成立;缺的是声明式载体;"不可绕过 profile"系 07-16 台账**明确拒绝**(brick 风险,替代=当日 ack);真残留=_env_bool 非法 token 静默回默认(config.py:776)+57 处散装内联布尔解析闭集不一致 | B2(未知变量检测)+B6 |
| P1-09 PII 掩码 fail-open | ✅(1 纠偏) | qa_logger.py:85-87 异常退原文(逐字复核);content_blocks 对照组丢字段属实;Agent 三条原子事务路径同经此掩码;纠偏:**生产关 flag 会启动硬断**(config.py:1031-1036 P2-27),"配置允许关闭"仅限非生产——残留=flag-on 异常通道 | **B1** |

### P2

| ID | 裁决 | 关键证据/纠偏 | 批次 |
|---|---|---|---|
| P2-01 upload key 回退 session key | ✅ | auth_token.py:106-113;生产无独立 key 断言;typ 双向拒斥已缓解(降为纵深项) | B3(warning+posture)→B7(硬断) |
| P2-02 typ 永久兼容 | ⚠️ | 方向①(无 typ 旧 token 当 session)属实但被 2h TTL 自然收敛+伪造需签名密钥(无增量攻击面);"无 min-issued-at"**事实错误**(RAG_SESSION_TOKEN_MIN_IAT 在,auth_token.py:223-229);方向②③已被拒斥 | 维持现状(记录) |
| P2-03 VLM 缓存 MD5 | ✅ | unified_extractor.py:1824 md5→:352 cache_key;pub/sec 二值 namespace;RAG_VLM_CACHE_VERSION 后缀存在但默认空 | B4 |
| P2-04 Stream 异常 ACK OK | ✅ | dingtalk_stream_runner.py:130-133;模块头 :25-27 文档化取舍;**两阶段 dedup 已存在**(dingtalk_bot.py:1233-1241,评审建议的前提已备)只是 ACK 语义未据此改 | B6(拍板项) |
| P2-05 localStorage 30 会话 | ✅ | useAsk.ts:582-591;无 TTL;唯一缓解=uid 戳换人清空 | B5 |
| P2-06 sessionStorage token | ✅ | useAuth.ts:113-139;系带威胁模型的有意决策(#F-console-urltoken:tab 级/401 即清);真修=session.ts:18 过时注释 | B5 |
| P2-07 无安全响应头 | ✅ | 全仓 0 命中;唯 console meta referrer;**注意:console 被钉钉 PC 工作台内嵌,frame 保护必须 allowlist 钉钉域,不能 DENY** | B5 |
| P2-08 webhook urlopen 无 allowlist | ✅ | alerting.py:71-72 零校验(POST 型内网 SSRF);范围收窄:其余发送点全部硬编码官方域名或已有 allowlist(dingtalk_bot.py:137-154 范式在而未套用) | B5 |
| P2-09 hot-questions cohort 死功能 | ✅ | 后端 api.py:2317-2326 匿名短路静态兜底 × 前端 useAsk.ts:576 `{auth:false}`——两侧各自有意,叠加成死功能 | B5 |
| P2-10 成本账本 fail-open | ✅ | cost_breaker.py:71-73/96-97/211-214,注释自认"轻微超冲可接受";daily_budget 默认 0=关;缓解=进程内三道硬闸不受影响 | B1 |
| P2-11 actions mutable tag | ✅ | 19/20 处 mutable(仅 trivy SHA 钉死——还是被上游删 tag 逼的) | B4 |
| P2-12 coverage 无门 | ✅ | ci.yml:64-65 注释明示"先看后守"分阶段;基线已有(79%)→可以谈"守"了 | B6 |
| P2-13 DataWorks 运行时安装 | ⚠️ | 钉版已在基线内全量落地(567519a+b028a65+3983e44,9 脚本非 5);残留=py3.8+ 分支浮动+oss2/ha3sdk/jieba/typing_extensions 未钉+py3.7 不入 CI+7 脚本零测试+钉版回归测试只盖 1 节点 | B4 |
| P2-14 无 OTel | ✅ | 全仓零埋点;request-id 单服务内在但出站零透传 | B7(立项) |
| P2-15 告警调度无证据 | ⚠️**实测更糟** | 纠偏:launchd 日跑(07-18 02:31 有日志)+dead-man 心跳代码在(queue_monitor.py:194/222);实测:webhook 未配→SUPPRESSED-CRITICAL、PROD-RO 挡心跳写→红灯永不触发——**整链端到端不通** | B6(代码)+B7(配置) |
| P2-16 全仓 lint 85 项 | ✅(亲手复算) | 85 精确复现(F401×24/F541×21/E402×21/…);4 F821 全为 odps 注入(评审已预判);CI 仅管 core/tests(该口径全绿) | B6 |
| P2-17 346 配置引用 | ✅(1 纠偏) | 346=基线唯一 flag 名精确口径(现 353);config.py 外直读 261 行(含 api.py:458/494 安全关键直读);纠偏:typed 配置中心**存在**,真问题是双轨 | B2(小步)+B7(立项) |
| P2-18 on_event 弃用 | ⚠️ | 仅剩 routes/agent.py:344 一处(有 atexit 双保险);**api.py 从未用过 on_event**(git log -S 空),"api.py 等"表述错误 | B5 |
| P2-19 purge_subject 范围 | ✅ | 12 表覆盖;三缺口与 retention.py:568-575 代码自留台账逐字一致(评审实为复述代码自认) | B7 |
| P2-20 无 a11y 门 | ✅ | 0 axe;aria-* 93 处/30÷57 文件(覆盖比"tab/图形"宽但确无门) | B7(立项) |
| P2-21 /api 未版本化 | ✅ | 88/93 路由裸 /api/*、零版本化;缓释:全部客户端同仓同发、无外部消费者 | B7(立项) |
| P2-22 无合规工件 | ✅ | DPA/DPIA/no-training/处理清单全仓零存在;缺口已被两份内部审计文档点名但工件本体无 | B7 |
| P2-23 价表可空无经济门 | ⚠️ | "估算为 0"**推翻**——cost_estimate 诚实 NULL 系 schema/023 拍板("不编造模型单价",kb_console.py:1560-1561);且价表机制根本不存在(比评审说的更彻底);"无 RMB 单位经济门"属实 | B7(立项) |

### P3

| ID | 裁决 | 关键证据/纠偏 | 批次 |
|---|---|---|---|
| P3-01 store 三写 API 无 caller/审计 | ✅ | store.py:1436/1469/1501 零守卫零审计(对照 mint_object 等有);定级前提成立——唯一生产调用链=离线 ontology_seed.py CLI(dry-run 默认+prod 拒绝);**+record_population_snapshot 同罪** | B7(升级条件监控) |
| P3-02 god modules | ✅ | 8378/3045/2992/2894 四数在 c34b1da 逐位复现 | B7(立项) |
| P3-03 README 引缺失 CLAUDE.md | ⚠️ | 引用+仓内缺失属实;纠偏:**有意 gitignore**(.gitignore:57-58,含敏感端点不入公开仓)非意外缺失;README 单实例说明=默认态准确/能力态过时 | B5 |
| P3-04 stress 文档矛盾 | ⚠️ | dbprobe 降级宣称逐字属实+agent 500 属实;但 stress README:9 已明示 MySQL 前置——文档集自洽,缺一句互引 | B5 |
| P3-05 npm deprecated | ✅ | glob@10.5.0(传递)+lucide-vue-next@1.0.0 均实标 deprecated(npm registry 实查);后者官方迁移=@lucide/vue | B5 |
| P3-06 测试 warnings | ⚠️ | invalid escape 全仓恰 1 处(annotation_parser.py:270,raw 串被 ASCII 引号提前终结)+无 filterwarnings 治理属实;Pillow/fixture 两族抽样未复现(不推翻) | B5 |

## 2. 修复批次

原则沿用既有台账惯例:全部代码落 `claude/ontology-p0`;涉及现网 serving 的项按 [[main-sync-cherrypicks-2026-07-13]] 铁律摘上 main;行为变化一律带 kill switch + 回归测试;`make test`+`make lint` 绿为每批 done 标准。批次内条目可独立验收。

### 批次1 · fail-closed 语义四刀(最高优先,HIGH_WRITE/durable 开闸前置)

1. **P1-01**:`RAG_AGENT_DURABLE_DISPATCH=true` 时 enqueue 异常 → 503+Retry-After,不再直通(flag off 路径字节不变);对齐 dispatch_outbox docstring;集成测试三注入(enqueue DB timeout / 043 缺失 / enqueue 成功后执行前崩溃→恢复重驱)。
2. **P1-09**:`_redact_for_log` 异常不再退原文——正文置为不可逆占位(`[PII_REDACT_FAILED sha256:<16> len:N]`),warning 升 error;monkeypatch 异常回归测试。(历史行残留抽样=批次7,需 PROD-RO。)
3. **P1-06**:production/staging 且 `RAG_AGENT_ENABLE=true` ⇒ `RAG_PROMPT_INJECTION_GUARD` 必开(config 姿态断言组新增条目;文档注明与 B2 基线 refreeze 绑定——guard 翻转改变 L7 regime)。
4. **P2-10**:`daily_budget_rmb>0` 且日账本不可用 → 走既有 `cost_deferred` 通道(下轮重捡)而非跳过日闸;kill switch。

### 批次2 · readiness/schema 契约(RB-04 + RB-06b)

5. 契约探针 038→050 补齐:043 表/044 kind ENUM 含 resume/045 表/046 operation_id 列/047 cancel 列(operation 库);**新增 knowledge 库探针集**收 048 三 lease 列+idx/049 generation 列;050 rewritten_query;042 索引。
6. **flag-conditional critical**:`RAG_AGENT_DURABLE_DISPATCH`⇒043/044、`RAG_INGEST_LEASE_ENABLE`⇒048、`RAG_FOLLOWUP_REWRITE`⇒050——flag 开而契约缺 ⇒ critical not-ready(**不依赖 strict**;flag 开=运营者已声明依赖该 schema)。
7. **security_posture 全量自报**:auth/ACL/guard/卡片验签/TLS(ssl_ca 配置态+cipher 探测结果)/durable/strict/legacy-ack + 不含 secret 的配置 digest——补齐 RB-06 的机器可验证 attestation 代码侧。
8. 未知 `RAG_*` 变量检测:启动扫描 env 中 RAG_* 与已知消费集比对,未知→响亮 warning(拼错安全 flag 不再静默失效)。
9. (strict 默认翻转=批次7,维持"staging 验证后再开"拍板。)

### 批次3 · RDS TLS 收尾(RB-02 残留 + P2-01)

10. dataworks 3 节点(register/scan/stage3_with_cleanup)+5 个 scripts 连接位点接 `pymysql_ssl_args()`/显式 ssl_disabled——消灭残存 PREFERRED 版本依赖态。
11. **Ssl_cipher 自检**:配置了 ssl_ca 时启动后 `SHOW STATUS LIKE 'Ssl_cipher'`,空 ⇒ production fail-fast(配 CA 还明文=配置错误,可硬断不违拍板);结果进 security_posture。
12. production 未配 CA 的 warning 文案更新(CA 已随包,指明设 env 即启用);P2-01 production 无独立 upload key ⇒ warning+posture 字段。(两者硬断升级=批次7,随 SAE env 落地,避免 brick。)

### 批次4 · 供应链 + 解析 DoS(P1-02/P1-03/P2-03/P2-11/P2-13)

13. 统一安全解压(zip-slip+总解压量+成员数+单成员+压缩比预算)内嵌进全部 9 个解压点(节点自包含粘贴脚本,逐个复制;含新 gap_groups_node)。
14. 制品完整性:打包脚本产 `.sha256` sidecar 资源,节点下载双资源校验(sidecar 缺失过渡期 warning,后续硬化);运行日志记 zip digest——闭环"运行结果可追溯到提交"。
15. defusedxml 进依赖,image_extraction_utils 五处 fromstring 切换;摄取路径大小闸(下载前 OSS HEAD size>阈值→NEEDS_REVIEW 不下载);恶意样本回归(zip bomb/XML 炸弹/超深嵌套)。
16. **P2-03**:VLM cache MD5→SHA-256,`RAG_VLM_CACHE_VERSION` 默认置 "2" 整体失效(注记:存量图下次触碰重审计,一次性 VLM 增量成本,量级=再摄取面而非全量)。
17. **P2-11**:19 处 actions 全部 SHA 钉死(带版本注释,与 trivy/Dockerfile 同纪律)。
18. **P2-13 残留**:py3.7 分支补钉 oss2/alibabacloud_ha3engine_vector/jieba/typing_extensions;`test_node_deps_pinned` 从 1 节点扩到全部节点。

### 批次5 · 边界与前端小刀(P2-05/06/07/08/09/18 + P3-03/04/05/06)

19. 安全响应头中间件:nosniff/Referrer-Policy/Permissions-Policy 全局,CSP 先 report-only;**frame-ancestors 必须 allowlist 钉钉域**(PC 工作台内嵌 console,DENY 会打死现网入口)。
20. alerting.py webhook allowlist(https + 默认 *.dingtalk.com,env 扩展)——套用 dingtalk_bot 既有范式。
21. hot-questions:console 调用改带 auth(匿名兜底保留),cohort 复活+测试。
22. localStorage 会话:TTL(建议 14 天)+登出清除;30 上限保留。
23. 注释/文档五小刀:session.ts:18 过时注释、README 的 CLAUDE.md gitignore 说明+单实例"默认态 vs 能力态"、stress README↔dbprobe 互引一句、annotation_parser.py:270 raw 串修正、routes/agent.py on_event→lifespan(atexit 兜底保留)。
24. lucide-vue-next→@lucide/vue 迁移(glob 为传递依赖随上游)。

### 批次6 · 观测/告警骨架(P2-12/15/16 + P2-04 拍板项)

25. **P2-15 代码侧**:governance 增"期望心跳"配置——开启后 heartbeat=None 直接红(不再依赖 >26h 判定);SUPPRESSED-CRITICAL 计数上浮到 governance 面板。(webhook/secret 配置本体=批次7。)
26. **P2-12**:coverage 加全局 fail-under 保底(当前 79%,建议 75 起步),关键模块清单二期。
27. **P2-16**:ruff 纳管 scripts/+dataworks_nodes/+deploy/(per-file-ignores:odps F821 声明、节点脚本 E402 豁免),85 项清零;CI lint 范围同步扩。
28. **P2-04(需拍板)**:Stream 异常 ACK 语义改"发送前失败→可重试 NACK(两阶段 dedup 已备)、发送后失败→维持 ACK OK"——有重复回答风险面,方案先行文档化,Sam 拍板后动工。

### 批次7 · user-gated / 云侧 / 立项(不动工,决策与执行都在 Sam)

**云侧动作**(对应评审 0-24h/1-7d 表):
- RB-01:安全组/CLB 关 EIP:8000 → [CARD-CB-HTTP] 存量计数(SLS 只读凭证待配,[[aliyun-cli-sls-tooling-2026-07-18]])→ `RAG_DINGTALK_HTTP_ENDPOINTS_ENABLE=false` → 云侧 301/HSTS → 外扫验证。
- RB-02:SAE 重打包 env 加 `RAG_RDS_SSL_CA=opensearch_pipeline/certs/aliyun-rds-ca.pem`;DataWorks 凭据块同步;之后升硬断言;叶子证书 2027-07-17 年续入日历。
- RB-03:大合并(冲突 25→38,越拖越贵)。
- RB-05:A2/B2 live refreeze + E2 翻硬门 + staging 压测(runbook=docs/rb05_scorecard_realignment_2026-07-18_DRAFT.md §2)。
- RB-06:DingTalk/session/upload 三类 secret 轮换(卡回调需 FORCE_UPDATE,否则反馈按钮注册被拒)+branch protection UI+SAE 重打包 env 清单(REQUIRE_AUTH/ACL_FAIL_CLOSED,ack 用当日制格式)。
- P1-04:SAE 双副本(Redis 内网串已备)+拔 Dockerfile workers 钉子+kill/restart 演练(PR-3 尾巴)。
- P1-05:首次 PITR+HA3 重建+OSS 恢复演练;阿里工单问 rebuild 草案 4 个未知项。
- P1-09 尾巴:qa_session_log 历史残留抽样扫描(PROD-RO)。
- P2-15:launchd env 配 RAG_OPS_ALERT_WEBHOOK/SECRET;DataWorks monitor 节点控制台粘贴恢复。
- P2-22:DPA/DPIA/数据处理清单/供应商 no-training 条款(法务面,代码不可解)。
- 批次2/3 的硬断升级三件(strict 默认、TLS 硬断、upload key 硬断)随 SAE env 落地后翻。
- schema/050 三环境 apply 状态确认(新迁移,follow-up rewrite flag 默认关)。

**立项级(维持评审定位,不进近期批次)**:P2-14 OTel 端到端 tracing、P2-17 typed-settings 收敛+双轨清理、P2-21 /api/v1 版本化策略、P2-19 离职钩子+跨系统删除对账、P2-20 axe/WCAG 门、P2-23 价表+单位经济、P3-02 god-module 拆分。P3-01 维持 P3 但设升级监控:三 API(+record_population_snapshot)一旦接 route/Agent 工具即升 P1。

## 2.1 批次执行状态

### 批次1 ✅ 已落地（2026-07-18，ontology-p0；as-built 与批次定义的差异注记如下）

1. **P1-01**（`routes/agent.py`）：flag 开启时受理面按异常时点分流——**enqueue 失败 → 503 + `Retry-After: 5`**（命令不 durable=没接单，客户端可安全重试）；**enqueue 成功后 claim 异常 → 409**（命令已在案，恢复扫描必然重驱，本方直通=双执行——批次定义只写了 503，实现时按 command-as-truth 语义补上这条分叉）。逃生口 `RAG_AGENT_DISPATCH_ACCEPT_FAILOPEN=true` 还原旧直通回退（043 未 apply 过渡窗口专用）；flag off 路径零变化。旧测试 `test_p3_enqueue_failure_falls_back_to_direct` 行为更替改判为 `test_b1_enqueue_failure_fail_closed_503`，另增逃生口/claim 异常 2 条；「enqueue 成功后执行前崩溃→恢复重驱」由既有 `test_p3_unbound_queued_redriven` 覆盖。
2. **P1-09**（`qa_logger.py::_redact_for_log`）：掩码异常不再退回原文——正文写不可逆占位 `[PII_REDACT_FAILED sha256:<16> len:N]`（可对账/去重，不可还原），warning 升 error+exc_info；逃生口 `RAG_QA_LOG_REDACT_FAILOPEN=true` 还原旧行为。三条 monkeypatch 回归（占位/逃生口/正常掩码不受扰）。历史行残留抽样仍在批次7（PROD-RO）。
3. **P1-06**（`config.py` 姿态断言组）：production/staging 且 `RAG_AGENT_ENABLE` 开而 `RAG_PROMPT_INJECTION_GUARD` 未开 → 启动 ValueError（与 self-approval 同级、无 ack 逃生口——现网 prod 未开 agent，零 brick 风险；**staging 起 agent 档从此必须连带设 guard**）。断言文案与注释均标注「guard 翻转改变 L7 regime，生产开启随 B2/RB-05 agent 基线 refreeze 一并做」。五条守卫测试（prod/staging 触发+三个放行象限）；连带：两组拓扑守卫测试基座（test_agent_pr3_stage_d / test_agent_batch4_fixes 的 `_BASE`/`_TOPO_BASE`）补 `RAG_PROMPT_INJECTION_GUARD="true"` 前置——它们构造 prod+agent-on 形态专测拓扑守卫，与 `_fresh_load` 注入当日 ack 同型。
4. **P2-10**（`extraction/cost_breaker.py`）：`daily_budget_rmb>0` 且账本读不可用（非 simulate）→ 瞬态拒绝 `DAILY budget ledger unavailable (fail-closed)`（新增进 `_TRANSIENT_DENY_MARKERS` → 既有 cost_deferred 通道顺延，不封存）；simulate 与逃生口 `RAG_COST_DAILY_LEDGER_FAILOPEN=true` 维持旧跳闸。`_ledger_read_today` 签名未动（两处既有零参 monkeypatch 不受扰）；`test_g8_daily_ledger_failopen` 显式钉 simulate 语义，新增 4 条 B1 测试。**范围注记**：`_ledger_add` 记账失败仍 fail-open（欠计不拦截），完整修复需事务化记账——维持评审 P2 定位不在本批。

新增 env（全部为 kill switch，默认 off=新 fail-closed 行为）：`RAG_AGENT_DISPATCH_ACCEPT_FAILOPEN` / `RAG_QA_LOG_REDACT_FAILOPEN` / `RAG_COST_DAILY_LEDGER_FAILOPEN`。

未验证声明：真实 HA3/RDS 上的 enqueue 故障注入、钉钉端到端、SAE 部署包——本批全部在本地测试栈验证（simulate + 本地 MySQL）。

### 批次2 ✅ 已落地（2026-07-18，ontology-p0；as-built 注记如下）

5. **契约探针 038→050**（`readiness.py`）：基础 agent 契约列补 047 `cancel_requested_at`（跨实例 cancel 裸调 UPDATE=500，蓝绿窗口假健康）；新增 flag-conditional 探针组——043 表+044 kind ENUM（新 `_enum_contains` 探针，MODIFY COLUMN 无新表新列可探只能读 COLUMN_TYPE）、048 三 lease 列+idx_lease_expiry（**knowledge 库探针集**，此前探针全在 operation/ontology 库）、045 表+046 列（写工具门）、050、049。**as-built 偏离两处**：①042 `idx_user_started`（纯性能索引）有意不进契约探针——缺失=慢而非坏，由台账层 unapplied 检测覆盖；②050 探针 **report-only** 而非台账定义的 critical——qa_logger 对缺列有 1054 降级+TTL 负缓存（改写功能照常、仅溯源列不落），摘流量过度。049 同为 report-only（drain 双路径优雅降级）。
6. **flag-conditional critical**（`api.py` critical_ok）：`RAG_AGENT_DURABLE_DISPATCH`⇒043/044、`RAG_INGEST_LEASE_ENABLE`⇒048、写工具启用⇒045/046——flag 开而契约缺（或探针 error）⇒ critical not-ready，**不依赖 RAG_READY_SCHEMA_STRICT**（strict 默认翻转仍留批次7）。
7. **security_posture 全量自报**（`readiness.security_posture_report`）：auth/ACL/guard/agent/durable/HIGH_WRITE/ingest-lease/strict 开关态 + 卡回调 secret 与 RDS CA 的 configured/missing（**不含任何 secret 值**）+ legacy-ack（旧字符串语义保留在 `legacy_open_ack` 键）+ **聚合配置 digest**（排序的 name=sha256(value)[:12] 逐行拼接再整体 sha256[:16]——同配置同 digest 可跨副本比对，逐变量哈希不外泄）+ 未知变量清单。cipher 实测=B3。
8. **未知 RAG_* 检测**（新 `rag_env_registry.py` + `config.load_config` 钩子）：432 名冻结清单（六根 *.py 字面量 + config.py `_env*()` 裸名两模式；排除注册表自身防「自证存在」；tests/ 有意不扫）；`tests/test_rag_env_registry.py` 双向新鲜度门（漏登记/悬空条目即红——首跑就抓到我自己注释里的拼错示例）；启动未知名响亮 warning + posture 自报，report-only 不拦启动。重生成：`python -m opensearch_pipeline.rag_env_registry`。

未验证声明：真实三环境 /api/ready 响应（新探针在 staging/prod 的实际状态词）、SAE 部署包——本批在本地测试栈验证。

## 3. 与评审验收条款的映射备注

- 评审 RB-04 验收"durable enqueue 失败返回 503"=批次1-1;"为每个 flag 建条件探针"=批次2-6;"strict=true"=批次7。
- 评审 RB-06 验收"配置 attestation/readiness 安全姿态摘要"=批次2-7;轮换类=批次7。
- 评审 P1-02/03 验收(SHA/签名/统一解压/defusedxml/预算)=批次4;"隔离 worker+cgroup 限额"未采纳(DataWorks serverless 执行器不可控 cgroup,以预算+fail-fast 替代)。
- 评审 G0-G11 门中,代码可解部分集中在 G3(posture 自报)/G4(契约探针)/G5(sidecar 校验)/G6-G7(RB-05 已闭环的指纹与新鲜度门);其余门本质是云侧执行与演练,全在批次7。
