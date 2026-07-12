-- schema/031_agent_approval_execution_hardening.sql
-- 库：fuling_operation
-- 2026-07-10 重评报告 P0-C / P0-E / P1 修复的 schema 侧（复核备忘 docs/audits/agent_worktree_reeval_verification_2026-07-11.md）：
--
-- ① P0-C「edited decision 未绑定」：approval_decision 增 final_args_digest——决定行绑定
--    **最终执行参数**的摘要（approved=原 args_digest；edited=人工改后参数按原文算的 sha256，
--    digest 本身 PII-safe）。已决重放（/approve 对非 pending 请求的重试）必须携带与该摘要
--    完全一致的参数，否则 409——堵住「DB 批 qty=1、重放执行 qty=999999」。
--
-- ② P0-E「stale executing 无对账」：tool_invocation.status 增 **uncertain** 终态——
--    超时/进程崩溃后副作用不可知的调用不得静默判 failed（盲目重试=重复副作用）。
--    uncertain 行阻断同幂等键自动重试，走 /api/agent/invocations 人工对账
--    （确认已生效→succeeded / 确认未生效→failed）后方可重发。
--
-- ③ P1-11「backup steward 未生效」：approver_scope 加宽 64→160——stewardship scope 带
--    backup_dept 时审批路由键写 CSV（"steward,backup"），决策侧按 CSV 拆分裁决。
--
-- 注：本文件为 ALTER 迁移（非幂等 DDL）——重复执行时 ①/③ 是 no-op 语义但 MySQL 会对
-- 已存在列报 1060，由 scripts/apply_migration.py 的 information_schema 幂等预检跳过。

ALTER TABLE approval_decision
  ADD COLUMN final_args_digest CHAR(64) NULL COMMENT '最终执行参数摘要（approved=原digest；edited=改后参数sha256）' AFTER edited_args_json;

ALTER TABLE approval_request
  MODIFY COLUMN approver_scope VARCHAR(160) NOT NULL COMMENT '审批人裁决键；含backup steward时为CSV（steward,backup）';

ALTER TABLE tool_invocation
  MODIFY COLUMN status ENUM('proposed','denied','pending_approval','executing',
                            'succeeded','failed','compensated','uncertain') NOT NULL;
