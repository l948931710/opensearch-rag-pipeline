# main 全面代码评审(2026-07-20)逐条核查台账

> 核查日期:2026-07-21
> 评审原文:`docs/main_code_review_2026-07-20.md`(审 main@ae4cbc2)
> 核查基线:本地 main@6f53925(评审基线后仅 1 个 README-only 提交,改动 README 第 3/5/21 行;全部代码类 file:line 与评审所见逐位一致)
> 核查方式:5 路只读子代理分域逐条核查(每条引原文代码求证控制流,双向怀疑)+ 亲手复跑全部「Verification performed」数字 + 承重结论抽查(api.py:537-548 严格模式降级条件、spot_checker.py:259-271 无 CAS 门控的 chunk 失活,均逐字复核)。全程零改动 tracked 文件(git status 复核为证)。

## 0. 总裁决

**17 条 finding:属实 15 / 部分属实(纠偏)2(F14、F15)/ 整条推翻 0。全部 ~50 个 file:line 引用精确命中或仅差几行(def/注释与操作行之别),无一处 LINE-MISMATCH。**

评审读码质量高,REVISE 总裁决对「从 main 原样发布」这一前提站得住。但与 07-16/07-17/07-18 三轮外审核查同款,存在四类系统性偏差,引用时须区分「评审事实」与「评审定级/归因」:

1. **把记录在案的拍板/有意设计当疏漏**(≥4 处):F2 TLS 告警不阻断(拍板"CA 到位随 B7 SAE env 一并翻硬断",config.py:700-704 注释在案,CA 已随包 certs/aliyun-rds-ca.pem);F7 钉钉不过限流器(api.py:612 注释明示);F8 ACK 语义(B7-P2-04,Sam 拍板 2026-07-18,runner:25-32 模块头文档化);F16 浮动版本(钉版曾落地 e3c6fa0,**a64aa86 于 07-20 有意回退**——SAE buildImage exit 1,批次8,回退前提写进 requirements.txt:35-38)。
2. **未区分 main 与 ontology-p0 的既定分叉**(拍板 2026-07-18:agent 架构长期留分支,main 不吃):F1 生产姿态硬断言(P0-07d)、F6 摄取租约(PR-4/schema 048)、F9 checksum 列(schema/032)、F10 三件套(Redis 后端/durable dispatch/拓扑守卫)——**全部在 op0 已存在,main 0/4**。评审说"absent on main"字面全对,但漏了修复已在、且 DataWorks 生产代码包本就从 op0 打(stage1_node.py:31-35 硬设两 flag)。
3. **漏报缓解面**(收窄爆炸半径,不推翻主体):F1 匿名≠全量数据(仅 public 层+四层限流+生产关 /docs);F3d/F5 serving 侧 `main_hit_revalidate` 默认 ON 挡直接泄漏;F4 写权/控制台权确实被撤、钉钉离职者自然归零组;F7 通道验签+msgId 去重四态机+general 层日配额;F8 同步窗瞬时失败会请求钉钉重投(ACK 非无条件);F13 单文件 200MB 门(RAG_EXTRACT_MAX_BYTES)在。
4. **两处表述/算术偏差**(F14、F15,见逐条)。

**已知 gap 重述占比高**:F5(双活版本)、F6(无租约)本就是 CLAUDE.md「Open reliability gaps」记录在案的已知项;F1/F4 的 flag 默认 off 是 07-10 main P0 加固的既定「生效差部署+开 flag」状态。评审的增量价值主要在 F3(对账竞态的具体交错)、F9(台账幂等语义)、F12(镜像全量重传)三处新面,和把已知项拼成整体 REVISE 裁决。

## 1. 逐条裁决表

裁决:✅=属实 ⚠️=部分属实/纠偏 ☁️=仓库不可验证(SAE env 注入/运行态)。

### Release Blockers

| # | 裁决 | 关键证据/纠偏 |
|---|---|---|
| F1 认证默认关 | ✅ | api.py:566 flag 门+:573 匿名透传+test_main_p0_hardening.py:64 锁行为,三引全中;main 的 config 生产姿态块**无** REQUIRE_AUTH 断言(仅 op0 有 P0-07d 硬断);缓解=匿名仅 public 层(api.py:810 user_dept=None)+ask 限流 fail-closed;现网 SAE env 是否已注入 ☁️(07-16 台账早已把 REQUIRE_AUTH+ACL_FAIL_CLOSED 列为 SAE 重打包前置) |
| F2 无 CA 显式明文 | ✅(拍板在案) | config.py:197-199 无 CA→ssl_disabled、:705-710 生产仅 warning、prod_access.py:67-78 同款,全中;纠偏:api.py:210-213 启动自检系「CA 已配时的接线自检」(B3,配了 CA 后接线明文会 RuntimeError 拒启),无 CA 时 no-op 属实但非"跳过验证"语义;显式明文本身是 RB-02 修复本体(pymysql 2.x PREFERRED 非确定坑);CA 已随包;"告警不阻断→B7 随 SAE env 翻硬断"拍板注释在案 |
| F3 对账竞态 | ✅(全 4 子条) | 3a: :239-243 裸 SELECT 无认领;3b: 版本 UPDATE 有 CAS(:260-265)但 chunk 失活**不看 CAS rowcount**(:266-270,亲手复核);3c: kb_restore(kb_console.py:2544)的 PENDING_DELETE→NOT_INDEXED 翻转只防**下一轮**对账,防不了在飞;3d: :373-377 无 CAS(同事务下一句反而有 CAS,不一致);**最坏交错=恢复成功响应后文档被静默永久下线**(NOT_INDEXED+is_active=0,stage-3 loader 只挑 is_active=1,无自愈);加重项见 §2;缓解=serving revalidation 挡直接泄漏,BLOCKER 定级属频率判断 |

### Major(正确性/安全)

| # | 裁决 | 关键证据/纠偏 |
|---|---|---|
| F4 撤权 fail-open | ✅(4d 比评审更糟) | 4a-4c 全链路成立:`AND is_active=1` 过滤(dingtalk_identity.py:340-347)→墓碑等同未见→钉钉 API 重取→ON DUPLICATE KEY 不碰 is_active(:415-432)→api.py:786 照发全组令牌;**4d 加重:RAG_ACL_FAIL_CLOSED 开着也拦不住**——无在册行 db_ok=True 保留令牌组,只有 DB 异常才降级(api.py:541-548,注释自认);4e 属实但以偏概全:跨部门授权路径 `_deny_revoked_cross_dept` 恒 fail-closed(retriever.py:568-571);缓解=写权/控制台权经 resolve_kb_identity fail-closed 确实被撤、钉钉离职者取回空组=仅 public、令牌 TTL 2h |
| F5 部分索引双活 | ✅(已知 gap+新面) | 5a/5b/5c 三引精确;关键坐实:chunk 自 DAG-2 INSERT 即 is_active=1(pipeline_nodes.py:5541-5604),已推子集在 HA3+RDS 双活,revalidation SQL 不查 version 完整性(retriever.py:639-641),stitcher 注释自认「双活版本窗口」(:1214);缓解=下轮 drain 通常自愈(FAILED 可重捡);**新面:chunk 进 DEAD 后窗口永不闭合**(stranded 对账谓词 spot_checker.py:333-336 永久排除);CLAUDE.md 已记录为已知 gap |
| F6 摄取无主权 | ✅(已知,修复在 op0) | 6a: stage-1 全程不翻状态(orchestrator:683-684 注释自认守卫必需);6b: 行锁提交即放**系设计**,真 gap=LOADING 无 holder 身份;6c: :595-625 纯年龄制打回,活 worker 终态写无 fencing;6d: node_acquire_index_lock(:5935-5942)同款,takers 互斥但原 holder 不被隔离;**main 上 ingest_lease.py/lease 列/schema 048 零命中**(全在 op0,PR-4) |
| F7 钉钉旁路熔断 | ✅(有意+缓解漏报) | api.py:1102 admit_ask 在;dingtalk_bot.py 全文唯一限流调用=general 层 admit_general(:1120,flag 默认 off),KB 主路径零准入,**且钉钉问答不计入全局 2000/day 熔断→熔断少算真实开销**;:1729-1736 每消息一条无界 daemon 线程属实(无任何 semaphore/executor);api.py:612 注释明示钉钉不过此函数(有意);缓解=HMAC 验签 300s 容差+msgId 四态去重(重试封顶 3)+群聊降级 |
| F8 ACK 先于持久化 | ✅(拍板取舍) | :1725/1736/1739 精确;纠偏:ACK **非无条件**——同步窗瞬时失败抛 TransientMessageError→STATUS_SYSTEM_EXCEPTION 请求钉钉重投(封顶 3 次),重投可复用已算答案(fetch_answer_by_message_id);模块头 :25-32 文档化拍板(B7-P2-04);**真残留=ACK 后 RAG 线程崩=用户只收到"查询中"永无下文**;8c 反馈竞态坐实:message_id 先于 qa 行到手,_feedback_owns_message 查无行→403,崩溃后该消息反馈**永久** 403(fail-open 分支只覆盖 SELECT 异常非无行) |
| F9 迁移台账非幂等 | ✅(含 1 处自我纠偏) | 9a/9b/9c/9d 四引全中;**049:20-21 头注声称的 apply 脚本 information_schema 预检在两分支都不存在**(_existing_tables 只打印,_table_names 只认 CREATE TABLE)——重跑 049 必 1060 崩(已修);幂等纪律混杂:012/014/015 头注引用的守卫在已退役的 scratch 一次性脚本里。**⚠️ 自我纠偏(07-21 实测,见 §4):原写「main 缺 032 → 漂移检测整体惰化」过头**——缺的只是 main 的 schema/ 文件与「纯从 main 建库」路径;staging/prod 的 checksum 列**早在 2026-07-10 由 `scratch/apply_ontology_dbs_20260710.py` 应用并记账**,现网检测本就生效 |
| F10 README 虚标多副本 | ✅ | README:100-102 两处宣称「Redis 四态后端+durable dispatch(PR-3)+RAG_EXPECTED_REPLICAS 拓扑守卫,配置不完整启动拦截」;main 实况:session_store.py:51 纯进程内、rate_limiter.py:274 同、Dockerfile:46 `--workers 1`、全仓 .py 零 redis/durable/RAG_EXPECTED_REPLICAS 命中——**0/3;op0 3/3**(branch config.py:1148-1178 守卫真在);6f53925 未touched此段;对从 main 部署者实质误导属实;(往轮审计引 Dockerfile:76=op0 的 78 行版,main 48 行版 flag 在 :46,两版皆单 worker) |
| F11 readiness 假绿 | ✅ | :718 探针清单=RDS SELECT 1+HA3 零向量+DashScope 配置有无+embedding 契约,**运营库表(qa_session_log/反馈/审计)零探针**;纠偏:embedding-contract 是真 schema 级深检,"generic config only"措辞低估了它;qa_logger.py:583-598 全吞属实(1054/1146 升 logger.critical 但 **send_ops_alert 零调用**=不 page 人);:149-153 Stream 启动失败仅 warning,`is_stream_active`(真连接探针)存在但只喂 dingtalk_card 回调路由,readiness 零引用——Stream 死+HTTP 未配=主通道全聋仍 ready 200 |

### Major(效率/伸缩)

| # | 裁决 | 关键证据/纠偏 |
|---|---|---|
| F12 镜像全量重传 | ✅ | :229-233 dirty>0 即推、:280-297 checkpoint+整文件 put_object_from_file、:6742(+:6806 Gemini 支)每 drain 迭代(≤1000 chunk)一次,全中;唯一抑制=dirty 标志(全命中批次跳过);**加重:无 VACUUM,evict 只删行,sqlite 文件单调不缩**→上传体积只增不减;12d last-writer-wins 无 CAS 属实,但爆炸半径=重花 DashScope 费,不伤正确性(advisory cache) |
| F13 行封顶无字节封顶 | ✅(1 缓解漏报) | stage-1 LIMIT 100 无字节谓词(:107-124)、stage-2 整 JSON 入内存(orchestrator:258-260)、canonicals 全程滞留 ctx 不释放+**prefetch dict 也全程不修剪**(:268-276,并发>1 时叠加峰值),全中;缓解=单文件 200MB 门(RAG_EXTRACT_MAX_BYTES,:399-414,B4/P1-03)评审未提——但批次聚合(100×200MB)无界,主体成立 |
| F14 逐 chunk 事务 | ⚠️(表述错位) | **失活本体是单条 OR-链 bulk UPDATE 单次 commit**(:6430-6435/:6497,E#45 已优化过)——逐 chunk 的只是审计尾巴(:6512-6525 逐行 write_audit);audit_log.py:98-106 每次独立 commit 属实但连接是 **DBUtils 池 checkout 非新建 TCP**(db.py:32-35);单版本典型数十 chunk,1000 只在批次聚合(如 485 重推)出现;逐条审计+fail-open+批处理路径,1000 行≈1-3s,MAJOR 偏高;修复 trivial(executemany/复用连接)方向对 |
| F15 每请求 3 线程 | ⚠️(算术偏高) | :872 每请求新建 ThreadPoolExecutor(max_workers=3) 属实(with 块出即 join,无泄漏);**client fusion 默认开**坐实(config.py:251/:881 default True)——**retriever.py:795/:1016 的「默认关」docstring 已过时**;api.py:138 默认 120 AnyIO tokens+/api/ask 系 sync def 属实;"360 线程"是全 120 请求同时处于融合窗的瞬时上界,融合窗几十~几百 ms vs 请求生命周期秒级,稳态远低;真实成本=每请求 2-3 次线程建销(无 sparse 时 2),MAJOR 偏高但共享池方向对 |

### Release Engineering

| # | 裁决 | 关键证据/纠偏 |
|---|---|---|
| F16 构建不可复现 | ✅(有意回退非疏漏) | requirements.txt 10/10 条 `>=` 零钉版、无 lock 文件(requirements-prod.lock 仅存在于注释)、pyproject 全浮动、Dockerfile 只装 `.[api,production]` 不读 requirements.txt,全中;**关键补正:钉版曾在(e3c6fa0/分支孪生 3983e44),a64aa86(07-20,ae4cbc2 祖先)有意回退**——SAE buildImage 老 pip 解析 exit 1,批次8,重钉前提(staging buildImage 验证)写在文件头;评审未点破的额外问题:**SAE 真实部署走 buildpack 吃 requirements.txt,Docker 路径吃 pyproject extras,两路径装的集合实质不同**(dashscope/jieba/Pillow 差集),CI 供应链扫描部分对着 SAE 不用的路径 |
| F17 live gate 非机器强制 | ✅(字面属实) | ci.yml:7-8 明示 live 层不进阻断门(署因:凭证+金集+仓外 data repo);**结构性约束评审未提:prod HA3 IP 白名单只到用户笔记本,GitHub-hosted runner 物理够不着**(eval_release_gate.sh:4 自己指明出路=VPC 内 self-hosted runner,未建);纠偏:「DRAFT」≠stub——86 行完整实现,`make release-gate`(Makefile:115-116)本地可跑且是 CLAUDE.md 部署双门之一;baseline-freshness job 在但 continue-on-error(ci.yml:228,E2 翻硬门时删)——即强制=纪律非机器,PROCESS 定级公允 |

### Verification performed(数字复跑)

| 评审声明 | 本机复跑(2026-07-21) | 裁决 |
|---|---|---|
| make test 2,814 passed / 30 skipped | **2844 passed / 0 skipped**,17.5s,exit 0——本机 MySQL+OpenSearch 已 wired,30 个 skip 项实跑;2814+30=2844 逐位吻合 | ✅ |
| miniapp-test 24/24 | 24 pass / 0 fail | ✅ |
| console npm test 31 文件 326 测试 | Test Files 31 passed, Tests 326 passed | ✅ |
| console npm run build 通过 | ✓ built,exit 0(产物字节级无 tracked 变更) | ✅ |
| make lint 受 .agents 干扰/主树干净 | 全仓 ruff 69 错(44 E402+20 E401+4 F401+1 E741,全在未跟踪 .agents/.codex 脚本);主树六目录 All checks passed exit 0 | ✅ |

## 2. 核查副产品(评审未发现,本次坐实)

1. **F3b 加重**:`_delete_chunks_from_index` 选 PK **无 is_active 谓词**(spot_checker.py:173-176)——竞态窗内连刚恢复的 chunk 一并从 HA3 删掉。
2. **F4d 加重**:`RAG_ACL_FAIL_CLOSED=on` 也保不住墓碑——`_resolve_user_dept_cached` 无在册行返回 (None, db_ok=True)→保留令牌内嵌组,仅 DB 异常触发降级(api.py:541-548;dingtalk_identity.py:509/519/527)。is_active=0 在读路径**任何一环都不咬人**。
3. **F5 加重**:chunk 进 DEAD(重试预算耗尽)后,双活窗口**无界**——stranded 对账的 `NOT EXISTS ... != 'INDEXED'` 谓词永久排除该 doc(spot_checker.py:333-336)。
4. **F7 加重**:钉钉 KB 问答对全局日 LLM 熔断(2000/day)**不可见**——熔断计数只在 api 路径,真实 DashScope 开销被低估。
5. **F9 加重**:049:20-21 头注声称「重复执行由 apply_migration.py 的 information_schema 预检跳过(同 031/048 约定)」——**该预检两分支都不存在**;main 无 schema/032,checksum 漂移检测在 main 整体惰化。
6. **F12 加重**:无 VACUUM,镜像 sqlite 单调不缩。
7. **过时注释 ×2**:retriever.py:795/:1016「RAG_HA3_CLIENT_FUSION 默认关」(实际 config 默认 True);Dockerfile:66-69 类注释在 op0 版同样过时(往轮已记)。
8. **8c 永久 403**:crash-after-ACK 后该 message_id 的反馈永久 403(_feedback_owns_message 无行=拒,fail-open 只覆盖 SELECT 异常)。

## 3. 与既有台账的关系

- F1/F4 flag 默认 off = [main 现网 P0 加固 2026-07-10] 的既定「生效差部署+开 flag」态;07-16 unknown-unknowns 台账已把 REQUIRE_AUTH+ACL_FAIL_CLOSED 列为 SAE 重打包前置。**本评审的增量=催办力度,非新发现。**
- F2 = RB-02 族收尾态(TLS 已 LIVE 于现网 b9eeb873;代码默认软姿态待 B7 翻硬断)。
- F5/F6 = CLAUDE.md 已记录 open gaps;修复(租约 PR-4/durable PR-3)在 op0,user-gated。
- F16 回退 = 批次8 拍板;重钉版随下次 SAE 重打包(带 staging buildImage 验证)。
- **真正的新增修复候选**(未在任何既有台账排期):F3 对账 CAS 门控(chunk 失活按版本 CAS rowcount 门控+stranded healer 补 CAS,改动小、纯 main 侧)、F9(049 补 information_schema 守卫或修头注;main 补 032)、F11(readiness 接 is_stream_active+运营表探针)、F12(推送节流+VACUUM)、§2 的两处过时注释。

## 4. 032 apply 收口(2026-07-21,Sam 授权后执行)

Sam 授权 `032 apply` 后按 local → staging → prod 分层核查执行,**结论:现网无需任何写操作**。

| 层 | 执行前状态 | 动作 | 结果 |
|---|---|---|---|
| LOCAL(fuling_knowledge / fuling_operation) | 列在、台账**无** 032 行 | `--commit` ×2 | ✅ exit 0,两库台账记 `032 … checksum=ca44c238…` |
| STAGING(*_stg 两库) | 列在、台账**已记** 032,checksum=`ca44c238…` | 只读核查 | ✅ 与 main 文件逐位一致,**无动作** |
| PROD(fuling_knowledge / fuling_operation) | 列在、台账**已记** 032 | 只读核查(prod_access RO) | ✅ `applied_at=2026-07-10 18:17:31/32`,`by=scratch/apply_ontology_dbs_20260710.py`,checksum 与 main 文件一致,**无动作**;**未使用任何 PROD-RW 令牌** |

**本地端到端验证 032 的实际效用**(生产不可做的破坏性验证在本地做):
1. 重跑同一文件(checksum 一致)→ 幂等成功 `exit 0`(SQL 自带 information_schema+PREPARE 守卫,列已在时 no-op);
2. 人为把本地台账 checksum 改成 `deadbeef…` 模拟「同版本内容被改过」→ `apply_migration.py` **中止 `exit 4`**(EXIT_CHECKSUM_MISMATCH),漂移检测确认生效;
3. 已还原本地台账原值(`ca44c238…`)。

**副产品(纠正核查台账 §1 的 F9 表述)**:F9「main 缺 032」是**文件层**缺口(schema/ 目录 + CI/新建库路径 + main 侧记账纪律),不是现网能力缺口——staging/prod 的列与台账 07-10 即已就位。评审与我的一轮核查都只读了 main 的 schema/ 目录就外推到「漂移检测整体惰化」,**未核对现网 information_schema**,属同类过度外推。commit a7219a9 的价值因此收窄为:让 main 自建库/CI 与现网对齐 + 补上 main 侧 F-35 记账,**不改变现网既有防护**。
