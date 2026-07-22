-- 055_agent_step_bookkeeping.sql — 记账幂等键(Majors α2/M3.2,2026-07-21)
-- 库：fuling_operation
--
-- 背景(07-21 评审 M3.2,codex 共识方案 docs/majors_remediation_plan_2026-07-21_DRAFT.md)：
--   record_turn/record_tool_call 合并事务 commit-ACK 丢失时,executor 现行回退分段写
--   step/budget/llm 行——若原事务实际已提交,即双计(step 双行、预算虚胖、llm 行重复)。
--   本列是「事务是否已落」的可判定标记：executor 在任何 DB 写前生成 bookkeeping_id
--   传入合并事务(随 agent_step INSERT 落库)；commit 异常 → 同键新连接整事务重放×1,
--   撞 uk_step_bookkeeping 1062 = 原事务已落 → 幂等成功；重放仍失败 → 放弃全部 durable
--   回退写(本地计数权威,durable 欠账由 suspend_run_atomic 的 GREATEST 同事务纠偏)。
--
-- 语义：
--   · NULL=旧行/未启用路径,唯一索引对 NULL 不约束——存量零影响；
--   · 代码可先行：写侧对 1054(错误文本含 bookkeeping_id 才触发)warn-once 回退 legacy
--     分段路径——启用幂等记账语义前**建议先 apply 本文件**(否则 M3.2 双计缺口仍在)。
--
-- 非幂等 DDL：重复执行由 scripts/apply_migration.py 的 information_schema 预检跳过。

ALTER TABLE agent_step
  ADD COLUMN bookkeeping_id CHAR(32) NULL
    COMMENT '记账幂等键(055/M3.2)：executor 每 model-turn/tool-call 预生成;重放撞唯一键=已落';

CREATE UNIQUE INDEX uk_step_bookkeeping ON agent_step (bookkeeping_id);
