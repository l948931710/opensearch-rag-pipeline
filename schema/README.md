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
   （022-031/033-038/042-047/052-058）只存在于分支侧，main 只落共享/主域号（032/039/
   040/041/048/049/050/051/059/060/061/062）。048 于 2026-08-02 随 PR-4 租约移植回 main
   （文件与分支侧逐字节一致，checksum 台账不变）。**下一个可用号 = 064**，取号前先查两侧 `schema/` 目录与分支
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
| 048_ingest_lease.sql | fuling_knowledge | PR-4 摄取台账租约/栅栏（2026-07-17 立项，2026-08-02 随移植回 main；**三环境已 apply**）：document_version 加 lease_holder/lease_expires_at/lease_epoch + idx_lease_expiry——认领带租约、运行中续租、终态/破坏性写带 fencing 谓词；协议在 opensearch_pipeline/ingest_lease.py，总闸 RAG_INGEST_LEASE_ENABLE **默认关**（关=代码不读写本组列，先 apply 后部署与先部署后 apply 均安全）；头注引用的 schema/043 是分支侧历史类比（main 无该文件，**勿改原文**——checksum 台账约束）；启用前置 runbook 见 docs/ingest_lease_fencing_scope_2026-07-17.md 附录 |
| 050_qa_rewritten_query.sql | fuling_operation | 多轮追问检索前改写（RAG_FOLLOWUP_REWRITE 默认关，2026-07-18）：qa_session_log.rewritten_query 存改写后的独立问题（脱敏后）——写侧 qa_logger 落列（1054 **TTL** 负缓存降级，apply 后无须重启恢复；改写行 question_hash 改按改写后文本计算）；读侧 kb_gaps 对本列 1054 回退无列 SQL（**代码可先行**）；无存量 backfill |
| 051_dingtalk_msg_dedup.sql | fuling_operation | 钉钉消息 msgId 去重 RDS 兜底（B7-P2-04 四态机，2026-07-18）：msg_id 主键+state/attempts/message_id——主层（memory/redis）失效时的第二层幂等；写侧 dingtalk_bot（1146 负缓存 1h 降级，**先部署后 apply 安全**）；kill switch RAG_MSG_DEDUP_RDS_FALLBACK（默认开、simulate 关）；无存量 backfill |
| 059_image_funnel_verdict.sql | fuling_knowledge | 图片漏斗判决记录（选项 E，2026-07-26）：内容寻址主键 (image_sha256, namespace, funnel_policy_version)——把判决从可淘汰的 VLM 缓存升级为**不淘汰、显式失效**的记录，让已判过的图免疫 VLM 制度漂移（实测 6.5% 判决单向翻 DISCARD，4 张是 GT 期望的步骤配图）；**载荷刻意不含 ocr_text/visual_summary**（派生内容可能含 PII，留在既有缓存那个已存在的暴露面，不复制进 RDS 新查询面）；读写侧 extraction/unified_extractor（1146 → 恒 UNAVAILABLE 降级为纯缓存行为，**先部署后 apply 安全**）；flag RAG_FUNNEL_VERDICT_STORE 默认关且与 RAG_VLM_DOC_CONTEXT 互斥；无存量 backfill |
| 060_node_acl.sql | fuling_knowledge | node-ACL 阶段 A（2026-07-29 已 apply staging+**生产**）：document_meta 加 acl_mode/owner_dept_id/acl_revision + idx_acl_mode_owner_node；新表 kb_doc_node_grant（+scope subtree\|exact）/dept_admin_node_grant/dept_dim/staff_dim；存量全 legacy 行为逐字节不变；读写侧 access_grants/acl_policy（information_schema 探测降级，**先部署后 apply 安全**）|
| 061_node_owner_axis.sql | fuling_knowledge | node-ACL 阶段 B 归属轴（2026-08-01，codex 4 轮 APPROVE；**同日已 apply staging+生产**，台账 checksum fb9b03ed）：kb_doc_meta_projection_outbox（doc-meta 改标题/分类的持久投影意图，generation CAS 防 stage-3 loader 窗口丢更新——语义与 049 逐字一致）+ dept_admin_node_candidate（管辖根自动派生候选，与权威表分离防静默提权）+ acl_revision 注释扩展为「文档管理面编辑 CAS」；读写侧 routes/kb_console(doc-meta)/dataworks_orchestrator(pre-drain)/org_sync(派生)；1054 负缓存回退，**先部署后 apply 安全** |
| 062_acl_projection_epoch.sql | fuling_knowledge | **2026-08-03 已 apply staging+生产**（Sam 当日授权 `PROD-RW:2026-08-03`；台账 checksum d98971f6；staging 二次 apply 验幂等 exit 0、列不重复；生产 MySQL 8.0.36 尾部加列走 INSTANT，chunk_meta 63882 行零改写）。⚠️ **列已就位但代码侧零消费**——bump/stamp 写方与 sweep 仍待拍板单（等 Sam 勾 `docs/ops/c3prime_acl_projection_convergence_signoff_2026-08-03.md`；codex 两轮 REVISE 后修订）：C3/C3′ ACL 投影收敛——**只两列** `document_meta.acl_epoch`（投影失效代次，单调只增）+ `chunk_meta.acl_epoch`（投影水位，NULL=从未投影过）。修的是「diff 在无上次结果时恒为空 ⇒ 从未投影过的 node 文档永远判 unchanged」这一**结构性**缺陷（Kendra/Coveo/Azure/Elastic/OpenText 五系统收敛结论）。**认证输出严格 = project_doc_acl 产出的 (owner_dept, allowed_depts)；permission_level 不在【认证输出】内**（其同步归 C9）——⚠️ 但有效的每版本 permission 是 allowed_depts 的 **gate 输入，其变化仍须 bump**（否则 flag 关时改级别、开 flag 后 epoch 相等致授权永不投影）；epoch 只证明 RDS 投影、**不证明 HA3 已发布**（后者是 index_status）。**刻意不复用 acl_revision**（管理面编辑 CAS，含 title/category）。v1 的 `acl_state` 与 `idx_acl_projection_state` **已删**（三态皆可由 epoch 推导且不防"忘记 bump"；索引待生产 EXPLAIN 后另取号）。上线次序硬约束：**DDL 先 apply → 再部署 bump/stamp 写方（含 stage-2 node_write_chunk_meta，漏则新 chunk 天生 NULL 永久 dirty）→ 全版本 sweep 必须与 C3′ 多版本 materializer 同批启用**；bump **不得受 RAG_ALLOWED_DEPTS_ACL 门控**。回填走 certify-only / projection-changed 二分（前者只 stamp 不动 index_status），**不是**全量重推。**必须 apply-before-enable**（缺列 1054 直接报错不降级，与 048 不同）；本地 scratch 库实测幂等 ×2 |
| 063_visibility_intent.sql | fuling_knowledge | **未 apply**。C9 方案 B′（Sam 2026-08-03 拍板）：`document_version.permission_override` —— nullable **canonical 权限值**（非布尔位），NULL=无显式意图走 raw_key 路径解析（历史行为逐字节不变），非 NULL=管理员显式声明、stage-2 loader 优先采用（命中 `resolve_permission_level` 优先级 1）。修的是「set-visibility 改完被 stage-2 按 raw_key 覆盖回写」——机械根因是**两个写方锁不相交的行**（端点只锁 document_meta、stage-2 只锁 document_version 的 `FOR UPDATE OF dv SKIP LOCKED`），故本批同时给端点补当前版本行 `FOR UPDATE`，两者才真互斥。**只由显式管理员动作写**——绝不能反过来让 RDS 全局赢（stage-1 自动注册 INSERT 不含 permission_level 列、落默认 'public'，DataWorks 批量注册硬编码 'public'，全局让 RDS 赢会确定性破坏 raw/.../internal/ 与 restricted）。读写两侧**均带 capability 探测**，先部署后 apply 安全（与 048/049/050/062 同款；⚠️ 与 062 不同，本列缺失是**降级跳过**而非报错） |
| 064_content_binding.sql | fuling_knowledge | ✅ **2026-08-04 已 apply 生产**（PROD-RW:2026-08-04，apply_migration.py；3070 行存量全 LEGACY_UNBOUND、raw_version_id 全 NULL ⇒ 行为不变）；staging 未 apply。C8 审批内容绑定（Sam 2026-08-04 拍板 version-id 固化）：`document_version.raw_version_id` + `content_binding_mode`。修的是**签名 PUT URL 的 TOCTOU** —— put_url 与 upload token 共用 30min TTL 且**预签名 URL 服务端无法逐个撤销**，于是「上传 A → 审批放行 → 同一 url 重 PUT B → 摄取 B」。⚠️ 裸 ETag 复核**不够**（register 存 ETag(B)、审批前 PUT A 给人看、审批后 PUT B 过 If-Match），必须**同时**固定「审批预览读的」与「摄取读的」两处身份 —— version-id 天然满足（重 PUT 产生新 version）。`content_binding_mode` 是**三态显式契约**（codex BLOCKER）：只靠 `raw_version_id IS NULL` 分不出「存量」与「新写失败」，会让绑定静默退化。默认 `LEGACY_UNBOUND` ⇒ apply 后、flag 开前逐字节不变行为（先部署后 apply 安全）|

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
