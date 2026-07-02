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
3. **编号严格单调递增**，下一个可用号 = 012。历史上有三对编号冲突（002/003/006 各两个文件，
   见下表）——**不改名**（外部引用会悬空），台账里用 `002b/003b/006b` 区分，新文件绝不再冲突。
4. **`CREATE DATABASE` 必须显式 `CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci`**，每张新表
   显式 COLLATE —— staging `_stg` 库曾因缺省漂移到 `_0900_ai_ci` 引发跨库 JOIN 1267。
5. DDL↔代码列契约由 `tests/test_schema_ddl_parity.py` 钉住（INSERT/SELECT 用到的列
   必须存在于权威 DDL）——新增读写列时同步改 schema 文件，否则测试红。

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
| 011_schema_migrations.sql | 双库 | DDL 变更台账（本机制自身） |
| 012_qa_session_log_perf_index.sql | fuling_operation | (answer_status, created_at) 复合索引（性能第一梯队 #1） |

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
