-- ════════════════════════════════════════════════════════════════════════════
-- 049_acl_outbox_generation.sql — 批次8（ultra schema/009:34）：投影 outbox 代次列
--
-- @DB fuling_knowledge（staging 为 fuling_knowledge_stg）
--
-- 动机（ultra 全仓评审 2026-07-17，P2 security）：kb_acl_projection_outbox 一行一 doc、
--   ON DUPLICATE 复活、**无代次列**——drain 的 done 标记是无条件 `WHERE id=%s`：
--   T1 drain 按权威物化投影 → T2 撤销落库并复活同一行（done_at=NULL）→ T3 drain 的
--   done 标记把 T2 的复活**擦掉**——撤销意图静默丢失，HA3 残留陈旧 allowed_depts 直到
--   capped 全扫 reconcile 兜到（读侧 fail-closed 复核挡住了泄露，残留只在索引面）。
--
-- 语义：
--   · generation：每次 enqueue/复活 +1（ON DUPLICATE KEY UPDATE generation=generation+1）；
--   · drain SELECT 时带走 generation，done/attempts 标记 CAS
--     `WHERE id=%s AND generation=%s`——mid-drain 复活（generation 已 +1）时 CAS 落空，
--     行保持待处理，下轮按新代次重新物化（撤销必达恢复）。
--   · 代码侧（access_grants.py）双路径兼容：本迁移未 apply 时（1054 Unknown column）
--     自动回退旧 SQL——可先部署代码后 apply（🔒 apply user-gated）。
--
-- ADDITIVE：单列 ALTER（DEFAULT 0，存量行零回填）。非幂等 DDL：重复执行由
--   scripts/apply_migration.py 的 information_schema 预检跳过（同 031/048 约定）。
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE kb_acl_projection_outbox
  ADD COLUMN generation BIGINT UNSIGNED NOT NULL DEFAULT 0
    COMMENT '批次8 代次：每次 enqueue/复活 +1；drain 的 done/attempts 标记按 (id, generation) CAS，mid-drain 复活的撤销不再被擦掉'
    AFTER attempts;
