# Majors 残余修复方案(2026-07-21,Claude-Codex 共识版)

> 依据:`docs/main_and_agent_v2_production_readiness_review_2026-07-21.md`(评审)+ `docs/audits/main_and_agent_v2_production_readiness_review_verification_2026-07-21.md`(逐条核查)。
> 范围:Major concerns 残余 M2–M15(M1.2/M12.1(main)/M16 已由 07-21 迁移批修复,不在本方案)。
> 评审流程:codex-review 六阶段,Codex 独立 onboarding(READY)→ 方案 v1→v4 四轮评审,**最终 APPROVE / CONSENSUS=FULL / 无 unresolved blocker·major**。
> 基线:main@69f1b39(origin+4 未 push)/ op0 worktree@1afb048(origin+5 未 push)——**九个迁移批提交实施全程原样保护**。

## 0. 总原则(双方共识约束)

1. **落树纪律**:op0⊇main——共享文件(dingtalk_bot/qa_logger/alerting/cost_breaker/config/frontend.yml/console 共享件)先落 main、同 patch 落 op0;agent 域(routes/agent、agent_runtime、agent_tools、stress_harness、canary)仅落 op0。
2. **默认 off/byte-identical**:所有新行为门 flag 默认 off/0/空;例外=纯观测且 fail-open 的接线(γ2 有独立 flag)。翻闸=Sam 部署批次决策。
3. **user-gated 不代做**:SAE env、DataWorks 控制台重贴/调度、webhook secret、ACR secret、branch protection、RDS 独立账号、OSS WORM、staging live 压测——仓内只做门+清单+attestation 位。
4. **schema 铁律**:先 schema/NNN 文件(每条 DDL=单动作、runner 预检可救或自幂等;ENGINE=InnoDB utf8mb4/unicode_ci)→ MANIFEST.tsv → apply 脚本+同会话 ledger;README 矩阵同步(顺带修过时"下一号=039"注)。
5. **env registry**:op0 每个新 RAG_* 必跑 `python -m opensearch_pipeline.rag_env_registry` 重生成,否则 test_rag_env_registry 红。
6. **状态诚实**:M7/M11/M12/M13 收尾后台账记 **PARTIAL/OPEN**(具名残余),其余记代码侧关闭;Sam-gated 外部证据不由仓库测试替代。

## 1. 批次 α — Agent 正确性(仅 op0):M3/M4/M5/M6

### α1(M3.1)commit-ACK 消歧
- `ontology/store.py::insert_identifier_closing_case`:`conn.commit()` 单独包——commit 段异常 → rollback(best-effort,不掩原异常)→ raise `CommitAmbiguous(原异常)`。
- `agent_tools/ontology_identity_resolve.py` catch-all 改三段:①`CommitAmbiguous` → `operation_ledger.check(tool_name, operation_id)`,**仅** `result["outcome"]=="applied"` → `ToolResult.ok` 幂等收口;`not_applied`/`unknown`/operation_id 未注入 → 原样 re-raise → executor 归 uncertain(053 对账器收敛);②其余 Exception(commit 前,已回滚)→ 维持 `ToolResult.fail`。**不做** uk_ns_norm_active 读回(行无 operation_id,无法归属;慢提交窗口"无行"≠未提交)。

### α2(M3.2)记账幂等(schema/055)
- 055:`agent_step ADD bookkeeping_id CHAR(32) NULL` + `CREATE UNIQUE INDEX uk_step_bookkeeping (bookkeeping_id)`。
- executor 在**任何 DB 写前**为 model-turn/tool-call 各生成 bookkeeping_id,传入 `record_turn`/`record_tool_call`(签名加可选参,写入 step INSERT)。
- 异常处置:同键新连接**整事务重放×1**;撞 `uk_step_bookkeeping` 1062(限定 `_is_dup` 且错误文本含索引名)=原事务已落 → 成功;重放仍败 → **放弃全部 durable 回退写**(删现行 `_budget_used`/`_record_model_step`/`_flush_llm_rows` 分段回退;幂等 `_heartbeat` 保留)——本地计数仍权威,durable 欠账由 `suspend_run_atomic` GREATEST 同事务纠偏。非目标 1062 原样失败。
- 1054 且错误文本含 bookkeeping_id(055 未 apply)→ warn-once 走 legacy 分段路径;其余 1054 原样抛。

### α3(M4)ask 业务域幂等(schema/054,flag `RAG_AGENT_ASK_IDEM_ENABLE` 默认 off)
- 054:`agent_run ADD client_request_id VARCHAR(64) NULL`、`ADD question_digest CHAR(64) NULL`、`ADD active_latency_ms BIGINT NULL`、`CREATE UNIQUE INDEX uk_run_client_req (user_id, client_request_id)`(四条单动作;cancel_requested_at **不加**——047 已有且 request_cancel 已落戳)。
- op0 `routes/agent.py` 定义 `AgentAskRequest(AskRequest)` 加 `client_request_id`(**不动共享 api.py**)。`question_digest` = 原始 UTF-8 question 的 SHA-256(不 trim/不规范化)。
- 回放查询位于 `_enforce_rate_limit` **之前**(回放命中不计 admission、不建 dispatch 命令):命中且 digest 同 → 非终态 `202 application/json {run_id,status,replayed:true}` / 终态 `200 {run_id,status,replayed:true}`(前端拉 run detail 水合);digest 异 → 409。
- 竞态:`create_run` 撞 `uk_run_client_req` 1062 **或** `ThreadBusy`(uk_thread_active 先报)→ 均先按 (user_id, client_request_id) 回读:命中同 digest → 回放;否则 ThreadBusy 按现行 409。输家已 enqueue+claim 的 dispatch 命令用既有 `done` 状态收口(`complete(status='done')`,last_error 记 `deduplicated-to-run:<run_id>`,不新增状态枚举)。
- 前端 `useAgentAsk`:每次提交生成 uuid,网络级重试复用;按 content-type 分流(json→回放分支;event-stream→现行)。
- flag on 前置=054 已 apply(readiness schema 形态检查,durable_dispatch 同款)。

### α4(M5)stewardship 轮换收敛
- **registered resolver(resolve_scope_live 非 None)→ live 唯一权威**,列表可见/预可见门/裁决三处同口径(旧 steward 轮换后不再可见参数);unregistered → snapshot fallback。授权路径**零缓存**。
- `list_pending` 重构:去 scope SQL 预过滤 → status='pending' 按 (created_at, request_id) keyset 候选扫描(LIMIT n+1 循环填页)→ 逐行 live resolve 判可见;kb_admin 跳过 resolve;?mine 不变。`next_cursor`=最后一个**已扫描候选**,版本化+HMAC 签名编码。
- scope 漂移落库(裁决路径+reaper 腿+agent_health 三处):`UPDATE ... WHERE status='pending' AND approver_scope=<old>` CAS,rowcount=1 才写 old→new audit 行(同事务);裁决 audit payload 增记 decision-time live scope(弥补 025 无 scope 列)。

### α5(M6)审批积压治理(schema/058)
- 058:`CREATE TABLE IF NOT EXISTS agent_quota_lock (lock_name VARCHAR(64) PRIMARY KEY) ENGINE=InnoDB ...` + `INSERT IGNORE` 哨兵行 'approval_admission' + `CREATE INDEX idx_approval_quota ON approval_request (status, requested_by, tool_name)`。
- 三层配额(env 默认全 0=off):`RAG_AGENT_APPROVAL_PENDING_CAP`(per-requester)/`RAG_AGENT_APPROVAL_PER_TOOL_CAP`/`RAG_AGENT_APPROVAL_GLOBAL_CAP`。caps 全 0 → **完全不触 058**(byte-identical)。任一 cap>0:`insert_request`(=suspend 同一事务)内先哨兵 `FOR UPDATE`(单锁无死锁面)→ global→user→user-tool 三 COUNT(走新索引)→ 超限 raise `ApprovalQuotaExceeded` → 整事务回滚、零副作用。**058/哨兵不可用(1146/无行)→ raise `ApprovalQuotaUnavailable` fail-closed,绝不静默放行**;readiness:任一 cap>0 → 检查 058+哨兵,缺 → 红。
- 分页:α4 的 keyset 游标即 M6 分页交付。老化指标:γ3 agent_health(`RAG_AGENT_APPROVAL_SLA_HOURS` 默认 24)。
- abuse 测试:真库双连接并发穿透(conftest 串行组)+ cap>0 缺 058 fail-closed+readiness 红。

## 2. 批次 β — 钉钉 serving 防护(两树):M2

- **有界并发**:`dingtalk_bot.py` 线程孵化点前置模块级 `BoundedSemaphore(RAG_DT_MAX_WORKERS)`(默认 0=不启用=现状):`acquire(blocking=False)` 失败 → 忙话术走 **`_send_terminal_text`**(msg_id 进发送状态机;`_send_text_reply` 白名单锁定不可用)+`_process_claimed_body` 返回显式形态(如 `{"msgtype":"rejected_busy"}`),`_run_claimed` **仅对"后台已启动"形态标 processing**(不覆盖发送层 sent);成功 → 线程 finally release,且 `thread.start()` 抛错路径同样 release。
- **主链 admission**:`RAG_DT_ADMISSION_ENABLE`(默认 off):on 时 claim 后、spawn 前调 `LIMITER.admit_ask(actor=f"u:{staffId}"|"ip:anon", is_user, count_llm 镜像 /api/ask)`,denial → 对应话术终答+状态收口;限流器自身异常按 ask 档 fail-closed 精神回忙话术。
- 测试:flag off 逐字节等价;饱和/denial/start 抛错三路;`test_msg_ack_taxonomy` 白名单同步。

## 3. 批次 γ — 可观测/姿态(两树+op0):M9/M15/M8/M10/M7-lite

### γ1(M9.1)agent latency
- `qa_logger.insert_qa_row_tx` 加 `latency_ms:int=0` 关键字参数(两树;既有调用不变)。
- `active_latency_ms` 累计:执行腿结束时把本腿 monotonic 毫秒累加——挂起走 `suspend_run_atomic` 新可选参、**全部终态 CAS**(成功 `_complete_run_txn` + 失败/取消/超预算 `transition/_transition_checked`)统一加可选 active_ms delta,`SET active_latency_ms=COALESCE(active_latency_ms,0)+%s` 同事务;成功路径顺序:锁行 → 算 stored+delta → 传 QA writer(extra_writer 在状态 UPDATE 前)→ UPDATE。三组 QA 写点(含 AGENT_ERROR/取消)传累计值;1054(054 未 apply)→ 跳过累计 warn-once,QA 行回退本腿 monotonic(仍>0)。TTR(started_at→ended_at)只进 056,不进通用 p95。

### γ2(M15 告警)成本熔断接 ops 告警
- `extraction/cost_breaker.py` RUN/DAILY 触发点接 `send_ops_alert`(lazy import),`RAG_COST_ALERT_ENABLE` 默认 off;dedup_key 分 `cost-breaker-run`(warning)/`cost-breaker-daily`(critical)。

### γ3(M9.3/9.4+M6.iii)agent_health 权威面(op0,schema/056)
- 056:`agent_daily_metrics` CREATE TABLE IF NOT EXISTS,按日 UPSERT(北京日界,与 qa_rollup 同约定):八维 + **agent 可用率事实**(agent_run 终态 total/succeeded/failed/cancelled/expired、success_rate/error_rate、SLO 裁决列)+ TTR 分位。
- 新模块 `agent_health.py`,ops_monitor._JOBS 第 8 项(op0-only;`RAG_AGENT_HEALTH_ENABLE` 默认 off → skipped/exit 0)。八维定案:dispatch_backlog(backlog_count 首个消费者)/approval_pending(count+oldest)/stale_running(复用 `RAG_AGENT_STALE_RUNNING_S`)/uncertain(复用 `RAG_AGENT_INV_STALE_S`)/relay_health(**权威=近窗终态 run 的 Redis 终局帧缺失率+Redis 可达性**;publish_failures INCR 计数器=best-effort 下界如实标注;**Redis 探测失败 → 该维 error、作业 exit 3,绝不报 0**;relay off=N/A)/worker_heartbeat(durable_dispatch on:dispatcher tick 循环逐轮写 rag_runtime_contract 心跳带 holder,读龄;off=N/A)/cancellation_latency(047 现列 ended_at−cancel_requested_at)/recovery_convergence(outbox created_at→run ended_at,代理口径标注)。
- 阈值 env(master off 下含理性默认):`RAG_AGENT_BACKLOG_MAX`(50)/`RAG_AGENT_RELAY_MISS_RATE_MAX`(0.01)/`RAG_AGENT_WORKER_HB_MAX_S`(300)/`RAG_AGENT_CANCEL_LAT_MAX_S`(120)/`RAG_AGENT_RECOVERY_MAX_S`(900)/`RAG_AGENT_AVAIL_MIN`(0.95)/`RAG_AGENT_APPROVAL_SLA_HOURS`(24)。每维采集 SQL/N-A/exit 0-2-3 映射写死 docstring+056 列注释。越阈 `send_ops_alert(dedup_key="agent-health")`。

### γ4(M15 价表)schema/057
- 057:`llm_call_log ADD price_table_version VARCHAR(32) NULL`。`RAG_MODEL_PRICE_TABLE_JSON`(默认空 → cost_estimate 维持 NULL,拍板不破)契约:`{version, effective_from, models:{name:{in_rmb_per_mtok, out_rmb_per_mtok}}}`;Decimal 计算、非负有限校验、未知 model→NULL、非空非法 → load_config raise;同步+折叠两条写路径同落 cost_estimate+price_table_version;DECIMAL(10,4) 舍入行为文档化。历史回填=另行 scratch 脚本(user-gated)。

### γ5(M8)TLS 姿态
- staging/test 分支补同文 P0-02 warning(无条件,warning-only)。
- `RAG_RDS_REQUIRE_TLS`(默认 off):on 且 env∈{production,staging} → 姿态断言要求 `ssl_ca` 非空+文件存在+`ssl_verify_cert=True`,api 启动探针对"无法证明客户端 TLS"fail-closed;**无当日 ack 逃生**(回滚=保持 flag off)。既有"无 CA 不硬断"契约测试保持(flag off 仍真)+flag-on 新测试。

### γ6(M7-lite)审计篡改检测 —— **M7 状态=OPEN(检测层缓解)**
- **不做**行内哈希链(需读前行哈希=head 行锁串行化全部治理事务,且与 HIGH_WRITE 写前审计 fail-closed 事务耦合);**撤** RAG_AUDIT_DB_DSN 构想(同事务审计用调用方游标,换 DSN=失去原子性,凭证分离该 env 无法兑现)。
- 独立 job `audit_digest`(`RAG_AUDIT_DIGEST_ENABLE` 默认 off,进 _JOBS+launchd runner+DW 节点 --only 仓侧文件;控制台重贴=user-gated):从**最后成功 manifest 水位追赶全部缺失完整日**;每日 manifest=count+有序行哈希 Merkle 根,canonical JSON(sorted keys)、行序 (created_at,audit_id)、created_at 由 SQL DATE_FORMAT 固定毫秒格式+会话 SET time_zone 固定、含前日 manifest digest 成链、key_id+轮换规则(换 key=新 key_id,旧链用旧 key 验);HMAC(`RAG_AUDIT_HMAC_KEY`)签名写 OSS 独立前缀;午夜结算宽限窗+签名 correction manifest 规格入文档。retention 归档同款 HMAC manifest。`scripts/verify_audit_digest.py` 只读核验。当日内篡改窗如实记残余。独立凭证/只写账号/WORM/KMS=user-gated 清单。

### γ7(M10)webhook 姿态(两树)
- `RAG_OPS_ALERT_REQUIRE`(默认 off):on 且 prod/staging → 姿态断言要求 `RAG_OPS_ALERT_WEBHOOK` 非空**且过 `_webhook_allowed` 域校验**;文档明示配置存在≠送达证明(送达=Sam live attestation)。

## 4. 批次 δ — CI/供应链/E2E:M11/M12/M13/M14

### δ1(M11)压测触发 —— **M11 状态=PARTIAL**
- op0 stress.yml 加 `push: branches:[claude/ontology-p0]` **无 paths 限定**(smoke 档;时长超预算再收窄并注释记录取舍);schedule 死因(不在默认分支)注释注明。G1-G9 ratification/硬门翻转=ε 具名 Sam-gated 验收项(RB-05 E2 归属),**本批不实施 live**。

### δ2(M12.3)镜像工件 —— **M12 状态=PARTIAL**
- 新独立 `image.yml` 两 job:**job1**(push/dispatch,零外部写)build → 同镜像 smoke(import+uvicorn 起服 curl /api/health,RAG_SIMULATE=true)→ `docker save`(Docker image archive)+attestation.json(git sha/Dockerfile sha/lock sha/image id/digest=null)上传 artifact;**job2 promotion**(仅 workflow_dispatch+input push_acr=true+GitHub environment 保护)下载 job1 tar → load → push ACR(逐字节同工件)→ 产**第二份** immutable attestation artifact(v2 含 manifest digest,v1 不改)。顺带闭 B3 迁移批留验尾巴(容器非 root+amd64)。
- canary `--require-digest`(默认 off):on 时 `--image` 必须含 `@sha256:`。
- README:镜像=唯一正式 SAE 工件**路线**;ZIP=有截止条件的 break-glass(截止=SAE 镜像化迁移,user-gated);δ2b 前置调查:zip 构建脚本依赖装配方式实况核查,若自装依赖改走 requirements-prod.lock --require-hashes。

### δ3(M13)DataWorks 供应链 —— **M13 状态=PARTIAL**
- **HMAC 分离信任边界**:build_dataworks_zip.sh 加 --hmac(本地 env `RAG_DW_ZIP_HMAC_KEY` 签 zip 摘要产 .sha256.hmac);节点:DataWorks **调度 env** 配同 key(env≠资源=边界分离)→ 有 key 则 HMAC 硬验;`RAG_DW_SIDECAR_STRICT`(默认 off=现状放行+显著警告)on 时缺 sidecar/不可读/无 key 均 raise。威胁模型边界(能改节点代码或读调度 env 者不在防护面)文档如实。test_dataworks_supply_chain 保留 off 契约+新增 on 契约。
- py3.7:抽 `requirements-dataworks-py37.txt`(节点 DEPS 钉版镜像)+parity 测试;ci security job pip-audit 追加该文件(CVE 处置=逐条评估+--ignore-vuln 注释,先例 ci-green-gotchas)。传递闭包冻结=user-gated(节点加 pip freeze 打印,Sam 跑一次 DW 后回填)。cosign/gpg 信任根=user-gated 清单。

### δ4(M14)Playwright 进 CI(两树)
- δ4a:frontend.yml 加 e2e job:npm ci → `npx playwright install --with-deps chromium` → `npm run e2e -- --trace=retain-on-failure`(CLI 传 trace,不动 config retries:0 本地纪律)跑**三视口 projects**(硬门口径)→ 失败双上传 playwright-report/+test-results/。
- δ4b:管理页组真实修复=?token 离线登录+ux-gate.helpers 同款路由 mock+空态/错误态/表单/删除/提交状态构造+testid 接线(console 源码 data-testid)→ 去 `.skip`。允许与 δ4a 分 commit。

## 5. 批次 ε — 文档/清单(双树)

1. `docs/ops/alerting_chain_attestation_2026-07-21_DRAFT.md`:07-21 告警链闭环运维记录归档(内容以 Sam 现网记录为准,有证据后去 _DRAFT)。
2. user-gated 总清单(单页):SAE env 四联(REQUIRE_AUTH/ACL_FAIL_CLOSED/RAG_RDS_REQUIRE_TLS/RAG_OPS_ALERT_REQUIRE+webhook secret)/schema 054-058 三环境 apply(055 幂等记账、054+flag、058+caps、056+health、057+价表的 apply-before-enable 前置)/DataWorks strict+HMAC key 配置与节点重贴(与"清理stage3 重贴"同族)/ACR secret+environment 保护/G1-G9 staging ratification(owner=Sam,RB-05 E2)/M7 独立凭证+WORM+KMS/py37 传递闭包捕获/branch protection/qa-rollup launchd exit=2 核查。
3. schema/README:054-058 矩阵行+修过时"下一号=039"注。
4. 状态台账:M2/M3/M4/M5/M6/M8/M9/M10 姿态/M14/M15=代码侧关闭;**M7/M11/M12/M13=PARTIAL/OPEN 具名残余**。

## 6. 新增 env 总表(全默认 off/0/空;op0 重生成 registry)

行为门:RAG_DT_MAX_WORKERS / RAG_DT_ADMISSION_ENABLE / RAG_RDS_REQUIRE_TLS / RAG_OPS_ALERT_REQUIRE / RAG_DW_SIDECAR_STRICT / RAG_COST_ALERT_ENABLE / RAG_AGENT_ASK_IDEM_ENABLE / RAG_AGENT_HEALTH_ENABLE / RAG_AUDIT_DIGEST_ENABLE / RAG_AGENT_APPROVAL_PENDING_CAP / RAG_AGENT_APPROVAL_PER_TOOL_CAP / RAG_AGENT_APPROVAL_GLOBAL_CAP。
密钥/数据:RAG_DW_ZIP_HMAC_KEY / RAG_AUDIT_HMAC_KEY / RAG_MODEL_PRICE_TABLE_JSON。
监控阈值(master off 下带默认):RAG_AGENT_APPROVAL_SLA_HOURS=24 / RAG_AGENT_BACKLOG_MAX=50 / RAG_AGENT_RELAY_MISS_RATE_MAX=0.01 / RAG_AGENT_WORKER_HB_MAX_S=300 / RAG_AGENT_CANCEL_LAT_MAX_S=120 / RAG_AGENT_RECOVERY_MAX_S=900 / RAG_AGENT_AVAIL_MIN=0.95。

## 7. schema 新文件(op0)

| 文件 | 内容 | 备注 |
|---|---|---|
| 054_agent_run_request_identity.sql | ADD client_request_id / ADD question_digest / ADD active_latency_ms / CREATE UNIQUE INDEX uk_run_client_req(user_id,client_request_id) | cancel_requested_at 不加(047 已有) |
| 055_agent_step_bookkeeping.sql | ADD bookkeeping_id / CREATE UNIQUE INDEX uk_step_bookkeeping | 记账幂等 |
| 056_agent_daily_metrics.sql | CREATE TABLE IF NOT EXISTS(八维+可用率+TTR 分位,北京日界) | health flag-on 前置 |
| 057_llm_call_log_price_version.sql | ADD price_table_version | 价表非空前置 |
| 058_agent_quota_lock.sql | CREATE TABLE IF NOT EXISTS agent_quota_lock + INSERT IGNORE 哨兵 + CREATE INDEX idx_approval_quota | caps>0 前置,缺=fail-closed |

readiness posture 三联:health on→056 在;价表非空→057 在;任一 cap>0→058+哨兵在。

## 8. 测试与验证协议

- 每批:两树 `make test`+`make lint` 全绿,退出码显式回显;β 补 dingtalk 单测矩阵+`make sim` 冒烟。
- 关键新测试:M3 真库 commit-ACK 注入矩阵(目标/非目标 1062、llm_rows=[]、folded rows、055 缺失兼容)/M4 并发双 POST+ThreadBusy 双路先报+重试不重计 admission/M6 双连接穿透+058 缺失 fail-closed/M5 轮换后列表首屏可见/056 UPSERT 日界+exit 0-2-3/Redis 不可用与缺帧/TLS·webhook·sidecar posture 各 flag-on;真库测试全部进 conftest 串行组。
- CI job(image/e2e/stress push)以 workflow_dispatch/push 实跑验证。
- 回滚:行为=不开 flag;代码=逐 commit revert(每子项独立 commit,点名文件,禁 git add -A)。

## 9. 遗留决策点(Sam)

- 批次实施顺序拍板(建议 α→β→γ→δ→ε,亦可 β 先行配合 SAE 重打包窗口)。
- 9 个既有迁移批提交与本方案各批的 push 时点。
- ε 清单各 user-gated 项的执行窗口。
