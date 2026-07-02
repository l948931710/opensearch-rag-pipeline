-- ════════════════════════════════════════════════════════════════════════════
-- 015_kb_audit_log_history_index.sql — kb_audit_log 审批历史复合索引（perf#83/#96）
--
-- @DB fuling_knowledge（staging 为 fuling_knowledge_stg）
--
-- 动机：console 审批历史两条查询（routes/kb_access.py:476 / :496）形态均为
--   WHERE operator_type='user' AND action_type IN (...) ORDER BY created_at DESC LIMIT N
-- 而 kb_audit_log 现仅有 idx_doc_id（001:258 附近）——两条查询全表扫 + filesort，
-- 且该表由管线逐日 append-only 写入、无留存清理，退化随时间线性恶化。
--
-- 复合索引 (operator_type, action_type, created_at)：
--   · operator_type 等值 + action_type IN 区间 + created_at 有序 → 索引倒序扫描取前 N，
--     两条查询（APPROVE/REJECT 与 KB_ADMIN_GRANT/REVOKE）同一索引全覆盖谓词与排序；
--   · 同时兼容 perf#83 提议的 (action_type, created_at) 场景（operator_type 恒为等值前缀）。
--
-- ADDITIVE / 幂等：纯加索引（InnoDB online DDL），无数据变更，查询 fail-open 不依赖索引存在。
-- ✅ APPLIED 2026-07-02（生产 fuling_knowledge，scratch/apply_migration_007_015.py，
--    schema_migrations v015；SHOW INDEX 三列序 operator_type→action_type→created_at 已确认）。
-- ════════════════════════════════════════════════════════════════════════════

-- 幂等守卫（沿用 002 的 _add_index_if_not_exists 模式；裸 SQL 版本如下）：
CREATE INDEX idx_operator_action_created ON kb_audit_log (operator_type, action_type, created_at);

-- 回滚：
-- DROP INDEX idx_operator_action_created ON kb_audit_log;
