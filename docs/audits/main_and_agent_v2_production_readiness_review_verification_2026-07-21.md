# Main + Agent v2 Production-Readiness Review(2026-07-21)逐条核查台账

> 核查日期:2026-07-21
> 评审原文:`docs/main_and_agent_v2_production_readiness_review_2026-07-21.md`
> 核查基线:与评审完全同基线——main@bcc7acb(=origin/main,tracked 树干净)/ agent-v2 worktree `/Users/laijunchen/Projects/agent-v2-worktree` @9d7a631(=origin/claude/ontology-p0,干净)
> 核查方式:12 路只读子代理分域逐条核查 + 亲手重跑全部可本地复现的验证矩阵项(两树 pytest/ruff/vitest/console build/离线通用能力门/freshness 脚本,退出码逐一回显)+ 承重引用抽查。所有 file:line 以上述两 SHA 为准。

## 0. 总裁决

**95 条子断言逐条核查(B1-B9 共 43、M1-M16 共 39、Minor 1-6 共 6、Strong foundations 抽查 7)+ Scope 3 项 + 验证矩阵 8 项亲手重跑:整条推翻 0;属实 89;子事实/措辞级纠偏 6(无一动摇所在条目主体)。评审的 REVISE/NO-GO 总裁决、"main 是更安全的 RAG 基线"、"先并 main 再谈发布"的处置排序,全部立得住。**

这是历次外审中读码质量最高的一份:所有可复算数字逐位命中(444 文件/74315+/1170−、23 冲突文件逐个吻合、金集 SHA `b3403db07db7b71a`/`3bed9881eefaf0ee` 逐位、258/251/84/84、4318+1、326/420),自设 hedge("may be stale external state")也诚实。六处纠偏:

1. **B2 场景前提**:僵尸 run "commits a HIGH_WRITE" 并非任意路径——未授 grant 的 HIGH_WRITE 被 suspend CAS 意外拦下;成立面=resume 已注 grant/已在飞,而 **allow 档 LOW/MEDIUM 写完全无栅**(比评审说的面窄一点也宽一点)。reaper 腿还需心跳巧合,drain 腿不需要。
2. **B6.1 归属不完整**:freshness 的 continue-on-error 掩蔽是 **job 级且 main ci.yml 逐字节同构同样掩蔽**(两阶段 E2 计划的记录在案状态),非 op0 独有。
3. **M9.4 绝对化**:八维里 stale runs 实有进程内 reaper 探针+书面 20 分钟 SLO+收尸告警(但探针与进程同生死);其余 6 维确实零覆盖。
4. **M11.2 触发器**:stress.yml 实有三触发(dispatch/PR 路径限定/cron)非"仅 PR"——但无 push 触发、schedule 因 workflow 不在默认分支永不发车、无 PR:净效果(无自动当前-head 压测)成立。
5. **M14.3 以偏概全**:ux-gate 三组仅管理页组 `.skip`(testid 未接线),另两组 32 测试可本地跑——"套件自身被跳"不准;"CI 不跑 Playwright"准。
6. **M15.3 绝对化**:"无价表/货币预算"在 Agent/serving 域成立,但摄取侧 `cost_breaker.py` 是带测试的 RMB 单价表+三级预算熔断;树级真命题只剩"无 webhook 级成本告警"。

**须与裁决同读的三类定性语境**(评审事实无错,但补救框架应 credit 记录在案的拍板——07-18 台账同款系统性偏差的轻量重现):M8 TLS 告警不阻断=拍板过的过渡态(注释自认,硬断随 B7 SAE env 落地;且现网 07-21 已实配 TLS——库外事实);M15.1 成本 NULL=schema/023"不编造单价"拍板;B6.1 continue-on-error=RB-05 E2 两阶段计划注释明示;M10.3 webhook 必填=节点文件自警在案。另 M10.1 的"LaunchAgent unloaded"是 06-17 旧文档句,与仓内 07-18 后的 attestation 及本机 launchctl 实测(三任务全加载)相反——评审 hedge 应验,其"告警链无当前 attestation"命题在**仓内**仍成立(07-21 闭环只在库外记忆/现网)。

**对处置排序的核查结论**:评审开出的 8 步 sequence 与"先整合 main(B1 四修复+不相交的墓碑机制)、HIGH_WRITE 维持关死、canary 只读起步"与本核查全部证据一致,无需修正。unknown-unknown 12 探针清单中,#1/2/3/4/6 对应的缺口全部实证存在(B2/B3/B4/M3/M4/B9),#5/7/10/11/12 属 staging/现网执行项本核查不可代证。

## 1. Scope 与验证矩阵复核(亲手重跑,非转述)

### 1.1 Scope 声明

| 评审声明 | 复核结果 |
|---|---|
| main=`bcc7acb5…0823`,matches origin/main,tracked tree unchanged | ✅ 逐位吻合;`git status --porcelain` tracked 变更=0 |
| Agent v2=`9d7a6318…fd56`,branch claude/ontology-p0,matches origin,clean | ✅ 逐位吻合;worktree=`~/Projects/agent-v2-worktree`,tracked 变更=0 |
| merge base=`9b09aaa6…9ecd` | ✅ `git merge-base` 逐位吻合 |
| 265 ahead / 30 behind | ✅ `git rev-list --left-right --count main...op0` = 30/265 |
| 444 files,~74,315 additions / 1,170 deletions | ✅ `git diff --shortstat 9b09aaa6...op0` = 444 files, 74315(+), 1170(−)——逐位精确 |
| 三方合并投影 23 个冲突文件,含 api.py/config.py/qa_logger.py/clients.py、schema/CI 控制、11 个 console 文件 | ✅ `git merge-tree --write-tree main claude/ontology-p0` exit=1,冲突文件恰 23 个:console 11(AccessGrantList/AccessRequestQueue/ApprovalHistory/ApprovalQueue/DocTable/FeedbackReviewList/Composer/AppShell/useAsk/useKb/ManageView)+ 热路径 4(api/clients/config/qa_logger)+ schema/README.md + ci_load_schema.sh + conftest.py + 5 个 test 文件(其中 3 个 add/add) |

### 1.2 验证矩阵(全部亲手重跑,退出码显式回显)

| Gate | 评审值(main / Agent v2) | 本机复跑 | 裁决 |
|---|---|---|---|
| Python tests | 2,868 passed 35 skipped / 4,318 passed 1 skipped | main:**2902 passed 1 skipped**(exit=0);op0:**4318 passed 1 skipped**(exit=0) | ✅(op0 逐位吻合;main 总数恒等 2903=2868+35=2902+1,差异纯环境性——本机本地栈在跑,34 个 service-gated 测试实跑通过而评审环境 skip;无一失败) |
| Ruff | main 全量 lint 仅败于未跟踪 .agents 脚本 / op0 通过 | main `make lint` exit≠0,错误行 100% 落在 `.agents/skills/...`(tracked 文件错误=0);op0 exit=0 All checks passed | ✅ 逐字吻合 |
| Console unit | 326 / 420 | main 326 passed(31 files,exit=0);op0 420 passed(38 files,exit=0) | ✅ 逐位吻合 |
| Console build | 双通过 | main exit=0(built 528ms);op0 exit=0(built 556ms) | ✅ |
| 离线通用能力门 | main 258 题 0 劫持+84/84 / op0 251 题 0 劫持+84/84 | main:金集 258 题 0 劫持,路由 84/84(15+15+10+14+6+4+10+10),exit=0;op0:251 题 0 劫持,84/84,exit=0 | ✅ 逐位吻合 |
| Baseline freshness | main 过 / op0 败于金集 SHA+LLM 版本+agent regime 缺失 | main exit=0 全 PASS;op0 exit=1,恰三项 FAIL:`eval_set_sha` 现 `b3403db07db7b71a` vs 冻结 `3bed9881eefaf0ee`、`llm_model` 冻结 qwen3.6-plus vs 当前默认 qwen3.7-plus、`agent.regime_present` 旧格式无指纹 | ✅ 三项 FAIL 逐字吻合(评审所引 SHA 逐位正确) |
| 当前分支 CI | op0 整体绿,freshness 被 continue-on-error 掩蔽 | `gh run list --branch claude/ontology-p0`:head 提交 CI+Frontend 双 completed/success;op0 ci.yml:248-264 `baseline-freshness` job 带 `continue-on-error: true`(248 行注释"先亮红叉不阻塞;refreeze 后删掉…翻硬门(RB-05 E2)") | ✅(纠偏:掩蔽为 **job 级**而非 step 级;且 **main ci.yml:225-241 逐字节同构同样掩蔽**——见 B6.1) |
| Live RAG gate / Live Agent eval / staging 压测 | 未跑 | 与声明一致,无需复核(声明自身即"未跑") | — |

## 2. 逐条裁决表

裁决图例:✅=属实 ⚠️=部分属实/纠偏(主体成立,细节需修正) ❌=推翻 ☁️=本地不可验证(现网/云端运行态)

### Release Blockers

**B1(集成候选不存在/op0 缺四项 main 修复)——4/4 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B1.1 reconcile 无锁/无重验/无 CAS | ✅ | op0 `spot_checker.py:219-284`:批扫后直接 `_delete_chunks_from_index`(253,无界客户端),RDS 更新在不可逆删除**之后**,rowcount 不检查;`FOR UPDATE\|_lock_doc\|rowcount` 零命中(exit=1)。main(9e87131):`_lock_doc`:23-33 `FOR UPDATE`、锁内重验 337-347、有界客户端+deadline 313-322/349-351、锁序 chunk 先于 dv 356-368、CAS≠1 → rollback 369-390。四提交均非 op0 祖先(`merge-base --is-ancestor` rc=1) |
| B1.2 is_active=1 过滤=墓碑可复活 | ✅ | op0 `dingtalk_identity.py:344,509` `AND is_active = 1` → 无行 → 钉钉 API 兜底 → 满组回填缓存/token(788-793 复用);main(79b924c)342-360 读 is_active,`not row[3]` → `return [], True` 权威空组跳 API。**纠偏性 nuance**:op0 有评审未提的 `user_row_revoked()`(533-557,P1-08)但只接在 `RAG_ACL_FAIL_CLOSED` 严格模式(默认关)api.py:513-516 与 agent 审批重验 routes/agent.py:1354-1355;main 反而无 `user_row_revoked`(exit=1)——两树墓碑机制**不相交**,加重 B1 合并论点 |
| B1.3 op0 readiness 缺运营库/Stream/QA 漂移三探针 | ✅ | main(c7b8723)api.py:719-747 `_probe_operation_schema`(qa_session_log+user_feedback 列契约)、798-810 Stream 探针、qa_logger `_alert_schema_drift_once`:34-56 on 1054/1146 critical page;op0 readiness 15 项 agent 检查但 `qa_session_log\|dingtalk_stream\|QA_SESSION_MANDATORY` 探针零命中,op0 qa_logger.py:587-597 仅 `logger.critical` 不 page |
| B1.4 op0 apply 脚本无 information_schema 预检 | ✅ | main(a7219a9)`apply_migration.py:222-254` `_statement_skip_reason`(columns/statistics 预检,ADD COLUMN/CREATE INDEX 跳过),接 dry-run 342-346+commit 380-382;op0 同名脚本 361 行无该函数,commit 环直接 `cur.execute(s)`,`1060\|1061\|duplicate` 零命中 |

**B2(终态 run 可继续产出生产副作用)——6/6 ✅(场景前提两处纠偏,承重引用已亲手复核)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B2.1 drain 超时标 failed 不 cancel 线程 | ✅(亲验) | executor.py:1143-1174(逐字复核):等待→`_transition_checked(run_id,"running","failed")`→`_pool.shutdown(wait=False)`;全函数无 `request_cancel`(cancel-flag 仅 A5 路由与 durable-poll 两处设);docstring 自认唯一兜底=完成侧 CAS |
| B2.2 reaper 纯心跳龄 running→failed | ✅(亲验) | run_store.py:732-737(逐字复核)`WHERE status='running' AND heartbeat_at < DATE_SUB(...)`——无 holder/epoch(agent_runtime 全目录 `epoch` grep exit=1);另两腿(resuming→suspended/suspended→expired)非终态,唯 running 腿终态,与评审一致 |
| B2.3 driver 只查内存 cancel flag | ✅ | executor.py:519-563:预算→`handle.cancelled()`(threading.Event)→`_adjudicate`;policy/tool_executor 全程不读 run row;durable cancel 标记在终态后**不可设**(run_store.py:665-668 `AND status IN ('running','resuming')`)——僵尸期连补救通道都关了 |
| B2.4 record_tool_call 无谓词 | ✅ | run_store.py:360-372 UPDATE 仅 PK 谓词;且即便加谓词也拦不住——executor 551-558 对 bookkeeping 异常 fail-open 回退 `_budget_used`(有意非阻断设计,当不了 fence) |
| B2.5 完成 CAS 只围答案 | ✅ | `_complete_run_txn`(518-573)`FOR UPDATE`+`AND status='running'` 单事务只围 final answer+状态;CAS 败→"结果作废不落库"仅弃答案;工具变更早已在各自事务提交,无补偿路径;invocation 级 CAS(950-952)只围结果**记录** |
| B2.6 测试只盖完成侧 fencing | ✅ | test_agent_reaudit_fixes.py:161-177 drain 测试单工具结构上不可能"后续写";operation_ledger 271-284 是同 operation_id 重提交 fencing(新工具调用=新 id,畅通);无任何测试构造"标 failed→同线程再提工具→断言被拦" |

**场景前提纠偏(不改主裁决)**:①评审第 4 步"commits a HIGH_WRITE"**并非任意路径**——未授 grant 的 HIGH_WRITE 会在 suspend CAS 上意外被拦(suspend `WHERE status='running'` 失败→terminal fail);成立面=(a)resume 已注入 grant 后才被标终态、(b)HIGH_WRITE 已在飞(tool_executor.py:675 自认"已在跑的线程杀不掉");而 allow 档 LOW/MEDIUM 写**完全无 fence**。②reaper 腿吃到活线程需前提(心跳 ticker 独立线程 30s 跳,须 deadline 主动停跳或心跳持续吞错 ~15min);**drain 腿无需任何巧合**。③缺口为分支**明知的记账范围**:routes/agent.py:225-229 逐字"重放语义与工具副作用 fence 未建,宣称 durable execution 前不越界"——评审要求的 lease/epoch 机制全 agent_runtime 无一处存在(grep exit=1,仓内唯一 lease=摄取侧 schema/048 另一子系统)。

**B3(durable redrive 丢审批时 scope)——3/3 ✅(承重引用已亲手复核)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B3.1 inline 路径带 approver_scope | ✅ | routes/agent.py:1118 `_authorize_approver`(1019 读存量 scope,1023-1027 可 live 重算)→1281-1285 `approval_meta={request_id, decided_by, **approver_scope**}`→executor.py:331-336 `ApprovalGrant`→policy.py:234-237 stamp 到 ctx。inline 时 scope 恒为字符串(可 "")永不 None |
| B3.2 durable resume/redrive 只带 request_id+decided_by,丢 scope | ✅(亲验) | outbox payload routes/agent.py:1156-1158 恰 4 键无 scope(逐字复核);重驱单引擎 `_redrive_resume_run`(1647-1711,B6 对账与 PR-3 恢复共用)resume 调用 1692-1696 `approval_meta={request_id, decided_by}` 无 scope(逐字复核);**scope 明明持久在 approval_request 行(schema/025:25)且函数收了 approval_store 参数,但函数体从不读**;approval_decision 表无 scope 列(025:37-49)无从回填 |
| B3.3 None 静默禁用 TOCTOU 检查 | ✅(亲验) | ontology_identity_resolve.py:240-248(逐字复核):`if granted_scope is not None:` 无 else——None 直落穿到变更;"" 会比对、比对内重算失败 fail-closed,**唯 None 分支 fail-open**,恰是 redrive 产物;redrive 且完全绕过 `_authorize_approver` 决策门;测试仅盖非 None(test_ontology_acl_matrix.py:376 等),None-skip 无 pin |

**B4(审批后 requester 重验非真 fail-closed)——3/3 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B4.1 三 helper 内吞异常返回宽默认 | ✅ | (a)dingtalk_identity.py:442-444 `except → return [], False` 逐字存在;(b)888-890 `resolve_kb_identity` except → `KbIdentity.build(role=ROLE_EMPLOYEE)`;(c)555-557 `user_row_revoked` except → False,且调用点 routes/agent.py:1353-1357 **双重吞**(注释"读失败 fail-open 到既有语义")。RDS/钉钉宕机时 `IdentityUnresolvable(retryable=True)` 分支实际不可达 |
| B4.2 空 ACL 仍放行 public 对象 | ✅ | ontology/authz.py:44-45 `data_classification == "public" → return True`(ACL 之前);can_mutate_identity(95-102)无附加部门要求;SQL 孪生 68-69 同。三 fallback 组合=对 public+active 目标**放行** |
| B4.3 测试 mock helper 抛异常,真吞噬行为未测 | ✅⚠️措辞 | test_agent_batch3_fixes.py:298-310 `boom` raise "dingtalk api down"——恰是真实现**吞掉**的那类故障;墓碑测试(313-335)与 e2e(test_agent_runtime_e2e_local_db.py:614-621)全是固定 lambda stub。纠偏:评审"mock to throw"字面只符合第一个测试(其余为固定返回 mock);且 `user_row_revoked` 的 fail-open 有独立单测 pin 为**有意行为**(382-384)——未测的是"组合路径"而非各件 |

nuance(评审有利面之节制):B4 复合场景要求**部分故障**(身份库败而 ontology 库可达)——评审"transient identity-store failure"措辞精确;墓碑读成功时被禁用者会被拦,缺口特定于故障窗口。

**B5(confidential 结果击穿模型出境天花板)——4/4 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B5.1 ToolSpec 静态 classification=internal | ✅ | ontology_resolve.py:58-70 `data_classification="internal"`(frozen dataclass,tool.py:44/58)。nuance:该工具 docstring"刻意不进 registry"已过时——`agent_tools/__init__.py:55-58` 在 `RAG_ONTOLOGY_TOOLS_ENABLE` 开时注册,恰是 B5 关心的开旗配置;knowledge_search 同病(spec=internal 而 KB chunk 可 restricted——Literal 都表达不了) |
| B5.2 同部门可读 confidential 对象 | ✅ | authz.py:37-47:confidential 读判据=`owner_dept ∈ acl`;schema/027:28 ENUM 含 confidential;resolve.py:358-366 精确命中回真实密级。nuance:437 行的 confidential 过滤仅盖 embedding 候选池(护外呼),精确/规则路径正常放行 |
| B5.3 执行器 stamp 的是 spec 密级 | ✅ | tool_executor.py:435-442 读 `spec.data_classification`,且**在工具运行前** stamp(RR-2 注释)——结构上不可能见到返回行;`ToolResult`(tool.py:134-144)**根本没有 classification 字段**,结果级密级无通道 |
| B5.4 egress 天花板只见 internal | ✅ | model_gateway.py:442-462 `_egress_guard` 比较对象=`ctx.max_data_classification`(即 spec 级 max);internal>internal=False → 放行,confidential 标题/refs 经 loop.py:365 进下一次 DashScope 调用;测试仅手工 `__setattr__` stamp(test_agent_r3_p2_batch.py:45-61),真实 tool→executor→model 路径无测试——与评审"test the real path"要求吻合 |

**B6(成绩单失效)——8/8 ✅(含两处精度纠偏)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B6.1 CI continue-on-error 掩蔽 freshness | ✅⚠️精度 | op0 ci.yml:248-264 `baseline-freshness` **job 级** `continue-on-error: true`(注释:"refreeze 后删掉…翻硬门(RB-05 E2)");**main ci.yml:225-241 逐字节同构同样掩蔽**——评审把它única归 op0 不完整;两树同病。GitHub 实况:head 提交 CI+Frontend 双绿(gh run list 亲验) |
| B6.2 release-gate 仅 RAG L0-L6,agent 评测独立手动 | ✅ | op0 Makefile:118-119→`deploy/eval_release_gate.sh:29` `LAYERS=l0..l6`;`agent-eval`/`agent-eval-gate`/`agent-eval-baseline-freeze`(129-137)无任何自动调用点(workflows/deploy/dataworks 全 exit=1,仅 freshness 脚本提示字符串) |
| B6.3 agent 出闸仅 light 档 | ✅ | runner.py:513-519:`--tier` 四档可跑,但 `--gate and tier != "light"` → return 2("基线冻在 light 臂");tier 亦是 regime match key(69) |
| B6.4 ontology 指标仅显示不进闸 | ✅ | runner.py:454-457 计算、544 注释"仅显示——…不进闸";`HARD_INVARIANTS=("approval_suspend_rate",)`、`GATED_METRICS` 五项均无 ontology(59-61);`_gate()`(463-480)不读之 |
| B6.5 默认 31 例,漏 5 例 on-arm ontology | ✅ | `agent_cases.json`=31(tool_expected 10/no_tool 6/write_approval 6/grounded 9);`agent_cases_ontology.json`=5(o01-o05,自述"默认…不吃它");`agent_cases_onarm.json`=36=31∪5。**附加发现**:冻结基线 n_total=36(on-arm 冻),默认门 31 例——旧格式无 cases_sha16,案例集与基线无结构绑定 |
| B6.6 无模型行为 ACL 案例族 | ✅ | 三案例文件族清单无 ACL;runner.py:349-350 全案例固定 `acl_groups=["production"], roles=["employee"]`;注释自认"ACL 有独立单测族"(代码路径单测在,模型行为评测无——评审措辞精确) |
| B6.7 冻结 agent 报告=07-12 | ✅ | baseline.json:2-4 `frozen_at: 2026-07-12T01:16:31`,frozen_from=agent_eval_20260712T011535.json(live/light/36 例);盘上无更新报告 |
| B6.8 agent 基线旧格式无 regime | ✅⚠️精度 | agent baseline 键={frozen_from,frozen_at,provider,metrics} 无 regime;RAG baseline 有完整 regime;现 runner REGIME_MATCH_KEYS=(cases_sha16,model,tier,ontology_flag_on,prompt_sha16,provider)且冻结/出闸双向 fail-closed 拒旧格式。精度:评审括号列的是 agent 侧键名,RAG 侧 regime 键集不同(eval_set_sha/fusion/…) |

(freshness 数字三项、CI 绿、258/251 金集数已在 §1.2 亲手复现,逐位吻合)

**B7(通用迁移 runner 重放不安全)——6/6 ✅(附三点精化)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B7.1 `_ledger_conflict` 两态同返 None→重放全量重执行 | ✅ | op0 apply_migration.py:216-233(docstring 自认"checksum 一致…→ None(幂等重跑)"——把幂等性押在文件自身);caller 314-327 无 skip 分支直接 `cur.execute(s)`,1060 直接崩 |
| B7.2 裸 ADD COLUMN/多动作 ALTER 清单 | ✅ | 裸单列:031/036/046/049/050;多动作(main 守卫也刻意不救):037/039/047/048(+048 第二句 ADD KEY 两个正则都不匹配)/052/053;042 裸 CREATE INDEX(其注释自认守卫在 scratch 脚本里)。**重放安全**的:026/035/044(MODIFY)、027/032/033/038(PREPARE 自守卫)、其余 IF NOT EXISTS |
| B7.3 049 注释谎称 runner 会跳 | ✅ | 049:20-21 逐字"重复执行由 scripts/apply_migration.py 的 information_schema 预检跳过(同 031/048 约定)";op0 runner `_statement_skip_reason\|information_schema.statistics\|1060` 全 exit=1。**这是家族病**:031/036/046/047/048(+035 MODIFY 变体)同措辞;main 守卫 docstring 自证"此前并不存在" |
| B7.4 CI 走 ci_load_schema.sh 不测重放 | ✅ | 每文件恰载一次(119-131);workflows 中 `apply_migration` 零调用;**更狠**:test job 无 MySQL(重放测试 skipif 自跳),db-integration 有 MySQL 但点名模块不含重放/守卫,stress 只跑冒烟——runner commit 路径 CI 里一次都没跑过 |
| B7.5 无"同迁移二次 apply"测试(经通用 runner) | ✅⚠️范围 | 守卫测试全 MagicMock 单测,通用 runner --commit 路径零执行。纠偏:`test_apply_ontology_dbs_replay.py:94-148` **确有**真库双 apply+brick 恢复——但走 `apply_ontology_dbs.py` 自己的 `_plan_action` 台账 skip,仅盖自幂等 ontology 族,且不进 CI;评审限定措辞恰好准确 |
| B7.6 main 已有 information_schema 预检 | ✅ | main:216-258 双正则+单 ADD 限定+"绝不因守卫故障吞掉真错误",接 dry-run+commit 双环;**但 `_ledger_conflict` 两树逐字节相同**——评审要求的"区分 already/not applied"在 main 也未做(main=逐句补偿式);多动作 ALTER 在 main 重放同样崩(main 自己就带 039),"多列复合 ALTER 刻意不支持"为其 docstring 明示 |
| 附:部分应用无台账行 | ✅ | op0 语句环 323-327 单 commit 在后,台账 INSERT 更后(332-349);DDL 隐式提交→中途失败=schema 半改+无台账行→重跑 1060 卡死;分支明知此病(replay 测试 139 行自名"brick 恢复")但只修在 ontology 编排器 |

**B8(灰度不证 Agent 可用)——4/4 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B8.1 verify_health 仅 /api/version+/api/ready | ✅ | sae_canary_deploy.py:60-76 恰两 URL,无认证 GET;`agent\|Authorization\|/api/ask\|synthetic` 全文零命中。nuance:有 git_commit 前缀匹配(69)——版本核对,非功能事务 |
| B8.2 自述不改/不验 env flag | ✅ | 169-170 逐字:"SAE env 变量若有新 flag,**本脚本不改 env**——在控制台核对后再灰度开 flag";全文无 DescribeApplicationConfig |
| B8.3 任意 tag 无 digest/attestation | ✅ | `--image` 直通 `--ImageUrl`(116/137),无 `@sha256:` 校验;唯一绑定=运行时自报 git_commit 子串匹配。nuance:**基础镜像** digest-pinned+`--require-hashes`(Dockerfile:12/24/48)——构建期硬,发布引用软 |
| B8.4 ready 可在 Agent 栈惰死时 200 | ✅ | (a)flag off→`skipped` 恒过(readiness.py:89-91/97-99/427-428;api.py:796-799);(b)Redis 默认非关键(api.py:773-787 "ready 与 Redis 解耦…RAG_READY_REDIS_STRICT 恢复旧语义",ask 路径自身 503);(c)DashScope 默认 config-only,live 探针 opt-in 且"只报告不摘流量"(readiness.py:725-733);(d)worker/reaper/heartbeat/owner readiness 零命中(exit=1),durable_dispatch 检查仅 schema 形态(422-443) |

**B9(冷启动无恢复 owner)——4/4 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| B9.1 reaper/dispatcher 惰启于首个 agent 请求 | ✅ | routes/agent.py:379-390 `_get_runtime()` "惰性建运行时单例"→`_build_runtime()` 末 452-453 `_start_reaper`+`_start_dispatcher`(daemon 线程 299/324-325);调用点全在 agent 请求 handler。**加重发现:连 /api/ready 也不建 runtime**(tool_registry 检查仅建 registry,readiness.py:111-119)——LB 探活养着惰死实例 |
| B9.2 FastAPI startup 不初始化 | ✅ | api.py:121-183 `_lifespan` 启动侧仅线程令牌/钉钉回调/Stream/limiter/embedding 契约/TLS 自检;agent 唯一引用在**关闭**侧 177-183 `_agent_shutdown_drain`;`_get_runtime\|_start_dispatcher\|_start_reaper` 在 api.py 零命中 |
| B9.3 Dockerfile 仅 uvicorn | ✅ | Dockerfile:72-78 唯一 CMD=uvicorn --workers 1;docker-compose.yml 仅本地 mysql+opensearch,无 app/worker 服务 |
| B9.4 agent_worker user-gated 无部署清单 | ✅ | agent_worker.py:22 逐字"部署形态(SAE 第二应用/单副本 worker)**user-gated**";`git ls-files` 全仓引用恰 6 文件,非文档/源码仅 Makefile:33 本地前台便捷目标;三个 deploy/*.plist 均与之无关;build_worker 42-47 双 flag fail-fast |

### Major Concerns

**M1(main 默认开放)——4/4 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M1.1 RAG_REQUIRE_AUTH 默认关+public=员工非互联网 | ✅ | main api.py:562-566 默认 off "现网行为不变(匿名按 public)";573-574 逐字"公司内知识库的 public 语义是全员可见,不是全互联网可调用" |
| M1.2 main 生产启动无姿态断言 | ✅ | config.py 生产守卫块仅 P2-27 PII/上传签名 warning/Gemini 禁令;`REQUIRE_AUTH\|ACL_FAIL_CLOSED\|LEGACY_OPEN` 在 config/env_guard/lifespan/deploy/Dockerfile 全无强制(逐 pattern exit=1) |
| M1.3 main 测试锁死默认开放 | ✅ | test_main_p0_hardening.py:64-68 `test_require_auth_off_allows_anon`("现网行为不变");117-125 默认保留全部;test_miniapp_serving.py:509-528 匿名照常 |
| M1.4 op0 生产/staging 启动强制,当日 ack 逃生 | ✅ | op0 config.py:1124-1146:缺 `RAG_REQUIRE_AUTH`/`RAG_ACL_FAIL_CLOSED` 任一→ValueError,除非 `RAG_ALLOW_LEGACY_OPEN_PROD=ack:<当日 YYYY-MM-DD>`(午夜过期,重启重签,critical 留痕,裸 ack 显式失效——P1-14) |

**M2(钉钉主路径绕 admission+无界线程)——2/2 ✅(两树同病,文件仅注释差异)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M2.1 主 RAG 路径无 admit_ask | ✅ | 两树 dingtalk_bot.py 全文唯一 admission=`admit_general`(main:1120/op0:1118,通用能力 helper,pre-route 与兜底两支共用);主链 webhook→claim→线程→`_process_rag_query` 仅验签+msgId 去重;api.py:612 自认"/dingtalk/* 不经过本函数(admit_ask)";answer_flow/retriever/llm_generator `admit_` exit=1 |
| M2.2 每消息新 daemon 线程无界 | ✅ | main:1727-1736(op0 同)`threading.Thread(target=_process_rag_query, daemon=True).start()`+流式再起 `dt-stream-push` 线程(972);`Semaphore\|ThreadPoolExecutor\|max_workers\|Queue` 全文零并发原语;Stream 入口 default-executor 只托管 claim+spawn 即返回,RAG 线程仍无界;msgId 去重=幂等非 admission |

**M3(commit-ACK 歧义误分类)——2/2 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M3.1 丢 ACK→failed 而非 uncertain,对账器只扫 uncertain | ✅ | store.py:877-939 commit 异常 re-raise;工具 ontology_identity_resolve.py:309-312 `except Exception → ToolResult.fail`(**catch-all 连 commit 段一起包**——恰违反 executor 553-554 注释自设的"预边界失败才用 fail"假设);tool_executor.py:536-541 返回值→failed,555-561 仅 raise 才 uncertain;run_store.py:1166/1182 `WHERE status='uncertain'`。缓解(不翻案):同 key 重试可 CAS 回收+台账 PK fence 成幂等成功——但前提是有人在被告知"确定失败"后仍重试;对账器本身还默认旗关 |
| M3.2 记账合并事务丢 ACK→回退分段写双计,resume 吃虚胖计数 | ✅ | run_store.py:300-348 合并事务 rethrow(docstring 自认"executor 侧包 fail-open 并回退分段写");executor.py:889-924 回退再写 step+budget+llm rows(llm_call_log 新 uuid 无去重);resume 507-509 从 durable 计数播种。伤害方向保守(多计→提前爆预算),非失控花费 |

**M4(幂等 run 域而非业务请求域)——2/2 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M4.1 ask 无客户端幂等键 | ✅ | 请求模型 api.py:321-351 无幂等字段;routes/agent.py:725 每 POST 无条件新 message_id,run_store:180 新 uuid run;**对照**:approve 端点自己就有 idempotency_key(937-943)——非仓风格缺失而是 ask 独漏。缓解:uk_thread_active 挡"同线程仍活着"的双击 409;不挡"响应丢失后重试"或无 session_id 客户端(每 POST 铸新 sid: 线程,699-701) |
| M4.2 工具键 run/turn/call 域;ontology 有业务唯一键兜底 | ✅ | policy.py:249-251 `idem = f"{run_id}:t{turn}:{call_id}"`;operation_id=invocation uuid 同 run 域;兜底=schema/028:42 `uk_ns_norm_active`+工具双路径处理(幂等成功/受治理冲突拒绝);平台层无业务域键推导——评审"未来 ERP/外部写无保护"推断准确 |

**M5(stewardship 轮换卡死审批)——✅(附激活前提)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M5.1 可见性=存量 scope,裁决=live scope,轮换后 A 见不能批/B 批不能见 | ✅ | 列表 approval_store.py:406-421 `FIND_IN_SET(%s, approver_scope)` 纯查存量列;裁决 routes/agent.py:1023-1041 `resolve_scope_live` 覆盖→dept_admin 不在 live scope→403;**预可见门也用存量 scope**(1085-1094,B 连拿 run_id 都 404);kb_admin 双过(2199/1035-1036)。前提:需 HIGH_WRITE ontology 工具注册 live resolver(双旗默认关)——恰是 M5 自身场景;"死锁"有界:kb_admin 或 3 天 TTL 过期 |

**M6(审批积压无预算/无分页)——✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M6.1 无 pending 配额;3 天 TTL;200 上限 oldest-first 无游标 | ✅ | 入场仅共享 ask limiter(routes/agent.py:685);`count_active_runs` 明确不计 suspended(run_store:693-705 docstring)→挂起审批不占全局 run 容量;TTL=259200s(approval_store:33-34);列表参数恰 `mine`+`limit`(≤200),`ORDER BY created_at ASC LIMIT` 无 cursor/offset(2178-2192/419-422);queue_monitor.py:69-77 只盯 review_task/user_feedback,approval_request 无老化指标 |

**M7(审计证据不防篡改)——✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M7.1 普通 InnoDB+运营凭证写入+无哈希链+retention 可删+gzip 无签名 | ✅ | schema/024:9 自认"APPEND-ONLY 是约定…瘦身归 retention.py 不在 DDL 层",uuid PK 无链无签名列,schema/ 无 TRIGGER/GRANT;audit.py:110-112 复用 `_get_db_conn()` 运营连接(config.py `audit` 零命中——无独立凭证);retention.py:23/247-254 24 月后 DELETE(双闸但闸在同一应用内,凭证本身持 DELETE 权);归档 336-357 纯 gzip put_object 无签名清单,`object.lock\|worm` 全仓 exit=1;checkpoint HMAC(035)不覆盖审计行 |

**M8(RDS TLS 告警不阻断)——✅(定性纠偏:记录在案的拍板,非疏漏;现网已 TLS)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M8.1 无 CA→ssl_disabled,生产仅 warning | ✅⚠️定性+☁️ | 两树 `pymysql_ssl_args()` 无 CA→`{"ssl_disabled": True}`(显式明文,防 pymysql2.x PREFERRED 半开);生产分支 warning 不 raise(main config.py:700-710/op0:717-727 同文);**但注释逐字自认**"告警不阻断(记录在案的用户决策;…硬断随 SAE env 落地一并翻(B7,避免重部署 brick))",op0 姿态断言也明示 TLS 刻意不在范围(1122-1123);staging 分支连 warning 都无(比评审说的还弱一点)。☁️现网层面:07-21 运维台账记载生产实例 TLS 已实配验活(f09+CA)——评审"observed verified TLS cipher in the deployed instance"的实况在库外,本核查不背书 |

**M9(Agent 延迟/可用性缺席权威 SLO)——3 ✅ + 1 ⚠️**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M9.1 agent QA 行 latency_ms=0 | ✅ | 9 个写点全在 routes/agent.py(model_name="agent"),无一传延迟;事务路径 `insert_qa_row_tx` **签名根本没有 latency 参数**,列值硬编码 `0, None, None`(qa_logger.py:247/256);真实模型调用延迟另存 llm_call_log(有值)——端到端 run 延迟恒 0 |
| M9.2 rollup 排除非正延迟→agent 慢不动 p95 | ✅ | qa_rollup.py:212-214 `lat > 0` 才进 p50/p95(SLO 25s 判定 164-166);weekly 报告读预计算表继承排除;governance 磁贴同款 SQL `WHERE latency_ms > 0`(kb_console.py:1277)。滤波先于 Agent 存在(也滤存量空值行),效果如评审所述 |
| M9.3 通用看板排除 agent 行,无 agent 可用性口径 | ✅ | kb_console.py:1502/1511 `model_name <> 'agent'`,注释 1494-1496 自认"agent 可用性走自己的口径"——**承诺无实现**(AGENT_ERROR 零聚合消费者,ops_monitor._JOBS 无 agent 作业)。补充事实(对 op0 略有利):**夜间 rollup 不排除 agent 行**(qa_rollup.py:380-391 无谓词)——agent 行进 total/answer_rate/error_rate,只对 latency 与看板可用性磁贴不可见;评审原文只说 dashboard,措辞精确 |
| M9.4 八维监控全缺 | ⚠️ | 6/8 维确实零覆盖(dispatch backlog:`backlog_count()` 定义了**零消费者**;relay lag/取消延迟/收敛:无度量;uncertain/approval age:仅 logger.warning);**纠偏**:stale runs 有真探针——进程内 reaper+书面 SLO("≈20 分钟内必收尸…收尸即 ops 告警",routes/agent.py:215-242)+EDITED 重驱告警(1663-1668)——但活在 serving 进程内(进程死=探针死),不在 ops_monitor/DataWorks 权威面。评审标题"absent from authoritative SLOs"存活,绝对化列表需 stale-run 豁免 |

**M10(常态监控未证明)——3/3 ✅(其一 hedge 应验+☁️)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M10.1 仓内声明:DataWorks 未排程/LaunchAgent unloaded/节点暂停手贴 | ✅☁️ | 三处声明逐字在案(ops_monitoring_schedule.md:21/71-73/95;节点头注 dev id>2^53 须控制台手贴);**评审自己 hedge"may be stale"应验**:"unloaded"是 06-17 旧话,被仓内更新文档(07-18 台账"launchd 当天实跑"、metrics_writer"daily 02:50 verified")与本机实测双重反证——`launchctl list` 三任务**当前全部已加载**(plist mtime 07-19);但评审更强的那句"无 alert path 的当前 attestation"在仓内成立:07-18 台账记告警链断裂,两树均无 07-21 闭环的文档记录(在库外);DataWorks 控制台实况=☁️ |
| M10.2 节点只跑只读对账;QA SLO rollup 注释掉 | ✅ | main:171-173 `--only reconcile_ha3 reconcile_oss`/op0:178-181 五只读作业;两树 `# 阶段2…含 qa_rollup` 均为注释;唯一 qa_daily_metrics 写手恰是被注释部分(节点每跑仍 UPSERT 一行心跳——PROD-RO 下被挡,07-18 台账已records) |
| M10.3 webhook 未配=告警 no-op | ✅ | alerting.py:101-109 `SUPPRESSED-CRITICAL`/no-op + `_note_suppressed`;op0 节点文件 :83-84 自警"2026-07-15 实锤…日日 Failure 无人收 ⚠️必填";压制可见化仅进程内(跨进程 launchd/DataWorks 的压制不进 governance 面) |

**M15(成本控制是代理指标)——2 ✅ + 1 ⚠️**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M15.1 agent LLM 台账 cost_estimate=None | ✅ | model_gateway.py:402/412 唯二写点均 None;**记录在案的拍板**:schema/023:6"不编造模型单价…价表配置后回算",kb_console.py:1577 同注——诚实 NULL 而非缺陷性置零(07-18 台账已裁过同款) |
| M15.2 默认 12 turns/200k tokens | ✅ | context.py:24-27 逐字 `DEFAULT_MAX_TURNS = 12`/`DEFAULT_TOKEN_BUDGET = 200_000`;executor.py:526-533 fail-closed。评审漏提:另有 24 tool-call 上限+10 分钟 run deadline(真实边界,但同样非货币) |
| M15.3 全局只数调用;"无价表/货币预算/成本告警" | ⚠️ | Agent/serving 域属实(rate_limiter.py:41-45 语义="模型调用数";熔断告警是次数告警);**绝对化被推翻**:摄取侧 `extraction/cost_breaker.py` 是**带测试的 RMB 货币控制**——config.py:304-312 单价表(OCR 0.06/页,VLM 0.04/图)+单文档 5/单跑 200/日预算熔断+成本隔离;树级真命题只剩"无 webhook 级成本告警"(breaker 触发仅 logger.warning+print,未接 send_ops_alert) |

**M11(容量证据陈旧/mock/非阻断)——2/2 ✅(触发器措辞纠偏)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M11.1 压测证据落后 head 且 mock | ✅ | 唯一 full 报告=run_20260718T090755Z(代码指纹 b9424f7,落后 head **139 提交**/约 2 天);报告「诚实范围」自认 DashScope chat/embedding mock+检索走本地回退+mock 时延常数 0.3s——评审是在转述报告的自我声明,非揭发 |
| M11.2 G1-G9 draft;RSS 未测;无当前 head 压测 | ✅⚠️触发器 | gates.py:41-42 hard_fail 只计非 draft,scenarios.py 12 处 draft=True;G9-rss 实测 n/a(serverctl.py:134 读 /proc,macOS 无);**纠偏**:stress.yml 实有三触发(dispatch/pull_request 路径限定/cron 周日)非"仅 PR"——但**无 push 触发**、schedule 死的(stress.yml 不在默认分支,gh 404 亲验)、无 PR(gh pr list 空)→净效果=无自动当前 head 压测路径,评审结论成立 |

**M12(无单一可复现出货工件)——3/3 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M12.1 op0 Dockerfile 四硬化 | ✅ | :12/:24 node+python 双 digest 钉;:48 `--require-hashes`(lock 1945 个 sha256);:59-60 useradd/USER appuser;:17-21/52-53 前端多阶段 |
| M12.2 README 同时描述手工 SAE zip 浮动依赖 | ✅ | README.md:95 手工装配配方(requirements.txt 必须);requirements.txt 全下界浮动(`requests>=2.31` 等)——两条工件路径供应链姿态不同并存 |
| M12.3 CI 不 build/smoke 镜像不绑 digest | ✅ | workflows+Makefile `docker build\|build-push` exit=1;Trivy 是 fs 扫非镜像扫;canary --image 任意 tag。nuance:canary 部署后验 git_commit==--git-sha 是真实缓解,但信操作员自报,与 CI 测试/评测零绑定 |

**M13(DataWorks 完整性/依赖控制弱于文档)——3/3 ✅(两树同病)**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M13.1 sidecar 缺失/不可读全放行 | ✅ | 两树所有 zip 消费节点同一 `_verify_zip_integrity`:`except Exception → "过渡期放行" return digest`(op0 8 节点/main 6 节点逐一列点);裸 except 连 I/O/权限错都吞——"unreadable"精确成立;仅 mismatch 才 raise;README 称"节点硬校验"言过其实 |
| M13.2 sidecar 与 zip 同信任边界,无签名 | ✅ | build_dataworks_zip.sh:7-8/41:两份都是 DataWorks 资源,同 `odps.get_resource()` 取;能换 zip 者必能换 sidecar;`gpg\|cosign\|minisign` exit=1 |
| M13.3 现代 Python 分支运行时裸装;py3.7 钉版在 lock/审计外 | ✅ | stage3:18-34 DEPS 全不钉+`pip install --force-reinstall` 每跑一遍;py3.7 钉版集(Pillow 9.5.0 等)与 op0 lock 版本不同(pypdf 3.17.4 vs 6.14.2),main 无 lock;两树 pip-audit 目标均不含 py3.7 钉版 |

**M14(Agent UX E2E 不进 CI)——2/3 ✅ + 1 ⚠️**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M14.1 Playwright agent 场景存在 | ✅ | agent-canary.spec.ts:27 flag 不可见/:41 SSE+审批卡/:87-89 持久化/:93 运行中心,及 ux-gate/contribute 等 spec |
| M14.2 前端 CI 仅 vitest+build | ✅ | frontend.yml 恰两步;`playwright` 在全部 workflows exit=1 |
| M14.3 vitest 排除 tests/**;硬门套件"自身被跳" | ⚠️ | 排除属实(vite.config.ts:13);**纠偏**:ux-gate.spec.ts 三组只有 1 组 `.skip`(管理页组,testid 未接线 TODO),「AI 助手」「审批中心」两组未跳(32 测试)——但只能本地 `npm run e2e` 跑,CI 缺口结论存活,"套件自身被跳"以偏概全 |

**M16(文档与代码实质矛盾)——4/4 ✅**

| 子项 | 裁决 | 关键证据 |
|---|---|---|
| M16.1 execution-model.md 否认 worker 层+列 pending 实已存在 | ✅ | 文档 :15 "无独立 agent worker 层"、§5 loop/executor/relay/排水"尚待实现";代码 DefaultAgentLoop/ThreadedRunExecutor/drain/_RedisRelay/durable_dispatcher 全在 |
| M16.2 implementation-plan 冻在 7c704ce(落后 499 提交),许诺不存在的 deploy.yml+可配 workers | ✅ | deploy.yml 不存在(exit=1);`RAG_UVICORN_WORKERS` exit=1;Dockerfile:76 硬编码 --workers 1 |
| M16.3 op0 报 251 vs main 258 | ✅ | 两树 README.md:21 逐字 251/258;goldset 长度实测 251/258(亲手复跑离线门同值);op0 内部自洽,drift 是分支-vs-main。**副产品:main CLAUDE.md:78,80 自己仍写 251-q 两处**(main 自身文档漂移) |
| M16.4 main README 许诺 Redis 四态/durable dispatch/副本拓扑守卫而 main 代码没有 | ✅ | main README.md:100-102 逐字"多副本能力已备……拓扑守卫启动拦截";main 代码 `RAG_EXPECTED_REPLICAS\|durable_dispatcher\|SESSION_BACKEND` 全 exit=1——守卫只在 op0 config.py:1148-1182。**已核实的最危险文档漂移:main 部署上这守卫永远不会响** |

### Minor issues

| 条目 | 裁决 | 关键证据 |
|---|---|---|
| MIN3 relay 恢复可发 [DONE] 无语义终局帧 | ✅ | event_relay.py:160-163 XADD 败→`_dead=True`,145-147 后续帧(含终态+`__end__`)静默丢——272-273 注释自认;恢复路径 286-299 探到 durable 终态即 return **不合成终局帧**;routes/agent.py:2106 循环落空直发 `[DONE]`;console useAgentAsk.ts:413-415 自认"clean EOF 没终局帧=转轮询兜底"。RR-EVT-02 全文修复只救"终态帧发出过"的场景 |
| MIN4 gen.send 与下次模型调用间无 cancel 检查 | ✅⚠️(实况更糟) | executor.py 全文 `cancelled()` 恰 539/649 两处;563(工具执行)→590(`gen.send`)之间零检查;loop.py:384 下一模型调用在 resumption 内同步发生。**加重**:流式下一轮首 delta 即中止(649);**非流式**(RAG_AGENT_STREAM=false)下一轮若返 RunCompleted,621-638 `_complete_run` 全程无 cancel 检查——取消被整个吞掉,run 落 succeeded |
| MIN5 每真实工具调用双写 agent_step(tool_call) | ✅ | 写点1:executor.py:547-556→record_tool_call(run_store:367-372);写点2:policy.py:188-190 append_step(286-291);同一事件两行(step_no 各取 MAX+1 必不同);读侧 list_steps 无去重直出 run-detail。**加重**:审批 resume 再过两写点,单逻辑调用最多 4 行;两行 payload 形状不同(args_digest 仅裁决行)难以事后合并 |
| MIN6 Agent 仅 console;钉钉/miniapp 不触 | ✅ | dingtalk_bot/stream_runner/card/intent_router 四文件 `agent_runtime\|/api/agent` 全 exit=1;miniapp API 面无 /api/agent/*;所有 ExecutionContext `channel="console"`(routes/agent.py:763/851/1163);正向对照:console 三 composable 在调 |

### Strong foundations(评审正面清单抽查)

| 条目 | 裁决 | 关键证据 |
|---|---|---|
| SF2 HIGH_WRITE 独立双层旗 | ✅ | agent_tools/__init__.py:17-39 `RAG_ONTOLOGY_WRITE_TOOLS_ENABLE` 字面层叠在 `RAG_ONTOLOGY_TOOLS_ENABLE` 之上(读关强制写关,双默认关);registry/policy/审批三层各自执行;写工具注册还硬依赖注入护栏开启(62-66) |
| SF3 生产 Agent 启动四件套 | ✅ | config.py:1070-1101:`RAG_PROMPT_INJECTION_GUARD` 缺→raise;checkpoint 三件(专用 KEY/REQUIRE_HMAC/ENCRYPT)缺任一→raise;运行时双保险真实存在(executor.py:472-489 HMAC 错/裸 digest→RunRejected;loop.py:106-130 生产拒明文回退) |
| SF4 答案+succeeded 同事务+commit 歧义处理 | ✅ | run_store.py:487-542 单事务(FOR UPDATE→extra_writer→CAS);551-569 commit 异常→新连接读后验证×3 重试;executor.py:697-709 "绝不发 done" |
| SF5 决定+resume 命令同事务 | ✅⚠️门控 | approval_store.py:215-302 单事务含 outbox_writer,"决定 commit ⇒ 命令 durable";**但仅当 `RAG_AGENT_DURABLE_DISPATCH` 开(默认关)且 outcome≠edited**;旗关时 resume=进程内,无命令可耦合 |
| SF6 ontology 写+台账同事务 | ✅ | operation_ledger.py:56-72(重复 PK→OperationAlreadyApplied→整事务回滚=fencing);store.py:886-929 单事务闭环;operation_writer 未注入的直调/简化 ctx 走人工对账通道(记录在案的 fail-open 边缘) |
| SF7 dispatch 命令→run 唯一键 fence | ✅⚠️门控 | schema/052:10-24 `uk_run_dispatch_cmd`;run_store.py:182-251 同 INSERT 原子落锚+1062→DispatchCommandBound→find_run_by_dispatch_command,"绝不产生第二个 run";**052 未 apply 时(1054)退化为无锚 INSERT 仅警一次——fence 在 DB 层不存在**(与记忆"052/053 开 flag 前必 apply"一致);另有 037 `uk_thread_active` 独立第二 fence |
| SF8 无 shell/URL 工具;schema 拒身份参数 | ✅(比评审说的更强) | 注册面恰 4 工具(knowledge_search 恒注册;ontology_resolve/packing_calc/ontology_identity_resolve 旗控);agent_tools/ `subprocess\|urlopen\|requests\|httpx` exit=1;四工具 schema **全部** `additionalProperties: False`+反伪造身份注释,Draft202012Validator 前置强制——评审"generally"实为 4/4 |

## 9. 核查副产品(评审未提、本次核查新增的事实)

1. **main 自身文档漂移**:CLAUDE.md:78/80 仍写"251-q gold set"两处(README 已改 258)——M16.3 的镜像问题在 main 自家。
2. **B1.2 双向不相交**:不只 op0 缺 main 的权威空组墓碑;**main 也缺 op0 的 `user_row_revoked`**(严格模式 401+agent 审批重验)。合并时两套墓碑机制都可能被"选边"丢掉——比评审说的更具体的合并险。
3. **B6.5 附加**:冻结 agent 基线 n_total=36(on-arm 冻)而默认门吃 31 例集;旧格式无 cases_sha16,案例集与基线无结构绑定——除 regime 缺失外的第二重不可比性。
4. **MIN4 比评审更糟**:非流式(RAG_AGENT_STREAM=false)下取消可被整个吞掉,run 落 **succeeded**(流式只是多付一次首 delta 即断)。
5. **MIN5 加重**:审批 resume 再过双写点,单逻辑工具调用最多 **4 行** agent_step;两行 payload 形状不同,读侧难以事后合并。
6. **stress.yml schedule 永不发车**:cron 触发在,但 workflow 不在 GitHub 默认分支(gh 404 亲验)——周日定时压测是死字母。
7. **M13.1 裸 except**:sidecar 校验 `except Exception` 连 I/O/权限错一并放行;README 称"节点硬校验"言过其实(两树 README 同病)。
8. **staging 无 TLS warning**:生产分支有 P0-02 告警,staging 分支连告警都没有——比评审含混带过的还弱一档。
9. **本机 launchd 实况**(只读观察):`com.fuling.ops-monitor`/`com.fuling.qa-rollup`/`com.fuling.qa-weekly-report` 三任务当前全部加载(plist mtime 07-19);前两者 last exit=2。ops-monitor 的 2=其自身 drift/SLO 违约退出码(即"在跑并发现漂移"),**qa-rollup 的 exit=2 语义未核——值得看一眼当天日志**。
10. **B1.1 竞态在 op0 有真实并发写手**:kb_console.py:2452/2500/2698 的 restore/retire/visibility 端点恰是喂/撤 PENDING_DELETE 的并发方——评审说的竞态不是理论面。
11. **main 测试环境差异说明**:评审 2868 passed/35 skipped 与本机 2902/1 总数恒等 2903——34 个 service-gated 测试在评审环境 skip、本机(本地栈在跑)实跑全过;两份数字都真。
12. **op0 `_ledger_conflict` 与 main 逐字节相同**:评审 B7 要求的"区分 already/not applied"在 **main 也未做**(main 的 F9 修复是逐句补偿式)——该 required-fix 适用两树。

## 10. 未验证面(现网/云端,本核查不背书)

- SAE 现网 env flag 实况(REQUIRE_AUTH/ACL_FAIL_CLOSED/TLS cipher/副本数/Redis 后端)——评审与本核查同样只能☁️;记忆台账称 b9eeb873 已带 TLS+三新子线,未据此背书。
- DataWorks 控制台节点/调度实况(工单 3 确认项、清理stage3 重贴等)。
- GitHub branch protection 设置(B6.1 的 job 若被设为 required 则掩蔽失效——UI 态不可本地验)。
- 07-21 告警链端到端闭环:记忆有案,两树文档无 attestation——下次归档时补进 docs/。

## 11. 迁移批实施记录(2026-07-21,codex 三轮共识后)

用户拍板执行 A/B/C 三组「迁移即可解决」项。codex-review 六阶段:onboarding→方案→评审 REVISE(A1 双路径 CAS/B3 缺构建验证两 blocker)→修订→APPROVE 达成共识。逐文件点名提交,禁 git add -A。

**A 组(main→op0,四修复)——op0 全量 4368 passed exit=0**:
- A1 `7927fab` fix(reconcile):clients.py 保留常规 60s/10s 超时+新增 bounded 变体;pending-delete CAS→rollback / stranded CAS→skipped_stale 双语义;conftest 真库串行组;RAG_RECONCILE_HA3_DEADLINE_S 补 rag_env_registry。
- A4 `793af4e` fix(schema):_statement_skip_reason+双接线+守卫测试;032 已同跳过;三库 manifest 不碰。
- A3 `985db05` feat(obs):运营库/Stream 探针+两可选严格钳;critical_ok 保留 agent/ontology 检查;qa_logger 漂移升 ops page。
- A2 `4e0fcc6` fix(identity):resolver 去 is_active=1 过滤=权威空组;api 严格块统一契约(live=[]→user_row_revoked 确认→401,确认读失败回退 public-only);P1-08 死分支删除,agent 审批消费点保留。

**B 组(op0→main,三硬化)——main 全量 2912 passed exit=0(B1)/2904(B2)**:
- B2 `9495523` fix(identity):user_row_revoked verbatim 移植+api 严格块与分支同文;两树墓碑机制收敛(副产品②合并险消除)。
- B1 `693fcae` feat(security):姿态断言移植;test_posture_guard 七例+三既有守卫测试注入过渡 ack;本地四 overlay 补两 flag(备份在 scratchpad,亲验四标签空载全过);SAE 前置落 environment_design §2.1。
- B3 `69f1b39` chore(supply-chain):Dockerfile digest 钉+lock `--require-hashes --no-deps`;uv 从 main extras 生成 lock(1819 hash,op0-only jsonschema/redis/fakeredis 确认缺席=非抄 op0);ci.yml 保留 requirements.txt 审计+新增 lock 审计+修过时注释。**验证走替代路径**(本机 docker daemon 病态卡死,buildx 挂 31min@0.5s CPU 已杀):✅ lock 在 CPython 3.11 下 `uv pip install --require-hashes` 全量装通(=Dockerfile RUN 确切行为)+ main 生产 import 全通;⚠️ 未本地验=容器内非 root(USER appuser 行未改承基线)+digest 的 amd64 manifest(与 op0 已核验 digest 同),留 CI+真实 runner 终验,失败则整体 revert(requirements.txt 未动零副作用)。

**C 组(文档纠偏)**:
- main `83894bb` docs(readme):撤多副本虚假许诺+DW 硬校验措辞;CLAUDE.md 251→258 provenance 注(gitignored 本地生效不入库)。
- op0 `1afb048` docs:execution-model/implementation-plan 冻结状态注(不重写正文)+DW 措辞。

均未 push(push 属 Sam 决策);A/C 落 op0,B 落 main;两树保持 op0⊇main 前须后续吸收合并重做。
