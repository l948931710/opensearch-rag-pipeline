-- ════════════════════════════════════════════════════════════════════════════
-- 046_tool_invocation_operation_id.sql — PR-3 Stage C：invocation 行的对账资格章
--
-- @DB fuling_operation（staging 为 fuling_operation_stg）
--
-- 动机：自动对账（operation_reconciler）把「台账（agent_tool_operation，schema/045）
--   无行」读作 not_applied → 放行重试。该推断只对**确实拿到 operation_id 的执行**成立
--   ——Stage C 部署前遗留的 uncertain 行、未注入 ctx 的执行，台账本来就不会有行，
--   按无行判 failed 是误判（副作用可能已发生）。本列是资格章：executor 对副作用工具
--   注入 ctx.operation_id **成功后**回填（值=invocation_id 自身），对账扫描只认
--   `operation_id IS NOT NULL` 的行；老行/未注入执行恒 NULL → 永留人工通道。
--
-- 语义：
--   · 值恒 = invocation_id（同源；列存在的意义是「何时注入过」，不是第二个键）；
--   · 盖章失败 fail-open（少个章=少条自动化，方向保守——绝不因盖章挡执行）。
--
-- ADDITIVE：单列 ALTER（NULL 默认，存量行零回填）。未部署新代码时列恒 NULL、
--   零读写——可先 apply 后部署。非幂等 DDL：重复执行由 scripts/apply_migration.py
--   的 information_schema 预检跳过（同 031 约定）。
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE tool_invocation
  ADD COLUMN operation_id CHAR(32) NULL
    COMMENT 'Stage C 对账资格章：executor 注入成功后=invocation_id；NULL=不进自动对账'
    AFTER approval_request_id;
