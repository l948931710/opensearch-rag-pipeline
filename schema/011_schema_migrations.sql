-- ════════════════════════════════════════════════════════════════════════════
-- 011_schema_migrations.sql — DDL 变更台账（F-35，2026-07-01）
--
-- 背景：此前 schema/ 文件靠人肉纪律应用到生产（scratch/apply_migration_NNN.py 一次性
-- 脚本），没有任何"哪个库应用过哪个文件"的账——010 漂移（生产有 normalized_gap_query
-- 而权威 DDL 没有）正是这么漏出来的。本表把"已应用"变成可查询事实。
--
-- ADDITIVE / IDEMPOTENT：仅新建一张表 + INSERT IGNORE 基线回填；不改任何既有表。
--
-- ⚠️ 两库各建一张（fuling_knowledge 与 fuling_operation 各自记录自己收到的 DDL）：
--    USE fuling_knowledge; 后执行一遍，再 USE fuling_operation; 执行一遍。
--    staging（*_stg 库）同样各执行一遍。
--
-- 规则（详见 schema/README.md）：
--   1. schema/ 目录是唯一 DDL 权威；任何直连生产改表必须先改 schema/ 文件。
--   2. 每次对某库应用一个 schema 文件，必须在【同一会话】向该库 schema_migrations
--      INSERT 一行（apply_migration_NNN.py 模板已含此步）。
--   3. 修订已发布文件（如本次 010 补列）记 'NNNa' 修订号，不改原行。
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename     VARCHAR(255) NOT NULL COMMENT 'schema/ 下的权威 DDL 文件名（含修订记号，如 010_kb_contribution.sql@010a）',
    version      VARCHAR(16)  NOT NULL COMMENT '排序用迁移号：文件名数字前缀 + 可选修订字母（001…011、010a）',
    applied_at   DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '应用（或基线回填）时刻',
    applied_by   VARCHAR(128) DEFAULT NULL COMMENT '操作人/工具，如 laijunchen / scratch/apply_migration_011.py',
    notes        VARCHAR(512) DEFAULT NULL,
    PRIMARY KEY (filename)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='DDL 变更台账：本库应用过的 schema/ 文件（F-35）';

-- ── 基线回填（应用本文件时按目标库二选一执行；applied_at=回填时刻，真实应用早于台账建立）──

-- @DB fuling_knowledge 执行这一段：
-- INSERT IGNORE INTO schema_migrations (filename, version, applied_by, notes) VALUES
--   ('001_opensearch_pipeline.sql',    '001',  'baseline', '基线回填：台账建立前已在生产'),
--   ('002_step_card_enhancement.sql',  '002b', 'baseline', '基线回填；与 002_feedback_system 编号冲突，见 README'),
--   ('003_provenance_lineage.sql',     '003',  'baseline', '基线回填'),
--   ('003_user_role_unique.sql',       '003b', 'baseline', '基线回填；编号冲突，见 README'),
--   ('004_observability_metrics.sql',  '004',  'baseline', '基线回填：仅 pipeline_run ALTER 段属本库'),
--   ('005_cross_doc_dedup_index.sql',  '005',  'baseline', '基线回填：idx_canonical_sha256 已应用（2026-06-22）'),
--   ('006_kb_admin_authz.sql',         '006b', 'baseline', '基线回填；编号冲突，见 README'),
--   ('007_kb_etag_dedup_index.sql',    '007',  'baseline', '基线回填'),
--   ('008_kb_access_request.sql',      '008',  'baseline', '基线回填（collation 对齐 _unicode_ci 修复含在文件内）'),
--   ('009_acl_projection_outbox.sql',  '009',  'baseline', '基线回填'),
--   ('011_schema_migrations.sql',      '011',  'apply',    '台账自身');

-- @DB fuling_operation 执行这一段：
-- INSERT IGNORE INTO schema_migrations (filename, version, applied_by, notes) VALUES
--   ('002_feedback_system.sql',        '002',  'baseline', '基线回填：台账建立前已在生产'),
--   ('004_observability_metrics.sql',  '004',  'baseline', '基线回填：仅 qa_daily_metrics 段属本库'),
--   ('006_conversation_history.sql',   '006',  'baseline', '基线回填（flag RAG_CONVERSATION_HISTORY 默认 OFF）'),
--   ('010_kb_contribution.sql',        '010',  'baseline', '基线回填：经 scratch/apply_migration_010.py 应用'),
--   ('010_kb_contribution.sql@010a',   '010a', 'apply',    'F-35 修订：权威 DDL 补 normalized_gap_query（生产表自始有该列，无 DDL 动作，纯对齐）'),
--   ('011_schema_migrations.sql',      '011',  'apply',    '台账自身');
