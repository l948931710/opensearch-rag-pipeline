-- ════════════════════════════════════════════════════════════════════════════
-- 044_agent_dispatch_resume.sql — PR-3 Stage B：dispatch 命令 kind 扩 'resume'
--
-- @DB fuling_operation（staging 为 fuling_operation_stg）
--
-- 动机（docs/pr3_durable_worker_design_2026-07-17_DRAFT.md §3 Stage B）：
--   审批决定（approval_decision）与 resume 命令此前是两套恢复机制——decide 提交后
--   进程崩溃靠 B6 周期对账扫描（list_decided_unresumed）兜底。Stage B 让 /approve 的
--   decide 事务**同事务**写入一条 kind='resume' 的命令行：决定 commit ⇒ 命令必然
--   durable，恢复从「周期扫描推断」升级为「命令消费」（lease/attempts 纪律与 submit
--   同款）。B6 对账保留为兜底（覆盖 Stage B 之前的历史决定与 edited 人工流）。
--
-- resume 命令语义与 submit 的差异（dispatch_outbox/durable_dispatcher kind-aware）：
--   · run_id 在 enqueue 时即有值（要续跑的 run）——「已绑 run 的过期命令收口 done」
--     只适用于 kind='submit'；resume 命令按 run 现状收敛（suspended→重驱 /
--     resuming、running→等 / 终态→done）。重驱天然幂等（CAS suspended→resuming
--     first-claim-wins），at-least-once 安全。
--   · edited 决定**有意不入队**（库存参数已脱敏不可自动重驱，P1-07 拍板不变）。
--
-- 变更：ENUM 尾部追加值（MySQL 8 ALGORITHM=INSTANT，不锁表不重建）。
-- ADDITIVE / 幂等：重复执行报 1060/无变化均无害；flag（RAG_AGENT_DURABLE_DISPATCH，
--   默认 off）关闭时代码不写 resume 命令——可先 apply 后部署。
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE agent_dispatch_command
  MODIFY COLUMN kind ENUM('submit','resume') NOT NULL DEFAULT 'submit'
  COMMENT 'submit=ask 受理命令；resume=审批决定同事务命令（Stage B）';
