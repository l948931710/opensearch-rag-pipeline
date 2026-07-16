-- ════════════════════════════════════════════════════════════════════════════
-- 042_agent_run_user_index.sql — agent_run 用户维列表复合索引（perf 批次 A）
--
-- @DB fuling_operation（staging 为 fuling_operation_stg）
--
-- 动机：agent_run 按 user_id 过滤的两条热查询均无可用索引——
--   · run_store.list_runs_by_user（运行中心「我的 runs」）:
--       WHERE user_id=%s ORDER BY started_at DESC LIMIT %s   → 全表扫 + filesort
--   · retention.purge_subject（PIPL 主体擦除）:
--       DELETE FROM agent_run WHERE user_id=%s LIMIT %s（子表经 run_id 子查询关联）
--   agent_run 现有索引仅 PRIMARY(run_id) / idx_thread(thread_id,started_at) /
--   idx_status_hb(status,heartbeat_at) / uk_thread_active(active_thread)——
--   无任何以 user_id 打头的索引，表随上线逐 run 单调增长。
--
-- (user_id, started_at)：user_id 等值前缀 + started_at 有序 →
--   list_runs_by_user 走反向索引扫描取前 N（MySQL 8 backward index scan，免 filesort）；
--   purge 的等值过滤走同一前缀。
--
-- ADDITIVE / 幂等：仅加二级索引（InnoDB online DDL，INPLACE 不锁写）。所有查询 fail-open，
--   不依赖索引存在——纯性能加成，无列/无语义变更。
--   ⚠️ GATED PROD DDL——幂等守卫（information_schema.statistics 查重）与 schema_migrations
--   台账 INSERT 写在 scratch/apply_migration_042.py，不内嵌本 DDL 文件（F-35 铁律 1）。
-- ════════════════════════════════════════════════════════════════════════════

-- 幂等守卫在 apply 脚本里（information_schema.statistics 查重）；MySQL 不支持
-- CREATE INDEX IF NOT EXISTS，故裸建（重复执行由 apply 脚本拦截）：
CREATE INDEX idx_user_started ON agent_run (user_id, started_at);

-- 回滚：
-- DROP INDEX idx_user_started ON agent_run;
