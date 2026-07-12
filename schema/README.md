# schema/ — RDS DDL 单一权威（F-35，2026-07-01 确立）

**本目录是全部 RDS 表结构的唯一事实来源。** 任何环境（生产 / staging / 灾备 / 本地）
的表结构差异，以本目录文件为准裁决；任何直连改表都必须**先改这里的文件**，再经
apply 脚本落库并记台账。

## 铁律

1. **先文件后落库**：改生产表结构 = ①改/新增 `schema/NNN_*.sql` → ②写
   `scratch/apply_migration_NNN.py`（information_schema 幂等守卫 +
   `prod_access.get_prod_rw_conn(ack=...)` 当日 RW token）→ ③**同一会话**向目标库
   `schema_migrations` INSERT 一行。跳过任何一步都是事故预备役——010 漂移
   （生产有 `normalized_gap_query`、权威 DDL 没有，重建环境提交贡献必 1054）就是这么来的。
2. **修订已发布文件**记 `NNNa` 修订号（台账 filename 记 `NNN_xxx.sql@NNNa`），不改原行。
3. **编号严格单调递增**，下一个可用号 = 036（031=agent 审批/执行硬化【2026-07-11 占用——
   原「本体事件」的 P2 预订作废顺延，落地时取当时最新号】、032=台账 checksum、033=本体
   link 不变量、034=sem 视图 product ACL 列、035=checkpoint 摘要 HMAC 加宽）。历史上有三对编号冲突（002/003/006 各两个文件，
   见下表）——**不改名**（外部引用会悬空），台账里用 `002b/003b/006b` 区分，新文件绝不再冲突。
   ⚠️ **本体层文档编号勘误**：《本体层设计 v1.1》《P0-P1 落地细化》所写迁移号 024–028 成文时
   未预见 agent v2 占号，已全部作废——本体表族实际为 **027(core)/028(identity)/029(link)/
   030(sem_views)/033(link 不变量)**（event 表 P2 落地时取当时最新号；原预订的 031 已被
   agent 硬化占用）；文档所称"018 审批""023 预留 v2 P4"同样以本表为准
   （018=gen_meta、023=llm_call_log、审批=025）。详见 `docs/ontology_p0_plan_2026-07-10.md`。
4. **`CREATE DATABASE` 必须显式 `CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci`**，每张新表
   显式 COLLATE —— staging `_stg` 库曾因缺省漂移到 `_0900_ai_ci` 引发跨库 JOIN 1267。
5. DDL↔代码列契约由 `tests/test_schema_ddl_parity.py` 钉住（INSERT/SELECT 用到的列
   必须存在于权威 DDL）——新增读写列时同步改 schema 文件，否则测试红。
6. **新增 schema 文件须同步更新 `scripts/ci_load_schema.sh` 的 file→DB 清单**——CI
   db-integration job 用它从零建双库跑 DB 集成测试，清单缺文件时该 job 直接红（显式报名）。

## 文件 → 目标库

| 文件 | 库 | 内容 |
|---|---|---|
| 001_opensearch_pipeline.sql | fuling_knowledge | 基础管线：document_meta/version、chunk_meta、kb_audit_log、document_sensitive_finding、qa_session_log(初版) 等 |
| 002_feedback_system.sql | fuling_operation | user_feedback、escalation_ticket；qa_session_log 现行定义（含 message_id/延迟列） |
| 002_step_card_enhancement.sql | fuling_knowledge | chunk_meta 步骤卡列（parent_chunk_id/step_no/image_refs_json）⚠️ 编号冲突（002b） |
| 003_provenance_lineage.sql | fuling_knowledge | document_version.canonical_sha256、pipeline_run |
| 003_user_role_unique.sql | fuling_knowledge | user_role UNIQUE(user_id) ⚠️ 编号冲突（003b） |
| 004_observability_metrics.sql | 双库 | pipeline_run 嵌入指标列（knowledge）+ qa_daily_metrics（operation） |
| 005_cross_doc_dedup_index.sql | fuling_knowledge | idx_canonical_sha256（已应用生产 2026-06-22） |
| 006_conversation_history.sql | fuling_operation | 服务端会话历史（flag 默认 OFF） |
| 006_kb_admin_authz.sql | fuling_knowledge | kb 写授权基座（user_role.role/dept_admin_grant）⚠️ 编号冲突（006b） |
| 007_kb_etag_dedup_index.sql | fuling_knowledge | ETag 查重索引 |
| 008_kb_access_request.sql | fuling_knowledge | 跨部门检索授权申请（collation 对齐 _unicode_ci） |
| 009_acl_projection_outbox.sql | fuling_knowledge | ACL 投影 outbox（同事务 enqueue + UNIQUE(doc_id) 复活） |
| 010_kb_contribution.sql | fuling_operation | 员工知识贡献（010a 修订：补 normalized_gap_query） |
| 011_schema_migrations.sql | 三库（knowledge/operation/ontology 各一份） | DDL 变更台账（本机制自身） |
| 032_schema_migrations_checksum.sql | 三库（同 011 分布） | 台账加 checksum 列（PR-D P0-09：同名不同 SHA-256 即中止，防同版本内容漂移；information_schema 守卫幂等） |
| 012_qa_session_log_perf_index.sql | fuling_operation | (answer_status, created_at) 复合索引（性能第一梯队 #1） |
| 013_qa_retrieved_doc_fact.sql | fuling_operation | 检索/引用文档物化事实表 + 存量回填（perf#3；读侧 RAG_QA_FACT_JOIN 门控） |
| 014_document_version_raw_key_hash_index.sql | fuling_knowledge | raw_key_hash 回填 + idx_raw_key_hash（perf#5 注册幂等点查） |
| 015_kb_audit_log_history_index.sql | fuling_knowledge | kb_audit_log (operator_type, action_type, created_at) 复合索引（perf#83/#96 审批历史查询） |
| 016_user_feedback_dedup_unique.sql | fuling_operation | user_feedback (message_id,user_id) 去重 + uk_message_user 唯一键补建（存量库补救） |
| 017_qa_admission_reject.sql | fuling_operation | 限流拒绝聚合表 + qa_daily_metrics 拒绝列（盲区审计 P1-1：熔断日 SLO 不再假绿） |
| 018_gen_meta_runtime_contract.sql | 双库 | qa_session_log.gen_meta_json 生成元数据列（盲区审计 P2-20/21/22）+ rag_runtime_contract 运行时 KV（operation=嵌入契约行 P2-8；knowledge=ops 心跳 P2-14） |
| 019_chunk_meta_index_retry.sql | fuling_knowledge | chunk_meta.index_retry_count + DEAD 死信终态（G9：毒 chunk 队头阻塞修复；代码侧 1054 fail-open） |
| 020_document_version_simhash.sql | fuling_knowledge | document_version.content_simhash（G19：simhash 近重复 WARN；代码侧 1054 fail-open） |
| 021_ingest_quality_metrics.sql | fuling_knowledge | ingest_quality_metrics 批次质量指标表（G22 per-batch 质量哨兵；写侧 fail-open） |
| 022_agent_runtime.sql | fuling_operation | 企业级 Agent Runtime durable 表族：tool_registry + agent_run（单向状态机+心跳）+ agent_step + agent_checkpoint + tool_invocation（uk_tool_idem 幂等）（WS1-2；run_store.py） |
| 023_llm_call_log.sql | fuling_operation | LLM 调用账本（ModelGateway 每次调用记 token/成本/延迟；成本按 user/dept 归集，E5）（WS1 收尾②） |
| 024_agent_audit_log.sql | fuling_operation | Agent 合规审计（append-only；执行前 write-ahead 审计，HIGH_WRITE fail-closed / 普通 fail-open；audit.py）（WS1 收尾③） |
| 025_approval_workflow.sql | fuling_operation | Agent 审批闭环持久化：approval_request（挂起侧写，approver_scope + 过期即拒）+ approval_decision（决策侧写，uk_req_idem 幂等）——request→decision→invocation→audit 四表回放链（WS3；approval_store.py；深度审查 A 组 P1） |
| 026_agent_family_collation_request_id.sql | fuling_operation | agent 表族 9 表 COLLATE 显式钉到 utf8mb4_unicode_ci（铁律 4；022/025 吃库默认在漂移环境与显式 unicode_ci 的 023/024 混排 → run_id JOIN 1267）+ request_id 加宽 VARCHAR(64)（UUID 36 字符原宽度装不下）（深度审查 schema 组） |
| 027_ontology_core.sql | fuling_ontology | 本体控制面核心：ontology_object（canonical ULID+乐观锁+密级）+ ontology_ref_seq（展示号发号）+ ontology_attribute_source（属性溯源，纯来源治理）+ ontology_stewardship（授权 scope，S5）（本体 P0；docs/ontology_p0_plan_2026-07-10.md） |
| 028_ontology_identity.sql | fuling_ontology | 身份脊柱：ontology_identifier（别名映射，至多一行 active 生成列唯一键 S4；norm 不剥改模后缀）+ ontology_resolution_case/candidate（候选承载层 S2：证据快照+积压统计，一个未解析编号×N 候选）（本体 P0） |
| 029_ontology_link.sql | fuling_ontology | 本体关系：ontology_link（sku_of_product 等；uk_src_dst_type）（本体 P0） |
| 030_sem_views.sql | fuling_ontology | PMC-1 语义投影：sem_packing/sem_stacking 视图（spec↔SKU 走 029 link 非 JSON 关联）+ packing_spec/stacking_spec 发号登记；**S7：本体表族整体在独立库 fuling_ontology（PR-B P0-02），fuling_ro 不授本库——隔离由 DB 授权面强制（tests/test_ontology_db_isolation.py 钉住），唯一读取口 ontology/sem.py** |
| 031_agent_approval_execution_hardening.sql | fuling_operation | 重评报告 P0-C/P0-E/P1-11：approval_decision.final_args_digest（决定绑定最终执行参数摘要，堵改参重放）+ tool_invocation.status 增 uncertain（超时/崩溃副作用不可知→人工对账，不再谎报 failed）+ approver_scope 加宽 160（backup steward CSV）（2026-07-11；已 apply 本地） |
| 033_ontology_link_invariants.sql | fuling_ontology | P1-8/P1-9 link 不变量：active_single_key 生成列 + uk_link_active_single（single 基数 link 型 DB 级拒双活，扩展方式见文件头）+ 三条引用 FK（object.merged_into RESTRICT；identifier.source_case_id / case.resolved_identifier_id 均 ON DELETE SET NULL）；刻意不加 superseded_by FK（repoint 事务序不容）（本体 P1，2026-07-11；已 apply 本地） |
| 034_sem_views_product_acl.sql | fuling_ontology | 重评审计 P0-03：sem_packing/sem_stacking 补投 product_owner_dept/product_classification——product 三字段（id/ref/name）此前只随 spec 侧行过滤键透出，公开 SKU+公开 spec+confidential product 整行泄露；sem.py 出参前按 product 自身密级独立裁决，旧行（无两列）fail-closed 遮蔽（2026-07-11；已 apply 本地） |
| 035_agent_checkpoint_digest_hmac.sql | fuling_operation | 重评审计 P1-2：agent_checkpoint.state_digest CHAR(64)→VARCHAR(80)——摘要从裸 sha256 升级 `hmac1:<hmac_sha256>`（带密钥真实性，能写表者重算 sha256 绕不过）；**须在 HMAC 代码之前 apply**（纯加宽零影响，反序 1406 挂起失败）；静态加密 RAG_AGENT_CHECKPOINT_ENCRYPT 默认 off（2026-07-11；已 apply 本地） |

## 台账（schema_migrations）

两库各一张，记录"本库应用过哪些 schema 文件"。查询某环境落后哪些迁移：

```sql
SELECT filename, version, applied_at, notes FROM schema_migrations ORDER BY version;
```

基线回填（真实应用时间早于台账建立的旧文件）见 011 文件内注释的 INSERT IGNORE 段。

## 留存策略（F-36）

日志/审计类表（qa_session_log、kb_audit_log、document_sensitive_finding、pipeline_run）
的留存与瘦身**不在 DDL 层做**（无分区重建），由 `opensearch_pipeline/retention.py`
批量执行（dry-run 默认、遵守 env_guard 三层守卫），DataWorks 日任务节点
`dataworks_nodes/retention_node.py` 调度。策略与默认窗口见 retention.py 模块 docstring。
