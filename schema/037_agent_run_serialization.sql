-- schema/037_agent_run_serialization.sql
-- 库：fuling_operation
-- A1 per-thread run 串行化（审计复核批次1，docs/reverify_fix_batches_2026-07-12.md）：
--   同一 thread 同时只允许一个非终态 run（running/suspended/resuming）——此前同会话
--   并发双 submit 各建一个 run 并行跑，会话记忆双写乱序、预算双烧。
-- MySQL 不支持部分唯一索引 → 生成列占坑：非终态行 active_thread=thread_id、
--   终态行 NULL（MySQL UNIQUE 允许多 NULL，终态行不占坑）。并发双 INSERT 恰一个 1062
--   → run_store.create_run 识别 uk_thread_active 撞键抛 ThreadBusy → 路由层 409
--   「该会话已有回答在进行中」。语义拍板：suspended（等审批）同样占坑——non-terminal
--   一律互斥，防挂起期间同会话再起 run 分叉记忆。
-- 宽度 160 对齐 thread_id VARCHAR(160)（utf8mb4 640 字节 < 3072 索引上限）。
-- ⚠️ 部署顺序：可先 apply 后部署（列为生成列，代码不读写它；未迁移库上新代码的
--   1062 兜底路径自然不触发=回到修复前行为）；但 RAG_AGENT_ENABLE 灰度开闸前必须已 apply。
-- ⚠️ apply 前置：存量若有同 thread 多条非终态残行，ADD UNIQUE 会失败——先跑 reaper
--   收尸（reap_stale_runs）或人工核对：
--   SELECT thread_id, COUNT(*) FROM agent_run
--     WHERE status IN ('running','suspended','resuming')
--     GROUP BY thread_id HAVING COUNT(*) > 1;

ALTER TABLE agent_run
  ADD COLUMN active_thread VARCHAR(160)
    GENERATED ALWAYS AS (IF(status IN ('running','suspended','resuming'), thread_id, NULL))
    VIRTUAL
    COMMENT '非终态 run 的 thread 占坑生成列（037 A1 串行化；终态=NULL 不占坑）',
  ADD UNIQUE KEY uk_thread_active (active_thread);
