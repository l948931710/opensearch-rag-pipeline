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
3. **编号严格单调递增，且跨分支全局分配**（Majors ε3 纠偏 2026-07-22：本注长期停在
   022 已过时）——`claude/ontology-p0` 与 main 共用同一号池，agent/ontology 域文件
   （022-031/033-038/042-048/052-058）只存在于分支侧，main 只落共享/主域号（032/039/
   040/041/049/050/051）。**下一个可用号 = 059**，取号前先查两侧 `schema/` 目录与分支
   README。历史上有三对编号冲突（002/003/006 各两个文件，见下表）——**不改名**（外部
   引用会悬空），台账里用 `002b/003b/006b` 区分，新文件绝不再冲突。
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
| 002_feedback_system.sql | fuling_operation | user_feedback、escalation_ticket（转人工已下线 2026-07：表保留存量数据，不再有写入端）；qa_session_log 现行定义（含 message_id/延迟列） |
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
| 011_schema_migrations.sql | 双库 | DDL 变更台账（本机制自身） |
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
| 032_schema_migrations_checksum.sql | 双库 | schema_migrations.checksum 列（评审F9：同名异 checksum 漂移检测激活——apply_migration.py 的 _ledger_conflict 无此列恒惰化）。**逐字节拷自 claude/ontology-p0**（跨分支同 sha256，台账/漂移检测两分支一致）；information_schema+PREPARE 守卫幂等；编号 032 沿用分支取号（22-31 为分支侧历史空洞，勿复用） |
| 039_qa_question_hash.sql | fuling_operation | 缺口语义去重 Layer-1（pmc 远期立项，2026-07-15）：qa_session_log.question_hash 归一化哈希列 + 索引——写侧 qa_logger 落列（对脱敏后文本，contribution.question_hash 同口径；1054 负缓存降级，**代码可先行**）；存量回填=scripts/backfill_qa_question_hash.py |
| 040_qa_gap_semantic_group.sql | fuling_operation | 缺口语义去重 Layer-2：qa_gap_semantic_group 相似问法语义组映射表——生成=scripts/build_qa_gap_semantic_groups.py（embedding 贪心归组，阈值 0.90 保守）；读侧 RAG_QA_GAP_SEMANTIC 默认关且 fail-open；**预注册边界：仅展示层归并，绝不驱动缺口自动关闭**（022-038 agent/ontology 表族条目随 ontology-p0 大合并） |
| 041_qa_gap_dismissal.sql | fuling_operation | 「忽略此缺口」台账（ε-4 遗留，2026-07-15 拍板交 dept_admin）：question_hash 主键 + revoked_at 可撤销留痕——dismiss/restore 端点写入（语义组开时联动全组成员），kb_gaps 读侧排除 active 行（fail-open）；员工 403 |
| 050_qa_rewritten_query.sql | fuling_operation | 多轮追问检索前改写（RAG_FOLLOWUP_REWRITE 默认关，2026-07-18）：qa_session_log.rewritten_query 存改写后的独立问题（脱敏后）——写侧 qa_logger 落列（1054 **TTL** 负缓存降级，apply 后无须重启恢复；改写行 question_hash 改按改写后文本计算）；读侧 kb_gaps 对本列 1054 回退无列 SQL（**代码可先行**）；无存量 backfill |
| 051_dingtalk_msg_dedup.sql | fuling_operation | 钉钉消息 msgId 去重 RDS 兜底（B7-P2-04 四态机，2026-07-18）：msg_id 主键+state/attempts/message_id——主层（memory/redis）失效时的第二层幂等；写侧 dingtalk_bot（1146 负缓存 1h 降级，**先部署后 apply 安全**）；kill switch RAG_MSG_DEDUP_RDS_FALLBACK（默认开、simulate 关）；无存量 backfill |

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
